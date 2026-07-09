"""
Agent Interface - Flexible tool-based system instead of hardcoded workflows

This system lets the LLM decide which tools to use for any query,
making it much more flexible than keyword matching.
"""

import json
import re
from typing import Dict, List, Any
from core.llm_interface import call_ollama
from core.maps_service import (
    get_nearby_places_osm, 
    get_crime_data_by_location,
    calculate_travel_time,
    calculate_distance_m
)
from core.web_search import get_search_snippets


# Registry of available tools
AVAILABLE_TOOLS = {
    "get_nearby_places_osm": {
        "description": "Find nearby amenities like gyms, parks, restaurants using OpenStreetMap",
        "parameters": ["address", "amenity_type", "radius_m"],
        "function": get_nearby_places_osm
    },
    "get_crime_data": {
        "description": "Get crime statistics for an area from UK Police API",
        "parameters": ["address"],
        "function": get_crime_data_by_location
    },
    "web_search": {
        "description": "Search the web for general information",
        "parameters": ["query", "num_results"],
        "function": get_search_snippets
    },
    "get_travel_time": {
        "description": "Calculate travel time from one location to another",
        "parameters": ["from_address", "to_address", "mode"],
        "function": None  # To be implemented
    },
    "get_travel_cost": {
        "description": "Get the cost of traveling between two locations (tube, bus, train, etc.)",
        "parameters": ["from_address", "to_address", "mode"],
        "function": None  # To be implemented
    },
    "get_area_info": {
        "description": "Get general information about an area (neighborhood, cost of living, etc.)",
        "parameters": ["address", "info_type"],
        "function": None  # To be implemented
    }
}


def build_tools_description() -> str:
    """Build a description of available tools for the LLM"""
    tools_text = "Available Tools:\n\n"
    for tool_name, tool_info in AVAILABLE_TOOLS.items():
        params = ", ".join(tool_info["parameters"])
        tools_text += f"- {tool_name}({params}): {tool_info['description']}\n"
    return tools_text


