"""compare_or_rank_areas — rank residential areas by an explainable, cache-grounded
value/commute composite (design §2.5b, the missing capability behind the trigger
case "哪些区域综合性价比最高、通勤不长、价格适中").

The whole tool works OFFLINE from the OnTheMarket listing cache: candidate areas
come from the shared ``generate_candidate_areas`` core (with commute), rent stats
come from ``area_stats.aggregate`` over the cache, and the composite score is a
transparent weighted sum of 0-100 components whose weights (derived from the
caller's ``priorities``) ride along in the payload. Honesty is structural — an area
with no cached listings gets ``rent: null`` + a no-data marker, a thin sample is
flagged ``low_sample`` and its rent confidence is shrunk, and components with no
cheap data (safety/amenities) are excluded from the weights and explained rather
than faked. If the cache has nothing for the requested city, the tool returns
status ``no_data`` (suggesting a search first) instead of scraping in-line.
"""

from __future__ import annotations

from typing import List, Optional

from core.tool_system import Tool
from core import area_stats
from core.recommend_areas import generate_candidate_areas

# Fewer cached listings than this -> low_sample, and the value component is shrunk
# toward a neutral 50 so a 1-2 listing area cannot masquerade as a confident bargain.
_LOW_SAMPLE = 5

_PRIORITY_ENUM = ("value", "commute", "safety", "amenities")
# Components computable from cheap/offline data. safety/amenities have no cheap data
# source here, so they are EXCLUDED from the weights (and explained) — never faked.
_AVAILABLE_KINDS = ("value", "commute")


def _L(is_zh: bool, en: str, zh: str) -> str:
    return zh if is_zh else en


def _weights(priorities: list, usable: set) -> dict:
    """Rank-based weights (leading priority heaviest, summing to 1) over the
    priorities that are actually usable. Priority order is preserved; any usable
    kind the caller did not name is appended after (so ``value`` still counts even
    if the user only said 'commute'), with the lowest weight."""
    ordered: list = []
    seen: set = set()
    for p in priorities:
        if p in usable and p not in seen:
            ordered.append(p)
            seen.add(p)
    for p in _AVAILABLE_KINDS:
        if p in usable and p not in seen:
            ordered.append(p)
            seen.add(p)
    k = len(ordered)
    if k == 0:
        return {}
    raw = {p: (k - i) for i, p in enumerate(ordered)}  # linear decreasing
    tot = float(sum(raw.values()))
    rounded = {p: round(raw[p] / tot, 4) for p in ordered}
    # Fix rounding drift on the last (smallest) weight so the set sums to exactly 1.
    drift = round(1.0 - sum(rounded.values()), 4)
    rounded[ordered[-1]] = round(rounded[ordered[-1]] + drift, 4)
    return rounded


def _value_components(stats_by_slug: dict, budget_present: bool) -> dict:
    """Value component (0-100) per slug: inverse median rent normalised WITHIN the
    candidate set (cheaper -> higher), adjusted by budget_match_rate when a budget is
    given, then shrunk toward a neutral 50 for low-sample areas (rent confidence)."""
    medians = {
        s: st["median"] for s, st in stats_by_slug.items()
        if st["sample_size"] > 0 and st["median"] is not None
    }
    if not medians:
        return {}
    lo, hi = min(medians.values()), max(medians.values())
    comps: dict = {}
    for s, m in medians.items():
        base = 100.0 if hi == lo else (hi - m) / (hi - lo) * 100.0
        st = stats_by_slug[s]
        if budget_present and st.get("budget_match_rate") is not None:
            base = 0.6 * base + 0.4 * (st["budget_match_rate"] * 100.0)
        conf = min(1.0, st["sample_size"] / float(_LOW_SAMPLE))
        comps[s] = round(base * conf + 50.0 * (1.0 - conf), 1)
    return comps


