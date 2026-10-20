"""Memory honesty in answers (R2-19, R2-21)."""
from __future__ import annotations

from core import loop_prompts
from core.agent_loop import (AUTOMATIC_MEMORY_DISCLOSURE_EN, AUTOMATIC_MEMORY_DISCLOSURE_ZH,
                             acknowledge_remembered_budget, disclose_automatic_memory)


def test_r2_19_a_silent_storage_question_gets_the_truthful_description():
    answer = "No — there are no saved memories about you, and I won't save anything silently."
    out = disclose_automatic_memory(
        answer, "Do you store anything about me when I haven't asked you to remember it? "
                "Be honest.")
    assert out.endswith(AUTOMATIC_MEMORY_DISCLOSURE_EN)
    assert "automatically keeps a short log" in out
    zh = disclose_automatic_memory("没有。", "你会不会在我没让你记的时候自动保存我的信息？", "zh")
    assert zh.endswith(AUTOMATIC_MEMORY_DISCLOSURE_ZH)


def test_r2_19_other_questions_are_untouched():
    for message in ("What do you remember about me?", "Find a 1-bed in Camden",
                    "Please remember that I have a cat"):
        assert disclose_automatic_memory("Sure.", message) == "Sure."


def test_r2_19_the_model_is_told_how_automatic_memory_works():
    assert "AUTOMATIC MEMORY" in loop_prompts.MEMORY_IN_CONTEXT_RULE
    assert loop_prompts.MEMORY_IN_CONTEXT_RULE in loop_prompts.behaviour_rules()


_MEMORY = ("What I remember about this user:\n"
           "[FACT] My max budget is £1,800/month\n[PAST] User asked: 1-bed in Hackney")


def test_r2_21_a_saved_budget_is_never_denied():
    answer = "You haven't given a budget, so here are the cheapest 1-beds I found: £2,751 ..."
    out = acknowledge_remembered_budget(answer, _MEMORY, None)
    assert "you saved a budget of £1,800/month earlier" in out


def test_r2_21_untouched_when_a_budget_is_applied_or_none_is_saved():
    answer = "You haven't given a budget, so here are some options."
    assert acknowledge_remembered_budget(answer, _MEMORY, 1800) == answer
    assert acknowledge_remembered_budget(answer, "[FACT] I have a cat", None) == answer
    assert acknowledge_remembered_budget("Here are flats under £1,800.", _MEMORY, None) == \
        "Here are flats under £1,800."
