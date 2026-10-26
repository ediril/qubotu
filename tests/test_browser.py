import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from qubotu import browser as browser_mod
from qubotu import config, prompts
from qubotu.browser import Browser, BrowserError, new_lines, normalize_host
from qubotu.db import DB

needs_chrome = pytest.mark.skipif(
    not Path("/Applications/Google Chrome.app").exists(), reason="needs Google Chrome"
)

FEED = """<body style="margin:0"><div id="spacer" style="height:6000px;position:relative"></div><script>
function draw() {
  const top = scrollY - 200, bottom = scrollY + innerHeight + 200, s = document.getElementById('spacer');
  s.innerHTML = '';
  for (let i = 1; i <= 60; i++) {
    const y = (i - 1) * 100;
    if (y + 100 < top || y > bottom) continue;
    const d = document.createElement('div');
    d.style = `position:absolute;top:${y}px;height:100px`;
    d.textContent = 'Item ' + i;
    s.appendChild(d);
  }
}
addEventListener('scroll', draw); draw();
</script></body>"""


class Site(BaseHTTPRequestHandler):
    posts: list[str] = []
    gets: list[str] = []

    def log_message(self, *a):
        pass

    def page(self, body: str, headers: dict | None = None):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        url = urlsplit(self.path)
        port = self.server.server_address[1]
        Site.gets.append(url.path)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(400)
            self.end_headers()
        elif url.path == "/":
            self.page(
                f"<p>cookie: {self.headers.get('Cookie') or 'none'}</p><a href='/next'>Next page</a>"
                "<form action='/search'><input name='q' placeholder='Search'></form>"
                "<form method='post' action='/like'><button>Like</button></form>"
                "<form method='post' action='/like-popup' target='_blank'><button>Like in a new window</button></form>"
                f"<a href='http://127.0.0.1:{port}/next'>Elsewhere</a>"
            )
        elif url.path == "/next":
            self.page("<p>Second page</p>")
        elif url.path == "/search":
            self.page(f"<p>Results for {parse_qs(url.query)['q'][0]}</p>")
        elif url.path == "/feed":
            self.page(FEED)
        elif url.path == "/redirect-away":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/next")
            self.end_headers()
        elif url.path == "/account":
            self.page("<a href='/logout'>Log out</a><p>Your account</p>")
        elif url.path == "/logout":
            self.page("<p>Logged out</p>")
        elif url.path == "/chat":
            self.page(f"<p>Chat</p><script>new WebSocket('ws://' + location.host + '/socket')</script>")
        elif url.path == "/api-elsewhere":
            self.page(
                f"<p>App</p><script>fetch('http://127.0.0.1:{port}/like-elsewhere', "
                "{method: 'POST', mode: 'no-cors', credentials: 'include'})</script>"
            )
        elif url.path == "/login":
            self.page("<p>Logged in</p>", {"Set-Cookie": "session=abc; Path=/; Max-Age=3600"})

    def do_POST(self):
        Site.posts.append(self.path)
        self.page("<p>Liked</p>")


@pytest.fixture
def server():
    Site.posts = []
    Site.gets = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
