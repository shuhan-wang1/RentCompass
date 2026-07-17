"""Regression test for PropertyEmbeddingStore.build_index embedding memoization.

build_index is called once per search request (search_properties rebuilds the
index on freshly scraped rows), so unchanged rows must reuse their cached vector
instead of being re-encoded every call.
"""
import importlib
import os
import sys

import numpy as np
import pytest


def _pin_app():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)
    for name in list(sys.modules):
        if name in ("core", "rag") or name.startswith(("core.", "rag.")):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "app" not in path:
                del sys.modules[name]


_pin_app()

pe_mod = importlib.import_module("rag.property_embeddings")
PropertyEmbeddingStore = pe_mod.PropertyEmbeddingStore


class _FakeModel:
    """Deterministic stand-in for SentenceTransformer; records each encode batch."""

    def __init__(self):
        self.batches = []

    def encode(self, texts, show_progress_bar=False):
        texts = list(texts)
        self.batches.append(texts)
        # 3-dim vector derived from text so identical text -> identical vector
        return np.array([[float(len(t)), float(sum(map(ord, t)) % 97), 1.0] for t in texts],
                        dtype=np.float32)


def _store():
    # Bypass __init__ so we never download the real sentence-transformers model.
    store = PropertyEmbeddingStore.__new__(PropertyEmbeddingStore)
    store.index = None
    store.properties = []
    store.property_name_index = {}
    store._embedding_cache = {}
    store.model = _FakeModel()
    return store


def _rows():
    return [
        {"Address": "1 Camden Rd", "Price": "£1200 pcm", "Description": "cosy studio"},
        {"Address": "2 Camden Rd", "Price": "£1400 pcm", "Description": "bright 1-bed"},
        {"Address": "3 Camden Rd", "Price": "£1600 pcm", "Description": "spacious flat"},
    ]


def test_unchanged_rows_are_not_re_encoded():
    store = _store()
    rows = _rows()

    store.build_index(rows)
    assert len(store.model.batches) == 1
    assert len(store.model.batches[0]) == 3  # all three encoded on first build
    assert store.index is not None
    assert len(store.properties) == 3

    # Rebuild with the SAME rows: nothing new to encode -> no further encode call.
    store.build_index(rows)
    assert len(store.model.batches) == 1  # unchanged: no second batch
    assert store.index is not None


def test_only_new_or_changed_rows_are_encoded():
    store = _store()
    rows = _rows()
    store.build_index(rows)
    assert len(store.model.batches) == 1

    # Add one brand-new row and change the description of an existing one.
    changed = [dict(r) for r in rows]
    changed[0]["Description"] = "cosy studio, newly refurbished"  # content changed
    changed.append({"Address": "4 Camden Rd", "Price": "£1800 pcm", "Description": "penthouse"})

    store.build_index(changed)
    assert len(store.model.batches) == 2
    # only the changed row + the new row get encoded (2), the two unchanged reuse cache
    assert len(store.model.batches[1]) == 2
    assert len(store.properties) == 4


def test_cache_produces_consistent_search_results():
    store = _store()
    rows = _rows()
    store.build_index(rows)
    first = store.search("cosy studio Camden", top_k=3)

    # Rebuild (all cached) and search again: same ordering / scores.
    store.build_index(rows)
    second = store.search("cosy studio Camden", top_k=3)

    assert [p["Address"] for p in first] == [p["Address"] for p in second]
    assert [round(p["similarity_score"], 5) for p in first] == \
           [round(p["similarity_score"], 5) for p in second]
