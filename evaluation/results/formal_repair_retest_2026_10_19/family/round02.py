from browser import Browser,FOLDER
import json
b=Browser()
try:
 card=b.page.locator('#resultsContent .property-card').first
 actions=card.locator('button').evaluate_all('(els)=>els.map(e=>({text:e.innerText,aria:e.getAttribute("aria-label"),title:e.title,cls:e.className}))')
 (FOLDER/'round02-card-actions.json').write_text(json.dumps(actions,indent=2))
 card.get_by_role('button',name='Ask AI',exact=True).click()
 b.page.wait_for_timeout(1000)
 if len(b.requests):
  (FOLDER/'round02-unexpected-askai-request.json').write_text(json.dumps(b.requests,indent=2))
  print('Ask AI made model request: stopped without sending another prompt')
 else:
  r=b.send((FOLDER/'round02.txt').read_text(),'round02')
  q=(r.get('result')or{}).get('payload')or{}
  print(json.dumps({'seconds':r['seconds'],'ids':{k:q.get(k) for k in ['conversation_id','turn_id','request_id']},'response':q.get('message') or q.get('response'),'criteria':q.get('view_state',{}).get('criteria'),'count':r['network_request_count'],'focus':r['after']['focus'],'cards':r['after']['card_count']},ensure_ascii=False))
finally:b.close()
