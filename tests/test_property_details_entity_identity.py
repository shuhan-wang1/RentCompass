"""get_property_details must not answer about a DIFFERENT property.

HANDOFF §0 defect class, instance N+1: a resolution decision was computed and then
discarded. `find_property_by_name_or_address` picked `matches[0]` and the tool
returned it as "the property" with `found: True` and a price, while NOTHING
compared the requested entity to the returned one — so no caller could tell.
Same shape as `route_source` (`tfl` vs `estimate`) having zero consumers
repo-wide: a haversine guess and a real journey plan arrived in the same field.

Measured in the round of record `.runtime/round-8793c0b-internal-2026-07-25/`:

    requested                                returned
    "Spring Mews SE11 5AL"               ->  "Raleigh Mews, Angel, N1"
    "Vega Building E15 2GN"              ->  "Plimsoll Building, N1C"
    "Chapter Kings Cross, 30 Pentonville ->  "Pentonville Road", £1,300 pcm,
     Road, London N1 9HJ" @ £400/week         1 bed Flat

and case F14 then answered, verbatim from that round's `raw_runs.jsonl`:

    "The official monthly price for this property is **£1,300 pcm** (per calendar
     month). Note that this listing is actually a **1-bed flat**, not a studio,
     located on Pentonville Road, London, a short walk from King's Cross Station."

The requested listing was Chapter Kings Cross at £400/week; its true monthly
figure is £1,733.33. £1,300 pcm is another property's rent. Case C9 spent both
of its tool calls noticing the substitution and never computed the commutes.

Two mechanisms, both tested here:
  (a) `match_score >= 2` counted ANY two tokens, so generic words ("mews",
      "building", "road", "london") were enough to declare a match;
  (b) a "looser" single-keyword retry compared by bare substring, so the token
      "mews" matched every mews in the cache.

The fix is a SOURCE guard, not a returned confidence field: nothing in this repo
reads such a field (grep for `formatted_details` / `room_type_analysis` /
`total_matches` / `other_matches` finds zero consumers outside the tool, and
`found` is never read either), so a row that fails identity is never emitted.
"""

import json

import pytest

from core.scraping import on_demand
from core.tools import get_property_details as gpd


def _seed_cache(monkeypatch, tmp_path, rows):
    cache = on_demand.ListingCache(tmp_path / "listings.sqlite3")
    cache.set(on_demand._query_key("london"), rows)
    monkeypatch.setattr(on_demand, "_CACHE", cache)
    return cache


def _row(address, price, room_type="Studio", url=None, **extra):
    row = {
        "Address": address,
        "URL": url or f"https://www.onthemarket.com/details/{abs(hash(address)) % 99999}/",
        "Price": price,
        "Room_Type_Category": room_type,
        "Description": f"A {room_type} at {address}.",
        "Detailed_Amenities": "Wifi",
        "Guest_Policy": "",
        "Payment_Rules": "",
        "Excluded_Features": "",
        "Available From": "2026-09-01",
        "geo_location": "51.52, -0.12",
    }
    row.update(extra)
    return row


def _blob(result):
    """Everything the model would see, as one string."""
    return json.dumps(result, ensure_ascii=False, default=str)


# ── The three production substitutions ──────────────────────────────────────────
# Each of these FAILS on the pre-fix matcher, which returned found=True plus the
# wrong listing's price.

# F14's real cache shape: the requested listing (Chapter Kings Cross) is NOT
# cached; an unrelated Pentonville Road flat is.
_PENTONVILLE_OTHER = _row(
    "Pentonville Road, London N1 9JP", "£1,300 pcm", "1 bed Flat",
    url="https://www.onthemarket.com/details/pentonville-road-flat/")
_RALEIGH_MEWS = _row("Raleigh Mews, Angel, London N1 8QX", "£2,100 pcm", "1 bed Flat")
_PLIMSOLL_BUILDING = _row("Plimsoll Building, N1C 4BP", "£2,750 pcm", "2 bed Flat")


