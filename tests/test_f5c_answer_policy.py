"""Round-5 answer-policy fixes.

* P1-6 — the commute-verification template must be an APPENDED limitation, never a
  replacement. Live it shipped 71 chars where the same turn had produced 1086 chars of TfL
  legs (`B-C6`), 15 chars where it had produced 443 (`B-C12`), and it fired on the fc arm
  too (`A-D12`, 71 chars against B's 1562).
* P1-7 — `critic.verdict … issues=['empty_answer']` shipped a 0-char HTTP 200
  (`agenttest-t2-a5-fc`, reproduced on a second turn).
* P1-8 — `stage=regenerated grounded=False issues=['unsupported_external_numbers']` shipped
  as-is on both architectures.
"""

from __future__ import annotations

import asyncio

import pytest

from core.candidate_validation import validate_commute_response
import core.langgraph_agent as lga
import core.llm_config as llm_config
from tests.test_fc_critic import _FakeLLM, _artifact, _fc_state


def _load_local_core():
    """Direct imports rather than test_fc_critic's sys.modules surgery.

    ``pythonpath = ["src", "app"]`` already puts the real ``core`` package first, and
    rewriting ``sys.modules`` mid-session is a documented source of order-dependent
    failures in this suite."""
    return llm_config, lga


# ── P1-6: the commute template is appended, never a replacement ───────────────


FIXED_EN = "The commute condition could not be verified for the listing this round."
FIXED_ZH = "本轮无法核实该房源的通勤条件。"


def _commute_state(*, language="en", validation=None):
    return {
        "extracted_context": {
            "current_message": "how long is the commute from these to UCL?",
            "reply_language": language,
        },
        "candidate_validation": validation or {},
        "tool_artifacts": [],
    }


def test_a_mixed_answer_keeps_everything_except_the_unverified_commute():
    answer = (
        "Rugby House, 6 Great Ormond Street is £2,100/month, 2 bedrooms, available now.\n"
        "The commute to UCL is about 18 minutes, well within your 30 minute limit.\n"
        "Crime in WC1N is below the London average per data.police.uk."
    )
    out = validate_commute_response(answer, _commute_state())

    assert "Rugby House" in out and "data.police.uk" in out
    assert "18 minutes" not in out
    assert len(out) > len(FIXED_EN)


def test_a_pure_commute_answer_still_ships_the_listing_evidence_it_produced():
    """The worst live outcome: a 1086-char answer replaced by one sentence. The validated
    candidate ledger is structured tool output, so it can ship when the prose cannot."""
    validation = {
        "constraints": {"max_commute_minutes": 30},
        "statuses": [{"candidate": {"address": "12 Cedar Way", "price": "£1,900/month"},
                      "status": "unknown", "reasons": [],
                      "unknown_reasons": ["commute evidence is missing"],
                      "evidence_status": "missing"}],
        "eligible": [],
        "excluded": [],
        "unknown": [{"candidate": {"address": "12 Cedar Way", "price": "£1,900/month"},
                     "status": "unknown", "reasons": [],
                     "unknown_reasons": ["commute evidence is missing"],
                     "evidence_status": "missing"}],
    }
    # A SUBSTANTIAL answer that is nonetheless all commute prose: the live shape was
    # 1086 chars of TfL Journey Planner legs replaced by 71 chars.
    legs = ("It takes 18 minutes to UCL door to door.\n"
            "The return trip takes 21 minutes.\n"
            "The walk to the station takes 6 minutes.\n"
            "The Northern line leg takes 9 minutes.\n"
            "The final walk takes 4 minutes.\n"
            "Overall the commute is well inside your 30 minute limit.")
    assert len(legs) > 200
    out = validate_commute_response(legs, _commute_state(validation=validation))

    assert "12 Cedar Way" in out
    assert len(out) > 2 * len(FIXED_EN)
    assert FIXED_EN in out            # appended as the limitation
    assert not out.startswith(FIXED_EN)
    assert "18 minutes" not in out    # fail-closed on the figure is preserved


def test_a_short_single_claim_reply_keeps_the_bare_sentence():
    """A one-sentence commute claim has nothing else in it; the sentence IS the answer."""
    assert validate_commute_response(
        "It takes 18 minutes to UCL.", _commute_state()) == FIXED_EN


