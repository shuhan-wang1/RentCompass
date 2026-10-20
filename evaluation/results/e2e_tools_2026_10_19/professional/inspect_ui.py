import json, pathlib, sys, time
sys.path.insert(0, '/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser

ROOT=pathlib.Path(__file__).parent
b=AuditBrowser(ROOT, profile='professional')
evidence={'time':time.time(),'url':b.page.url}
try:
    b.page.get_by_role('button', name='EN', exact=True).click()
    b.page.wait_for_timeout(250)
    b.screenshot('ui-desktop-initial')
    evidence['desktop'] = b.page.evaluate('''() => ({title:document.title,text:document.body.innerText,
      dimensions:{innerWidth,scrollWidth:document.documentElement.scrollWidth,innerHeight,scrollHeight:document.documentElement.scrollHeight},
      controls:[...document.querySelectorAll('button,input,select,a,textarea')].map(e=>({tag:e.tagName,id:e.id,type:e.type,text:e.innerText,name:e.name,placeholder:e.placeholder,aria:e.getAttribute('aria-label'),visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))})''')
    focused=[]
    b.page.evaluate('document.activeElement.blur()')
    for _ in range(24):
        b.page.keyboard.press('Tab')
        focused.append(b.page.evaluate('''() => {const e=document.activeElement;return {tag:e.tagName,id:e.id,text:e.innerText?.slice(0,100),aria:e.getAttribute('aria-label'),outline:getComputedStyle(e).outline,rect:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height}}}'''))
    evidence['keyboard']=focused
    b.page.locator('#criteriaHeader').focus()
    b.page.keyboard.press('Enter')
    b.page.wait_for_timeout(200)
    evidence['criteria_keyboard'] = b.page.evaluate('''() => ({text:document.body.innerText,controls:[...document.querySelectorAll('input,select')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)).map(e=>({tag:e.tagName,type:e.type,placeholder:e.placeholder,labels:[...(e.labels||[])].map(l=>l.innerText),outer:e.outerHTML.slice(0,1000)}))})''')
    b.screenshot('ui-desktop-criteria-keyboard')
    b.page.set_viewport_size({'width':390,'height':844})
    b.page.wait_for_timeout(500)
    b.screenshot('ui-mobile-initial')
    evidence['mobile'] = b.page.evaluate('''() => ({text:document.body.innerText,dimensions:{innerWidth,scrollWidth:document.documentElement.scrollWidth,innerHeight,scrollHeight:document.documentElement.scrollHeight},controls:[...document.querySelectorAll('button,input,select,a,textarea')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)).map(e=>({tag:e.tagName,id:e.id,text:e.innerText,aria:e.getAttribute('aria-label'),rect:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height}}))})''')
    b.page.locator('#menuToggle').click()
    b.screenshot('ui-mobile-menu')
    evidence['mobile_menu']=b.page.evaluate('''() => ({text:document.body.innerText,menuExpanded:document.querySelector('#menuToggle').getAttribute('aria-expanded'),activeElement:document.activeElement.id})''')
    b.page.keyboard.press('Escape')
    evidence['mobile_escape']=b.page.locator('#newChatBtn').bounding_box()
    evidence['chat_requests']=b.page.evaluate('window.__auditRequests')
    assert not evidence['chat_requests'], 'Passive UI check unexpectedly submitted chat'
    ROOT.joinpath('ui-inspection.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    print(json.dumps(evidence,ensure_ascii=False))
finally:
    b.close()
