"""
Tool: Get Property Details
获取数据库中特定房产的详细信息

当用户询问特定房产的详情（如房型、设施、价格等）时，
应该直接查询本地数据库，而不是进行网络搜索。

使用场景：
1. 用户点击前端 "Ask AI" 按钮询问某个房产
2. 用户提到特定房产名称/地址并询问详情
3. 用户对之前推荐的房产提出具体问题

ENTITY IDENTITY IS PART OF THE ANSWER. See the "entity-resolution guard" block
below: this tool must never return listing B's price when the user asked about
listing A. When it cannot confirm the identity it returns a not-found / ambiguous
result and NO price at all.
"""

import pandas as pd
from typing import Optional, Dict, List, Tuple
from core.tool_system import Tool, ToolResult
import re


def load_property_database() -> pd.DataFrame:
    """加载房产数据库。

    直接读取 on-demand 抓取缓存（listing_cache.sqlite3）—— 与列表/搜索路径
    （core.scraping.on_demand.get_listings）完全相同的数据源，因此用户询问
    "介绍一下某套房" 时看到的是同一批真实房源，而不是离线批处理 CSV / 假数据。
    缓存为空或不可用时返回空 DataFrame（诚实地表示"暂无可查数据"）。"""
    try:
        from core.scraping.on_demand import iter_cached_listings
        rows = iter_cached_listings()
    except Exception as e:
        print(f"❌ 加载房产数据失败: {e}")
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def normalize_text(text: str) -> str:
    """标准化文本用于匹配"""
    if not text:
        return ""
    # 转小写，移除多余空格和特殊字符
    text = text.lower().strip()
    text = re.sub(r'[,\.\-\'\"]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text


# ═══════════════════════════════════════════════════════════════════════════════
# Entity-resolution guard
#
# HANDOFF §0 defect class: *a value is computed, stored where a reader could find
# it, and then never asserted on.* This tool used to DECIDE which cached listing
# the user meant (`matches[0]`) and then throw the decision away — the returned
# dict said "found: True" and carried a price, with nothing anywhere comparing the
# requested entity to the returned one. Measured, round-8793c0b-internal-2026-07-25:
#
#     requested                                returned
#     "Spring Mews SE11 5AL"               ->  "Raleigh Mews, Angel, N1"
#     "Vega Building E15 2GN"              ->  "Plimsoll Building, N1C"
#     "Chapter Kings Cross, 30 Pentonville ->  "Pentonville Road", £1,300 pcm,
#      Road, London N1 9HJ" @ £400/week         1 bed Flat
#
# Case F14 then answered, verbatim: "The official monthly price for this property
# is **£1,300 pcm** ... located on Pentonville Road" — a DIFFERENT listing's rent
# (the true figure for the requested listing was £400/week = £1,733.33 pcm).
# Case C9 spent both its tool calls noticing the substitution and never computed
# the commutes it was asked for.
#
# Two independent causes, both removed:
#   (a) the old score threshold accepted ANY two overlapping query tokens, so
#       purely generic words ("mews", "building", "road", "london") were enough to
#       declare a match;
#   (b) a "looser" single-keyword retry compared by bare substring, so the token
#       "mews" matched every mews in the cache — that retry produced Raleigh Mews
#       and Plimsoll Building.
#
# This is a SOURCE guard, not a flag. A row that fails it is never emitted as the
# answer, because nothing in this repo reads a returned confidence field:
#   grep -rn 'formatted_details|room_type_analysis|total_matches|other_matches'
# finds ZERO consumers outside this file, and the `found` key is likewise never
# read (`langgraph_agent.py:2348` only BUILDS this tool's params). The whole result
# dict is serialised straight into the model's context, so a score nobody asserts
# on would be this same defect a second time. Refusal at the tool boundary is the
# only place the decision can actually bind.
#
# The guard deliberately errs toward refusal: a false refusal costs one extra turn
# ("I couldn't find that listing — send me its URL"), a false match costs a user
# acting on another property's rent.
# ═══════════════════════════════════════════════════════════════════════════════

# A full UK postcode ("N1 9HJ") and its two halves. Outward = "N1"/"SE11"/"WC1H".
_POSTCODE_UNIT_RE = re.compile(r'\b([a-z]{1,2}[0-9][a-z0-9]?)\s+([0-9][a-z]{2})\b')
_OUTWARD_RE = re.compile(r'^[a-z]{1,2}[0-9][a-z0-9]?$')
_INWARD_RE = re.compile(r'^[0-9][a-z]{2}$')
_URL_TOKEN_RE = re.compile(r'https?://|www\.|onthemarket\.com|\.co\.uk/|\.com/', re.I)

# Tokens that carry no identity because nearly every London listing has them.
# "mews" and "building" are exactly the tokens that produced the Raleigh Mews and
# Plimsoll Building substitutions, so they must be classed generic. Street-type
# words are also what makes "St" vs "Street" / "Rd" vs "Road" a non-issue: both
# spellings land in this set on BOTH sides and drop out of the comparison, so no
# abbreviation table is needed and none can be got wrong.
_TYPE_TOKENS = frozenset({
    # street-type suffixes
    'street', 'streets', 'st', 'road', 'rd', 'avenue', 'ave', 'av', 'lane', 'ln',
    'place', 'pl', 'square', 'sq', 'court', 'crt', 'ct', 'crescent', 'cres',
    'drive', 'drv', 'dr', 'way', 'walk', 'close', 'terrace', 'terr', 'ter',
    'row', 'mews', 'parade', 'gardens', 'garden', 'gdns', 'gdn', 'mansions',
    # building / unit / tenure words
    'building', 'buildings', 'bldg', 'house', 'apartment', 'apartments', 'apt',
    'apts', 'flat', 'flats', 'block', 'residence', 'residences', 'studio',
    'studios', 'room', 'rooms', 'bedroom', 'bedrooms', 'bed', 'beds', 'unit',
    'units', 'suite', 'hall', 'halls', 'lodge', 'tower', 'towers', 'floor',
    'ground', 'basement', 'annex', 'annexe',
    # geography / boilerplate that appears on almost every row
    'london', 'uk', 'gbr', 'england', 'united', 'kingdom', 'greater', 'city',
    'borough', 'centre', 'center',
})

# Conversational filler the model sometimes leaks into property_name /
# property_address instead of a bare address. These are skipped rather than
# treated as a name, so "tell me about the Spring Mews" still resolves to
# Spring Mews. Deliberately does NOT contain address words ('place', 'court',
# 'park', 'green', 'hill', 'cross'...) — those can be part of a real name.
_FILLER_TOKENS = frozenset({
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'these', 'those',
    'tell', 'about', 'what', 'whats', 'which', 'where', 'who', 'why', 'how',
    'please', 'give', 'show', 'find', 'look', 'lookup', 'know', 'more', 'much',
    'many', 'any', 'all', 'also', 'get', 'got', 'was', 'were', 'been', 'have',
    'has', 'had', 'does', 'did', 'are', 'you', 'your', 'our', 'their', 'its',
    'listing', 'listings', 'property', 'properties', 'details', 'detail',
    'info', 'information', 'price', 'prices', 'pricing', 'rent', 'rents',
    'cost', 'costs', 'monthly', 'weekly', 'official', 'exact', 'figure',
    'pcm', 'pppw', 'week', 'month', 'per',
})

# A request's primary name must be at least half covered by the candidate.
_NAME_HIT_FRACTION = 0.5
# ...and so must the request's full distinctive vocabulary, unless the postcode
# independently corroborates the row.
_BREADTH_FRACTION = 0.5


def _tokens(text: str) -> List[str]:
    """Lowercase alphanumeric tokens. '19-29' -> ['19', '29']."""
    return [t for t in re.split(r'[^a-z0-9]+', normalize_text(text)) if t]


def strip_urls(text: str) -> str:
    """Drop URL-ish whitespace-delimited tokens.

    A URL's slug ("/details/12345/") is an exact identifier, handled by the
    URL branch. Feeding it to the fuzzy matcher instead injects tokens like
    'onthemarket', 'details' and 'www' into the request's vocabulary, which is
    noise at best and a spurious match at worst."""
    if not text:
        return ""
    kept = [t for t in text.split() if not _URL_TOKEN_RE.search(t)]
    return " ".join(kept)


def _is_placeholder(token: str) -> bool:
    """True when a token cannot identify a listing on its own."""
    if len(token) < 3 or token.isdigit():
        return True
    if token in _TYPE_TOKENS or token in _FILLER_TOKENS:
        return True
    return bool(_OUTWARD_RE.match(token) or _INWARD_RE.match(token))


def _distinctive_tokens(tokens: List[str]) -> List[str]:
    """Every token that could identify a listing (name words only)."""
    return [t for t in tokens if not _is_placeholder(t)]


def _primary_name_tokens(tokens: List[str]) -> List[str]:
    """The leading run of name words — the thing being named.

    "Spring Mews SE11 5AL"                      -> ['spring']
    "Vega Building E15 2GN"                     -> ['vega']
    "Chapter Kings Cross, 30 Pentonville Road"  -> ['chapter', 'kings', 'cross']
    "19-29 Woburn Place, Bloomsbury"            -> ['woburn']
    "tell me about the Spring Mews"             -> ['spring']

    Leading placeholders (house numbers, "flat", filler) are skipped; the first
    placeholder AFTER the name has started ends it, because in a UK address the
    identity precedes the street type / number / postcode."""
    head: List[str] = []
    for token in tokens:
        if _is_placeholder(token):
            if head:
                break
            continue
        head.append(token)
    return head


# A number introduced by one of these is a FLAT/unit number, not a house number.
# "Flat 4, Spring Mews" and "Spring Mews, 10 Tinworth Street" are the same
# building: comparing 4 against 10 would refuse a legitimate request, which is
# the opposite failure this guard must not cause.
_UNIT_NUMBER_PREFIXES = frozenset({
    'flat', 'flats', 'apt', 'apts', 'apartment', 'unit', 'room', 'suite',
    'no', 'number', 'floor',
})


def _house_numbers(tokens: List[str]) -> set:
    """Standalone numeric tokens that denote a HOUSE number or building range.

    Flat/unit numbers are excluded (see `_UNIT_NUMBER_PREFIXES`). Postcode halves
    never land here either: 'se11' and '9hj' are not all-digits."""
    out = set()
    previous = ""
    for token in tokens:
        if token.isdigit() and 1 <= len(token) <= 4:
            if previous not in _UNIT_NUMBER_PREFIXES:
                out.add(token)
        previous = token
    return out


def parse_property_reference(text: str) -> Dict:
    """Structured identity of a property reference (a request or a cache row)."""
    cleaned = strip_urls(text or "")
    tokens = _tokens(cleaned)
    joined = " ".join(tokens)
    unit = outward = None
    m = _POSTCODE_UNIT_RE.search(joined)
    if m:
        unit, outward = m.group(1) + m.group(2), m.group(1)
    else:
        for t in tokens:
            if _OUTWARD_RE.match(t):
                outward = t
                break
    return {
        "raw": (text or "").strip(),
        "tokens": tokens,
        "name_tokens": _primary_name_tokens(tokens),
        "distinctive_tokens": _distinctive_tokens(tokens),
        "postcode_unit": unit,
        "postcode_outward": outward,
        "house_numbers": _house_numbers(tokens),
    }


def _needed(n: int, fraction: float) -> int:
    """At least one token, and at least `fraction` of them (rounded up)."""
    return max(1, int(-(-n * fraction // 1)))


def compare_property_reference(requested: Dict, candidate_address: str) -> Dict:
    """Is `candidate_address` the property described by `requested`?

    Returns {'match': bool, 'score': int, 'reasons': [str], 'corroborated_by': [str]}.
    `reasons` is non-empty exactly when the row is NOT the requested property, and
    each entry names the requested value and the candidate value so a caller (or a
    reader of the tool result) can see WHY it was refused.

    Five vetoes. Each of the three production substitutions above trips V-NAME on
    its own; two of the three additionally trip V-POSTCODE.
    """
    cand = parse_property_reference(candidate_address)
    cand_names = set(cand["distinctive_tokens"])
    req_names = requested["name_tokens"]
    reasons: List[str] = []
    corroborated: List[str] = []
    score = 0

    # V-POSTCODE — compare at the finest granularity present on BOTH sides.
    # A postcode is the strongest cheap identity signal in a UK address and the
    # one that separates "30 Pentonville Road N1 9HJ" from "Pentonville Road
    # N1 9JP". Absent on either side => no information, never a veto.
    postcode_ok = False
    if requested["postcode_unit"] and cand["postcode_unit"]:
        if requested["postcode_unit"] != cand["postcode_unit"]:
            reasons.append(
                f"postcode_conflict: requested postcode "
                f"{requested['postcode_unit'].upper()}, cache row is "
                f"{cand['postcode_unit'].upper()}")
        else:
            score += 4
            postcode_ok = True
            corroborated.append("postcode_unit")
    elif requested["postcode_outward"] and cand["postcode_outward"]:
        if requested["postcode_outward"] != cand["postcode_outward"]:
            reasons.append(
                f"outward_postcode_conflict: requested district "
                f"{requested['postcode_outward'].upper()}, cache row is "
                f"{cand['postcode_outward'].upper()}")
        else:
            score += 2
            postcode_ok = True
            corroborated.append("postcode_district")

    # V-VAGUE — nothing to resolve on. Refusing beats picking a row at random.
    if not req_names and not (requested["postcode_unit"] or requested["postcode_outward"]):
        reasons.append(
            "request_not_resolvable: no building/street name and no postcode in "
            f"'{requested['raw']}'")

    # V-NAME — the request's primary name must actually appear on the row.
    if req_names:
        hits = [t for t in req_names if t in cand_names]
        score += 2 * len(hits)
        if len(hits) >= _needed(len(req_names), _NAME_HIT_FRACTION):
            corroborated.append("primary_name")
        else:
            reasons.append(
                f"name_mismatch: requested '{' '.join(req_names)}', cache row is "
                f"'{cand['raw']}'")

    # V-BREADTH — a request that names TWO things (development + street, as F14
    # did) is only satisfied by a row that accounts for most of them. Skipped
    # when the postcode already pins the row, since extra area words the row
    # omits ("Spring Mews, Vauxhall" vs "Spring Mews, 10 Tinworth Street") are
    # legitimate variation, not a different property.
    req_all = requested["distinctive_tokens"]
    if req_all and not postcode_ok:
        covered = [t for t in req_all if t in cand_names]
        score += len(covered)
        if len(covered) < _needed(len(req_all), _BREADTH_FRACTION):
            reasons.append(
                f"partial_match_only: {len(covered)}/{len(req_all)} of the "
                f"requested name words appear in '{cand['raw']}'")
    elif req_all:
        score += len([t for t in req_all if t in cand_names])

    # V-NUMBER — "30 Pentonville Road" is not "88 Pentonville Road". Deliberately
    # conservative: only a single-number-vs-single-number disagreement vetoes, so
    # a MISSING flat number ("Flat 5, Woburn Place" vs "19-29 Woburn Place") and a
    # building range are never read as a conflict.
    req_nums, cand_nums = requested["house_numbers"], cand["house_numbers"]
    if len(req_nums) == 1 and len(cand_nums) == 1 and req_nums != cand_nums:
        reasons.append(
            f"house_number_conflict: requested number {next(iter(req_nums))}, "
            f"cache row is number {next(iter(cand_nums))}")
    elif req_nums and cand_nums and (req_nums & cand_nums):
        score += 1
        corroborated.append("house_number")

    # Corroboration bonus (NOT a veto): the row's own name was asked for. Asking
    # by street for a named development ("10 Tinworth St" -> "Spring Mews, 10
    # Tinworth Street") is legitimate, so a row whose name the user never typed
    # must still be allowed — it just ranks below one whose name matches.
    if cand["name_tokens"] and any(
            t in set(req_all) for t in cand["name_tokens"]):
        score += 1
        corroborated.append("row_name_requested")

    return {
        "match": not reasons,
        "score": score,
        "reasons": reasons,
        "corroborated_by": corroborated,
        "candidate": cand,
    }


def resolve_property_reference(
    query: str,
    df: pd.DataFrame,
) -> Tuple[Dict, List[Dict], List[Dict]]:
    """Resolve `query` against the cache.

    Returns (requested, confirmed, rejected). `confirmed` holds only rows whose
    identity was verified against the request — a row that fails the guard is
    NEVER promoted into it, so a caller cannot accidentally read a wrong
    property's price. `rejected` holds near-misses for a "did you mean" prompt,
    address + URL only and never a price."""
    requested = parse_property_reference(query)
    confirmed: List[Dict] = []
    rejected: List[Dict] = []
    if df.empty:
        return requested, confirmed, rejected

    for _, row in df.iterrows():
        data = row.to_dict()
        address = str(data.get('Address', '') or '')
        verdict = compare_property_reference(requested, address)
        if verdict["match"]:
            confirmed.append({
                "score": verdict["score"],
                "corroborated_by": verdict["corroborated_by"],
                "data": data,
            })
        elif verdict["score"] > 0:
            rejected.append({
                "score": verdict["score"],
                "address": address,
                "url": str(data.get('URL', '') or ''),
                "why_rejected": "; ".join(verdict["reasons"]),
            })

    confirmed.sort(key=lambda m: m["score"], reverse=True)
    rejected.sort(key=lambda m: m["score"], reverse=True)
    return requested, confirmed, rejected


def find_property_by_name_or_address(query: str, df: pd.DataFrame) -> List[Dict]:
    """根据名称或地址查找房产（只返回身份已核对通过的房源）。

    GUARDED. Kept so that any caller of the old name inherits the guard instead
    of the old fuzzy behaviour: rows that are not the requested property are not
    in the returned list at all."""
    _requested, confirmed, _rejected = resolve_property_reference(query, df)
    return [m["data"] for m in confirmed]


def _dedupe_listings(confirmed: List[Dict]) -> List[Dict]:
    """Collapse cache rows that describe the SAME listing (same URL)."""
    seen = set()
    out = []
    for m in confirmed:
        data = m["data"]
        key = (str(data.get('URL', '') or '').strip().rstrip('/')
               or normalize_text(str(data.get('Address', '') or '')))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def format_property_details(property_data: Dict, match_note: Optional[str] = None) -> str:
    """
    格式化房产详细信息

    Args:
        property_data: 房产数据字典
        match_note: 身份核对结论（requested vs resolved），渲染在最前面

    Returns:
        格式化的房产详情字符串
    """
    address = property_data.get('Address', 'Unknown')
    price = property_data.get('Price', 'Unknown')
    room_type = property_data.get('Room_Type_Category', 'Unknown')
    description = property_data.get('Description', '')
    amenities = property_data.get('Detailed_Amenities', '')
    guest_policy = property_data.get('Guest_Policy', '')
    payment_rules = property_data.get('Payment_Rules', '')
    excluded_features = property_data.get('Excluded_Features', '')
    url = property_data.get('URL', '')
    available_from = property_data.get('Available From', 'Now')

    # 身份核对结论写在最前面：读到这段文字的人（或模型）能立刻看出
    # 返回的是不是他问的那一套房，而不是只能看到一个价格。
    header = f"{match_note}\n\n" if match_note else ""

    # 构建详细信息
    details = f"""{header}📍 **{address}**

💰 **价格**: {price}
🏠 **房型**: {room_type}
📅 **可入住日期**: {available_from}

📝 **描述**:
{description}

✨ **设施与配置**:
{amenities}

👥 **访客政策**:
{guest_policy}

💳 **付款规则**:
{payment_rules}

⛔ **不包含的设施**:
{excluded_features}

🔗 **链接**: {url}
"""
    return details.strip()


def _refusal(verdict: str, requested: Dict, search_query: str,
             reasons: List[str], rejected: List[Dict],
             message: str, suggestion: str) -> dict:
    """A not-found / ambiguous result.

    Carries requested-vs-resolved and the reasons, and carries NO price, room
    type or description for any listing — the whole point is that the model
    cannot lift another property's rent out of this payload. F14 asserted
    "£1,300 pcm" for a property that was never in the cache; there is now no
    £-figure in the payload for it to assert."""
    return {
        "success": False,
        "found": False,
        "search_query": search_query,
        "match": {
            "verdict": verdict,
            "requested": requested["raw"] or search_query,
            "requested_name": " ".join(requested["name_tokens"]),
            "requested_postcode": (requested["postcode_unit"]
                                   or requested["postcode_outward"] or ""),
            "resolved": None,
            "reasons": reasons,
        },
        "did_you_mean": [
            {"address": r["address"], "url": r["url"],
             "why_rejected": r["why_rejected"]}
            for r in rejected[:4]
        ],
        "message": message,
        "suggestion": suggestion,
    }


def get_property_details_impl(
    property_name: str = "",
    property_address: str = "",
    property_url: str = "",
    question: Optional[str] = None,
    **kwargs
) -> dict:
    """
    获取特定房产的详细信息

    NOTE: this is a PLAIN SYNC function on purpose. load_property_database /
    find_cached_listing_by_url perform SYNCHRONOUS blocking I/O (sqlite reads over the
    on-demand listing cache + pandas DataFrame construction). Registering it as sync means
    Tool.execute offloads it to an executor thread (tool_system.py :279-284), keeping the
    asyncio event loop responsive so the fc-loop's per-tool timeout / batch budget can fire.

    当用户询问数据库中某个房产的具体信息时使用此工具。
    优先通过 URL 精确命中 sqlite 缓存；否则通过名称/地址匹配 —— 但只有身份核对
    通过（名称 + 邮编 + 门牌号都不冲突）才会返回房源详情。核对不通过时返回
    not-found / ambiguous，而不是别的房源，因为返回别人的租金比什么都不返回更糟。

    Args:
        property_name: 房产名称（如 "Scape Bloomsbury"）
        property_address: 房产地址或部分地址（如 "Woburn Place"）
        property_url: 房产 URL（推荐！推荐索引/结果里每条都带 URL，直接传它可精确命中缓存里那一条）
        question: 用户关于这个房产的具体问题（可选）

    Returns:
        包含房产详细信息的字典；`match` 字段给出 requested vs resolved 与判定结论。
    """
    print(f"\n{'='*60}")
    print(f"🏠 [PROPERTY DETAILS] 查询房产详情")
    print(f"   property_name: {property_name}")
    print(f"   property_address: {property_address}")
    print(f"   property_url: {property_url}")
    print(f"   question: {question}")
    print(f"{'='*60}")

    # 加载数据库
    df = load_property_database()
    if df.empty:
        return {
            "success": False,
            "error": "无法加载房产数据库",
            "message": "抱歉，无法访问房产数据库。请稍后重试。"
        }

    # 构建查询字符串（URL 放最前，触发下方按 URL 精确命中缓存的直查分支）。
    search_query = ""
    if property_url:
        search_query = property_url
    if property_name:
        search_query = f"{search_query} {property_name}".strip()
    if property_address:
        search_query = f"{search_query} {property_address}".strip()

    if not search_query:
        return {
            "success": False,
            "error": "需要提供房产名称、地址或 URL",
            "message": "请提供您想查询的房产名称、地址或 URL。"
        }

    # 精确优先：若查询本身就是一条房源 URL（如前端 "Ask AI" 传入的 focus URL），
    # 直接按 URL 命中缓存中的那一条，避免模糊匹配歧义。
    primary: Optional[Dict] = None
    verdict = ""
    corroborated: List[str] = []
    others: List[Dict] = []
    had_url = bool(_URL_TOKEN_RE.search(search_query))
    if had_url:
        try:
            from core.scraping.on_demand import find_cached_listing_by_url
            for token in search_query.split():
                if _URL_TOKEN_RE.search(token):
                    hit = find_cached_listing_by_url(token)
                    if hit:
                        primary, verdict = hit, "exact_url"
                        corroborated = ["listing_url"]
                        break
        except Exception as e:
            print(f"  [PROPERTY DETAILS] URL 直查失败: {e}")

    requested = parse_property_reference(search_query)

    if primary is None:
        resolvable = bool(requested["name_tokens"]) or bool(
            requested["postcode_unit"] or requested["postcode_outward"])
        if not resolvable:
            if had_url:
                # A URL that the cache has never seen, and no name/postcode to fall
                # back on. Fuzzy-matching the leftover text would hand back some
                # other listing under the identity of the one that was asked about.
                return _refusal(
                    "url_not_in_cache", requested, search_query,
                    [f"no cached listing has URL {property_url or search_query}"],
                    [],
                    "NOT FOUND: that listing URL is not in the local cache. Do NOT "
                    "state a price, room type or any other detail for it, and do NOT "
                    "substitute a different listing. "
                    "该 URL 不在缓存中；不要用别的房源代替。",
                    "Re-run the property search for this area so the listing is "
                    "cached, then ask again.")
            # Nothing to resolve on at all. Picking a row would be picking at random.
            return _refusal(
                "not_resolvable", requested, search_query,
                [f"request_not_resolvable: no building/street name and no postcode "
                 f"in '{requested['raw'] or search_query}'"],
                [],
                "CANNOT RESOLVE: that request does not name a building, street or "
                "postcode, so no listing can be identified. Ask which listing is "
                "meant. 无法定位房源：请求里没有楼盘名/街道名/邮编。",
                "Ask the user for the listing's name, address or URL.")

        # 名称/地址匹配 —— 只接受身份核对通过的房源。
        # 旧实现在这里还有一段"更宽松的搜索"：逐个关键词再试一次、且用子串比较。
        # 那段代码就是 "mews" -> Raleigh Mews、"building" -> Plimsoll Building 的
        # 直接来源，已删除：放宽到单个通用词的匹配没有任何身份含义。
        requested, confirmed, rejected = resolve_property_reference(search_query, df)
        confirmed = _dedupe_listings(confirmed)

        if not confirmed:
            reasons = ([r["why_rejected"] for r in rejected[:3]]
                       or ["no cache row shares the requested name or postcode"])
            return _refusal(
                "no_match", requested, search_query, reasons, rejected,
                f"NOT FOUND: no cached listing matches "
                f"'{requested['raw'] or search_query}'. Do NOT state a price, room "
                f"type, size or any other detail for this property — the cache holds "
                f"no record of it. Any nearby listing shown under 'did_you_mean' is a "
                f"DIFFERENT property; its details do not apply. "
                f"未找到该房源；不要拿别的房源的价格/房型来回答。",
                "Tell the user the listing is not in the current results and ask for "
                "its URL, or re-run a property search for that area.")

        # 多条身份都核对通过、且分数并列 —— 无法判断用户指的是哪一条。
        # 静默取第一条正是本次缺陷的形状，所以这里同样拒绝并要求消歧。
        if len(confirmed) > 1 and confirmed[0]["score"] == confirmed[1]["score"]:
            tied = confirmed[:4]
            return _refusal(
                "ambiguous", requested, search_query,
                [f"{len(tied)} different cached listings match "
                 f"'{requested['raw'] or search_query}' equally well"],
                [{"address": str(m["data"].get('Address', '') or ''),
                  "url": str(m["data"].get('URL', '') or ''),
                  "why_rejected": "tied with the other candidates — cannot tell "
                                  "which listing was meant"}
                 for m in tied],
                "AMBIGUOUS: several different cached listings match that description "
                "equally well. Do NOT pick one and do NOT state a price — ask which "
                "one is meant. 有多条房源同样匹配；不要替用户挑一条。",
                "Ask the user which listing they mean (or pass its URL to "
                "get_property_details).")

        primary = confirmed[0]["data"]
        verdict = "resolved"
        corroborated = confirmed[0]["corroborated_by"]
        others = confirmed[1:5]

    # ── 身份核对通过：requested vs resolved 都写进结果 ────────────────────────
    resolved = parse_property_reference(str(primary.get('Address', '') or ''))
    match_block = {
        "verdict": verdict,                       # exact_url | resolved
        "requested": requested["raw"] or search_query,
        "requested_name": " ".join(requested["name_tokens"]),
        "requested_postcode": (requested["postcode_unit"]
                               or requested["postcode_outward"] or ""),
        "resolved": resolved["raw"],
        "resolved_postcode": (resolved["postcode_unit"]
                              or resolved["postcode_outward"] or ""),
        "corroborated_by": corroborated,
        "reasons": [],
    }
    if verdict == "exact_url":
        match_note = (f"✅ Identity confirmed by listing URL: this IS the listing "
                      f"you asked about ({resolved['raw']}).")
    else:
        match_note = (f"✅ Identity checked: requested \"{match_block['requested']}\" "
                      f"→ resolved \"{resolved['raw']}\" "
                      f"(confirmed by: {', '.join(corroborated) or 'name'}). "
                      f"The price below belongs to THIS listing.")

    formatted_details = format_property_details(primary, match_note=match_note)

    # 提取关键信息用于回答特定问题
    room_type = primary.get('Room_Type_Category', '') or ''

    # 判断房型相关信息
    is_studio = 'studio' in room_type.lower()
    is_shared = 'shared' in room_type.lower() or 'twin' in room_type.lower()
    is_ensuite = 'en-suite' in room_type.lower() or 'ensuite' in room_type.lower()
    has_private_kitchen = 'own kitchen' in room_type.lower() or 'private kitchen' in room_type.lower()

    result = {
        "success": True,
        "found": True,
        "search_query": search_query,
        "match": match_block,
        "property": {
            "address": primary.get('Address', ''),
            "price": primary.get('Price', ''),
            "room_type": room_type,
            "description": primary.get('Description', ''),
            "amenities": primary.get('Detailed_Amenities', ''),
            "guest_policy": primary.get('Guest_Policy', ''),
            "payment_rules": primary.get('Payment_Rules', ''),
            "excluded_features": primary.get('Excluded_Features', ''),
            "url": primary.get('URL', ''),
            "available_from": primary.get('Available From', ''),
            "geo_location": primary.get('geo_location', ''),
        },
        "room_type_analysis": {
            "is_studio": is_studio,
            "is_shared_room": is_shared,
            "is_ensuite": is_ensuite,
            "has_private_kitchen": has_private_kitchen,
            "room_type_category": room_type
        },
        "formatted_details": formatted_details,
        "total_matches": 1 + len(others),
        "message": f"找到房产: {primary.get('Address', '')}（已核对与请求一致）",
    }

    # 其他同样核对通过的房源：只给地址 + URL。不给价格 —— 其中至多一条是用户
    # 问的那一套，把别人的价格放进上下文就是本次缺陷的复现路径。
    if others:
        result["other_matches"] = [
            {
                "address": m["data"].get('Address', ''),
                "url": m["data"].get('URL', ''),
                "note": "a different listing that also matches the description; "
                        "its price is NOT this listing's price",
            }
            for m in others
        ]

    print(f"\n✅ [PROPERTY DETAILS] 身份核对通过 ({verdict})")
    print(f"   requested: {match_block['requested']}")
    print(f"   resolved:  {resolved['raw']}")
    print(f"   corroborated_by: {corroborated}")
    print(f"   房型: {room_type} / 是否Studio: {is_studio}")

    return result


# 创建工具实例
get_property_details_tool = Tool(
    name="get_property_details",
    description="""Get a specific property's full details from the local cache (description, amenities, visitor/payment policy, room type) — same source as search, more accurate than the web. Use when the user asks about a specific listing, clicks "Ask AI" on one, or asks about any previously recommended listing. The RECOMMENDED LISTINGS INDEX in context holds only summaries; pass a listing's URL to property_url for an exact cache hit (avoids same-name ambiguity), else use its name or address.
This tool VERIFIES that the row it found is the listing you asked for (name + postcode + house number) and returns `match.verdict`. If it returns found=false (verdict no_match / ambiguous / url_not_in_cache) then the listing is NOT in the cache: say so, and never quote a price, room type or size from a different listing.
获取某房源的完整详情；优先用该房源 URL 精确命中缓存。若返回 found=false，说明缓存里没有这套房，不要用别的房源代替。""",
    parameters={
        "type": "object",
        "properties": {
            "property_url": {
                "type": "string",
                "description": "房产 URL（首选）。推荐索引/搜索结果里每条都带 URL，直接传它可精确命中缓存里那一条房源。"
            },
            "property_name": {
                "type": "string",
                "description": "房产名称，如 'Scape Bloomsbury', 'iQ Bloomsbury', 'Tufnell House' 等"
            },
            "property_address": {
                "type": "string",
                "description": "房产地址或部分地址，如 '19-29 Woburn Place' 或 'London WC1H'"
            },
            "question": {
                "type": "string",
                "description": "房产相关的具体问题（可选），如 '是不是studio？' 或 '访客政策是什么？'"
            }
        },
        "required": []  # 至少需要 property_url / property_name / property_address 之一
    },
    func=get_property_details_impl
)
