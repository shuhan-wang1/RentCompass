"""ISSUE #78 (A) — a reply that re-narrates listings must repaint the panel it narrates.

Reported 2026-08-03. A turn that calls NO tool still has the previous turn's listings in
its context (we put them there — see _build_results_context), so the model can answer "show
me those flats again" by re-narrating them. tool_data is then empty, /api/alex ships no
recommendations, and the frontend's paint branch never runs.

In the reported conversation (1541eacc, 06:04:36, llm_calls=1 tool_batches=0) the chat
listed five properties while the right-hand panel still showed the empty result of a form
search run 70 seconds earlier.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# --- Pin the real source roots ahead of tests/ (stale shadow copies live under tests/).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

os.environ["CONVERSATION_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="issue78_a_"), "conversations.sqlite3")
os.environ["USE_MCP_TOOLS"] = "0"
os.environ["PROPERTY_SOURCE"] = "csv"
os.environ.setdefault("AGENT_MEMORY_ENABLED", "0")

import app as appmod  # noqa: E402 — heavy one-time import after env setup
from app import _narrates_cached_listings  # noqa: E402


LAST_RESULTS = [
    {"address": "Woburn Place, London WC1H", "price": "£1800/month"},
    {"address": "Witley Court, London, WC1N", "price": "£2400/month"},
    {"address": "Woburn Place, London WC1H", "price": "£1900/month"},
    {"address": "Tavistock Square, London WC1H", "price": "£1950/month"},
    {"address": "Cartwright Gardens", "price": "£1972/month"},
]

# The actual 06:04:36 reply from the report, trimmed to the listing enumeration.
REPORTED_REPLY = (
    "我为您重新搜索了去 UCL 通勤 20 分钟内的 Studio 或 1b1b 公寓。以下是符合条件的房源：\n"
    "**1. Woburn Place, London WC1H（Bloomsbury 区）— £1,800/月**\n"
    "**2. Witley Court, London WC1N（Camden 区）— £2,400/月**\n"
    "**3. Woburn Place, London WC1H（Bloomsbury 区）— £1,900/月**\n"
    "**4. Tavistock Square, London WC1H（Bloomsbury 区）— £1,950/月**\n"
    "**5. Cartwright Gardens（Camden 区）— £1,972/月**\n"
)


# ── the detector ────────────────────────────────────────────────────────────────
def test_reported_reply_is_detected_as_a_re_narration():
    assert _narrates_cached_listings(REPORTED_REPLY, LAST_RESULTS) is True


def test_single_listing_follow_up_does_not_repaint():
    """"Tell me more about Woburn Place" names ONE listing. Repainting there would clobber
    a newer result set the user is looking at, so one hit must not be enough."""
    reply = "Woburn Place is a studio on the fourth floor of Russell Court, with a concierge."
    assert _narrates_cached_listings(reply, LAST_RESULTS) is False


def test_duplicate_address_cannot_reach_the_threshold_alone():
    """The cached set holds "Woburn Place" twice. Counting rows rather than DISTINCT names
    would let a single-listing answer trip the 2-hit threshold."""
    cached = [
        {"address": "Woburn Place, London WC1H", "price": "£1800/month"},
        {"address": "Woburn Place, London WC1H", "price": "£1900/month"},
    ]
    assert _narrates_cached_listings("Woburn Place has 24h concierge.", cached) is False


def test_prose_without_listings_does_not_repaint():
    reply = "Bloomsbury is expensive. Would you like me to widen the search to Camden?"
    assert _narrates_cached_listings(reply, LAST_RESULTS) is False


def test_empty_inputs_are_safe():
    assert _narrates_cached_listings("", LAST_RESULTS) is False
    assert _narrates_cached_listings(REPORTED_REPLY, []) is False
    assert _narrates_cached_listings(REPORTED_REPLY, None) is False
    assert _narrates_cached_listings(REPORTED_REPLY, [None, "junk", {}]) is False


def test_short_names_are_not_matched():
    """A 3-letter area name would hit on any stray occurrence in prose."""
    cached = [{"address": "Bow, London E3"}, {"address": "Kew, London TW9"}]
    assert _narrates_cached_listings("I'll bow to your budget and go to Kew Gardens.",
                                     cached) is False


# ── the wiring: the turn payload actually carries the re-attached set ────────────
class _FakeGraph:
    """Stands in for the compiled graph: a turn that called NO tool, so tool_data is empty
    while final_response still enumerates the previous turn's listings."""

    def __init__(self, final_response):
        self._final_response = final_response

    async def ainvoke(self, graph_input, config=None):
        return {"final_response": self._final_response,
                "response_type": "answer", "tool_data": {}}


def _run_turn(monkeypatch, reply, cached, *, conversation_id):
    monkeypatch.setattr(appmod, "agent_graph", _FakeGraph(reply))
    user_id = "issue78-user"
    appmod._get_session(user_id, conversation_id).last_results = list(cached)
    payload = asyncio.run(appmod.handle_with_react_agent(
        "显示去 UCL 通勤 20 分钟内的公寓,studio单间或者1b1b", {}, False,
        user_id=user_id, conversation_id=conversation_id,
        request_id=f"req-{conversation_id}", ui_language="en"))
    return payload, appmod._get_session(user_id, conversation_id)


def test_a_toolless_re_narration_ships_a_search_payload(monkeypatch):
    """The reported turn end to end: the graph returns tool_data={} and a reply listing
    properties; the payload must still carry them or the panel never repaints."""
    payload, _ = _run_turn(monkeypatch, REPORTED_REPLY, LAST_RESULTS,
                           conversation_id="c-renarration")
    assert payload["response_type"] == "search"
    assert [r["address"] for r in payload["recommendations"]] == \
        [r["address"] for r in LAST_RESULTS]


def test_the_re_attached_set_reaches_the_write_back(monkeypatch):
    """Placement matters: the re-attach has to run BEFORE _write_back_turn, or the stored
    turn keeps no listings and a page reload reproduces the empty panel."""
    _, sess = _run_turn(monkeypatch, REPORTED_REPLY, LAST_RESULTS,
                        conversation_id="c-writeback")
    stored = sess.persistent_state.get('extracted_context', {}).get('last_results')
    assert stored, "write-back saw no recommendations — re-attach ran too late"


def test_a_toolless_answer_without_listings_stays_an_answer(monkeypatch):
    """Prose about the area must not drag the previous result set onto the panel."""
    payload, _ = _run_turn(
        monkeypatch,
        "Bloomsbury is expensive. Shall I widen the search to Camden or Islington?",
        LAST_RESULTS, conversation_id="c-prose")
    assert payload["response_type"] != "search"
    assert not payload.get("recommendations")
