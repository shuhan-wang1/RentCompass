"""Deterministic candidate and commute-evidence contracts.

The search tool is a candidate generator.  It is not allowed to turn an incomplete
candidate into a recommendation merely because a model-described summary sounds positive.
This module is the shared boundary used by both agent architectures and the direct search
path: each listing is classified as ``eligible``, ``excluded`` or ``unknown`` from
structured fields and explicitly linked tool evidence.
"""

from __future__ import annotations

import asyncio
import functools
import math
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
    total_budget = _number(c.get("monthly_total_budget"))
    office_days = _number(c.get("office_days_per_week"))
    changes = _number(c.get("max_changes"))
    destination = c.get("commute_destination") or c.get("destination")
    areas = c.get("areas") or ([c.get("area")] if c.get("area") else [])
    if isinstance(areas, str):
        areas = [areas]
    features = c.get("property_features") or []
    if isinstance(features, str):
        features = [features]
    return {
        "max_budget_monthly": budget if budget and budget > 0 else None,
        # This is an independent rent-plus-travel constraint, never an invented rent cap.
        "monthly_total_budget": total_budget if total_budget is not None and
            math.isfinite(total_budget) and total_budget > 0 else None,
        "office_days_per_week": office_days if office_days is not None and
            math.isfinite(office_days) and 0 <= office_days <= 7 else None,
        "max_commute_minutes": commute if commute and commute > 0 else None,
        "commute_destination": str(destination).strip() if destination else None,
        "no_commute": bool(c.get("no_commute")),
        "max_changes": int(changes) if changes is not None and math.isfinite(changes) and changes.is_integer() and 0 <= changes <= 30 else None,
        "bedrooms": _number(c.get("bedrooms")),
        "bedrooms_minimum": bool(c.get("bedrooms_minimum")),
        "room_type": _norm(c.get("room_type")) or None,
        "areas": [value for value in (_norm(area) for area in areas) if value],
        "move_in_date": c.get("move_in_date") or None,
        "property_features": [value for value in (_norm(feature) for feature in features) if value],
    }


def revalidate_view_results(rows: list[dict], criteria: dict | None, *,
                            commute_evidence: Any = None) -> list[dict]:
    """A changed plan cannot reuse a previous plan's green qualification label."""
    constraints = hard_constraints(criteria)
    result = []
    for original in rows or []:
        row = dict(original)
        previous = row.get('qualification') or {}
        if previous.get('constraints') != constraints:
            # A change of area/budget does not invalidate a route from this exact listing
            # to the same destination. A bare old minute field is never such a proof.
            proof = None
            records = (commute_evidence.values() if isinstance(commute_evidence, dict)
                       else commute_evidence or [])
            if isinstance(commute_evidence, dict) and commute_evidence.get('candidate_key'):
                records = [commute_evidence]
            for entry in records:
                if _route_scope_matches(row, constraints, entry):
                    proof = entry  # the latest matching failure also overrides an old success
            if proof is None:
                proof = _stored_route_evidence(row, constraints, row.get('verified_commute_evidence'))
            status = validate_candidates([row], criteria, [proof] if proof else [])['statuses'][0]
            row = dict(status['candidate'])
            row['qualification'] = {
                'status': status['status'], 'reasons': status['reasons'],
                'unknown_reasons': status['unknown_reasons'],
                'constraints': constraints, 'source': 'structured_listing',
            }
            row['budget_basis'] = constraints['max_budget_monthly']
            price = _number(row.get('price', row.get('Price')))
            row['budget_status_code'] = ('unspecified' if row['budget_basis'] is None else
                'unknown' if price is None else 'over' if price > row['budget_basis'] else 'within')
        if row.get('budget_basis') is None or row.get('budget_status_code') == 'unspecified':
            row['budget_status'] = ''
        # Older stored views may have the current constraints but lack display text.
        # Rebuild only presentation metadata, retaining the stable machine reasons.
        if isinstance(row.get('qualification'), dict):
            qualification = dict(row['qualification'])
            reasons = list(dict.fromkeys(
                str(reason) for reason in [*(qualification.get('reasons') or []),
                                          *(qualification.get('unknown_reasons') or [])]))
            qualification['reason_messages'] = {
                language: [candidate_reason_text(reason, language=language) for reason in reasons]
                for language in ('en', 'zh')
            }
            row['qualification'] = qualification
        _withdraw_unverified_commute_display(row, criteria)
        result.append(row)
    return result


def _withdraw_unverified_commute_display(candidate: dict, criteria: dict) -> None:
    """A withdrawn route proof must also withdraw its old verified display label."""
    unknown = candidate.get('candidate_unknown_reasons',
        (candidate.get('qualification') or {}).get('unknown_reasons') or [])
    unverified = any('commute' in str(reason).lower() for reason in unknown)
    if not commute_constraint_required(criteria) or (
            candidate.get('verified_commute_minutes') is not None and not unverified):
        return
    candidate.pop('verified_commute_minutes', None)
    candidate.pop('verified_commute_evidence', None)
    previous = str(candidate.get('travel_time') or '')
    if previous and not re.search(r'estimat|unverified|not verified|待核实|未核实|估算', previous, re.I):
        candidate['travel_time'] = 'Commute not verified for current criteria'
    if isinstance(candidate.get('explanation'), str):
        candidate['explanation'] = re.sub(
            r'\(TfL transit:[^)]*\)', '(commute not verified for current criteria)',
            candidate['explanation'], flags=re.I)



def revalidate_saved_favorite(original: dict) -> dict:
    """Recheck the saved criteria and evidence rather than trust an old green label."""
    row = dict(original)
    qualification = row.get('qualification') or {}
    constraints = qualification.get('constraints')
    if not isinstance(constraints, dict) or not constraints:
        return row
    criteria = dict(constraints, max_commute_time=constraints.get('max_commute_minutes'))
    # Saved criteria are deliberately independent of the currently open conversation.
    row['qualification'] = dict(qualification, constraints=None)
    return revalidate_view_results([row], criteria)[0]

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


def _route_scope_matches(candidate: dict, constraints: dict, entry: Any) -> bool:
    """Reuse only a listing-key, origin and destination bound route, never town context."""
    if not isinstance(entry, dict) or entry.get('candidate_key') != candidate_key(candidate):
        return False
    raw = entry.get('raw_data') if isinstance(entry.get('raw_data'), dict) else {}
    origin = _norm(candidate.get('address') or candidate.get('Address'))
    destination = _norm(constraints.get('commute_destination'))
    origins = [value for value in (entry.get('from_address'), raw.get('from_address')) if value]
    destinations = [value for value in (entry.get('to_address'), raw.get('to_address')) if value]
    return bool(origin and destination and origins and destinations
                and all(_norm(value) == origin for value in origins)
                and all(_norm(value) == destination for value in destinations))


def _stored_route_evidence(candidate: dict, constraints: dict, entry: Any) -> dict | None:
    """A small immutable fact record survives display-only criteria revalidation."""
    if (not _route_scope_matches(candidate, constraints, entry)
            or entry.get('success') is not True or _evidence_status(entry) != 'success'):
        return None
    if any(entry.get(flag) for flag in ('timed_out', 'denied', 'abandoned', 'outcome_unknown')):
        return None
    raw = entry.get('raw_data') if isinstance(entry.get('raw_data'), dict) else entry
    if raw.get('success') is False:
        return None
    duration = _measured_minutes({**raw, 'duration_minutes': entry.get(
        'duration_minutes', raw.get('duration_minutes'))})
    if duration is None:
        return None
    returned_duration = raw.get('duration_minutes')
    if returned_duration is not None and _measured_minutes(raw) != duration:
        return None
    origin = str(candidate.get('address') or candidate.get('Address')).strip()
    destination = constraints['commute_destination']
    facts = {'from_address': origin, 'to_address': destination, 'duration_minutes': duration}
    facts.update({name: raw[name] for name in ('basis', 'route_source', 'source') if raw.get(name)})
    changes = raw.get('changes_count')
    if isinstance(changes, int) and not isinstance(changes, bool) and changes >= 0:
        facts['changes_count'] = changes
    return {'candidate_key': candidate_key(candidate), 'from_address': origin,
            'to_address': destination, 'success': True, 'evidence_status': 'success',
            'duration_minutes': duration, 'raw_data': facts}


_STUDIO_WORD = re.compile(r"\bstudio\b|开间|单间公寓|一室户", re.I)


def _candidate_bedrooms(candidate: dict) -> float | None:
    """Bedroom count; a studio is 0 bedrooms.

    Listings carry ``bedrooms: 'Studio'`` / ``property_type: 'Studio'``; read as "no number",
    every studio of a studio search was filed "Not verified: bedroom count is not verified"
    under "No listing is both fully eligible and verified" (R4-07)."""
    raw = candidate.get("bedrooms", candidate.get("Bedrooms"))
    value = _number(raw)
    if value is not None:
        return value
    if isinstance(raw, str) and _STUDIO_WORD.search(raw):
        return 0.0
    if raw in (None, "", "N/A", "n/a", "Unknown") and any(
            isinstance(candidate.get(key), str) and _STUDIO_WORD.search(candidate[key])
            for key in ("property_type", "Type", "room_type", "Room_Type_Category")):
        return 0.0
    return None


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


def _monthly_transport_evidence(candidate: dict, constraints: dict, evidence: Any) -> tuple[float | None, str]:
    """A certified monthly fare belongs to one listing, destination AND attendance plan.

    Current-turn calculate_commute_cost artifacts and collector-style evidence records are
    supported. An estimated daily-cap bound is useful context, not a verified actual fare.
    Do not resurrect an older success after a newer matching query failed or used stale
    attendance. The caller owns current-turn evidence selection; candidate prose is ignored.
    """
    destination = _norm(constraints.get("commute_destination"))
    frequency = constraints.get("office_days_per_week")
    if not destination or frequency is None:
        return None, "missing_plan"
    if isinstance(evidence, dict):
        entries = [evidence] if any(key in evidence for key in (
            "candidate_key", "raw_data", "from_address", "transport_cost")) else list(evidence.values())
    else:
        entries = list(evidence or [])
    address = _norm(candidate.get("address") or candidate.get("Address"))
    matching = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("raw_data") if isinstance(entry.get("raw_data"), dict) else entry
        if not (entry.get("tool") == "calculate_commute_cost" or
                "transport_cost" in raw or "monthly_cost" in raw):
            continue
        key = entry.get("candidate_key")
        returned_from = _norm(raw.get("from_address") or entry.get("from_address"))
        if key and key != candidate_key(candidate):
            continue
        if returned_from and returned_from != address:
            continue
        if not returned_from and not (key and key == candidate_key(candidate)):
            continue
        if _norm(raw.get("to_address") or entry.get("to_address")) != destination:
            continue
        matching.append((entry, raw))
    if not matching:
        return None, "missing"
    entry, raw = matching[-1]
    if _evidence_status(entry) != "success" or raw.get("success") is False or any(
            entry.get(flag) for flag in ("timed_out", "denied", "abandoned", "outcome_unknown")):
        return None, "failed"
    cost = raw.get("transport_cost") if isinstance(raw.get("transport_cost"), dict) else raw
    frequencies = [obj.get("office_days_per_week") for obj in (entry, raw, cost)
                   if obj.get("office_days_per_week") is not None]
    if not frequencies or any(isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) or abs(value - frequency) > 1e-9
                              for value in frequencies):
        return None, "stale_frequency"
    if cost.get("error") or cost.get("coverage_verified") is not True:
        return None, "unverified_coverage"
    basis = _norm(cost.get("monthly_cost_basis"))
    is_bound = bool(re.search(r"cap|upper.bound|estimate|approx|封顶|上界|估算", basis))
    if (cost.get("monthly_cost_is_upper_bound") is True or is_bound or
            (cost.get("payg_estimate_kind") == "daily_cap_upper_bound"
             and basis not in {"monthly travelcard", "monthly pass"})):
        return None, "upper_bound"
    value = cost.get("monthly_cost")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None, "missing_amount"
    return float(value), "success"


def validate_candidates(candidates: list[dict], criteria: dict | None,
                        commute_evidence: Any = None, *, transport_cost_evidence: Any = None) -> dict:
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
        # A previous plan's measured figure is not evidence for this validation pass.
        candidate.pop("verified_commute_minutes", None)
        candidate.pop("verified_commute_evidence", None)
        candidate.pop("verified_monthly_transport_cost", None)
        candidate.pop("verified_monthly_total_cost", None)
        reasons: list[str] = []
        unknown_reasons: list[str] = []
        evidence_status = "not_required"
        cost_evidence_status = "not_required"

        if candidate.get("alternative") or candidate.get("match_type") in {
            "soft_violation", "similar_suggestion"
        }:
            reasons.append("alternative is outside the declared hard result set")

        price = _number(candidate.get("price", candidate.get("Price")))
        if price is not None and (not math.isfinite(price) or price < 0):
            price = None
        budget = constraints["max_budget_monthly"]
        if budget is not None:
            if price is None:
                unknown_reasons.append("price is not structured")
            elif price > budget + 1e-9:
                reasons.append(f"monthly price {price:g} exceeds budget {budget:g}")

        total_budget = constraints["monthly_total_budget"]
        if total_budget is not None:
            if price is None:
                unknown_reasons.append("monthly total cannot be verified without a structured rent")
                cost_evidence_status = "missing_rent"
            elif price > total_budget + 1e-9:
                # Travel cannot make an already over-budget rent affordable.
                reasons.append(f"monthly rent {price:g} alone exceeds total budget {total_budget:g}")
                cost_evidence_status = "rent_exceeds_total"
            else:
                monthly_travel, cost_evidence_status = _monthly_transport_evidence(
                    candidate, constraints, transport_cost_evidence if transport_cost_evidence is not None
                    else commute_evidence)
                if monthly_travel is None:
                    unknown_reasons.append("monthly travel cost is not verified for this listing, destination and attendance")
                else:
                    total = round(price + monthly_travel, 2)
                    candidate["verified_monthly_transport_cost"] = monthly_travel
                    candidate["verified_monthly_total_cost"] = total
                    if total > total_budget + 1e-9:
                        reasons.append(f"monthly rent and travel {total:g} exceeds total budget {total_budget:g}")

        bedrooms = constraints["bedrooms"]
        if bedrooms is not None:
            actual_bedrooms = _candidate_bedrooms(candidate)
            if actual_bedrooms is None:
                unknown_reasons.append("bedroom count is not verified")
            elif (actual_bedrooms < bedrooms if constraints['bedrooms_minimum'] else actual_bedrooms != bedrooms):
                reasons.append(
                    f"bedroom count {actual_bedrooms:g} does not equal requested {bedrooms:g}")

        if constraints["room_type"]:
            actual_type = _norm(candidate.get("room_type") or candidate.get("property_type")
                                or candidate.get("Type"))
            if not actual_type:
                unknown_reasons.append("room type is not verified")
            elif constraints['room_type'] == 'whole':
                from core.tools.search_properties import _matches_room_type
                if not _matches_room_type({'Room_Type_Category': actual_type,
                    'Type': candidate.get('property_type') or candidate.get('Type') or ''}, 'whole'):
                    reasons.append('room type does not match whole property')
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
                # Production rows carry a three-way evidence ledger. A requirement
                # introduced after this row was fetched is still unknown unless the
                # provider explicitly denied it. Older binary callers retain their
                # original meaning for verified_features alone.
                unverified = {
                    _norm(feature) for feature in (candidate.get("unverified_features") or [])
                    if _norm(feature)
                }
                explicit_absent = {_norm(feature) for feature in
                    (candidate.get('absent_features') or []) if _norm(feature)}
                tri_state = 'absent_features' in candidate and 'unverified_features' in candidate
                unknown_features = [feature for feature in missing_features if
                    (feature not in explicit_absent if tri_state else feature in unverified)]
                absent_features = [feature for feature in missing_features if
                    (feature in explicit_absent if tri_state else feature not in unverified)]
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

        if (constraints['max_commute_minutes'] is not None and not constraints['no_commute']
                and not constraints['commute_destination']):
            unknown_reasons.append('commute destination is not verified')

        if commute_constraint_required(criteria):
            entry = _evidence_for(candidate, commute_evidence)
            evidence_status = _evidence_status(entry)
            if entry is None:
                unknown_reasons.append("commute evidence is missing")
            elif evidence_status != "success":
                unknown_reasons.append(f"commute tool {evidence_status}")
            else:
                raw = entry.get("raw_data") if isinstance(entry.get("raw_data"), dict) else entry
                # A result that cannot be tied back to the requested listing is not evidence
                # for that listing, even if it contains a plausible duration.
                returned_from = raw.get("from_address") if isinstance(raw, dict) else None
                returned_to = raw.get("to_address") if isinstance(raw, dict) else None
                if ((returned_from and _norm(returned_from) != _norm(
                        candidate.get("address") or candidate.get("Address")))
                        or (returned_to and _norm(returned_to) != _norm(
                            constraints["commute_destination"]))):
                    evidence_status = "unlinked"
                    unknown_reasons.append("commute result cannot be linked to this listing")
                duration = _measured_minutes({**raw, "duration_minutes": entry.get(
                    "duration_minutes", raw.get("duration_minutes"))})
                if evidence_status == "unlinked":
                    pass
                elif duration is None or raw.get("success") is False:
                    # Estimated ranges are useful context but do not license a binary
                    # meets-the-limit claim.
                    evidence_status = "insufficient"
                    unknown_reasons.append("commute result has no measured duration")
                elif duration > constraints["max_commute_minutes"]:
                    candidate["verified_commute_minutes"] = duration
                    reasons.append(
                        f"measured commute {duration:g} min exceeds limit "
                        f"{constraints['max_commute_minutes']:g} min")
                else:
                    candidate["verified_commute_minutes"] = duration
                if evidence_status == "success" and duration is not None:
                    proof = _stored_route_evidence(candidate, constraints, entry)
                    if proof is not None:
                        candidate['verified_commute_evidence'] = proof

        if constraints['max_changes'] is not None and not constraints['no_commute']:
            entry = _evidence_for(candidate, commute_evidence)
            raw = (entry.get('raw_data') if isinstance((entry or {}).get('raw_data'), dict) else entry) or {}
            changes = raw.get('changes_count')
            linked = (_evidence_status(entry) == 'success' and raw.get('success') is not False
                      and _norm(raw.get('from_address')) == _norm(candidate.get('address') or candidate.get('Address'))
                      and _norm(raw.get('to_address')) == _norm(constraints['commute_destination']))
            if not linked or isinstance(changes, bool) or not isinstance(changes, int) or changes < 0:
                unknown_reasons.append('journey changes are not verified for this listing and destination')
            elif changes > constraints['max_changes']:
                reasons.append(f"journey changes {changes} exceed limit {constraints['max_changes']}")

        status = "excluded" if reasons else "unknown" if unknown_reasons else "eligible"
        candidate["candidate_status"] = status
        candidate["candidate_reasons"] = list(reasons)
        candidate["candidate_unknown_reasons"] = list(unknown_reasons)
        _withdraw_unverified_commute_display(candidate, criteria)
        statuses.append({
            "candidate": candidate,
            "candidate_key": candidate_key(candidate),
            "index": index,
            "status": status,
            "reasons": reasons,
            "unknown_reasons": unknown_reasons,
            "evidence_status": evidence_status,
            "transport_cost_evidence_status": cost_evidence_status,
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
    if zh:
        return "；".join(dict.fromkeys(_candidate_reason_zh(reason) for reason in reasons))
    return "; ".join(dict.fromkeys(
        "cannot verify the commute condition" if "commute" in str(reason).lower()
        else humanize_requirement_text(str(reason)) for reason in reasons))


def humanize_requirement_text(text: str, *, language: str = 'en') -> str:
    """Present precise requirements as words without changing the evidence keys."""
    labels = ({'wheelchair_space_68cm':'68cm轮椅通过房门及浴室的通行空间',
               'cat_written_consent':'猫的书面饲养许可'}
              if (language or '').lower().startswith('zh') else
              {'wheelchair_space_68cm':'door and bathroom clearance for a 68 cm wheelchair',
               'cat_written_consent':'written permission for the cat'})
    for key, label in labels.items():
        text = str(text).replace(key, label)
    return text


def render_nearby_failure_notice(state: dict, *, language: str = 'en') -> str:
    """State unsuccessful facility coverage and the actual configured radius."""
    zh = (language or '').lower().startswith('zh')
    latest = next((a for a in reversed(state.get('tool_artifacts') or []) if
                   isinstance(a, dict) and a.get('tool') == 'search_nearby_pois'), None)
    if not latest or any(latest.get(flag) for flag in ('denied','abandoned','outcome_unknown')):
        return ''
    raw = latest.get('raw_data') or {}
    if not isinstance(raw, dict):
        return ''
    failed = raw.get('failed_types') or raw.get('unqueried_types') or []
    if not failed and latest.get('success') is True:
        return ''
    if not failed and not raw.get('service_status'):
        return ''
    categories = ', '.join(str(kind) for kind in failed) or 'requested facilities'
    radius, requested = raw.get('radius_m'), raw.get('user_requested_radius_m') or raw.get('requested_radius_m')
    notice = (f'周边设施服务未完成以下类别的查询：{categories}；不能据此断言附近没有这些设施。'
              if zh else f'The nearby facilities service did not complete these lookups: {categories}. This does not establish that no such facilities exist nearby.')
    if raw.get('service_status') in ('unavailable','cooldown') or any(
            isinstance(value, dict) and value.get('service_status') == 'cooldown'
            for value in (raw.get('category_status') or {}).values()):
        notice += ' 服务不可用或处于失败冷却期。' if zh else ' The service is unavailable or cooling down after failures.'
    if requested is not None and radius is not None and requested != radius:
        notice += (f' 请求半径为{requested:g}米，当前查询半径上限为{radius:g}米，未覆盖完整请求范围。'
                   if zh else f' Requested radius: {requested:g} m; configured query limit: {radius:g} m. The full requested radius was not covered.')
    return notice


def _candidate_reason_zh(reason: Any) -> str:
    """Localize presentation only; the stable internal reason strings remain unchanged."""
    text = str(reason)
    labels = {
        "alternative is outside the declared hard result set": "该房源是备选，未满足全部已声明条件",
        "price is not structured": "月租信息尚未核实",
        "bedroom count is not verified": "卧室数量尚未核实",
        "room type is not verified": "房型尚未核实",
        "area is not verified": "所在区域尚未核实",
        "property features are not structurally verified": "所需设施尚未核实",
        "availability is not verified": "可入住日期尚未核实",
        "insufficient evidence": "证据不足",
        "journey changes are not verified for this listing and destination": "尚未核实该房源至目的地的换乘次数",
        "commute destination is not verified": "通勤目的地尚未核实，无法判断时长限制",
        "commute evidence is missing": "缺少该房源的通勤证据",
        "commute result cannot be linked to this listing": "通勤结果无法对应到该房源",
        "commute result has no measured duration": "没有经路线核实的通勤时长",
        "monthly total cannot be verified without a structured rent": "缺少经核实的月租，无法核实每月总预算",
        "monthly travel cost is not verified for this listing, destination and attendance": "尚未核实该房源、目的地及最新出勤频率对应的月交通费，不能确认符合总预算",
    }
    if text in labels:
        return labels[text]
    patterns = (
        (r"monthly price (.+) exceeds budget (.+)", "月租 £{} 超过预算 £{}"),
        (r"monthly rent (.+) alone exceeds total budget (.+)", "仅月租 £{} 就已超过每月总预算 £{}"),
        (r"monthly rent and travel (.+) exceeds total budget (.+)", "经核实的月租和交通费合计 £{}，超过每月总预算 £{}"),
        (r"bedroom count (.+) does not equal requested (.+)", "卧室数量为 {}，不符合所需的 {} 间"),
        (r"area (.+) is outside the requested areas", "所在区域 {} 不在指定范围内"),
        (r"available from (.+), after requested date", "可入住日期为 {}，晚于要求日期"),
        (r"measured commute (.+) min exceeds limit (.+) min", "经核实通勤为 {} 分钟，超过 {} 分钟上限"),
    )
    for pattern, template in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return template.format(*match.groups())
    if text.startswith("room type does not match "):
        kind = text.removeprefix("room type does not match ")
        return "房型不符合要求：" + {
            "whole property": "整套住宅", "whole": "整套住宅", "studio": "独立单间公寓",
            "shared": "合租房间", "ensuite": "独立卫浴房间",
        }.get(kind, kind)
    for prefix, label in (("missing required features: ", "缺少必需设施："),
                          ("required features are not verified: ", "必需设施尚未核实：")):
        if text.startswith(prefix):
            features = {"step-free": "无台阶通行", "lift": "电梯", "wheelchair-accessible": "轮椅通行条件",
                        "pet-friendly": "允许宠物", "furnished": "家具",
                        "cats allowed": "允许养猫", "pets allowed": "允许养宠物",
                        "accessible bathroom": "无障碍浴室",
                        "wheelchair_space_68cm": "68cm轮椅通过房门及浴室的通行空间",
                        "cat_written_consent": "猫的书面饲养许可"}
            return label + "、".join(features.get(v.strip(), v.strip())
                                     for v in text[len(prefix):].split(","))
    if text.startswith("commute tool "):
        return "通勤条件尚未核实（" + {
            "failed": "查询失败", "timeout": "查询超时", "outcome_unknown": "结果未知",
            "budget_exhausted": "查询时间已用尽", "skipped": "未完成查询",
        }.get(text[len("commute tool "):], "查询未完成") + "）"
    return "相关条件尚未核实或不符合要求"


def candidate_reason_text(reason: str, *, language: str = "en") -> str:
    """Public presentation formatter; machine reason values remain unchanged."""
    return (_candidate_reason_zh(reason) if (language or "").lower().startswith("zh")
            else humanize_requirement_text(str(reason)))


def _cached_listing_note(rows: list[dict], *, zh: bool) -> str:
    cached = any(row.get("possibly_outdated") or "cached" in _norm(row.get("source"))
                 or "缓存" in str(row.get("source") or "") for row in rows)
    if not cached:
        return ""
    return ("房源来自缓存，价格和出租状态可能已变化；请联系中介核实。" if zh else
            "These listings are cached; prices and availability may have changed. Confirm with the agent.")

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
            lines.extend(f"- {_address(s)}：{'；'.join(_candidate_reason_zh(reason) for reason in s.get('reasons') or ['不满足硬约束'])}" for s in excluded)
        if unknown:
            lines.append("尚未核实：")
            lines.extend(f"- {_address(s)}：{_unknown_reason_text(s, zh=True)}" for s in unknown)
        note = _cached_listing_note([s.get("candidate") or {} for s in eligible + excluded + unknown], zh=True)
        if note:
            lines.append(note)
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
    note = _cached_listing_note([s.get("candidate") or {} for s in eligible + excluded + unknown], zh=False)
    if note:
        lines.append(note)
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


def validate_search_payload(payload: dict, *, commute_evidence: list[dict] | None = None,
                            transport_cost_evidence: Any = None) -> dict:
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
        transport_cost_evidence=(transport_cost_evidence if transport_cost_evidence is not None
                                 else payload.get("transport_cost_evidence")),
    )
    out = dict(payload)
    out["candidate_validation"] = validation
    out["candidate_states"] = validation["statuses"]
    out["commute_evidence"] = list(commute_evidence or [])
    if transport_cost_evidence is not None:
        out["transport_cost_evidence"] = transport_cost_evidence
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


# A minute figure starts where a number starts. Without the look-behind the engine retried
# the match at EVERY digit of a long run, each attempt re-scanning the rest of the run before
# failing to find "minutes" — O(n²) on a degenerate digit string, which is what the model
# emitted (8 192 tokens of 9s) for a message carrying a 1 500-digit "flat number". Two passes
# over that answer stalled the whole process for ~100 s (2026-10-19 B-01). Anchored, each run
# is scanned once. Python 3.10 has no possessive quantifiers, hence the look-behind.
# "min" is a whole word: "(29 minus 21)" read as a 29-minute claim and the evidenced
# difference beside it was withdrawn (r06 099518b9). Same rule as ``_MINUTE_TOKEN``.
_MINUTE_VALUE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)[\s\-–]*(?:minutes?|mins?\b|分钟)", re.IGNORECASE)


