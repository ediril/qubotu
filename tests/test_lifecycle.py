import asyncio
import json

from qubotu import config
from qubotu.codex import CodexError
from qubotu.db import now
from qubotu.engine import Engine

from .conftest import FakeCodex, FakeEmbedder, settle


def make(engine, name="Scout", **kw):
    fields = dict(name=name, specialty=f"{name} job", goals="", provider="openai", model="", schedule_minutes=0)
    fields.update(kw)
    return engine.create_agent(**fields)


def turn_starts(engine):
    return [c[1] for c in engine.codex.calls if c[0] == "turn/start"]


def interrupts(engine):
    return [c[1] for c in engine.codex.calls if c[0] == "turn/interrupt"]


async def wait_running(engine, agent_id):
    for _ in range(500):
        if any(r.running and r.turn_id for r in engine.runs.get(agent_id, [])):
            return
        await asyncio.sleep(0)
    raise AssertionError("run never started")


async def test_timeout_interrupts_the_codex_turn(engine, monkeypatch):
    monkeypatch.setattr(config, "TURN_TIMEOUT", 0.05)
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[("hang",)], [("say", "next turn fine")]]
    engine.chat(aid, "work")
    await settle(engine)
    assert len(interrupts(engine)) == 1, "the timed-out turn is interrupted in Codex"
    assert engine.db.one("SELECT * FROM runs")["error"] == "The run timed out."
    engine.chat(aid, "again")
    await settle(engine)
    assert engine.db.messages(f"agent:{aid}")[-1]["text"] == "next turn fine"


async def test_late_events_from_an_old_turn_are_ignored(engine):
    aid = make(engine)
    # The no-op steps give the engine time to act on the stale completion before the real answer.
    idle = [("do", lambda: None)] * 5
    engine.codex.scripts["Scout"] = [[("late", "stale answer"), *idle, ("say", "fresh answer")]]
    engine.chat(aid, "go")
    await settle(engine)
    assert engine.db.messages(f"agent:{aid}")[-1]["text"] == "fresh answer"
    assert engine.db.one("SELECT status FROM runs")["status"] == "completed"


async def test_stop_while_other_runs_finish(engine):
    a, b = make(engine, "Alice"), make(engine, "Bob")
    engine.codex.scripts["Alice"] = [[("hang",)]]
    engine.chat(a, "long")
    engine.chat(b, "short")
    await wait_running(engine, a)
    engine.stop_runs(a)
    await settle(engine)
    runs = {engine.db.agent(r["agent_id"])["name"]: r for r in engine.db.all("SELECT * FROM runs")}
    assert runs["Alice"]["status"] == "interrupted" and runs["Bob"]["status"] == "completed"
    assert engine.status(engine.db.agent(a)) == "idle"


async def test_stop_cancels_queued_runs(engine):
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[("hang",)]]
    engine.chat(aid, "first")
    engine.chat(aid, "second")
    await wait_running(engine, aid)
    assert engine.status(engine.db.agent(aid)) == "running"
    engine.stop_runs(aid)
    await settle(engine)
    assert [i["status"] for i in engine.db.all("SELECT status FROM inbox ORDER BY id")] == ["cancelled", "cancelled"]
    assert len(turn_starts(engine)) == 1, "the queued chat never ran"
    assert engine.status(engine.db.agent(aid)) == "idle"


async def test_pause_drops_queued_scheduled_runs(engine):
    engine.semaphore = asyncio.Semaphore(1)
    busy, aid = make(engine, "Busy"), make(engine, "Scout", schedule_minutes=60)
    engine.codex.scripts["Busy"] = [[("hang",)]]
    engine.chat(busy, "occupy the only slot")
    await wait_running(engine, busy)
    engine.db.update("agents", aid, next_run_at="2000-01-01T00:00:00+00:00")
    assert engine.run_due_agents() == [aid]
    engine.set_paused(aid, True)
    engine.stop_runs(busy)
    await settle(engine)
    assert not engine.db.all("SELECT * FROM runs WHERE agent_id = ?", aid)


async def test_scheduled_item_rechecks_pause_at_dispatch(engine):
    aid = make(engine, schedule_minutes=60)
    engine.db.update("agents", aid, status="paused")
    engine.enqueue(aid, "schedule")
    await settle(engine)
    assert engine.db.one("SELECT status FROM inbox")["status"] == "done"
    assert not engine.db.all("SELECT * FROM runs")


async def test_unexpected_error_does_not_leave_agent_running(engine):
    aid = make(engine)
    real = engine.codex.request

    async def broken(method, params, timeout=60):
        if method == "turn/start":
            raise RuntimeError("boom")
        return await real(method, params, timeout)

    engine.codex.request = broken
    engine.chat(aid, "hi")
    await settle(engine)
    assert engine.status(engine.db.agent(aid)) == "idle"
    run = engine.db.one("SELECT * FROM runs")
    assert run["status"] == "failed" and "boom" in run["error"]


async def test_deleting_a_running_agent_leaves_nothing_behind(engine):
    aid = make(engine)
    (config.AGENTS_DIR / str(aid) / "NOTES.md").write_text("old tasks")
    engine.codex.scripts["Scout"] = [[
        ("do", lambda: engine.delete_agent(aid)),
        ("tool", "share_output", {"kind": "finding", "title": "late"}),
        ("say", "late reply"),
    ]]
    engine.chat(aid, "work")
    await settle(engine)
    assert engine.codex.tool_results[0][2][0] is False, "a winding-down turn can't write for a deleted agent"
    for table in ("messages", "outputs", "runs", "inbox"):
        assert not engine.db.all(f"SELECT * FROM {table} WHERE agent_id = ?", aid), table
    assert not (config.AGENTS_DIR / str(aid)).exists()
    new = make(engine, "Next")
    assert new != aid and not (config.AGENTS_DIR / str(new) / "NOTES.md").exists()


