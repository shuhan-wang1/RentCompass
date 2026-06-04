"""
LangGraph-based Agent for UK Rent Recommendation

Replaces the custom ReAct agent with a LangGraph StateGraph architecture.
Preserves all business logic: majority voting, accumulated criteria injection,
Alex persona prompts, preference extraction, and response formatting.

Graph Flow:
    START -> extract_preferences -> decide_tool -> [conditional routing]
        -> execute_tool -> [conditional routing]
        -> generate_response -> format_output -> END
"""

import asyncio
import json
import re
import logging
import datetime
from typing import TypedDict, Optional, Dict, List, Any, Annotated
from collections import Counter

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

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

class AgentState(TypedDict):
    """LangGraph agent state — flows through all graph nodes."""
    # User input
    user_query: str
    # Property context from UI (address, price, amenities, etc.)
    extracted_context: Dict[str, Any]
    # Accumulated across conversation turns
    user_preferences: Dict[str, List[str]]
    accumulated_search_criteria: Dict[str, Any]
    # Per-turn tool execution
    tool_decision: Dict[str, Any]
    tool_observation: Optional[str]
    tool_raw_data: Optional[Any]
    # Output
    final_response: str
    response_type: str
    tool_data: Dict[str, Any]


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

TOOLS:
1. reasoning_property - User asks about a SPECIFIC property's details/features/policies
2. search_properties - User wants to FIND/SHOW/GET properties from database
3. calculate_commute_cost - Calculate SPECIFIC commute cost between two addresses
4. web_search - User wants INFORMATION, ADVICE, COMPARISONS, or GENERAL PRICING
5. search_nearby_pois - Questions about SURROUNDINGS / NEARBY AMENITIES
6. check_safety - Safety/crime questions about specific location
7. get_weather - Weather questions
8. multi_search - Multiple independent sub-questions requiring different tools

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


def _make_decide_tool_node(tool_registry, classification_llm):
    """Create the decide_tool node with majority voting."""

    def decide_tool_node(state: AgentState) -> dict:
        user_query = state["user_query"]
        extracted_context = state["extracted_context"]
        query_lower = user_query.lower()

        # 1) Property context check
        if extracted_context.get('property_address'):
            poi_kws = ['nearby', 'near', 'close to', 'supermarket', 'station', 'gym',
                        'restaurant', 'cafe', 'park', 'tube', 'metro',
                        '\u8d85\u5e02', '\u5730\u94c1', '\u8f66\u7ad9', '\u8ddd\u79bb',
                        '\u9644\u8fd1', '\u65c1\u8fb9', '\u5468\u56f4']
            if not any(kw in query_lower for kw in poi_kws):
                return {"tool_decision": {
                    "tool": "reasoning_property", "params": {},
                    "reason": "Property context detected - use database info"
                }}

        # 2) Simple greetings
        greetings = ['hi', 'hello', '\u4f60\u597d', '\u60a8\u597d', 'hey', 'thanks', '\u8c22\u8c22']
        if any(g == query_lower.strip() for g in greetings) or (
                len(user_query) < 10 and any(g in query_lower for g in greetings)):
            return {"tool_decision": {
                "tool": "direct_answer", "params": {},
                "reason": "Simple greeting"
            }}

        # 3) Majority voting
        decision = _majority_vote(user_query, extracted_context, classification_llm, tool_registry)
        return {"tool_decision": decision}

    return decide_tool_node


