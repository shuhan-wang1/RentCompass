"""
Tool 2: Calculate Commute Tool
计算两个地址之间的通勤时间
"""

from core.tool_system import Tool
from typing import Optional


# Airports and other hubs the geocoder places at an arbitrary point of a large site.
_TRANSPORT_HUBS = {
    "heathrow": "Heathrow Terminals 2 & 3, Hounslow TW6 1EW",
    "heathrow airport": "Heathrow Terminals 2 & 3, Hounslow TW6 1EW",
    "london heathrow": "Heathrow Terminals 2 & 3, Hounslow TW6 1EW",
    "london heathrow airport": "Heathrow Terminals 2 & 3, Hounslow TW6 1EW",
    "gatwick": "Gatwick Airport railway station, Crawley RH6 0RD",
    "gatwick airport": "Gatwick Airport railway station, Crawley RH6 0RD",
    "london gatwick": "Gatwick Airport railway station, Crawley RH6 0RD",
    "stansted": "Stansted Airport railway station, CM24 1RW",
    "stansted airport": "Stansted Airport railway station, CM24 1RW",
    "london city airport": "London City Airport DLR station, London E16 2PX",
    "city airport": "London City Airport DLR station, London E16 2PX",
    "luton airport": "Luton Airport Parkway station, Luton LU2 9NQ",
}
_LONG_FINAL_WALK_MIN = 25


def transport_hub_address(address: str) -> str:
    """The station address of an airport named on its own, else ``address`` unchanged."""
    key = " ".join(str(address or "").lower().replace(",", " ").split())
    key = key[:-len(" uk")] if key.endswith(" uk") else key
    key = key[:-len(" london")] if key.endswith(" london") and key != "london" else key
    return _TRANSPORT_HUBS.get(key.strip(), address)


def calculate_commute_impl(
    from_address: str,
    to_address: str,
    mode: str = "transit"
) -> dict:
    """
    计算两个地址之间的通勤时间

    NOTE: this is a PLAIN SYNC function on purpose. calculate_travel_details performs
    SYNCHRONOUS network I/O (TfL Journey Planner HTTP + geopy geocoding). Registering it
    as sync means Tool.execute offloads it to an executor thread (tool_system.py :279-284),
    so the asyncio event loop stays responsive and the fc-loop's per-tool asyncio.wait_for /
    batch budget can actually fire. As an async def with these blocking calls inline, four
    concurrent calls serialized to ~52s despite a 20s batch budget (the confirmed bug).
    """
    try:
        from core.maps_service import calculate_travel_details
        from core.commute_basis import (
            BASIS_MEASURED, is_measured, withdraw_uncalibrated_mode,
        )

        print(
            "   🚇 计算通勤 "
            f"from_chars={len(from_address)} to_chars={len(to_address)} "
            f"mode_supported={mode in {'transit', 'driving', 'walking', 'bicycling'}}"
        )

        # "Liverpool Street 5 days a week" is a place plus how often the user goes there; the
        # frequency failed geocoding for the rest of the conversation (K-03).
        try:
            from core.langgraph_agent import _strip_destination_time_words
            to_address = _strip_destination_time_words(to_address) or to_address
        except Exception:
            pass

        # "Heathrow" geocoded to "Airside Road Tunnel, Hillingdon" and the route ended with a
        # 48-minute walk (R4-12): an airport resolves to its station.
        # The requested addresses stay what the result reports (evidence binds to them).
        routed_from = transport_hub_address(from_address)
        routed_to = transport_hub_address(to_address)

        # 调用地图服务计算通勤时间 + 真实路线（TfL 线路明细）
        details = calculate_travel_details(routed_from, routed_to, mode)

        if not details:
            return {
                'success': False,
                'error': 'Could not calculate the commute time (an address could not be geocoded).'
            }

        # calculate_travel_details does not pass its `mode` down to describe_estimate, so a
        # cycling/driving request can come back carrying the public-transport calibration
        # (up to 2.71 km the raw cycling and transit formulas agree to within a minute, so
        # nothing downstream of maps_service can tell them apart from the number alone). This
        # is the layer that KNOWS the mode, so this is where the mismatch is corrected: any
        # mode the calibration does not cover falls back to the uncalibrated treatment.
        details = withdraw_uncalibrated_mode(details, mode)

        base = {
            'from_address': from_address,
            'to_address': to_address,
            'mode': mode,
            'route_summary': details.get('route_summary'),
            'route_legs': details.get('route_legs', []),
            'changes_count': details.get('changes_count'),
            'route_source': details.get('source'),
            'basis': details.get('basis'),
            'basis_note': details.get('basis_note'),
        }
        if routed_to != to_address:
            base['routed_to_address'] = routed_to
        if routed_from != from_address:
            base['routed_from_address'] = routed_from

        duration = details.get('duration_minutes')
        if duration is not None and is_measured(details.get('source')):
            # A real TfL itinerary. This is the ONLY branch that emits duration_minutes,
            # and therefore the only one whose figure may be stated as a journey time.
            base.update({
                'success': True,
                'duration_minutes': duration,
                'is_acceptable': duration <= 45,  # 默认45分钟为可接受
                'duration_category': (
                    'Short (< 20 min)' if duration < 20
                    else 'Medium (20-45 min)' if duration <= 45
                    else 'Long (> 45 min)'
                ),
                'basis': details.get('basis') or BASIS_MEASURED,
            })
            legs = [leg for leg in (base.get('route_legs') or []) if isinstance(leg, dict)]
            last = legs[-1] if legs else {}
            walk = last.get('duration_minutes')
            if (len(legs) > 1 and str(last.get('mode') or '').lower() == 'walking'
                    and isinstance(walk, (int, float)) and walk > _LONG_FINAL_WALK_MIN):
                # A long last walk usually means the destination was placed at the wrong point.
                base['endpoint_check'] = 'final_walk_long'
                base['caveat'] = (
                    f"This route ends with a {walk:g}-minute walk"
                    + (f" from {last.get('from')}" if last.get('from') else "")
                    + "; the destination may have been placed at the wrong point. Say so, and "
                      "ask the user to confirm the exact address before relying on this time.")
            return base

        # No journey plan. Everything derived from a straight-line guess is either
        # withheld or labelled — is_acceptable and duration_category are assertions the
        # guess cannot support, so they are None rather than computed from it.
        est = details.get('estimated_duration_minutes')
        base.update({
            'success': True,
            'duration_minutes': None,
            'estimated_duration_minutes': est,
            'estimate_low_minutes': details.get('estimate_low_minutes'),
            'estimate_high_minutes': details.get('estimate_high_minutes'),
            'straight_line_km': details.get('straight_line_km'),
            # Which model produced the estimate travels WITH the estimate. A calibrated and an
            # uncalibrated figure license different sentences and carry different bands; a
            # reader that cannot tell them apart is back to reading route_source.
            'estimate_model': details.get('estimate_model'),
            'is_acceptable': None,
            'duration_category': None,
            'caveat': details.get('caveat'),
        })
        if est is None:
            km = details.get('straight_line_km')
            distance_clause = (
                f" The one fact available is the straight-line distance, {km} km — you may "
                f"state that as a distance, but it is not a travel time."
                if isinstance(km, (int, float)) else "")
            base['recommendation'] = (
                "No commute time is available for this pair. Say that plainly — do NOT state "
                "a number of minutes, and do not infer one from the distance."
                + distance_clause)
        else:
            note = details.get('basis_note')
            base['recommendation'] = (
                f"There is no journey plan for this pair. If you mention a time at all, give it "
                f"as an estimated {details.get('estimate_low_minutes')}-"
                f"{details.get('estimate_high_minutes')} minute range and say it is estimated "
                f"from the straight-line distance, not measured. Never state "
                f"'{est} minutes' as the commute."
                + (f" Basis to disclose: {note}" if note else ""))
        return base

    except Exception as exc:
        print(f"   ❌ 通勤计算出错 exception_type={type(exc).__name__}")
        return {
            "success": False,
            "error": "Commute calculation failed",
            "retryable": True,
        }


