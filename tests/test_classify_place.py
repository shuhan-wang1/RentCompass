"""
Unit tests for on_demand.classify_place() — the destination/area classifier that
lets the search layer default universities & workplaces to commute-mode.

Three tiers are exercised offline (OSM/LLM mocked); two opt-in live OSM checks
run only with RUN_LIVE_OSM=1 so the default suite makes no network calls.

Run:  pytest tests/test_classify_place.py
"""

import os
import sys

# Pin the real source roots ahead of tests/ (which holds stale copies of `core`).
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


@pytest.fixture(autouse=True)
def _clear_memo():
    """Each test starts with an empty classify memo so tier spies see fresh calls."""
    on_demand._CLASSIFY_CACHE.clear()
    yield
    on_demand._CLASSIFY_CACHE.clear()


@pytest.fixture
def no_network(monkeypatch):
    """Make the OSM and LLM tiers explode if reached — proves tier-1 curated hits
    never touch the network."""
    def boom_osm(name):
        raise AssertionError(f"OSM tier must not run for curated name: {name!r}")

    def boom_llm(name):
        raise AssertionError(f"LLM tier must not run for curated name: {name!r}")

    monkeypatch.setattr(on_demand, "_osm_classify", boom_osm)
    monkeypatch.setattr(on_demand, "_llm_classify", boom_llm)


# --------------------------------------------------------------------------
# Return-shape contract
# --------------------------------------------------------------------------
_KEYS = {"kind", "slug", "city", "address", "source"}


def _assert_shape(result):
    assert set(result) == _KEYS, result
    assert result["kind"] in {"university", "workplace", "area", "unknown"}
    assert isinstance(result["slug"], str)
    assert result["city"] is None or isinstance(result["city"], str)
    assert result["address"] is None or isinstance(result["address"], str)
    assert result["source"] in {"curated", "osm", "llm", "fallback"}
    # DESTINATIONS carry an address; areas/unknowns never do.
    if on_demand.is_destination(result["kind"]):
        assert result["address"], "a destination must expose a geocodable address"
    else:
        assert result["address"] is None


# --------------------------------------------------------------------------
# Tier 1 — curated universities (instant, decisive, addressed, NO network)
# --------------------------------------------------------------------------
# NB: bare "Imperial" is intentionally NOT curated — it collides with the
# residential "Imperial Wharf" (Fulham) — so the curated form is "Imperial College".
@pytest.mark.parametrize("name", ["UCL", "LSE", "Imperial College", "KCL"])
def test_curated_universities_instant_with_address(name, no_network):
    r = on_demand.classify_place(name)
    _assert_shape(r)
    assert r["kind"] == "university"
    assert r["source"] == "curated"
    assert r["address"] and "London" in r["address"]


# --------------------------------------------------------------------------
# Tier 1 — education keyword -> university (still no network)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["University of Warwick", "Manchester Metropolitan University"])
def test_keyword_universities(name, no_network):
    r = on_demand.classify_place(name)
    _assert_shape(r)
    assert r["kind"] == "university"
    assert r["source"] == "curated"
    assert r["address"]  # geocodable (the name itself when no curated address)


# --------------------------------------------------------------------------
# Tier 1 — curated workplaces (employer names / office districts), NO network
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,addr_fragment", [
    ("Deloitte London", "New Street Square"),
    ("Google London office", "Pancras Square"),
    ("Barclays HQ", "Churchill Place"),
    ("City of London", "City of London"),
])
def test_curated_workplaces(name, addr_fragment, no_network):
    r = on_demand.classify_place(name)
    _assert_shape(r)
    assert r["kind"] == "workplace"
    assert r["source"] == "curated"
    assert addr_fragment in (r["address"] or "")
    assert r["city"] == "london"
    assert r["slug"] == "london"  # usable residential slug when no area given


def test_employer_keyword_without_curated_name_is_workplace(no_network):
    # "hospital" is an employer-ish keyword: an unknown hospital is still a workplace.
    r = on_demand.classify_place("St Thomas' Hospital")
    _assert_shape(r)
    assert r["kind"] == "workplace"
    assert r["address"]  # falls back to the (geocodable) name


