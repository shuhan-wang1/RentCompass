import asyncio
import time
from typing import Any, Dict, Optional
from dataclasses import dataclass

@dataclass
class ToolResult:
    '''统一的工具返回格式'''
    success: bool
    data: Any
    error: Optional[str] = None
    execution_time_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "execution_time_ms": self.execution_time_ms
        }
    

class Tool:
    '''自定义tool system, 不依赖LangChain'''
    
    def __init__(self, name: str, description: str, func, parameters: Dict[str, Any]):
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters

    async def execute(self, **kwargs) -> ToolResult:
        """执行工具，自动测量耗时与错误捕获"""
        start_time = time.time()
        try:
            # 检查参数
            for param in self.parameters.get("required", []):
                if param not in kwargs:
                    raise ValueError(f"缺少必需参数：{param}")

            # 支持异步函数和同步函数
            if asyncio.iscoroutinefunction(self.func):
                result = await self.func(**kwargs)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: self.func(**kwargs))

            elapsed = (time.time() - start_time) * 1000
            return ToolResult(success=True, data=result, execution_time_ms=elapsed)

        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            return ToolResult(success=False, data=None, error=str(e), execution_time_ms=elapsed)
        

# 定义一个具体工具
def get_weather(city: str) -> Dict[str, Any]:
    fake_data = {
        "北京": {"temperature": 22, "condition": "晴"},
        "上海": {"temperature": 25, "condition": "多云"},
        "深圳": {"temperature": 29, "condition": "小雨"}
    }
    return fake_data.get(city, {"temperature": None, "condition": "未知城市"})

# 创建工具实例
weather_tool = Tool(
    name="get_weather",
    description="根据城市名查询天气（离线模拟）",
    func=get_weather,
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称，例如 '北京'"}
        },
        "required": ["city"]
    }
)

async def main():
    result = await weather_tool.execute(city="上海")
    print(result.to_dict())

# 在 Python 运行环境中执行：
if __name__ == "__main__":
    asyncio.run(main())