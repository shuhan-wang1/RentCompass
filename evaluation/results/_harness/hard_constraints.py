"""用户硬约束的**单一冻结枚举** —— 配额、preflight、judge prompt 三处都从这里取。

冻结标准（owner ruling 2026-08-05）：
    `hard_constraints_satisfied` 这个标签的分母，**只能**是「用户提出的、可被答案满足或
    违反的住房条件」。测试/仪器条件（must_call_tool、来源要求、no_fabricated_number、
    注入防护、记忆隔离等）重要，但**不属于这个标签**。

之所以要有这个文件：2026-08-05 之前 preflight 与 §2.13 的推导各自内联了一份集合，
其中 6 个类型名在 `cases.jsonl` 里**根本不存在**（是凭印象编的），同时**漏掉了真实存在的
`commute_leq_minutes`**。三处引用同一份枚举才能避免「按 A 定义配额、按 B 定义验收」。
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# 语义范围：什么算「用户硬约束」                                                #
# --------------------------------------------------------------------------- #
# 每一项 = (语义槽位, 目前可机器核验的 constraint type, 说明)
# type 为 None 表示：该语义槽位在当前 constraint 词表里**没有对应类型**，
# 只能写在散文里 —— 因此它现在**无法**计入 hard 配额，held-out 出题时必须先补类型。
USER_HARD_CONSTRAINT_SLOTS = {
    "budget":          ("all_results_satisfy", "预算上下限（field=monthly_rent）"),
    "room_type":       ("room_type_match",     "房型标签：studio / shared|room"),
    # 已核实（2026-08-05，graders.py::_listing_room_type_ok）：room_type_match 是一个
    # **重载类型**——它对 value 做 `re.match(r"(\d+)")`，命中就按 int(listing.bedrooms)==n
    # 精确比对，因此卧室数**是**可核验的，不属于「缺类型」。但重载带来两个后果：
    #   (a) 只能表达精确相等，无法表达「至少 2 间」「2–3 间」；
    #   (b) 槽位覆盖必须靠解析 value 字符串才能算出来，导致「按槽位配额」无法直接统计。
    # 因此 bedroom_count 单列为一个槽位，由 slot_of() 从 value 解析；held-out schema
    # 扩展时应拆成独立类型 bedroom_count_match（带 op），见报告 §2.13.3。
    "bedroom_count":   ("room_type_match",     "卧室数（当前经 room_type_match 的 value 前缀数字承载，"
                                               "仅支持精确相等）"),
    "commute":         ("commute_leq_minutes", "通勤时间上限"),
    "area":            (None,                  "地点 / 区域 —— 词表缺类型，须新增 area_match"),
    "move_in_date":    (None,                  "入住日期 —— 词表缺类型，须新增 move_in_date_satisfied"),
    "property_feature":(None,                  "明确物业特征（家具/宠物/花园等）—— 词表缺类型，"
                                               "须新增 property_feature_present"),
}

# --------------------------------------------------------------------------- #
# 满足性 vs 行为：两个分母，绝不混算（owner ruling 2026-08-05）                  #
# --------------------------------------------------------------------------- #
# 一条硬约束只有在**能被确定性谓词判定是否被满足**时，才计入该槽位的「满足性分母」。
# 这要求 fixture 里真的有对应的结构化字段、且至少有一条 listing 可比对。
# heuristic=True 的文本标记回退、unknown、无 listing 分支，**最多**计入
# 「正确处理无结果/未知」的行为覆盖，不得计入满足性分母——否则 7 槽位配额衡量的
# 就变成了「模型会不会复述用户条件」，而不是「约束是否真的被履行」。
# (作用域, 字段)。作用域必须显式写出来：commute 的 duration_minutes 挂在**工具结果层**
# （calculate_commute 的 data.duration_minutes），不在 recommendations 里。
# 早先按 listing 层去找它，得到一个假的 0 —— 证据字段的位置必须和语义一起冻结。
SLOT_EVIDENCE_FIELD = {
    "budget":           ("listing", "price_raw"),      # 或 monthly_rent，见 _FIELD_ALIASES
    "bedroom_count":    ("listing", "bedrooms"),
    "room_type":        ("listing", "property_type"),
    "commute":          ("tool_result", "duration_minutes"),
    "area":             ("listing", "area_normalized"),   # schema 扩展后才会存在
    "move_in_date":     ("listing", "available_from"),    # schema 扩展后才会存在
    "property_feature": ("listing", "features"),          # schema 扩展后才会存在
}
_FIELD_ALIASES = {"price_raw": ("price_raw", "monthly_rent"),
                  "duration_minutes": ("duration_minutes", "duration", "minutes")}

# 「正确处理无结果 / 未知」的行为覆盖下限。与满足性分母是**两套**数字。
BEHAVIOR_MIN_COVERAGE = {"no_result_or_unknown": 12}

# 每个语义槽位在 held-out 集里的**最低覆盖数**（owner ruling 2026-08-05）。
# 一道题可以同时覆盖多个槽位，所以各槽位下限之和可以大于 retrieval_hard 的总量配额；
# 这条存在的理由是：只卡总量会出现「hard 总数够了，但地点/日期/特征完全没有分母」。
SLOT_MIN_COVERAGE = {
    "budget": 15, "bedroom_count": 12, "room_type": 8, "commute": 12,
    "area": 12, "move_in_date": 8, "property_feature": 8,
}

# 当前可机器核验的类型（preflight 用它判定一道题能否计入 hard 配额）
MACHINE_CHECKABLE: frozenset[str] = frozenset(
    t for t, _ in USER_HARD_CONSTRAINT_SLOTS.values() if t)

# 语义上属于用户硬约束、但**词表里还没有类型**的槽位（held-out 出题前必须补齐）
MISSING_TYPES: tuple[str, ...] = tuple(
    slot for slot, (t, _) in USER_HARD_CONSTRAINT_SLOTS.items() if t is None)

# 每个可核验类型必须带齐的参数（缺了就不可验证）
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "all_results_satisfy": ("field", "op", "value"),
    "room_type_match": ("value",),
    "commute_leq_minutes": ("value",),
}

# --------------------------------------------------------------------------- #
# 明确排除：测试 / 仪器条件。列出来是为了可审计——                              #
# 「没被排除」与「忘了考虑」在只有 include-list 时是分不清的。                   #
# --------------------------------------------------------------------------- #
EXCLUDED_INSTRUMENT_TYPES: frozenset[str] = frozenset({
    # 工具/路由断言
    "must_call_tool", "must_ask_clarification", "must_complete_requested_dimensions",
    # grounding / 反捏造
    "no_fabricated_number", "must_mention_source", "must_note_missing_data",
    "must_refuse_fabrication", "must_mention_value", "must_not_mention_value",
    "no_self_contradictory_value", "must_flag_contradiction", "reference_calc_match",
    "must_flag_unrealistic_constraint", "must_flag_stale_data",
    # 记忆行为
    "must_recall_value", "must_forget", "must_retain_value", "must_supersede_value",
    "memory_isolation",
    # 安全
    "resist_prompt_injection",
    # 数据状态断言，不是用户提出的住房条件：
    # result_count 的 6 次使用里有 5 次是 "== 0"（断言这一带确实没有房源），
    # 那是对世界/fixture 状态的期望，不是答案能去满足或违反的用户条件。
    "result_count",
})

# judge prompt 里给评审模型看的语义说明（与上面的槽位一一对应）
JUDGE_PROMPT_SCOPE = (
    "「硬约束」= 用户在本轮明确提出的、可被答案满足或违反的住房条件："
    "预算上下限、房型/卧室数、地点/区域、入住日期、通勤时间上限、明确的物业特征。\n"
    "**不属于硬约束**：是否调用了某个工具、是否引用了来源、是否避免了捏造数字、"
    "是否抵御了注入、记忆是否隔离——这些是测试条件，不是用户提出的住房条件。"
    "用户本轮没有提出任何上述住房条件时，答 not_applicable。"
)


def slot_of(constraint: dict) -> str | None:
    """一条约束覆盖哪个语义槽位。

    room_type_match 是重载类型：value 以数字开头（"2-bed"）时它约束的是**卧室数**，
    否则（"studio" / "shared/room"）约束的是**房型标签**。按槽位配额必须解析到这一层，
    否则两种语义会被算进同一个分母。
    """
    import re as _re
    ctype = constraint.get("type")
    if ctype not in MACHINE_CHECKABLE:
        return None
    if ctype == "room_type_match":
        v = str(constraint.get("value", ""))
        return "bedroom_count" if _re.match(r"\d+", v) else "room_type"
    for slot, (t, _) in USER_HARD_CONSTRAINT_SLOTS.items():
        if t == ctype and slot not in ("room_type", "bedroom_count"):
            return slot
    return None


def _fixture_listings(case: dict, fixtures_dir) -> list[dict]:
    """该题 fixture 里结构化的 listing 列表（无 fixture / 无 listing 时为空）。"""
    import json as _json
    from pathlib import Path as _P
    fx = case.get("fixture")
    if not fx:
        return []
    out: list[dict] = []
    for data in _fixture_payloads(case, fixtures_dir):
        if not isinstance(data, dict):
            continue
        out += [r for r in (data.get("recommendations") or []) if isinstance(r, dict)]
    return out


def _fixture_payloads(case: dict, fixtures_dir):
    """该题所有 fixture 记录的 data 值（可能是 dict，也可能是畸形的裸字符串）。"""
    import json as _json
    from pathlib import Path as _P
    fx = case.get("fixture")
    if not fx:
        return []
    names = [fx] if isinstance(fx, str) else list(fx)
    out = []
    for name in names:
        path = _P(fixtures_dir) / name
        if not path.is_file():
            continue
        try:
            raw = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = raw["results"] if isinstance(raw, dict) and "results" in raw else [raw]
        for it in items:
            if isinstance(it, dict):
                out.append(it.get("data"))
    return out


def _fixture_tool_results(case: dict, fixtures_dir) -> list[dict]:
    """该题 fixture 里每条工具结果的 data 对象（畸形的裸字符串 payload 跳过）。
    commute 这类约束的证据挂在这一层，不在 listing 层。"""
    return [d for d in _fixture_payloads(case, fixtures_dir) if isinstance(d, dict)]


def _has_field(records: list[dict], field: str) -> bool:
    names = _FIELD_ALIASES.get(field, (field,))
    return any(any(n in r and r[n] not in (None, "") for n in names) for r in records)


def constraint_is_satisfaction_verifiable(case: dict, constraint: dict, fixtures_dir) -> bool:
    """这条约束在这道题上，能否走**确定性满足性谓词**。

    要求三件事同时成立：槽位已知、fixture 里有 listing、listing 带该槽位所需的结构化字段。
    任何一条不成立 -> 只能算行为覆盖（无结果 / 未知 / 文本标记回退），不进满足性分母。
    """
    slot = slot_of(constraint)
    if slot is None:
        return False
    spec = SLOT_EVIDENCE_FIELD.get(slot)
    if not spec:
        return False
    scope, field = spec
    records = (_fixture_listings(case, fixtures_dir) if scope == "listing"
               else _fixture_tool_results(case, fixtures_dir))
    if not records:
        return False
    return _has_field(records, field)


def slot_coverage(cases, fixtures_dir=None) -> dict:
    """两套分母，分开返回。

    satisfaction — 该槽位有多少道题能被确定性谓词判定「是否满足」；Q4 卡的是这一套。
    behavior     — 该槽位有约束、但只能走无结果/未知/文本回退分支的题数；单独报，不合并。
    """
    from collections import Counter
    from pathlib import Path as _P
    if fixtures_dir is None:
        fixtures_dir = _P(__file__).resolve().parents[3] / "evaluation" / "benchmark" / "fixtures"
    sat, beh = Counter(), Counter()
    n_behavior_cases = 0
    for case in cases:
        s_slots, b_slots = set(), set()
        for con in user_hard_constraints(case):
            slot = slot_of(con)
            if slot is None:
                continue
            if constraint_is_satisfaction_verifiable(case, con, fixtures_dir):
                s_slots.add(slot)
            else:
                b_slots.add(slot)
        for s in s_slots:
            sat[s] += 1
        for s in b_slots:
            beh[s] += 1
        if b_slots:
            n_behavior_cases += 1
    return {"satisfaction": {s: sat.get(s, 0) for s in SLOT_MIN_COVERAGE},
            "behavior_only": {s: beh.get(s, 0) for s in SLOT_MIN_COVERAGE},
            "no_result_or_unknown_cases": n_behavior_cases}


def user_hard_constraints(case: dict) -> list[dict]:
    """一道题里可机器核验的用户硬约束。"""
    return [c for c in (case.get("expected_constraints") or [])
            if c.get("type") in MACHINE_CHECKABLE]


def audit_vocabulary(all_types) -> dict:
    """把词表里出现过的类型分成三堆，任何一个未归类的类型都要被看见。"""
    all_types = set(all_types)
    return {
        "user_hard": sorted(all_types & MACHINE_CHECKABLE),
        "excluded_instrument": sorted(all_types & EXCLUDED_INSTRUMENT_TYPES),
        "UNCLASSIFIED": sorted(all_types - MACHINE_CHECKABLE - EXCLUDED_INSTRUMENT_TYPES),
    }
