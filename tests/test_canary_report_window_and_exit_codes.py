"""Two tool defects in `scripts/canary_report.py`, and the guards that keep them fixed.

Both are the SAME defect class this repo keeps shipping: a value is computed, stored
where a reader could find it, and then never actually used for the thing it was
computed for.

  (a) `--since` was parsed into a datetime and used ONLY to compute STAGE-PROGRESS
      elapsed hours. It never filtered records — only `--window HOURS` did. Worse, the
      `--expect-turns` anchor block printed `window = the selected --window / --since
      range`, actively claiming a bound the report had not applied. The first run of the
      2026-07-25 internal round of record counted a warm-up turn from before the stated
      stage start and returned INSTRUMENTATION-HOLD because of it.

  (b) `--json` takes a PATH. A bare `--json` aborted in argparse with exit **2** — the
      same code as STAGE-PAUSE / INSTRUMENTATION-HOLD. An operator or CI driver checking
      only `$?` could not tell "I mistyped a flag" from "the gate failed".

The tests below fail on the pre-fix code: the population/anchor ones because `--since`
did not filter and the anchor named it anyway, the exit-code ones because argparse
answered 2. The last group PINS the gate verdict codes and the gate thresholds so an
instrument fix — this one or a later one — cannot drift them silently.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "canary_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("canary_report_window", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load_module()

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
STAGE_START = NOW - timedelta(hours=30)          # a c1-eligible elapsed time (>= 24h)

# The gate's verdict codes, written as literals ON PURPOSE. Referencing the module
# constant would make these assertions agree with whatever the source currently says,
# which is exactly the kind of self-certifying check that let the original defect ship.
GATE_CODES = (0, 2, 3)
USAGE_CODE = 64

_RID = 0


def _rec(arch="fc_loop", *, ts=NOW, latency_ms=1000.0, endpoint="alex", **extra):
    """A clean, contract-valid schema-v2 canary.turn record."""
    global _RID
    _RID += 1
    arch_full = "fc_loop" if arch in ("fc", "fc_loop") else "legacy"
    r = {
        "event": "canary.turn",
        "telemetry_schema_version": 2,
        "ts": ts.isoformat() if isinstance(ts, datetime) else ts,
        "endpoint": endpoint,
        "agent_arch": arch_full,
        "candidate_sha": "c9e60c2",
        "strict": arch_full == "fc_loop",
        "request_id": f"w{_RID}",
        "conversation_id": f"conv{_RID}",
        "user_id_hash": "h" * 32,
        "user_id_hash_status": "keyed",
        "http_status": 200,
        "turn_outcome": "ok",
        "soft_wrapped": False,
        "partial": False,
        "tool_budget_timeout": False,
        "security": {"denied_write_count": 0, "tainted_write_executed_count": 0,
                     "forbidden_write_executed_count": 0},
        "dsml_blocked": 0,
        "dsml_leak": 0,
        "provider_schema_400_count": 0,
        "turn_latency_ms": latency_ms,
        "llm_calls": 2,
        "tool_batches": 1,
        "llm_usage": {
            "calls": 2, "input_tokens": 10, "output_tokens": 5,
            "cache_read_tokens": 0,
            "models": {"fixture-model": {
                "calls": 2, "input_tokens": 10, "output_tokens": 5,
                "cache_read_tokens": 0,
            }},
        },
        "llm_usage_status": "complete",
        "forbidden_read": None,
        "no_evidence_numbers": None,
        "eval_only": ["forbidden_read", "no_evidence_numbers"],
    }
    r.update(extra)
    return r


def _fc(n, **kw):
    return [_rec("fc_loop", **kw) for _ in range(n)]


# =========================================================================== #
# (a) --since must FILTER the population                                      #
# =========================================================================== #

def test_a_record_before_since_is_excluded_and_one_inside_is_kept():
    """The whole claim of `--since`: judge the traffic from the stage start onward.

    Pre-fix this reported 12 fc turns — the two turns from before the stage started
    stayed in the population, in the percentile denominator and in every rate.
    """
    inside = _fc(10, ts=STAGE_START + timedelta(hours=1))
    outside = _fc(2, ts=STAGE_START - timedelta(minutes=1))
    report = cr.build_report(inside + outside, now_override=NOW, since=STAGE_START)
    assert report["arches"]["fc"]["turns"] == 10, (
        "a record older than --since must not be in the population")
    assert report["records_in_window"] == 10
    # ...and the negative control: the inside record is not lost by an over-eager bound.
    only_inside = cr.build_report(inside, now_override=NOW, since=STAGE_START)
    assert only_inside["arches"]["fc"]["turns"] == 10


def test_a_record_exactly_at_since_is_inside_the_window():
    """The bound is inclusive, matching --window. A turn emitted on the stage-start
    second belongs to the stage."""
    report = cr.build_report(_fc(3, ts=STAGE_START), now_override=NOW, since=STAGE_START)
    assert report["arches"]["fc"]["turns"] == 3


def test_since_bounds_the_expect_turns_anchor_population():
    """The 2026-07-25 failure, reproduced.

    A warm-up turn was driven before the stage window opened, then 50 turns of record.
    The operator passed `--since <window open>` and `--expect-turns 50`; because
    `--since` did not filter, the anchor saw 51 eligible turns and reported
    INSTRUMENTATION-HOLD for a run that had actually gone exactly to plan.
    """
    warmup = _fc(1, ts=STAGE_START - timedelta(minutes=5))
    round_of_record = _fc(50, ts=STAGE_START + timedelta(minutes=5))
    control = [_rec("legacy", ts=STAGE_START + timedelta(minutes=5)) for _ in range(50)]
    rep = cr.build_report(warmup + round_of_record + control, now_override=NOW,
                          since=STAGE_START, expect_turns=50)
    et = rep["verdict"]["expected_turns"]
    assert et["observed"] == 50, "the warm-up turn must be outside the --since window"
    assert et["unique_request_ids"] == 50
    assert et["matched"] is True
    assert rep["verdict"]["exit_code"] == 0

    # Negative control: without --since the warm-up turn is genuinely in the file, and
    # the anchor SHOULD hold. The fix is a real filter, not a suppressed mismatch.
    unfiltered = cr.build_report(warmup + round_of_record, now_override=NOW,
                                 expect_turns=50)
    assert unfiltered["verdict"]["expected_turns"]["observed"] == 51
    assert unfiltered["verdict"]["exit_code"] == 2


@pytest.mark.parametrize("window_hours,since_hours_ago,expect_recent_only", [
    (72.0, 1.0, True),     # --since is the tighter bound -> it must win
    (1.0, 72.0, True),     # --window is the tighter bound -> it must win
    (72.0, 72.0, False),   # both loose -> everything dated is kept
])
def test_window_and_since_intersect_the_tighter_bound_wins(window_hours,
                                                           since_hours_ago,
                                                           expect_recent_only):
    """Both flags are lower bounds on ts, so passing both means the intersection.
    Whichever is later wins; neither may widen the other."""
    recent = _fc(4, ts=NOW - timedelta(minutes=30))
    old = _fc(3, ts=NOW - timedelta(hours=48))
    rep = cr.build_report(recent + old, now_override=NOW, window_hours=window_hours,
                          since=NOW - timedelta(hours=since_hours_ago))
    assert rep["arches"]["fc"]["turns"] == (4 if expect_recent_only else 7)


def test_stage_progress_turn_count_counts_only_the_stage_window():
    """`--stage` asks "has THIS stage accumulated its minimum turns?". Pre-fix the count
    was every fc turn in the file, so turns from a previous stage — or a previous build —
    could satisfy a floor the current stage had not reached."""
    this_stage = _fc(210, ts=STAGE_START + timedelta(hours=1))
    previous_stage = _fc(300, ts=STAGE_START - timedelta(hours=10))
    rep = cr.build_report(this_stage + previous_stage, now_override=NOW,
                          stage="c1", since=STAGE_START)
    sp = rep["verdict"]["stage_progress"]
    assert sp["fc_turns"] == 210, "stage turns must be counted inside the stage window"
    assert sp["hours_ok"] is True and sp["turns_ok"] is True


def test_filter_window_honours_since_directly():
    """The filter primitive itself, not just build_report — `--since` has to be applied
    where the records are dropped, not re-derived by each caller."""
    recs = _fc(2, ts=NOW) + _fc(5, ts=NOW - timedelta(hours=5))
    kept = cr.filter_window(recs, None, NOW, since=NOW - timedelta(hours=1))
    assert len(kept) == 2


# =========================================================================== #
# (a2) the anchor/window text must describe the filter that ACTUALLY ran      #
# =========================================================================== #

def test_anchor_window_text_states_the_cutoff_that_was_applied():
    rep = cr.build_report(_fc(5, ts=NOW), now_override=NOW, since=STAGE_START,
                          expect_turns=5)
    described = rep["verdict"]["expected_turns"]["filters"]["window"]
    assert STAGE_START.isoformat() in described, (
        f"the anchor must state the cutoff it applied, got {described!r}")
    # The report's own record of the filter and the anchor's description agree because
    # they come from one place.
    assert rep["window_cutoff"] == STAGE_START.isoformat()
    assert rep["window_filter"] == described
    text = cr.render_text(rep)
    assert f"window=ts >= {STAGE_START.isoformat()}" in text
    assert f"record filter  : ts >= {STAGE_START.isoformat()}" in text


def test_anchor_window_text_claims_no_filter_when_none_was_applied():
    """Pre-fix this printed `window = the selected --window / --since range` even when
    neither flag was given and every record in the file was counted."""
    rep = cr.build_report(_fc(5, ts=NOW), now_override=NOW, expect_turns=5)
    described = rep["verdict"]["expected_turns"]["filters"]["window"]
    assert described.startswith("UNFILTERED"), (
        f"no filter was applied, so the report must say so outright: {described!r}")
    # No cutoff may be claimed, in any wording, when none was applied.
    assert "ts >=" not in described and "range" not in described, (
        f"no bound was applied, so none may be described: {described!r}")
    assert rep["window_cutoff"] is None


def test_window_only_text_does_not_mention_since():
    rep = cr.build_report(_fc(5, ts=NOW), now_override=NOW, window_hours=24.0,
                          expect_turns=5)
    described = rep["verdict"]["expected_turns"]["filters"]["window"]
    assert "--window" in described
    assert "--since" not in described, (
        f"--since was not given, so it cannot appear in the window description: "
        f"{described!r}")


def test_source_carries_no_fixed_window_claim():
    """SOURCE GUARD, not a promise. The anchor's window line must be derived from the
    cutoff that was applied; a hard-coded sentence about the flags is how the report
    came to claim a filter it never performed."""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "the selected --window / --since range" not in src, (
        "the anchor must report the applied cutoff, not a fixed claim about flags")


def test_expected_turns_says_so_when_the_caller_states_no_window():
    """Called directly (as tests/test_zero_call_turn_telemetry.py does), the anchor
    cannot know what filter its caller applied — so it must say that, not guess."""
    et = cr.evaluate_expected_turns(_fc(1, ts=NOW), 1)
    assert "not stated" in et["filters"]["window"]


# =========================================================================== #
# (b) argparse misuse must not share an exit code with a gate verdict         #
# =========================================================================== #

def _cli_code(argv):
    """The exit code `argv` produces, however the CLI chooses to report it.

    Pre-fix, argparse aborted the interpreter itself with SystemExit(2); post-fix the
    usage code comes back as run()'s return value. Both are normalised here so the
    assertion is about the CODE an operator's shell sees, not about the mechanism.
    """
    try:
        return cr.run(argv)
    except SystemExit as exc:
        return exc.code


def test_bare_json_flag_is_a_usage_error_not_a_gate_verdict(capsys):
    """`--json` without its PATH. Pre-fix: exit 2 == STAGE-PAUSE."""
    code = _cli_code(["--json"])
    assert code not in GATE_CODES, (
        f"a mistyped flag exited with GATE VERDICT code {code}: an operator checking "
        f"only $? cannot tell usage failure from a gate result")
    assert code == USAGE_CODE
    assert "error" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("argv", [
    ["--bogus"],                          # unknown flag
    ["-i", "x.jsonl", "--window", "nan-hours"],   # bad float
    ["-i", "x.jsonl", "--stage", "not-a-stage"],  # bad choice
    ["-i", "x.jsonl", "--expect-turns", "many"],  # bad int
    ["-i", "x.jsonl", "--since"],                 # another option missing its argument
])
def test_every_argparse_abort_uses_the_usage_code(argv):
    code = _cli_code(argv)
    assert code not in GATE_CODES, f"{argv} exited with gate code {code}"
    assert code == USAGE_CODE


def test_help_is_not_misuse():
    """Asking for help is not a typo; it stays 0 (and prints to stdout, so nobody
    reads it as a verdict)."""
    assert _cli_code(["--help"]) == 0


def test_parser_error_and_exit_paths_both_refuse_code_two():
    """SOURCE GUARD: argparse funnels aborts through error() AND exit(). Overriding
    only one leaves the collision reachable from the other."""
    parser = cr._build_parser()
    with pytest.raises(SystemExit) as e1:
        parser.error("boom")
    assert e1.value.code == USAGE_CODE
    with pytest.raises(SystemExit) as e2:
        parser.exit(2, None)
    assert e2.value.code == USAGE_CODE


def test_input_and_argument_errors_keep_their_own_non_verdict_code(tmp_path):
    """Unchanged behaviour, pinned: these were already outside the verdict codes."""
    assert cr.run([]) == 1
    p = tmp_path / "c.jsonl"
    p.write_text("", encoding="utf-8")
    assert cr.run(["-i", str(p), "--since", "not-a-timestamp", "--quiet"]) == 1
    assert cr.run(["-i", str(p), "--now", "not-a-timestamp", "--quiet"]) == 1
    assert cr.run(["-i", str(p), "--expect-turns", "-1", "--quiet"]) == 1


# =========================================================================== #
# Pins: verdict codes and gate thresholds                                     #
# =========================================================================== #

def test_gate_verdict_exit_codes_are_pinned():
    """Every verdict a driver branches on, with its exact code. A future edit that
    renumbers one — including an "instrument fix" like this one — fails here."""
    clean = _fc(30, ts=NOW) + [_rec("legacy", ts=NOW) for _ in range(30)]

    def vd(recs, **kw):
        return cr.build_report(recs, now_override=NOW, **kw)["verdict"]

    proceed = vd(clean)
    assert (proceed["decision"], proceed["exit_code"]) == ("PROCEED", 0)

    hold = vd(clean, stage="c1", since=NOW - timedelta(hours=1))
    assert (hold["decision"], hold["exit_code"]) == ("HOLD", 2)

    progress = vd(_fc(200, ts=NOW) + [_rec("legacy", ts=NOW) for _ in range(10)],
                  stage="c1", since=NOW - timedelta(hours=30))
    assert (progress["decision"], progress["exit_code"]) == ("STAGE-PROGRESS-OK", 0)

    slow = vd([_rec("fc_loop", ts=NOW, latency_ms=40000.0) for _ in range(20)]
              + [_rec("legacy", ts=NOW) for _ in range(20)])
    assert (slow["decision"], slow["exit_code"]) == ("STAGE-PAUSE", 2)

    held = vd(clean, expect_turns=999)
    assert (held["decision"], held["exit_code"]) == ("INSTRUMENTATION-HOLD", 2)

    breach = list(clean)
    breach[0] = _rec("fc_loop", ts=NOW,
                     security={"denied_write_count": 0,
                               "tainted_write_executed_count": 1,
                               "forbidden_write_executed_count": 0})
    blocked = vd(breach)
    assert (blocked["decision"], blocked["exit_code"]) == ("CANARY-BLOCK", 3)

    # The verdict codes are exactly the gate codes, and the usage code is not one.
    assert tuple(cr.GATE_EXIT_CODES) == GATE_CODES
    assert cr.EXIT_USAGE == USAGE_CODE and cr.EXIT_USAGE not in GATE_CODES
    assert cr.EXIT_INPUT_ERROR not in GATE_CODES


def test_gate_thresholds_are_unchanged_by_this_instrument_fix():
    """A p50 gate is currently breached and overridden by decision. Touching a
    threshold while "fixing the instrument" is the one move this project refuses, so
    the numbers are pinned here as literals."""
    assert cr.P50_LIMIT_MS == 6000.0
    assert cr.P95_LIMIT_MS == 30000.0
    assert cr.OVER_SLO_MS == 30000.0
    assert cr.DEGRADED_RATE_LIMIT == 0.10
    assert cr.RELATIVE_PP == 1.0
    assert cr.STAGES == {"internal": (50, 24), "c1": (200, 24), "c2": (500, 48),
                         "c3": (1000, 72), "flip": (2000, 168)}