def _commute_components(commute_by_slug: dict, max_commute: Optional[int]) -> dict:
    """Commute component (0-100) per slug: inverse commute minutes vs
    ``max_commute`` when given (0 at the cap, 100 at zero), else normalised WITHIN
    the candidate set (shortest -> highest)."""
    vals = {s: c for s, c in commute_by_slug.items() if c is not None}
    if not vals:
        return {}
    comps: dict = {}
    if max_commute and max_commute > 0:
        for s, c in vals.items():
            comps[s] = round(max(0.0, min(1.0, 1.0 - c / float(max_commute))) * 100.0, 1)
    else:
        lo, hi = min(vals.values()), max(vals.values())
        for s, c in vals.items():
            comps[s] = round(100.0 if hi == lo else (hi - c) / (hi - lo) * 100.0, 1)
    return comps


async def _resolve_candidates(city, destination, max_commute, candidate_areas) -> list:
    """[{name, slug, commute_minutes}] via the shared candidate-generation core.

    Seeds on the destination (commute mode) or, absent one, the city (no-commute
    mode). A caller-supplied ``candidate_areas`` list is validated verbatim. Degrades
    to [] on any failure (the tool then reports no_data)."""
    seed = destination or city
    if not seed and candidate_areas:
        # No city/destination but explicit areas: rank them with no commute axis.
        seed = str(candidate_areas[0])
    if not seed:
        return []
    names = None
    if candidate_areas:
        names = [str(a) for a in candidate_areas if str(a or "").strip()]
    try:
        raw = await generate_candidate_areas(
            seed,
            city=city,
            max_commute_time=max_commute,
            candidate_names=names,
            no_commute_mode=(destination is None),
        )
    except Exception as exc:  # never let candidate generation 500 the tool
        print(f"  [compare_or_rank_areas] candidate generation failed: {exc}")
        raw = []
    out: list = []
    seen: set = set()
    for it in raw or []:
        slug = str(it.get("slug") or "").strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append({
            "name": it.get("name") or slug,
            "slug": slug,
            "commute_minutes": it.get("commute_minutes"),
        })
    return out


