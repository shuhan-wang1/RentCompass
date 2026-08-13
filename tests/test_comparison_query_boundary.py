"""Comparison routing must use words, not arbitrary substrings."""
import pytest

import app as appmod


@pytest.mark.parametrize(
    "message",
    [
        "Not sure, depends on the budget, but the maximum I would take for commute would be 30 min.",
        "I am not sure about it but I got my job near Canary Wharf",
        "What is the rent for a studio?",
    ],
)
def test_ordinary_or_substrings_do_not_trigger_comparison_context(message):
    assert appmod._is_comparison_query(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "Compare the first two listings",
        "Camden vs Stratford",
        "Should I choose this one or the previous one?",
        "Which one is better?",
    ],
)
def test_real_comparison_phrases_still_trigger(message):
    assert appmod._is_comparison_query(message) is True
