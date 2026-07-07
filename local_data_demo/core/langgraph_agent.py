"""
LangGraph-based Agent for UK Rent Recommendation

Replaces the custom ReAct agent with a LangGraph StateGraph architecture.
Preserves all business logic: majority voting, accumulated criteria injection,
Alex persona prompts, preference extraction, and response formatting.

Graph Flow:
    START -> extract_preferences -> decide_tool
    decide_tool routes via Command(goto=...):
        - direct_answer   -> generate_response
        - clarification   -> format_output
        - multi_search    -> dispatch_searches
        - any other tool  -> execute_tool
    execute_tool routes via Command(goto=...) -> format_output | generate_response
    multi_search map-reduce:
        dispatch_searches -(Send fan-out)-> search_worker x N -> gather_searches
        -> generate_response
    generate_response -> format_output -> END
"""

import asyncio
import operator
import json
import re
import logging
import datetime
from typing import TypedDict, Optional, Dict, List, Any, Annotated, Literal
from collections import Counter

from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, Send
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from uk_rent_agent.agent.state import AgentState, create_initial_state
from uk_rent_agent.agent.contracts import ToolInvocation
from uk_rent_agent.agent.critic import evaluate_grounding, safe_fallback
from uk_rent_agent.agent.guardrails import sanitize_untrusted, tool_allowed

logger = logging.getLogger(__name__)

# ─── POI Display Info ───────────────────────────────────────────────
POI_TYPES = {
    "restaurant": {"icon": "\U0001f37d\ufe0f", "name": "Restaurant"},
    "chinese_restaurant": {"icon": "\U0001f962", "name": "Chinese Restaurant"},
    "supermarket": {"icon": "\U0001f6d2", "name": "Supermarket"},
    "convenience": {"icon": "\U0001f3ea", "name": "Convenience Store"},
    "cafe": {"icon": "\u2615", "name": "Cafe"},
    "pharmacy": {"icon": "\U0001f48a", "name": "Pharmacy"},
    "gym": {"icon": "\U0001f3cb\ufe0f", "name": "Gym"},
    "park": {"icon": "\U0001f333", "name": "Park"},
    "bus_stop": {"icon": "\U0001f68c", "name": "Bus Stop"},
    "tube_station": {"icon": "\U0001f687", "name": "Tube Station"},
    "bank": {"icon": "\U0001f3e6", "name": "Bank"},
    "atm": {"icon": "\U0001f4b3", "name": "ATM"},
}


# ═══════════════════════════════════════════════════════════════════
# STATE SCHEMA
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS (ported from react_agent.py)
# ═══════════════════════════════════════════════════════════════════

def extract_preferences_from_message(user_message: str, current_prefs: dict) -> dict:
    """Extract user preferences from a message. Returns updated prefs dict."""
    prefs = {k: list(v) for k, v in current_prefs.items()}
    user_lower = user_message.lower()

    def _add(ptype, val):
        if val and val not in prefs.get(ptype, []):
            prefs.setdefault(ptype, []).append(val)

    # Safety concerns
    safety_kws = ['safe', 'safety', 'crime', 'dangerous', 'worried', 'unsafe']
    if any(kw in user_lower for kw in safety_kws):
        areas = ['brent cross', 'brent', 'hackney', 'tottenham', 'brixton', 'peckham', 'lewisham']
        for area in areas:
            if area in user_lower:
                _add('safety_concerns', f"User expressed safety concerns about {area.title()}")

    # Amenity requirements
    amenity_patterns = {
        'gym': ['gym', 'fitness', 'workout'], 'pool': ['pool', 'swimming'],
        'parking': ['parking', 'car park'], 'laundry': ['laundry', 'washing machine'],
        'balcony': ['balcony', 'terrace'], 'concierge': ['concierge', '24/7', 'reception'],
    }
    for amenity, keywords in amenity_patterns.items():
        if any(kw in user_lower for kw in keywords):
            strong = any(w in user_lower for w in ['must', 'need', 'require', 'essential'])
            if strong:
                _add('required_amenities', amenity)
                _add('hard_preferences', f"Must have {amenity}")
            else:
                _add('soft_preferences', f"Would like {amenity}")

    # Exclusion preferences
    exclude_patterns = ["don't want", 'not interested', 'avoid', 'no thanks', 'without']
    if any(p in user_lower for p in exclude_patterns):
        if 'brent' in user_lower:
            _add('excluded_areas', 'Brent Cross')

    # Lifestyle preferences
    lifestyle = {
        'quiet': 'Prefers quiet neighborhood', 'vibrant': 'Likes vibrant area',
        'social': 'Values social facilities', 'study': 'Needs good study environment',
        'cooking': 'Wants to cook', 'guest': 'Will have guests',
        'couple': 'Living as a couple', 'female': 'Female student - safety priority',
    }
    for kw, pref in lifestyle.items():
        if kw in user_lower:
            _add('soft_preferences', pref)

    return prefs


def update_search_criteria(accumulated: dict, new_criteria: dict) -> dict:
    """Merge new search criteria into accumulated state."""
    result = {k: (list(v) if isinstance(v, list) else v) for k, v in accumulated.items()}
    if not new_criteria:
        return result

    for field in ['destination', 'max_budget', 'max_travel_time']:
        if new_criteria.get(field):
            result[field] = new_criteria[field]

    for field in ['property_features', 'soft_preferences', 'amenities_of_interest']:
        new_items = new_criteria.get(field, [])
        if isinstance(new_items, str) and new_items:
            new_items = [new_items]
        elif not isinstance(new_items, list):
            new_items = []
        for item in new_items:
            if item and isinstance(item, str) and len(item) > 1 and item not in result.get(field, []):
                result.setdefault(field, []).append(item)

    for tag in new_criteria.get('property_tags', []):
        if tag and tag not in result.get('property_features', []):
            result.setdefault('property_features', []).append(tag)

    return result


