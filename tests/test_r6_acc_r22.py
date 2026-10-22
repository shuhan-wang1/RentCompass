"""acc-r22: the move-in pool's regression, arithmetic in words, the ledger, a cut enumeration.

``tests/data/r6_r22_corpus.json`` is exported from the frozen r22 inst-D checkpoints (and one
r20 one); ids scrubbed:

* r22_7f13d3b6 "What would a 6-week deposit and the first month's rent come to for a £1,950/month
  flat in Hackney? Find me one under £2,000 too." — the draft's "capped at five weeks' rent
  (£1,875 on this rent)" is wrong (5 x 1,950 x 12 / 52 = £2,250) and shipped unflagged: the
  move-in pool matched it, with the ±£10 tolerance, to the £875 flat share's 1,009.62 + 875.
* r22_4a7fe20c Stratford 20 / Bow 29 (ranking 28), 3 office days — the regeneration's correct
  "(18 minutes times 3 office days)" and "(54 minutes times 4 = 216 minutes)" were flagged and
  redacted; its first draft's period table runs the other way (periods as columns).
* r22_c9faf055 "帮我在Hackney找一个一居室，预算每月2000英镑，这些房源现在还能租吗？" — the grounded,
  honest answer was replaced by the candidate ledger, one cache line printed twice.
* r20_3745b76a "…find me a studio within 25 minutes of work" — the redactor's withdrawal of
  "… often give more options within a 25-minute commute" left "…such as Islington, Hoxton,
  Bethnal Green."
"""
import json
import pathlib

import pytest

from core.agent_loop import build_fc_nodes
from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import (
    _successful_evidence, unsupported_external_numbers, unsupported_reply_prices,
)

DATA = pathlib.Path(__file__).parent / "data"
CORPUS = json.loads((DATA / "r6_r22_corpus.json").read_text(encoding="utf-8"))


def _ctx(key):
    case = CORPUS[key]
    state = json.loads(json.dumps(case["state"]))
    return case, state, _collect_grounding_evidence(state, ""), case["message"]


# ── r22 7f13d3b6: a move-in figure is tied to its rent, at the precision written ──

def _prices(text, evidence, message):
    return unsupported_reply_prices(text, [_successful_evidence(evidence), message])


def test_the_wrong_five_week_cap_is_flagged_and_the_right_one_is_not():
    case, _state, evidence, message = _ctx("r22_7f13d3b6")
    draft = case["drafts"][-1]
    assert "(£1,875 on this rent)" in draft and "**Total: £4,650**" in draft
    assert _prices(draft, evidence, message) == [1875.0]
    assert unsupported_external_numbers(draft, evidence, message)
    right = draft.replace("(£1,875 on this rent)", "(£2,250 on this rent)")
    assert _prices(right, evidence, message) == []
    assert not unsupported_external_numbers(right, evidence, message)


@pytest.mark.parametrize("sentence,ok", [
    ("For the £875 flat share, a 5-week deposit and the first month come to £1,884.62.", True),
    ("For the £875 flat share, a 5-week deposit and the first month come to £1,885.", True),
    ("For the £875 flat share, a 5-week deposit and the first month come to about £1,880.", True),
    ("For the £875 flat share, a 5-week deposit and the first month come to £1,880.", False),
    ("For the £875 flat share, a 5-week deposit and the first month come to £1,875.", False),
    ("A 5-week deposit and the first month come to £1,884.62.", False),     # names no rent
])
def test_a_move_in_total_holds_only_for_the_rent_it_names(sentence, ok):
    case, _state, evidence, message = _ctx("r22_7f13d3b6")
    draft = case["drafts"][-1].replace("(£1,875 on this rent)", "(£2,250 on this rent)")
    text = draft.replace("Want me to check the commute", sentence + "\n\nWant me to check the commute")
    assert sentence in text
    flagged = _prices(text, evidence, message)
    assert (flagged == []) is ok, flagged


# ── r22 4a7fe20c: arithmetic in words, and a table turned round ──────────────

def test_the_regenerations_word_arithmetic_is_kept_and_grounded():
    case, state, evidence, message = _ctx("r22_4a7fe20c")
    regen = case["finals"][1]
    assert "(18 minutes times 3 office days)" in regen and "(54 minutes times 4 = 216 minutes)" in regen
    assert not unsupported_external_numbers(regen, evidence, message)
    assert validate_commute_response(regen, state) == regen


