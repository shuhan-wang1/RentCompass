"""Did the case's fixture actually reach the agent?

`load_fixture_queue` keys the replay queue BY TOOL NAME. If the agent routes to a
different tool, the fixture is never served — yet the case is still graded against
constraints that encode the fixture's premise. F11 is the worked example: it binds
`ext_fg_web_stale_fare.json` (a 2019 £134.80 travelcard snippet) to `web_search` and
asserts `must_flag_stale_data`. The agent called `get_transport_info`, received LIVE 2025
data, and correctly reported £164 citing TfL. There was no stale figure to flag, so the
constraint was unsatisfiable AS RUN, and the record could not distinguish "the product
fabricated" from "the product never saw the fixture".

These tests pin the RECORDING of that fact, and — just as importantly — pin that it does
NOT change grading. Skipping or excusing constraints on unserved fixtures would move the
pass rate by +5.4pp (fc) and +12.5pp (legacy) on the retained round
.runtime/round-8793c0b-internal-2026-07-25, i.e. it would inflate both arms of the
comparison the programme is deciding, by different amounts. A silently higher pass rate is
the worst available outcome, so `test_recording_a_fixture_bypass_cannot_move_the_gate`
fails loudly if anyone wires these fields into the verdict without making it visible.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from evaluation import results_package as rp
from evaluation import run_benchmark as rb

BENCH = Path(__file__).resolve().parents[1] / "evaluation" / "benchmark"


# ── the pure report ──────────────────────────────────────────────────
def test_no_fixture_declared_is_not_applicable_not_false():
    """A case with no fixture must read None, never False: "no fixture to serve" and
    "a fixture that was bypassed" are opposite findings and must not collapse."""
    assert rb.fixture_service_report({}, {}) == {
        "fixture_served": None, "fixture_unserved_tools": [], "fixture_records_unserved": 0}


def test_every_bound_tool_ran_is_served():
    r = rb.fixture_service_report({"web_search": 1}, {"web_search": 1})
    assert r["fixture_served"] is True
    assert r["fixture_unserved_tools"] == []
    assert r["fixture_records_unserved"] == 0


def test_bypassed_tool_is_unserved_and_named():
    """F11's shape: the fixture is bound to web_search, the agent called something else."""
    r = rb.fixture_service_report({"web_search": 1}, {})
    assert r["fixture_served"] is False
    assert r["fixture_unserved_tools"] == ["web_search"]
    assert r["fixture_records_unserved"] == 1


def test_partial_delivery_is_visible_not_hidden_behind_the_boolean():
    """C9's shape: three recorded commutes, the agent called calculate_commute once. The
    tool DID run, so fixture_served is True — but two of the three premises never arrived,
    and a lone boolean would hide that."""
    r = rb.fixture_service_report({"calculate_commute": 3}, {"calculate_commute": 1})
    assert r["fixture_served"] is True
    assert r["fixture_records_unserved"] == 2


def test_one_bound_tool_of_several_bypassed_is_still_unserved():
    r = rb.fixture_service_report({"check_safety": 1, "web_search": 1}, {"check_safety": 1})
    assert r["fixture_served"] is False
    assert r["fixture_unserved_tools"] == ["web_search"]


def test_more_calls_than_records_never_reports_negative_leftovers():
    """The replay path reuses the last record when calls exceed records; the leftover
    count must floor at zero rather than go negative."""
    r = rb.fixture_service_report({"web_search": 1}, {"web_search": 4})
    assert r["fixture_served"] is True
    assert r["fixture_records_unserved"] == 0


# ── the wiring: _patch_tools fills the report ────────────────────────
@dataclasses.dataclass
class _StubToolResult:
    success: bool
    data: object
    error: object
    tool_name: str
    execution_time_ms: float


class _StubRegistry:
    def __init__(self):
        async def _orig(name, **kwargs):
            raise AssertionError("offline replay must not fall through to a real tool")
        self.execute_tool = _orig


