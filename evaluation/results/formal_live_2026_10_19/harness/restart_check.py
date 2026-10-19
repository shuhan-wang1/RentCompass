from pathlib import Path
import json,urllib.request,hashlib,os,signal,subprocess,time
B=Path('/tmp/rentcompass-live-20261019');actors=[('student','student_ucl','8d6e107e605d4b81af03463111d59118'),('professional','professional','f108b7f2a8d048eca32f83ed764a7135'),('family','family','31d24203be794c72982a020ccf45b0f8')];records=[];cookies={}
for who,profile,cid in actors:
 p=B/who/(profile+'-storage.json')
 if not p.exists():
  candidates=list((B/who).glob('*storage.json'));assert len(candidates)==1;candidates=candidates[0];p=candidates
 d=json.loads(p.read_text());cookies[who]='; '.join(c['name']+'='+c['value'] for c in d.get('cookies',[]))
def get(who,cid):
 req=urllib.request.Request('http://127.0.0.1:15009/api/conversations/'+cid+'/messages',headers={'Cookie':cookies[who]})
 with urllib.request.urlopen(req,timeout=10) as r:d=json.load(r);return {'status':r.status,'sha256':hashlib.sha256(json.dumps(d,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'message_count':len(d.get('messages',[]))}
before={who:get(who,cid) for who,_,cid in actors};ids=json.loads((B/'private/pids.json').read_text());os.killpg(ids['app'],signal.SIGTERM)
for _ in range(30):
 try:os.kill(ids['app'],0);time.sleep(.1)
 except ProcessLookupError:break
# Reuse the environment builder, without its process-launch section.
namespace={};exec((B/'harness/start.py').read_text().split('pids={}')[0],namespace);e=namespace['e'];P=namespace['P'];f=(B/'private/app.log').open('a');p=subprocess.Popen([P,str(B/'harness/boot.py')],cwd=B/'source',env=e,stdout=f,stderr=subprocess.STDOUT,start_new_session=True);ids['app']=p.pid;(B/'private/pids.json').write_text(json.dumps(ids))
for _ in range(90):
 try:
  with urllib.request.urlopen('http://127.0.0.1:15009/ready',timeout=1) as r:
   if r.status==200:break
 except Exception:time.sleep(.5)
else:raise RuntimeError('restarted formal test app not ready')
for who,_,cid in actors:
 after=get(who,cid);records.append({'persona':who,'case':'same-signed-session-restores-identical-history-after-process-restart','before':before[who],'after':after,'passed':before[who]==after})
(B/'evidence/restart-checks.json').write_text(json.dumps(records,indent=2));print(json.dumps(records,indent=2))
