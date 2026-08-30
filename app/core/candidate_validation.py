"""Deterministic candidate and commute-evidence contracts.

The search tool is a candidate generator.  It is not allowed to turn an incomplete
candidate into a recommendation merely because a model-described summary sounds positive.
This module is the shared boundary used by both agent architectures and the direct search
path: each listing is classified as ``eligible``, ``excluded`` or ``unknown`` from
structured fields and explicitly linked tool evidence.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from core.tenancy_reference import monthly_from_weekly


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_COMMUTE_CONTEXT = re.compile(
    r"\b(?:commute|travel\s*time|journey\s*time|minutes?|mins?)\b|通勤|路程|分钟|多久",
    re.IGNORECASE,
)
_COMMUTE_CLAIM = re.compile(
    r"\b(?:meets?|within|under|over|about|around|takes?)\b[^.!?\n]{0,60}"
    r"\b\d+(?:\.\d+)?\s*(?:minutes?|mins?)\b|"
    r"\b\d+(?:\.\d+)?\s*(?:minutes?|mins?)\b|符合|满足|超过|分钟",
    re.IGNORECASE,
)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def candidate_key(candidate: dict) -> str:
    """Stable identity for evidence binding; a listing URL beats a display name."""
    url = _norm(candidate.get("url") or candidate.get("URL"))
    if url:
        return f"url:{url.rstrip('/')}"
    address = _norm(candidate.get("address") or candidate.get("Address"))
    price = _norm(candidate.get("price") or candidate.get("Price"))
    return f"address:{address}|price:{price}" if address else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value or "").replace("£", ""))
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def hard_constraints(criteria: dict | None) -> dict:
    """Normalize the hard constraints used by candidate validation.

    ``search_properties`` returns ``max_budget`` in monthly units after its own input
    normalization.  Newer payloads also carry ``max_budget_monthly``; the fallback below
    keeps old payloads safe for callers that still provide the original period.
    """
    c = criteria if isinstance(criteria, dict) else {}
    raw_budget = c.get("max_budget_monthly", c.get("max_budget"))
    budget = _number(raw_budget)
    period = _norm(c.get("budget_period"))
    if budget is not None and period in {"week", "weekly", "pw"} and "max_budget_monthly" not in c:
        budget = monthly_from_weekly(budget)
    commute = _number(c.get("max_commute_time", c.get("max_travel_time")))
    destination = c.get("commute_destination") or c.get("destination")
    areas = c.get("areas") or ([c.get("area")] if c.get("area") else [])
    if isinstance(areas, str):
        areas = [areas]
    features = c.get("property_features") or []
    if isinstance(features, str):
        features = [features]
    return {
        "max_budget_monthly": budget if budget and budget > 0 else None,
        "max_commute_minutes": commute if commute and commute > 0 else None,
        "commute_destination": str(destination).strip() if destination else None,
        "no_commute": bool(c.get("no_commute")),
        "bedrooms": _number(c.get("bedrooms")),
        "room_type": _norm(c.get("room_type")) or None,
        "areas": [value for value in (_norm(area) for area in areas) if value],
        "move_in_date": c.get("move_in_date") or None,
        "property_features": [value for value in (_norm(feature) for feature in features) if value],
    }


def commute_constraint_required(criteria: dict | None) -> bool:
    c = hard_constraints(criteria)
    return bool(c["max_commute_minutes"] and c["commute_destination"] and not c["no_commute"])


def _evidence_status(entry: dict | None) -> str:
    if not entry:
        return "missing"
    status = str(entry.get("evidence_status") or "").lower()
    if status:
        return status
    if entry.get("timed_out") or entry.get("timeout"):
        return "timeout"
    if entry.get("success") is False:
        return "failed"
    return "success" if entry.get("success") is True else "missing"


def _evidence_for(candidate: dict, evidence: Any) -> dict | None:
    key = candidate_key(candidate)
    entries = evidence.values() if isinstance(evidence, dict) else (evidence or [])
    if isinstance(evidence, dict) and evidence.get("candidate_key"):
        entries = [evidence]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("candidate_key") == key:
            return entry
        from_address = entry.get("from_address")
        candidate_address = candidate.get("address") or candidate.get("Address")
        if from_address and _norm(from_address) == _norm(candidate_address):
            return entry
    return None


def _candidate_bedrooms(candidate: dict) -> float | None:
    return _number(candidate.get("bedrooms", candidate.get("Bedrooms")))


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.lower() in {"available now", "now", "immediately"}:
        return "available_now"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None


def _structured_features(candidate: dict) -> set[str] | None:
    for key in ("verified_features", "features", "amenities"):
        value = candidate.get(key)
        if isinstance(value, (list, tuple, set)):
            return {_norm(item) for item in value if _norm(item)}
    return None


def validate_candidates(candidates: list[dict], criteria: dict | None,
                        commute_evidence: Any = None) -> dict:
    """Classify every candidate without reading model-generated prose.

    A failed/timeout/missing commute call is deliberately ``unknown`` rather than
    ``excluded``.  Only a positively evidenced failed hard constraint produces
    ``excluded``.  This distinction is important for a user-facing explanation and for
    preventing a timeout from being presented as proof that a listing fails.
    """
    constraints = hard_constraints(criteria)
    statuses = []
    for index, original in enumerate(candidates or []):
        if not isinstance(original, dict):
            continue
        candidate = dict(original)
        reasons: list[str] = []
        unknown_reasons: list[str] = []
        evidence_status = "not_required"

        if candidate.get("alternative") or candidate.get("match_type") in {
            "soft_violation", "similar_suggestion"
        }:
            reasons.append("alternative is outside the declared hard result set")

        price = _number(candidate.get("price", candidate.get("Price")))
        budget = constraints["max_budget_monthly"]
        if budget is not None:
            if price is None:
                unknown_reasons.append("price is not structured")
            elif price > budget + 1e-9:
                reasons.append(f"monthly price {price:g} exceeds budget {budget:g}")

        bedrooms = constraints["bedrooms"]
        if bedrooms is not None:
            actual_bedrooms = _candidate_bedrooms(candidate)
            if actual_bedrooms is None:
                unknown_reasons.append("bedroom count is not verified")
            elif actual_bedrooms != bedrooms:
                reasons.append(
                    f"bedroom count {actual_bedrooms:g} does not equal requested {bedrooms:g}")

        if constraints["room_type"]:
            actual_type = _norm(candidate.get("room_type") or candidate.get("property_type")
                                or candidate.get("Type"))
            if not actual_type:
                unknown_reasons.append("room type is not verified")
            elif constraints["room_type"] not in actual_type:
                reasons.append(f"room type does not match {constraints['room_type']}")

        requested_areas = constraints["areas"]
        if requested_areas:
            actual_area = _norm(candidate.get("area") or candidate.get("Area"))
            if not actual_area:
                unknown_reasons.append("area is not verified")
            elif actual_area not in requested_areas:
                reasons.append(f"area {actual_area} is outside the requested areas")

        requested_features = constraints["property_features"]
        if requested_features:
            available_features = _structured_features(candidate)
            if available_features is None:
                unknown_reasons.append("property features are not structurally verified")
            else:
                missing_features = [feature for feature in requested_features
                                    if feature not in available_features]
                # search_properties distinguishes an explicit structured denial
                # from a provider that simply did not disclose an accessibility
                # fact. Preserve the legacy meaning of verified_features=[] for
                # all other callers; only an explicit unverified_features entry
                # changes a missing hard feature from excluded to unknown.
                unverified = {
                    _norm(feature) for feature in (candidate.get("unverified_features") or [])
                    if _norm(feature)
                }
                unknown_features = [feature for feature in missing_features
                                    if feature in unverified]
                absent_features = [feature for feature in missing_features
                                   if feature not in unverified]
                if absent_features:
                    reasons.append("missing required features: " + ", ".join(absent_features))
                if unknown_features:
                    unknown_reasons.append(
                        "required features are not verified: " + ", ".join(unknown_features))

        if constraints["move_in_date"]:
            available = _iso_date(candidate.get("available_from"))
            requested_date = _iso_date(constraints["move_in_date"])
            if available is None or requested_date is None:
                unknown_reasons.append("availability is not verified")
            elif available != "available_now" and available > requested_date:
                reasons.append(f"available from {available}, after requested date")

        if commute_constraint_required(criteria):
            entry = _evidence_for(candidate, commute_evidence)
            evidence_status = _evidence_status(entry)
            if entry is None:
                unknown_reasons.append("commute evidence is missing")
            elif evidence_status in {
                "failed", "timeout", "outcome_unknown", "budget_exhausted", "skipped",
            }:
                unknown_reasons.append(f"commute tool {evidence_status}")
            else:
                raw = entry.get("raw_data") if isinstance(entry.get("raw_data"), dict) else entry
                # A result that cannot be tied back to the requested listing is not evidence
                # for that listing, even if it contains a plausible duration.
                returned_from = raw.get("from_address") if isinstance(raw, dict) else None
                if returned_from and _norm(returned_from) != _norm(
                        candidate.get("address") or candidate.get("Address")):
                    evidence_status = "unlinked"
                    unknown_reasons.append("commute result cannot be linked to this listing")
                duration = _number(entry.get("duration_minutes"))
                if duration is None and isinstance(raw, dict):
                    duration = _number(raw.get("duration_minutes"))
                if duration is None:
                    # Estimated ranges are useful context but do not license a binary
                    # meets-the-limit claim.
                    evidence_status = "insufficient"
                    unknown_reasons.append("commute result has no measured duration")
                elif duration > constraints["max_commute_minutes"]:
                    reasons.append(
                        f"measured commute {duration:g} min exceeds limit "
                        f"{constraints['max_commute_minutes']:g} min")
                else:
                    candidate["verified_commute_minutes"] = duration

        status = "excluded" if reasons else "unknown" if unknown_reasons else "eligible"
        candidate["candidate_status"] = status
        candidate["candidate_reasons"] = list(reasons)
        candidate["candidate_unknown_reasons"] = list(unknown_reasons)
        statuses.append({
            "candidate": candidate,
            "candidate_key": candidate_key(candidate),
            "index": index,
            "status": status,
            "reasons": reasons,
            "unknown_reasons": unknown_reasons,
            "evidence_status": evidence_status,
        })

    return {
        "constraints": constraints,
        "statuses": statuses,
        "eligible": [s for s in statuses if s["status"] == "eligible"],
        "excluded": [s for s in statuses if s["status"] == "excluded"],
        "unknown": [s for s in statuses if s["status"] == "unknown"],
    }


def _address(status: dict) -> str:
    c = status.get("candidate") or {}
    return str(c.get("address") or c.get("Address") or "Unnamed listing")


def _price_text(status: dict) -> str:
    c = status.get("candidate") or {}
    return str(c.get("price") or c.get("Price") or "price unavailable")


def _unknown_reason_text(status: dict, *, zh: bool = False) -> str:
    reasons = status.get("unknown_reasons") or ["insufficient evidence"]
    if any("commute" in str(reason).lower() for reason in reasons):
        return "通勤条件无法核实" if zh else "cannot verify the commute condition"
    return "；".join(reasons) if zh else "; ".join(reasons)

def render_candidate_status(validation: dict, *, language: str = "en") -> str:
    """Render a safe candidate summary; only ``eligible`` enters the first section."""
    zh = (language or "").lower().startswith("zh")
    lines: list[str] = []
    eligible = validation.get("eligible") or []
    excluded = validation.get("excluded") or []
    unknown = validation.get("unknown") or []
    if zh:
        if eligible:
            lines.append("满足全部已声明条件（已核实）：")
        lines.extend(f"- {_address(s)}（{_price_text(s)}" +
                     (f"，通勤已核实 {s['candidate']['verified_commute_minutes']:g} 分钟" if
                      s.get("candidate", {}).get("verified_commute_minutes") is not None else "") + ")"
                     for s in eligible)
        if not eligible:
            lines.append("- 没有同时满足且已核实的房源。")
        if excluded:
            lines.append("已排除/不符合：")
            lines.extend(f"- {_address(s)}：{'；'.join(s.get('reasons') or ['不满足硬约束'])}" for s in excluded)
        if unknown:
            lines.append("尚未核实：")
            lines.extend(f"- {_address(s)}：{_unknown_reason_text(s, zh=True)}" for s in unknown)
        return "\n".join(lines)

    if eligible:
        lines.append("Meets all declared conditions (verified):")
    lines.extend(f"- {_address(s)} ({_price_text(s)}" +
                 (f", commute verified at {s['candidate']['verified_commute_minutes']:g} min" if
                  s.get("candidate", {}).get("verified_commute_minutes") is not None else "") + ")"
                 for s in eligible)
    if not eligible:
        lines.append("- No listing is both fully eligible and verified.")
    if excluded:
        lines.append("Excluded / does not meet:")
        lines.extend(f"- {_address(s)}: {('; '.join(s.get('reasons') or ['hard constraint failed']))}"
                     for s in excluded)
    if unknown:
        lines.append("Not verified:")
        lines.extend(f"- {_address(s)}: {_unknown_reason_text(s)}" for s in unknown)
    return "\n".join(lines)


def render_similar_listings(rows: list[dict], *, language: str = "en") -> str:
    """Render near-miss listings without implying they satisfied every condition.

    ``render_candidate_status`` describes a listing that FAILED a stated hard constraint.
    A ``no_exact_match_but_similar`` row failed nothing — the exact-match pool was empty and
    these are the closest recalls — so it needs its own honest heading, and it must keep the
    price and the provenance-labelled commute string the card also shows.
    """
    zh = (language or "").lower().startswith("zh")
    lines = ["以下房源与您的条件相近，但并未逐条满足，请自行核对："
             if zh else
             "These listings are close to your criteria but do not meet every condition:"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        parts = [str(row.get("address") or row.get("Address")
                     or ("未命名房源" if zh else "Unnamed listing"))]
        for key in ("price", "budget_status", "travel_time"):
            value = row.get(key) or row.get(key.capitalize())
            if value:
                parts.append(str(value))
        lines.append("- " + ("，".join(parts) if zh else ", ".join(parts)))
    return "\n".join(lines)


def _bounded_positive_int(env_name: str, default: int, *, ceiling: int) -> int:
    try:
        value = int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, ceiling))


async def collect_commute_evidence(
        provider, candidates: list[dict], destination: str, *, mode: str = "transit",
        timeout_s: float = 20.0, deadline_monotonic: float | None = None,
        max_candidates: int | None = None, concurrency: int | None = None) -> list[dict]:
    """Call ``calculate_commute`` once per listing and preserve failure classes.

    This helper intentionally does not retry or collapse calls. The tool's own retry policy
    handles retry-safe reads; the per-listing evidence ledger must still show which listing
    succeeded, failed, timed out, or was not dispatched because the shared deadline/fan-out
    cap was exhausted. Every provider call shares one absolute deadline and a bounded
    semaphore so a large scraper result cannot consume the process-wide default executor.
    """
    destination = str(destination or "").strip()
    rows = [candidate for candidate in (candidates or []) if isinstance(candidate, dict)]
    if max_candidates is None:
        max_candidates = _bounded_positive_int(
            "FC_COMMUTE_VALIDATION_MAX_CANDIDATES", 10, ceiling=50)
    else:
        max_candidates = max(1, min(int(max_candidates), 50))
    if concurrency is None:
        concurrency = _bounded_positive_int(
            "FC_COMMUTE_VALIDATION_CONCURRENCY", 4, ceiling=16)
    else:
        concurrency = max(1, min(int(concurrency), 16))
    semaphore = asyncio.Semaphore(concurrency)
    dispatch_stopped = asyncio.Event()

    def _base(candidate: dict) -> dict:
        address = str(candidate.get("address") or candidate.get("Address") or "").strip()
        return {
            "candidate_key": candidate_key(candidate),
            "from_address": address,
            "to_address": destination,
            "mode": mode,
        }

    async def _one(candidate: dict) -> dict:
        base = _base(candidate)
        address = base["from_address"]
        if not address or not destination:
            return {**base, "success": False, "evidence_status": "missing",
                    "error": "from_address or to_address is missing", "elapsed_ms": 0}
        queued_at = time.monotonic()
        async with semaphore:
            started_at = time.monotonic()
            if dispatch_stopped.is_set():
                return {
                    **base, "success": False, "evidence_status": "skipped",
                    "error": "commute dispatch stopped after an abandoned timed-out call",
                    "elapsed_ms": int((started_at - queued_at) * 1000),
                }
            remaining = float(timeout_s)
            if deadline_monotonic is not None:
                remaining = min(remaining, deadline_monotonic - started_at)
            if remaining <= 0:
                return {
                    **base, "success": False, "evidence_status": "budget_exhausted",
                    "error": "commute validation deadline exhausted before dispatch",
                    "elapsed_ms": int((started_at - queued_at) * 1000),
                }
            try:
                result = await asyncio.wait_for(provider.execute_tool(
                    "calculate_commute", from_address=address, to_address=destination,
                    mode=mode), timeout=remaining)
            except asyncio.TimeoutError:
                dispatch_stopped.set()
                return {
                    **base, "success": False, "evidence_status": "timeout",
                    "timed_out": True, "error": "calculate_commute timed out",
                    "elapsed_ms": int((time.monotonic() - queued_at) * 1000),
                }
            except Exception as exc:
                return {
                    **base, "success": False, "evidence_status": "failed",
                    "error": f"calculate_commute failed ({type(exc).__name__})",
                    "elapsed_ms": int((time.monotonic() - queued_at) * 1000),
                }

        success = bool(getattr(result, "success", False))
        data = getattr(result, "data", None)
        error = getattr(result, "error", None)
        if isinstance(result, dict):
            success = result.get("success") is True
            data = result.get("data", result)
            error = result.get("error")
        raw = data if isinstance(data, dict) else {}
        duration = raw.get("duration_minutes")
        elapsed_ms = int((time.monotonic() - queued_at) * 1000)
        if not success:
            return {**base, "success": False, "evidence_status": "failed",
                    "raw_data": raw or None, "error": error or "calculate_commute failed",
                    "elapsed_ms": elapsed_ms}
        return {**base, "success": True, "evidence_status": "success",
                "duration_minutes": duration, "raw_data": raw, "elapsed_ms": elapsed_ms}

    admitted = rows[:max_candidates]
    evidence = list(await asyncio.gather(*[_one(candidate) for candidate in admitted]))
    for candidate in rows[max_candidates:]:
        evidence.append({
            **_base(candidate), "success": False, "evidence_status": "skipped",
            "error": f"commute validation fan-out capped at {max_candidates} candidates",
            "elapsed_ms": 0,
        })
    return evidence


def validate_search_payload(payload: dict, *, commute_evidence: list[dict] | None = None) -> dict:
    """Attach candidate state to a search payload without trusting its summary text."""
    if not isinstance(payload, dict):
        return payload
    main = [dict(r) for r in (payload.get("recommendations") or []) if isinstance(r, dict)]
    alternatives = [dict(r) for r in (payload.get("over_budget_alternatives") or [])
                    if isinstance(r, dict)]
    for row in alternatives:
        row["alternative"] = True
    rows = main + alternatives
    criteria = payload.get("search_criteria") or payload.get("known_criteria") or {}
    scoped_areas = hard_constraints(criteria).get("areas") or []
    if len(scoped_areas) == 1:
        # Older search payloads carried the authoritative single-area scope once at the
        # payload level instead of duplicating it into every row. Preserve that structured
        # evidence during migration; current rows carry their own area and are never changed.
        for row in rows:
            if not row.get("area") and not row.get("Area"):
                row["area"] = scoped_areas[0]
                row["area_evidence"] = "search_scope"
    validation = validate_candidates(
        rows, criteria, commute_evidence=commute_evidence or [],
    )
    out = dict(payload)
    out["candidate_validation"] = validation
    out["candidate_states"] = validation["statuses"]
    out["commute_evidence"] = list(commute_evidence or [])
    eligible_keys = {s["candidate_key"] for s in validation["eligible"]}
    # Keep the established listing-card schema stable. Internal status and evidence
    # details live in candidate_states; they must not leak into eligible card rows.
    _internal = {"candidate_status", "candidate_reasons", "candidate_unknown_reasons",
                 "verified_commute_minutes", "area_evidence"}

    def _public_candidate(status: dict) -> dict:
        candidate = status["candidate"]
        hidden = set(_internal)
        if candidate.get("area_evidence") == "search_scope":
            hidden.add("area")
        return {key: value for key, value in candidate.items() if key not in hidden}

    out["recommendations"] = [
        _public_candidate(status) for status in validation["eligible"]
    ]
    out["unverified_candidates"] = [s["candidate"] for s in validation["unknown"]]
    out["excluded_candidates"] = [s["candidate"] for s in validation["excluded"]]
    return out


def _minute_claim_values(text: str) -> list[float]:
    values = []
    for match in re.finditer(
        r"(\d+(?:\.\d+)?)[\s\-–]*(?:minutes?|mins?|分钟)", str(text or ""), re.IGNORECASE
    ):
        try:
            values.append(float(match.group(1)))
        except ValueError:
            continue
    return values


def _known_commute_caps(state: dict) -> set[float]:
    caps: set[float] = set()
    accumulated = state.get("accumulated_search_criteria") or {}
    validation = state.get("candidate_validation") or {}
    for value in (
        accumulated.get("max_travel_time"),
        accumulated.get("max_commute_time"),
        (validation.get("constraints") or {}).get("max_commute_minutes"),
    ):
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            caps.add(number)
    return caps


def _label_in_text(label: object, normalized_text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(label or "").lower()).strip()
    return bool(normalized and f" {normalized} " in f" {normalized_text} ")


def _area_ranking_commute_supported(text: str, state: dict) -> bool:
    """Accept commute claims grounded in ``compare_or_rank_areas`` rows.

    Area ranking intentionally owns its commute routing (the loop prompt forbids a
    redundant per-area ``calculate_commute`` fan-out). The final prose guard must
    therefore recognise that structured evidence while still rejecting invented
    durations or a claim about an area whose route was unavailable.
    """
    all_rows: list[dict] = []
    for artifact in state.get("tool_artifacts") or []:
        if artifact.get("tool") != "compare_or_rank_areas" or artifact.get("success") is not True:
            continue
        if any(artifact.get(flag) for flag in (
            "timed_out", "denied", "abandoned", "outcome_unknown"
        )):
            continue
        raw = artifact.get("raw_data")
        if isinstance(raw, dict):
            all_rows.extend(row for row in (raw.get("areas") or []) if isinstance(row, dict))
    if not all_rows:
        return False

    normalized_text = re.sub(
        r"[^a-z0-9\u4e00-\u9fff]+", " ", str(text or "").lower()
    ).strip()
    mentioned = [
        row for row in all_rows
        if _label_in_text(row.get("name"), normalized_text)
        or _label_in_text(str(row.get("slug") or "").replace("-", " "), normalized_text)
    ]
    if not mentioned:
        return False

    grounded: list[float] = []
    for row in mentioned:
        value = row.get("commute_minutes")
        sources = {str(source).strip().lower() for source in (row.get("sources") or [])}
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if "commute routing" not in sources and "通勤路由估算" not in sources:
            return False
        grounded.append(float(value))

    claimed = _minute_claim_values(text)
    if not claimed:
        return False
    caps = _known_commute_caps(state)
    allowed = set(grounded) | caps
    if any(not any(abs(value - valid) < 1e-9 for valid in allowed) for value in claimed):
        return False

    # At least one routed duration must be quoted, unless the prose only claims
    # the named areas fit the user's known cap and every routed row proves that.
    if any(any(abs(value - duration) < 1e-9 for duration in grounded) for value in claimed):
        return True
    return bool(caps and all(any(duration <= cap for cap in caps) for duration in grounded))


# ── Commute redaction ────────────────────────────────────────────────────────
# What must be removed is an ASSERTED COMMUTE OUTCOME. That is deliberately narrower than
# the entry gate `_COMMUTE_CLAIM`, which also fires on a bare 符合/满足/超过 anywhere in a
# Chinese reply — far more often a statement about budget or area than about the commute.
# Redacting those too would delete exactly the prices and areas this redaction exists to
# preserve.
# The separator is optional and may be a hyphen: "45 minutes", "45min" and the attributive
# "your 45-minute limit" are the same figure. Missing the hyphenated form left a cap quoted
# inside a verdict invisible to the clause check, so the whole-line guard had to fail closed
# on answers that were otherwise fine.
_MINUTE_TOKEN = re.compile(
    r"\d+(?:\.\d+)?[\s\-–]*(?:minutes?|mins?\b|分钟|分鐘)", re.IGNORECASE)
_COMMUTE_WORD = re.compile(
    r"commute|travel\s*time|journey|通勤|路程|车程|車程", re.IGNORECASE)
# A commute VERDICT needs no number to be a claim: "this one exceeds your commute limit" is
# exactly as unevidenced as "this one is 58 minutes", and a redactor that removes only the
# figure leaves the conclusion standing under a note saying unverified figures were removed.
# Both directions count — passing and failing a limit are equally unsupported without evidence.
_FIT_WORD = re.compile(
    r"符合|满足|不满足|未满足|超过|超出|超标|未超过|以内|之内|范围内|达标|未达|"
    r"更远|更近|太远|够近|不远|"
    r"\b(?:meets?|met|within|under|over|about|around|takes?|took|"
    r"exceeds?|exceeded|exceeding|beyond|outside|above|below|longer|shorter|"
    r"further|farther|closer|nearer|faster|slower|fits?|satisf(?:y|ies|ied)|"
    r"qualifies|violates?|breaches?|misses|fails?)\b",
    re.IGNORECASE)
# A comma between digits is a thousands separator, never a clause boundary — splitting there
# would cut "£1,733" in half.
_FRAGMENT_SPLIT = re.compile(
    r"((?<!\d),(?!\d)|(?<!\d):(?!\d)|[，；;、。！？：]|\.(?=\s|$)|[!?](?=\s|$)"
    r"|\s+[–—-]\s+)")
_PARENTHETICAL = re.compile(r"\s*[（(][^()（）]*[)）]")
_BULLET_PREFIX = re.compile(r"^(\s*(?:[-*•]|\d+\s*[.)、]|[（(]\d+[)）])\s*)")
_HAS_CONTENT = re.compile(r"[0-9A-Za-z一-鿿]")
# A trailing clause that leans on the one before it: "…24 minutes, which is well within your
# limit". It names no subject of its own, so on its own it reads as a claim about nothing —
# but once the clause it modifies is redacted, leaving it behind restates the same verdict.
_CONTINUATION = re.compile(
    r"^\s*(?:(?:which|that|and|so|but|it|this|these|those|both|all)\b"
    r"|且|而且|并且|所以|因此|但是|但|它|这|那|同样|也|均|都)",
    re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?。！？]")
# The DIRECTION a verdict asserts. Evidence of 17 minutes against a 45-minute cap supports
# "within", and refutes "exceeds" just as firmly — a measured duration is not a licence for
# whatever conclusion the prose drew from it. Ambiguous comparatives are deliberately in
# neither set: they fall through to the plain figure check instead of guessing a direction.
_PASS_VERDICT = re.compile(
    r"\b(?:within|under|below|inside|meets?|met|fits?|satisf(?:y|ies|ied)|qualifies|"
    r"shorter|closer|nearer|faster)\b|以内|之内|范围内|符合|满足|达标|够近|不远",
    re.IGNORECASE)
_FAIL_VERDICT = re.compile(
    r"\b(?:exceeds?|exceeded|exceeding|over|beyond|outside|above|longer|further|farther|"
    r"slower|violates?|breaches?|misses|fails?)\b|超过|超出|超标|不满足|未满足|未达|更远|太远",
    re.IGNORECASE)


def _asserts_commute(fragment: str) -> bool:
    """True when this fragment states a commute duration or verdict."""
    text = str(fragment or "")
    if _MINUTE_TOKEN.search(text):
        return True
    return bool(_COMMUTE_WORD.search(text) and _FIT_WORD.search(text))


def _listing_bindings(validation: dict) -> list[dict]:
    """``[{labels, minutes}]`` for EVERY listing in the ledger; ``minutes`` is None unless
    its commute is positively evidenced.

    ``validate_candidates`` writes ``verified_commute_minutes`` only for a candidate that is
    ``eligible`` with a linked ``success`` evidence entry, so a non-None ``minutes`` is the
    exact figure the prose may quote for that listing.

    Unverified listings are here ON PURPOSE. Their labels have to compete for a line, because
    listing labels nest: with only the verified ones in the running, a line about "Park Drive
    London E14" (unverified) matches the label "Park Drive" (verified, 9 min) and the wrong
    listing's evidence licenses the figure. Present but unmeasured, it wins the line on length
    and licenses nothing.
    """
    bindings: list[dict] = []
    for status in validation.get("statuses") or []:
        if not isinstance(status, dict):
            continue
        candidate = status.get("candidate") or {}
        labels = [str(label).strip() for label in
                  (candidate.get("address"), candidate.get("Address"), candidate.get("name"))
                  if label and str(label).strip()]
        if not labels:
            continue
        minutes = candidate.get("verified_commute_minutes")
        evidenced = (status.get("status") == "eligible"
                     and status.get("evidence_status") == "success"
                     and minutes is not None)
        bindings.append({"labels": labels,
                         "minutes": float(minutes) if evidenced else None})
    return bindings


def _line_binding(line: str, bindings: list[dict], default: dict | None = None) -> dict | None:
    """The verified listing this line is about, if it names exactly one.

    Longest match wins, because listing labels nest: "Park Drive" is a substring of "Park
    Drive London E14", so first-match binding attributed the long address's line to the short
    address's evidence and published one listing's minutes for another. When two DIFFERENT
    listings match equally well the line cannot be attributed at all, and an unattributable
    line has no evidence — it fails closed rather than borrowing someone else's.
    """
    lowered = line.lower()
    best = None
    best_len = 0
    ambiguous = False
    for binding in bindings:
        matched = max((len(label) for label in binding["labels"]
                       if label and label.lower() in lowered), default=0)
        if not matched:
            continue
        if matched > best_len:
            best, best_len, ambiguous = binding, matched, False
        elif matched == best_len and binding is not best:
            ambiguous = True
    if ambiguous:
        return None
    return best if best is not None else default


def _claim_is_evidenced(fragment: str, binding: dict | None, caps: set) -> bool:
    """True when this fragment's commute claim — figure AND conclusion — follows the evidence.

    Three separate things have to hold, and each one was a way through on its own:

    * every minute it quotes is the listing's measured duration or a cap the user stated;
    * a verdict ("within" / "exceeds") points the way the measurement actually points. A
      fragment with no figure used to pass vacuously here — ``all([])`` is True — so
      "this exceeds your 45-minute limit" rode out on evidence of 17 minutes;
    * a verdict has some cap to be checked against at all.
    """
    if binding is None or binding.get("minutes") is None:
        return False
    allowed = {binding["minutes"]} | caps
    values = _minute_claim_values(fragment)
    if any(not any(abs(value - permitted) < 1e-9 for permitted in allowed)
           for value in values):
        return False

    asserts_pass = bool(_PASS_VERDICT.search(fragment))
    asserts_fail = bool(_FAIL_VERDICT.search(fragment))
    if asserts_pass and asserts_fail:
        return False  # both directions in one fragment: nothing coherent to check
    if asserts_pass or asserts_fail:
        quoted_caps = [value for value in values
                       if any(abs(value - cap) < 1e-9 for cap in caps)]
        against = quoted_caps or sorted(caps)
        if not against:
            return False  # a verdict with no limit to measure it against
        meets = all(binding["minutes"] <= cap + 1e-9 for cap in against)
        return meets if asserts_pass else not meets
    # No verdict: the fragment merely carries a figure, and that figure checked out above.
    return bool(values)


def _redact_commute_claims(text: str, *, zh: bool, bindings: list[dict] | None = None,
                           caps: set | None = None,
                           default_binding: dict | None = None) -> str | None:
    """Drop the unverifiable commute assertions and keep everything else.

    Price, area, availability and an honest caveat are all still true and still useful when
    the commute could not be verified; replacing the whole answer with one fixed sentence
    throws them away. Redaction works clause-by-clause so a listing line keeps its address
    and price while losing only the minutes.

    Redaction is PER LISTING, not per turn. When one recommendation's commute is evidenced and
    another's is not, only the unevidenced one loses its figure — deleting every commute
    number in the answer because one row failed is the same all-or-nothing error one level
    down. A line is treated as evidenced only when it names a listing in ``bindings`` AND every
    minute it quotes is either that listing's measured duration or a cap the user themselves
    stated (``caps``); an unattributed line is never evidenced.

    Returns ``None`` when nothing usable survives, or when an unevidenced assertion is still
    present after redaction — the caller then falls back to the fixed sentence, so the guard
    stays fail-closed.
    """
    bindings = list(bindings or [])
    caps = set(caps or ())
    lines: list[str] = []
    for line in str(text or "").split("\n"):
        bullet = _BULLET_PREFIX.match(line)
        prefix = bullet.group(1) if bullet else ""
        body = line[len(prefix):]
        binding = _line_binding(line, bindings, default_binding)

        def _evidenced(fragment: str, _binding=binding) -> bool:
            """True when this fragment's commute claim is backed for THIS line's listing."""
            return _claim_is_evidenced(fragment, _binding, caps)

        # A parenthetical commute aside ("(17 min to Canary Wharf)") is an attachment to a
        # clause, not a clause of its own; remove it before clause splitting.
        body = _PARENTHETICAL.sub(
            lambda m: "" if (_asserts_commute(m.group(0)) and not _evidenced(m.group(0)))
            else m.group(0), body)

        parts = _FRAGMENT_SPLIT.split(body)
        kept: list[str] = []
        dropped_in_sentence = False
        for index in range(0, len(parts), 2):
            fragment = parts[index]
            separator = parts[index + 1] if index + 1 < len(parts) else ""
            drop = fragment.strip() and _asserts_commute(fragment) and not _evidenced(fragment)
            # A dependent clause after a redacted one restates its verdict without repeating
            # its subject ("…, which is well within your limit"), so it goes with it.
            if (not drop and dropped_in_sentence and fragment.strip()
                    and _CONTINUATION.match(fragment) and _FIT_WORD.search(fragment)
                    and not _evidenced(fragment)):
                drop = True
            if drop:
                dropped_in_sentence = True
                continue  # its trailing separator goes with it
            if _SENTENCE_END.search(separator):
                dropped_in_sentence = False
            kept.append(fragment + separator)

        rebuilt = "".join(kept)
        # Tidy up after removal: no doubled or dangling separators.
        rebuilt = re.sub(r"([,，；;、:：])\s*(?=[,，；;、:：])", "", rebuilt)
        rebuilt = re.sub(r"^[\s,，；;、]+", "", rebuilt)
        rebuilt = re.sub(r"[\s,，；;、:：]+$", "", rebuilt)
        if rebuilt and _HAS_CONTENT.search(body) and not _HAS_CONTENT.search(rebuilt):
            rebuilt = ""
        if rebuilt and re.search(r"[.!?。！？]\s*$", body) and not re.search(
                r"[.!?。！？]\s*$", rebuilt):
            rebuilt += "。" if zh else "."

        if not rebuilt.strip():
            # The line was nothing but a commute claim; drop it, prefix included. A blank
            # separator line in the original stays blank so paragraphs do not fuse.
            if not _HAS_CONTENT.search(line):
                lines.append(line)
            continue
        # Fail closed per line: clause splitting is a heuristic, so verify the OUTCOME rather
        # than trusting the split. Anything still asserting a commute here must be covered by
        # this line's own evidence, or the whole redaction is abandoned.
        if _asserts_commute(rebuilt) and not _evidenced(rebuilt):
            return None
        lines.append(prefix + rebuilt)

    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if not _HAS_CONTENT.search(result):
        return None
    return result


def _commute_unverified_note(zh: bool) -> str:
    """Says SOME, because redaction is per listing — an evidenced figure is still standing."""
    return ("注：本轮未能核实其中部分房源的通勤条件，未经核实的通勤数据已从回答中移除。"
            if zh else
            "Note: the commute could not be verified for some of these listings, so the "
            "unverified commute figures have been removed from this answer.")


def validate_commute_response(response: str, state: dict) -> str:
    """Fail closed when prose asserts a commute result without linked evidence.

    Search responses normally use :func:`render_candidate_status`; this is the final
    defence for plain-text paths. A single successful commute artifact may support a
    direct one-origin commute answer. Multi-listing recommendations require the
    per-candidate validation ledger, so one successful call cannot license every row.

    Failing closed means the unverified COMMUTE CLAIM does not ship — not that the whole
    answer does not ship, and not that every commute claim in it goes. Redaction is per
    listing and per clause (see :func:`_redact_commute_claims`): prices, areas and honest
    caveats survive, and so does a figure for a listing whose commute IS evidenced. The fixed
    sentence remains for a reply that is nothing but an unverifiable commute claim, or one
    where an unevidenced assertion survives redaction.
    """
    text = str(response or "")
    current = ((state.get("extracted_context") or {}).get("current_message")
               or state.get("user_query") or "")
    # A commute VERDICT is a claim even with no figure in it: "this one exceeds your commute
    # limit" carries no minutes, so `_COMMUTE_CLAIM` alone never saw it and the guard did not
    # run at all on the one shape that states a conclusion without any evidence to quote.
    if not ((_COMMUTE_CLAIM.search(text) or _asserts_commute(text))
            and _COMMUTE_CONTEXT.search(current + " " + text)):
        return text

    language = str((state.get("extracted_context") or {}).get("reply_language") or "")
    zh = language.lower().startswith("zh")
    fixed = ("本轮无法核实该房源的通勤条件。" if zh
             else "The commute condition could not be verified for the listing this round.")

    validation = state.get("candidate_validation") or {}

    def redact(bindings, default_binding=None) -> str:
        """Evidenced listings keep their figures; only the unevidenced ones lose theirs."""
        redacted = _redact_commute_claims(
            text, zh=zh, bindings=bindings, caps=_known_commute_caps(state),
            default_binding=default_binding)
        if redacted is None:
            return fixed
        if redacted.strip() == text.strip():
            return text  # nothing needed removing; do not bolt a note onto a clean answer
        return f"{redacted}\n\n{_commute_unverified_note(zh)}"

    def fallback() -> str:
        return redact(_listing_bindings(validation))

    statuses = list(validation.get("statuses") or [])
    if statuses:
        lowered = text.lower()
        named = []
        for status in statuses:
            candidate = status.get("candidate") or {}
            labels = [candidate.get("address"), candidate.get("Address"), candidate.get("name")]
            if any(label and str(label).lower() in lowered for label in labels):
                named.append(status)
        targets = named or statuses
        commute_required = bool((validation.get("constraints") or {}).get("max_commute_minutes"))
        if commute_required and all(
                status.get("status") == "eligible"
                and status.get("evidence_status") == "success" for status in targets):
            # Every listing named is evidenced — but evidence for a listing is not a licence
            # to quote ANY duration for it, nor to draw the opposite conclusion from it. Run
            # the same per-listing check so a figure or verdict that disagrees with what was
            # measured is still removed; when the prose agrees with the ledger, `redact`
            # returns it untouched and no note is added.
            #
            # A target whose ledger entry carries no duration is NOT a pass: `success` with no
            # `verified_commute_minutes` leaves nothing to check the prose against, so any
            # figure quoted for it is unverifiable and goes. Returning the text here was a
            # fail-OPEN branch — the whole point of this function is that an unbacked figure
            # does not ship, and "we could not check" is not "we checked".
            return redact(_listing_bindings(validation))
        return fallback()

    if _area_ranking_commute_supported(text, state):
        return text

    successful = [
        artifact for artifact in (state.get("tool_artifacts") or [])
        if artifact.get("tool") == "calculate_commute"
        and artifact.get("success") is True
        and not any(artifact.get(flag) for flag in (
            "timed_out", "denied", "abandoned", "outcome_unknown"))
        and isinstance(artifact.get("raw_data"), dict)
        and isinstance(artifact["raw_data"].get("duration_minutes"), (int, float))
    ]
    if len(successful) != 1:
        return fallback()
    # ONE successful call licenses ONE duration — the one it returned. Counting artifacts and
    # stopping there let a turn that measured 17 minutes answer "it takes 9 minutes": the call
    # succeeded, so the count was satisfied and nothing ever compared the prose to the result.
    # Bind the figure the artifact actually produced; the origin is not named in a
    # single-origin answer, so it applies to every line rather than to a matched address.
    measured = float(successful[0]["raw_data"]["duration_minutes"])
    return redact([], default_binding={"labels": [], "minutes": measured})


async def validate_search_payload_with_provider(
        provider, payload: dict, *, timeout_s: float = 20.0,
        deadline_monotonic: float | None = None, max_candidates: int | None = None,
        concurrency: int | None = None) -> tuple[dict, list[dict]]:
    """Validate a search result and obtain per-listing commute evidence when required."""
    if not isinstance(payload, dict):
        return payload, []
    existing = payload.get("commute_evidence")
    if payload.get("candidate_validation") is not None and isinstance(existing, list):
        return payload, existing

    criteria = payload.get("search_criteria") or payload.get("known_criteria") or {}
    rows = [r for r in (payload.get("recommendations") or []) if isinstance(r, dict)]
    rows += [r for r in (payload.get("over_budget_alternatives") or []) if isinstance(r, dict)]
    evidence: list[dict] = []
    if commute_constraint_required(criteria):
        destination = hard_constraints(criteria).get("commute_destination")
        names = set()
        try:
            names = {spec.name for spec in provider.list_specs()}
        except Exception:
            names = set()
        if not names or "calculate_commute" in names:
            evidence = await collect_commute_evidence(
                provider, rows, destination, timeout_s=timeout_s,
                deadline_monotonic=deadline_monotonic, max_candidates=max_candidates,
                concurrency=concurrency)
    return validate_search_payload(payload, commute_evidence=evidence), evidence
