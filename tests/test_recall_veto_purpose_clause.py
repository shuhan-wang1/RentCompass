"""Recall-veto over-breadth: a save request's PURPOSE clause is not a recall question.

Benchmark case **G1** (`denied_recall`), reported in PR #25 and left unfixed there:

    "Please note my budget is 1800. Just so you remember for next time."

``_RECALL_VETO_EN``'s bare ``\\byou\\s+(?:remember|recall)\\b`` pattern fired on the
purpose clause "just so you remember", so the utterance was classified as a memory-RECALL
question. Both consumers of the tuple then did the wrong thing:

  * ``user_authorizes_memory`` — the veto runs BEFORE the positive cues, so an explicit
    "please remember my budget is 1800, just so you remember for next time" was not
    authorization at all.
  * ``is_pure_recall_question`` — returned True, and ``agent_loop`` denies a
    model-initiated ``remember`` on a pure-recall turn (``decision="denied_recall"``,
    ``dispatch_started=False``) regardless of taint. The save was dropped.

The pattern is NOT deleted: it is the only one that catches an auxiliary-less
"you remember the flat we looked at?" (the sibling interrogative pattern requires
do/does/did/can/could/would/will). Instead the recall phrase is skipped when a
purpose/result subordinator introduces it. Chinese has the identical false positive —
「帮我记住，这样你就记得我的预算了」 tripped the ``记得我`` veto — and is fixed by the
same guard.

These tests pin both directions (purpose clauses through, questions still vetoed) and
the single-source invariant that made one fix cover both call sites.
"""
import importlib
import json
import os
import sys

import pytest


def _pin_app():
    """Pin the real ``app`` root first and evict any shadowed core/rag modules
    (mirrors tests/test_taint_aplus._pin_app)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("app", "src"):
        local = os.path.join(repo, sub)
        if local in sys.path:
            sys.path.remove(local)
        sys.path.insert(0, local)  # app ends first, then src
    for name in list(sys.modules):
        if name in ("core", "rag") or name.startswith(("core.", "rag.")):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "app" not in path:
                del sys.modules[name]


_pin_app()

mg = importlib.import_module("core.memory_gate")

# The literal utterance from the defect report. Pinned verbatim — this exact string is
# what production misclassified.
G1_UTTERANCE = "Please note my budget is 1800. Just so you remember for next time."
# The benchmark's own G1 query (evaluation/benchmark/cases.jsonl), same construction.
G1_BENCH_UTTERANCE = "Just so you remember for next time: my max budget is £1400 a month."


# ------------------------------------------------------- purpose clauses are NOT recall

PURPOSE_CLAUSE_SAVES = [
    G1_UTTERANCE,
    G1_BENCH_UTTERANCE,
    "Please remember my budget is 1800. Just so you remember for next time.",
    "Note that my budget is 1800, so that you remember next time.",
    "Make a note of my move-in date, so you remember it later.",
    "So that you remember, my budget is £1800 a month.",
    "just so you remember, I have a dog",
    "just so you remember that my budget is 1800",
    "Save this so you recall it next session: I need parking.",
    "I'm telling you in order that you remember it next time: budget 1800.",
    # zh — same class of false positive (记得我 / 我说过 inside a purpose clause)
    "帮我记住，这样你就记得我的预算了",
    "记一下我的预算，免得你不记得我说过的",
    "记住我要一楼，为了让你记得我的要求",
    "记一下我的预算，省得你忘了我说过的话",
]


@pytest.mark.parametrize("msg", PURPOSE_CLAUSE_SAVES)
def test_purpose_clause_is_not_a_recall_cue(msg):
    """The shared recall-cue family must not fire on a purpose clause (the source of the
    G1 misclassification, before either consumer sees it)."""
    assert mg._has_recall_cue(msg) is False


@pytest.mark.parametrize("msg", PURPOSE_CLAUSE_SAVES)
def test_purpose_clause_save_is_not_pure_recall(msg):
    """Consumer 2 (``agent_loop`` ``denied_recall`` gate): a save whose purpose clause
    mentions remembering must NOT be denied as a pure recall question."""
    assert mg.is_pure_recall_question(msg) is False


@pytest.mark.parametrize("msg", [
    "Please remember my budget is 1800. Just so you remember for next time.",
    "Note that my budget is 1800, so that you remember next time.",
    "Make a note of my move-in date, so you remember it later.",
    "just so you remember that my budget is 1800",
    "Save this so you recall it next session: I need parking.",
    "帮我记住，这样你就记得我的预算了",
    "记一下我的预算，免得你不记得我说过的",
    "记住我要一楼，为了让你记得我的要求",
])
def test_purpose_clause_save_is_authorized(msg):
    """Consumer 1 (``user_authorizes_memory``): an explicit save cue plus a purpose
    clause is authorization. The veto used to eat the cue."""
    assert mg.user_authorizes_memory(msg) is True


def test_g1_utterance_reaches_dispatch_instead_of_denied_recall():
    """End-to-end on the decision sequence ``agent_loop`` runs for a write tool
    (app/core/agent_loop.py ~1591-1618) on the literal G1 utterance, clean context.

    OLD: is_pure_recall_question → True → decision="denied_recall", never dispatched.
    NEW: not pure recall → memory_write_allowed decides → allowed on a clean turn.
    """
    content = "user max budget 1800 per month"
    user_authorized = mg.write_authorization(G1_UTTERANCE, content)
    assert mg.is_pure_recall_question(G1_UTTERANCE) is False, "would be denied_recall"
    assert mg.memory_write_allowed(
        context_tainted=False, user_authorized=user_authorized) is True
    # A tainted turn still routes through the safe path (freeze + ask_user), not a
    # silent write: this fix does not widen the taint rule.
    assert mg.memory_write_allowed(
        context_tainted=True, user_authorized=user_authorized) is user_authorized


def test_g1_benchmark_case_query_is_not_pure_recall():
    """Pin the LIVE benchmark string rather than only our copy of it, so the case and
    the gate cannot drift apart."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, "evaluation", "benchmark", "cases.jsonl")
    if not os.path.exists(path):
        pytest.skip("benchmark cases.jsonl not present in this checkout")
    with open(path, encoding="utf-8") as fh:
        queries = [json.loads(line)["user_query"] for line in fh
                   if line.strip() and json.loads(line).get("case_id") == "G1"]
    assert queries, "benchmark case G1 not found in cases.jsonl"
    for q in queries:
        assert mg.is_pure_recall_question(q) is False
        assert mg._has_recall_cue(q) is False


