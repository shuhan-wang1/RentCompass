"""Deterministic candidate and commute-evidence contracts.

The search tool is a candidate generator.  It is not allowed to turn an incomplete
candidate into a recommendation merely because a model-described summary sounds positive.
This module is the shared boundary used by both agent architectures and the direct search
path: each listing is classified as ``eligible``, ``excluded`` or ``unknown`` from
structured fields and explicitly linked tool evidence.
"""

from __future__ import annotations

import asyncio
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
_MINUTE_VALUE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)[\s\-–]*(?:minutes?|mins?|分钟)", re.IGNORECASE)


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
    ):
        caps.update(float(value) for value in re.findall(pattern, current, re.I) if float(value) > 0)
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
    r"\b(?:meets?|met|within|under|over|about|around|takes?|took|"
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
_SCOPE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n|[；;]")
_NAME_ABBREVIATION = r"(?:St|Dr|Mr|Mrs|Ms|Prof|Sr|Jr)"


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
            abbreviation = re.search(r"\b" + _NAME_ABBREVIATION + r"\.$", text[:position + 1], re.I)
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


def _asserts_commute(fragment: str) -> bool:
    """True when this fragment states a commute duration or verdict."""
    text = str(fragment or "")
    if _MINUTE_TOKEN.search(text):
        return True
    # A travel expense within a monetary budget is not a travel-time verdict.
    if re.search(r"fare|\bcost\b|\bprice\b|票价|费用|交通费", text, re.I):
        return False
    return bool(_COMMUTE_WORD.search(text) and _FIT_WORD.search(text))


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
            and not re.search(r"(?:limit|cap|maximum|requirement|target|上限|要求|目标)\s*(?:is|为|是|[:：])\s*\d+",
                              fragment, re.I)):
        return False
    if re.search(r"\b(?:it|this|that|the\s+(?:flat|property|listing|journey|route|commute|trip))\b.{0,35}"
                 r"\b(?:within|under|inside|below|over|exceeds?)\b|"
                 r"(?:这套|该房源|该路线|此行程).{0,20}(?:以内|之内|超过|小于)", fragment, re.I):
        return False
    return True


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


def _label_in(label: Any, text: Any) -> bool:
    """True when ``label`` occurs in ``text`` as whole words (CJK: as a substring)."""
    key, hay = _label_key(label), _label_key(text)
    if not key:
        return False
    if re.search(r"[\u4e00-\u9fff]", key):
        return key in hay
    return f" {key} " in f" {hay} "


def _route_legs(journey: dict) -> list[dict]:
    """``[{minutes, labels}]`` for each measured leg of a TfL itinerary.

    A leg's own duration is part of the same measurement as the total; an answer that lists
    "Northern line to Moorgate (6 min)" under a 23-minute journey is quoting the evidence,
    not inventing a figure."""
    legs = []
    for leg in journey.get("route_legs") or []:
        if not isinstance(leg, dict):
            continue
        value = leg.get("duration_minutes")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        labels = [str(line) for line in leg.get("lines") or [] if str(line or "").strip()]
        labels += _place_labels(leg.get("to"))
        if str(leg.get("mode") or "").lower() == "walking":
            labels.append("walk")
        summary = str(leg.get("summary") or "")
        if summary:
            labels.append(summary)
        legs.append({"minutes": float(value), "labels": list(dict.fromkeys(labels))})
    return legs


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


