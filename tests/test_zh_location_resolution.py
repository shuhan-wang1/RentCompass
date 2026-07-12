"""
Chinese (zh) location-resolution for the customer criteria-form path.

Reproduces and locks the fix for the bug where a Chinese area typed into the
search form ("曼彻斯特大学") reached on_demand._match_location, whose _norm() strips
ALL CJK -> empty string -> empty OnTheMarket slug -> zero rows. The fix adds a
tier-0 zh->canonical-English alias layer applied at the top of _match_location and
classify_place's normalization, first-class non-London university curated entries,
and a CJK long-tail OSM fallback for uncurated Chinese names.

No live network: the OSM tier is monkeypatched. Curated hits additionally prove
they never touch the network (boom_osm / boom_llm).

Run:  pytest tests/test_zh_location_resolution.py
"""

import os
import sys

# --- Pin the real source roots ahead of tests/ (which holds stale shadow copies of
# `core`/`rag` that would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest

import core.scraping.onthemarket as om_mod
from core.scraping import on_demand


@pytest.fixture(autouse=True)
def _clear_memo():
    """Each test starts with an empty classify memo so tier spies see fresh calls."""
    on_demand._CLASSIFY_CACHE.clear()
    yield
    on_demand._CLASSIFY_CACHE.clear()


@pytest.fixture
def no_network(monkeypatch):
    """Make the OSM and LLM tiers explode if reached — proves curated tier-1 hits
    (including aliased Chinese names) never touch the network."""
    def boom_osm(name):
        raise AssertionError(f"OSM tier must not run for curated name: {name!r}")

    def boom_llm(name):
        raise AssertionError(f"LLM tier must not run for curated name: {name!r}")

    monkeypatch.setattr(on_demand, "_osm_classify", boom_osm)
    monkeypatch.setattr(on_demand, "_llm_classify", boom_llm)


# --------------------------------------------------------------------------
# 1) The reported bug: a Chinese UNIVERSITY resolves + is a commute destination.
# --------------------------------------------------------------------------
def test_manchester_university_zh_resolves_and_is_destination(no_network):
    # resolve_location (the scrape hot path) must map to the city slug, no network.
    assert on_demand.resolve_location("曼彻斯特大学") == ("manchester", "manchester")
    r = on_demand.classify_place("曼彻斯特大学")
    assert r["kind"] == "university"
    assert on_demand.is_destination(r) is True
    assert r["slug"] == "manchester" and r["city"] == "manchester"
    assert r["source"] == "curated"
    assert r["address"] == "Oxford Road, Manchester M13 9PL"


# --------------------------------------------------------------------------
# 2) Chinese CITY aliases (bare + colloquial) map to the city slug (area, no dest).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("zh", ["曼彻斯特", "曼城"])
def test_manchester_city_zh_is_area(zh, no_network):
    assert on_demand.resolve_location(zh) == ("manchester", "manchester")
    r = on_demand.classify_place(zh)
    assert r["kind"] == "area"
    assert on_demand.is_destination(r) is False
    assert r["slug"] == "manchester" and r["city"] == "manchester"


@pytest.mark.parametrize("zh,slug,city", [
    ("伦敦", "london", "london"),
    ("爱丁堡", "edinburgh", "edinburgh"),
    ("考文垂", "coventry", "coventry"),
    ("纽卡斯尔", "newcastle-upon-tyne", "newcastle"),
])
def test_city_aliases_resolve(zh, slug, city, no_network):
    assert on_demand.resolve_location(zh) == (slug, city)


# --------------------------------------------------------------------------
# 3) Warwick — the interesting case: campus city is COVENTRY (no substring match).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["华威大学", "warwick university", "University of Warwick"])
def test_warwick_resolves_to_coventry_destination(name, no_network):
    assert on_demand.resolve_location(name) == ("coventry", "coventry")
    r = on_demand.classify_place(name)
    assert r["kind"] == "university" and on_demand.is_destination(r) is True
    assert r["slug"] == "coventry" and r["city"] == "coventry"
    assert r["address"] == "University of Warwick, Coventry CV4 7AL"


