"""fc_loop grounding-critic adaptation (Phase 2.1 / agent P2).

The fc arch reuses the legacy critic node but never writes ``tool_decision`` and
records tool output in ``tool_artifacts`` (not ``tool_raw_data``/``observations``).
Without adaptation an fc tool-answer is treated as a direct answer and grounding is
skipped — the H3 guard case (SearXNG down, web_search empty, model fabricates a
Zone-2 rent table, critic passes it). These tests pin the three fixes:

1. retrieval detection also fires on retrieval-tool artifacts (shared
   ``NON_RETRIEVAL_TOOLS`` frozenset / ``_is_retrieval_tool``);
2. ``_collect_grounding_evidence`` folds successful artifact ``raw_data`` in and
   SKIPS ``success is False`` ones;
3. a deterministic no-evidence 兜底 hard-REPLACES a still-numeric answer when every
   executed retrieval source is empty/failed (zh + en), while turns with real
   evidence and no-tools turns are untouched, and the legacy path is unchanged.

No live API: the regeneration LLM is stubbed exactly as ``test_critic_grounding``
does. ``pythonpath = ["src", "app"]`` puts ``uk_rent_agent`` + ``core`` on the path.
"""

from __future__ import annotations

import asyncio

import pytest

from uk_rent_agent.agent.critic import (
    LEGACY_INCONSISTENCY_FALLBACK,
    LEGACY_RETRIEVAL_MISS_FALLBACK,
    CAVEAT,
    evidence_usable,
    has_specific_price_claims,
    no_reliable_data_message,
)

_LEGACY_FALLBACKS = {LEGACY_INCONSISTENCY_FALLBACK, LEGACY_RETRIEVAL_MISS_FALLBACK}


# ── shared local-core import (mirror test_critic_grounding) ─────────────────
def _load_local_core():
    import importlib
    import os
    import sys

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)
    for name in list(sys.modules):
        if name == "core" or name.startswith("core."):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "app" not in path:
                del sys.modules[name]
    return importlib.import_module("core.llm_config"), importlib.import_module("core.langgraph_agent")


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return _FakeResp(self.content)


def _run(coro):
    return asyncio.run(coro)


def _artifact(tool, raw_data, *, success=True, error=None, turn=1):
    """fc artifact in the P1-enriched shape ({turn, tool, raw_data, params_digest,
    success, error}); tolerant helpers must also cope when success/error are absent."""
    art = {"turn": turn, "tool": tool, "raw_data": raw_data, "params_digest": f"{tool}:d"}
    if success is not None:
        art["success"] = success
    if error is not None:
        art["error"] = error
    return art


def _fc_state(final_response, artifacts, *, reply_language="en"):
    """An fc turn: tool_decision stays empty ({}); evidence lives in tool_artifacts."""
    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state(
        "how much is rent around Zone 2?",
        extracted_context={"current_message": "how much is rent around Zone 2?",
                           "reply_language": reply_language},
    )
    state["tool_artifacts"] = list(artifacts)
    state["final_response"] = final_response
    return state


# ── 1. retrieval detection ──────────────────────────────────────────────────
def test_is_retrieval_tool_shared_frozenset():
    _llm, lga = _load_local_core()
    is_ret = lga._is_retrieval_tool
    # retrieval-ish tools -> True
    for name in ("web_search", "search_properties", "check_safety", "reasoning_property"):
        assert is_ret(name) is True, name
    # non-retrieval pseudo-routes / write / clarification -> False
    for name in ("", "direct_answer", "clarification", "ask_user", "remember"):
        assert is_ret(name) is False, name
    # the legacy exclusion set is preserved as a subset of the shared frozenset
    assert {"", "direct_answer", "clarification"} <= lga.NON_RETRIEVAL_TOOLS