# --------------------------------------------------------------------------
# Tier 1 — curated residential AREAS stay `area` (Canary Wharf must NOT flip)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,slug", [
    ("Camden", "camden"), ("Shoreditch", "shoreditch"),
    ("Manchester", "manchester"), ("Canary Wharf", "canary-wharf"),
])
def test_curated_areas_unchanged(name, slug, no_network):
    r = on_demand.classify_place(name)
    _assert_shape(r)
    assert r["kind"] == "area"
    assert r["slug"] == slug
    assert r["address"] is None


# --------------------------------------------------------------------------
# Tier 2 — OSM place type decides on a tier-1 miss
# --------------------------------------------------------------------------
def test_tier2_osm_place_type_is_area(monkeypatch):
    calls = {"llm": 0}
    monkeypatch.setattr(on_demand, "_osm_classify",
                        lambda name: ("area", "Jesmond, Newcastle, England"))
    monkeypatch.setattr(on_demand, "_llm_classify",
                        lambda name: calls.__setitem__("llm", calls["llm"] + 1))
    r = on_demand.classify_place("Jesmond")
    _assert_shape(r)
    assert r["kind"] == "area" and r["source"] == "osm"
    assert calls["llm"] == 0  # place type is decisive -> LLM never consulted


def test_tier2_osm_university_type(monkeypatch):
    # A tier-1 miss (no curated match, no edu/employer keyword, no city substring).
    monkeypatch.setattr(on_demand, "_osm_classify",
                        lambda name: ("university", "Durham University, Durham DH1"))
    monkeypatch.setattr(on_demand, "_llm_classify",
                        lambda name: pytest.fail("LLM must not run when OSM is decisive"))
    r = on_demand.classify_place("Qwerty Foo Bar")
    _assert_shape(r)
    assert r["kind"] == "university" and r["source"] == "osm"
    assert r["address"] == "Durham University, Durham DH1"


def test_tier2_osm_workplace_type(monkeypatch):
    monkeypatch.setattr(on_demand, "_osm_classify",
                        lambda name: ("workplace", "Acme Ltd, 1 High St, Leeds LS1"))
    r = on_demand.classify_place("Qwerty Baz Depot")
    _assert_shape(r)
    assert r["kind"] == "workplace" and r["source"] == "osm"
    assert "Leeds" in r["address"]


# --------------------------------------------------------------------------
# Cost bound — a plain residential name never invokes the LLM
# --------------------------------------------------------------------------
def test_plain_residential_name_never_calls_llm(monkeypatch):
    """OSM returns a place type -> area, and the (expensive) LLM tier is skipped."""
    llm_spy = {"n": 0}

    def spy_llm(name):
        llm_spy["n"] += 1
        return "workplace"  # would wrongly flip it — proving it must NOT be called

    monkeypatch.setattr(on_demand, "_osm_classify",
                        lambda name: ("area", "Selly Oak, Birmingham"))
    monkeypatch.setattr(on_demand, "_llm_classify", spy_llm)
    r = on_demand.classify_place("Selly Oak")
    assert r["kind"] == "area"
    assert llm_spy["n"] == 0


def test_unknown_when_osm_finds_nothing_no_llm(monkeypatch):
    """OSM finds nothing and there is no destination signal -> unknown, no LLM."""
    llm_spy = {"n": 0}
    monkeypatch.setattr(on_demand, "_osm_classify", lambda name: (None, None))
    monkeypatch.setattr(on_demand, "_llm_classify",
                        lambda name: llm_spy.__setitem__("n", llm_spy["n"] + 1))
    r = on_demand.classify_place("Zzqqxx Nowhere")
    _assert_shape(r)
    assert r["kind"] == "unknown" and r["source"] == "fallback"
    assert r["slug"] == "zzqqxx-nowhere" and r["city"] is None
    assert llm_spy["n"] == 0


