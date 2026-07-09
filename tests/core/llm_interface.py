# ollama_interface.py - COMPLETE UPDATED VERSION

import json
import re
import requests

OLLAMA_BASE_URL = "http://localhost:11434"
MODEL_NAME = "llama3.2:1b"  # Change to your model (qwen2.5:1.5b, llama3.2:3b, etc.)

USE_FINETUNED_MODEL = True
# ========================================
FINETUNED_BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"  # or path to Ollama's download
FINETUNED_ADAPTER_PATH = "./student_model_lora/"     # Your LoRA adapters directory
# ========================================
  # Default model if not using fine-tuned


def call_ollama(prompt: str, system_prompt: str = None, timeout: int = 6000) -> str:
    """Call Ollama with better defaults"""
    url = f"{OLLAMA_BASE_URL}/api/generate"
    
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "top_p": 0.9,
            "num_predict": 4000,
            "num_ctx": 8192,
        }
    }
    
    if system_prompt:
        payload["system"] = system_prompt
    
    # DEBUG: Print what we're sending
    print(f"[DEBUG] Ollama URL: {url}")
    print(f"[DEBUG] Model: {MODEL_NAME}")
    print(f"[DEBUG] Prompt length: {len(prompt)} chars")
    print(f"[DEBUG] Has system prompt: {system_prompt is not None}")
    
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        
        # DEBUG: Print response status
        print(f"[DEBUG] Response status: {response.status_code}")
        
        response.raise_for_status()  # This line throws the 404 error
        result = response.json()
        return result.get("response", "")
    except requests.exceptions.HTTPError as e:
        print(f"❌ Ollama HTTP error: {e}")
        print(f"[DEBUG] Response text: {response.text[:500]}")
        return None
    except requests.exceptions.Timeout:
        print(f"⚠️  Ollama timeout after {timeout}s")
        return None
    except Exception as e:
        print(f"❌ Ollama API error: {e}")
        return None

