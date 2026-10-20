"""No-model restoration check. Run only after root authorizes the restart window.
Only local application is reachable. Never submits chat or Search.
"""
from browser import Browser,FOLDER
import json,time,sys,re
expected=json.loads((FOLDER/'expected-state.json').read_text())
b=Browser()
try:
 before=b.ui()
 b.page.reload(wait_until='domcontentloaded')
 b.page.locator('#chatInput').wait_for(state='visible')
 b.page.wait_for_function('!document.querySelector("#chatInput").disabled')
 b.page.wait_for_timeout(700)
 after=b.ui()
 cid=expected['conversation_id']
 response=b.context.request.get('http://127.0.0.1:15019/api/conversations/'+cid+'/messages')
 server=response.json() if response.ok else {'http_status':response.status}
 actual=server.get('view_state',{}).get('criteria',{})
 assertions={
  'no_model_requests':len(b.requests)==0,
  'conversation_readable':response.ok,
  'criteria_restored':all(actual.get(k)==v for k,v in expected['criteria'].items()),
  'favorite_count_restored':after['favorite_count']==str(expected['favorite_count']),
  'focus_restored':all(x in after['focus'] for x in expected['focused_addresses']),
  'cards_restored':after['card_count']==expected['card_count'],
  'legacy_tfl_transit_label_removed':'TfL transit:' not in after['cards'],
  'old_37_44_minute_labels_removed':not re.search(r'(?:37|44)\s*min',after['cards'],re.I),
  'explicit_unknown_commute_labels_present':after['cards'].count('Commute not verified for current criteria')>=2,
  'commute_explicitly_unverified':all(re.search(r'unverified|not verified|not a verified',line,re.I) for line in after['cards'].splitlines() if 'min to' in line.lower()),
  'unknown_qualification_retained':'Conditions need verification' in after['cards'],
  'viewport_no_overflow':after['viewport']['scrollWidth']<=after['viewport']['width'],
 }
 result={'time':time.time(),'expected':expected,'before':before,'after':after,'server':server,'assertions':assertions,'model_requests':b.requests}
 (FOLDER/'restart-recovery-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 b.screenshot('restart-recovery-check')
 print(json.dumps({'assertions':assertions,'conversation_id':cid},ensure_ascii=False))
 if not all(assertions.values()):sys.exit(1)
finally:b.close()
