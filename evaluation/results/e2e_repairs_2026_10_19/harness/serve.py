import pathlib,subprocess,os
B=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019')
env={'PYTHONPATH':'/audit/harness:/work/src:/work/app:/work','APP_PROJECT_ROOT':'/work','PYTHONDONTWRITEBYTECODE':'1',
'HF_HOME':'/opt/hf-cache','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','SEARCH_EMBED_PREWARM':'0','RAG_EAGER_INIT':'0',
'ENABLE_CHECKPOINTER':'1','BACKGROUND_JOBS_ENABLED':'1','CANARY_LOG_PATH':'off','SEARXNG_URL':'http://127.0.0.1:19092',
'AGENT_ARCH':'fc_loop','LLM_PROVIDER':'deepseek','DEEPSEEK_BASE_URL':'http://127.0.0.1:19091','PROPERTY_SOURCE':'csv',
'REQUIRE_AUTH':os.environ.get('AUDIT_REQUIRE_AUTH','0'),'SESSION_COOKIE_SECURE':'0','ALLOW_LEGACY_CLIENT_USER_ID':'0',
'READINESS_REQUIRE_LLM':'0','RELEASE_METADATA_REQUIRED':'0','CORS_ORIGINS':'http://127.0.0.1:15008','APP_CANDIDATE_SHA':'e2e-repair-frozen-working-tree','PROMPT_VERSION':'2.2.0',
'DESC_ENRICH_ENABLED':'0','SEARCH_CACHE_TTL_HOURS':'87600','OTM_DESC_CACHE_TTL_HOURS':'87600',
'IDEMPOTENCY_DB':'/audit/runtime/idempotency.sqlite3','MEMORY_GATE_DB':'/audit/runtime/memory_gate.sqlite3',
'OTM_DESC_CACHE_PATH':'/audit/runtime/otm_desc_cache.sqlite3','AREA_RECO_CACHE_PATH':'/audit/runtime/area_reco_cache.sqlite3'}
for key,name in {'CHECKPOINT_DB_PATH':'checkpoints.sqlite3','CONVERSATION_DB_PATH':'conversations.sqlite3','AUTH_DB_PATH':'auth.sqlite3','RATE_LIMIT_DB_PATH':'rate_limits.sqlite3','RUNTIME_CACHE_PATH':'runtime_cache.sqlite3','SEARCH_LISTING_CACHE_PATH':'listing_cache.sqlite3','SEARCH_CACHE_DB_PATH':'search_cache.sqlite3','AGENT_MEMORY_DB_PATH':'agent-memory'}.items():env[key]=f'/audit/runtime/{name}'
cmd=['docker','run','--rm','--name','rentcompass-e2e-repair-test','--network','none','--read-only','--user',f'{os.getuid()}:{os.getgid()}','--memory','3g','--cpus','2','--tmpfs','/tmp:rw,nosuid,size=256m','-v',f'{B}/web-source:/work:ro','-v',f'{B}:/audit','-v','uk_rent_recommendation_hf_cache:/opt/hf-cache:ro','-w','/work','--env-file',f'{B}/private/model.env']
for k,v in env.items():cmd+=['-e',f'{k}={v}']
cmd+=['--entrypoint','python','uk-rent-agent:latest','/audit/harness/boot.py']
with (B/'private/server.log').open('a') as log:result=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
raise SystemExit(result.returncode)
