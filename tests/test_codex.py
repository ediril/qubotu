import asyncio
import stat
import sys
from pathlib import Path

import pytest

from qubotu.codex import CodexClient, CodexError

SERVER = Path(__file__).with_name("fake_app_server.py")


@pytest.fixture
def client(tmp_path, monkeypatch):
    exe = tmp_path / "codex"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {SERVER} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    notes: list[str] = []
    tool_calls: list[dict] = []

    async def on_tool(params):
        tool_calls.append(params)
        await asyncio.sleep(0)
        return True, "tool ran"

    c = CodexClient(lambda m, p: notes.append(m), on_tool)
    c.executable = str(exe)
    c.notes, c.tool_calls = notes, tool_calls
    yield c


async def test_requests_wait_for_initialize(client, monkeypatch):
    monkeypatch.setenv("FAKE_INIT_DELAY", "0.3")
    first = asyncio.create_task(client.request("echo", {"n": 1}))
    await asyncio.sleep(0.05)
    assert not client.running, "a process mid-initialize is not ready"
    second = await client.request("echo", {"n": 2})
    assert second == {"n": 2}
    assert await first == {"n": 1}
    assert client.generation == 1
    await client.stop()


async def test_timeouts_and_errors_are_codex_errors(client):
    with pytest.raises(CodexError, match="didn't answer silent"):
        await client.request("silent", {}, timeout=0.2)
    with pytest.raises(CodexError, match="unknown method"):
        await client.request("nope", {})
    await client.stop()


async def test_bad_line_does_not_kill_the_reader(client):
    assert await client.request("garbage", {}) == {"after": "garbage"}
    assert await client.request("echo", {"still": "alive"}) == {"still": "alive"}
    await client.stop()


async def test_process_exit_fails_pending_and_reconnects(client):
    first = (await client.request("pid", {}))["pid"]
    with pytest.raises(CodexError):
        await client.request("die", {}, timeout=5)
    await asyncio.sleep(0.1)
    assert "qubotu/codexExited" in client.notes
    assert not client.running
    second = (await client.request("pid", {}))["pid"]
    assert second != first and client.generation == 2
    await client.stop()


async def test_tool_reply_goes_to_the_process_that_asked(client):
    r = await client.request("tool", {"tool": "remember", "threadId": "t"})
    assert client.tool_calls == [{"tool": "remember", "threadId": "t"}]
    assert r["toolReply"]["id"] == "srv-1"
    assert r["toolReply"]["result"]["contentItems"][0]["text"] == "tool ran"
    await client.stop()


async def test_restart_and_stop_do_not_race(client):
    await client.ensure_started()
    r1, req, r2 = await asyncio.gather(
        client.restart(), client.request("echo", {}), client.restart(), return_exceptions=True
    )
    assert r1 is None and r2 is None
    assert req == {} or isinstance(req, CodexError), "an in-flight request may fail cleanly, never hang"
    assert client.running and client.generation == 3
    assert await client.request("echo", {"ok": 1}) == {"ok": 1}
    await client.stop()
    assert not client.running


async def test_missing_executable(client):
    client.executable = "definitely-not-codex"
    with pytest.raises(CodexError, match="not found"):
        await client.request("echo", {})
    assert client.last_error
