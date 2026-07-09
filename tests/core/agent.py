"""
Agent - ReAct 循环的实现
Reasoning → Action → Observation → 重复
"""

import asyncio
from typing import Callable, List, Optional, Dict, Any
from core.tool_system import ToolRegistry, FunctionCalling


class Agent:
    """
    ReAct Agent - 自动推理和执行
    
    工作流程：
    1. 用户输入问题
    2. Agent 分析问题（Reasoning）
    3. 决定执行哪个工具（Action）
    4. 执行工具并获得结果（Observation）
    5. 根据结果决定下一步（回到步骤 2 或结束）
    """
    
    def __init__(
        self,
        tool_registry: ToolRegistry,
        llm_func: Callable,
        max_turns: int = 5,
        verbose: bool = True
    ):
        """
        参数：
            tool_registry: 工具注册表
            llm_func: LLM 函数（接收 prompt，返回文本）
            max_turns: 最大迭代次数
            verbose: 是否打印详细信息
        """
        self.tool_registry = tool_registry
        self.llm_func = llm_func
        self.max_turns = max_turns
        self.verbose = verbose
        self.function_calling = FunctionCalling(tool_registry)
        self.observations: List[Dict[str, Any]] = []
        self.turn = 0
    
    async def run(self, user_query: str) -> Dict[str, Any]:
        """
        运行 Agent - 处理用户查询
        
        返回：
        {
            'success': True/False,
            'final_answer': '最终答案',
            'observations': [{...}, {...}],  # 所有观察记录
            'turns': 实际执行的轮次
        }
        """
        self.observations = []
        self.turn = 0
        
        self._print(f"\n{'='*70}")
        self._print(f"🚀 Agent 开始工作")
        self._print(f"{'='*70}")
        self._print(f"📝 用户问题: {user_query}\n")
        
        # Agent 循环
        while self.turn < self.max_turns:
            self.turn += 1
            
            # 步骤 1: 询问 AI 做什么
            decision = self.function_calling.ask_ai_to_choose_tool(
                user_query=user_query,
                llm_func=self.llm_func,
                context={'observations': self.observations}
            )
            
            # 检查决定
            if decision.get('action') == 'error':
                self._print(f"\n❌ 第 {self.turn} 步: 解析 AI 响应失败")
                self._print(f"   原始响应: {decision.get('raw_response', 'N/A')[:100]}...")
                return {
                    'success': False,
                    'error': decision.get('error'),
                    'final_answer': None,
                    'observations': self.observations,
                    'turns': self.turn
                }
            
            # 步骤 2: 根据 AI 的决定行动
            if decision.get('action') == 'use_tool':
                self._print(f"\n🔧 第 {self.turn} 步: 执行工具")
                
                tool_name = decision.get('tool_name')
                tool_params = decision.get('tool_params', {})
                reasoning = decision.get('reasoning', '')
                
                self._print(f"   推理: {reasoning}")
                self._print(f"   工具: {tool_name}")
                self._print(f"   参数: {tool_params}")
                
                # 步骤 3: 执行工具
                result = await self.tool_registry.execute_tool(tool_name, **tool_params)
                
                # 记录观察
                observation = {
                    'tool_name': tool_name,
                    'tool_params': tool_params,
                    'success': result.success,
                    'data': result.data,
                    'error': result.error,
                    'execution_time_ms': result.execution_time_ms
                }
                self.observations.append(observation)
                
                if result.success:
                    self._print(f"   ✅ 执行成功")
                    if result.data:
                        # 打印数据摘要
                        if isinstance(result.data, dict) and 'count' in result.data:
                            self._print(f"   📊 找到 {result.data['count']} 个结果")
                else:
                    self._print(f"   ❌ 执行失败: {result.error}")
                
                # 继续下一次循环
                continue
            
            elif decision.get('action') == 'finish':
                # 任务完成
                final_answer = decision.get('final_answer', '')
                self._print(f"\n✅ 第 {self.turn} 步: 任务完成")
                self._print(f"\n💡 最终答案:")
                self._print(f"{final_answer}")
                
                return {
                    'success': True,
                    'final_answer': final_answer,
                    'observations': self.observations,
                    'turns': self.turn
                }
            
            elif decision.get('action') == 'clarify':
                # 需要澄清
                question = decision.get('question', '')
                self._print(f"\n❓ 第 {self.turn} 步: 需要澄清")
                self._print(f"   问题: {question}")
                
                # 在实际应用中，这里应该询问用户
                # 这里为了演示，我们使用默认回答
                user_query = self._handle_clarification(question, user_query)
                
                continue
            
            else:
                # 未知动作
                self._print(f"\n⚠️  第 {self.turn} 步: 未知动作: {decision.get('action')}")
                return {
                    'success': False,
                    'error': f"Unknown action: {decision.get('action')}",
                    'final_answer': None,
                    'observations': self.observations,
                    'turns': self.turn
                }
        
        # 达到最大轮次
        self._print(f"\n⏱️  达到最大轮次 ({self.max_turns})")
        
        return {
            'success': False,
            'error': f'Max turns ({self.max_turns}) exceeded',
            'final_answer': None,
            'observations': self.observations,
            'turns': self.turn
        }
    
    def _handle_clarification(self, question: str, current_query: str) -> str:
        """
        处理澄清问题
        在真实应用中，这里应该与用户交互
        这里为了演示，返回一个默认答案
        """
        # 可以增强查询
        clarified_query = f"{current_query}\n注: {question}"
        return clarified_query
    
    def _print(self, msg: str):
        """只在 verbose 模式下打印"""
        if self.verbose:
            print(msg)
    
    def get_stats(self):
        """获取执行统计"""
        return {
            'turns': self.turn,
            'observations_count': len(self.observations),
            'successful_tools': sum(1 for o in self.observations if o.get('success')),
            'failed_tools': sum(1 for o in self.observations if not o.get('success')),
            'total_execution_time_ms': sum(
                o.get('execution_time_ms', 0) for o in self.observations
            )
        }
    
    async def process_user_request(self, user_query: str):
        """处理用户请求"""
        
        # 解析用户偏好
        user_prefs = self.parse_user_preferences(user_query)
        
        print(f"\n📋 解析的用户偏好:")
        print(f"   💰 预算: £{user_prefs.get('budget', 'N/A')}")
        print(f"   ⏱️ 通勤: {user_prefs.get('max_commute', 'N/A')} 分钟")
        print(f"   🚨 关心安全性: {user_prefs.get('care_about_safety', True)}")
        
        # 存储到会话中，供工具使用
        self.current_user_prefs = user_prefs
        
        # ... 继续执行 Agent 逻辑 ...
    
    def parse_user_preferences(self, query: str) -> dict:
        """
        从用户查询中解析偏好
        """
        prefs = {
            'care_about_safety': True,  # 默认关心
            'budget': None,
            'max_commute': 50,
            'location': None
        }
        
        # 检查用户是否明确说不关心安全
        query_lower = query.lower()
        if any(phrase in query_lower for phrase in [
            'do not care about safety',
            'don\'t care about safety',
            'not care about safety',
            'don\'t care about crime',
            '不在乎安全',
            '不关心安全',
            '不考虑安全'
        ]):
            prefs['care_about_safety'] = False
        
        # 解析其他信息...
        
        return prefs