def _response_commute_bindings(state: dict) -> tuple[list[dict], dict | None]:
    """Combine stale candidate ledgers with this turn's explicitly bound route results.

    A successful station/area journey is useful even when the candidate list has no routes;
    it is not evidence for an apartment merely because both are in the same town.
    """
    bindings = _listing_bindings(state.get("candidate_validation") or {})
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
                                 "destinations": []})
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
                   "minutes": value}
        legs = _route_legs(journey) if value is not None else []
        if legs:
            binding["legs"] = legs
        if estimate:
            # An estimate is never a measurement, but quoting it AS an estimate — the range
            # the tool told the model to give — is honest; deleting it left "Estimated public
            # transport time:" with no figure (2026-10-19 C-08).
            binding["estimate"] = estimate
        # The same origin from calculate_commute and a journey/fare lookup is one route,
        # not ambiguous evidence. Different destinations or different measurements remain
        # separate and cannot license an unqualified claim.
        equivalent = next((prior for prior in routes
                           if prior["minutes"] == value
                           and set(map(_norm, prior["labels"])) & set(map(_norm, binding["labels"]))
                           and set(map(_norm, prior["destinations"])) &
                           set(map(_norm, binding["destinations"]))), None)
        if equivalent is not None:
            equivalent["labels"] = list(dict.fromkeys(equivalent["labels"] + binding["labels"]))
            equivalent["destinations"] = list(dict.fromkeys(
                equivalent["destinations"] + binding["destinations"]))
        elif not any(binding == prior for prior in routes):
            routes.append(binding)

    for route in routes:
        for binding in bindings:
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
        if destinations and explicit_destination:
            destination_text = explicit_destination.group(1)
            if not any(_label_in(label, destination_text) for label in _route_places(binding)):
                continue
        # Origin specificity comes first. A long destination name must never let a town
        # route outrank an unmeasured, more specific property address in that town.
        score = (matched, max((len(label) for label in destinations
                               if _label_in(label, line)), default=0))
        if score > best_len:
            best, best_len, ambiguous = binding, score, False
        elif score == best_len and binding is not best and binding != best:
            ambiguous = True
    independent = [label for label in named if not any(
        label != other and label in other for other in named)]
    if len(set(independent)) > 1:
        return {"labels": [], "minutes": None, "destinations": [], "members": named_bindings}
    if ambiguous:
        return {"labels": [], "minutes": None, "destinations": []}
    if best is not None:
        return best
    # A single returned route can support "It takes …", never a newly named place. It also
    # must not silently switch the destination even in an otherwise implicit sentence.
    # "Journey time: 23 minutes", "The fastest route takes 23 minutes" and a line naming one
    # of the route's own legs ("Northern line to Moorgate (6 min)") are that same route.
    if default is not None and not default.get("labels") and not default.get("destinations"):
        # The legacy NAMELESS result: one measurement and no place to compare a line
        # against. The claim check still requires the quoted figure to equal it exactly.
        return default
    if default is not None and (_IMPLICIT_ROUTE_SUBJECT.match(line) or any(
            _label_in(label, line) for leg in default.get("legs") or []
            for label in leg["labels"])):
        destinations = default.get("destinations") or []
        target = _TARGET.search(_without_abbreviation_dots(line))
        if not target or not destinations or any(
                _label_in(label, target.group(1)) for label in _route_places(default)):
            return default
    return None


# The place a clause travels TO. An infinitive is not a destination: "leave at 08:35 to
# arrive by 09:00" named no place, but read as one it failed every route (B-02).
_NOT_A_PLACE_AFTER_TO = (r"(?!\s+(?:arrive|get|reach|be|make|catch|leave|travel|go|walk|"
                         r"change|work|study|commute|allow|avoid|spend|save|pay|check)\b)")
def _without_abbreviation_dots(text: str) -> str:
    """"St. Pancras" -> "St Pancras": a target clause ends at a full stop, not a title."""
    return re.sub(r"\b(" + _NAME_ABBREVIATION + r")\.", r"\1", str(text or ""))


_TARGET = re.compile(r"(?:\bto\b" + _NOT_A_PLACE_AFTER_TO + r"|至|到)\s*([^,.;，。；\n]+)", re.I)
_TARGET_WITH_FROM = re.compile(
    r"(?:\bto\b" + _NOT_A_PLACE_AFTER_TO + r"|\bfrom\b(?=\s+\S)|至|到)\s*([^,.;，。；\n]+)", re.I)


