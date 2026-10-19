import json,pathlib,sys,time
B=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019');sys.path.insert(0,str(B/'public'));from test_driver import AuditBrowser
mode=sys.argv[1];checks=[];outputs=[]
def check(case,value):
 checks.append({'case':case,'passed':bool(value)})
 if not value:raise AssertionError(case)
def payload(r):
 check('chat-completes-with-success',r.get('result',{}).get('status')==200)
 p=r['result']['payload'];outputs.append(p);return p
if mode=='student':
 b=AuditBrowser(B/'student_retest',profile='student')
 try:
  b.page.locator('#newChatBtn').click();b.page.wait_for_timeout(200)
  p=payload(b.send('请按之前已经保存的长期默认继续找房；请使用实际保存的条件，不要参考别人的条件，也不要把允许养猫作为筛选。说明哪些候选的通勤已经核实，哪些超过上限。',timeout=240000))
  c=(p.get('view_state') or {}).get('criteria') or p.get('search_criteria') or {}
  check('defaults-restored-without-restatement',c.get('max_budget')==2400 and c.get('bedrooms')==2 and c.get('room_type')=='whole' and c.get('max_travel_time')==30)
  rows=(p.get('view_state') or {}).get('results') or [];raw=p.get('recommendations') or []
  matched=[]
  for row in raw:
   if not row.get('verified_commute_evidence'):continue
   v=next((v for v in rows if v.get('url')==row.get('url')),None)
   check('same-listing-commute-evidence-in-response-and-card',v is not None and v.get('verified_commute_minutes')==row.get('verified_commute_minutes') and v.get('verified_commute_evidence')==row.get('verified_commute_evidence'))
   matched.append(row.get('url'))
  check('at-least-one-exact-listing-route-replayed',bool(matched))
  check('cards-immediately-visible',b.page.locator('.property-card').count()>0)
  b.screenshot('candidate4-defaults-and-exact-routes')
  cid=p['conversation_id'];before=b.api('/api/conversations/'+cid+'/messages')['body'];b.page.reload(wait_until='networkidle');b.install_observer();b.page.wait_for_timeout(300)
  after=b.api('/api/conversations/'+cid+'/messages')['body'];check('history-and-evidence-survive-browser-refresh',before==after)
  b.screenshot('candidate4-refreshed-routes')
 finally:b.close()
elif mode=='family':
 b=AuditBrowser(B/'family_retest',profile='family_retest')
 try:
  p=payload(b.send('Search Camden only. Set the maximum monthly rent to £3,000, a whole home and at least two bedrooms. Genuine step-free street-to-door access and enough internal wheelchair space are essential. Our cat needs permission. Keep the 50-minute public-transport commute limit to UCL. A lift is only a preference, not a requirement. Search the cached candidates and keep missing access and cat-permission evidence unknown.',timeout=240000))
  c=p['view_state']['criteria'];check('cat-permission-is-an-active-hard-constraint','cats allowed' in c.get('property_features',[]));check('family-area-bedroom-and-budget-correct',c.get('area')=='Camden' and c.get('bedrooms')==2 and c.get('max_budget')==3000)
  rows=p['view_state']['results'];check('unknown-cat-permission-never-green',bool(rows) and all(r.get('qualification',{}).get('status')!='eligible' for r in rows))
  weather='For a separate day trip, what is the current weather in Islington, London? Don\'t run a commute calculation this round. This is only a weather lookup: do not change any housing-search area, budget, bedroom, access, pet or other condition, and do not move the housing search to Islington.'
  q=payload(b.send(weather,timeout=240000));check('all-housing-criteria-unchanged-after-weather',q['view_state']['criteria']==c);check('same-card-set-after-weather',[r.get('url') for r in q['view_state']['results']]==[r.get('url') for r in rows]);check('weather-value-retained','14.2' in q.get('message',''));check('no-false-commute-removal-note','some commute claims' not in q.get('message','').lower());check('no-old-dangling-recap','internal wheelchair space, Islington' not in q.get('message',''))
  b.screenshot('candidate4-cat-permission-and-weather');b.page.reload(wait_until='networkidle');b.install_observer();b.page.wait_for_timeout(300);check('cards-survive-browser-refresh',b.page.locator('.property-card').count()==len(rows));b.screenshot('candidate4-refreshed-family')
 finally:b.close()
else:raise ValueError(mode)
(B/'evidence'/('candidate4-'+mode+'-checks.json')).write_text(json.dumps(checks,ensure_ascii=False,indent=2));(B/'evidence'/('candidate4-'+mode+'-payloads.json')).write_text(json.dumps(outputs,ensure_ascii=False,indent=2));print(json.dumps({'persona':mode,'checks':len(checks),'passed':sum(c['passed'] for c in checks),'failed':[c for c in checks if not c['passed']]},ensure_ascii=False))
