import json

from qubotu.db import later, now

from .conftest import settle


def make(engine, name="Scout", **kw):
    fields = dict(name=name, specialty=f"{name} specialty", goals="", provider="openai", model="", schedule_minutes=0)
    fields.update(kw)
    return engine.create_agent(**fields)


async def test_chat_turn_stores_reply_and_reuses_thread(engine):
    aid = make(engine)
    engine.chat(aid, "hello")
    await settle(engine)
    msgs = engine.db.messages(f"agent:{aid}")
    assert [m["role"] for m in msgs] == ["user", "agent"]
    assert msgs[1]["text"] == "ok"

    engine.chat(aid, "again")
    await settle(engine)
    starts = [c for c in engine.codex.calls if c[0] == "thread/start"]
    assert len(starts) == 1, "the agent's thread should be reused"
    assert starts[0][1]["dynamicTools"], "Qubotu tools are registered on the thread"
    run = engine.db.one("SELECT * FROM runs ORDER BY id DESC")
    assert run["status"] == "completed"


async def test_context_sent_once_until_it_changes(engine):
    aid = make(engine, goals="Track AI news")
    engine.chat(aid, "one")
    await settle(engine)
    engine.chat(aid, "two")
    await settle(engine)
    turns = [c[1]["input"][0]["text"] for c in engine.codex.calls if c[0] == "turn/start"]
    assert "Track AI news" in turns[0]
    assert "Track AI news" not in turns[1]

    engine.update_agent(aid, goals="Track AI and robotics news")
    engine.chat(aid, "three")
    await settle(engine)
    turns = [c[1]["input"][0]["text"] for c in engine.codex.calls if c[0] == "turn/start"]
    assert "robotics" in turns[2]


async def test_context_is_resent_after_codex_compacts_the_thread(engine):
    aid = make(engine, goals="Track AI news")
    engine.codex.scripts["Scout"] = [[("say", "ok")], [("item", {"type": "contextCompaction", "id": "c1"}), ("say", "ok")]]
    for text in ("one", "two", "three"):
        engine.chat(aid, text)
        await settle(engine)
    turns = [c[1]["input"][0]["text"] for c in engine.codex.calls if c[0] == "turn/start"]
    assert "Track AI news" in turns[0]
    assert "Track AI news" not in turns[1]
    assert "Track AI news" in turns[2], "the job and memory are sent again after compaction"


async def test_older_memories_matching_the_message_are_included(engine):
    old = await engine.memory.add("User lives in Lisbon", "fact")
    engine.db.update("memories", old, updated_at="2020-01-01T00:00:00+00:00")
    for i in range(30):
        await engine.memory.add(f"Filler fact number {i}", "fact")
    aid = make(engine)
    engine.chat(aid, "What's the weather like in Lisbon this week?")
    await settle(engine)
    engine.chat(aid, "Summarize robotics news")
    await settle(engine)
    turns = [c[1]["input"][0]["text"] for c in engine.codex.calls if c[0] == "turn/start"]
    assert "User lives in Lisbon" in turns[0], "an old memory outside the profile is recalled by relevance"
    assert "User lives in Lisbon" not in turns[1]


async def test_tools_share_ask_remember(engine):
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[
        ("tool", "share_output", {"kind": "table", "title": "Frameworks", "columns": ["Name"], "rows": [["Codex"]]}),
        ("tool", "ask_user", {"question": "Which region?", "options": ["US", "EU"]}),
        ("tool", "remember", {"text": "User prefers concise summaries", "kind": "preference"}),
        ("say", "done"),
    ]]
    engine.chat(aid, "go")
    await settle(engine)
    outs = engine.db.all("SELECT * FROM outputs ORDER BY id")
    assert [o["kind"] for o in outs] == ["table", "question"]
    assert json.loads(outs[0]["data"])["rows"] == [["Codex"]]
    assert outs[1]["status"] == "open"
    assert engine.memory.all()[0]["text"] == "User prefers concise summaries"
    assert engine.memory.all()[0]["agent_id"] == aid

    engine.answer_question(outs[1]["id"], "EU")
    await settle(engine)
    last_turn = [c[1]["input"][0]["text"] for c in engine.codex.calls if c[0] == "turn/start"][-1]
    assert "Answer: EU" in last_turn
    assert engine.db.one("SELECT status FROM outputs WHERE id = ?", outs[1]["id"])["status"] == "answered"