_IMPLICIT_ROUTE_SUBJECT = re.compile(
    r"\s*[*#>•\-\s]*(?:\d+[.)]\s*)?(?:it\b|this\s+(?:trip|journey|commute|route)\b|"
    r"(?:the\s+)?(?:(?:fastest|quickest|best|recommended|returned|total|whole|overall|direct|"
    r"estimated|typical|door[-\s]to[-\s]door|public\s+transport)\s+)*"
    r"(?:trip|journey|commute|route|travel|public\s+transport)(?:\s+time)?\b|"
    r"该(?:行程|路线|通勤)|通勤|行程|总行程|预计)", re.I)


def _route_places(binding: dict) -> list[str]:
    """Every place a route may be said to travel TO: its destination and each leg's end.

    Not the origin: "from King's Cross it is 23 minutes to Stratford" names the origin and
    a DIFFERENT destination, and must not pass because the origin is a known place."""
    places = list(binding.get("destinations") or [])
    for leg in binding.get("legs") or []:
        places += leg["labels"]
    return places


_WHOLE_TRIP_WORD = re.compile(
    r"\b(?:journey|commute|trip|total|overall|door|whole|altogether|in\s+all)\b|全程|总|通勤",
    re.I)
_ESTIMATE_MARKER = re.compile(
    r"\bestimat\w*|\bapprox\w*|\broughly\b|\brange\b|\bnot\s+(?:a\s+)?measured\b|"
    r"估计|估算|预计|大约|约", re.I)


def _estimate_is_quoted_as_estimate(fragment: str, binding: dict, caps: set,
                                    context: str = "") -> bool:
    """An unmeasured route's ESTIMATE may be quoted only as an estimate, never as a verdict.

    The tool says exactly this ("give it as an estimated 16-24 minute range ... never state
    '19 minutes' as the commute"), and deleting the range left a heading with no figure and
    "the lower end of that range" pointing at nothing (2026-10-19 C-08)."""
    estimate = binding.get("estimate") or []
    values = _minute_claim_values(fragment)
    if not estimate or not values:
        return False
    if not _ESTIMATE_MARKER.search(f"{fragment} {context}"):
        return False
    if _PASS_VERDICT.search(fragment) or _FAIL_VERDICT.search(fragment):
        return False      # an estimate cannot establish that a limit is met or missed
    allowed = set(estimate) | caps
    return all(any(abs(value - permitted) < 1e-9 for permitted in allowed) for value in values)


