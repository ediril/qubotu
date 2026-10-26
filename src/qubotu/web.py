"""FastAPI app: server-rendered pages, htmx fragments, and a server-sent events stream."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from .browser import normalize_host
from .db import parse_ts
from .engine import PROVIDERS, Engine
from .version import current_version

HERE = Path(__file__).parent
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
SCHEDULES = [(0, "Manual only"), (15, "Every 15 minutes"), (30, "Every 30 minutes"), (60, "Every hour"),
             (180, "Every 3 hours"), (360, "Every 6 hours"), (720, "Every 12 hours"), (1440, "Every day")]
DEFAULT_SCHEDULE = 1440
FIREWORKS_MODELS = [
    "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct",
    "accounts/fireworks/models/qwen3-235b-a22b-instruct-2507",
    "accounts/fireworks/models/deepseek-v3p1",
]

md = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": True}).enable("table").enable("strikethrough")


def render_md(text: str | None) -> Markup:
    return Markup(md.render(text or ""))


def humanize(ts: str | None) -> str:
    dt = parse_ts(ts)
    if not dt:
        return "never"
    secs = (dt - datetime.now(UTC)).total_seconds()
    future, secs = secs > 0, abs(secs)
    if secs < 45:
        s = "a few seconds"
    elif secs < 3600:
        s = f"{round(secs / 60)} min"
    elif secs < 86400:
        s = f"{round(secs / 3600)} h"
    else:
        s = f"{round(secs / 86400)} d"
    return f"in {s}" if future else f"{s} ago"


def static_url(name: str) -> str:
    """Static file URL that changes whenever the file does, so browsers never use a stale copy."""
    version = int((HERE / "static" / name).stat().st_mtime)
    return f"/static/{name}?v={version}"


LOCAL_HOSTS = {"127.0.0.1", "localhost"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname(netloc: str) -> str:
    return netloc.rsplit(":", 1)[0] if not netloc.startswith("[") else netloc.split("]")[0] + "]"


def local_only(request: Request) -> str | None:
    """Why a request must be refused, or None. Qubotu has no login, so it only answers its own pages:
    the Host must be local (blocks DNS rebinding), and a state-changing request must come from
    Qubotu's own origin (blocks other websites posting forms to it)."""
    host = request.headers.get("host", "")
    if _hostname(host) not in LOCAL_HOSTS:
        return "Unknown host."
    if request.method in SAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if origin is not None:
        return None if origin == f"http://{host}" else "Cross-origin request refused."
    if request.headers.get("sec-fetch-site") == "cross-site":
        return "Cross-site request refused."
    return None


