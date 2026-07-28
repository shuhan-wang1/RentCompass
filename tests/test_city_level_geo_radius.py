"""A proximity radius calibrated for a neighbourhood is the WRONG TEST for a city.

Observed in production on 2026-07-27, serving a real user who asked for London::

    🌐 [SEARCH] 抓取实时房源: areas=['London', ...]
       ✅ 实时房源 2 个 (areas=4, cached=False)
       [GEO] Verified 0/2 listings within 2 miles of requested area(s); rejected 2

Both listings were in London. They were rejected for being more than two miles from the
single point a geocoder returns for "London" — a city roughly thirty miles across. The
user's answer contained no listings at all.

`radius_miles` defaulted to 2.0 and was applied uniformly to every requested area, with
no notion of granularity. 2 miles is right for "Camden" and meaningless for "London".

A city's REAL containment test already exists and already runs: `on_demand._wrong_city`,
applied at scrape time by `_clean(rows, city)`. The disc is only a coarse backstop, so
the fix widens it to city scale for city-level areas rather than switching it off —
a `None` centre fails CLOSED in `filter_properties_by_radius`, so switching it off by
dropping the centroid would reject everything instead.
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

from core.geography import filter_properties_by_radius
from core.scraping.on_demand import is_city_level_area

# Geocoders land "London" around Charing Cross. Stratford is ~6 miles east of it and is
# unambiguously in London — the exact shape of the production rejection.
LONDON = (51.5074, -0.1278)
STRATFORD = (51.5416, -0.0042)
MANCHESTER = (53.4808, -2.2426)


def _row(area, geo):
    return {"_search_area": area, "geo_location": {"lat": geo[0], "lng": geo[1]},
            "Address": "1 Test Road"}


# ══════════════════════════════════════════════════════════════════════════
# The granularity predicate
# ══════════════════════════════════════════════════════════════════════════

def test_cities_are_recognised_as_city_level():
    for name in ("London", "london", "  LONDON ", "Manchester", "Birmingham"):
        assert is_city_level_area(name) is True, name


def test_city_slugs_are_recognised_too():
    assert is_city_level_area("newcastle-upon-tyne") is True
    assert is_city_level_area("stoke-on-trent") is True


def test_neighbourhoods_are_not_city_level():
    for name in ("Camden", "Bloomsbury", "King's Cross", "Shoreditch", "Didsbury"):
        assert is_city_level_area(name) is False, name


def test_junk_is_not_city_level():
    for name in (None, "", "   ", {}, 0):
        assert is_city_level_area(name) is False, repr(name)


# ══════════════════════════════════════════════════════════════════════════
# The filter
# ══════════════════════════════════════════════════════════════════════════

def test_a_london_listing_six_miles_out_survives_a_city_level_search():
    """The production failure. Fails on the old signature's behaviour: 2 miles rejects it."""
    kept, rejected = filter_properties_by_radius(
        [_row("London", STRATFORD)], {"London": LONDON}, 2.0,
        {"London": 20.0},
    )
    assert len(kept) == 1, (
        "a listing in London was rejected from a London search for being >2 miles from "
        f"the city's centroid; rejections={[r.get('_geo_rejection') for r in rejected]}"
    )
    assert not rejected


def test_the_same_listing_is_still_rejected_from_a_neighbourhood_search():
    """The fix must not become 'stop checking'. Camden is not city-level, so it keeps
    the 2-mile disc and Stratford is correctly too far."""
    kept, rejected = filter_properties_by_radius(
        [_row("Camden", STRATFORD)], {"Camden": (51.5390, -0.1426)}, 2.0, {},
    )
    assert not kept
    assert rejected[0]["_geo_rejection"] == "outside_radius"


def test_the_city_disc_is_still_a_backstop_not_an_exemption():
    """A Manchester listing tagged to a London search is 160+ miles out and must still
    be rejected even at city scale — the disc is widened, not removed."""
    kept, rejected = filter_properties_by_radius(
        [_row("London", MANCHESTER)], {"London": LONDON}, 2.0, {"London": 20.0},
    )
    assert not kept
    assert rejected[0]["_geo_rejection"] == "outside_radius"


def test_per_area_radii_are_independent():
    """One request, two granularities, one call: each area is judged on its own radius."""
    kept, rejected = filter_properties_by_radius(
        [_row("London", STRATFORD), _row("Camden", STRATFORD)],
        {"London": LONDON, "Camden": (51.5390, -0.1426)},
        2.0, {"London": 20.0},
    )
    assert [r["_search_area"] for r in kept] == ["London"]
    assert [r["_search_area"] for r in rejected] == ["Camden"]


# ══════════════════════════════════════════════════════════════════════════
# Fail-closed behaviour is unchanged — this is the property the override must not cost
# ══════════════════════════════════════════════════════════════════════════

def test_an_unresolved_area_still_fails_closed_even_with_an_override():
    kept, rejected = filter_properties_by_radius(
        [_row("London", STRATFORD)], {"London": None}, 2.0, {"London": 20.0},
    )
    assert not kept
    assert rejected[0]["_geo_rejection"] == "area_unresolved"


def test_a_listing_without_coordinates_still_fails_closed():
    kept, rejected = filter_properties_by_radius(
        [{"_search_area": "London", "Address": "somewhere"}],
        {"London": LONDON}, 2.0, {"London": 20.0},
    )
    assert not kept
    assert rejected[0]["_geo_rejection"] == "listing_unresolved"


def test_a_junk_override_falls_back_to_the_base_radius():
    """A bad env value must not silently disable the check."""
    for junk in ("abc", None, -5, float("inf"), 0):
        kept, rejected = filter_properties_by_radius(
            [_row("London", STRATFORD)], {"London": LONDON}, 2.0, {"London": junk},
        )
        assert not kept, f"override {junk!r} silently widened the radius"
        assert rejected[0]["_geo_rejection"] == "outside_radius"


def test_omitting_area_radii_entirely_is_the_old_behaviour():
    """Backward compatibility: existing 3-argument callers are unaffected."""
    kept, rejected = filter_properties_by_radius(
        [_row("London", STRATFORD)], {"London": LONDON}, 2.0,
    )
    assert not kept
    assert rejected[0]["_geo_rejection"] == "outside_radius"