def test_bare_warwick_is_not_coventry():
    # Bare "warwick"/"华威" is intentionally NOT curated ("Warwick Avenue" London W9,
    # Warwickshire town...). It must fall through to an honest slug, never Coventry.
    slug, city = on_demand.resolve_location("Warwick")
    assert (slug, city) == ("warwick", None)


# --------------------------------------------------------------------------
# 4) MMU vs UoM — the longest-first alias must NOT collapse 城市大学 into 大学.
# --------------------------------------------------------------------------
def test_manchester_metropolitan_is_not_uom(no_network):
    mmu = on_demand.classify_place("曼彻斯特城市大学")
    uom = on_demand.classify_place("曼彻斯特大学")
    assert mmu["kind"] == uom["kind"] == "university"
    assert mmu["slug"] == uom["slug"] == "manchester"
    # Distinct campuses -> distinct addresses (proves no collapse).
    assert mmu["address"] == "All Saints, Oxford Road, Manchester M15 6BH"
    assert uom["address"] == "Oxford Road, Manchester M13 9PL"
    assert mmu["address"] != uom["address"]


@pytest.mark.parametrize("name", ["MMU", "manchester metropolitan", "manchester metropolitan university"])
def test_mmu_english_forms(name, no_network):
    r = on_demand.classify_place(name)
    assert r["kind"] == "university" and r["slug"] == "manchester"
    assert "All Saints" in r["address"]


# --------------------------------------------------------------------------
# 5) A London university via a Chinese abbreviation (LSE) -> holborn/london dest.
# --------------------------------------------------------------------------
def test_lse_zh_abbreviation(no_network):
    assert on_demand.resolve_location("伦敦政经") == ("holborn", "london")
    r = on_demand.classify_place("伦敦政经")
    assert r["kind"] == "university" and on_demand.is_destination(r) is True
    assert r["slug"] == "holborn" and r["city"] == "london"
    assert "Houghton Street" in r["address"]


@pytest.mark.parametrize("zh,slug", [
    ("帝国理工", "south-kensington"),
    ("伦敦大学学院", "bloomsbury"),
    ("亚非学院", "bloomsbury"),
])
def test_other_london_uni_zh_aliases(zh, slug, no_network):
    r = on_demand.classify_place(zh)
    assert r["kind"] == "university" and r["slug"] == slug and r["city"] == "london"


# --------------------------------------------------------------------------
# 6) English regressions FROZEN — the alias layer is a no-op for ASCII input.
# --------------------------------------------------------------------------
def test_english_regressions_frozen(no_network):
    # "oxford street" (a London street) must NOT become the city of Oxford.
    assert on_demand.resolve_location("oxford street") == ("london", "london")
    assert on_demand.resolve_location("UCL") == ("bloomsbury", "london")
    assert on_demand.resolve_location("University of Manchester") == ("manchester", "manchester")
    assert on_demand.resolve_location("Manchester") == ("manchester", "manchester")
    assert on_demand.classify_place("UCL")["kind"] == "university"
    assert on_demand.classify_place("Camden")["kind"] == "area"
    assert on_demand.classify_place("Manchester")["kind"] == "area"


def test_glued_typo_fallback_still_works():
    # The step-5 last-resort substring fallback must survive the step-3 refinement.
    assert on_demand.resolve_location("axocamden") == ("camden", "london")
    assert on_demand.resolve_location("axomanchester") == ("manchester", "manchester")


def test_unknown_english_is_slugified():
    assert on_demand.resolve_location("Narnia") == ("narnia", None)


# --------------------------------------------------------------------------
# 7) Collision safety — short acronyms ("mmu") must match at word boundaries only,
#    so common domain words containing them are NOT hijacked to a university.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["community", "Community College", "the community centre"])
def test_community_not_misclassified_as_mmu(name, monkeypatch):
    # "mmu" ⊂ "community": with plain-substring matching this would resolve to MMU
    # (manchester). It must instead fall through (OSM never confirms a university).
    monkeypatch.setattr(on_demand, "_osm_classify", lambda n: (None, None))
    slug, city = on_demand.resolve_location(name)
    assert city != "manchester"
    r = on_demand.classify_place(name)
    assert r["kind"] != "university"
    assert on_demand.is_destination(r) is False


