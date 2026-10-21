"""Offline reproductions for formal-live OTM refusals and bounded inventory."""
import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

from core.scraping import on_demand, onthemarket as om, otm_access as access


@pytest.fixture(autouse=True)
def isolated_source(tmp_path, monkeypatch):
    monkeypatch.setenv("OTM_ACCESS_STATE_PATH", str(tmp_path / "access.sqlite3"))
    monkeypatch.setattr(access, "ROBOTS_VERIFIED_AT", datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(access, "MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(om, "_DESC_CACHE", om._DescCache(tmp_path / "descriptions.sqlite3"))
    monkeypatch.setattr(om, "_DESC_THREAD_LOCAL", threading.local())
    monkeypatch.setattr(om, "_rate_limited_detail_wait", lambda **kwargs: True)
    monkeypatch.setattr(on_demand, "_CACHE", on_demand.ListingCache(tmp_path / "listings.sqlite3"))
    monkeypatch.setattr(on_demand, "ALLOW_DEMO_FALLBACK", False)


def page(listings=None, *, modern=True, prop=None):
    state = {"results": {"list": listings or []}}
    if prop is not None:
        state = {"property": prop}
    props = {"pageProps": {"initialReduxState": state}} if modern else {"initialReduxState": state}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": props}) + '</script>'


def raw_listing(n=1, price="£1,200 pcm", beds=2):
    return {"address": f"{n} Example Road, London", "details-url": f"/details/{n}/",
            "price": price, "bedrooms": beds, "humanised-property-type": "Flat",
            "property-title": f"{beds} bedroom flat", "location": {"lat": 51.54, "lon": -0.14}}


class Response:
    def __init__(self, code=200, text=None, headers=None):
        self.status_code, self.text, self.headers = code, text or page(), headers or {}


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.parametrize("modern", [True, False])
def test_search_and_details_support_both_next_roots(modern):
    assert len(om._extract_listings(page([raw_listing()], modern=modern))) == 1
    data = om._next_data(page(modern=modern, prop={
        "description": "<p>Two bedroom flat with lift.</p>",
        "lettingDetails": {"items": ["Availability date: 1 Nov 2026"]}}))
    assert "with lift" in om._find_description(data)
    assert om._extract_available_from(data) == "2026-11-01"


def test_program_user_agent_is_stable_and_not_browser(monkeypatch):
    monkeypatch.delenv("OTM_USER_AGENT", raising=False)
    assert om._new_session().headers["User-Agent"] == "RentCompassBot/1.0"
    monkeypatch.setenv("OTM_USER_AGENT", "RentCompassBot/1.1 (+https://example.test/contact)")
    assert om._new_session().headers["User-Agent"].startswith("RentCompassBot/1.1")
    monkeypatch.setenv("OTM_USER_AGENT", "Mozilla/5.0 Chrome/120.0")
    with pytest.raises(access.OTMSourceError):
        om._new_session()


@pytest.mark.parametrize("query", ["max-price=2000", "min-price=100", "max-bedrooms=2",
                                  "min-bedrooms=1", "radius=1", "prop-types=flats", "sort-field=price"])
def test_robots_forbidden_facets_never_get_dispatched(query):
    with pytest.raises(access.OTMSourceError, match="robots_query_not_allowed"):
        access.validate_url("https://www.onthemarket.com/to-rent/property/camden/?" + query)


@pytest.mark.parametrize("url", ["https://other.test/to-rent/property/camden/",
                                 "https://www.onthemarket.com/to-rent/property/camden/flat/",
                                 "https://www.onthemarket.com/details/1/?max-price=2",
                                 "https://www.onthemarket.com/details/1/#private"])
def test_unknown_origin_or_path_fails_closed(url):
    with pytest.raises(access.OTMSourceError):
        access.validate_url(url)


def test_expired_policy_refreshes_robots_once_and_caches_new_rules(monkeypatch):
    monkeypatch.setattr(access, "ROBOTS_VERIFIED_AT", "2020-01-01T00:00:00+00:00")
    session = Session([Response(text="User-agent: *\nDisallow: *max-price=\nDisallow: /details/\n"),
                       Response(), Response()])
    url = "https://www.onthemarket.com/to-rent/property/camden/"
    access.get(session, url)
    access.get(session, url)
    with pytest.raises(access.OTMSourceError, match="robots_disallowed"):
        access.get(session, "https://www.onthemarket.com/details/1/")
    assert len(session.calls) == 3
    assert session.calls[0][0] == "https://www.onthemarket.com/robots.txt"


def test_expired_policy_never_uses_failed_or_html_robots(monkeypatch):
    monkeypatch.setattr(access, "ROBOTS_VERIFIED_AT", "2020-01-01T00:00:00+00:00")
    session = Session([Response(text="<html>Sorry robots is unavailable</html>")])
    with pytest.raises(access.OTMSourceError, match="robots_invalid_response"):
        access.get(session, "https://www.onthemarket.com/to-rent/property/camden/")
    with pytest.raises(access.OTMSourceError, match="robots_invalid_response"):
        access.get(session, "https://www.onthemarket.com/to-rent/property/islington/")
    assert len(session.calls) == 1


def test_robots_longest_matching_rule_and_program_group():
    rules = "User-agent: *\nDisallow: /\nAllow: /to-rent/\nDisallow: *max-price=\nUser-agent: RentCompassBot\nDisallow: /details/\nAllow: /details/1/$\n"
    assert access._robots_allow(rules, "/details/1/") is True
    assert access._robots_allow(rules, "/details/2/") is False
    # The programme's specific group replaces the wildcard group's rules.
    assert access._robots_allow(rules, "/to-rent/property/camden/") is True
    assert access._robots_allow("User-agent: OtherBot\nDisallow: /\n", "/details/1/") is True


def test_expired_robots_refresh_is_shared_across_concurrent_requests(monkeypatch):
    monkeypatch.setattr(access, "ROBOTS_VERIFIED_AT", "2020-01-01T00:00:00+00:00")
    session = Session([Response(text="User-agent: *\nDisallow: *max-price=\n"), Response(), Response()])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: access.get(session, "https://www.onthemarket.com/to-rent/property/camden/"), range(2)))
    assert len(values) == 2
    assert sum(url.endswith("/robots.txt") for url, kwargs in session.calls) == 1


