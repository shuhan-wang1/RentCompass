"""R3-01: the commute redactor's PRECISION, on real drafts and states from rounds r04/r05.

The policy is unchanged (an unsupported commute claim is removed). What these pin down is
that a removal (1) leaves no fragment, (2) never strips a figure this turn's tools evidence,
(3) never touches non-commute or system-composed text, and (4) adds the note only when
visible text actually went — once.

The corpus (``tests/data/overnight_r03_commute_redactor_corpus.json``) holds each turn's
pre-validation draft plus the state keys the validator reads, exported from the frozen
round checkpoints.
"""
import json
import pathlib
import re

import pytest

from core.agent_loop import (
    AUTOMATIC_MEMORY_DISCLOSURE_EN,
    AUTOMATIC_MEMORY_DISCLOSURE_ZH,
    disclose_automatic_memory,
)
from core.candidate_validation import validate_commute_response

CORPUS = json.loads((pathlib.Path(__file__).parent / "data" /
                     "overnight_r03_commute_redactor_corpus.json").read_text(encoding="utf-8"))
NOTE_EN = "Note: some commute claims could not be verified and have been removed from this answer."
NOTE_ZH = "注：部分通勤结论缺少可对应的证据，已从回答中移除。"


def _case(name):
    case = CORPUS[name]
    return case["draft"], case["state"]


def _balanced(text):
    return all(text.count(o) == text.count(c) for o, c in (("(", ")"), ("（", "）")))


# ── property 1: no fragments ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["r2_01c_unclosed_bracket", "r05_zh_bracket_fragment",
                                  "r05_zh_list_item_fragment", "r05_en_bracket_fragment"])
def test_removal_leaves_no_unbalanced_bracket(name):
    draft, state = _case(name)
    out = validate_commute_response(draft, state)
    assert _balanced(out), out
    for line in out.splitlines():
        assert _balanced(line), line


def test_unclosed_bracket_case_drops_the_whole_sentence():
    """R2-01c: "(King's Cross St Pancras to Canary Wharf, I have not verified that …" shipped
    with its bracket open. The unsupported 25-35 minutes go, and so does the sentence that
    carried them, rather than a broken remainder."""
    draft, state = _case("r2_01c_unclosed_bracket")
    out = validate_commute_response(draft, state)
    assert "25 to 35 minutes" not in out
    assert "I have not verified that" not in out
    assert "(King's Cross St Pancras to Canary Wharf" not in out
    # the measured-estimate paragraph and the offer are untouched
    assert "roughly 34 to 51 minutes" in out
    assert "I can try the journey again from that exact point" in out


def test_chinese_bracket_fragment_is_gone():
    draft, state = _case("r05_zh_bracket_fragment")
    out = validate_commute_response(draft, state)
    assert "并非实际查询到的行程时间）" not in out
    assert "是一套一居室整租公寓（1 bed flat）。" in out
    assert "20 分钟" not in out


def test_chinese_list_item_does_not_keep_a_tail():
    draft, state = _case("r05_zh_list_item_fragment")
    out = validate_commute_response(draft, state)
    assert "到 UCL 的通勤：并非" not in out
    assert "33 分钟" not in out
    assert "价格：£875/月（在预算内，且包含全部账单和网络）" in out


def test_english_list_item_ends_cleanly():
    draft, state = _case("r05_en_bracket_fragment")
    out = validate_commute_response(draft, state)
    assert "not a verified journey time)." not in out
    assert "1. Highgate West Hill, London, N6 — £2,000/month, 1-bed flat." in out
    assert "28 minutes" not in out and "24 minutes" not in out


# ── property 2: an evidenced figure is never stripped ───────────────────────────────

def test_tfl_leg_minutes_from_route_legs_are_kept():
    """R2-01a: legs 6/5/7/5 are route_legs[].duration_minutes of the planner's journey."""
    draft, state = _case("r2_01a_leg_minutes")
    out = validate_commute_response(draft, state)
    for leg in ("1. Northern line from King's Cross St. Pancras to Moorgate — 6 min",
                "2. Walk to Liverpool Street — 5 min",
                "3. Elizabeth line from Liverpool Street to Canary Wharf — 7 min",
                "4. Walk to Canary Wharf station — 5 min"):
        assert leg in out, leg
    assert out == draft
    assert NOTE_EN not in out


def test_alternative_itinerary_figure_is_kept():
    """"A slightly slower alternative is about 40 minutes" quotes the planner's second
    itinerary (37 vs 40); "slower" compares the two routes, it is no verdict on a limit."""
    draft, state = _case("r04_alternative_route")
    out = validate_commute_response(draft, state)
    assert "A slightly slower alternative is about 40 minutes: bus 69 to Drapers Field" in out
    assert out == draft
    assert NOTE_EN not in out


# ── property 3: non-commute and system text is out of scope ─────────────────────────

@pytest.mark.parametrize("disclosure", [AUTOMATIC_MEMORY_DISCLOSURE_EN, AUTOMATIC_MEMORY_DISCLOSURE_ZH])
def test_fixed_memory_disclosure_is_never_redacted(disclosure):
    _, state = _case("r2_19_disclosure")
    assert validate_commute_response(disclosure, state) == disclosure


def test_disclosure_survives_a_second_validation_pass():
    """The plain-answer path validates the final text again after the disclosure was
    appended (agent_loop ``response = …validate_commute_response(final_response, state)``)."""
    draft, state = _case("r2_19_disclosure")
    first = validate_commute_response(draft, state)
    with_disclosure = disclose_automatic_memory(
        first, "Be honest: do you store things about me even when I haven't asked you to?", "en")
    assert AUTOMATIC_MEMORY_DISCLOSURE_EN in with_disclosure
    second = validate_commute_response(with_disclosure, state)
    assert AUTOMATIC_MEMORY_DISCLOSURE_EN in second
    assert "(such as budget, across conversations." not in second
    assert NOTE_EN not in second
    assert validate_commute_response(second, state) == second


def test_word_commute_without_a_claim_is_out_of_scope():
    _, state = _case("r2_19_disclosure")
    text = ("We store facts such as budget, areas or commute and form inferences about your "
            "priorities. Your commute preference is noted.")
    assert validate_commute_response(text, state) == text


# ── property 4: accurate note ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(CORPUS))
def test_note_only_when_visible_text_was_removed_and_never_twice(name):
    draft, state = _case(name)
    out = validate_commute_response(draft, state)
    notes = out.count(NOTE_EN) + out.count(NOTE_ZH)
    assert notes <= 1
    body = out.replace(NOTE_EN, "").replace(NOTE_ZH, "")
    visible = lambda t: re.sub(r"[^0-9A-Za-z一-鿿£]", "", t)  # noqa: E731
    if visible(body) == visible(draft):
        assert notes == 0, name
    # idempotent, and a second pass never adds a second note
    again = validate_commute_response(out, state)
    assert again == out


def test_note_present_before_is_kept_once():
    draft, state = _case("r05_en_bracket_fragment")
    out = validate_commute_response(draft, state)
    assert out.count(NOTE_EN) == 1
    twice = validate_commute_response(out + "\n\nPlease double-check the exact prices.", state)
    assert twice.count(NOTE_EN) == 1
