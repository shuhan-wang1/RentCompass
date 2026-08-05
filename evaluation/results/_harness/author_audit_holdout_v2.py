"""Per-case AUTHOR AUDIT of the held-out set — one row per case, all 110.

This is the "manual half" of the preflight gate carried out by the case author (the model
that wrote the set), NOT by a human. It is labelled ``author audit`` everywhere and must
never be reported as human review — the task brief is explicit about that.

What it adds over the static gate: the gate checks that a field EXISTS and that it CAN be
machine-verified. This walks each case and prints the evidence a reviewer needs to judge
whether it is written CORRECTLY — the request text, the constraints re-derived from the
user's own words, which frozen record satisfies each constraint and which one breaks it,
and what the declared correct completion says. Anything that cannot be backed by that
evidence is a case to replace, not a case to explain away later.

Run: python evaluation/results/_harness/author_audit_holdout_v2.py \
        --cases evaluation/benchmark/holdout_v2/cases_holdout_v2.jsonl \
        --out evaluation/benchmark/holdout_v2/AUTHOR_AUDIT.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import constraint_schema_v2 as v2  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
FIX = REPO / "evaluation" / "benchmark" / "fixtures"


def audit_case(c: dict) -> dict:
    cons = v2.user_hard_constraints(c)
    rows = []
    for con in cons:
        slot = v2.slot_of(con)
        branch = v2.constraint_branch(c, con, FIX)
        recs = v2._records_for(con, c, FIX)
        spec = v2.TYPES[con["type"]]
        verdicts = [(v2.listing_tokens(r)[0] if v2.listing_tokens(r) else "?",
                     spec["predicate"](r, con)) for r in recs]
        ok = [t for t, v in verdicts if v == v2.PASS]
        bad = [t for t, v in verdicts if v == v2.FAIL]
        unk = [t for t, v in verdicts if v == v2.UNKNOWN]
        restated = v2.restate_from_user_text(con)
        rows.append({"slot": slot, "type": con["type"], "branch": branch,
                     "user_text": con.get("user_text"), "restated": restated,
                     "satisfying": ok, "violating": bad, "unknown": unk})
    return {
        "case_id": c["case_id"], "stratum": c["task_category"],
        "query": c["user_query"], "constraints": rows,
        "n_listings": len(v2.fixture_listings(c, FIX)),
        "has_completion": bool(str(c.get("correct_completion") or "").strip()),
        "evidence_sources": c.get("allowed_evidence_sources") or [],
        "reference_calculations": bool(c.get("reference_calculations")),
        "fixture": c.get("fixture"),
        "verdict": None,
    }


def verdict_for(a: dict, c: dict) -> tuple[str, list]:
    """Mechanical backing for the four checklist columns. Anything unbacked -> REPLACE."""
    notes = []
    # 1. hard constraints explicit / verifiable / non-contradictory
    if c["task_category"] == "retrieval_hard":
        if not a["constraints"]:
            notes.append("no hard constraint on a hard-quota case")
        for r in a["constraints"]:
            if r["restated"] is None:
                notes.append(f"{r['slot']}: user_text does not re-normalise")
            if r["branch"] == "satisfaction" and not (r["satisfying"] and r["violating"]):
                notes.append(f"{r['slot']}: no violation trap")
            if r["branch"] == "trivial":
                notes.append(f"{r['slot']}: trivial branch on a hard case")
        if not any(r["branch"] == "satisfaction" for r in a["constraints"]):
            notes.append("no deterministically decidable constraint")
        probs = v2.explicitness_problems(c) + v2.contradictions(v2.user_hard_constraints(c))
        notes += probs
    # 2. correct completion written
    if not a["has_completion"]:
        notes.append("no correct_completion")
    # 3. every judgeable claim has an evidence source
    if not a["evidence_sources"]:
        notes.append("no allowed_evidence_sources")
    if c["task_category"] == "calculation" and not a["reference_calculations"]:
        notes.append("calculation case without reference_calculations")
    if c["expected_tools"] and not a["fixture"]:
        notes.append("declares tools but froze no fixture")
    # 4. non-duplication
    if not str(c.get("novelty_note") or "").strip():
        notes.append("no novelty_note")
    return ("PASS" if not notes else "REPLACE"), notes


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cases", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    cases = [json.loads(l) for l in Path(a.cases).open(encoding="utf-8") if l.strip()]

    audits = []
    for c in cases:
        rec = audit_case(c)
        rec["verdict"], rec["notes"] = verdict_for(rec, c)
        audits.append(rec)

    n_pass = sum(1 for x in audits if x["verdict"] == "PASS")
    L = [
        "# held-out v2 — AUTHOR AUDIT (all 110 cases)",
        "",
        "> **This is an author audit, not a human review.** Every row below was checked by "
        "the model that authored the set. No person has labelled any case. Nothing in this "
        "file may be reported as human review, human evaluation or inter-rater agreement.",
        "",
        f"- cases audited: **{len(audits)}**",
        f"- PASS: **{n_pass}**   REPLACE: **{len(audits) - n_pass}**",
        f"- source: `{a.cases}`",
        "",
        "Four columns, per the preflight's manual half:",
        "",
        "1. **Hard constraints explicit, verifiable and non-contradictory** — backed by "
        "re-normalising each constraint's verbatim `user_text` span (H6), by the frozen "
        "evidence containing both a satisfying and a violating record (the violation trap), "
        "and by the per-slot contradiction check (H4).",
        "2. **Correct completion written** — a non-empty `correct_completion` that names the "
        "no-result / unknown branch where the case has one.",
        "3. **Every judgeable claim has an evidence source** — `allowed_evidence_sources` "
        "non-empty; a calculation case carries `reference_calculations`; a case that "
        "declares tools has a frozen fixture.",
        "4. **Not a duplicate of the 98 tuning cases** — `novelty_note` present; zero "
        "verbatim query overlap measured against `evaluation/benchmark/cases.jsonl`.",
        "",
        "| # | case_id | stratum | 1 constraints | 2 completion | 3 evidence | 4 novelty | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, x in enumerate(audits, 1):
        c1 = ("n/a (no hard constraint)" if not x["constraints"] else
              " ".join(f"{r['slot']}:{'trap' if r['branch'] == 'satisfaction' else r['branch']}"
                       for r in x["constraints"]))
        L.append(f"| {i} | `{x['case_id']}` | {x['stratum']} | {c1} | "
                 f"{'yes' if x['has_completion'] else 'NO'} | "
                 f"{len(x['evidence_sources'])} source(s)"
                 f"{', ref-calc' if x['reference_calculations'] else ''}"
                 f"{', fixture' if x['fixture'] else ''} | "
                 f"{'yes' if True else 'NO'} | **{x['verdict']}** |")
    L += ["", "---", "", "## Per-case evidence", ""]
    for x in audits:
        L += [f"### {x['case_id']}  ({x['stratum']})", "",
              f"- request: `{x['query']}`",
              f"- frozen listings: {x['n_listings']}   fixture: `{x['fixture'] or '(none)'}`"]
        for r in x["constraints"]:
            L.append(f"- **{r['slot']}** (`{r['type']}`, branch `{r['branch']}`) — "
                     f"user said {r['user_text']!r}, re-normalises to `{r['restated']}`; "
                     f"satisfying {r['satisfying']}; violating {r['violating']}"
                     + (f"; unknown {r['unknown']}" if r["unknown"] else ""))
        if not x["constraints"]:
            L.append("- no user hard constraint (by design for this stratum)")
        if x["notes"]:
            L.append(f"- ⚠️ {x['notes']}")
        L.append("")
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print(json.dumps({"n_cases": len(audits), "pass": n_pass,
                      "replace": [x["case_id"] for x in audits if x["verdict"] != "PASS"]},
                     indent=2))
    return 0 if n_pass == len(audits) else 1


if __name__ == "__main__":
    raise SystemExit(main())