def _minute_claim_values(text: str) -> list[float]:
    values = []
    for match in _MINUTE_VALUE.finditer(str(text or "")):
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
    current = str((state.get("extracted_context") or {}).get("current_message") or
                  state.get("user_query") or "")
    # A user's explicitly stated ceiling is a constraint even before a criteria refresh.
    # Read only user text, never the answer whose measurements are being checked.
    for pattern in (
        r"(?:within|under|at most|no more than|maximum of)\s*(\d+(?:\.\d+)?)\s*[-–]?\s*(?:minutes?|mins?)",
        r"(\d+(?:\.\d+)?)\s*[-–]?\s*(?:minutes?|mins?)\s*(?:commute\s+)?(?:cap|limit|maximum)",
        r"(\d+(?:\.\d+)?)\s*分钟\s*(?:以内|之内|上限|为上限)",
    ) + _COMMUTE_LIMIT_PATTERNS:
        caps.update(float(value) for value in re.findall(pattern, current, re.I) if float(value) > 0)
    # ... and so is one the user stated earlier and saved: a [FACT]/[PAST] line of the memory
    # block, a semantic/episodic row a recall returned, or the user's own earlier message in
    # this conversation. "Max commute: 35 minutes" in a recall answer restates the saved
    # "The user's maximum commute is 35 minutes." and was withdrawn as an unmeasured journey
    # (r07 inst-A turn 26, A-F10). Never the assistant's [INFERENCE] lines or its own prose.
    for text in _user_stated_texts(state):
        for pattern in _COMMUTE_LIMIT_PATTERNS:
            caps.update(float(value) for value in re.findall(pattern, text, re.I) if float(value) > 0)
    return caps


# A commute LIMIT stated in words that name the commute ("my maximum commute is 35 minutes",
# "commute of no more than 40 mins", "通勤不超过35分钟").
_COMMUTE_LIMIT_PATTERNS = (
    r"\b(?:max(?:imum)?|longest)\s+(?:commute|commuting\s+time|travel(?:ling)?\s+time|journey(?:\s+time)?)"
    r"(?:\s+time)?\s*(?:is|of|=|:|would\s+be|should\s+be)?\s*(?:about\s+|around\s+)?"
    r"(\d{1,3}(?:\.\d+)?)\s*[-–]?\s*(?:minutes?|mins?)\b",
    r"\b(?:commute|travel\s+time|journey(?:\s+time)?)\s+(?:limit|cap|ceiling|maximum)\s*(?:is|of|=|:)?\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*[-–]?\s*(?:minutes?|mins?)\b",
    r"\b(\d{1,3}(?:\.\d+)?)[-\s]?(?:minutes?|mins?)\s+(?:max(?:imum)?|limit)\s+(?:on\s+the\s+)?(?:commute|journey|travel)",
    r"\b(?:commute|journey|travel\s+time)\b[^.;\n]{0,30}?\b(?:no\s+more\s+than|at\s+most|up\s+to|within|under|"
    r"less\s+than|no\s+longer\s+than)\s*(\d{1,3}(?:\.\d+)?)\s*[-–]?\s*(?:minutes?|mins?)\b",
    r"通勤[^。；\n]{0,8}?(?:最长|最多|不超过|不要超过|上限|以内|之内)[^。；\n]{0,4}?(\d{1,3})\s*分钟",
    r"通勤[^。；\n]{0,8}?(\d{1,3})\s*分钟\s*(?:以内|之内|为上限|上限)",
)


def _user_stated_texts(state: dict) -> list[str]:
    """User-stated or user-saved text of this turn: never the assistant's inferences."""
    texts: list[str] = []
    for line in str(state.get("memory_context") or "").splitlines()[:200]:
        if re.match(r"\s*\[(?:FACT|PAST)\]", line):
            texts.append(line)
    for artifact in state.get("tool_artifacts") or []:
        if not (isinstance(artifact, dict) and artifact.get("tool") == "recall_memory"
                and artifact.get("success") is True):
            continue
        raw = artifact.get("raw_data") if isinstance(artifact.get("raw_data"), dict) else {}
        for memory in (raw.get("memories") or [])[:100]:
            if isinstance(memory, dict) and memory.get("mtype") in ("semantic", "episodic"):
                texts.append(str(memory.get("text") or ""))
    for turn in ((state.get("extracted_context") or {}).get("history") or [])[-20:]:
        if isinstance(turn, dict):
            texts.append(str(turn.get("user") or (turn.get("content") if turn.get("role") == "user" else "") or ""))
    return [text[:2000] for text in texts if text]


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
# Anchored at the start of a number for the same reason as ``_MINUTE_VALUE`` (B-01).
_MINUTE_TOKEN = re.compile(
    r"(?<![\d.])\d+(?:\.\d+)?[\s\-–]*(?:minutes?|mins?\b|分钟|分鐘)", re.IGNORECASE)
_COMMUTE_WORD = re.compile(
    r"commute|travel\s*time|journey|通勤|路程|车程|車程", re.IGNORECASE)
# A commute VERDICT needs no number to be a claim: "this one exceeds your commute limit" is
# exactly as unevidenced as "this one is 58 minutes", and a redactor that removes only the
# figure leaves the conclusion standing under a note saying unverified figures were removed.
# Both directions count — passing and failing a limit are equally unsupported without evidence.
_FIT_WORD = re.compile(
    r"符合|满足|不满足|未满足|超过|超出|超标|未超过|以内|之内|范围内|达标|未达|"
    r"更远|更近|太远|够近|不远|"
    r"\b(?:meets?|met|within|"
    # "about/around/over/under" compare a TIME or a LIMIT only when one follows: "about 40
    # minutes", "over an hour", "under your limit". "forms its own inferences about your
    # priorities" beside the word commute is not a commute claim (R3-01, R2-19).
    r"(?:under|over|about|around)(?=\s+(?:(?:your|the|my|its|a|an|half|just|roughly|about|around|commute|"
    r"commuting|journey|travel|time|daily|stated|maximum|max)\s+){0,3}"
    r"(?:\d|(?:hours?|hrs?|limit|cap|max|maximum|target|threshold|ceiling|requirement|minutes?|mins?)\b))|"
    # "takes about 40 minutes" / "takes an hour" — never "take an umbrella" (R2-01 f).
    r"(?:takes?|took)(?=\s+(?:about|around|roughly|approximately|over|under|less|more|an?\s+"
    r"(?:hour|while|bit|long)|\d|just|only|at\s+least|up\s+to|nearly|almost|half|longer|"
    r"ages|forever|too\s+long))|"
    r"exceeds?|exceeded|exceeding|beyond|outside|inside|above|below|longer|shorter|"
    r"further|farther|closer|nearer|faster|slower|fits?|satisf(?:y|ies|ied)|"
    r"qualifies|violates?|breaches?|misses|fails?)\b",
    re.IGNORECASE)
# A comma between digits is a thousands separator, never a clause boundary — splitting there
# would cut "£1,733" in half.
# The spaced-dash separator is anchored at the FIRST whitespace of a run: unanchored, a long
# run of spaces was re-scanned from every position (O(n²), same class as B-01).
_FRAGMENT_SPLIT = re.compile(
    r"((?<!\d),|,(?!\d)|(?<!\d):|:(?!\d)|[，；;、。！？：|]|\.(?=\s|$)|[!?](?=\s|$)"
    r"|(?<!\s)\s+[–—-]\s+)")
_BRACKET_SPAN = re.compile(r"[（(][^()（）\n]{0,400}[)）]")
_SCOPE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n|[；;]")
# "vs." too: "studio vs. a room in a shared flat vs. a whole one-bed" is one clause, and
# splitting it there left "studio vs. a room in a shared flat vs." behind (stress B-02).
_NAME_ABBREVIATION = r"(?:St|Dr|Mr|Mrs|Ms|Prof|Sr|Jr|vs)"


def _repair_abbreviation_breaks(text: str) -> str:
    # A preceding scope pass may already have inserted a line break after a title/name.
    return re.sub(r"(\b" + _NAME_ABBREVIATION + r"\.)[ \t]*\n[ \t]*(?=[A-Z])",
                  r"\1 ", str(text or ""))


def _guard_text_parts(text: str, *, sentences: bool = False) -> list[str]:
    """Split genuine clauses/sentences, retaining punctuation inside facts and names.

    The clause form retains alternating separators like re.split's capturing group;
    the sentence form drops separators but retains the preceding sentence punctuation.
    """
    protected = [(match.start(), match.start() + len(match.group().rstrip('.,;:!?')))
                 for match in re.finditer(r"(?:https?://|www\.)[^\s<>\"'()]+", text, re.I)]
    if not sentences:
        # A bracketed aside is one unit: "(that commute figure is an estimate, not a verified
        # journey time)" split at its comma, the clause before it went and ")." shipped alone
        # (R3-01). Bounded and unnested, so linear.
        protected.extend((match.start() + 1, match.end() - 1)
                         for match in _BRACKET_SPAN.finditer(text))
    parts = []
    start = 0
    pattern = _SCOPE_SENTENCE_SPLIT if sentences else _FRAGMENT_SPLIT
    for match in pattern.finditer(text):
        position = match.start()
        if sentences and text[position:position + 1].isspace() and position:
            position -= 1
        if any(left <= position < right for left, right in protected):
            continue
        if text[position:position + 1] == '.':
            # "192 minutes, i.e. 3 hours 12 minutes a month" is one clause (r17 8c35fd5b).
            abbreviation = re.search(r"\b(?:" + _NAME_ABBREVIATION + r"|i\.e|e\.g)\.$", text[:position + 1], re.I)
            decimal = (position > 0 and position + 1 < len(text)
                       and text[position - 1].isdigit() and text[position + 1].isdigit())
            ellipsis = text[max(0, position - 1):position] == '.' or text[position + 1:position + 2] == '.'
            if abbreviation or decimal or ellipsis:
                continue
        parts.append(text[start:match.start()])
        if not sentences:
            parts.append(match.group())
        start = match.end()
    parts.append(text[start:])
    return parts


# Anchored at the first whitespace of a run (see ``_FRAGMENT_SPLIT``); leftmost-match
# semantics already started every real match there, so the result is unchanged.
_PARENTHETICAL = re.compile(r"(?<!\s)\s*[（(][^()（）]*[)）]")
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


# Without a figure, a clause that mentions the commute is a commute VERDICT only when the thing
# it compares is time. "a studio in Hackney under £1,500 with no commute" compares money, and
# "for your commute it will be about 12°C" compares the weather; both were withdrawn as
# unverified commute claims, with the footnote, on a refusal and a weather turn (R2-01 e/f).
# The money/temperature SPAN is taken out before the comparative is looked for, so a clause
# that also carries a price still counts: "Flat A (£1,500/month) is within your commute limit"
# is a commute verdict. Every number is anchored at its first digit (the B-01 class).
_MONEY_OR_WEATHER_SPAN = re.compile(
    r"(?:\b(?:under|within|below|over|above|about|around|roughly|up\s+to|at\s+most|less\s+than|"
    r"more\s+than|no\s+more\s+than)\s+)?(?:£|\$|€)\s*\d[\d,.]*\s*k?"
    r"(?:\s*(?:pcm|pw|p/?m|/\s*(?:month|week|mo)\b|per\s+(?:month|week)|a\s+(?:month|week)))?|"
    # "search actual listings … within a budget" (r18 eb65e918) is money too.
    r"\b(?:within|under|over|above|below|inside|outside|beyond)\s+(?:your\s+|the\s+|my\s+|a\s+|any\s+)?"
    r"(?:budget|price\s+range|ceiling|rent\s+(?:limit|cap|ceiling))\b|"
    r"(?:\b(?:about|around|roughly|over|under|above|below)\s+)?(?<![\d.])-?\d+(?:\.\d+)?\s*"
    r"(?:°\s*[CF]?|℃|degrees?(?:\s+[CF]\b)?)|"
    r"\btemperatures?\s+(?:\w+\s+){0,2}(?:above|below|around|about|over|under)\s+\w+|"
    # A DISTANCE is not a travel time: "derived from the straight-line distance of about
    # 7.8 km" was withdrawn as a commute verdict (R2-01 c).
    r"(?:\b(?:about|around|roughly|over|under|within|nearly|almost)\s+)?(?<![\d.])\d+(?:\.\d+)?\s*"
    r"(?:km|kilomet(?:er|re)s?|miles?|mi|metres?|meters?|m)\b|"
    r"(?:预算|租金|月租)(?:以内|之内|范围内|内)|(?<![\d.])\d+\s*(?:度|℃)",
    re.IGNORECASE)


# "above"/"below" pointing at the answer itself compares nothing: "Hackney is the stronger pick
# on the numbers above" was withdrawn as a failed commute verdict (r06 099518b9).
_POSITIONAL_REFERENCE = re.compile(
    r"\b(?:(?:the|these|those|my|our)\s+(?:[\w'’-]+\s+){0,2}|"
    r"(?:see|shown|listed|mentioned|given|quoted|described|noted|set\s+out)\s+)(?:above|below)\b"
    r"(?=\s*(?:[,.;:!?)]|$|(?:and|but|or|so|for|is|are|was|were)\b))|"
    # "some of the above may have come from that" (r10 385542cc recall): "the above" is a noun.
    r"\b(?:of\s+)?the\s+(?:above|below)\b(?!\s*(?:\d|your\b|the\b|a\b|an\b|limit|cap|threshold|max))|"
    # "The 19-minute Stratford figure above comes from the separate journey calculation" (r17
    # 8c35fd5b) points at the answer's own figure, whatever verb follows.
    r"\b(?:figures?|numbers?|table|list|section|summary|breakdown|calculation|comparison|"
    r"itinerar(?:y|ies))\s+(?:above|below)\b(?!\s*(?:\d|your\b|the\b|a\b|an\b|limit|cap|threshold|max))",
    re.I)
# A bracketed list of categories names the commute as a TOPIC, it says nothing about one:
# "extracts durable details you mention (budget, areas, commute, needs)".
_TOPIC_LIST = re.compile(r"[（(](?![^()（）]*\d)[^()（）]*[,，、][^()（）]*[)）]")


# Walking DISTANCE is proximity, not a journey time: the offer "what's within walking distance
# of an address" after a commute clause was withdrawn on a weather turn (stress f-weather-unavail).
_PROXIMITY_PHRASE = re.compile(
    r"\b(?:within|in)\s+(?:(?:easy|short|comfortable|reasonable|a)\s+)?(?:walking|cycling)\s+distance\b",
    re.I)


def _without_money_or_weather(text: str) -> str:
    text = _MONEY_OR_WEATHER_SPAN.sub(" ", str(text or ""))
    return _PROXIMITY_PHRASE.sub(" ", _POSITIONAL_REFERENCE.sub(" ", text))


# A clause that says there IS no commute (none was set, none applies) asserts nothing about one.
# "your earlier search was a studio in Hackney with no commute." was deleted whole (R2-01 e).
# "no commute over 30 minutes" is NOT a denial: a comparative after it makes it a claim.
_COMMUTE_DENIAL = re.compile(
    r"\b(?:no|without(?:\s+(?:a|any))?)\s+commute\b(?!\s*(?:time\s+)?(?:over|under|above|below|"
    r"longer|shorter|more|less|beyond|within|exceed\w*|of\s+(?:over|under|more|less)))|"
    r"\bcommute\s+(?:(?:limit|filter|requirement|time|cap|constraint|destination|preference)\s+)?"
    r"(?:(?:was|is|were|has\s+been|had\s+been)\s+)?(?:not|never)\s+"
    r"(?:set|given|specified|applied|provided|mentioned|included|requested)\b|"
    r"\b(?:didn't|did\s+not|haven't|have\s+not|hasn't|has\s+not|never)\s+"
    r"(?:set|give|given|specif(?:y|ied)|mention(?:ed)?|include[d]?|appl(?:y|ied)|ask(?:ed)?\s+for)\s+"
    r"(?:(?:a|any|your)\s+)?commute\b|"
    r"没有(?:设置|设定|提供|指定)?通勤|无通勤|不考虑通勤|未(?:设置|设定|指定)通勤",
    re.IGNORECASE)


def _denies_commute(fragment: str) -> bool:
    """True for a figure-free clause saying no commute was set or applies."""
    text = str(fragment or "")
    return bool(not _MINUTE_TOKEN.search(text) and _COMMUTE_DENIAL.search(text))


def _asserts_commute(fragment: str) -> bool:
    """True when this fragment states a commute duration or verdict."""
    text = str(fragment or "")
    if _MINUTE_TOKEN.search(text):
        return True
    if _arithmetic.JOURNEY_COUNT.search(text) and _arithmetic.journey_counts(text):
        return True       # "(8 journeys)": a count the week must bear out (r17)
    # A travel expense within a monetary budget is not a travel-time verdict.
    if re.search(r"fare|\bcost\b|\bprice\b|票价|费用|交通费", text, re.I):
        return False
    text = _without_money_or_weather(text)
    if _denies_commute(text):
        return False
    if _GENERIC_COMMUTE_REFERENCE.search(text):
        # "flats within a certain budget or commute time" offers a kind of search; it
        # asserts nothing about any journey (r08 NEW-D).
        return False
    if _PRIORITY_CONDITION.match(text) or _ADVICE_CLAUSE.match(text) or _SPECULATION.search(text):
        # "If your own priority is a shorter journey rather than lower rent" states a
        # hypothetical preference of the user's, not a journey (r06 099518b9).
        return False
    # A bound in words is a verdict too: "a commute under a quarter of an hour", and so is
    # "about the same commute" as another place, and a time total in hours or words ("roughly
    # 3 hours a week on a five-day commute", r12 53bfec0e).
    return bool(_COMMUTE_WORD.search(text) and (_FIT_WORD.search(text) or _duration_bounds(text)
                                                or _SAME_WORD.search(text) or _period_total(text)))


def _period_total(text: str) -> bool:
    """A journey-time total stated in hours or words, or one per day/week."""
    text = str(text or "")
    return bool(_HOURS_AND_MINUTES.search(text) or (
        (_DAY_PERIOD.search(text) or _WEEK_PERIOD.search(text)) and _durations_in(text)))


# A figure-free possibility: "a particular listing in Bow could be closer to the station (or
# further)" (r15 452df7d4) says what may be so, it asserts no journey.
_SPECULATION = re.compile(r"\b(?:could|might|may)\s+(?:well\s+|also\s+|even\s+)?(?:be|sit|lie|end\s+up|turn\s+out)\b",
                          re.I)


# Figure-free advice on where to look: "and for Bow you would want to look at addresses closer
# to Bow Church DLR Station than the one measured here" (r11 d36a82c2) recommends, it measures
# nothing.
_ADVICE_CLAUSE = re.compile(
    r"^\s*(?:(?:and|but|so|or)\s+)?(?:(?:for|in)\s+[\w'’ -]{1,40}?,?\s+)?"
    r"(?:you|i|we)\s+(?:would|might|may|could|should)\s+(?:also\s+)?(?:want|need|like|prefer|do\s+well|have)?"
    r"\s*(?:to\s+)?(?:either\s+)?(?:look|search|consider|focus|try|check|aim|target|accept|choose|pick|find|"
    r"weigh|trade|compromise|live|go)\b", re.I)


# A figure-free condition on the USER's priorities ("If your priority is a shorter journey",
# "if a faster commute matters more to you").
_PRIORITY_CONDITION = re.compile(
    r"^\s*(?:(?:and|but|so|or)\s+)?(?:if|whether|should|when)\b[^.;!?]{0,80}?"
    r"\b(?:priorit(?:y|ies|ise|ize)|matters?|care\s+(?:more|most|about)|prefer\w*|value\s+\w+\s+more|"
    r"is\s+(?:more|most)\s+important)\b", re.I)


_GENERIC_COMMUTE_REFERENCE = re.compile(
    r"\b(?:within|under|below|inside)\s+(?:(?:a|an|some)\s+(?:certain|given|specific|chosen|particular)|any)"
    r"\s+(?:\w+\s+(?:or|and)\s+)?(?:commute|travel|journey)(?:\s+time)?\b", re.I)


def _honest_commute_reference(fragment: str, caps: set) -> bool:
    """Keep an explicit limitation or the user's target, never an unevidenced duration.

    Saying a 50-minute requirement remains unverified is not saying the trip takes 50
    minutes. An arbitrary 17-minute figure is still removed even inside a disclaimer.
    """
    values = _minute_claim_values(fragment)
    if any(value not in caps for value in values):
        return False
    limitation = re.search(
        r"cannot\s+(?:confirm|verify|establish|guarantee)|could\s+not\s+(?:confirm|verify)|"
        r"not\s+(?:confirmed|verified|guaranteed)|unverified|unknown|"
        r"无法(?:确认|核实|保证)|尚未(?:确认|核实)|未(?:确认|核实)|不能(?:确认|保证)",
        fragment, re.I)
    # An adversarial affirmative after 'but' is a separate assertion, not a limitation.
    if limitation and not re.search(r"\bbut\b|但是|但实际|不过", fragment, re.I):
        return True
    return bool(values and re.search(
        r"(?:your|user'?s|requested|target|required)\s+.{0,30}(?:limit|cap|target|requirement)|"
        r"(?:your|user'?s|requested|target|required)\s+.{0,12}(?:minutes?|mins?)|"
        r"(?:用户|你的|要求|目标).{0,20}(?:上限|限制|分钟)", fragment, re.I)
        # Naming the user's limit is honest; asserting the listing FITS it is a verdict.
        # "the commute is well inside your 30 minute limit" used to pass as a reference.
        and not re.search(r"takes?|returned|meets?|within|exceeds?|inside|below|beyond|outside|"
                          r"\bfits?\b|satisf(?:y|ies|ied)|qualif|comfortabl|符合|满足|超过|耗时|需时",
                          fragment, re.I))