async def test_request_help_round_trip(engine):
    a = make(engine, "Alice")
    make(engine, "Bob")
    engine.codex.scripts["Alice"] = [
        [("tool", "request_help", {"agent": "bob", "task": "Find three sources"}), ("say", "asked Bob")],
        [("say", "used Bob's sources")],
    ]
    engine.codex.scripts["Bob"] = [[("say", "Here are three sources")]]
    engine.chat(a, "research this")
    await settle(engine)
    req = engine.db.one("SELECT * FROM requests")
    assert req["status"] == "done" and req["result"] == "Here are three sources"
    bob_chat = [m["text"] for m in engine.db.messages(f"agent:{engine.db.agent_by_name('Bob')['id']}")]
    assert any("Request #1 from Alice" in t for t in bob_chat)
    alice_turns = [c[1]["input"][0]["text"] for c in engine.codex.calls if c[0] == "turn/start" and "Result:" in c[1]["input"][0]["text"]]
    assert alice_turns and "Here are three sources" in alice_turns[0]


async def test_request_rejects_self_unknown_and_deep_chains(engine):
    make(engine, "Alice")
    make(engine, "Bob")
    engine.codex.scripts["Alice"] = [[
        ("tool", "request_help", {"agent": "Alice", "task": "x"}),
        ("tool", "request_help", {"agent": "Nobody", "task": "x"}),
        ("say", "ok"),
    ]]
    engine.chat(engine.db.agent_by_name("Alice")["id"], "go")
    await settle(engine)
    results = [r for _, name, r in engine.codex.tool_results if name == "request_help"]
    assert results[0][0] is False and "yourself" in results[0][1]
    assert results[1][0] is False and "Bob" in results[1][1]
    assert engine.db.value("SELECT COUNT(*) FROM requests") == 0






async def test_scheduler_collapses_missed_runs(engine):
    aid = make(engine, schedule_minutes=60)
    engine.db.update("agents", aid, next_run_at="2000-01-01T00:00:00+00:00")
    assert engine.run_due_agents() == [aid]
    await settle(engine)
    agent = engine.db.agent(aid)
    assert agent["next_run_at"] > now()
    assert engine.run_due_agents() == []
    assert engine.db.value("SELECT COUNT(*) FROM runs WHERE trigger = 'schedule'") == 1


async def test_paused_agents_are_not_scheduled(engine):
    aid = make(engine, schedule_minutes=15)
    engine.set_paused(aid, True)
    engine.db.update("agents", aid, next_run_at="2000-01-01T00:00:00+00:00")
    assert engine.run_due_agents() == []


async def test_feedback_reaches_next_turn(engine):
    aid = make(engine)
    engine.chat(aid, "first")
    await settle(engine)
    oid = engine.db.insert("outputs", agent_id=aid, kind="finding", title="A finding", created_at=now(), updated_at=now())
    engine.db.update("outputs", oid, feedback="Useful: more like this", feedback_pending=1)
    engine.chat(aid, "second")
    await settle(engine)
    turn = [c[1]["input"][0]["text"] for c in engine.codex.calls if c[0] == "turn/start"][-1]
    assert "more like this" in turn
    assert engine.db.one("SELECT feedback_pending FROM outputs WHERE id = ?", oid)["feedback_pending"] == 0


async def test_failed_turn_is_recorded(engine):
    aid = make(engine)

    async def boom(*a, **k):
        from qubotu.codex import CodexError
        raise CodexError("not signed in")

    engine.codex.ensure_started = boom
    engine.chat(aid, "hi")
    await settle(engine)
    run = engine.db.one("SELECT * FROM runs")
    assert run["status"] == "failed" and "not signed in" in run["error"]
    assert engine.db.messages(f"agent:{aid}")[-1]["role"] == "system"


