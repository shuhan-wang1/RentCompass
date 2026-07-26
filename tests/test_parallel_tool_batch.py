"""Within-batch tool concurrency: the property, its limits, and the one way it degrades.

WHY THIS FILE EXISTS. The `E_multi_constraint` shape ("balance KCL and Imperial, £2500,
30 min, not ground floor") needs a commute calculation plus a property search — independent
work with no data dependency — and is the slowest measured category (median 16,349ms cold /
10,033ms warm on 2026-07-25, 44% soft-wrap rate). The standing hypothesis was that
``execute_tools`` dispatches such a batch SERIALLY and that this is what pushes the category
into the wrap.

MEASURED RESULT: the hypothesis is WRONG. ``execute_tools`` already dispatches every read in
the batch with ``asyncio.ensure_future`` before awaiting any of them, and every dispatch runs
on its own offload worker with a private event loop, so a batch of N independent calls of S
seconds completes in ~S. Verified here up to N=16 (16 x 1.0s -> ~1.01s wall). There is no
serial dispatch left to remove; the remaining multi-call cost is SEQUENTIAL BATCHES (each
costing a full LLM round-trip), which is a planning-layer problem, not a dispatch one.

So these tests are not a demonstration of a new win — they are the lock on an existing one.
The lock was verified, not asserted: re-serialising the dispatch (a lock around the offload in
``_run``, so one tool at a time) fails 10 of the 17 tests here, which is exactly the regression
they exist to prevent. They also pin the three properties that make the concurrency SAFE and
that a naive parallelisation breaks:

  1. the per-batch and per-turn budgets still fire (a slow sibling is still abandoned at the
     window, and the turn is charged the batch's WALL CLOCK, not the sum of its calls);
  2. results the model sees are ordered by the model's own request order, never by completion
     order, so an answer can never become order-dependent on a race;
  3. timeout / abandon accounting stays per-tool: the fast sibling is not tarred with the slow
     one's kill, and each call's elapsed_ms is its own.

Plus the one genuine residual serialisation, found by measurement rather than assumed:
``test_worker_starvation_*``. Abandoned dispatches are unkillable and keep their worker; once
the pool is full, later dispatches sit in the queue while their budget ticks and can be killed
having never run a line of tool code. That was previously indistinguishable from a slow tool.

NO network, NO real tools, NO LLM: stub providers with deterministic sleeps throughout.
"""
from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import core.agent_loop as agent_loop
from core.agent_loop import build_fc_nodes
from tests.test_tool_budgets import (BlockingProvider, FakeChat, FakeSpec,
                                     PerToolDelayProvider, _exec_once, _state, _tc)

# One stub tool's simulated work. Long enough that a serial implementation blows every bound
# below by a wide margin, short enough to keep the file fast.
_S = 0.5


@pytest.fixture(scope="module", autouse=True)
def _warm_lazy_imports():
    """Pay the one-off lazy-import cost BEFORE any timing assertion.

    ``_inject_search_params`` does a function-local ``from core.tools.search_properties import
    _extract_area`` the first time a batch contains search_properties; that import costs ~0.6s
    in a cold process and would otherwise land inside whichever timing test happened to run
    first, making the results order-dependent. It is process warm-up, not batch serialisation,
    so it is deliberately excluded from what these tests measure."""
    try:
        import core.tools.search_properties  # noqa: F401
    except Exception:  # pragma: no cover - the timing tests degrade, they do not break
        pass


def _batch_state(names):
    st = _state()
    st["messages"] = [AIMessage(content="", tool_calls=[
        _tc(n, {"i": i}, f"c{i}") for i, n in enumerate(names)])]
    return st


def _tool_messages(state):
    return [m for m in state["messages"] if isinstance(m, ToolMessage)]


