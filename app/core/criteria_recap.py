"""A recap of the conversation's search criteria, put first when the user's own message asks for one.

Browser test 2026-11-05: a couple asked the assistant to "first summarise all my requirements" (cat
allowed, Canary Wharf, move-in date, 40-minute commute, room type, budget). The reply went straight
to a candidate list and never restated them. Whether a recap was asked for is read from the user's
message, and the recap is rendered from the accumulated criteria, so it does not depend on the model.
"""
from __future__ import annotations

import re
from datetime import date

HEADING_EN = "Your requirements as I have them:"
HEADING_ZH = "目前记录的条件："

# ── Detection ────────────────────────────────────────────────────────────────────────────────
_EN_VERB = (r"summari[sz]e|sum\s+up|summary|recap|recapitulate|restate|repeat(?:\s+back)?|"
            r"read\s+back|list(?:\s+out|\s+back)?|confirm|go\s+(?:over|through)|"
            r"run\s+(?:me\s+|us\s+)?through|outline|remind\s+(?:me|us)\s+of")
_EN_QUALIFIER = (r"(?:current|search|housing|rental|flat|home|house|main|key|hard|stated|full|"
                 r"whole|other|list\s+of|set\s+of)")
_EN_OBJECT = (
    r"(?:(?:all|each|every\s+one)\s+(?:of\s+)?)?(?:my|our)\s+(?:" + _EN_QUALIFIER + r"\s+){0,2}"
    r"(?:requirements?|criteria|conditions|needs|must[- ]haves|preferences|filters|constraints|"
    r"wish\s*list|specs?|specifications|parameters)\b"
    r"|(?:everything|all|what)\s+(?:that\s+)?(?:i|we)(?:'ve|’ve|\s+have)?\s+(?:asked\s+for|told\s+you|"
    r"said|stated|specified|mentioned|want(?:ed)?|need(?:ed)?|given\s+you|listed)\b"
    r"|what\s+(?:i|we)(?:'m|’m|\s+am|'re|’re|\s+are)\s+looking\s+for\b"
    r"|the\s+(?:search\s+)?(?:requirements?|criteria|conditions)\s+(?:(?:that\s+)?(?:i|we)\b|so\s+far\b)"
    # "summarise every requirement (cat allowed, ...)", but not "all the requirements for renting".
    r"|(?:(?:every|each)\s+(?:single\s+)?(?:requirement|criterion|condition)|"
    r"all\s+(?:the\s+)?(?:requirements|criteria|conditions))\b"
    r"(?!\s+(?:for|of|to|in|when|needed|required|that)\b)")
_EN_RECAP = re.compile(r"\b(?:" + _EN_VERB + r")\b(?P<gap>[^.!?\n]{0,40}?)\b(?:" + _EN_OBJECT + r")",
                       re.I)
# Between the verb and "my requirements": a listing, a yes/no check or a comparison means the
# message is about that thing ("confirm this flat meets all my requirements").
_EN_GAP_OTHER = re.compile(
    r"\b(?:this|that|these|those|it|they|them|whether|if|against|listing|listings|flat|flats|"
    r"property|properties|house|houses|home|homes|room|rooms|place|places|area|areas|results?)\b",
    re.I)
_EN_AFTER_OTHER = re.compile(
    r"^[^.!?\n]{0,25}?\b(?:against|meets?|met|match(?:es|ed)?|fits?|satisf\w*)\b", re.I)
# A bare "Summarise, and make it a 1-bed": the verb opens the message and its object is the
# conversation itself, which the rest of the message goes on to change.
_EN_BARE = re.compile(r"^\W*(?:(?:please|can\s+you|could\s+you|first|ok(?:ay)?|right)[\s,]+)*"
                      r"(?:summari[sz]e|recap|sum\s+up)(?:\s+(?:first|please|again))?\s*"
                      r"(?:[,;:]|\s(?:and|then)\b)", re.I)
_EN_NEGATED = re.compile(r"\b(?:don'?t|don’t|do\s+not|no\s+need\s+to|never|without)\s+(?:\w+\s+){0,2}$",
                         re.I)

_ZH_VERB = (r"总结|复述|确认|核对|列(?:一下|出来|出|下)?|汇总|梳理|概括|整理|回顾|重复|说一下|说一遍|"
            r"再说一遍|过一遍|捋一下|理一下|归纳")
