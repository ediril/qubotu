"""Qubotu's tools, registered on each agent's Codex thread as dynamic tools."""

import hashlib
import json
from typing import TYPE_CHECKING

from . import collab
from .browser import BrowserError, normalize_host
from .db import now

if TYPE_CHECKING:
    from .engine import Engine
    from .inbox import Run

OUTPUT_KINDS = ["finding", "report", "table", "draft", "link"]


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }


SPECS = [
    _fn(
        "share_output",
        "Share a structured result with the user in your Shared tab: a finding, report, table, draft, or link. "
        "Use markdown in body. For a table, provide columns and rows.",
        {
            "kind": {"type": "string", "enum": OUTPUT_KINDS},
            "title": {"type": "string", "description": "Short, specific headline."},
            "body": {"type": "string", "description": "Markdown content. Can be empty for a bare link or table."},
            "url": {"type": "string", "description": "Link, for kind=link or a source."},
            "columns": {"type": "array", "items": {"type": "string"}, "description": "Table column headers."},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "Table rows."},
            "importance": {"type": "string", "enum": ["low", "normal", "high"], "description": "high only for things the user should see soon."},
        },
        ["kind", "title"],
    ),
    _fn(
        "list_shared",
        "List what you have already shared with the user, newest first, with status and any feedback. Check before sharing to avoid repeats.",
        {"limit": {"type": "integer", "description": "Default 30."}},
        [],
    ),
    _fn(
        "ask_user",
        "Ask the user a question. Only when essential information is missing or an action exceeds your authority. "
        "The answer arrives later as a separate message; keep doing unblocked work meanwhile.",
        {
            "question": {"type": "string"},
            "context": {"type": "string", "description": "Why you need this, briefly."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Suggested answers, if the choice is between known options."},
            "blocking": {
                "type": "boolean",
                "description": "True only if you can't go on with your job at all until the user answers. "
                "The user is alerted prominently, so don't overuse it. Otherwise the question waits in your Shared tab.",
            },
        },
        ["question"],
    ),
    _fn(
        "ask_to_log_in",
        "Ask the user to log in to a site you need, when a page shows a login wall or your login there has expired. "
        "This alerts the user prominently. When they've logged in, you'll get a message and can open the site again.",
        {
            "site": {"type": "string", "description": "The site, for example x.com."},
            "reason": {"type": "string", "description": "What you need it for, in one sentence."},
        },
        ["site", "reason"],
    ),
    _fn(
        "request_help",
        "Ask another agent on the team to do part of a task that fits their specialty better. Returns immediately; the result arrives later as a separate message.",
        {
            "agent": {"type": "string", "description": "The other agent's name."},
            "task": {"type": "string", "description": "A complete, self-contained description of what you need."},
        },
        ["agent", "task"],
    ),
    _fn(
        "remember",
        "Save something durable about the user to shared memory, visible to all agents.",
        {
            "text": {"type": "string", "description": "One self-contained fact, preference, or project."},
            "kind": {"type": "string", "enum": ["fact", "preference", "project"]},
        },
        ["text"],
    ),
    _fn(
        "recall",
        "Search shared memory for what the team knows about the user.",
        {"query": {"type": "string"}, "limit": {"type": "integer"}},
        ["query"],
    ),
    _fn(
        "update_memory",
        "Correct or refine a shared memory by id.",
        {"id": {"type": "integer"}, "text": {"type": "string"}},
        ["id", "text"],
    ),
    _fn("forget", "Remove a shared memory that is wrong or no longer true.", {"id": {"type": "integer"}}, ["id"]),
    _fn(
        "open_page",
        "Open a web page in your browser tab and read it: its text, and numbered elements you can click or type into. "
        "On sites the user lets you use as them, you're logged in, read-only.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _fn("read_page", "Read the open page again: its text and numbered elements. Element refs change each time you read.", {}, []),
    _fn(
        "scroll_page",
        "Scroll the open page down and return only the text that wasn't visible before. Works on feeds that load more as you scroll.",
        {"times": {"type": "integer", "description": "How many screens to scroll, 1 to 10. Default 1."}},
        [],
    ),
    _fn(
        "click_element",
        "Click an element on the open page by its ref from the last read, then read the page.",
        {"ref": {"type": "string", "description": "For example e3."}},
        ["ref"],
    ),
    _fn(
        "type_into",
        "Type text into a field on the open page by its ref. Set submit to press Enter afterwards, for example to search.",
        {"ref": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
        ["ref", "text"],
    ),
]


# Codex fixes a thread's tools when it starts, so threads started with a different tool set are replaced.
FINGERPRINT = hashlib.sha256(json.dumps(SPECS, sort_keys=True).encode()).hexdigest()[:16]


def _limit(args: dict, default: int, cap: int) -> int:
    try:
        n = int(args.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, cap))


async def call(engine: "Engine", state: "Run", tool: str, args: dict) -> tuple[bool, str]:
    agent_id = state.agent_id
    db = engine.db
    args = args or {}

    if tool == "share_output":
        data = {}
        if args.get("url"):
            data["url"] = args["url"]
        if args.get("columns") or args.get("rows"):
            data["columns"] = args.get("columns") or []
            data["rows"] = args.get("rows") or []
        kind = args.get("kind") if args.get("kind") in OUTPUT_KINDS else "finding"
        ts = now()
        oid = db.insert(
            "outputs",
            agent_id=agent_id,
            kind=kind,
            title=args.get("title", "").strip() or "(untitled)",
            body=args.get("body", ""),
            data=json.dumps(data) if data else None,
            importance=args.get("importance") if args.get("importance") in ("low", "normal", "high") else "normal",
            created_at=ts,
            updated_at=ts,
        )
        engine.bus.refresh(f"outputs:{agent_id}", "outputs", "status")
        return True, f"Shared as output #{oid}."

    if tool == "list_shared":
        limit = _limit(args, 30, 100)
        rows = db.all(
            "SELECT id, kind, title, status, feedback, answer, created_at FROM outputs WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
            agent_id,
            limit,
        )
        if not rows:
            return True, "You haven't shared anything yet."
        lines = []
        for r in rows:
            line = f"#{r['id']} [{r['kind']}, {r['status']}, {r['created_at'][:10]}] {r['title']}"
            if r["feedback"]:
                line += f" (user feedback: {r['feedback']})"
            if r["answer"]:
                line += f" (answer: {r['answer']})"
            lines.append(line)
        return True, "\n".join(lines)

    if tool == "ask_user":
        question = args.get("question", "").strip()
        if not question:
            return False, "question is required."
        data = {"context": args.get("context") or "", "options": args.get("options") or []}
        blocking = bool(args.get("blocking"))
        ts = now()
        qid = db.insert(
            "outputs",
            agent_id=agent_id,
            kind="question",
            title=question,
            body=data["context"],
            data=json.dumps(data),
            importance="high" if blocking else "normal",
            status="open",
            blocking=int(blocking),
            created_at=ts,
            updated_at=ts,
        )
        engine.bus.refresh(f"outputs:{agent_id}", "outputs", "status", "agents")
        return True, f"Question #{qid} sent to the user. Their answer will arrive later; continue unblocked work."

    if tool == "ask_to_log_in":
        host = normalize_host(args.get("site", ""))
        if not host:
            return False, "site must be a site address, for example x.com."
        oid, new = engine.request_login(agent_id, host, args.get("reason", "").strip())
        if not new:
            return True, f"You already asked the user to log in to {host} (request #{oid}). Wait for their reply."
        return True, f"Asked the user to log in to {host} (request #{oid}). You'll get a message when they have; continue other work."

    if tool == "request_help":
        return collab.submit_request(engine, state, args.get("agent", ""), args.get("task", ""))

    if tool == "remember":
        text = args.get("text", "").strip()
        if not text:
            return False, "text is required."
        mid = await engine.memory.add(text, args.get("kind", "fact"), agent_id)
        engine.bus.refresh("memory")
        return True, f"Saved as memory #{mid}."

    if tool == "recall":
        results = await engine.memory.search(args.get("query", ""), _limit(args, 8, 30))
        if not results:
            return True, "No matching memories."
        return True, "\n".join(f"#{m['id']} [{m['kind']}] {m['text']}" for m in results)

    if tool == "update_memory":
        if not args.get("text", "").strip():
            return False, "text is required. Use forget to remove a memory."
        ok = await engine.memory.update(int(args.get("id", 0)), text=args["text"])
        engine.bus.refresh("memory")
        return (True, "Updated.") if ok else (False, "No memory with that id.")

    if tool == "forget":
        ok = engine.memory.delete(int(args.get("id", 0)))
        engine.bus.refresh("memory")
        return (True, "Forgotten.") if ok else (False, "No memory with that id.")

    try:
        if tool == "open_page":
            return True, await engine.browser.open(state.item_id, agent_id, args.get("url", "").strip())
        if tool == "read_page":
            return True, await engine.browser.read(state.item_id)
        if tool == "scroll_page":
            return True, await engine.browser.scroll(state.item_id, _limit({"limit": args.get("times")}, 1, 10))
        if tool == "click_element":
            return True, await engine.browser.click(state.item_id, args.get("ref", ""))
        if tool == "type_into":
            return True, await engine.browser.type(state.item_id, args.get("ref", ""), str(args.get("text", "")), bool(args.get("submit")))
    except BrowserError as e:
        return False, str(e)

    return False, f"Unknown tool {tool}."
