"""Re-score persisted benchmark runs with ONE evaluator (2026-07-23 ruling).

Why this exists. In a paired A/B the two arms necessarily execute different product code,
so each arm also carries its own graders and its own case contracts. Comparing the
`passed` flags each arm computed for itself therefore compares two evaluators as much as
two products — and when the grader is under active repair (as it was after the E11
threshold defects), that comparison is not sound at all.

This re-scores BOTH arms' persisted `grader_input.jsonl` with the graders and case
contracts of THIS tree, and records which evaluator did it. Nothing is re-executed: no
model, no tools, no network. A run can only be re-scored if it persisted its grader
input — rounds produced before that landed are retainable as evidence but NOT
re-scorable, and this tool says so rather than silently scoring fewer runs.

The identity gate keys on the GRADER as well as the contract (2026-07-27). It used to
stamp `grader_sha256` and assert only `case_contract_sha256`, so a grader change walked
straight through: the same retained evidence scored fc 59/98 under one grader and 74/98
under the next with the contract digest unchanged for part of that move. Grader identity is
now a tri-state — `match` / `GRADER-MISMATCH` / `UNKNOWN` — over the whole
verdict-determining file set (`results_package.GRADER_SET_FILES`), and `UNKNOWN` is never
folded into `match`.

    python -m evaluation.rescore --runs DIR [DIR ...] --cases evaluation/benchmark/cases.jsonl \\
        [--out rescored.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from evaluation.results_package import (
    GRADER_MATCH,
    GRADER_MISMATCH,
    GRADER_SET_PRIMARY,
    compare_grader_identity,
    grader_set_identity,
    recorded_grader_key,
    resolve_commit_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bootstrap():
    """Put the app packages on sys.path exactly as run_benchmark does, so the graders
    import identically. No app graph is built and no tool ever runs."""
    for p in (REPO_ROOT / "app", REPO_ROOT / "src", REPO_ROOT):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def _evaluator_identity() -> Dict[str, Any]:
    """WHO is about to score. Every field here is asserted on below — see the gate.

    The commit probe is delegated to ``results_package.resolve_commit_identity`` rather
    than re-rolled locally. The local version returned ``evaluator_dirty: False`` whenever
    git could not answer at all (``bool("".strip())``), which is the NORMAL condition here:
    the re-scorer runs in the bench container off a bind mount, and a git worktree's
    ``.git`` is a FILE pointing at a host path that does not exist inside it. Observed
    2026-07-27 on real evidence — ``evaluator_sha: null`` next to ``evaluator_dirty:
    false``, i.e. an unread tree reported as clean. This was the last file exempted from
    ``tests/test_eval_writer_commit_identity.py``'s git-probe guard; the exemption is now
    removed.
    """
    ident = resolve_commit_identity(repo_root=REPO_ROOT)
    grader = grader_set_identity(REPO_ROOT)
    return {
        "evaluator_sha": ident["git_commit"],
        # Tri-state on purpose: None means "git could not look", NOT "clean".
        "evaluator_dirty": ident["git_dirty"],
        "evaluator_commit_source": ident["git_commit_source"],
        "evaluator_commit_trust": ident["commit_trust"],
        "evaluator_identity_warnings": list(ident["identity_warnings"]),
        # graders.py alone, kept so this report stays comparable to every manifest already
        # on disk. It is NOT what the gate keys on — it is too narrow (see GRADER_SET_FILES).
        "grader_sha256": grader["grader_set_files"][GRADER_SET_PRIMARY],
        **grader,
    }


def load_cases(path: Path) -> Dict[str, dict]:
    return {json.loads(l)["case_id"]: json.loads(l)
            for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def rescore_dir(run_dir: Path, cases: Dict[str, dict], graders,
                expected_contract: Optional[str] = None,
                expected_grader: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    gi = run_dir / "grader_input.jsonl"
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not gi.exists():
        return {"run_dir": run_dir.name, "rescorable": False,
                "reason": "no grader_input.jsonl — this round predates evidence persistence "
                          "and cannot be faithfully re-scored; retain as evidence only",
                "product_sha": manifest.get("product_sha") or manifest.get("git_commit")}

    # IDENTITY GATE (2026-07-23 ruling req.7; grader leg added 2026-07-27). A unified
    # re-score is only meaningful if every arm declares which PRODUCT ran, which CAPTURE
    # tree recorded it, which case contract it was recorded against, and WHICH EVALUATOR
    # produced the `scored_passed` column this report prints beside the re-scored one. A
    # missing or mismatched identity is refused, never scored with a default — a silently
    # defaulted identity is how two different products get compared as if they were one.
    #
    # The grader leg is the half that was missing. `grader_sha256` was stamped from the
    # start and asserted on nowhere, so a grader change slipped through a gate that keys
    # only on the contract — exactly what happened when the same retained evidence scored
    # fc 59/98 and then 74/98 with the contract digest unchanged. Verified 2026-07-27 on
    # real stored rounds: `idp98_r1_base` recorded grader c25a027d04… and `idp98_r1_cand`
    # recorded 4cf33e0553…, one paired round, ONE contract, TWO graders, and this gate
    # passed both clean.
    ident_problems = []
    product = manifest.get("product_sha")
    capture = manifest.get("capture_sha") or manifest.get("product_sha")
    contract = manifest.get("case_contract_sha256")
    grader = compare_grader_identity(manifest, expected_grader)
    if not product:
        ident_problems.append("manifest carries no product_sha")
    if not capture:
        ident_problems.append("manifest carries no capture_sha")
    if not contract:
        ident_problems.append("manifest carries no case_contract_sha256")
    elif expected_contract and contract != expected_contract:
        ident_problems.append(
            f"case_contract_sha256 {contract[:12]}… != the evaluator's contract "
            f"{expected_contract[:12]}… — the arms did not score the same contract")
    # A provable grader mismatch is refused on the SAME footing as a contract mismatch, and
    # with no opt-out flag: §3B already settled that the one legitimate reason to score
    # evidence against a different evaluator lives in a purpose-built tool that says so
    # (`contract_delta.py`), NOT in a loosened `rescore.py` — a flag here would remove the
    # guarantee for every other caller. UNKNOWN is NOT refused: the round of record and
    # every other retained package predate `grader_set_sha256`, and refusing them all would
    # be a gate nobody leaves switched on. It is reported as UNKNOWN instead, which is the
    # third answer this codebase already chose for the working tree's dirty flag.
    if grader["grader_identity"] == GRADER_MISMATCH:
        ident_problems.append(grader["grader_detail"])
    if ident_problems:
        return {"run_dir": run_dir.name, "rescorable": False,
                "reason": "IDENTITY REFUSED: " + "; ".join(ident_problems),
                "product_sha": product, "capture_sha": capture, **grader,
                # Carried on the refusal path too: "which grader did this arm record itself
                # under" is a fact about the stored round, independent of whether we are
                # willing to re-score it, and the cross-arm check needs it either way.
                "recorded_grader_key": recorded_grader_key(manifest)}

    # grader_input.jsonl is APPENDED to, so a resumed run — or an out dir reused without
    # being cleaned — carries a run_id more than once. Deduplicate on run_id keeping the
    # LAST record (the one that actually produced the run's final verdict) and REPORT the
    # count: silently scoring 196 records for a 98-case round is how a duplicate becomes
    # a number nobody questions.
    by_run: Dict[str, dict] = {}
    duplicates = 0
    for line in gi.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        rid = rec.get("run_id") or f"{rec.get('case_id')}#{rec.get('repeat')}"
        if rid in by_run:
            duplicates += 1
        by_run[rid] = rec

    rows: List[dict] = []
    for rec in by_run.values():
        case = cases.get(rec["case_id"])
        if case is None:
            rows.append({"case_id": rec["case_id"], "run_id": rec.get("run_id"),
                         "error": "case_id absent from the supplied case contract"})
            continue
        # Integrity: prove we scored the same bytes the run recorded.
        blob = json.dumps(rec.get("evidence") or [], ensure_ascii=False,
                          sort_keys=True, default=str)
        digest_ok = (hashlib.sha256(blob.encode("utf-8")).hexdigest()
                     == rec.get("raw_evidence_sha256"))
        gin = rec["grader_input"]
        ctx = graders.GradeContext(
            final_answer=gin.get("final_answer") or "",
            tools_called=gin.get("tools_called") or [],
            tool_call_events=gin.get("tool_call_events") or [],
            evidence=rec.get("evidence") or [],
            route=gin.get("route"),
            user_texts=gin.get("user_texts") or [],
            reference_calculations=gin.get("reference_calculations"),
            error=gin.get("error"),
            reconstructed_context=gin.get("reconstructed_context"),
            history_texts=gin.get("history_texts") or [],
        )
        verdict = graders.grade_case(case, ctx)
        rows.append({
            "case_id": rec["case_id"],
            "run_id": rec.get("run_id"),
            "repeat": rec.get("repeat"),
            "evidence_digest_ok": digest_ok,
            "scored_passed": rec.get("scored_passed"),
            "rescored_passed": verdict.passed,
            "changed": bool(rec.get("scored_passed")) != bool(verdict.passed),
            "failed_constraints": [c.get("type") for c in verdict.to_dict().get("constraints", [])
                                   if not c.get("passed")],
        })
    return {
        "run_dir": run_dir.name,
        "rescorable": True,
        "product_sha": product,
        "capture_sha": capture,
        "capture_is_product": product == capture,
        "case_contract_sha256": contract,
        # Which evaluator produced `scored_passed`, and how sure we are of that. Carried
        # into the report so the number cannot be quoted without the grader stated
        # (HANDOFF §3.14: "Any future round must state which grader produced it").
        **grader,
        "recorded_grader_key": recorded_grader_key(manifest),
        "n": len(rows),
        "rescored_passed": sum(1 for r in rows if r.get("rescored_passed")),
        "scored_passed": sum(1 for r in rows if r.get("scored_passed")),
        "verdict_changes": sum(1 for r in rows if r.get("changed")),
        "digest_mismatches": [r["run_id"] for r in rows if r.get("evidence_digest_ok") is False],
        "duplicate_records_dropped": duplicates,
        "rows": rows,
    }


def _cross_arm_grader_check(runs: List[dict]) -> Dict[str, Any]:
    """Did the ARMS OF THIS ROUND record themselves under one grader?

    Separate from the per-run gate and asserted separately, because it is a different
    claim. The per-run gate answers "is the evaluator about to score this the one that
    recorded it"; this answers "is the ``scored`` column one quantity across the arms at
    all". `idp98_r1_base` (grader c25a027d04…) and `idp98_r1_cand` (grader 4cf33e0553…) are
    a real paired round on disk where the answer is no, under ONE contract — and where
    nothing said so.
    """
    keys = {r["run_dir"]: r["recorded_grader_key"] for r in runs
            if r.get("recorded_grader_key")}
    distinct = {k for k in keys.values() if k != "undeclared"}
    return {"recorded_grader_keys": keys, "arms_agree_on_grader": len(distinct) <= 1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", nargs="+", required=True, help="run output directories")
    ap.add_argument("--cases", required=True, help="the ONE case contract to score against")
    ap.add_argument("--out", default=None, help="write the JSON report here")
    args = ap.parse_args()

    _bootstrap()
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    from evaluation.metrics import graders

    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    ident = _evaluator_identity()
    ident["case_contract"] = str(cases_path)
    ident["case_contract_sha256"] = hashlib.sha256(cases_path.read_bytes()).hexdigest()

    runs = [rescore_dir(Path(d), cases, graders,
                        expected_contract=ident["case_contract_sha256"],
                        expected_grader=ident)
            for d in args.runs]
    report = {"evaluator": ident, "runs": runs, **_cross_arm_grader_check(runs)}

    print("evaluator:", json.dumps(ident, indent=2))
    unre = [r for r in report["runs"] if not r.get("rescorable")]
    for r in report["runs"]:
        if not r.get("rescorable"):
            print(f"  {r['run_dir']:20} NOT RE-SCORABLE — {r['reason']}")
            continue
        print(f"  {r['run_dir']:20} product={r['product_sha']} capture={r['capture_sha']} "
              f"grader={r['grader_identity']} "
              f"n={r['n']:3} "
              f"scored={r['scored_passed']:3} -> rescored={r['rescored_passed']:3} "
              f"(changed {r['verdict_changes']})"
              + (f"  [{r['duplicate_records_dropped']} dup records dropped]"
                 if r.get("duplicate_records_dropped") else "")
              + (f"  DIGEST MISMATCH {r['digest_mismatches']}" if r["digest_mismatches"] else ""))
        if r["grader_identity"] != GRADER_MATCH:
            print(f"  {'':20} ^ grader {r['grader_identity']}: {r['grader_detail']}")
    if not report["arms_agree_on_grader"]:
        print("\n!! THE ARMS DID NOT RECORD UNDER ONE GRADER: "
              + "; ".join(f"{k} -> {v}" for k, v in
                          sorted(report["recorded_grader_keys"].items()))
              + "\n   Their own `scored` columns are two evaluators' verdicts and must not "
                "be compared with each other, whatever the re-scored column says.")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.out}")
    if unre:
        print(f"\n{len(unre)} run dir(s) could not be re-scored — a paired comparison over "
              f"them is NOT single-evaluator and must not be reported as one.")
        return 1
    if not report["arms_agree_on_grader"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