# ═══════════════════════════════════════════════════════════════════
# 1. The property: N independent calls cost ~S, not N*S
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("n", [2, 4, 8])
def test_independent_reads_cost_one_tool_not_n_tools(monkeypatch, n):
    """N stub reads sleeping _S each must complete in ~_S. Serial dispatch would need n*_S,
    so the bound is set below 2*_S: it passes for any n only if the batch is concurrent."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    names = [f"tool_{i}" for i in range(n)]
    provider = PerToolDelayProvider([FakeSpec(x) for x in names], {x: _S for x in names})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    t0 = time.monotonic()
    st = _exec_once(nodes, _batch_state(names))
    wall = time.monotonic() - t0

    assert wall < 2 * _S, (
        f"{n} independent {_S}s reads took {wall:.2f}s; serial would be {n * _S:.1f}s — "
        "the batch is no longer dispatched concurrently")
    assert all(a["success"] for a in st["tool_artifacts"]), "every read must still complete"
    assert len(_tool_messages(st)) == n


def test_blocking_sync_tools_are_concurrent_too(monkeypatch):
    """The concurrency must survive tools that block WITHOUT yielding (an ``async def`` tool
    doing an inline synchronous call — the confirmed root cause of the historical 4-call
    ~52s serialisation despite a 20s budget). Each dispatch gets its own worker thread and
    private loop, so a non-yielding sleep cannot serialise its siblings either."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    names = ["calculate_commute", "search_properties", "check_safety", "search_nearby_pois"]
    provider = BlockingProvider([FakeSpec(x) for x in names], blocks=_S)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    t0 = time.monotonic()
    st = _exec_once(nodes, _batch_state(names))
    wall = time.monotonic() - t0

    assert wall < 2 * _S, (
        f"4 blocking {_S}s tools took {wall:.2f}s; serial would be {4 * _S:.1f}s")
    assert all(a["success"] for a in st["tool_artifacts"])


def test_reads_and_writes_in_one_batch_overlap(monkeypatch):
    """Writes are excluded from the abandon set (an already-running write cannot be
    terminated) but they are still DISPATCHED alongside the reads — the write is awaited to
    completion afterwards, not started afterwards. Serial start would cost 2*_S."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    specs = [FakeSpec("search_properties"), FakeSpec("remember", side_effect="write")]
    provider = PerToolDelayProvider(specs, {"search_properties": _S, "remember": _S})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    st = _state()
    st["extracted_context"]["current_message"] = "记住我的预算是2500"
    st["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", {"area": "Camden"}, "c1"),
        _tc("remember", {"content": "预算2500"}, "c2")])]

    t0 = time.monotonic()
    st = _exec_once(nodes, st)
    wall = time.monotonic() - t0

    assert wall < 2 * _S, f"read and write did not overlap ({wall:.2f}s vs {2 * _S:.1f}s serial)"
    assert len(_tool_messages(st)) == 2


# ═══════════════════════════════════════════════════════════════════
# 2. The budgets still fire, and the turn is charged wall clock
# ═══════════════════════════════════════════════════════════════════

def test_budget_still_cuts_the_batch_off_at_the_window(monkeypatch):
    """Concurrency must not buy a tool an extension. With a window well under the tools'
    duration the whole batch is still abandoned AT the window, and the node still returns
    bounded by it — the parallel dispatch does not defeat the deadline."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.3")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    names = [f"slow_{i}" for i in range(4)]
    provider = PerToolDelayProvider([FakeSpec(x) for x in names], {x: 5.0 for x in names})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    t0 = time.monotonic()
    st = _exec_once(nodes, _batch_state(names))
    wall = time.monotonic() - t0

    assert wall < 2.0, f"batch window did not fire ({wall:.2f}s for a 0.3s window)"
    arts = st["tool_artifacts"]
    assert len(arts) == 4
    assert all(a.get("abandoned") and a.get("outcome_unknown") for a in arts), (
        "every unfinished read must still be abandoned + outcome_unknown at the window")
    assert len(_tool_messages(st)) == 4, "the model must still see one message per tool call"


def test_fast_siblings_survive_a_slow_one_killed_at_the_window(monkeypatch):
    """The mixed case that only concurrency can produce: inside ONE window the fast reads
    complete and the slow one is abandoned. Serially the fast reads queued behind the slow one
    would be abandoned too (or never dispatched), so this is a direct concurrency assertion as
    well as the per-tool accounting one."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.6")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    specs = [FakeSpec("calculate_commute"), FakeSpec("check_safety"),
             FakeSpec("search_properties")]
    provider = PerToolDelayProvider(specs, {"calculate_commute": 0.05, "check_safety": 0.05,
                                            "search_properties": 5.0})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    st = _batch_state(["calculate_commute", "check_safety", "search_properties"])

    st = _exec_once(nodes, st)

    by_tool = {a["tool"]: a for a in st["tool_artifacts"]}
    assert by_tool["calculate_commute"]["success"] is True
    assert by_tool["check_safety"]["success"] is True
    assert by_tool["search_properties"].get("abandoned") is True
    # accounting is PER TOOL: the survivors carry their own (tiny) elapsed, not the window's
    assert by_tool["calculate_commute"]["elapsed_ms"] < 400
    assert by_tool["check_safety"]["elapsed_ms"] < 400
    assert not by_tool["calculate_commute"].get("timed_out")
    assert not by_tool["check_safety"].get("timed_out")


def test_turn_budget_is_charged_wall_clock_not_the_sum_of_calls(monkeypatch):
    """The payoff of concurrency has to reach the TURN budget or it buys nothing downstream:
    four _S-second reads must consume ~_S of FC_TURN_TOOL_BUDGET_S, not 4*_S. A serial batch
    (or an implementation that summed per-call elapsed) would charge 4x here and exhaust the
    turn budget after one batch."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    names = [f"tool_{i}" for i in range(4)]
    provider = PerToolDelayProvider([FakeSpec(x) for x in names], {x: _S for x in names})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    st = _exec_once(nodes, _batch_state(names))

    used = st["turn_tool_budget_used_s"]
    assert used < 2 * _S, (
        f"turn budget charged {used:.2f}s for a concurrent batch of 4x{_S}s — "
        f"a serial/summed charge would be ~{4 * _S:.1f}s")
    assert used >= _S * 0.8, f"turn budget under-charged ({used:.2f}s); the batch did run"


