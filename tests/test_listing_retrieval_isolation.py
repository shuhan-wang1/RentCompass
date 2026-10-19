from __future__ import annotations

import threading

from core.listing_retrieval import ListingRetrievalSession


class Store:
    def build_index(self, rows):
        self.rows = rows

    def search(self, _query, top_k=10):
        return self.rows[:top_k]


class Coordinator:
    def __init__(self):
        self.property_store = Store()

    def enhanced_search(self, _query, _criteria):
        return self.property_store.rows, [], []


def test_fallback_preserves_request_candidates_after_another_search():
    shared = Coordinator()
    lock = threading.Lock()
    first = ListingRetrievalSession(shared, [{"address": "Camden"}], lock)
    second = ListingRetrievalSession(shared, [{"address": "Islington"}], lock)
    first.rank([("Camden", "first")], {})
    second.rank([("Islington", "second")], {})

    assert first.similar("first") == [{"address": "Camden"}]
    assert second.similar("second") == [{"address": "Islington"}]


def test_recall_results_are_detached_from_provider_and_input_rows():
    rows = [{"address": "Camden", "amenities": ["lift"]}]
    session = ListingRetrievalSession(Coordinator(), rows, threading.Lock())
    ranked, _, _ = session.rank([("Camden", "query")], {})
    ranked[0]["amenities"].append("changed")
    rows[0]["amenities"].append("input changed")

    assert session.similar("query")[0]["amenities"] == ["lift"]
