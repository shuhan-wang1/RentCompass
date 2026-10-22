"""The critic and the evidence-scope pass in linear time (r6e follow-up, the B-01 class).

Measured on the code before this change (the same machine, one call each):

* ``unsupported_external_numbers``: 81 KB of "£2,700 + £1,950 = £4,650, " ~240 s (the price
  check re-scanned the reply once per figure); "£" + "1," * 12 500 34 s (every comma restarted
  the number patterns); 25 KB of "about 3 hours a month, " 11 s and of "Stratford 20 minutes
  versus Bow 27 minutes, " 12 s (each figure re-read its whole sentence: the money blanking,
  the stated days, the compared routes, the key of the whole prefix); a route named once
  ahead of 75 KB of figures 9 s.
* ``unsupported_reply_prices``: "£2,350" + 50 KB of blanks + "a month 8 minutes" ~20 s (the
  multiplication pattern split the blank run every way); a 60 KB reply of figures no pool
  holds against a 60 KB message of prices 26 s (every figure scanned the whole pool).
* ``validate_evidence_scope``: 130 KB of short sentences 18 s (each full stop searched the
  whole prefix for an abbreviation).

The limits are generous for the fixed code (all of these now take well under half a second)
and far below what the old code took. Replies longer than ``CRITIC_MAX_CHARS`` fail closed,
so the external-number shapes are measured just under it.
"""
import json
import pathlib
import random
import re
import time

import pytest

from core.candidate_validation import validate_evidence_scope
from core.langgraph_agent import _collect_grounding_evidence
from uk_rent_agent.agent import commute_arithmetic as arithmetic
from uk_rent_agent.agent.critic import (
    CRITIC_MAX_CHARS, _TIMES_STEP, unsupported_external_numbers, unsupported_reply_prices,
)

DATA = pathlib.Path(__file__).parent / "data"
CORPUS = json.loads((DATA / "r6_r21_corpus.json").read_text(encoding="utf-8"))
COMMUTE, DEPOSIT, CACHED = CORPUS["r21_8f4f0a00"], CORPUS["r21_63fb6f8a"], CORPUS["r21_0136f2f6"]
LIMIT = 2.0


