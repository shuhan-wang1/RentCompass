"""Every tool call in an assistant message is answered before the next model call (r10).

DeepSeek acceptance r10, rid 6e02119fd1744ec6bd0e496562747e89 (osm fault = unavailable):
"Which gyms are near Camden Road, Islington N7, and which nearby area is best value for a
1-bed?" The model returned two calls (model-calls n=81). The second one's arguments were not
valid JSON (an unquoted ``"destination": Camden Road, Islington N7``), so LangChain put it in
``invalid_tool_calls`` and only ``search_nearby_pois`` ran. langchain-openai serialises
``tool_calls`` AND ``invalid_tool_calls`` into the assistant message, so the next plan call
(n=82, 5 messages) carried two call ids and one tool result: DeepSeek 400 "insufficient tool
messages following tool_calls message", ``fc_loop.plan_llm_failed type=BadRequestError``,
and the user got "The model service is temporarily unavailable".

The assistant message below is the checkpointed one, byte for byte. The fake model checks
each request the way DeepSeek does, on the payload langchain-openai would actually send.
"""
from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai.chat_models.base import _convert_message_to_dict

import core.agent_loop as agent_loop
from tests.test_agent_contract_guards import Provider, Result, Spec, _drive, _state

MESSAGE = ("Which gyms are near Camden Road, Islington N7, and which nearby area is best value "
           "for a 1-bed?")
_BAD_ARGS = ('{"city": "London", "destination": Camden Road, Islington N7, "max_commute_minutes": '
             'null, "budget_hint": null, "priorities": ["value"], "candidate_areas": null, '
             '"limit": 8, "reply_language": "en"}')
RECORDED_AI = AIMessage(
    content="I'll look up gyms near Camden Road and compare nearby areas for value.",
    tool_calls=[{"name": "search_nearby_pois", "args": {
        "address": "Camden Road, Islington, London N7", "poi_type": "gym", "radius": 800,
        "user_query": "Which gyms are near Camden Road, Islington N7?", "latitude": None,
        "longitude": None}, "id": "call_00_WOCfKogU9Rgt14ehmBkt8522", "type": "tool_call"}],
    invalid_tool_calls=[{
        "type": "invalid_tool_call", "id": "call_01_hEWIQshC0hYVmPmtbpwR0408",
        "name": "compare_or_rank_areas", "args": _BAD_ARGS,
        "error": ("Function compare_or_rank_areas arguments:\n\n" + _BAD_ARGS + "\n\nare not valid "
                  "JSON. Received JSONDecodeError Expecting value: line 1 column 35 (char 34)")}])
# The recorded search_nearby_pois outcome with OpenStreetMap down (abridged to its status fields).
POI_FAILED = Result(False, {
    "success": False, "address": "Camden Road, Islington, London N7", "pois": {},
    "source": "OpenStreetMap Overpass", "requested_types": ["gym"], "failed_types": ["gym"],
    "category_status": {"gym": {"status": "failed", "error": "POI lookup failed",
                                "error_type": "OverpassError", "retryable": True}},
    "partial": True, "search_status": "failed", "service_status": "unavailable"},
    error="POI lookup failed")


class BadRequest(Exception):
    pass


def deepseek_check(messages) -> list[dict]:
    """The payload langchain-openai sends, rejected the way DeepSeek rejects it."""
    payload = [_convert_message_to_dict(m) for m in messages]
    for k, msg in enumerate(payload):
        if msg["role"] == "tool":
            before = payload[k - 1] if k else {}
            if not (before.get("role") == "tool" or before.get("tool_calls")):
                raise BadRequest("tool message without a preceding tool_calls message")
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            ids = [tc["id"] for tc in msg["tool_calls"]]
            j, answered = k + 1, []
            while j < len(payload) and payload[j]["role"] == "tool":
                answered.append(payload[j]["tool_call_id"])
                j += 1
            if sorted(answered) != sorted(ids):
                raise BadRequest("insufficient tool messages following tool_calls message")
            for tc in msg["tool_calls"]:
                json.loads(tc["function"]["arguments"])   # history arguments are valid JSON
    return payload


class DeepSeekLike:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, messages):
        self.requests.append(deepseek_check(messages))
        return self.replies.pop(0)


def _nodes(chat, provider):
    return agent_loop.build_fc_nodes(provider, agent_llm=chat)