@pytest.mark.parametrize("working,ok", [
    ("(18 minutes times 3 office days)", True),
    ("(18 minutes multiplied by 3 days)", True),
    ("(18 minutes × 3 days a week)", True),
    ("(3 days × 18 minutes)", True),
    ("(18 minutes times 4 office days)", False),
    ("(20 minutes times 3 office days)", False),
])
def test_a_weekly_total_with_its_working(working, ok):
    case, state, evidence, message = _ctx("r22_4a7fe20c")
    regen = case["finals"][1]
    text = regen.replace("(18 minutes times 3 office days)", working)
    assert unsupported_external_numbers(text, evidence, message) is (not ok)
    assert (working in validate_commute_response(text, state)) is ok


@pytest.mark.parametrize("step,ok", [
    ("(54 minutes times 4 = 216 minutes)", True),
    ("(54 minutes multiplied by 4 = 216 minutes)", True),
    ("(54 minutes times 4 = 240 minutes)", False),
    ("(60 minutes times 4 = 240 minutes)", False),
])
def test_a_monthly_step_in_words(step, ok):
    case, state, evidence, message = _ctx("r22_4a7fe20c")
    regen = case["finals"][1]
    text = regen.replace("(54 minutes times 4 = 216 minutes)", step)
    assert unsupported_external_numbers(text, evidence, message) is (not ok)
    assert (step in validate_commute_response(text, state)) is ok


ROW = "| Stratford vs Bow | 18 min | 54 min | about 3 h 54 min |"


@pytest.mark.parametrize("row,ok", [
    (ROW, True),
    ("| Stratford vs Bow | 18 min | 60 min | about 3 h 54 min |", False),
    ("| Stratford vs Bow | 20 min | 54 min | about 3 h 54 min |", False),
    ("| Stratford vs Bow | 18 min | 54 min | about 5 h |", False),
])
def test_a_period_table_with_periods_as_columns(row, ok):
    case, state, _evidence, _message = _ctx("r22_4a7fe20c")
    draft = case["drafts"][1]
    assert ROW in draft
    assert (row in validate_commute_response(draft.replace(ROW, row), state)) is ok


# ── r22 c9faf055: a question the ledger does not answer keeps its answer ────────

class _NoTools:
    def list_specs(self):
        return []


CACHE_LINE = "房源来自缓存，价格和出租状态可能已变化；请联系中介核实。"


def _format(state, answer, message=None, **extra):
    state = dict(state, final_response=answer, **extra)
    if message is not None:
        state["extracted_context"] = dict(state["extracted_context"], current_message=message)
    return build_fc_nodes(_NoTools())["format_output_fc"](state)["final_response"]


def test_the_grounded_answer_leads_and_the_ledger_follows():
    case, state, _evidence, _message = _ctx("r22_c9faf055")
    draft = case["drafts"][-1]
    out = _format(state, draft)
    assert out.startswith(draft.strip())
    assert "满足全部已声明条件" in out[len(draft.strip()):]
    assert out.count(CACHE_LINE) == 1


@pytest.mark.parametrize("message", [
    "Find me a 1-bed in Hackney under £2,000. Are these still available?",
    "Find me a 1-bed in Hackney under £2,000 and compare them.",
    "帮我在Hackney找一个一居室，预算每月2000英镑，这几套哪个更好？",
])
def test_other_questions_the_ledger_cannot_answer(message):
    case, state, _evidence, _message = _ctx("r22_c9faf055")
    draft = case["drafts"][-1]
    assert _format(state, draft, message).startswith(draft.strip())


def test_a_plain_search_or_an_ungrounded_answer_keeps_the_ledger_alone():
    case, state, _evidence, _message = _ctx("r22_c9faf055")
    draft = case["drafts"][-1]
    plain = _format(state, draft, "帮我在Hackney找一个一居室，预算每月2000英镑")
    assert plain.startswith("满足全部已声明条件") and plain.count(CACHE_LINE) == 1
    ungrounded = _format(state, draft, verdict={"grounded": False, "issues": ["unsupported_prices"]})
    assert ungrounded.startswith("满足全部已声明条件") and ungrounded.count(CACHE_LINE) == 1
