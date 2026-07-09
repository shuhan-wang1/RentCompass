# tools/base.py
# 核心：Tool、ToolResult、ToolRegistry


from typing import Callable, Dict, Any, Optional, List
from dataclasses import dataclass
import asyncio
# 同步: 一件事做完才能做下一件事。 异步: 可以同时做多件事, 不必等待前一件事结束
import time
import traceback
import json


@dataclass
class ToolResult: # 这个是执行Tool对应的python函数，执行结束后返回的标准格式输出
    """
    所有工具都返回这个统一格式，把结果、错误、执行时间都封装起来
    """
    success: bool
    data: Any
    error: Optional[str] = None # 变量类型是可选的字符串
    execution_time_ms: Optional[float] = None
    tool_name: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """转换为字典格式"""
        return {
            'success': self.success,
            'data': self.data,
            'error': self.error,
            'execution_time_ms': self.execution_time_ms,
            'tool_name': self.tool_name # 我自己定义的tool的名字，不是python function的名字
        }


class Tool:
    """
    使用 OpenAI Function Calling 格式（但不调用 OpenAI）
    这个格式也被 Ollama、Claude、Llama 等模型支持
    """
    
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable, # 可调用函数
        parameters: Dict[str, Any],
        return_direct: bool = False,
        max_retries: int = 2,
        retry_on_error: bool = True
    ):
        """
        参数说明：
            name: 工具名（snake_case，如 'search_properties'）
            description: 详细描述，告诉 AI 何时使用这个工具
            func: 实际执行的函数（可以是同步或异步函数）
            parameters: 参数定义（OpenAI 格式的 JSON）
            return_direct: 是否直接返回结果（不经过 AI 处理）
            max_retries: 失败时最大重试次数
            retry_on_error: 是否在出错时重试
        """
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters
        self.return_direct = return_direct
        self.max_retries = max_retries
        self.retry_on_error = retry_on_error
        
        # 验证参数格式
        self._validate_parameters()
    
    def _validate_parameters(self):
        """验证参数是否符合 OpenAI Function Calling 的标准 JSON Schema 格式"""
        if not isinstance(self.parameters, dict):
            raise ValueError(f"[{self.name}] parameters 必须是字典")
        
        if 'type' not in self.parameters:
            raise ValueError(f"[{self.name}] parameters 必须包含 'type' 字段")
        
        if self.parameters['type'] != 'object':
            raise ValueError(f"[{self.name}] parameters['type'] 必须是 'object'")
        
        if 'properties' not in self.parameters:
            raise ValueError(f"[{self.name}] parameters 必须包含 'properties' 字段")

    
    async def execute(self, **kwargs) -> ToolResult: # 执行python函数，kwarg是从LLM JSON中获得的
        """
        执行工具（带重试和错误处理）
        """
        start_time = time.time()
        
        for attempt in range(self.max_retries):
            try:
                print(f"[{self.name}] 执行中... (尝试 {attempt + 1}/{self.max_retries})")
                
                # 验证输入参数
                self._validate_input(kwargs)
                
                # 执行函数（支持同步和异步）
                if asyncio.iscoroutinefunction(self.func): # 检查self.func是不是异步函数
                    result = await self.func(**kwargs)
                else:
                    # 同步函数在 executor 中运行（避免阻塞）
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(None, lambda: self.func(**kwargs))
                
                execution_time = (time.time() - start_time) * 1000
                
                print(f"✅ [{self.name}] 成功 ({execution_time:.0f}ms)")
                
                return ToolResult(
                    success=True,
                    data=result,
                    execution_time_ms=execution_time,
                    tool_name=self.name
                )
            
            except Exception as e:
                execution_time = (time.time() - start_time) * 1000
                error_msg = f"{type(e).__name__}: {str(e)}"
                
                print(f"❌ [{self.name}] 错误: {error_msg}")
                
                # 是否重试
                if attempt < self.max_retries - 1 and self.retry_on_error: # retry_on_error=True
                    wait_time = 2 ** attempt  # 指数退避：2, 4, 8...
                    print(f"⏳ [{self.name}] {wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # 最后一次尝试失败
                    print(f"💥 [{self.name}] 所有尝试都失败")
                    if attempt == self.max_retries - 1:
                        traceback.print_exc()
                    
                    return ToolResult(
                        success=False,
                        data=None,
                        error=error_msg,
                        execution_time_ms=execution_time,
                        tool_name=self.name
                    )
    
    def _validate_input(self, kwargs: Dict):
        """验证是否满足JSON中定义的required的参数"""
        required = self.parameters.get('required', [])
        
        for param in required:
            if param not in kwargs:
                raise ValueError(f"缺少必需参数: {param}")
    
    def to_llm_format(self) -> str:
        """
        把这个Tool转换为给 LLM 看的文字说明格式
        这个格式会放在 prompt 中，不调用任何 API，而是一段说明文本，告诉LLM：
        我是谁，我能做什么，我需要哪些参数
        """
        # 构建参数描述
        params_lines = []
        for param_name, param_info in self.parameters['properties'].items():
            is_required = param_name in self.parameters.get('required', [])
            required_mark = " **(必需)**" if is_required else " (可选)"
            
            param_type = param_info.get('type', 'any')
            param_desc = param_info.get('description', '无描述')
            
            # 如果有枚举值，显示出来
            if 'enum' in param_info:
                param_type += f" (可选值: {', '.join(param_info['enum'])})"
            
            # 如果有默认值，显示出来
            if 'default' in param_info:
                param_type += f" (默认: {param_info['default']})"
            
            params_lines.append(f"  • {param_name}{required_mark}: {param_type} - {param_desc}")
        
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔧 Tool: {self.name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 描述:
{self.description}

⚙️  参数:
{chr(10).join(params_lines)}

💡 使用示例:
{self._generate_example()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    def _generate_example(self) -> str:
        """生成使用示例"""
        example_params = {}
        for param_name, param_info in self.parameters['properties'].items():
            param_type = param_info.get('type', 'string')
            
            if param_type == 'string':
                example_params[param_name] = '"example_value"'
            elif param_type == 'integer':
                example_params[param_name] = '1500'
            elif param_type == 'number':
                example_params[param_name] = '5.0'
            elif param_type == 'boolean':
                example_params[param_name] = 'true'
            else:
                example_params[param_name] = '...'
        
        params_str = ', '.join([f'"{k}": {v}' for k, v in example_params.items()])
        return f'{{"tool": "{self.name}", "params": {{{params_str}}}}}'
    
    def __repr__(self) -> str:
        return f"Tool(name='{self.name}')"


class ToolRegistry: # 我有很多个工具Tool，谁来统一管理、协调、让LLM知道它们都存在
    """工具注册中心, 负责存放、检索、组织多个Tool实例"""
    
    def __init__(self):
        self.tools: Dict[str, Tool] = {}
        self._stats: Dict[str, Dict] = {}
    
    def register(self, tool: Tool):
        """注册一个工具"""
        if tool.name in self.tools:
            print(f"⚠️  工具 '{tool.name}' 已存在，将被覆盖")
        
        self.tools[tool.name] = tool
        self._stats[tool.name] = {
            'total_calls': 0, # 工具被调用的总次数
            'successful_calls': 0,
            'failed_calls': 0,
            'total_time_ms': 0 # 累计执行时间
        }
        
        print(f"✅ 注册工具: {tool.name}")
    
    def register_multiple(self, tools: List[Tool]):
        """批量注册工具"""
        for tool in tools:
            self.register(tool)
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self.tools.get(name)
    
    def list_tool_names(self) -> List[str]:
        """列出所有工具名称"""
        return list(self.tools.keys())
    
    def list_tools_for_llm(self) -> str:
        """
        生成给 LLM 看的工具列表（文本格式）
        这个会放在 prompt 中，不调用任何 API
        """
        if not self.tools:
            return "暂无可用工具"
        
        tools_text = "\n".join([tool.to_llm_format() for tool in self.tools.values()])
        
        return f"""
╔═══════════════════════════════════════════════════════════╗
║                    可用工具列表                             ║
║              （共 {len(self.tools)} 个工具）                    ║
╚═══════════════════════════════════════════════════════════╝

{tools_text}

📌 使用说明:
1. 根据用户需求选择合适的工具
2. 返回 JSON 格式: {{"tool": "工具名", "params": {{参数}}}}
3. 一次只能调用一个工具
"""
    
    async def execute_tool(self, name: str, **kwargs) -> ToolResult:
        """执行工具"""
        tool = self.get(name)
        
        if not tool:
            return ToolResult(
                success=False,
                data=None,
                error=f"工具 '{name}' 不存在",
                tool_name=name
            )
        
        # 执行工具
        result = await tool.execute(**kwargs)
        
        # 更新统计
        stats = self._stats[name]
        stats['total_calls'] += 1
        if result.success:
            stats['successful_calls'] += 1
        else:
            stats['failed_calls'] += 1
        if result.execution_time_ms:
            stats['total_time_ms'] += result.execution_time_ms
        
        return result
    
    def get_stats(self) -> Dict:
        """获取执行统计"""
        return self._stats
    
    def print_stats(self):
        """打印统计信息"""
        print("\n" + "="*60)
        print("📊 工具执行统计")
        print("="*60)
        
        for name, stats in self._stats.items():
            if stats['total_calls'] == 0:
                continue
            
            success_rate = (stats['successful_calls'] / stats['total_calls']) * 100
            avg_time = stats['total_time_ms'] / stats['total_calls']
            
            print(f"\n🔧 {name}")
            print(f"   总调用: {stats['total_calls']}")
            print(f"   成功: {stats['successful_calls']} ({success_rate:.1f}%)")
            print(f"   失败: {stats['failed_calls']}")
            print(f"   平均耗时: {avg_time:.0f}ms")
        
        print("="*60 + "\n")