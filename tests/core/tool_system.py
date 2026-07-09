"""
Tool System - Agent框架的核心工具系统
核心概念：
1. Tool - 工具定义（名称、描述、参数、执行函数）
2. ToolResult - 标准化工具返回结果
3. ToolRegistry - 工具注册中心（管理、查询、执行所有工具）
4. FunctionCalling - 让LLM从工具列表中选择合适的工具
5. Agent - ReAct循环（推理→行动→观察→反馈）
"""

import asyncio
import time
import json
import re
from typing import Callable, Dict, Any, Optional, List
from dataclasses import dataclass
import traceback


@dataclass
class ToolResult:
    """
    标准化的工具执行结果 - 所有工具都返回这个格式
    """
    success: bool
    data: Any = None
    error: Optional[str] = None
    execution_time_ms: Optional[float] = None
    tool_name: Optional[str] = None
    
    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            'success': self.success,
            'data': self.data,
            'error': self.error,
            'execution_time_ms': self.execution_time_ms,
            'tool_name': self.tool_name
        }


class Tool:
    """
    工具基类 - 使用 OpenAI Function Calling 格式
    这个格式被 OpenAI、Ollama、Claude、Llama 等都支持
    """
    
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
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
            parameters: 参数定义（OpenAI 格式的 JSON Schema）
            return_direct: 是否直接返回结果
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

    
    async def execute(self, **kwargs) -> ToolResult:
        """
        执行工具（带重试和错误处理）
        """
        start_time = time.time()
        
        for attempt in range(self.max_retries):
            try:
                print(f"  [EXECUTE] [{self.name}] 执行中... (尝试 {attempt + 1}/{self.max_retries})")
                
                # 验证输入参数
                self._validate_input(kwargs)
                
                # 执行函数（支持同步和异步）
                if asyncio.iscoroutinefunction(self.func):
                    result = await self.func(**kwargs)
                else:
                    # 同步函数在 executor 中运行（避免阻塞）
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(None, lambda: self.func(**kwargs))
                
                execution_time = (time.time() - start_time) * 1000
                
                print(f"  ✅ [{self.name}] 成功 ({execution_time:.0f}ms)")
                
                return ToolResult(
                    success=True,
                    data=result,
                    execution_time_ms=execution_time,
                    tool_name=self.name
                )
            
            except Exception as e:
                execution_time = (time.time() - start_time) * 1000
                error_msg = f"{type(e).__name__}: {str(e)}"
                
                print(f"  ❌ [{self.name}] 错误: {error_msg}")
                
                # 是否重试
                if attempt < self.max_retries - 1 and self.retry_on_error:
                    wait_time = 2 ** attempt  # 指数退避：2, 4, 8...
                    print(f"  ⏳ [{self.name}] {wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # 最后一次尝试失败
                    print(f"  💥 [{self.name}] 所有尝试都失败")
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
        """验证是否满足 required 的参数"""
        required = self.parameters.get('required', [])
        
        for param in required:
            if param not in kwargs:
                raise ValueError(f"缺少必需参数: {param}")
    
    def to_llm_format(self) -> str:
        """
        把这个 Tool 转换为给 LLM 看的文字说明格式
        这个格式会放在 prompt 中，告诉 LLM：
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
{chr(10).join(params_lines) if params_lines else "  (无参数)"}

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
    
    def to_openai_format(self) -> Dict:
        """转换为 OpenAI/Ollama Function Calling 格式"""
        return {
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters
        }
    
    def __repr__(self) -> str:
        return f"Tool(name='{self.name}')"


class ToolRegistry:
    """
    工具注册中心 - 负责存放、检索、组织多个 Tool 实例
    """
    
    def __init__(self):
        self.tools: Dict[str, Tool] = {}
        self._stats: Dict[str, Dict] = {}
    
    def register(self, tool: Tool):
        """注册一个工具"""
        if tool.name in self.tools:
            print(f"⚠️  工具 '{tool.name}' 已存在，将被覆盖")
        
        self.tools[tool.name] = tool
        self._stats[tool.name] = {
            'total_calls': 0,
            'successful_calls': 0,
            'failed_calls': 0,
            'total_time_ms': 0
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


# ============================================================================
# Function Calling - 让 LLM 从工具列表中选择工具
# ============================================================================

def extract_json_from_text(text: str) -> Optional[Dict]:
    """
    从 LLM 回复中提取 JSON
    """
    # 策略 1: 直接解析
    try:
        return json.loads(text.strip())
    except:
        pass
    
    # 策略 2: 从 markdown 代码块提取
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    
    # 策略 3: 找第一个完整的 JSON 对象
    brace_count = 0
    start_idx = -1
    
    for i, char in enumerate(text):
        if char == '{':
            if brace_count == 0:
                start_idx = i
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0 and start_idx != -1:
                try:
                    return json.loads(text[start_idx:i+1])
                except:
                    start_idx = -1
                    continue
    
    return None


class FunctionCalling:
    """
    Function Calling - 让 AI 从工具列表中选择合适的工具
    """
    
    def __init__(self, tool_registry: ToolRegistry):
        self.tool_registry = tool_registry
    
    def ask_ai_to_choose_tool(
        self, 
        user_query: str,
        llm_func: Callable,
        context: Optional[Dict] = None
    ) -> Dict:
        """
        让 AI 选择要调用的工具
        
        参数：
            user_query: 用户的问题
            llm_func: LLM 函数（接收 prompt，返回文本）
            context: 上下文信息（包括之前的观察）
        
        返回格式:
        {
            'action': 'use_tool' | 'finish' | 'clarify' | 'error',
            'tool_name': 'search_properties',
            'tool_params': {'location': 'UCL', 'max_budget': 1500},
            'reasoning': 'AI 的思考过程'
        }
        """
        print("\n" + "="*60)
        print("🤖 Function Calling: 询问 AI 选择工具")
        print("="*60)
        
        # 构造 Prompt
        prompt = self._build_function_calling_prompt(user_query, context)
        
        # 调用 LLM
        print("📤 发送给 LLM...")
        ai_response = llm_func(prompt)
        
        print(f"📥 LLM 回复 ({len(ai_response)} 字符)")
        print(f"   {ai_response[:200]}...")
        
        # 解析 LLM 的回复
        decision = extract_json_from_text(ai_response)
        
        if not decision:
            print("❌ 无法解析 LLM 回复")
            return {
                'action': 'error',
                'error': 'Failed to parse AI response',
                'raw_response': ai_response
            }
        
        print(f"✅ AI 决定: {decision.get('action', 'unknown')}")
        if decision.get('action') == 'use_tool':
            print(f"   工具: {decision.get('tool_name')}")
            print(f"   参数: {decision.get('tool_params')}")
        
        return decision
    
    def _build_function_calling_prompt(
        self, 
        user_query: str, 
        context: Optional[Dict] = None
    ) -> str:
        """
        构造给 LLM 的 Prompt
        这里就是 "Function Calling" 的核心！
        """
        # 获取所有工具的描述（文本格式）
        tools_description = self.tool_registry.list_tools_for_llm()
        
        # 构造上下文信息
        context_text = ""
        if context and context.get('observations'):
            context_text = "\n已完成的操作:\n"
            for i, obs in enumerate(context['observations'], 1):
                context_text += f"{i}. 使用了 {obs.get('tool_name', 'unknown')}\n"
                if obs.get('success'):
                    context_text += f"   结果: 成功\n"
                else:
                    context_text += f"   结果: 失败 - {obs.get('error')}\n"
        
        # 完整的 Prompt
        prompt = f"""
你是一个智能助手，可以使用工具来帮助用户。

═══════════════════════════════════════════
用户请求:
{user_query}
═══════════════════════════════════════════

{context_text}

{tools_description}

═══════════════════════════════════════════
你的任务:
═══════════════════════════════════════════

1. 分析用户的请求
2. 决定下一步行动

你有三种选择:

【选择 A】使用工具
如果需要调用工具来获取信息，返回:
{{
  "action": "use_tool",
  "reasoning": "我需要搜索房源，因为用户要找房子",
  "tool_name": "search_properties",
  "tool_params": {{
    "location": "UCL",
    "max_budget": 1500
  }}
}}

【选择 B】任务完成
如果已经有足够信息回答用户，返回:
{{
  "action": "finish",
  "reasoning": "我已经有所有需要的信息了",
  "final_answer": "根据您的需求，我推荐..."
}}

【选择 C】需要澄清
如果用户请求不清楚，返回:
{{
  "action": "clarify",
  "reasoning": "用户没有说预算",
  "question": "请问您的预算是多少？"
}}

═══════════════════════════════════════════
重要规则:
═══════════════════════════════════════════
- 一次只能调用一个工具
- 必须提供所有必需参数
- 返回有效的 JSON 格式
- 包含 reasoning 字段解释你的思考

现在，分析用户请求并返回你的决定（只返回 JSON）：
"""
        
        return prompt


# ============================================================================
# 工具注册表创建和初始化
# ============================================================================

def create_tool_registry() -> ToolRegistry:
    """
    创建并配置工具注册表
    返回 ToolRegistry 实例，包含所有已注册的工具
    """
    from core.tools import (
        search_properties_tool,
        calculate_commute_tool,
        check_safety_tool,
        get_weather_tool
    )
    
    registry = ToolRegistry()
    
    # 注册所有工具
    registry.register(search_properties_tool)
    registry.register(calculate_commute_tool)
    registry.register(check_safety_tool)
    registry.register(get_weather_tool)
    
    print(f"\n✅ 工具系统初始化完成！共注册 {len(registry.tools)} 个工具")
    
    return registry
