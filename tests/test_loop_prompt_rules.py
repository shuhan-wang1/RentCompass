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


# ---------------------------------------------------------------------------
# H2 — area-switch continuation routes to search_properties, not web_search
# ---------------------------------------------------------------------------

def test_area_switch_rule_routes_to_search_properties():
    rule = loop_prompts.AREA_SWITCH_RULE
    assert loop_prompts.AREA_SWITCH_MARKER in rule           # "AREA SWITCH CONTINUATION"
    # continuation is a property search with the existing criteria ...
    assert "search_properties" in rule
    # ... and explicitly NOT web research (the observed misroute).
    assert "web_search" in rule


def test_area_switch_rule_defers_to_negative_directive():
    # CRITICAL: an explicit no-search / research-only directive keeps HIGHER priority
    # so the rule cannot regress guard case H3.
    rule = loop_prompts.AREA_SWITCH_RULE
    assert "EXCEPTION" in rule
    assert "HIGHER priority" in rule
    assert "RESEARCH vs LISTING SEARCH" in rule              # names the winning rule


def test_area_switch_rule_reaches_the_system_directive():
    directive = loop_prompts.build_system_directive("en")
    assert loop_prompts.AREA_SWITCH_MARKER in directive
    # The negative-directive rule it defers to is present too (H3 not regressed).
    assert loop_prompts.NO_SEARCH_YET_RULE in directive
    assert "只是了解一下" in directive                         # H3 research cue preserved


def test_area_switch_rule_is_bilingual():
    rule = loop_prompts.AREA_SWITCH_RULE
    assert "换到" in rule and "那 Camden 呢" in rule           # zh switch cues
    assert "what about" in rule.lower()                       # en switch cue
