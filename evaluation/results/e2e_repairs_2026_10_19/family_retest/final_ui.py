import sys,pathlib,json
sys.dont_write_bytecode=True
sys.path.insert(0,'/tmp/rentcompass-e2e-repair-20261019/public')
from test_driver import AuditBrowser
f=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/family_retest');b=AuditBrowser(f,profile='family_retest',mobile=True);p=b.page
try:
 p.locator('#chatMessages').get_by_text('For the last focused Camden flat, St. Augustine',exact=False).first.wait_for(state='attached',timeout=30000);p.wait_for_function('!document.querySelector("#chatInput").disabled');p.wait_for_timeout(250)
 r={'user_message_count':p.locator('#chatMessages').inner_text().count('Edit message'),'focus':p.locator('#propertyFocusIndicator').inner_text(),'fav_count':p.locator('#favCount').inner_text(),'criteria':p.locator('#criteriaBody').inner_text(),'forms':p.locator('#criteriaBody input,#criteriaBody select').evaluate_all('(els)=>els.map(e=>({type:e.type,value:e.value,checked:e.checked,aria:e.getAttribute("aria-label")}))'),'cards':p.locator('#resultsContent').inner_text(),'view_links':p.get_by_role('link',name='View Deal',exact=True).evaluate_all('(els)=>els.map(e=>e.getAttribute("href"))')}
 b.screenshot('ui-07-final-restored');r['mobile_checks']=[]
 for width in [320,390]:
  p.set_viewport_size({'width':width,'height':844});p.locator('#menuToggle').click();tabs=[]
  for i in range(12):
   p.keyboard.press('Tab');tabs.append(p.evaluate('({id:document.activeElement.id,inside:!!document.activeElement.closest("#convSidebar")})'))
  p.keyboard.press('Escape');r['mobile_checks'].append({'width':width,'scroll_width':p.evaluate('document.documentElement.scrollWidth'),'tabs':tabs,'expanded_after_escape':p.locator('#menuToggle').get_attribute('aria-expanded'),'focused_after_escape':p.evaluate('document.activeElement.id')})
 r['chat_requests']=p.evaluate('window.__auditRequests');(f/'ui-final.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in r.items() if k not in ['cards','criteria']},ensure_ascii=False))
finally:b.close()