def _preference_commute_reference(fragment: str, caps: set, *, context: bool = False) -> bool:
    """A saved/default time ceiling describes the user's plan, not a measured journey."""
    values = _minute_claim_values(fragment)
    if not values or any(value not in caps for value in values):
        return False
    if not (context or re.search(
            r"preference|default|remember|requirement|target|your\s+.{0,20}(?:limit|cap)|"
            r"偏好|默认|保存|记住|要求|上限", fragment, re.I)):
        return False
    if re.search(r"takes?|took|measured|returned|actually|verified|meets?|satisf(?:y|ies)|"
                 r"实测|耗时|需时|已核实|经核实|符合|满足", fragment, re.I):
        return False
    if (re.search(r"\b(?:is|are|was|were|has)\s+(?:(?:about|around|roughly)\s+)?\d+\s*(?:minutes?|mins?)|"
                  r"(?:是|需要)\s*\d+\s*分钟", fragment, re.I)
            and not re.search(r"(?:limit|cap|maximum|requirement|target|上限|要求|目标)\s*(?:is|为|是|[:：])\s*\d+|"
                              r"\b(?:max(?:imum)?|longest)\s+(?:commute|journey|travel)(?:\s+time)?\s+is\s+\d+",
                              fragment, re.I)):
        return False
    if re.search(r"\b(?:it|this|that|the\s+(?:flat|property|listing|journey|route|commute|trip))\b.{0,35}"
                 r"\b(?:within|under|inside|below|over|exceeds?)\b|"
                 r"(?:这套|该房源|该路线|此行程).{0,20}(?:以内|之内|超过|小于)", fragment, re.I):
        return False
    return True


# A label or clause that names the user's commute LIMIT ("Max commute:", "your maximum
# commute is"), as opposed to a journey.
_LIMIT_LABEL = re.compile(
    r"\b(?:max(?:imum)?|longest|limit\s+on\s+(?:the\s+|your\s+)?)\s*(?:commute|commuting|travel|journey)|"
    r"\b(?:commute|travel|journey)(?:\s+time)?\s+(?:limit|cap|ceiling|maximum)\b|最长通勤|通勤上限", re.I)


def _clean_commute_fragments(text: str) -> tuple[str, bool]:
    """Remove orphaned assertion/Markdown pieces, never a whole independent paragraph.

    A previous pass can leave a heading without its question, 'which is under …' without
    its measured subject, or a bare 'basis …' after its measurement was withdrawn. Those
    are not useful standalone assertions; callers can restore structured route facts.
    """
    if not (_COMMUTE_WORD.search(text) or _MINUTE_TOKEN.search(text) or "tfl_journey_plan" in text):
        return text, False
    lines = []
    changed = False
    for line in str(text or "").splitlines():
        body = re.sub(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)", "", line).strip()
        plain = body.strip("*# \t")
        # Evidence redaction may remove a duration while leaving its label and a
        # dependent assertion. Keep explicit unavailability statements intact.
        empty_duration = bool(re.match(
            r'^(?:(?:total\s+)?(?:journey|commute|travel)\s+time|通勤时间|总行程时间)'
            r'\s*[:：]\s*(?:(?:which|that is)\b|由\s*(?:TfL|交通)|$)', plain, re.I))
        empty_table_row = bool(re.fullmatch(r'\|\s*(?:commute|通勤)\s*\|\s*', plain, re.I))
        orphan = bool(re.match(
            r"^(?:which|and which|that is)\b.*(?:minute|cap|limit|within|under|over)|"
            r"^(?:basis|依据)\s*[:：\"'“\s]*(?:tfl_journey_plan|straight_line_estimate)|"
            r"^(?:No[.!]?\s*)?What is established\s*[:：]?$|"
            r"^Does this establish a reliable\s*$|^已确认这次只\s*$|"
            r"^It does not tell (?:you|us)[.!]?\s*$", plain, re.I))
        dependent_aside = _dependent_commute_aside(plain)
        broken_heading = (body.startswith(("**", "##")) and not plain.endswith(("?", "？", ":", "："))
                          and bool(re.search(r"\b(?:a|an|the|reliable|within|under|to|of|and)$", plain, re.I)))
        if orphan or broken_heading or dependent_aside or empty_duration or empty_table_row:
            changed = True
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip(), changed


def _dependent_commute_aside(fragment: str) -> bool:
    """A bare 'including walking legs ...' cannot stand after its assertion is removed."""
    return bool(re.match(r"^\s*including\b", fragment, re.I)
                and re.search(r"walking|interchanges?|transfers?|journey|route|travel", fragment, re.I)
                and not re.search(r"\b(?:is|are|was|were|means|matters|remains?|does|can|will)\b",
                                  fragment, re.I))


def _usable_artifact(artifact: Any) -> bool:
    return (isinstance(artifact, dict) and artifact.get("success") is True
            and not any(artifact.get(flag) for flag in (
                "timed_out", "denied", "abandoned", "outcome_unknown"))
            and isinstance(artifact.get("raw_data"), dict)
            and artifact["raw_data"].get("success") is not False)


def _measured_minutes(raw: dict) -> float | None:
    value = raw.get("duration_minutes")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    basis = raw.get("basis") or raw.get("route_source") or raw.get("source")
    if basis:
        from core.commute_basis import is_measured
        if not is_measured(basis):
            return None
    # Legacy calculate_commute artifacts omit the basis; that tool's duration_minutes
    # contract denotes measured time. Explicit estimate/unknown bases never gain this pass.
    return float(value)


# "King's Cross St. Pancras Underground Station" is what TfL resolves; "King's Cross St.
# Pancras" is what every answer writes. Without the bare form a route could never be
# attributed to the line that quotes it, and its measured minutes were deleted as
# unverifiable (2026-10-19 B-02: 9 of 16 measured answers lost their figure).
_TRANSPORT_SUFFIX = re.compile(
    r"\s+(?:(?:underground|rail(?:way)?|overground|dlr|tube|train|elizabeth[\s-]line|bus|coach)"
    r"\s+)?(?:station|stop)$", re.I)


def _journey_end_label(value: Any) -> tuple[str, str | None] | None:
    """(resolved station, the address it stands in for) when the planner substituted a
    station for the requested place, else None."""
    if not isinstance(value, dict):
        return None
    station = str(value.get("resolved_station") or "").strip()
    asked = str(value.get("input") or "").strip()
    if not station or _label_key(station) == _label_key(asked):
        return None
    return station, asked or None


def _render_end(end: tuple[str, str | None], zh: bool) -> str:
    station, asked = end
    if not asked:
        return station
    return f"{station}（{asked} 的就近车站）" if zh else f"{station} (nearest station to {asked})"


# A destination the answer names by its usual short form. "从 Limes Avenue 到 UCL 约 70 分钟"
# quoted calculate_commute's measured 70 minutes to "University College London, Gower
# Street …", but "UCL" was no label of that binding, so the evidenced total was withdrawn
# and the answer said only "较长通勤" (R1-10).
_PLACE_ALIASES = (
    ("university college london", ("UCL", "伦敦大学学院")),
    ("king's college london", ("KCL", "伦敦国王学院", "国王学院")),
    ("london school of economics", ("LSE", "伦敦政经", "伦敦政治经济学院")),
    ("imperial college london", ("Imperial College", "帝国理工", "帝国理工学院")),
    ("queen mary university of london", ("QMUL", "Queen Mary", "伦敦玛丽女王大学")),
    ("school of oriental and african studies", ("SOAS", "亚非学院")),
)


