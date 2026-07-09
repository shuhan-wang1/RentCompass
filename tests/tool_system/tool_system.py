'''
Tool system 是整个agent的底层执行能力层
它定义了Agent能做什么，以及如何做。在Tool system里的怎么做，指的不是决策逻辑，而是某个工具的具体实现

Workflow 是定义Agent如何使用这些工具的一层。它是Tool之间调用关系与执行顺序
把许多只能完成单一任务的Tool，按照一定顺序和逻辑组合起来，从而完成一个更复杂、更高层次的任务

ReAct/CoT/AutoGPT 这类机制，就是Agent的顶层控制逻辑，它决定了：
1. 什么时候调用哪个Workflow
2. 直接调用哪些tools
以及如何根据结果进行下一步reasoning
'''

from typing import Callable, Dict, Any, Optional
from dataclasses import dataclass
import asyncio
import json

@dataclass
class ToolResult:
    """标准化的工具返回结果"""
    success: bool
    data: Any
    error: Optional[str] = None
    metadata: Dict[str, Any] = None  # 执行时间、API 调用次数等
    
    def to_dict(self):
        return {
            'success': self.success,
            'data': self.data,
            'error': self.error,
            'metadata': self.metadata or {}
        }

class Tool:
    """基础工具类"""
    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        func: Callable,
        retry_on_failure: bool = True,
        max_retries: int = 3
    ):
        self.name = name
        self.description = description  # 给 AI 看的描述
        self.parameters = parameters    # JSON Schema 格式
        self.func = func
        self.retry_on_failure = retry_on_failure
        self.max_retries = max_retries
    
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具，带重试和错误处理"""
        for attempt in range(self.max_retries):
            try:
                print(f"🔧 [{self.name}] Executing (attempt {attempt + 1}/{self.max_retries})...")
                
                # 执行工具函数
                if asyncio.iscoroutinefunction(self.func):
                    result = await self.func(**kwargs)
                else:
                    result = self.func(**kwargs)
                
                print(f"✅ [{self.name}] Success")
                return ToolResult(
                    success=True,
                    data=result,
                    metadata={'attempts': attempt + 1}
                )
            
            except Exception as e:
                print(f"❌ [{self.name}] Error: {str(e)}")
                
                if attempt == self.max_retries - 1 or not self.retry_on_failure:
                    return ToolResult(
                        success=False,
                        data=None,
                        error=str(e),
                        metadata={'attempts': attempt + 1}
                    )
                
                # 指数退避
                await asyncio.sleep(2 ** attempt)
    
    def to_openai_format(self) -> Dict:
        """转换为 OpenAI Function Calling 格式（也适用于 Ollama）"""
        return {
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters
        }


class ToolRegistry: # 我有很多个工具Tool，谁来统一管理、协调、让LLM知道它们都存在
    """工具注册中心, 负责存放、检索、组织多个Tool实例"""
    def __init__(self):
        self.tools: Dict[str, Tool] = {}
    
    def register(self, tool: Tool):
        """注册工具"""
        self.tools[tool.name] = tool
        print(f"📝 Registered tool: {tool.name}")
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self.tools.get(name)
    
    def list_tools(self) -> list:
        """列出所有工具（给 AI 看的）"""
        return [tool.to_openai_format() for tool in self.tools.values()]
    
    async def execute_tool(self, name: str, **kwargs) -> ToolResult:
        """执行工具"""
        tool = self.get(name)
        if not tool:
            return ToolResult(
                success=False,
                data=None,
                error=f"Tool '{name}' not found"
            )
        return await tool.execute(**kwargs)