async def test_provider_change_starts_new_thread(engine):
    aid = make(engine)
    engine.chat(aid, "hi")
    await settle(engine)
    engine.update_agent(aid, provider="fireworks", model="accounts/fireworks/models/x")
    engine.chat(aid, "hi again")
    await settle(engine)
    starts = [c[1] for c in engine.codex.calls if c[0] == "thread/start"]
    assert len(starts) == 2 and starts[1]["modelProvider"] == "fireworks"




async def test_tool_set_change_starts_fresh_thread(engine, monkeypatch):
    from qubotu import tools

    aid = make(engine)
    engine.chat(aid, "hi")
    await settle(engine)
    old_thread = engine.db.agent(aid)["thread_id"]
    monkeypatch.setattr(tools, "FINGERPRINT", "changed-tools")
    engine.loaded.clear()  # as after a daemon restart
    engine.chat(aid, "hi again")
    await settle(engine)
    starts = [c for c in engine.codex.calls if c[0] == "thread/start"]
    assert len(starts) == 2, "a thread started with old tools is replaced"
    assert not [c for c in engine.codex.calls if c[0] == "thread/resume"]
    assert engine.db.agent(aid)["thread_tools"] == "changed-tools"
    archived = [c[1]["threadId"] for c in engine.codex.calls if c[0] == "thread/archive"]
    assert archived == [old_thread]
    assert engine.db.agent(aid)["thread_id"] != old_thread


async def test_agent_threads_run_under_the_restricted_profile(engine):
    from qubotu.codex import AGENT_PROFILE, CodexClient

    aid = make(engine)
    engine.chat(aid, "hi")
    await settle(engine)
    start = next(c[1] for c in engine.codex.calls if c[0] == "thread/start")
    assert start["permissions"] == AGENT_PROFILE and "sandbox" not in start
    turn = next(c[1] for c in engine.codex.calls if c[0] == "turn/start")
    assert "sandboxPolicy" not in turn, "a per-turn sandbox would override the profile"
    args = " ".join(CodexClient(None, None)._args())
    assert f'permissions.{AGENT_PROFILE}.filesystem={{":root"="read","~"="deny"' in args


async def test_agent_is_not_run_without_the_restricted_profile(engine):
    aid = make(engine)
    real = engine.codex.request

    async def ignores_profiles(method, params, timeout=60):
        r = await real(method, params, timeout)
        r.pop("activePermissionProfile", None)
        return r

    engine.codex.request = ignores_profiles
    engine.chat(aid, "hi")
    await settle(engine)
    assert not [c for c in engine.codex.calls if c[0] == "turn/start"]
    run = engine.db.one("SELECT * FROM runs")
    assert run["status"] == "failed" and "permissions" in run["error"]


async def test_page_tools_use_the_runs_tab_and_close_it_after(engine, monkeypatch):
    calls = []

    async def fake_open(key, agent_id, url):
        calls.append(("open", key, agent_id, url))
        engine.browser.tabs[key] = object()
        return "URL: " + url

    async def fake_close(key):
        calls.append(("close", key))
        engine.browser.tabs.pop(key, None)

    monkeypatch.setattr(engine.browser, "open", fake_open)
    monkeypatch.setattr(engine.browser, "close_tab", fake_close)
    aid = make(engine)
    engine.codex.scripts["Scout"] = [[("tool", "open_page", {"url": "news.ycombinator.com"}), ("say", "read it")]]
    engine.chat(aid, "what's on HN?")
    await settle(engine)
    item = engine.db.one("SELECT id FROM inbox WHERE kind = 'chat'")["id"]
    assert calls == [("open", item, aid, "news.ycombinator.com"), ("close", item)]
    assert engine.codex.tool_results[-1] == ("Scout", "open_page", (True, "URL: news.ycombinator.com"))