def get_preferences_context(prefs: dict) -> str:
    """Build a text summary of user preferences."""
    parts = []
    if prefs.get('hard_preferences'):
        parts.append(f"HARD REQUIREMENTS: {'; '.join(prefs['hard_preferences'])}")
    if prefs.get('soft_preferences'):
        parts.append(f"SOFT PREFERENCES: {'; '.join(prefs['soft_preferences'])}")
    if prefs.get('excluded_areas'):
        parts.append(f"EXCLUDED AREAS: {', '.join(prefs['excluded_areas'])}")
    if prefs.get('required_amenities'):
        parts.append(f"REQUIRED AMENITIES: {', '.join(prefs['required_amenities'])}")
    if prefs.get('safety_concerns'):
        parts.append(f"SAFETY CONCERNS: {'; '.join(prefs['safety_concerns'])}")
    return '\n'.join(parts)


def build_context_info(extracted_context: dict, tool_name: str, prefs: dict) -> str:
    """Build context info string for LLM prompts."""
    if tool_name in ['web_search', 'multi_search']:
        return ("This is a GENERAL INFORMATION query about UK/London living costs, rent, transport, etc. "
                "Do NOT reference specific property listings from previous searches.")

    info = []
    prefs_ctx = get_preferences_context(prefs)
    if prefs_ctx:
        info.append("=== USER PREFERENCES ===")
        info.append(prefs_ctx)
        info.append("=== END PREFERENCES ===\n")

    if extracted_context.get('previous_search_results'):
        info.append("=== PREVIOUSLY SHOWN PROPERTIES ===")
        info.append(extracted_context['previous_search_results'])
        info.append("=== END PREVIOUS RESULTS ===\n")

    if extracted_context.get('comparison_properties'):
        info.append("=== PROPERTY COMPARISON DATA ===")
        info.append(extracted_context['comparison_properties'])
        info.append("=== END COMPARISON DATA ===\n")

    if extracted_context.get('property_address'):
        info.append("=== Current Property Context ===")
        for key, label in [
            ('property_address', 'Address'), ('property_price', 'Price'),
            ('room_type', 'Room Type'), ('amenities', 'Amenities'),
            ('guest_policy', 'Guest Policy'), ('payment_rules', 'Payment Rules'),
            ('excluded_features', 'NOT Included'), ('description', 'Description'),
            ('property_url', 'Booking URL'),
        ]:
            if extracted_context.get(key):
                info.append(f"{label}: {extracted_context[key]}")
        info.append("=== End Property Context ===\n")

    return '\n'.join(info) if info else "No specific property context."


def clean_response(response: str) -> str:
    """Clean internal formatting artifacts from LLM response."""
    if not response:
        return response

    response = re.sub(r'^\s*\*\*Final Answer:\*\*\s*', '', response, flags=re.IGNORECASE)
    response = re.sub(r'^\s*Final Answer:\s*', '', response, flags=re.IGNORECASE)

    lines = []
    for line in response.split('\n'):
        ll = line.lower().strip()
        if ll.startswith('thought:') or ll.startswith('action:') or ll.startswith('observation:'):
            continue
        if ll.startswith('action input:'):
            after = line.split(':', 1)
            if len(after) > 1 and after[1].strip():
                lines.append(after[1].strip())
            continue
        if line.strip() in ('**', '** '):
            continue
        lines.append(line)

    result = '\n'.join(lines).strip() or response

    # Year validation: flag only years AFTER the current year as projected
    current_year = datetime.datetime.now().year

    def _flag_future_year(match):
        year = int(match.group(1))
        if year > current_year:
            return f"{year} {match.group(2)} (projected, verify officially)"
        return match.group(0)

    result = re.sub(
        r'\b(20\d{2})\s*(NHS|visa|Council Tax|rent)',
        _flag_future_year,
        result,
        flags=re.IGNORECASE,
    )

    return result


def apply_preference_filter(recommendations: list, prefs: dict) -> list:
    """Filter recommendations based on user preferences."""
    excluded = [a.lower() for a in prefs.get('excluded_areas', [])]
    if not excluded:
        return recommendations
    filtered = []
    for prop in recommendations:
        addr = prop.get('address', '').lower()
        area = prop.get('area', '').lower()
        if not any(ex in addr or ex in area for ex in excluded):
            filtered.append(prop)
    return filtered


# ═══════════════════════════════════════════════════════════════════
# PROMPT TEMPLATES
# ═══════════════════════════════════════════════════════════════════

CLASSIFICATION_PROMPT = '''You are a tool router. Classify this query into ONE tool.

USER QUERY: "{user_query}"

TOOLS (generated from the live registry; pseudo routes are marked):
{tool_catalog}
reasoning_property - pseudo route for a specific property already in context
multi_search - pseudo route for multiple independent read-only questions

Output ONLY the tool name:
Tool: '''

REASONING_PROPERTY_PROMPT = """You are Alex, a friendly rental assistant helping explain property details from our DATABASE.

User Question: {user_query}

=== PROPERTY INFORMATION FROM DATABASE ===

{observation}

=== YOUR TASK ===
Answer the user's question using ONLY the property information above.
- DO NOT call external APIs
- Explain room types, policies, amenities clearly
- If user asks "Why recommend this?", mention location, price, amenities, room type
- If info is missing, say "This detail isn't in our database for this property"
- If user asks in ENGLISH, reply in ENGLISH; if in CHINESE, reply in CHINESE

Your response:"""

SYNTHESIS_PROMPT = """You are a helpful assistant for UK student housing.

{context_info}

User Question: {user_query}

I have already gathered the following REAL DATA for you:

{observation}

=== YOUR ROLE: SENIOR HOUSING CONSULTANT ===
Synthesize the data into actionable answers. Do NOT just list links.

GROUNDING RULES:
- Only use information that appears in the search results above
- Do NOT fabricate prices, area names, or policies not in the results
- If data is missing, say "search results don't cover this" and suggest official sources
- Match the user's language (English question = English answer)

SOURCES (when data unavailable):
- Transport fares: tfl.gov.uk
- Rent prices: rightmove.co.uk, zoopla.co.uk
- Official stats: ons.gov.uk

Your response:"""

