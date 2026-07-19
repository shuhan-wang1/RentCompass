"""
Tool System - Agent框架的核心工具系统
核心概念：
1. Tool - 工具定义（名称、描述、参数、执行函数）
2. ToolResult - 标准化工具返回结果
3. ToolRegistry - 工具注册中心（管理、查询、执行所有工具）
"""

import asyncio
import time
from typing import Callable, Dict, Any, Optional, List
from dataclasses import dataclass
import traceback
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field, create_model
from uk_rent_agent.tools.idempotency import IdempotencyStore

logger = logging.getLogger(__name__)


def _model_from_schema(name: str, schema: Dict[str, Any]) -> type[BaseModel]:
    """Create the runtime input contract once; JSON schema is then generated from it."""
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    required = set(schema.get("required", []))
    fields = {}
    for field_name, definition in schema.get("properties", {}).items():
        annotation = type_map.get(definition.get("type"), Any)
        is_required = field_name in required
        if not is_required:
            annotation = Optional[annotation]
        default = ... if is_required else definition.get("default", None)
        fields[field_name] = (
            annotation,
            Field(default=default, description=definition.get("description")),
        )
    return create_model(f"{''.join(part.title() for part in name.split('_'))}Input", **fields)


# JSON-schema constraint keywords the pydantic round-trip drops (it only captures
# type/default/description). Losing enum/items degrades native function-calling:
# the model never sees the legal values, so it guesses parameters it could have read.
_CONSTRAINT_KEYWORDS = (
    "enum", "items", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
)


def _merge_constraint_keywords(emitted: Dict[str, Any], original: Dict[str, Any]) -> Dict[str, Any]:
    """Copy per-property constraint keywords from the author-written schema back into
    the pydantic-emitted one (additive only — never overrides what pydantic emitted)."""
    emitted_props = emitted.get("properties") or {}
    for pname, pdef in (original.get("properties") or {}).items():
        target = emitted_props.get(pname)
        if not isinstance(target, dict) or not isinstance(pdef, dict):
            continue
        for kw in _CONSTRAINT_KEYWORDS:
            if kw in pdef and kw not in target:
                target[kw] = pdef[kw]
    return emitted


@dataclass(frozen=True)
class ToolSpec:
    """
    统一工具描述契约（design §2.8a）。

    - in-process 由 ``Tool.to_spec()`` / ``ToolRegistry.list_specs()`` 构造；
    - MCP 由 ``MCPToolClient.list_specs()`` 从 list_tools() 的 inputSchema+annotations
      构造（缺字段时回退到 fallback_registry 的 spec）。
    两进程共享同一份工具代码，registry 是单一事实源。
    """
    name: str
    description: str
    input_schema: dict      # 原始 JSON schema（OpenAI FC 格式，即 Tool.parameters）
    side_effect: str        # "none" | "write"
    retry_safe: bool
    version: str = "1"      # 幂等键的工具版本语义——必须与 Tool.version 一致
    terminal: bool = False  # ask_user