async def compare_or_rank_areas_impl(
    city: str = None,
    destination: str = None,
    max_commute_minutes: int = None,
    budget_hint: dict = None,
    priorities: List[str] = None,
    candidate_areas: List[str] = None,
    limit: int = 8,
    reply_language: str = None,
    **kwargs,
) -> dict:
    """Rank areas by an explainable value/commute composite from the listing cache.

    Requires ``city`` OR ``destination`` (or an explicit ``candidate_areas`` list).
    Returns ``{success, status, areas, explanation_notes, ...}`` — see the module
    docstring and TOOL contract. Never scrapes; honest ``no_data`` over invention."""
    is_zh = (reply_language == "zh")

    # ---- Normalise inputs ------------------------------------------------
    city = city.strip() if isinstance(city, str) and city.strip() else None
    destination = destination.strip() if isinstance(destination, str) and destination.strip() else None
    priorities = [p for p in (priorities or []) if p in _PRIORITY_ENUM] or ["value", "commute"]
    try:
        max_commute_minutes = int(max_commute_minutes) if max_commute_minutes else None
    except (TypeError, ValueError):
        max_commute_minutes = None
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 8
    budget = None
    if isinstance(budget_hint, dict):
        amt, per = budget_hint.get("amount"), budget_hint.get("period", "month")
        try:
            if amt is not None and float(amt) > 0:
                period = "week" if str(per or "month").strip().lower().startswith("w") else "month"
                budget = (float(amt), period)
        except (TypeError, ValueError):
            budget = None

    # ---- Input validation: city OR destination (clarification-style error) ----
    if not city and not destination and not candidate_areas:
        return {
            "success": False,
            "status": "need_input",
            "clarification_kind": "other",
            "question": _L(
                is_zh,
                "Which city or commute destination should I compare areas for? "
                "(e.g. a city like Manchester, or a university/workplace like UCL)",
                "你想比较哪个城市或通勤目的地周边的区域？"
                "（例如城市 Manchester，或大学/公司如 UCL）",
            ),
            "missing_fields": ["city_or_destination"],
        }

    notes: list = []

    # ---- 1) Candidate areas (with commute) -------------------------------
    candidates = await _resolve_candidates(city, destination, max_commute_minutes, candidate_areas)
    if not candidates:
        return {
            "success": True,
            "status": "no_data",
            "areas": [],
            "explanation_notes": [_L(
                is_zh,
                "I couldn't assemble candidate areas to compare. Try naming a city, a "
                "commute destination, or specific areas.",
                "没有可比较的候选区域。请提供城市、通勤目的地，或直接指定几个区域。",
            )],
        }

    slugs = [c["slug"] for c in candidates]
    commute_by_slug = {c["slug"]: c["commute_minutes"] for c in candidates}

    # ---- 2) Rent aggregation (offline, from the listing cache) -----------
    stats_by_slug = area_stats.aggregate(slugs, budget)

    value_available = any(st["sample_size"] > 0 for st in stats_by_slug.values())
    commute_available = (destination is not None) and any(c is not None for c in commute_by_slug.values())

    # ---- 3) Weights (from priorities, only over usable components) -------
    usable: set = set()
    if value_available:
        usable.add("value")
    if commute_available:
        usable.add("commute")
    weights = _weights(priorities, usable)

    if any(p in ("safety", "amenities") for p in priorities):
        notes.append(_L(
            is_zh,
            "Safety and amenities were requested but have no cheap offline data here, "
            "so they are excluded from the score (not estimated).",
            "你提到了治安/配套，但此处没有可用的低成本离线数据，已将其排除在评分之外（不做臆测）。",
        ))

    # ---- 4) Component scores ---------------------------------------------
    value_comps = _value_components(stats_by_slug, budget is not None) if value_available else {}
    commute_comps = _commute_components(commute_by_slug, max_commute_minutes) if commute_available else {}

    # ---- 5) Assemble per-area payload ------------------------------------
    areas: list = []
    low_sample_names: list = []
    no_data_names: list = []
    for c in candidates:
        slug = c["slug"]
        st = stats_by_slug.get(slug, area_stats._empty())
        sample = st["sample_size"]
        rent = None
        if sample > 0:
            rent = {
                "min": st["min"], "max": st["max"], "median": st["median"],
                "sample_size": sample,
                "freshness_days": st["freshness_days"],
                "low_sample": sample < _LOW_SAMPLE,
            }
            if sample < _LOW_SAMPLE:
                low_sample_names.append(c["name"])
        else:
            no_data_names.append(c["name"])

        components: dict = {}
        if slug in value_comps:
            components["value"] = value_comps[slug]
        if slug in commute_comps:
            components["commute"] = commute_comps[slug]

        # Per-area total: weighted sum over the components PRESENT for this area,
        # renormalised by their weights so a no-data area can still rank on commute.
        total = None
        present = {k: v for k, v in components.items() if k in weights}
        wsum = sum(weights[k] for k in present)
        if present and wsum > 0:
            total = round(sum(weights[k] * present[k] for k in present) / wsum, 1)

        sources: list = []
        if rent is not None:
            sources.append(_L(is_zh, "OnTheMarket listing cache", "OnTheMarket 房源缓存"))
        if c["commute_minutes"] is not None:
            sources.append(_L(is_zh, "commute routing", "通勤路由估算"))

        areas.append({
            "name": c["name"],
            "slug": slug,
            "rent": rent,
            "commute_minutes": c["commute_minutes"],
            "budget_match_rate": st["budget_match_rate"],
            "score": {"total": total, "components": components, "weights": weights},
            "sources": sources,
            "no_data": rent is None,
        })

    # ---- 6) Sort by total desc (None last), cap at limit -----------------
    areas.sort(key=lambda a: (a["score"]["total"] is None, -(a["score"]["total"] or 0.0)))
    areas = areas[:limit]

    # ---- 7) Explanation notes (formula + weights + caveats) --------------
    notes.insert(0, _L(
        is_zh,
        "Composite score = weighted sum of 0-100 components (value = lower median rent, "
        "adjusted by how many cached listings fall under budget; commute = shorter time). "
        "Weights come from your priorities and sum to 1: " + _fmt_weights(weights, is_zh) + ".",
        "综合分 = 各 0-100 分项的加权和（value=更低的中位租金，并按缓存中低于预算的房源占比调整；"
        "commute=更短的通勤）。权重来自你的优先级、总和为 1：" + _fmt_weights(weights, is_zh) + "。",
    ))
    if not commute_available:
        notes.append(_L(
            is_zh,
            "No commute destination was given, so commute is excluded and commute_minutes is null.",
            "未提供通勤目的地，已排除通勤维度，commute_minutes 为空。",
        ))
    if low_sample_names:
        notes.append(_L(
            is_zh,
            "Thin rent sample (<%d cached listings), rent confidence reduced: %s."
            % (_LOW_SAMPLE, ", ".join(low_sample_names)),
            "以下区域缓存样本较少（<%d 套），已降低租金置信度：%s。"
            % (_LOW_SAMPLE, "、".join(low_sample_names)),
        ))
    if no_data_names:
        notes.append(_L(
            is_zh,
            "No cached listings for: %s — rent shown as null (search these areas first)."
            % ", ".join(no_data_names),
            "以下区域无缓存房源：%s —— 租金显示为空（可先搜索这些区域）。"
            % "、".join(no_data_names),
        ))

    status = "ok" if value_available else "no_data"
    if not value_available:
        notes.append(_L(
            is_zh,
            "No cached rent data for any candidate area; run a property search first, "
            "then compare.",
            "所有候选区域都没有缓存的租金数据；请先做一次房源搜索，再进行比较。",
        ))

    return {
        "success": True,
        "status": status,
        "areas": areas,
        "explanation_notes": notes,
        "priorities": priorities,
        "weights": weights,
    }