# ═══════════════════════════════════════════════════════════════════
# GRAPH NODES
# ═══════════════════════════════════════════════════════════════════

def _make_extract_preferences_node():
    """Create the extract_preferences node."""
    def extract_preferences_node(state: AgentState) -> dict:
        prefs = extract_preferences_from_message(
            state["user_query"],
            state["user_preferences"]
        )
        return {"user_preferences": prefs}
    return extract_preferences_node


_RECALL_KWS = [
    'do you remember', 'what do you remember', 'you remember', 'remind me',
    'what am i looking for', "what i'm looking for", 'what i am looking for',
    'what do you know about me', 'what do you know about my', 'about my search',
    'my requirements', 'my preferences', 'summarise my', 'summarize my',
    'what did i say', 'what have i told you', 'recap',
    '你还记得', '还记得', '我在找什么', '我要找什么', '我的需求', '我之前说',
    '总结一下我', '你知道我',
]


def _current_message(user_query: str) -> str:
    """Strip the injected long-term-memory block and conversation history so we can
    detect the user's ACTUAL current intent (the memory block contains words like
    'remember' / 'looking for' that would otherwise false-trigger recall detection)."""
    q = user_query or ""
    if q.startswith("What I remember about this user:"):
        sep = chr(10) + chr(10)
        idx = q.find(sep)
        if idx != -1:
            q = q[idx + 2:]
    for marker in ("Current user message:", "answer to the clarification question:"):
        if marker in q:
            q = q.split(marker)[-1]
    return q.strip()


def _make_decide_tool_node(tool_registry, classification_llm):
    """Create the decide_tool node with majority voting.

    Routes via Command(goto=...) based on the computed tool decision:
    direct_answer -> generate_response, clarification -> format_output,
    multi_search -> dispatch_searches, anything else -> execute_tool.
    """

    def _compute_decision(state: AgentState) -> dict:
        user_query = state["user_query"]
        extracted_context = state["extracted_context"]
        query_lower = user_query.lower()

        # 0) Memory-recall questions -> answer conversationally from the injected
        #    long-term memory (which is already prepended to user_query).
        if any(kw in _current_message(user_query).lower() for kw in _RECALL_KWS):
            return {
                "tool": "direct_answer", "params": {},
                "reason": "User is asking what we remember about them - answer from long-term memory"
            }

        # 1) Property context check
        if extracted_context.get('property_address'):
            poi_kws = ['nearby', 'near', 'close to', 'supermarket', 'station', 'gym',
                        'restaurant', 'cafe', 'park', 'tube', 'metro',
                        '\u8d85\u5e02', '\u5730\u94c1', '\u8f66\u7ad9', '\u8ddd\u79bb',
                        '\u9644\u8fd1', '\u65c1\u8fb9', '\u5468\u56f4']
            if not any(kw in query_lower for kw in poi_kws):
                return {
                    "tool": "reasoning_property", "params": {},
                    "reason": "Property context detected - use database info"
                }

        # 2) Simple greetings
        greetings = ['hi', 'hello', '\u4f60\u597d', '\u60a8\u597d', 'hey', 'thanks', '\u8c22\u8c22']
        if any(g == query_lower.strip() for g in greetings) or (
                len(user_query) < 10 and any(g in query_lower for g in greetings)):
            return {
                "tool": "direct_answer", "params": {},
                "reason": "Simple greeting"
            }

        # 3) Majority voting
        return _majority_vote(user_query, extracted_context, classification_llm,
                              tool_registry, accumulated=state["accumulated_search_criteria"])

    def decide_tool_node(state: AgentState) -> Command[Literal[
            "execute_tool", "generate_response", "format_output", "dispatch_searches"]]:
        decision = _compute_decision(state)
        tool = decision["tool"]
        if tool == "direct_answer":
            goto = "generate_response"
        elif tool == "clarification":
            goto = "format_output"
        elif tool == "multi_search":
            goto = "dispatch_searches"
        else:
            goto = "execute_tool"
        return Command(update={"tool_decision": decision}, goto=goto)

    return decide_tool_node


def _majority_vote(user_query, extracted_context, llm, tool_registry, num_votes=1, accumulated=None):
    """LLM tool selection. num_votes=1 by default: a cloud LLM (DeepSeek) is reliable
    in one shot and each call has real network latency, so 5 sequential votes were the
    main source of latency. Raise num_votes for more robustness with a weaker model."""
    catalog_lines = []
    for name in tool_registry.list_tool_names():
        tool = tool_registry.get(name) if hasattr(tool_registry, "get") else None
        description = getattr(tool, "description", "registered tool")
        catalog_lines.append(f"- {name}: {description}")
    catalog = "\n".join(catalog_lines)
    prompt = CLASSIFICATION_PROMPT.format(user_query=user_query, tool_catalog=catalog)
    votes = []

    for i in range(num_votes):
        try:
            response = llm.invoke(prompt)
            text = response.content.strip().lower() if hasattr(response, 'content') else str(response).strip().lower()
            text = text.replace('tool:', '').replace('**', '').strip()

            tool = None
            # Priority matching (specific to general)
            candidates = sorted(
                set(tool_registry.list_tool_names()) | {"multi_search", "reasoning_property"},
                key=len,
                reverse=True,
            )
            for name in candidates:
                if name in text or name.replace('_', ' ') in text:
                    tool = name
                    break
            votes.append(tool or 'web_search')
        except Exception as e:
            logger.warning(f"Vote {i+1} failed: {e}")
            continue

    if not votes:
        return _heuristic_fallback(user_query, extracted_context, tool_registry, accumulated)

    counter = Counter(votes)
    winner, count = counter.most_common(1)[0]
    logger.info(f"Vote result: {dict(counter)}, winner: {winner} ({count}/{len(votes)})")

    # Tie-breaking
    query_lower = user_query.lower()
    consult_kws = ['should i', 'help me decide', 'which is better', 'worth it',
                   '\u5e94\u8be5', '\u5e2e\u6211\u9009', '\u54ea\u4e2a\u597d',
                   '\u503c\u5f97\u5417', '\u6bd4\u8f83']
    if any(kw in query_lower for kw in consult_kws) and 'web_search' in counter:
        winner = 'web_search'

    action_kws = ['find me', 'show me', 'get me', 'search for',
                  '\u5e2e\u6211\u627e\u623f', '\u641c\u7d22\u623f\u6e90']
    if any(kw in query_lower for kw in action_kws) and 'search_properties' in counter:
        winner = 'search_properties'

    return _build_tool_params(winner, user_query, extracted_context, tool_registry, accumulated)


