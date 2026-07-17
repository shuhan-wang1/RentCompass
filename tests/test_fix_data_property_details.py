"""
get_property_details now reads the SAME on-demand SQLite listing cache the search
path serves (Fix 1), not the offline provider CSV / fake data.

A listing that exists in the cache (because search put it there) must be
resolvable by the detail tool, both by fuzzy address and by exact URL.
"""

import asyncio

from core.scraping import on_demand
from core.tools import get_property_details as gpd


def _seed_cache(monkeypatch, tmp_path, rows):
    cache = on_demand.ListingCache(tmp_path / "listings.sqlite3")
    key = on_demand._query_key("london", 1, 1, 1000, 2000)
    cache.set(key, rows)
    monkeypatch.setattr(on_demand, "_CACHE", cache)
    return cache


_ROW = {
    "Address": "19-29 Woburn Place, Bloomsbury, London WC1H 0JR",
    "URL": "https://www.onthemarket.com/details/12345/",
    "Price": "£2,300 pcm",
    "Room_Type_Category": "Studio",
    "Description": "A bright studio in Bloomsbury.",
    "Detailed_Amenities": "Gym, Concierge, Wifi",
    "Available From": "2026-09-01",
    "geo_location": "51.52, -0.12",
}


def test_load_property_database_reads_on_demand_cache(monkeypatch, tmp_path):
    _seed_cache(monkeypatch, tmp_path, [_ROW])
    df = gpd.load_property_database()
    assert not df.empty
    assert "19-29 Woburn Place" in df.iloc[0]["Address"]


def test_details_resolves_listing_in_cache_by_address(monkeypatch, tmp_path):
    _seed_cache(monkeypatch, tmp_path, [_ROW])
    res = asyncio.run(gpd.get_property_details_impl(
        property_address="Woburn Place Bloomsbury"))
    assert res["success"] is True and res["found"] is True
    assert "Woburn Place" in res["property"]["address"]
    assert res["property"]["room_type"] == "Studio"
    assert res["room_type_analysis"]["is_studio"] is True
    # Availability carried through from the cached rich-schema row.
    assert res["property"]["available_from"] == "2026-09-01"


def test_details_resolves_listing_in_cache_by_url(monkeypatch, tmp_path):
    _seed_cache(monkeypatch, tmp_path, [_ROW])
    res = asyncio.run(gpd.get_property_details_impl(
        property_address="https://www.onthemarket.com/details/12345/"))
    assert res["success"] is True and res["found"] is True
    assert res["property"]["url"] == _ROW["URL"]


def test_details_honest_not_found_when_absent(monkeypatch, tmp_path):
    _seed_cache(monkeypatch, tmp_path, [_ROW])
    res = asyncio.run(gpd.get_property_details_impl(
        property_name="Nonexistent Palace Tower"))
    assert res.get("found") is False


def test_empty_cache_yields_empty_database(monkeypatch, tmp_path):
    # No rows cached -> honest empty DataFrame (no CSV / fake-data fallback).
    cache = on_demand.ListingCache(tmp_path / "empty.sqlite3")
    monkeypatch.setattr(on_demand, "_CACHE", cache)
    assert gpd.load_property_database().empty


def test_no_provider_csv_dependency():
    # The provider-CSV coupling is fully removed from this tool.
    import inspect
    src = inspect.getsource(gpd)
    assert "get_active_property_csv" not in src
    assert "fake_property_listings" not in src
