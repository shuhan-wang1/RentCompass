"""Read-only real browser recovery check; every /api/alex is blocked by ui_action inspect."""
import json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
label=sys.argv[1] if len(sys.argv)>1 else 'recovery-after-final-restart'
env=dict(os.environ,PLAYWRIGHT_BROWSERS_PATH='/tmp/rentcompass-live-20261019/browsers',LD_LIBRARY_PATH='/tmp/rentcompass-live-20261019/browser/lib/usr/lib/x86_64-linux-gnu')
before=(ROOT/'chat-requests.jsonl').read_text().splitlines()
p=subprocess.run([sys.executable,str(ROOT/'ui_action.py'),'inspect',label],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
(ROOT/f'{label}-runner.txt').write_text(p.stdout)
if p.returncode:print(p.stdout);raise SystemExit(p.returncode)
records=json.loads((ROOT/f'{label}-local-network.json').read_text())
baseline=json.loads((ROOT/'recovery-baseline.json').read_text())
hist=next((r for r in records if r['path']==f"/api/conversations/{baseline['conversation_id']}/messages"),None)
favorites=next((r.get('response') for r in records if r['path']=='/api/favorites'),{})
actual_favorites=favorites.get('favorites',[]) if isinstance(favorites,dict) else []
checks={'no_model_requests':before==(ROOT/'chat-requests.jsonl').read_text().splitlines(),
 'same_conversation_restored':hist is not None,'favorites_count':len(actual_favorites)==baseline['favorites_count'],
 'favorite_identities':sorted(r.get('url') or r.get('address') or '' for r in actual_favorites)==baseline['favorite_identities']}
if hist:
 view=hist.get('response',{}).get('view_state',{});criteria=view.get('criteria',{})
 for key,value in baseline['criteria'].items():checks[f'criteria_{key}']=criteria.get(key)==value
 checks['visible_result_identities']=sorted(r.get('url') or r.get('address') or '' for r in view.get('results',[]))==baseline['result_identities']
result={'time':time.time(),'checks':checks,'all_passed':all(checks.values()),'label':label}
(ROOT/f'{label}-checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False,indent=2))
raise SystemExit(0 if result['all_passed'] else 1)
