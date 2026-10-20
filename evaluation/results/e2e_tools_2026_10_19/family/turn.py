import sys,json,pathlib
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/family')
message_path=pathlib.Path(sys.argv[1]); message=message_path.read_text()
turn=message_path.stem
b=AuditBrowser(folder,profile='family',mobile=True)
try:
 pre={'body':b.page.locator('body').inner_text(),'url':b.page.url,'forms':b.page.locator('input,select').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,type:e.type,value:e.value,checked:e.checked,placeholder:e.placeholder,labels:e.labels?[...e.labels].map(x=>x.innerText):[]}))')}
 b.screenshot(turn+'-reopened')
 if len(sys.argv)>2 and sys.argv[2]=='ask-first':
  b.page.get_by_role('button',name='Ask AI',exact=True).first.click()
  pre['ask_ai_body']=b.page.locator('body').inner_text();pre['ask_ai_requests']=b.page.evaluate('window.__auditRequests')
  b.screenshot(turn+'-ask-ai')
 r=b.send(message,timeout=180000)
 r['pre']=pre
 (folder/(turn+'-response.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2))
 b.screenshot(turn+'-mobile')
 print(json.dumps({'turn':turn,'message':message,'seconds':r['seconds'],'error':r['error'],'payload_summary':{k:v for k,v in (r['result'] or {}).get('payload',{}).items() if k in ['message','conversation_id','turn_id','response_type']},'criteria':{k:v for k,v in (r['result'] or {}).get('payload',{}).get('search_criteria',{}).items() if k!='constraint_sources'},'recommendation_count':len((r['result'] or {}).get('payload',{}).get('recommendations',[])),'request':r['request']},ensure_ascii=False))
finally:b.close()
