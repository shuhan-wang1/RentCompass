"""Defect G9 — an explicit delete must scrub EVERY layer recall_memory can read.

Measured on the 8793c0b internal round: after the user said "Actually, forget my
budget entirely — delete it, don't keep any budget for me.", the semantic fact was
deleted but BOTH episodic records survived. ``recall_memory`` returned ``count: 2``,
one of them the raw "Please remember my budget is £1400 a month.", and the answer read
the deleted figure straight back to the user. The user had been told the deletion
worked when it had not — a correctness AND a user-trust failure.

The contract pinned here:

  * a first-person delete imperative in the user's own message triggers a CROSS-LAYER
    scrub (``forget_fact``): every record for that user that STATES the field's value
    goes, whatever its mtype;
  * value-FREE mentions (the deletion request itself) are retained on purpose and
    COUNTED in the report, so the retention is stated rather than implied;
  * the report's ``complete`` flag is computed by RE-READING the store after the
    delete — an assertion, not a promise;
  * the destructive path is reachable ONLY from the user's own message (an episodic
    role="user" write). A model-initiated ``remember`` (semantic) or an LLM-extracted
    fact can never delete a user's data. No taint/authorization check is relaxed.

Every test runs against a temporary SQLite AgentMemory directory — never the live store.
"""
import importlib
import os
import sys

import pytest


def _pin_app():
    """``tests/`` has no ``__init__.py`` so pytest prepends it to ``sys.path``, where the
    stale scratch copies ``tests/core`` and ``tests/rag`` shadow the real ``app``
    packages. Pin the real root first and evict any shadowed module already imported.
    (Same helper as test_agent_memory_isolation._pin_app.)"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(repo, "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)
    for name in list(sys.modules):
        if name in ("core", "rag") or name.startswith(("core.", "rag.")):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "app" not in path:
                del sys.modules[name]


_pin_app()

am_mod = importlib.import_module("rag.agent_memory")
AgentMemory = am_mod.AgentMemory


def erasure_request_fields(text):
    """Resolved at CALL time, not import time, so this module still COLLECTS against a
    store that lacks the detector — the point of a regression test is that its
    assertions fail on the old behaviour, not that pytest cannot import it."""
    fn = getattr(am_mod, "erasure_request_fields", None)
    assert fn is not None, "rag.agent_memory exposes no erasure_request_fields detector"
    return fn(text)

# The literal production strings from benchmark case G9 (evaluation/benchmark/
# cases_ext_FG.jsonl). Pinned verbatim: the defect was reported against THESE, and a
# paraphrase would not prove the fix.
G9_SAVE = "Please remember my budget is £1400 a month."
G9_DELETE = "Actually, forget my budget entirely — delete it, don't keep any budget for me."
G9_USER = "fg_user_g9"


@pytest.fixture()
def memory(tmp_path, monkeypatch):
    # No LLM calls in unit tests: importance rating / extraction / reflection all go
    # through call_ollama — stub it.
    monkeypatch.setattr(am_mod, "call_ollama", lambda *a, **k: "5")
    return AgentMemory(db_path=str(tmp_path / "chroma_forget"))


def _replay_g9(memory):
    """Exactly how run_benchmark._seed_memory replays a case's conversation_history:
    every prior USER turn is stored as a raw episodic record (run_benchmark.py :949-953)."""
    for content in (G9_SAVE, G9_DELETE):
        memory.add(content, "episodic", session_id=G9_USER, user_id=G9_USER, role="user")


# ------------------------------------------------------- the G9 regression itself

def test_g9_delete_scrubs_the_episodic_echo_of_the_budget(memory):
    """THE defect. Old behaviour: recall returned count 2 including the raw
    "Please remember my budget is £1400 a month." — the deleted figure, handed back."""
    _replay_g9(memory)

    got = memory.retrieve("So what's my budget on file now?", user_id=G9_USER, n=10)
    texts = [m["text"] for m in got]

    # The literal string that survived the delete in production must be gone.
    assert G9_SAVE not in texts, f"deleted budget resurfaced via recall: {texts}"
    # And no record anywhere may still state the figure.
    assert not any("1400" in t for t in texts), f"£1400 still recallable: {texts}"


def test_g9_delete_scrubs_the_semantic_fact_too(memory):
    """The semantic layer was already handled by _consolidate's DELETE op, but the scrub
    must not depend on the LLM having decided to issue one."""
    memory.add("budget £1400/month", "semantic", session_id=G9_USER, user_id=G9_USER)
    _replay_g9(memory)

    texts = [m["text"] for m in memory.retrieve("budget", user_id=G9_USER, n=10)]
    assert "budget £1400/month" not in texts
    assert not any("1400" in t for t in texts), texts


def test_g9_recall_after_delete_supports_the_no_budget_on_file_answer(memory):
    """What retrieve() DOES return after the scrub: the user's own deletion request,
    which carries no figure and is what lets the next turn answer "no budget on file"
    instead of guessing. Retention is deliberate and asserted, not accidental."""
    _replay_g9(memory)

    got = memory.retrieve("what is my budget", user_id=G9_USER, n=10)
    assert [m["text"] for m in got] == [G9_DELETE]
    assert "1400" not in memory.format_for_prompt(got)


def test_erasure_report_is_complete_and_states_what_it_kept(memory):
    _replay_g9(memory)
    report = memory.last_erasure_report

    assert report["fields"] == ("budget",)
    assert report["deleted"] == 1
    assert report["by_layer"] == {"episodic": 1}
    # The value-free deletion request is retained — and SAID so.
    assert report["retained_mentions"] == 1
    # complete is re-read from the store after the delete, not assumed.
    assert report["residual_ids"] == ()
    assert report["complete"] is True


def test_incomplete_scrub_reports_partial_never_success(memory, monkeypatch):
    """A source guard, not a promise: when the delete cannot land, ``complete`` is False
    so the caller must report a PARTIAL deletion. Silently claiming success is the bug."""
    memory.add(G9_SAVE, "episodic", session_id=G9_USER, user_id=G9_USER, role="user")
    monkeypatch.setattr(memory.col, "delete", lambda *a, **k: None)  # delete never lands

    report = memory.forget_fact(G9_USER, ("budget",))
    assert report["complete"] is False
    assert report["residual_ids"], "a surviving record must be reported, not hidden"


def test_forget_fact_is_scoped_to_one_user(memory):
    memory.add("Please remember my budget is £1400 a month.", "episodic",
               user_id="user-A", role="user")
    memory.add("Please remember my budget is £1400 a month.", "episodic",
               user_id="user-B", role="user")

    memory.forget_fact("user-A", ("budget",))

    assert memory.retrieve("budget", user_id="user-A", n=5) == []
    assert [m["text"] for m in memory.retrieve("budget", user_id="user-B", n=5)] == [
        "Please remember my budget is £1400 a month."]


def test_forget_fact_fails_closed_without_a_real_identity(memory):
    """Same strict identity gate as every other read/write path — no shared bucket."""
    memory.add(G9_SAVE, "episodic", user_id="user-A", role="user")
    for bad in (None, "", "   ", "default", "DEFAULT", 7):
        report = memory.forget_fact(bad, ("budget",))
        assert report["deleted"] == 0 and report["complete"] is False
    # user-A's record is untouched by the fail-closed calls.
    assert memory.retrieve("budget", user_id="user-A", n=5)


# ------------------------------------------------------------- the erasure detector

@pytest.mark.parametrize("message", [
    G9_DELETE,
    "delete my budget",
    "forget my budget",
    "remove my rent limit",
    "please don't keep any budget for me",
    "stop storing my budget",
    "忘掉我的预算",
    "删除我的预算吧",
])
def test_erasure_detector_fires_on_a_first_person_delete_imperative(message):
    assert erasure_request_fields(message) == ("budget",)


@pytest.mark.parametrize("message", [
    G9_SAVE,                                    # a SAVE request, not a delete
    "forget about the price of that flat",      # no first-person reference
    "forget it",                                # no field
    "Find me a 2-bed in Camden under £1400",    # no delete verb
    "what's my budget again?",                  # a recall question
    "",
    None,
])
def test_erasure_detector_stays_silent_otherwise(message):
    assert erasure_request_fields(message) == ()


# ----------------------------------------------------- the scrub cannot be weaponised

def test_a_model_initiated_semantic_write_cannot_delete_user_data(memory):
    """Taint/authorization boundary. ``remember`` writes SEMANTIC, so even content that
    reads as a delete imperative must NOT reach the destructive path — otherwise an
    injected instruction could erase a user's memory. Only the user's own message
    (episodic role="user") may scrub."""
    memory.add(G9_SAVE, "episodic", user_id="user-A", role="user")
    memory.last_erasure_report = None

    memory.add("forget my budget", "semantic", user_id="user-A")

    assert memory.last_erasure_report is None, "a semantic write triggered a scrub"
    assert any(G9_SAVE == m["text"] for m in memory.retrieve("budget", user_id="user-A", n=5))


def test_a_tool_derived_episodic_record_cannot_delete_user_data(memory):
    """role != "user" means the text is not the user's own message — no scrub."""
    memory.add(G9_SAVE, "episodic", user_id="user-A", role="user")
    memory.last_erasure_report = None

    memory.add("forget my budget", "episodic", user_id="user-A", role="assistant")

    assert memory.last_erasure_report is None
    assert any(G9_SAVE == m["text"] for m in memory.retrieve("budget", user_id="user-A", n=5))


