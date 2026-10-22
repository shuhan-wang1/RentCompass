"""acc-r19 (round 6c, output): a period table judged cell by cell; a step on a withdrawn figure.

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


def test_the_recorded_saving_table_ships_whole_and_grounded():
    case, state, evidence, message = _case()
    text = case["raw_draft"]
    assert all(row in text for row in ROWS)
    assert validate_commute_response(text, state) == text
    assert not unsupported_external_numbers(text, evidence, message)
    assert evaluate_grounding(text, evidence, retrieval_expected=True, tool_errored=False,
                              user_text=message).grounded


@pytest.mark.parametrize("row,wrong", [
    (ROWS[2], "| Per week (3 office days) | 2 h 00 min | 2 h 48 min | **60 min** |"),     # saving
    (ROWS[2], "| Per week (3 office days) | 2 h 10 min | 2 h 48 min | **48 min** |"),     # route cell
    (ROWS[3], "| Per month (approx. 13 office days) | 8 h 40 min | 12 h 08 min | **about 5 h** |"),
    (ROWS[0], "| Per one-way trip | 20 min | 28 min | 9 min |"),
    (ROWS[1], "| Per office day (return) | 40 min | 56 min | 18 min |"),
])
def test_a_wrong_cell_takes_its_row_never_a_placeholder(row, wrong):
    case, state, evidence, message = _case()
    text = case["raw_draft"].replace(row, wrong)
    out = validate_commute_response(text, state)
    assert wrong not in out
    assert all(other in out for other in ROWS if other != row)       # the true rows stay
    assert "| — |" not in out and "— |" not in out                    # no placeholder cells
    assert out.count(NOTE_EN) == 1
    assert unsupported_external_numbers(text, evidence, message)


def test_a_table_that_loses_every_row_goes_with_its_header():
    case, state, _, _ = _case()
    text = case["raw_draft"]
    for row in ROWS:
        cells = row.strip("|").split("|")
        text = text.replace(row, "|" + "|".join(cells[:-1]) + "| 99 min |")
    out = validate_commute_response(text, state)
    assert "| | Stratford | Bow | Saving |" not in out and "|---|" not in out
    assert "So Stratford saves you roughly **48 minutes a week**" in out


def test_an_entity_row_left_with_no_data_goes():
    """Outside period tables a withdrawn cell keeps its column ("| Hackney | 5.5 | £2,400 | — |",
    R2-01 d); a row left with nothing but placeholders is no row."""
    case = R17["r17_8c35fd5b"]
    table = "| Area | Commute |\n|---|---|\n| Stratford | 19 min |\n| Bow | 25 min |"
    text = case["raw_draft"].replace("Two caveats worth knowing:", table + "\n\nTwo caveats worth knowing:")
    out = validate_commute_response(text, case["state"])
    assert "| Stratford | 19 min |" in out
    assert "| Bow |" not in out and "25 min" not in out


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