def _runner_stub():
    """_patch_tools only touches self.mode / self.ToolResult / self.collector, so it can be
    driven without standing up a whole CaseRunner (which builds graphs and stores)."""
    class _Collector:
        def record_tool_call(self, *a, **k):
            pass

    class _Self:
        mode = "offline"
        ToolResult = _StubToolResult
        collector = _Collector()

    return _Self()


def _replay(fixture_queue, calls):
    """Run `calls` through the patched executor and return the fixture report."""
    registry = _StubRegistry()
    report: dict = {}
    evidence: list = []
    with rb.CaseRunner._patch_tools(_runner_stub(), registry, fixture_queue,
                                   evidence, report):
        for name in calls:
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                registry.execute_tool(name))
    return report, evidence


def test_patch_tools_records_a_served_fixture():
    queue = rb.load_fixture_queue({"fixture": "ext_fg_web_stale_fare.json"})
    report, evidence = _replay(queue, ["web_search"])
    assert report["fixture_served"] is True
    assert report["fixture_unserved_tools"] == []
    # the agent really did receive the fixture payload, not a canned stub
    assert "134.80" in json.dumps(evidence, default=str)


def test_patch_tools_records_f11s_actual_bypass():
    """The measured F11 run: fixture bound to web_search, agent called get_transport_info.
    The tool still answers (offline canned stub), so nothing errors — which is exactly why
    this needed recording rather than being self-evident from the trace."""
    queue = rb.load_fixture_queue({"fixture": "ext_fg_web_stale_fare.json"})
    report, evidence = _replay(queue, ["get_transport_info"])
    assert report["fixture_served"] is False
    assert report["fixture_unserved_tools"] == ["web_search"]
    assert report["fixture_records_unserved"] == 1
    assert "134.80" not in json.dumps(evidence, default=str)


def test_patch_tools_report_survives_an_exception_inside_the_context():
    """A run that dies before reaching its bound tool is precisely an unserved fixture."""
    queue = rb.load_fixture_queue({"fixture": "ext_fg_web_stale_fare.json"})
    report: dict = {}
    with pytest.raises(RuntimeError):
        with rb.CaseRunner._patch_tools(_runner_stub(), _StubRegistry(), queue, [], report):
            raise RuntimeError("graph blew up")
    assert report["fixture_served"] is False


def test_patch_tools_restores_the_original_executor():
    registry = _StubRegistry()
    original = registry.execute_tool
    with rb.CaseRunner._patch_tools(_runner_stub(), registry, {}, [], {}):
        assert registry.execute_tool is not original
    assert registry.execute_tool is original


