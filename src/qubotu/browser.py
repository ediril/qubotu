"""Chrome, driven by the daemon on agents' behalf.

Agents never touch a browser profile. They call Qubotu's page tools, and Qubotu runs Chrome.
Public browsing uses a fresh, logged-out context for each run. Each site the user connects gets
its own persistent profile under the data folder, which agents can't read. A site's profile is
used only for that site, and only to read: requests that would change data there are blocked.
"""

import asyncio
import difflib
import functools
import logging
import platform
import plistlib
import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .db import DB, now

log = logging.getLogger(__name__)

# Seconds to pause after each action, milliseconds between typed keys, and seconds between the
# small wheel steps of one scroll, so agents browse at a person's pace.
PACE = (0.6, 1.4)
TYPING = (30, 80)
WHEEL_PAUSE = (0.05, 0.15)
# Seconds between checks while waiting for a page's text to stop changing, and the most to wait.
SETTLE_POLL = 0.3
SETTLE_LIMIT = 10
MAX_TEXT = 8000
MAX_ELEMENTS = 80
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# Bot checks aren't data changes. Blocking them could get the agent treated as a bot.
CHALLENGE_PATH = "/cdn-cgi/challenge-platform/"
CHALLENGE_HOSTS = ("challenges.cloudflare.com",)
CHROME_ARGS = ["--disable-blink-features=AutomationControlled"]
CHROME_APP = Path("/Applications/Google Chrome.app")
# A common MacBook screen, instead of headless Chrome's telltale 1280x720.
SCREEN = {"width": 1512, "height": 982}
VIEWPORT = {"width": 1440, "height": 820}
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$|^localhost$")

SNAPSHOT_JS = """(max) => {
  document.querySelectorAll('[data-qb-ref]').forEach(e => e.removeAttribute('data-qb-ref'));
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], ' +
    '[role=tab], [role=menuitem], [role=checkbox], [contenteditable=""], [contenteditable=true]';
  const elements = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.bottom < 0 || r.top > innerHeight * 2) continue;
    const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') ||
      el.getAttribute('title') || el.getAttribute('name') || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    const ref = 'e' + (elements.length + 1);
    el.setAttribute('data-qb-ref', ref);
    const kind = el.getAttribute('role') || (el.tagName === 'INPUT' ? 'input:' + (el.type || 'text') : el.tagName.toLowerCase());
    elements.push({ref, kind, name, href: el.tagName === 'A' ? el.getAttribute('href') : null});
    if (elements.length >= max) break;
  }
  return {url: location.href, title: document.title, text: document.body ? document.body.innerText : '', elements};
}"""


class BrowserError(Exception):
    pass


def normalize_host(text: str) -> str | None:
    """'https://www.X.com/home' -> 'x.com'. None if it isn't a host name."""
    text = text.strip().lower()
    host = urlsplit(text if "//" in text else f"//{text}").hostname or ""
    host = host.removeprefix("www.")
    return host if HOST_RE.match(host) else None


