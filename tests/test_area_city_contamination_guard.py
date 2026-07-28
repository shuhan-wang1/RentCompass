"""The area recommender's contamination guard must compare CITIES, not their casing.

Observed in production on 2026-07-27, serving a real user asking to live near UCL::

    [AREA_RECO] drop 'Bloomsbury': wrong city (london != London)
    [AREA_RECO] drop 'King's Cross': wrong city (london != London)

Both areas are in the destination's own city. The guard exists to stop an LLM slip
putting a Manchester suburb in a London answer (see the module docstring: "sit in the
destination's city (contamination guard)"), and instead it dropped every same-city
candidate the pipeline produced — the user got zero areas and zero listings.

The two sides of that comparison come from different places and were never reconciled:

  * ``cand_city`` is ``classify_place()``'s canonical city, and the curated tables in
    ``core.scraping.on_demand`` store it LOWERCASE (e.g. ``{"city": "london"}``);
  * ``dest_city`` is the requested area as it arrived from the caller — "London".

So the guard compared "london" to "London" and fired on the contamination-FREE case.
Only the comparison is normalised: the values keep their original case because
``anchor`` geocodes with them and the emitted ``city`` field is user-visible.
"""

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

import core.recommend_areas as ram


def _validate(monkeypatch, *, cand_city, dest_city, name="Bloomsbury"):
    """Run _validate_one with every network dependency stubbed.

    ``compute_commute=False`` keeps the geocode + commute gates out of the way so the
    ONLY thing under test is the city check.
    """
    monkeypatch.setattr(
        ram, "classify_place",
        lambda n: {"kind": "area", "slug": n.lower().replace(" ", "-"), "city": cand_city},
    )
    monkeypatch.setattr(ram, "is_destination", lambda place: False)
    monkeypatch.setattr(ram, "resolve_location", lambda n: (n.lower(), None))
    monkeypatch.setattr(ram, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    monkeypatch.setattr(
        ram, "commute_minutes",
        lambda *a, **k: pytest.fail("commute must not be computed in no-commute mode"),
    )
    return ram._validate_one(
        {"name": name, "reason": "close to campus"},
        "UCL Bloomsbury",
        {"lat": 51.5246, "lng": -0.1340},
        dest_city,
        True,          # london
        40,            # max_commute
        set(),         # excludes
        compute_commute=False,
    )


def test_same_city_differing_case_is_not_dropped(monkeypatch):
    """The exact production failure: canonical 'london' vs requested 'London'."""
    got = _validate(monkeypatch, cand_city="london", dest_city="London")
    assert got is not None, (
        "Bloomsbury was dropped as 'wrong city' while sitting in the destination's own "
        "city — the contamination guard fired on the contamination-free case."
    )
    assert got["name"] == "Bloomsbury"


def test_surrounding_whitespace_is_not_a_different_city(monkeypatch):
    assert _validate(monkeypatch, cand_city="london", dest_city="  London ") is not None


def test_a_genuinely_different_city_is_still_dropped(monkeypatch):
    """The guard must still do its job — this is what stops a Manchester suburb
    appearing in a London answer, and normalising case must not disable it."""
    got = _validate(monkeypatch, cand_city="manchester", dest_city="London",
                    name="Didsbury")
    assert got is None, "the contamination guard was weakened, not fixed"


def test_the_emitted_city_keeps_its_original_case(monkeypatch):
    """Only the COMPARISON is normalised. `city` is user-visible and `anchor` is fed to
    the geocoder, so neither may be silently lower-cased as a side effect of the fix."""
    got = _validate(monkeypatch, cand_city="London", dest_city="London")
    assert got["city"] == "London"


def test_a_missing_city_on_either_side_never_drops(monkeypatch):
    """Unchanged pre-existing behaviour: the guard only fires on two KNOWN cities."""
    assert _validate(monkeypatch, cand_city=None, dest_city="London") is not None
    assert _validate(monkeypatch, cand_city="london", dest_city=None) is not None


# ══════════════════════════════════════════════════════════════════════════
# The same un-normalised city string, five lines up: the name|city exclude key
# ══════════════════════════════════════════════════════════════════════════
# `_norm_excludes` lower-cases the whole exclude set, and the lookup key is built as
# f"{name.lower()}|{cand_city}" with cand_city NOT normalised. classify_place's curated
# tier happens to store lowercase, but the OSM and LLM tiers are not guaranteed to — and
# when they don't, the exclusion silently never fires. A guard that fails OPEN and says
# nothing is the defect this repo keeps finding, so it is pinned rather than argued about.

def _excluded(monkeypatch, *, cand_city, excludes, name="Bloomsbury"):
    monkeypatch.setattr(
        ram, "classify_place",
        lambda n: {"kind": "area", "slug": n.lower().replace(" ", "-"), "city": cand_city},
    )
    monkeypatch.setattr(ram, "is_destination", lambda place: False)
    monkeypatch.setattr(ram, "resolve_location", lambda n: (n.lower(), None))
    monkeypatch.setattr(ram, "geocode_address", lambda addr: {"lat": 51.52, "lng": -0.13})
    return ram._validate_one(
        {"name": name, "reason": "r"}, "UCL Bloomsbury",
        {"lat": 51.5246, "lng": -0.1340}, "London", True, 40,
        ram._norm_excludes(excludes), compute_commute=False,
    )


def test_name_city_exclusion_fires_regardless_of_the_classifier_s_casing(monkeypatch):
    """`exclude_slugs=['Bloomsbury|London']` must exclude Bloomsbury whatever case
    classify_place returned the city in."""
    assert _excluded(monkeypatch, cand_city="london",
                     excludes=["Bloomsbury|London"]) is None
    assert _excluded(monkeypatch, cand_city="London",
                     excludes=["Bloomsbury|London"]) is None, (
        "the name|city exclude key missed because classify_place returned 'London' while "
        "_norm_excludes had lower-cased the set — the exclusion failed open, silently."
    )


def test_the_name_city_exclusion_still_only_matches_its_own_city(monkeypatch):
    """Normalising case must not make the key match a DIFFERENT city."""
    assert _excluded(monkeypatch, cand_city="london",
                     excludes=["Bloomsbury|Manchester"]) is not None