def agent_chat(user_message: str, property_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main agent function that uses LLM to decide which tools to use
    
    Args:
        user_message: The user's query
        property_context: Information about the property (address, price, etc.)
    
    Returns:
        Dictionary with response, tools_used, and tool_results
    """
    
    print("\n" + "="*70)
    print("[AGENT] Processing user query with flexible tool system")
    print("="*70)
    print(f"User: {user_message}")
    print(f"Property: {property_context.get('address', 'Unknown')}")
    
    # Step 1: LLM analyzes query and decides which tools to use
    tools_decision = _get_tool_decisions(user_message, property_context)
    
    if not tools_decision or not tools_decision.get('tools_needed'):
        print("[AGENT] No tools needed - answering directly")
        return {
            "response": "I can help with that. Could you provide more details?",
            "tools_used": [],
            "data": {}
        }
    
    # Step 2: Execute the tools LLM decided to use
    tool_results = _execute_tools(tools_decision['tools_needed'], property_context)
    
    print(f"\n[AGENT] Tools executed: {list(tool_results.keys())}")
    
    # Step 3: LLM synthesizes the answer based on tool results
    final_response = _synthesize_answer(user_message, tool_results, property_context)
    
    return {
        "response": final_response,
        "tools_used": list(tool_results.keys()),
        "data": tool_results,
        "reasoning": tools_decision.get('reasoning', '')
    }


def _get_tool_decisions(user_message: str, property_context: Dict) -> Dict:
    """
    Ask LLM to decide which tools to use for this query
    """
    
    tools_desc = build_tools_description()
    
    system_prompt = """You are a helpful UK rental assistant with access to various tools.

Analyze the user's query and decide which tools you need to answer it properly.
Return your decision in JSON format ONLY.

""" + tools_desc + """

IMPORTANT: Return ONLY valid JSON, no other text.

Example format:
{
    "analysis": "The user is asking for...",
    "tools_needed": [
        {
            "name": "tool_name",
            "parameters": {
                "key1": "value1",
                "key2": "value2"
            }
        }
    ],
    "reasoning": "I chose these tools because..."
}"""

    context = f"""
Property Information:
- Address: {property_context.get('address', 'Unknown')}
- Price: {property_context.get('price', 'N/A')}
- Travel Time to Destination: {property_context.get('travel_time', 'N/A')} minutes
- Description: {property_context.get('description', 'N/A')}

User Query: {user_message}
"""

    print(f"\n[AGENT-STEP1] Asking LLM to decide which tools to use...")
    
    response = call_ollama(context, system_prompt, timeout=30)
    
    if not response:
        print("[AGENT-STEP1] LLM call failed")
        return None
    
    # Extract JSON from response
    try:
        # Try to find JSON in the response
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            tools_decision = json.loads(json_match.group())
            print(f"[AGENT-STEP1] LLM decided to use: {[t['name'] for t in tools_decision.get('tools_needed', [])]}")
            return tools_decision
        else:
            print("[AGENT-STEP1] No JSON found in response")
            return None
    except json.JSONDecodeError as e:
        print(f"[AGENT-STEP1] JSON parsing error: {e}")
        return None


def _execute_tools(tool_calls: List[Dict], property_context: Dict) -> Dict[str, Any]:
    """
    Execute the tools that LLM decided to use
    """
    
    results = {}
    
    for tool_call in tool_calls:
        tool_name = tool_call.get('name')
        params = tool_call.get('parameters', {})
        
        print(f"[AGENT-STEP2] Executing tool: {tool_name} with params: {params}")
        
        try:
            if tool_name == "get_nearby_places_osm":
                result = get_nearby_places_osm(
                    params.get('address', property_context.get('address')),
                    params.get('amenity_type', 'gym'),
                    params.get('radius_m', 1500)
                )
                results[tool_name] = result
                
            elif tool_name == "get_crime_data":
                result = get_crime_data_by_location(
                    params.get('address', property_context.get('address'))
                )
                results[tool_name] = result
                
            elif tool_name == "web_search":
                result = get_search_snippets(
                    params.get('query'),
                    params.get('num_results', 3)
                )
                results[tool_name] = result
                
            elif tool_name == "get_travel_time":
                # Placeholder - to be implemented
                results[tool_name] = {
                    "status": "not_implemented",
                    "note": "Travel time calculation coming soon"
                }
                
            elif tool_name == "get_travel_cost":
                # Placeholder - to be implemented
                results[tool_name] = {
                    "status": "not_implemented",
                    "note": "Travel cost lookup coming soon"
                }
                
            elif tool_name == "get_area_info":
                # Placeholder - to be implemented
                results[tool_name] = {
                    "status": "not_implemented",
                    "note": "Area info coming soon"
                }
            else:
                print(f"[AGENT-STEP2] Unknown tool: {tool_name}")
                
        except Exception as e:
            print(f"[AGENT-STEP2] Error executing {tool_name}: {e}")
            results[tool_name] = {"error": str(e)}
    
    return results


def _synthesize_answer(user_message: str, tool_results: Dict, property_context: Dict) -> str:
    """
    LLM synthesizes the final answer based on tool results
    """
    
    print(f"\n[AGENT-STEP3] LLM synthesizing final answer...")
    
    # Format tool results for LLM
    results_text = "Tool Results:\n"
    for tool_name, result in tool_results.items():
        if isinstance(result, list):
            if result:
                results_text += f"\n{tool_name}:\n"
                for item in result[:5]:  # Show first 5 items
                    if isinstance(item, dict):
                        results_text += f"  - {item.get('name', 'Unknown')}: {item.get('distance_m', 'N/A')}m away\n"
                    else:
                        results_text += f"  - {item}\n"
            else:
                results_text += f"{tool_name}: No results found\n"
        elif isinstance(result, dict):
            results_text += f"\n{tool_name}:\n"
            for key, value in result.items():
                results_text += f"  {key}: {value}\n"
        else:
            results_text += f"{tool_name}: {result}\n"
    
    synthesis_prompt = f"""Based on the tool results below, provide a helpful answer to the user's question.

{results_text}

User's Original Question: {user_message}
Property: {property_context.get('address', 'Unknown')}

Instructions:
1. Use ONLY the information from the tool results
2. Do NOT make up any information
3. If a tool returned no results or not implemented, be honest about it
4. Provide helpful suggestions based on what data IS available
5. Keep the response natural and conversational"""
    
    answer = call_ollama(synthesis_prompt, timeout=30)
    
    print(f"[AGENT-STEP3] Answer synthesized")
    
    return answer if answer else "I apologize, but I couldn't process that query properly."


# Export
__all__ = ['agent_chat', 'AVAILABLE_TOOLS']
