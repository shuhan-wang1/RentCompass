from pathlib import Path
import os,json,subprocess,time,urllib.request
B=Path('/tmp/rentcompass-live-20261019');P='/home/shuhan/.cache/rentcompass-ci-venv/bin/python';e=dict(os.environ);e.update(json.loads((B/'private/env.json').read_text()));e.update(PYTHONPATH=f'{B}/harness:{B}/source/src:{B}/source/app:{B}/source',APP_PROJECT_ROOT=str(B/'source'),PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',RAG_EAGER_INIT='0',SEARCH_EMBED_PREWARM='0',ENABLE_CHECKPOINTER='1',BACKGROUND_JOBS_ENABLED='0',AGENT_ARCH='fc_loop',USE_MCP_TOOLS='0',TOOLS_OVER_MCP='0',LLM_PROVIDER='deepseek',DEEPSEEK_BASE_URL='http://127.0.0.1:19093',REQUIRE_AUTH='0',SESSION_COOKIE_SECURE='0',ALLOW_LEGACY_CLIENT_USER_ID='0',CANARY_LOG_PATH='off',READINESS_REQUIRE_LLM='0',RELEASE_METADATA_REQUIRED='0',PROMPT_VERSION='2.2.0',APP_CANDIDATE_SHA='live-test-frozen-working-tree',PROPERTY_SOURCE='csv',SEARCH_ALLOW_DEMO_FALLBACK='0',DESC_ENRICH_ENABLED='0',SEARCH_CANONICAL_SCRAPE_LIMIT='12',SEARCH_BAND_RESCUE_SCRAPE_LIMIT='12',SEARCH_MAX_IN_FLIGHT_SCRAPES='1',SEARXNG_URL='http://127.0.0.1:8080',CORS_ORIGINS='http://127.0.0.1:15009')
for key,name in {'CHECKPOINT_DB_PATH':'checkpoints.sqlite3','CONVERSATION_DB_PATH':'conversations.sqlite3','AUTH_DB_PATH':'auth.sqlite3','RATE_LIMIT_DB_PATH':'rate_limits.sqlite3','RUNTIME_CACHE_PATH':'cache.sqlite3','SEARCH_LISTING_CACHE_PATH':'listing_cache.sqlite3','SEARCH_CACHE_DB_PATH':'search_cache.sqlite3','AGENT_MEMORY_DB_PATH':'memory','IDEMPOTENCY_DB':'idempotency.sqlite3','MEMORY_GATE_DB':'memory_gate.sqlite3','OTM_DESC_CACHE_PATH':'descriptions.sqlite3','AREA_RECO_CACHE_PATH':'areas.sqlite3'}.items():e[key]=str(B/'runtime'/name)
pids={}
for label,script in [('relay','relay.py'),('app','boot.py')]:
 f=(B/'private'/(label+'.log')).open('a');p=subprocess.Popen([P,str(B/'harness'/script)],cwd=B/'source',env=e,stdout=f,stderr=subprocess.STDOUT,start_new_session=True);pids[label]=p.pid
(B/'private/pids.json').write_text(json.dumps(pids))
for n in range(90):
 try:
  with urllib.request.urlopen('http://127.0.0.1:15009/ready',timeout=2) as r:
   d=json.load(r);(B/'evidence/readiness.json').write_text(json.dumps(d,indent=2));print({'ready':r.status,'status':d.get('status'),'checks':{k:v.get('status') for k,v in d.get('checks',{}).items()}});break
 except Exception:time.sleep(.5)
else:raise RuntimeError('formal test application not ready; inspect private app.log')
