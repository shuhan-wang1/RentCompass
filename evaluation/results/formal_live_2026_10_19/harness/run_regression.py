from pathlib import Path
import os,subprocess
B=Path('/tmp/rentcompass-live-20261019');R=Path('/home/shuhan/uk_rent_recommendation');e=dict(os.environ);e.update(PYTHONPATH=f'{R}/src:{R}/app:{R}',APP_PROJECT_ROOT=str(R),PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',SEARCH_EMBED_PREWARM='0',RAG_EAGER_INIT='0',ENABLE_CHECKPOINTER='0',BACKGROUND_JOBS_ENABLED='0',CANARY_LOG_PATH='off',SEARXNG_URL='',USE_MCP_TOOLS='0',RUN_LIVE_SCRAPE='0',RUN_LIVE_OSM='0')
for key,name in {'CHECKPOINT_DB_PATH':'checkpoints.sqlite3','CONVERSATION_DB_PATH':'conversations.sqlite3','AUTH_DB_PATH':'auth.sqlite3','RATE_LIMIT_DB_PATH':'rate_limits.sqlite3','RUNTIME_CACHE_PATH':'cache.sqlite3','SEARCH_LISTING_CACHE_PATH':'listings.sqlite3','SEARCH_CACHE_DB_PATH':'search.sqlite3','AGENT_MEMORY_DB_PATH':'memory','IDEMPOTENCY_DB':'idempotency.sqlite3','MEMORY_GATE_DB':'gate.sqlite3'}.items():e[key]=str(B/'runtime/unit'/name)
(B/'runtime/unit').mkdir(exist_ok=True)
with (B/'artifacts/full-regression.log').open('w') as f:r=subprocess.run(['/home/shuhan/.cache/rentcompass-ci-venv/bin/python','-m','pytest','-q','--basetemp',str(B/'runtime/pytest'),'--junitxml',str(B/'artifacts/full-regression.xml')],cwd=R,env=e,stdout=f,stderr=subprocess.STDOUT)
print('exit',r.returncode);print((B/'artifacts/full-regression.log').read_text()[-4500:]);raise SystemExit(r.returncode)