def test_source_refusal_is_shared_with_another_process(monkeypatch):
    session = Session([Response(403, headers={"Retry-After": "60"})])
    with pytest.raises(access.OTMSourceError):
        access.get(session, "https://www.onthemarket.com/to-rent/property/camden/")
    script = """import json,sys
sys.path[:0]=['app','src']
from core.scraping import otm_access as access
try:
    access.wait_for_slot(budget_s=0.1)
except access.OTMSourceError as exc:
    print(json.dumps({'status':exc.source_status, 'error':exc.as_dict()}))
else:
    raise AssertionError('separate process did not respect source refusal')
"""
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "blocked"
    assert evidence["error"]["status_code"] == 403


def test_shared_source_pacing_covers_search_and_details(monkeypatch):
    monkeypatch.setattr(access, "MIN_INTERVAL_SECONDS", 0.1)
    stamps = []
    class TimedSession:
        def get(self, url, **kwargs):
            stamps.append(time.monotonic())
            return Response()
    session = TimedSession()
    urls = ["https://www.onthemarket.com/to-rent/property/camden/",
            "https://www.onthemarket.com/details/1/",
            "https://www.onthemarket.com/to-rent/property/islington/"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda url: access.get(session, url), urls))
    assert max(stamps) - min(stamps) >= 0.18


def test_bare_page_and_only_permitted_pagination_are_sent(monkeypatch):
    session = Session([Response(text=page([raw_listing(i) for i in range(1, 31)])),
                       Response(text=page([raw_listing(31), raw_listing(32, "£6,000 pcm", 5)]))])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    result = om.find_rich_onthemarket("camden", 3.0, 100, 2000, 40, 1, 2)
    assert len(result) == 31
    assert [call[1]["params"] for call in session.calls] == [{}, {"page-number": 2}]
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert result.coverage["rows_sampled"] == 32
    assert result.coverage["exhaustive"] is False


@pytest.mark.parametrize("code,status", [(403, "blocked"), (429, "rate_limited")])
def test_first_http_refusal_is_typed_and_pauses_search_and_details(code, status, monkeypatch):
    session = Session([Response(code, headers={"Retry-After": "90"})])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    monkeypatch.setattr(om, "_desc_session", lambda: session)
    with pytest.raises(access.OTMSourceError) as failure:
        om.find_rich_onthemarket("camden", 1, 100, 2000)
    assert failure.value.source_status == status
    assert failure.value.as_dict() == {"reason": "http_rate_limited" if code == 429 else "http_access_denied",
                                       "status_code": code, "retry_after_seconds": 90}
    details = om.fetch_listing_details("https://www.onthemarket.com/details/1/")
    assert details["source_status"] == status
    assert details["source_error"]["retry_after_seconds"] <= 90
    assert details["retrieval_complete"] is False
    assert len(session.calls) == 1


def test_challenge_200_is_a_shared_block_not_a_schema_or_empty(monkeypatch):
    session = Session([Response(text="<title>Just a moment...</title><div>Verify you are human</div>")])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    result = on_demand.get_listings("Camden")
    assert result["rows"] == []
    assert result["meta"]["source_status"] == "blocked"
    assert result["meta"]["source_error"]["reason"] == "challenge_response"
    assert result["meta"]["retrieval_complete"] is False
    assert on_demand._cache().get(on_demand._query_key("camden")) is None
    on_demand.get_listings("Islington")
    assert len(session.calls) == 1


