"""Area-is-a-destination reclassification (search_properties_impl, step 1.4).

When the user names a DESTINATION (university/workplace) as where they want to
LIVE, the tool must:
  * lock it in as the commute destination and default to commute-mode
    (never ask "commute or not?"),
  * NON-BLOCKING DEFAULT: default the residential search_area to the destination's
    OWN area (its searchable slug) and proceed — never hard-block with a
    "where do you want to live?" (missing_area) question. A concurrent recommender
    then surfaces verified nearby areas as clickable chips.
  * so with nothing else known the SOFT gate fires for budget/room_type ONLY —
    commute is already satisfied by the lock and is never re-asked.

A genuine residential area (Camden) must keep the CURRENT flow: the soft gate
still asks about commute when nothing is known.

classify_place / is_destination are monkeypatched so these tests do NOT depend
on the parallel classifier build landing (the tool consumes both by contract).
The scraper is stubbed to prove the gate fires before any network call.
"""

import asyncio
import os
import sys

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

from core.scraping import on_demand
from core.tools.search_properties import search_properties_impl


_UCL_ADDR = "University College London, Gower St, London WC1E 6BT"
_GOOGLE_ADDR = "Google, 6 Pancras Square, London N1C 4AG"

# name -> (kind, geocodable address). Everything else is a residential "area".
_DESTINATIONS = {
    "ucl": ("university", _UCL_ADDR),
    "university college london": ("university", _UCL_ADDR),
    "google london": ("workplace", _GOOGLE_ADDR),
}


def _fake_classify(name):
    n = (name or "").strip().lower()
    if n in _DESTINATIONS:
        kind, address = _DESTINATIONS[n]
        return {"kind": kind, "slug": n.replace(" ", "-"), "city": "london",
                "address": address, "source": "landmark"}
    return {"kind": "area", "slug": n.replace(" ", "-"), "city": None,
            "address": None, "source": "landmark"}


def _fake_is_dest(kind_or_result):
    kind = (kind_or_result.get("kind")
            if isinstance(kind_or_result, dict) else kind_or_result)
    return kind in ("university", "workplace")


@pytest.fixture
def classifier(monkeypatch):
    """Patch the classifier contract the tool consumes at call time. is_destination
    may not yet exist on the real module, so raising=False (the tool has a fallback,
    but here we want the real symbol path exercised)."""
    monkeypatch.setattr(on_demand, "classify_place", _fake_classify)
    monkeypatch.setattr(on_demand, "is_destination", _fake_is_dest, raising=False)


def _no_scrape(monkeypatch):
    monkeypatch.setattr(
        on_demand, "get_listings",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("gate must fire before scraping")))


def _run(**kwargs):
    return asyncio.run(search_properties_impl(**kwargs))


# ── destination token: lock commute + DEFAULT area to the destination's own area ──
def test_destination_area_locks_commute_and_defaults_area(classifier, monkeypatch):
    # area="UCL" (a destination) with no residential area: lock commute to UCL AND
    # default the search area to UCL's own slug (non-blocking). NOT the hard
    # missing-area gate; commute is satisfied so the soft gate asks budget/room_type only.
    _no_scrape(monkeypatch)  # the soft gate still fires before any scrape
    res = _run(area="UCL", current_message="places near UCL")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"   # NOT missing_area anymore
    assert "commute" not in res["missing_fields"]          # commute satisfied by the lock
    kc = res["known_criteria"]
    assert kc["area"] == "ucl"                     # defaulted to the destination's own slug
    assert kc["commute_destination"] == _UCL_ADDR  # locked, geocodable address
    assert kc["no_commute"] is False               # commute-mode defaulted on
    # the lock is persisted so it survives to the next turn
    assert res["extracted_so_far"]["destination"] == _UCL_ADDR


def test_destination_area_zh_soft_gate(classifier, monkeypatch):
    # zh path: same non-blocking default; a soft-criteria clarification (zh) with the
    # commute already locked — never the hard missing-area gate.
    _no_scrape(monkeypatch)
    res = _run(area="UCL", current_message="UCL 附近有什么房源")
    assert res["clarification_kind"] == "soft_criteria"
    assert res["known_criteria"]["area"] == "ucl"
    assert res["known_criteria"]["commute_destination"] == _UCL_ADDR


def test_workplace_area_also_locks_and_defaults(classifier, monkeypatch):
    # Broadening the university-only check to is_destination covers workplaces too.
    _no_scrape(monkeypatch)
    res = _run(area="Google London", current_message="somewhere near Google London")
    assert res["clarification_kind"] == "soft_criteria"
    assert "commute" not in res["missing_fields"]
    kc = res["known_criteria"]
    assert kc["area"] == "google-london"           # defaulted to the workplace's own slug
    assert kc["commute_destination"] == _GOOGLE_ADDR


# ── residential token: current flow intact, commute still asked ─────────────
def test_residential_area_unchanged_commute_still_asked(classifier, monkeypatch):
    _no_scrape(monkeypatch)
    res = _run(area="Camden", current_message="flats in Camden")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"   # NOT reclassified
    assert "commute" in res["missing_fields"]             # soft gate still asks commute
    assert "budget" in res["missing_fields"]
    assert "room_type" in res["missing_fields"]
    kc = res["known_criteria"]
    assert kc["area"] == "Camden"
    assert kc["commute_destination"] is None


# ── both supplied: residential area + destination both honored ─────────────
def test_both_supplied_area_and_destination_honored(classifier, monkeypatch):
    _no_scrape(monkeypatch)
    # area="UCL" (a destination) + location="Camden" (a real residential area).
    res = _run(area="UCL", location="Camden", current_message="near UCL, Camden area")
    assert res["status"] == "need_clarification"
    # residential area present -> soft gate (not the hard missing-area gate)
    assert res["clarification_kind"] == "soft_criteria"
    # destination locked -> commute is satisfied and dropped from soft_missing
    assert "commute" not in res["missing_fields"]
    kc = res["known_criteria"]
    assert kc["area"] == "Camden"                  # residential kept
    assert kc["commute_destination"] == _UCL_ADDR  # destination locked


# ── no regression: a commute_destination-derived slug is NOT reclassified ───
def test_commute_destination_slug_not_reclassified(classifier, monkeypatch):
    _no_scrape(monkeypatch)
    # commute_destination given, no area -> search_area is derived from UCL's slug.
    # That derived slug must NOT be reclassified/cleared; commute stays annotated to
    # UCL, and the soft gate must NOT re-ask commute (destination already known).
    res = _run(commute_destination="UCL", current_message="find me a place")
    assert res["status"] == "need_clarification"
    assert res["clarification_kind"] == "soft_criteria"
    assert "commute" not in res["missing_fields"]
    assert res["known_criteria"]["area"] == "ucl"   # slug, still used as search area
