"""Agent-to-agent requests. The kind functions here are registered in `engine.KINDS`."""

from typing import TYPE_CHECKING

from . import config, prompts
from .db import now
from .inbox import Run, TurnResult, TurnSpec

if TYPE_CHECKING:
    from .engine import Engine

# Requests

def submit_request(engine: "Engine", run: Run, agent_name: str, task: str) -> tuple[bool, str]:
    db = engine.db
    target = db.agent_by_name(agent_name)
    task = task.strip()
    if not target:
        names = ", ".join(a["name"] for a in db.agents() if a["id"] != run.agent_id) or "none"
        return False, f"No agent named {agent_name!r}. Agents on the team: {names}."
    if target["id"] == run.agent_id:
        return False, "You can't send a request to yourself."
    if not task:
        return False, "task is required."
    if target["status"] == "paused":
        return False, f"{target['name']} is paused. Do the task yourself or ask the user."
    depth = run.depth + 1
    if depth > config.MAX_CHAIN_DEPTH:
        return False, "This request chain is already deep. Do the task yourself."
    with db.transaction():
        rid = db.insert(
            "requests", from_agent=run.agent_id, to_agent=target["id"], task=task, depth=depth, created_at=now(),
        )
        engine.enqueue(target["id"], "request", depth=depth, request_id=rid)
    engine.bus.refresh("requests")
    return True, (
        f"Request #{rid} sent to {target['name']}. Their result will arrive as a separate message. "
        "Continue with other work; don't wait for it in this run."
    )


def build_request(engine: "Engine", item: dict, agent: dict) -> TurnSpec | None:
    req = engine.db.one("SELECT * FROM requests WHERE id = ?", item["payload"]["request_id"])
    source = req and engine.db.agent(req["from_agent"])
    if not source:
        return None
    return TurnSpec(prompts.incoming_request(req["id"], source["name"], req["task"]))


def begin_request(engine: "Engine", item: dict) -> None:
    req = engine.db.one("SELECT * FROM requests WHERE id = ?", item["payload"]["request_id"])
    source = engine.db.agent(req["from_agent"])
    engine.db.update("requests", req["id"], status="running")
    engine.post(f"agent:{item['agent_id']}", "system", f"Request #{req['id']} from {source['name']}: {req['task']}", item["agent_id"])
    engine.bus.refresh("requests")


def finish_request(engine: "Engine", item: dict, spec: TurnSpec | None, result: TurnResult) -> None:
    req = engine.db.one("SELECT * FROM requests WHERE id = ?", item["payload"]["request_id"])
    if not req:
        return
    ok = result.ok and bool(result.text)
    text = result.text if ok else (result.error or "No result was returned.")
    fail_request(engine, req, text, ok)


def fail_request(engine: "Engine", req: dict, text: str, ok: bool = False) -> None:
    """Record a request's outcome and tell the agent that asked, whatever happened."""
    db = engine.db
    db.update("requests", req["id"], status="done" if ok else "failed", result=text, completed_at=now())
    source = db.agent(req["from_agent"])
    engine.bus.refresh("requests")
    if source:
        engine.post(f"agent:{source['id']}", "system", f"Result of request #{req['id']} arrived", source["id"])
        engine.enqueue(source["id"], "result", depth=req["depth"], request_id=req["id"])


def build_result(engine: "Engine", item: dict, agent: dict) -> TurnSpec | None:
    req = engine.db.one("SELECT * FROM requests WHERE id = ?", item["payload"]["request_id"])
    if not req:
        return None
    target = engine.db.agent(req["to_agent"])
    return TurnSpec(prompts.request_result(
        req["id"], target["name"] if target else "a deleted agent", req["task"], req["result"] or "", req["status"] == "done",
    ))
