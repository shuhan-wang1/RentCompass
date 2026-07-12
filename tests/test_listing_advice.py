"""listing-advice route — opinion / suitability / recommendation over ALREADY-shown listings.

Regression: with search results on screen, 「如果我和我女朋友一块住的话你会推荐这个房源么？」
("would you recommend this listing if I live with my girlfriend?") used to re-run
search_properties and reply "在 manchester 为你找到 15 套当前房源…" instead of reasoning
about the referenced listing. These tests pin the fix, WITHOUT any live LLM call:

- Chinese deictics / ordinals resolve the referenced listing (_resolve_last_result).
- The deterministic step-1.5 interception answers over the real shown listings
  (reasoning_property for a specific listing, direct_answer for the set) — never a
  fresh search — and the evidence surface now carries the listing DESCRIPTION.
- Genuinely-new searches ("再帮我找几个更便宜的房子", "find me other options in Salford")
  still route to the vote / search.
- The parse-failure fallback answers over existing results instead of defaulting to
  search when the message isn't a new search.

The classifier LLM is stubbed (JSON / garbage / never-called) exactly like
test_intent_router.py; the deterministic paths use a never-called stub to PROVE the
interception fires before the vote.
"""

import json
import os
import sys
import types

# --- Pin the real source roots ahead of tests/ (stale shadow copies of `core` live
# under tests/ and would otherwise shadow the app packages under prepend mode).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
for _m in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
    if "tests" in (getattr(sys.modules[_m], "__file__", "") or "").replace("\\", "/").split("/"):
        del sys.modules[_m]

import pytest


@pytest.fixture(scope="module")
def lga():
    pytest.importorskip("langgraph")
    import importlib
    return importlib.import_module("core.langgraph_agent")


# ── stubs (mirror test_intent_router.py) ──────────────────────────────────────
class _DummyRegistry:
    def list_tool_names(self):
        return ["search_properties", "web_search", "get_transport_info", "check_safety"]

    def get(self, name):
        return None


class _JsonLLM:
    """Returns the given intent as strict JSON (mimics the DeepSeek classifier)."""

    def __init__(self, intent):
        self.intent = intent
        self.seen = None

    def invoke(self, prompt):
        self.seen = prompt
        return types.SimpleNamespace(content=json.dumps({"intent": self.intent}))


class _GarbageLLM:
    """Returns text the parse ladder cannot map to any catalog intent -> None."""

    def invoke(self, prompt):
        return types.SimpleNamespace(content="completely unrelated blah blah")


class _NoVoteLLM:
    """Fails if the vote is ever reached — proves the deterministic interception fired."""

    def invoke(self, prompt):
        raise AssertionError("listing-advice interception must route before the LLM vote")


# ── fixtures: realistic shown-result records (shape from app._build_results_context) ──
_DESC0 = ("Spacious one-bedroom flat with a double bedroom, bills included, "
          "ideal for couples and sharers. 20 min to the centre.")
_DESC1 = "Cosy single room in a shared student house, students only, no couples."


def _records():
    return [
        {"name": "Maple Court", "address": "Maple Court, 12 Oak Rd, Manchester",
         "price": "£1200 pcm", "travel_time": "20 mins", "bedrooms": 1,
         "property_type": "Flat", "budget_status": "within budget",
         "source": "onthemarket", "url": "https://onthemarket.com/details/1/",
         "description": _DESC0},
        {"name": "Elm House", "address": "Elm House, 5 Pine St, Manchester",
         "price": "£650 pcm", "travel_time": "35 mins", "bedrooms": 1,
         "property_type": "Room", "budget_status": "within budget",
         "source": "onthemarket", "url": "https://onthemarket.com/details/2/",
         "description": _DESC1},
    ]


def _decide(lga, msg, llm, extra_ctx=None, accumulated=None):
    node = lga._make_decide_tool_node(_DummyRegistry(), llm)
    ec = {"current_message": msg}
    if extra_ctx:
        ec.update(extra_ctx)
    state = {"user_query": msg, "extracted_context": ec,
             "accumulated_search_criteria": accumulated or {}}
    return node(state)


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE EXACT REGRESSION
# ═══════════════════════════════════════════════════════════════════════════
def test_regression_couple_recommendation_answers_over_listing(lga):
    recs = _records()
    cmd = _decide(lga, "如果我和我女朋友一块住的话你会推荐这个房源么？",
                  _NoVoteLLM(), extra_ctx={"last_results": recs})
    d = cmd.update["tool_decision"]
    obs = cmd.update.get("tool_observation")
    # Must NOT re-run a search; must answer over the shown listing with an observation.
    assert d["tool"] != "search_properties"
    assert d["tool"] in ("reasoning_property", "direct_answer")
    assert obs is not None
    # The evidence surface now carries the real listing DESCRIPTION (bug #4 fix).
    assert "ideal for couples" in obs
    # Tainted so the untrusted listing text is sanitized before generation.
    assert cmd.update.get("context_tainted") is True


