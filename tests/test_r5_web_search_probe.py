"""R4-16: a user-requested web_search is not blocked by a cooldown it did not get to test.

r07 inst-B B-F8 (05fc634b, 43f8329b): the shipped search_health said ``reason: cooldown,
attempt_count: 0``. Replayed from the recorded HTTP evidence and app.log, the user's search
DID make two real requests (``engines=google`` ReadTimeout after 3.0 s, then HTTP 200 in 4.5 s
with no results and every engine unresponsive). That opened the 15 s cooldown, the tool layer
retried the call one second later, the retry was refused by that cooldown, and only the
retry's health shipped. Internal area-research searches were failing alongside it and open
the same process-wide cooldown. Offline: ``requests.get`` is stubbed, no network.
"""
import asyncio

import pytest
import requests

import core.tools.web_search as search_tool
import core.web_search as search

MANCHESTER = ("Renting in Manchester student guide: average rent by area, room types, bills, "
              "best neighbourhoods 2026")      # the recorded 05fc634b query


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


UNRESPONSIVE = {"results": [], "unresponsive_engines": [["google", "timeout"], ["bing", "timeout"],
                                                          ["duckduckgo", "timeout"], ["brave", "timeout"]]}
RESULTS = {"results": [{"title": "Student guide to renting in Manchester",
                        "url": "https://www.manchester.ac.uk/accommodation/renting-guide",
                        "content": "Average student rent in Manchester and the areas students choose."}]}


@pytest.fixture
def client(monkeypatch):
    fresh = search.SearXNGSearch(instance_url="http://searxng.invalid:8080")
    monkeypatch.setattr(search, "_searxng_client", fresh)
    monkeypatch.setattr(search, "_read_search_cache", lambda key: ("miss", None))
    monkeypatch.setattr(search, "_write_search_cache", lambda key, value: None)
    return fresh


def _replay(monkeypatch, responses):
    """Each call pops the next recorded outcome (an exception class or a payload)."""
    calls = []

    def get(url, params, timeout):
        calls.append({"params": dict(params), "timeout": timeout})
        outcome = responses.pop(0) if responses else UNRESPONSIVE
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome("offline replay")
        return _Response(outcome)

    monkeypatch.setattr(search.requests, "get", get)
    return calls


def _open_cooldown_internally(monkeypatch, client):
    """An internal research search (recommend_areas-style, no user mark) fails both attempts."""
    calls = _replay(monkeypatch, [requests.exceptions.ReadTimeout, UNRESPONSIVE])
    assert search.get_search_snippets("best areas to live commuting to Canary Wharf rent", 6) \
        == "No search results found for this query."
    assert len(calls) == 2 and client._unavailable_until > 0


def test_user_search_after_an_internal_failure_gets_one_real_attempt(monkeypatch, client):
    _open_cooldown_internally(monkeypatch, client)
    calls = _replay(monkeypatch, [RESULTS])
    result = asyncio.run(search_tool.web_search_func(MANCHESTER))
    assert result["success"] is True
    assert len(calls) == 1 and "engines" not in calls[0]["params"]   # one probe, default engines
    health = result["search_health"]["web_search"]
    assert health["attempt_count"] == 1 and health["reason"] == "cooldown_probe"
    assert client._unavailable_until == 0.0          # the service answered: cooldown closed


def test_internal_and_background_searches_never_bypass_the_cooldown(monkeypatch, client):
    _open_cooldown_internally(monkeypatch, client)
    calls = _replay(monkeypatch, [RESULTS])
    snippet = search.get_search_snippets("cost of living in Hackney London", 5)
    assert calls == []
    assert snippet.service_health["reason"] == "cooldown"
    assert snippet.service_health["attempt_count"] == 0


def test_the_probe_is_bounded_during_an_outage(monkeypatch, client):
    _open_cooldown_internally(monkeypatch, client)
    calls = _replay(monkeypatch, [])                 # every request: 200, no results, engines down
    for _ in range(5):
        assert asyncio.run(search_tool.web_search_func(MANCHESTER))["success"] is False
    assert len(calls) == 1                            # one probe per interval, never a retry
    assert calls[0]["timeout"] <= client.probe_deadline_seconds / 2 + 1e-9
    assert client._unavailable_until > 0             # the failed probe re-armed the cooldown
    client._last_probe_at -= client.probe_interval_seconds
    asyncio.run(search_tool.web_search_func(MANCHESTER))
    assert len(calls) == 2


def test_the_mark_does_not_leak_to_later_internal_searches(monkeypatch, client):
    _open_cooldown_internally(monkeypatch, client)
    _replay(monkeypatch, [UNRESPONSIVE])
    asyncio.run(search_tool.web_search_func(MANCHESTER))
    assert search.USER_REQUESTED_SEARCH.get() is False
    client._last_probe_at = float("-inf")            # a probe would be allowed again
    calls = _replay(monkeypatch, [RESULTS])
    search.get_search_snippets("internal research query", 6)
    assert calls == []


def test_recorded_b_f8_sequence_ships_the_real_attempts(monkeypatch, client):
    """The recorded 05fc634b sequence through the real tool, retry included: the first
    execution's two requests open the cooldown, the tool layer's retry probes once, and the
    shipped health shows a real attempt, never "cooldown, attempt_count 0"."""
    calls = _replay(monkeypatch, [requests.exceptions.ReadTimeout, UNRESPONSIVE, UNRESPONSIVE])
    result = asyncio.run(search_tool.web_search_tool.execute(query=MANCHESTER))
    assert len(calls) == 3
    health = result.data["search_health"]["web_search"]
    assert health["attempt_count"] == 1 and health["reason"] == "cooldown_probe"


def test_a_refused_search_names_the_attempts_that_opened_the_cooldown(monkeypatch, client):
    _open_cooldown_internally(monkeypatch, client)
    snippet = search.get_search_snippets("another internal query", 6)
    after = snippet.service_health["cooldown_after"]
    assert [a["status"] for a in after["attempts"]] == ["unavailable", "unavailable"]
    assert after["attempts"][0]["error_type"] == "ReadTimeout"
