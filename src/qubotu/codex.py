"""JSON-RPC client for `codex app-server` over stdio."""

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable

from . import config
from .version import current_version

log = logging.getLogger(__name__)

ToolHandler = Callable[[dict], Awaitable[tuple[bool, str]]]
NotificationHandler = Callable[[str, dict], None]


# The permission profile agent threads run under. Agents run commands as the user, and Codex's
# default sandbox only limits writes, so without this an agent (or a web page it reads) could read
# ~/.qubotu, ~/.codex/auth.json, SSH keys, and the Keychain. Agents can read the system, write their
# own working folder (the thread's workspace root) and temp files, and reach the network; the
# home folder and Qubotu's data folder (which may live outside it) are denied, except ~/.gitconfig,
# which git needs to start.
AGENT_PROFILE = "qubotu-agent"


def agent_filesystem() -> str:
    entries = {
        ":root": "read",
        "~": "deny",
        str(config.HOME): "deny",
        "~/.gitconfig": "read",
        ":tmpdir": "write",
    }
    pairs = [f"{json.dumps(path)}={json.dumps(access)}" for path, access in entries.items()]
    # The workspace root is more specific than the denied folders around it, so it stays writable.
    pairs.append('":workspace_roots"={"."="write"}')
    return "{" + ",".join(pairs) + "}"


class CodexError(Exception):
    pass


class CodexTimeout(CodexError):
    pass


