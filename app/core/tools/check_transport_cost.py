"""
Tool: Check Transport Cost
查询伦敦交通（TfL）票价的专用工具
数据源：TfL 官方 2026 票价表（统一数据模块，含生效日期和来源）
"""

from core.tool_system import Tool
import math
from typing import Optional
from uk_rent_agent.data.tfl_fares import get_zonal_fare


def _commuting_frequency(office_days_per_week: Optional[float] = None) -> dict:
    if office_days_per_week is None:
        days = None
        monthly_days = 22.0
    else:
        if isinstance(office_days_per_week, bool):
            raise ValueError('office_days_per_week must be a number between 0 and 7.')
        days = float(office_days_per_week)
        if not math.isfinite(days) or not 0 <= days <= 7:
            raise ValueError('office_days_per_week must be a number between 0 and 7.')
        monthly_days = days * 52 / 12
    return {
        'office_days_per_week': days,
        'commuting_days_per_month': monthly_days,
        'weeks_per_month': 52 / 12,
        'frequency_basis': ('legacy_22_commuting_days_per_month' if days is None
                            else 'office_days_per_week_times_52_div_12'),
    }


def _payg_commute_estimates(daily_cap: float, office_days_per_week: Optional[float] = None) -> dict:
    frequency = _commuting_frequency(office_days_per_week)
    days = frequency['office_days_per_week']
    return {
        **frequency,
        'payg_weekly_estimate': round(daily_cap * days, 2) if days is not None else None,
        'payg_monthly_estimate': round(daily_cap * frequency['commuting_days_per_month'], 2),
        'payg_estimate_kind': 'daily_cap_upper_bound',
        'return_trips_per_week': days,
        'payg_assumptions': ('One return commuting day per office day; uses the published daily '
                             'cap, not a verified single/return fare. Actual singles may cost less. '
                             'Monthly average uses 52 weeks / 12 months when office days are supplied; '
                             'holidays and additional travel are not modelled.'),
    }


async def check_transport_cost_impl(
    start_zone: int = 1,
    end_zone: int = 2,
    travel_type: str = "student",  # 'student' or 'adult'
    office_days_per_week: Optional[float] = None,
) -> dict:
    """查询具体的交通费用"""
    try:
        try:
            _commuting_frequency(office_days_per_week)
        except (TypeError, ValueError):
            return {'success': False, 'error': 'office_days_per_week must be between 0 and 7.'}
        # 数据规范化
        if start_zone > end_zone:
            start_zone, end_zone = end_zone, start_zone
        
        user_type = "student" if "student" in travel_type.lower() else "adult"
        try:
            prices = get_zonal_fare(start_zone, end_zone, user_type)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": "Fare lookup failed. Please check tfl.gov.uk/fares."
            }
            
        return {
            "success": True,
            "data": {
                **_payg_commute_estimates(prices['daily_cap'], office_days_per_week),
                "zones": (
                    f"Zone {start_zone}" if start_zone == end_zone
                    else f"Zone {start_zone}-{end_zone}"
                ),
                "user_type": "18+ Student Oyster" if user_type == "student" else "Adult",
                "prices": {
                    "monthly_pass": f"£{prices['monthly']:.2f}",
                    "weekly_pass": f"£{prices['weekly']:.2f}",
                    "daily_cap_payg": (
                        f"£{prices['daily_cap']:.2f} "
                        "(standard daily cap; no 18+ Student PAYG discount)"
                    )
                },
                "note": "Student discount (30% off) applies to Travelcards (Weekly/Monthly), NOT Pay As You Go single fares.",
                "source": prices["source"],
                "source_url": prices["source_url"],
                "effective_date": prices["effective_date"],
                "fare_edition": prices["edition"],
                'currency': 'GBP',
                'daily_cap': prices['daily_cap'],
                'monthly_travelcard': prices['monthly'],
                'weekly_travelcard': prices['weekly'],
                'coverage_verified': False,
                'coverage_note': ('These prices apply only to the explicitly selected TfL zones. '
                                  'They do not verify that an address, station or whole route is '
                                  'covered; out-of-zone stations such as Watford Junction require '
                                  'a separately verified fare/extension.'),
            }
        }
    except Exception:
        return {"success": False, "error": "Transport cost lookup failed"}

check_transport_cost_tool = Tool(
    name="check_transport_cost",
    description="Get official TfL 2026 zonal PAYG caps and weekly/monthly Travelcard prices with source/effective date. Pass office_days_per_week for hybrid attendance; PAYG weekly and average monthly totals use daily caps × office days and 52/12 weeks per month, not fixed 22 days. These are cap-based upper bounds, not verified singles or coverage of an address/route. Do not assume Watford Junction is covered by zones 1-2. Student prices require an 18+ Student Oyster photocard.",
    func=check_transport_cost_impl,
    parameters={
        "type": "object",
        "properties": {
            'start_zone': {'type': 'integer', 'enum': [1, 2, 3, 4, 5, 6],
                           'description': 'Explicit first zone; this does not infer station coverage.',
                           'default': 1},
            "end_zone": {
                "type": "integer", 
                "description": "The furthest zone (e.g., 2, 3, 4, 5, 6). Usually start_zone is 1.",
                "enum": [2, 3, 4, 5, 6]
            },
            "travel_type": {
                "type": "string", 
                "enum": ["student", "adult"], 
                "description": "Type of passenger. Use 'student' for 18+ Student Oyster Card holders."
            },
            'office_days_per_week': {
                'type': 'number', 'minimum': 0, 'maximum': 7,
                'description': 'Return commuting days each week, e.g. 2 or 3 for hybrid work. If omitted, monthly estimates preserve the legacy 22-day assumption.'
            },
        },
        "required": ["end_zone"]
    }
)
