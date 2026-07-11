"""
Tool 4: Get Weather Tool
获取地点的天气信息
"""

import requests
from core.tool_system import Tool
from typing import Optional

# Open-Meteo weather_code -> human-readable condition
_WEATHER_CODE = {
    0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain", 71: "Light Snow", 73: "Snow",
    75: "Heavy Snow", 80: "Showers", 81: "Showers", 82: "Heavy Showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


async def get_weather_impl(
    location: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None
) -> dict:
    """
    获取地点的真实天气信息（Open-Meteo —— 免费、无需 API key）
    """
    try:
        print(f"   🌤️  获取天气: {location}")

        # 无坐标时用 Open-Meteo 的免费地理编码解析地点
        if latitude is None or longitude is None:
            g = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en"}, timeout=10,
            )
            results = (g.json() or {}).get("results") if g.status_code == 200 else None
            if not results:
                return {"success": False, "error": f"无法解析地点: {location}"}
            latitude = results[0]["latitude"]
            longitude = results[0]["longitude"]

        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": latitude, "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "precipitation,weather_code,wind_speed_10m",
            "hourly": "uv_index", "timezone": "auto", "wind_speed_unit": "kmh",
        }, timeout=10)
        w.raise_for_status()
        data = w.json()
        c = data.get("current", {})
        precip = c.get("precipitation") or 0
        return {
            "location": location,
            "latitude": latitude,
            "longitude": longitude,
            "current_temp": c.get("temperature_2m"),
            "condition": _WEATHER_CODE.get(c.get("weather_code"), "Unknown"),
            "humidity": c.get("relative_humidity_2m"),
            "wind_speed": c.get("wind_speed_10m"),
            "uv_index": (data.get("hourly", {}).get("uv_index") or [None])[0],
            "feels_like": c.get("apparent_temperature"),
            "rainfall_chance": None,
            "recommendation": "天气良好，适合外出看房" if precip == 0 else "可能有雨，看房记得带伞",
        }

    except Exception as e:
        print(f"   ❌ 天气获取出错: {e}")
        return {"success": False, "error": f"天气查询失败: {e}"}


# 创建工具实例
get_weather_tool = Tool(
    name="get_weather",
    
    description="""
获取指定地点的天气信息。

**功能:**
- 获取当前天气状况
- 温度、风速、湿度等信息
- 降雨概率和紫外线指数

**何时使用:**
- 用户想了解该地区的天气
- 规划看房时间时需要天气信息
- 评估地区气候环境

**何时不用:**
- 用户没有提到天气
- 与房源选择无直接关系

**返回内容:**
- current_temp: 当前温度
- condition: 天气状况
- humidity: 湿度
- wind_speed: 风速
- rainfall_chance: 降雨概率
""",
    
    func=get_weather_impl,
    
    parameters={
        'type': 'object',
        'properties': {
            'location': {
                'type': 'string',
                'description': '地点名称（如 Bloomsbury, London）'
            },
            'latitude': {
                'type': 'number',
                'description': '纬度（可选）'
            },
            'longitude': {
                'type': 'number',
                'description': '经度（可选）'
            }
        },
        'required': ['location']
    },
    
    max_retries=2
)
