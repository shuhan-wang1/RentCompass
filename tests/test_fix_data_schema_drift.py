"""
Schema-drift vs honest-empty for the OnTheMarket source (Fix 2).

A markup change on OTM that breaks the __NEXT_DATA__ shape must be surfaced as a
scrape FAILURE (OTMSchemaDriftError -> on_demand stale-if-error), NOT silently
returned as "0 listings". A page that parses fine but genuinely has 0 results
must stay an honest empty.
"""

import json

import pytest

import core.scraping.onthemarket as om
from core.scraping import on_demand


def _next_data(list_value):
    """Build a minimal HTML page carrying a __NEXT_DATA__ blob whose results.list
    equals `list_value`."""
    blob = {"props": {"initialReduxState": {"results": {"list": list_value}}}}
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(blob)
        + "</script></body></html>"
    )


# --------------------------------------------------------------------------
# honest empty (path present, list empty/null) -> [] , no raise
# --------------------------------------------------------------------------
def test_empty_results_is_honest_empty():
    assert om._extract_listings(_next_data([])) == []
    assert om._extract_listings(_next_data(None)) == []


def test_populated_results_returned():
    got = om._extract_listings(_next_data([{"address": "1 A St"}]))
    assert got == [{"address": "1 A St"}]


# --------------------------------------------------------------------------
# drift (missing tag / bad json / missing path) -> OTMSchemaDriftError
# --------------------------------------------------------------------------
def test_missing_next_data_tag_is_drift():
    with pytest.raises(om.OTMSchemaDriftError):
        om._extract_listings("<html><body>no next data here</body></html>")


def test_unparseable_json_is_drift():
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{not valid json,,}</script>')
    with pytest.raises(om.OTMSchemaDriftError):
        om._extract_listings(html)


def test_missing_results_path_is_drift():
    # __NEXT_DATA__ present and valid JSON, but the results.list path is gone.
    blob = {"props": {"initialReduxState": {"somethingElse": {}}}}
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(blob) + "</script>")
    with pytest.raises(om.OTMSchemaDriftError):
        om._extract_listings(html)


# --------------------------------------------------------------------------
# find_rich_onthemarket: page-1 drift propagates; honest-empty does not
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_find_rich_propagates_drift_on_first_page(monkeypatch):
    class _Sess:
        headers = {}

        def get(self, *a, **k):
            return _Resp("<html>totally different markup</html>")

    monkeypatch.setattr(om, "_new_session", lambda: _Sess())
    with pytest.raises(om.OTMSchemaDriftError):
        om.find_rich_onthemarket("london", 1.0, 500, 2000)


def test_find_rich_honest_empty_does_not_raise(monkeypatch):
    class _Sess:
        headers = {}

        def get(self, *a, **k):
            return _Resp(_next_data([]))

    monkeypatch.setattr(om, "_new_session", lambda: _Sess())
    assert om.find_rich_onthemarket("nowhereville", 1.0, 500, 2000) == []


# --------------------------------------------------------------------------
# on_demand composes drift with stale-if-error (does NOT cache empty)
# --------------------------------------------------------------------------
def test_on_demand_drift_serves_stale_cache(tmp_path, monkeypatch):
    import sqlite3
    import time

    cache = on_demand.ListingCache(tmp_path / "c.sqlite3")
    monkeypatch.setattr(on_demand, "_CACHE", cache)
    monkeypatch.setattr(on_demand, "ALLOW_DEMO_FALLBACK", False)

    key = on_demand._query_key("manchester", 1, 1, 500, 1200)
    cache.set(key, [{"Address": "1 Old St, Manchester, M1",
                     "URL": "https://www.onthemarket.com/details/9/",
                     "Price": "£900 pcm"}])
    # Age the entry beyond TTL so a fresh scrape is attempted.
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE listings SET fetched = ?", (time.time() - 10_000_000,))

    def drift(*a, **k):
        raise om.OTMSchemaDriftError("markup changed")

    monkeypatch.setattr(om, "find_rich_onthemarket", drift)
    res = on_demand.get_listings("Manchester", 1, 1, 500, 1200)
    # Drift = failure -> stale-if-error, not honest-empty "none".
    assert res["meta"]["source"] == "stale-cache"
    assert res["meta"]["stale"] is True
    assert res["rows"]
