"""Independent black-box audit. No chat is sent without --start-chat."""
import argparse,json,pathlib,sys,time
sys.path.insert(0,'/tmp/rentcompass-e2e-tools-20261019/public')
from test_driver import AuditBrowser
ROOT=pathlib.Path(__file__).parent
P=argparse.ArgumentParser()
P.add_argument('--start-chat',action='store_true')
P.add_argument('--turn',type=int)
P.add_argument('--message')
P.add_argument('--message-file')
P.add_argument('--action',choices=['inspect','refresh','new','return','listing','save','criteria','form','ask'])
P.add_argument('--index',type=int,default=0)
P.add_argument('--mobile',action='store_true')
A=P.parse_args()
JOURNAL=ROOT/'request-budget.jsonl'
old=[json.loads(x) for x in JOURNAL.read_text().splitlines()] if JOURNAL.exists() else []
chat=A.turn is not None or A.action=='form'
if chat and not A.start_chat:P.error('Chat awaits explicit START authorization; pass --start-chat only after it arrives.')
if chat and sum(not x.get('no_request',False) for x in old)>=14:P.error('14-request audit budget exhausted.')
if A.message and A.message_file:P.error('Choose --message or --message-file.')
b=AuditBrowser(ROOT,profile='professional',mobile=A.mobile)
start=time.time()
label=(f'turn-{A.turn:02d}' if A.turn is not None else f'ui-{A.action or "inspect"}-{int(start)}')
try:
    b.page.get_by_role('button',name='EN',exact=True).click()
    if A.mobile and A.action in ['new','return']:
        b.page.locator('#menuToggle').click()
    if A.action=='refresh':
        before=b.page.locator('#chatMessages').inner_text()
        b.page.reload(wait_until='domcontentloaded');b.page.locator('#chatInput').wait_for(state='visible');b.page.wait_for_timeout(750)
        result={'before':before,'after':b.page.locator('#chatMessages').inner_text()}
    elif A.action=='new':
        before=b.page.locator('#chatMessages').inner_text();b.page.locator('#newChatBtn').click();b.page.wait_for_timeout(300)
        result={'before':before,'after':b.page.locator('#chatMessages').inner_text()}
    elif A.action=='return':
        items=b.page.locator('.conv-item-main')
        if not items.count():raise RuntimeError('Inspect conversation selectors before selecting a history item.')
        items.nth(A.index).click();b.page.wait_for_timeout(500)
        result={'dom':b.page.locator('#chatMessages').inner_text()}
    elif A.action in ['listing','save']:
        # Use an on-screen property card; blocklist in the shared driver prevents external navigation.
        candidates=b.page.locator('.property-card')
        if not candidates.count():raise RuntimeError('No .property-card. Inspect rendered DOM and adapt before clicking.')
        card=candidates.nth(A.index)
        if A.action=='listing':card.click()
        else:card.get_by_role('button',name='Save',exact=True).click()
        b.page.wait_for_timeout(500)
        result={'dom':b.page.locator('body').inner_text()}
    elif A.action=='criteria':
        b.page.locator('#criteriaHeader').click();b.page.wait_for_timeout(200)
        result={'dom':b.page.locator('body').inner_text()}
    elif chat:
        message=pathlib.Path(A.message_file).read_text() if A.message_file else A.message
        if A.action=='form':message='UI form: Camden; rent budget 1500/month; exact studio (0 bedrooms); room type Studio; no commute.'
        elif A.action=='ask':message='UI Ask AI on first actual returned property card.'
        elif not message:
            plan=json.loads((ROOT/'turn-plan.json').read_text())
            message=next(x['message'] for x in plan if x['turn']==A.turn)
        if old:
            delay=5-(time.time()-old[-1]['start'])
            if delay>0:time.sleep(delay)
        reservation={'turn':A.turn,'start':time.time(),'message':message}
        with JOURNAL.open('a') as f:f.write(json.dumps(reservation)+'\n')
        if A.action in ['form','ask']:
            if A.action=='form':
                area=b.page.get_by_placeholder('e.g. Camden — Enter to add',exact=True)
                if not area.is_visible():b.page.locator('#criteriaHeader').click()
                area.fill('Camden');area.press('Enter')
                b.page.get_by_placeholder('e.g. 1500',exact=True).fill('1500')
                selects=b.page.locator('.criteria-select')
                selects.nth(0).select_option('month');selects.nth(1).select_option('0');selects.nth(2).select_option('studio')
                b.page.get_by_label("I don't commute",exact=True).check()
            else:b.page.locator('#btn-results').click()
            b.install_observer();before=b.page.evaluate('window.__auditResults.length');t=time.time();error=None
            direct_response=None
            if A.action=='form':
                with b.page.expect_response(lambda r:'/api/search_direct' in r.url,timeout=240000) as response_info:
                    b.page.locator('.criteria-search-btn').click()
                direct_response=response_info.value
            else:
                b.page.locator('.property-card').first.get_by_role('button',name='Ask AI',exact=True).click()
                b.page.wait_for_timeout(400)
                if not b.page.evaluate('window.__auditRequests.length'):
                    if not b.page.locator('#chatInput').input_value():
                        b.page.locator('#chatInput').fill("Please verify this exact property's room type, price and whether it meets my current requirements, including my latest two-day commute and rent-plus-travel budget. Distinguish details you can verify from anything missing.")
                    b.page.locator('#sendBtn').click()
            try:
                if A.action!='form':b.page.wait_for_function(f'window.__auditResults.length>{before}',timeout=240000)
            except Exception as e:error=str(e)
            b.page.wait_for_timeout(250)
            result={'time':time.time(),'message':message,'seconds':round(time.time()-t,3),'error':error,
                'result':{'status':direct_response.status,'payload':direct_response.json()} if direct_response else b.page.evaluate(f'window.__auditResults[{before}]||null'),
                'request':b.page.evaluate('window.__auditRequests.at(-1)||null'),
                'dom':b.page.locator('#chatMessages').inner_text(),'criteria':b.page.locator('body').inner_text()[:12000],
                'stages':b.page.evaluate('window.__auditStages')}
            with b.log.open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
        else:result=b.send(message,timeout=240000)
        payload=(result.get('result') or {}).get('payload') or {}
        print(json.dumps({'turn':A.turn,'started':reservation['start'],'finished':time.time(),'seconds':result['seconds'],'error':result['error'],'status':(result.get('result') or {}).get('status'),'message':payload.get('message'),'criteria':payload.get('search_criteria'),'conversation_id':payload.get('conversation_id'),'recommendations_count':len(payload.get('recommendations',[])),'request_count_in_page':b.page.evaluate('window.__auditRequests.length')},ensure_ascii=False))
    else:
        result={'dom':b.page.locator('body').inner_text(),'elements':b.page.evaluate('''() => [...document.querySelectorAll('button,[role=button],.property-card,.conversation-item')].map(e=>({tag:e.tagName,id:e.id,cls:e.className,text:e.innerText?.slice(0,400),aria:e.getAttribute('aria-label')}))''')}
    b.screenshot(label)
    evidence={'start':start,'end':time.time(),'action':A.action,'turn':A.turn,'result':result,'url':b.page.url,
              'body':b.page.locator('body').inner_text(),'requests':b.page.evaluate('window.__auditRequests||[]')}
    (ROOT/(label+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    if not chat:print(json.dumps(evidence,ensure_ascii=False))
finally:b.close()
