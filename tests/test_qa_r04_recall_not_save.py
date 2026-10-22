"""R4-04: a question about what was remembered is not a save request.

r07 A-F1/A-F3: 「我之前让你记住了什么？」 -> remember(content '了什么') and "已保存的原文：「了什么」";
「你记住了我的什么信息？」 saved verbatim. D-F11: "Do you still remember my name and
workplace?" saved the question and never answered it.
"""
from __future__ import annotations

import pytest

from core.memory_gate import (DECISION_REJECTED_QUESTION, explicit_remember_clause,
                              memory_write_rejection, user_authorizes_memory)

RECALL_QUESTIONS = [
    "我之前让你记住了什么？",
    "你记住了我的什么信息？",
    "你还记住哪些关于我的事",
    "你记住我的预算了吗？",
    "Do you still remember my name and workplace?",
    "What did you remember about me?",
    "What have you remembered so far?",
    "Have you saved my budget?",
]


@pytest.mark.parametrize("message", RECALL_QUESTIONS)
def test_recall_question_is_not_a_save_authorization(message):
    assert user_authorizes_memory(message) is False
    assert explicit_remember_clause(message) == ""


@pytest.mark.parametrize("message,fact", [
    ("记住我对花粉过敏。", "我对花粉过敏"),
    ("Remember that I have a cat.", "I have a cat"),
    ("帮我记住：我有一只狗", "我有一只狗"),
    ("Please remember that my guarantor is in Manchester", "my guarantor is in Manchester"),
])
def test_real_save_requests_still_authorize(message, fact):
    assert user_authorizes_memory(message) is True
    assert explicit_remember_clause(message) == fact


@pytest.mark.parametrize("content", ["了什么", "什么信息", "Do you still remember my name and workplace?",
                                     "你记住了我的什么信息？"])
def test_question_fragment_is_never_written(content):
    assert memory_write_rejection(content) == DECISION_REJECTED_QUESTION


@pytest.mark.parametrize("content", ["I have a cat", "我对花粉过敏", "My budget is £1,800, okay?",
                                     "我的预算是1800，好吗？"])
def test_facts_and_polite_requests_are_written(content):
    assert memory_write_rejection(content) is None


def test_remember_tool_refuses_a_question_with_its_own_reason(tmp_path, monkeypatch):
    import core.tools.memory_tools as mt
    monkeypatch.setattr(mt, "_turn_guard_flagged", lambda: False, raising=False)
    monkeypatch.setattr(mt, "_mem", lambda: (_ for _ in ()).throw(AssertionError("no write")))
    result = mt.remember_impl(content="了什么", kind="semantic", session_id="s", user_id="u-r404")
    assert result["success"] is False and result["stored"] is None
    assert "question" in result["error"]
