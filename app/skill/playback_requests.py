"""Invalidate pending voice playback when a newer command arrives."""

import asyncio
from contextvars import ContextVar
from threading import Lock, RLock

_lock = RLock()
_generations = {}
_write_locks = {}
_ticket = ContextVar("voice_playback_ticket", default=None)


class SupersededPlayback(Exception):
    pass


def begin(player):
    with _lock:
        generation = _generations.get(player, 0) + 1
        _generations[player] = generation
        return player, generation


def current(ticket):
    with _lock:
        return _generations.get(ticket[0]) == ticket[1]


def activate(ticket):
    _ticket.set(ticket)


def check():
    ticket = _ticket.get()
    if ticket is not None and not current(ticket):
        raise SupersededPlayback()


def write_lock(player):
    with _lock:
        return _write_locks.setdefault(player, Lock())


async def write(operation, *args, **kwargs):
    ticket = _ticket.get()
    if ticket is None:
        return await operation(*args, **kwargs)
    lock = write_lock(ticket[0])
    # Poll without blocking the request thread or leaking an acquired lock
    # if this coroutine is cancelled while waiting.
    while not lock.acquire(blocking=False):
        check()
        await asyncio.sleep(0.02)
    try:
        check()
        return await operation(*args, **kwargs)
    finally:
        lock.release()
