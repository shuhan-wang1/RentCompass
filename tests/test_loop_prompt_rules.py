"""Tests for the fc-loop latency + routing behaviour rules (fix-agent F1).

Assert on module-level marker constants rather than full prose so wording tweaks
don't break tests. Covers: the recall_memory suppression rule, the tool-efficiency /
web_search cap rule, the safety-target routing rule, and the grounded-citation rule —
each must be reachable from build_system_directive so the live loop actually carries it.
"""

from core import loop_prompts


def test_memory_in_context_rule_suppresses_recall():
    rules = loop_prompts.behaviour_rules()
    assert loop_prompts.NO_RECALL_MARKER in rules            # "Do NOT call recall_memory"
    assert loop_prompts.MEMORY_IN_CONTEXT_RULE in rules


def test_efficiency_rule_caps_web_search_and_prefers_batch():
    rules = loop_prompts.behaviour_rules()
    assert loop_prompts.WEB_SEARCH_BUDGET_MARKER in rules    # "at most 2 web_search"
    assert "ONE batch of parallel tool calls" in loop_prompts.EFFICIENCY_RULE


def test_safety_target_rule_routes_to_check_safety():
    rule = loop_prompts.SAFETY_TARGET_RULE
    assert "check_safety" in rule
    assert loop_prompts.POLICE_SOURCE_MARKER in rule         # "data.police.uk"
    # Must steer away from the observed misroute.
    assert "recall_memory" in rule


def test_grounded_citation_rule_names_source():
    assert loop_prompts.POLICE_SOURCE_MARKER in loop_prompts.GROUNDED_CITATION_RULE


def test_new_rules_reach_the_system_directive():
    directive = loop_prompts.build_system_directive("en")
    for marker in (
        loop_prompts.NO_RECALL_MARKER,
        loop_prompts.WEB_SEARCH_BUDGET_MARKER,
        loop_prompts.POLICE_SOURCE_MARKER,
    ):
        assert marker in directive
    # Pre-existing rules still present (no regression).
    assert loop_prompts.SOFT_GATE_CONFIRMED_MARKER in directive
    assert loop_prompts.NO_EMOJI_MARKER in directive