# ── the four real cases this was built for ───────────────────────────
def _case(case_id):
    for path in sorted(BENCH.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                c = json.loads(line)
                if c["case_id"] == case_id:
                    return c
    raise AssertionError(f"{case_id} not found in any shard")


# case_id -> (fixture-bound tool, the tool the retained fc round actually executed)
_MEASURED_BYPASSES = {
    "F11": ("web_search", ["get_transport_info"]),
    "C3": ("calculate_commute", ["get_property_details"]),
    "C9": ("calculate_commute", ["get_property_details", "get_property_details"]),
    "F12": ("calculate_commute", ["search_properties"]),
}


@pytest.mark.parametrize("case_id", sorted(_MEASURED_BYPASSES))
def test_the_measured_bypasses_report_unserved(case_id):
    """Replaying each case's real fixture against the tools its fc run really executed
    (.runtime/round-8793c0b-internal-2026-07-25/eval/sweep/per_case.csv) must report the
    bypass. These are the cases whose constraints encode a premise the agent never got."""
    bound, executed = _MEASURED_BYPASSES[case_id]
    queue = rb.load_fixture_queue(_case(case_id))
    assert bound in queue, f"{case_id}: fixture is no longer bound to {bound}"
    report, _ = _replay(queue, executed)
    assert report["fixture_served"] is False
    assert bound in report["fixture_unserved_tools"]


def test_f7_is_not_a_bypass_now_that_its_route_is_expressible():
    """Counter-example, so the guard above is not just "everything is unserved". F7's
    fixture is bound to web_search and its run executed web_search, so it was served —
    F7's old route miss came from the pseudo-route in expected_tools, not from a fixture
    that never arrived. The two defects are independent."""
    queue = rb.load_fixture_queue(_case("F7"))
    report, _ = _replay(queue, ["web_search", "search_properties"])
    assert report["fixture_served"] is True


# ── recording must not become grading ────────────────────────────────
def _fake_run(case_id, *, passed, fixture_served, unserved):
    rr = rb.RunResult(case_id=case_id, category="F_grounding", config="c", mode="offline",
                      run_id=f"{case_id}#r1#c", repeat=1)
    rr.passed = passed
    rr.verdict = {"task_completed": passed, "constraints_passed": 1, "constraints_total": 1}
    rr.grounding = {}
    rr.route_matched = passed
    rr.turn_latency_ms = 100.0
    rr.fixture_served = fixture_served
    rr.fixture_unserved_tools = list(unserved)
    rr.fixture_records_unserved = len(unserved)
    return rr


def test_recording_a_fixture_bypass_cannot_move_the_gate(tmp_path, monkeypatch):
    """THE load-bearing test. Recording is mandatory; changing grading on it is an owner
    decision. Two run sets identical except for the fixture-service fields must produce a
    byte-identical summary — same pass rate, same gates. If a future change makes an
    unserved fixture skip or excuse a constraint, this fails and forces that change to be
    argued and surfaced instead of quietly lifting the pass rate."""
    monkeypatch.setattr(rb, "_IDENTITY_CACHE", {}, raising=False)
    monkeypatch.setattr(rp, "probe_git", lambda *_a, **_k: ("f00d123", False))
    ident = rb.commit_identity(refresh=True)

    kwargs = dict(mode="offline", cfg_name="c", repeats=1, cost_cap=0.0,
                  stopped_reason=None, n_selected=2, timestamp="t", arch="fc_loop",
                  identity=ident)

    served = [_fake_run("F11", passed=False, fixture_served=True, unserved=[]),
              _fake_run("F7", passed=True, fixture_served=True, unserved=[])]
    bypassed = [_fake_run("F11", passed=False, fixture_served=False, unserved=["web_search"]),
                _fake_run("F7", passed=True, fixture_served=False, unserved=["web_search"])]

    out_a, out_b = tmp_path / "a", tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    a = rb.write_summary(out_a, [*served], **kwargs)
    b = rb.write_summary(out_b, [*bypassed], **kwargs)

    assert a["passed"] == b["passed"], (
        "the pass rate moved when only the fixture-service record changed — grading is "
        "now reading fixture_served"
    )
    assert a == b, "the summary is no longer independent of the fixture-service record"
    # And the fields are genuinely absent from the summary/gate surface, not merely equal.
    assert "fixture_served" not in json.dumps(a)


def test_fixture_fields_round_trip_through_raw_runs(tmp_path):
    """The record must survive to disk and back, or a resumed round would lose it (and
    RunResult.to_dict is what a reviewer actually reads in raw_runs.jsonl)."""
    rr = _fake_run("F11", passed=False, fixture_served=False, unserved=["web_search"])
    rb.write_raw_runs(tmp_path, [rr])
    line = (tmp_path / "raw_runs.jsonl").read_text(encoding="utf-8").strip()
    d = json.loads(line)
    assert d["fixture_served"] is False
    assert d["fixture_unserved_tools"] == ["web_search"]
    assert d["fixture_records_unserved"] == 1
    back = rb._runresult_from_dict(d)
    assert back.fixture_served is False
    assert back.fixture_unserved_tools == ["web_search"]


def test_default_runresult_reads_not_applicable():
    """The default must be the not-applicable reading, so a case with no fixture is never
    mistaken for a bypass."""
    rr = rb.RunResult(case_id="X1", category="c", config="c", mode="offline",
                      run_id="X1#r1#c", repeat=1)
    assert rr.fixture_served is None
    assert rr.fixture_unserved_tools == []
    assert rr.fixture_records_unserved == 0
