#!/usr/bin/env python3
"""Fail-closed cost attribution for canary.turn records.

The request path records raw usage; this script applies a versioned price table
offline. Records are kept in exact (agent_arch, rollout_id) groups so a manager
candidate can never be folded into an fc or legacy total.

CLI compatibility is retained with the original script:

    python3 scripts/canary_cost.py CANARY.jsonl --prices PRICES.json

--allow-unverified is still accepted for operator compatibility, but it can
only expose a clearly-labelled provisional figure. An unverified price table
always produces HOLD and a non-zero exit status.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from canary_report import (  # noqa: E402
    OBSERVER_INSTALLED_FIELD, UNOBSERVABLE_OUTCOMES, load_records,
)


EXIT_PROCEED = 0
EXIT_HOLD = 2
MISSING_DIMENSION = "<missing>"
INVALID_DIMENSION = "<invalid>"
# A record with no rollout_id is not a broken record. Every historical turn and
# every direct :5001/:5002 smoke turn has none — the field only exists on traffic
# the trusted edge labelled. Treating its absence as a malformed identity put
# `decision=HOLD, total_cost=None` on all of them permanently, so the entire cost
# history could never be costed by the script written to cost it. It gets its own
# named bucket and an INFORMATIONAL issue instead: attribution is coarser, but the
# usage is fully measured and the arithmetic is sound.
UNLABELLED_ROLLOUT = "<none>"
# ...but only for traffic that was never supposed to carry one. `canary_report`
# already treats a `traffic_source="edge"` record without a `rollout_id` as a
# contract violation ("edge record has missing/invalid rollout_id"), because the
# trusted edge is the thing that stamps the label. Folding such a record into the
# historical bucket silently moved this rollout's spend into "pre-rollout" — one
# mislabelled edge turn with 999k input tokens read as `r-1: $0.00076` next to
# `<none>: $0.399`, and the report said PROCEED. It gets its own bucket and, unlike
# `<none>`, it BLOCKS: an unlabelled edge record means the edge is misconfigured,
# and cost attribution for the rollout cannot be trusted until it is fixed.
UNLABELLED_EDGE_ROLLOUT = "<unlabelled-edge>"
EDGE_TRAFFIC_SOURCE = "edge"
KNOWN_AGENT_ARCHES = frozenset({"legacy", "fc_loop", "manager_v1"})
VALID_USAGE_STATUSES = frozenset(
    {"complete", "partial", "no_llm_calls", "not_instrumented"}
)
USAGE_FIELDS = ("calls", "input_tokens", "output_tokens", "cache_read_tokens")
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_REPORTED_ISSUES = 100


def load_prices(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("price table must be a JSON object")
    return payload


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validated_usage(
    usage: Any, *, record_label: str
) -> Tuple[Optional[Dict[str, Dict[str, int]]], List[str]]:
    """Validate one llm_usage_status=complete payload.

    Missing models, missing counters, negative values, or inconsistent top-level
    totals invalidate the complete claim. None of that record's usage is then
    added to a total.
    """
    issues: List[str] = []
    if not isinstance(usage, dict):
        return None, [f"{record_label}: complete usage is not an object"]
    models = usage.get("models")
    if not isinstance(models, dict) or not models:
        return None, [f"{record_label}: complete usage has no model breakdown"]

    validated: Dict[str, Dict[str, int]] = {}
    totals = {field: 0 for field in USAGE_FIELDS}
    for raw_model, raw_metrics in models.items():
        if (
            not isinstance(raw_model, str)
            or not raw_model.strip()
            or raw_model != raw_model.strip()
        ):
            issues.append(f"{record_label}: model name is missing or not canonical")
            continue
        if not isinstance(raw_metrics, dict):
            issues.append(
                f"{record_label}: usage for model {raw_model!r} is not an object"
            )
            continue
        metrics: Dict[str, int] = {}
        for field in USAGE_FIELDS:
            value = raw_metrics.get(field)
            if not _non_negative_int(value):
                issues.append(
                    f"{record_label}: model {raw_model!r} {field} "
                    "is not a non-negative int"
                )
            else:
                metrics[field] = value
        if len(metrics) != len(USAGE_FIELDS):
            continue
        if metrics["calls"] < 1:
            issues.append(f"{record_label}: model {raw_model!r} reports zero calls")
            continue
        validated[raw_model] = metrics
        for field in USAGE_FIELDS:
            totals[field] += metrics[field]

    if not validated:
        issues.append(f"{record_label}: complete usage contains no valid model entry")

    # The producer emits these totals. Requiring and reconciling them prevents a
    # partial model map from looking like the whole turn's spend.
    for field in USAGE_FIELDS:
        value = usage.get(field)
        if not _non_negative_int(value):
            issues.append(
                f"{record_label}: top-level usage.{field} is missing or invalid"
            )
        elif value != totals[field]:
            issues.append(
                f"{record_label}: usage.{field}={value} does not match models "
                f"total {totals[field]}"
            )
    if _non_negative_int(usage.get("calls")) and usage["calls"] < 1:
        issues.append(f"{record_label}: complete usage reports zero total calls")

    return (validated if not issues else None), issues


def sum_usage(records: List[dict]) -> Dict[str, Any]:
    """Aggregate fully measured usage while preserving legacy model keys.

    Metadata lives under underscore-prefixed keys so the original
    compute_cost(sum_usage(...), ...) call pattern remains valid. A genuine
    no_llm_calls turn is counted separately and is not unmeasured.
    """
    per_model: Dict[str, Any] = {}
    unmeasured = 0
    no_calls = 0
    chargeable = 0
    # A SUBSET of `unmeasured`, never a substitute for it: a turn that crashed while
    # the callback observer was running still cost real money we cannot count, so it
    # stays chargeable and unpriced. The split exists so an operator reading a HOLD
    # can tell "the observer is broken" from "the candidate is crashing" — two very
    # different next actions, previously one number. canary_report withdraws its
    # CONTRACT complaint for these turns; the spend is still unknown, and this tool
    # still says so.
    unobservable = 0
    issues: List[str] = []

    for index, record in enumerate(records):
        label = f"record[{index}]"
        if not isinstance(record, dict):
            unmeasured += 1
            chargeable += 1
            issues.append(f"{label}: record is not an object")
            continue
        status = record.get("llm_usage_status")
        usage = record.get("llm_usage")

        if status == "no_llm_calls":
            if usage is not None:
                # Contradictory evidence: do not price this ambiguous turn as zero.
                unmeasured += 1
                issues.append(
                    f"{label}: no_llm_calls carries a non-null llm_usage"
                )
            else:
                no_calls += 1
            continue

        chargeable += 1
        if status not in VALID_USAGE_STATUSES:
            unmeasured += 1
            issues.append(
                f"{label}: llm_usage_status is missing or invalid ({status!r})"
            )
            continue
        if status != "complete":
            unmeasured += 1
            if (record.get("turn_outcome") in UNOBSERVABLE_OUTCOMES
                    and record.get(OBSERVER_INSTALLED_FIELD) is True):
                unobservable += 1
                issues.append(
                    f"{label}: llm_usage_status={status!r} on an unobservable "
                    f"{record.get('turn_outcome')!r} turn (observer was installed): "
                    f"unmeasurable spend, not broken instrumentation"
                )
            else:
                issues.append(
                    f"{label}: llm_usage_status={status!r} is not fully measured"
                )
            continue

        validated, record_issues = _validated_usage(
            usage, record_label=label
        )
        if record_issues:
            unmeasured += 1
            issues.extend(record_issues)
            continue
        assert validated is not None
        for model, metrics in validated.items():
            slot = per_model.setdefault(
                model, {field: 0 for field in USAGE_FIELDS}
            )
            for field in USAGE_FIELDS:
                slot[field] += metrics[field]

    per_model["_unmeasured_turns"] = {"count": unmeasured}
    per_model["_unobservable_turns"] = {"count": unobservable}
    per_model["_no_llm_call_turns"] = {"count": no_calls}
    per_model["_chargeable_turns"] = {"count": chargeable}
    per_model["_usage_issues"] = {
        "count": len(issues),
        "items": issues[:_MAX_REPORTED_ISSUES],
    }
    return per_model


def _metadata_count(per_model: dict, key: str) -> int:
    value = per_model.get(key)
    if not isinstance(value, dict):
        return 0
    count = value.get("count")
    return count if _non_negative_int(count) else 0


def _price_rate(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def compute_cost(
    per_model: dict, prices: dict, allow_unverified: bool = False
) -> dict:
    """Price one aggregate and return a fail-closed gate result.

    allow_unverified never clears the gate. It only permits a provisional_cost
    field when every numerical rate is present.
    """
    issues: List[str] = []
    usage_issue_meta = per_model.get("_usage_issues")
    usage_issue_count = (
        usage_issue_meta.get("count", 0)
        if isinstance(usage_issue_meta, dict)
        else 0
    )
    usage_issue_items = (
        usage_issue_meta.get("items", [])
        if isinstance(usage_issue_meta, dict)
        else []
    )
    unmeasured = _metadata_count(per_model, "_unmeasured_turns")
    if unmeasured or usage_issue_count:
        issues.append(
            f"{unmeasured} chargeable/ambiguous turn(s) have incomplete LLM usage"
        )
        issues.extend(str(item) for item in usage_issue_items[:10])

    if not isinstance(prices, dict):
        prices = {}
        issues.append("price table is not an object")
    price_version = prices.get("price_table_version")
    if (
        price_version is None
        or isinstance(price_version, bool)
        or not isinstance(price_version, (int, str))
        or (isinstance(price_version, str) and not price_version.strip())
    ):
        issues.append("price_table_version is missing or invalid")
    currency = prices.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        issues.append("price table currency is missing or invalid")
    if prices.get("unit") != "per_1m_tokens":
        issues.append("price table unit must be 'per_1m_tokens'")
    verified = prices.get("unverified") is False
    if not verified:
        issues.append(
            f"price table v{price_version} is unverified "
            "or lacks an explicit unverified=false marker"
        )
    rates = prices.get("models")
    if not isinstance(rates, dict):
        rates = {}
        issues.append("price table models mapping is missing or invalid")

    total = 0.0
    lines = []
    missing_or_invalid_rates: List[str] = []
    usage_models = [
        name for name in per_model if not str(name).startswith("_")
    ]
    for model in sorted(usage_models):
        metrics = per_model.get(model)
        if not isinstance(metrics, dict) or any(
            not _non_negative_int(metrics.get(field))
            for field in USAGE_FIELDS
        ):
            issues.append(f"aggregated usage for model {model!r} is malformed")
            continue
        raw_rate = rates.get(model)
        if not isinstance(raw_rate, dict):
            missing_or_invalid_rates.append(model)
            continue
        parsed_rates = {
            field: _price_rate(raw_rate.get(field))
            for field in ("input", "output", "cache_read")
        }
        if any(value is None for value in parsed_rates.values()):
            missing_or_invalid_rates.append(model)
            continue
        cost = (
            metrics["input_tokens"] * parsed_rates["input"]
            + metrics["output_tokens"] * parsed_rates["output"]
            + metrics["cache_read_tokens"] * parsed_rates["cache_read"]
        ) / 1_000_000.0
        total += cost
        lines.append(
            {"model": model, **metrics, "cost": round(cost, 6)}
        )

    if missing_or_invalid_rates:
        issues.append(
            "no verified non-negative rate for model(s) "
            f"{sorted(missing_or_invalid_rates)} in price table "
            f"v{price_version}"
        )

    pricing_numerically_complete = not missing_or_invalid_rates
    usage_complete = not unmeasured and not usage_issue_count
    ok = not issues
    rounded = round(total, 6)
    result = {
        "ok": ok,
        "decision": "PROCEED" if ok else "HOLD",
        "exit_code": EXIT_PROCEED if ok else EXIT_HOLD,
        "currency": currency,
        "price_table_version": price_version,
        # Never label an incomplete or unverified subtotal as total cost.
        "total_cost": rounded if ok else None,
        "by_model": lines,
        "unmeasured_turns": unmeasured,
        # The crash-driven subset of the line above. Same spend, different cause,
        # different fix.
        "unobservable_turns": _metadata_count(
            per_model, "_unobservable_turns"
        ),
        "no_llm_call_turns": _metadata_count(
            per_model, "_no_llm_call_turns"
        ),
        "chargeable_turns": _metadata_count(
            per_model, "_chargeable_turns"
        ),
        "issues": issues[:_MAX_REPORTED_ISSUES],
    }
    if issues:
        result["error"] = "; ".join(issues[:10])
    if (
        allow_unverified
        and not verified
        and pricing_numerically_complete
        and usage_complete
    ):
        result["provisional_cost"] = rounded
    return result


def _dimension(
    value: Any, *, name: str
) -> Tuple[str, Optional[str]]:
    if value is None:
        return MISSING_DIMENSION, f"missing {name}"
    if not isinstance(value, str) or not _MACHINE_ID_RE.fullmatch(value):
        return INVALID_DIMENSION, f"invalid {name}"
    return value, None


def group_records(
    records: Sequence[dict],
) -> Tuple[
    Dict[Tuple[str, str], List[dict]],
    Dict[Tuple[str, str], List[str]],
    Dict[Tuple[str, str], List[str]],
]:
    """Group without canonicalisation; malformed identities get explicit buckets.

    Returns ``(groups, blocking_issues, informational_notes)``. The third value is
    the fix for the permanent-HOLD defect: a MISSING rollout_id is a fact about
    when the record was written, not a defect in it, so it is reported and costed
    rather than refused. A MALFORMED one is still a defect and still HOLDs — the
    two were previously collapsed into the same ``<missing>`` bucket, which is how
    "we cannot parse this id" and "this predates ids" ended up with one verdict.

    A missing id is only "a fact about when it was written" for traffic that never
    had one to lose. ``traffic_source`` is what separates the two: ``direct`` (and
    records predating the field) go to ``<none>`` informationally, while an
    ``edge`` record with no ``rollout_id`` is a LABELLING DEFECT — the trusted edge
    is what stamps it — so it gets ``<unlabelled-edge>`` and blocks. Without that
    split, this rollout's own spend could hide inside the historical bucket while
    the report printed a reassuring, and much smaller, per-rollout total.
    """
    groups: Dict[Tuple[str, str], List[dict]] = {}
    attribution_issues: Dict[Tuple[str, str], List[str]] = {}
    attribution_notes: Dict[Tuple[str, str], List[str]] = {}
    for record in records:
        obj = record if isinstance(record, dict) else {}
        arch, arch_issue = _dimension(
            obj.get("agent_arch"), name="agent_arch"
        )
        if obj.get("rollout_id") is None:
            rollout_note = None
            if obj.get("traffic_source") == EDGE_TRAFFIC_SOURCE:
                # The edge stamps this label. Missing it is a live misconfiguration,
                # not history, and its spend belongs to a rollout we cannot name.
                rollout_id = UNLABELLED_EDGE_ROLLOUT
                rollout_issue = (
                    "traffic_source='edge' with no rollout_id: the trusted edge "
                    "failed to label these records, so their spend cannot be "
                    "attributed to the rollout that incurred it"
                )
            else:
                # Absent, not malformed. `notes` never becomes a HOLD; `issues` does.
                rollout_id, rollout_issue = UNLABELLED_ROLLOUT, None
                # DESCRIPTIVE, not assertive: this says what is true of the records
                # (no id) rather than asserting a provenance nobody verified.
                rollout_note = (
                    "no rollout_id on these records (traffic_source is not 'edge'), "
                    f"costed together under {UNLABELLED_ROLLOUT!r}"
                )
        else:
            rollout_id, rollout_issue = _dimension(
                obj.get("rollout_id"), name="rollout_id"
            )
            rollout_note = None
        key = (arch, rollout_id)
        groups.setdefault(key, []).append(obj)
        bucket_issues = attribution_issues.setdefault(key, [])
        bucket_notes = attribution_notes.setdefault(key, [])
        if rollout_note and rollout_note not in bucket_notes:
            bucket_notes.append(rollout_note)
        for issue in (arch_issue, rollout_issue):
            if issue and issue not in bucket_issues:
                bucket_issues.append(issue)
        if (
            arch not in {MISSING_DIMENSION, INVALID_DIMENSION}
            and arch not in KNOWN_AGENT_ARCHES
        ):
            message = (
                f"unknown agent_arch {arch!r}; exact values are "
                f"{sorted(KNOWN_AGENT_ARCHES)}"
            )
            if message not in bucket_issues:
                bucket_issues.append(message)
    return groups, attribution_issues, attribution_notes


def build_cost_report(
    records: Sequence[dict],
    prices: dict,
    *,
    allow_unverified: bool = False,
    skipped: int = 0,
) -> dict:
    """Build the aggregate and exact-identity group report."""
    records_list = list(records)
    overall_usage = sum_usage(records_list)
    overall = compute_cost(overall_usage, prices, allow_unverified)
    groups, attribution, attribution_note_map = group_records(records_list)

    rendered_groups = []
    attribution_issues: List[str] = []
    attribution_notes: List[str] = []
    for (arch, rollout_id), group in sorted(groups.items()):
        usage = sum_usage(group)
        cost = compute_cost(usage, prices, allow_unverified)
        identity_issues = attribution.get((arch, rollout_id), [])
        identity_notes = attribution_note_map.get((arch, rollout_id), [])
        if identity_notes:
            # Reported on the group and in the top-level `notes`, and deliberately
            # NOT merged into `issues`: `issues` is what forces HOLD, and this
            # group's usage is fully measured.
            cost["notes"] = list(cost.get("notes") or []) + identity_notes
            attribution_notes.extend(
                f"group ({arch}, {rollout_id}): {note}" for note in identity_notes
            )
        if identity_issues:
            attribution_issues.extend(
                f"group ({arch}, {rollout_id}): {issue}"
                for issue in identity_issues
            )
            cost["ok"] = False
            cost["decision"] = "HOLD"
            cost["exit_code"] = EXIT_HOLD
            cost["total_cost"] = None
            cost["issues"] = list(cost.get("issues") or []) + identity_issues
            cost["error"] = "; ".join(cost["issues"][:10])
        rendered_groups.append(
            {
                "agent_arch": arch,
                "rollout_id": rollout_id,
                "records": len(group),
                "usage": usage,
                **cost,
            }
        )

    extra_issues: List[str] = []
    if not records_list:
        extra_issues.append("no canary.turn records were loaded")
    if skipped:
        extra_issues.append(
            f"{skipped} unparseable input line(s); "
            "a lost line may contain chargeable usage"
        )
    extra_issues.extend(attribution_issues)

    result = {
        **overall,
        "records": len(records_list),
        "skipped_lines": skipped,
        "usage": overall_usage,
        "groups": rendered_groups,
    }
    if attribution_notes:
        result["notes"] = list(result.get("notes") or []) + attribution_notes
    if extra_issues:
        issues = list(result.get("issues") or []) + extra_issues
        result.update(
            {
                "ok": False,
                "decision": "HOLD",
                "exit_code": EXIT_HOLD,
                "total_cost": None,
                "issues": issues[:_MAX_REPORTED_ISSUES],
                "error": "; ".join(issues[:10]),
            }
        )
    return result


def _input_error(message: str) -> dict:
    return {
        "ok": False,
        "decision": "HOLD",
        "exit_code": EXIT_HOLD,
        "total_cost": None,
        "issues": [message],
        "error": message,
        "records": 0,
        "groups": [],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--prices", required=True)
    ap.add_argument(
        "--allow-unverified",
        action="store_true",
        help=(
            "show a provisional figure when possible; "
            "never clears the HOLD gate"
        ),
    )
    args = ap.parse_args(argv)

    try:
        records, skipped = load_records(args.inputs)
        prices = load_prices(args.prices)
        result = build_cost_report(
            records,
            prices,
            allow_unverified=args.allow_unverified,
            skipped=skipped,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result = _input_error(
            f"input error: {type(exc).__name__}: {str(exc)[:240]}"
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    return int(result.get("exit_code", EXIT_HOLD))


if __name__ == "__main__":
    sys.exit(main())
