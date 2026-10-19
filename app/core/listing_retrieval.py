"""Request-owned listing recall over a shared embedding model."""
from __future__ import annotations

import copy
from collections import deque
from contextlib import contextmanager


class ListingRetrievalSession:
    """Keep both ordinary and fallback recall tied to the same candidate snapshot.

    Native coordinators fork a cheap index while sharing their encoder. Older
    injected providers are serialized and rebound to this snapshot on every read.
    No provider-owned rows escape into a request's downstream mutation pipeline.
    """

    def __init__(self, coordinator, rows, compatibility_lock):
        factory = getattr(coordinator, "for_search", None)
        self._private = callable(factory)
        self.coordinator = factory() if self._private else coordinator
        self.rows = copy.deepcopy(rows)
        self._lock = compatibility_lock
        self._built = False

    @contextmanager
    def _indexed(self):
        if not self._private:
            self._lock.acquire()
        try:
            if not self._private or not self._built:
                self.coordinator.property_store.build_index(self.rows)
                self._built = True
            yield
        finally:
            if not self._private:
                self._lock.release()

    def rank(self, area_queries, criteria):
        with self._indexed():
            if len(area_queries) == 1:
                return copy.deepcopy(
                    self.coordinator.enhanced_search(area_queries[0][1], criteria)
                )
            queues = []
            for area, query in area_queries:
                ranked, _, _ = self.coordinator.enhanced_search(query, criteria)
                queues.append(deque(
                    copy.deepcopy(row) for row in ranked
                    if str(row.get("_search_area", "")).lower() == area.lower()
                ))
            ranked = []
            while any(queues):
                for queue in queues:
                    if queue:
                        ranked.append(queue.popleft())
            return ranked, [], []

    def similar(self, query, top_k=10):
        with self._indexed():
            return copy.deepcopy(self.coordinator.property_store.search(query, top_k=top_k))