def _repeat(unit: str, size: int) -> str:
    return unit * (size // len(unit))


def _seconds(call) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


_EXTERNAL = {
    "money-sums": lambda n: _repeat("£2,700 + £1,950 = £4,650, ", n),
    "hours-totals": lambda n: _repeat("about 3 hours a month, ", n),
    "side-by-side": lambda n: _repeat("Stratford 20 minutes versus Bow 27 minutes, ", n),
    "route-named-once": lambda n: "Stratford: " + _repeat("the commute is 20 minutes and saves 7 minutes, ", n - 11),
    "no-route-named": lambda n: _repeat("the commute is 20 minutes and saves 7 minutes, ", n),
    "distinct-figures": lambda n: ("Each week the commute adds up to "
                                   + "".join(f"{100 + i} minutes, " for i in range(n // 13))),
    "digit-commas": lambda n: "£" + "1," * (n // 2),
}


@pytest.mark.parametrize("label", sorted(_EXTERNAL))
def test_the_external_number_check_is_linear(label):
    text = _EXTERNAL[label](CRITIC_MAX_CHARS - 2_000)
    evidence = _collect_grounding_evidence(COMMUTE["state"], "")
    assert _seconds(lambda: unsupported_external_numbers(text, evidence, COMMUTE["message"])) < LIMIT, label


_PRICES = {
    "money-sums": lambda n: _repeat("£2,700 + £1,950 = £4,650, ", n),
    "blank-run": lambda n: "£2,350" + " " * n + "a month 8 minutes",
    "digit-commas": lambda n: "£" + "1," * (n // 2),
    "many-prices": lambda n: "".join(f"£{1000 + i % 9000:,} pcm. " for i in range(n // 12)),
}


@pytest.mark.parametrize("label", sorted(_PRICES))
def test_the_reply_price_check_is_linear(label):
    text = _PRICES[label](100_000)
    evidence = _collect_grounding_evidence(DEPOSIT["state"], "")
    assert _seconds(lambda: unsupported_reply_prices(text, [evidence, DEPOSIT["message"]])) < LIMIT, label


def test_a_long_reply_against_a_long_message_of_prices_is_linear():
    message = "".join(f"£{1000 + 7 * i:,} pcm and {10 + i} minutes, " for i in range(2_000))       # ~57 KB
    reply = "".join(f"£{90000 + 13 * i:,}.37 pcm, {5000 + i} minutes. " for i in range(2_000))      # ~61 KB
    evidence = _collect_grounding_evidence(COMMUTE["state"], "")
    assert _seconds(lambda: unsupported_reply_prices(reply, [evidence, message])) < LIMIT
    short = reply[:CRITIC_MAX_CHARS - 2_000]
    assert _seconds(lambda: unsupported_external_numbers(short, evidence, message)) < LIMIT


_SCOPE = {
    "short-sentences": lambda n: _repeat("First, the price is live. Second, it is not a live check. "
                                         "Two things to keep in mind. ", n),
    "brackets": lambda n: _repeat("(a) ", n),
    "abbreviations": lambda n: _repeat("St. a. ", n),
}


@pytest.mark.parametrize("label", sorted(_SCOPE))
def test_the_evidence_scope_pass_is_linear(label):
    text = _SCOPE[label](100_000)
    assert _seconds(lambda: validate_evidence_scope(text, CACHED["state"])) < LIMIT, label


# ── fail closed, never open ───────────────────────────────────────────────────

def test_a_64_character_figure_in_a_reply_is_unsupported():
    evidence = _collect_grounding_evidence(COMMUTE["state"], "")
    draft = COMMUTE["raw_draft"]
    assert not unsupported_external_numbers(draft, evidence, COMMUTE["message"])
    assert unsupported_external_numbers(draft + "\n\nReference " + "1," * 32, evidence, COMMUTE["message"])
    assert not unsupported_external_numbers(draft + "\n\nReference " + "1," * 31, evidence, COMMUTE["message"])
    deposit = _collect_grounding_evidence(DEPOSIT["state"], "")
    assert unsupported_reply_prices(DEPOSIT["raw_draft"], [deposit, DEPOSIT["message"]]) == []
    assert unsupported_reply_prices(DEPOSIT["raw_draft"] + "\n\nFlat " + "9" * 64,
                                    [deposit, DEPOSIT["message"]]) == [float("9" * 64)]


def test_a_long_figure_in_the_message_evidences_nothing():
    evidence = _collect_grounding_evidence(COMMUTE["state"], "")
    message = COMMUTE["message"] + " " + "1," * 20_000 + " 25 minutes"
    # the run is skipped, the figure after it still counts as the user's
    assert not unsupported_external_numbers(COMMUTE["raw_draft"], evidence, message)
    wrong = COMMUTE["raw_draft"].replace("**Bow — 27 minutes each way**", "**Bow — 111 minutes each way**")
    assert unsupported_external_numbers(wrong, evidence, message)


def test_a_reply_beyond_the_backstop_is_unsupported():
    evidence = _collect_grounding_evidence(COMMUTE["state"], "")
    draft = COMMUTE["raw_draft"]
    padding = "\n\n" + _repeat("This paragraph adds nothing. ", CRITIC_MAX_CHARS)
    assert not unsupported_external_numbers((draft + padding)[:CRITIC_MAX_CHARS], evidence, COMMUTE["message"])
    assert unsupported_external_numbers(draft + padding, evidence, COMMUTE["message"])


# ── the rewritten scans answer as the old ones did ───────────────────────────

_OLD_TIMES_STEP = re.compile(
    r"(?<![\d.])£?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(?:[a-z ]{0,20}?)\s*([×x*÷/])\s*£?\s*(\d[\d,]*(?:\.\d{1,2})?)"
    r"(?:\s*[a-z ]{0,20}?)\s*=\s*\**\s*£\s*(\d[\d,]*(?:\.\d{1,2})?)", re.I)


def test_the_possessive_multiplication_step_finds_the_same_steps():
    pieces = ["£", " ", "  ", "\t", "1", "50", "1,950", ".", ".5", "x", "X", "×", "*", "÷", "/", "=", "**",
              "per", " month ", "a", ",", "(", "days"]
    rng = random.Random(1009)
    for _ in range(3_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 24)))
        old = [(m.groups(), m.end()) for m in _OLD_TIMES_STEP.finditer(text)]
        new = [(m.groups(), m.end()) for m in _TIMES_STEP.finditer(text)]
        assert old == new, text


def _old_route_before(text, end, routes):
    best, best_at = None, -1
    head = f" {arithmetic._key(text[:end])} "
    for route in routes:
        for label in route["labels"]:
            key = arithmetic._key(label)
            at = head.rfind(f" {key} ") if key else -1
            if at > best_at:
                best, best_at = route, at
    return best


def test_the_route_named_last_is_the_one_the_whole_prefix_named():
    routes = [{"labels": ["Bow", "Bow, London"], "minutes": 27}, {"labels": ["Stratford"], "minutes": 20},
              {"labels": ["Bow Road"], "minutes": 25}, {"labels": ["King's Cross"], "minutes": 9}]
    pieces = ["Bow", "bow", " ", ",", "'s", "’s", "'", "Road", "Stratford", "King", "Kings", "Cross", "-",
              "\n", "20", "min", "é", "_", "通勤", "，", ".", "(", "Bowl", "s"]
    rng = random.Random(2027)
    for _ in range(600):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 20)))
        for end in range(len(text) + 2):
            assert _old_route_before(text, end, routes) is arithmetic._route_before(text, end, routes), (text, end)
