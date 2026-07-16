"""Integration regression for strict room-type filtering in ranker_v2."""

import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)


def test_requested_room_type_does_not_fall_back_to_a_different_type(monkeypatch):
    from core.scraping import on_demand
    from core.tools.search_properties import search_properties_impl, set_rag_coordinator

    class Store:
        def build_index(self, rows):
            self.rows = list(rows)

    class Coordinator:
        def __init__(self):
            self.property_store = Store()

        def enhanced_search(self, _query, _criteria):
            return self.property_store.rows, [], []

    row = {
        "Address": "One-bed flat, Camden",
        "URL": "https://www.onthemarket.com/details/example/",
        "Price": "1200 pcm",
        "Room_Type_Category": "1 bed Flat",
        "Description": "One bedroom flat",
        "Images": [],
    }
    monkeypatch.setattr(on_demand, "get_listings", lambda *args, **kwargs: {
        "rows": [row],
        "meta": {"source": "scraped", "stale": False, "count": 1, "requested_city": "london"},
    })
    set_rag_coordinator(Coordinator())
    try:
        result = asyncio.run(search_properties_impl(
            area="Camden", room_type="studio", confirmed=True,
        ))
    finally:
        set_rag_coordinator(None)

    assert result["status"] == "no_results"
    assert result["recommendations"] == []