_ZH_OBJECT = (r"(?:我|我们)(?:的|目前的|现在的|当前的)?(?:所有|全部|各项|每一?[个条项])?的?"
              r"(?:找房|租房|搜索|筛选)?(?:条件|要求|需求|标准|偏好)"
              r"|(?:所有|全部|各项)的?(?:找房|租房|搜索|筛选)?(?:条件|要求|需求|标准)")
_ZH_RECAP = (
    re.compile(r"(?:" + _ZH_VERB + r")(?P<gap>[^。！？!?\n]{0,6}?)(?:" + _ZH_OBJECT + r")"),
    re.compile(r"(?:把|将)(?:" + _ZH_OBJECT + r")(?P<gap>[^。！？!?\n]{0,6}?)(?:" + _ZH_VERB + r")"),
    re.compile(r"(?:" + _ZH_OBJECT + r")(?P<gap>[^。！？!?\n]{0,3}?)"
               r"(?:总结|复述|汇总|梳理|概括|整理|归纳|列|确认)一下"),
    re.compile(r"(?P<gap>)(?:需求|条件|要求)(?:总结|汇总|清单)"),
)
# 「复述一下」 on its own asks to hear the requirements back, unless it names something else.
_ZH_REPEAT_BACK = re.compile(r"复述(?:一下|一遍)?")
_ZH_GAP_OTHER = re.compile(r"房源|房子|这套|那套|这个|那个|这家|该|区域|地区|小区|附近|合同|评价")
_ZH_AFTER_OTHER = re.compile(r"^[^。！？!?\n]{0,10}?(?:符合|满足|匹配|对得上|相符)")
_ZH_NEGATED = re.compile(r"(?:不要|不用|别|无需|不必)$")
# "What do you remember about me?" has its own answer: every saved fact (app._asks_what_is_remembered).
_RECALL = re.compile(
    r"\bwhat\s+(?:do\s+|did\s+)?you\s+(?:remember|know|have\s+(?:saved|stored|on\s+file))\b|"
    r"\b(?:remember|saved|stored|know)\s+about\s+(?:me|us)\b|"
    r"\byou(?:'ve|’ve|\s+have)?\s+(?:saved|stored|remembered)\b|"
    r"还?记得|记不记得|记住了(?:什么|哪些)|保存了(?:什么|哪些)|存了(?:什么|哪些)|记忆", re.I)


def asks_for_criteria_recap(message: str) -> bool:
    """True when the user's message asks to have their requirements summarised back to them.

    "Before you search, summarise all my requirements", "recap my criteria", 「先总结一下我的条件」,
    「确认一下我所有的要求」, 「把我的条件列一下」, 「复述一下」. Not "what do you remember about me"
    (the recall path), "summarise this listing" or "give me a summary of the area"."""
    text = str(message or "")[:4000]
    if not text.strip() or _RECALL.search(text):
        return False
    if _EN_BARE.search(text):
        return True
    for match in _EN_RECAP.finditer(text):
        if not (_EN_GAP_OTHER.search(match.group("gap") or "")
                or _EN_AFTER_OTHER.search(text[match.end():])
                or _EN_NEGATED.search(text[max(0, match.start() - 30):match.start()])):
            return True
    for pattern in _ZH_RECAP:
        for match in pattern.finditer(text):
            if not (_ZH_GAP_OTHER.search(match.group("gap") or "")
                    or _ZH_AFTER_OTHER.search(text[match.end():])
                    or _ZH_NEGATED.search(text[max(0, match.start() - 3):match.start()])):
                return True
    for match in _ZH_REPEAT_BACK.finditer(text):
        if not (_ZH_GAP_OTHER.search(text[match.end():match.end() + 12])
                or _ZH_NEGATED.search(text[max(0, match.start() - 3):match.start()])):
            return True
    return False


# ── Rendering ────────────────────────────────────────────────────────────────────────────────
_ROOM_EN = {"whole": "whole property", "studio": "studio", "ensuite": "room with en-suite bathroom",
            "shared": "room in a shared home"}
_ROOM_ZH = {"whole": "整套", "studio": "独立单间公寓（studio）", "ensuite": "带独立卫浴的房间",
            "shared": "合租房间"}
_FEATURE_EN = {"cat_written_consent": "written permission for the cat",
               "wheelchair_space_68cm": "door and bathroom clearance for a 68 cm wheelchair"}
