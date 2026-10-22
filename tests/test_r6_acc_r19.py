"""acc-r19 (round 6c, output): a step on a withdrawn figure.

``tests/data/r6_r19_corpus.json`` is exported from the frozen r19 inst-D checkpoints:

* r19_de1fba15 "I go into the office 3 days a week … Stratford and Bow …" — calculate_commute
  Stratford 20, Bow 28; office days 3. The draft's "Saving" table (rows one-way / office day /
  week / month, columns Stratford / Bow / Saving) is all correct, yet the critic flagged it and
  the redactor shipped "| Per week (3 office days) | — | — | — |" and dropped the day row.

The chained-step cases use the r17 8c35fd5b state (Stratford 19, Bow 27, 3 office days).
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_commute_response
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent.critic import evaluate_grounding, unsupported_external_numbers

DATA = pathlib.Path(__file__).parent / "data"
CORPUS = json.loads((DATA / "r6_r19_corpus.json").read_text(encoding="utf-8"))
R17 = json.loads((DATA / "r6_r17_corpus.json").read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
ROWS = ("| Per one-way trip | 20 min | 28 min | 8 min |",
        "| Per office day (return) | 40 min | 56 min | 16 min |",
        "| Per week (3 office days) | 2 h 00 min | 2 h 48 min | **48 min** |",
        "| Per month (approx. 13 office days) | 8 h 40 min | 12 h 08 min | **about 3 h 28 min** |")


def _case(name="r19_de1fba15"):
    case = CORPUS[name]
    return case, case["state"], _collect_grounding_evidence(case["state"], ""), case["message"]


# ── a step on a withdrawn figure goes with it ─────────────────────────────────────────

R17_LINE = "- Per day: 8 minutes each way, so 16 minutes a day"


@pytest.mark.parametrize("line,kept", [
    ("- Per day: 8 minutes each way, so 16 minutes a day, 16 × 3 = 48 a week", True),
    ("- Per day: 8 minutes each way, so 16 minutes a day, 16 × 3 = 48 minutes a week", True),
    ("- Per day: 19 minutes each way, so 38 minutes a day, 38 x 3 = 114 a week", False),   # r19 W3b
    ("- Per day: 8 minutes each way, so 16 minutes a day, 16 × 3 = 52 a week", False),
])
def test_a_chained_step_stands_or_falls_with_its_operand(line, kept):
    case = R17["r17_8c35fd5b"]
    state = case["state"]
    text = case["raw_draft"].replace(R17_LINE, line)
    out = validate_commute_response(text, state)
    flagged = unsupported_external_numbers(text, _collect_grounding_evidence(state, ""), case["message"])
    if kept:
        assert line in out and NOTE_EN not in out and not flagged
    else:
        assert "114" not in out and "52 a week" not in out
        assert "- Per day: 38" not in out          # no "Per day:" label carrying a weekly step
        assert out.count(NOTE_EN) == 1 and flagged
