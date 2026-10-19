from pathlib import Path
import asyncio,json,time
B=Path('/tmp/rentcompass-fix-live-20261019');ns={};exec((B/'harness/live_tools.py').read_text().split('cases=[')[0],ns)
async def main():
 records=[]
 for uid in ('formal_integration_A','formal_integration_B'):
  result=await ns['registry'].execute_tool('recall_memory',query='Synthetic formal integration quiet cat permission',user_id=uid,session_id='new_process_after_final_restart')
  records.append({'namespace':uid,'result':result.to_dict()})
 a,b=[r['result'].get('data')or{}for r in records];checks={'same_namespace_persists_in_new_process':a.get('count')==1 and 'quiet whole home' in a.get('formatted',''),'other_namespace_empty':b.get('count')==0,'both_reads_successful':all(r['result'].get('success')for r in records)}
 out={'checks':checks,'passed':all(checks.values()),'records':records,'new_python_process':True,'time':time.time()};(B/'evidence/memory-new-process.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(checks));raise SystemExit(0 if out['passed']else 1)
asyncio.run(main())
