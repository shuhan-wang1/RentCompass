"""The live-claim and listing-point checks of ``validate_evidence_scope`` (r6e follow-up).

``tests/data/r6_r6e_scope_corpus.json`` holds every recorded reply (r06-r21) from which the
pass withdrew a sentence before this change — all of them honest:

* advice and offers: "- Ask the agent whether the flat is still available and what the
  move-in date is.", "- I can also fetch live TfL fare and journey information …" (r07
  9f5e1f70 / 97b3d456), "- Is the property still available, and from what date?";
* Chinese caveats: "…没有实时核实。", "…未经实时核实…", "…实时租金请向中介确认。";
* the safety check's own reference: "…the geocoded centre of the area, not any particular
  street or home.", "…the EC1V postcode district centre, not the flat itself.",
  "…不是这套房源的具体门牌坐标", and offers to re-run it on the flat's coordinates.

A withdrawal also used to return the reply one sentence per line; paragraphs, list items and
line breaks now stay as written.
"""
import json
import pathlib

import pytest

from core.candidate_validation import validate_evidence_scope

DATA = pathlib.Path(__file__).parent / "data"
SCOPE = json.loads((DATA / "r6_r6e_scope_corpus.json").read_text(encoding="utf-8"))
R21 = json.loads((DATA / "r6_r21_corpus.json").read_text(encoding="utf-8"))
ADVICE = SCOPE["r07_advice_offer"]


@pytest.mark.parametrize("name", sorted(SCOPE))
def test_the_recorded_reply_ships_as_written(name):
    case = SCOPE[name]
    assert validate_evidence_scope(case["text"], case["state"]) == case["text"]


@pytest.mark.parametrize("claim", [
    "The flat is still available and the price is live.",
    "Check it out — it's still available!",
    "I can confirm it is still available.",
    "It is still available if you move fast.",
    "I can check live availability, and it is still available.",
    "It's still available — want me to book a viewing?",
])
def test_a_live_claim_still_goes_and_the_paragraphs_stay(claim):
    text = ADVICE["text"]
    assert "\n\n" in text
    probe = text.replace("\n\n", "\n\n" + claim + " ", 1)
    out = validate_evidence_scope(probe, ADVICE["state"])
    assert claim not in out
    assert out.startswith(text)                    # everything else exactly as written
    assert "current price and availability were not verified live" in out[len(text):]


def test_a_withdrawn_last_sentence_keeps_its_paragraph_break():
    text = ADVICE["text"]
    first, rest = text.split("\n\n", 1)
    probe = f"{first} The flat is still available.\n\n{rest}"
    out = validate_evidence_scope(probe, ADVICE["state"])
    assert "still available." not in out.split("\n\n", 1)[0]
    assert out.startswith(text)


# Chinese sentences end at 。！？ with no blank after them: a withdrawn claim takes only its
# own sentence, its neighbours in the same paragraph stay (r6e).
_ZH_PARAGRAPHS = {
    "r07_zh_advice": ("高峰时段公交可能更慢。", "该房源的价格和可入住时间来自缓存记录"),
    "r07_zh_not_live": ("没有实时核实。", "当前租金和是否仍可租，请先向中介确认。"),
}


@pytest.mark.parametrize("name", sorted(_ZH_PARAGRAPHS))
@pytest.mark.parametrize("claim", ["该房源目前可租。", "价格已实时核实。", "实时租金为2,225镑。"])
def test_a_chinese_live_claim_goes_alone(name, claim):
    case = SCOPE[name]
    text = case["text"]
    before, after = _ZH_PARAGRAPHS[name]
    assert before + after in text or before in text and after in text
    probe = text.replace(before, before + claim, 1)
    out = validate_evidence_scope(probe, case["state"])
    assert claim not in out
    assert out.startswith(text)                    # its neighbours, and the rest, as written


def test_a_chinese_claim_inside_a_sentence_takes_that_sentence():
    case = SCOPE["r07_zh_advice"]
    text = case["text"]
    sentence = "需要说明的是，这是按规划器默认出发时间得到的结果，高峰时段公交可能更慢。"
    assert sentence in text
    out = validate_evidence_scope(text.replace("可能更慢。", "可能更慢，该房源目前可租。", 1), case["state"])
    assert "目前可租" not in out and "高峰时段公交可能更慢" not in out
    assert "该房源的价格和可入住时间来自缓存记录" in out and out.startswith(text.split(sentence)[0])


@pytest.mark.parametrize("name,claim", [
    ("r07_district_centre", "The safety check used the flat's exact coordinates."),
    ("r07_offer_coordinates",
     "I can run the safety check again, and the safety check used the flat's exact coordinates."),
])
def test_a_listing_point_claim_still_goes(name, claim):
    case = SCOPE[name]
    text = case["text"]
    out = validate_evidence_scope(text.replace("\n\n", "\n\n" + claim + " ", 1), case["state"])
    assert claim not in out


def test_the_enumeration_fix_keeps_the_paragraphs():
    case = R21["r21_0136f2f6"]
    caveat = ("First, this is a cached record, not a live check — the price and availability have not "
              "been verified as current, so confirm both with the agent before relying on them.")
    text = case["raw_draft"].replace(caveat, "First, the price and availability were checked live today, so it is ready to book.")
    out = validate_evidence_scope(text, case["state"])
    assert ("\n\nIt is still over your budget, so it remains an alternative rather than a match.\n\n"
            "If you want, I can check") in out
    assert out.startswith(case["raw_draft"].split("Two things to keep in mind.")[0].rstrip() + "\n\n")
