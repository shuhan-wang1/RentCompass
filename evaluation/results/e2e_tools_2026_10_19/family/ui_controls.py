import sys,json,pathlib
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
folder=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019/family')
b=AuditBrowser(folder,profile='family',mobile=True);p=b.page
snap=lambda:p.evaluate('(()=>{const e=document.activeElement;return {active:{tag:e.tagName,id:e.id,text:e.innerText},menu:document.querySelector("#menuToggle").outerHTML,criteria:document.querySelector("#criteriaHeader").outerHTML,dialog:[...document.querySelectorAll("[role=dialog]")].map(x=>x.outerHTML),width:innerWidth,scrollWidth:document.documentElement.scrollWidth,body:document.body.innerText}})()')
r={}
p.locator('#menuToggle').focus();p.keyboard.press('Enter');p.wait_for_timeout(200);r['menuEnter']=snap();b.screenshot('02-menu-open')
p.keyboard.press('Escape');p.wait_for_timeout(200);r['menuEscape']=snap();b.screenshot('03-menu-escape')
p.locator('#menuToggle').click();p.wait_for_timeout(200)
p.locator('#criteriaHeader').focus();p.keyboard.press('Enter');p.wait_for_timeout(200);r['criteriaEnter']=snap();b.screenshot('04-criteria-enter')
r['criteria_controls']=p.locator('input,button,select,a,textarea').evaluate_all('(els)=>els.filter(e=>e.getBoundingClientRect().width>0).map(e=>({tag:e.tagName,id:e.id,type:e.type,text:e.innerText,placeholder:e.placeholder,aria:e.getAttribute("aria-label"),label:e.labels?[...e.labels].map(x=>x.innerText):[],box:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}}))')
r['keyboard_form']=[]
for i in range(15):
 p.keyboard.press('Tab');r['keyboard_form'].append(p.evaluate('(()=>{const e=document.activeElement,r=e.getBoundingClientRect();return {tag:e.tagName,id:e.id,text:e.innerText,placeholder:e.placeholder,aria:e.getAttribute("aria-label"),box:{x:r.x,y:r.y,width:r.width,height:r.height}}})()'))
r['chat_requests']=p.evaluate('window.__auditRequests')
(folder/'ui-controls.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r,ensure_ascii=False));b.close()
