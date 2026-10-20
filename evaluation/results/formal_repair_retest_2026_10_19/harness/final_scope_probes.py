from pathlib import Path
import asyncio,json,time
B=Path('/tmp/rentcompass-fix-live-20261019');ns={};exec((B/'harness/live_tools.py').read_text().split('cases=[')[0],ns)
async def main():
 records=[]
 for name,args in [('get_weather',{'location':'Stratford'}),('get_transport_info',{'query_type':'journey','from_location':'Tennyson Road, Stratford','to_location':"King's Cross, London",'date':'2026-10-20','time':'14:00','time_is':'departure'})]:
  start=time.time();r=await asyncio.wait_for(ns['registry'].execute_tool(name,**args),timeout=25);records.append({'tool':name,'args':args,'result':r.to_dict(),'seconds':round(time.time()-start,3)})
 w=records[0]['result'];raw=w.get('data')or{};checks={'bare_stratford_weather_success':w.get('success')is True,'london_canonical_location':raw.get('canonical_location')=='Stratford, London','london_coordinates':51.4<float(raw.get('latitude',0))<51.7 and -.3<float(raw.get('longitude',99))<.3,'no_persona_model_requests':True}
 out={'layer':'direct_registry_after_final_persona_budget_exhausted_not_a_new_ui_turn','records':records,'checks':checks,'passed':all(checks.values())};(B/'evidence/final-location-scope-probes.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps({'checks':checks,'journey_success':records[1]['result'].get('success'),'journey_error':records[1]['result'].get('error')},ensure_ascii=False));raise SystemExit(0 if out['passed']else 1)
asyncio.run(main())
