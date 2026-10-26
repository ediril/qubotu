from types import SimpleNamespace

from fastapi.testclient import TestClient

from qubotu.db import now
from qubotu.web import create_app


KEY = "test-key"


def client(engine):
    c = TestClient(create_app(engine, scheduler=False, ui_key=KEY), base_url="http://127.0.0.1:8765")
    c.cookies.set("qubotu_key", KEY)
    return c


def test_pages_render(engine):
    with client(engine) as c:
        for path in ["/", "/partials/dashboard", "/memory", "/settings", "/agents/new"]:
            r = c.get(path)
            assert r.status_code == 200, path

        r = c.post("/agents", data={"name": "Scout", "goals": "Track news", "provider": "openai", "schedule_minutes": "60"}, follow_redirects=False)
        assert r.status_code == 303
        aid = int(r.headers["location"].rsplit("/", 1)[1])
        for tab in ["chat", "shared", "settings"]:
            assert c.get(f"/agents/{aid}?tab={tab}").status_code == 200
        assert c.get(f"/agents/{aid}/messages").status_code == 200
        assert c.get(f"/agents/{aid}/outputs").status_code == 200
        assert c.get("/partials/dashboard").status_code == 200
        assert "Scout" in c.get("/partials/sidebar").text


def test_agent_name_validation(engine):
    with client(engine) as c:
        r = c.post("/agents", data={"name": "has space", "provider": "openai"})
        assert "Name must start with a letter" in r.text
        assert engine.db.agents() == []
        c.post("/agents", data={"name": "Scout", "provider": "openai"})
        r = c.post("/agents", data={"name": "scout", "provider": "openai"})
        assert "already exists" in r.text
        r = c.post("/agents", data={"name": "Fw", "provider": "fireworks"})
        assert "model ID" in r.text


def test_memory_forms(engine):
    with client(engine) as c:
        c.post("/memory", data={"text": "Likes teal", "kind": "preference"})
        assert "Likes teal" in c.get("/memory").text
        about = "About me:\nI build agent tools.\nI live in Lisbon."
        c.post("/memory", data={"text": about})
        [m] = [m for m in engine.memory.all() if m["text"] == about]
        assert about in c.get(f"/memory?edit={m['id']}").text, "the edit box keeps every line"
        c.post(f"/memory/{m['id']}/delete")
        mid = engine.memory.all()[0]["id"]
        c.post(f"/memory/{mid}/pin")
        assert engine.memory.get(mid)["pinned"] == 1
        c.post(f"/memory/{mid}/delete")
        assert engine.memory.all() == []


