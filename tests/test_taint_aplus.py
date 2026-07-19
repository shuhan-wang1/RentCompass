"""Taint policy A+ (design §2.8c) — unit coverage for the write-memory gate.

Scope (Agent G ownership):
  * ``core.memory_gate.user_authorizes_memory`` — zh+en positives and negatives;
    recall questions ("do you remember", "还记得吗") are NOT authorization.
  * ``core.memory_gate.memory_write_allowed`` — the A+ truth table.
  * ``core.memory_gate.freeze_pending_write`` / ``consume_pending_write`` —
    round-trip, wrong digest, double-consume, cross-session isolation, single
    consumption.
  * ``uk_rent_agent.agent.guardrails.tool_allowed`` — default flip to deny + legacy
    ``allow_tainted_memory=True`` preserved.
  * ``rag.agent_memory`` tainted auto-memory bypass — assistant text excluded from
    extraction input on a tainted turn, clean path unchanged, episodic still
    written when tainted.

No live LLM: ``call_ollama`` is stubbed and ``_extract_facts`` monkeypatched.
"""
import importlib
import os
import sys

import pytest


def _pin_app():
    """Pin the real ``app`` root first and evict any shadowed core/rag modules
    (mirrors tests/test_agent_memory_branching._pin_app)."""
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
guardrails = importlib.import_module("uk_rent_agent.agent.guardrails")
am_mod = importlib.import_module("rag.agent_memory")
AgentMemory = am_mod.AgentMemory


# ------------------------------------------------------------- user_authorizes_memory

@pytest.mark.parametrize("msg", [
    # English imperative "save this" cues
    "Please remember that my budget is 1500 pcm",
    "Remember this address for later",
    "remember to book the viewing on Friday",
    "Note that I prefer a ground floor flat",
    "make a note that I have a dog",
    "save this listing please",
    "Keep in mind I need parking",
    "Don't forget my move-in date is August 1st",
    # Chinese
    "记住我的预算是1500镑",
    "帮我记一下这个地址",
    "记录一下我要一楼",
    "别忘了我下个月搬家",
    "存一下这个房源",
])
def test_user_authorizes_positive(msg):
    assert mg.user_authorizes_memory(msg) is True


@pytest.mark.parametrize("msg", [
    # ordinary informational messages
    "I want to live near UCL",
    "What is the cheapest flat available?",
    "我想住在UCL附近",
    "预算大概1500镑一个月",
    # recall QUESTIONS — asking what we know, not asking to save
    "Do you remember my budget?",
    "can you remember what I said earlier?",
    "you remember the flat we looked at?",
    "remember when I mentioned the budget?",
    "你还记得我的预算吗",
    "记不记得我要一楼",
    "是否记得我说过的话",
    # empty / whitespace
    "",
    "   ",
])
def test_user_authorizes_negative(msg):
    assert mg.user_authorizes_memory(msg) is False


# ---------------------------------------------------------------- is_pure_recall_question (H12)

@pytest.mark.parametrize("msg", [
    # zh recall questions — no store intent
    "你还记得我的预算吗",
    "还记得我要一楼吗",
    "记得我的预算是多少吗",
    "你记不记得我说过什么",
    "我之前说过什么预算来着",
    "上次说的那个区域是哪里",
    "我说过我想住哪吗",
    # en recall questions
    "Do you remember my budget?",
    "do you recall what I said earlier?",
    "you remember the flat we looked at?",
    "What did I say my budget was?",
    "did I tell you my move-in date?",
    "what's my budget again?",
    # recall question quoting numbers from history (still a question, not a save)
    "你还记得我说过预算是1500吗",
    "did I say my budget was 1500?",
])
def test_is_pure_recall_question_true(msg):
    assert mg.is_pure_recall_question(msg) is True


@pytest.mark.parametrize("msg", [
    # mixed recall + store — store intent WINS, must NOT be blocked (product ruling)
    "回忆一下我的预算，另外记住我现在想要两居室",
    "你还记得我的预算吗？顺便记住我现在要两居室",
    "what's my budget again? also remember I now want a 2-bed",
    "do you remember my budget? please remember I have a dog now",
    # plain new-fact statements — not a recall question at all
    "我预算1500",
    "I want to live near UCL",
    "预算大概1500镑一个月",
    # explicit store commands — a save, not a recall
    "记住我的预算是1500",
    "Please remember that my budget is 1500 pcm",
    "帮我记一下这个地址",
    # empty / whitespace
    "",
    "   ",
])
def test_is_pure_recall_question_false(msg):
    assert mg.is_pure_recall_question(msg) is False


