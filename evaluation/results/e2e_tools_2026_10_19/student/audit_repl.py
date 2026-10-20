import sys,json,traceback,pathlib,time
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
sys.dont_write_bytecode=True
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/student')
a=AuditBrowser(folder,profile='student')
def observe_raw():
 a.page.evaluate('''()=>{window.__studentRaw=[];const prior=window.fetch;window.fetch=async(...args)=>{const r=await prior(...args);if(String(args[0]).includes('/api/alex'))r.clone().text().then(t=>window.__studentRaw.push(t));return r;};}''')
def log_ui(command,value=None):
 record={'time':time.time(),'command':command,'body':a.page.locator('body').inner_text(),'value':value}
 with (folder/'student-ui-actions.jsonl').open('a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
 return record
observe_raw();log_ui({'action':'browser_open'});print(json.dumps({'ready':True,'body_tail':a.page.locator('body').inner_text()[-2600:]},ensure_ascii=False),flush=True)
for line in sys.stdin:
 try:
  c=json.loads(line);kind=c['action']
  if kind=='send':
   r=a.send(c['message'],timeout=240000);p=(r.get('result')or{}).get('payload')or{}
   (folder/'student-sse.json').write_text(json.dumps(a.page.evaluate('window.__studentRaw'),ensure_ascii=False,indent=2))
   print(json.dumps({'kind':'turn','message':r['message'],'seconds':r['seconds'],'error':r['error'],'status':(r.get('result')or{}).get('status'),'answer':p.get('message'),'turn_id':p.get('turn_id'),'conversation_id':p.get('conversation_id'),'criteria':p.get('search_criteria',p.get('view_state',{}).get('criteria')),'recommendations':[{'address':x.get('address'),'price':x.get('price'),'bedrooms':x.get('bedrooms'),'room_type':x.get('room_type'),'status':x.get('candidate_status')}for x in p.get('recommendations',[])]},ensure_ascii=False),flush=True)
  elif kind=='eval':
   v=a.page.evaluate(c['js']);log_ui(c,v);print(json.dumps({'value':v},ensure_ascii=False),flush=True)
  elif kind=='click':
   a.page.locator(c['selector']).click(timeout=15000);a.page.wait_for_timeout(c.get('wait',700));rec=log_ui(c);print(json.dumps({'body_tail':rec['body'][-3200:]},ensure_ascii=False),flush=True)
  elif kind=='fill':a.page.locator(c['selector']).fill(c['value']);log_ui({'action':'fill','selector':c['selector'],'redacted':True});print('{"filled":true}',flush=True)
  elif kind=='screenshot':a.screenshot(c['name']);print('{"screenshot":true}',flush=True)
  elif kind=='reload':a.page.reload(wait_until='domcontentloaded');a.page.wait_for_timeout(800);a.install_observer();observe_raw();rec=log_ui(c);print(json.dumps({'body_tail':rec['body'][-3200:]},ensure_ascii=False),flush=True)
  elif kind=='api':
   r=a.api(c['path'],c.get('method','GET'),c.get('data'));(folder/c.get('file','api-evidence.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r,ensure_ascii=False),flush=True)
  elif kind=='export':
   with a.page.expect_download(timeout=15000) as info:a.page.locator(c['selector']).click()
   d=info.value;d.save_as(str(folder/('export-'+d.suggested_filename)));print(json.dumps({'download':d.suggested_filename}),flush=True);log_ui(c)
  elif kind=='save':a.context.storage_state(path=str(a.state));log_ui(c);print('{"saved":true}',flush=True)
  elif kind=='close':log_ui(c);a.close();print('{"closed":true}',flush=True);break
 except Exception as e:print(json.dumps({'error':str(e),'trace':traceback.format_exc()},ensure_ascii=False),flush=True)