# ═══════════════════════════════════════════════════════════════════════════
# 2. CHINESE ORDINAL RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════
def test_chinese_ordinal_resolves_second_result(lga):
    recs = _records()
    # Pure resolver: 第二个 -> results[1].
    assert lga._resolve_last_result("第二个怎么样？", {"last_results": recs}) is recs[1]
    # Digit form 第2套 -> results[1] as well.
    assert lga._resolve_last_result("第2套呢", {"last_results": recs}) is recs[1]
    # Bare Chinese numeral without a measure word must NOT be read as an ordinal
    # (第一次 = "first time", not "the first listing").
    assert lga._resolve_last_result("这是我第一次租房", {"last_results": recs}) is None


def test_second_one_routes_to_that_record(lga):
    recs = _records()
    cmd = _decide(lga, "第二个怎么样？", _NoVoteLLM(), extra_ctx={"last_results": recs})
    d = cmd.update["tool_decision"]
    assert d["tool"] == "reasoning_property"
    assert cmd.update["tool_raw_data"]["property"] is recs[1]
    assert _DESC1[:20] in cmd.update["tool_observation"]


def test_chinese_deictic_resolves_first_result(lga):
    recs = _records()
    for deictic in ("这个房源含账单吗", "那套房怎么样", "刚才那个"):
        assert lga._resolve_last_result(deictic, {"last_results": recs}) is recs[0]
    # 最后一个 -> the most-recent single referent (results[-1]).
    assert lga._resolve_last_result("最后一个呢", {"last_results": recs}) is recs[-1]


# ═══════════════════════════════════════════════════════════════════════════
# 3. ENGLISH ADVICE (long-form, goes through the advice path not the bare-detail one)
# ═══════════════════════════════════════════════════════════════════════════
def test_english_recommend_first_for_couple_uses_advice_path(lga):
    recs = _records()
    cmd = _decide(lga, "would you recommend the first one for a couple who want a double bed?",
                  _NoVoteLLM(), extra_ctx={"last_results": recs})
    d = cmd.update["tool_decision"]
    obs = cmd.update.get("tool_observation")
    assert d["tool"] == "reasoning_property"
    assert "listing-advice" in d["reason"]
    assert cmd.update["tool_raw_data"]["property"] is recs[0]
    assert "ideal for couples" in obs          # description surfaced for the first listing


# ═══════════════════════════════════════════════════════════════════════════
# 4. SET-LEVEL ADVICE
# ═══════════════════════════════════════════════════════════════════════════
def test_set_level_which_suits_a_couple(lga):
    recs = _records()
    cmd = _decide(lga, "这些里面你最推荐哪一个适合情侣住？",
                  _NoVoteLLM(), extra_ctx={"last_results": recs})
    d = cmd.update["tool_decision"]
    obs = cmd.update.get("tool_observation")
    assert d["tool"] == "direct_answer"
    assert cmd.update["tool_raw_data"]["compared_results"] is recs
    # Comparison surface lists EVERY shown listing (with descriptions) so the model can pick.
    assert "Previously recommended properties" in obs
    assert "Maple Court" in obs and "Elm House" in obs
    assert "ideal for couples" in obs           # per-listing desc slice (bug #4 fix)


# ═══════════════════════════════════════════════════════════════════════════
# 5. GUARDS — genuinely-new searches must NOT be hijacked by the advice interception
# ═══════════════════════════════════════════════════════════════════════════
def test_new_cheaper_search_still_routes_to_search(lga):
    recs = _records()
    # 更便宜的房子 with results on screen is a NEW search, not advice about the shown set.
    cmd = _decide(lga, "再帮我找几个更便宜的房子",
                  _JsonLLM("search_properties"), extra_ctx={"last_results": recs})
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_find_other_options_still_routes_to_search(lga):
    recs = _records()
    cmd = _decide(lga, "find me other options in Salford",
                  _JsonLLM("search_properties"), extra_ctx={"last_results": recs})
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_advice_followup_guard_bails_on_new_search_verbs(lga):
    recs = _records()
    ctx = {"last_results": recs, "current_message": "find me other options in Salford"}
    assert lga._is_advice_followup("find me other options in Salford", ctx) is None
    ctx2 = {"last_results": recs, "current_message": "再帮我找几个更便宜的房子"}
    assert lga._is_advice_followup("再帮我找几个更便宜的房子", ctx2) is None