def test_r10_the_malformed_sibling_is_answered_and_the_turn_continues():
    retry = AIMessage(content="", tool_calls=[{"name": "compare_or_rank_areas", "args": {
        "city": "London", "destination": "Camden Road, Islington N7", "priorities": ["value"],
        "limit": 8}, "id": "call_retry", "type": "tool_call"}])
    final = AIMessage(content="The gym lookup failed this turn. For value, Holloway ranks first.")
    chat = DeepSeekLike(RECORDED_AI, retry, final)
    provider = Provider([Spec("search_nearby_pois"), Spec("compare_or_rank_areas")], {
        "search_nearby_pois": POI_FAILED,
        "compare_or_rank_areas": Result(True, {"status": "ok", "areas": [{"area": "Holloway"}]})})
    state = asyncio.run(_drive(_nodes(chat, provider), _state(MESSAGE)))

    assert len(chat.requests) == 3               # every request passed the provider check
    assert state.get("response_type") != "error"
    assert state["final_response"].startswith("The gym lookup failed")
    # The second request told the model its malformed call did not run, and why.
    tool_rows = [m for m in chat.requests[1] if m["role"] == "tool"]
    by_id = {m["tool_call_id"]: m["content"] for m in tool_rows}
    assert set(by_id) == {"call_00_WOCfKogU9Rgt14ehmBkt8522", "call_01_hEWIQshC0hYVmPmtbpwR0408"}
    assert "not valid JSON" in by_id["call_01_hEWIQshC0hYVmPmtbpwR0408"]
    # The malformed text itself was never echoed back.
    assert '"destination": Camden Road' not in json.dumps(chat.requests[1], ensure_ascii=False)
    assert '"destination": Camden Road' not in "".join(
        tc["function"]["arguments"] for m in chat.requests[1] for tc in m.get("tool_calls") or [])
    # The ledger records it as requested, not executed.
    bad = [a for a in state["tool_artifacts"] if a.get("tool") == "compare_or_rank_areas"
           and a.get("denied")]
    assert len(bad) == 1 and "not valid JSON" in bad[0]["error"]
    assert [m["role"] for m in chat.requests[1]][-3:] == ["assistant", "tool", "tool"]


def test_r10_the_state_transcript_pairs_every_call():
    provider = Provider([Spec("search_nearby_pois"), Spec("compare_or_rank_areas")],
                        {"search_nearby_pois": POI_FAILED})
    state = _state(MESSAGE)
    state["messages"] = [HumanMessage(content=MESSAGE), RECORDED_AI]
    command = asyncio.run(_nodes(DeepSeekLike(), provider)["execute_tools"](state))
    answered = [m.tool_call_id for m in command.update["messages"] if isinstance(m, ToolMessage)]
    assert sorted(answered) == ["call_00_WOCfKogU9Rgt14ehmBkt8522", "call_01_hEWIQshC0hYVmPmtbpwR0408"]


def test_r10_the_provider_view_repairs_any_unanswered_or_orphan_result():
    """The seam itself, for a transcript no executor path repaired: an unanswered valid call,
    an unanswered malformed one, a duplicate result and an orphan result."""
    ai = AIMessage(content="", tool_calls=[
        {"name": "check_safety", "args": {"area": "Holloway"}, "id": "a", "type": "tool_call"},
        {"name": "search_nearby_pois", "args": {"address": "N7"}, "id": "b", "type": "tool_call"}],
        invalid_tool_calls=RECORDED_AI.invalid_tool_calls)
    transcript = [
        SystemMessage(content="system"), ToolMessage(content="{}", tool_call_id="orphan"),
        HumanMessage(content=MESSAGE), ai,
        ToolMessage(content='{"success": true}', tool_call_id="a", name="check_safety"),
        ToolMessage(content='{"success": true}', tool_call_id="a", name="check_safety"),
    ]
    payload = deepseek_check(agent_loop._provider_transcript(transcript))
    roles = [m["role"] for m in payload]
    assert roles == ["system", "user", "assistant", "tool", "tool", "tool"]
    by_id = {m["tool_call_id"]: m["content"] for m in payload if m["role"] == "tool"}
    assert by_id["a"] == '{"success": true}'
    assert "no result was recorded" in by_id["b"]
    assert "not valid JSON" in by_id["call_01_hEWIQshC0hYVmPmtbpwR0408"]
    # A well-formed transcript passes through unchanged.
    clean = [HumanMessage(content=MESSAGE), AIMessage(content="ok")]
    assert agent_loop._provider_transcript(clean) == clean


