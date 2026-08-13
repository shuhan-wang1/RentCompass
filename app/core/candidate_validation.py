"""Deterministic candidate and commute-evidence contracts.

The search tool is a candidate generator.  It is not allowed to turn an incomplete
candidate into a recommendation merely because a model-described summary sounds positive.
This module is the shared boundary used by both agent architectures and the direct search
path: each listing is classified as ``eligible``, ``excluded`` or ``unknown`` from
structured fields and explicitly linked tool evidence.
"""

from __future__ import annotations

import asyncio
import re
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
                if missing_features:
                    reasons.append("missing required features: " + ", ".join(missing_features))

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
            elif evidence_status in {"failed", "timeout", "outcome_unknown"}:
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


async def collect_commute_evidence(provider, candidates: list[dict], destination: str,
                                   *, mode: str = "transit", timeout_s: float = 20.0) -> list[dict]:
    """Call ``calculate_commute`` once per listing and preserve failure classes.

    This helper intentionally does not retry or collapse calls.  The tool's own retry policy
    handles retry-safe reads; the per-listing evidence ledger must still show which listing
    succeeded, failed, or timed out.
    """
    destination = str(destination or "").strip()

    async def _one(candidate: dict) -> dict:
        address = str(candidate.get("address") or candidate.get("Address") or "").strip()
        base = {
            "candidate_key": candidate_key(candidate),
            "from_address": address,
            "to_address": destination,
            "mode": mode,
        }
        if not address or not destination:
            return {**base, "success": False, "evidence_status": "missing",
                    "error": "from_address or to_address is missing"}
        try:
            result = await asyncio.wait_for(provider.execute_tool(
                "calculate_commute", from_address=address, to_address=destination, mode=mode),
                timeout=timeout_s)
        except asyncio.TimeoutError:
            return {**base, "success": False, "evidence_status": "timeout",
                    "timed_out": True, "error": "calculate_commute timed out"}
        except Exception as exc:
            return {**base, "success": False, "evidence_status": "failed", "error": str(exc)}

        success = bool(getattr(result, "success", False))
        data = getattr(result, "data", None)
        error = getattr(result, "error", None)
        if isinstance(result, dict):
            success = result.get("success") is True
            data = result.get("data", result)
            error = result.get("error")
        raw = data if isinstance(data, dict) else {}
        duration = raw.get("duration_minutes")
        if not success:
            return {**base, "success": False, "evidence_status": "failed",
                    "raw_data": raw or None, "error": error or "calculate_commute failed"}
        return {**base, "success": True, "evidence_status": "success",
                "duration_minutes": duration, "raw_data": raw}

    return await asyncio.gather(*[_one(c) for c in (candidates or [])])


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
        r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|分钟)", str(text or ""), re.IGNORECASE
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


def validate_commute_response(response: str, state: dict) -> str:
    """Fail closed when prose asserts a commute result without linked evidence.

    Search responses normally use :func:`render_candidate_status`; this is the final
    defence for plain-text paths. A single successful commute artifact may support a
    direct one-origin commute answer. Multi-listing recommendations require the
    per-candidate validation ledger, so one successful call cannot license every row.
    """
    text = str(response or "")
    current = ((state.get("extracted_context") or {}).get("current_message")
               or state.get("user_query") or "")
    if not (_COMMUTE_CLAIM.search(text) and _COMMUTE_CONTEXT.search(current + " " + text)):
        return text

    language = str((state.get("extracted_context") or {}).get("reply_language") or "")
    fallback = ("本轮无法核实该房源的通勤条件。" if language.lower().startswith("zh")
                else "The commute condition could not be verified for the listing this round.")
    validation = state.get("candidate_validation") or {}
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
            return text
        return fallback

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
    return text if len(successful) == 1 else fallback


async def validate_search_payload_with_provider(provider, payload: dict, *,
                                                timeout_s: float = 20.0) -> tuple[dict, list[dict]]:
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
                provider, rows, destination, timeout_s=timeout_s)
    return validate_search_payload(payload, commute_evidence=evidence), evidence