def extract_first_json(text: str) -> dict | None:
    """Extracts the first valid JSON object from a string"""
    if not text:
        return None
    
    try:
        cleaned_text = text.strip()
        return json.loads(cleaned_text)
    except (json.JSONDecodeError, TypeError):
        pass
    
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    
    match = re.search(r'`\s*(\{.*?\})\s*`', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    
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
                    json_str = text[start_idx:i+1]
                    json_str = json_str.replace('\n', ' ').replace('\r', '')
                    parsed = json.loads(json_str)
                    
                    if isinstance(parsed, dict) and len(parsed) > 0:
                        if "$schema" not in parsed and "properties" not in parsed:
                            return parsed
                except json.JSONDecodeError:
                    pass
                finally:
                    start_idx = -1
    
    return None

def retry_with_simple_prompt(user_query: str) -> dict:
    """Ultra-simple prompt for stubborn models"""
    
    prompt = f"""Extract these values from the user's request and return ONLY a JSON object (no explanation):

User request: "{user_query}"

{{
  "status": "success",
  "destination": "",
  "max_budget": 0,
  "max_travel_time": 0,
  "soft_preferences": "",
  "city_context": "London",
  "suggested_search_locations": [],
  "amenities_of_interest": [],
  "area_vibe": ""
}}

Fill in the values. Return ONLY the JSON object, nothing else."""

    response_text = call_ollama(prompt, timeout=60000)
    
    if response_text:
        parsed = extract_first_json(response_text)
        if parsed and "$schema" not in parsed:
            return parsed
    
    return {
        "status": "clarification_needed",
        "data": {
            "question": "Could you specify your destination, budget, and maximum commute time?"
        }
    }

def clarify_and_extract_criteria(user_query: str) -> dict:
    """Extract criteria from user query - now supports fine-tuned model"""
    
    
    if USE_FINETUNED_MODEL:
        try:
            print("[INFO] Attempting to use fine-tuned model...")
            from finetuned_parser import get_finetuned_parser
            
            parser = get_finetuned_parser(FINETUNED_BASE_MODEL, FINETUNED_ADAPTER_PATH)
            result = parser.parse_query(user_query)
            
            print("[INFO] ✓ Used fine-tuned model for parsing")
            print(f"[INFO] Fine-tuned model result: status={result.get('status')}")
            
            # Validate result has required fields
            if result.get('status') == 'success':
                required = ['destination', 'max_budget', 'max_travel_time']
                if all(result.get(field) for field in required):
                    # Fill in default values for missing optional fields
                    if not result.get('suggested_search_locations'):
                        result['suggested_search_locations'] = []
                    if not result.get('area_vibe'):
                        result['area_vibe'] = None
                    if not result.get('amenities_of_interest'):
                        result['amenities_of_interest'] = []
                    
                    print(f"[INFO] ✓ All required fields present, returning fine-tuned model result")
                    return result
                else:
                    missing = [f for f in required if not result.get(f)]
                    print(f"[WARN] Fine-tuned model missing required fields: {missing}, falling back to Ollama")
            elif result.get('status') == 'clarification_needed':
                # Fill in default values for optional fields
                if not result.get('suggested_search_locations'):
                    result['suggested_search_locations'] = []
                if not result.get('area_vibe'):
                    result['area_vibe'] = None
                if not result.get('amenities_of_interest'):
                    result['amenities_of_interest'] = []
                
                # Ensure clarification questions are specific and helpful
                if not result.get('data') or 'question' not in result.get('data', {}):
                    destination = result.get('destination', 'not specified')
                    budget = result.get('max_budget', 'not specified')
                    travel_time = result.get('max_travel_time', 'not specified')
                    
                    clarification_msg = "I've got some initial info: "
                    missing_items = []
                    
                    if not destination or destination == 'not specified':
                        missing_items.append("a specific location or destination")
                    if not budget or budget == 'not specified':
                        missing_items.append("your budget range")
                    if not travel_time or travel_time == 'not specified':
                        missing_items.append("your preferred commute time")
                    
                    if missing_items:
                        clarification_msg += f"Could you clarify: {', '.join(missing_items)}?"
                    else:
                        clarification_msg = f"You mentioned: {destination} area, £{budget}/month, {travel_time} min commute. Anything else important (safety concerns, amenities, quiet area)?"
                    
                    result['data'] = {'question': clarification_msg}
                
                print(f"[INFO] ✓ Fine-tuned model returned clarification_needed")
                print(f"[INFO] Clarification: {result['data']['question']}")
                return result
            elif result.get('status') == 'error':
                print(f"[WARN] Fine-tuned model returned error: {result.get('data', {}).get('message')}")
                print("[INFO] Falling back to Ollama")
            
        except ImportError as e:
            print(f"[ERROR] Could not import finetuned_parser: {e}")
            print("[INFO] Falling back to Ollama")
        except Exception as e:
            print(f"[ERROR] Fine-tuned model failed: {e}")
            import traceback
            traceback.print_exc()
            print("[INFO] Falling back to Ollama")
    else:
        print("[INFO] Using Ollama (USE_FINETUNED_MODEL = False)")
    
    # EXISTING: Original Ollama-based parsing (fallback)
    system_prompt = """You are a JSON extraction tool. You MUST return ONLY valid JSON, no explanations.
Extract UK rental search criteria from user requests.
If a specific place is mentioned (e.g., 'UCL', 'King's Cross'), use it as the destination.
If travel time is 'unlimited' or 'any', set max_travel_time to 999.
IMPORTANT: Extract any specific concerns or preferences the user mentions (safety, crime, noise, quiet, modern, etc.) into soft_preferences."""

    prompt = f"""USER REQUEST: "{user_query}"

YOUR TASK: Extract rental criteria and return ONLY the JSON below (NO explanations, NO markdown, NO backticks):

{{
  "status": "success",
  "destination": "",
  "max_budget": 0,
  "max_travel_time": 0,
  "soft_preferences": "",
  "property_tags": [],
  "amenities_of_interest": [],
  "area_vibe": "",
  "suggested_search_locations": [],
  "city_context": "London"
}}

RULES:
1. destination: Be specific (e.g., "University College London" not just "London")
2. max_budget: Extract numeric value (e.g., 5000 for "£5000/month")
3. max_travel_time: Extract minutes ONLY. "40 min" = 40, "1 hour" = 60, "90 minutes" = 90
4. If unlimited travel time, set to 999
5. suggested_search_locations: List nearby areas for the destination
6. soft_preferences: Extract SPECIFIC user concerns like "concerned about crime", "want safe area", "need quiet location", "prefer modern", etc. This is IMPORTANT!
7. CRITICAL: Return ONLY the completed JSON object, nothing else

JSON OUTPUT:"""

    response_text = call_ollama(prompt, system_prompt, timeout=6000)
    
    if not response_text:
        print("[ERROR] Ollama timeout")
        return {"status": "error", "data": {"message": "Ollama timeout"}}
    
    print(f"[DEBUG] Raw Ollama response length: {len(response_text)} chars")
    print(f"[DEBUG] First 300 chars: {response_text[:300]}")
    
    parsed_json = extract_first_json(response_text)
    
    if parsed_json:
        if "$schema" in parsed_json or "properties" in parsed_json:
            print("[WARN] Got schema instead of data, retrying with simple prompt")
            return retry_with_simple_prompt(user_query)
        
        required = ['destination', 'max_budget', 'max_travel_time']
        has_required = all(parsed_json.get(field) for field in required)
        
        if has_required:
            if not parsed_json.get('soft_preferences'):
                query_lower = user_query.lower()
                concerns = []
                if 'crime' in query_lower or 'safe' in query_lower:
                    concerns.append('safety and crime rates')
                if 'quiet' in query_lower:
                    concerns.append('quiet area')
                if 'modern' in query_lower or 'new' in query_lower:
                    concerns.append('modern property')
                if 'park' in query_lower or 'green' in query_lower:
                    concerns.append('access to parks')
                
                if concerns:
                    parsed_json['soft_preferences'] = ', '.join(concerns)
            
            print("[SUCCESS] Extracted valid criteria")
            return parsed_json
        else:
            print(f"[WARN] Missing required fields, retrying")
            return retry_with_simple_prompt(user_query)
    else:
        print("[ERROR] Could not parse JSON from Ollama response")
        return retry_with_simple_prompt(user_query)

def refine_criteria_with_answer(original_criteria: dict, user_answer: str) -> dict:
    """
    Refine criteria with additional user input.
    ✅ FIXED: 现在正确处理澄清回复
    
    关键改变:
    1. 如果原始条件有所有必需字段，直接合并答案到 soft_preferences
    2. 返回 status='success' 而不是继续澄清
    3. 处理否定回复（用户说 'nope'）
    """
    required_fields = ['destination', 'max_budget', 'max_travel_time']
    has_all_required = all(original_criteria.get(field) for field in required_fields)
    
    print(f"\n{'='*60}")
    print(f"[Refine] 细化搜索条件")
    print(f"[Refine] 用户回复: '{user_answer}'")
    print(f"[Refine] 原始条件有所有必需字段: {has_all_required}")
    print(f"{'='*60}")
    
    if has_all_required:
        # ✅ 已有所有必需字段 - 直接合并用户回复到 soft_preferences
        print("[Refine] ✓ 合并回复到现有条件（不需要重新解析）")
        
        # 检查是否是否定回复
        answer_lower = user_answer.lower().strip()
        is_negative = any(phrase in answer_lower for phrase in [
            'no, i do not', 'no i do not', "no, don't", "no don't", 
            'nope', 'not really', 'nothing else', 'nothing more',
            'no thanks', 'none of that', 'no worries', 'nothing'
        ])
        
        if is_negative:
            print("[Refine] ✓ 用户拒绝了进一步的偏好 - 清除不必要的偏好")
            # ✅ FIXED: 当用户说"nope"时，清除所有可选的软偏好
            # 只保留原始查询中明确提到的硬要求
            original_criteria['soft_preferences'] = ""
            # 确保返回 success 状态
            if original_criteria.get('status') != 'success':
                original_criteria['status'] = 'success'
            print("[Refine] ✓ 软偏好已清除，将使用默认搜索策略")
            return original_criteria
        
        # 从回复中提取新的偏好
        answer_lower = user_answer.lower()
        new_preferences = []
        
        if any(word in answer_lower for word in ['quiet', 'peaceful', 'noise', 'silent', 'tranquil']):
            new_preferences.append('quiet area')
        if any(word in answer_lower for word in ['safe', 'security', 'crime', 'secure']):
            new_preferences.append('safety and crime rates')
        if any(word in answer_lower for word in ['park', 'green', 'garden', 'nature', 'outdoor']):
            new_preferences.append('access to parks and green spaces')
        if any(word in answer_lower for word in ['modern', 'new', 'renovated', 'contemporary']):
            new_preferences.append('modern property')
        if any(word in answer_lower for word in ['gym', 'fitness', 'sport', 'exercise']):
            new_preferences.append('fitness facilities nearby')
        if any(word in answer_lower for word in ['restaurant', 'cafe', 'dining', 'food', 'bar']):
            new_preferences.append('good dining options')
        if any(word in answer_lower for word in ['shop', 'store', 'supermarket', 'grocery']):
            new_preferences.append('shopping and supermarkets nearby')
        if any(word in answer_lower for word in ['school', 'education', 'university']):
            new_preferences.append('good schools and education')
        if any(word in answer_lower for word in ['transport', 'transit', 'bus', 'tube', 'train']):
            new_preferences.append('good public transport')
        
        # ✅ FIXED: 仅当用户主动提到新偏好时才添加，否则清除
        if new_preferences:
            # 仅保留用户明确提到的新偏好，移除初始查询中自动提取的内容
            original_criteria['soft_preferences'] = ', '.join(new_preferences)
            print(f"[Refine] ✓ 基于用户回复的偏好: {', '.join(new_preferences)}")
        else:
            # 用户没有提到新偏好，并且已经否定了澄清问题中的建议
            original_criteria['soft_preferences'] = ""
            print(f"[Refine] ℹ 用户回复中没有提到具体偏好，清除软偏好")
        
        # ✅ 确保返回 success 状态
        original_criteria['status'] = 'success'
        
        print(f"[Refine] ✓ 最终条件: {json.dumps(original_criteria, indent=2)}")
        return original_criteria
    
    else:
        # ❌ 缺少必需字段 - 需要重新解析结合两个输入
        print("[Refine] ✗ 缺少必需字段 - 重新解析组合输入")
        
        # ✅ 首先检查是否是否定回复（即使字段不完整）
        answer_lower = user_answer.lower().strip()
        is_negative = any(phrase in answer_lower for phrase in [
            'no, i do not', 'no i do not', "no, don't", "no don't", 
            'nope', 'not really', 'nothing else', 'nothing more',
            'no thanks', 'none of that', 'no worries', 'nothing',
            'i do not care about anything else'  # 添加这个特定的否定模式
        ])
        
        if is_negative:
            print("[Refine] ✓ 检测到否定回复 - 尝试从回复中提取必需字段")
            
            # ✅ 尝试从用户回复中提取关键信息
            import re
            
            # 提取旅行时间 - 优先查找"min"或"minutes"相关的数字
            if not original_criteria.get('max_travel_time'):
                time_match = re.search(r'(\d+)\s*(?:min|minutes|mins)', answer_lower)
                if time_match:
                    max_travel = int(time_match.group(1))
                    original_criteria['max_travel_time'] = max_travel
                    print(f"[Refine] ✓ 从回复中提取旅行时间: {max_travel} 分钟")
            
            # 提取预算 - 优先查找带货币符号的金额
            if not original_criteria.get('max_budget'):
                # 首先查找 £ 符号
                budget_match = re.search(r'£\s*(\d+(?:,\d{3})*)', answer_lower)
                if budget_match:
                    budget_str = budget_match.group(1).replace(',', '')
                    max_budget = int(budget_str)
                    original_criteria['max_budget'] = max_budget
                    print(f"[Refine] ✓ 从回复中提取预算: £{max_budget}")
            
            # 清除软偏好
            original_criteria['soft_preferences'] = ""
            original_criteria['status'] = 'success'
            print("[Refine] ✓ 软偏好已清除")
            
            # 检查是否仍然缺少字段
            required_fields = ['destination', 'max_budget', 'max_travel_time']
            still_missing = [f for f in required_fields if not original_criteria.get(f)]
            if still_missing:
                print(f"[Refine] ⚠️  仍缺少字段: {still_missing}")
                print(f"[Refine] ℹ️  将使用现有值进行搜索")
            
            print(f"[Refine] ✓ 最终条件: {json.dumps(original_criteria, indent=2)}")
            return original_criteria
        
        # 重新解析组合查询
        combined_query = f"用户首先说: '{original_criteria.get('_original_query', 'N/A')}'. 然后补充说: '{user_answer}'."
        print(f"[Refine] 组合查询: {combined_query}")
        
        result = clarify_and_extract_criteria(combined_query)
        
        # 如果新解析也失败，保留原始数据
        if result.get('status') != 'success' and original_criteria:
            for field in required_fields:
                if not result.get(field) and original_criteria.get(field):
                    result[field] = original_criteria[field]
        
        return result

def _get_property_url(prop: dict) -> str:
    """Helper to get URL from property dict"""
    for key in ['URL', 'url', 'Url', 'link', 'Link']:
        if key in prop and prop[key]:
            return prop[key]
    return ''

def _validate_and_fix_price_in_explanations(recommendations: dict, properties_data: list[dict]) -> dict:
    """
    Validates that prices in explanations match actual property prices.
    Fixes any mismatches found.
    """
    if not recommendations or 'recommendations' not in recommendations:
        return recommendations
    
    print("\n[PRICE VALIDATION] Checking price consistency in explanations...")
    
    for rec in recommendations['recommendations']:
        rank = rec.get('rank', 0)
        address = rec.get('address', '').lower().strip()
        stated_price = rec.get('price', 'N/A')
        explanation = rec.get('explanation', '')
        
        # Find the original property
        original_prop = None
        for prop in properties_data:
            prop_address = prop.get('Address', '').lower().strip()
            if address[:30] in prop_address or prop_address[:30] in address:
                original_prop = prop
                break
        
        if not original_prop and 1 <= rank <= len(properties_data):
            original_prop = properties_data[rank - 1]
        
        if original_prop:
            actual_price = original_prop.get('Price', 'N/A')
            
            # ✅ FIXED: 改进价格修复逻辑以处理所有价格格式变体
            import re
            price_pattern = r'£\d+(?:,\d{3})*(?:\.\d{2})?'
            prices_in_explanation = re.findall(price_pattern, explanation)
            
            # 规范化实际价格用于比较
            actual_price_normalized = str(actual_price).replace(' pcm', '').strip()
            
            if prices_in_explanation:
                found_price = prices_in_explanation[0]
                # 检查是否存在不匹配
                if found_price != actual_price_normalized:
                    print(f"  ⚠️ PRICE MISMATCH found in Rank {rank}:")
                    print(f"     Stated in header: {stated_price}")
                    print(f"     Actual property: {actual_price}")
                    print(f"     Found in explanation: {found_price}")
                    
                    # ✅ 改进: 更激进的替换 - 替换所有发现的价格
                    explanation_fixed = rec['explanation']
                    for wrong_price in prices_in_explanation:
                        # 尝试多种替换模式
                        patterns = [
                            f"At {wrong_price}",
                            f"at {wrong_price}",
                            f"At {wrong_price} pcm",
                            f"at {wrong_price} pcm",
                            wrong_price,  # 直接替换任何地方的价格
                        ]
                        for pattern in patterns:
                            if pattern in explanation_fixed:
                                replacement = actual_price if "pcm" not in pattern else f"{actual_price}"
                                explanation_fixed = explanation_fixed.replace(pattern, replacement)
                                print(f"     📝 Replaced '{pattern}' with '{replacement}'")
                    
                    rec['explanation'] = explanation_fixed
                    print(f"     ✓ Fixed explanation to use: {actual_price}")
                else:
                    print(f"  ✓ Rank {rank}: Price consistent - {actual_price}")
            else:
                print(f"  ✓ Rank {rank}: Price consistent - {actual_price}")
    
    return recommendations

def generate_recommendations(properties_data: list[dict], user_query: str, soft_preferences: str) -> dict | None:
    """Generate personalized property recommendations with natural explanations"""

    print(f"\n🤖 [RECOMMENDATION ENGINE] Starting...")
    print(f"   Properties to analyze: {len(properties_data)}")

    if not properties_data:
        return {"recommendations": []}

    top_props = properties_data[:5]
    
    # Prepare property data for the model
    simple_props = []
    
    # ✅ FIXED: 先确定应该提到的数据类型
    soft_prefs_lower = soft_preferences.lower() if soft_preferences else ""
    should_include_crime = any(kw in soft_prefs_lower for kw in ['safe', 'crime', 'security', 'dangerous'])
    should_include_amenities = any(kw in soft_prefs_lower for kw in ['supermarket', 'shop', 'park', 'gym', 'restaurant', 'amenities'])
    
    print(f"  -> [Generate] should_include_crime: {should_include_crime}")
    print(f"  -> [Generate] should_include_amenities: {should_include_amenities}")
    
    for i, prop in enumerate(top_props):
        url = _get_property_url(prop)
        travel_time = prop.get('travel_time_minutes', 'N/A')
        # ✅ DO NOT send images to LLM - images contain placeholder text that can be misinterpreted
        # Only real description field should be used for feature extraction
        
        simple_prop = {
            'id': i + 1,
            'address': prop.get('Address', 'Unknown')[:70],
            'price': prop.get('Price', 'N/A'),
            'price_numeric': prop.get('parsed_price', 0),
            'url': url,
            'travel_time_minutes': travel_time,
            'description': prop.get('Description', '')[:200]
            # NOTE: images field intentionally omitted to prevent LLM from using placeholder text as features
        }
        
        # ✅ 只在用户关心时才添加 crime data
        if should_include_crime and 'crime_data_summary' in prop:
            crime_data = prop.get('crime_data_summary', {})
            simple_prop['crimes_6m'] = crime_data.get('total_crimes_6m', 0)
            simple_prop['crime_trend'] = crime_data.get('crime_trend', 'unknown')
            simple_prop['top_crime_types'] = crime_data.get('top_crime_types', [])[:2]
        else:
            # ✅ 明确设置为 None，表示不相关
            simple_prop['crimes_6m'] = None
            simple_prop['crime_trend'] = None
            simple_prop['top_crime_types'] = []
        
        # ✅ 只在用户关心时才添加 amenities data
        if should_include_amenities and 'amenities_nearby' in prop:
            amenities = prop.get('amenities_nearby', {})
            simple_prop['nearby_supermarkets'] = amenities.get('supermarket_in_1500m', 0)
            simple_prop['nearby_parks'] = amenities.get('park_in_1500m', 0)
            simple_prop['nearby_gyms'] = amenities.get('gym_in_1500m', 0)
        else:
            # ✅ 明确设置为 None
            simple_prop['nearby_supermarkets'] = None
            simple_prop['nearby_parks'] = None
            simple_prop['nearby_gyms'] = None
        
        simple_props.append(simple_prop)

    system_prompt = """You are Alex, a friendly and knowledgeable London rental assistant with years of experience helping people find their perfect home. 

Your task is to write engaging, personalized property recommendations that feel like advice from a trusted friend who really understands the London rental market.

CRITICAL RULES:
1. Write in a warm, conversational tone - like you're talking to a friend
2. Tell a story about each property - don't just list facts
3. Compare properties naturally (e.g., "While Property 1 is closer, Property 2 offers better value...")
4. Be honest about downsides (high crime, expensive, etc.) but frame them constructively
5. Use specific numbers to back up your points, but weave them into the narrative
6. Consider the user's priorities and explain WHY each property matches or doesn't match
7. Each explanation should be 3-5 sentences, not just one sentence of facts
8. ⚠️ CRITICAL: ONLY mention aspects the user cares about (from their priorities). DO NOT mention crime/safety if user didn't ask about it!"""

    # ✅ FIXED: Extract key user concerns AND create exclusion list
    user_concerns = []
    data_to_mention = set()  # 跟踪应该提到的数据类型
    
    if soft_preferences:
        sp_lower = soft_preferences.lower()
        if 'crime' in sp_lower or 'safe' in sp_lower or 'security' in sp_lower:
            user_concerns.append("safety and low crime")
            data_to_mention.add("crime")
        if 'quiet' in sp_lower or 'noise' in sp_lower:
            user_concerns.append("a quiet neighborhood")
        if 'modern' in sp_lower or 'new' in sp_lower:
            user_concerns.append("modern amenities")
        if 'pet' in sp_lower or 'dog' in sp_lower or 'cat' in sp_lower:
            user_concerns.append("pet-friendly properties")
        if any(kw in sp_lower for kw in ['supermarket', 'shop', 'park', 'gym', 'amenities']):
            user_concerns.append("nearby amenities")
            data_to_mention.add("amenities")

    concerns_text = ", ".join(user_concerns) if user_concerns else "good value and convenience"
    
    # ✅ 创建明确的指示，告诉 LLM 应该/不应该提到什么
    data_guidance = ""
    if data_to_mention:
        data_guidance = f"\n\n✅ User DOES care about: {', '.join(data_to_mention)}. You SHOULD mention these aspects."
    
    # 明确列出不应该提到的内容
    excluded_topics = []
    if "crime" not in data_to_mention:
        excluded_topics.append("crime/safety statistics")
    if "amenities" not in data_to_mention:
        excluded_topics.append("nearby amenities")
    
    if excluded_topics:
        data_guidance += f"\n\n🚫 User did NOT ask about: {', '.join(excluded_topics)}. DO NOT mention these in your explanations."

    # ✅ FIXED: 添加预算信息用于超预算解释
    max_budget = properties_data[0].get('_max_budget', None) if properties_data else None
    budget_section = ""
    if max_budget:
        budget_section = f"""
User's budget limit: £{max_budget} per month

Note: Some properties listed below may exceed the budget. For any property above £{max_budget}:
- Explain why it's worth considering despite the overage (e.g., shorter commute, better value score, unique features)
- State the overage amount and percentage clearly (e.g., "£150 above budget (7.5% over)")
- Compare it to on-budget alternatives if applicable"""

    prompt = f"""The user is searching for a London rental with these priorities: {concerns_text}.
Their original query: "{user_query}"
{budget_section}
{data_guidance}

Here are the top properties that match their criteria:

{json.dumps(simple_props, indent=2)}

YOUR TASK:
Recommend the TOP 3-5 properties. For each one, write a natural, engaging explanation that:
- Starts with why this property stands out
- Discusses the commute USING ONLY the "travel_time_minutes" field - this is the ACTUAL verified commute time
  ✅ Use: "travel_time_minutes": 36 → Say "36-minute commute"
  ❌ Do NOT make up different travel times like "2 minutes" or "45 minutes" 
  ❌ Do NOT mention destinations or specific locations that would imply different times
- ⚠️ ONLY discuss safety/crime if the "crimes_6m" field is NOT null/null in the data
- Mentions value for money (is it a good deal for the area?)
- ⚠️ ONLY mention amenities if the "nearby_supermarkets" field is NOT null
- Notes any standout features (from description ONLY)
- Ends with who this property is perfect for
- ⚠️ IF property is above budget: explicitly mention the overage and why it might be worth it

🔴 CRITICAL RULES - NO FABRICATION ALLOWED:
1. Use ONLY the ACTUAL data provided - NO FABRICATION ALLOWED
   ❌ Do NOT invent amenities like "pet-friendly", "modern", "student-friendly", "newly renovated"
   ❌ Do NOT make up features that aren't in the description or data fields
   ❌ Do NOT invent travel times - USE travel_time_minutes field exactly
   ✅ ONLY mention features that are explicitly in the "description" field
   ✅ ONLY mention amenities that are in the provided amenity fields (nearby_supermarkets, nearby_parks, nearby_gyms, etc.)

2. Do NOT make up prices - use actual prices from property data

3. If a field is null, missing, or empty string, DO NOT mention it
   ✅ If the description is just "2 bedroom flat", mention the bedrooms - that's factual
   ❌ Do NOT add "it's perfect for students" or "modern amenities" if not in description

4. Property description is the ONLY source for physical features
   - If description says "2 bedroom flat" → mention "2 bedrooms"
   - If description says "Studio flat" → mention "studio"
   - If description says "1 bedroom flat" → mention "1 bedroom", NOT "modern kitchen" or "balcony"
   - If description is vague/short → focus on commute, price, location instead

5. Each explanation should be 3-5 sentences, but ONLY if you have real data to mention

Example patterns (with actual data only):
- Simple property: "This [description] in [AREA] has a [COMMUTE]-minute commute to UCL. At [PRICE], you're getting competitive value. This would suit someone prioritizing [user_priority]."
- With amenities data: "Great news - there are [COUNT] gyms nearby within walking distance! Plus the [COMMUTE]-minute commute is quick."
- With crime data: "The area has seen [CRIME COUNT] reported crimes over the past 6 months with [TREND]."
- Without features: Focus on commute and price: "Quick [COMMUTE]-minute commute at good value price of [PRICE]."

⚠️ ALWAYS use travel_time_minutes from the data - NEVER fabricate different commute times!

Return ONLY this JSON structure:
{{
  "recommendations": [
    {{
      "rank": 1,
      "address": "full address from data",
      "price": "£X pcm",
      "travel_time": "X minutes",
      "explanation": "Your engaging 3-5 sentence explanation using ACTUAL data here",
      "url": "property url"
    }},
    {{
      "rank": 2,
      ...
    }},
    {{
      "rank": 3,
      ...
    }}
  ]
}}

Return ONLY valid JSON, no other text."""

    response_text = call_ollama(prompt, system_prompt, timeout=90)

    if not response_text:
        print("[INFO] Ollama failed, using rule-based recommendations")
        return create_fallback_recommendations(properties_data, soft_preferences)

    parsed = extract_first_json(response_text)

    if parsed and 'recommendations' in parsed:
        print("\n[DEBUG] Fixing travel times and images...")
        
        # Match recommendations back to original properties
        for rec in parsed['recommendations']:
            rank = rec.get('rank', 0)
            rec_address = rec.get('address', '').lower().strip()
            
            # Find matching property
            original_prop = None
            for prop in properties_data:
                prop_address = prop.get('Address', '').lower().strip()
                if rec_address[:30] in prop_address or prop_address[:30] in rec_address:
                    original_prop = prop
                    break
            
            if not original_prop and 1 <= rank <= len(properties_data):
                original_prop = properties_data[rank - 1]
            
            if original_prop:
                tt_mins = original_prop.get('travel_time_minutes')
                rec['travel_time'] = f"{tt_mins} minutes" if isinstance(tt_mins, (int, float)) else "N/A"
                rec['images'] = original_prop.get('Images', [])
                rec['url'] = original_prop.get('URL', rec.get('url', ''))
                rec['address'] = original_prop.get('Address', rec.get('address', ''))
                
                actual_price = original_prop.get('Price', 'N/A')
                rec['price'] = actual_price
                
                print(f"  ✓ Rank {rank}: {rec['address'][:40]} - {rec['travel_time']} - Price: {actual_price}")
        
        parsed = _validate_and_fix_price_in_explanations(parsed, properties_data)
        
        return parsed
    else:
        print("[WARN] Could not parse JSON, using fallback")
        return create_fallback_recommendations(properties_data, soft_preferences)

def create_fallback_recommendations(properties_data: list[dict], soft_preferences: str = "") -> dict:
    """
    ✅ FIXED: High-quality fallback with natural explanations
    现在也遵循条件化逻辑 - 只在用户关心时才提到相关数据
    """
    print("   🔧 Creating intelligent rule-based recommendations...")
    
    sorted_props = sorted(
        properties_data[:15],
        key=lambda x: (
            x.get('travel_time_minutes', 999),
            x.get('parsed_price', 9999)
        )
    )
    
    # ✅ 根据 soft_preferences 决定要提到哪些方面
    soft_prefs_lower = soft_preferences.lower() if soft_preferences else ""
    user_cares_about_crime = any(kw in soft_prefs_lower for kw in ['crime', 'safe', 'security', 'dangerous'])
    user_cares_about_amenities = any(kw in soft_prefs_lower for kw in ['supermarket', 'shop', 'park', 'gym', 'restaurant', 'amenities'])
    
    recommendations = []
    for i, prop in enumerate(sorted_props[:5]):
        travel_time = prop.get('travel_time_minutes', 'N/A')
        price = prop.get('Price', 'N/A')
        parsed_price = prop.get('parsed_price', 0)
        address = prop.get('Address', 'Unknown')
        description = prop.get('Description', '')
        
        # ✅ FIXED: 只在用户关心时才提取 crime data
        crime_data = prop.get('crime_data_summary', {}) if user_cares_about_crime else {}
        crime_count = crime_data.get('total_crimes_6m', 0) if crime_data else 0
        crime_trend = crime_data.get('crime_trend', 'unknown') if crime_data else 'unknown'
        top_crimes = crime_data.get('top_crime_types', []) if crime_data else []
        
        # Extract area name
        area_parts = address.split(',')
        area = area_parts[1].strip() if len(area_parts) > 1 else "the area"
        
        # Build natural explanation with VARIED templates to avoid repetition
        explanation_parts = []
        
        if i == 0:
            # Top choice - emphasize efficiency
            explanation_parts.append(f"🏆 My top pick! Located in {area}, this property offers a {travel_time}-minute commute - the fastest option in this batch. You'll save significant time compared to other listings.")
        elif i == 1:
            # Second choice - value or convenience angle
            if parsed_price < 1900:
                explanation_parts.append(f"💰 Strong value alternative in {area}: {travel_time}-minute commute at {price} - competitive pricing for the location.")
            else:
                explanation_parts.append(f"✨ Second choice in {area}: {travel_time}-minute commute. Slightly longer than the top option but with potential additional value.")
        elif i == 2:
            # Third choice - highlight different angle
            if travel_time < 30:
                explanation_parts.append(f"⚡ Quick commute option in {area}: Just {travel_time} minutes! This could be a great backup choice if the top options don't work out.")
            else:
                explanation_parts.append(f"🏘️ {area} option: {travel_time}-minute commute. Worth considering if you prioritize this specific area.")
        elif i == 3:
            # Fourth choice
            explanation_parts.append(f"🔍 Another {area} option: {travel_time}-minute commute at {price}. Different pros and cons compared to the above.")
        else:
            # Fifth+ choice
            explanation_parts.append(f"Option #{i+1}: {area} area, {travel_time}-minute commute, {price} per month.")
        
        if isinstance(travel_time, (int, float)) and parsed_price > 0 and travel_time > 0:
            value_score = parsed_price / travel_time
            if value_score < 60:
                explanation_parts.append(f"At {price}, this is exceptional value - you're getting a great location without breaking the bank.")
            elif value_score < 80:
                explanation_parts.append(f"Priced at {price}, it's competitively priced for the area and commute time.")
            else:
                explanation_parts.append(f"At {price}, this is at the premium end for the commute time, but that might reflect the quality or location.")
        else:
            explanation_parts.append(f"Priced at {price}.")
        
        # ✅ FIXED: 只在用户关心安全时才提到 crime data
        if user_cares_about_crime:
            if crime_count > 0:
                if crime_count < 100:
                    explanation_parts.append(f"The neighborhood feels quite safe with only {crime_count} incidents reported over the past 6 months ({crime_trend} trend), which is below average for London.")
                elif crime_count < 200:
                    explanation_parts.append(f"Safety-wise, there were {crime_count} incidents in the area over 6 months ({crime_trend} trend) - about average for a busy London neighborhood.")
                else:
                    explanation_parts.append(f"I should mention that the area has seen {crime_count} incidents in the past 6 months ({crime_trend} trend), which is higher than some other neighborhoods, so security might be something to check when viewing.")
                
                if top_crimes:
                    crime_types = " and ".join(top_crimes[:2]).lower()
                    explanation_parts.append(f"Most incidents were {crime_types}.")
            elif crime_trend != 'unknown':
                explanation_parts.append(f"Great news on safety - no crimes were reported in this immediate area over the past 6 months!")
        
        if description and len(description) > 20:
            desc_lower = description.lower()
            highlights = []
            
            # ✅ ONLY mention features that are explicitly in the description
            # DO NOT invent features like "modern", "pet-friendly", "student-friendly"
            if 'newly renovated' in desc_lower or 'new build' in desc_lower:
                highlights.append("newly renovated")
            elif 'modern' in desc_lower or 'contemporary' in desc_lower:
                highlights.append("modern finish")
            
            if 'garden' in desc_lower:
                highlights.append("private garden")
            if 'balcony' in desc_lower or 'terrace' in desc_lower:
                highlights.append("balcony")
            if 'parking' in desc_lower:
                highlights.append("parking")
            if 'furnished' in desc_lower or 'unfurnished' in desc_lower:
                highlights.append("furnished" if 'furnished' in desc_lower else "unfurnished")
            
            if highlights:
                explanation_parts.append(f"Key features: {', '.join(highlights[:3])}.")
            else:
                # ✅ 如果没有特殊特征，不要硬编造
                pass
        
        if i == 0 and crime_count < 100 and travel_time < 25:
            explanation_parts.append(f"Perfect if you want the best of both worlds - safety and convenience.")
        elif parsed_price < 2000 and travel_time < 30:
            explanation_parts.append(f"Ideal for budget-conscious students who still want a reasonable commute.")
        
        explanation = " ".join(explanation_parts)
        
        recommendations.append({
            'rank': i + 1,
            'address': address,
            'price': price,
            'travel_time': f"{travel_time} minutes" if isinstance(travel_time, (int, float)) else str(travel_time),
            'explanation': explanation,
            'url': _get_property_url(prop),
            'images': prop.get('Images', [])
        })
    
    print(f"   ✓ Created {len(recommendations)} natural recommendations")
    return {'recommendations': recommendations[:3]}