import asyncio
import hashlib

import numpy as np
import pytest

from qubotu import config
from qubotu.engine import Engine


class FakeEmbedder:
    """Deterministic bag-of-words embeddings, so tests don't download a model."""

    available = True

    async def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for w in t.lower().split():
                v[int(hashlib.md5(w.strip(".,").encode()).hexdigest(), 16) % 64] += 1
            out.append(v / (np.linalg.norm(v) or 1))
        return out


class FakeCodex:
    """Stands in for the app-server. Each turn plays a script of steps:
    ("tool", name, args), ("say", text), ("item", item), ("hang",) which waits for turn/interrupt, and
    ("late", text) which emits a final answer and completion tagged with an older turn's id, and
    ("do", fn) which calls fn() mid-turn, and ("ignore_interrupt",) which never ends the turn.
    The last "say" is the final answer."""

    def __init__(self, engine):
        self.engine = engine
        self.generation = 1
        self.running = True
        self.last_error = None
        self.env_extra = {}
        self.calls = []
        self.scripts = {}  # agent name -> list of scripts, consumed in order
        self.default_script = [("say", "ok")]
        self.tool_results = []
        self.turn_starts = asyncio.Event()
        self.gate: asyncio.Event | None = None  # set to an Event to hold runs inside thread setup
        self._interrupts: dict[str, asyncio.Event] = {}
        self._n = 0

    async def ensure_started(self):
        if self.gate:
            await self.gate.wait()

    async def stop(self):
        pass

    async def restart(self):
        self.generation += 1

    async def request(self, method, params, timeout=60):
        self.calls.append((method, params))
        self._n += 1
        profile = {"activePermissionProfile": {"id": params.get("permissions")}} if params.get("permissions") else {}
        if method == "thread/start":
            return {"thread": {"id": f"thread-{self._n}"}, **profile}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}, **profile}
        if method == "turn/start":
            turn_id = f"turn-{self._n}"
            self._interrupts[turn_id] = asyncio.Event()
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self._play(params["threadId"], turn_id)))
            self.turn_starts.set()
            return {"turn": {"id": turn_id}}
        if method == "turn/interrupt":
            self._interrupts[params["turnId"]].set()
            return {}
        if method == "model/list":
            return {"data": [{"id": "gpt-test", "displayName": "GPT Test", "isDefault": True}, {"id": "gpt-other"}]}
        if method == "account/read":
            return {"account": {"email": "me@example.com", "planType": "plus"}}
        return {}

    def _notify(self, method, thread_id, turn_id, **params):
        self.engine._on_notification(method, {"threadId": thread_id, "turnId": turn_id, **params})

    async def _play(self, thread_id, turn_id):
        run = self.engine.by_thread[thread_id]
        queue = self.scripts.get(run.agent_name) or []
        script = queue.pop(0) if queue else self.default_script
        status = "completed"
        for i, step in enumerate(script):
            await asyncio.sleep(0)
            if step[0] == "tool":
                res = await self.engine._on_tool_call({"threadId": thread_id, "turnId": turn_id, "tool": step[1], "arguments": step[2]})
                self.tool_results.append((run.agent_name, step[1], res))
            elif step[0] == "ignore_interrupt":
                await asyncio.Event().wait()
            elif step[0] == "hang":
                await self._interrupts[turn_id].wait()
                status = "interrupted"
                break
            elif step[0] == "item":
                self._notify("item/completed", thread_id, turn_id, item=step[1])
            elif step[0] == "do":
                step[1]()
            elif step[0] == "late":
                old = "turn-old"
                self._notify("item/completed", thread_id, old, item={"type": "agentMessage", "id": "late", "text": step[1], "phase": "final_answer"})
                self._notify("turn/completed", thread_id, None, turn={"id": old, "status": "completed"})
            else:
                final = i == len(script) - 1
                item = {"type": "agentMessage", "id": f"m{i}", "text": step[1], "phase": "final_answer" if final else "commentary"}
                self._notify("item/completed", thread_id, turn_id, item=item)
        self._notify("turn/completed", thread_id, None, turn={"id": turn_id, "status": status})


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(config, "BROWSER_DIR", tmp_path / "browser")
    e = Engine(tmp_path / "test.db")
    e.memory.embedder = FakeEmbedder()
    e.codex = FakeCodex(e)
    return e


async def settle(engine, rounds=200):
    """Let background tasks finish."""
    for _ in range(rounds):
        await asyncio.sleep(0)
        if not [t for t in engine._tasks if not t.done()]:
            return
    await asyncio.gather(*[t for t in engine._tasks if not t.done()])