# --------------------------------------------------------------------------
# 8) CJK long-tail fallback — an UNCURATED Chinese name resolves via the OSM tier
#    (Nominatim, accept-language=en) mapped back through CITY_SLUGS.
# --------------------------------------------------------------------------
def test_uncurated_cjk_area_via_osm(monkeypatch):
    # A CJK suburb the alias table doesn't cover; OSM returns an English display.
    monkeypatch.setattr(
        on_demand, "_osm_classify",
        lambda n: ("area", "Jesmond, Newcastle upon Tyne, England"),
    )
    monkeypatch.setattr(on_demand, "_llm_classify",
                        lambda n: pytest.fail("place type is decisive, no LLM"))
    assert on_demand.resolve_location("杰斯蒙德") == ("newcastle-upon-tyne", "newcastle")
    r = on_demand.classify_place("杰斯蒙德")
    assert r["kind"] == "area" and r["source"] == "osm"
    assert r["slug"] == "newcastle-upon-tyne" and r["city"] == "newcastle"


def test_uncurated_cjk_university_via_osm(monkeypatch):
    monkeypatch.setattr(
        on_demand, "_osm_classify",
        lambda n: ("university", "University of Strathclyde, Glasgow, Scotland"),
    )
    r = on_demand.classify_place("斯特拉思克莱德大学")
    assert r["kind"] == "university" and on_demand.is_destination(r) is True
    assert r["slug"] == "glasgow" and r["city"] == "glasgow"
    assert r["source"] == "osm"
    assert "Strathclyde" in r["address"]
    # resolve_location routes the raw CJK through the same memoized OSM tier.
    assert on_demand.resolve_location("斯特拉思克莱德大学") == ("glasgow", "glasgow")


def test_uncurated_cjk_osm_failure_is_honest_empty(monkeypatch):
    # OSM finds nothing -> empty slug + honest no-results, never a wrong-city guess.
    monkeypatch.setattr(on_demand, "_osm_classify", lambda n: (None, None))
    assert on_demand.resolve_location("某某小区") == ("", None)
    r = on_demand.classify_place("某某小区")
    assert r["kind"] == "unknown" and r["slug"] == "" and r["city"] is None


def test_cjk_osm_call_is_memoized(monkeypatch):
    calls = {"n": 0}

    def counting_osm(name):
        calls["n"] += 1
        return ("area", "Somewhere, Leeds, England")

    monkeypatch.setattr(on_demand, "_osm_classify", counting_osm)
    # resolve_location + a repeat classify_place on the same uncurated CJK name
    # must consult OSM at most once (memoized).
    on_demand.resolve_location("某地方")
    on_demand.classify_place("某地方")
    on_demand.resolve_location("某地方")
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# 9) Empty / garbage input still returns an empty slug (no crash, no network).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", "!!!"])
def test_empty_and_garbage_input(bad, no_network):
    assert on_demand.resolve_location(bad) == ("", None)


# --------------------------------------------------------------------------
# 10) End-to-end scrape path: get_listings("曼彻斯特大学") scrapes the manchester
#     slug (the form/search_direct path calls resolve_location directly).
# --------------------------------------------------------------------------
def test_get_listings_zh_area_scrapes_city_slug(tmp_path, monkeypatch, no_network):
    monkeypatch.setattr(on_demand, "_CACHE", on_demand.ListingCache(tmp_path / "c.sqlite3"))
    monkeypatch.setattr(on_demand, "ALLOW_DEMO_FALLBACK", False)
    seen = {}

    def fake_scrape(slug, radius, min_price, max_price, limit, min_beds, max_beds):
        seen["slug"] = slug
        return [{"Address": "1 Test St, Manchester, M1",
                 "URL": "https://www.onthemarket.com/details/1/",
                 "Price": "£1000 pcm"}]

    monkeypatch.setattr(om_mod, "find_rich_onthemarket", fake_scrape)
    res = on_demand.get_listings("曼彻斯特大学", 1, 1, 500, 1200)
    assert seen["slug"] == "manchester"          # NOT an empty slug
    assert res["meta"]["source"] == "scraped"
    assert res["meta"]["requested_city"] == "manchester"
    assert res["rows"] and "Manchester" in res["rows"][0]["Address"]