def _majority_vote(user_query, extracted_context, llm, tool_registry, num_votes=5):
    """LLM majority voting for tool selection."""
    prompt = CLASSIFICATION_PROMPT.format(user_query=user_query)
    votes = []

    for i in range(num_votes):
        try:
            response = llm.invoke(prompt)
            text = response.content.strip().lower() if hasattr(response, 'content') else str(response).strip().lower()
            text = text.replace('tool:', '').replace('**', '').strip()

            tool = None
            # Priority matching (specific to general)
            for name in ['search_nearby_pois', 'calculate_commute_cost', 'multi_search',
                         'reasoning_property', 'search_properties', 'check_safety',
                         'get_weather', 'web_search']:
                if name in text or name.replace('_', ' ') in text:
                    tool = name
                    break
            votes.append(tool or 'web_search')
        except Exception as e:
            logger.warning(f"Vote {i+1} failed: {e}")
            continue

    if not votes:
        return _heuristic_fallback(user_query, extracted_context, tool_registry)

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

    return _build_tool_params(winner, user_query, extracted_context, tool_registry)


def _heuristic_fallback(user_query, extracted_context, tool_registry):
    """Fallback when no votes succeed."""
    ql = user_query.lower()
    if any(k in ql for k in ['find me', 'show me', '\u627e\u623f', '\u641c\u623f', '\u79df\u623f']):
        return {"tool": "search_properties", "params": {"user_query": user_query}, "reason": "Heuristic: property search"}
    if any(k in ql for k in ['safe', 'crime', '\u5b89\u5168', '\u72af\u7f6a']):
        addr = extracted_context.get('property_address')
        if addr:
            return {"tool": "check_safety", "params": {"address": addr, "area": addr, "user_query": user_query}, "reason": "Heuristic: safety"}
        return {"tool": "clarification", "params": {},
                "clarification_message": "Please provide a postcode or click 'Ask AI' on a property card.",
                "reason": "Need address for safety check"}
    if any(k in ql for k in ['weather', '\u5929\u6c14']):
        return {"tool": "get_weather", "params": {"location": "London"}, "reason": "Heuristic: weather"}
    return {"tool": "web_search", "params": {"query": user_query}, "reason": "Heuristic: default web search"}


