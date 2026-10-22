"""r6f: a caveat line ships once (r23 de60c6a8, Chinese availability turn).

``tests/data/r6_r23_corpus.json`` holds the r23 inst-D state (ids scrubbed), the model's draft,
the checkpointed final (draft, then the ledger ending with "房源来自缓存…"), and the shipped text.
After the checkpoint, app.py ran the scope pass, which withdrew the advice "要确认是否仍在出租，需要
直接向发布该房源的中介核实。" as a live claim and appended its cache note and details block — whose
last line is the same "房源来自缓存…" caveat, so the reply ended with it twice.
"""
import json
import pathlib

import pytest

from core.candidate_validation import dedupe_caveat_lines, validate_evidence_scope

DATA = pathlib.Path(__file__).parent / "data"
CASE = json.loads((DATA / "r6_r23_corpus.json").read_text(encoding="utf-8"))["r23_de60c6a8"]
CACHE_ZH = "房源来自缓存，价格和出租状态可能已变化；请联系中介核实。"
CACHE_EN = "These listings are cached; prices and availability may have changed. Confirm with the agent."
ADVICE = "要确认是否仍在出租，需要直接向发布该房源的中介核实。"


def test_the_advice_to_ask_whether_it_is_still_let_is_kept():
    final = CASE["checkpointed_final"]
    assert ADVICE in final and final.count(CACHE_ZH) == 1
    assert validate_evidence_scope(final, CASE["state"]) == final


@pytest.mark.parametrize("claim", ["该房源目前仍在出租。", "该房源仍在出租，是否需要我帮你预约看房？"])
def test_a_chinese_live_claim_beside_a_question_still_goes(claim):
    final = CASE["checkpointed_final"]
    out = validate_evidence_scope(final.replace(ADVICE, ADVICE + claim), CASE["state"])
    assert claim not in out and ADVICE in out


def test_the_shipped_reply_carries_the_cache_caveat_once():
    shipped = CASE["shipped"]
    assert shipped.count(CACHE_ZH) == 2
    out = dedupe_caveat_lines(shipped)
    assert out.count(CACHE_ZH) == 1
    assert out.index(CACHE_ZH) == shipped.index(CACHE_ZH)          # the first place is kept
    assert "返回的房源详情：Jacob House" in out and "可入住信息：Contact agent。当前是否仍可租须向中介核实。" in out


def test_english_caveats_once_fields_and_items_untouched():
    text = "\n".join([
        "Two listings matched.", "", CACHE_EN, "",
        "Returned listing details: A Street, London; £1,900 pcm.",
        "Availability: Contact agent. Confirm current availability with the agent.",
        CACHE_EN,
        "Returned listing details: B Street, London; £1,950 pcm.",
        "Availability: Contact agent. Confirm current availability with the agent.",
        CACHE_EN,
        "- Confirm availability with the agent before you book.",
        "- Confirm availability with the agent before you book.",
    ])
    out = dedupe_caveat_lines(text)
    assert out.count(CACHE_EN) == 1
    assert out.count("Availability: Contact agent. Confirm current availability with the agent.") == 2
    assert out.count("- Confirm availability with the agent before you book.") == 2


def test_a_reply_without_repeats_is_returned_as_is():
    assert dedupe_caveat_lines(CASE["checkpointed_final"]) == CASE["checkpointed_final"]
    assert dedupe_caveat_lines("Stratford 20 min.\n\nStratford 20 min.") == "Stratford 20 min.\n\nStratford 20 min."


@pytest.mark.parametrize("claim,kept", [
    ("如果房源还在出租，你想去看看吗？", True),        # a condition and a question
    ("这套房还在出租吗？", True),
    ("该房源目前仍在出租，你想去看看吗？", False),    # a statement, then the question
])
def test_a_chinese_question_asserts_only_what_it_states(claim, kept):
    final = CASE["checkpointed_final"]
    out = validate_evidence_scope(final.replace(ADVICE, ADVICE + claim), CASE["state"])
    assert (claim in out) is kept
