"""A conditional offer is not an announced lookup (overnight round r01: R1-16).

An out-of-scope refusal ("I can't search for a flat in Paris … If you'd rather skip those, say
'go ahead' and I'll search Stratford on your budget alone.") was nudged as an announced-but-
unexecuted lookup and then shipped with "I was not able to run that lookup this round, so
nothing above is a fresh result." — no lookup had been promised for this turn.
"""
from __future__ import annotations

import pytest

from core.agent_loop import announces_unexecuted_tool

REFUSAL = ("I can't search for a flat in Paris because I only cover UK rental listings. If you'd "
           "like to stay in the UK, I can search with your £1,500 monthly budget.\n\n"
           "If you'd rather skip those, say \"go ahead\" and I'll search Stratford on your "
           "budget alone.")


@pytest.mark.parametrize("text", [
    REFUSAL,
    "Shall I check the commute for you?",
    "Once you confirm, I'll search Camden.",
    "Would you like me to look up the weather? I'll check it as soon as you say so.",
    "我可以帮你查一下通勤时间。",
    "如果需要的话，我来查一下天气。",
])
def test_r1_16_offers_are_not_announcements(text):
    assert announces_unexecuted_tool(text, []) is False


@pytest.mark.parametrize("text", [
    "Let me search for one-bedroom listings in Shoreditch.",
    "I'll check the weather for you.",
    "I can't do that from memory, so let me check the listing.",
    "让我查一下天气。",
])
def test_r1_16_real_announcements_still_detected(text):
    assert announces_unexecuted_tool(text, []) is True
