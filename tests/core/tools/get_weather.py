"""
Tool 4: Get Weather Tool
获取地点的天气信息
"""

from core.tool_system import Tool
from typing import Optional


async def get_weather_impl(
    location: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None
) -> dict:
    """
    获取地点的天气信息
    """
    try:
        from core.location_service import resolve_location
        
        print(f"   🌤️  获取天气:")
        print(f"      地点: {location}")
        
        # 如果没有坐标，尝试解析地址
        if latitude is None or longitude is None:
            loc = resolve_location(location)
            if loc:
                latitude = loc.get('latitude')
                longitude = loc.get('longitude')
        
        if latitude is None or longitude is None:
            return {
                'success': False,
                'error': '无法获取位置坐标'
            }
        
        # 这里可以调用真实的 API（如 Open-Meteo 或 WeatherAPI）
        # 暂时返回示例数据
        weather_data = {
            'location': location,
            'latitude': latitude,
            'longitude': longitude,
            'current_temp': 15,  # 摄氏度
            'condition': 'Partly Cloudy',
            'humidity': 65,
            'wind_speed': 12,  # km/h
            'rainfall_chance': 30,  # %
            'uv_index': 3,
            'feels_like': 13,  # 摄氏度
            'recommendation': '天气良好，适合外出看房'
        }
        
        return weather_data
    
    except Exception as e:
        print(f"   ❌ 天气获取出错: {e}")
        raise


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
