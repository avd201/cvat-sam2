"""FIFO admission to ONE exclusive resource (the GPU propagation slot).

Replaces the old "second request gets HTTP 429" rule: requests now wait their turn, in arrival order, and the
HTTP layer tells the client how many runs are ahead of it.

Lifecycle of a request:   join()  ->  wait_turn() (polled until it returns True)  ->  leave()
  * join() is called BEFORE the (slow) frame download, so the order is the order of arrival and a full queue is
    refused before any work is wasted (QueueFull -> HTTP 429).
  * leave() is idempotent and MUST run on every exit path (finished, failed, cancelled, client gone).
  * Safety net: a ticket whose owner stops polling for `stale_s` seconds is dropped, so a leaked ticket (e.g. a
    response that was created but whose stream never started because the client vanished) cannot freeze the
    service for everybody else. Only tickets that are still WAITING can expire; the running one is released by its
    own worker.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class QueueFull(Exception):
    """Too many requests are already waiting."""


class TicketExpired(Exception):
    """The ticket was dropped (stale) before it got its turn."""


class Ticket:
    __slots__ = ("touched",)

    def __init__(self, now: float) -> None:
        self.touched = now


class FifoSlot:
    def __init__(self, max_waiting: int = 8, stale_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_waiting < 0:
            raise ValueError("max_waiting must be >= 0")
        self.max_waiting = max_waiting
        self.stale_s = stale_s
        self._clock = clock
        self._cond = threading.Condition()
        self._waiting: deque[Ticket] = deque()
        self._running: Ticket | None = None

    # ------------------------------------------------------------------ state (for /healthz)
    @property
    def busy(self) -> bool:
        with self._cond:
            return self._running is not None

    @property
    def waiting(self) -> int:
        with self._cond:
            self._purge()
            return len(self._waiting)

    # ------------------------------------------------------------------ protocol
    def join(self) -> Ticket:
        """Take a place at the back of the queue. Raises QueueFull when `max_waiting` requests already wait
        (tickets that have not reached the front yet count as waiting, even while the slot is idle)."""
        with self._cond:
            self._purge()
            if len(self._waiting) >= self.max_waiting:
                raise QueueFull(f"{len(self._waiting)} requests are already waiting")
            ticket = Ticket(self._clock())
            self._waiting.append(ticket)
            return ticket

    def position(self, ticket: Ticket) -> int:
        """Number of runs ahead of `ticket` (the running one counts). 0 = it is its turn / already running."""
        with self._cond:
            self._purge()
            ticket.touched = self._clock()
            if self._running is ticket:
                return 0
            try:
                return self._waiting.index(ticket) + (1 if self._running is not None else 0)
            except ValueError:
                raise TicketExpired("queue ticket expired") from None

    def wait_turn(self, ticket: Ticket, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the slot. True = `ticket` now holds it (call leave() when done)."""
        with self._cond:
            ticket.touched = self._clock()
            if self._running is ticket:
                return True

            def my_turn() -> bool:
                self._purge()
                return self._running is None and bool(self._waiting) and self._waiting[0] is ticket

            if ticket not in self._waiting:
                raise TicketExpired("queue ticket expired")
            if self._cond.wait_for(my_turn, timeout):
                self._waiting.popleft()
                self._running = ticket
                return True
            if ticket not in self._waiting:
                raise TicketExpired("queue ticket expired")
            return False

    def leave(self, ticket: Ticket) -> None:
        """Give the slot back / withdraw from the queue. Idempotent."""
        with self._cond:
            if self._running is ticket:
                self._running = None
            else:
                try:
                    self._waiting.remove(ticket)
                except ValueError:
                    pass
            self._cond.notify_all()

    # ------------------------------------------------------------------ internals
    def _purge(self) -> None:
        """Drop waiting tickets nobody has polled for `stale_s` seconds. Caller holds the lock."""
        now = self._clock()
        if any(now - t.touched > self.stale_s for t in self._waiting):
            self._waiting = deque(t for t in self._waiting if now - t.touched <= self.stale_s)
            self._cond.notify_all()
