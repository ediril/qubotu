"""In-process pub/sub that feeds the browser's server-sent events stream."""

import asyncio


class Bus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self.closed = False

    def close(self) -> None:
        """End every open stream, so the server can shut down."""
        self.closed = True
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def refresh(self, *topics: str) -> None:
        """Tell the browser to reload every fragment subscribed to one of these topics."""
        self.publish({"type": "refresh", "topics": list(topics)})