async def test_pending_work_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    first = Engine(tmp_path / "q.db")
    a = first.db.insert("agents", name="Alice", created_at=now())
    b = first.db.insert("agents", name="Bob", created_at=now())
    first.db.add_message(f"agent:{a}", "user", "unanswered question", a)
    first.db.insert("inbox", agent_id=a, kind="chat", payload=json.dumps({"text": "unanswered question"}), status="running", created_at=now())
    rid = first.db.insert("requests", from_agent=a, to_agent=b, task="help", status="running", created_at=now())
    first.db.insert("inbox", agent_id=b, kind="request", payload=json.dumps({"request_id": rid}), status="pending", created_at=now())

    second = Engine(tmp_path / "q.db")
    second.memory.embedder = FakeEmbedder()
    second.codex = FakeCodex(second)
    second.codex.scripts["Bob"] = [[("say", "here is help")]]
    await second.start(scheduler=False)
    await settle(second)
    assert second.db.messages(f"agent:{a}")[-1]["text"] != "unanswered question", "the chat was answered"
    assert second.db.one("SELECT status, result FROM requests")["status"] == "done"
    kinds = [(i["kind"], i["status"]) for i in second.db.all("SELECT kind, status FROM inbox ORDER BY id")]
    assert kinds == [("chat", "done"), ("request", "done"), ("result", "done")]
    await second.stop()


async def test_undelivered_answer_reopens_the_question(engine):
    aid = make(engine)
    oid = engine.db.insert("outputs", agent_id=aid, kind="question", title="Which?", status="open", created_at=now(), updated_at=now())

    async def down(method, params, timeout=60):
        raise CodexError("Codex is down")

    engine.codex.ensure_started = lambda: down("", {})
    engine.answer_question(oid, "EU")
    await settle(engine)
    q = engine.db.one("SELECT * FROM outputs WHERE id = ?", oid)
    assert q["status"] == "open" and q["answer"] == "EU"




async def test_feedback_edited_mid_turn_stays_pending(engine):
    aid = make(engine)
    oid = engine.db.insert("outputs", agent_id=aid, kind="finding", title="F", feedback="Useful", feedback_pending=1, created_at=now(), updated_at=now())
    engine.codex.scripts["Scout"] = [
        [("do", lambda: engine.db.update("outputs", oid, feedback="Not useful: wrong", feedback_pending=1)), ("say", "ok")]
    ]
    engine.chat(aid, "go")
    await settle(engine)
    o = engine.db.one("SELECT * FROM outputs WHERE id = ?", oid)
    assert o["feedback_pending"] == 1 and o["feedback"] == "Not useful: wrong"


async def test_failed_turn_keeps_feedback_pending(engine, monkeypatch):
    monkeypatch.setattr(config, "TURN_TIMEOUT", 0.05)
    aid = make(engine)
    oid = engine.db.insert("outputs", agent_id=aid, kind="finding", title="F", feedback="Useful", feedback_pending=1, created_at=now(), updated_at=now())
    engine.codex.scripts["Scout"] = [[("hang",)]]
    engine.chat(aid, "go")
    await settle(engine)
    assert engine.db.one("SELECT feedback_pending FROM outputs WHERE id = ?", oid)["feedback_pending"] == 1


async def test_clearing_the_model_switches_back_to_the_default(engine):
    aid = make(engine, model="gpt-other")
    engine.chat(aid, "one")
    await settle(engine)
    engine.update_agent(aid, model="")
    engine.chat(aid, "two")
    await settle(engine)
    assert [t.get("model") for t in turn_starts(engine)] == ["gpt-other", "gpt-test"]






async def test_tool_limits_are_clamped(engine):
    aid = make(engine)
    for i in range(3):
        engine.db.insert("outputs", agent_id=aid, kind="finding", title=f"F{i}", created_at=now(), updated_at=now())
    engine.codex.scripts["Scout"] = [[("tool", "list_shared", {"limit": -5}), ("tool", "update_memory", {"id": 1, "text": " "}), ("say", "ok")]]
    engine.chat(aid, "go")
    await settle(engine)
    (_, _, (ok, text)), (_, _, (ok2, _)) = engine.codex.tool_results
    assert ok and text.count("#") == 1, "a negative limit becomes 1, not unlimited"
    assert not ok2, "a memory can't be emptied"


async def test_all_pinned_memories_are_in_the_profile(engine):
    for i in range(30):
        mid = await engine.memory.add(f"pinned fact {i}")
        await engine.memory.update(mid, pinned=True)
    await engine.memory.add("unpinned fact")
    profile = engine.memory.profile(limit=25)
    assert len(profile) == 30 and all("pinned" in m["text"] for m in profile)


async def test_stop_catches_work_queued_a_moment_ago(engine):
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[("hang",)]]
    engine.chat(aid, "first")
    await wait_running(engine, aid)
    engine.chat(aid, "just now")
    assert engine.status(engine.db.agent(aid)) == "running"
    engine.stop_runs(aid)
    await settle(engine)
    assert [i["status"] for i in engine.db.all("SELECT status FROM inbox ORDER BY id")] == ["cancelled", "cancelled"]
    assert len(turn_starts(engine)) == 1


async def test_queued_shows_immediately(engine):
    aid = make(engine)
    engine.chat(aid, "hi")
    assert engine.status(engine.db.agent(aid)) == "queued"
    await settle(engine)
    assert engine.status(engine.db.agent(aid)) == "idle"
