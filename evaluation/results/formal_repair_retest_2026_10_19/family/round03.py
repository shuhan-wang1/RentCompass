from browser import Browser,FOLDER
import json
b=Browser()
try:
 cards=b.page.locator('#resultsContent .property-card')
 for i in range(min(2,cards.count())):
  button=cards.nth(i).get_by_role('button',name='Save',exact=True)
  if button.count():button.click();b.page.wait_for_timeout(300)
 (FOLDER/'favorites-before-round03.json').write_text(json.dumps({'ui':b.ui(),'buttons':cards.locator('button.favorite-btn').evaluate_all('(els)=>els.map(e=>({aria:e.getAttribute("aria-label"),title:e.title,cls:e.className}))')},ensure_ascii=False,indent=2))
 r=b.send((FOLDER/'round03.txt').read_text(),'round03');q=(r.get('result')or{}).get('payload')or{}
 print(json.dumps({'seconds':r['seconds'],'ids':{k:q.get(k) for k in ['conversation_id','turn_id']},'request_id':r['response_ids'],'response':q.get('message'),'criteria':{k:v for k,v in q.get('view_state',{}).get('criteria',{}).items() if k not in ['constraint_sources','plans']},'count':r['network_request_count'],'focus':r['after']['focus'],'cards':r['after']['card_count'],'fav':r['after']['favorite_count']},ensure_ascii=False))
finally:b.close()