async def browser(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROWSER_DIR", tmp_path / "browser")
    monkeypatch.setattr(browser_mod, "PACE", (0.1, 0.1))
    monkeypatch.setattr(browser_mod, "TYPING", (0, 0))
    monkeypatch.setattr(browser_mod, "SETTLE_POLL", 0.05)
    monkeypatch.setattr(browser_mod, "WHEEL_PAUSE", (0, 0))
    monkeypatch.setattr(browser_mod, "ALLOW_LOCAL", True)
    b = Browser(DB(tmp_path / "q.db"))
    yield b
    await b.stop()


async def log_in(b: Browser, host: str, port: int) -> int:
    """Stand in for the user logging in: the site sets a session cookie in its profile."""
    site_id = b.add_site(host)
    ctx = await b._site_context(b.db.one("SELECT * FROM sites WHERE id = ?", site_id))
    page = await ctx.new_page()
    await page.goto(f"http://{host}:{port}/login")
    await b._close_site(site_id)
    return site_id


def test_normalize_host():
    assert normalize_host("https://www.X.com/home") == "x.com"
    assert normalize_host(" news.ycombinator.com ") == "news.ycombinator.com"
    assert normalize_host("not a site") is None
    assert normalize_host("") is None


def test_new_lines_handles_growing_and_virtualized_pages():
    assert new_lines(["a", "b", "c"], ["a", "b", "c", "d", "e"]) == ["d", "e"]
    assert new_lines(["a", "b", "c", "d"], ["c", "d", "e", "f"]) == ["e", "f"], "earlier lines dropped from the page"
    assert new_lines(["a", "b"], ["x", "y"]) == ["x", "y"]
    assert new_lines(["nav", "p1", "p2", "footer"], ["nav", "p2", "p3", "p4", "footer"]) == ["p3", "p4"], (
        "a fixed footer after the feed doesn't hide what's new"
    )
    assert new_lines(["nav", "Ann", "hi", "Bob", "yo", "side"], ["nav", "Bob", "yo", "Ann", "again", "side"]) == ["Ann", "again"], (
        "a new post by an author already seen keeps its author line"
    )


def test_sites_appear_in_the_agent_profile():
    agent = {"name": "Scout", "goals": "", "schedule_minutes": 0}
    assert "x.com" in prompts.profile_block(agent, [], ["x.com"])
    assert "logged in" not in prompts.profile_block(agent, [])


@needs_chrome
async def test_read_click_and_type_on_a_public_page(browser, server):
    text = await browser.open(1, 7, f"http://127.0.0.1:{server}/")
    assert "cookie: none" in text and "Not logged in" not in text
    assert '"Next page"' in text and '"Search"' in text
    ref = next(line.split("]")[0][1:] for line in text.splitlines() if '"Next page"' in line)
    assert "Second page" in await browser.click(1, ref)

    text = await browser.open(1, 7, f"http://127.0.0.1:{server}/")
    ref = next(line.split("]")[0][1:] for line in text.splitlines() if '"Search"' in line)
    assert "Results for teal" in await browser.type(1, ref, "teal", submit=True)


@needs_chrome
async def test_scrolling_collects_a_feed_that_drops_old_items(browser, server):
    first = await browser.open(1, 7, f"http://127.0.0.1:{server}/feed")
    collected = first + "\n" + await browser.scroll(1, 10) + "\n" + await browser.scroll(1, 10)
    assert all(f"Item {i}\n" in collected + "\n" for i in range(1, 61)), "every item, though the page only ever shows a few"
    assert "Reached the end" in collected


@needs_chrome
async def test_logged_in_site_is_read_only_and_stays_on_its_site(browser, server):
    site_id = await log_in(browser, "localhost", server)
    browser.db.insert("agent_sites", agent_id=7, site_id=site_id)

    text = await browser.open(1, 7, f"http://localhost:{server}/")
    assert "cookie: session=abc" in text, "the login from the site's profile is used"
    assert "Read-only" in text

    like = next(line.split("]")[0][1:] for line in text.splitlines() if '"Like"' in line)
    assert "because localhost is read-only" in await browser.click(1, like)
    assert Site.posts == [], "the change never reached the site"

    text = await browser.read(1)
    popup = next(line.split("]")[0][1:] for line in text.splitlines() if '"Like in a new window"' in line)
    await browser.click(1, popup)
    assert Site.posts == [], "a popup the page opens is read-only too"

    text = await browser.read(1)
    away = next(line.split("]")[0][1:] for line in text.splitlines() if '"Elsewhere"' in line)
    assert "127.0.0.1 is outside localhost" in await browser.click(1, away)


@needs_chrome
async def test_only_granted_agents_get_the_login(browser, server):
    await log_in(browser, "localhost", server)
    text = await browser.open(2, 8, f"http://localhost:{server}/")
    assert "cookie: none" in text
    assert "can let you use their localhost login" in text
    await browser.click(2, next(line.split("]")[0][1:] for line in text.splitlines() if '"Like"' in line))
    assert Site.posts == ["/like"], "logged-out browsing isn't read-only"


@needs_chrome
async def test_pages_carry_the_users_chrome_identity(browser, server):
    await browser.open(1, 7, f"http://127.0.0.1:{server}/")
    seen = await browser.tabs[1].page.evaluate(
        "async () => ({ua: navigator.userAgent, webdriver: navigator.webdriver, "
        "hints: await navigator.userAgentData.getHighEntropyValues(['platformVersion', 'fullVersionList']), "
        "brands: navigator.userAgentData.brands.map(b => b.brand)})"
    )
    me = browser_mod.identity()
    assert seen["ua"] == me["userAgent"] and "Headless" not in seen["ua"]
    assert "HeadlessChrome" not in seen["brands"] and "Google Chrome" in seen["brands"]
    assert seen["hints"]["platformVersion"] == me["userAgentMetadata"]["platformVersion"] != "10_15_7"
    assert seen["webdriver"] is False


async def logged_in_tab(browser, server, path: str) -> str:
    site_id = await log_in(browser, "localhost", server)
    browser.db.insert("agent_sites", agent_id=7, site_id=site_id)
    return await browser.open(1, 7, f"http://localhost:{server}{path}")


@needs_chrome
async def test_logged_in_pages_send_no_websocket_traffic(browser, server):
    await browser.open(2, 8, f"http://localhost:{server}/chat")
    assert "/socket" in Site.gets, "logged out, the page's socket connects"
    Site.gets.clear()
    await logged_in_tab(browser, server, "/chat")
    assert "/socket" not in Site.gets, "logged in, it never leaves the browser"


@needs_chrome
async def test_logged_in_pages_change_nothing_on_other_hosts_either(browser, server):
    await logged_in_tab(browser, server, "/api-elsewhere")
    assert "/like-elsewhere" not in Site.posts, "a site's API on another domain is read-only too"


@needs_chrome
async def test_a_redirect_never_shows_another_site_in_the_logged_in_tab(browser, server):
    with pytest.raises(BrowserError, match="outside localhost"):
        await logged_in_tab(browser, server, "/redirect-away")
    assert browser.tabs[1].page.url == "about:blank"


@needs_chrome
async def test_logout_links_are_not_followed(browser, server):
    text = await logged_in_tab(browser, server, "/account")
    logout = next(line.split("]")[0][1:] for line in text.splitlines() if '"Log out"' in line)
    assert "logging out of localhost" in await browser.click(1, logout)
    assert "/logout" not in Site.gets


@needs_chrome
async def test_local_addresses_are_refused(browser, server, monkeypatch):
    monkeypatch.setattr(browser_mod, "ALLOW_LOCAL", False)
    for url in (f"http://127.0.0.1:{server}/", "http://localhost:8765/", "http://192.168.1.1/", "http://router.local/"):
        with pytest.raises(BrowserError, match="Local and private"):
            await browser.open(1, 7, url)
    assert Site.gets == []


@needs_chrome
async def test_tab_errors_are_plain(browser, server):
    with pytest.raises(BrowserError, match="open_page first"):
        await browser.read(1)
    with pytest.raises(BrowserError, match="http and https"):
        await browser.open(1, 7, "file:///etc/passwd")
    await browser.open(1, 7, f"http://127.0.0.1:{server}/")
    with pytest.raises(BrowserError, match="ref must look like"):
        await browser.click(1, "#evil")


async def test_closing_the_login_window_reports_the_site_once(browser, monkeypatch):
    import asyncio

    closed = asyncio.Event()

    async def window(site_id):
        await closed.wait()

    told = []
    browser.on_logged_in = told.append
    monkeypatch.setattr(browser, "_login", window)
    site_id = browser.add_site("x.com")
    browser.start_login(site_id)
    browser.start_login(site_id)
    assert list(browser.logging_in) == [site_id], "one window, however many asked"
    closed.set()
    await asyncio.sleep(0.01)
    assert told == [site_id] and not browser.logging_in


async def test_a_login_window_and_an_agent_never_share_a_profile(browser, monkeypatch):
    import asyncio

    running, peak, contexts = 0, 0, []

    class Page:
        url = "about:blank"

        async def goto(self, *a, **k):
            raise RuntimeError("offline")

        async def close(self):
            pass

    class Context:
        def __init__(self, headless):
            self.headless, self.pages, self.on_close = headless, [], None

        def on(self, event, callback):
            if event == "close":
                self.on_close = callback

        async def route(self, *a):
            pass

        async def route_web_socket(self, *a):
            pass

        async def new_page(self):
            return Page()

        async def close(self):
            nonlocal running
            running -= 1
            if self.on_close:
                self.on_close(None)

    async def launch(pw, site_id, headless):
        nonlocal running, peak
        await asyncio.sleep(0.05)  # Chrome takes a moment to start
        running += 1
        peak = max(peak, running)
        contexts.append(Context(headless))
        return contexts[-1]

    async def nothing(*a):
        return None

    monkeypatch.setattr(browser, "_launch_profile", launch)
    monkeypatch.setattr(browser, "_playwright", nothing)
    monkeypatch.setattr(browser, "_dress", nothing)
    site_id = browser.add_site("x.com")
    browser.db.insert("agent_sites", agent_id=7, site_id=site_id)

    opening = asyncio.create_task(browser.open(1, 7, "https://x.com/"))
    await asyncio.sleep(0.01)  # the agent's Chrome is starting
    browser.start_login(site_id)
    with pytest.raises(BrowserError):
        await opening
    await asyncio.sleep(0.2)
    assert [c.headless for c in contexts] == [True, False], "the agent's Chrome, then the login window"
    assert peak == 1, "never two Chromes on one profile"
    with pytest.raises(BrowserError, match="logging in"):
        await browser.open(2, 7, "https://x.com/")

    await contexts[-1].close()  # the user closes the login window
    await asyncio.sleep(0.01)
    assert not browser.logging_in
