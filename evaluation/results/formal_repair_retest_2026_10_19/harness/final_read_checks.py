from pathlib import Path
import json,requests,time
B=Path('/tmp/rentcompass-fix-live-20261019');U='http://127.0.0.1:15019';before=(B/'evidence/model-calls.jsonl').read_bytes();records={}
profiles={'student':('student-storage.json','recovery-baseline.json'),'professional':('professional-private-storage.json','expected-state.json'),'family':('family-live-storage.json','expected-state.json')}
for name,(storage_name,expected_name)in profiles.items():
 expected=json.loads((B/name/expected_name).read_text());storage=json.loads((B/name/storage_name).read_text());session=requests.Session()
 for cookie in storage.get('cookies',[]):session.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie.get('path','/'))
 r=session.get(U+'/api/conversations/'+expected['conversation_id']+'/messages',timeout=15);body=r.json();view=body.get('view_state')or{};criteria=view.get('criteria')or{};f=session.get(U+'/api/favorites',timeout=15).json().get('favorites')or[]
 checks={'conversation_readable':r.status_code==200,'criteria_restored':all(criteria.get(k)==v for k,v in expected['criteria'].items()),'two_favorites':len(f)==2}
 if name=='professional':checks['budget_alias_consistent']=criteria.get('max_budget')==criteria.get('max_budget_monthly')==2000
 if name=='family':
  rows=view.get('results')or[];checks['no_stale_tfl_display']=all('TfL transit:' not in row.get('explanation','')for row in rows);checks['unknown_commute_labelled']=all('not verified'in row.get('travel_time','').lower()or'unverified'in row.get('travel_time','').lower()for row in rows)
 records[name]={'checks':checks,'status':r.status_code,'view_state':view,'favorite_identities':[row.get('url')for row in f]}
checks={'no_new_model_calls':before==(B/'evidence/model-calls.jsonl').read_bytes(),'all_profiles_passed':all(all(r['checks'].values())for r in records.values())};out={'at':time.time(),'exact_final_source':True,'checks':checks,'profiles':records};(B/'evidence/final-authenticated-read-checks.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps({'checks':checks,'profiles':{k:v['checks']for k,v in records.items()}}));assert all(checks.values())
