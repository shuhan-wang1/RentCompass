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
