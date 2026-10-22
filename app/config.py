# config.py

import os
from dotenv import load_dotenv

from uk_rent_agent.llm.router import reject_retired_model_names

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
# deepseek-chat / deepseek-reasoner were RETIRED by the provider on 2026-07-24 and now
# return HTTP 400 ("The supported API model names are deepseek-v4-pro or
# deepseek-v4-flash"). core/llm_config.py and uk_rent_agent/llm/router.py were migrated;
# THIS default was missed, and it feeds core/llm_interface.py. A retired name left in a
# default is a live outage waiting for someone to drop the env override.
# tests/test_model_name_defaults.py pins this.
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-flash')
# Pinning the default is only half of it — a retired name can also arrive as an ENV
# OVERRIDE of correct defaults, which no source-scan test can see. Refuse the override here
# too: this module reads the repo-ROOT .env (load_dotenv() with no path) while
# core/llm_config.py reads app/.env, so the two see different files and a retired name can
# arrive through either one.
reject_retired_model_names('config (import time)', DEEPSEEK_MODEL=DEEPSEEK_MODEL)

# LLM provider: 'deepseek' (cloud API) or 'ollama' (local). See core/llm_config.py
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'deepseek')

# Choose which service to use for travel time calculation
# Options: 'google' (accurate, paid), 'openroute' (free, approximate)
USE_TRAVEL_SERVICE = 'google'  # Change to 'openroute' if you want free