_FEATURE_ZH = {"cats allowed": "允许养猫", "pets allowed": "允许养宠物", "pet-friendly": "允许宠物",
               "cat_written_consent": "猫的书面饲养许可", "bills included": "包含账单（bills）",
               "furnished": "带家具", "unfurnished": "不带家具", "step-free": "无台阶通行",
               "lift": "电梯", "wheelchair-accessible": "轮椅通行条件",
               "accessible bathroom": "无障碍浴室",
               "wheelchair_space_68cm": "68cm轮椅通过房门及浴室的通行空间",
               "balcony": "阳台", "garden": "花园", "parking": "停车位"}
_TENANT_EN = {"student": "a student", "postgraduate": "a postgraduate student",
              "healthcare": "a healthcare / NHS worker", "other": "not a student"}
_TENANT_ZH = {"student": "学生", "postgraduate": "研究生", "healthcare": "医护人员（NHS）",
              "other": "非学生"}
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")
# A weekly figure in the words that set the budget: "£300 a week", "每周£300", "300pw".
_WEEKLY = re.compile(
    r"£\s?(\d[\d,]*(?:\.\d+)?)\s*(?:a|per|/|each)\s*week|£\s?(\d[\d,]*(?:\.\d+)?)\s*(?:pw|p/w)\b|"
    r"(?:每周|一周|每星期)[^0-9£。，,;；]{0,6}£?\s?(\d[\d,]*(?:\.\d+)?)|"
    r"£\s?(\d[\d,]*(?:\.\d+)?)\s*(?:英镑|镑)?\s*(?:每周|一周|/周)", re.I)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _money(value) -> str:
    return f"£{value:,.0f}" if float(value).is_integer() else f"£{value:,.2f}"


def _weekly_figure(criteria: dict, monthly: float):
    """The weekly amount the user gave, when the stored monthly ceiling was converted from it."""
    from core.tenancy_reference import monthly_from_weekly
    source = ((criteria.get("constraint_sources") or {}).get("max_budget") or {})
    text = str(source.get("text") or "") if isinstance(source, dict) else ""
    for match in _WEEKLY.finditer(text[:4000]):
        weekly = _number(next(g for g in match.groups() if g).replace(",", ""))
        if weekly and abs(monthly_from_weekly(weekly) - monthly) < 1:
            return weekly
    return None


def _budget_line(criteria: dict, zh: bool) -> str:
    from core.tenancy_reference import monthly_from_weekly
    amount = _number(criteria.get("max_budget"))
    period = str(criteria.get("budget_period") or "").lower()
    if amount is None or amount <= 0:
        text = "尚未说明" if zh else "not given yet"
    elif period in ("week", "weekly", "pw"):
        monthly = monthly_from_weekly(amount)
        text = (f"每周不超过 {_money(amount)}（约每月 {_money(round(monthly))}）" if zh else
                f"up to {_money(amount)} a week (about {_money(round(monthly))} a month)")
    else:
        weekly = _weekly_figure(criteria, amount)
        text = f"每月不超过 {_money(amount)}" if zh else f"up to {_money(amount)} a month"
        if weekly:
            text += f"（你给出的是每周 {_money(weekly)}）" if zh else f" (you gave it as {_money(weekly)} a week)"
    total = _number(criteria.get("monthly_total_budget"))
    if total:
        text += (f"；房租加交通每月合计不超过 {_money(total)}" if zh else
                 f"; rent plus travel at most {_money(total)} a month in total")
    return ("预算：" if zh else "Budget: ") + text


def _commute_line(criteria: dict, zh: bool) -> str:
    destination = str(criteria.get("commute_destination") or criteria.get("destination") or "").strip()
    minutes = _number(criteria.get("max_travel_time") or criteria.get("max_commute_time"))
    if criteria.get("no_commute") is True:
        text = "不需要通勤" if zh else "none (no commute needed)"
    elif destination and minutes:
        text = (f"到 {destination}，不超过 {minutes:g} 分钟" if zh else
                f"to {destination}, at most {minutes:g} minutes")
    elif destination:
        text = f"到 {destination}（未设时长上限）" if zh else f"to {destination} (no time limit given)"
    elif minutes:
        text = (f"不超过 {minutes:g} 分钟（目的地尚未说明）" if zh else
                f"at most {minutes:g} minutes (destination not given yet)")
    else:
        text = "尚未说明" if zh else "not given yet"
    if criteria.get("no_commute") is not True:
        days = _number(criteria.get("office_days_per_week"))
        changes = _number(criteria.get("max_changes"))
        if days:
            text += f"，每周到岗 {days:g} 天" if zh else f", {days:g} office days a week"
        if changes is not None:
            text += f"，最多换乘 {changes:g} 次" if zh else f", at most {changes:g} changes"
    return ("通勤：" if zh else "Commute: ") + text