def _build_tool_params(tool_name, user_query, extracted_context, tool_registry):
    """Build appropriate params for the selected tool."""
    if tool_name == 'reasoning_property':
        return {"tool": "reasoning_property", "params": {}, "reason": f"Voted: {tool_name}"}
    elif tool_name == 'search_properties':
        return {"tool": "search_properties", "params": {"user_query": user_query}, "reason": f"Voted: {tool_name}"}
    elif tool_name == 'search_nearby_pois':
        addr = extracted_context.get('property_address', 'London')
        return {"tool": "search_nearby_pois",
                "params": {"address": addr, "user_query": user_query, "radius": 1000},
                "reason": f"Voted: {tool_name}"}
    elif tool_name == 'check_safety':
        addr = extracted_context.get('property_address')
        if not addr:
            return {"tool": "clarification", "params": {},
                    "clarification_message": "Please provide a postcode or click 'Ask AI' on a property card.",
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
        return {"tool": "calculate_commute_cost",
                "params": {"user_query": user_query},
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
    """Create the execute_tool node."""

    async def execute_tool_node(state: AgentState) -> dict:
        decision = state["tool_decision"]
        tool_name = decision["tool"]
        params = dict(decision.get("params", {}))
        accumulated = state["accumulated_search_criteria"]
        extracted_context = state["extracted_context"]

        observation = None
        raw_data = None

        try:
            if tool_name == 'multi_search':
                observation, raw_data = await _execute_multi_search(
                    decision['params']['searches'], tool_registry)

            elif tool_name == 'reasoning_property':
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

                result = await tool_registry.execute_tool(tool_name, **params)
                raw_data = result.data if result.success else None
                observation = json.dumps(result.data, ensure_ascii=False, indent=2) if result.success else f"Error: {result.error}"

                # Update accumulated criteria from search results
                if raw_data:
                    extracted = raw_data.get('extracted_so_far') or raw_data.get('search_criteria') or {}
                    if extracted:
                        new_acc = update_search_criteria(accumulated, extracted)
                        return {
                            "tool_observation": observation, "tool_raw_data": raw_data,
                            "accumulated_search_criteria": new_acc
                        }

            else:
                # Standard tool execution
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

        return {"tool_observation": observation, "tool_raw_data": raw_data}

    return execute_tool_node


async def _execute_multi_search(searches, tool_registry):
    """Execute multiple tools in parallel."""
    async def _run_one(search):
        tool_name = search.get('tool', 'web_search')
        params = search.get('params', {})
        try:
            result = await tool_registry.execute_tool(tool_name, **params)
            if result.success:
                obs = result.data.get('results', json.dumps(result.data, ensure_ascii=False)) if isinstance(result.data, dict) else str(result.data)
                return obs, result.data
            return f"Error: {result.error}", None
        except Exception as e:
            return f"Error: {e}", None

    results = await asyncio.gather(*[_run_one(s) for s in searches], return_exceptions=True)

    all_obs = []
    all_raw = {}
    for i, (search, res) in enumerate(zip(searches, results)):
        tool_name = search.get('tool', 'web_search')
        if isinstance(res, Exception):
            obs, rd = f"Error: {res}", None
        else:
            obs, rd = res
        if not isinstance(obs, str):
            obs = str(obs)
        all_obs.append(f"### Sub-search {i+1}: {tool_name}\nParams: {json.dumps(search.get('params',{}), ensure_ascii=False)}\nResult:\n{obs}")
        if rd:
            all_raw[f"{tool_name}_{i+1}"] = rd

    combined = "\n" + "="*50 + "\n## Combined Results\n" + "="*50 + "\n\n"
    combined += "\n---\n".join(all_obs)
    combined += f"\n\nTotal: {len(searches)} tools executed.\n"
    return combined, all_raw


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
                summary = raw_data.get('summary', f"Found {len(recs)} properties.")
                response = f"Great news! {summary}\n\nCheck out the listings on the right panel."
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

def route_by_tool(state: AgentState) -> str:
    """Route after tool decision."""
    tool = state["tool_decision"].get("tool", "")
    if tool == "direct_answer":
        return "generate_response"
    if tool == "clarification":
        return "format_output"
    return "execute_tool"


def route_after_execution(state: AgentState) -> str:
    """Route after tool execution."""
    tool = state["tool_decision"].get("tool", "")
    raw = state.get("tool_raw_data")

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
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_agent_graph(tool_registry):
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
    graph.add_node("generate_response", _make_generate_response_node())
    graph.add_node("format_output", _make_format_output_node())

    # Edges
    graph.add_edge(START, "extract_preferences")
    graph.add_edge("extract_preferences", "decide_tool")
    graph.add_conditional_edges("decide_tool", route_by_tool, {
        "generate_response": "generate_response",
        "format_output": "format_output",
        "execute_tool": "execute_tool",
    })
    graph.add_conditional_edges("execute_tool", route_after_execution, {
        "format_output": "format_output",
        "generate_response": "generate_response",
    })
    graph.add_edge("generate_response", "format_output")
    graph.add_edge("format_output", END)

    return graph.compile()


def create_initial_state(user_query: str,
                          extracted_context: dict = None,
                          user_preferences: dict = None,
                          accumulated_search_criteria: dict = None) -> AgentState:
    """Create an initial AgentState for graph invocation."""
    return AgentState(
        user_query=user_query,
        extracted_context=extracted_context or {},
        user_preferences=user_preferences or {
            'hard_preferences': [], 'soft_preferences': [],
            'excluded_areas': [], 'required_amenities': [],
            'safety_concerns': [],
        },
        accumulated_search_criteria=accumulated_search_criteria or {
            'destination': None, 'max_budget': None, 'max_travel_time': None,
            'property_features': [], 'soft_preferences': [],
            'amenities_of_interest': [],
        },
        tool_decision={},
        tool_observation=None,
        tool_raw_data=None,
        final_response="",
        response_type="answer",
        tool_data={},
    )
