"""Bounded public stages; drafts and tool payloads never enter this channel."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction

_sink = ContextVar("chat_progress_sink", default=None)
STAGES = frozenset({"understanding", "searching", "checking", "writing", "saving"})


@contextmanager
def progress_context(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def report_progress(stage: str) -> None:
    sink = _sink.get()
    if sink is not None and stage in STAGES:
        try:
            sink(stage)
        except RuntimeError:
            # A disconnected transport must not change graph execution semantics.
            pass


def with_progress(stage, function):
    """Preserve node signatures and sync/async behavior while reporting a stage."""
    if stage is None:
        return function
    if iscoroutinefunction(function):
        @wraps(function)
        async def run(*args, **kwargs):
            report_progress(stage)
            return await function(*args, **kwargs)
    else:
        @wraps(function)
        def run(*args, **kwargs):
            report_progress(stage)
            return function(*args, **kwargs)
    return run