def test_f14_chapter_kings_cross_does_not_resolve_to_pentonville_road(
        monkeypatch, tmp_path):
    """THE regression. Pinned to the F14 production string.

    Pre-fix: found=True, property.price="£1,300 pcm", address "Pentonville Road"
    — and the answer asserted "The official monthly price for this property is
    **£1,300 pcm**" for a listing advertised at £400/week (£1,733.33 pcm).
    """
    _seed_cache(monkeypatch, tmp_path, [_PENTONVILLE_OTHER])

    res = gpd.get_property_details_impl(
        property_name="Chapter Kings Cross",
        property_address="30 Pentonville Road, London N1 9HJ")

    assert res["found"] is False, (
        "returned a property for a request the cache cannot satisfy: "
        f"{res.get('property', {}).get('address')}")
    assert res["match"]["verdict"] == "no_match"
    assert res["match"]["resolved"] is None
    assert "Chapter Kings Cross" in res["match"]["requested"]

    blob = _blob(res)
    # The wrong property's rent must not be anywhere in the payload — that string
    # is what the model lifted into the F14 answer.
    assert "1,300" not in blob and "1300" not in blob
    assert "1 bed Flat" not in blob
    # And the refusal must say WHY, naming both entities.
    reasons = " ".join(res["match"]["reasons"])
    assert "name_mismatch" in reasons or "postcode_conflict" in reasons


def test_f14_resolves_correctly_when_the_real_listing_is_cached(
        monkeypatch, tmp_path):
    """The guard must not simply refuse everything: with Chapter Kings Cross in
    the cache, F14's request resolves to it and returns ITS weekly price."""
    chapter = _row("Chapter Kings Cross, 30 Pentonville Road, London N1 9HJ",
                   "£400 per week", "Studio",
                   url="https://www.onthemarket.com/details/chapter-kings-cross/")
    _seed_cache(monkeypatch, tmp_path, [chapter, _PENTONVILLE_OTHER])

    res = gpd.get_property_details_impl(
        property_name="Chapter Kings Cross",
        property_address="30 Pentonville Road, London N1 9HJ")

    assert res["found"] is True
    assert res["match"]["verdict"] == "resolved"
    assert res["match"]["resolved"].startswith("Chapter Kings Cross")
    assert res["property"]["price"] == "£400 per week"
    assert "postcode_unit" in res["match"]["corroborated_by"]
    # The other Pentonville Road flat's rent is not in the payload at all.
    assert "1,300" not in _blob(res)


def test_spring_mews_does_not_resolve_to_raleigh_mews(monkeypatch, tmp_path):
    """C9, listing 1. Pre-fix the shared generic token "mews" was the whole match."""
    _seed_cache(monkeypatch, tmp_path, [_RALEIGH_MEWS])
    res = gpd.get_property_details_impl(property_address="Spring Mews SE11 5AL")
    assert res["found"] is False
    assert res["match"]["verdict"] == "no_match"
    assert "Raleigh" not in _blob(res["match"])
    assert "2,100" not in _blob(res)


def test_vega_building_does_not_resolve_to_plimsoll_building(monkeypatch, tmp_path):
    """C9, listing 2. Pre-fix the shared generic token "building" was the match."""
    _seed_cache(monkeypatch, tmp_path, [_PLIMSOLL_BUILDING])
    res = gpd.get_property_details_impl(property_address="Vega Building E15 2GN")
    assert res["found"] is False
    assert res["match"]["verdict"] == "no_match"
    assert "2,750" not in _blob(res)


def test_all_three_c9_lookups_refuse_against_a_realistic_mixed_cache(
        monkeypatch, tmp_path):
    """The three C9/F14 requests against one cache holding all the decoys."""
    _seed_cache(monkeypatch, tmp_path,
                [_RALEIGH_MEWS, _PLIMSOLL_BUILDING, _PENTONVILLE_OTHER,
                 _row("Tinworth Street, Vauxhall, London SE11 5EG", "£1,800 pcm")])
    for query in ("Spring Mews, 10 Tinworth Street, SE11 5AL",
                  "Vega Building, 6 Cook Road, E15 2GN",
                  "Chapter Kings Cross, 30 Pentonville Road, London N1 9HJ"):
        res = gpd.get_property_details_impl(property_address=query)
        assert res["found"] is False, f"{query} -> {res.get('property')}"
        assert res["match"]["resolved"] is None
        # Near-misses may be offered as "did you mean", but never with a price.
        for suggestion in res.get("did_you_mean", []):
            assert set(suggestion) == {"address", "url", "why_rejected"}
            assert "£" not in suggestion["address"]


# ── The source guard is in the matcher, not in a flag ───────────────────────────

