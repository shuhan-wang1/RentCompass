# finetuned_parser.py - FIXED VERSION
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
import json
import re

class FinetunedParser:
    """Loads and uses your fine-tuned model for query→JSON parsing"""
    
    def __init__(self, base_model_name: str, adapter_path: str):
        print(f"[Model] Loading base model: {base_model_name}")
        
        # Load tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(
            base_model_name,
            trust_remote_code=True
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        
        # Load base model (CPU-safe: only request CUDA dtypes/dispatch when a GPU is present)
        print(f"[Model] Loading base model weights...")
        if torch.cuda.is_available():
            model_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            device_map = "auto"
        else:
            print("[Model] No CUDA GPU detected — loading on CPU (float32). This will be slower.")
            model_dtype = torch.float32
            device_map = None
        self.base_model = AutoModelForCausalLM.from_pretrained(
            base_model_name,
            torch_dtype=model_dtype,
            device_map=device_map,
            trust_remote_code=True
        )
        
        # Load LoRA adapters
        print(f"[Model] Loading LoRA adapters from: {adapter_path}")
        self.model = PeftModel.from_pretrained(self.base_model, adapter_path)
        self.model.eval()
        
        print("✓ Fine-tuned model ready for inference")
    
    def parse_query(self, user_query: str) -> dict:
        """Parse user query into structured JSON using fine-tuned model"""
        
        # Build prompt (match your training format)
        messages = [
            {"role": "system", "content": "You are a UK rental criteria extraction expert. Convert user queries to structured JSON."},
            {"role": "user", "content": user_query}
        ]
        
        # Apply chat template
        try:
            prompt = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True
            )
        except Exception as e:
            print(f"[WARN] Chat template failed: {e}, using fallback")
            prompt = f"<|im_start|>system\nYou are a UK rental criteria extraction expert. Convert user queries to structured JSON.<|im_end|>\n<|im_start|>user\n{user_query}<|im_end|>\n<|im_start|>assistant\n"
        
        # Tokenize
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        
        # Generate - FIXED: proper sampling parameters
        print(f"[Model] Generating response...")
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=512,
                do_sample=True,
                temperature=0.1,
                top_p=0.9,
                pad_token_id=self.tokenizer.pad_token_id,
                eos_token_id=self.tokenizer.eos_token_id
            )
        
        # Decode
        response = self.tokenizer.decode(
            outputs[0][inputs['input_ids'].shape[1]:], 
            skip_special_tokens=True
        )
        
        print(f"[Model] Raw output: {response[:500]}...")
        
        # Extract and normalize JSON
        parsed = self._extract_json(response)
        normalized = self._normalize_fields(parsed)
        
        return normalized
    
    def _extract_json(self, text: str) -> dict:
        """Extract JSON from model output (handles various formats)"""
        # Try direct parse
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        
        # Try markdown code block
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        
        # Try to find first complete JSON object
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
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        start_idx = -1
        
        # If all else fails, return error
        print(f"[ERROR] Could not extract JSON from: {text}")
        return {
            "status": "error",
            "data": {"message": "Could not parse model output"}
        }
    
    def _normalize_fields(self, parsed_json: dict) -> dict:
        """Normalize field names to match expected format"""
        if not isinstance(parsed_json, dict):
            return parsed_json
        
        # Map alternative field names to expected names
        field_mappings = {
            'max_walk_time': 'max_travel_time',
            'max_commute_time': 'max_travel_time',
            'max_time': 'max_travel_time',
        }
        
        for old_key, new_key in field_mappings.items():
            if old_key in parsed_json and new_key not in parsed_json:
                parsed_json[new_key] = parsed_json.pop(old_key)
        
        # Ensure soft_preferences is a string, not a list
        if 'soft_preferences' in parsed_json:
            if isinstance(parsed_json['soft_preferences'], list):
                parsed_json['soft_preferences'] = ', '.join(parsed_json['soft_preferences'])
        
        # Ensure property_tags is a list
        if 'property_tags' in parsed_json and isinstance(parsed_json['property_tags'], str):
            parsed_json['property_tags'] = [parsed_json['property_tags']]
        
        return parsed_json

# Singleton instance
_parser = None

def get_finetuned_parser(base_model: str, adapter_path: str):
    """Get or create parser instance (singleton pattern)"""
    global _parser
    if _parser is None:
        _parser = FinetunedParser(base_model, adapter_path)
    return _parser