def _home_line(criteria: dict, zh: bool) -> str:
    kinds = [part for part in str(criteria.get("room_type") or "").split("+") if part.strip()]
    labels = (_ROOM_ZH if zh else _ROOM_EN)
    parts = [("或".join if zh else " or ".join)(labels.get(kind, kind) for kind in kinds)] if kinds else []
    bedrooms = _number(criteria.get("bedrooms"))
    if bedrooms is not None and bedrooms > 0:
        at_least = criteria.get("bedrooms_minimum") is True
        if zh:
            parts.append(f"{'至少 ' if at_least else ''}{bedrooms:g} 间卧室")
        else:
            parts.append(f"{'at least ' if at_least else ''}{bedrooms:g} bedroom{'' if bedrooms == 1 else 's'}")
    text = ("，" if zh else ", ").join(parts) if parts else ("尚未说明" if zh else "not given yet")
    return ("房型：" if zh else "Home: ") + text


def _move_in_line(criteria: dict, zh: bool) -> str:
    value = str(criteria.get("move_in_date") or "").strip()
    try:
        day = date.fromisoformat(value[:10])
    except ValueError:
        day = None
    if not value:
        text = "尚未说明" if zh else "not given yet"
    elif day is None:
        text = value
    else:
        text = f"{day.year}年{day.month}月{day.day}日" if zh else f"{day.day} {_MONTHS[day.month - 1]} {day.year}"
    return ("入住日期：" if zh else "Move-in date: ") + text


def _strings(values) -> list:
    if isinstance(values, str):
        values = [values]
    return [str(v).strip() for v in values or [] if isinstance(v, str) and v.strip()]


def render_criteria_recap(criteria: dict | None, language: str = "en") -> str:
    """The requirements this conversation holds, one line per slot, in the reply language.
    The main slots are always listed ("not given yet" when empty); the rest only when set."""
    criteria = criteria if isinstance(criteria, dict) else {}
    zh = str(language or "").lower().startswith("zh")
    areas = _strings(criteria.get("areas")) or _strings(criteria.get("area"))
    lines = [("区域：" if zh else "Areas: ")
             + (("、" if zh else ", ").join(areas) if areas else ("尚未说明" if zh else "not given yet")),
             _commute_line(criteria, zh), _budget_line(criteria, zh), _home_line(criteria, zh),
             _move_in_line(criteria, zh)]
    features = _strings(criteria.get("property_features"))
    if features:
        labels = _FEATURE_ZH if zh else _FEATURE_EN
        lines.append(("必须满足：" if zh else "Must have: ")
                     + ("、" if zh else ", ").join(labels.get(f, f) for f in dict.fromkeys(features)))
    preferences = _strings(criteria.get("soft_preferences"))
    if preferences:
        lines.append(("偏好（加分项，不作为筛选条件）：" if zh else
                      "Preferences (nice to have, not filters): ")
                     + ("；" if zh else "; ").join(dict.fromkeys(preferences)))
    tenant = str(criteria.get("tenant_status") or "").strip().lower()
    if tenant:
        lines.append(("身份：" + _TENANT_ZH.get(tenant, tenant)) if zh else
                     ("About you: " + _TENANT_EN.get(tenant, tenant)))
    return "\n".join([HEADING_ZH if zh else HEADING_EN, *("- " + line for line in lines)])


def with_criteria_recap(response: str, criteria: dict | None, *, message: str = "",
                        language: str = "en") -> str:
    """``response`` with the recap first when ``message`` asks for one, else unchanged."""
    if not asks_for_criteria_recap(message):
        return response
    body = str(response or "").strip()
    if HEADING_EN in body or HEADING_ZH in body:
        return response
    recap = render_criteria_recap(criteria, language)
    return f"{recap}\n\n{body}" if body else recap