# --------------------------------------------------------------------------
# Tier 3 — LLM only when OSM returned an ambiguous (non-place) type
# --------------------------------------------------------------------------
def test_tier3_llm_resolves_ambiguous_osm(monkeypatch):
    monkeypatch.setattr(on_demand, "_osm_classify",
                        lambda name: (None, "Some Big Employer, London EC2"))
    monkeypatch.setattr(on_demand, "_llm_classify", lambda name: "workplace")
    r = on_demand.classify_place("Some Ambiguous Employer")
    _assert_shape(r)
    assert r["kind"] == "workplace" and r["source"] == "llm"
    assert r["address"] == "Some Big Employer, London EC2"


def test_tier3_llm_unavailable_falls_back_to_area(monkeypatch):
    """OSM found a real GB place of ambiguous type but the LLM is down -> area (osm),
    never an exception."""
    monkeypatch.setattr(on_demand, "_osm_classify",
                        lambda name: (None, "A Landmark, Bristol"))
    monkeypatch.setattr(on_demand, "_llm_classify", lambda name: None)
    r = on_demand.classify_place("Some Landmark")
    _assert_shape(r)
    assert r["kind"] == "area" and r["source"] == "osm"


def test_llm_classify_never_raises(monkeypatch):
    """The real _llm_classify swallows any downstream error and returns None."""
    def broken_import(*a, **k):
        raise RuntimeError("LLM backend exploded")

    # call_ollama is imported lazily inside _llm_classify; force it to blow up.
    import core.llm_interface as li
    monkeypatch.setattr(li, "call_ollama", broken_import)
    assert on_demand._llm_classify("anything") is None


# --------------------------------------------------------------------------
# Memoization — a name is classified at most once per process
# --------------------------------------------------------------------------
def test_memoized_single_classification(monkeypatch):
    calls = {"n": 0}

    def counting_osm(name):
        calls["n"] += 1
        return ("area", "Somewhere, England")

    monkeypatch.setattr(on_demand, "_osm_classify", counting_osm)
    monkeypatch.setattr(on_demand, "_llm_classify",
                        lambda name: pytest.fail("place type is decisive"))
    a = on_demand.classify_place("Repeatville")
    b = on_demand.classify_place("Repeatville")
    assert a == b
    assert calls["n"] == 1  # second call served from memo


def test_memo_returns_a_copy(no_network):
    a = on_demand.classify_place("UCL")
    a["kind"] = "MUTATED"
    b = on_demand.classify_place("UCL")
    assert b["kind"] == "university"  # cache entry untouched by caller mutation


# --------------------------------------------------------------------------
# is_destination()
# --------------------------------------------------------------------------
def test_is_destination_all_kinds():
    assert on_demand.is_destination("university") is True
    assert on_demand.is_destination("workplace") is True
    assert on_demand.is_destination("area") is False
    assert on_demand.is_destination("unknown") is False
    # also accepts a full result dict
    assert on_demand.is_destination({"kind": "workplace"}) is True
    assert on_demand.is_destination({"kind": "area"}) is False
    assert on_demand.is_destination(None) is False


# --------------------------------------------------------------------------
# Backward compatibility — legacy keys/kinds preserved
# --------------------------------------------------------------------------
def test_backward_compat_keys_present(no_network):
    for name in ["UCL", "Camden", "Manchester"]:
        r = on_demand.classify_place(name)
        assert {"kind", "slug", "city"} <= set(r)
    assert on_demand.classify_place("UCL")["kind"] == "university"
    assert on_demand.classify_place("Camden")["kind"] == "area"


# --------------------------------------------------------------------------
# Live OSM (opt-in) — proves the real Nominatim type-read wiring end to end.
# --------------------------------------------------------------------------
@pytest.mark.skipif(os.getenv("RUN_LIVE_OSM") != "1",
                    reason="set RUN_LIVE_OSM=1 to hit the real Nominatim service")
def test_live_osm_university_is_destination():
    r = on_demand.classify_place("University of Bath")
    assert r["kind"] == "university" and r["address"]


@pytest.mark.skipif(os.getenv("RUN_LIVE_OSM") != "1",
                    reason="set RUN_LIVE_OSM=1 to hit the real Nominatim service")
def test_live_osm_suburb_is_area():
    # A non-curated residential suburb should resolve to `area` via OSM place type.
    r = on_demand.classify_place("Headingley")
    assert r["kind"] == "area"