def test_the_bare_sentence_survives_only_when_the_turn_produced_nothing_else():
    out = validate_commute_response("It takes 18 minutes to UCL.", _commute_state())
    assert out == FIXED_EN


def test_the_zh_path_behaves_the_same():
    out = validate_commute_response("到 UCL 只要 18 分钟。",
                                    _commute_state(language="zh"))
    assert out == FIXED_ZH


def test_an_evidenced_commute_answer_is_untouched():
    state = _commute_state()
    state["tool_artifacts"] = [{
        "turn": 1, "tool": "calculate_commute", "success": True,
        "raw_data": {"duration_minutes": 18}, "params_digest": "d",
    }]
    answer = "Rugby House is 18 minutes from UCL."
    assert validate_commute_response(answer, state) == answer


# ── P1-7: an empty answer is a hard stop ──────────────────────────────────────


def test_an_empty_answer_is_regenerated_rather_than_shipped(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    fake = _FakeLLM("Chessington studios start around £850/month in the results above.")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    state = _fc_state("", [_artifact("search_properties",
                                     {"recommendations": [{"price": "£850/month"}]})])
    update = asyncio.run(lga._make_critic_node()(state))

    assert fake.calls >= 1
    assert update["final_response"].strip()
    assert "£850" in update["final_response"]


def test_an_empty_answer_with_no_regeneration_budget_still_never_ships_empty(monkeypatch):
    """A soft-wrapped turn gets no LLM repair — and must still not return a 0-char 200."""
    pytest.importorskip("langgraph")
    _llm_config, lga = _load_local_core()

    state = _fc_state("", [])
    state["soft_wrapped"] = True
    update = asyncio.run(lga._make_critic_node()(state))

    assert update["final_response"].strip()


def test_an_empty_answer_falls_back_to_the_artifacts_when_regeneration_fails(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: _FakeLLM(""))

    state = _fc_state("", [_artifact("search_properties",
                                     {"status": "found",
                                      "recommendations": [{"address": "12 Cedar Way",
                                                           "price": "£850/month"}]})])
    update = asyncio.run(lga._make_critic_node()(state))

    assert update["final_response"].strip()


def test_a_non_empty_answer_is_not_disturbed_by_the_empty_answer_policy(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    fake = _FakeLLM("should not be called")
    monkeypatch.setattr(llm_config, "get_react_llm", lambda *a, **k: fake)

    art = _artifact("web_search", {"summary": "Typical Zone-2 rent is £1,500 pcm."})
    state = _fc_state("Zone-2 rents are around £1,500 pcm.", [art])
    update = asyncio.run(lga._make_critic_node()(state))

    assert fake.calls == 0
    assert update.get("final_response", state["final_response"]).strip()


# ── P1-8: a still-ungrounded regeneration ships marked ────────────────────────


def test_unsupported_external_numbers_are_labelled_unverified(monkeypatch):
    """Both arches shipped `grounded=False issues=['unsupported_external_numbers']` with
    only the generic double-check caveat. The figures the critic could not tie to this
    turn's evidence are the finding, so they are named as unverified."""
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    # The regeneration is ALSO ungrounded, which is exactly the live shape.
    monkeypatch.setattr(
        llm_config, "get_react_llm",
        lambda *a, **k: _FakeLLM("The journey to UCL takes 42 minutes."))

    art = _artifact("search_properties", {"recommendations": [{"price": "£850/month"}]})
    state = _fc_state("The journey to UCL takes 37 minutes.", [art])
    update = asyncio.run(lga._make_critic_node()(state))

    assert update["verdict"]["grounded"] is False
    assert "Unverified" in update["final_response"]


def test_the_unverified_label_is_localised(monkeypatch):
    pytest.importorskip("langgraph")
    llm_config, lga = _load_local_core()
    monkeypatch.setattr(
        llm_config, "get_react_llm",
        lambda *a, **k: _FakeLLM("该行程需要 42 分钟。"))

    art = _artifact("search_properties", {"recommendations": [{"price": "£850/month"}]})
    state = _fc_state("该行程需要 37 分钟。", [art], reply_language="zh")
    update = asyncio.run(lga._make_critic_node()(state))

    assert "未核实" in update["final_response"]
