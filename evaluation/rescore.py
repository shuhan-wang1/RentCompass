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

    python -m evaluation.rescore --runs DIR [DIR ...] --cases evaluation/benchmark/cases.jsonl \\
        [--out rescored.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bootstrap():
    """Put the app packages on sys.path exactly as run_benchmark does, so the graders
    import identically. No app graph is built and no tool ever runs."""
    for p in (REPO_ROOT / "app", REPO_ROOT / "src", REPO_ROOT):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def _evaluator_identity() -> Dict[str, Any]:
    graders_path = REPO_ROOT / "evaluation" / "metrics" / "graders.py"
    sha = None
    try:
        sha = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        pass
    dirty = None
    try:
        st = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10).stdout
        dirty = bool(st.strip())
    except Exception:
        pass
    return {
        "evaluator_sha": sha,
        "evaluator_dirty": dirty,
        "grader_sha256": hashlib.sha256(graders_path.read_bytes()).hexdigest(),
    }


def load_cases(path: Path) -> Dict[str, dict]:
    return {json.loads(l)["case_id"]: json.loads(l)
            for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def rescore_dir(run_dir: Path, cases: Dict[str, dict], graders,
                expected_contract: Optional[str] = None) -> Dict[str, Any]:
    gi = run_dir / "grader_input.jsonl"
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not gi.exists():
        return {"run_dir": run_dir.name, "rescorable": False,
                "reason": "no grader_input.jsonl — this round predates evidence persistence "
                          "and cannot be faithfully re-scored; retain as evidence only",
                "product_sha": manifest.get("product_sha") or manifest.get("git_commit")}

    # IDENTITY GATE (2026-07-23 ruling req.7). A unified re-score is only meaningful if
    # every arm declares which PRODUCT ran, which CAPTURE tree recorded it, and which case
    # contract it was recorded against. A missing or mismatched identity is refused, never
    # scored with a default — a silently defaulted identity is how two different products
    # get compared as if they were one.
    ident_problems = []
    product = manifest.get("product_sha")
    capture = manifest.get("capture_sha") or manifest.get("product_sha")
    contract = manifest.get("case_contract_sha256")
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
    if ident_problems:
        return {"run_dir": run_dir.name, "rescorable": False,
                "reason": "IDENTITY REFUSED: " + "; ".join(ident_problems),
                "product_sha": product, "capture_sha": capture}

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
        "n": len(rows),
        "rescored_passed": sum(1 for r in rows if r.get("rescored_passed")),
        "scored_passed": sum(1 for r in rows if r.get("scored_passed")),
        "verdict_changes": sum(1 for r in rows if r.get("changed")),
        "digest_mismatches": [r["run_id"] for r in rows if r.get("evidence_digest_ok") is False],
        "duplicate_records_dropped": duplicates,
        "rows": rows,
    }


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

    report = {"evaluator": ident,
              "runs": [rescore_dir(Path(d), cases, graders,
                                   expected_contract=ident["case_contract_sha256"])
                       for d in args.runs]}

    print("evaluator:", json.dumps(ident, indent=2))
    unre = [r for r in report["runs"] if not r.get("rescorable")]
    for r in report["runs"]:
        if not r.get("rescorable"):
            print(f"  {r['run_dir']:20} NOT RE-SCORABLE — {r['reason']}")
            continue
        print(f"  {r['run_dir']:20} product={r['product_sha']} capture={r['capture_sha']} "
              f"n={r['n']:3} "
              f"scored={r['scored_passed']:3} -> rescored={r['rescored_passed']:3} "
              f"(changed {r['verdict_changes']})"
              + (f"  [{r['duplicate_records_dropped']} dup records dropped]"
                 if r.get("duplicate_records_dropped") else "")
              + (f"  DIGEST MISMATCH {r['digest_mismatches']}" if r["digest_mismatches"] else ""))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.out}")
    if unre:
        print(f"\n{len(unre)} run dir(s) could not be re-scored — a paired comparison over "
              f"them is NOT single-evaluator and must not be reported as one.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
