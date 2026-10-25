"""Regression tests for the second review: restarts, crashes between writes, deletion mid-setup,
cancellation during startup, and the edges of the web and sandbox protections."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qubotu import codex, collab, config
from qubotu.codex import CodexClient, CodexError, CodexTimeout
from qubotu.db import now
from qubotu.engine import Engine
from qubotu.web import create_app

from .conftest import FakeCodex, FakeEmbedder, settle
from .test_lifecycle import make, turn_starts, wait_running



def fresh_engine(path) -> Engine:
    e = Engine(path)
    e.memory.embedder = FakeEmbedder()
    e.codex = FakeCodex(e)
    return e


async def test_stop_and_pause_hold_across_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    first = fresh_engine(tmp_path / "q.db")
    await first.start(scheduler=False)
    aid = make(first)
    first.codex.scripts["Scout"] = [[("ignore_interrupt",)]]
    first.chat(aid, "long job")
    await wait_running(first, aid)
    first.chat(aid, "queued behind it")
    first.set_paused(aid, True)
    await first.stop()  # before Codex ever confirms the interrupt

    second = fresh_engine(tmp_path / "q.db")
    await second.start(scheduler=False)
    await settle(second)
    assert not turn_starts(second), "nothing the user stopped runs after the restart"
    assert [i["status"] for i in second.db.all("SELECT status FROM inbox ORDER BY id")] == ["cancelled", "cancelled"]
    await second.stop()


async def test_stop_is_recorded_without_waiting_for_a_slot(engine):
    engine.semaphore = asyncio.Semaphore(1)
    busy, aid = make(engine, "Busy"), make(engine, "Scout")
    engine.codex.scripts["Busy"] = [[("hang",)]]
    engine.chat(busy, "hold the only slot")
    await wait_running(engine, busy)
    engine.chat(aid, "waiting for a slot")
    engine.stop_runs(aid)
    assert engine.db.one("SELECT status FROM inbox WHERE agent_id = ?", aid)["status"] == "cancelled"
    assert engine.status(engine.db.agent(aid)) == "idle"
    engine.stop_runs(busy)
    await settle(engine)


async def test_settling_is_all_or_nothing(engine, monkeypatch):
    a, b = make(engine, "Alice"), make(engine, "Bob")
    engine.codex.scripts["Alice"] = [[("tool", "request_help", {"agent": "Bob", "task": "t"}), ("say", "asked")]]
    engine.codex.scripts["Bob"] = [[("say", "the answer")]]

    def crash(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(collab, "fail_request", crash)
    engine.chat(a, "go")
    await settle(engine)
    bob_item = engine.db.one("SELECT * FROM inbox WHERE agent_id = ?", b)
    assert bob_item["status"] == "failed"
    assert not engine.db.all("SELECT * FROM messages WHERE agent_id = ? AND role = 'agent'", b), "the reply was rolled back with the rest"


async def test_deleted_requester_never_gets_someone_elses_answer(engine):
    alice, bob, carol = make(engine, "Alice"), make(engine, "Bob"), make(engine, "Carol")
    engine.codex.scripts["Alice"] = [[("tool", "request_help", {"agent": "Bob", "task": "alice's task"}), ("say", "asked")]]
    engine.codex.scripts["Bob"] = [[("hang",)], [("say", "carol's answer")]]
    engine.chat(alice, "go")
    await wait_running(engine, bob)
    old_request = engine.db.value("SELECT id FROM requests")
    engine.delete_agent(alice)
    engine.codex.scripts["Carol"] = [[("tool", "request_help", {"agent": "Bob", "task": "carol's task"}), ("say", "asked")], [("say", "thanks")]]
    engine.chat(carol, "go")
    engine.stop_runs(bob)
    await settle(engine)
    new_request = engine.db.one("SELECT * FROM requests")
    assert new_request["id"] != old_request and new_request["task"] == "carol's task"
    result_prompt = [t["input"][0]["text"] for t in turn_starts(engine) if "Result:" in t["input"][0]["text"]]
    assert result_prompt and "alice" not in result_prompt[0].lower()


async def test_delete_during_thread_setup_starts_nothing(engine):
    aid = make(engine)
    engine.codex.gate = asyncio.Event()
    engine.chat(aid, "go")
    await wait_until(lambda: any(r.running for r in engine.runs.get(aid, [])))
    engine.delete_agent(aid)
    engine.codex.gate.set()
    await settle(engine)
    assert not turn_starts(engine)
    assert not (config.AGENTS_DIR / str(aid)).exists(), "the deleted folder isn't recreated"
    started = [c for c in engine.codex.calls if c[0] == "thread/start"]
    archived = [c[1]["threadId"] for c in engine.codex.calls if c[0] == "thread/archive"]
    assert len(archived) == len(started), "a thread created during setup is archived"


async def test_stop_during_thread_setup_starts_no_turn(engine):
    aid = make(engine)
    engine.codex.gate = asyncio.Event()
    engine.chat(aid, "go")
    await wait_until(lambda: any(r.running for r in engine.runs.get(aid, [])))
    engine.stop_runs(aid)
    engine.codex.gate.set()
    await settle(engine)
    assert not turn_starts(engine)
    assert engine.db.one("SELECT status FROM inbox")["status"] == "cancelled"


async def test_stop_that_loses_the_race_keeps_the_result(engine):
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[("do", lambda: engine.stop_runs(aid)), ("say", "finished anyway")]]
    engine.chat(aid, "go")
    await settle(engine)
    assert engine.db.messages(f"agent:{aid}")[-1]["text"] == "finished anyway"
    assert engine.db.one("SELECT status FROM runs")["status"] == "completed"


async def test_unconfirmed_interrupt_gets_a_fresh_thread(engine, monkeypatch):
    monkeypatch.setattr(config, "TURN_TIMEOUT", 0.05)
    monkeypatch.setattr(config, "INTERRUPT_GRACE", 0.05)
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[("ignore_interrupt",)]]
    engine.chat(aid, "go")
    await settle(engine)
    assert engine.db.agent(aid)["thread_id"] is None
    engine.chat(aid, "again")
    await settle(engine)
    assert len([c for c in engine.codex.calls if c[0] == "thread/start"]) == 2


async def test_turn_start_timeout_gets_a_fresh_thread(engine):
    aid = make(engine)
    real = engine.codex.request

    async def slow(method, params, timeout=60):
        if method == "turn/start":
            raise CodexTimeout("Codex didn't answer turn/start")
        return await real(method, params, timeout)

    engine.codex.request = slow
    engine.chat(aid, "go")
    await settle(engine)
    assert engine.db.agent(aid)["thread_id"] is None


async def test_codex_exit_mid_turn_fails_the_run(engine):
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[("hang",)]]
    engine.chat(aid, "go")
    await wait_running(engine, aid)
    engine.codex.last_error = "Codex app-server exited (code 1)."
    engine._on_notification("qubotu/codexExited", {})
    await settle(engine)
    run = engine.db.one("SELECT * FROM runs")
    assert run["status"] == "failed" and "exited" in run["error"]




async def test_nothing_starts_after_shutdown(engine):
    await engine.start(scheduler=False)
    await engine.stop()
    aid = make(engine)
    engine.chat(aid, "too late")
    await asyncio.sleep(0.05)
    assert not turn_starts(engine)
    assert engine.db.one("SELECT status FROM inbox")["status"] == "pending", "kept for the next start"


async def test_cancelled_startup_does_not_leak_codex(tmp_path, monkeypatch):
    exe = tmp_path / "codex"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).with_name('fake_app_server.py')} \"$@\"\n")
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_INIT_DELAY", "2")
    procs = []
    real_exec = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        p = await real_exec(*args, **kwargs)
        procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)

    async def on_tool(params):
        return True, ""

    client = CodexClient(lambda m, p: None, on_tool)
    client.executable = str(exe)
    task = asyncio.create_task(client.ensure_started())
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await client.stop()
    assert procs and procs[0].returncode is not None, "the half-started process was stopped"
    with pytest.raises(CodexError, match="shutting down"):
        await client.request("echo", {})


def test_profile_denies_the_data_folder_wherever_it_lives(tmp_path, monkeypatch):
    elsewhere = tmp_path / "Volumes" / "qubotu"
    monkeypatch.setattr(config, "HOME", elsewhere)
    fs = codex.agent_filesystem()
    assert f'"{elsewhere}"="deny"' in fs and '"~"="deny"' in fs
    assert '":workspace_roots"={"."="write"}' in fs


def test_reading_outputs_changes_nothing_and_marking_seen_needs_our_origin(engine):
    aid = engine.create_agent(name="Scout", specialty="", goals="", provider="openai", model="", schedule_minutes=0)
    oid = engine.db.insert("outputs", agent_id=aid, kind="finding", title="F", importance="high", created_at=now(), updated_at=now())
    app = create_app(engine, scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.get(f"/agents/{aid}/outputs", headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"})
        assert engine.db.one("SELECT status FROM outputs WHERE id = ?", oid)["status"] == "new"
        assert c.post(f"/agents/{aid}/outputs/seen", headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.post(f"/agents/{aid}/outputs/seen", headers={"Origin": "null"}).status_code == 403
        assert c.post(f"/agents/{aid}/outputs/seen", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 204
        assert engine.db.one("SELECT status FROM outputs WHERE id = ?", oid)["status"] == "seen"
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"host": ""}) as c:
        assert c.get("/").status_code == 403


async def wait_until(predicate, rounds=500):
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")
