"""An invalid measurement must never produce an automated rollback.

`deploy/run_canary_gate.sh` turns exit 3 into `set_canary_weight.sh --weight 0`.
On 2026-09-03 a window that straddled a deploy exited 3 — off zero-tolerance
counters written by EARLIER builds of the candidate arch, in schema v2, while the
window also mixed two schema versions and five candidate labels — so the operator's
own documented remedy (`--since <before-the-deploy>`) was the command that would
have rolled a healthy candidate to 0%.

These tests pin the four rules that close it:

* a window that is not a measurement HOLDs at exit 2 and says so on the console;
* the breach facts are still printed, marked NOT ACTIONABLE;
* a VALID window with a real breach on the candidate still exits 3;
* zero-tolerance answers for one ARM (arch, and a build when pinned), with the
  control arm and the other builds reported separately as informational.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "canary_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("canary_report_f5b", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load_module()

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
SHA_NEW = "1df0d4f7140f366e42b120fc9629fdb87e738428"
SHA_OLD = "4171d84778be57be5d5e135922552752a06de2f3"

_RID = 0


def _rec(arch="fc_loop", *, version=3, sha=SHA_NEW, latency=5000.0,
         tainted=False, ts=None, **extra):
    global _RID
    _RID += 1
    rec = {
        "event": "canary.turn",
        "telemetry_schema_version": version,
        "ts": (ts or NOW.isoformat()),
        "endpoint": "alex",
        "agent_arch": arch,
        "candidate_sha": sha,
        "strict": arch != "legacy",
        "request_id": f"f5b-{_RID}",
        "conversation_id": f"c{_RID}",
        "user_id_hash": "h" * 32,
        "user_id_hash_status": "keyed",
        "http_status": 200,
        "turn_outcome": "ok",
        "soft_wrapped": False,
        "partial": False,
        "tool_budget_timeout": False,
        "security": {
            "denied_write_count": 0,
            "tainted_write_executed_count": 1 if tainted else 0,
            "forbidden_write_executed_count": 0,
        },
        "dsml_blocked": 0,
        "dsml_leak": 0,
        "provider_schema_400_count": 0,
        "turn_latency_ms": latency,
        "llm_calls": 2,
        "tool_batches": 1,
        "llm_usage": {
            "calls": 2, "input_tokens": 20, "output_tokens": 10,
            "cache_read_tokens": 0,
            "models": {"deepseek-v4-flash": {
                "calls": 2, "input_tokens": 20, "output_tokens": 10,
                "cache_read_tokens": 0}},
        },
        "llm_usage_status": "complete",
        "llm_observer_installed": True,
        "forbidden_read": None,
        "no_evidence_numbers": None,
        "eval_only": ["forbidden_read", "no_evidence_numbers"],
    }
    if version == 2:
        for field in ("llm_calls", "tool_batches"):
            rec.pop(field, None)
    rec.update(extra)
    return rec


def _arms(n=10, **kw):
    return ([_rec("fc_loop", **kw) for _ in range(n)]
            + [_rec("legacy", sha="legacy-sha") for _ in range(n)])


# --------------------------------------------------------------------------- #
# P0-2: an invalid measurement can never produce an automated rollback         #
# --------------------------------------------------------------------------- #

def test_mixed_schema_window_with_breaches_holds_at_exit_2_not_3():
    """The exact 2026-09-03 shape: v2 history + v3 candidate, breaches in the v2 half."""
    records = _arms(6)
    records += [_rec("fc_loop", version=2, sha=SHA_OLD, tainted=True) for _ in range(4)]

    verdict = cr.build_report(records, now_override=NOW)["verdict"]

    assert verdict["decision"] == "INSTRUMENTATION-HOLD"
    assert verdict["exit_code"] == 2, "an invalid measurement must never exit 3"
    assert verdict["measurement_valid"] is False
    # The breach is still SEEN and still REPORTED — it is simply not actionable.
    assert verdict["zero_tolerance"]["breached"] is True
    assert verdict["zero_tolerance"]["actionable"] is False
    assert any("NOT ACTIONABLE" in note for note in verdict["zero_tolerance"]["notes"])
    assert any("telemetry_schema_version" in r
               for r in verdict["measurement_validity"]["reasons"])


def test_mixed_candidate_builds_alone_makes_the_measurement_invalid():
    records = _arms(6)
    records += [_rec("fc_loop", sha="9129280f21d4c6af10b9564ed1b5868abcb275d9",
                     tainted=True) for _ in range(3)]

    verdict = cr.build_report(records, now_override=NOW)["verdict"]

    assert verdict["exit_code"] == 2
    assert verdict["measurement_valid"] is False
    assert any("candidate builds" in r for r in verdict["measurement_validity"]["reasons"])


def test_contract_violation_rate_above_the_limit_invalidates_the_window():
    records = _arms(10)
    # 3 of 23 records (13%) are unreadable — above the 5% validity limit.
    for _ in range(3):
        broken = _rec("fc_loop", tainted=True)
        broken["security"]["tainted_write_executed_count"] = None
        records.append(broken)

    verdict = cr.build_report(records, now_override=NOW)["verdict"]

    assert verdict["exit_code"] == 2
    assert verdict["measurement_valid"] is False
    assert verdict["measurement_validity"]["violation_rate"] > \
        cr.MEASUREMENT_INVALID_VIOLATION_RATE


def test_window_level_holds_do_not_count_toward_the_violation_rate():
    """A missing control arm holds the gate but does not make the records unreadable.

    `_hold_instrumentation` adds to `violating` for window-level problems that are
    not records failing their contract, so measurement validity reads the frozen
    per-record count instead. Without that, one clean candidate record with no
    control read as 100% violating and suppressed a real breach.
    """
    candidate = _rec("fc_loop", tainted=True)

    verdict = cr.build_report([candidate], now_override=NOW)["verdict"]

    assert verdict["measurement_valid"] is True
    assert verdict["decision"] == "CANARY-BLOCK"
    assert verdict["exit_code"] == 3


def test_valid_window_with_a_candidate_breach_still_exits_3():
    records = _arms(10)
    records.append(_rec("fc_loop", tainted=True))

    verdict = cr.build_report(records, now_override=NOW)["verdict"]

    assert verdict["measurement_valid"] is True
    assert verdict["decision"] == "CANARY-BLOCK"
    assert verdict["exit_code"] == 3
    assert verdict["zero_tolerance"]["actionable"] is True


def test_clean_valid_window_still_proceeds():
    verdict = cr.build_report(_arms(10), now_override=NOW)["verdict"]

    assert verdict["decision"] == "PROCEED"
    assert verdict["exit_code"] == 0
    assert verdict["measurement_valid"] is True


# --------------------------------------------------------------------------- #
# P0-3: zero-tolerance answers for ONE ARM                                     #
# --------------------------------------------------------------------------- #

def test_control_arm_breaches_never_block_the_candidate():
    records = _arms(10)
    records += [_rec("legacy", sha="legacy-sha", tainted=True) for _ in range(5)]

    report = cr.build_report(records, now_override=NOW)

    assert report["verdict"]["zero_tolerance"]["breached"] is False
    assert report["verdict"]["exit_code"] == 0
    # ...and the control arm's breaches are still visible, as information.
    assert report["zero_tolerance_control"]["tainted_unauth_write_count"] == 5


def test_pinned_candidate_sha_excludes_an_earlier_build_of_the_same_arch():
    records = _arms(10)
    records += [_rec("fc_loop", sha=SHA_OLD, tainted=True) for _ in range(4)]

    report = cr.build_report(records, now_override=NOW, candidate_sha="1df0d4f")

    assert report["verdict"]["zero_tolerance"]["breached"] is False
    assert report["zero_tolerance_global"]["tainted_unauth_write_count"] == 0
    assert report["zero_tolerance_other_builds"]["tainted_unauth_write_count"] == 4
    # The window still mixes builds, so it is still not a measurement.
    assert report["verdict"]["exit_code"] == 2
    by_sha = report["zero_tolerance_by_candidate_sha"]
    assert by_sha[SHA_OLD]["tainted_unauth_write_count"] == 4
    assert by_sha[SHA_NEW]["tainted_unauth_write_count"] == 0


def test_pinned_sha_that_selects_nothing_holds():
    report = cr.build_report(_arms(10), now_override=NOW, candidate_sha="deadbee")

    assert report["verdict"]["exit_code"] == 2
    assert any("selects zero" in r
               for r in report["verdict"]["instrumentation"]["reasons"])


def test_zero_tolerance_arm_matches_full_and_abbreviated_forms():
    full = _rec("fc_loop", sha=SHA_NEW)
    short = _rec("fc_loop", sha=SHA_NEW[:7])
    other = _rec("fc_loop", sha=SHA_OLD)

    mine, others = cr.zero_tolerance_arm(
        [full, short, other], arch="fc_loop", candidate_sha=SHA_NEW[:7])

    assert mine == [full, short]
    assert others == [other]


# --------------------------------------------------------------------------- #
# P2-6: 7-char and 40-char forms of one commit are one build                   #
# --------------------------------------------------------------------------- #

def test_abbreviated_sha_folds_onto_the_full_sha_in_the_same_window():
    records = _arms(6)
    records += [_rec("fc_loop", sha=SHA_NEW[:7]) for _ in range(3)]

    report = cr.build_report(records, now_override=NOW)

    assert report["instrumentation"]["candidate_shas"] == [SHA_NEW]
    assert report["instrumentation"]["mixed_candidate_shas"] is False
    assert report["instrumentation"]["candidate_sha_aliases"] == {SHA_NEW[:7]: SHA_NEW}
    assert report["verdict"]["exit_code"] == 0


def test_abbreviation_with_no_full_sha_in_the_window_stays_its_own_group():
    canonical, mapping = cr.normalise_candidate_shas(["7db03e7", "unknown"])

    assert canonical == ["7db03e7", "unknown"]
    assert mapping == {"7db03e7": "7db03e7", "unknown": "unknown"}


def test_ambiguous_abbreviation_is_never_guessed():
    a = "ab" + "0" * 38
    b = "ab" + "1" * 38
    canonical, mapping = cr.normalise_candidate_shas(["ab", a, b])

    assert mapping["ab"] == "ab"
    assert set(canonical) == {"ab", a, b}


# --------------------------------------------------------------------------- #
# P2-3: the text report must show the instrumentation state                    #
# --------------------------------------------------------------------------- #

def test_render_text_prints_an_instrumentation_section():
    records = _arms(6)
    records += [_rec("fc_loop", version=2, sha=SHA_OLD, tainted=True) for _ in range(4)]
    report = cr.build_report(records, now_override=NOW)

    text = cr.render_text(report)

    assert "[INSTRUMENTATION]" in text
    assert "measurement_valid=False" in text
    assert "schema versions" in text
    assert "candidate builds" in text
    assert "MIXED" in text
    assert "NOT ACTIONABLE" in text
    assert "BREACH (not actionable)" in text


def test_render_text_instrumentation_section_is_printed_on_a_clean_window_too():
    text = cr.render_text(cr.build_report(_arms(10), now_override=NOW))

    assert "[INSTRUMENTATION] measurement_valid=True" in text
    assert "violation rate" in text
