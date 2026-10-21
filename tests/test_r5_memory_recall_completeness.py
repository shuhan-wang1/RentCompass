"""R4-13: a recall question gets every saved fact, and the block says when it does not.

r07 inst-A vol1 (A-F10): 25 x "Please remember that …", then "What do you remember about me? List
all of it." The answer opened "Here is everything I have stored about you" and listed 19. The
memory block took ``semantic_records(...)[:20]``, newest first, so the five oldest facts (budget,
cat Pixel, workplace, east-facing, stairs) never reached the model, and nothing said the list was
capped. (The 20th, "maximum commute is 35 minutes", was then removed by the commute redactor:
an output-layer issue, not this one.)
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest


def _pin_app():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)


_pin_app()
am_mod = importlib.import_module("rag.agent_memory")
USER = "user-r5-recall-vol1"
RECALL = "What do you remember about me? List all of it."

# The 25 semantic rows the vol1 account saved, oldest first (memory_records, user 19b9efe9…;
# the two rows R4-02 had corrupted are shown as they were saved).
VOL1_FACTS = (
    "The user's budget is £1,650 a month.", "The user has a cat named Pixel.",
    "The user works at King's College London Strand campus.", "The user prefers east-facing flats.",
    "The user cannot use stairs more than 2 floors.", "The user needs a washing machine.",
    "The user's move-in date is 1 December 2026.", "The user does not want to live near Kings Cross.",
    "The user is a non-smoker.", "The user cycles to work.",
    "The user needs a desk space for working from home (WFH).",
    "The user prefers areas with a farmers market.", "The user's guarantor is in Manchester.",
    "The user has a car and needs parking.", "The user likes Victorian conversions.",
    "The user's maximum commute is 35 minutes.", "The user dislikes basement flats.",
    "The user wants a dishwasher.", "The user prefers a landlord rather than an agency.",
    "The user wants a 12 month lease only.", "The user likes being near a gym.",
    "The user needs fast broadband.", "The user's partner works in Canary Wharf.",
    "The user wants a balcony.", "The user prefers quiet streets.",
)


@pytest.fixture
def vol1_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "5")
    memory = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    for index, fact in enumerate(VOL1_FACTS):
        memory.add(fact, "semantic", session_id=USER, user_id=USER,
                   extra_meta={"source": am_mod.SOURCE_EXPLICIT})
        # created_at as recorded: one fact per turn, oldest first
        got = memory.col.get(where={"$and": [{"user_id": USER}, {"hash": am_mod.hashlib.md5(
            f"semantic|{USER}|{fact}".encode()).hexdigest()}]})
        meta = dict(got["metadatas"][0], created_at=f"2026-10-20T04:{26 + index // 6:02d}:{index % 6 * 9:02d}")
        memory.col.update(ids=got["ids"], metadatas=[meta])
    memory.add("The user wants a practical home base; a washing machine and a desk seem to matter.",
               "reflection", session_id=USER, user_id=USER, importance=8)
    return memory


def _recall_block(memory):
    import app as appmod
    mems = memory.retrieve(RECALL, session_id=USER, user_id=USER, n=6)
    mems, total = appmod._with_all_saved_facts(memory, USER, mems)
    return memory.format_for_prompt(mems, saved_total=total)


def test_r4_13_the_recall_block_lists_all_25_saved_facts(vol1_memory):
    block = _recall_block(vol1_memory)
    missing = [fact for fact in VOL1_FACTS if fact not in block]
    assert missing == []
    assert "all 25 saved facts are listed" in block


def test_r4_13_a_capped_list_says_it_is_not_complete(vol1_memory, monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod, "_RECALL_FACTS_MAX", 20)
    block = _recall_block(vol1_memory)
    assert "showing" in block and "of 25 saved facts; this list is NOT complete" in block
    assert "all 25 saved facts are listed" not in block


def test_r4_13_the_fc_prompt_keeps_every_fact_of_the_vol1_recall(vol1_memory):
    from core.context_assembler import assemble_messages
    history = [{"user": f"Please remember that {fact[9:]}", "assistant": "I've saved that to memory."}
               for fact in VOL1_FACTS]
    msgs = assemble_messages(user_message=RECALL, history=history,
                             memory_block=_recall_block(vol1_memory), token_budget=6000)
    packet = "\n".join(m.content for m in msgs if "WHAT I REMEMBER" in (m.content or ""))
    assert [fact for fact in VOL1_FACTS if fact not in packet] == []
    assert "were cut for space" not in packet


def test_r4_13_a_memory_block_cut_for_space_says_so(vol1_memory):
    from core.context_assembler import assemble_messages
    msgs = assemble_messages(user_message=RECALL, history=[],
                             memory_block=_recall_block(vol1_memory), token_budget=1200)
    packet = "\n".join(m.content for m in msgs if "WHAT I REMEMBER" in (m.content or ""))
    assert "more remembered line(s) were cut for space" in packet
    assert "INCOMPLETE: never say it is everything you have stored" in packet
    assert sum(fact in packet for fact in VOL1_FACTS) < len(VOL1_FACTS)


def test_r4_13_the_legacy_assembler_also_states_the_cut():
    from core.context_assembler import assemble
    block = "What I remember about this user:\n" + "\n".join(f"[FACT] {f}" for f in VOL1_FACTS)
    out = assemble(user_message=RECALL, history=[], memory_block=block, token_budget=300)
    assert "were cut for space" in out and RECALL in out


def test_r4_13_the_recall_tool_never_reads_as_the_whole_store(vol1_memory, monkeypatch):
    import core.tools.memory_tools as mt
    monkeypatch.setattr(mt, "_mem", lambda: vol1_memory)
    result = mt.recall_memory_impl(RECALL, user_id=USER, n=6)
    assert result["count"] <= 6
    assert "not a complete list" in result["completeness"]
    assert "25 saved facts in total" in result["completeness"]
