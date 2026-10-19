"""Direct real-provider integration, separate from browser/LLM coverage."""
from pathlib import Path
import os,sys,json,asyncio,time
B=Path('/tmp/rentcompass-fix-live-20261019');sys.path[:0]=[str(B/'harness'),str(B/'source/src'),str(B/'source/app'),str(B/'source')]
e=json.loads((B/'private/env.json').read_text());os.environ.update(e);os.environ.update(PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',SEARCH_EMBED_PREWARM='0',RAG_EAGER_INIT='0',ENABLE_CHECKPOINTER='0',BACKGROUND_JOBS_ENABLED='0',SEARCH_ALLOW_DEMO_FALLBACK='0',DESC_ENRICH_ENABLED='0',USE_MCP_TOOLS='0',TOOLS_OVER_MCP='0',SEARXNG_URL='http://127.0.0.1:8080',CANARY_LOG_PATH='off',DEEPSEEK_BASE_URL='http://127.0.0.1:19094')
for key,name in {'RUNTIME_CACHE_PATH':'cache.sqlite3','SEARCH_LISTING_CACHE_PATH':'listings.sqlite3','SEARCH_CACHE_DB_PATH':'search.sqlite3','AGENT_MEMORY_DB_PATH':'memory','IDEMPOTENCY_DB':'idempotency.sqlite3','MEMORY_GATE_DB':'gate.sqlite3','OTM_DESC_CACHE_PATH':'descriptions.sqlite3','OTM_ACCESS_STATE_PATH':'otm_access.sqlite3','AREA_RECO_CACHE_PATH':'areas.sqlite3'}.items():os.environ[key]=str(B/'runtime'/name)
(B/'runtime/integration').mkdir(exist_ok=True)
import network_guard
network_guard.install()
from core.tool_system import create_tool_registry
from core.tools.web_search import set_tool_registry
registry=create_tool_registry();set_tool_registry(registry);checks=[]
cases=[
 ('search_properties',{'area':'Camden','max_budget':3000,'bedrooms':2,'room_type':'whole','no_commute':True,'confirmed':True}),
 ('get_weather',{'location':'Lewisham London','latitude':51.461,'longitude':-0.012,'target_date':'2026-10-20','target_time':'09:00'}),
 ('get_property_details',{'property_url':'https://www.onthemarket.com/details/19558239/','question':'What is the exact listing price and evidence for accessible access?'}),
 ('get_transport_info',{'query_type':'line_status','line':'Central'}),
 ('get_transport_info',{'query_type':'journey','from_location':'Lewisham station','to_location':'Bank station','date':'2026-10-20','time':'08:45','time_is':'arrival'}),
 ('calculate_commute',{'from_address':'Lewisham station, London','to_address':'Bank station, London','mode':'transit'}),
 ('calculate_commute_cost',{'from_address':'Lewisham station, London','to_address':'Bank station, London','travel_type':'adult','office_days_per_week':3}),
 ('check_transport_cost',{'end_zone':3,'travel_type':'adult','office_days_per_week':3}),
 ('check_safety',{'address':'Bloomsbury London','latitude':51.523,'longitude':-0.127}),
 ('search_nearby_pois',{'address':'Bloomsbury London','latitude':51.523,'longitude':-0.127,'poi_type':'school,gp','radius':300}),
 ('web_search',{'query':'site:gov.uk private renting guide tenancy deposit viewing'}),
 ('compare_or_rank_areas',{'city':'London','candidate_areas':['Camden','Lewisham'],'priorities':['value'],'budget_hint':{'amount':2400,'period':'month'}}),
 ('remember',{'content':'Synthetic formal integration preference: quiet whole home; cat permission must be verified.','user_id':'formal_integration_A','session_id':'formal_one','idempotency_key':'formal-integration-save-one'}),
 ('recall_memory',{'query':'Synthetic formal integration quiet cat permission','user_id':'formal_integration_A','session_id':'formal_two'}),
 ('ask_user',{'question':'Which budget basis applies?','clarification_kind':'budget_ambiguity','missing_fields':['budget_basis'],'options':[{'label':'Whole home total','value':'whole_total'},{'label':'Per person','value':'per_person'}]}),
]
async def main():
 for tool,args in cases:
  started=time.time();record={'tool':tool,'args':args,'started':started,'layer':'direct_tool_integration'}
  try:
   result=await asyncio.wait_for(registry.execute_tool(tool,**args),timeout=30);record['result']=result.to_dict()
  except Exception as e:record['exception']=type(e).__name__
  record['seconds']=round(time.time()-started,3);checks.append(record)
  (B/'evidence/live-tool-integration.json').write_text(json.dumps(checks,indent=2,ensure_ascii=False));print(json.dumps({'tool':tool,'seconds':record['seconds'],'success':record.get('result',{}).get('success'),'exception':record.get('exception')},ensure_ascii=False),flush=True)
 (B/'evidence/live-tool-integration.json').write_text(json.dumps(checks,indent=2,ensure_ascii=False))
asyncio.run(main())
