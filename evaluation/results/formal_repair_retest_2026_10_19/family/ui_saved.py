from browser import Browser,FOLDER
import json
b=Browser()
try:
 before=b.ui();b.page.locator('#btn-favorites').click();b.page.wait_for_timeout(500)
 saved={'body':b.page.locator('body').inner_text(),'cards':b.page.locator('.property-card').evaluate_all('(els)=>els.map(e=>({text:e.innerText,parent:e.parentElement.id}))')}
 b.screenshot('ui-saved-after-round06')
 b.page.locator('#btn-results').click();b.page.locator('#menuToggle').tap();b.page.wait_for_timeout(200)
 menu=b.page.locator('#convSidebar').evaluate('(e)=>({text:e.innerText,controls:[...e.querySelectorAll("button,a,[role=button],[data-id],[data-conversation-id]")].map(x=>({tag:x.tagName,cls:x.className,id:x.id,text:x.innerText,outer:x.outerHTML.slice(0,600)}))})')
 b.page.keyboard.press('Escape')
 result={'before':before,'saved':saved,'menu':menu,'after':b.ui(),'model_requests':b.requests}
 (FOLDER/'ui-saved-after-round06.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 print(json.dumps({'saved':saved,'menu':menu,'model_requests':len(b.requests)},ensure_ascii=False))
finally:b.close()
