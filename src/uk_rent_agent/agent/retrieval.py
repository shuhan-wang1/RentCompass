from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from uk_rent_agent.agent.contracts import Listing, RetrievalResult, SourceHealth

SourceCallable = Callable[[dict[str, Any]], Awaitable[list[dict]] | list[dict]]


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None


@dataclass
class RetrievalSource:
    name: str
    fetch: SourceCallable
    concurrency: int = 2
    min_interval_seconds: float = 0.0
    timeout_seconds: float = 20.0
    failure_threshold: int = 3
    recovery_seconds: float = 60.0
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)
    _last_call: float = field(default=0.0, init=False, repr=False)
    _circuit: _Circuit = field(default_factory=_Circuit, init=False, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(max(1, self.concurrency))

    def circuit_open(self) -> bool:
        opened = self._circuit.opened_at
        if opened is None:
            return False
        if time.monotonic() - opened >= self.recovery_seconds:
            self._circuit = _Circuit()
            return False
        return True

    async def run(self, criteria: dict[str, Any]) -> tuple[list[dict], SourceHealth]:
        if self.circuit_open():
            return [], SourceHealth(
                source=self.name, ok=False, error="circuit_open", cache_status="skipped", circuit_open=True
            )
        started = time.monotonic()
        try:
            async with self._semaphore:
                wait = self.min_interval_seconds - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait + random.uniform(0, min(0.1, wait)))
                self._last_call = time.monotonic()
                value = self.fetch(criteria)
                if asyncio.iscoroutine(value):
                    value = await asyncio.wait_for(value, timeout=self.timeout_seconds)
            rows = list(value or [])
            self._circuit = _Circuit()
            return rows, SourceHealth(
                source=self.name,
                ok=True,
                latency_ms=(time.monotonic() - started) * 1000,
                count=len(rows),
            )
        except Exception as exc:  # source failures are isolated by design
            self._circuit.failures += 1
            if self._circuit.failures >= self.failure_threshold:
                self._circuit.opened_at = time.monotonic()
            return [], SourceHealth(
                source=self.name,
                ok=False,
                latency_ms=(time.monotonic() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
                circuit_open=self.circuit_open(),
            )


class RetrievalPipeline:
    def __init__(self, sources: list[RetrievalSource], *, max_fanout: int = 4):
        self.sources = sources
        self._fanout = asyncio.Semaphore(max(1, max_fanout))

    async def retrieve(self, criteria: dict[str, Any]) -> RetrievalResult:
        async def run(source: RetrievalSource):
            async with self._fanout:
                return source.name, await source.run(criteria)

        results = await asyncio.gather(*(run(source) for source in self.sources))
        listings: list[Listing] = []
        health: dict[str, SourceHealth] = {}
        seen: set[str] = set()
        freshness: dict[str, float] = {}
        for name, (rows, status) in results:
            health[name] = status
            for raw in rows:
                item = dict(raw)
                item.setdefault("source", name)
                key = str(item.get("url") or item.get("URL") or item.get("id") or item.get("Address") or item)
                if key in seen:
                    continue
                seen.add(key)
                stamp = float(item.get("freshness") or time.time())
                freshness[key] = stamp
                item["freshness"] = stamp
                listings.append(Listing.model_validate(item))
        return RetrievalResult(listings=listings, per_source=health, freshness=freshness)