def test_scrub_spares_a_market_fact_the_user_merely_asked_about(memory):
    """Over-deletion guard: a price the user did not claim as THEIR budget has no
    first-person framing, so it is not a statement of their value."""
    memory.add("User asked: what do Camden rents average? £1800 apparently",
               "episodic", user_id="user-A", role="user")
    memory.add(G9_SAVE, "episodic", user_id="user-A", role="user")

    memory.add("forget my budget", "episodic", user_id="user-A", role="user")

    texts = [m["text"] for m in memory.retrieve("rent budget camden", user_id="user-A", n=10)]
    assert not any("1400" in t for t in texts), texts
    assert any("1800" in t for t in texts), texts


def test_a_delete_request_that_itself_carries_the_value_is_not_reported_as_stored(memory):
    """"forget my £1400 budget" states the figure, so the scrub erases that record too;
    returning its id would claim a record exists that does not."""
    mem_id = memory.add("forget my £1400 budget", "episodic", user_id="user-A", role="user")

    assert mem_id is None
    assert memory.retrieve("budget", user_id="user-A", n=5) == []


def test_forget_me_full_wipe_is_unchanged(memory):
    """The GDPR erasure boundary (/api/forget_me) still wipes everything for a user."""
    memory.add("A fact", "semantic", user_id="user-A")
    memory.add("Another", "episodic", user_id="user-A", role="user")
    memory.add("B fact", "semantic", user_id="user-B")

    assert memory.forget("user-A") == 2
    assert memory.retrieve("fact", user_id="user-A", n=5) == []
    assert [m["text"] for m in memory.retrieve("fact", user_id="user-B", n=5)] == ["B fact"]
