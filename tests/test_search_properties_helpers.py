"""
Unit tests for the search-tool helpers that make the customer path city-correct:
bedroom parsing (so a "1 bed" query filters exactly), geo parsing, the
coordinate-based commute estimate (reliable outside London), and the London
bounding-box check.

Importing search_properties is cheap: the sentence-transformers model is only
loaded lazily on a real search, not at import time.
"""

import os
import sys

# --- Pin the real source roots ahead of tests/ (which holds stale copies of
# `core` that would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

from core.tools.search_properties import (
    _extract_bedrooms,
    _parse_geo,
    _in_london,
    _coord_commute_minutes,
)


def test_extract_bedrooms():
    assert _extract_bedrooms("a studio near UCL") == (0, 0)
    assert _extract_bedrooms("1 bed flat in Manchester under 1200") == (1, 1)
    assert _extract_bedrooms("looking for a 2-bedroom place") == (2, 2)
    assert _extract_bedrooms("two bed apartment") == (2, 2)
    assert _extract_bedrooms("somewhere nice and central") is None


def test_parse_geo():
    assert _parse_geo("53.4415, -2.2159") == (53.4415, -2.2159)
    assert _parse_geo("") is None
    assert _parse_geo("not coords") is None


def test_in_london():
    assert _in_london({"lat": 51.52, "lng": -0.13}) is True     # Bloomsbury
    assert _in_london({"lat": 53.48, "lng": -2.24}) is False    # Manchester
    assert _in_london(None) is False


def test_coord_commute_minutes_is_reliable_and_bounded():
    # ~2 km apart in Manchester -> a small, sane transit estimate (not 1000+).
    dest = {"lat": 53.4425, "lng": -2.2325}
    mins = _coord_commute_minutes("53.4587, -2.2845", dest)
    assert isinstance(mins, int)
    assert 0 < mins < 60
    # Missing coordinates degrade gracefully.
    assert _coord_commute_minutes("", dest) is None
    assert _coord_commute_minutes("53.4, -2.2", None) is None


def test_missing_rag_dependency_falls_back_to_live_listing_order(monkeypatch):
    import builtins
    import core.tools.search_properties as search_properties

    monkeypatch.setattr(search_properties, "_RAG_COORDINATOR", None)
    real_import = builtins.__import__

    def missing_rag(name, *args, **kwargs):
        if name == "rag.rag_coordinator":
            raise ModuleNotFoundError("No module named 'faiss'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_rag)
    coordinator = search_properties._get_rag_coordinator()
    rows = [{"Address": "1 Camden Rd"}, {"Address": "2 Camden Rd"}]
    coordinator.property_store.build_index(rows)
    ranked, _context, _area = coordinator.enhanced_search("Camden", {})
    assert ranked == rows
