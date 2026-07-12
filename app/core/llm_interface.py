# ollama_interface.py - COMPLETE UPDATED VERSION

import json
import re
import os
import requests

from core.llm_config import (
    LLM_PROVIDER, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
)

OLLAMA_BASE_URL = "http://localhost:11434"
MODEL_NAME = "gemma3:27b-cloud"  # 使用 Ollama 云端模型，更强的推理能力

USE_FINETUNED_MODEL = False  # 关闭本地 LoRA（CPU 太慢）；查询解析改走 DeepSeek（call_ollama）
# ========================================
FINETUNED_BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"  # base model (downloaded from HF Hub on first use)
# Robust absolute path to the trained LoRA adapter (repo_root/fine_tuning/student_model_lora),
# independent of the process working directory.
FINETUNED_ADAPTER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "fine_tuning", "student_model_lora",
)
# ========================================
  # Default model if not using fine-tuned


def _record_deepseek_eval(resp, latency_ms, success, error=None):
    """Emit an offline-eval llm_call event for a raw DeepSeek call.

    Additive; no-op unless RENTCOMPASS_EVAL is active. Covers the memory +
    place-classify calls that bypass ModelRouter. Never logs secrets.
    """
    try:
        from evaluation.metrics import collector

        if not collector.is_active():
            return
        it = ot = cached = None
        usage = getattr(resp, "usage", None) if resp is not None else None
        if usage is not None:
            it = getattr(usage, "prompt_tokens", None)
            ot = getattr(usage, "completion_tokens", None)
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", None)
            if cached is None:
                cached = getattr(usage, "prompt_cache_hit_tokens", None)
        collector.record_llm_call(
            provider="deepseek", model=DEEPSEEK_MODEL, purpose="memory",
            input_tokens=it, output_tokens=ot, cached_tokens=cached,
            latency_ms=latency_ms, success=success, error=error,
        )
    except Exception:
        pass


def _call_deepseek(prompt: str, system_prompt: str = None, timeout: int = 360,
                   temperature: float = 0.1, max_tokens: int = 4000) -> str:
    """Call DeepSeek's OpenAI-compatible chat API. Returns text, or None on error."""
    from openai import OpenAI
    import time as _time
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=timeout)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    _started = _time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        _record_deepseek_eval(resp, (_time.perf_counter() - _started) * 1000, True)
        return resp.choices[0].message.content
    except Exception as e:
        _record_deepseek_eval(None, (_time.perf_counter() - _started) * 1000, False,
                              error=type(e).__name__)
        print(f"❌ DeepSeek API error: {e}")
        return None


def call_ollama(prompt: str, system_prompt: str = None, timeout: int = 360) -> str:
    """Call the configured LLM (DeepSeek API or local Ollama) with sane defaults."""
    if LLM_PROVIDER == "deepseek":
        return _call_deepseek(prompt, system_prompt, timeout)
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


def generate_react_response(prompt: str, timeout: int = 120, temperature: float = 0.2) -> str:
    """
    为 ReAct Agent 生成响应
    使用 Ollama 进行推理
    
    Args:
        prompt: 输入提示
        timeout: 超时时间
        temperature: 温度参数，越高越随机（用于投票时增加多样性）
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,  # 可调节的温度参数
            "top_p": 0.9,
            "num_predict": 4000,  # 增加到 4000，确保长回答不被截断
            "num_ctx": 8192,
            "stop": ["Observation:"]  # 遇到 Observation 就停止
        }
    }
    
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        return result.get("response", "")
    except Exception as e:
        print(f"[ReAct] Ollama error: {e}")
        return ""


def generate_classification_response(prompt: str, timeout: int = 30, temperature: float = 0.7) -> str:
    """
    专门用于工具分类的函数，可调整 temperature（默认较高增加多样性）
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,  # 可调节温度
            "top_p": 0.95,
            "top_k": 40,  # 添加 top_k 增加采样多样性
            "num_predict": 50,  # 只需要短回答
            "num_ctx": 4096,
        }
    }
    
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        return result.get("response", "")
    except Exception as e:
        print(f"[Classification] Ollama error: {e}")
        return ""


class LLMInterface:
    """LLM 接口包装类，供 ReAct Agent 使用"""
    
    def __init__(self):
        self.base_url = OLLAMA_BASE_URL
        self.model = MODEL_NAME
    
    def generate_react_response(self, prompt: str, temperature: float = 0.5) -> str:
        """生成 ReAct 响应
        
        Args:
            prompt: 输入提示
            temperature: 温度参数 (0.0-1.0)
                - 规划阶段推荐 0.7-0.8 (发散思维,挖掘隐藏需求)
                - 生成阶段推荐 0.1-0.2 (严谨准确,减少幻觉)
        """
        return generate_react_response(prompt, temperature=temperature)
    
    def generate_classification_response(self, prompt: str, timeout: int = 30, temperature: float = 0.7) -> str:
        """生成工具分类响应（可指定温度）"""
        return generate_classification_response(prompt, timeout, temperature)