def test_unanchored_weak_cues_are_not_hijacked(lga):
    """怎么样/好不好 also appear in area/weather questions — with results on screen those
    must NOT be answered from the listings. Set-level advice needs a set reference."""
    recs = _records()
    for msg in ("曼彻斯特天气怎么样", "曼彻斯特怎么样", "Shoreditch 好不好"):
        ctx = {"last_results": recs, "current_message": msg}
        assert lga._is_advice_followup(msg, ctx) is None, msg


def test_unanchored_weather_reaches_the_vote(lga):
    """Decision-level: 天气怎么样 with results on screen flows through to the LLM vote
    (here voting get_weather) instead of the deterministic listing interception."""
    recs = _records()
    cmd = _decide(lga, "曼彻斯特天气怎么样", _JsonLLM("get_weather"),
                  extra_ctx={"last_results": recs})
    assert cmd.update["tool_decision"]["tool"] == "get_weather"


def test_set_reference_still_required_only_for_set_level(lga):
    """Anchoring rules: a resolvable single reference fires record-level advice with no
    set reference; a set reference fires set-level; an anchored-to-nothing strong cue
    (city suitability) falls through to the vote."""
    recs = _records()
    # record-level: deictic anchors it, no set-ref needed
    ctx = {"last_results": recs, "current_message": "这个房源适合情侣吗"}
    assert lga._is_advice_followup("这个房源适合情侣吗", ctx) == {"record": recs[0]}
    # set-level: 哪个 is a set reference
    ctx2 = {"last_results": recs, "current_message": "哪个适合情侣"}
    assert lga._is_advice_followup("哪个适合情侣", ctx2) == {"set": True}
    # unanchored strong cue about a CITY -> not intercepted (belongs to the vote)
    ctx3 = {"last_results": recs, "current_message": "曼彻斯特适合学生住吗"}
    assert lga._is_advice_followup("曼彻斯特适合学生住吗", ctx3) is None


# ═══════════════════════════════════════════════════════════════════════════
# 6. _format_single_result — description, cap, availability, tolerance
# ═══════════════════════════════════════════════════════════════════════════
def test_format_single_result_includes_description(lga):
    out = lga._format_single_result(_records()[0])
    assert "Description:" in out
    assert "ideal for couples" in out
    assert "Property: Maple Court, 12 Oak Rd, Manchester" in out


def test_format_single_result_caps_long_description(lga):
    rec = {"address": "A", "description": "x" * 2000}
    out = lga._format_single_result(rec)
    # The description is capped to 1500 chars + an ellipsis; the full 2000 never appears.
    assert ("Description: " + "x" * 1500 + "…") in out
    assert ("x" * 1600) not in out


def test_format_single_result_surfaces_availability(lga):
    rec = {"address": "A", "available_from": "2026-09-01", "availability_status": "Available"}
    out = lga._format_single_result(rec)
    assert "Available from: 2026-09-01" in out
    assert "Availability: Available" in out


def test_format_single_result_tolerates_missing_fields(lga):
    # No description / available_from keys at all — must not raise, no Description line.
    out = lga._format_single_result({"address": "A, London"})
    assert "Property: A, London" in out
    assert "Description:" not in out
    # Entirely empty record -> graceful placeholder.
    assert "no details captured" in lga._format_single_result({})


def test_format_result_line_carries_desc_and_availability(lga):
    line = lga._format_result_line(1, {"name": "X", "description": "y" * 400,
                                        "available_from": "2026-09-01"})
    assert "available from: 2026-09-01" in line
    assert "desc: " in line
    assert line.count("y") == 250           # description sliced to first 250 chars


# ═══════════════════════════════════════════════════════════════════════════
# 7. PARSE-FAILURE FALLBACK — answer over results, unless it's a new search
# ═══════════════════════════════════════════════════════════════════════════
def test_fallback_answers_over_results_when_not_a_search(lga):
    recs = _records()
    # Classifier output unparseable + results on screen + no search verb -> answer over
    # the shown listings, NOT a fresh search (root-cause #3).
    cmd = _decide(lga, "honestly I can't decide, help me out",
                  _GarbageLLM(), extra_ctx={"last_results": recs})
    d = cmd.update["tool_decision"]
    assert d["tool"] != "search_properties"
    assert d["tool"] == "direct_answer"
    assert cmd.update.get("tool_observation") is not None


def test_fallback_with_search_verb_still_searches(lga):
    recs = _records()
    # Same unparseable classifier, but the message explicitly asks to FIND more -> search.
    cmd = _decide(lga, "find me somewhere cheaper please",
                  _GarbageLLM(), extra_ctx={"last_results": recs})
    assert cmd.update["tool_decision"]["tool"] == "search_properties"


def test_fallback_no_results_defaults_to_heuristic(lga):
    # No results to answer over -> the heuristic fallback owns it (generic -> web_search).
    cmd = _decide(lga, "tell me about UK guarantors", _GarbageLLM())
    assert cmd.update["tool_decision"]["tool"] == "web_search"