def _place_labels(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = [value.get("input"), value.get("resolved_station"), value.get("name")]
    else:
        values = [value]
    labels = []
    for item in values:
        text = str(item or "").strip()
        if text:
            labels.extend([text, text.split(",", 1)[0].strip()])
            key = _label_key(text)
            for full, short in _PLACE_ALIASES:
                if _label_key(full) in key or any(_label_key(s) == key for s in short):
                    labels.extend((full.title(),) + short)
    for label in list(labels):
        bare = _TRANSPORT_SUFFIX.sub("", label).strip()
        if bare != label and len(bare) >= 4:
            labels.append(bare)
    return list(dict.fromkeys(label for label in labels if label))


def _label_key(text: Any) -> str:
    """Punctuation/case-insensitive form for place-label containment.

    "King’s Cross St Pancras", "King's Cross St. Pancras" and "kings cross st pancras" are
    one place; a raw substring test treated them as three."""
    lowered = str(text or "").lower().replace("’", "").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", lowered).split())


def _key_in(key: str, hay: str) -> bool:
    """Whole-word containment of two ``_label_key`` forms (CJK: substring)."""
    if not key:
        return False
    if re.search(r"[\u4e00-\u9fff]", key):
        return key in hay
    return f" {key} " in f" {hay} "


_POSSESSIVE = re.compile(r"(?<=[A-Za-z])['’]s\b")


def _label_in(label: Any, text: Any) -> bool:
    """True when ``label`` occurs in ``text`` as whole words (CJK: as a substring).

    A possessive names the place too: "Islington's 15-minute journey" is about Islington,
    but its key "islingtons" did not contain "islington", so the clause bound to nothing
    and its evidenced figure was withdrawn (r06 ecbf2c1f). The plain key is tried first,
    so "King's Cross" still matches "Kings Cross"."""
    key = _label_key(label)
    if _key_in(key, _label_key(text)):
        return True
    text = str(text or "")
    return bool(("'" in text or "’" in text)
                and _key_in(key, _label_key(_POSSESSIVE.sub("", text))))


def _route_legs(journey: dict) -> list[dict]:
    """``[{minutes, modes, places}]`` for each measured leg of a TfL itinerary.

    A leg's own duration is part of the same measurement as the total; an answer that lists
    "Northern line to Moorgate (6 min)" under a 23-minute journey is quoting the evidence,
    not inventing a figure. A leg is identified by its LINE or MODE ("Northern", "walk"),
    never by its arrival place alone: the last leg arrives at the destination, so a place
    label would let "Stratford to Canary Wharf: 8 minutes" borrow the final walk."""
    legs = []
    for leg in journey.get("route_legs") or []:
        if not isinstance(leg, dict):
            continue
        value = leg.get("duration_minutes")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        modes = [str(line) for line in leg.get("lines") or [] if str(line or "").strip()]
        if str(leg.get("mode") or "").lower() == "walking":
            modes.extend(("walk", "步行", "走路"))
        if not modes:
            continue
        legs.append({"minutes": float(value),
                     "modes": [_label_key(m) for m in dict.fromkeys(modes)],
                     "places": [_label_key(p) for p in _place_labels(leg.get("to"))],
                     "origins": [_label_key(p) for p in _place_labels(leg.get("from"))]})
    return legs


def _endpoint_keys(binding: dict | None) -> list[str]:
    return [k for k in (_label_key(label) for label in
                        list((binding or {}).get("labels") or [])
                        + list((binding or {}).get("destinations") or [])) if k]


def _named_legs(text: str, binding: dict | None) -> list[dict]:
    """The route legs whose line or mode ``text`` names.

    A line named like an endpoint does not count: on Brixton -> Victoria "It takes 8
    minutes to Victoria" names the destination, not the Victoria line's 8-minute leg."""
    legs = (binding or {}).get("legs") or []
    if not legs:
        return []
    hay = _label_key(text)
    endpoints = _endpoint_keys(binding)
    return [leg for leg in legs
            if any(_key_in(mode, hay) and not any(mode == e or _key_in(mode, e) for e in endpoints)
                   for mode in leg["modes"])]


def _legs_at_named_places(legs: list, text: str) -> list:
    """Of ``legs``, those that start or end at a place ``text`` names, when it names any: "the
    13-minute walk to Bow Road" is the walk TO Bow Road (13), not the walk at the Liverpool
    Street end (6); "versus 4 minutes at Stratford" is the walk at Stratford (r17). A text that
    names no leg's place leaves every leg it names by mode."""
    hay = _label_key(text)
    placed = [leg for leg in legs
              if any(_key_in(place, hay) for place in (leg.get("places") or []) + (leg.get("origins") or []))]
    return placed or legs


def _legs_named_at(text: str, binding: dict | None, places_in: str | None = None) -> list[dict]:
    """The legs ``text`` names by line or mode, narrowed to the places ``places_in`` (the claim
    itself, never the line's label: "- **Stratford: 19 minutes.** … 6 min walk at the other
    end" names the route, not Stratford station) names."""
    return _legs_at_named_places(_named_legs(text, binding), text if places_in is None else places_in)


def _arrives_at_destination(leg: dict, binding: dict) -> bool:
    destinations = [k for k in (_label_key(d) for d in binding.get("destinations") or []) if k]
    return any(d in place or place in d for place in leg["places"] for d in destinations)


def _itinerary_bindings(text: str, bindings: list[dict]) -> set:
    """ids of the routes whose legs ``text`` ENUMERATES (two or more named by line/mode).

    Only inside such an itinerary may a leg that ends at the destination quote its own
    figure ("..., Elizabeth line to Canary Wharf (7 min), then walk (5 min)"). Alone,
    "It takes 17 minutes on the Jubilee line to Canary Wharf" is a whole-trip claim."""
    return {id(binding) for binding in bindings
            if len(_named_legs(text, binding)) >= 2}


def _estimate_minutes(journey: dict) -> list[float]:
    """The estimate and its stated range, when the tool returned an ESTIMATE, else []."""
    values = []
    for key in ("estimate_low_minutes", "estimated_duration_minutes", "estimate_high_minutes"):
        value = journey.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values.append(float(value))
    return values


def _listing_bindings(validation: dict) -> list[dict]:
    """``[{labels, minutes}]`` for EVERY listing in the ledger; ``minutes`` is None unless
    its commute is positively evidenced.

    A measured journey remains a fact even when rent or another condition excludes the
    listing. The linked successful measurement supports that figure, not eligibility.

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
        labels = list(dict.fromkeys(label for value in (
            candidate.get("address"), candidate.get("Address"), candidate.get("name"))
            for label in _place_labels(value)))
        if not labels:
            continue
        minutes = candidate.get("verified_commute_minutes")
        value = _measured_minutes({"duration_minutes": minutes})
        evidenced = status.get("evidence_status") == "success" and value is not None
        bindings.append({"labels": labels,
                         "minutes": value if evidenced else None,
                         "destinations": _place_labels(
                             (validation.get("constraints") or {}).get("commute_destination"))})
    return bindings


_SEARCH_TRAVEL_TIME = re.compile(
    r"^\s*~?\s*(\d{1,3})\s*min(?:ute)?s?\s+to\s+(.+?)(?:\s*\((?:estimated|unverified)[^)]*\))?\s*$", re.I)


def _bind_search_travel_times(bindings: list[dict], artifacts: list,
                              shown_rows: list[dict] | tuple = ()) -> None:
    """Per-listing commute evidence that search_properties itself returned (r08 NEW-B).

    A row's ``travel_time`` ("28 min to Liverpool Street", source routing) is that
    listing's measured figure; an estimated one ("~18 min ... (estimated, unverified)") is
    quotable only as an estimate. Every commute cell of a 7-listing table shipped as "—"
    with the Note, though each figure equalled the listing's own travel_time. Bound per
    listing, so one listing's minutes never license another's."""
    for artifact in artifacts:
        if not (isinstance(artifact, dict) and artifact.get("tool") == "search_properties"
                and _usable_artifact(artifact)):
            continue
        raw = artifact["raw_data"]
        rows = [r for key in ("recommendations", "over_budget_alternatives")
                for r in (raw.get(key) or []) if isinstance(r, dict)]
        for row in rows:
            _bind_listing_travel_time(bindings, row)
    # The listings the user is LOOKING AT come from an earlier turn's search, and the server
    # kept that search's rows (``view_results``). "Show details of the cheapest one" answered
    # "Commute: 35 min to Canary Wharf" with Kingsland Road's own routed 35 minutes, and it was
    # withdrawn because this turn ran only a details lookup (r07 97b3d456). Only the server's
    # copy of the tool rows counts, never model prose, and a listing this turn's search already
    # bound keeps this turn's figure.
    for row in shown_rows:
        labels = _place_labels(row.get("address")) + _place_labels(row.get("provider_address"))
        if any(b.get("listing") and set(b.get("labels") or []) & set(labels) for b in bindings):
            continue
        _bind_listing_travel_time(bindings, row)


def _shown_listing_rows(state: dict) -> list[dict]:
    """The server-held rows of the listings an earlier turn showed (never model prose).

    Only while the commute they were measured for is still the user's commute: a row whose
    travel_time goes to another destination, or any row once the commute was dropped, is
    history, not evidence for this answer."""
    criteria = state.get("accumulated_search_criteria") or {}
    current = criteria.get("commute_destination") or criteria.get("destination")
    if criteria.get("no_commute") is True or not current:
        return []
    current_keys = [_label_key(label) for label in _place_labels(current)]
    context = state.get("extracted_context") or {}
    for key in ("view_results", "last_results_full"):
        rows = context.get(key)
        if not (isinstance(rows, list) and rows):
            continue
        kept = []
        for row in rows[:50]:
            match = _SEARCH_TRAVEL_TIME.match(str(row.get("travel_time") or "")) \
                if isinstance(row, dict) else None
            if not match:
                continue
            target = _label_key(match.group(2))
            if any(_key_in(key_, target) or _key_in(target, key_) for key_ in current_keys):
                kept.append(row)
        return kept
    return []


def _bind_listing_travel_time(bindings: list[dict], row: dict) -> None:
    match = _SEARCH_TRAVEL_TIME.match(str(row.get("travel_time") or ""))
    if not match:
        return
    minutes = float(match.group(1))
    destination = match.group(2).strip()
    measured = str(row.get("travel_time_source") or "").lower() in {
        "routing", "tfl", "tfl_journey_plan", "measured", "journey_planner"}
    labels = list(dict.fromkeys(_place_labels(row.get("address"))
                                + _place_labels(row.get("provider_address"))))
    if not labels:
        return
    existing = next((b for b in bindings if not b.get("members")
                     and set(b.get("labels") or []) & set(labels)), None)
    if existing is None:
        existing = {"labels": labels, "minutes": None, "destinations": []}
        bindings.append(existing)
    existing["listing"] = True
    if not existing.get("destinations"):
        existing["destinations"] = _place_labels(destination)
    if measured and existing.get("minutes") is None:
        existing["minutes"] = minutes
    elif not measured and existing.get("minutes") is None and not existing.get("estimate"):
        existing["estimate"] = [minutes]


def _response_commute_bindings(state: dict) -> tuple[list[dict], dict | None]:
    """Combine stale candidate ledgers with this turn's explicitly bound route results.

    A successful station/area journey is useful even when the candidate list has no routes;
    it is not evidence for an apartment merely because both are in the same town.
    """
    bindings = _listing_bindings(state.get("candidate_validation") or {})
    _bind_search_travel_times(bindings, state.get("tool_artifacts") or [],
                              shown_rows=_shown_listing_rows(state))
    routes = []
    for artifact in state.get("tool_artifacts") or []:
        if not _usable_artifact(artifact):
            continue
        raw = artifact["raw_data"]
        tool = artifact.get("tool")
        if tool == "get_property_details":
            prop = raw.get("property") or {}
            labels = _place_labels(prop.get("address"))
            if labels and not any(set(labels) & set(b["labels"]) for b in bindings):
                bindings.append({"labels": labels, "minutes": None, "destinations": []})
            continue
        if tool == "compare_or_rank_areas":
            for row in raw.get("areas") or []:
                if not isinstance(row, dict):
                    continue
                sources = {_norm(source) for source in row.get("sources") or []}
                value = row.get("commute_minutes")
                measured = (isinstance(value, (float, int)) and not isinstance(value, bool)
                            and math.isfinite(value) and value >= 0
                            and bool(sources & {"commute routing", "通勤路由估算"}))
                bindings.append({"labels": _place_labels(row.get("name")) +
                                 _place_labels(str(row.get("slug") or "").replace("-", " ")),
                                 "minutes": float(value) if measured else None,
                                 "destinations": [], "area_row": True})
            continue
        if tool not in {"calculate_commute", "calculate_commute_cost", "get_transport_info"}:
            continue
        if tool == "get_transport_info" and raw.get("query_type") not in {"journey", "fare"}:
            continue
        journey = raw.get("commute") if tool == "calculate_commute_cost" else raw
        if not isinstance(journey, dict):
            continue
        value = _measured_minutes(journey)
        estimate = _estimate_minutes(journey) if value is None else []
        if value is None and not estimate:
            continue
        binding = {"labels": _place_labels(raw.get("from_address") or raw.get("from")),
                   "destinations": _place_labels(raw.get("to_address") or raw.get("to")),
                   "minutes": value, "mode": _arithmetic.route_mode(raw.get("mode"))}
        legs = _route_legs(journey) if value is not None else []
        # The planner's CHEAPER itinerary for the same pair is part of the same result: "A
        # cheaper alternative takes 45 minutes with one change" and "Cheapest route, £29.00:
        # about 47 minutes" quoted it and lost the figure (R2-25).
        alternative = raw.get("cheapest_journey") if tool == "get_transport_info" else None
        if value is not None and isinstance(alternative, dict):
            alt_value = _measured_minutes(alternative)
            if alt_value is not None and alt_value != value:
                binding["alt_minutes"] = [alt_value]
                legs = legs + _route_legs(alternative)
        if legs:
            binding["legs"] = legs
        if estimate:
            # An estimate is never a measurement, but quoting it AS an estimate — the range
            # the tool told the model to give — is honest; deleting it left "Estimated public
            # transport time:" with no figure (2026-10-19 C-08).
            binding["estimate"] = estimate
        # The same origin from calculate_commute and a journey/fare lookup is one route,
        # not ambiguous evidence. Different destinations or different measurements remain
        # separate and cannot license an unqualified claim. A straight-line ESTIMATE and a
        # measurement of the same pair are one route too: the measurement is its time and the
        # estimate stays quotable as an estimate. Kept apart, the two equally specific
        # bindings made every line about the pair "ambiguous" and its measured 14 minutes was
        # withdrawn (R2-01 b).
        equivalent = next((prior for prior in routes
                           if (prior["minutes"] == value or prior["minutes"] is None
                               or value is None)
                           and set(map(_norm, prior["labels"])) & set(map(_norm, binding["labels"]))
                           and set(map(_norm, prior["destinations"])) &
                           set(map(_norm, binding["destinations"]))), None)
        if equivalent is not None:
            equivalent["labels"] = list(dict.fromkeys(equivalent["labels"] + binding["labels"]))
            equivalent["destinations"] = list(dict.fromkeys(
                equivalent["destinations"] + binding["destinations"]))
            if equivalent["minutes"] is None and value is not None:
                equivalent["minutes"] = value
                if legs:
                    equivalent["legs"] = legs
                if binding.get("alt_minutes"):
                    equivalent["alt_minutes"] = binding["alt_minutes"]
            if estimate:
                equivalent["estimate"] = sorted(set(equivalent.get("estimate") or []) | set(estimate))
        elif not any(binding == prior for prior in routes):
            routes.append(binding)

    for route in routes:
        for binding in bindings:
            if any(binding is other for other in routes):
                continue          # routes were merged above; only listings take a route
            if _same_area_route(binding, route):
                # compare_or_rank_areas ranked "Bow" at 27 and calculate_commute measured "Bow,
                # London" -> Liverpool Street at 27: one route, not two. Kept apart, the two
                # equally specific "Bow" bindings made every Bow line ambiguous, and the header
                # "Bow — 27 minutes each way" and its leg list went whole (r17 8c35fd5b).
                route["labels"] = list(dict.fromkeys(route["labels"] + binding["labels"]))
                if binding.get("minutes") is not None and binding["minutes"] != route["minutes"]:
                    route["place_alternatives"] = sorted(set(route.get("place_alternatives") or [])
                                                         | {binding["minutes"]})
                    route["alt_minutes"] = sorted(set(route.get("alt_minutes") or []) | {binding["minutes"]})
                binding.pop("area_row", None)
                binding.update(route)
                continue
            if (binding.get("minutes") is None and route["labels"]
                    and _norm(route["labels"][0]) == _norm(binding["labels"][0])
                    and (not binding.get("destinations") or not route["destinations"]
                         or set(map(_norm, binding["destinations"])) &
                         set(map(_norm, route["destinations"])) )):
                binding.update(route)
        if not any(route == binding for binding in bindings):
            bindings.append(route)

    distinct = {(tuple(map(_norm, r["labels"])), tuple(map(_norm, r["destinations"])),
                 r["minutes"]) for r in routes}
    # Two tools may name the same station slightly differently. A default is allowed only
    # for a genuinely single origin/destination/measurement, or the legacy nameless result.
    default = routes[0] if len(distinct) == 1 else None
    if default is not None and (state.get("candidate_validation") or {}).get("statuses"):
        current = str((state.get("extracted_context") or {}).get("current_message")
                      or state.get("user_query") or "").lower()
        listing_bindings = _listing_bindings(state.get("candidate_validation") or {})
        names_listing = any(label.lower() in current for binding in listing_bindings
                            for label in binding["labels"])
        names_route = any(label.lower() in current for label in default["labels"])
        same_listing = any(set(map(_norm, binding["labels"])) &
                           set(map(_norm, default["labels"])) for binding in listing_bindings)
        if not same_listing and (names_listing or not names_route):
            default = None
    return bindings, default


_THE_LISTING = re.compile(r"\b(?:the|this|that)\s+(?:flat|listing|property|apartment|home|house|studio)\b", re.I)


def _the_listing_referred_to(fragment: str, inherited: dict | None, local: dict | None,
                             previous: str, bindings: list) -> dict | None:
    """"That's a measured journey from the Islington area rather than from the King Square flat
    specifically, so treat the flat's own ~20 minutes as an estimate" (r17 4f27cfd2): a clause
    that names nothing itself but refers to "the flat" is about the ONE listing the clause
    before it named, not the route that clause was about."""
    if local is not None or not _THE_LISTING.search(fragment) or (inherited or {}).get("listing"):
        return inherited
    named = _line_binding(previous, bindings) if previous.strip() else None
    pool = (named or {}).get("members") or ([named] if named else []) or (inherited or {}).get("members") or []
    listings = [member for member in pool if member.get("listing")]
    return listings[0] if len(listings) == 1 else inherited


_SAVING_HEADER = re.compile(
    r"\b(?:time\s+saved|saved|saves?|saving|faster|quicker|slower|difference|gap|versus|vs\.?|compared)\b", re.I)


# The user's own limit, restated as a criterion: "Your criteria are saved: studio, up to
# £2,000/month, within 25 minutes of Old Street." (r19 a81297be) — nothing is said to BE within it.
_BARE_LIMIT = re.compile(
    r"^\s*(?:and\s+|with\s+|plus\s+)?(?:a\s+commute\s+(?:of\s+)?)?(?:within|under|up\s+to|inside|no\s+more\s+than|"
    r"at\s+most|max(?:imum)?(?:\s+of)?)\s+(?:about\s+)?\d{1,3}\s*(?:minutes?|mins?)(?:['’]?\s+(?:commute|travel))?"
    r"(?:\s+(?:of|to|from)\s+(?P<place>[^.,;!?]+?))?\s*$", re.I)
_WORKPLACE_WORDS = re.compile(r"^(?:(?:your\s+|the\s+)?(?:work|office|workplace|job|campus|university|school))$", re.I)


def _restates_user_limit(fragment: str, bindings: list, user_destinations: list | None) -> bool:
    """A clause that is ONLY the user's limit, to the user's own destination (or "work"), with no
    listing, area or route as its subject. ``_preference_commute_reference`` still requires the
    figure to be the user's cap and still rejects "this flat … within …"."""
    match = _BARE_LIMIT.match(str(fragment or ""))
    if not match:
        return False
    place = (match.group("place") or "").strip()
    if place and not _WORKPLACE_WORDS.match(place) and not any(
            _label_key(label) and _label_key(label) == _label_key(place) for label in user_destinations or []):
        return False      # "within 25 minutes of Bank" when the user works at Old Street
    named = _line_binding(fragment, bindings)
    return named is None or not (named.get("labels") or named.get("members"))


def _user_commute_destinations(state: dict) -> list:
    criteria = state.get("accumulated_search_criteria") or {}
    labels: list = []
    for key in ("commute_destination", "destination"):
        labels.extend(_place_labels(criteria.get(key)))
    return list(dict.fromkeys(labels))


def _per_period_line(line: str, fragment: str) -> bool:
    """The line is a per-day/week/month figure ("- Per office day: …", "Per week (3 office
    days): …") or the clause itself names a period."""
    label = line.split(":", 1)[0] if ":" in line[:80] else ""
    scope = f"{label} {fragment}"
    return bool(_DAY_PERIOD.search(scope) or _WEEK_PERIOD.search(scope)
                or _arithmetic.LONG_PERIOD.search(_arithmetic.without_money(scope)))


def _same_area_route(area: dict, route: dict) -> bool:
    """An area-ranking row and a measured route from that same place: one route. The route
    (address/station level) is the more specific measurement and the line's binding; a
    different ranking time is its alternative, so "Bow — 27 minutes" (the route) and "Bow (28
    min)" (the ranking) both hold and nothing about Bow is ambiguous (r21 8f4f0a00)."""
    return bool(area.get("area_row") and route.get("minutes") is not None and route.get("labels")
                and set(map(_norm, area.get("labels") or [])) & set(map(_norm, route["labels"])))


def _line_binding(line: str, bindings: list[dict], default: dict | None = None) -> dict | None:
    """The verified listing this line is about, if it names exactly one.

    Longest match wins, because listing labels nest: "Park Drive" is a substring of "Park
    Drive London E14", so first-match binding attributed the long address's line to the short
    address's evidence and published one listing's minutes for another. When two DIFFERENT
    listings match equally well the line cannot be attributed at all, and an unattributable
    line has no evidence — it fails closed rather than borrowing someone else's.
    """
    best = None
    best_len = (0, 0)
    ambiguous = False
    tied: list = []                 # the bindings sharing the best score
    named = []
    named_bindings = []
    for binding in bindings:
        matched = max((len(label) for label in binding["labels"]
                       if label and _label_in(label, line)), default=0)
        if not matched:
            continue
        matched_labels = [label.lower() for label in binding["labels"]
                          if label and _label_in(label, line)]
        named.append(max(matched_labels, key=len))
        named_bindings.append(binding)
        destinations = binding.get("destinations") or []
        explicit_destination = _TARGET_WITH_FROM.search(_without_abbreviation_dots(line))
        # Only a NAMED place switches the route: "The 19-minute Stratford figure above comes
        # from the separate journey calculation" travels nowhere, and reading "the separate
        # journey calculation" as an origin unbound Stratford's own 19 (r17 8c35fd5b).
        if destinations and explicit_destination and _names_a_place(explicit_destination.group(1)):
            if not _names_route_place(explicit_destination.group(1), binding, line):
                continue
        # Origin specificity comes first. A long destination name must never let a town
        # route outrank an unmeasured, more specific property address in that town.
        score = (matched, max((len(label) for label in destinations
                               if _label_in(label, line)), default=0))
        if score > best_len:
            best, best_len, ambiguous, tied = binding, score, False, [binding]
        elif score == best_len and binding is not best and binding != best:
            ambiguous = True
            tied.append(binding)
    independent = [label for label in named if not any(
        label != other and label in other for other in named)]
    listing_hits = [b for b in named_bindings if b.get("listing")]
    if len(set(independent)) > 1 and len({id(b) for b in listing_hits}) == 1:
        # A listing row names its own area ("| Jacob House, E8 | Hackney | £1,950 | 28 min |"):
        # the area's route measured in the same turn does not make the row ambiguous; the
        # one listing it names owns the row (r08 NEW-B).
        return listing_hits[0]
    if len(set(independent)) > 1:
        return {"labels": [], "minutes": None, "destinations": [], "members": named_bindings}
    if ambiguous:
        # The same place measured by bike and by public transport is two journeys, told apart
        # by the mode the line names; a line naming none is about the public-transport one (r6b).
        modes = [b.get("mode") or "transit" for b in tied]
        if len(set(modes)) == len(modes):
            wanted = _arithmetic.figure_mode(line) or "transit"
            chosen = [b for b in tied if (b.get("mode") or "transit") == wanted]
            if len(chosen) == 1:
                return chosen[0]
        return {"labels": [], "minutes": None, "destinations": []}
    if best is not None:
        return best
    # A single returned route never licenses a figure for a newly named place or a switched
    # destination; those checks run per clause ("It takes 23 minutes to Stratford" fails).
    if default is not None:
        # The turn returned ONE route and this line names no other entity, so the line is
        # about that route — whatever its grammatical subject. Requiring an "It takes…" or
        # "The journey…" subject deleted the measured 23 minutes from "Fastest option: 23
        # minutes, 1 change" and the 41-minute estimate's range from "What I can give you is
        # an estimate: roughly 34 to 51 minutes" (R2-01 a/c). Nothing is licensed by this
        # alone: every clause still has to quote the route's own figure, travel to one of its
        # own places (``_targets_on_route``) and draw the verdict its figure supports.
        return default
    return None


# The place a clause travels TO. An infinitive is not a destination: "leave at 08:35 to
# arrive by 09:00" named no place, but read as one it failed every route (B-02).
# A number is not a place either: "roughly 34 to 51 minutes" is a range (R2-01 c).
_NOT_A_PLACE_AFTER_TO = (r"(?!\s+(?:arrive|get|reach|be|make|catch|leave|travel|go|walk|"
                         r"change|work|study|commute|allow|avoid|spend|save|pay|check|spare)\b)"
                         r"(?!\s*[~≈]?\s*\d)")
def _without_abbreviation_dots(text: str) -> str:
    """"St. Pancras" -> "St Pancras": a target clause ends at a full stop, not a title."""
    return re.sub(r"\b(" + _NAME_ABBREVIATION + r")\.", r"\1", str(text or ""))


_TARGET = re.compile(r"(?:\bto\b" + _NOT_A_PLACE_AFTER_TO + r"|至|到)\s*([^,.;，。；\n]+)", re.I)
_TARGET_WITH_FROM = re.compile(
    r"(?:\bto\b" + _NOT_A_PLACE_AFTER_TO + r"|\bfrom\b(?=\s+\S)|至|到)\s*([^,.;，。；\n]+)", re.I)


def _names_route_place(target_text: str, binding: dict, scope: str) -> bool:
    """True when ``target_text`` (what a clause travels TO) is a place this route reaches.

    That is the route's destination, or the arrival of a leg that ``scope`` itself
    describes by line or mode ("Northern line ... to Moorgate"). Never the origin: "from
    King's Cross it is 23 minutes to Stratford" names the origin and a DIFFERENT
    destination. Never an interchange on its own: "30 minutes to Moorgate" is not a leg.
    """
    hay = _label_key(target_text)
    if any(_key_in(_label_key(label), hay) for label in binding.get("destinations") or []):
        return True
    if re.search(r"\bline\b|\bdlr\b|\boverground\b|\bthameslink\b", hay) and any(
            _key_in(_label_key(mode), hay) for leg in binding.get("legs") or []
            for mode in leg.get("modes") or [] if mode not in ("walk", "步行", "走路")):
        # One of the route's own LINES: "puts you about 4 minutes from the Jubilee line"
        # travels to nowhere off the route (r11 d36a82c2).
        return True
    return any(_key_in(place, hay) for leg in _named_legs(scope, binding)
               for place in leg["places"])


def _names_a_place(text: str) -> bool:
    """A travel target is a NAMED place: a capitalised word or CJK. "Walk to the exit (5
    min)" and "walk to your office" travel to no place a route could fail to reach."""
    plain = re.sub(r"[（(][^()（）]{0,200}[)）]", " ", str(text or ""))
    return bool(re.search(r"(?<![A-Za-z'’])[A-Z]|[\u4e00-\u9fff]", plain))


_ORIGIN_CLAUSE = re.compile(r"\bfrom\s+(?=\S)([^,.;，。；\n]+)", re.I)
_NOT_AN_ORIGIN = re.compile(r"^\s*(?:door|start|end|the\s+(?:station|stop|platform|door)|here|there)\b",
                            re.I)


def _targets_on_route(fragment: str, binding: dict, scope: str) -> bool:
    """True unless ``fragment`` travels TO, or FROM, a place this route does not cover.

    The destination side is ``_names_route_place``. The origin side may name the route's own
    origin: "From King's Cross it takes 23 minutes." quoted the planner's 23 minutes for
    King's Cross -> Canary Wharf, but the old single-pattern check read the origin as a
    destination and withdrew it (R2-01). "From Stratford it takes 23 minutes" still fails."""
    text = _without_abbreviation_dots(fragment)
    target = _TARGET.search(text)
    if (target and _names_a_place(target.group(1))
            and not _names_route_place(target.group(1), binding, scope)):
        return False
    origin = _ORIGIN_CLAUSE.search(text)
    if origin:
        place = re.split(r"\bto\b|至|到", origin.group(1), maxsplit=1, flags=re.I)[0]
        if (_names_a_place(place) and not _NOT_AN_ORIGIN.match(place)
                and not re.match(r"\s*[~≈]?\s*\d", place)):
            hay = _label_key(place)
            # The route's origin, or any stop one of its legs departs from or arrives at
            # ("Elizabeth line from Liverpool Street to Canary Wharf").
            if not (_names_route_place(place, binding, scope)
                    or any(_key_in(_label_key(label), hay)
                           for label in binding.get("labels") or [])
                    or any(_key_in(stop, hay)
                           for leg in binding.get("legs") or []
                           for stop in (leg.get("origins") or []) + (leg.get("places") or []))):
                return False
    return True


_WHOLE_TRIP_WORD = re.compile(
    r"\b(?:journey|commute|trip|total|overall|door|whole|altogether|in\s+all)\b|全程|总|通勤",
    re.I)
# "~28 min" is how search_properties itself renders an estimate ("~28 min to Liverpool Street
# (estimated, unverified)"); quoting it with the tilde quotes it as one (r08 ff6c228f).
_ESTIMATE_MARKER = re.compile(
    r"(?<![\w~])~\s*\d|≈\s*\d|"
    r"\bestimat\w*|\bapprox\w*|\broughly\b|\brough\b|\brange\b|\bnot\s+(?:a\s+)?measured\b|"
    r"\bstraight[-\s]line\b|估计|估算|预计|大约|约|直线距离", re.I)


def _estimate_is_quoted_as_estimate(fragment: str, binding: dict, caps: set,
                                    context: str = "", sentence_marked: bool = False) -> bool:
    """An unmeasured route's ESTIMATE may be quoted only as an estimate, never as a verdict.

    The tool says exactly this ("give it as an estimated 16-24 minute range ... never state
    '19 minutes' as the commute"), and deleting the range left a heading with no figure and
    "the lower end of that range" pointing at nothing (2026-10-19 C-08). The estimate
    marker must be in the claim itself (``context`` is only the clause a parenthetical is
    attached to), and only the tool's own figures are quotable — a user's cap is not an
    estimate.

    ``sentence_marked`` says the SENTENCE the claim sits in carries a marker. "What I can give
    you is an estimate: 34 to 51 minutes" says it is an estimate one clause earlier, in the
    same sentence; that is still quoting it as one (R2-01 c). A marker in ANOTHER sentence
    ("The commute takes 19 minutes. Rent is approximately £1,500.") still licenses nothing.
    It is a flag, computed once per sentence: searching the sentence again for each of its
    clauses would be quadratic in a long one (the B-01 class)."""
    estimate = binding.get("estimate") or []
    values = _minute_claim_values(fragment)
    if not estimate or not values:
        return False
    if not (sentence_marked or _ESTIMATE_MARKER.search(fragment)
            or _ESTIMATE_MARKER.search(context)):
        return False
    if _PASS_VERDICT.search(fragment) or _FAIL_VERDICT.search(fragment):
        return False      # an estimate cannot establish that a limit is met or missed
    return all(any(abs(value - permitted) < 1e-9 for permitted in estimate) for value in values)


# Each figure of a minute LIST: "31 and 14 minutes", "31, 14 or 41 min". ``_MINUTE_VALUE`` sees
# only the last one, the one the unit is attached to.
# Every gap is bounded, so a long blank run costs O(1) per figure (the B-01 class).
_MINUTE_LIST_VALUE = re.compile(
    r"(?<![\d.£$])(\d{1,3}(?:\.\d{1,2})?)(?=(?:[ \t]{0,3}(?:,|and|&|/|or)[ \t]{0,3}"
    r"\d{1,3}(?:\.\d{1,2})?){0,8}[ \t]{0,3}[-–]?[ \t]{0,3}(?:minutes?|mins?\b|分钟))", re.IGNORECASE)


_ROUTE_COMPARATIVE = re.compile(
    r"\b(?:slower|faster|quicker|longer|shorter)\b|更慢|更快|更长|更短", re.I)


def _compares_own_routes(fragment: str, values: list, binding: dict) -> bool:
    """"A slightly slower alternative is about 40 minutes" compares two itineraries the
    planner returned for this route (37 and 40 minutes); it is no verdict on a limit (R3-01).

    Holds only when the only verdict words are comparatives, every figure is one of the
    route's own whole-trip measurements, and the comparison points the right way."""
    figures = [binding.get("minutes")] + list(binding.get("alt_minutes") or [])
    figures = [f for f in figures if isinstance(f, (int, float))]
    if not values or len(set(figures)) < 2:
        return False
    rest = _ROUTE_COMPARATIVE.sub(" ", fragment)
    if _PASS_VERDICT.search(rest) or _FAIL_VERDICT.search(rest):
        return False
    if not all(any(abs(v - f) < 1e-9 for f in figures) for v in values):
        return False
    slower = re.search(r"\b(?:slower|longer)\b|更慢|更长", fragment, re.I)
    faster = re.search(r"\b(?:faster|quicker|shorter)\b|更快|更短", fragment, re.I)
    if slower and faster:
        return False
    if slower:
        return all(v > min(figures) for v in values)
    return all(v < max(figures) for v in values)


# A BOUND on the journey time, in figures or in words: "still under half an hour", "comfortably
# under 20 minutes", "well over an hour", "不到半小时". It is a claim about the measured figure,
# and a true one when the figure satisfies it: "a commute that's still under half an hour" for
# Stratford's measured 29 minutes was withdrawn because a verdict with no user cap had nothing
# to be checked against (r06 099518b9, NEW-5); so was "comfortably under 20 minutes" for a
# measured 11/15-minute trip (r06 27d8b56d). The bound is checked against the binding's own
# measured minutes, never against an estimate, and a bound the figure does not satisfy is
# still withdrawn. A walking/driving/cycling time is a different journey, never this bound.
_DURATION_WORDS = (
    ("an hour and a half", 90.0), ("one and a half hours", 90.0), ("an hour and a quarter", 75.0),
    ("three quarters of an hour", 45.0), ("half an hour", 30.0), ("half-an-hour", 30.0),
    ("a half hour", 30.0), ("a quarter of an hour", 15.0), ("quarter of an hour", 15.0),
    ("two hours", 120.0), ("an hour", 60.0), ("one hour", 60.0),
)
_DURATION = (r"(?:(?<![\d.])\d{1,3}(?:\.\d{1,2})?[ \t]*[-–]?[ \t]*(?:minutes?|mins?\b|hours?|hrs?\b)|"
             + "|".join(re.escape(words) for words, _value in _DURATION_WORDS) + r")")
_BOUND_OPERATORS = (
    ("lt", r"under|below|less\s+than|fewer\s+than|shorter\s+than|not\s+even"),
    ("le", r"within|inside|no\s+more\s+than|not\s+more\s+than|no\s+longer\s+than|at\s+most|up\s+to"),
    ("gt", r"over|above|more\s+than|longer\s+than"),
    ("ge", r"at\s+least"),
)
_DURATION_BOUND = re.compile(
    r"\b(?:" + "|".join(f"(?P<{name}>{words})" for name, words in _BOUND_OPERATORS) + r")"
    r"[ \t]+(?P<duration>" + _DURATION + r")"
    r"(?![\w'’]*[ \t]+(?:walk|walking|on\s+foot|drive|driving|by\s+car|cycle|cycling|bike|ride)\b)",
    re.I)
_DURATION_BOUND_ZH = re.compile(
    r"(?P<lt>不到|少于|不足)?(?P<value>\d{1,3}|半|一|两)\s*(?P<unit>分钟|个?小时)"
    r"(?(lt)|(?P<le>以内|之内|内)?)")
_ZH_OVER = re.compile(r"(?:超过|多于)\s*(?P<value>\d{1,3}|半|一|两)\s*(?P<unit>分钟|个?小时)")


def _duration_minutes(text: str) -> float | None:
    words = " ".join(str(text or "").lower().split())
    for phrase, value in _DURATION_WORDS:
        if words == phrase:
            return value
    match = re.match(r"(\d{1,3}(?:\.\d{1,2})?)\s*[-–]?\s*(minutes?|mins?|hours?|hrs?)", words)
    if not match:
        return None
    value = float(match.group(1))
    return value * 60 if match.group(2).startswith("h") else value


def _zh_minutes(value: str, unit: str) -> float:
    number = {"半": 0.5, "一": 1.0, "两": 2.0}.get(value)
    number = float(value) if number is None else number
    return number * 60 if "时" in unit else number


def _duration_bounds(fragment: str) -> list[tuple[str, float, tuple[int, int]]]:
    """``[(op, minutes, span)]`` for each journey-time bound ``fragment`` asserts."""
    text = str(fragment or "")
    bounds = []
    for match in _DURATION_BOUND.finditer(text):
        op = next(name for name, _words in _BOUND_OPERATORS if match.group(name))
        minutes = _duration_minutes(match.group("duration"))
        if minutes is not None:
            bounds.append((op, minutes, match.span()))
    for match in _DURATION_BOUND_ZH.finditer(text):
        op = "lt" if match.group("lt") else "le" if match.group("le") else None
        if op:
            bounds.append((op, _zh_minutes(match.group("value"), match.group("unit")), match.span()))
    for match in _ZH_OVER.finditer(text):
        bounds.append(("gt", _zh_minutes(match.group("value"), match.group("unit")), match.span()))
    return bounds


def _bound_holds(op: str, limit: float, minutes: float) -> bool:
    return {"lt": minutes < limit - 1e-9, "le": minutes <= limit + 1e-9,
            "gt": minutes > limit + 1e-9, "ge": minutes >= limit - 1e-9}[op]


def _without_spans(text: str, bounds: list) -> str:
    for _op, _minutes, (start, end) in sorted(bounds, key=lambda b: b[2][0], reverse=True):
        text = text[:start] + " " + text[end:]
    return text


def _bound_is_evidenced(fragment: str, minutes: float | None) -> bool | None:
    """True/False when ``fragment``'s journey-time bounds decide it; None when it has none.

    Every bound must hold for the measured ``minutes``, and what is left once the bounds are
    taken out may quote only that measured figure and draw no other verdict: "Stratford at 20
    minutes is comfortably under 25 minutes" quotes the measurement beside the bound and was
    withdrawn (r10 bb5cdb25)."""
    bounds = _duration_bounds(fragment)
    if not bounds:
        return None
    if minutes is None or not all(_bound_holds(op, limit, minutes) for op, limit, _ in bounds):
        return False
    rest = _without_spans(fragment, bounds)
    if any(abs(value - minutes) > 1e-9 for value in _minute_claim_values(rest)):
        return False
    return not (_PASS_VERDICT.search(rest) or _FAIL_VERDICT.search(rest))


_VERSUS = re.compile(r"\b(?:vs\.?|versus|compared\s+(?:with|to)|against)(?=\s|$)|相比|对比", re.I)


def _compares_measured_routes(fragment: str, context: str, pool: list) -> bool:
    """"20 minutes versus 29 minutes" / "(4 min vs 14 min)" set two measured routes side by
    side: each figure is a DIFFERENT route's own whole-trip figure, or, when the clause names a
    leg's mode ("a much shorter walk to the station"), that mode's leg on a different route.
    Where the clause (or the one it qualifies) names the routes, the figures must belong to
    them in that order. No verdict may ride along (r10 bb5cdb25)."""
    if not _VERSUS.search(fragment):
        return False
    values = _minute_claim_values(fragment)
    routes = [route for route in pool if route.get("minutes") is not None]
    if len(values) < 2 or len(values) > 4 or len(routes) < len(values):
        return False
    rest = _ROUTE_COMPARATIVE.sub(" ", _VERSUS.sub(" ", fragment))
    if _PASS_VERDICT.search(rest) or _FAIL_VERDICT.search(rest) or _duration_bounds(fragment):
        return False
    scope_key = _label_key(f"{fragment} {context}")
    by_mode = any(_key_in(_label_key(mode), scope_key) for route in routes
                  for leg in route.get("legs") or [] for mode in leg.get("modes") or [])

    def figures(route) -> set:
        if not by_mode:
            return {route["minutes"]}
        return {leg["minutes"] for leg in _legs_at_named_places(
            [leg for leg in route.get("legs") or []
             if any(_key_in(_label_key(mode), scope_key) for mode in leg.get("modes") or [])],
            f"{fragment} {context}")}

    def named_in(text: str) -> list:
        """Routes named in ``text``, in order of first mention."""
        found = []
        key = _label_key(text)
        for route in routes:
            positions = [f" {key} ".find(f" {_label_key(label)} ") for label in route.get("labels") or []
                         if _label_key(label)]
            positions = [p for p in positions if p >= 0]
            if positions:
                found.append((min(positions), id(route), route))
        return [route for _p, _i, route in sorted(found, key=lambda item: item[0])]

    own_names = named_in(_POSSESSIVE.sub("", fragment))
    lead = own_names if len(own_names) >= len(values) else named_in(_POSSESSIVE.sub("", context))[:1]
    import itertools
    for assignment in itertools.permutations(routes, len(values)):
        if any(assignment[i] is not route for i, route in enumerate(lead[:len(values)])):
            continue
        if all(any(abs(value - figure) < 1e-9 for figure in figures(route))
               for value, route in zip(values, assignment)):
            return True
    return False


_COMPARED_WITH = re.compile(
    r"\b(?:than|compared\s+(?:with|to)|versus|vs\.?|relative\s+to|"
    # "near-identical overall score to Camden" (r12 8573f129)
    r"(?:identical|similar|close|comparable|equal|level|equivalent)\s+(?:[\w'’-]+\s+){0,3}?(?:to|with)|"
    # "Its 25-minute commute is nearly as fast as Kentish Town's" (r14 95f29d0b)
    r"as\s+(?:fast|quick|short|long|slow|good)\s+as)"
    r"\s+(?:the\s+|that\s+(?:of|in)\s+)?", re.I)


def _comparison_target(fragment: str, bindings: list) -> dict | None:
    """The one measured entity ``fragment`` compares something WITH ("than Camden")."""
    for match in _COMPARED_WITH.finditer(str(fragment or "")):
        tail = re.split(r"[,;:(]|\s(?:but|and|while|though|whereas|so)\s", fragment[match.end():], maxsplit=1)[0]
        named = [b for b in bindings if not b.get("members")
                 and any(_label_in(label, tail) for label in b.get("labels") or [])]
        unique = [b for i, b in enumerate(named) if all(b is not other for other in named[:i])]
        if len(unique) == 1:
            return unique[0]
    return None


_SHORTER_WORD = re.compile(r"\b(?:shorter|quicker|faster|closer|nearer|less\s+travel)\b|更短|更快|更近", re.I)
_LONGER_WORD = re.compile(r"\b(?:longer|slower|further|farther|more\s+travel)\b|更长|更慢|更远", re.I)
_SAME_WORD = re.compile(
    r"\b(?:(?:about|roughly|much|more\s+or\s+less|practically|essentially)\s+the\s+same|"
    r"(?:similar|comparable)\s+(?:commute|journey|travel(?:\s+time)?|trip))\b|差不多|相近", re.I)


def _comparative_against(fragment: str, subject: dict | None, target: dict) -> bool:
    """A figure-free comparison of the subject's commute with ``target``'s, checked against
    both measurements: shorter/quicker needs fewer minutes, longer more, "about the same"
    within 3 minutes. A missing measurement, or both directions at once, is not evidence."""
    if subject is not None and subject.get("members"):
        others = [m for m in subject["members"] if m is not target]
        subject = others[0] if len(others) == 1 else None
    if subject is None or subject is target:
        return False
    mine, theirs = subject.get("minutes"), target.get("minutes")
    if mine is None or theirs is None:
        return False
    text = _without_money_or_weather(fragment)
    directions = [bool(_SHORTER_WORD.search(text)), bool(_LONGER_WORD.search(text)),
                  bool(_SAME_WORD.search(text))]
    if sum(directions) != 1:
        return False
    rest = _SAME_WORD.sub(" ", _LONGER_WORD.sub(" ", _SHORTER_WORD.sub(" ", text)))
    if _PASS_VERDICT.search(rest) or _FAIL_VERDICT.search(rest):
        return False
    if directions[0]:
        return mine < theirs
    if directions[1]:
        return mine > theirs
    return abs(mine - theirs) <= 3


def _recapitalise(rebuilt: str, original: str) -> str:
    """A sentence that now starts where a withdrawn clause stood starts with a capital: "Value
    score 68.8, total 75.9. the data is older here" shipped (r11 97bdde65)."""
    def fix(match):
        tail = rebuilt[match.start(2):match.start(2) + 30]
        if (original.lstrip().startswith(tail)
                or re.search(r"[.!?]\s+" + re.escape(tail), original)):
            return match.group(0)        # it already started a sentence in the original
        return match.group(1) + match.group(2).upper()
    return re.sub(r"(^\s*|[.!?]\s+)([a-z])", fix, rebuilt)


from uk_rent_agent.agent import commute_arithmetic as _arithmetic  # noqa: E402

# One definition of arithmetic on measured journey times, shared with the critic.
_WORD_NUMBERS = _arithmetic.WORD_NUMBERS
_DAY_PERIOD = _arithmetic.DAY_PERIOD
_WEEK_PERIOD = _arithmetic.WEEK_PERIOD
_ROUGHLY = _arithmetic.HEDGE
_HOURS_AND_MINUTES = _arithmetic.HOURS_AND_MINUTES
_durations_in = _arithmetic.durations


def _office_days(state: dict) -> set:
    """Commuting days a week the user stated (criteria or this message), besides 5."""
    days = set()
    value = (state.get("accumulated_search_criteria") or {}).get("office_days_per_week")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 7:
        days.add(float(value))
    context = state.get("extracted_context") or {}
    current = str(context.get("current_message") or "")
    days |= _arithmetic.days_stated(current)
    if not days:
        for turn in (context.get("history") or [])[-20:]:
            if isinstance(turn, dict):
                days |= _arithmetic.days_stated(str(turn.get("user") or ""))
    return {d for d in days if d}


_OVER_A_PERIOD = re.compile(
    r"\b(?:over|across|during|in|for)\s+(?:a|an|the|one|your|five|four|three|two|\d)\s+"
    r"(?:[\w-]+\s+){0,2}?(?:days?|weeks?|months?|years?|trips?|journeys?)\b", re.I)
_COMPARISON_CUE = re.compile(
    r"\b(?:versus|vs\.?|over|than|compared|saves?|saving|difference|quicker|faster|shorter|longer|"
    r"slower|less|more|instead\s+of)\b", re.I)
_SUPERLATIVE = re.compile(r"\b(?:fastest|quickest|shortest)\b|\b(?:slowest|longest)\b", re.I)
@functools.lru_cache(maxsize=256)
def _line_has_wrong_base(line: str, measured: tuple, days: tuple) -> bool:
    """A base figure the line quotes that is not a difference of two measured routes. Once
    per line (the B-01 class)."""
    return _arithmetic._bases(line, list(measured), set(days))[1]


@functools.lru_cache(maxsize=512)
def _sentence_of(line: str, fragment: str) -> str:
    """The sentence of ``line`` that holds ``fragment``."""
    probe = fragment.strip()[:40]
    for sentence in _arithmetic.sentences(line):
        if probe and probe in sentence:
            return sentence
    return line


def _arithmetic_evidenced(fragment: str, *, context: str, previous_clause: str, line: str,
                          pool: list, days: set, subject: dict | None, user_days: set = frozenset(),
                          caps: set = frozenset()) -> bool:
    """Arithmetic on this turn's measured routes, in any notation, by THE shared rule
    (``uk_rent_agent.agent.commute_arithmetic.check_figures``, also the critic's): differences
    of the PAIR the text names or implies, their x2 / x days / x2 x days totals, products and
    subtraction steps whose operands are evidenced, chained steps included (r13 2264a2d5, r14
    95f29d0b). The redactor adds what only it judges: no other verdict may ride along, and
    "the fastest" must be the fastest."""
    routes = [{"labels": list(route.get("labels") or []), "minutes": route["minutes"],
               "mode": route.get("mode") or "transit",
               "alternatives": list(route.get("place_alternatives") or [])}
              for route in pool if route.get("minutes") is not None and route.get("labels")]
    measured = sorted({route["minutes"] for route in routes})
    if len(measured) < 2:
        return False
    text = str(fragment or "")
    if not (_arithmetic.MINUTES.search(text) or _HOURS_AND_MINUTES.search(text)
            or _arithmetic.JOURNEY_COUNT.search(text) or _arithmetic.products(text)
            or any(phrase in text.lower() for phrase, _v in _arithmetic.DURATION_WORDS)):
        return False
    label = line[:80].split(":", 1)[0] if ":" in line[:80] else ""
    scope = f"{label} {previous_clause} {context} {text}"
    if not (_DAY_PERIOD.search(scope) or _WEEK_PERIOD.search(scope) or _COMPARISON_CUE.search(scope)
            or _arithmetic.LONG_PERIOD.search(_arithmetic.without_money(scope)) or _arithmetic.period_steps(text)
            or _arithmetic.products(text) or _arithmetic.subtractions(text)
            or _arithmetic.JOURNEY_COUNT.search(text)
            or (caps and re.search(r"\b(?:limit|cap|max(?:imum)?|spare|headroom|margin|buffer)\b", scope, re.I))):
        return False      # no arithmetic here: cheap exit, before any line-wide work
    if all(_line_has_wrong_base(line, tuple(sorted({r["minutes"] for r in variant})),
                                tuple(sorted(set(days) | set(user_days) | {5.0})))
           for variant in _arithmetic.route_variants(routes)):
        return False      # consistency: a wrong base takes the line's totals with it
    plain = _OVER_A_PERIOD.sub(" ", _ROUTE_COMPARATIVE.sub(" ", _SUPERLATIVE.sub(" ", text)))
    plain = re.sub(r"\b(?:over|than|versus|vs\.?|against)\s+(?:[A-Z][\w'’-]*\s*){1,3}", " ", plain)
    if caps:
        # A margin's own direction word is checked by the margin rule: "25 − 19 = 6 minutes
        # under", "outside your limit, by 4 minutes" (r16 0a6c11d6).
        plain = re.sub(r"(?<=\bminutes)\s+(?:under|over)\b|(?<=\bmins)\s+(?:under|over)\b", " ", plain, flags=re.I)
        plain = re.sub(r"\b(?:within|inside|under|below|over|above|beyond|outside|past)\s+"
                       r"(?:your|the)\s+(?:\d+[\s-]*minute\s+)?(?:cap|limit|max(?:imum)?|ceiling|threshold)\b",
                       " ", plain, flags=re.I)
    plain = _arithmetic.SUBTRACTION.sub(" ", plain)
    if _PASS_VERDICT.search(plain) or _FAIL_VERDICT.search(plain) or _duration_bounds(text):
        return False
    sentence = _sentence_of(line, text) if text.strip() else line
    subject_route = None
    if subject is not None and subject.get("minutes") is not None and subject.get("labels"):
        subject_route = {"labels": list(subject["labels"]), "minutes": subject["minutes"]}
    ok, seen = _arithmetic.check_figures(text, sentence=f"{previous_clause} {sentence}", line=line,
                                         label=label, context=context, routes=routes, days=days,
                                         subject=subject_route, user_days=user_days, caps=caps,
                                         lead=previous_clause)
    if not (ok and seen):
        return False      # plain journey times are the ordinary per-route check's business
    superlative = _SUPERLATIVE.search(text)
    if superlative:
        owner = _arithmetic._route_before(text, superlative.start(), routes) or subject_route
        if owner is None:
            return False
        fastest = superlative.group().lower() in ("fastest", "quickest", "shortest")
        if owner["minutes"] != (measured[0] if fastest else measured[-1]):
            return False
    return True


def _owned_by_named_routes(fragment: str, bindings: list, caps: set) -> bool:
    """Each figure is the measured time of the route named right before it: ", versus Camden
    at about 36 minutes" after "The quickest is Kentish Town at about 22 minutes" (r14
    95f29d0b) is Camden's own 36, not a claim about the clause's subject."""
    text = str(fragment or "")
    figures = list(_MINUTE_VALUE.finditer(text))
    if not figures:
        return False
    key = f" {_label_key(text)} "
    for index, figure in enumerate(figures):
        # "versus Stratford's 4-minute walk": the possessive names Stratford (r21 8f4f0a00)
        head = f" {_label_key(_POSSESSIVE.sub('', text[:figure.start()]))} "
        best, best_at = None, -1
        for binding in bindings:
            if binding.get("members") or binding.get("minutes") is None:
                continue
            for label in binding.get("labels") or []:
                at = head.rfind(f" {_label_key(label)} ") if _label_key(label) else -1
                if at > best_at:
                    best, best_at = binding, at
        if best is None:
            return False
        # The claim runs from the route's name to the next figure, so "… 23 minutes to
        # Stratford" keeps the place it travels to.
        stop = figures[index + 1].start() if index + 1 < len(figures) else len(text)
        start = max((m.start() for label in best["labels"] if _label_key(label)
                     for m in re.finditer(re.escape(str(label)), text[:figure.start()], re.I)), default=None)
        if start is None or not _claim_is_evidenced(text[start:stop], best, caps):
            return False
    return bool(key.strip())


def _shared_by_every_route(fragment: str, pool: list) -> bool:
    """"the in-transit time is identical at 9 minutes on both routes": a figure said to hold
    for every route the turn measured, and it does (a whole trip or a leg of each)."""
    if not re.search(r"\b(?:both|each|every|all)\b[^.;!?]{0,20}\b(?:routes?|journeys?|options?|lines?)\b|"
                     r"\bon\s+both\b|\b(?:minutes?|mins?)\s+(?:each|apiece)\b", fragment, re.I):
        return False
    values = _minute_claim_values(fragment)
    routes = [route for route in pool if route.get("minutes") is not None]
    if not values or len(routes) < 2:
        return False
    if _PASS_VERDICT.search(fragment) or _FAIL_VERDICT.search(fragment) or _duration_bounds(fragment):
        return False
    return all(any(abs(value - figure) < 1e-9
                   for figure in [route["minutes"]] + [leg["minutes"] for leg in route.get("legs") or []])
               for value in values for route in routes)


def _claim_is_evidenced(fragment: str, binding: dict | None, caps: set,
                        context: str = "", heading: str = "",
                        itinerary: frozenset | set = frozenset(),
                        sentence_marked: bool = False, mode: str | None = None) -> bool:
    """True when this fragment's commute claim — figure AND conclusion — follows the evidence.

    Three separate things have to hold, and each one was a way through on its own:

    * every minute it quotes is the listing's measured duration or a cap the user stated;
    * a verdict ("within" / "exceeds") points the way the measurement actually points. A
      fragment with no figure used to pass vacuously here — ``all([])`` is True — so
      "this exceeds your 45-minute limit" rode out on evidence of 17 minutes;
    * a verdict has some cap to be checked against at all.
    """
    if _honest_commute_reference(fragment, caps):
        return True
    if binding and binding.get("members"):
        # A plain list of the named entities' OWN figures, with no verdict drawn from them, is
        # quoting the evidence: "Camden and Islington are 31 and 14 minutes away
        # respectively" lost both area-comparison figures (R2-01 d). Each figure must be one
        # of the named entities' measurements.
        listed = {float(value) for value in _MINUTE_LIST_VALUE.findall(fragment)}
        measured = {member.get("minutes") for member in binding["members"]}
        if (listed and None not in measured and listed == measured
                and not (_PASS_VERDICT.search(fragment) or _FAIL_VERDICT.search(fragment))):
            # Every named entity's own figure, and nothing else: "both 31 minutes" for areas
            # measured at 31 and 14 is NOT this, and still has to pass the shared check.
            return True
        if _duration_bounds(fragment):
            # "Both are under half an hour": the bound holds for each named entity's own
            # measured figure, or the claim goes.
            return all(_bound_is_evidenced(fragment, member.get("minutes")) is True
                       for member in binding["members"])
        # A shared verdict can be checked for each named entity. Different measured times
        # in one clause cannot be pooled and freely reassigned between those entities.
        return bool(_minute_claim_values(fragment) and
                    all(value in caps for value in _minute_claim_values(fragment)) and
                    all(_claim_is_evidenced(fragment, member, caps)
                        for member in binding["members"]))
    if binding is None:
        return False
    if mode is None:
        mode = _arithmetic.figure_mode(fragment) if _MINUTE_TOKEN.search(fragment) else None
    if mode is not None and (binding.get("mode") or "transit") != mode:
        return False      # a bike / car figure is never a transit route's (r6b)
    destinations = binding.get("destinations") or []
    scope = f"{fragment} {context}"
    values = _minute_claim_values(fragment)
    # A clause that names one of the route's legs and quotes exactly that leg's figure is the
    # leg, whatever it walks TO: "a 12-minute walk to the Hammersmith & City platforms" (R2-25).
    leg_quote = bool(values) and all(
        any(abs(value - leg["minutes"]) < 1e-9 for leg in _legs_named_at(f"{scope} {heading}", binding, scope))
        for value in values)
    if destinations and not leg_quote and not _targets_on_route(fragment, binding, scope):
        return False
    if binding.get("minutes") is None:
        return _estimate_is_quoted_as_estimate(fragment, binding, caps, context, sentence_marked)
    if re.search(r"wheelchair|step.free|accessible|轮椅|无障碍|无台阶", fragment, re.I):
        # A fastest itinerary is not an accessibility audit or a personalised travel time.
        return False
    bounded = _bound_is_evidenced(fragment, binding["minutes"])
    if bounded is not None:
        return bounded
    own = {binding["minutes"]} | set(binding.get("alt_minutes") or [])   # the route's figures
    allowed = own | caps
    # The margin between this measurement and the user's cap: "meets your 25-minute limit with
    # about 5 minutes to spare", "comes in roughly 4 minutes over" (r10 bb5cdb25). Only in the
    # direction the measurement actually points.
    # The margin is judged by the shared rule (uk_rent_agent.agent.commute_arithmetic), so the
    # critic and this redactor agree: its reference is the cap, exact or hedged, and never
    # another route ("about 9 minutes to spare" with Stratford 20 under a 25 cap goes).
    hedged_margin = bool(_ROUGHLY.search(fragment))
    for start, end, value in _arithmetic.figure_spans(fragment):
        side = _arithmetic.margin_side(fragment, start, end, fragment, caps_known=bool(caps))
        if side is None:
            continue
        margins = [cap - binding["minutes"] if side == "under" else binding["minutes"] - cap for cap in caps]
        if any(margin > 0 and _arithmetic.close(value, margin, hedged_margin) for margin in margins):
            allowed.add(value)
            own.add(value)        # "(5 minutes to spare)" restates this route against the cap
    # A leg's own duration is part of the same measurement, but only for a leg this claim
    # names by line or mode — "(6 min)" attached to "Northern line to Moorgate". A claim
    # about the WHOLE trip may not borrow one: a whole-trip word ("the journey takes 6
    # minutes"), or naming both the route's origin and its destination ("Stratford to
    # Canary Wharf takes 17 minutes on the Jubilee line").
    # ``heading`` is the line's label before a colon: "Commute: 17 minutes (Jubilee line)"
    # is a whole-trip claim even though the clause split leaves only "17 minutes".
    # A clause that names the leg ITSELF ("then a 3-minute walk") is that leg under any label:
    # "On the commute itself: … a short walk, the 476 bus, then a 3-minute walk" lost the walk
    # to the label's "commute" (r17 4f27cfd2).
    names_own_leg = bool(values) and all(
        any(abs(value - leg["minutes"]) < 1e-9 for leg in _legs_named_at(scope, binding)) for value in values)
    hay = _label_key(scope)
    end_to_end = (any(_key_in(_label_key(l), hay) for l in binding.get("labels") or [])
                  and any(_key_in(_label_key(l), hay) for l in destinations))
    if not (_WHOLE_TRIP_WORD.search(fragment) or (_WHOLE_TRIP_WORD.search(heading) and not names_own_leg)
            or end_to_end):
        # A leg named in the line's label counts too: "- Heathrow Express to London
        # Paddington: 20 minutes" (R2-25).
        for leg in _legs_named_at(f"{scope} {heading}", binding, scope):
            # A leg ending at an intermediate stop is unambiguous; one ending at the
            # destination only inside an enumerated itinerary.
            if not _arrives_at_destination(leg, binding) or id(binding) in itinerary:
                allowed.add(leg["minutes"])
                own.add(leg["minutes"])
        if id(binding) in itinerary:
            # Inside an enumerated itinerary, "then that line to King's Cross St Pancras
            # (11 min)" names the leg by where it arrives (R2-25).
            target = _TARGET.search(_without_abbreviation_dots(scope))
            if target:
                hay_target = _label_key(target.group(1))
                arrived = {leg["minutes"] for leg in binding.get("legs") or []
                           if any(_key_in(place, hay_target) for place in leg["places"])}
                allowed.update(arrived)
                own.update(arrived)
    if any(not any(abs(value - permitted) < 1e-9 for permitted in allowed)
           for value in values):
        # A measured pair may also carry the straight-line estimate another tool returned;
        # quoting THAT as an estimate is still honest.
        return _estimate_is_quoted_as_estimate(fragment, binding, caps, context, sentence_marked)

    pointed = _POSITIONAL_REFERENCE.sub(" ", fragment)     # "the figure above" is no verdict
    asserts_pass = bool(_PASS_VERDICT.search(pointed))
    asserts_fail = bool(_FAIL_VERDICT.search(pointed))
    if asserts_pass and asserts_fail:
        return False  # both directions in one fragment: nothing coherent to check
    if asserts_pass or asserts_fail:
        quoted_caps = [value for value in values
                       if any(abs(value - cap) < 1e-9 for cap in caps)]
        if not quoted_caps and _compares_own_routes(fragment, values, binding):
            return True
        against = quoted_caps or sorted(caps)
        if not against:
            return False  # a verdict with no limit to measure it against
        meets = all(binding["minutes"] <= cap + 1e-9 for cap in against)
        return meets if asserts_pass else not meets
    # No verdict: the fragment merely carries a figure, and that figure checked out above. A
    # figure that is only the user's LIMIT is not this route's journey time: "Stratford: 35
    # minutes to Canary Wharf" with Stratford measured at 29 and a 35-minute limit stated was
    # kept as if measured. It stands only where the clause names the limit as a limit.
    if not all(any(abs(value - figure) < 1e-9 for figure in own) for value in values):
        return bool(_LIMIT_LABEL.search(f"{fragment} {heading}") or re.search(
            r"\b(?:limit|cap|ceiling|maximum|max|target|requirement)\b|上限|要求", fragment, re.I))
    return bool(values)


def _redact_commute_claims(text: str, *, zh: bool, bindings: list[dict] | None = None,
                           caps: set | None = None,
                           default_binding: dict | None = None,
                           preference_context: bool = False,
                           source_texts: list[str] | None = None,
                           removed: list | None = None,
                           office_days: set | None = None,
                           user_destinations: list | None = None) -> str | None:
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

    Returns ``None`` when nothing usable survives. Unsupported assertions are withdrawn
    individually rather than escalating one unresolved line into whole-answer replacement.
    """
    bindings = list(bindings or [])
    caps = set(caps or ())
    # Commuting days a week for derived totals: 5, the user's own, or an N-day week the answer states.
    answer_days = _arithmetic.days_stated(text)         # N-day weeks the answer states
    user_days = set(office_days or ())                   # the user's own office days
    itinerary = _itinerary_bindings(text, bindings + ([default_binding] if default_binding else []))
    itinerary_routes = [b for b in bindings + ([default_binding] if default_binding else []) if id(b) in itinerary]
    removed = removed if removed is not None else []
    # Every route/area/listing of this turn that carries a figure. A clause attributable to
    # none of them may still quote one of them: "About 14 minutes in the tube" after a
    # journey lookup that ran beside a calculate_commute estimate (two routes, so no single
    # default) lost the only figure the user asked for (R2-01 b). Such a clause is kept when
    # ANY of them evidences it under the full claim check — the figure, the leg-vs-trip rule,
    # the places it travels to and the direction of any verdict.
    pool: list[dict] = []
    for candidate in bindings + ([default_binding] if default_binding else []):
        if ((candidate.get("minutes") is not None or candidate.get("estimate"))
                and all(candidate is not seen for seen in pool)):
            pool.append(candidate)
    pool_figures = set(caps)
    for candidate in pool:
        if candidate.get("minutes") is not None:
            pool_figures.add(candidate["minutes"])
        pool_figures.update(candidate.get("alt_minutes") or [])
        pool_figures.update(candidate.get("estimate") or [])
        pool_figures.update(leg["minutes"] for leg in candidate.get("legs") or [])
    # "its commute is 20 minutes shorter each way" compares two measured routes (36 vs 16):
    # the DIFFERENCE of two of this turn's measurements, quoted with a comparative (R2-25).
    measured = sorted({c["minutes"] for c in pool if c.get("minutes") is not None})[:40]
    differences = {round(abs(a - b), 2) for i, a in enumerate(measured) for b in measured[i + 1:]}
    # ... under each consistent choice of a place's measurements (r21 8f4f0a00)
    for variant in _arithmetic.route_variants(
            [{"labels": list(c.get("labels") or []), "minutes": c["minutes"],
              "alternatives": list(c.get("place_alternatives") or [])}
             for c in pool if c.get("minutes") is not None and c.get("place_alternatives")] +
            [{"labels": list(c.get("labels") or []), "minutes": c["minutes"]}
             for c in pool if c.get("minutes") is not None and not c.get("place_alternatives")]):
        values = sorted({r["minutes"] for r in variant})
        differences |= {round(abs(a - b), 2) for i, a in enumerate(values) for b in values[i + 1:]}
    lines: list[str] = []
    source_lines = str(text or "").split("\n")
    # Period tables (rows = periods, columns = routes or the saving) are judged cell by cell by
    # the shared rule; a row with a wrong cell goes whole, never as a "—" placeholder row, and a
    # table that loses every row goes with its header (r19 de1fba15).
    table_rows = _arithmetic.period_table_rows(
        text, [{"labels": list(c.get("labels") or []), "minutes": c["minutes"],
                "mode": c.get("mode") or "transit",
                "alternatives": list(c.get("place_alternatives") or [])}
               for c in pool if c.get("minutes") is not None],
        user_days=user_days, answer_days=answer_days)
    tables_kept: dict = {}
    produced_at: list[int] = []        # len(lines) before each source line was processed
    block_binding = None                # the listing a header line names, for its bullets
    block_minutes = False               # an earlier line of this paragraph/list quoted a journey time
    for line in source_lines:
        produced_at.append(len(lines))
        row_verdict = table_rows.get(len(produced_at) - 1)
        if row_verdict is not None:
            tables_kept.setdefault(row_verdict["table"], False)
            if row_verdict["ok"]:
                tables_kept[row_verdict["table"]] = True
                lines.append(line)
            else:
                removed.append(line)
            continue
        in_timed_block = block_minutes
        block_minutes = bool(line.strip()) and (block_minutes or bool(_MINUTE_TOKEN.search(line)))
        removed_before_line = len(removed)
        bullet = _BULLET_PREFIX.match(line)
        prefix = bullet.group(1) if bullet else ""
        body = line[len(prefix):]
        binding = _line_binding(line, bindings, default_binding)
        # Bullets under a listing's header are about that listing: "**King Square, London,
        # EC1V — £1,800/month**" then "- Commute to King's Cross: roughly 20 minutes (an
        # estimate …)" quoted King Square's own ~20 (r15 40a2522f), not the area route.
        list_item = bool(re.match(r"^\s*(?:[-•]|\*(?!\*)|\d+[.)])\s+", line))   # "**Bold" is a header
        if list_item and _line_binding(line, bindings) is None and block_binding is not None:
            binding = block_binding
        elif not list_item and line.strip():
            own_line = _line_binding(line, bindings)
            # A listing's header, or a route's: "**Bow — no, it's over**" / "### Bow" / "Bow:"
            # (r16 0a6c11d6), so "- 29 minutes door to door, so 4 minutes over your limit" is Bow's.
            header = bool(re.match(r"^\s*(?:\*\*|#{1,4}\s)", line) or line.rstrip().endswith((":", "：")))
            block_binding = own_line if own_line is not None and own_line.get("labels") and not own_line.get(
                "members") and (own_line.get("listing") or header) else None
            if block_binding is not None and not block_binding.get("listing") and _SAVING_HEADER.search(line):
                # "**Time saved by the faster one (Stratford):**" heads the SAVINGS, not
                # Stratford's journey: "- Per day: 19 minutes each way" under it is no
                # Stratford time, and was kept as one once its wrong total went (r6b).
                block_binding = None
        preference_line = preference_context or bool(re.search(
            r"preference|default|remember|偏好|默认|保存|记住|"
            r"requirements?\s+(?:stand|remain)|conditions?\s+(?:stand|remain)|"
            r"(?:criteria|requirements?|conditions?).{0,30}(?:unchanged|unmodified)|"
            r"(?:area|ceiling|budget)\s+stays|条件.{0,10}(?:保持|不变)", line, re.I))
        clean_question = body.strip().strip("*# ")
        if (clean_question.endswith(("?", "？")) and re.match(
                r"^(?:Does|Can|Would|Is|Will|Whether|是否|能否|请问)", clean_question, re.I)
                and all(value in caps for value in _minute_claim_values(clean_question))):
            lines.append(line)
            continue

        # A pure LABEL before a colon ("Commute: 17 minutes") qualifies what follows; a
        # heading that carries its own figure ("The trip takes 30 minutes: walk (5 min),
        # ...") is a claim of its own and labels nothing.
        heading = line.split(":", 1)[0] if ":" in line[:60] else ""
        if _MINUTE_TOKEN.search(heading):
            heading = ""
        # ... and only the sentence it introduces: "So Stratford is the clear winner on commute:
        # it's both faster and change-free. Bow's problem is mostly the 13-minute walk to Bow
        # Road" read "commute" as the label of the walk too, so its leg figure counted as a
        # whole-trip claim and went (r17 392cdf42).
        colon_at = body.find(":") if heading else -1
        heading_close = re.search(r"[.!?。！？](?=\s|$)", body[colon_at + 1:]) if colon_at >= 0 else None
        heading_reach = colon_at + 1 + heading_close.end() if heading_close else len(body)
        heading_now = [heading]     # the label in force for the clause being judged

        def _evidenced(fragment: str, _binding=None, context: str = "",
                       sentence_marked: bool = False, previous_clause: str = "") -> bool:
            """True when this fragment's commute claim is backed for THIS line's listing.

            ``context`` is only ever the clause a parenthetical is attached to — never the
            whole line: line-wide scope let one clause license another's figure, and cost
            O(fragments x line) (the B-01 class again). ``sentence_marked``: the sentence
            carries an estimate marker ("estimated", "roughly")."""
            if _preference_commute_reference(
                    fragment, caps, context=preference_line or bool(
                        _LIMIT_LABEL.search(heading_now[0]) or _LIMIT_LABEL.search(fragment))
                    or _restates_user_limit(fragment, bindings, user_destinations)):
                return True
            if _quotes_source_text(fragment, source_texts):
                return True
            effective = _binding if _binding is not None else binding
            mode = (_arithmetic.figure_mode(f"{previous_clause} {fragment}")
                    if _MINUTE_TOKEN.search(fragment) else None)
            if mode is not None:
                # "8 minutes each way by bike": a different journey from the transit routes
                # measured (the rule ``_DURATION_BOUND`` already applies to bounds). Only a route
                # measured in that mode evidences it; with none, nothing does (r6b).
                same = [c for c in pool if (c.get("mode") or "transit") == mode]
                quoted = _minute_claim_values(fragment)
                if not same or not all(any(abs(v - f) < 1e-9 for c in same
                                           for f in [c.get("minutes")] + list(c.get("estimate") or []))
                                       for v in quoted):
                    return False
                if effective is not None and (effective.get("mode") or "transit") != mode:
                    # The place the line names, measured in THAT mode, or nothing: "Bow is 22
                    # minutes by bike" may not borrow Stratford's bike route.
                    keys = set(map(_norm, effective.get("labels") or []))
                    effective = next((c for c in same if keys & set(map(_norm, c.get("labels") or []))), None)
                    if effective is None:
                        return False
                    return _claim_is_evidenced(fragment, effective, caps, context=context,
                                               heading=heading_now[0], itinerary=itinerary,
                                               sentence_marked=sentence_marked, mode=mode)
            if _arithmetic.journey_counts(fragment) and not _MINUTE_TOKEN.search(fragment):
                # A journey count alone ("(8 journeys)", "6 journeys"): it must match the week it
                # is for, whatever the turn measured (r17). Same rule as the critic.
                return _arithmetic.journey_counts_ok(
                    fragment, context=f"{context} {previous_clause}", sentence=line,
                    user_days=user_days, answer_days=answer_days)
            if _claim_is_evidenced(fragment, effective, caps, context=context, heading=heading_now[0],
                                   itinerary=itinerary, sentence_marked=sentence_marked):
                return True
            if (_compares_measured_routes(fragment, f"{context} {previous_clause}", pool)
                    or _shared_by_every_route(fragment, pool)):
                return True
            values = _minute_claim_values(fragment)
            # "Hackney is the faster commute, by 8 minutes (29 minus 21)": the comparative is
            # in the clause the "by …" figure qualifies (r06 099518b9).
            # "- Versus Camden (35 min): 14 minutes each way" (r12 53bfec0e): the label the
            # figure qualifies names the comparison.
            comparative = bool(_COMPARATIVE_DIFFERENCE.search(fragment) or _VERSUS.search(fragment) or (
                (re.match(r"\s*by\b", fragment, re.I)
                 or re.search(r"\b(?:each|one)\s+way\b|\bper\s+(?:trip|journey)\b", fragment, re.I))
                and (_COMPARATIVE_DIFFERENCE.search(previous_clause) or _VERSUS.search(previous_clause))))
            # ... and nothing else: "saves you 7 minutes each way and roughly 5 hours a month"
            # is a period total too, which the arithmetic below judges (r21 8f4f0a00).
            if (values and differences and comparative
                    and all(round(value, 2) in differences for value in values)
                    and not _HOURS_AND_MINUTES.search(fragment)):
                return True
            if _arithmetic_evidenced(fragment, context=context, previous_clause=previous_clause,
                                     line=line, pool=pool, days=answer_days, subject=effective,
                                     user_days=user_days, caps=caps):
                return True
            if values and _owned_by_named_routes(fragment, bindings, caps):
                return True
            target = _comparison_target(fragment, bindings)
            if target is not None and not values:
                # "Slightly dearer median than Camden but a much shorter commute" compares
                # Holloway's measured 10 minutes with Camden's 27 (r11 97bdde65): decided by
                # the two measurements, not by the turn's overall shortest route.
                return _comparative_against(fragment, effective, target)
            if (not values and len(measured) > 1 and effective is not None
                    and effective.get("minutes") is not None):
                # "has a much shorter commute" compares this listing's measured route with the
                # other measured routes of the turn; it holds when the direction is right.
                if re.search(r"\b(?:shorter|quicker|faster|closer|nearer)\b|更短|更快|更近", fragment, re.I):
                    return effective["minutes"] == measured[0]
                if re.search(r"\b(?:longer|slower|further|farther)\b|更长|更慢|更远", fragment, re.I):
                    return effective["minutes"] == measured[-1]
            if pool and (_arithmetic.SPREAD_BOUND.search(fragment) or _arithmetic.SPREAD_EXACT.search(fragment)):
                # "All three are within 4 minutes of each other": max - min of the routes the
                # line names, or of every measured route (r15 700d77a8). Same rule as the critic.
                routes = [{"labels": list(c.get("labels") or []), "minutes": c["minutes"]}
                          for c in pool if c.get("minutes") is not None]
                ok, seen = _arithmetic.check_figures(fragment, sentence=line, line=line, routes=routes,
                                                     days=answer_days, user_days=user_days)
                return bool(ok and seen)
            if not pool:
                return False
            if _duration_bounds(fragment) and (effective is None or (
                    not effective.get("labels") and effective.get("minutes") is None
                    and not effective.get("members"))):
                # A bound in a clause that names no single route ("Either way, this is a short
                # commute — comfortably under 20 minutes") holds only when EVERY measured
                # route of the turn satisfies it (r06 27d8b56d).
                measured_routes = [c for c in pool if c.get("minutes") is not None]
                return bool(measured_routes) and all(
                    _bound_is_evidenced(fragment, c["minutes"]) is True for c in measured_routes)
            if effective is not None:
                return False
            if _per_period_line(line, fragment):
                # "- Per office day: 18 minutes each way" names no route: under a per-period
                # label the figure is a saving or a total, never "some route's own time". With
                # its wrong total withdrawn it used to stay alone on the line (r6b).
                return False
            values = _minute_claim_values(fragment)
            if not values or not all(any(abs(value - figure) < 1e-9 for figure in pool_figures)
                                     for value in values):
                return False          # cheap reject before trying each route (B-01 class)
            return any(_claim_is_evidenced(fragment, candidate, caps, context=context,
                                           heading=heading_now[0], itinerary=itinerary,
                                           sentence_marked=sentence_marked)
                       for candidate in pool)

        # A parenthetical commute aside ("(17 min to Canary Wharf)") is an attachment to a
        # clause, not a clause of its own; remove it before clause splitting.
        def _attached_clause(match) -> str:
            """The clause a parenthetical belongs to: "Northern line to Moorgate (6 min)".

            It ends at the previous parenthetical or coordinator too: in "Camden (31 min)
            and Islington (14 min)" the second figure belongs to Islington alone."""
            lead = _without_abbreviation_dots(body[max(0, match.start() - 200):match.start()])
            return re.split(r"[,;:!?，；：。！？)）]|\.(?=\s|$)|\s(?:and|while|but|whereas|vs\.?)\s",
                            lead)[-1]

        def _sentence_around(start: int, end: int) -> str:
            """The sentence of ``body`` that contains [start, end)."""
            lead = body[max(0, start - 300):start]
            tail = body[end:end + 300]
            opening = max((m.end() for m in re.finditer(r"[.!?。！？](?=\s|$)", lead)), default=0)
            closing = re.search(r"[.!?。！？](?=\s|$)", tail)
            return lead[opening:] + body[start:end] + (tail[:closing.end()] if closing else tail)

        # The line names no single entity (several, none or ambiguously): a parenthetical
        # belongs to the entity its own clause names, if exactly one.
        unattributed_line = (binding is None or bool(binding.get("members"))
                             or (not binding.get("labels") and binding.get("minutes") is None
                                 and not binding.get("estimate")))

        vetted: list[str] = []      # parentheticals that passed on their own clause

        def _parenthetical(m) -> str:
            if not _asserts_commute(m.group(0)):
                return m.group(0)
            attached = _attached_clause(m)
            own = None
            if unattributed_line:
                local = _line_binding(attached, bindings)
                if local is None or not local.get("labels") or local.get("members"):
                    # The clause cut at commas lost the address ("Featherstone Street, Old
                    # Street, EC1Y at £2,701 (about 14 min estimated)"): retry on the clause
                    # back to the previous coordinator or sentence end (r08 NEW-B).
                    lead = _without_abbreviation_dots(body[max(0, m.start() - 200):m.start()])
                    wider = re.split(r"[;:!?；：。！？)）]|\.(?=\s|$)|\s(?:and|while|but|whereas|vs\.?)\s",
                                     lead)[-1]
                    local = _line_binding(wider, bindings)
                if local is not None and local.get("labels") and not local.get("members"):
                    own = local
            else:
                # The line's entity is not necessarily this sentence's: "On the commute itself: for
                # Islington generally, … — a 1-minute walk, the 476 bus (10 min), … That's a measured
                # journey … rather than from the King Square flat" bound "(10 min)" to the King
                # Square listing named a sentence later, and the bus leg went (r17 4f27cfd2). The
                # one entity its own sentence names before it owns the aside.
                lead = _without_abbreviation_dots(body[max(0, m.start() - 300):m.start()])
                opening = max((x.end() for x in re.finditer(r"[.!?。！？](?=\s|$)", lead)), default=0)
                local = _line_binding(lead[opening:], bindings) if lead[opening:].strip() else None
                if (local is not None and local is not binding and local.get("labels")
                        and not local.get("members")):
                    own = local
            heading_now[0] = heading if m.start() < heading_reach else ""
            if not _evidenced(m.group(0), own, context=attached,
                              sentence_marked=bool(_ESTIMATE_MARKER.search(
                                  _sentence_around(m.start(), m.end())))):
                removed.append(m.group(0))
                return ""
            vetted.append(m.group(0))
            return m.group(0)

        original_body = body
        body = _PARENTHETICAL.sub(_parenthetical, body)
        heading_now[0] = heading
        colon_passed = False
        split_coordinators: list = []   # (original words, the text after them) per split

        labels = sorted({label for item in bindings for label in item["labels"] if label},
                        key=len, reverse=True)
        if labels:
            coordinated = re.compile(r"(?<!\s)\s+(?:and|while|but)\s+(?=(?:" +
                                     "|".join(re.escape(label) for label in labels) + r"))", re.I)
            # "A takes 18 minutes and B takes 99 minutes" contains two independent claims.
            # "A and B are within 30 minutes" is one shared verdict and stays together.
            # The first figure is located ONCE: re-searching the prefix for every
            # conjunction was quadratic in the number of conjunctions.
            first_minute = _MINUTE_TOKEN.search(body)

            def _split_coordinator(m) -> str:
                if first_minute is None or first_minute.end() > m.start():
                    return m.group(0)
                # Remember the words the split replaced, so a kept pair of clauses reads as
                # written: "…to spare, while Bow comes in…" shipped as "…to spare; Bow comes…".
                start = m.start()
                while start > 0 and body[start - 1] in " ,，":
                    start -= 1
                tail = re.split(r"\s+(?:and|while|but)\s+", body[m.end():m.end() + 40], maxsplit=1)[0]
                split_coordinators.append((body[start:m.end()], tail))
                return "; "
            body = coordinated.sub(_split_coordinator, body)

        parts = _guard_text_parts(body)
        # Whether the sentence each fragment belongs to carries an estimate marker, computed
        # once per sentence (linear in the line).
        marked_at: dict[int, bool] = {}
        opened = 0
        for index in range(0, len(parts), 2):
            separator = parts[index + 1] if index + 1 < len(parts) else ""
            if _SENTENCE_END.search(separator) or index + 2 >= len(parts):
                marked = any(_ESTIMATE_MARKER.search(parts[member])
                             for member in range(opened, index + 1, 2))
                for member in range(opened, index + 1, 2):
                    marked_at[member] = marked
                opened = index + 2
        kept: list[str] = []
        dropped_in_sentence = False
        crossed_sentence = False     # the last withdrawn clause ended its sentence
        sentence_start = 0          # index in ``kept`` where the current sentence starts
        sentence_dropped = False    # something was withdrawn from the current sentence
        inherited = binding
        commute_context = False
        # An earlier clause of this line quoted a journey time, or (for a list item) an earlier
        # line of its list did: "- Per week: 48 minutes" then "- Per month: about 3.5 hours" is a
        # journey total too, judged like one (r17 8c35fd5b; a wrong "about 5 hours" shipped).
        figure_context = bool(in_timed_block and list_item)
        last_fragment = ""          # the previous clause of the current sentence
        continuation_broken = False  # a kept clause leans on a withdrawn one ("…, but I have
                                     # not verified that") — no clean cut exists (R3-01)
        address_tail_open = False    # the last clause was withdrawn and ended at a comma
        withdrawn_this_sentence = False  # a clause of the CURRENT sentence was withdrawn
        gap_lost = False             # a withdrawn clause took the only blank before the next kept one
        withdrawn_values: set = set()  # the figures this line's withdrawn clauses stated

        line_index = len(produced_at) - 1
        heads_a_list = (not list_item and bool(re.match(r"^\s*(?:\*\*|#{1,4}\s)", line))
                        and line_index + 1 < len(source_lines)
                        and bool(re.match(r"^\s*(?:[-•]|\*(?!\*)|\d+[.)])\s+", source_lines[line_index + 1])))

        def _keeps_its_block(remnant: str) -> bool:
            """A route header that lost its figure keeps the route's name: "**Bow — 25 minutes each
            way**" goes to "**Bow**", so the leg list under it is not left hanging under the
            previous route's block (r17 8c35fd5b: "- One change." shipped under Stratford)."""
            if not heads_a_list:
                return False
            plain = re.sub(r"[*_#`]+", " ", remnant).strip(" \t—–-:,")
            named = _line_binding(plain, bindings) if plain else None
            return bool(named and named.get("labels") and not named.get("members")
                        and _label_key(plain) in {_label_key(label) for label in named["labels"]})

        def _close_sentence() -> None:
            # A sentence that lost its claim and kept only a lead-in ("If you want to continue
            # here, for example,") or a bare label ("For your commute") goes whole (R2-01 e/f).
            nonlocal sentence_start, sentence_dropped
            remnant = "".join(kept[sentence_start:])
            if sentence_dropped and _stub_remnant(remnant) and not _keeps_its_block(remnant):
                del kept[sentence_start:]
            sentence_start = len(kept)
            sentence_dropped = False

        for index in range(0, len(parts), 2):
            fragment = parts[index]
            separator = parts[index + 1] if index + 1 < len(parts) else ""
            context_fragment = last_fragment
            if colon_passed and index >= 2 and _SENTENCE_END.search(parts[index - 1]):
                heading_now[0] = ""          # the labelled sentence has ended
            colon_passed = colon_passed or ":" in separator
            last_fragment = "" if _SENTENCE_END.search(separator) else fragment
            local = _line_binding(fragment, bindings)
            if local is not None:
                # A comma-delimited address component ("North Approach, Watford, WD25")
                # must not switch the listing's binding to a route measured from the town.
                nested_component = bool(inherited and local.get("labels") and any(
                    short.lower() in long.lower() and short.lower() != long.lower()
                    for short in local["labels"] for long in inherited.get("labels") or []))
                # A listing row's Area cell ("| Hackney |") names the listing's area, not a
                # switch to that area's route (r08 NEW-B).
                # A table row or a listing's list item; never prose ("for Islington as an area,
                # the TfL Journey Planner gives … 15 minutes" is the area route, r15 40a2522f).
                area_cell = bool(inherited and inherited.get("listing") and not local.get("listing")
                                 and not _asserts_commute(fragment)
                                 and (line.lstrip().startswith("|") or list_item))
                # "… than Camden but a much shorter commute" names Camden as what the line's
                # subject is compared WITH; the subject stays (r11 97bdde65).
                compared_with = local is not None and local is _comparison_target(fragment, bindings)
                if (not nested_component or _asserts_commute(fragment) or "→" in fragment) \
                        and not area_cell and not compared_with:
                    inherited = local
            # A parenthetical already checked against its own clause is not re-judged as part
            # of the enclosing clause: "Walk to the exit (5 min)" kept its leg figure above,
            # then lost it here because "the exit" is not on the route (R2-01 a).
            claim = fragment
            for aside in vetted:
                if aside in claim:
                    claim = claim.replace(aside, " ")
            if (_COMMUTE_WORD.search(_TOPIC_LIST.sub(" ", fragment))
                    and not _denies_commute(fragment)):
                commute_context = True
            # A clause that quotes money and no journey time compares money: "but the median
            # of £2,400 pcm is £200 pcm above your ceiling" after "the commute is excellent"
            # was withdrawn as a commute verdict (r06 ecbf2c1f).
            monetary_or_independent = (re.search(
                r"\brent\b|\bbudget\b|\bfare\b|\bcost\b|\bweather\b|\btemperature\b|"
                r"\bfurniture\b|\bbills?\b|\bdeposit\b|月租|租金|预算|票价|费用|天气|气温|家具|账单|押金",
                fragment, re.I) or _denies_commute(fragment)
                or bool(_MONEY_FIGURE.search(fragment) and not _MINUTE_TOKEN.search(fragment)))
            # The context rule is for a clause that leans on an earlier commute mention. A clause
            # that names the commute ITSELF is judged by ``_asserts_commute`` alone, whose
            # exemptions (a generic "within a certain commute time" offer) the context rule
            # used to override (stress B-02 80e0de95: a clarification turn got the Note).
            # "- Versus Holloway (24 min): 3 minutes each way, …, roughly an hour a week": a
            # time total after the line's own journey figures is a journey claim too.
            period_claim = bool(figure_context and _period_total(claim) and not monetary_or_independent)
            if _MINUTE_TOKEN.search(fragment):
                figure_context = True
            # "…, 38 x 3 = 114 a week" after the line's journey figures is journey arithmetic too.
            period_claim = period_claim or bool(figure_context and _arithmetic.products(claim)
                                                and not monetary_or_independent)
            asserts = period_claim or (_asserts_commute(claim) or
                       (commute_context and not _COMMUTE_WORD.search(claim)
                        and (_FIT_WORD.search(_without_money_or_weather(claim))
                             or _duration_bounds(claim) or _period_total(claim))
                        and not monetary_or_independent
                        and not ((_ADVICE_CLAUSE.match(claim) or _SPECULATION.search(claim))
                                 and not _MINUTE_TOKEN.search(claim))))
            sentence_marked = marked_at.get(index, False)
            # A bare figure split off by a dash belongs to the clause before it: "Northern
            # line from King's Cross St. Pancras to Moorgate — 6 min" is the leg's own measured
            # time, judged alone it named no leg and was withdrawn (R3-01, R2-01 a).
            bare_figure = bool(_BARE_FIGURE.fullmatch(claim.strip()))
            drop = fragment.strip() and asserts and not _evidenced(
                claim, _the_listing_referred_to(claim, inherited, local, context_fragment, bindings),
                context=context_fragment if bare_figure else "",
                sentence_marked=sentence_marked, previous_clause=context_fragment)
            # A dependent clause after a redacted one restates its verdict without repeating
            # its subject ("…, which is well within your limit"), so it goes with it.
            # Across a sentence boundary only a dependent VERDICT still leans on the withdrawn
            # clause ("... 12 minutes. Which is within your limit."); a new sentence such as
            # "That is an estimate, not a timetable figure" stands on its own (R2-01 c).
            within = dropped_in_sentence and not crossed_sentence
            if (not drop and dropped_in_sentence and fragment.strip()
                    # a verdict on money ("That is £250 over your ceiling") leans on no journey
                    and ((_CONTINUATION.match(fragment) and _FIT_WORD.search(_without_money_or_weather(fragment))
                          and not _evidenced(fragment, inherited,
                                             sentence_marked=sentence_marked))
                         or (within and _dependent_commute_aside(fragment)))):
                drop = True
            if (not drop and within and fragment.strip()
                    and _DANGLING_AFTER_DROP.match(fragment)):
                # "Cheapest route: with the same line pattern." / "Cons - the longest of the
                # three.": what is left of a sentence whose figure went cannot stand (R1-10).
                drop = True
            if not drop and withdrawn_this_sentence and _SO_TOTAL_AFTER_DROP.match(fragment):
                drop = True
            if not drop and withdrawn_values and any(
                    any(abs(value - gone) < 1e-9 for value, _unit in product["operands"] for gone in withdrawn_values)
                    for product in _arithmetic.products(claim)):
                # A step on a withdrawn figure goes with it: "19 minutes each way, so 38 minutes a
                # day, 38 x 3 = 114 a week" shipped "- Per day: 38 x 3 = 114 a week" (r19 W3b).
                drop = True
            if (not drop and address_tail_open and _ADDRESS_TAIL.fullmatch(fragment)):
                # "35 min to Canary Wharf, London E14 5AB": the comma split the destination,
                # and the withdrawn clause left "Commute: London E14 5AB." (r07 97b3d456).
                drop = True
            address_tail_open = False
            if drop:
                address_tail_open = separator.strip() in {",", "，"}
                withdrawn_this_sentence = not _SENTENCE_END.search(separator)
                # The blank that separated this clause from what follows goes with it; the next
                # kept clause must still be spaced ("£2,250/month.I'm mentioning…", r18 1787af1e).
                gap_lost = gap_lost or bool(re.search(r"\s", fragment[:1] + separator))
                withdrawn_values.update(_minute_claim_values(fragment))
                withdrawn_values.update(p["result"] for p in _arithmetic.products(fragment))
                if _MINUTE_TOKEN.search(fragment) and any(_named_legs(fragment, route) for route in itinerary_routes):
                    # A leg of an enumerated itinerary is wrong: what is left ("a short walk, the
                    # 476 bus.") is no longer the route the planner returned, so the sentence goes
                    # whole (r17 4f27cfd2, r6-O4: "remove it when the total or leg figure is wrong").
                    continuation_broken = True
                removed.append(fragment)
                if (len(kept) > sentence_start
                        and re.match(r"^\s*(?:so\s+|and\s+|but\s+)?(?:if|when|unless|once|whenever)\b[^.!?]*[,，]\s*$",
                                     kept[-1], re.I)):
                    # The condition lost the main clause it led into: "— so if Bow is on your
                    # shortlist, <withdrawn>." left "so if Bow is on your shortlist." (r16).
                    removed.append(kept.pop())
                    if len(kept) > sentence_start:
                        kept[-1] = re.sub(r"\s+[–—-]\s+$", "", kept[-1])
                dropped_in_sentence = True
                sentence_dropped = True
                crossed_sentence = bool(_SENTENCE_END.search(separator))
                if _SENTENCE_END.search(separator):
                    # The sentence ends with the withdrawn clause. What is left of it either
                    # goes whole (a stub) or keeps the full stop, so it does not run on into
                    # the next sentence ("B is nice, C is nice.").
                    opened_at = sentence_start
                    _close_sentence()
                    if len(kept) > opened_at:
                        kept[-1] = re.sub(r"[\s,，；;、:：–—-]+$", "", kept[-1]) + separator
                elif separator.strip() == "|" and line.lstrip().startswith("|"):
                    # A table cell keeps its column: the row would otherwise shift left.
                    kept.append(" — " + separator)
                elif kept and re.search(r"\s+[–—-]\s+$", kept[-1]):
                    # "pick — <withdrawn>, for only £150 more" shipped as "pick —  for only"
                    # (r12 8573f129): the dash introduced the withdrawn clause, it goes too.
                    kept[-1] = re.sub(r"\s+[–—-]\s+$", " ", kept[-1])
                continue  # its trailing separator goes with it
            if within:
                if (_LEANING_CONTINUATION.match(fragment)
                        and (_CAVEAT_ON_PREVIOUS.search(fragment)
                             or _CONCLUSION_ON_PREVIOUS.match(fragment)
                             # "If commute is your priority, <withdrawn> — but confirm rents
                             # there first": the condition lost its main clause (r13).
                             or _CONDITIONAL_LEAD.match("".join(kept[sentence_start:])))):
                    continuation_broken = True
                # "Features: and the listing says ..." - a coordinator left at the front.
                fragment = _LEADING_COORDINATOR.sub(r"\1", fragment)
            if crossed_sentence:
                # The withdrawn clause ended its sentence and this one starts a new sentence that
                # nothing was withdrawn from: "That is £250 over your ceiling, so it is not within
                # budget" is not "dependent" on the withdrawn journey a sentence earlier (r18).
                dropped_in_sentence = False
            crossed_sentence = False
            if (gap_lost and kept and kept[-1][-1:] and not kept[-1][-1:].isspace()
                    and fragment[:1] and not fragment[:1].isspace()):
                fragment = " " + fragment
            gap_lost = False
            kept.append(fragment + separator)
            if _SENTENCE_END.search(separator):
                withdrawn_this_sentence = False
                dropped_in_sentence = False
                commute_context = False
                _close_sentence()
        _close_sentence()

        if len(removed) == removed_before_line:
            # Nothing was withdrawn from this line: keep it byte for byte. The tidy-up below
            # used to strip a trailing ":" and that difference alone attached "some commute
            # claims ... have been removed" to answers with no commute claim at all (R1-10).
            lines.append(line)
            continue
        rebuilt = "".join(kept)
        for between, tail in split_coordinators:
            if not tail.strip():
                continue
            found = re.search(r"[ ,，]*;[ \t]*(?=" + re.escape(tail) + ")", rebuilt)
            if not found:
                continue
            head = rebuilt[:found.start()].rstrip()
            if not head or head.endswith((".", "!", "?", "。", "！", "？")):
                # The clause the coordinator joined was withdrawn: what follows is only a
                # subordinate clause ("While Bow comes in roughly 4 minutes over.", r15), so
                # it goes to the end of its sentence.
                close = re.search(r"[.!?。！？](?=\s|$)", rebuilt[found.end():])
                cut = found.end() + (close.end() if close else len(rebuilt) - found.end())
                rebuilt = (rebuilt[:found.start()] + " " + rebuilt[cut:]).strip()
            else:
                rebuilt = rebuilt[:found.start()] + between + rebuilt[found.end():]
        if continuation_broken or _unbalanced_after_removal(rebuilt, original_body):
            # No clean clause cut exists: a bracket lost its partner or a kept clause leans on
            # the withdrawn one. Drop each sentence a withdrawal touched, whole (R3-01).
            rebuilt = _without_touched_sentences(original_body, removed[removed_before_line:])
        if re.fullmatch(r"[*_#\s]*[^:：\n]{1,40}[:：]\s*[*_]*\s*", rebuilt) and not _MINUTE_TOKEN.search(rebuilt):
            rebuilt = ""         # "Cheapest route:" with nothing left after it (R1-10)
        label = original_body.split(":", 1)[0] if ":" in original_body[:80] else ""
        if (label.strip() and len(re.findall(r"[\w'’-]+", label)) <= 6
                and rebuilt.strip(" .:：;*") == label.strip(" .:：;*")):
            # Everything after a bare label went: "- Versus Camden (35 min)." shipped as a stub
            # bullet (r12 53bfec0e). The bullet goes whole. A complete sentence before the
            # colon ("If you want the better balance of rent and travel, Stratford wins") is
            # not a label and stays (r13 control 3709647b).
            rebuilt = ""
        rebuilt = re.sub(r"(?<=\S) {2,}(?=\S)", " ", rebuilt)
        rebuilt = re.sub(r"[,，;；:：]+\s*(?=[.!?。！？](?:\s|$))", "", rebuilt)   # "to spare,." (r14 follow-up)
        # "If commute is your priority,confirm rents" (r13): a kept comma keeps its space.
        rebuilt = re.sub(r"(?<=[A-Za-z)])([,;])(?=[A-Za-z])", r"\1 ", rebuilt)
        # Tidy up after removal: no doubled or dangling separators.
        rebuilt = re.sub(r"([,，；;、:：])\s*(?=[,，；;、:：])", "", rebuilt)
        rebuilt = re.sub(r"^[\s,，；;、]+", "", rebuilt)
        rebuilt = re.sub(r"[\s,，；;、:：–—]+$", "", rebuilt)
        if rebuilt and _HAS_CONTENT.search(body) and not _HAS_CONTENT.search(rebuilt):
            rebuilt = ""
        if rebuilt and re.search(r"[.!?。！？]\s*$", body) and not re.search(
                r"[.!?。！？]\s*$", rebuilt):
            rebuilt += "。" if zh else "."
        rebuilt = _recapitalise(rebuilt, original_body)

        if line.lstrip().startswith("|") and rebuilt.strip():
            cells = [c.strip(" *_") for c in rebuilt.strip().strip("|").split("|")]
            if len(cells) > 1 and all(c in ("", "—", "–", "-") for c in cells[1:]):
                rebuilt = ""      # "| Per week | — | — | — |" reads as data; the row goes (r19)
        if not rebuilt.strip():
            # The line was nothing but a commute claim; drop it, prefix included. A blank
            # separator line in the original stays blank so paragraphs do not fuse.
            if not _HAS_CONTENT.search(line):
                lines.append(line)
            continue
        # Each assertion was checked with its own entity above. Rechecking the reconstructed
        # WHOLE line joins unrelated clauses ("commute unknown; rent within budget") and
        # used to discard the entire answer, including successful weather/detail/fare facts.
        # The emphasis check reads the WHOLE line: "**Heading" matches the bullet prefix "*".
        lines.append(_balance_emphasis(prefix + rebuilt, line))

    emptied = {produced_at[header + offset] for header, kept_any in tables_kept.items() if not kept_any
               for offset in (0, 1) if header + offset < len(produced_at)}
    if emptied:
        lines = [line for index, line in enumerate(lines) if index not in emptied]
        produced_at = [at - sum(1 for e in emptied if e < at) for at in produced_at]
    lines = _without_orphaned_lead_ins(source_lines, produced_at, lines)
    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if not _HAS_CONTENT.search(result):
        return None
    return result


def _without_orphaned_lead_ins(source: list, produced_at: list, lines: list) -> list:
    """Drop a kept lead-in ending in ":" whose entire list was withdrawn: "the weekly saving
    from choosing Kentish Town over the others is:" shipped with no bullets under it (r13)."""
    ends = produced_at[1:] + [len(lines)]
    kept = [ends[i] > produced_at[i] for i in range(len(source))]
    drop: set[int] = set()
    for i, line in enumerate(source):
        if not kept[i] or not line.rstrip().rstrip("*_ ").endswith((":", "：")):
            continue
        block = []
        for j in range(i + 1, len(source)):
            if not source[j].strip():
                if block:
                    break
                continue
            if not _BULLET_PREFIX.match(source[j]) or not re.match(r"^\s*(?:[-*•]|\d+[.)])\s", source[j]):
                break
            block.append(j)
        if block and not any(kept[j] for j in block):
            drop.add(produced_at[i])
    return [line for index, line in enumerate(lines) if index not in drop]



_COMPARATIVE_DIFFERENCE = re.compile(
    r"\b(?:shorter|longer|quicker|faster|slower|less|more|saves?|saving|extra|difference|"
    r"cuts?|adds?)\b|更短|更长|更快|更慢|节省|省|多出|相差", re.I)


_BARE_FIGURE = re.compile(
    r"(?:about|around|roughly|approx(?:imately|\.)?|~|≈|约|大约)?\s*\d{1,3}(?:\.\d{1,2})?"
    r"(?:\s*[-–]\s*\d{1,3})?\s*[-–]?\s*(?:minutes?|mins?|分钟)\.?", re.I)
# A kept clause that leans on the withdrawn one before it: "…25 to 35 minutes, but I have
# not verified that with a live TfL plan" leaves "I have not verified that" pointing at
# nothing. "and" is not here: "…, and the rent is £1,500" stands on its own.
_LEANING_CONTINUATION = re.compile(
    r"^\s*(?:but|so|yet|though|although|whereas|however)\b|^\s*(?:但是|但|所以|因此|不过|然而)", re.I)
# ... and the leaning clause is a caveat ABOUT the withdrawn claim ("I have not verified that",
# "before relying on it"). "…, but it is the largest of the three" is a fact of its own.
_CAVEAT_ON_PREVIOUS = re.compile(
    r"\b(?:verif\w*|confirm\w*|check\w*|measur\w*|rely\w*|trust\w*)\b[^.;!?]{0,20}?"
    r"\b(?:that|this|it|them|these|those)\b(?!\s+(?:is|are|was|has)\b)|"
    r"(?:核实|确认|验证)(?:过)?(?:这|该|此)", re.I)
# ... or a conclusion drawn FROM it: "…or a 20-25 minute walk, so the real figure is likely at
# the lower end of that range" left "In practice, so the real figure is likely …" (stress C-08).
_CONCLUSION_ON_PREVIOUS = re.compile(
    r"^\s*(?:so|therefore|hence|thus)\b[^.;!?]*\b(?:that|this|it|these|those)\b|^\s*(?:所以|因此)", re.I)
_MONEY_FIGURE = re.compile(r"[£$€]\s*\d")
_CONDITIONAL_LEAD = re.compile(
    r"^\s*[*_]*\s*(?:if|when|unless|should|whether|assuming|provided)\b[^.!?]*[,，]\s*$", re.I)
# What is left of a destination split at its comma: "London E14 5AB", "E8", "London".
_ADDRESS_TAIL = re.compile(
    r"\s*(?:(?:Greater\s+)?London|(?:[A-Z][A-Za-z'’&-]+\s+){0,3}[A-Z]{1,2}\d[A-Z\d]?(?:\s*\d[A-Z]{2})?)\s*")
_BRACKET_PAIRS = (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"))


def _unbalanced_after_removal(rebuilt: str, original: str) -> bool:
    """True when a withdrawal left a bracket without its partner (R3-01)."""
    return any(rebuilt.count(o) - rebuilt.count(c) != original.count(o) - original.count(c)
               for o, c in _BRACKET_PAIRS)


_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s|$)|[。！？]")


def _without_touched_sentences(body: str, removed_fragments: list) -> str:
    """``body`` without every sentence that contains one of ``removed_fragments``.

    The whole-sentence fallback for a withdrawal with no clean clause cut: "drop the
    sentence rather than leave a fragment". Abbreviation and decimal points do not end a
    sentence ("St. Pancras", "7.79 km")."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(body):
        position = match.start()
        if body[position] == ".":
            if re.search(r"\b" + _NAME_ABBREVIATION + r"\.$", body[:position + 1], re.I):
                continue
            if position + 1 < len(body) and body[position + 1].isdigit():
                continue
        sentences.append(body[start:match.end()])
        start = match.end()
    if body[start:].strip():
        sentences.append(body[start:])
    keys = []
    for fragment in removed_fragments:
        key = " ".join(str(fragment or "").split())
        key = key.strip(" ,，;；:：")[:30]
        if _HAS_CONTENT.search(key):
            keys.append(key)
    kept = [sentence for sentence in sentences
            if not any(key in " ".join(sentence.split()) for key in keys)]
    if len(kept) == len(sentences):
        return ""            # the withdrawal cannot be located: keep nothing of this line
    return "".join(kept).strip()


def _stub_remnant(text: str) -> bool:
    """True when what is left of a sentence after a withdrawal cannot stand alone.

    That is a lead-in whose continuation went ("If you want to continue here, for example,"),
    or a few words with no fact in them ("For your commute", "Oak Row:"). A remnant carrying
    a figure, a price or a real clause stays: "Flat A, NW1 - £1,500/month, it has a garden."
    """
    plain = re.sub(r"[*_#`>|]+", " ", str(text or "")).strip()
    plain = _BULLET_PREFIX.sub("", plain).strip()
    if not _HAS_CONTENT.search(plain):
        return True
    if _LEAD_IN_TAIL.search(plain):
        return True
    if (_SUBORDINATE_ONLY.match(plain) or (_PREPOSITION_LED.match(plain) and not _FINITE_VERB.search(plain))) \
            and not re.search(r"[,;]\s*\w", plain):
        # Only a subordinate clause is left: "While Bow comes in roughly 4 minutes over.",
        # "With 3 office days a week (return trips)." (r15). The sentence goes whole.
        return True
    if re.search(r"\d|£", plain):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*|[\u4e00-\u9fff]", plain)
    return len(words) <= 4


# A clause led by a subordinating conjunction is a fragment on its own; one led by a
# preposition ("With three office days that's 9 x 2 x 3 = 54 minutes a week") is a fragment
# only when no verb follows it ("With 3 office days a week (return trips).").
_SUBORDINATE_ONLY = re.compile(
    r"^\s*(?:while|whilst|whereas|although|though|because|since|unless|if|when|whenever|so\s+that|"
    r"so\s+if|which|who)\b", re.I)
_PREPOSITION_LED = re.compile(r"^\s*(?:with|without|as|after|before|for|at)\b", re.I)
_FINITE_VERB = re.compile(
    r"\b(?:is|are|was|were|be|been|that['’]s|it['’]s|there['’]s|takes?|took|saves?|gives?|comes?|has|have|"
    r"had|would|will|can|could|means?|works?|adds?|makes?|puts?|gets?)\b", re.I)
_LEAD_IN_TAIL = re.compile(
    r"(?:\bfor\s+example|\bfor\s+instance|\be\.g\.?|\bsuch\s+as|\bnamely|\bi\.e\.?|"
    r"\bin\s+particular|\bspecifically|\bincluding|例如|比如)[\s,，:：]*$", re.I)


def _balance_emphasis(rebuilt: str, original: str) -> str:
    """Close or drop a ``**`` whose partner went with a withdrawn clause.

    "**Comparing Camden, Hackney and Islington — …, commute to King's Cross**" lost its
    tail and shipped as "**Comparing Camden" (R2-01 d)."""
    if rebuilt.count("**") % 2 == 0 or original.count("**") % 2 == 1:
        return rebuilt
    if original.rstrip().rstrip(".:：。").endswith("**"):
        body = rebuilt.rstrip()
        trailing = rebuilt[len(body):]
        stop = body[-1] if body and body[-1] in ".:：。" else ""
        body = body[:len(body) - len(stop)].rstrip()
        return body + "**" + stop + trailing
    position = rebuilt.rfind("**")
    return (rebuilt[:position] + rebuilt[position + 2:]).replace("  ", " ")

# What is left of a sentence after its commute figure was withdrawn and that cannot stand
# alone: a dependent continuation or a bare comparative about the removed figure.
_DANGLING_AFTER_DROP = re.compile(
    r"^\s*(?:(?:[-–—:]\s*)?(?:with|which|that|whereas|while|making it|so it|so this)\b"
    # "…, plus the extra change at Mile End" adds to the withdrawn clause's subject (r17).
    r"|(?:plus|as\s+well\s+as|along\s+with|together\s+with|on\s+top\s+of)\b"
    r"|(?:the\s+)?(?:longest|shortest|fastest|slowest|quickest|longer|shorter|faster|slower|quicker)\b"
    r"|(?:是|为)?(?:最长|最短|最快|最慢|较长|较短)(?:的)?(?:通勤)?\s*$)", re.I)
_LEADING_COORDINATOR = re.compile(r"^(\s*)(?:and|but|or|plus|also)\s+(?=\S)", re.I)
# "<withdrawn base>, so 22 minutes of travel per day" is a total OF the withdrawn figure and
# cannot stand without it (r18 eb65e918). Only within the withdrawn clause's own sentence.
_SO_TOTAL_AFTER_DROP = re.compile(r"^\s*so\b(?=[^.!?]{0,120}\d)", re.I)


def _quotes_source_text(fragment: str, source_texts: list[str] | None) -> bool:
    """True when every minute figure in ``fragment`` is quoted, with its own words, from a
    tool result of this turn — "3 mins walk to British Library" from the listing's own
    features is the listing's claim, not the model's, and must not be withdrawn (R1-10)."""
    if not source_texts:
        return False
    figures = list(_MINUTE_TOKEN.finditer(fragment))
    if not figures:
        return False
    blob = " ".join(" ".join(str(t).split()) for t in source_texts).lower()
    for figure in figures:
        # The figure plus up to three of its own following words ("3 mins walk to"), so a
        # bare "25 minutes" never matches some unrelated listing sentence.
        tail = re.match(r"[^,;.!?。，；！？]{0,40}", fragment[figure.end():]).group()
        words = " ".join(tail.lower().split()[:3])
        quoted = " ".join(figure.group().lower().split()) + ((" " + words) if words else "")
        if not words or quoted not in blob:
            return False
    return True


def _commute_unverified_note(zh: bool) -> str:
    """Says SOME, because redaction is per listing — an evidenced figure is still standing."""
    return ("注：部分通勤结论缺少可对应的证据，已从回答中移除。"
            if zh else
            "Note: some commute claims could not be verified and have been removed from this answer.")


def render_structured_evidence_fallback(state: dict, *, language: str | None = None) -> str:
    """Render independent facts from THIS turn's completed artifacts, without an LLM.

    Safe for provider-error degradation after tools have completed. It makes no candidate
    eligibility claim, computes no fare coverage/combined budget from incomplete inputs,
    and never treats a fastest itinerary as a wheelchair-accessibility guarantee. Empty
    means no supported evidence is available. Callers must pass current-turn artifacts.
    """
    language = language or str((state.get("extracted_context") or {}).get("reply_language") or "en")
    zh = language.lower().startswith("zh")
    lines: list[str] = []

    def add(en: str, chinese: str) -> None:
        value = chinese if zh else en
        if value and value not in lines:
            lines.append(value)

    def plain(value: Any, limit: int = 240) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    artifacts = state.get("tool_artifacts") or []
    usable_tools = {a.get('tool') for a in artifacts if _usable_artifact(a)}
    has_forecast = any(_usable_artifact(a) and a.get('tool') == 'get_weather'
                       and a['raw_data'].get('weather_type') == 'forecast' for a in artifacts)
    for artifact in artifacts:
        if not _usable_artifact(artifact):
            if (isinstance(artifact, dict) and artifact.get('tool') in {'get_transport_info', 'get_weather'}
                    and not any(artifact.get(flag) for flag in ('timed_out','denied','abandoned','outcome_unknown'))):
                name = 'TfL transport' if artifact['tool'] == 'get_transport_info' else 'Weather forecast'
                chinese = 'TfL交通信息' if artifact['tool'] == 'get_transport_info' else '天气预报'
                if artifact['tool'] in usable_tools:
                    add(f'Some requested {name.lower()} lookups failed. Successful results below apply only to their stated location and time.',
                        f'部分{chinese}查询失败；下方成功结果仅适用于注明的地点和时间。')
                else:
                    add(f'{name} could not be verified this turn; the service did not return a usable result.',
                        f'本轮未能核实{chinese}，服务未返回可用结果。')
            continue
        tool, raw = artifact.get("tool"), artifact["raw_data"]
        if tool == "get_weather":
            place = plain(raw.get("location"))
            forecast = raw.get('weather_type') == 'forecast'
            temp = raw.get('temperature', raw.get('forecast_temp')) if forecast else raw.get('current_temp')
            condition = plain(raw.get("condition"))
            parts = [part for part in (f"{temp}°C" if isinstance(temp, (int, float))
                                       and not isinstance(temp, bool) else "", condition) if part]
            if parts:
                if forecast:
                    valid_at = plain(raw.get('valid_at') or raw.get('requested_at'))
                    add(f"Weather forecast for {place} at {valid_at} (Europe/London): {', '.join(parts)}.",
                        f"{place} 在 {valid_at}（伦敦时间）的天气预报：{'，'.join(parts)}。")
                    if raw.get('assumed_time'):
                        add('No forecast time was specified; 09:00 was used for the morning check.',
                            '未指定预报时刻，本次以早上09:00查询。')
                else:
                    add(f"Current weather returned for {place}: {', '.join(parts)}.",
                        f"{place} 返回的当前天气：{'，'.join(parts)}。")
                    if has_forecast:
                        add('This lookup returned current conditions; the separately dated result below is the forecast.',
                            '此项查询返回当前天气；下方另行注明日期的是预报结果。')
                    else:
                        add("Only current conditions were returned; no multi-day forecast is verified.",
                            "本次只返回当前天气，未核实未来数日预报。")
                rain = raw.get('rainfall_chance')
                if isinstance(rain, (int, float)) and not isinstance(rain, bool):
                    add(f'Returned rain probability: {rain:g}%.', f'返回的降雨概率：{rain:g}%。')
                else:
                    add("A numerical rain probability was not returned.", "未返回具体降雨概率。")
        elif tool == "get_property_details":
            prop = raw.get("property") or {}
            if raw.get("found") is False or not isinstance(prop, dict):
                continue
            parts = [plain(prop.get(key)) for key in ("address", "price", "room_type") if prop.get(key)]
            if parts:
                add("Returned listing details: " + "; ".join(parts) + ".",
                    "返回的房源详情：" + "；".join(parts) + "。")
                if prop.get("amenities"):
                    add("Reported features: " + plain(prop["amenities"]) + ".",
                        "房源所列配置：" + plain(prop["amenities"]) + "。")
                availability = plain(prop.get("available_from"))
                add("Availability: " + (availability or "not provided") + ". Confirm current availability with the agent.",
                    "可入住信息：" + (availability or "未提供") + "。当前是否仍可租须向中介核实。")
                note = _cached_listing_note([raw, prop], zh=zh)
                if note:
                    lines.append(note)
        elif tool == "search_nearby_pois":
            ref = raw.get("reference_point") or {}
            place = plain(ref.get("query") or raw.get("address"))
            for kind, records in (raw.get("pois") or {}).items():
                if not isinstance(records, list):
                    continue
                for poi in records[:5]:
                    if not isinstance(poi, dict) or not poi.get("name"):
                        continue
                    distance = plain(poi.get("distance_display"))
                    if not distance and isinstance(poi.get("distance_m"), (int, float)):
                        distance = f"{poi['distance_m']:g} m"
                    add(f"Nearby record ({kind}): {plain(poi['name'])}" +
                        (f", {distance}" if distance else "") + ".",
                        f"周边记录（{kind}）：{plain(poi['name'])}" +
                        (f"，{distance}" if distance else "") + "。")
            if raw.get("pois"):
                precision = plain(ref.get("precision") or "unknown")
                add(f"Nearby reference: {place} (precision: {precision}). Distances are straight-line, not walking routes or verified property access.",
                    f"周边查询参考点：{place}（精度：{precision}）。距离为直线距离，不是步行路线，也未核实房源的实际通行条件。")
        elif tool == "check_safety":
            crime = raw.get("crime_data") or {}
            if not isinstance(crime, dict):
                continue
            total = crime.get("total_crimes_6m")
            months = crime.get("months_covered", raw.get("months_covered"))
            dates = crime.get("data_months") or []
            if isinstance(total, (int, float)) and isinstance(months, int) and months > 0:
                place = plain(raw.get("address"))
                add(f"Police records returned for {place}: {total:g} incidents across {months} covered months" +
                    (f" ({', '.join(map(str, dates))})" if dates else "") + ".",
                    f"{place} 返回的警方记录：覆盖 {months} 个月，共 {total:g} 起" +
                    (f"（{'、'.join(map(str, dates))}）" if dates else "") + "。")
                add("Recorded incidents depend on coverage and footfall; they do not establish personal safety or a London-wide ranking.",
                    "记录数量受覆盖范围和人流影响，不能据此保证个人安全或推断全伦敦排名。")
                if raw.get("caveat"):
                    lines.append(plain(raw["caveat"], 700))
        if tool in {"calculate_commute", "calculate_commute_cost", "get_transport_info"}:
            journey = raw.get("commute") if tool == "calculate_commute_cost" else raw
            if isinstance(journey, dict):
                value = _measured_minutes(journey)
                origin = _place_labels(raw.get("from_address") or raw.get("from"))
                target = _place_labels(raw.get("to_address") or raw.get("to"))
                if value is not None and origin and target:
                    # A station-to-station plan is labelled with the STATIONS it ran between:
                    # "Highgate West Hill, London N6 → UCL … 11 minutes" read as door-to-door
                    # when TfL had resolved the origin to Archway station (R1-14).
                    ends = [_journey_end_label(raw.get("from")), _journey_end_label(raw.get("to"))]
                    if any(end for end in ends):
                        start = ends[0] or (origin[0], None)
                        finish = ends[1] or (target[0], None)
                        add(f"Returned journey: {_render_end(start, False)} → {_render_end(finish, False)}, "
                            f"{value:g} minutes station to station (the planner's fastest returned "
                            "itinerary; walking to and from the stations is not included).",
                            f"返回的路线：{_render_end(start, True)} → {_render_end(finish, True)}，"
                            f"站到站 {value:g} 分钟（路线规划器返回的最快行程，不含往返车站的步行）。")
                    else:
                        add(f"Returned journey: {origin[0]} → {target[0]}, {value:g} minutes (the planner's fastest returned itinerary).",
                            f"返回的路线：{origin[0]} → {target[0]}，{value:g} 分钟（路线规划器返回的最快行程）。")
                    add("This origin-specific itinerary does not verify another property's travel time, arrival reliability, or step-free access.",
                        "该行程仅对应上述起点，不代表其他房源的通勤时长，也未证明准点到达或无台阶通行。")
            if tool == "calculate_commute_cost":
                cost = raw.get("transport_cost") or {}
                if isinstance(cost, dict) and cost.get("error"):
                    add("Travel cost remains unverified: " + plain(cost["error"]) + ".",
                        "交通费用仍未核实：" + plain(cost["error"]) + "。")
        if tool == "get_transport_info":
            if raw.get("fare_available") and isinstance(raw.get("fare_gbp"), (int, float)):
                fare = raw["fare_gbp"]
                origin = _place_labels(raw.get("from"))
                target = _place_labels(raw.get("to"))
                route = f" ({origin[0]} → {target[0]})" if origin and target else ""
                add(f"Returned one-way fare{route}: £{fare:.2f}; this is not a return fare or a monthly total.",
                    f"返回的单程票价{route}：£{fare:.2f}，并非往返或每月总额。")
            for line in raw.get("lines") or []:
                if isinstance(line, dict) and line.get("name") and line.get("status"):
                    add(f"Returned line status: {plain(line['name'])}: {plain(line['status'])}.",
                        f"返回的线路状态：{plain(line['name'])}：{plain(line['status'])}。")
        if tool in {"get_transport_info", "check_transport_cost"}:
            tariff = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            prices = tariff.get("prices") or {}
            parts = []
            for number_key, display_key, label in (
                ("monthly_gbp", "monthly_pass", "monthly"),
                ("weekly_gbp", "weekly_pass", "weekly"),
                ("daily_cap_gbp", "daily_cap_payg", "daily PAYG cap"),
            ):
                value = tariff.get(number_key)
                amount = f"£{value:.2f}" if isinstance(value, (float, int)) and not isinstance(value, bool) else plain(prices.get(display_key))
                if amount:
                    period = {"monthly": "每月", "weekly": "每周", "daily PAYG cap": "每日随乘随付上限"}[label] if zh else label
                    parts.append(f"{period}: {amount}")
            if parts:
                zone = plain(tariff.get("zones"))
                add(f"Returned {zone} tariff: {'; '.join(parts)}. Required route coverage is not established by this price table.",
                    f"返回的 {zone} 票价表：{'；'.join(parts)}。该价格表本身不能证明所需行程的覆盖分区。")
                edition = plain(tariff.get("effective_date") or tariff.get("fare_edition"))
                if edition:
                    add(f"Fare edition/effective date: {edition}.", f"票价版本或生效日期：{edition}。")
        if tool in {"calculate_commute_cost", "check_transport_cost"}:
            cost = raw.get("transport_cost") if tool == "calculate_commute_cost" else raw.get("data", raw)
            if not isinstance(cost, dict):
                continue
            days = cost.get("office_days_per_week", raw.get("office_days_per_week"))
            month_days = cost.get("commuting_days_per_month", raw.get("commuting_days_per_month"))
            if isinstance(days, (int, float)) and not isinstance(days, bool):
                add(f"Attendance used in the returned calculation: {days:g} days/week" +
                    (f", {month_days:g} commuting days/month" if isinstance(month_days, (int, float)) else "") + ".",
                    f"返回的费用计算采用每周出勤 {days:g} 天" +
                    (f"，每月通勤 {month_days:g} 天" if isinstance(month_days, (int, float)) else "") + "。")
            for key, period in (("payg_weekly_estimate", "week"), ("payg_monthly_estimate", "month")):
                value = cost.get(key)
                if isinstance(value, (float, int)) and not isinstance(value, bool):
                    upper = cost.get("payg_estimate_kind") == "daily_cap_upper_bound"
                    add(f"Returned PAYG {'daily-cap upper bound' if upper else 'estimate'}: £{value:.2f}/{period}.",
                        f"返回的随乘随付{'每日封顶价上界' if upper else '估算'}：£{value:.2f}/{'周' if period == 'week' else '月'}。")
            if isinstance(cost.get("monthly_cost"), (float, int)) and not isinstance(cost["monthly_cost"], bool):
                add(f"Returned recommended monthly travel estimate: £{cost['monthly_cost']:.2f}/month; this is not verified actual spending.",
                    f"返回的建议月交通费估算：£{cost['monthly_cost']:.2f}/月，并非已核实的实际支出。")
            if cost.get("coverage_verified") is False:
                add("Route fare-zone coverage remains unverified. " + plain(cost.get("coverage_note")),
                    "该行程所需票价分区仍未核实。" + plain(cost.get("coverage_note")))
    from core.commuting_estimate import estimate_monthly_commuting, render_monthly_commuting_estimate
    message = (state.get('extracted_context') or {}).get('current_message') or state.get('user_query') or ''
    for artifact in reversed(state.get('tool_artifacts') or []):
        if artifact.get('tool') != 'get_transport_info' or not _usable_artifact(artifact):
            continue
        estimate = estimate_monthly_commuting(message, artifact.get('raw_data') or {})
        if estimate:
            lines.append(render_monthly_commuting_estimate(estimate, language=language))
            break
    return "\n".join(lines)


def validate_evidence_scope(response: str, state: dict) -> str:
    """Withdraw provenance/scope claims unsupported by the relevant tool's OWN result.

    A nearby lookup's coordinates never establish the safety lookup's reference point;
    cache TTL freshness is never a live listing verification; a category not completed
    is not an empty category. Independent sentences survive each of these corrections.
    """
    text = _repair_abbreviation_breaks(str(response or ""))
    zh = str((state.get("extracted_context") or {}).get("reply_language") or "").startswith("zh")
    artifacts = [a for a in state.get("tool_artifacts") or [] if _usable_artifact(a)]
    for artifact in artifacts:
        scope = artifact['raw_data'].get('evidence_scope') or {}
        if (artifact.get('tool') == 'compare_or_rank_areas' and isinstance(scope, dict)
                and scope.get('rent_sample') == 'mixed_cached_listings'
                and scope.get('room_type_filtered') is False
                and scope.get('bedrooms_filtered') is False):
            scope_note = (
                '区域比较使用混合缓存房源，未按房型或卧室数筛选；预算匹配仅比较租金，'
                '新鲜度指缓存抓取时间，并非实时出租核验；通勤数字对应区域参考点，不能代表具体房源。'
                if zh else
                'The area comparison uses mixed cached listings without room-type or bedroom filtering; '
                'budget matching covers rent only, freshness describes the cache fetch time and availability is not verified live, '
                'and commute figures refer to area reference points, not individual homes.')
            if (scope.get('budget_match_basis') == 'rent_only'
                    and scope.get('freshness_basis') == 'cache_fetch_time'
                    and scope.get('live_verified') is False
                    and scope.get('commute_scope') == 'area_reference_point'
                    and scope_note not in text):
                text = f'{text}\n\n{scope_note}'.strip()
    safety = [a["raw_data"] for a in artifacts if a.get("tool") == "check_safety"]
    nearby = [a["raw_data"] for a in artifacts if a.get("tool") == "search_nearby_pois"]
    details = [a["raw_data"] for a in artifacts if a.get("tool") == "get_property_details"]
    cache_only = bool(details) and all(
        raw.get("from_cache") or raw.get("live_verified") is False
        or "cached" in _norm(raw.get("source"))
        or (raw.get("provenance") or {}).get("from_cache") for raw in details)
    safety_topic = re.compile(r"\bsafety\b|\bcrime\b|\bpolice\b|治安|犯罪|安全查询|安全检查", re.I)
    point_claim = re.compile(
        r"(?:listing|property|flat|home|exact|precise).{0,55}(?:coordinates?|cent(?:er|re))|"
        r"(?:coordinates?|cent(?:er|re)).{0,55}(?:listing|property|flat|home)|"
        r"(?:房源|公寓|这套|住宅|精确|自身).{0,25}(?:坐标|中心)|"
        r"(?:坐标|中心).{0,25}(?:房源|公寓|这套|住宅)", re.I)
    live_claim = re.compile(
        r"\b(?:live|real.time|up.to.date)\b|\b(?:still|currently)\s+available\b|"
        r"(?:verified|confirmed|checked).{0,35}(?:current\s+(?:price|rent|availability)|today)|"
        r"(?:current\s+(?:price|rent|availability)).{0,35}(?:verified|confirmed|checked)|"
        r"实时|最新(?:价格|租金|房源)|仍在出租|目前可租|已核实可入住", re.I)
    negative_poi = re.compile(r"\bno\b|not\s+found|none\b|could\s+not\s+find|未找到|没有找到|没有.{0,15}(?:学校|诊所|全科)|无.{0,8}(?:学校|诊所)", re.I)
    type_words = {"school": re.compile(r"\bschools?\b|学校|小学", re.I),
                  "gp": re.compile(r"\bgps?\b|\bdoctors?\b|medical\s+cent(?:er|re)|\bclinics?\b|全科|诊所|医疗中心", re.I)}
    changed: set[str] = set()
    kept = []
    safety_context = False
    for sentence in _guard_text_parts(text, sentences=True):
        if safety_topic.search(sentence):
            safety_context = True
        elif re.search(r"nearby|amenities|weather|listing details|周边|天气|房源详情", sentence, re.I):
            safety_context = False
        drop = False
        point_caveat = bool(re.search(
            r"(?:cannot|can't|unable to)\s+(?:confirm|verify|prove).{0,55}(?:coordinates?|reference|point)|"
            r"does\s+not\s+(?:verify|prove|confirm).{0,80}(?:coordinates?|point|used)|"
            r"(?:coordinates?|reference).{0,35}(?:not verified|not confirmed|unknown)|"
            r"(?:safety|police|crime).{0,35}(?:not checked|not verified)|"
            r"(?:无法|未|不能)(?:确认|核实|证明).{0,25}(?:坐标|参考点)|不能从周边设施查询推断", sentence, re.I))
        if safety_context and not point_caveat:
            refs = [(raw.get("reference_point") or (raw.get("crime_data") or {}).get("reference_point") or {})
                    for raw in safety]
            pairs = re.findall(r"(-?\d{1,3}\.\d+)\s*[,，/]\s*(-?\d{1,3}\.\d+)", sentence)
            if pairs and not all(any(
                    isinstance(ref.get("lat"), (int, float)) and isinstance(ref.get("lon"), (int, float))
                    and abs(float(lat) - ref["lat"]) < 1e-5 and abs(float(lon) - ref["lon"]) < 1e-5
                    for ref in refs) for lat, lon in pairs):
                drop = True
                changed.add("safety")
            if point_claim.search(sentence):
                # Supplied coordinates establish the queried point, not the identity of a
                # listing. Only an explicit identity attestation can support that claim.
                if not refs or not all(ref.get("coordinate_identity_verified") is True for ref in refs):
                    drop = True
                    changed.add("safety")
        live_caveat = bool(re.search(
            r"\bnot\s+(?:live|real.time)|not\s+(?:yet\s+)?(?:verified|confirmed|checked).{0,20}(?:live|current)|"
            r"(?:cannot|can't)\s+(?:confirm|verify).{0,35}(?:current|availability|live)|"
            r"current\s+(?:price|rent|availability).{0,20}(?:unknown|unverified)|"
            r"(?:未|不|无法)(?:能|曾)?(?:实时|核实|确认)|并非实时|不是实时", sentence, re.I))
        if cache_only and live_claim.search(sentence) and not live_caveat:
            drop = True
            changed.add("cache")
        no_query_claim = bool(re.search(
            r"(?:not|never)\s+(?:queried|checked|executed|completed)|"
            r"no\s+(?:completed|executed)\s+(?:lookup|query)|未(?:完成|执行|查询)|没有(?:执行|完成)", sentence, re.I))
        if not no_query_claim:
            if negative_poi.search(sentence) and re.search(
                    r"found|returned|nearby|within|附近|未找到|没有找到", sentence, re.I):
                for kind, pattern in type_words.items():
                    if not pattern.search(sentence):
                        continue
                    raw = nearby[-1] if nearby else {}
                    status = (raw.get("category_status") or {}).get(kind) or {}
                    if isinstance(status, dict):
                        status = status.get("status")
                    empty = (kind in (raw.get("empty_types") or []) or status == "empty"
                             or (kind in (raw.get("completed_types") or [])
                                 and kind in (raw.get("pois") or {}) and raw["pois"][kind] == []))
                    if not empty:
                        drop = True
                        changed.add(kind)
        if not drop:
            kept.append(sentence)
    if not changed:
        return text
    notes = []
    if "safety" in changed:
        for raw in safety:
            ref = raw.get("reference_point") or (raw.get("crime_data") or {}).get("reference_point") or {}
            query = str(ref.get("query") or raw.get("address") or "").strip()
            precision = str(ref.get("precision") or "unknown")
            notes.append(f"安全查询自己的参考点：{query}（精度：{precision}）；不能从周边设施查询推断它使用了该房源坐标。" if zh else
                         f"The safety lookup's own reference is {query} (precision: {precision}); the nearby lookup does not prove it used the property's coordinates.")
    if "cache" in changed:
        notes.append("详情来自缓存，并未实时核实价格或出租状态；须向中介确认。" if zh else
                     "The listing details are cached; current price and availability were not verified live. Confirm with the agent.")
    for kind in ("school", "gp"):
        if kind not in changed:
            continue
        raw = nearby[-1] if nearby else {}
        records = (raw.get("pois") or {}).get(kind)
        label = {"school": "学校", "gp": "全科诊所"}[kind] if zh else {"school": "Schools", "gp": "GP/medical centres"}[kind]
        if kind in (raw.get("completed_types") or []) and isinstance(records, list) and records:
            notes.append(f"{label}：已返回 {len(records)} 条记录，不能称为未找到。" if zh else
                         f"{label}: {len(records)} records were returned; this is not an empty result.")
        else:
            notes.append(f"{label}：本次未完成该类型的查询，不能据此断言附近没有。" if zh else
                         f"{label}: no completed lookup for this category supports an absence claim.")
    evidence = render_structured_evidence_fallback(state)
    if evidence:
        notes.append(evidence)
    return "\n".join(part for part in kept + notes if part.strip()).strip()


# Below this a reply is one claim, and the fixed sentence IS the honest answer.
_LEDGER_FALLBACK_MIN_CHARS = 200

# Input bound for the commute validator (B-01). The regexes above are linear, but this runs
# on every final answer, so a degenerate model output must not be able to make it the
# expensive part of a turn. A digit run this long is not a figure, a postcode, a phone number
# or a price; a run of blanks this long is not layout. Either is collapsed before validation,
# and an answer still longer than the cap (no real reply comes close: 8 192 output tokens of
# prose is ~30 000 characters) is cut at a line boundary.
_DEGENERATE_DIGIT_RUN = re.compile(r"\d{65,}")
_DEGENERATE_BLANK_RUN = re.compile(r"[ \t]{65,}")
VALIDATOR_MAX_CHARS = 32_000


def bound_validator_input(text: str) -> str:
    """``text`` with degenerate runs collapsed and its length capped. Linear, never raises."""
    value = str(text or "")
    if len(value) < 65:
        return value
    value = _DEGENERATE_DIGIT_RUN.sub(lambda m: m.group()[:12] + "…", value)
    value = _DEGENERATE_BLANK_RUN.sub(" ", value)
    if len(value) > VALIDATOR_MAX_CHARS:
        cut = value.rfind("\n", 0, VALIDATOR_MAX_CHARS)
        value = value[:cut if cut > VALIDATOR_MAX_CHARS // 2 else VALIDATOR_MAX_CHARS].rstrip() + "\n…"
    return value


# Fixed text the SYSTEM composes (the automatic-memory disclosure, this validator's own note).
# It is never a commute claim, so it is never redacted, and validating twice is idempotent:
# a second pass over a plain answer deleted "areas or commute) and forms its own inferences
# about your priorities," from the disclosure and added a note about it (R3-01, R2-19).
_PROTECTED_SYSTEM_TEXTS: list[str] = []


def register_protected_system_text(*texts: str) -> None:
    """Mark fixed system-composed text as out of the commute redactor's scope."""
    for text in texts:
        value = str(text or "").strip()
        if value and value not in _PROTECTED_SYSTEM_TEXTS:
            _PROTECTED_SYSTEM_TEXTS.append(value)


_PLACEHOLDER = "\u27e6SYSTEXT{}\u27e7"
_CONTENT_CHARS = re.compile(r"[0-9A-Za-z\u4e00-\u9fff£$€%]")


def _visible_content(text: str) -> str:
    return "".join(_CONTENT_CHARS.findall(str(text or "")))


def validate_commute_response(response: str, state: dict) -> str:
    """Remove only unsupported commute assertions; never touch fixed system text.

    System-composed passages (``register_protected_system_text``) and this validator's own
    note are masked before redaction and restored after it, in place. The note is appended
    only when this pass removed visible text, and never twice (R3-01)."""
    text = str(response or "")
    notes = (_commute_unverified_note(False), _commute_unverified_note(True))
    protected = [t for t in list(_PROTECTED_SYSTEM_TEXTS) + list(notes) if t in text]
    if not protected:
        return _validate_commute_response(text, state)
    masked = text
    for index, passage in enumerate(protected):
        masked = masked.replace(passage, _PLACEHOLDER.format(index))
    result = _validate_commute_response(masked, state,
                                        note_present=any(n in protected for n in notes))
    missing = []
    for index, passage in enumerate(protected):
        token = _PLACEHOLDER.format(index)
        if token in result:
            result = result.replace(token, passage)
        else:
            missing.append(passage)
    if missing:
        result = result.rstrip() + "".join(f"\n\n{passage}" for passage in missing)
    return result


def _validate_commute_response(response: str, state: dict, *, note_present: bool = False) -> str:
    """Remove only unsupported journey assertions; keep independent evidence and caveats.

    The return type and state contract are unchanged. Candidate qualification does not
    override an independently executed station/area route, and one such route never
    licenses another listing's duration. Structured fallback is used only when no prose
    survives, or when an earlier validation pass already replaced the answer entirely.
    The input is bounded first (``bound_validator_input``), so the cost is linear in a
    capped length whatever the model emitted.
    """
    original = _repair_abbreviation_breaks(bound_validator_input(response))
    text, previous_fragments = _clean_commute_fragments(original)
    language = str((state.get("extracted_context") or {}).get("reply_language") or "")
    zh = language.lower().startswith("zh")
    fixed = ("本轮无法核实该房源的通勤条件。" if zh
             else "The commute condition could not be verified for the listing this round.")
    if text.strip() in {"本轮无法核实该房源的通勤条件。",
                        "The commute condition could not be verified for the listing this round."}:
        return render_structured_evidence_fallback(state, language=language) or text
    if not previous_fragments and not (any(_asserts_commute(fragment) for fragment in _guard_text_parts(text))
            or (_COMMUTE_WORD.search(text) and _FIT_WORD.search(text))):
        return text
    bindings, default = _response_commute_bindings(state)
    current = str((state.get("extracted_context") or {}).get("current_message") or
                  state.get("user_query") or "")
    memory_reference = bool(re.search(r"remember|save|记住|保存", current, re.I) and (
        re.search(r"only\s+(?:save|remember)|只.{0,8}(?:保存|记住)|仅.{0,8}(?:保存|记住)|不用搜索|不要搜索", current, re.I)
        or not any(a.get("tool") in {"calculate_commute", "calculate_commute_cost", "get_transport_info"}
                   for a in state.get("tool_artifacts") or [])))
    removed: list = []
    redacted = _redact_commute_claims(
        text, zh=zh, bindings=bindings, caps=_known_commute_caps(state),
        default_binding=default, preference_context=memory_reference,
        source_texts=_listing_source_texts(state), removed=removed,
        office_days=_office_days(state), user_destinations=_user_commute_destinations(state))
    if redacted is None:
        fallback = render_structured_evidence_fallback(state, language=language)
        if not fallback and len(original.strip()) >= _LEDGER_FALLBACK_MIN_CHARS:
            # Nothing survived and no tool artifact can be rendered, but a substantial
            # answer was about real listings: their validated ledger (structured tool
            # output, never model prose) ships with the fixed limitation appended, rather
            # than one sentence replacing everything (ported from 0105aeb, P1-6).
            validation = state.get("candidate_validation") or {}
            if validation.get("statuses"):
                rendered = render_candidate_status(validation, language=language or "en")
                if rendered.strip():
                    return f"{rendered.strip()}\n\n{fixed}"
        return fallback or fixed
    redacted, new_fragments = _clean_commute_fragments(redacted)
    if previous_fragments or new_fragments:
        # Restore factual route/cost/detail evidence after removing an orphaned conclusion.
        # Weather already present in its own section need not be repeated in the repair.
        surviving = []
        for artifact in state.get("tool_artifacts") or []:
            raw = artifact.get("raw_data") or {}
            if artifact.get("tool") == "get_weather" and isinstance(raw, dict):
                if (str(raw.get("current_temp")) in redacted and raw.get("condition")
                        and str(raw["condition"]).lower() in redacted.lower()):
                    continue
            surviving.append(artifact)
        facts = render_structured_evidence_fallback({**state, "tool_artifacts": surviving}, language=language)
        missing_lines = [line for line in facts.splitlines() if line.strip() not in redacted]
        if missing_lines:
            redacted = f"{redacted}\n\n" + "\n".join(missing_lines)
    if redacted.strip() == original.strip():
        return original
    if (not any(_HAS_CONTENT.search(str(fragment)) for fragment in removed)
            or _visible_content(redacted) == _visible_content(original)):
        # Only orphaned leftovers of an earlier pass were tidied (or nothing at all): no
        # visible commute claim was withdrawn by THIS pass, so no note claims one was (R1-10,
        # R3-01).
        return redacted
    note = _commute_unverified_note(zh)
    if note_present or note in redacted or _commute_unverified_note(not zh) in redacted:
        return redacted
    return f"{redacted}\n\n{note}"


def _listing_source_texts(state: dict) -> list[str]:
    """Listing text returned by this turn's tools (features, descriptions)."""
    texts: list[str] = []
    for artifact in state.get("tool_artifacts") or []:
        raw = artifact.get("raw_data") if isinstance(artifact, dict) else None
        if not isinstance(raw, dict):
            continue
        rows = []
        for key in ("recommendations", "over_budget_alternatives", "properties"):
            rows.extend(r for r in (raw.get(key) or []) if isinstance(r, dict))
        rows.append(raw)
        for row in rows:
            for key in ("features", "key_features", "Key_Features", "description", "Description",
                        "Detailed_Amenities", "amenities", "reported_features", "property_details"):
                value = row.get(key)
                if isinstance(value, (list, tuple)):
                    texts.extend(str(v) for v in value if v)
                elif isinstance(value, dict):
                    texts.extend(f"{k} {v}" for k, v in value.items())
                elif value:
                    texts.append(str(value))
    return texts


async def validate_search_payload_with_provider(
        provider, payload: dict, *, timeout_s: float = 20.0,
        deadline_monotonic: float | None = None, max_candidates: int | None = None,
        concurrency: int | None = None,
        transport_cost_evidence: Any = None) -> tuple[dict, list[dict]]:
    """Validate a search result and obtain per-listing commute evidence when required."""
    if not isinstance(payload, dict):
        return payload, []
    existing = payload.get("commute_evidence")
    criteria = payload.get("search_criteria") or payload.get("known_criteria") or {}
    previous = payload.get("candidate_validation") or {}
    if (previous and isinstance(existing, list) and transport_cost_evidence is None
            and previous.get("constraints") == hard_constraints(criteria)):
        return payload, existing

    rows = [r for r in (payload.get("recommendations") or []) if isinstance(r, dict)]
    rows += [r for r in (payload.get("over_budget_alternatives") or []) if isinstance(r, dict)]
    evidence: list[dict] = list(existing) if isinstance(existing, list) else []
    if commute_constraint_required(criteria) and not evidence:
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
    return validate_search_payload(payload, commute_evidence=evidence,
                                   transport_cost_evidence=transport_cost_evidence), evidence