# UK postcode (full or outward+inward), e.g. "SW8 1RZ", "WC1E 6BT", "EC1A 1BB".
_UK_POSTCODE_RE = re.compile(r'\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b', re.IGNORECASE)
_ORDINAL_WORDS = {
    'first': 0, '1st': 0, 'second': 1, '2nd': 1, 'third': 2, '3rd': 2,
    'fourth': 3, '4th': 3, 'fifth': 4, '5th': 4,
}


def _resolve_target_address(user_query, extracted_context):
    """Resolve the address/postcode a location-specific tool (safety / POIs / commute)
    should use, preferring what the user JUST said over stale frontend context.

    Order: (1) an explicit postcode in the message, (2) an ordinal/deictic reference
    ("the first one", "that property") mapped to the previous search results, (3) a
    property name from the previous results mentioned in the message, (4) the
    frontend-provided property_address (the 'Ask AI' path). Returns None if nothing
    resolves, so the caller can ask for clarification."""
    # Prefer the raw current message: user_query may be prefixed with injected memory
    # and prior turns, whose postcodes/ordinals must not hijack this turn's resolution.
    q = (extracted_context.get('current_message') or user_query) or ""
    m = _UK_POSTCODE_RE.search(q)
    if m:
        return re.sub(r'\s+', ' ', m.group(1)).upper()

    ql = q.lower()
    results = extracted_context.get('last_results') or []
    if results:
        for word, idx in _ORDINAL_WORDS.items():
            if re.search(rf'\b{word}\b', ql) and idx < len(results):
                r = results[idx]
                return r.get('address') or r.get('name')
        if any(p in ql for p in ['that one', 'this one', 'that property', 'this property',
                                 'the property', 'the place', 'that place', 'the flat', 'the studio']):
            r = results[0]
            return r.get('address') or r.get('name')
        for r in results:
            name = (r.get('name') or '').strip().lower()
            if len(name) > 3 and name in ql:
                return r.get('address') or r.get('name')
            # Also match on the address itself: a user often types the street
            # ("is 40 Merchant St safe?") rather than the listing's name.
            addr = (r.get('address') or '').strip().lower()
            if addr:
                street = addr.split(',')[0].strip()           # "40 merchant st"
                street_nonum = re.sub(r'^\d+\s*', '', street)   # "merchant st"
                for cand in (street, street_nonum):
                    if len(cand) >= 4 and cand in ql:
                        return r.get('address') or r.get('name')

    return extracted_context.get('property_address')


# Common student/work destinations -> geocodable full addresses. Short tokens like
# "UCL" alone often fail geocoding, so we expand them to full addresses.
_KNOWN_DESTINATIONS = {
    'university college london': 'University College London, Gower Street, London WC1E 6BT',
    'ucl': 'University College London, Gower Street, London WC1E 6BT',
    'london school of economics': 'London School of Economics, Houghton Street, London WC2A 2AE',
    'lse': 'London School of Economics, Houghton Street, London WC2A 2AE',
    'imperial college': 'Imperial College London, South Kensington, London SW7 2AZ',
    'imperial': 'Imperial College London, South Kensington, London SW7 2AZ',
    "king's college": "King's College London, Strand, London WC2R 2LS",
    'kings college': "King's College London, Strand, London WC2R 2LS",
    'kcl': "King's College London, Strand, London WC2R 2LS",
    'canary wharf': 'Canary Wharf, London E14 5AB',
}


def _resolve_destination_address(user_query, extracted_context, accumulated):
    """Resolve the DESTINATION (to_address) for a commute query: a destination named
    in THIS message wins, else the user's accumulated destination — both normalised
    through the known-destination map so short tokens like 'UCL' geocode reliably."""
    accumulated = accumulated or {}
    q = (extracted_context.get('current_message') or user_query) or ""
    ql = q.lower()
    for kw, addr in _KNOWN_DESTINATIONS.items():
        if kw in ql:
            return addr
    dest = accumulated.get('destination')
    if dest:
        dl = str(dest).lower()
        for kw, addr in _KNOWN_DESTINATIONS.items():
            if kw in dl:
                return addr
        return dest
    return None


def _heuristic_fallback(user_query, extracted_context, tool_registry, accumulated=None):
    """Fallback when no votes succeed."""
    ql = user_query.lower()
    if any(k in ql for k in ['find me', 'show me', '\u627e\u623f', '\u641c\u623f', '\u79df\u623f']):
        return {"tool": "search_properties", "params": {"user_query": user_query}, "reason": "Heuristic: property search"}
    if any(k in ql for k in ['safe', 'crime', '\u5b89\u5168', '\u72af\u7f6a']):
        addr = _resolve_target_address(user_query, extracted_context)
        if addr:
            return {"tool": "check_safety", "params": {"address": addr, "area": addr, "user_query": user_query}, "reason": "Heuristic: safety"}
        return {"tool": "clarification", "params": {},
                "clarification_message": "Which property or area should I check? You can give me a postcode (e.g. SW8 1RZ), say 'the first one' from your last search, or click 'Ask AI' on a property card.",
                "reason": "Need address for safety check"}
    if any(k in ql for k in ['weather', '\u5929\u6c14']):
        return {"tool": "get_weather", "params": {"location": "London"}, "reason": "Heuristic: weather"}
    return {"tool": "web_search", "params": {"query": user_query}, "reason": "Heuristic: default web search"}


