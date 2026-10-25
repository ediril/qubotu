"""Shared memory: SQLite rows, local embeddings, hybrid vector + full-text recall.

Embeddings come from fastembed (ONNX, no PyTorch). If the model can't be
loaded, recall falls back to full-text search only.
"""

import asyncio
import logging
import re

import numpy as np

from . import config
from .db import DB, now

log = logging.getLogger(__name__)

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
KINDS = ("fact", "preference", "project")


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query that matches any of its words."""
    words = re.findall(r"\w+", text.lower())
    words = [w for w in words if len(w) > 1]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words[:20])


class Embedder:
    def __init__(self):
        self._model = None
        self._failed = False
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return not self._failed

    def _load(self):
        from fastembed import TextEmbedding

        return TextEmbedding(EMBED_MODEL, cache_dir=str(config.MODELS_DIR))

    async def embed(self, texts: list[str]) -> list[np.ndarray] | None:
        if self._failed or not texts:
            return None
        async with self._lock:
            try:
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load)
                vecs = await asyncio.to_thread(lambda: list(self._model.embed(texts)))
            except Exception:
                log.exception("Embedding model unavailable; using full-text search only.")
                self._failed = True
                return None
        out = []
        for v in vecs:
            v = np.asarray(v, dtype=np.float32)
            out.append(v / (np.linalg.norm(v) or 1.0))
        return out


class Memory:
    """The memory interface agents and the UI use. A dedicated system can replace it later."""

    def __init__(self, db: DB, embedder: Embedder | None = None):
        self.db = db
        self.embedder = embedder or Embedder()

    async def _embedding(self, text: str) -> bytes | None:
        vecs = await self.embedder.embed([text])
        return vecs[0].tobytes() if vecs else None

    async def add(self, text: str, kind: str = "fact", agent_id: int | None = None) -> int:
        text = text.strip()
        kind = kind if kind in KINDS else "fact"
        ts = now()
        mid = self.db.insert("memories", text=text, kind=kind, agent_id=agent_id, created_at=ts, updated_at=ts)
        emb = await self._embedding(text)
        if emb:
            self.db.update("memories", mid, embedding=emb)
        return mid

    async def update(self, memory_id: int, text: str | None = None, kind: str | None = None, pinned: bool | None = None) -> bool:
        if not self.get(memory_id):
            return False
        fields: dict = {"updated_at": now()}
        if text is not None:
            fields["text"] = text.strip()
            fields["embedding"] = await self._embedding(text)
        if kind in KINDS:
            fields["kind"] = kind
        if pinned is not None:
            fields["pinned"] = int(pinned)
        self.db.update("memories", memory_id, **fields)
        return True

    def delete(self, memory_id: int) -> bool:
        if not self.get(memory_id):
            return False
        self.db.run("DELETE FROM memories WHERE id = ?", memory_id)
        return True

    def get(self, memory_id: int) -> dict | None:
        return self.db.one("SELECT id, text, kind, agent_id, pinned, created_at, updated_at FROM memories WHERE id = ?", memory_id)

    def all(self) -> list[dict]:
        return self.db.all(
            "SELECT m.id, m.text, m.kind, m.agent_id, m.pinned, m.created_at, m.updated_at, a.name AS agent_name "
            "FROM memories m LEFT JOIN agents a ON a.id = m.agent_id ORDER BY m.pinned DESC, m.updated_at DESC"
        )

    def profile(self, limit: int = 25) -> list[dict]:
        """The memories included at the start of every run: pinned first, then most recently updated."""
        pinned = self.db.all("SELECT id, text, kind FROM memories WHERE pinned = 1 ORDER BY updated_at DESC")
        rest = self.db.all(
            "SELECT id, text, kind FROM memories WHERE pinned = 0 ORDER BY updated_at DESC LIMIT ?",
            max(0, limit - len(pinned)),
        )
        return pinned + rest

    async def search(self, query: str, limit: int = 8) -> list[dict]:
        ranked: dict[int, float] = {}

        # Full-text ranks.
        q = fts_query(query)
        if q:
            rows = self.db.all(
                "SELECT rowid AS id FROM memories_fts WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT 50", q
            )
            for rank, r in enumerate(rows):
                ranked[r["id"]] = ranked.get(r["id"], 0) + 1 / (60 + rank)

        # Vector ranks.
        vecs = await self.embedder.embed([query])
        if vecs:
            rows = self.db.all("SELECT id, embedding FROM memories WHERE embedding IS NOT NULL")
            if rows:
                matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                scores = matrix @ vecs[0]
                order = np.argsort(-scores)[:50]
                for rank, i in enumerate(order):
                    if scores[i] < 0.3:
                        break
                    mid = rows[int(i)]["id"]
                    ranked[mid] = ranked.get(mid, 0) + 1 / (60 + rank)

        top = sorted(ranked, key=ranked.get, reverse=True)[:limit]
        return [m for m in (self.get(i) for i in top) if m]

    async def backfill(self) -> None:
        """Embed memories that were saved while the embedding model was unavailable."""
        rows = self.db.all("SELECT id, text FROM memories WHERE embedding IS NULL")
        if not rows:
            return
        vecs = await self.embedder.embed([r["text"] for r in rows])
        if vecs:
            for r, v in zip(rows, vecs):
                self.db.update("memories", r["id"], embedding=v.tobytes())
