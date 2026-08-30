"""
Tool 2: Calculate Commute Tool
计算两个地址之间的通勤时间
"""

from core.tool_system import Tool
from typing import Optional


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

        # 调用地图服务计算通勤时间 + 真实路线（TfL 线路明细）
        details = calculate_travel_details(from_address, to_address, mode)

        if not details:
            return {
                'success': False,
                'error': '无法计算通勤时间（地址解析失败）'
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
            'route_source': details.get('source'),
            'basis': details.get('basis'),
            'basis_note': details.get('basis_note'),
        }

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
