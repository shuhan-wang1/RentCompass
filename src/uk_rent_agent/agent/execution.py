"""Loop-bound graph execution independent of the HTTP framework."""
from __future__ import annotations

import asyncio
import threading


def consume_abandoned_graph_task(task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


class GraphLoopRunner:
    """One loop-local graph/provider generation in a bounded self-healing pool."""

    def __init__(self):
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._loop = None
        self._thread = None
        self._quarantined = False
        self.graph = None

    def _serve(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def _ensure_started(self):
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._serve, name="agent_graph_loop", daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("agent graph loop failed to start")
        if self._loop is None:
            raise RuntimeError("agent graph loop unavailable")

    def available(self):
        with self._start_lock:
            return not self._quarantined

    def submit(self, coroutine):
        self._ensure_started()
        with self._start_lock:
            if self._quarantined:
                coroutine.close()
                raise RuntimeError("agent graph loop is quarantined")
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def quarantine(self, recovered_callback):
        """Reject new work until a queued heartbeat proves this loop is responsive."""
        self._ensure_started()
        with self._start_lock:
            if self._quarantined:
                return False
            self._quarantined = True
            loop = self._loop

        async def _heartbeat():
            return True

        probe = asyncio.run_coroutine_threadsafe(_heartbeat(), loop)

        def _recovered(_future):
            try:
                _future.result()
            except BaseException:
                return
            with self._start_lock:
                self._quarantined = False
            recovered_callback(self)

        probe.add_done_callback(_recovered)
        return True