class _Connection:
    """One app-server process. Requests, replies, and tool-call tasks all belong to it, so nothing
    from an old process can leak into its replacement."""

    def __init__(self, proc: asyncio.subprocess.Process, generation: int, client: "CodexClient"):
        self.proc = proc
        self.generation = generation
        self.client = client
        self.closing = False
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    @property
    def alive(self) -> bool:
        return not self.closing and self.proc.returncode is None

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.send({"id": rid, "method": method, "params": params})
            msg = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise CodexTimeout(f"Codex didn't answer {method} within {timeout:g} seconds.") from None
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise CodexError(msg["error"].get("message", str(msg["error"])))
        return msg.get("result") or {}

    async def send(self, obj: dict) -> None:
        if not self.alive:
            raise CodexError(self.client.last_error or "Codex app-server is not running.")
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            await self.proc.stdin.drain()
        except (OSError, RuntimeError) as e:
            raise CodexError(f"Lost the connection to Codex: {e}") from e

    async def close(self) -> None:
        self.closing = True
        tasks = [*self._tasks, *self._readers]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 5)
            except Exception:
                self.proc.kill()
                await self.proc.wait()
        self._fail_pending("Codex app-server stopped.")

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"error": {"message": reason}})
        self._pending.clear()

    def _handle_line(self, line: bytes) -> None:
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise ValueError("not a JSON object")
        method = msg.get("method")
        if method is None and "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut and not fut.done():
                fut.set_result(msg)
        elif method is not None and "id" in msg:
            task = asyncio.create_task(self._handle_server_request(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif method is not None:
            self.client.on_notification(method, msg.get("params") or {})

    async def _read_stdout(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    self._handle_line(line)
                except Exception:
                    log.exception("codex: could not handle line %r", line[:200])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("codex: stdout reader failed")
        finally:
            if not self.closing:
                # The process died or its output became unreadable: treat both as an exit.
                self.closing = True
                self.client.connection_lost(self)
                self._fail_pending(self.client.last_error or "Codex exited.")

    async def _read_stderr(self) -> None:
        # Keep draining even after an odd line, or Codex could block on a full pipe.
        while True:
            try:
                line = await self.proc.stderr.readline()
            except ValueError:
                continue
            if not line:
                break
            log.debug("codex stderr: %s", line.decode(errors="replace").rstrip())

    async def _handle_server_request(self, msg: dict) -> None:
        method, rid, params = msg["method"], msg["id"], msg.get("params") or {}
        if method == "item/tool/call":
            try:
                ok, text = await self.client.on_tool_call(params)
            except Exception as e:
                log.exception("tool call failed")
                ok, text = False, f"Tool error: {e}"
            reply = {"id": rid, "result": {"success": ok, "contentItems": [{"type": "inputText", "text": text}]}}
        elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            # Agents run with approvalPolicy "never", so these should not arrive. Decline to be safe.
            reply = {"id": rid, "result": {"decision": "decline"}}
        else:
            reply = {"id": rid, "error": {"code": -32601, "message": f"Qubotu does not handle {method}"}}
        try:
            await self.send(reply)
        except CodexError:
            pass


class CodexClient:
    """One long-lived app-server process shared by all agents.

    Notifications are passed to `on_notification`. Dynamic tool calls (`item/tool/call`) are passed
    to `on_tool_call`, whose (success, text) result is sent back to the same process.
    """

    def __init__(self, on_notification: NotificationHandler, on_tool_call: ToolHandler):
        self.on_notification = on_notification
        self.on_tool_call = on_tool_call
        self.env_extra: dict[str, str] = {}
        self.last_error: str | None = None
        self.executable = "codex"
        self._conn: _Connection | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._stopped = False

    @property
    def running(self) -> bool:
        return self._conn is not None and self._conn.alive

    @property
    def generation(self) -> int:
        """Bumped on every (re)start, so callers know to re-resume threads."""
        return self._conn.generation if self._conn else 0

    def _args(self) -> list[str]:
        fw = "model_providers.fireworks"
        profile = f"permissions.{AGENT_PROFILE}"
        return [
            "app-server",
            "-c", f'{fw}.name="Fireworks"',
            "-c", f'{fw}.base_url="{config.FIREWORKS_BASE_URL}"',
            "-c", f'{fw}.env_key="FIREWORKS_API_KEY"',
            "-c", f'{fw}.wire_api="responses"',
            "-c", f"{profile}.filesystem={agent_filesystem()}",
            "-c", f"{profile}.network.enabled=true",
            # Codex requires a default once custom profiles exist; Qubotu's threads name theirs.
            "-c", 'default_permissions=":workspace"',
        ]

    async def ensure_started(self) -> None:
        await self._connection()

    async def _connection(self) -> _Connection:
        conn = self._conn
        if conn and conn.alive:
            return conn
        async with self._lock:
            if self._stopped:
                raise CodexError("Qubotu is shutting down.")
            if self._conn and self._conn.alive:
                return self._conn
            if self._conn:
                await self._conn.close()
                self._conn = None
            self._conn = await self._open()
            return self._conn

    async def _open(self) -> _Connection:
        """Start a process and initialize it. It's only published to callers once it's ready."""
        exe = shutil.which(self.executable)
        if not exe:
            self.last_error = "The codex CLI was not found on PATH."
            raise CodexError(self.last_error)
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *self._args(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.env_extra},
                limit=1 << 26,
                # Keep Ctrl-C in the terminal from reaching Codex directly; Qubotu stops it on shutdown.
                start_new_session=True,
            )
        except OSError as e:
            self.last_error = f"Could not start Codex: {e}"
            raise CodexError(self.last_error) from e
        self._generation += 1
        conn = _Connection(proc, self._generation, self)
        try:
            await conn.request(
                "initialize",
                {
                    "clientInfo": {"name": "qubotu", "title": "Qubotu", "version": current_version()},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=30,
            )
            await conn.send({"method": "initialized"})
        except CodexError as e:
            await conn.close()
            self.last_error = f"Codex app-server failed to initialize: {e}"
            raise CodexError(self.last_error) from e
        except BaseException:
            # Cancelled mid-initialize: the process isn't published yet, so nothing else would stop it.
            await asyncio.shield(conn.close())
            raise
        self.last_error = None
        return conn

    def connection_lost(self, conn: _Connection) -> None:
        if conn is not self._conn:
            return
        self.last_error = f"Codex app-server exited (code {conn.proc.returncode})."
        log.warning(self.last_error)
        self.on_notification("qubotu/codexExited", {})

    async def restart(self) -> None:
        async with self._lock:
            if self._conn:
                await self._conn.close()
            self._conn = None
            self._conn = await self._open()

    async def stop(self) -> None:
        """Stop for good: later requests fail instead of starting Codex again."""
        self._stopped = True
        async with self._lock:
            conn, self._conn = self._conn, None
            if conn:
                await conn.close()

    async def request(self, method: str, params: dict, timeout: float = 60) -> dict:
        conn = await self._connection()
        return await conn.request(method, params, timeout)