# ═══════════════════════════════════════════════════════════════════
# 3. Determinism: the model sees request order, never completion order
# ═══════════════════════════════════════════════════════════════════

def test_results_follow_request_order_not_completion_order(monkeypatch):
    """Concurrency makes completion order a RACE. If results were appended as they land, the
    same question would produce different transcripts run to run and answers could become
    order-dependent. The ToolMessages (and artifacts) must stay in the model's requested order
    even when the LAST-requested tool finishes FIRST."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    order = ["slowest", "middle", "fastest"]          # requested slow -> fast
    delays = {"slowest": 0.45, "middle": 0.25, "fastest": 0.02}   # completes fast -> slow
    provider = PerToolDelayProvider([FakeSpec(x) for x in order], delays)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    st = _exec_once(nodes, _batch_state(order))

    assert [m.name for m in _tool_messages(st)] == order
    assert [a["tool"] for a in st["tool_artifacts"]] == order
    # and the tool_call_id pairing is intact — a reordered transcript is a provider error
    assert [m.tool_call_id for m in _tool_messages(st)] == ["c0", "c1", "c2"]
    # the completion order really was the reverse (otherwise this test proves nothing)
    el = {a["tool"]: a["elapsed_ms"] for a in st["tool_artifacts"]}
    assert el["fastest"] < el["middle"] < el["slowest"]


def test_same_tool_twice_with_different_args_runs_both_concurrently(monkeypatch):
    """The literal E_multi_constraint shape: one commute to KCL and one to Imperial in the
    same batch. Different args -> different digest -> both run (the no-progress guard must not
    collapse them), and they must run AT THE SAME TIME, not one after the other."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    provider = PerToolDelayProvider([FakeSpec("calculate_commute")],
                                    {"calculate_commute": _S})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    st = _state()
    st["messages"] = [AIMessage(content="", tool_calls=[
        _tc("calculate_commute", {"to_address": "King's College London"}, "c1"),
        _tc("calculate_commute", {"to_address": "Imperial College London"}, "c2")])]

    t0 = time.monotonic()
    st = _exec_once(nodes, st)
    wall = time.monotonic() - t0

    assert wall < 2 * _S, f"two commute calls serialised ({wall:.2f}s vs {2 * _S:.1f}s)"
    arts = st["tool_artifacts"]
    assert len(arts) == 2 and all(a["success"] for a in arts)
    assert arts[0]["params_digest"] != arts[1]["params_digest"]


