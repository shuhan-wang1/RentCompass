"""
ask_user — 终止型工具（design §2.5a）。

模型只提供主观判断部分（question / clarification_kind / missing_fields /
missing_optional_fields）。执行器（并行开发的循环执行层）会从
accumulated_search_criteria 确定性补齐 known_criteria——本工具**绝不**伪造它，
四字段齐备后 app.py 的前端澄清读取逻辑零改动。
"""
from typing import List
from core.tool_system import Tool


async def ask_user_impl(
    question: str,
    clarification_kind: str = "other",
    missing_fields: List[str] = None,
    missing_optional_fields: List[str] = None,
) -> dict:
    """回显模型提供的澄清字段（不做任何补齐/臆测）。"""
    return {
        "success": True,
        "status": "ask_user",
        "question": question,
        "clarification_kind": clarification_kind,
        "missing_fields": list(missing_fields or []),
        "missing_optional_fields": list(missing_optional_fields or []),
    }


ask_user_tool = Tool(
    name="ask_user",
    description=(
        "Ask the user ONE clarifying question, written in the user's own language, when you "
        "cannot proceed without their input. Also use it to confirm a memory save by showing the "
        "exact content to be stored. 用用户的语言反问一个澄清问题；确认写记忆时展示将要保存的确切内容。"
    ),
    func=ask_user_impl,
    parameters={
        'type': 'object',
        'properties': {
            'question': {
                'type': 'string',
                'description': "the clarifying question to show the user, in the user's language",
            },
            'clarification_kind': {
                'type': 'string',
                'enum': ['missing_area', 'soft_criteria', 'other'],
                'default': 'other',
                'description': 'what kind of clarification this is',
            },
            'missing_fields': {
                'type': 'array',
                'items': {'type': 'string'},
                'default': [],
                'description': 'required criteria fields still missing',
            },
            'missing_optional_fields': {
                'type': 'array',
                'items': {'type': 'string'},
                'default': [],
                'description': 'optional criteria fields still missing',
            },
        },
        'required': ['question'],
    },
    terminal=True,
    side_effect="none",
    max_retries=1,
)
