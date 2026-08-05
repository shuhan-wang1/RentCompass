"""Deterministic candidate eligibility and commute-evidence contracts."""
from __future__ import annotations

import re
from typing import Any

from core.tenancy_reference import monthly_from_weekly

_MONEY = re.compile(r"(?:£|GBP)?\s*([0-9][0-9,]*(?:\.\d+)?)\s*(pcm|pm|pw|/ ?month|/ ?week|monthly|weekly)?", re.I)
_COMMUTE = re.compile(r"\b(?:commute|travel\s*time|how\s+long|how\s+far|minutes?|mins?)\b|通勤|路程|分钟|多远|多久", re.I)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def listing_key(record: dict) -> str:
    record = record or {}
    url = _norm(record.get("url") or record.get("property_url") or record.get("listing_url"))
    if url:
        return "url:" + url
    address = _norm(record.get("address") or record.get("name"))
    price = _norm(record.get("price") or record.get("monthly_price") or record.get("price_raw"))
    return "listing:" + address + "|" + price


def _money(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), None
    match = _MONEY.search(str(value or ""))
    if not match:
        return None, None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None, None
    period = (match.group(2) or "").lower().replace(" ", "")
    return amount, ("weekly" if period in {"pw", "/week", "weekly"} else "monthly")


def _monthly_price(record: dict) -> float | None:
    for key in ("monthly_price", "price", "price_raw", "rent"):
        amount, period = _money(record.get(key))
        if amount is not None:
            return monthly_from_weekly(amount) if period == "weekly" else amount
    return None


def _executed(artifact: dict) -> bool:
    return not any(artifact.get(flag) for flag in ("timed_out", "denied", "abandoned", "outcome_unknown"))


def _same_place(left: Any, right: Any) -> bool:
    a, b = _norm(left), _norm(right)
    return bool(a and b and (a == b or a in b or b in a))