def _build_tool_params(tool_name, user_query, extracted_context, tool_registry, accumulated=None):
    """Build appropriate params for the selected tool."""
    if tool_name == 'reasoning_property':
        return {"tool": "reasoning_property", "params": {}, "reason": f"Voted: {tool_name}"}
    elif tool_name == 'search_properties':
        return {"tool": "search_properties", "params": {"user_query": user_query}, "reason": f"Voted: {tool_name}"}
    elif tool_name == 'search_nearby_pois':
        addr = _resolve_target_address(user_query, extracted_context) or 'London'
        return {"tool": "search_nearby_pois",
                "params": {"address": addr, "user_query": user_query, "radius": 1000},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'check_safety':
        addr = _resolve_target_address(user_query, extracted_context)
        if not addr:
            return {"tool": "clarification", "params": {},
                    "clarification_message": "Which property or area should I check? You can give me a postcode (e.g. SW8 1RZ), say 'the first one' from your last search, or click 'Ask AI' on a property card.",
                    "reason": "Need address for safety check"}
        return {"tool": "check_safety",
                "params": {"address": addr, "area": addr, "user_query": user_query},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'get_weather':
        loc = 'London'
        ql = user_query.lower()
        if 'manchester' in ql: loc = 'Manchester'
        elif 'birmingham' in ql: loc = 'Birmingham'
        return {"tool": "get_weather", "params": {"location": loc}, "reason": f"Voted: {tool_name}"}
    elif tool_name in ('web_search', 'multi_search'):
        return _plan_web_searches(user_query, tool_registry)
    elif tool_name == 'calculate_commute_cost':
        # The tool requires BOTH endpoints (and does NOT accept user_query), so the
        # old {"user_query": ...} always failed its required-param check. Resolve the
        # origin from conversation context and the destination from message/accumulated.
        from_addr = _resolve_target_address(user_query, extracted_context)
        to_addr = _resolve_destination_address(user_query, extracted_context, accumulated)
        if not from_addr and not to_addr:
            return {"tool": "clarification", "params": {},
                    "clarification_message": "To work out the commute cost I need a starting property (a postcode, 'the first one' from your last search, or an 'Ask AI' property) and a destination (e.g. your university or workplace).",
                    "reason": "commute needs both endpoints"}
        if not from_addr or not to_addr:
            missing = "starting property/address" if not from_addr else "destination (e.g. your university or workplace)"
            return {"tool": "clarification", "params": {},
                    "clarification_message": f"To calculate the commute cost I just need the {missing}. Could you tell me?",
                    "reason": "commute needs both endpoints"}
        return {"tool": "calculate_commute_cost",
                "params": {"from_address": from_addr, "to_address": to_addr},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'calculate_commute':
        from_addr = _resolve_target_address(user_query, extracted_context)
        to_addr = _resolve_destination_address(user_query, extracted_context, accumulated)
        if not from_addr or not to_addr:
            return {"tool": "clarification", "params": {},
                    "clarification_message": "Please provide both the starting address and destination for the commute.",
                    "reason": "commute needs both endpoints"}
        return {"tool": tool_name, "params": {"from_address": from_addr, "to_address": to_addr},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'get_property_details':
        address = _resolve_target_address(user_query, extracted_context)
        return {"tool": tool_name,
                "params": {"property_address": address or "", "question": user_query},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'check_transport_cost':
        zone_match = re.search(r"\bzone\s*([1-9])\b", user_query, re.IGNORECASE)
        if not zone_match:
            return {"tool": "clarification", "params": {},
                    "clarification_message": "Which London fare zone is the destination in?",
                    "reason": "transport cost needs destination zone"}
        return {"tool": tool_name, "params": {"end_zone": int(zone_match.group(1))},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'recall_memory':
        return {"tool": tool_name, "params": {"query": user_query},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'remember':
        return {"tool": tool_name, "params": {"content": user_query, "kind": "semantic"},
                "reason": f"Voted: {tool_name}"}
    else:
        return {"tool": "web_search", "params": {"query": user_query}, "reason": "Default fallback"}


def _plan_web_searches(user_query, tool_registry):
    """Plan web search queries using LLM (simplified version)."""
    from core.llm_config import get_planning_llm

    planning_prompt = f"""You are a search query planner for a STUDENT housing assistant.
USER QUESTION: {user_query}
Plan 1-5 web searches (in English, include "2025" and "London").
Output JSON: {{"searches": [{{"tool": "web_search", "params": {{"query": "..."}}}}], "reason": "..."}}
JSON:"""

    try:
        llm = get_planning_llm()
        resp = llm.invoke(planning_prompt)
        text = resp.content if hasattr(resp, 'content') else str(resp)
        text = ''.join(c for c in text if ord(c) >= 32 or c in '\n\t')
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            plan = json.loads(match.group().replace('\n', ' '))
            searches = plan.get('searches', [])
            # Ensure 2025 in queries
            for s in searches:
                if s.get('tool') == 'web_search':
                    q = s.get('params', {}).get('query', '')
                    if '2025' not in q and '2024' not in q:
                        s['params']['query'] = q + ' 2025'
            if searches:
                return {"tool": "multi_search", "params": {"searches": searches[:10]},
                        "reason": plan.get('reason', 'LLM planned searches')}
    except Exception as e:
        logger.warning(f"Search planning failed: {e}")

    # Fallback single search
    return {"tool": "multi_search",
            "params": {"searches": [{"tool": "web_search", "params": {"query": f"{user_query} London 2025"}}]},
            "reason": "Fallback search"}


def _make_execute_tool_node(tool_registry):
    """Create the execute_tool node.

    multi_search no longer reaches here (it runs through the dispatch_searches ->
    search_worker -> gather_searches map-reduce subgraph). This node routes via
    Command(goto=...) to format_output or generate_response.
    """

    async def execute_tool_node(state: AgentState) -> Command[Literal[
            "format_output", "generate_response"]]:
        decision = state["tool_decision"]
        tool_name = decision["tool"]
        params = dict(decision.get("params", {}))
        if tool_name in {"recall_memory", "remember"}:
            params["user_id"] = state.get("user_id", "default")
            params["session_id"] = state.get("session_id", "default")
        accumulated = state["accumulated_search_criteria"]
        extracted_context = state["extracted_context"]

        observation = None
        raw_data = None
        update = {}

        try:
            if tool_name == 'reasoning_property':
                # Assemble property info from context
                parts = [f"Property: {extracted_context.get('property_address', 'N/A')}"]
                for key, label in [('property_price', 'Price'), ('room_type', 'Room Type'),
                                   ('property_travel_time', 'Commute Time'),
                                   ('description', 'Description'), ('amenities', 'Amenities'),
                                   ('guest_policy', 'Guest Policy'), ('payment_rules', 'Payment Rules'),
                                   ('excluded_features', 'NOT Included'), ('property_url', 'Booking URL')]:
                    if extracted_context.get(key):
                        parts.append(f"{label}: {extracted_context[key]}")
                observation = '\n'.join(parts)
                raw_data = {'property_info': observation}

            elif tool_name == 'search_properties':
                # Raw current message so the tool can let an explicit budget/commute in
                # THIS message override the accumulated values injected just below.
                if not params.get('current_message'):
                    params['current_message'] = extracted_context.get('current_message', '')
                # Inject accumulated criteria
                if not params.get('location') and accumulated.get('destination'):
                    params['location'] = accumulated['destination']
                if not params.get('max_budget') and accumulated.get('max_budget'):
                    params['max_budget'] = accumulated['max_budget']
                if not params.get('max_commute_time') and accumulated.get('max_travel_time'):
                    params['max_commute_time'] = accumulated['max_travel_time']
                if accumulated.get('property_features'):
                    params['property_features'] = accumulated['property_features']
                if accumulated.get('soft_preferences'):
                    params['accumulated_preferences'] = accumulated['soft_preferences']

                invocation = ToolInvocation.create(
                    run_id=state.get("run_id", "legacy"), node_id="execute_tool",
                    tool=tool_name, params=params,
                    version=getattr(tool_registry.get(tool_name), "version", "1") if hasattr(tool_registry, "get") else "1",
                )
                params["idempotency_key"] = invocation.idempotency_key
                result = await tool_registry.execute_tool(tool_name, **params)
                # Domain-level outcomes such as ``need_clarification`` deliberately
                # use success=False but still carry authoritative structured data.
                # Keep that payload so routing/formatting can handle the outcome.
                raw_data = result.data
                if result.data is not None:
                    observation = json.dumps(result.data, ensure_ascii=False, indent=2)
                else:
                    observation = f"Error: {result.error}"

                # Update accumulated criteria from search results
                if raw_data:
                    extracted = raw_data.get('extracted_so_far') or raw_data.get('search_criteria') or {}
                    if extracted:
                        update["accumulated_search_criteria"] = update_search_criteria(accumulated, extracted)

            else:
                # Standard tool execution
                tool = tool_registry.get(tool_name) if hasattr(tool_registry, "get") else None
                side_effect = getattr(tool, "side_effect", "none")
                if not tool_allowed(
                    side_effect=side_effect,
                    context_tainted=state.get("context_tainted", False),
                    tool_name=tool_name,
                ):
                    raise PermissionError("write tool denied because this turn contains untrusted content")
                invocation = ToolInvocation.create(
                    run_id=state.get("run_id", "legacy"), node_id="execute_tool",
                    tool=tool_name, params=params, version=getattr(tool, "version", "1"),
                )
                params["idempotency_key"] = invocation.idempotency_key
                result = await tool_registry.execute_tool(tool_name, **params)
                raw_data = result.data if result.success else None

                if result.success:
                    if isinstance(result.data, (dict, list)):
                        observation = json.dumps(result.data, ensure_ascii=False, indent=2)
                    else:
                        observation = str(result.data)
                else:
                    observation = f"Error: {result.error}"

        except Exception as e:
            logger.error(f"Tool execution error ({tool_name}): {e}", exc_info=True)
            observation = f"Error executing {tool_name}: {str(e)}"
            raw_data = None

        update["tool_observation"] = observation
        update["tool_raw_data"] = raw_data
        update["context_tainted"] = state.get("context_tainted", False) or tool_name in {
            "web_search", "search_properties", "reasoning_property", "multi_search"
        }
        goto = _route_after_execution(tool_name, raw_data)
        return Command(update=update, goto=goto)

    return execute_tool_node


def _make_generate_response_node():
    """Create the generate_response node."""

    async def generate_response_node(state: AgentState) -> dict:
        from core.llm_config import get_react_llm

        observation = state.get("tool_observation")
        user_query = state["user_query"]
        decision = state["tool_decision"]
        tool_name = decision.get("tool", "")
        extracted_context = state["extracted_context"]
        prefs = state["user_preferences"]

        llm = get_react_llm()

        if observation:
            if state.get("context_tainted"):
                observation = sanitize_untrusted(str(observation)).text
            if tool_name == 'reasoning_property':
                prompt = REASONING_PROPERTY_PROMPT.format(
                    user_query=user_query, observation=observation)
            else:
                ctx = build_context_info(extracted_context, tool_name, prefs)
                prompt = SYNTHESIS_PROMPT.format(
                    context_info=ctx, user_query=user_query, observation=observation)
        else:
            # Direct answer (no tool data)
            ctx = build_context_info(extracted_context, tool_name, prefs)
            prompt = f"You are a helpful assistant for UK student housing.\n\n{ctx}\n\nUser: {user_query}\n\nProvide a helpful response in the user's language.\n\nYour response:"

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            text = response.content if hasattr(response, 'content') else str(response)
            return {"final_response": clean_response(text)}
        except Exception as e:
            logger.error(f"Response generation failed: {e}")
            return {"final_response": "I'm sorry, I couldn't process your request. Please try again."}

    return generate_response_node


def _make_critic_node():
    """Grounding-sensitive requests are checked before reaching the formatter."""
    def critic_node(state: AgentState) -> dict:
        tool_name = (state.get("tool_decision") or {}).get("tool", "")
        retrieval_expected = tool_name not in {"", "direct_answer", "clarification"}
        verdict = evaluate_grounding(
            state.get("final_response", ""),
            state.get("tool_raw_data"),
            retrieval_expected=retrieval_expected,
        )
        update = {
            "verdict": verdict.model_dump(),
            "critic_attempts": state.get("critic_attempts", 0) + 1,
        }
        if not verdict.grounded:
            update["final_response"] = safe_fallback(verdict)
        return update
    return critic_node


def _make_format_output_node():
    """Create the format_output node."""

    def format_output_node(state: AgentState) -> dict:
        decision = state["tool_decision"]
        tool_name = decision.get("tool", "")
        raw_data = state.get("tool_raw_data")
        response = state.get("final_response", "")
        prefs = state["user_preferences"]
        tool_data = {}

        response_type = "answer"

        # Format based on tool type
        if tool_name == 'check_safety' and raw_data and isinstance(raw_data, dict) and raw_data.get('safety_score') is not None:
            response, tool_data = _format_safety(raw_data)

        elif tool_name == 'search_nearby_pois' and raw_data and isinstance(raw_data, dict) and raw_data.get('pois'):
            response, tool_data = _format_pois(raw_data)

        elif tool_name == 'calculate_commute_cost' and raw_data and isinstance(raw_data, dict):
            response, tool_data = _format_commute_cost(raw_data)

        elif tool_name == 'search_properties' and raw_data:
            if raw_data.get('status') == 'need_clarification':
                response = raw_data.get('question', 'Could you please provide more details?')
                response_type = 'question'
            elif raw_data.get('status') == 'found' and raw_data.get('recommendations'):
                recs = apply_preference_filter(raw_data['recommendations'], prefs)
                summary = raw_data.get('summary', f"I found {len(recs)} properties.")
                response = f"{summary}\n\nCheck out the listings on the right panel."
                tool_data = {'recommendations': recs, 'search_criteria': raw_data.get('search_criteria', {})}

        elif tool_name == 'multi_search' and raw_data:
            tool_data = {'multi_search_results': raw_data}

        elif tool_name == 'clarification':
            response = decision.get('clarification_message', 'Please provide more details.')
            response_type = 'clarification'

        return {"final_response": response, "response_type": response_type, "tool_data": tool_data}

    return format_output_node


# ─── Formatting helpers ─────────────────────────────────────────

def _format_safety(data):
    addr = data.get('address', 'the area')
    score = data.get('safety_score', 50)
    level = data.get('safety_level', 'Moderate')
    emoji = "\u2705" if score >= 70 else "\u26a0\ufe0f" if score >= 50 else "\U0001f6a8"

    parts = [f"## {emoji} Safety Report for {addr}", "",
             f"**Safety Score:** {score}/100", f"**Risk Level:** {level}", ""]

    if data.get('scoring_explanation'):
        parts += ["---", "", data['scoring_explanation'], ""]
    if data.get('safety_analysis'):
        parts += ["---", "", data['safety_analysis'], ""]

    parts += ["---", "", "*Note: Based on area statistics. Visit in person before deciding.*"]
    return '\n'.join(parts), {'safety_data': data}


def _format_pois(data):
    pois = data.get('pois') or data.get('results', {})
    addr = data.get('address', 'the location')
    parts = [f"## \U0001f4cd Nearby Facilities - {addr}\n"]
    for poi_type, poi_list in pois.items():
        if poi_list:
            parts.append(f"\n### {poi_type.replace('_', ' ').title()}")
            for poi in poi_list[:5]:
                name = poi.get('name', 'Unknown')
                dist = poi.get('distance_display') or poi.get('distance', 'N/A')
                suffix = '' if isinstance(dist, str) and (dist.endswith('m') or dist.endswith('km')) else 'm'
                parts.append(f"- **{name}** - {dist}{suffix}")
    return '\n'.join(parts), {'poi_results': data}


def _format_commute_cost(data):
    if not data.get('success'):
        return f"Unable to calculate commute cost: {data.get('error', 'Unknown error')}", {}

    parts = ["## \U0001f687 Commute Cost Analysis\n",
             f"**From:** {data.get('from_address', 'N/A')}",
             f"**To:** {data.get('to_address', 'N/A')}\n"]

    commute = data.get('commute', {})
    if commute:
        dur = commute.get('duration_minutes', 'N/A')
        cat = commute.get('duration_category', '')
        parts += [f"### \u23f1\ufe0f Commute Time",
                  f"- **Duration:** {dur} minutes ({cat})",
                  f"- **Daily round trip:** ~{dur * 2 if isinstance(dur, (int, float)) else 'N/A'} minutes\n"]

    tc = data.get('transport_cost', {})
    if tc and 'monthly_cost' in tc:
        parts += [f"### \U0001f4b7 Monthly Transport Cost",
                  f"- **Pass:** {tc.get('recommended_pass', 'N/A')}",
                  f"- **Type:** {tc.get('user_type', 'N/A')}",
                  f"- **Monthly:** \u00a3{tc.get('monthly_cost', 'N/A')}",
                  f"- **Weekly:** \u00a3{tc.get('weekly_cost', 'N/A')}",
                  f"- **Daily Cap:** \u00a3{tc.get('daily_cap', 'N/A')}\n"]

    return '\n'.join(parts), {'commute_cost': data}


# ═══════════════════════════════════════════════════════════════════
# GRAPH ROUTING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def _route_after_execution(tool: str, raw) -> str:
    """Pure helper: decide the next node after tool execution from (tool, raw_data).

    Called by execute_tool to compute Command(goto=...)."""
    # search_properties: check for special statuses
    if tool == "search_properties" and raw and isinstance(raw, dict):
        status = raw.get("status")
        if status == "need_clarification":
            return "format_output"
        if status == "found" and raw.get("recommendations"):
            return "format_output"

    # Tools with structured direct output
    if tool in ("check_safety", "search_nearby_pois", "calculate_commute_cost"):
        if raw and isinstance(raw, dict):
            if (tool == "check_safety" and raw.get("safety_score") is not None) or \
               (tool == "search_nearby_pois" and raw.get("pois")) or \
               (tool == "calculate_commute_cost" and raw.get("success")):
                return "format_output"

    return "generate_response"


# ═══════════════════════════════════════════════════════════════════
# MULTI-SEARCH MAP-REDUCE (dispatch -> Send fan-out -> gather)
# ═══════════════════════════════════════════════════════════════════

def _make_dispatch_searches_node():
    """Create the dispatch_searches node.

    No-op node; its conditional edge (fan_out_searches) does the Send fan-out."""
    def dispatch_searches_node(state: AgentState) -> dict:
        return {}
    return dispatch_searches_node


def fan_out_searches(state: AgentState):
    """MAP: one Send per planned sub-search."""
    searches = (state["tool_decision"].get("params") or {}).get("searches") or []
    if not searches:
        return "gather_searches"   # nothing to fan out
    return [Send("search_worker", {
                "search": s, "search_index": i, "run_id": state.get("run_id", "legacy")
            })
            for i, s in enumerate(searches)]


def _make_search_worker_node(tool_registry):
    """Create the search_worker node (runs one sub-search; results reducer-merged)."""
    async def search_worker_node(state) -> dict:
        s = state["search"]
        i = state["search_index"]
        tool_name = s.get("tool", "web_search")
        params = s.get("params", {})
        try:
            result = await tool_registry.execute_tool(tool_name, **params)
            if result.success:
                obs = (result.data.get('results', json.dumps(result.data, ensure_ascii=False))
                       if isinstance(result.data, dict) else str(result.data))
                raw = result.data
            else:
                obs, raw = f"Error: {result.error}", None
        except Exception as e:
            obs, raw = f"Error: {e}", None
        return {"search_results": [{
            "index": i, "tool": tool_name, "params": params,
            "obs": obs if isinstance(obs, str) else str(obs), "raw": raw,
            "run_id": state.get("run_id", "legacy"),
        }]}
    return search_worker_node


def _make_gather_searches_node():
    """REDUCE: fold worker results into one observation, preserving the old
    _execute_multi_search combined-string format."""
    def gather_searches_node(state: AgentState) -> dict:
        run_id = state.get("run_id", "legacy")
        items = sorted(
            (item for item in state.get("search_results", []) if item.get("run_id") == run_id),
            key=lambda item: item.get("index", 0),
        )
        all_obs, all_raw = [], {}
        for it in items:
            all_obs.append(
                f"### Sub-search {it['index']+1}: {it['tool']}\n"
                f"Params: {json.dumps(it.get('params', {}), ensure_ascii=False)}\n"
                f"Result:\n{it['obs']}"
            )
            if it.get("raw"):
                all_raw[f"{it['tool']}_{it['index']+1}"] = it["raw"]
        combined = "\n" + "=" * 50 + "\n## Combined Results\n" + "=" * 50 + "\n\n"
        combined += "\n---\n".join(all_obs)
        combined += f"\n\nTotal: {len(items)} tools executed.\n"
        return {
            "tool_observation": combined,
            "tool_raw_data": all_raw,
            "context_tainted": True,
        }
    return gather_searches_node


# ═══════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_agent_graph(tool_registry, *, checkpointer=None, store=None):
    """Build and compile the LangGraph StateGraph.

    Args:
        tool_registry: ToolRegistry instance with all tools registered.

    Returns:
        Compiled LangGraph that can be invoked with AgentState.
    """
    from core.llm_config import get_classification_llm

    classification_llm = get_classification_llm()

    graph = StateGraph(AgentState)

    # Register nodes
    graph.add_node("extract_preferences", _make_extract_preferences_node())
    graph.add_node("decide_tool", _make_decide_tool_node(tool_registry, classification_llm))
    graph.add_node("execute_tool", _make_execute_tool_node(tool_registry))
    graph.add_node("dispatch_searches", _make_dispatch_searches_node())
    graph.add_node("search_worker", _make_search_worker_node(tool_registry))
    graph.add_node("gather_searches", _make_gather_searches_node())
    graph.add_node("generate_response", _make_generate_response_node())
    graph.add_node("critic", _make_critic_node())
    graph.add_node("format_output", _make_format_output_node())

    # Edges
    graph.add_edge(START, "extract_preferences")
    graph.add_edge("extract_preferences", "decide_tool")
    # decide_tool and execute_tool route via Command(goto=...) — no conditional edges needed
    graph.add_conditional_edges("dispatch_searches", fan_out_searches, ["search_worker", "gather_searches"])
    graph.add_edge("search_worker", "gather_searches")
    graph.add_edge("gather_searches", "generate_response")
    graph.add_edge("generate_response", "critic")
    graph.add_edge("critic", "format_output")
    graph.add_edge("format_output", END)

    compile_options = {}
    if checkpointer is not None:
        compile_options["checkpointer"] = checkpointer
    if store is not None:
        compile_options["store"] = store
    return graph.compile(**compile_options)