# ---------------------------------------------------------------- memory_write_allowed

@pytest.mark.parametrize("tainted,authorized,expected", [
    (False, False, True),   # clean turn always allowed
    (False, True, True),
    (True, False, False),   # tainted + unauthorized → the only deny
    (True, True, True),     # tainted but user explicitly authorized
])
def test_memory_write_allowed_truth_table(tainted, authorized, expected):
    assert mg.memory_write_allowed(
        context_tainted=tainted, user_authorized=authorized) is expected


# --------------------------------------------------------- content_is_user_stated (rule 2 refine)

@pytest.mark.parametrize("content,msg", [
    # cross-language number-anchored: content echoes only generic budget words → number anchors
    ("budget £1400/month", "记住我预算1400"),
    ("user budget 1500 pcm", "Please remember my budget is 1500 pcm"),
    # thousands-comma / currency normalisation
    ("max rent £1,400 per month", "记住我预算1400"),
    # en distinguishing nouns present in the message
    ("user has a dog and needs parking", "Remember that I have a dog and need parking"),
    # pure CJK: distinguishing bigrams overlap the message
    ("预算上限1400镑", "记住我预算上限1400镑"),
])
def test_content_is_user_stated_positive(content, msg):
    assert mg.content_is_user_stated(content, msg) is True


@pytest.mark.parametrize("content,msg", [
    # H13: tool-derived scraped price the user never typed (no number in the message)
    ("£950 (cheapest flat found in Camden)",
     "搜下 Camden 的房子，顺便把你找到的最便宜那套的价格记住"),
    ("the cheapest flat is £950 per month", "记住最便宜那套的价格"),
    # numbers match but the distinguishing text is unrelated to the message
    ("flat viewing at 1400 Camden Road", "记住我预算1400"),
    ("user prefers a studio on Baker Street", "记住我预算1400"),
    # CJK number-anchored but scraped figure absent from the message
    ("最便宜房源950镑", "记住最便宜那套"),
    # nothing to anchor on: no number, no shared distinguishing token
    ("north-facing garden flat", "记住我预算1400"),
    # empty / whitespace content
    ("", "记住这个"),
    ("   ", "记住这个"),
])
def test_content_is_user_stated_negative(content, msg):
    assert mg.content_is_user_stated(content, msg) is False


# ------------------------------------------------------------------------ write_authorization

def test_write_authorization_cue_and_user_stated():
    # cue present AND content derivable from the message → authorized
    assert mg.write_authorization("记住我预算1400", "budget £1400/month") is True


def test_write_authorization_cue_but_tool_derived_content():
    # H13: cue present, but the content is a scraped price → NOT authorized
    assert mg.write_authorization(
        "搜下 Camden 的房子，顺便把你找到的最便宜那套的价格记住",
        "£950 (cheapest flat found in Camden)") is False


def test_write_authorization_user_stated_but_no_cue():
    # content matches the message but there is no save cue → NOT authorized
    assert mg.write_authorization("我的预算是1400镑", "预算1400镑") is False


# ------------------------------------------------------------- freeze / consume ledger

@pytest.fixture()
def gate_store(tmp_path, monkeypatch):
    """Point the module singleton at an isolated sqlite file."""
    store = mg._PendingWriteStore(tmp_path / "memory_gate.sqlite3")
    monkeypatch.setattr(mg, "_STORE", store)
    return store


def test_freeze_consume_roundtrip(gate_store):
    digest = mg.freeze_pending_write("sess-1", "user hates east London", "semantic")
    assert isinstance(digest, str) and len(digest) == 64  # sha256 hex
    got = mg.consume_pending_write("sess-1", digest)
    assert got == {"content": "user hates east London", "kind": "semantic"}


def test_consume_wrong_digest_returns_none(gate_store):
    mg.freeze_pending_write("sess-1", "content A", "semantic")
    assert mg.consume_pending_write("sess-1", "deadbeef" * 8) is None


