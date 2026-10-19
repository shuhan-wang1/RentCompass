from pathlib import Path
import asyncio,json,time
B=Path('/tmp/rentcompass-fix-live-20261019')
ns={};exec((B/'harness/live_tools.py').read_text().split('cases=[')[0],ns)
async def main():
 args={'query_type':'fare','from_location':'Stratford station, London','to_location':'Bank station, London'}
 started=time.time();r=await asyncio.wait_for(ns['registry'].execute_tool('get_transport_info',**args),timeout=25)
 out={'layer':'separate_direct_registry_diagnostic_after_professional_local_audit_cap','args':args,'result':r.to_dict(),'seconds':round(time.time()-started,3),'no_persona_model_requests':True}
 (B/'evidence/final-fare-diagnostic.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
 print(json.dumps({'success':r.success,'seconds':out['seconds'],'fare_gbp':(r.data or{}).get('fare_gbp'),'error':r.error},ensure_ascii=False))
asyncio.run(main())
