"""The browser sees a criterion's provenance only as a bounded, sensitive-free excerpt (r6-C1).

``constraint_sources[field].text`` held the whole user message the value was parsed from,
and every response shipped it verbatim in ``view_state.criteria`` and ``search_criteria``.
r10 rid 93630cf9 (NEW-E acceptance) sent "I use a wheelchair, I'm on Universal Credit,.
Find a step-free 1-bed in Camden under £1,800." in eight sources of each of the two
carriers, although round 5 keeps benefits, disability and children's details out of the
criteria and memory. Owner decision: the browser gets only the sentence that supports the
value, with the sensitive clauses removed; the server keeps the full text, which
``criteria_recovery`` and the budget-provenance check in ``synchronize_monthly_budget_alias``
re-parse.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re

import httpx

import app as runtime
from core.criteria_contract import synchronize_monthly_budget_alias
from core.criteria_projection import browser_payload, source_excerpt
from core.langgraph_agent import _apply_explicit_criteria_updates
from core.tools.search_properties import _extract_budget
from test_native_chat_transport import chat, events  # noqa: F401  (fixture)
from uk_rent_agent.web.view_state import browser_criteria, view_state

OWNER_MESSAGE = "£1,800 a month max, I'm on Universal Credit and my son goes to St Mary's"
SENSITIVE = ("Universal Credit", "son", "St Mary", "wheelchair", "Tom", "Oakfield")

# r10 rid 93630cf9…: the recorded payload's view_state.criteria (turns-r10-newe.jsonl).
_R10_TEXT = "I use a wheelchair, I'm on Universal Credit,. Find a step-free 1-bed in Camden under £1,800."
R10_CRITERIA = {
    "amenities_of_interest": [], "area": "Camden", "areas": ["Camden"], "bedrooms": 1,
    "bedrooms_minimum": False, "budget_period": "month", "destination": None,
    "max_budget": 1800, "max_travel_time": None, "no_commute": False,
    "property_features": ["step-free"], "room_type": "whole", "soft_preferences": [],
    "constraint_sources": {field: {"source": "user_message", "text": _R10_TEXT, "value": value}
                           for field, value in (
                               ("area", "Camden"), ("areas", ["Camden"]), ("bedrooms", 1),
                               ("bedrooms_minimum", False), ("budget_period", "month"),
                               ("max_budget", 1800), ("property_features", ["step-free"]),
                               ("room_type", "whole"))},
}


def _no_sensitive_text(value):
    dumped = json.dumps(value, ensure_ascii=False)
    for word in SENSITIVE:
        assert not re.search(rf"\b{word}\b", dumped), word


def test_r10_recorded_criteria_reach_the_browser_without_the_sensitive_clauses():
    projected = browser_criteria(copy.deepcopy(R10_CRITERIA))
    _no_sensitive_text(projected)
    for field, source in projected["constraint_sources"].items():
        assert source["text"] == "Find a step-free 1-bed in Camden under £1,800.", field
        assert source["value"] == R10_CRITERIA["constraint_sources"][field]["value"]
    # Everything but the source texts is unchanged, and the server copy is untouched.
    assert {k: v for k, v in projected.items() if k != "constraint_sources"} == \
        {k: v for k, v in R10_CRITERIA.items() if k != "constraint_sources"}
    assert R10_CRITERIA["constraint_sources"]["area"]["text"] == _R10_TEXT


def test_owner_example_keeps_the_budget_excerpt_and_the_server_text():
    criteria = _apply_explicit_criteria_updates({}, OWNER_MESSAGE)
    assert criteria["constraint_sources"]["max_budget"]["text"] == OWNER_MESSAGE   # server
    projected = browser_criteria(criteria)
    _no_sensitive_text(projected)
    for field in ("max_budget", "budget_period"):
        assert projected["constraint_sources"][field]["text"] == "£1,800 a month max"
    # The excerpt still states the value it supports.
    assert _extract_budget(projected["constraint_sources"]["max_budget"]["text"]) == (1800, "month")


def test_budget_alias_sync_behaves_as_before_on_the_server_copy():
    criteria = dict(_apply_explicit_criteria_updates({}, OWNER_MESSAGE), max_budget_monthly=1500)
    synced = synchronize_monthly_budget_alias(criteria)
    assert synced["max_budget_monthly"] == 1800        # proven by the full user text
    # A user_message source that does not state the figure still cannot replace the alias.
    forged = copy.deepcopy(criteria)
    forged["constraint_sources"]["max_budget"]["text"] = "I'm on Universal Credit"
    assert synchronize_monthly_budget_alias(forged)["max_budget_monthly"] == 1500


def test_excerpt_selection():
    text = "Find a 1-bed in Camden. Actually make it Hackney, max 40 minutes to King's Cross."
    assert source_excerpt("area", "Hackney", text) == \
        "Actually make it Hackney, max 40 minutes to King's Cross."
    assert source_excerpt("bedrooms", 1, text) == "Find a 1-bed in Camden."
    weekly = "I earn £45k a year. Looking for a 2-bed in Hackney, up to £400 a week. My email is a@b.com"
    assert source_excerpt("max_budget", 1733.33, weekly) == \
        "Looking for a 2-bed in Hackney, up to £400 a week."
    # A value stated only inside a sensitive clause has no excerpt at all.
    assert source_excerpt("area", "Islington", "I'm on a Skilled Worker visa and need a flat in Islington") is None
    assert source_excerpt("max_budget", None, "") is None
    # Bounded: never the whole message.
    long = "Find a 1-bed in Camden, " + ", ".join(["quiet street"] * 60) + ", under £1,800."
    assert len(source_excerpt("max_budget", 1800, long)) <= 201


def test_nested_plans_and_recovered_sources_are_projected():
    criteria = {
        "max_budget": 1800,
        "constraint_sources": {"max_budget": {
            "source": "criteria_recovery", "value": 1800, "reason": "parser_value_corrected",
            "original_source": {"source": "user_message", "value": 1800, "text": OWNER_MESSAGE}}},
        "plans": {"Camden": {"constraint_sources": {
            "max_budget": {"source": "user_message", "value": 1800, "text": OWNER_MESSAGE}}}},
    }
    projected = browser_payload(criteria)
    _no_sensitive_text(projected)
    assert projected["constraint_sources"]["max_budget"]["original_source"]["text"] == "£1,800 a month max"
    assert projected["plans"]["Camden"]["constraint_sources"]["max_budget"]["text"] == "£1,800 a month max"
    assert projected["constraint_sources"]["max_budget"]["reason"] == "parser_value_corrected"


def test_projection_failure_drops_the_text_instead_of_shipping_it(monkeypatch):
    import core.criteria_projection as projection

    def broken(_value):
        raise RuntimeError("boom")

    monkeypatch.setattr(projection, "browser_payload", broken)
    projected = browser_criteria(copy.deepcopy(R10_CRITERIA))
    assert all("text" not in source for source in projected["constraint_sources"].values())
    assert projected["constraint_sources"]["max_budget"]["value"] == 1800


def _install_agent(monkeypatch):
    """The real criteria parser and write-back; only the model is left out."""
    async def agent(message, context, continuation, user_id, conversation_id, request_id,
                    ui_language="en", turn=None):
        criteria = _apply_explicit_criteria_updates({}, message)
        runtime._write_back_turn(user_id, conversation_id, message, "Here are some flats.", [],
                                 accumulated_search_criteria=criteria, turn_id=turn["id"])
        # As handle_with_react_agent does for a search turn.
        return {"response_type": "search", "message": "Here are some flats.",
                "recommendations": [], "search_criteria": criteria}

    monkeypatch.setattr(runtime, "handle_with_react_agent", agent)


def _assert_browser_body(body):
    for carrier in (body["view_state"]["criteria"], body.get("search_criteria")):
        if carrier is None:
            continue
        assert carrier["max_budget"] == 1800
        assert carrier["constraint_sources"]["max_budget"]["text"] == "£1,800 a month max"
    _no_sensitive_text({k: v for k, v in body.items() if k != "message"})


def test_every_browser_carrier_is_projected_and_the_snapshot_keeps_the_text(chat, monkeypatch):
    app, _, store = chat
    _install_agent(monkeypatch)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post("/api/alex", json={"message": OWNER_MESSAGE},
                                      headers={"X-Request-Id": "r6-c1-json"})
            assert first.status_code == 200
            body = first.json()
            assert body["search_criteria"]["constraint_sources"]
            _assert_browser_body(body)
            replay = await client.post("/api/alex", json={"message": OWNER_MESSAGE},
                                       headers={"X-Request-Id": "r6-c1-json"})
            assert replay.headers["x-idempotent-replay"] == "1"
            _assert_browser_body(replay.json())
            streamed = await client.post(
                "/api/alex", json={"message": OWNER_MESSAGE, "conversation_id": body["conversation_id"]},
                headers={"Accept": "text/event-stream", "X-Request-Id": "r6-c1-sse"})
            kind, final = events(streamed)[-1]
            assert kind == "final" and final["status"] == 200
            assert final["payload"]["search_criteria"]["constraint_sources"]
            _assert_browser_body(final["payload"])
            return body["conversation_id"], client.cookies

    conversation_id, cookies = asyncio.run(run())
    owner = next(row["user_id"] for row in store._conn.execute(
        "SELECT user_id FROM conversations WHERE id=?", (conversation_id,)))
    snapshot = store.latest_snapshot(owner, conversation_id)
    # Server-side state keeps the full text for criteria_recovery and the alias check.
    assert snapshot["accumulated_search_criteria"]["constraint_sources"]["max_budget"]["text"] == OWNER_MESSAGE
    assert view_state(snapshot)["criteria"]["constraint_sources"]["max_budget"]["text"] == "£1,800 a month max"
    monkeypatch.setattr(runtime, "_identity_from_request", lambda data=None: (owner, "test"))
    loaded = runtime.app.test_client().get(f"/api/conversations/{conversation_id}/messages").json
    _assert_browser_body({"view_state": loaded["view_state"]})