def test_double_consume_returns_none(gate_store):
    digest = mg.freeze_pending_write("sess-1", "content A", "semantic")
    assert mg.consume_pending_write("sess-1", digest) is not None
    # single-consumption: the frozen candidate is replayed exactly once
    assert mg.consume_pending_write("sess-1", digest) is None


def test_cross_session_isolation(gate_store):
    digest = mg.freeze_pending_write("sess-1", "content A", "semantic")
    # a different session cannot consume another session's frozen candidate
    assert mg.consume_pending_write("sess-2", digest) is None
    # the owning session still can
    assert mg.consume_pending_write("sess-1", digest) == {
        "content": "content A", "kind": "semantic"}


def test_confirmation_message_bilingual():
    en = mg.pending_confirmation_message("budget is 1500", "en")
    zh = mg.pending_confirmation_message("budget is 1500", "zh")
    assert "budget is 1500" in en and "budget is 1500" in zh
    assert en != zh
    # no emoji anywhere
    assert all(ord(ch) < 0x1F000 for ch in en + zh)


# --------------------------------------------------------------- guardrails default flip

def test_guardrails_default_denies_tainted_remember():
    # A+ rule 1: model-initiated remember in a tainted session is denied by default.
    assert guardrails.tool_allowed(
        side_effect="write", context_tainted=True, tool_name="remember") is False


def test_guardrails_legacy_allow_flag_preserved():
    # Legacy callers explicitly passing allow_tainted_memory=True keep old behaviour.
    assert guardrails.tool_allowed(
        side_effect="write", context_tainted=True, tool_name="remember",
        allow_tainted_memory=True) is True


def test_guardrails_clean_turn_and_non_write_allowed():
    # untainted turn → allowed regardless of tool
    assert guardrails.tool_allowed(
        side_effect="write", context_tainted=False, tool_name="remember") is True
    # read-only tool in a tainted turn → allowed
    assert guardrails.tool_allowed(
        side_effect="none", context_tainted=True, tool_name="remember") is True


# ------------------------------------------------------- agent_memory tainted bypass

@pytest.fixture()
def memory(tmp_path, monkeypatch):
    # No LLM calls: reflection / consolidate / importance rating all route through
    # call_ollama — stub it. Extraction is monkeypatched per-test to capture args.
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: '{"facts": []}')
    return AgentMemory(db_path=str(tmp_path / "chroma_mem"))


_USER_MSG = "My budget is 1500 pcm and I want to live near UCL"
_ASSISTANT_MSG = "Here are three flats near UCL within your budget."


def test_tainted_turn_excludes_assistant_from_extraction(memory, monkeypatch):
    captured = {}

    def _capture(user_msg, assistant_msg):
        captured["user"] = user_msg
        captured["assistant"] = assistant_msg
        return []

    monkeypatch.setattr(memory, "_extract_facts", _capture)
    memory.remember_turn(
        _USER_MSG, _ASSISTANT_MSG, session_id="s", user_id="user_1",
        context_tainted=True,
    )
    assert captured["user"] == _USER_MSG
    # assistant/tool output must not reach the extractor on a tainted turn
    assert captured["assistant"] == ""


def test_clean_turn_feeds_assistant_to_extraction(memory, monkeypatch):
    captured = {}

    def _capture(user_msg, assistant_msg):
        captured["user"] = user_msg
        captured["assistant"] = assistant_msg
        return []

    monkeypatch.setattr(memory, "_extract_facts", _capture)
    memory.remember_turn(
        _USER_MSG, _ASSISTANT_MSG, session_id="s", user_id="user_1",
        context_tainted=False,
    )
    assert captured["user"] == _USER_MSG
    assert captured["assistant"] == _ASSISTANT_MSG


def test_episodic_written_when_tainted(memory, monkeypatch):
    monkeypatch.setattr(memory, "_extract_facts", lambda u, a: [])
    memory.remember_turn(
        _USER_MSG, _ASSISTANT_MSG, session_id="s", user_id="user_1",
        context_tainted=True,
    )
    rows = memory.col.get(where={"$and": [
        {"user_id": "user_1"}, {"mtype": "episodic"}]})
    docs = rows.get("documents") or []
    assert any("near UCL" in d for d in docs)