# 创建工具实例
calculate_commute_tool = Tool(
    name="calculate_commute",
    
    description="""Calculate commute time between two UK addresses (transit / cycling / walking) via the free TfL Journey Planner; returns duration, the route (lines/changes/walking legs) and an acceptability category. Use when the user gives a commute-time requirement or asks "how long to X"; skip if none, or already computed for that listing.
READ THE BASIS FIELD: `duration_minutes` is populated ONLY when TfL returned a real journey plan (basis=tfl_journey_plan) and is the only figure you may state as a commute time. When TfL has no journey the result instead carries `estimated_duration_minutes` plus `estimate_low_minutes`/`estimate_high_minutes` (basis=straight_line_estimate) — quote that as an estimated RANGE and say it is derived from the straight-line distance, or, when it is null, say no commute time is available. `estimate_model` says which estimator produced it: "calibrated_overhead_plus_power_pace_v1" means the distance went through a model fitted to 14 measured TfL journeys (range 0.84x-1.24x), null means the raw uncorrected formula outside that model's domain (range 0.74x-1.45x). Either way state the range and the basis_note. Never present an estimate as a measured journey time.
计算两个英国地址间的通勤时间与路线。""",
    
    func=calculate_commute_impl,
    
    parameters={
        'type': 'object',
        'properties': {
            'from_address': {
                'type': 'string',
                'description': '出发地址（房源地址）'
            },
            'to_address': {
                'type': 'string',
                'description': '目的地址（工作地点、学校等）'
            },
            'mode': {
                'type': 'string',
                'enum': ['transit', 'driving', 'walking', 'bicycling'],
                'description': '通勤方式: transit (地铁/公交/火车), driving (开车), walking (步行), bicycling (骑车). 注意：不要使用 tube/tubing/underground，请用 transit',
                'default': 'transit'
            }
        },
        'required': ['from_address', 'to_address']
    },
    
    max_retries=2
)