def test_transport_error_is_not_empty_and_does_not_expose_exception(monkeypatch):
    session = Session([access.requests.Timeout("SECRET url and query")])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    result = on_demand.get_listings("Camden")
    assert result["meta"]["source_status"] == "unavailable"
    assert result["meta"]["source_error"] == {"reason": "transport_error"}
    assert "SECRET" not in json.dumps(result)
    assert result["meta"]["retrieval_complete"] is False


def test_schema_failure_after_first_page_keeps_partial_uncached(monkeypatch):
    session = Session([Response(text=page([raw_listing(i) for i in range(1, 31)])),
                       Response(text="<html>changed markup</html>")])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    result = on_demand.get_listings("Camden", 0, 4, 100, 2000)
    assert len(result["rows"]) == 15
    assert result["meta"]["source"] == "partial"
    assert result["meta"]["source_status"] == "schema_error"
    assert result["meta"]["retrieval_complete"] is False
    assert on_demand._cache().get(on_demand._query_key("camden")) is None


def test_successful_empty_is_cached_not_replaced_with_stale(monkeypatch):
    cache = on_demand._cache()
    key = on_demand._query_key("camden")
    cache.set(key, [{"Address": "Old, London", "Price": "£1000 pcm", "URL": "https://www.onthemarket.com/details/9/"}])
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE listings SET fetched=0")
    session = Session([Response(text=page())])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    empty = on_demand.get_listings("Camden")
    assert empty["rows"] == [] and empty["meta"]["source"] == "scraped"
    assert empty["meta"]["source_status"] == "ok"
    assert empty["meta"]["coverage"]["exhaustive"] is True
    assert on_demand.get_listings("Camden")["meta"]["source"] == "hit"
    assert len(session.calls) == 1


def test_failure_retains_stale_without_refreshing_timestamp(monkeypatch):
    cache, key = on_demand._cache(), on_demand._query_key("camden")
    cache.set(key, [{"Address": "Old, London", "Price": "£1000 pcm", "URL": "https://www.onthemarket.com/details/9/"}])
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE listings SET fetched=1")
    session = Session([Response(403)])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    result = on_demand.get_listings("Camden")
    assert result["rows"]
    assert result["meta"]["source"] == "stale-cache"
    assert result["meta"]["stale"] is True
    assert result["meta"]["source_status"] == "blocked"
    assert result["meta"]["retrieval_complete"] is False
    assert cache.get(key)[1] == 1


def test_new_budget_reuses_sample_even_when_empty_and_keeps_large_homes(monkeypatch):
    session = Session([Response(text=page([raw_listing(1, "£6,000 pcm", 5), raw_listing(2, "£3000 pcm", 2)])),
                       Response(text=page())])
    monkeypatch.setattr(om, "_new_session", lambda: session)
    first = on_demand.get_listings("Camden", 0, 2, 100, 1000)
    assert first["rows"] == []
    assert first["meta"]["coverage"]["exhaustive"] is True
    second = on_demand.get_listings("Camden", 5, 5, 5000, 7000)
    assert len(second["rows"]) == 1
    assert second["meta"]["source"] == "hit"
    assert len(session.calls) == 2  # one harvest: page 1 and its explicit terminal empty page


def test_concurrent_same_area_misses_fetch_only_once(monkeypatch):
    calls = []
    def scrape(*args):
        calls.append(1)
        time.sleep(0.1)
        return om.OTMHarvest([{ "Address": "Example, London", "URL": "https://www.onthemarket.com/details/1/",
                               "Price": "£1200 pcm", "Room_Type_Category": "2 bed Flat"}], rows_sampled=1)
    monkeypatch.setattr(om, "find_rich_onthemarket", scrape)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: on_demand.get_listings("Camden"), range(2)))
    assert len(calls) == 1
    assert sorted(value["meta"]["source"] for value in values) == ["hit", "scraped"]


def test_details_current_structure_is_cached_and_no_schema_empty_is_cached(monkeypatch):
    session = Session([Response(text=page(prop={"description": "<p>Lift available.</p>"})),
                       Response(text=page())])
    monkeypatch.setattr(om, "_desc_session", lambda: session)
    first = om.fetch_listing_details("https://www.onthemarket.com/details/1/")
    assert first["description"] == "Lift available."
    assert first["source_status"] == "ok"
    again = om.fetch_listing_details("https://www.onthemarket.com/details/1/")
    assert again["source_status"] == "cached"
    invalid = om.fetch_listing_details("https://www.onthemarket.com/details/2/")
    assert invalid["source_status"] == "schema_error"
    assert om._desc_cache().get("https://www.onthemarket.com/details/2/") is None
    assert len(session.calls) == 2