@functools.cache
def identity() -> dict:
    """How the user's own Chrome identifies itself: its version and the real macOS version. Headless
    Chrome would otherwise announce itself and report a fake macOS version, and some sites refuse it."""
    try:
        with open(CHROME_APP / "Contents" / "Info.plist", "rb") as f:
            version = plistlib.load(f)["CFBundleShortVersionString"]
    except (OSError, KeyError, plistlib.InvalidFileException):
        raise BrowserError("Google Chrome isn't installed. Install it from google.com/chrome.") from None
    major = version.split(".")[0]
    mac = (platform.mac_ver()[0] or "15.0").split(".")
    brands = [("Google Chrome", major, version), ("Not_A Brand", "8", "8.0.0.0"), ("Chromium", major, version)]
    return {
        # Chrome fixes the macOS part of its user agent at 10_15_7; the real version goes in the hints below.
        "userAgent": f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        "acceptLanguage": "en-US,en",
        "platform": "MacIntel",
        "userAgentMetadata": {
            "brands": [{"brand": b, "version": v} for b, v, _ in brands],
            "fullVersionList": [{"brand": b, "version": full} for b, _, full in brands],
            "fullVersion": version,
            "platform": "macOS",
            "platformVersion": ".".join((mac + ["0", "0"])[:3]),
            "architecture": "arm" if platform.machine() == "arm64" else "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        },
    }


def on_host(host: str, site: str) -> bool:
    return host == site or host.endswith("." + site)


def new_lines(before: list[str], after: list[str]) -> list[str]:
    """The lines of `after` that weren't in `before`, in page order. Works for pages that grow as you
    scroll, pages that drop what scrolled out of view (virtualized lists, like X), and pages with
    fixed content (menus, sidebars, footers) around the part that changes."""
    ops = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    return [line for tag, _, _, j1, j2 in ops if tag in ("insert", "replace") for line in after[j1:j2]]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _cap(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n… (cut off; scroll_page reads further)"


@dataclass
class Tab:
    page: object
    site: dict | None  # the connected site whose login this tab uses, or None when logged out
    context: object = None  # a logged-out tab's own context, closed with it
    lines: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


class Browser:
    def __init__(self, db: DB, on_change=lambda: None):
        self.db = db
        self.on_change = on_change  # called when a site's login state changes
        self.tabs: dict[int, Tab] = {}  # run item id -> its tab
        self.logging_in: dict[int, asyncio.Task] = {}  # site id -> its open login window
        self._pw = None
        self._public = None
        self._sites: dict[int, object] = {}  # site id -> its persistent context
        self._locks: dict[int, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()

    # Sites

    def sites(self) -> list[dict]:
        return self.db.all("SELECT * FROM sites ORDER BY host")

    def granted(self, agent_id: int) -> list[dict]:
        return self.db.all(
            "SELECT s.* FROM sites s JOIN agent_sites g ON g.site_id = s.id WHERE g.agent_id = ? ORDER BY s.host", agent_id
        )

    def add_site(self, host: str) -> int:
        existing = self.db.one("SELECT id FROM sites WHERE host = ?", host)
        return existing["id"] if existing else self.db.insert("sites", host=host, created_at=now())

    async def remove_site(self, site_id: int) -> None:
        task = self.logging_in.pop(site_id, None)
        if task:
            task.cancel()
        await self._close_site(site_id)
        with self.db.transaction():
            self.db.run("DELETE FROM agent_sites WHERE site_id = ?", site_id)
            self.db.run("DELETE FROM sites WHERE id = ?", site_id)
        await asyncio.to_thread(shutil.rmtree, self._profile(site_id), True)

    def _profile(self, site_id: int):
        return config.BROWSER_DIR / "sites" / str(site_id)

    def start_login(self, site_id: int) -> None:
        """Open a real Chrome window on the site so the user can log in. It counts as logged in
        once they close the window."""
        if site_id in self.logging_in:
            return
        task = self.logging_in[site_id] = asyncio.create_task(self._login(site_id))

        def done(t: asyncio.Task) -> None:
            if self.logging_in.get(site_id) is t:
                del self.logging_in[site_id]
            if not t.cancelled() and t.exception():
                log.error("login window failed", exc_info=t.exception())
            self.on_change()

        task.add_done_callback(done)
        self.on_change()

    async def _login(self, site_id: int) -> None:
        site = self.db.one("SELECT * FROM sites WHERE id = ?", site_id)
        if not site:
            return
        await self._close_site(site_id)
        pw = await self._playwright()
        ctx = await self._launch_profile(pw, site_id, headless=False)
        try:
            closed = asyncio.Event()
            ctx.on("close", lambda _: closed.set())
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(f"https://{site['host']}/")
            await closed.wait()
        finally:
            await ctx.close()
        self.db.update("sites", site_id, logged_in_at=now())

    # Tabs

    async def open(self, key: int, agent_id: int, url: str) -> str:
        if "//" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise BrowserError("Only http and https addresses can be opened.")
        host = parts.hostname.lower()
        site = next((s for s in self.granted(agent_id) if on_host(host, s["host"])), None)
        if site and site["id"] in self.logging_in:
            raise BrowserError(f"The user is logging in to {site['host']} right now. Try again later.")
        await self.close_tab(key)
        ctx = await (self._site_context(site) if site else self._public_context())
        page = await ctx.new_page()
        tab = self.tabs[key] = Tab(page, site, context=None if site else ctx)
        await self._dress(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            raise BrowserError(f"Couldn't open {url}: {str(e).splitlines()[0]}") from None
        await self._settle(page)
        return await self._snapshot(tab, intro=self._status(tab, host))

    async def read(self, key: int) -> str:
        tab = self._tab(key)
        return await self._snapshot(tab)

    async def scroll(self, key: int, times: int = 1) -> str:
        tab = self._tab(key)
        collected: list[str] = []
        at_end = False
        for _ in range(max(1, min(times, 10))):
            before = await tab.page.evaluate("scrollY")
            width, height = await tab.page.evaluate("[innerWidth, innerHeight]")
            # The wheel scrolls whatever is under the mouse, so aim near the middle, not a fixed sidebar.
            await tab.page.mouse.move(width * random.uniform(0.4, 0.6), height * random.uniform(0.4, 0.6),
                                      steps=random.randint(5, 12))
            steps = random.randint(3, 6)
            for _ in range(steps):
                await tab.page.mouse.wheel(0, height * 0.9 / steps)
                await asyncio.sleep(random.uniform(*WHEEL_PAUSE))
            await self._settle(tab.page)
            lines = _lines(await tab.page.evaluate("document.body ? document.body.innerText : ''"))
            collected += new_lines(tab.lines, lines)
            tab.lines = lines
            if await tab.page.evaluate("scrollY") == before:
                at_end = True
                break
        text = "\n".join(collected) or "(nothing new)"
        return _cap(text) + ("\n\n(Reached the end of the page.)" if at_end else "")

    async def click(self, key: int, ref: str) -> str:
        tab = self._tab(key)
        blocked = len(tab.blocked)
        try:
            el = self._element(tab, ref)
            await self._move_to(tab.page, el)
            await el.click(timeout=5000)
        except BrowserError:
            raise
        except Exception as e:
            raise BrowserError(f"Couldn't click {ref}: {str(e).splitlines()[0]}") from None
        await self._settle(tab.page)
        return await self._snapshot(tab, intro=self._blocked_note(tab, blocked))

    async def type(self, key: int, ref: str, text: str, submit: bool = False) -> str:
        tab = self._tab(key)
        blocked = len(tab.blocked)
        el = self._element(tab, ref)
        try:
            await self._move_to(tab.page, el)
            await el.click(timeout=5000)
            await el.fill("", timeout=5000)
            await el.press_sequentially(text, delay=random.uniform(*TYPING))
            if submit:
                await el.press("Enter")
        except Exception as e:
            raise BrowserError(f"Couldn't type into {ref}: {str(e).splitlines()[0]}") from None
        await self._settle(tab.page)
        if not submit:
            return "Typed."
        return await self._snapshot(tab, intro=self._blocked_note(tab, blocked))

    async def close_tab(self, key: int) -> None:
        tab = self.tabs.pop(key, None)
        if not tab:
            return
        await _quietly(tab.context.close() if tab.context else tab.page.close())
        if tab.site and not any(t.site and t.site["id"] == tab.site["id"] for t in self.tabs.values()):
            # Closing a persistent context writes its cookies to disk and frees the Chrome process.
            await self._close_site(tab.site["id"])

    async def stop(self) -> None:
        for task in self.logging_in.values():
            task.cancel()
        for key in list(self.tabs):
            await self.close_tab(key)
        for site_id in list(self._sites):
            await self._close_site(site_id)
        if self._public:
            await self._public.close()
            self._public = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    # Internals

    def _tab(self, key: int) -> Tab:
        tab = self.tabs.get(key)
        if not tab or tab.page.is_closed():
            self.tabs.pop(key, None)
            raise BrowserError("No page is open. Use open_page first.")
        return tab

    def _element(self, tab: Tab, ref: str):
        if not re.fullmatch(r"e\d+", ref or ""):
            raise BrowserError("ref must look like e3, from the page's element list.")
        return tab.page.locator(f'[data-qb-ref="{ref}"]').first

    @staticmethod
    async def _move_to(page, el) -> None:
        """Move the mouse to the element the way a hand would: over several steps, near its middle."""
        await el.scroll_into_view_if_needed(timeout=5000)
        box = await el.bounding_box()
        if box:
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.move(x, y, steps=random.randint(8, 20))

    async def _dress(self, page) -> None:
        """Give the page the user's Chrome identity, including the client hints that the user agent
        alone doesn't cover."""
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Network.setUserAgentOverride", identity())

    def _status(self, tab: Tab, host: str) -> str:
        if tab.site:
            return (f"(Logged in to {tab.site['host']} as the user. Read-only: changes on {tab.site['host']} are blocked, "
                    "and links to other sites don't open here; open them with open_page.)")
        sites = [s for s in self.sites() if on_host(host, s["host"])]
        if sites:
            return f"(Not logged in. The user can let you use their {sites[0]['host']} login in your settings.)"
        return ""

    def _blocked_note(self, tab: Tab, since: int) -> str:
        new = tab.blocked[since:]
        return f"(Blocked: {'; '.join(dict.fromkeys(new))}.)" if new else ""

    async def _snapshot(self, tab: Tab, intro: str = "") -> str:
        try:
            snap = await tab.page.evaluate(SNAPSHOT_JS, MAX_ELEMENTS)
        except Exception as e:
            raise BrowserError(f"Couldn't read the page: {str(e).splitlines()[0]}") from None
        tab.lines = _lines(snap["text"])
        parts = [f"URL: {snap['url']}", f"Title: {snap['title']}"]
        if intro:
            parts.append(intro)
        parts.append("Text:\n" + _cap("\n".join(tab.lines)))
        if snap["elements"]:
            listed = "\n".join(
                f"[{e['ref']}] {e['kind']} \"{e['name']}\"" + (f" -> {e['href']}" if e["href"] else "") for e in snap["elements"]
            )
            parts.append("Elements (use the ref with click_element or type_into):\n" + listed)
        return "\n\n".join(parts)

    async def _settle(self, page) -> None:
        """Wait until the page has loaded and its text stops changing. Many sites (X among them)
        fill the page with scripts well after the HTML arrives."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(*PACE))
        last, stable = -1, 0
        for _ in range(int(SETTLE_LIMIT / SETTLE_POLL)):
            try:
                size = await page.evaluate("document.body ? document.body.innerText.length : 0")
            except Exception:
                size = -1  # mid-navigation
            stable = stable + 1 if size == last and size > 0 else 0
            if stable >= 3:
                return
            last = size
            await asyncio.sleep(SETTLE_POLL)

    async def _guard(self, route, site: str) -> None:
        req = route.request
        parts = urlsplit(req.url)
        host = (parts.hostname or "").lower()
        try:
            page = req.frame.page
        except Exception:
            page = None
        # Popups opened by the page have no tab: they're still guarded, just not reported.
        tab = next((t for t in self.tabs.values() if t.page is page), None)
        blocked = tab.blocked if tab else []
        if host in CHALLENGE_HOSTS or (on_host(host, site) and parts.path.startswith(CHALLENGE_PATH)):
            await route.continue_()
        elif req.resource_type == "document" and not on_host(host, site):
            # The login profile stays on its own site, so it can't be used anywhere else.
            blocked.append(f"{host} is outside {site}; open it with open_page")
            await self._refuse(route)
        elif on_host(host, site) and req.method not in SAFE_METHODS:
            blocked.append(f"a change on {site} ({req.method} {parts.path[:60]}), because it's read-only")
            await self._refuse(route)
        else:
            await route.continue_()

    @staticmethod
    async def _refuse(route) -> None:
        # Chrome treats "aborted" as a silent cancel and keeps the current page; other errors replace it
        # with an error page. Either way the page's scripts see a failure, not a fake success.
        await route.abort("aborted")

    async def _playwright(self):
        async with self._start_lock:
            if not self._pw:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
            return self._pw

    async def _public_context(self):
        pw = await self._playwright()
        async with self._start_lock:
            if not self._public:
                self._public = await self._launch(pw.chromium.launch(channel="chrome", headless=True, args=CHROME_ARGS,
                                                                     ignore_default_args=["--enable-automation"]))
        ctx = await self._public.new_context(**self._context_options())
        ctx.on("page", lambda page: asyncio.ensure_future(_quietly(self._dress(page))))
        return ctx

    async def _site_context(self, site: dict):
        lock = self._locks.setdefault(site["id"], asyncio.Lock())
        async with lock:
            ctx = self._sites.get(site["id"])
            if ctx is None:
                ctx = await self._launch_profile(await self._playwright(), site["id"], headless=True)

                async def guard(route):
                    await self._guard(route, site["host"])

                # On the whole profile, so popups the page opens are read-only too.
                await ctx.route("**/*", guard)
                ctx.on("page", lambda page: asyncio.ensure_future(_quietly(self._dress(page))))
                self._sites[site["id"]] = ctx
            return ctx

    async def _launch_profile(self, pw, site_id: int, headless: bool):
        profile = self._profile(site_id)
        profile.mkdir(parents=True, exist_ok=True)
        # The login window is the user's own Chrome, as is. Agents' headless profile takes its identity.
        options = self._context_options() if headless else {"no_viewport": True}
        return await self._launch(pw.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=headless,
            args=CHROME_ARGS, ignore_default_args=["--enable-automation"], **options,
        ))

    @staticmethod
    def _context_options() -> dict:
        # Service workers would make requests that routes don't see, which read-only enforcement relies on.
        return dict(user_agent=identity()["userAgent"], locale="en-US", viewport=VIEWPORT, screen=SCREEN,
                    service_workers="block")

    async def _launch(self, coro):
        try:
            return await coro
        except Exception as e:
            msg = str(e).splitlines()[0]
            if "chrome" in msg.lower() and ("not found" in msg.lower() or "executable" in msg.lower()):
                raise BrowserError("Google Chrome isn't installed. Install it from google.com/chrome.") from None
            raise BrowserError(f"Couldn't start Chrome: {msg}") from None

    async def _close_site(self, site_id: int) -> None:
        ctx = self._sites.pop(site_id, None)
        if ctx:
            for key in [k for k, t in self.tabs.items() if t.site and t.site["id"] == site_id]:
                self.tabs.pop(key, None)
            await _quietly(ctx.close())


async def _quietly(coro) -> None:
    try:
        await coro
    except Exception:
        log.debug("browser close failed", exc_info=True)