# --------------------------------------------- genuine recall questions stay vetoed

RECALL_QUESTIONS = [
    # bare, auxiliary-less form — the ONLY pattern that catches it is the broad one, so
    # it must survive the narrowing (this is why the pattern is not deleted)
    "you remember the flat we looked at?",
    "you recall what I said about parking?",
    # discourse-marker "so": utterance-initial or sentence-initial "So you remember …?"
    # is a QUESTION, not a purpose clause — the narrowing must not reach it
    "So you remember my budget?",
    "So, you remember my budget?",
    "I moved to Camden. So you remember my budget?",
    # interrogative sibling + the rest of the family
    "Do you remember my budget?",
    "can you remember what I said earlier?",
    "do you recall what I said earlier?",
    "remember when I mentioned the budget?",
    "What did I say my budget was?",
    "did I tell you my move-in date?",
    "what's my budget again?",
    "did I say my budget was 1500?",
    # zh
    "你还记得我的预算吗",
    "记不记得我要一楼",
    "是否记得我说过的话",
    "记得我的预算是多少吗",
    "你记得我的预算是多少吗",
    "你记不记得我说过什么",
    "我之前说过什么预算来着",
    "上次说的那个区域是哪里",
    "我说过我想住哪吗",
    "你还记得我说过预算是1500吗",
    # a leading clause must not reach across punctuation and exempt the question
    "这样吧，你记得我的预算吗",
    "这样，我说过什么预算",
]


@pytest.mark.parametrize("msg", RECALL_QUESTIONS)
def test_recall_questions_still_vetoed(msg):
    assert mg._has_recall_cue(msg) is True
    assert mg.user_authorizes_memory(msg) is False
    assert mg.is_pure_recall_question(msg) is True


# ------------------------------------------------- single source of recall phrasing

def test_authorization_veto_delegates_to_the_shared_recall_cue(monkeypatch):
    """``user_authorizes_memory`` must consult ``_has_recall_cue``, not its own private
    copy of the veto loop. Two copies is how the purpose-clause exemption could be fixed
    in one consumer and silently missed in the other."""
    monkeypatch.setattr(mg, "_has_recall_cue", lambda text: True)
    assert mg.user_authorizes_memory("Please remember my budget is 1800") is False
    assert mg.user_authorizes_memory("记住我的预算是1500") is False
    monkeypatch.setattr(mg, "_has_recall_cue", lambda text: False)
    assert mg.user_authorizes_memory("Please remember my budget is 1800") is True
    # …and with the veto stubbed off, the recall question is only still rejected by the
    # absence of a save cue — proof the veto is the shared helper's decision, nothing else.
    assert mg.user_authorizes_memory("do you recall what I said earlier?") is False


def test_pure_recall_delegates_to_the_shared_recall_cue(monkeypatch):
    monkeypatch.setattr(mg, "_has_recall_cue", lambda text: False)
    assert mg.is_pure_recall_question("你还记得我的预算吗") is False
    monkeypatch.setattr(mg, "_has_recall_cue", lambda text: True)
    assert mg.is_pure_recall_question("你还记得我的预算吗") is True