def to_function_calling_format(spec: "ToolSpec") -> Dict[str, Any]:
    """把 ToolSpec 转成原生 Function-Calling 工具定义（非 strict）。

    strict 适配（全属性 required + additionalProperties:false + 剥不支持关键词）是
    Phase 2 的独立步骤，这里不做。
    """
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


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
    version: str = "1"
    idempotency_key: Optional[str] = None
    
    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            'success': self.success,
            'data': self.data,
            'error': self.error,
            'execution_time_ms': self.execution_time_ms,
            'tool_name': self.tool_name,
            'version': self.version,
            'idempotency_key': self.idempotency_key,
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
        retry_on_error: bool = True,
        version: str = "1",
        side_effect: str = "none",
        retry_safe: Optional[bool] = None,
        cacheable: bool = False,
        terminal: bool = False,
        input_model: Optional[type[BaseModel]] = None,
        output_model: Optional[type[BaseModel]] = None,
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
        self.input_model = input_model or _model_from_schema(name, parameters)
        self.output_model = output_model
        self.parameters = _merge_constraint_keywords(
            self.input_model.model_json_schema(), parameters)
        self.return_direct = return_direct
        self.max_retries = max_retries
        self.retry_on_error = retry_on_error
        self.version = version
        self.side_effect = side_effect
        self.retry_safe = (side_effect == "none") if retry_safe is None else retry_safe
        self.cacheable = cacheable
        self.terminal = terminal
        
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
        
        idempotency_key = kwargs.pop("idempotency_key", None)
        idempotency_store = kwargs.pop("_idempotency_store", None)

        # 填充默认值
        kwargs = self._apply_defaults(kwargs)
        try:
            kwargs = self.input_model.model_validate(kwargs).model_dump(exclude_none=True)
        except Exception as exc:
            return ToolResult(False, error=f"ValidationError: {exc}", tool_name=self.name,
                              version=self.version, idempotency_key=idempotency_key)

        claimed = False
        if self.side_effect == "write":
            if not idempotency_key:
                return ToolResult(False, error="idempotency_key is required for write tools",
                                  tool_name=self.name, version=self.version)
            previous = idempotency_store.get(idempotency_key) if idempotency_store else None
            if previous is not None:
                return ToolResult(True, data=previous, tool_name=self.name, version=self.version,
                                  idempotency_key=idempotency_key)
            if idempotency_store:
                claimed = idempotency_store.claim(idempotency_key, self.name)
                if not claimed:
                    return ToolResult(False, error="logical invocation is already in progress",
                                      tool_name=self.name, version=self.version,
                                      idempotency_key=idempotency_key)

        attempts = self.max_retries if self.retry_safe else 1
        for attempt in range(attempts):
            try:
                logger.debug("Executing %s (attempt %s/%s)", self.name, attempt + 1, attempts)
                
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
                
                logger.info("Tool %s succeeded (%.0fms)", self.name, execution_time)
                
                logical_success = not isinstance(result, dict) or result.get('success', True) is not False
                if logical_success and self.output_model is not None:
                    result = self.output_model.model_validate(result).model_dump()
                if logical_success and claimed:
                    idempotency_store.complete(idempotency_key, result)
                return ToolResult(
                    success=logical_success,
                    data=result,
                    error=result.get('error') if isinstance(result, dict) and not logical_success else None,
                    execution_time_ms=execution_time,
                    tool_name=self.name,
                    version=self.version,
                    idempotency_key=idempotency_key,
                )
            
            except Exception as e:
                execution_time = (time.time() - start_time) * 1000
                error_msg = f"{type(e).__name__}: {str(e)}"
                
                logger.warning("Tool %s failed: %s", self.name, error_msg)
                
                # 是否重试
                if attempt < attempts - 1 and self.retry_on_error and self.retry_safe:
                    wait_time = 2 ** attempt  # 指数退避：2, 4, 8...
                    logger.info("Retrying %s in %ss", self.name, wait_time)
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # 最后一次尝试失败
                    logger.error("Tool %s exhausted all retries", self.name)
                    if attempt == attempts - 1:
                        traceback.print_exc()
                    return ToolResult(
                        success=False,
                        data=None,
                        error=error_msg,
                        execution_time_ms=execution_time,
                        tool_name=self.name,
                        version=self.version,
                        idempotency_key=idempotency_key,
                    )
    
    def _validate_input(self, kwargs: Dict):
        """验证是否满足 required 的参数"""
        required = self.parameters.get('required', [])
        
        for param in required:
            if param not in kwargs:
                raise ValueError(f"缺少必需参数: {param}")
    
    def _apply_defaults(self, kwargs: Dict) -> Dict:
        """为缺失的参数填充默认值"""
        result = kwargs.copy()
        properties = self.parameters.get('properties', {})
        
        for param_name, param_info in properties.items():
            if (
                param_name not in result
                and 'default' in param_info
                and param_info['default'] is not None
            ):
                result[param_name] = param_info['default']
        
        return result
    
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

    def to_spec(self) -> "ToolSpec":
        """构造统一的 ToolSpec 契约（design §2.8a）。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
            side_effect=self.side_effect,
            retry_safe=self.retry_safe,
            version=self.version,
            terminal=self.terminal,
        )

    def __repr__(self) -> str:
        return f"Tool(name='{self.name}')"


class ToolRegistry:
    """
    工具注册中心 - 负责存放、检索、组织多个 Tool 实例
    """
    
    def __init__(self, idempotency_store: Optional[IdempotencyStore] = None):
        self.tools: Dict[str, Tool] = {}
        self._stats: Dict[str, Dict] = {}
        default_path = Path(
            os.getenv(
                "IDEMPOTENCY_DB",
                str(Path(__file__).resolve().parents[2] / ".runtime" / "idempotency.sqlite3"),
            )
        )
        self._idempotency_store = idempotency_store or IdempotencyStore(default_path)
    
    def register(self, tool: Tool):
        """注册一个工具"""
        if tool.name in self.tools:
            logger.warning("Tool %s already exists and will be replaced", tool.name)
        
        self.tools[tool.name] = tool
        self._stats[tool.name] = {
            'total_calls': 0,
            'successful_calls': 0,
            'failed_calls': 0,
            'total_time_ms': 0
        }
        
        logger.debug("Registered tool: %s", tool.name)
    
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

    def list_specs(self) -> List[ToolSpec]:
        """列出所有已注册工具的 ToolSpec 契约（design §2.8a）。"""
        return [tool.to_spec() for tool in self.tools.values()]
    
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
        result = await tool.execute(_idempotency_store=self._idempotency_store, **kwargs)
        
        # 更新统计
        stats = self._stats[name]
        stats['total_calls'] += 1
        if result.success:
            stats['successful_calls'] += 1
        else:
            stats['failed_calls'] += 1
        if result.execution_time_ms:
            stats['total_time_ms'] += result.execution_time_ms

        # Offline-eval instrumentation (additive; no-op unless active).
        try:
            from evaluation.metrics import collector
            if collector.is_active():
                collector.record_tool_call(name, result, kwargs, mcp=False)
        except Exception:
            pass

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
        get_weather_tool,
        web_search_tool,
        search_nearby_pois_tool,
        get_property_details_tool,
        calculate_commute_cost_tool  # 🆕 综合通勤成本计算工具
    )
    from core.tools.check_transport_cost import check_transport_cost_tool
    from core.tools.get_transport_info import get_transport_info_tool
    from core.tools.memory_tools import recall_memory_tool, remember_tool
    from core.tools.ask_user import ask_user_tool
    from core.tools.compare_or_rank_areas import compare_or_rank_areas_tool

    registry = ToolRegistry()

    # 注册所有工具
    registry.register(search_properties_tool)
    registry.register(calculate_commute_tool)
    registry.register(calculate_commute_cost_tool)  # 🆕 综合通勤成本计算工具（时间+费用）
    registry.register(check_safety_tool)
    registry.register(get_weather_tool)
    registry.register(web_search_tool)
    registry.register(search_nearby_pois_tool)
    registry.register(get_property_details_tool)
    registry.register(check_transport_cost_tool)  # 交通费用查询工具
    registry.register(get_transport_info_tool)    # 🚇 实时 TfL：journey/fare/travelcard/line status
    registry.register(recall_memory_tool)         # 🧠 长期记忆：召回
    registry.register(remember_tool)              # 🧠 长期记忆：写入
    registry.register(ask_user_tool)              # ❓ 终止型：向用户反问澄清
    registry.register(compare_or_rank_areas_tool)  # 🏙️ 区域性价比排序/比较（design §2.5b）

    logger.info("Tool registry initialized with %s tools", len(registry.tools))

    return registry
