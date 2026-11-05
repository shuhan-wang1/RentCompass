"""The results panel never says "No properties found" while a search is still running.

A tester saw "No properties found" in the results panel while their search was in
progress and read it as the final answer (2026-11-05). The panel now shows a localized
searching/working state while any pane has a chat turn or a criteria search in flight,
keeps the previous turn's cards (with a busy bar) when it has some, shows a neutral
"your matches will appear here" before any search has completed, and shows the
empty-results message only once a completed response has zero results.

There is no JavaScript test harness in this repo, so these tests read the page source,
the way the other unified-ui.html tests do.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "unified-ui.html").read_text(encoding="utf-8")
SCRIPT = PAGE[PAGE.index("<script>"):PAGE.rindex("</script>")]


def _function(name: str) -> str:
    """The source of the top-level function ``name`` (brace-matched)."""
    match = re.search(r"\n        (?:async )?function " + re.escape(name) + r"\(", SCRIPT)
    assert match, f"function {name} not found"
    start = SCRIPT.index("{", match.end())
    depth = 0
    for i in range(start, len(SCRIPT)):
        if SCRIPT[i] == "{":
            depth += 1
        elif SCRIPT[i] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[match.start():i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _translation(lang_index: int, key: str) -> str:
    values = re.findall(rf'\n\s+{key}: "([^"]*)",', SCRIPT)
    assert len(values) == 2, f"{key} must be defined once for en and once for zh"
    return values[lang_index]


NEW_KEYS = ("results_searching_title", "results_working_title", "results_busy_desc",
            "results_updating", "results_idle_title", "results_idle_desc")


@pytest.mark.parametrize("key", NEW_KEYS)
def test_the_new_panel_states_are_localized(key):
    en, zh = _translation(0, key), _translation(1, key)
    assert en and re.search(r"[A-Za-z]", en)
    assert re.search(r"[一-鿿]", zh), zh


def test_busy_means_a_turn_or_a_criteria_search_in_flight_in_any_pane():
    body = _function("resultsPanelBusy")
    assert "allPanes().some(p => p && p.isProcessing)" in body
    # The direct (criteria) search says "searching"; a chat turn says "working".
    state = _function("buildResultsBusyState")
    assert "p.processingKind === 'search'" in state
    assert "'results_searching_title'" in state and "'results_working_title'" in state
    assert "'results_busy_desc'" in state and "role', 'status'" in state


def test_an_empty_panel_mid_search_shows_the_busy_state_not_no_results():
    body = _function("displayResults")
    assert "const busy = resultsPanelBusy();" in body
    empty = body[body.index("if (!recommendations || recommendations.length === 0) {"):]
    busy_at = empty.index("if (busy) {")
    assert "content.appendChild(buildResultsBusyState());" in empty[busy_at:busy_at + 120]
    assert busy_at < empty.index("t('no_results_title')")
    assert busy_at < empty.index("t('search_incomplete_title')")


def test_nothing_is_reported_missing_before_a_search_has_completed():
    body = _function("displayResults")
    empty = body[body.index("if (!recommendations || recommendations.length === 0) {"):]
    idle_at = empty.index("if (!(resultsOwnerPane && resultsOwnerPane.retrieval)) {")
    assert "t('results_idle_title'), t('results_idle_desc')" in empty[idle_at:idle_at + 200]
    assert idle_at < empty.index("t('no_results_title')")
    # Every completed search marks its pane, so a real zero still says "No properties found".
    apply_state = _function("applyResponseState")
    assert "pane.retrieval = state.retrieval || null;" in apply_state
    assert "pane.retrieval = {source_status: data.source_status" in apply_state
    direct = _function("submitCriteriaSearch")
    marked = direct.index("pane.retrieval = {source_status: data.source_status")
    assert marked < direct.index("paintResults(pane, recs, data.area_recommendations)")
    assert marked < direct.index("paintResults(pane, [], data.area_recommendations)")


def test_previous_cards_stay_with_a_busy_bar_between_turns():
    sync = _function("syncResultsBusy")
    # With cards on screen only the bar is added or removed: no repaint, no empty state.
    cards = sync[sync.index("if (!Array.isArray(currentResults) || !currentResults.length) {"):]
    cards = cards[cards.index("return;") + len("return;"):]
    assert "displayResults(" not in cards
    assert "content.insertBefore(buildResultsBusyBar(), content.firstChild)" in cards
    assert "bar.remove()" in cards
    assert "if (favoritesTabActive()) return;" in sync      # the Saved tab is left alone
    # A full repaint while busy (language switch, owner change) keeps the bar too.
    body = _function("displayResults")
    assert "if (busy) content.insertBefore(buildResultsBusyBar(), content.firstChild);" in body
    assert "'results_updating'" in _function("buildResultsBusyBar")


@pytest.mark.parametrize("name,kind", [("sendMessage", "chat"), ("submitCriteriaSearch", "search")])
def test_starting_and_finishing_a_request_updates_the_panel(name, kind):
    body = _function(name)
    start = body.index("pane.isProcessing = true;")
    assert f"pane.processingKind = '{kind}';" in body[start:start + 160]
    assert "syncResultsBusy();" in body[start:start + 260]
    finally_block = body[body.rindex("} finally {"):]
    done = finally_block.index("pane.isProcessing = false;")
    assert "syncResultsBusy();" in finally_block[done:done + 260]


def test_every_path_that_ends_a_request_repaints_the_panel():
    # Whatever stops a request (cancel, conversation switch, new chat, closing a pane,
    # identity switch) must leave the panel showing a settled state, not "working…".
    headers = [(m.start(), m.group(1)) for m in
               re.finditer(r"\n        (?:async )?function (\w+)\(", SCRIPT)]
    checked = set()
    for stop in re.finditer(r"(?<!this)\.isProcessing = false;", SCRIPT):   # not the constructor
        name = [n for at, n in headers if at < stop.start()][-1]
        checked.add(name)
        body = _function(name)
        assert re.search(r"syncResultsBusy\(\)|displayResults\(|startBlankConversation\(", body), name
    assert {"sendMessage", "submitCriteriaSearch", "switchConversation",
            "startBlankConversation", "newChat", "applyIdentitySwitch"} <= checked
    switch = _function("switchConversation")
    assert "syncResultsBusy();" in switch[switch.rindex("} finally {"):]


def test_the_pane_tracks_what_kind_of_request_is_running():
    ctor = _function("ChatPane")
    assert "this.processingKind = null;" in ctor
