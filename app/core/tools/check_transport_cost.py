"""
Tool: Check Transport Cost
查询伦敦交通（TfL）票价的专用工具
数据源：TfL 官方 2026 票价表（统一数据模块，含生效日期和来源）
"""

from core.tool_system import Tool
from uk_rent_agent.data.tfl_fares import get_zonal_fare

async def check_transport_cost_impl(
    start_zone: int = 1,
    end_zone: int = 2,
    travel_type: str = "student"  # 'student' or 'adult'
) -> dict:
    """查询具体的交通费用"""
    try:
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
            }
        }
    except Exception:
        return {"success": False, "error": "Transport cost lookup failed"}

check_transport_cost_tool = Tool(
    name="check_transport_cost",
    description="Get official TfL 2026 zonal PAYG caps and weekly/monthly Travelcard prices, with effective date and official source URL. Student prices require an 18+ Student Oyster photocard.",
    func=check_transport_cost_impl,
    parameters={
        "type": "object",
        "properties": {
            "end_zone": {
                "type": "integer", 
                "description": "The furthest zone (e.g., 2, 3, 4, 5, 6). Usually start_zone is 1.",
                "enum": [2, 3, 4, 5, 6]
            },
            "travel_type": {
                "type": "string", 
                "enum": ["student", "adult"], 
                "description": "Type of passenger. Use 'student' for 18+ Student Oyster Card holders."
            }
        },
        "required": ["end_zone"]
    }
)
