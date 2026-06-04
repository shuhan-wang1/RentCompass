# config.py

import os
from dotenv import load_dotenv

load_dotenv()

# Gemini API (optional if using Ollama)
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')

# Google Maps API (PAID - most accurate)
GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY', '')

# OpenRouteService (FREE - less accurate but good enough)
OPENROUTESERVICE_API_KEY = os.getenv('OPENROUTESERVICE_API_KEY', '')

# DeepSeek API (OpenAI-compatible) - primary LLM when LLM_PROVIDER='deepseek'
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')

# LLM provider: 'deepseek' (cloud API) or 'ollama' (local). See core/llm_config.py
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'deepseek')

# Choose which service to use for travel time calculation
# Options: 'google' (accurate, paid), 'openroute' (free, approximate)
USE_TRAVEL_SERVICE = 'google'  # Change to 'openroute' if you want free