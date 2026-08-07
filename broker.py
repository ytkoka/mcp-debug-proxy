"""In-memory pub/sub for fanning proxy activity out to live subscribers
(e.g. the /events SSE endpoint), decoupled from the proxy's hot path.

publish() is entirely synchronous -- no `await` anywhere in this module --
so a caller on the request-handling path (relay() -> log() -> publish())
can never block on a slow or dead subscriber. Backpressure is absorbed by
dropping the oldest queued record for a subscriber whose queue is full,
never by blocking the publisher.
"""
from __future__ import annotations

import asyncio
import collections


class Broker:
    def __init__(self, queue_maxsize: int = 512, history_size: int = 500):
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_maxsize = queue_maxsize
        self._history: collections.deque[dict] = collections.deque(maxlen=history_size)
        self.dropped_total = 0

    def subscribe(self) -> tuple[asyncio.Queue, list[dict]]:
        """Register a new subscriber and snapshot history in one synchronous
        step (no `await` in between) so a concurrent publish() can't land in
        the gap and be missed by the new subscriber."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        history = list(self._history)
        self._subscribers.add(queue)
        return queue, history

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, record: dict, history: bool = True) -> None:
        """Non-blocking fan-out. Never raises -- a bug here must not be able
        to propagate into the proxy's request-handling path."""
        try:
            if history:
                self._history.append(record)
            if not self._subscribers:
                return
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(record)
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self.dropped_total += 1
                    try:
                        queue.put_nowait(record)
                    except asyncio.QueueFull:
                        pass
        except Exception:
            pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
