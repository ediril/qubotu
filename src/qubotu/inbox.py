"""The shapes the engine, the inbox kinds, and the tools share.

An inbox row moves pending -> running -> done | failed | cancelled (a running row Stop was
pressed on is `stopping` until its turn ends), and only the engine writes those
transitions, each in one transaction. A `Run` is the in-memory side of one delivery: it exists while
the item is queued or running in this process, and is never the record of what happened.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine


@dataclass(eq=False)
class Run:
    item_id: int
    agent_id: int
    agent_name: str
    kind: str
    depth: int
    channel: str = ""
    running: bool = False
    stop_requested: bool = False
    stop_reason: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    final_text: str = ""
    compacted: bool = False
    error: str | None = None
    task: asyncio.Task | None = None
    done: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def fail(self, error: str) -> None:
        """Record why the run failed. The first reason wins."""
        self.error = self.error or error


@dataclass
class TurnSpec:
    prompt: str
    channel: str | None = None  # where the reply goes; defaults to the agent's chat
    store_final: bool = True  # store the reply in the agent's chat
    context: dict = field(default_factory=dict)  # handed to the kind's finisher


@dataclass
class TurnResult:
    ok: bool
    text: str = ""
    error: str | None = None


@dataclass(frozen=True)
class Kind:
    """How one kind of inbox item runs. `build` turns it into a prompt when it's about to run, and
    must not write; returning None skips the item. `begin` records that it started. `finish` does the
    follow-up work; `spec` is None when the item was cancelled before it ran. `begin` and `finish`
    run inside the engine's transaction for that transition."""

    build: Callable[["Engine", dict, dict], TurnSpec | None]
    finish: Callable[["Engine", dict, TurnSpec | None, TurnResult], None] | None = None
    begin: Callable[["Engine", dict], None] | None = None