def test_fc_artifacts_make_retrieval_expected(monkeypatch):
    """fc turn (no tool_decision) with a retrieval artifact -> grounding IS enforced:
    a fabricated price triggers the corrective regeneration pass."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("The going rate is about £1,500 pcm.")  # corrected & grounded
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    art = _artifact("web_search", {"summary": "Typical Zone-2 rent is £1,500 pcm."})
    state = _fc_state("Zone-2 rents are around £9,999 pcm.", [art])  # fabricated figure
    update = _run(lga._make_critic_node()(state))

    assert fake.calls == 1  # retrieval_expected True -> grounding ran -> regeneration fired
    assert update["final_response"] == "The going rate is about £1,500 pcm."
    assert update["verdict"]["grounded"] is True


# ── 2. evidence collection from artifacts ────────────────────────────────────
def test_evidence_includes_successful_artifacts_and_excludes_failed():
    _llm, lga = _load_local_core()
    import json

    good = _artifact("search_properties", {"recommendations": [{"price": "£1,500 pcm"}]})
    bad = _artifact("web_search", {"leaked": "£9,999 pcm"}, success=False,
                    error="searxng connection refused")
    state = _fc_state("draft", [good, bad])

    pieces = lga._collect_grounding_evidence(state, "")
    blob = json.dumps(pieces, ensure_ascii=False, default=str)
    assert "1,500" in blob                       # successful artifact folded in
    assert "search_properties#1" in blob         # per-artifact "tool#turn" label
    assert "9,999" not in blob and "leaked" not in blob  # failed artifact excluded
    assert "searxng" not in blob                 # error text is not evidence


# ── 3. deterministic no-evidence 兜底 (H3) ───────────────────────────────────
_H3_ANSWER = ("Here are typical Zone-2 rents: studios £1,800 pcm, one-beds £2,200 pcm, "
              "two-beds £2,800 pcm.")


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_h3_all_failed_retrieval_numeric_answer_is_hard_replaced(monkeypatch, lang):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    # Regeneration also produces figures (still ungrounded) — the 兜底 replaces regardless.
    fake = _FakeLLM("Approximately £1,750 pcm on average.")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    failed = _artifact("web_search", None, success=False, error="searxng down")
    state = _fc_state(_H3_ANSWER, [failed], reply_language=lang)
    update = _run(lga._make_critic_node()(state))

    # fc turn (tool_artifacts present) -> artifact-grounded fallback, NOT the generic template.
    from core.agent_loop import _artifact_grounded_fallback_answer
    expected = _artifact_grounded_fallback_answer(state, reason="no_reliable_numbers")
    assert update["final_response"] == expected     # HARD replace, not a caveat
    assert update["final_response"] != no_reliable_data_message(lang)
    assert CAVEAT not in update["final_response"]
    assert update["final_response"] not in _LEGACY_FALLBACKS
    assert has_specific_price_claims(update["final_response"]) is False
    if lang == "zh":
        assert "可靠" in update["final_response"]


def test_evidence_present_with_numbers_is_not_replaced(monkeypatch):
    """Real, usable retrieval evidence -> normal path: grounded figures pass through and
    the 兜底 never fires."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("should not be called")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    art = _artifact("web_search", {"summary": "Zone-2: studios £1,800 pcm, one-beds £2,200 pcm."})
    original = "Zone-2 studios are £1,800 pcm and one-beds £2,200 pcm."  # grounded
    state = _fc_state(original, [art])
    update = _run(lga._make_critic_node()(state))

    assert fake.calls == 0                                   # grounded -> no regeneration
    # grounded pass-through: node leaves final_response as-is (no key) or equal to the original
    assert update.get("final_response", original) == original
    assert update.get("final_response", original) != no_reliable_data_message("en")