def create_app(engine: Engine | None = None, scheduler: bool = True) -> FastAPI:
    engine = engine or Engine()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.start(scheduler=scheduler)
        yield
        await engine.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def refuse_foreign_requests(request: Request, call_next):
        if reason := local_only(request):
            return PlainTextResponse(reason, status_code=403)
        return await call_next(request)
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["md"] = render_md
    templates.env.filters["ago"] = humanize
    templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
    templates.env.globals["default_schedule"] = DEFAULT_SCHEDULE
    templates.env.globals["app_version"] = current_version
    templates.env.globals["static"] = static_url
    templates.env.globals["site_list"] = engine.browser.sites
    templates.env.globals["granted_site_ids"] = lambda agent_id: [s["id"] for s in engine.browser.granted(agent_id)]
    db = engine.db

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("nav", "")
        return templates.TemplateResponse(
            request, name, {"engine": engine, "agents": sidebar_agents(), "blockers": engine.blockers(), **ctx}
        )

    def fragment(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"engine": engine, **ctx})

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def get_agent(agent_id: int) -> dict:
        agent = db.agent(agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        agent["state"] = engine.status(agent)
        return agent

    def sidebar_agents() -> list[dict]:
        rows = db.all(
            "SELECT a.*, (SELECT COUNT(*) FROM outputs o WHERE o.agent_id = a.id AND o.status IN ('new', 'open')) AS unseen "
            "FROM agents a ORDER BY a.name"
        )
        for a in rows:
            a["state"] = engine.status(a)
        return rows

    def attention() -> list[dict]:
        return db.all(
            "SELECT o.*, a.name AS agent_name FROM outputs o JOIN agents a ON a.id = o.agent_id "
            "WHERE o.blocking = 0 AND (o.status = 'open' OR (o.status = 'new' AND o.importance = 'high')) ORDER BY o.id DESC"
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return RedirectResponse(static_url("favicon.svg"), status_code=301)

    # Pages

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return page(request, "dashboard.html", nav="dashboard")

    @app.get("/partials/sidebar", response_class=HTMLResponse)
    async def sidebar(request: Request):
        return fragment(request, "partials/sidebar.html", agents=sidebar_agents())

    @app.get("/partials/dashboard", response_class=HTMLResponse)
    async def dashboard_body(request: Request):
        recent = db.all(
            "SELECT o.*, a.name AS agent_name FROM outputs o JOIN agents a ON a.id = o.agent_id "
            "WHERE o.kind NOT IN ('question', 'login') ORDER BY o.id DESC LIMIT 12"
        )
        requests = db.all(
            "SELECT r.*, f.name AS from_name, t.name AS to_name FROM requests r "
            "JOIN agents f ON f.id = r.from_agent JOIN agents t ON t.id = r.to_agent ORDER BY r.id DESC LIMIT 8"
        )
        return fragment(
            request, "partials/dashboard_body.html",
            agents=sidebar_agents(), attention=attention(), recent=recent, requests=requests,
            codex_error=engine.codex.last_error,
        )

    # Agents

    @app.get("/agents/new", response_class=HTMLResponse)
    async def new_agent(request: Request):
        return page(request, "agent_form.html", agent=None, error=None, models=await engine.models(),
                    providers=PROVIDERS, schedules=SCHEDULES, fireworks_models=FIREWORKS_MODELS, nav="new")

    def agent_fields(name, goals, provider, model, schedule_minutes) -> tuple[dict, str | None]:
        goals = goals.strip()
        summary = goals.splitlines()[0].strip() if goals else ""
        fields = dict(
            name=name.strip(), goals=goals, specialty=summary if len(summary) <= 140 else summary[:139] + "…",
            provider=provider if provider in PROVIDERS else "openai", model=model.strip(),
            schedule_minutes=max(0, int(schedule_minutes or 0)),
        )
        if not NAME_RE.match(fields["name"]):
            return fields, "Name must start with a letter and use only letters, numbers, - and _ (no spaces), so other agents can ask it for help by name."
        if fields["provider"] == "fireworks" and not fields["model"]:
            return fields, "Fireworks agents need a model ID."
        return fields, None

    @app.post("/agents")
    async def create_agent(
        request: Request, name: str = Form(...), goals: str = Form(""),
        provider: str = Form("openai"), model: str = Form(""), schedule_minutes: int = Form(DEFAULT_SCHEDULE),
        sites: list[int] = Form([]),
    ):
        fields, error = agent_fields(name, goals, provider, model, schedule_minutes)
        if not error and db.agent_by_name(fields["name"]):
            error = f"An agent named {fields['name']} already exists."
        if error:
            return page(request, "agent_form.html", agent={**fields, "site_ids": sites}, error=error, models=await engine.models(),
                        providers=PROVIDERS, schedules=SCHEDULES, fireworks_models=FIREWORKS_MODELS, nav="new")
        aid = engine.create_agent(**fields)
        engine.set_agent_sites(aid, sites)
        return back(f"/agents/{aid}")

    @app.get("/agents/{agent_id}", response_class=HTMLResponse)
    async def agent_page(request: Request, agent_id: int, tab: str = "chat"):
        agent = get_agent(agent_id)
        tab = tab if tab in ("chat", "shared", "settings") else "chat"
        ctx = {}
        if tab == "settings":
            ctx = dict(models=await engine.models(), providers=PROVIDERS, schedules=SCHEDULES,
                       fireworks_models=FIREWORKS_MODELS, error=None,
                       runs=db.all("SELECT * FROM runs WHERE agent_id = ? ORDER BY id DESC LIMIT 15", agent_id))
        return page(request, "agent.html", agent=agent, tab=tab, nav=f"agent:{agent_id}", **ctx)

    @app.get("/agents/{agent_id}/header", response_class=HTMLResponse)
    async def agent_header(request: Request, agent_id: int):
        return fragment(request, "partials/agent_header.html", agent=get_agent(agent_id))

    @app.get("/agents/{agent_id}/messages", response_class=HTMLResponse)
    async def agent_messages(request: Request, agent_id: int):
        return fragment(request, "partials/messages.html", messages=db.messages(f"agent:{agent_id}"))

    @app.post("/agents/{agent_id}/chat")
    async def agent_chat(agent_id: int, text: str = Form(...)):
        get_agent(agent_id)
        if text.strip():
            engine.chat(agent_id, text.strip())
        return Response(status_code=204)

    @app.get("/agents/{agent_id}/outputs", response_class=HTMLResponse)
    async def agent_outputs(request: Request, agent_id: int, show: str = "active"):
        get_agent(agent_id)
        where = "status NOT IN ('done', 'dismissed', 'answered')" if show == "active" else "1"
        outputs = db.all(
            f"SELECT * FROM outputs WHERE agent_id = ? AND {where} "
            "ORDER BY (status = 'open') DESC, id DESC LIMIT 200",
            agent_id,
        )
        return fragment(request, "partials/outputs.html", outputs=outputs, agent_id=agent_id, show=show)

    @app.post("/agents/{agent_id}/edit")
    async def edit_agent(
        request: Request, agent_id: int, name: str = Form(...), goals: str = Form(""),
        provider: str = Form("openai"), model: str = Form(""), schedule_minutes: int = Form(DEFAULT_SCHEDULE),
        sites: list[int] = Form([]),
    ):
        agent = get_agent(agent_id)
        fields, error = agent_fields(name, goals, provider, model, schedule_minutes)
        other = db.agent_by_name(fields["name"])
        if not error and other and other["id"] != agent_id:
            error = f"An agent named {fields['name']} already exists."
        if error:
            return page(request, "agent.html", agent={**agent, **fields, "site_ids": sites}, tab="settings", error=error,
                        models=await engine.models(), providers=PROVIDERS, schedules=SCHEDULES,
                        fireworks_models=FIREWORKS_MODELS, runs=[], nav=f"agent:{agent_id}")
        engine.update_agent(agent_id, **fields)
        engine.set_agent_sites(agent_id, sites)
        return back(f"/agents/{agent_id}?tab=settings")

    @app.post("/agents/{agent_id}/outputs/seen")
    async def outputs_seen(agent_id: int):
        # A POST, so other websites can't trigger it: the middleware checks the origin of POSTs only.
        get_agent(agent_id)
        engine.mark_seen(agent_id)
        return Response(status_code=204)

    @app.post("/agents/{agent_id}/{action}")
    async def agent_action(agent_id: int, action: str):
        get_agent(agent_id)
        if action == "pause":
            engine.set_paused(agent_id, True)
        elif action == "resume":
            engine.set_paused(agent_id, False)
        elif action == "run":
            engine.run_now(agent_id)
            return back(f"/agents/{agent_id}")
        elif action == "interrupt":
            engine.stop_runs(agent_id)
        elif action == "reset":
            engine.reset_thread(agent_id)
            return back(f"/agents/{agent_id}?tab=settings")
        elif action == "delete":
            engine.delete_agent(agent_id)
            return back("/")
        else:
            raise HTTPException(404)
        return back(f"/agents/{agent_id}")

    # Outputs

    def get_output(output_id: int) -> dict:
        o = db.one("SELECT * FROM outputs WHERE id = ?", output_id)
        if not o:
            raise HTTPException(404)
        return o

    def output_card(request: Request, output_id: int) -> HTMLResponse:
        return fragment(request, "partials/output_card.html", o=get_output(output_id))

    @app.post("/outputs/{output_id}/answer", response_class=HTMLResponse)
    async def answer_output(request: Request, output_id: int, answer: str = Form(...)):
        get_output(output_id)
        if answer.strip():
            engine.answer_question(output_id, answer.strip())
        return output_card(request, output_id)

    @app.get("/partials/blockers", response_class=HTMLResponse)
    async def blockers_partial(request: Request):
        return fragment(request, "partials/blockers.html", blockers=engine.blockers())

    @app.post("/outputs/{output_id}/login")
    async def output_login(output_id: int):
        get_output(output_id)
        engine.resolve_login(output_id)
        engine.bus.refresh("sites", "status", "outputs")
        return Response(status_code=204)

    @app.post("/outputs/{output_id}/decline")
    async def output_decline(output_id: int):
        get_output(output_id)
        engine.decline_login(output_id)
        return Response(status_code=204)

    @app.post("/outputs/{output_id}/feedback", response_class=HTMLResponse)
    async def output_feedback(request: Request, output_id: int, rating: str = Form(""), comment: str = Form("")):
        o = get_output(output_id)
        parts = [p for p in (rating.strip(), comment.strip()) if p]
        if parts:
            db.update("outputs", output_id, feedback=": ".join(parts), feedback_pending=1)
            engine.bus.refresh(f"outputs:{o['agent_id']}")
        return output_card(request, output_id)

    @app.post("/outputs/{output_id}/status", response_class=HTMLResponse)
    async def output_status(request: Request, output_id: int, status: str = Form(...)):
        o = get_output(output_id)
        if status in ("done", "dismissed", "seen"):
            db.update("outputs", output_id, status=status)
            engine.bus.refresh("status", "outputs", f"outputs:{o['agent_id']}")
        return output_card(request, output_id)

    # Shared memory

    @app.get("/memory", response_class=HTMLResponse)
    async def memory(request: Request, edit: int | None = None):
        return page(request, "memory.html", memories=engine.memory.all(), edit=edit, nav="memory")

    @app.get("/partials/memory", response_class=HTMLResponse)
    async def memory_list(request: Request):
        return fragment(request, "partials/memory_list.html", memories=engine.memory.all(), edit=None)

    @app.post("/memory")
    async def memory_add(text: str = Form(...), kind: str = Form("fact")):
        if text.strip():
            await engine.memory.add(text, kind)
        return back("/memory")

    @app.post("/memory/{memory_id}")
    async def memory_update(memory_id: int, text: str = Form(...), kind: str = Form("fact"), pinned: str = Form("")):
        await engine.memory.update(memory_id, text=text, kind=kind, pinned=bool(pinned))
        return back("/memory")

    @app.post("/memory/{memory_id}/pin")
    async def memory_pin(memory_id: int):
        m = engine.memory.get(memory_id)
        if m:
            await engine.memory.update(memory_id, pinned=not m["pinned"])
        return back("/memory")

    @app.post("/memory/{memory_id}/delete")
    async def memory_delete(memory_id: int):
        engine.memory.delete(memory_id)
        return back("/memory")

    # Settings

    async def settings_ctx(saved: int = 0) -> dict:
        return dict(account=await engine.account(), codex_error=engine.codex.last_error,
                    has_fireworks=bool(db.get_setting("fireworks_api_key")), saved=saved, nav="settings")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request, saved: int = 0):
        return page(request, "settings.html", **(await settings_ctx(saved)), **sites_ctx())

    @app.post("/settings")
    async def settings_save(fireworks_api_key: str = Form(""), clear_fireworks: str = Form("")):
        if clear_fireworks:
            db.set_setting("fireworks_api_key", None)
        elif fireworks_api_key.strip():
            db.set_setting("fireworks_api_key", fireworks_api_key.strip())
        if engine.apply_settings():
            await engine.restart_codex()
        return back("/settings?saved=1")

    # Sites agents can use as the user

    @app.get("/partials/sites", response_class=HTMLResponse)
    async def sites_partial(request: Request):
        return fragment(request, "partials/sites.html", **sites_ctx())

    def sites_ctx(error: str | None = None) -> dict:
        return dict(sites=engine.browser.sites(), logging_in=set(engine.browser.logging_in), site_error=error)

    @app.post("/sites")
    async def site_add(request: Request, host: str = Form("")):
        normalized = normalize_host(host)
        if not normalized:
            return page(request, "settings.html", **(await settings_ctx()), **sites_ctx(f"{host.strip() or 'That'} isn't a site address. Enter something like x.com."))
        engine.browser.start_login(engine.browser.add_site(normalized))
        return back("/settings#sites")

    @app.post("/sites/{site_id}/login")
    async def site_login(site_id: int):
        engine.browser.start_login(site_id)
        return back("/settings#sites")

    @app.post("/sites/{site_id}/delete")
    async def site_delete(site_id: int):
        await engine.browser.remove_site(site_id)
        engine.bus.refresh("sites")
        return back("/settings#sites")

    # Live updates

    @app.get("/events")
    async def events(request: Request):
        q = engine.bus.subscribe()

        async def stream():
            try:
                yield "retry: 2000\n\n"
                while not engine.bus.closed:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if ev is None:
                        break
                    yield f"data: {json.dumps(ev)}\n\n"
            finally:
                engine.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app