def _claim_is_evidenced(fragment: str, binding: dict | None, caps: set,
                        context: str = "") -> bool:
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
        # A shared verdict can be checked for each named entity. Different measured times
        # in one clause cannot be pooled and freely reassigned between those entities.
        return bool(_minute_claim_values(fragment) and
                    all(value in caps for value in _minute_claim_values(fragment)) and
                    all(_claim_is_evidenced(fragment, member, caps)
                        for member in binding["members"]))
    if binding is None:
        return False
    if binding.get("minutes") is None:
        return _estimate_is_quoted_as_estimate(fragment, binding, caps, context)
    destinations = binding.get("destinations") or []
    target = _TARGET_WITH_FROM.search(_without_abbreviation_dots(fragment))
    if destinations and target and not any(_label_in(label, target.group(1))
                                           for label in _route_places(binding)):
        return False
    if re.search(r"wheelchair|step.free|accessible|轮椅|无障碍|无台阶", fragment, re.I):
        # A fastest itinerary is not an accessibility audit or a personalised travel time.
        return False
    allowed = {binding["minutes"]} | caps
    # A leg's own duration is part of the same measurement, but only for a leg the line
    # actually names — "(6 min)" beside "Northern line to Moorgate".
    # A fragment about the WHOLE trip ("the journey takes 6 minutes") may not borrow a leg.
    scope = f"{fragment} {context}"
    if not _WHOLE_TRIP_WORD.search(fragment):
        allowed |= {leg["minutes"] for leg in binding.get("legs") or []
                    if any(_label_in(label, scope) for label in leg["labels"])}
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
                           default_binding: dict | None = None,
                           preference_context: bool = False) -> str | None:
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
    lines: list[str] = []
    for line in str(text or "").split("\n"):
        bullet = _BULLET_PREFIX.match(line)
        prefix = bullet.group(1) if bullet else ""
        body = line[len(prefix):]
        binding = _line_binding(line, bindings, default_binding)
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

        def _evidenced(fragment: str, _binding=None) -> bool:
            """True when this fragment's commute claim is backed for THIS line's listing."""
            if _preference_commute_reference(fragment, caps, context=preference_line):
                return True
            return _claim_is_evidenced(fragment, _binding if _binding is not None else binding,
                                       caps, context=line)

        # A parenthetical commute aside ("(17 min to Canary Wharf)") is an attachment to a
        # clause, not a clause of its own; remove it before clause splitting.
        body = _PARENTHETICAL.sub(
            lambda m: "" if (_asserts_commute(m.group(0)) and not _evidenced(m.group(0)))
            else m.group(0), body)

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
            body = coordinated.sub(
                lambda m: "; " if first_minute is not None and first_minute.end() <= m.start()
                else m.group(0), body)

        parts = _guard_text_parts(body)
        kept: list[str] = []
        dropped_in_sentence = False
        inherited = binding
        commute_context = False
        for index in range(0, len(parts), 2):
            fragment = parts[index]
            separator = parts[index + 1] if index + 1 < len(parts) else ""
            local = _line_binding(fragment, bindings)
            if local is not None:
                # A comma-delimited address component ("North Approach, Watford, WD25")
                # must not switch the listing's binding to a route measured from the town.
                nested_component = bool(inherited and local.get("labels") and any(
                    short.lower() in long.lower() and short.lower() != long.lower()
                    for short in local["labels"] for long in inherited.get("labels") or []))
                if not nested_component or _asserts_commute(fragment) or "→" in fragment:
                    inherited = local
            if _COMMUTE_WORD.search(fragment):
                commute_context = True
            monetary_or_independent = re.search(
                r"\brent\b|\bbudget\b|\bfare\b|\bcost\b|\bweather\b|\btemperature\b|"
                r"\bfurniture\b|\bbills?\b|\bdeposit\b|月租|租金|预算|票价|费用|天气|气温|家具|账单|押金",
                fragment, re.I)
            asserts = (_asserts_commute(fragment) or
                       (commute_context and _FIT_WORD.search(fragment)
                        and not monetary_or_independent))
            drop = fragment.strip() and asserts and not _evidenced(fragment, inherited)
            # A dependent clause after a redacted one restates its verdict without repeating
            # its subject ("…, which is well within your limit"), so it goes with it.
            if (not drop and dropped_in_sentence and fragment.strip()
                    and ((_CONTINUATION.match(fragment) and _FIT_WORD.search(fragment)
                          and not _evidenced(fragment, inherited))
                         or _dependent_commute_aside(fragment))):
                drop = True
            if drop:
                dropped_in_sentence = True
                continue  # its trailing separator goes with it
            if _SENTENCE_END.search(separator):
                dropped_in_sentence = False
                commute_context = False
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
        # Each assertion was checked with its own entity above. Rechecking the reconstructed
        # WHOLE line joins unrelated clauses ("commute unknown; rent within budget") and
        # used to discard the entire answer, including successful weather/detail/fare facts.
        lines.append(prefix + rebuilt)

    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if not _HAS_CONTENT.search(result):
        return None
    return result


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


def validate_commute_response(response: str, state: dict) -> str:
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
    redacted = _redact_commute_claims(
        text, zh=zh, bindings=bindings, caps=_known_commute_caps(state),
        default_binding=default, preference_context=memory_reference)
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
    note = _commute_unverified_note(zh)
    return redacted if note in redacted else f"{redacted}\n\n{note}"


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