def _matches(artifact: dict, record: dict) -> bool:
    raw = artifact.get("raw_data") if isinstance(artifact, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    key = raw.get("candidate_key") or artifact.get("candidate_key")
    if key:
        return _norm(key) == _norm(listing_key(record))
    return _same_place(raw.get("from_address") or raw.get("origin") or raw.get("address"),
                       record.get("address") or record.get("name"))


def _commute_status(record: dict, artifacts: list[dict], used: set[int]) -> tuple[str, dict | None]:
    matches = [(i, a) for i, a in enumerate(artifacts) if _matches(a, record)]
    for i, artifact in matches:
        if i in used:
            continue
        raw = artifact.get("raw_data")
        if (_executed(artifact) and artifact.get("success") and isinstance(raw, dict)
                and raw.get("success", True) is not False
                and isinstance(raw.get("duration_minutes"), (int, float))):
            used.add(i)
            return "verified", raw
        used.add(i)
        if artifact.get("timed_out") or artifact.get("outcome_unknown") or artifact.get("abandoned"):
            return "timeout", None
        if artifact.get("success") is False or artifact.get("error"):
            return "failed", None
        return "unmeasured", raw if isinstance(raw, dict) else None
    # Failed calls have no address in some failure paths; bind them one-to-one in call order.
    for i, artifact in enumerate(artifacts):
        if i in used or not (artifact.get("success") is False or artifact.get("timed_out")):
            continue
        used.add(i)
        if artifact.get("timed_out") or artifact.get("outcome_unknown") or artifact.get("abandoned"):
            return "timeout", None
        return "failed", None
    return "missing", None


def _criteria(criteria: dict) -> tuple[float | None, float | None, str | None]:
    amount, period = _money((criteria or {}).get("max_budget"))
    if amount is not None and period == "weekly":
        amount = monthly_from_weekly(amount)
    limit = (criteria or {}).get("max_travel_time")
    if limit is None:
        limit = (criteria or {}).get("max_commute_time")
    try:
        limit = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    destination = (criteria or {}).get("commute_destination") or (criteria or {}).get("destination")
    return amount, limit, destination


def build_candidate_state(candidates: list[dict], criteria: dict | None = None,
                          artifacts: list[dict] | None = None) -> dict:
    criteria = dict(criteria or {})
    records = [dict(c) for c in (candidates or []) if isinstance(c, dict)]
    commute = [a for a in (artifacts or []) if a.get("tool") == "calculate_commute"]
    max_budget, max_time, destination = _criteria(criteria)
    needs_commute = bool(destination and max_time is not None and not criteria.get("no_commute"))
    used: set[int] = set()
    eligible, excluded, unknown, all_items = [], [], [], []
    for record in records:
        item = dict(record)
        item["candidate_key"] = listing_key(record)
        reason = None
        unknown_reason = None
        monthly_price = _monthly_price(record)
        if max_budget is not None:
            if monthly_price is None:
                unknown_reason = "price_unverified"
            elif monthly_price > max_budget + 0.005:
                reason = "over_budget"
        if needs_commute:
            status, raw = _commute_status(record, commute, used)
            item["commute_evidence_status"] = status
            if status == "verified":
                item["commute_duration_minutes"] = float(raw["duration_minutes"])
                if item["commute_duration_minutes"] > max_time:
                    reason = "over_commute"
            elif reason is None:
                unknown_reason = "commute_" + status
        item["candidate_status"] = "excluded" if reason else ("unknown" if unknown_reason else "eligible")
        item["status_reason"] = reason or unknown_reason
        if item["candidate_status"] == "eligible":
            eligible.append(item)
        elif item["candidate_status"] == "excluded":
            item["reason"] = reason
            excluded.append(item)
        else:
            item["reason"] = unknown_reason
            unknown.append(item)
        all_items.append(item)
    return {
        "all": all_items, "eligible": eligible, "excluded": excluded, "unknown": unknown,
        "meets_all": eligible, "eligible_recommendations": eligible,
        "unknown_recommendations": unknown, "excluded_recommendations": excluded,
        "criteria": criteria, "requires_commute_evidence": needs_commute,
    }


def validate_commute_response(response: str, state: dict) -> str:
    text = str(response or "")
    candidate = state.get("candidate_state") or {}
    criteria = candidate.get("criteria") or state.get("accumulated_search_criteria") or {}
    current = ((state.get("extracted_context") or {}).get("current_message")
               or state.get("user_query") or "")
    validation = state.get("candidate_validation") or {}
    if validation.get("statuses"):
        statuses = list(validation.get("statuses") or [])
        lowered = text.lower()
        named = [s for s in statuses
                 if any(str((s.get("candidate") or {}).get(key) or "").lower() in lowered
                        for key in ("address", "name"))]
        targets = named or statuses
        commute_required = bool((validation.get("constraints") or {}).get("max_commute_minutes"))
        if any(s.get("status") != "eligible"
               or (commute_required and s.get("evidence_status") != "success")
               for s in targets):
            return "The commute condition could not be verified for the listing this round."
    claim = bool(re.search(r"\b(?:meets?|within|under|over|about|around)\b.{0,50}"
                           r"\b\d+\s*(?:minutes?|mins?)\b|\d+\s*(?:minutes?|mins?)\b|符合|满足|超过|分钟",
                           text, re.I))
    _amount, limit, destination = _criteria(criteria)
    if limit is None:
        match = re.search(
            r"\b(?:under|within|below|up to)\s+(\d+(?:\.\d+)?)\s*(?:minutes?|mins?)\b",
            current, re.I)
        if match:
            limit = float(match.group(1))
            destination = destination or "the requested destination"
    if not claim or not (destination and limit is not None) or not _COMMUTE.search(current + " " + text):
        return text
    successful = [a for a in state.get("tool_artifacts") or []
                  if a.get("tool") == "calculate_commute" and _executed(a) and a.get("success")
                  and isinstance(a.get("raw_data"), dict)
                  and isinstance(a["raw_data"].get("duration_minutes"), (int, float))]
    if not (state.get("candidate_state") or {}).get("all"):
        if validation.get("statuses") and (validation.get("constraints") or {}).get("max_commute_minutes"):
            return text
        return "The commute condition could not be verified for the listing this round."
    return text if successful else "The commute condition could not be verified for the listing this round."


def render_candidate_response(candidate_state: dict, language: str = "en") -> str | None:
    if not candidate_state or not (candidate_state.get("unknown") or candidate_state.get("excluded")):
        return None
    if language == "zh":
        lines = ["候选房源状态（仅‘符合全部条件’的房源会列入推荐）："]
        for item in candidate_state.get("eligible", []):
            lines.append(f"- {item.get('address') or item.get('name') or '该房源'}：符合已核实的条件。")
        for item in candidate_state.get("unknown", []):
            lines.append(f"- {item.get('address') or item.get('name') or '该房源'}：通勤条件尚未核实。")
        for item in candidate_state.get("excluded", []):
            lines.append(f"- {item.get('address') or item.get('name') or '该房源'}：已排除（{item.get('reason')}）。")
        return "\n".join(lines)
    lines = ["Candidate status (only listings that pass all verified hard constraints are recommendations):"]
    for item in candidate_state.get("eligible", []):
        label = item.get("address") or item.get("name") or "This listing"
        duration = item.get("commute_duration_minutes")
        suffix = f"; measured commute {duration:g} minutes" if duration is not None else ""
        lines.append(f"- {label}: eligible{suffix}.")
    for item in candidate_state.get("unknown", []):
        lines.append(f"- {item.get('address') or item.get('name') or 'This listing'}: unknown — the required condition could not be verified.")
    for item in candidate_state.get("excluded", []):
        lines.append(f"- {item.get('address') or item.get('name') or 'This listing'}: excluded ({item.get('reason')}).")
    return "\n".join(lines)
