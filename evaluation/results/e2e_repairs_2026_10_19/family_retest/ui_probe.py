import sys,json,pathlib
sys.dont_write_bytecode=True
sys.path.insert(0,'/tmp/rentcompass-e2e-repair-20261019/public')
from test_driver import AuditBrowser
f=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019/family_retest');b=AuditBrowser(f,profile='family_retest',mobile=True);p=b.page;r={}
try:
 p.wait_for_timeout(500);r['closed_tabs']=[]
 for i in range(22):
  p.keyboard.press('Tab');r['closed_tabs'].append(p.evaluate('(()=>{const e=document.activeElement,r=e.getBoundingClientRect();return {id:e.id,text:e.innerText.slice(0,60),x:r.x,y:r.y,w:r.width,h:r.height}})()'))
 p.locator('#menuToggle').click();r['open']={'expanded':p.locator('#menuToggle').get_attribute('aria-expanded'),'focused':p.evaluate('document.activeElement.id')};r['open_tabs']=[]
 for i in range(20):
  p.keyboard.press('Tab');r['open_tabs'].append(p.evaluate('({id:document.activeElement.id,inSidebar:!!document.activeElement.closest("#convSidebar")})'))
 b.screenshot('ui-01-menu-open');p.keyboard.press('Escape');r['escape']={'expanded':p.locator('#menuToggle').get_attribute('aria-expanded'),'focused':p.evaluate('document.activeElement.id')};b.screenshot('ui-02-menu-closed')
 r['labels']=p.locator('#chatInput,#chatMessages,#criteriaBody input,#criteriaBody select').evaluate_all('(els)=>els.map(e=>({id:e.id,tag:e.tagName,type:e.type,aria:e.getAttribute("aria-label"),role:e.getAttribute("role"),live:e.getAttribute("aria-live")}))');r['viewports']=[]
 for w in [320,390]:
  p.set_viewport_size({'width':w,'height':844});r['viewports'].append(p.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})'));b.screenshot('ui-'+str(w))
 r['chat_requests']=p.evaluate('window.__auditRequests');(f/'ui-probe.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r,ensure_ascii=False))
finally:b.close()
