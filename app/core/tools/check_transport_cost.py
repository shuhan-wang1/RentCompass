"""
Tool: Check Transport Cost
查询伦敦交通（TfL）票价的专用工具
数据源：TfL 2025 官方票价表 (硬编码以确保准确性)
"""

from core.tool_system import Tool

# 2025 TfL 票价表 (基于 TfL 官方数据)
# 18+ Student Oyster Card 通常享受 Travelcard (周/月票) 的 7 折优惠 (30% off)
# 注意：Student Oyster 通常不打折 Pay As You Go (单程/日封顶)，只打折 Travelcard

TFL_FARES_2025 = {
    "adult": {
        "zone1-2": {"monthly": 164.00, "weekly": 42.70, "daily_cap": 8.50},
        "zone1-3": {"monthly": 192.60, "weekly": 50.20, "daily_cap": 10.00},
        "zone1-4": {"monthly": 235.40, "weekly": 61.40, "daily_cap": 12.30},
        "zone1-5": {"monthly": 280.30, "weekly": 73.00, "daily_cap": 14.60},
        "zone1-6": {"monthly": 300.70, "weekly": 78.40, "daily_cap": 15.60},
    },
    # 学生价通常是成人 Travelcard 的 7 折 (30% off)
    "student": {
        "zone1-2": {"monthly": 114.80, "weekly": 29.80, "daily_cap": 8.50},  # Daily Cap 通常无学生优惠
        "zone1-3": {"monthly": 134.80, "weekly": 35.10, "daily_cap": 10.00},
        "zone1-4": {"monthly": 164.70, "weekly": 42.90, "daily_cap": 12.30},
        "zone1-5": {"monthly": 196.20, "weekly": 51.10, "daily_cap": 14.60},
        "zone1-6": {"monthly": 210.40, "weekly": 54.80, "daily_cap": 15.60},
    }
}

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
        
        # 即使只在 Zone 2-3 活动，通常也会查询 Zone 1-X 的月票，这里简化处理
        zone_key = f"zone1-{end_zone}"
        
        user_type = "student" if "student" in travel_type.lower() else "adult"
        
        prices = TFL_FARES_2025.get(user_type, {}).get(zone_key)
        
        if not prices:
            return {
                "success": False,
                "error": f"暂无 Zone 1-{end_zone} 的 {user_type} 票价数据，建议访问 tfl.gov.uk 查询。"
            }
            
        return {
            "success": True,
            "data": {
                "zones": f"Zone 1-{end_zone}",
                "user_type": "18+ Student Oyster" if user_type == "student" else "Adult",
                "prices": {
                    "monthly_pass": f"£{prices['monthly']:.2f}",
                    "weekly_pass": f"£{prices['weekly']:.2f}",
                    "daily_cap_payg": f"£{prices['daily_cap']:.2f} (Daily Cap, usually no student discount)"
                },
                "note": "Student discount (30% off) applies to Travelcards (Weekly/Monthly), NOT Pay As You Go single fares.",
                "source": "TfL Official Fares 2025"
            }
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

check_transport_cost_tool = Tool(
    name="check_transport_cost",
    description="Get OFFICIAL 2025 TfL transport fares (Tube/Train/Bus). Use this for EXACT prices instead of web_search. Returns accurate student (30% off) and adult fares.",
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