def test_matcher_itself_refuses_to_emit_a_non_matching_row(monkeypatch, tmp_path):
    """A guard, not a promise: the matcher never returns the wrong row, so a
    future caller cannot reintroduce the defect by ignoring a confidence field."""
    import pandas as pd
    df = pd.DataFrame([_RALEIGH_MEWS, _PLIMSOLL_BUILDING])
    assert gpd.find_property_by_name_or_address("Spring Mews SE11 5AL", df) == []
    assert gpd.find_property_by_name_or_address("Vega Building E15 2GN", df) == []


def test_single_generic_keyword_retry_is_gone():
    """Mechanism (b): the old "looser search" retried each keyword on its own with
    a bare substring test, which is how "mews" reached Raleigh Mews. It must not
    come back."""
    import inspect
    src = inspect.getsource(gpd)
    assert "query_normalized in address_normalized" not in src
    assert "match_score >= 2" not in src


def test_generic_tokens_alone_never_identify_a_listing():
    """Unit-level: the tokens that produced all three substitutions carry no identity."""
    for token in ("mews", "building", "road", "street", "london", "house", "flat"):
        assert gpd._is_placeholder(token), token
    # ...while the development / street names do.
    for token in ("spring", "vega", "chapter", "raleigh", "plimsoll", "pentonville"):
        assert not gpd._is_placeholder(token), token


# ── The opposite failure: legitimate variation must still match ─────────────────

_SPRING_MEWS = _row("Spring Mews, 10 Tinworth Street, Vauxhall, London SE11 5AL",
                    "£1,950 pcm", "Studio",
                    url="https://www.onthemarket.com/details/spring-mews/")


@pytest.mark.parametrize("query,why", [
    ("Spring Mews, 10 Tinworth Street, Vauxhall, London SE11 5AL", "exact"),
    ("spring mews 10 tinworth street vauxhall london se11 5al", "lowercase"),
    ("SPRING MEWS, SE11 5AL", "uppercase + punctuation dropped"),
    ("Spring Mews", "name only, no area, no postcode"),
    ("Spring Mews SE11 5AL", "name + postcode, street omitted"),
    ("Spring Mews, Vauxhall", "area suffix the row's street part omits"),
    ("10 Tinworth St, SE11 5AL", "'St' for 'Street' — asked by street, not name"),
    ("10 Tinworth Street", "street + number, development name omitted"),
    ("Tinworth Street, Vauxhall", "street + area, no number"),
    ("Flat 4, Spring Mews, SE11 5AL", "a flat number the row does not carry"),
    ("tell me about the Spring Mews listing", "a sentence, not an address"),
])
def test_legitimate_variation_still_resolves(monkeypatch, tmp_path, query, why):
    _seed_cache(monkeypatch, tmp_path, [_SPRING_MEWS])
    res = gpd.get_property_details_impl(property_address=query)
    assert res["found"] is True, f"false refusal ({why}): {query}"
    assert res["match"]["verdict"] == "resolved"
    assert res["match"]["resolved"].startswith("Spring Mews")
    assert res["property"]["price"] == "£1,950 pcm"


def test_url_hit_is_an_exact_identity(monkeypatch, tmp_path):
    _seed_cache(monkeypatch, tmp_path, [_SPRING_MEWS, _RALEIGH_MEWS])
    res = gpd.get_property_details_impl(
        property_url="https://www.onthemarket.com/details/spring-mews/")
    assert res["found"] is True
    assert res["match"]["verdict"] == "exact_url"
    assert res["property"]["url"] == _SPRING_MEWS["URL"]


# ── Where the line is drawn: the three vetoes, each on its own ──────────────────

def test_postcode_conflict_vetoes_even_when_the_street_matches(
        monkeypatch, tmp_path):
    """The F14 shape at its hardest: same street name, different unit postcode
    (N1 9HJ vs N1 9JP) — a different listing."""
    _seed_cache(monkeypatch, tmp_path, [_PENTONVILLE_OTHER])
    res = gpd.get_property_details_impl(
        property_address="Pentonville Road, London N1 9HJ")
    assert res["found"] is False
    assert any("postcode_conflict" in r for r in res["match"]["reasons"])


def test_outward_postcode_conflict_vetoes(monkeypatch, tmp_path):
    """N1 and N1C are different districts."""
    _seed_cache(monkeypatch, tmp_path, [_PLIMSOLL_BUILDING])
    res = gpd.get_property_details_impl(property_address="Plimsoll Building, N1")
    assert res["found"] is False
    assert any("outward_postcode_conflict" in r for r in res["match"]["reasons"])