def extract_first_json(text: str) -> dict | None:
    """Extracts the first valid JSON object from a string"""
    if not text:
        print("[JSON PARSER] Empty text received")
        return None
    
    # 尝试1: 直接解析整个文本
    try:
        cleaned_text = text.strip()
        result = json.loads(cleaned_text)
        print(f"[JSON PARSER] ✅ Method 1: Direct parse successful")
        return result
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[JSON PARSER] ❌ Method 1 failed: {str(e)[:100]}")
    
    # 尝试2: 提取```json...```代码块 (改进正则：使用贪婪匹配来获取完整 JSON)
    match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            print(f"[JSON PARSER] ✅ Method 2: Code block parse successful")
            return result
        except json.JSONDecodeError as e:
            print(f"[JSON PARSER] ❌ Method 2 failed: {str(e)[:100]}")
    
    # 尝试3: 提取`{...}`内联代码
    match = re.search(r'`\s*(\{.*?\})\s*`', text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            print(f"[JSON PARSER] ✅ Method 3: Inline code parse successful")
            return result
        except json.JSONDecodeError as e:
            print(f"[JSON PARSER] ❌ Method 3 failed: {str(e)[:100]}")
    
    # 尝试4: 查找第一个完整的JSON对象
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
                            print(f"[JSON PARSER] ✅ Method 4: Brace matching successful")
                            return parsed
                except json.JSONDecodeError as e:
                    print(f"[JSON PARSER] ❌ Method 4 attempt failed: {str(e)[:100]}")
                finally:
                    start_idx = -1
    
    print("[JSON PARSER] ❌ All methods failed")
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

    response_text = call_ollama(prompt, timeout=360)
    
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

def _extract_destination_with_regex(user_query: str) -> str:
    """
    使用正则表达式从查询中提取目的地（作为 Fine-tuned model 的后备）
    """
    import re
    
    query_lower = user_query.lower()
    
    # 常见的位置关键词模式
    patterns = [
        r'near\s+([A-Z][A-Za-z\s]+?)(?:\s+that|\s+under|\s+within|\s*,|\s+cost|$)',
        r'close to\s+([A-Z][A-Za-z\s]+?)(?:\s+that|\s+under|\s+within|\s*,|\s+cost|$)',
        r'around\s+([A-Z][A-Za-z\s]+?)(?:\s+that|\s+under|\s+within|\s*,|\s+cost|$)',
        r'in\s+([A-Z][A-Za-z\s]+?)(?:\s+that|\s+under|\s+within|\s*,|\s+cost|$)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, user_query, re.IGNORECASE)
        if match:
            location = match.group(1).strip()
            # 清理常见的干扰词
            location = re.sub(r'\b(that|under|within|cost|apartment|flat|studio)\b', '', location, flags=re.IGNORECASE).strip()
            if location:
                return location
    
    # 检查常见的地标缩写
    landmarks = {
        'ucl': 'University College London',
        'king\'s cross': 'King\'s Cross',
        'kings cross': 'King\'s Cross',
        'canary wharf': 'Canary Wharf',
        'london bridge': 'London Bridge',
        'westminster': 'Westminster',
        'camden': 'Camden',
        'bloomsbury': 'Bloomsbury',
        'imperial': 'Imperial College London',
        'lse': 'London School of Economics',
    }
    
    for abbr, full_name in landmarks.items():
        if abbr in query_lower:
            return full_name
    
    return None

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
            
            # 🔧 POST-PROCESSING: Extract destination using regex if model missed it
            if not result.get('destination'):
                extracted_destination = _extract_destination_with_regex(user_query)
                if extracted_destination:
                    print(f"[INFO] 🔧 Regex补充提取 destination: {extracted_destination}")
                    result['destination'] = extracted_destination
            
            # Validate result has required fields
            if result.get('status') == 'success':
                required = ['destination', 'max_budget', 'max_travel_time']
                # 🔧 修复：正确检查必需字段，0 对于 budget 和 travel_time 是无效值
                missing = []
                for f in required:
                    val = result.get(f)
                    if val is None:
                        missing.append(f)
                    elif f in ['max_budget', 'max_travel_time'] and val == 0:
                        missing.append(f)  # 0 是无效值，需要用户提供
                    elif not val:  # 空字符串等
                        missing.append(f)
                
                if not missing:
                    # All required fields present
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
                    # ✅ NEW: Missing required fields - ask user instead of falling back to Ollama
                    print(f"[INFO] Fine-tuned model missing required fields: {missing}")
                    print(f"[INFO] → Asking user for missing information instead of falling back to Ollama")
                    
                    # Build a friendly clarification question
                    field_names = {
                        'destination': 'your target location or workplace',
                        'max_budget': 'your monthly budget',
                        'max_travel_time': 'your maximum commute time'
                    }
                    
                    missing_descriptions = [field_names.get(f, f) for f in missing]
                    
                    # Keep the fields we already have
                    # 🔧 修复：确保 soft_preferences 始终是字符串或列表，不会被错误遍历
                    soft_prefs = result.get('soft_preferences', '')
                    if isinstance(soft_prefs, list):
                        soft_prefs = ', '.join(soft_prefs) if soft_prefs else ''
                    
                    clarification_result = {
                        'status': 'clarification_needed',
                        'destination': result.get('destination'),
                        'max_budget': result.get('max_budget'),
                        'max_travel_time': result.get('max_travel_time'),
                        'soft_preferences': soft_prefs,  # 保持为字符串
                        'property_tags': result.get('property_tags', []),
                        'amenities_of_interest': result.get('amenities_of_interest', []),
                        'area_vibe': result.get('area_vibe'),
                        'suggested_search_locations': result.get('suggested_search_locations', []),
                        'city_context': result.get('city_context', 'London'),
                        'data': {
                            'question': f"I understood most of your requirements! Could you please also tell me {' and '.join(missing_descriptions)}?"
                        }
                    }
                    
                    print(f"[INFO] ✓ Returning clarification request for: {missing}")
                    return clarification_result
            elif result.get('status') == 'clarification_needed':
                # ✅ CRITICAL FIX: Check if all required fields are actually present
                # 🔧 修复：正确检查必需字段，0 是无效值
                required = ['destination', 'max_budget', 'max_travel_time']
                has_all_required = True
                for field in required:
                    val = result.get(field)
                    if val is None or val == '' or (field in ['max_budget', 'max_travel_time'] and val == 0):
                        has_all_required = False
                        break
                
                if has_all_required:
                    # ✅ Model said "clarification_needed" but we have everything!
                    # Change status to success and proceed with search
                    print("[INFO] ✅ Fine-tuned model returned clarification_needed, but all required fields are present!")
                    print(f"[INFO]   → Destination: {result.get('destination')}")
                    print(f"[INFO]   → Budget: £{result.get('max_budget')}")
                    print(f"[INFO]   → Travel time: {result.get('max_travel_time')} min")
                    print(f"[INFO]   → Soft preferences: {result.get('soft_preferences')}")
                    
                    result['status'] = 'success'
                    
                    # Fill in default values for missing optional fields
                    if not result.get('suggested_search_locations'):
                        result['suggested_search_locations'] = []
                    if not result.get('area_vibe'):
                        result['area_vibe'] = None
                    if not result.get('amenities_of_interest'):
                        result['amenities_of_interest'] = []
                    
                    # Remove the clarification question since we don't need it
                    result.pop('data', None)
                    
                    print("[INFO] ✓ Converted to success status, proceeding with search")
                    return result
                
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
  "budget_period": "month",
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
2. max_budget: Extract NUMERIC value ONLY (e.g., 280 for "£280/week", 1500 for "£1500/month")
3. budget_period: CRITICAL! Identify if budget is "week" or "month":
   - "£280 per week" / "£280/week" / "£280 pw" → budget_period: "week"
   - "£1500 per month" / "£1500/month" / "£1500 pcm" → budget_period: "month"
   - If not specified, assume "month"
4. max_travel_time: Extract minutes ONLY. "40 min" = 40, "1 hour" = 60, "90 minutes" = 90
5. If unlimited travel time, set to 999
6. suggested_search_locations: List nearby areas for the destination
7. soft_preferences: Extract SPECIFIC user concerns like "concerned about crime", "want safe area", "need quiet location", "prefer modern", etc. This is IMPORTANT!
8. CRITICAL: Return ONLY the completed JSON object, nothing else

JSON OUTPUT:"""

    response_text = call_ollama(prompt, system_prompt, timeout=360)
    
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
        
        # 🔧 修复：正确检查必需字段，0 是无效值
        required = ['destination', 'max_budget', 'max_travel_time']
        has_required = True
        for field in required:
            val = parsed_json.get(field)
            if val is None or val == '':
                has_required = False
                break
            elif field in ['max_budget', 'max_travel_time'] and val == 0:
                has_required = False
                break
        
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
    ✅ FIXED: Properly merge clarification answers without losing context
    """
    import re
    
    print(f"\n{'='*60}")
    print(f"[Refine] Merging clarification answer")
    print(f"[Refine] User answer: '{user_answer}'")
    print(f"{'='*60}")
    
    # Make a copy to avoid mutation issues
    result = original_criteria.copy()
    
    # ✅ CRITICAL FIX: Check _original_query first for missing destination
    original_query = result.get('_original_query', '')
    if original_query and not result.get('destination'):
        print(f"[Refine] 📝 Checking original query for destination: '{original_query}'")
        
        # Try to extract destination from original query
        original_lower = original_query.lower().strip()
        
        # Common destination patterns
        destination_patterns = [
            (r'near\s+([a-zA-Z\s]+?)(?:,|\.|$|my|budget|within|\d)', 'near'),
            (r'close\s+to\s+([a-zA-Z\s]+?)(?:,|\.|$|my|budget|within|\d)', 'close to'),
            (r'around\s+([a-zA-Z\s]+?)(?:,|\.|$|my|budget|within|\d)', 'around'),
            (r'at\s+([a-zA-Z\s]+?)(?:,|\.|$|my|budget|within|\d)', 'at'),
        ]
        
        for pattern, keyword in destination_patterns:
            match = re.search(pattern, original_lower)
            if match:
                location = match.group(1).strip()
                # Expand common abbreviations
                if location == 'ucl':
                    location = 'University College London'
                elif location == "king's cross" or location == 'kings cross':
                    location = "King's Cross"
                elif location == 'lse':
                    location = 'London School of Economics'
                
                result['destination'] = location.title() if location.islower() else location
                print(f"[Refine] ✅ Extracted destination from original query: {result['destination']}")
                break
    
    # Extract info from the user's clarification answer
    answer_lower = user_answer.lower().strip()
    
    # 1. Extract commute time (e.g., "30 min", "within 30 minutes")
    time_match = re.search(r'(\d+)\s*(?:min|minutes|mins)', answer_lower)
    if time_match and not result.get('max_travel_time'):
        result['max_travel_time'] = int(time_match.group(1))
        print(f"[Refine] ✓ Extracted commute time: {result['max_travel_time']} min")
    
    # 2. Extract budget (e.g., "£1400", "1400 pounds")
    budget_match = re.search(r'£?\s*(\d+(?:,\d{3})*)\s*(?:pounds?|per month|pcm)?', answer_lower)
    if budget_match and not result.get('max_budget'):
        budget_str = budget_match.group(1).replace(',', '')
        result['max_budget'] = int(budget_str)
        print(f"[Refine] ✓ Extracted budget: £{result['max_budget']}")
    
    # 3. Extract destination - only if not already present
    if not result.get('destination'):
        location_keywords = ['near', 'around', 'close to', 'at', 'in']
        for keyword in location_keywords:
            if keyword in answer_lower:
                parts = answer_lower.split(keyword, 1)
                if len(parts) > 1:
                    location_text = parts[1].strip().split(',')[0].strip()
                    # Only extract if it looks like a real location (not a number/time)
                    if not re.match(r'^\d+\s*min', location_text) and len(location_text) > 2:
                        result['destination'] = location_text.title()
                        print(f"[Refine] ✓ Extracted destination: {result['destination']}")
                        break
    
    # 4. Handle negative/decline responses
    is_negative = any(phrase in answer_lower for phrase in [
        'no, i do not', 'no i do not', "no, don't", "no don't", 
        'nope', 'not really', 'nothing else', 'nothing more',
        'no thanks', 'none', 'no worries', "don't care"
    ])
    
    if is_negative:
        print("[Refine] ✓ User declined further input")
        if not result.get('max_travel_time'):
            result['max_travel_time'] = 50  # Default
            print(f"[Refine]   → Using default commute: 50 min")
        if not result.get('soft_preferences'):
            result['soft_preferences'] = ""
    
    # 5. Check if all required fields are present
    # 🔧 修复：正确检查必需字段，0 是无效值
    required_fields = ['destination', 'max_budget', 'max_travel_time']
    missing_fields = []
    for f in required_fields:
        val = result.get(f)
        if val is None or val == '':
            missing_fields.append(f)
        elif f in ['max_budget', 'max_travel_time'] and val == 0:
            missing_fields.append(f)
    
    if missing_fields:
        print(f"[Refine] ⚠️  Still missing: {missing_fields}")
        
        # Generate specific question
        if 'destination' in missing_fields:
            question = "Where would you like to live? (e.g., near UCL, Camden, King's Cross)"
        elif 'max_budget' in missing_fields:
            question = "What's your maximum monthly budget in pounds?"
        elif 'max_travel_time' in missing_fields:
            question = "What's your maximum commute time in minutes?"
        else:
            question = f"Please specify: {', '.join(missing_fields)}"
        
        result['status'] = 'clarification_needed'
        result['data'] = {'question': question}
        return result
    
    # 6. Success - all required fields present
    print(f"[Refine] ✅ All required fields complete!")
    print(f"[Refine]   → Destination: {result.get('destination')}")
    print(f"[Refine]   → Budget: £{result.get('max_budget')}")
    print(f"[Refine]   → Max commute: {result.get('max_travel_time')} min")
    
    # Set defaults for optional fields
    if not result.get('suggested_search_locations'):
        result['suggested_search_locations'] = []
    if not result.get('soft_preferences'):
        result['soft_preferences'] = ""
    if not result.get('amenities_of_interest'):
        result['amenities_of_interest'] = []
    if not result.get('area_vibe'):
        result['area_vibe'] = None
    if not result.get('property_tags'):
        result['property_tags'] = []
    if not result.get('city_context'):
        result['city_context'] = "London"
    
    result['status'] = 'success'
    
    # Clean up temporary/internal fields
    result.pop('_original_query', None)
    if 'data' in result and result.get('status') == 'success':
        result.pop('data', None)
    
    return result

def _get_property_url(prop: dict) -> str:
    """Helper to get URL from property dict"""
    for key in ['URL', 'url', 'Url', 'link', 'Link']:
        if key in prop and prop[key]:
            return prop[key]
    return ''

def _normalize_price_format(price_str: str) -> str:
    """
    ✅ Normalize price format to always be '£XXXX pcm'
    Handles various input formats:
    - '£1342 pcm' -> '£1342 pcm'
    - '£1342pcm' -> '£1342 pcm'
    - '£1342 pw' -> '£5814 pcm' (convert weekly to monthly)
    - '£1342' -> '£1342 pcm'
    - '1342' -> '£1342 pcm'
    """
    import re
    
    if not price_str or not isinstance(price_str, str):
        return 'N/A'
    
    # Remove all whitespace and convert to lowercase for processing
    price_lower = price_str.lower().strip()
    
    # Extract numeric value
    numeric_match = re.search(r'(\d+(?:,\d{3})*(?:\.\d{2})?)', price_lower)
    if not numeric_match:
        return price_str  # Return as-is if no number found
    
    price_value = float(numeric_match.group(1).replace(',', ''))
    
    # Check if it's weekly (pw) and convert to monthly (pcm)
    if 'pw' in price_lower or 'per week' in price_lower or 'weekly' in price_lower:
        # Convert weekly to monthly: multiply by 52 weeks, divide by 12 months
        price_value = (price_value * 52) / 12
        print(f"  [Price] Converted weekly price to monthly: {price_str} -> £{int(price_value)} pcm")
    
    # Return normalized format: £XXXX pcm (no decimals for monthly rent)
    return f"£{int(price_value)} pcm"

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
            # ✅ FIX 2B: Normalize actual price format
            actual_price_raw = original_prop.get('Price', 'N/A')
            actual_price_normalized = _normalize_price_format(actual_price_raw)
            
            # Get numeric price and budget for comparison
            price_numeric = original_prop.get('parsed_price', 0)
            max_budget = original_prop.get('_max_budget', None)
            
            # ✅ IMPROVED: Fix "over budget" logic errors
            import re
            
            # Pattern to match various price formats in explanation
            price_pattern = r'£\s*\d+(?:,\d{3})*(?:\.\d+)?(?:\s*pcm)*(?:\s*pw)*'
            
            # Find all price mentions
            prices_in_explanation = re.findall(price_pattern, explanation, re.IGNORECASE)
            
            if prices_in_explanation:
                print(f"  🔍 Rank {rank}: Found {len(prices_in_explanation)} price mentions")
                
                # Replace ALL price mentions with clean format (no pcm)
                explanation_fixed = explanation
                price_num_only = actual_price_normalized.replace(' pcm', '')
                
                for old_price in prices_in_explanation:
                    explanation_fixed = explanation_fixed.replace(old_price, price_num_only)
                    print(f"     📝 Replaced '{old_price}' with '{price_num_only}'")
                
                # ✅ FIX CRITICAL: Fix "over budget" errors
                if max_budget and price_numeric:
                    if price_numeric <= max_budget:
                        # Property is WITHIN budget - AGGRESSIVELY remove all "over budget" mentions
                        # Pattern 1: "within budget at £1342 over budget" → "within budget at £1342"
                        explanation_fixed = re.sub(
                            r'(within budget at [£\d,]+)\s+over budget',
                            r'\1',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        # Pattern 2: "£1342 over budget at £1342" → "within budget at £1342"
                        explanation_fixed = re.sub(
                            r'£[\d,]+\s+over budget at (£[\d,]+)',
                            r'within budget at \1',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        # Pattern 3: "over budget at £1342" → "within budget at £1342"
                        explanation_fixed = re.sub(
                            r'over budget at (£[\d,]+)',
                            r'within budget at \1',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        # Pattern 4: Any remaining "over budget" or "above budget"
                        explanation_fixed = re.sub(
                            r'\s+over budget\b',
                            '',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        explanation_fixed = re.sub(
                            r'\s+above budget\b',
                            '',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        print(f"     ✅ Fixed: Property is WITHIN budget (£{price_numeric} ≤ £{max_budget})")
                    else:
                        # Property is OVER budget - ensure correct overage with EXACT amount
                        overage = int(price_numeric - max_budget)
                        # Pattern 1: "within budget at £1468 over budget" → "£68 over budget at £1468"
                        explanation_fixed = re.sub(
                            r'within budget at (£[\d,]+)\s+over budget',
                            f'£{overage} over budget at \\1',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        # Pattern 2: "£1468 over budget at £1468" → "£68 over budget at £1468"
                        explanation_fixed = re.sub(
                            r'£[\d,]+\s+over budget at (£[\d,]+)',
                            f'£{overage} over budget at \\1',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        # Pattern 3: "over budget at £1468" → "£68 over budget at £1468"
                        explanation_fixed = re.sub(
                            r'over budget at (£[\d,]+)',
                            f'£{overage} over budget at \\1',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        # Pattern 4: Plain "above budget" or "over budget" without price
                        explanation_fixed = re.sub(
                            r'\babove budget\b',
                            f'£{overage} over budget',
                            explanation_fixed,
                            flags=re.IGNORECASE
                        )
                        print(f"     ✅ Fixed: Property is £{overage} OVER budget (£{price_numeric} > £{max_budget})")
                
                rec['explanation'] = explanation_fixed
            else:
                print(f"  ✓ Rank {rank}: No price mentions found")
            
            # ✅ FIX CRITICAL: Validate and fix travel time mentions
            actual_travel_time = original_prop.get('travel_time_minutes')
            if actual_travel_time and isinstance(actual_travel_time, (int, float)):
                # Find incorrect travel time mentions in explanation
                # Pattern: "15-minute", "15 minute", "15 minutes", "in 15 minutes"
                time_pattern = r'(\d+)[-\s]minute[s]?'
                time_mentions = re.findall(time_pattern, rec['explanation'], re.IGNORECASE)
                
                if time_mentions:
                    incorrect_times = [int(t) for t in time_mentions if int(t) != actual_travel_time]
                    if incorrect_times:
                        print(f"     ⚠️  Found INCORRECT travel time mentions: {incorrect_times}")
                        print(f"     ✅ Actual travel time: {actual_travel_time} minutes")
                        
                        # Replace all incorrect time mentions with correct time
                        explanation_fixed = rec['explanation']
                        for wrong_time in set(time_mentions):
                            if int(wrong_time) != actual_travel_time:
                                # Replace patterns like "15-minute" or "15 minute"
                                explanation_fixed = re.sub(
                                    rf'{wrong_time}[-\s]minute[s]?',
                                    f'{actual_travel_time}-minute',
                                    explanation_fixed,
                                    flags=re.IGNORECASE
                                )
                        
                        rec['explanation'] = explanation_fixed
                        print(f"     ✅ Fixed travel time mentions to {actual_travel_time} minutes")
    
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
    # 🆕 处理 soft_preferences 可能是列表或字符串的情况
    if isinstance(soft_preferences, list):
        soft_prefs_lower = ' '.join(str(p) for p in soft_preferences).lower()
    else:
        soft_prefs_lower = str(soft_preferences).lower() if soft_preferences else ""
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
        
        # ✅ 只在用户关心且有有效数据时才添加 crime data
        if should_include_crime:
            crime_data = prop.get('crime_data_summary', {})
            # Check if crime data is valid and not empty
            if crime_data and isinstance(crime_data, dict) and 'total_crimes_6m' in crime_data:
                simple_prop['crimes_6m'] = crime_data.get('total_crimes_6m', 0)
                simple_prop['crime_trend'] = crime_data.get('crime_trend', 'unknown')
                simple_prop['top_crime_types'] = crime_data.get('top_crime_types', [])[:2]
                print(f"    ✅ Property {i+1} - Including crime data: {simple_prop['crimes_6m']} crimes")
            else:
                print(f"    ⚠️  Property {i+1} - No valid crime data available")
        # ✅ 不添加这些字段，防止模型看到 null 值
        
        # ✅ 只在用户关心时才添加 amenities data
        if should_include_amenities and 'amenities_nearby' in prop:
            amenities = prop.get('amenities_nearby', {})
            simple_prop['nearby_supermarkets'] = amenities.get('supermarket_in_1500m', 0)
            simple_prop['nearby_parks'] = amenities.get('park_in_1500m', 0)
            simple_prop['nearby_gyms'] = amenities.get('gym_in_1500m', 0)
        # ✅ 不添加这些字段，防止模型看到 null 值
        
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
        # 🆕 处理 soft_preferences 可能是列表或字符串的情况
        if isinstance(soft_preferences, list):
            sp_lower = ' '.join(str(p) for p in soft_preferences).lower()
        else:
            sp_lower = str(soft_preferences).lower()
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
- 🚨 CRITICAL: Discusses the commute using EXACTLY the "travel_time_minutes" number from the data
  ✅ CORRECT: "travel_time_minutes": 27 → Say "27-minute commute" or "in just 27 minutes"
  ❌ WRONG: "travel_time_minutes": 27 → Do NOT say "15-minute commute" or any other number
  ⚠️ If you mention commute time, it MUST match the travel_time_minutes field EXACTLY
- 🚨 CRIME DATA RULES (STRICT):
  • IF "crimes_6m" field EXISTS → MUST mention the EXACT number in your explanation
    ✅ CORRECT: "crimes_6m": 145 → "The area recorded 145 crimes over 6 months"
    ✅ CORRECT: "crimes_6m": 7 → "The area is relatively safe with only 7 crimes reported in 6 months"
    ❌ WRONG: "crimes_6m": 145 → "higher number of crimes" (too vague)
    ❌ WRONG: "crimes_6m": 7 → "has a stable crime trend" (missing the number)
  • IF "crimes_6m" field MISSING or null → DO NOT mention crime/safety AT ALL
    ❌ Do NOT say "prioritizing safety" or "safe area" without data
- 🚨 IF property price_numeric > budget: MUST say "£X over budget" with exact overage amount
  ✅ Example: price_numeric=1468, budget=1400 → "£68 over budget" or "slightly above budget at £1468 (£68 over)"
  ❌ Do NOT say "above budget" without the exact amount
- ⚠️ ONLY discuss safety/crime if "crimes_6m" field exists AND give the EXACT number
  ✅ Example: "crimes_6m": 145 → "145 crimes reported over 6 months"
  ❌ Do NOT say "higher number of crimes" without the actual number
  ❌ Do NOT mention crime/safety if crimes_6m field is missing
- Mentions value for money (is it a good deal for the area?)
- ⚠️ ONLY mention amenities if "nearby_supermarkets" field exists
- Notes any standout features (from description ONLY)
- Ends with who this property is perfect for OR a specific benefit

🔴 CRITICAL RULES - NO FABRICATION ALLOWED:
1. Use ONLY the ACTUAL data provided - NO FABRICATION ALLOWED
   ❌ Do NOT invent amenities like "pet-friendly", "modern", "student-friendly", "newly renovated"
   ❌ Do NOT make up features that aren't in the description or data fields
   ❌ Do NOT invent travel times - USE travel_time_minutes field exactly
   ✅ ONLY mention features that are explicitly in the "description" field
   ✅ ONLY mention amenities that are in the provided amenity fields (nearby_supermarkets, nearby_parks, nearby_gyms, etc.)

2. Do NOT make up prices - use actual prices from property data

3. If a field is null, missing, or empty string, DO NOT mention it AT ALL
   ✅ If "crimes_6m": null → DO NOT mention "safety" or "crime" in ANY way
   ✅ If "nearby_supermarkets": null → DO NOT mention "amenities" in ANY way
   ✅ If the description is just "2 bedroom flat", mention the bedrooms - that's factual
   ❌ Do NOT add "it's perfect for students" or "modern amenities" if not in description
   ❌ Do NOT end sentences with "This would suit someone prioritizing X" if X data is null

4. Property description is the ONLY source for physical features
   - If description says "2 bedroom flat" → mention "2 bedrooms"
   - If description says "Studio flat" → mention "studio"
   - If description says "1 bedroom flat" → mention "1 bedroom", NOT "modern kitchen" or "balcony"
   - If description is vague/short → focus on commute, price, location instead

5. Each explanation should be 3-5 sentences, but ONLY if you have real data to mention

6. 🚨 FORBIDDEN - DO NOT END WITH GENERIC PHRASES:
   ❌ "This would suit someone prioritizing safety" (when crimes_6m is null)
   ❌ "This would suit someone prioritizing amenities" (when nearby_supermarkets is null)
   ❌ "Perfect for students" (unless explicitly in description)
   ❌ "Ideal for [any group]" (unless you have data to support it)
   ✅ Instead, end with specific facts: "The 15-minute commute and £1,200 price make this a practical choice."

Example patterns (with actual data only):
- Simple property IN budget: "Located in [AREA], this offers a [EXACT_TRAVEL_TIME]-minute commute. At £[EXACT_PRICE], it's within budget and good value."
- Property OVER budget: "This [AREA] property has a [EXACT_TRAVEL_TIME]-minute commute but is £[OVERAGE] over budget at £[PRICE]. The shorter commute/better location might justify the extra cost."
- 🚨 With crime data (ONLY if crimes_6m EXISTS in the JSON data):
  ✅ CORRECT: "crimes_6m": 7 → "The area is relatively safe with only 7 crimes reported over 6 months."
  ✅ CORRECT: "crimes_6m": 145 → "The area recorded 145 crimes over 6 months, mainly anti-social behavior."
  ✅ CORRECT: "crimes_6m": 28 → "Safety-conscious renters should note the 28 crimes reported in the past 6 months."
  ❌ WRONG: "The area has a stable crime trend" (missing the actual number)
- 🚨 WITHOUT crime data (crimes_6m field NOT in JSON): 
  ❌ Do NOT mention crime/safety/security AT ALL
  ❌ Do NOT say "prioritizing safety" or "safe neighborhood"
- With amenities (ONLY if nearby_supermarkets exists): "[COUNT] supermarkets within 1.5km make daily shopping convenient."
- WITHOUT amenities: Do NOT mention amenities at all

🚨 ABSOLUTE REQUIREMENTS:
1. Travel time MUST be the exact number from travel_time_minutes field
2. If over budget, MUST state the exact overage amount in pounds
3. 🚨 IF crimes_6m field EXISTS in the property JSON → MUST mention the EXACT number
   ✅ Example: If you see "crimes_6m": 7 → Say "7 crimes reported over 6 months"
   ❌ Do NOT say "stable crime trend" or "prioritizing safety" without the number
4. 🚨 IF crimes_6m field MISSING from the property JSON → DO NOT mention crime/safety AT ALL
5. DO NOT say "prioritizing safety" or "prioritizing amenities" if those fields don't exist
6. DO NOT recommend the same property twice - each recommendation must be a DIFFERENT address

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

    response_text = call_ollama(prompt, system_prompt, timeout=360)

    if not response_text:
        print("[INFO] Ollama failed, using rule-based recommendations")
        return create_fallback_recommendations(properties_data, soft_preferences)

    # 🔍 添加调试：查看LLM原始响应
    print(f"\n[DEBUG] LLM Response Length: {len(response_text)} chars")
    print(f"[DEBUG] First 500 chars of response:")
    print(response_text[:500])
    print(f"[DEBUG] Last 300 chars of response:")
    print(response_text[-300:])
    
    parsed = extract_first_json(response_text)

    if parsed and 'recommendations' in parsed:
        print("\n[DEBUG] ✅ JSON解析成功")
        print(f"[DEBUG] Found {len(parsed['recommendations'])} recommendations")
        print("\n[DEBUG] Fixing travel times and images...")
        
        # ✅ FIX 1: Re-rank recommendations to ensure sequential ranking (1, 2, 3, not 1, 3, 5)
        for i, rec in enumerate(parsed['recommendations'], start=1):
            rec['rank'] = i
        
        # ✅ FIX 3: Remove duplicate properties (same address)
        seen_addresses = set()
        unique_recommendations = []
        
        for rec in parsed['recommendations']:
            # Normalize address for comparison (lowercase, remove spaces)
            addr_normalized = rec.get('address', '').lower().strip()[:50]
            
            if addr_normalized not in seen_addresses:
                seen_addresses.add(addr_normalized)
                unique_recommendations.append(rec)
            else:
                print(f"  ⚠️  Skipping duplicate: {rec.get('address', '')[:40]}")
        
        # Re-rank after deduplication
        for i, rec in enumerate(unique_recommendations, start=1):
            rec['rank'] = i
        
        parsed['recommendations'] = unique_recommendations
        print(f"  ✓ After deduplication: {len(unique_recommendations)} unique properties")
        
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
                rec['geo_location'] = original_prop.get('geo_location', '')  # ✅ ADD: Include coordinates for map generation
                
                # ✅ FIX 2: Normalize price format to prevent "pcm pcm" and handle pw->pcm conversion
                actual_price_raw = original_prop.get('Price', 'N/A')
                actual_price_normalized = _normalize_price_format(actual_price_raw)
                rec['price'] = actual_price_normalized
                
                print(f"  ✓ Rank {rank}: {rec['address'][:40]} - {rec['travel_time']} - Price: {actual_price_normalized}")
        
        parsed = _validate_and_fix_price_in_explanations(parsed, properties_data)
        
        return parsed
    else:
        print("[WARN] Could not parse JSON, using fallback")
        print(f"[DEBUG] Parsed result: {parsed}")
        print(f"[DEBUG] Has 'recommendations' key: {'recommendations' in parsed if parsed else 'N/A (parsed is None)'}")
        if parsed:
            print(f"[DEBUG] Parsed keys: {list(parsed.keys())}")
        return create_fallback_recommendations(properties_data, soft_preferences)

def create_fallback_recommendations(properties_data: list[dict], soft_preferences: str = "") -> dict:
    """
    ✅ FIXED: High-quality fallback with natural explanations
    现在也遵循条件化逻辑 - 只在用户关心时才提到相关数据
    """
    print("   🔧 Creating intelligent rule-based recommendations...")
    
    # ✅ FIX 3B: Deduplicate properties before sorting
    seen_addresses = set()
    unique_properties = []
    
    for prop in properties_data[:15]:
        addr_normalized = prop.get('Address', '').lower().strip()[:50]
        if addr_normalized not in seen_addresses:
            seen_addresses.add(addr_normalized)
            unique_properties.append(prop)
    
    print(f"   → Deduplicated: {len(unique_properties)} unique properties from {len(properties_data[:15])}")
    
    sorted_props = sorted(
        unique_properties,
        key=lambda x: (
            x.get('travel_time_minutes', 999),
            x.get('parsed_price', 9999)
        )
    )
    
    # ✅ 根据 soft_preferences 决定要提到哪些方面
    # 🆕 处理 soft_preferences 可能是列表或字符串的情况
    if isinstance(soft_preferences, list):
        soft_prefs_lower = ' '.join(str(p) for p in soft_preferences).lower()
    else:
        soft_prefs_lower = str(soft_preferences).lower() if soft_preferences else ""
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
        
        # ✅ FIX 2C: Normalize price in fallback recommendations too
        normalized_price = _normalize_price_format(price)
        
        recommendations.append({
            'rank': i + 1,
            'address': address,
            'price': normalized_price,
            'travel_time': f"{travel_time} minutes" if isinstance(travel_time, (int, float)) else str(travel_time),
            'explanation': explanation,
            'url': _get_property_url(prop),
            'images': prop.get('Images', []),
            'geo_location': prop.get('geo_location', '')  # ✅ ADD: Include coordinates for map generation
        })
    
    print(f"   ✓ Created {len(recommendations)} natural recommendations")
    return {'recommendations': recommendations[:3]}