def test_output_card_escapes_and_blocks_js_links(engine):
    aid = engine.create_agent(name="Scout", specialty="", goals="", provider="openai", model="", schedule_minutes=0)
    engine.db.insert("outputs", agent_id=aid, kind="link", title="<script>x</script>", body="<img src=x onerror=alert(1)>",
                     data='{"url": "javascript:alert(1)"}', created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
    with client(engine) as c:
        html = c.get(f"/agents/{aid}/outputs").text
        assert "<script>x</script>" not in html
        assert "<img src=x" not in html
        assert 'href="javascript:' not in html


def test_job_first_line_becomes_summary(engine):
    with client(engine) as c:
        c.post("/agents", data={"name": "Scout", "goals": "Track agent tool releases\nSkip patch releases.", "provider": "openai"})
    agent = engine.db.agent_by_name("Scout")
    assert agent["specialty"] == "Track agent tool releases"
    assert agent["schedule_minutes"] == 1440


def test_space_page_is_gone(engine):
    with client(engine) as c:
        assert c.get("/space").status_code == 404


def test_foreign_hosts_and_origins_are_refused(engine):
    app = create_app(engine, scheduler=False, ui_key=KEY)
    with TestClient(app, base_url="http://attacker.example:8765", cookies={"qubotu_key": KEY}) as c:
        assert c.get("/").status_code == 403, "DNS rebinding: a foreign Host header is refused"
    with TestClient(app, base_url="http://127.0.0.1:8765", cookies={"qubotu_key": KEY}) as c:
        evil = c.post("/agents", data={"name": "Evil", "goals": "x"}, headers={"Origin": "http://attacker.example"})
        assert evil.status_code == 403
        other_port = c.post("/agents", data={"name": "Evil", "goals": "x"}, headers={"Origin": "http://127.0.0.1:3000"})
        assert other_port.status_code == 403
        site = c.post("/memory", data={"text": "hi"}, headers={"Sec-Fetch-Site": "cross-site"})
        assert site.status_code == 403
        assert engine.db.agents() == []
        ok = c.post("/agents", data={"name": "Mine", "goals": "x"}, headers={"Origin": "http://127.0.0.1:8765"}, follow_redirects=False)
        assert ok.status_code == 303
    with TestClient(app, base_url="http://localhost:8765", cookies={"qubotu_key": KEY}) as c:
        assert c.get("/").status_code == 200


def test_rejected_name_never_reaches_script(engine):
    aid = engine.create_agent(name="Scout", specialty="", goals="", provider="openai", model="", schedule_minutes=0)
    with client(engine) as c:
        r = c.post(f"/agents/{aid}/edit", data={"name": "x');alert(1);('", "goals": "g"})
    assert "Name must start with a letter" in r.text
    assert "onsubmit" not in r.text and "onclick" not in r.text
    # The name only appears as attribute text, which the browser never runs.
    assert 'data-confirm="Delete x&#39;);alert(1);(&#39;, its chats' in r.text


def test_sites_connect_grant_and_remove(engine, monkeypatch):
    opened = []
    monkeypatch.setattr(engine.browser, "start_login", opened.append)
    with client(engine) as c:
        r = c.post("/sites", data={"host": "not a site"})
        assert "a site address" in r.text
        c.post("/sites", data={"host": "https://www.X.com/home"})
        [site] = engine.browser.sites()
        assert site["host"] == "x.com" and opened == [site["id"]], "connecting opens the login window"
        assert "x.com" in c.get("/settings").text

        c.post("/agents", data={"name": "Scout", "goals": "Read my X list", "sites": [str(site["id"])]})
        aid = engine.db.agent_by_name("Scout")["id"]
        assert [s["host"] for s in engine.browser.granted(aid)] == ["x.com"]
        assert "checked" in c.get(f"/agents/{aid}?tab=settings").text
        c.post(f"/agents/{aid}/edit", data={"name": "Scout", "goals": "Read my X list"})
        assert engine.browser.granted(aid) == [], "unticking removes the grant"

        c.post(f"/agents/{aid}/edit", data={"name": "Scout", "goals": "x", "sites": [str(site["id"])]})
        c.post(f"/sites/{site['id']}/delete")
        assert engine.browser.sites() == [] and engine.browser.granted(aid) == []


def test_blockers_show_on_every_page_apart_from_notifications(engine):
    with client(engine) as c:
        c.post("/agents", data={"name": "Scout", "goals": "Read my X list"})
        aid = engine.db.agent_by_name("Scout")["id"]
        engine.request_login(aid, "x.com", "To read your AI list.")
        engine.db.insert("outputs", agent_id=aid, kind="finding", title="A finding", importance="high", created_at=now(), updated_at=now())
        for path in ["/", "/memory", "/settings", f"/agents/{aid}"]:
            page = c.get(path).text
            assert "needs you to log in to <strong>x.com</strong>" in page, path
            assert "<title>(1) " in page, path
        assert "badge-blocked" in c.get("/partials/sidebar").text
        engine.runs[aid].append(SimpleNamespace(running=False))  # queued: the blocker doesn't hide the run, nor the run the blocker
        header = c.get(f"/agents/{aid}/header").text
        assert "pill-blocked" in header and "Stop run" in header
        dashboard = c.get("/partials/dashboard").text
        assert "A finding" in dashboard and "Log in to x.com" not in dashboard, "blockers aren't mixed in with findings"

        [b] = engine.blockers()
        assert c.post(f"/outputs/{b['id']}/decline").status_code == 204
        assert "is blocked" not in c.get("/partials/blockers").text


def test_only_a_browser_holding_the_key_can_use_qubotu(engine):
    app = create_app(engine, scheduler=False, ui_key=KEY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as agent:
        # What an agent's shell can do: same machine, our own origin, but no key.
        own = {"Origin": "http://127.0.0.1:8765"}
        assert agent.get("/").status_code == 401 and "qubotu open" in agent.get("/").text
        assert agent.post("/agents", data={"name": "Evil", "goals": "x"}, headers=own).status_code == 401
        assert agent.post("/sites", data={"host": "x.com"}, headers=own).status_code == 401
        assert agent.get("/?key=wrong", follow_redirects=False).status_code == 401
        assert engine.db.agents() == []
        assert agent.get("/static/app.css").status_code == 200

    with TestClient(app, base_url="http://127.0.0.1:8765") as browser:
        r = browser.get(f"/?key={KEY}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", "the key leaves the address bar"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert browser.get("/").status_code == 200, "the cookie keeps the browser in"
