"""R4-06: the model sees every listing of a large search, not the first ~4.

search_properties payloads ran 17k-43k chars and the model channel was ``content[:6000]``
(fc_loop.toolmsg_truncated x65 in r07 B): the model could not tell which area a listing was
in, and said it lacked a commute time the cards showed ("72 min to Euston").
"""
from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage, ToolMessage

from core.agent_loop import _TOOLMSG_CAPS, build_fc_nodes, project_search_payload
from tests.test_fc_loop import FakeChat, FakeProvider, FakeResult, FakeSpec, _base_state, _drive, _tc


def _listing(i, area):
    return {
        "rank": i, "address": f"{i} Example Street, London, N{i}", "provider_address": f"{i} Example St",
        "price": f"£{1500 + 25 * i}/month", "budget_status": "within", "score": 80,
        "score_breakdown": {"x": 1, "y": 2}, "property_type": "Flat", "room_type": "1-bed flat",
        "bedrooms": 1, "match_type": "perfect", "source": "OnTheMarket", "possibly_outdated": False,
        "url": f"https://www.onthemarket.com/details/{1000 + i}/", "images": ["https://img/x.jpg"] * 8,
        "geo_location": "51.5, -0.1", "distance_miles": 0.4 + i / 10,
        "explanation": "A bright flat " * 40, "area": area,
        "description": "Long agent description. " * 120,
        "available_from": "Available now", "availability_status": "",
        "verified_features": [], "unverified_features": [], "absent_features": [],
        "travel_time": f"{20 + i} min to Euston",
    }


RAW = {
    "success": True, "status": "found", "summary": "Found 12 listings",
    "search_criteria": {"areas": ["Hackney", "Islington"], "max_budget": 2200},
    "recommendations": [_listing(i, "Hackney" if i % 2 else "Islington") for i in range(1, 13)],
    "candidate_validation": {"eligible": [{"address": f"{i} Example Street", "reasons": []}
                                          for i in range(1, 13)]},
}


def test_projection_keeps_every_listing_and_its_decision_fields_within_the_cap():
    cap = _TOOLMSG_CAPS["search_properties"]
    assert len(json.dumps(RAW)) > 3 * cap
    view = project_search_payload(RAW, cap)
    assert len(json.dumps(view, ensure_ascii=False)) <= cap
    assert list(view)[:2] == ["status", "summary"]
    assert len(view["recommendations"]) == 12
    for i, row in enumerate(view["recommendations"], start=1):
        for key in ("address", "area", "price", "bedrooms", "property_type", "match_type", "travel_time"):
            assert key in row, (i, key)
        assert "images" not in row and "description" not in row
    assert view["recommendations"][10]["travel_time"] == "31 min to Euston"


def test_raw_artifact_untouched_and_model_message_not_hard_cut():
    specs = [FakeSpec("search_properties")]
    provider = FakeProvider(specs, {"search_properties": FakeResult(True, RAW)})
    chat = FakeChat([
        AIMessage(content="", tool_calls=[_tc("search_properties", {"user_query": "flats"}, "c1")]),
        AIMessage(content="done"),
    ])
    nodes = build_fc_nodes(provider, agent_llm=chat)
    state = asyncio.run(_drive(nodes, _base_state()))
    art = next(a for a in state["tool_artifacts"] if a["tool"] == "search_properties")
    assert art["raw_data"]["recommendations"][0]["description"] == RAW["recommendations"][0]["description"]
    tmsg = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert "...[truncated]" not in tmsg.content
    assert "12 Example Street" in tmsg.content and "31 min to Euston" in tmsg.content
