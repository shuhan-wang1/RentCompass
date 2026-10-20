import sys,json,pathlib
sys.dont_write_bytecode=True
sys.path.insert(0,'/tmp/rentcompass-e2e-repair-20261019/public')
from test_driver import AuditBrowser
f=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/family_retest');pfile=pathlib.Path(sys.argv[1]);name=pfile.stem
b=AuditBrowser(f,profile='family_retest',mobile=True);p=b.page
try:
 response_ids=[]
 def capture_response(response):
  if '/api/alex' in response.url:
   response_ids.append({k:response.headers.get(k) for k in ['x-request-id','x-correlation-id','request-id'] if response.headers.get(k)})
 p.on('response',capture_response)
 p.wait_for_timeout(500);p.wait_for_function('!document.querySelector("#chatInput").disabled')
 pre={'body':p.locator('body').inner_text(),'focus':p.locator('#propertyFocusIndicator').inner_text(),'forms':p.locator('#criteriaBody input,#criteriaBody select').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,type:e.type,value:e.value,checked:e.checked,aria:e.getAttribute("aria-label")}))')}
 if len(sys.argv)>2 and sys.argv[2]=='ask-first':
  p.get_by_role('button',name='Ask AI',exact=True).first.click();pre['ask_ai_requests']=p.evaluate('window.__auditRequests');b.screenshot(name+'-ask-ai')
 b.screenshot(name+'-before')
 r=b.send(pfile.read_text());r['pre']=pre;r['response_ids']=response_ids
 r['post_ui']={'criteria':p.locator('#criteriaBody').inner_text(),'forms':p.locator('#criteriaBody input,#criteriaBody select').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,type:e.type,value:e.value,checked:e.checked,aria:e.getAttribute("aria-label")}))'),'card_count':p.locator('#resultsContent .property-card').count(),'cards':p.locator('#resultsContent').inner_text(),'view_urls':p.get_by_role('link',name='View Deal',exact=True).evaluate_all('(els)=>els.map(e=>e.getAttribute("href"))'),'focus':p.locator('#propertyFocusIndicator').inner_text(),'favorite_count':p.locator('#favCount').inner_text()}

 (f/(name+'-response.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2));b.screenshot(name+'-after')
 q=(r.get('result')or{}).get('payload')or{}
 print(json.dumps({'turn':name,'seconds':r['seconds'],'error':r['error'],'status':(r.get('result')or{}).get('status'),'result':{k:v for k,v in q.items() if k in ['message','conversation_id','turn_id','response_type','options']},'criteria':{k:v for k,v in q.get('search_criteria',q.get('view_state',{}).get('criteria',{})).items() if k not in ['constraint_sources','plans']},'recommendations':[{'address':x.get('address'),'url':x.get('url'),'status':x.get('candidate_status')} for x in q.get('recommendations',[])][:3],'request':r['request'],'response_ids':response_ids,'card_count':r['post_ui']['card_count'],'ui_criteria':r['post_ui']['criteria'][:300]},ensure_ascii=False))
finally:b.close()