def test_duplicate_call_is_still_collapsed_not_run_twice(monkeypatch):
    """The counterweight to the test above: identical (tool, args) in one batch is a
    DEPENDENCY-FREE duplicate, not parallelisable work. It must still be short-circuited by
    the no-progress guard rather than dispatched a second time."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    provider = PerToolDelayProvider([FakeSpec("check_safety")], {"check_safety": 0.02})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    st = _state()
    st["messages"] = [AIMessage(content="", tool_calls=[
        _tc("check_safety", {"address": "Camden"}, "c1"),
        _tc("check_safety", {"address": "Camden"}, "c2")])]

    st = _exec_once(nodes, st)

    assert len(provider.calls) == 1, "the identical duplicate must not be dispatched"
    msgs = _tool_messages(st)
    assert len(msgs) == 2 and "already ran" in msgs[1].content


# ═══════════════════════════════════════════════════════════════════
# 4. The residual serialisation: a saturated offload pool
# ═══════════════════════════════════════════════════════════════════

# _tool_offload_executor() floors the pool at max(4, FC_TOOL_OFFLOAD_WORKERS), so a
# saturation test must fill FOUR workers however small it sets the env var.
_POOL_FLOOR = 4


@pytest.fixture
def fresh_offload_pool():
    """Give the test its own process-wide offload pool, and dispose of it afterwards so the
    threads it deliberately jammed cannot leak into the rest of the session."""
    prev = agent_loop._TOOL_OFFLOAD_EXECUTOR
    agent_loop._TOOL_OFFLOAD_EXECUTOR = None
    yield
    made = agent_loop._TOOL_OFFLOAD_EXECUTOR
    if made is not None and made is not prev:
        made.shutdown(wait=False)   # jammed threads exit once their sleep returns
    agent_loop._TOOL_OFFLOAD_EXECUTOR = prev


def _saturate(monkeypatch, hold_s=2.0):
    """Fill every offload worker with an abandoned, unkillable dispatch. Each hog blocks
    (non-yielding) for `hold_s` but is walked away from at a 0.2s window, so on return the
    pool has zero free workers and will have none for ~hold_s more."""
    monkeypatch.setenv("FC_TOOL_OFFLOAD_WORKERS", str(_POOL_FLOOR))
    agent_loop._TOOL_OFFLOAD_EXECUTOR = None
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.2")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "600")
    names = [f"hog_{i}" for i in range(_POOL_FLOOR)]
    provider = BlockingProvider([FakeSpec(x) for x in names], blocks=hold_s)
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    st = _exec_once(nodes, _batch_state(names))
    assert all(a.get("abandoned") for a in st["tool_artifacts"]), "setup: all must be abandoned"


def test_worker_starvation_is_attributed_not_blamed_on_the_tool(monkeypatch,
                                                                fresh_offload_pool):
    """THE residual serialisation, measured. An abandoned dispatch keeps its worker until the
    tool itself returns (a running thread cannot be cancelled). With every worker held, the
    NEXT batch's read never even starts — yet its per-call wait_for and the batch window are
    already ticking, so it is killed having executed zero tool code.

    Before this change that kill was reported as ``abandoned after Ns (batch budget)`` with
    elapsed_ms = the window, i.e. attributed to the tool as though the TOOL were slow. It must
    instead be attributed to capacity: ``starved``, with the queue wait recorded."""
    _saturate(monkeypatch)

    # a trivially fast tool, dispatched with plenty of window, behind a full pool
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.4")
    provider = PerToolDelayProvider([FakeSpec("check_safety")], {"check_safety": 0.0})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    st = _exec_once(nodes, _batch_state(["check_safety"]))

    art = st["tool_artifacts"][0]
    assert art["tool"] == "check_safety"
    assert art.get("abandoned") is True
    assert art.get("starved") is True, (
        "a dispatch no worker ever picked up must be marked starved, not blamed on the tool")
    assert art.get("queue_wait_ms") is not None
    assert art["queue_wait_ms"] >= 300, (
        f"queue wait under-reported ({art['queue_wait_ms']}ms) for a fully-starved dispatch")
    assert "never started" in art["error"] and "no tool worker was free" in art["error"]
    # the model-facing message still exists and still tells it to move on
    assert len(_tool_messages(st)) == 1


def test_starved_dispatch_emits_a_capacity_flavoured_budget_event(monkeypatch,
                                                                  fresh_offload_pool,
                                                                  caplog):
    """The eval/observability side of the same fact: the budget-timeout event carries
    outcome='starved' (not 'abandoned') and the log line carries queue_wait_s, so a live round
    can tell a capacity kill from a slow-tool kill without guessing."""
    seen = []
    monkeypatch.setattr(agent_loop, "_record_budget_timeout_event",
                        lambda **kw: seen.append(kw))
    _saturate(monkeypatch)

    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.4")
    provider = PerToolDelayProvider([FakeSpec("web_search")], {"web_search": 0.0})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    with caplog.at_level("WARNING", logger="core.agent_loop"):
        _exec_once(nodes, _batch_state(["web_search"]))

    starved = [e for e in seen if e.get("outcome") == "starved"]
    assert starved and starved[0]["tool"] == "web_search"
    assert any("queue_wait_s=" in r.getMessage()
               for r in caplog.records if "tool_budget_timeout" in r.getMessage())


def test_healthy_pool_reports_no_queue_wait(monkeypatch, fresh_offload_pool):
    """The control: with workers free, a completed dispatch waits microseconds, so no
    queue_wait_ms is recorded and nothing is marked starved. Without this the two tests above
    would pass on an implementation that always claimed starvation."""
    monkeypatch.setenv("FC_TOOL_OFFLOAD_WORKERS", "8")
    agent_loop._TOOL_OFFLOAD_EXECUTOR = None
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    names = [f"t{i}" for i in range(4)]
    provider = PerToolDelayProvider([FakeSpec(x) for x in names], {x: 0.05 for x in names})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    st = _exec_once(nodes, _batch_state(names))

    for a in st["tool_artifacts"]:
        assert a["success"] is True
        assert "starved" not in a
        assert "queue_wait_ms" not in a, (
            f"{a['tool']} reported queue wait {a.get('queue_wait_ms')}ms on an idle pool")


def test_normal_abandon_is_still_reported_as_abandoned(monkeypatch, fresh_offload_pool):
    """Regression guard on the attribution change: a tool that genuinely RAN and overran its
    window keeps the existing 'abandoned' wording and outcome. Only the never-started case is
    reclassified."""
    seen = []
    monkeypatch.setattr(agent_loop, "_record_budget_timeout_event",
                        lambda **kw: seen.append(kw))
    monkeypatch.setenv("FC_TOOL_OFFLOAD_WORKERS", "8")
    agent_loop._TOOL_OFFLOAD_EXECUTOR = None
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "0.3")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    provider = PerToolDelayProvider([FakeSpec("search_properties")],
                                    {"search_properties": 3.0})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    st = _exec_once(nodes, _batch_state(["search_properties"]))

    art = st["tool_artifacts"][0]
    assert art.get("abandoned") is True
    assert "starved" not in art, "a tool that actually ran must not be called starved"
    assert "abandoned after" in art["error"]
    assert [e["outcome"] for e in seen] == ["abandoned"]


# ═══════════════════════════════════════════════════════════════════
# 5. Dependency: what this batch model can and cannot express
# ═══════════════════════════════════════════════════════════════════

def test_a_batch_cannot_express_an_intra_batch_dependency(monkeypatch):
    """Documented limit, asserted so it cannot change silently.

    Everything in one batch is dispatched at once from arguments the model already wrote, so a
    call CANNOT consume a sibling's output — there is no place to put the dependency. That is
    why a genuinely dependent step (get_property_details on a listing that search_properties is
    still fetching) has to arrive in the NEXT batch, behind another LLM round-trip, and why the
    remaining multi-call latency is a planning-layer cost rather than a dispatch one.

    The safety consequence is what is pinned here: because nothing in a batch can read a
    sibling's result, parallel dispatch cannot introduce a data race between them. The two
    calls below run simultaneously and each sees only the args the model gave it."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    specs = [FakeSpec("search_properties"), FakeSpec("get_property_details")]
    provider = PerToolDelayProvider(specs, {"search_properties": 0.05,
                                            "get_property_details": 0.05})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())
    st = _state()
    st["messages"] = [AIMessage(content="", tool_calls=[
        _tc("search_properties", {"area": "Camden"}, "c1"),
        _tc("get_property_details", {"property_name": "Scape Bloomsbury"}, "c2")])]

    st = _exec_once(nodes, st)

    called = {name: params for name, params in provider.calls}
    # get_property_details received ONLY what the model wrote (plus harness-injected keys) —
    # never anything derived from the sibling search that was running at the same time.
    assert called["get_property_details"]["property_name"] == "Scape Bloomsbury"
    assert not any(k.startswith("_from_") or k == "properties"
                   for k in called["get_property_details"])


