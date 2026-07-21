"""Tests for core.context_assembler.assemble_messages + core.loop_prompts.

Covers the message array shape/ordering (§2.7), the reply-language directive, empty
context omission, the token-budget trimming ladder ported to message granularity,
verbatim current-message (no legacy string-prefix leakage), evidence rendering, and
the behaviour-rules substrings (asserted against module constants, not prose).
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from core import loop_prompts
from core.context_assembler import assemble_messages, estimate_tokens


# ---------------------------------------------------------------------------
# Shape / ordering
# ---------------------------------------------------------------------------

def test_minimal_shape_system_then_human():
    # No history, no context -> just the system directive + the verbatim user message.
    msgs = assemble_messages(user_message="find me a flat near UCL", history=[])
    assert len(msgs) == 2
    assert isinstance(msgs[0], SystemMessage)
    assert isinstance(msgs[1], HumanMessage)
    assert msgs[-1].content == "find me a flat near UCL"


def test_empty_context_block_omits_message_two():
    msgs = assemble_messages(
        user_message="hi", history=[], context_block={}, memory_block="")
    # Only system + human; no second SystemMessage.
    system_msgs = [m for m in msgs if isinstance(m, SystemMessage)]
    assert len(system_msgs) == 1


def test_all_empty_context_values_still_omits_message_two():
    msgs = assemble_messages(
        user_message="hi", history=[],
        context_block={"accumulated_criteria": {}, "focused_property": None,
                       "last_results": [], "recommendations_index": []})
    assert len([m for m in msgs if isinstance(m, SystemMessage)]) == 1


def test_context_block_present_adds_second_system_message():
    msgs = assemble_messages(
        user_message="which is cheapest?",
        history=[],
        context_block={"accumulated_criteria": {"budget": "1200 pcm", "area": "Camden"}})
    system_msgs = [m for m in msgs if isinstance(m, SystemMessage)]
    assert len(system_msgs) == 2
    # Ordering: system directive, then context, then human.
    assert isinstance(msgs[0], SystemMessage)
    assert isinstance(msgs[1], SystemMessage)
    assert isinstance(msgs[-1], HumanMessage)
    assert "budget: 1200 pcm" in msgs[1].content
    assert "Camden" in msgs[1].content


def test_history_becomes_alternating_human_ai_pairs():
    history = [
        {"user": "hi", "assistant": "hello, how can I help?"},
        {"user": "near UCL", "assistant": "sure, what's your budget?"},
    ]
    msgs = assemble_messages(user_message="about 1200", history=history)
    # system, H, A, H, A, H(current)
    assert isinstance(msgs[0], SystemMessage)
    assert isinstance(msgs[1], HumanMessage) and msgs[1].content == "hi"
    assert isinstance(msgs[2], AIMessage) and msgs[2].content == "hello, how can I help?"
    assert isinstance(msgs[3], HumanMessage) and msgs[3].content == "near UCL"
    assert isinstance(msgs[4], AIMessage) and msgs[4].content == "sure, what's your budget?"
    assert isinstance(msgs[-1], HumanMessage) and msgs[-1].content == "about 1200"


# ---------------------------------------------------------------------------
# No string-prefix leakage — the whole point of the rewrite
# ---------------------------------------------------------------------------

def test_current_message_is_verbatim_no_wrapper():
    msg = "距离可以远一点，可以坐地铁火车，性价比最高的"
    msgs = assemble_messages(
        user_message=msg,
        history=[{"user": "UCL 附近住哪", "assistant": "有几个区域可以考虑"}],
        memory_block="MEM ABOUT USER",
        context_block={"accumulated_criteria": {"area": "Bloomsbury"}})
    current = msgs[-1]
    assert isinstance(current, HumanMessage)
    assert current.content == msg  # exact, no memory/history/prefix concatenation


# ---------------------------------------------------------------------------
# Reply-language directive
# ---------------------------------------------------------------------------

def test_reply_language_zh_directive():
    msgs = assemble_messages(user_message="你好", history=[], reply_language="zh")
    directive = msgs[0].content
    assert "Write the ENTIRE reply in Chinese" in directive


def test_reply_language_en_directive():
    msgs = assemble_messages(user_message="hello", history=[], reply_language="en")
    directive = msgs[0].content
    assert "Write the ENTIRE reply in English" in directive


# ---------------------------------------------------------------------------
# Behaviour rules — assert on module constants, not prose
# ---------------------------------------------------------------------------

def test_behaviour_rules_contain_soft_gate_confirmed_and_no_emoji():
    rules = loop_prompts.behaviour_rules()
    assert loop_prompts.SOFT_GATE_CONFIRMED_MARKER in rules   # "confirmed=true"
    assert loop_prompts.NO_EMOJI_MARKER in rules              # "Never use emoji"


def test_system_directive_embeds_behaviour_rules_and_security():
    directive = loop_prompts.build_system_directive("en")
    assert loop_prompts.SOFT_GATE_CONFIRMED_MARKER in directive
    assert loop_prompts.NO_EMOJI_MARKER in directive
    # Reused verbatim content from langgraph_agent.
    assert "SECURITY & SCOPE" in directive
    assert "YOUR ACTUAL CAPABILITIES" in directive


def test_system_directive_present_in_first_message():
    msgs = assemble_messages(user_message="hi", history=[])
    assert loop_prompts.SOFT_GATE_CONFIRMED_MARKER in msgs[0].content


# ---------------------------------------------------------------------------
# Evidence rendering — focused_property / last_results include address + price
# ---------------------------------------------------------------------------

def test_focused_property_renders_address_and_price():
    record = {"address": "12 Gower St, WC1E", "price": "£1,300 pcm",
              "travel_time": "8 min", "url": "https://x/1"}
    msgs = assemble_messages(
        user_message="is this one pet friendly?",
        history=[],
        context_block={"focused_property": record})
    ctx = msgs[1].content
    assert "12 Gower St, WC1E" in ctx
    assert "£1,300 pcm" in ctx
    assert "FOCUSED PROPERTY" in ctx


def test_last_results_render_numbered_with_address_and_price():
    results = [
        {"address": "1 A Road", "price": "£1000", "travel_time": "10 min"},
        {"address": "2 B Road", "price": "£1100", "travel_time": "20 min"},
    ]
    msgs = assemble_messages(
        user_message="which is cheapest?",
        history=[],
        context_block={"last_results": results})
    ctx = msgs[1].content
    assert "1 A Road" in ctx and "£1000" in ctx
    assert "2 B Road" in ctx and "£1100" in ctx


def test_recommendations_index_renders():
    index = [{"index": 1, "address": "5 C Lane", "price": "£900",
              "url": "https://x/5"}]
    msgs = assemble_messages(
        user_message="tell me about number 1",
        history=[],
        context_block={"recommendations_index": index})
    ctx = msgs[1].content
    assert "5 C Lane" in ctx
    assert "RECOMMENDED LISTINGS INDEX" in ctx


# ---------------------------------------------------------------------------
# Token budget ladder
# ---------------------------------------------------------------------------

def test_history_trimming_keeps_floor_of_two_and_current_message():
    # Many turns + a tiny budget forces trimming down to the 2-turn floor.
    history = [{"user": f"u{i} " + ("x " * 40), "assistant": f"a{i} " + ("y " * 40)}
               for i in range(12)]
    msgs = assemble_messages(
        user_message="FINAL QUESTION", history=history, token_budget=200)
    human_contents = [m.content for m in msgs if isinstance(m, HumanMessage)]
    ai_contents = [m.content for m in msgs if isinstance(m, AIMessage)]
    # Floor of 2 history turns => 2 history HumanMessages + 2 AIMessages, plus current.
    assert len(ai_contents) == 2
    assert len(human_contents) == 3  # 2 history users + the current message
    # The current message is always last and never dropped.
    assert msgs[-1].content == "FINAL QUESTION"
    # The kept turns are the MOST RECENT two.
    assert any("u11" in c for c in human_contents)
    assert any("u10" in c for c in human_contents)


def test_current_message_never_dropped_even_under_zero_budget():
    msgs = assemble_messages(
        user_message="keep me", history=[{"user": "a", "assistant": "b"}],
        token_budget=1)
    assert msgs[-1].content == "keep me"
    assert isinstance(msgs[0], SystemMessage)  # directive never trimmed


def test_memory_block_capped_under_budget():
    big_memory = "\n".join(f"remembered fact line {i} " + ("z " * 20)
                           for i in range(200))
    msgs = assemble_messages(
        user_message="hello", history=[], memory_block=big_memory,
        token_budget=400)
    context_msgs = [m for m in msgs if isinstance(m, SystemMessage)][1:]
    # There is a context message (carries memory); its memory content is capped well
    # under the raw block.
    assert context_msgs, "expected a context/memory system message"
    mem_msg = context_msgs[0].content
    assert estimate_tokens(mem_msg) < estimate_tokens(big_memory)
    assert estimate_tokens(mem_msg) <= 400


def test_untrimmed_when_within_budget():
    history = [{"user": "hi", "assistant": "hello"},
               {"user": "near UCL", "assistant": "budget?"},
               {"user": "1200", "assistant": "ok searching"}]
    msgs = assemble_messages(
        user_message="thanks", history=history, token_budget=6000)
    # All 3 turns retained (nothing trimmed).
    assert len([m for m in msgs if isinstance(m, AIMessage)]) == 3
