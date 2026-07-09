# agent/function_calling.py

"""
Function Calling, 效果类似Openai
负责让 pretrained model 从工具列表中选择合适的工具
"""

import json
import re
from typing import Dict, Optional
from tool_system.base import ToolRegistry


def call_ollama(prompt: str, timeout: int = 30) -> str:
    """
    调用本地 Ollama（免费）
    """
    import requests
    
    url = "http://localhost:11434/api/generate"
    payload = {
        "model": "llama3.2:1b",
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 500
        }
    }
    
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        return result.get("response", "")
    except Exception as e:
        print(f"❌ Ollama 调用失败: {e}")
        return ""


def extract_json_from_text(text: str) -> Optional[Dict]:
    """
    从 AI 回复中提取 JSON
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
    Function Calling 实现
    让 AI 从工具列表中选择合适的工具
    """
    
    def __init__(self, tool_registry: ToolRegistry):
        self.tool_registry = tool_registry
    
    def ask_ai_to_choose_tool(
        self, 
        user_query: str, 
        context: Optional[Dict] = None
    ) -> Dict:
        """
        让 AI 选择要调用的工具
        
        返回格式:
        {
            'action': 'use_tool' | 'finish' | 'error',
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
        
        # 调用 Ollama（本地免费）
        print("📤 发送给 Ollama...")
        ai_response = call_ollama(prompt, timeout=30)
        
        print(f"📥 AI 回复 ({len(ai_response)} 字符)")
        print(f"   {ai_response[:200]}...")
        
        # 解析 AI 的回复
        decision = extract_json_from_text(ai_response)
        
        if not decision:
            print("❌ 无法解析 AI 回复")
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
        构造给 AI 的 Prompt
        这里就是 "Function Calling" 的核心！
        """
        # 获取所有工具的描述（文本格式）
        tools_description = self.tool_registry.list_tools_for_llm()
        
        # 构造上下文信息
        context_text = ""
        if context:
            if context.get('observations'):
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


# ============================================
# 使用示例
# ============================================

async def demo_function_calling():
    """
    演示 Function Calling 的完整流程
    """
    from tools import create_tool_registry
    
    # 1. 创建工具注册中心
    print("📦 初始化工具系统...")
    registry = create_tool_registry()
    
    # 2. 创建 Function Calling 实例
    fc = FunctionCalling(registry)
    
    # 3. 用户查询
    user_query = "Find me a flat near UCL, budget £1500, max 30 min commute"
    
    # 4. 让 AI 选择工具（这就是 Function Calling！）
    decision = fc.ask_ai_to_choose_tool(user_query)
    
    # 5. 根据 AI 的决定执行
    if decision['action'] == 'use_tool':
        tool_name = decision['tool_name']
        tool_params = decision['tool_params']
        
        print(f"\n🔧 执行工具: {tool_name}")
        print(f"   参数: {tool_params}")
        
        # 执行工具
        result = await registry.execute_tool(tool_name, **tool_params)
        
        if result.success:
            print(f"✅ 工具执行成功")
            print(f"   数据: {result.data}")
        else:
            print(f"❌ 工具执行失败: {result.error}")
    
    elif decision['action'] == 'finish':
        print(f"\n✅ 任务完成")
        print(f"   答案: {decision.get('final_answer')}")
    
    elif decision['action'] == 'clarify':
        print(f"\n❓ 需要澄清")
        print(f"   问题: {decision.get('question')}")
    
    else:
        print(f"\n❌ 未知动作: {decision.get('action')}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(demo_function_calling())