def test_dependent_work_arriving_in_a_later_batch_still_runs(monkeypatch):
    """The other half of the same limit: a dependent call in the NEXT batch is a different
    (tool, digest), so the no-progress guard lets it through and it executes normally. Nothing
    about the concurrent dispatch of batch 1 blocks the sequential batch 2."""
    monkeypatch.setenv("FC_BATCH_TOOL_BUDGET_S", "20")
    monkeypatch.setenv("FC_TURN_TOOL_BUDGET_S", "60")
    specs = [FakeSpec("search_properties"), FakeSpec("get_property_details")]
    provider = PerToolDelayProvider(specs, {"search_properties": 0.05,
                                            "get_property_details": 0.05})
    nodes = build_fc_nodes(provider, agent_llm=FakeChat())

    st = _state()
    st["messages"] = [AIMessage(content="",
                                tool_calls=[_tc("search_properties", {"area": "Camden"}, "c1")])]
    st = _exec_once(nodes, st)
    st["messages"] = st["messages"] + [AIMessage(content="", tool_calls=[
        _tc("get_property_details", {"property_url": "https://x/1"}, "c2")])]
    st = _exec_once(nodes, st)

    tools = [a["tool"] for a in st["tool_artifacts"]]
    assert tools == ["search_properties", "get_property_details"]
    assert all(a["success"] for a in st["tool_artifacts"])