def _fmt_weights(weights: dict, is_zh: bool) -> str:
    if not weights:
        return _L(is_zh, "none", "无")
    return ", ".join(f"{k}={v}" for k, v in weights.items())


compare_or_rank_areas_tool = Tool(
    name="compare_or_rank_areas",
    description=(
        "Rank/compare residential AREAS by an explainable value-for-money + commute "
        "composite when the user asks which areas are best overall / best value / cheapest "
        "with a reasonable commute (not for listing a single area's flats — that's "
        "search_properties). Rent stats come from the offline listing cache; give city OR "
        "destination. 按性价比+通勤综合排序/比较居住区域（"
        "如「哪个区域综合性价比最高、通勤不长、价格适中」）；需 city 或 destination 之一。"
    ),
    func=compare_or_rank_areas_impl,
    parameters={
        'type': 'object',
        'properties': {
            'city': {
                'type': 'string',
                'description': 'City to compare areas within (e.g. Manchester, London). '
                               'Provide this or destination.',
            },
            'destination': {
                'type': 'string',
                'description': 'A commute destination (university/workplace, e.g. UCL) whose '
                               'nearby areas to rank; enables the commute dimension.',
            },
            'max_commute_minutes': {
                'type': 'integer',
                'description': 'Optional commute-time cap (minutes) to the destination.',
            },
            'budget_hint': {
                'type': 'object',
                'properties': {
                    'amount': {'type': 'number', 'description': 'budget amount in GBP'},
                    'period': {'type': 'string', 'enum': ['week', 'month'],
                               'description': "budget period"},
                },
                'description': 'Optional budget used for the budget-match rate and value scoring.',
            },
            'priorities': {
                'type': 'array',
                'items': {'type': 'string', 'enum': ['value', 'commute', 'safety', 'amenities']},
                'default': ['value', 'commute'],
                'description': 'Ranking priorities, leading one weighted heaviest. safety/amenities '
                               'are excluded when no cheap data is available.',
            },
            'candidate_areas': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Optional explicit areas to compare; when absent, candidates are '
                               'generated from the destination/city.',
            },
            'limit': {
                'type': 'integer',
                'default': 8,
                'description': 'Maximum areas to return (default 8).',
            },
            'reply_language': {
                'type': 'string',
                'enum': ['zh', 'en'],
                'description': "Language for user-facing strings ('zh' or 'en').",
            },
        },
        'required': [],
    },
    side_effect="none",
    max_retries=1,
)
