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
        if url.path == "/":
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
        elif url.path == "/login":
            self.page("<p>Logged in</p>", {"Set-Cookie": "session=abc; Path=/; Max-Age=3600"})

    def do_POST(self):
        Site.posts.append(self.path)
        self.page("<p>Liked</p>")


@pytest.fixture
def server():
    Site.posts = []
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
    assert "because it's read-only" in await browser.click(1, like)
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


@needs_chrome
async def test_tab_errors_are_plain(browser, server):
    with pytest.raises(BrowserError, match="open_page first"):
        await browser.read(1)
    with pytest.raises(BrowserError, match="http and https"):
        await browser.open(1, 7, "file:///etc/passwd")
    await browser.open(1, 7, f"http://127.0.0.1:{server}/")
    with pytest.raises(BrowserError, match="ref must look like"):
        await browser.click(1, "#evil")


async def test_everyone_waiting_on_a_login_window_hears_when_it_closes(browser, monkeypatch):
    import asyncio

    closed = asyncio.Event()

    async def window(site_id):
        await closed.wait()

    monkeypatch.setattr(browser, "_login", window)
    site_id = browser.add_site("x.com")
    told = []
    browser.start_login(site_id, on_done=lambda: told.append("settings"))
    browser.start_login(site_id, on_done=lambda: told.append("agent"))
    assert list(browser.logging_in) == [site_id], "one window, however many asked"
    closed.set()
    await asyncio.sleep(0.01)
    assert told == ["settings", "agent"] and not browser.logging_in