def test_no_tools_conversational_turn_is_untouched(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("should not be called")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    state = _fc_state("Rents around there are roughly £2,000 pcm.", [])  # no artifacts
    update = _run(lga._make_critic_node()(state))

    assert fake.calls == 0                    # retrieval_expected False -> no gating at all
    assert "final_response" not in update     # answer unchanged
    assert update["verdict"]["grounded"] is True


# ── 4. legacy (tool_decision-driven) path unchanged ─────────────────────────
def _legacy_reasoning_state(final_response):
    from uk_rent_agent.agent.state import create_initial_state

    state = create_initial_state("tell me more about this flat")
    state["tool_decision"] = {"tool": "reasoning_property"}
    state["tool_observation"] = "Property: 40 Merchant St\nPrice: £2,678 pcm\nRoom Type: Studio"
    state["tool_raw_data"] = {"property_info": "Price: £2,678 pcm"}
    state["final_response"] = final_response
    return state


def test_legacy_tool_decision_path_still_regenerates(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("The monthly rent is £2,678 pcm.")  # corrected & grounded
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    state = _legacy_reasoning_state("The rent is £4,500 pcm.")  # fabricated
    update = _run(lga._make_critic_node()(state))

    assert fake.calls == 1
    assert update["final_response"] == "The monthly rent is £2,678 pcm."
    assert update["verdict"]["grounded"] is True
    # legacy evidence present -> 兜底 must NOT fire
    assert update["final_response"] != no_reliable_data_message("en")


def test_legacy_present_evidence_persistent_failure_keeps_caveat_not_fallback(monkeypatch):
    """Legacy turn WITH usable raw_data but a stubbornly-fabricated regeneration keeps the
    existing caveat behavior — the no-evidence 兜底 is scoped to zero-evidence turns only."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("Actually the rent is £7,777 pcm.")  # still fabricated
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    state = _legacy_reasoning_state("The rent is £4,500 pcm.")
    update = _run(lga._make_critic_node()(state))

    assert fake.calls == 1
    assert CAVEAT in update["final_response"]                       # caveat path, not 兜底
    assert update["final_response"] != no_reliable_data_message("en")
    assert update["final_response"] not in _LEGACY_FALLBACKS


# ── 5. evidence_usable truth table over the REAL emitted shapes (H3) ─────────
# The exact strings/dicts the web-search stack produces (see core.web_search and
# core.tools.web_search): get_search_snippets / format_for_llm return the placeholder
# below when SearXNG is down; web_search's error path returns {success:False,...}; a
# mislabelled success path can still carry a placeholder blob.
_PLACEHOLDER_SNIPPET = "No search results found for this query."
_WEB_SEARCH_PLACEHOLDER_BLOB = (
    f"### Web Search: zone 2 rent\n{_PLACEHOLDER_SNIPPET}\n"
)
# The full web_search tool return when SearXNG is down but success is (wrongly) True.
_WEB_SEARCH_SUCCESS_BUT_EMPTY = {
    "success": True,
    "query": "zone 2 rent",
    "results": _WEB_SEARCH_PLACEHOLDER_BLOB,
    "detailed_data": {"web_search_1": _PLACEHOLDER_SNIPPET},
}
# The truthful web_search error shape (post-fix / simple path).
_WEB_SEARCH_ERROR = {
    "success": False, "error": "No search results found",
    "query": "zone 2 rent", "results": "", "detailed_data": {},
}
# A genuine, usable web_search return.
_WEB_SEARCH_VALID = {
    "success": True,
    "query": "zone 2 rent",
    "results": ("[1] Title: Average Zone 2 rents 2026\n"
                "    Link: https://example.co.uk/rents\n"
                "    Summary: One-beds average about £1,800 pcm."),
    "detailed_data": {"web_search": "Zone 2 one-beds ~£1,800 pcm."},
}


@pytest.mark.parametrize("evidence, expected", [
    # unusable — empty / missing
    (None, False),
    ("", False),
    ("   \n  ", False),
    ([], False),
    ({}, False),
    # unusable — placeholder strings the search stack really emits
    (_PLACEHOLDER_SNIPPET, False),
    ("Could not retrieve search information.", False),
    ("No rent price information found.", False),
    (_WEB_SEARCH_PLACEHOLDER_BLOB, False),
    ([_PLACEHOLDER_SNIPPET, ""], False),
    # unusable — dict shapes
    (_WEB_SEARCH_ERROR, False),
    (_WEB_SEARCH_SUCCESS_BUT_EMPTY, False),          # success=True but only placeholders
    ({"success": True, "results": [], "detailed_data": {}}, False),  # zero-entry set
    # usable — real content
    (_WEB_SEARCH_VALID, True),
    ("[1] Title: X\n    Summary: rent about £1,800 pcm.", True),
    ({"summary": "Zone-2 one-beds ~£1,800 pcm."}, True),
    ({"recommendations": [{"price": "£1,500 pcm"}]}, True),
    (1800, True),
])
def test_evidence_usable_truth_table(evidence, expected):
    assert evidence_usable(evidence) is expected


def test_evidence_usable_unwraps_fc_artifacts():
    # Artifact wrapper: success flag and raw_data content both respected.
    assert evidence_usable(_artifact("web_search", _WEB_SEARCH_VALID)) is True
    assert evidence_usable(_artifact("web_search", None, success=False)) is False
    # success=True at the wrapper but a placeholder payload -> still unusable.
    assert evidence_usable(_artifact("web_search", _WEB_SEARCH_SUCCESS_BUT_EMPTY)) is False


# ── 6. H3 end-to-end: the exact SearXNG-down shape triggers the hard replace ──
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_h3_web_search_placeholder_success_true_is_hard_replaced(monkeypatch, lang):
    """The live H3 defect: web_search returns success=True with a placeholder blob
    (SearXNG down), the model fabricates a Zone-2 rent table. The deterministic 兜底
    must now fire because evidence_usable rejects the placeholder as unusable."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("Approximately £1,750 pcm on average.")  # regeneration still numeric
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    # success=True at BOTH artifact and payload level — the exact mislabelled shape.
    art = _artifact("web_search", dict(_WEB_SEARCH_SUCCESS_BUT_EMPTY), success=True)
    state = _fc_state(_H3_ANSWER, [art], reply_language=lang)
    update = _run(lga._make_critic_node()(state))

    # fc turn -> artifact-grounded fallback replaces the fabricated table (still a hard replace).
    from core.agent_loop import _artifact_grounded_fallback_answer
    assert update["final_response"] == _artifact_grounded_fallback_answer(
        state, reason="no_reliable_numbers")
    assert update["final_response"] != no_reliable_data_message(lang)   # not the generic template
    assert CAVEAT not in update["final_response"]
    assert has_specific_price_claims(update["final_response"]) is False


# ── 7. no-evidence 兜底 variant selection: fc artifact-grounded vs legacy template ──
_CE_MSG = "any 1-bed in Islington, how is the safety there?"


def _completed_empty_search_artifact():
    """A search_properties artifact that COMPLETED and matched zero listings (retrieval tool,
    but NOT usable evidence) — the CR5 cold shape that drives the 兜底."""
    return _artifact(
        "search_properties",
        {"status": "no_results", "recommendations": [],
         "search_criteria": {"bedrooms": 1, "area": "islington"}},
        success=True)


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_no_evidence_fallback_fc_uses_artifact_grounded(monkeypatch, lang):
    """fc turn (tool_artifacts present) whose only retrieval finished EMPTY and whose answer
    fabricates a price: the critic's no-evidence 兜底 replaces it with the ARTIFACT-GROUNDED
    fallback (names the completed-empty 1-bed search) — NOT the generic legacy template — and
    the replacement itself asserts no price (cannot re-trigger the branch)."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("About £1,650 pcm on average.")  # regeneration still numeric/ungrounded
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    from uk_rent_agent.agent.state import create_initial_state
    state = create_initial_state(
        _CE_MSG,
        extracted_context={"current_message": _CE_MSG, "reply_language": lang})
    state["tool_artifacts"] = [_completed_empty_search_artifact()]
    state["final_response"] = "1-bed flats in Islington are about £1,650 pcm."  # fabricated

    update = _run(lga._make_critic_node()(state))
    final = update["final_response"]

    # artifact-grounded, NOT the generic legacy template
    assert final != no_reliable_data_message(lang)
    assert "1-bed" in final                              # names the completed-empty room type
    assert "Islington" in final
    from core.agent_loop import _artifact_grounded_fallback_answer
    assert final == _artifact_grounded_fallback_answer(state, reason="no_reliable_numbers")
    # sanity guard: the replacement makes no price claim -> the branch cannot re-fire on it
    assert has_specific_price_claims(final) is False
    # no time-budget framing (the turn did not time out)
    for banned in ("cut short", "time budget", "ran long", "超时", "时间限制"):
        assert banned not in final


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_no_evidence_fallback_legacy_keeps_generic_template(monkeypatch, lang):
    """Legacy arch (NO tool_artifacts) with no usable evidence and a fabricated price keeps the
    generic no_reliable_data_message verbatim — the artifact-grounded variant is fc-only."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("About £1,650 pcm on average.")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    from uk_rent_agent.agent.state import create_initial_state
    state = create_initial_state(
        _CE_MSG,
        extracted_context={"current_message": _CE_MSG, "reply_language": lang})
    state["tool_decision"] = {"tool": "search_properties"}  # retrieval_expected via legacy decision
    state["tool_raw_data"] = None                            # no usable evidence
    state["final_response"] = "1-bed flats in Islington are about £1,650 pcm."  # fabricated
    # no tool_artifacts -> legacy branch

    update = _run(lga._make_critic_node()(state))
    assert update["final_response"] == no_reliable_data_message(lang)  # generic template kept


def test_h3_valid_web_search_evidence_still_passes(monkeypatch):
    """Guardrail: a genuine web_search payload with grounded figures is NOT replaced —
    the 兜底 is scoped strictly to the no-usable-evidence case."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()

    fake = _FakeLLM("should not be called")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    art = _artifact("web_search", dict(_WEB_SEARCH_VALID))
    original = "Zone-2 one-beds average about £1,800 pcm."  # grounded in the payload
    state = _fc_state(original, [art])
    update = _run(lga._make_critic_node()(state))

    assert fake.calls == 0                                            # grounded -> no regen
    assert update.get("final_response", original) == original
    assert update.get("final_response", original) != no_reliable_data_message("en")