def test_house_number_conflict_vetoes(monkeypatch, tmp_path):
    _seed_cache(monkeypatch, tmp_path,
                [_row("88 Pentonville Road, London N1 9HJ", "£1,450 pcm")])
    res = gpd.get_property_details_impl(
        property_address="30 Pentonville Road, London N1 9HJ")
    assert res["found"] is False
    assert any("house_number_conflict" in r for r in res["match"]["reasons"])


def test_missing_flat_number_is_not_a_conflict(monkeypatch, tmp_path):
    """A single flat number against a building RANGE is not a disagreement — the
    conservative side of the number rule."""
    _seed_cache(monkeypatch, tmp_path,
                [_row("19-29 Woburn Place, Bloomsbury, London WC1H 0JR", "£2,300 pcm")])
    res = gpd.get_property_details_impl(property_address="Flat 5, Woburn Place")
    assert res["found"] is True
    assert res["property"]["price"] == "£2,300 pcm"


def test_a_request_with_no_name_and_no_postcode_is_refused(monkeypatch, tmp_path):
    """Nothing to resolve on: picking a row would be picking at random."""
    _seed_cache(monkeypatch, tmp_path, [_SPRING_MEWS, _RALEIGH_MEWS])
    res = gpd.get_property_details_impl(property_address="the flat")
    assert res["found"] is False
    assert any("request_not_resolvable" in r for r in res["match"]["reasons"])


def test_two_equally_good_listings_are_ambiguous_not_silently_first(
        monkeypatch, tmp_path):
    """Silently taking matches[0] out of several is the same defect. Refuse."""
    _seed_cache(monkeypatch, tmp_path, [
        _row("Spring Mews, Vauxhall, London", "£1,950 pcm",
             url="https://www.onthemarket.com/details/spring-mews-a/"),
        _row("Spring Mews, Vauxhall, London", "£2,400 pcm",
             url="https://www.onthemarket.com/details/spring-mews-b/"),
    ])
    res = gpd.get_property_details_impl(property_address="Spring Mews")
    assert res["found"] is False
    assert res["match"]["verdict"] == "ambiguous"
    blob = _blob(res)
    assert "1,950" not in blob and "2,400" not in blob
    assert len(res["did_you_mean"]) == 2


def test_uncached_url_is_not_fuzzy_substituted(monkeypatch, tmp_path):
    """A front-end "Ask AI" URL the cache has never seen must not come back as
    some other listing."""
    _seed_cache(monkeypatch, tmp_path, [_SPRING_MEWS, _RALEIGH_MEWS])
    res = gpd.get_property_details_impl(
        property_url="https://www.onthemarket.com/details/never-cached-999/")
    assert res["found"] is False
    assert res["match"]["verdict"] == "url_not_in_cache"
    assert "1,950" not in _blob(res) and "2,100" not in _blob(res)


# ── The resolution is surfaced, not just used ──────────────────────────────────

def test_resolved_result_carries_requested_versus_resolved(monkeypatch, tmp_path):
    """`match` is the field a caller can refuse on; it is also rendered into
    `formatted_details`, which IS read (it goes into the model's context), so the
    verdict cannot be dropped on the floor the way `route_source` was."""
    _seed_cache(monkeypatch, tmp_path, [_SPRING_MEWS])
    res = gpd.get_property_details_impl(property_address="Spring Mews SE11 5AL")
    match = res["match"]
    assert set(match) >= {"verdict", "requested", "resolved", "resolved_postcode",
                          "corroborated_by", "reasons"}
    assert match["requested_postcode"] == "se115al"
    assert match["resolved_postcode"] == "se115al"
    assert match["reasons"] == []
    assert "Identity checked" in res["formatted_details"]
    assert match["resolved"] in res["formatted_details"]


def test_refusal_payload_contains_no_price_for_any_listing(monkeypatch, tmp_path):
    """The structural property that makes the fix hold: on a refusal there is no
    £ figure in the payload, so there is nothing for the model to assert."""
    _seed_cache(monkeypatch, tmp_path,
                [_RALEIGH_MEWS, _PLIMSOLL_BUILDING, _PENTONVILLE_OTHER])
    res = gpd.get_property_details_impl(property_address="Spring Mews SE11 5AL")
    assert res["found"] is False
    assert "£" not in _blob(res)
    assert "property" not in res and "room_type_analysis" not in res