def test_r10_the_wrap_up_view_drops_a_malformed_only_call():
    only_bad = AIMessage(content="", invalid_tool_calls=RECORDED_AI.invalid_tool_calls)
    rows = agent_loop._descaffold_for_wrap([HumanMessage(content=MESSAGE), only_bad])
    deepseek_check(rows)
    assert not any(getattr(m, "invalid_tool_calls", None) for m in rows)


# ── Secondary (r10 b1e1ee9180e54f958dc8d0f907f83d4d) ─────────────────────────────────
# compare_or_rank_areas with eight caller-named areas came back no_data. Not the osm outage:
# the tool never calls Overpass. The destination "Camden Road Station, London N7" did not
# geocode (Nominatim answered 200 with no match, fault group "geocode" ok), and the candidate
# core returned [] for a destination it could not locate, so areas with cached rent data were
# never ranked. Caller-named areas now rank on rent with an unknown commute.
R10_AREAS = ["Camden", "Islington", "Holloway", "Kentish Town", "Finsbury Park", "Archway",
             "Tufnell Park", "Highbury"]


def _offline_places(monkeypatch, geocode, commute):
    import core.recommend_areas as ra
    slug = lambda name: name.strip().lower().replace(" ", "-")  # noqa: E731
    monkeypatch.setattr(ra, "classify_place", lambda name: {
        "slug": slug(name), "city": "london", "kind": "area", "address": None})
    monkeypatch.setattr(ra, "resolve_location", lambda name: (slug(name), "london"))
    monkeypatch.setattr(ra, "is_destination", lambda place: False)
    monkeypatch.setattr(ra, "geocode_address", geocode)
    monkeypatch.setattr(ra, "commute_minutes", commute)
    return ra


def test_r10_named_areas_rank_on_rent_when_the_destination_cannot_be_located(monkeypatch):
    from core import area_stats
    from core.tools.compare_or_rank_areas import compare_or_rank_areas_impl

    _offline_places(monkeypatch,
                    lambda addr: None if "Camden Road Station" in addr else {"lat": 51.55, "lng": -0.12},
                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("no destination to route to")))
    medians = {"holloway": 1650.0, "archway": 1700.0, "camden": 2100.0}

    def aggregate(slugs, budget=None):
        out = {}
        for s in slugs:
            st = dict(area_stats._empty())
            if s in medians:
                st.update(min=medians[s] - 200, max=medians[s] + 200, median=medians[s],
                          sample_size=12, freshness_days=1)
            out[s] = st
        return out
    monkeypatch.setattr(area_stats, "aggregate", aggregate)
    result = asyncio.run(compare_or_rank_areas_impl(
        candidate_areas=list(R10_AREAS), city="London", destination="Camden Road Station, London N7",
        limit=8, priorities=["value"], reply_language="en"))

    assert result["status"] != "no_data"
    names = [a["name"] for a in result["areas"]]
    assert names[:3] == ["Holloway", "Archway", "Camden"]      # ranked on rent
    assert set(names) == set(R10_AREAS)                        # none silently dropped
    assert all(a["commute_minutes"] is None for a in result["areas"])
    notes = " ".join(result["explanation_notes"])
    assert "could not be computed" in notes and "No commute destination was given" not in notes


def test_r10_a_named_area_without_a_route_ranks_after_the_routed_ones(monkeypatch):
    import core.recommend_areas as ra
    _offline_places(monkeypatch, lambda addr: {"lat": 51.55, "lng": -0.12},
                    lambda destination, coords, origin_address=None, **k:
                    None if origin_address == "Archway" else (25 if origin_address == "Holloway" else 70))
    out = asyncio.run(ra.generate_candidate_areas(
        "Camden Road Station, London N7", city="london", dest_coords={"lat": 51.55, "lng": -0.12},
        london=True, candidate_names=["Holloway", "Archway", "Camden"], max_commute_time=45))
    by_slug = {i["slug"]: i["commute_minutes"] for i in out}
    # Unknown commute: kept, unscored on commute. A known commute over the cap: still dropped.
    assert by_slug == {"holloway": 25, "archway": None}
