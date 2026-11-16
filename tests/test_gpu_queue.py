"""FifoSlot: order, positions, capacity, idempotent leave, and expiry of abandoned tickets."""
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sam-service"))

from app.gpu_queue import FifoSlot, QueueFull, TicketExpired  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_idle_slot_is_granted_immediately():
    s = FifoSlot(max_waiting=2)
    t = s.join()
    assert s.wait_turn(t, 0) is True and s.busy and s.waiting == 0
    assert s.position(t) == 0
    s.leave(t)
    assert not s.busy


def test_fifo_order_and_positions():
    s = FifoSlot(max_waiting=5)
    a, b, c = s.join(), s.join(), s.join()
    assert s.wait_turn(a, 0)
    assert [s.position(x) for x in (a, b, c)] == [0, 1, 2]       # the running one counts as "ahead"
    assert s.wait_turn(c, 0) is False                             # c may not jump the queue
    assert s.wait_turn(b, 0) is False                             # b waits while a runs
    s.leave(a)
    assert s.wait_turn(c, 0) is False                             # still b's turn
    assert s.wait_turn(b, 0) is True
    assert s.position(c) == 1
    s.leave(b)
    assert s.wait_turn(c, 0) is True


def test_a_waiter_blocked_in_wait_turn_wakes_when_the_slot_is_released():
    s = FifoSlot(max_waiting=3)
    a, b = s.join(), s.join()
    assert s.wait_turn(a, 0)
    got = []
    th = threading.Thread(target=lambda: got.append(s.wait_turn(b, 5)))
    th.start()
    time.sleep(0.1)
    s.leave(a)
    th.join(3)
    assert got == [True]


def test_queue_full_counts_tickets_that_are_not_running_yet():
    s = FifoSlot(max_waiting=2)
    a = s.join()                      # holds a place even though it has not asked for the slot yet
    s.join()
    with pytest.raises(QueueFull):
        s.join()
    s.leave(a)
    s.join()                          # a place was freed


def test_max_waiting_zero_refuses_everyone():
    s = FifoSlot(max_waiting=0)
    with pytest.raises(QueueFull):
        s.join()


def test_leave_is_idempotent_and_withdraws_a_waiter():
    s = FifoSlot(max_waiting=3)
    a, b, c = s.join(), s.join(), s.join()
    assert s.wait_turn(a, 0)
    s.leave(b)
    s.leave(b)                        # second call is harmless
    assert s.position(c) == 1
    s.leave(a)
    s.leave(a)
    assert s.wait_turn(c, 0) and s.waiting == 0
    s.leave(c)
    assert not s.busy


def test_abandoned_waiter_expires_and_does_not_block_the_next_one():
    clock = Clock()
    s = FifoSlot(max_waiting=3, stale_s=60, clock=clock)
    a = s.join()                      # never polled again (client vanished before its stream started)
    b = s.join()
    clock.t += 30
    assert s.position(b) == 1         # b polls, a does not
    clock.t += 31                     # a is now 61 s stale, b only 31 s
    assert s.wait_turn(b, 0) is True  # a was dropped, b gets the slot
    with pytest.raises(TicketExpired):
        s.wait_turn(a, 0)
    with pytest.raises(TicketExpired):
        s.position(a)
    s.leave(a)                        # harmless


def test_the_running_ticket_never_expires():
    clock = Clock()
    s = FifoSlot(max_waiting=2, stale_s=10, clock=clock)
    a = s.join()
    assert s.wait_turn(a, 0)
    clock.t += 10_000
    assert s.busy and s.position(a) == 0
    b = s.join()
    assert s.position(b) == 1
