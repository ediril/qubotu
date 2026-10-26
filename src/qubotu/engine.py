"""Runs agent turns on Codex, tracks their progress, and schedules recurring runs.

Everything that should make an agent run is first written to the `inbox` table, then delivered by
`_deliver`. The table is the source of truth for every item (see `inbox.py`); Stop, Pause, and
Delete change it directly, so they hold across restarts. Each kind of item has a builder that turns
it into a prompt when it's about to run, and a finisher for its follow-up work (see `KINDS`).
"""

import asyncio
import json
import logging
import shutil
from collections import defaultdict
from pathlib import Path

from . import collab, config, prompts, tools
from .browser import Browser
from .codex import AGENT_PROFILE, CodexClient, CodexError, CodexTimeout
from .db import DB, later, now
from .events import Bus
from .inbox import Kind, Run, TurnResult, TurnSpec
from .memory import Memory

log = logging.getLogger(__name__)

PROVIDERS = {"openai": "OpenAI (ChatGPT subscription)", "fireworks": "Fireworks (API key)"}
STALE_TURNS_KEPT = 1000
RELATED_MEMORIES = 5


class Engine:
    def __init__(self, db_path: Path | str | None = None):
        self.db = DB(db_path or config.DB_PATH)
        self.bus = Bus()
        self.memory = Memory(self.db)
        self.codex = CodexClient(self._on_notification, self._on_tool_call)
        self.browser = Browser(self.db, on_change=lambda: self.bus.refresh("sites"))
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TURNS)
        self.runs: dict[int, list[Run]] = defaultdict(list)  # agent id -> its queued and running runs
        self.by_thread: dict[str, Run] = {}  # thread id -> the run whose turn is on it
        self.stale_turns: dict[str, None] = {}  # turns that ended (oldest first); their late events are ignored
        self.loaded: dict[str, int] = {}  # thread id -> codex generation it was resumed in
        self.stopping = False
        self._tasks: set[asyncio.Task] = set()
        self._models: list[dict] | None = None

    # Lifecycle

    async def start(self, scheduler: bool = True) -> None:
        with self.db.transaction():
            self.db.run("UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'", now())
            # Stopped by the user before Qubotu went down: they stay stopped.
            for row in self.db.all("SELECT id FROM inbox WHERE status = 'stopping' ORDER BY id"):
                self._record(self._load_item(row["id"]), None, TurnResult(False, error="Stopped by you."), None, cancelled=True)
            # Interrupted by the shutdown itself: deliver again.
            self.db.run("UPDATE inbox SET status = 'pending' WHERE status = 'running'")
            # Requests whose delivery is gone (from before the inbox existed): tell the agent that asked.
            for req in self.db.all(
                "SELECT * FROM requests r WHERE status IN ('pending', 'running') AND NOT EXISTS ("
                "SELECT 1 FROM inbox i WHERE i.kind = 'request' AND i.status = 'pending' "
                "AND json_extract(i.payload, '$.request_id') = r.id)"
            ):
                collab.fail_request(self, req, "Interrupted by a restart.")
        self.apply_settings()
        for item in self.db.all("SELECT id FROM inbox WHERE status = 'pending' ORDER BY id"):
            self._dispatch(item["id"])
        if scheduler:
            self.spawn(self._scheduler())
        self.spawn(self.memory.backfill())
        self.spawn(self._warm_codex())

    async def stop(self) -> None:
        """Cancel everything in flight. Running items stay `running` in the database and are
        delivered again on the next start."""
        self.stopping = True
        # Loop: cancelling a task can schedule follow-up tasks, which need cancelling too.
        while tasks := [t for t in self._tasks if not t.done()]:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.browser.stop()
        await self.codex.stop()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())

    async def _warm_codex(self) -> None:
        try:
            await self.codex.ensure_started()
        except CodexError as e:
            log.warning("%s", e)
        self.bus.refresh("status")

    def apply_settings(self) -> bool:
        """Push settings into the Codex environment. Returns True if Codex needs a restart."""
        key = self.db.get_setting("fireworks_api_key") or ""
        env = {"FIREWORKS_API_KEY": key} if key else {}
        changed = env != self.codex.env_extra
        self.codex.env_extra = env
        return changed and self.codex.running

    async def restart_codex(self) -> None:
        for run in list(self.by_thread.values()):
            self._end_turn(run, "Codex was restarted.")
        await self.codex.restart()
        self.bus.refresh("status")

    def post(self, channel: str, role: str, text: str, agent_id: int | None = None, meta: dict | None = None) -> None:
        self.db.add_message(channel, role, text, agent_id, meta)
        self.bus.refresh(f"messages:{channel}")

    # Agents

    def workdir(self, agent_id: int) -> Path:
        return config.AGENTS_DIR / str(agent_id)

    def status(self, agent: dict) -> str:
        runs = self.runs.get(agent["id"], [])
        if any(r.running for r in runs):
            return "running"
        if self.db.value("SELECT 1 FROM outputs WHERE agent_id = ? AND status = 'open' AND blocking = 1", agent["id"]):
            return "blocked"
        if runs:
            return "queued"
        return "paused" if agent["status"] == "paused" else "idle"

    # Blockers: open questions or login requests the agent can't go on without

    def blockers(self) -> list[dict]:
        rows = self.db.all(
            "SELECT o.*, a.name AS agent_name FROM outputs o JOIN agents a ON a.id = o.agent_id "
            "WHERE o.status = 'open' AND o.blocking = 1 ORDER BY o.id"
        )
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else {}
            if r["kind"] == "login":
                site = self.db.one("SELECT * FROM sites WHERE host = ?", r["data"]["host"])
                granted = site and self.db.value(
                    "SELECT 1 FROM agent_sites WHERE agent_id = ? AND site_id = ?", r["agent_id"], site["id"]
                )
                # An agent that already has the site hit an expired login; otherwise a working login just needs granting.
                r["action"] = "grant" if site and site["logged_in_at"] and not granted else "login"
                r["logging_in"] = bool(site) and site["id"] in self.browser.logging_in
        return rows

    def request_login(self, agent_id: int, host: str, reason: str) -> tuple[int, bool]:
        """Ask the user to log in to a site for this agent. Returns the request's id, and whether it's new."""
        existing = self.db.one(
            "SELECT id FROM outputs WHERE agent_id = ? AND kind = 'login' AND status = 'open' AND json_extract(data, '$.host') = ?",
            agent_id, host,
        )
        if existing:
            return existing["id"], False
        ts = now()
        oid = self.db.insert(
            "outputs", agent_id=agent_id, kind="login", title=f"Log in to {host}", body=reason,
            data=json.dumps({"host": host}), importance="high", status="open", blocking=1, created_at=ts, updated_at=ts,
        )
        self.bus.refresh(f"outputs:{agent_id}", "outputs", "status", "agents")
        return oid, True

    def resolve_login(self, output_id: int) -> None:
        """Act on a login request: grant a site that's already logged in, or open the login window
        and grant it once the user closes it. Either way the agent is told and carries on."""
        o = self.db.one("SELECT * FROM outputs WHERE id = ? AND kind = 'login' AND status = 'open'", output_id)
        if not o:
            return
        host = json.loads(o["data"])["host"]
        action = next(b for b in self.blockers() if b["id"] == output_id)["action"]
        site_id = self.browser.add_site(host)
        self.db.run("INSERT OR IGNORE INTO agent_sites (agent_id, site_id) VALUES (?, ?)", o["agent_id"], site_id)
        if action == "grant":
            self.answer_question(output_id, f"You can use {host} now, logged in as the user.")
            return
        self.browser.start_login(
            site_id, on_done=lambda: self.answer_question(output_id, f"The user logged in to {host}. You can use it now.")
        )

    def decline_login(self, output_id: int) -> None:
        o = self.db.one("SELECT * FROM outputs WHERE id = ? AND kind = 'login'", output_id)
        if o:
            host = json.loads(o["data"])["host"]
            self.answer_question(output_id, f"Not now: the user didn't log in to {host}. Carry on without it.")

    def create_agent(self, **fields) -> int:
        schedule = int(fields.get("schedule_minutes") or 0)
        aid = self.db.insert("agents", created_at=now(), next_run_at=later(schedule) if schedule else None, **fields)
        self.workdir(aid).mkdir(parents=True, exist_ok=True)
        self.bus.refresh("agents", "status")
        return aid

    def update_agent(self, agent_id: int, **fields) -> None:
        agent = self.db.agent(agent_id)
        if not agent:
            return
        if "schedule_minutes" in fields and int(fields["schedule_minutes"] or 0) != agent["schedule_minutes"]:
            s = int(fields["schedule_minutes"] or 0)
            fields["next_run_at"] = later(s) if s else None
        self.db.update("agents", agent_id, **fields)
        self.bus.refresh("agents", "status")

    def set_agent_sites(self, agent_id: int, site_ids: list[int]) -> None:
        known = {s["id"] for s in self.browser.sites()}
        with self.db.transaction():
            self.db.run("DELETE FROM agent_sites WHERE agent_id = ?", agent_id)
            for sid in set(site_ids) & known:
                self.db.insert("agent_sites", agent_id=agent_id, site_id=sid)

    def delete_agent(self, agent_id: int) -> None:
        agent = self.db.agent(agent_id)
        if not agent:
            return
        with self.db.transaction():
            self.stop_runs(agent_id, "The agent was deleted.")
            # Other agents' queued work on this agent's requests has no one to report to.
            self.db.run(
                "UPDATE inbox SET status = 'cancelled', finished_at = ? WHERE status = 'pending' AND kind = 'request' "
                "AND json_extract(payload, '$.request_id') IN (SELECT id FROM requests WHERE from_agent = ?)",
                now(), agent_id,
            )
            for table in ("messages", "outputs", "runs", "inbox"):
                self.db.run(f"DELETE FROM {table} WHERE agent_id = ?", agent_id)
            self.db.run("DELETE FROM messages WHERE channel = ?", f"agent:{agent_id}")
            self.db.run("DELETE FROM requests WHERE from_agent = ? OR to_agent = ?", agent_id, agent_id)
            self.db.run("UPDATE memories SET agent_id = NULL WHERE agent_id = ?", agent_id)
            self.db.run("DELETE FROM agent_sites WHERE agent_id = ?", agent_id)
            self.db.run("DELETE FROM agents WHERE id = ?", agent_id)
        # Detach a running turn now, so nothing it does from here on is recorded.
        for run in self.runs.get(agent_id, []):
            if run.thread_id and self.by_thread.get(run.thread_id) is run:
                del self.by_thread[run.thread_id]
            self._end_turn(run, "The agent was deleted.")
        self.spawn(asyncio.to_thread(shutil.rmtree, self.workdir(agent_id), True))
        if agent["thread_id"]:
            self.spawn(self._archive_thread(agent["thread_id"]))
        self.bus.refresh("agents", "status", "outputs", "requests")

    async def _archive_thread(self, thread_id: str) -> None:
        try:
            await self.codex.request("thread/archive", {"threadId": thread_id})
        except CodexError:
            pass

    def reset_thread(self, agent_id: int) -> None:
        agent = self.db.agent(agent_id)
        if agent and agent["thread_id"]:
            self.loaded.pop(agent["thread_id"], None)
            self.spawn(self._archive_thread(agent["thread_id"]))
        self.db.update("agents", agent_id, thread_id=None, thread_provider=None, thread_tools=None, context_hash=None)

    def stop_runs(self, agent_id: int, reason: str = "Stopped by you.") -> None:
        """Cancel the agent's queued items and interrupt its running turn. Cancellation is written to
        the database right away, with each item's follow-up, so it holds even if Qubotu restarts."""
        queued = [r for r in self.runs.get(agent_id, []) if not r.running]
        with self.db.transaction():
            for row in self.db.all("SELECT id FROM inbox WHERE agent_id = ? AND status = 'pending' ORDER BY id", agent_id):
                self._record(self._load_item(row["id"]), None, TurnResult(False, error=reason), None, cancelled=True)
            # The running item settles when its turn ends; if Qubotu stops first, startup settles it.
            self.db.run("UPDATE inbox SET status = 'stopping' WHERE agent_id = ? AND status = 'running'", agent_id)
        for run in queued:
            self._forget(run)
            if run.task:
                run.task.cancel()
        for run in self.runs.get(agent_id, []):
            if run.running:
                run.stop_requested = True
                run.stop_reason = reason
                if run.turn_id:
                    self.spawn(self._interrupt(run))
                # A run still setting up checks stop_requested before it starts its turn.
        self.bus.refresh("status", "agents")

    async def _interrupt(self, run: Run) -> None:
        try:
            await self.codex.request("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})
        except CodexError as e:
            log.warning("interrupt failed: %s", e)

    def set_paused(self, agent_id: int, paused: bool) -> None:
        agent = self.db.agent(agent_id)
        if not agent:
            return
        fields = {"status": "paused" if paused else "active"}
        if not paused and agent["schedule_minutes"]:
            fields["next_run_at"] = later(agent["schedule_minutes"])
        with self.db.transaction():
            self.db.update("agents", agent_id, **fields)
            if paused:
                self.stop_runs(agent_id, "The agent was paused.")
        self.bus.refresh("agents", "status")

    async def models(self) -> list[dict]:
        if self._models is None:
            try:
                r = await self.codex.request("model/list", {})
            except CodexError:
                return []
            self._models = [
                {"id": m["id"], "name": m.get("displayName") or m["id"], "default": bool(m.get("isDefault"))}
                for m in r.get("data", [])
            ]
        return self._models

    async def _effective_model(self, agent: dict) -> str | None:
        """The model to run with. An empty setting means the Codex default, sent explicitly so a thread
        started with another model switches back."""
        if agent["model"]:
            return agent["model"]
        if agent["provider"] != "openai":
            return None
        return next((m["id"] for m in await self.models() if m["default"]), None)

    async def account(self) -> dict | None:
        try:
            return (await self.codex.request("account/read", {}, timeout=15)).get("account")
        except CodexError:
            return None

    # Inbox

    def enqueue(self, agent_id: int, kind: str, depth: int = 0, **payload) -> int:
        item_id = self.db.insert(
            "inbox", agent_id=agent_id, kind=kind, payload=json.dumps(payload), depth=depth, created_at=now()
        )
        self._dispatch(item_id)
        return item_id

    def _dispatch(self, item_id: int) -> None:
        """Queue delivery. The run is registered before its task starts, so Stop and the status see it
        immediately. While shutting down, items stay pending for the next start."""
        if self.stopping:
            return
        item = self._load_item(item_id)
        agent = item and self.db.agent(item["agent_id"])
        if not item or item["status"] != "pending" or not agent:
            return
        run = Run(item_id, agent["id"], agent["name"], item["kind"], item["depth"])
        self.runs[agent["id"]].append(run)
        run.task = self.spawn(self._deliver(run))
        self.bus.refresh("status")

    def _forget(self, run: Run) -> None:
        if run in self.runs.get(run.agent_id, []):
            self.runs[run.agent_id].remove(run)

    def _load_item(self, item_id: int) -> dict | None:
        item = self.db.one("SELECT * FROM inbox WHERE id = ?", item_id)
        if item:
            item["payload"] = json.loads(item["payload"])
        return item

    def _pending(self, run: Run) -> dict | None:
        item = self._load_item(run.item_id)
        return item if item and item["status"] == "pending" and self.db.agent(run.agent_id) else None

    async def _deliver(self, run: Run) -> None:
        try:
            async with self.locks[run.agent_id]:
                if self._pending(run):
                    async with self.semaphore:
                        await self._execute(run)
        finally:
            self._forget(run)
            if run.item_id in self.browser.tabs:
                self.spawn(self.browser.close_tab(run.item_id))
            self.bus.refresh("status")

    async def _execute(self, run: Run) -> None:
        """Runs with the agent's lock held. Everything is re-read here, since the item may have waited."""
        item = self._pending(run)
        if not item:
            return
        agent = self.db.agent(run.agent_id)
        kind = KINDS[item["kind"]]
        spec = kind.build(self, item, agent)
        if spec is None:
            self.db.update("inbox", item["id"], status="done", finished_at=now())
            return
        run.channel = spec.channel or f"agent:{agent['id']}"
        run.running = True
        with self.db.transaction():
            self.db.update("inbox", item["id"], status="running")
            run_id = self.db.insert("runs", agent_id=agent["id"], trigger=item["kind"], started_at=now())
            if kind.begin:
                kind.begin(self, item)
        self.bus.refresh("status")
        try:
            result = await self._turn(agent, run, spec.prompt)
        except CodexError as e:
            result = TurnResult(False, error=str(e))
        except asyncio.CancelledError:
            # Shutting down: leave the item running, so the next start delivers it again.
            self.db.update("runs", run_id, status="interrupted", finished_at=now())
            raise
        except Exception as e:
            log.exception("run %s failed", run.item_id)
            result = TurnResult(False, error=f"Internal error: {e}")
        self._settle(item, spec, result, run_id, stopped=run.stop_requested and not result.ok)

    def _settle(self, item: dict, spec: TurnSpec, result: TurnResult, run_id: int, stopped: bool) -> None:
        try:
            with self.db.transaction():
                self._record(item, spec, result, run_id, cancelled=stopped)
        except Exception:
            log.exception("could not record the end of run %s", item["id"])
            self.db.update("inbox", item["id"], status="failed", finished_at=now())
            self.db.update("runs", run_id, status="failed", error="Qubotu couldn't record the result.", finished_at=now())
        self.bus.refresh("status", "agents")

    def _record(self, item: dict, spec: TurnSpec | None, result: TurnResult, run_id: int | None, cancelled: bool) -> None:
        """Write how an item ended and do its follow-up work. Callers hold a transaction. Writes
        nothing for a deleted agent."""
        agent_id = item["agent_id"]
        if not self.db.agent(agent_id):
            return
        status = "done" if result.ok else "cancelled" if cancelled else "failed"
        self.db.update("inbox", item["id"], status=status, finished_at=now())
        chat = f"agent:{agent_id}"
        if run_id is not None:
            run_status = "completed" if result.ok else "interrupted" if cancelled else "failed"
            self.db.update("runs", run_id, status=run_status, error=result.error, finished_at=now())
            self.db.update("agents", agent_id, last_run_at=now())
            if result.ok and result.text and spec.store_final:
                self.post(chat, "agent", result.text, agent_id, {"trigger": item["kind"]})
            elif not result.ok:
                self.post(chat, "system", f"Run {'stopped' if cancelled else 'failed'}: {result.error}", agent_id)
        finish = KINDS[item["kind"]].finish
        if finish:
            finish(self, item, spec, result)

    # Turns

    async def _turn(self, agent: dict, run: Run, message: str) -> TurnResult:
        thread_id = await self._ensure_thread(agent)
        if not self.db.agent(agent["id"]):
            # Deleted while the thread was being set up.
            self.spawn(self._archive_thread(thread_id))
            return TurnResult(False, error="The agent was deleted.")
        if run.stop_requested:
            return TurnResult(False, error=run.stop_reason)
        prompt, ctx_hash, feedback = await self._with_context(agent, message)
        model = await self._effective_model(agent)
        run.thread_id = thread_id
        self.by_thread[thread_id] = run
        params = {"threadId": thread_id, "input": [{"type": "text", "text": prompt}], "approvalPolicy": "never"}
        if model:
            params["model"] = model
        try:
            try:
                r = await self.codex.request("turn/start", params)
            except CodexTimeout:
                # The turn may have started without us knowing its id; don't share the thread with it.
                self.reset_thread(agent["id"])
                raise
            run.turn_id = r["turn"]["id"]
            self.db.update("agents", agent["id"], context_hash=ctx_hash, thread_model=model)
            if run.stop_requested:
                await self._interrupt(run)
            try:
                # Shielded: on timeout we still need the future to learn how the interrupted turn ended.
                await asyncio.wait_for(asyncio.shield(run.done), config.TURN_TIMEOUT)
            except TimeoutError:
                run.fail("The run timed out.")
                await self._interrupt(run)
                try:
                    await asyncio.wait_for(asyncio.shield(run.done), config.INTERRUPT_GRACE)
                except TimeoutError:
                    # Codex never confirmed the stop: give the agent a fresh thread next time.
                    self.reset_thread(agent["id"])
        finally:
            if run.compacted:
                # Codex summarized the thread, so the context sent in earlier turns may be gone.
                self.db.update("agents", agent["id"], context_hash=None)
            if run.turn_id:
                self._mark_stale(run.turn_id)
            if self.by_thread.get(thread_id) is run:
                del self.by_thread[thread_id]
        if run.error:
            return TurnResult(False, run.final_text, run.error)
        for f in feedback:
            # Only clear the version the agent saw; feedback edited during the turn stays pending.
            self.db.run("UPDATE outputs SET feedback_pending = 0 WHERE id = ? AND feedback = ?", f["id"], f["feedback"])
        return TurnResult(True, run.final_text.strip())

    def _mark_stale(self, turn_id: str) -> None:
        self.stale_turns[turn_id] = None
        while len(self.stale_turns) > STALE_TURNS_KEPT:
            del self.stale_turns[next(iter(self.stale_turns))]

    async def _ensure_thread(self, agent: dict) -> str:
        await self.codex.ensure_started()
        provider = agent["provider"] if agent["provider"] in PROVIDERS else "openai"
        workdir = self.workdir(agent["id"])
        if self.db.agent(agent["id"]):
            workdir.mkdir(parents=True, exist_ok=True)
        common = {
            "cwd": str(workdir),
            "approvalPolicy": "never",
            "permissions": AGENT_PROFILE,
            "developerInstructions": prompts.developer_instructions(agent),
            "model": await self._effective_model(agent),
            "modelProvider": provider,
        }
        tid = agent["thread_id"]
        if tid and agent["thread_provider"] == provider and agent["thread_tools"] == tools.FINGERPRINT:
            if self.loaded.get(tid) == self.codex.generation:
                return tid
            try:
                r = await self.codex.request("thread/resume", {"threadId": tid, "excludeTurns": True, **common})
            except CodexError as e:
                log.warning("could not resume thread %s, starting a new one: %s", tid, e)
            else:
                _require_agent_profile(r)
                self.loaded[tid] = self.codex.generation
                return tid
        r = await self.codex.request(
            "thread/start",
            {**common, "dynamicTools": tools.SPECS, "serviceName": "qubotu", "config": {"web_search": "live"}},
        )
        _require_agent_profile(r)
        if tid:
            # Replaced because its provider or tool set changed, or it couldn't be resumed.
            self.spawn(self._archive_thread(tid))
        tid = r["thread"]["id"]
        self.loaded[tid] = self.codex.generation
        self.db.update(
            "agents", agent["id"], thread_id=tid, thread_provider=provider, thread_tools=tools.FINGERPRINT, context_hash=None
        )
        agent["context_hash"] = None
        return tid

    async def _with_context(self, agent: dict, message: str) -> tuple[str, str, list[dict]]:
        """Prepend what changed since the agent's last turn, plus older memories that match the message.
        Unchanged context is not repeated."""
        others = [a for a in self.db.agents() if a["id"] != agent["id"]]
        profile = prompts.profile_block(agent, others, [s["host"] for s in self.browser.granted(agent["id"])])
        known = self.memory.profile()
        mem = prompts.memory_block(known)
        ctx_hash = prompts.context_hash(profile, mem)
        sections: list[tuple[str, str]] = [("Current time", now())]
        if ctx_hash != agent["context_hash"]:
            sections += [("Your profile (updated)", profile), ("Shared memory (updated)", mem)]
        known_ids = {m["id"] for m in known}
        related = [m for m in await self.memory.search(message, RELATED_MEMORIES) if m["id"] not in known_ids]
        if related:
            sections.append(("Older shared memories that may be relevant", prompts.memory_lines(related)))
        feedback = self.db.all(
            "SELECT id, title, feedback FROM outputs WHERE agent_id = ? AND feedback_pending = 1", agent["id"]
        )
        if feedback:
            sections.append(
                ("Feedback from the user on your shared outputs", "\n".join(f"- #{f['id']} {f['title']}: {f['feedback']}" for f in feedback))
            )
        return prompts.wrap(sections, message), ctx_hash, feedback

    def _end_turn(self, run: Run, error: str | None = None) -> None:
        if error:
            run.fail(error)
        if not run.done.done():
            run.done.set_result(None)

    # Codex callbacks

    def _run_for(self, params: dict) -> Run | None:
        """The run a Codex event belongs to, or None for events from a turn that already ended."""
        run = self.by_thread.get(params.get("threadId", ""))
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if not run or turn_id in self.stale_turns:
            return None
        if run.turn_id and turn_id and turn_id != run.turn_id:
            return None
        return run

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "qubotu/codexExited":
            for run in list(self.by_thread.values()):
                self._end_turn(run, self.codex.last_error or "Codex exited.")
            self.bus.refresh("status")
            return
        run = self._run_for(params)
        if not run:
            return
        if method == "item/agentMessage/delta":
            self.bus.publish(
                {"type": "delta", "channel": run.channel, "item": params["itemId"], "delta": params["delta"], "agent": run.agent_name}
            )
        elif method == "item/completed":
            self._on_item(run, params["item"])
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            err = turn.get("error")
            status = turn.get("status")
            if err:
                self._end_turn(run, err.get("message") or str(err))
            elif status == "interrupted":
                # Interrupted by Stop or Pause, or by the timeout (which already set the reason).
                self._end_turn(run, run.stop_reason or "The run was interrupted.")
            elif status == "failed":
                self._end_turn(run, "The run failed.")
            else:
                # It finished before a Stop could land: keep the result.
                self._end_turn(run)
        elif method == "error" and not params.get("willRetry"):
            run.fail((params.get("error") or {}).get("message") or "Codex reported an error.")

    def _on_item(self, run: Run, item: dict) -> None:
        kind = item.get("type")
        chat = f"agent:{run.agent_id}"
        if kind == "agentMessage":
            self.bus.publish({"type": "item_done", "item": item.get("id")})
            if item.get("phase") in (None, "final_answer"):
                run.final_text = item.get("text", "")
            elif run.channel == chat and item.get("text"):
                self.post(chat, "agent", item["text"], run.agent_id, {"phase": "commentary"})
            return
        note = None
        if kind == "commandExecution":
            note = f"Ran `{(item.get('command') or '')[:200]}`"
        elif kind == "webSearch":
            note = f"Searched the web: {item.get('query') or ''}"
        elif kind == "fileChange":
            paths = ", ".join(Path(c.get("path", "")).name for c in item.get("changes") or [])
            note = f"Edited {paths}" if paths else "Edited files"
        elif kind == "dynamicToolCall":
            args = item.get("arguments") or {}
            detail = args.get("title") or args.get("question") or args.get("agent") or args.get("query") or args.get("text") or ""
            note = f"Used {item.get('tool')}" + (f": {str(detail)[:120]}" if detail else "")
        elif kind == "contextCompaction":
            run.compacted = True
        elif kind == "mcpToolCall":
            note = f"Used {item.get('server')}.{item.get('tool')}"
        if note:
            self.post(chat, "activity", note, run.agent_id)

    async def _on_tool_call(self, params: dict) -> tuple[bool, str]:
        run = self._run_for(params)
        if not run:
            return False, "No active Qubotu run for this thread."
        return await tools.call(self, run, params.get("tool", ""), params.get("arguments") or {})

    # Scheduling

    async def _scheduler(self) -> None:
        while True:
            try:
                self.run_due_agents()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(config.SCHEDULER_TICK)

    def run_due_agents(self) -> list[int]:
        """Queue one run for each agent that is due. Missed runs collapse into one; the next run is
        scheduled a full interval after this one is queued."""
        due = self.db.all(
            "SELECT * FROM agents WHERE status = 'active' AND schedule_minutes > 0 "
            "AND next_run_at IS NOT NULL AND next_run_at <= ?",
            now(),
        )
        started = []
        for agent in due:
            if self.runs.get(agent["id"]):
                continue
            with self.db.transaction():
                self.db.update("agents", agent["id"], next_run_at=later(agent["schedule_minutes"]))
                self.post(f"agent:{agent['id']}", "system", "Scheduled run", agent["id"])
                self.enqueue(agent["id"], "schedule")
            started.append(agent["id"])
        return started

    # User actions that start turns

    def run_now(self, agent_id: int) -> None:
        with self.db.transaction():
            self.post(f"agent:{agent_id}", "system", "Run started by you", agent_id)
            self.enqueue(agent_id, "manual")

    def chat(self, agent_id: int, text: str) -> None:
        with self.db.transaction():
            self.post(f"agent:{agent_id}", "user", text, agent_id)
            self.enqueue(agent_id, "chat", text=text)

    def answer_question(self, output_id: int, answer_text: str) -> None:
        q = self.db.one("SELECT * FROM outputs WHERE id = ? AND kind IN ('question', 'login')", output_id)
        if not q or q["status"] != "open":
            return
        with self.db.transaction():
            self.db.update("outputs", output_id, status="answered", answer=answer_text, updated_at=now())
            self.post(f"agent:{q['agent_id']}", "user", f"Answered #{output_id}: {answer_text}", q["agent_id"])
            self.enqueue(q["agent_id"], "answer", output_id=output_id)
        self.bus.refresh(f"outputs:{q['agent_id']}", "outputs", "status", "agents")

    def mark_seen(self, agent_id: int) -> None:
        self.db.run("UPDATE outputs SET status = 'seen' WHERE agent_id = ? AND status = 'new'", agent_id)
        self.bus.refresh("status", "agents")


def _require_agent_profile(response: dict) -> None:
    """Refuse to run an agent unless Codex confirms the restricted profile is in force. A Codex
    version that ignored it would otherwise silently give agents read access to the home folder."""
    applied = (response.get("activePermissionProfile") or {}).get("id")
    if applied != AGENT_PROFILE:
        raise CodexError(
            f"Codex didn't apply Qubotu's agent permissions (got {applied or 'none'}), so the agent wasn't run. "
            "This Codex version may not support permission profiles."
        )


# Inbox kinds

def _build_scheduled(engine: Engine, item: dict, agent: dict) -> TurnSpec | None:
    if agent["status"] == "paused" or not agent["schedule_minutes"]:
        return None
    return TurnSpec(prompts.scheduled_run(agent["schedule_minutes"]))


def _build_answer(engine: Engine, item: dict, agent: dict) -> TurnSpec | None:
    q = engine.db.one("SELECT * FROM outputs WHERE id = ?", item["payload"]["output_id"])
    if not q:
        return None
    return TurnSpec(prompts.answer(q["title"], q["answer"], q["id"]))


def _finish_answer(engine: Engine, item: dict, spec: TurnSpec | None, result: TurnResult) -> None:
    if result.ok:
        return
    # The agent never got the answer: reopen the question so it can be answered again.
    oid = item["payload"]["output_id"]
    engine.db.run("UPDATE outputs SET status = 'open', updated_at = ? WHERE id = ? AND status = 'answered'", now(), oid)
    engine.bus.refresh(f"outputs:{item['agent_id']}", "outputs", "status")


KINDS: dict[str, Kind] = {
    "chat": Kind(lambda e, item, a: TurnSpec(item["payload"]["text"])),
    "manual": Kind(lambda e, item, a: TurnSpec(prompts.manual_run())),
    "schedule": Kind(_build_scheduled),
    "answer": Kind(_build_answer, _finish_answer),
    "request": Kind(collab.build_request, collab.finish_request, collab.begin_request),
    "result": Kind(collab.build_result),
}
