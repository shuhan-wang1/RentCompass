#!/usr/bin/env python3
"""Browser regression checks for the UI with every request fulfilled locally.

No app, model, account, or external website is contacted. Install Playwright locally
or use the already-provisioned audit runtime; no dependency downloads are attempted.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

AUDIT_LIBS = Path('/tmp/carbonminer-update-20261016/browser-deps/usr/lib/x86_64-linux-gnu')
if AUDIT_LIBS.exists():
    os.environ['LD_LIBRARY_PATH'] = str(AUDIT_LIBS) + (':' + os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else '')
if Path('/tmp/carbonminer-qa-python').exists():
    sys.path.insert(0, '/tmp/carbonminer-qa-python')
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://rentcompass-offline.test'
DEFAULT_CHROME = '/tmp/carbonminer-qa-browsers/chromium-1248/chrome-linux64/chrome'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', default=os.environ.get('CHROMIUM_PATH', DEFAULT_CHROME))
    parser.add_argument('--output', type=Path, default=Path('/tmp/rentcompass-ui-offline'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    html = (ROOT / 'app/unified-ui.html').read_text()
    criteria = {'area': 'Watford', 'areas': ['Watford'], 'max_budget': 2000, 'budget_period': 'month', 'bedrooms': 2, 'bedrooms_minimum': True, 'room_type': 'whole'}
    property_a = {'address': 'Family flat A, Watford', 'url': 'https://fixtures.invalid/property-a', 'price': '£1600/month', 'area': 'Watford', 'rank': 1, 'possibly_outdated': True, 'candidate_status': 'unknown', 'candidate_unknown_reasons': ['required features are not verified: step-free, lift, wheelchair_space_68cm, cat_written_consent', 'commute tool skipped']}
    property_b = dict(property_a, address='Family flat B, Camden', url='https://fixtures.invalid/property-b', area='Camden')
    conversations = [{'id': 'conv-a', 'title': 'Family A'}, {'id': 'conv-b', 'title': 'Family B'}]
    history = {'conv-a': [], 'conv-b': []}
    state_overrides = {}
    requests, queue, blocked, page_errors, checks = [], [], [], [], []

    def state(cid, revision=1):
        if cid in state_overrides:
            return copy.deepcopy(state_overrides[cid])
        c = dict(criteria)
        if cid == 'conv-b':
            c.update(area='Camden', areas=['Camden'])
        return {'operation': 'replace', 'revision': revision, 'criteria': c, 'results': [property_b if cid == 'conv-b' else property_a]}

    def route_request(route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc != urlparse(ORIGIN).netloc:
            blocked.append(request.resource_type)
            route.abort('blockedbyclient')
            return
        path = parsed.path
        payload, status, headers = {}, 200, {}
        if path == '/':
            route.fulfill(body=html, content_type='text/html')
            return
        if path == '/api/auth/me':
            payload = {'authenticated': False}
        elif path == '/api/conversations' and request.method == 'GET':
            payload = {'conversations': conversations}
        elif path.startswith('/api/conversations/') and request.method == 'DELETE':
            cid = path.split('/')[-1]
            conversations[:] = [item for item in conversations if item['id'] != cid]
            history.pop(cid, None)
            payload = {'success': True}
        elif path.endswith('/messages'):
            cid = path.split('/')[-2]
            payload = {'messages': history.get(cid, []), 'view_state': state(cid)}
        elif path.endswith('/version_map'):
            payload = {'version_map': {}}
        elif path == '/api/favorites':
            payload = {'favorites': []}
        elif path == '/api/alex':
            body = request.post_data_json
            requests.append(body)
            item = queue.pop(0) if queue else {'payload': {'message': 'Offline answer.', 'response_type': 'chat'}}
            status = item.get('status', 200)
            headers = item.get('headers', {})
            payload = dict(item.get('payload', {}))
            cid = body.get('conversation_id', 'conv-new')
            payload.setdefault('conversation_id', cid)
            if status == 200 and payload.get('response_type') != 'error':
                history.setdefault(cid, []).extend([{'role': 'user', 'content': body['message']}, {'role': 'assistant', 'content': payload.get('message', ''), 'response_type': payload.get('response_type', 'chat'), 'options': payload.get('options')}])
            if item.get('stream'):
                stream = 'event: final\ndata: ' + json.dumps({'status': item.get('final_status', status), 'payload': payload}) + '\n\n'
                route.fulfill(status=200, body=stream, content_type='text/event-stream', headers=headers)
                return
            if item.get('interrupted'):
                route.fulfill(status=200, body='event: step\ndata: {"stage":"searching"}\n\n', content_type='text/event-stream')
                return
        else:
            route.fulfill(status=404, body='{}', content_type='application/json')
            return
        route.fulfill(status=status, body=json.dumps(payload), content_type='application/json', headers=headers)

    def check(name, condition):
        assert condition, name
        checks.append(name)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser if Path(args.browser).exists() else None, headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 390, 'height': 844})
        context.route('**/*', route_request)
        context.add_init_script("if(window.top===window){if(!localStorage.getItem('ukra_user_id'))localStorage.setItem('ukra_user_id','offline-user'); if(!localStorage.getItem('ukra_active_conversation'))localStorage.setItem('ukra_active_conversation','conv-a');}")
        page = context.new_page()
        page.on('pageerror', lambda error: page_errors.append(str(error)))

        def settled(cid='conv-a'):
            page.wait_for_function('(cid)=>mainPane && mainPane.conversationId===cid && !mainPane.isLoading && !mainPane.isProcessing', arg=cid)

        def send(message, payload=None, **item):
            if payload is not None:
                item['payload'] = payload
            queue.append(item)
            count = len(requests)
            page.locator('#chatInput').fill(message)
            page.locator('#sendBtn').click()
            page.wait_for_function('()=>!mainPane.isProcessing')
            assert len(requests) == count + 1, f'exactly one request for {message}'

        def switch(cid):
            page.locator('#menuToggle').click()
            title = 'Family A' if cid == 'conv-a' else 'Family B'
            page.locator('#convList').get_by_text(title, exact=True).click()
            settled(cid)

        page.goto(ORIGIN, wait_until='domcontentloaded')
        settled()
        check('mobile closed sidebar is inert', page.locator('#convSidebar').evaluate('(e)=>e.inert'))
        tab_targets = []
        for _ in range(25):
            page.keyboard.press('Tab')
            tab_targets.append(page.evaluate('document.activeElement.id'))
        check('closed sidebar has no invisible Tab stops', not {'newChatBtn', 'userMenuBtn'}.intersection(tab_targets))
        page.locator('#menuToggle').click()
        check('menu announces expanded and focuses first action', page.locator('#menuToggle').get_attribute('aria-expanded') == 'true' and page.evaluate('document.activeElement.id') == 'newChatBtn')
        check('open menu makes background inert', page.locator('#chatPanesWrap').evaluate('(e)=>e.inert'))
        for _ in range(25):
            page.keyboard.press('Tab')
            check('open menu Tab stays in boundary', page.evaluate("document.activeElement.id==='menuToggle'||!!document.activeElement.closest('#convSidebar')"))
        page.keyboard.press('Escape')
        check('Escape closes menu and restores trigger focus', page.locator('#menuToggle').get_attribute('aria-expanded') == 'false' and page.evaluate('document.activeElement.id') == 'menuToggle')
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.wait_for_function("!document.getElementById('convSidebar').inert && !document.getElementById('chatPanesWrap').inert")
        check('desktop sidebar and content remain usable', not page.locator('#convSidebar').evaluate('(e)=>e.inert') and not page.locator('#chatPanesWrap').evaluate('(e)=>e.inert'))
        page.set_viewport_size({'width': 390, 'height': 844})
        check('mobile textarea has explicit accessible name', page.get_by_role('textbox', name='Message Alex', exact=True).count() == 1)
        check('area and budget period have accessible names', page.get_by_role('textbox', name='Area', exact=True).count() == 1 and page.get_by_role('combobox', name='Budget period', exact=True).count() == 1)
        check('message log announces additions', page.locator('#chatMessages').get_attribute('role') == 'log' and page.locator('#chatMessages').get_attribute('aria-live') == 'polite')

        page.locator('#menuToggle').click()
        page.locator('#userMenuBtn').click()
        page.locator('#loginBtn').click()
        check('account dialog closes mobile sidebar before taking focus', page.locator('#menuToggle').get_attribute('aria-expanded') == 'false' and page.locator('#dialogOverlay [role=dialog]').count() == 1)
        check('account fields have explicit names', page.locator('#dialogOverlay input').first.get_attribute('aria-label') is not None)
        for _ in range(12):
            page.keyboard.press('Tab')
            check('dialog Tab stays inside modal', page.evaluate("!!document.activeElement.closest('#dialogOverlay')"))
        page.keyboard.press('Escape')
        check('closing dialog restores usable page and trigger focus', not page.locator('.main-container').evaluate('(e)=>e.inert') and page.evaluate('document.activeElement.id') == 'menuToggle')

        conversations[0]['title'] = '轮椅家庭带猫找房与学校通勤预算确认' * 8
        page.reload(wait_until='domcontentloaded')
        settled()
        page.locator('#menuToggle').click()
        page.wait_for_function("Math.abs(document.getElementById('convSidebar').getBoundingClientRect().left)<0.5")
        bounds = page.locator('.conv-item.active .conv-item-actions button').evaluate_all("""buttons => buttons.map(button => {
            const r=button.getBoundingClientRect(), sidebar=document.getElementById('convSidebar').getBoundingClientRect();
            return {rect:r.toJSON(),sidebar:sidebar.toJSON(),inside:r.left>=sidebar.left && r.right<=sidebar.right, hit:button.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))};
        })""")
        (args.output / 'long-title-bounds.json').write_text(json.dumps(bounds, indent=2))
        page.screenshot(path=str(args.output / 'mobile-long-title.png'), full_page=True)
        check('long Chinese title keeps all four actions visible and pointer reachable', len(bounds)==4 and all(item['inside'] and item['hit'] for item in bounds))
        page.keyboard.press('Escape')
        conversations[0]['title'] = 'Family A'
        page.reload(wait_until='domcontentloaded')
        settled()
        page.get_by_role('button', name='Ask AI', exact=True).first.click()
        check('Ask AI focuses without sending', len(requests) == 0 and page.evaluate('document.activeElement.id') == 'chatInput')
        page.reload(wait_until='domcontentloaded')
        settled()
        check('focus chip survives refresh', 'Family flat A' in page.locator('#propertyFocusIndicator').inner_text())
        send('Does this selected flat have step-free access?', {'message': 'Access is unverified.', 'response_type': 'chat'})
        check('refreshed request keeps exact selected URL', requests[-1]['context']['property']['url'] == property_a['url'] and requests[-1]['context']['focus_stack'][-1]['url'] == property_a['url'])
        switch('conv-b')
        check('conversation B has no A focus chip', page.locator('#propertyFocusIndicator').inner_text() == '')
        send('What is currently selected?', {'message': 'No selected property.', 'response_type': 'chat'})
        check('conversation B request excludes A history and focus', requests[-1]['context']['viewed_properties'] == [] and 'focus_stack' not in requests[-1]['context'])
        page.get_by_role('button', name='Ask AI', exact=True).first.click()
        switch('conv-a')
        check('switching back restores only conversation A focus', page.evaluate('mainPane.focusStack.map(x=>x.url)') == [property_a['url']])

        options = [{'label': 'A: Keep £2,000', 'value': 'Keep my maximum budget at £2000 per month.'}, {'label': 'B: Use £2,500', 'value': 'Replace my maximum budget with £2500 per month.'}, {'label': '<img onerror=alert(1)> C: Ask later', 'value': 'Leave the budget unresolved.'}]
        send('Which budget should apply?', {'message': 'Choose the budget.', 'response_type': 'clarification', 'options': options})
        count = len(requests)
        check('clarification renders native buttons without autosend', page.locator('.clarification-option').count() == 3 and len(requests) == count)
        check('option labels are escaped text', page.locator('.clarification-options img').count() == 0)
        check('clarification rendering leaves criteria unchanged', page.locator('#criteriaBody input[type=number]').first.input_value() == '2000')
        revised = state('conv-a', 2)
        revised['criteria']['max_budget'] = 2500
        queue.append({'payload': {'message': 'Budget updated.', 'response_type': 'chat', 'view_state': revised}})
        page.get_by_role('button', name='B: Use £2,500', exact=True).focus()
        page.keyboard.press('Enter')
        settled()
        check('keyboard choice sends its explicit value once', len(requests) == count + 1 and requests[-1]['message'] == options[1]['value'] and requests[-1]['is_continuation'])
        check('criteria updates only after chosen response', page.locator('#criteriaBody input[type=number]').first.input_value() == '2500')
        check('consumed clarification buttons cannot resend', page.locator('.clarification-option:not([disabled])').count() == 0)
        send('A text-only clarification please', {'message': 'Which date?', 'response_type': 'clarification'})
        send('Next month', {'message': 'Noted.', 'response_type': 'chat'})
        check('text-only clarification stays compatible', requests[-1]['is_continuation'])

        send('Rate limit example', {'error_code': 'rate_limited', 'message': 'raw limit'}, status=429, headers={'Retry-After': '2'})
        check('429 shows wait duration rather than connection error', 'wait 2 seconds' in page.locator('#chatMessages').inner_text() and 'Connection error' not in page.locator('#chatMessages').inner_text())
        count = len(requests)
        page.locator('#chatInput').fill('Wait for the deadline')
        page.locator('#sendBtn').click()
        check('Retry-After prevents early duplicate request', len(requests) == count and page.locator('#chatInput').input_value() == 'Wait for the deadline')
        page.wait_for_timeout(2100)
        send('Resume after deadline', {'message': 'Recovered.', 'response_type': 'chat'})
        check('manual send recovers after Retry-After', 'Recovered.' in page.locator('#chatMessages').inner_text())
        send('Service failure', {'error_code': 'llm_unavailable'}, status=503)
        check('503 has service-specific error', 'service is temporarily unavailable' in page.locator('#chatMessages').inner_text())
        send('Invalid input', {'error_code': 'invalid_request'}, status=400)
        check('400 has input-specific error', 'check your request' in page.locator('#chatMessages').inner_text())
        send('SSE rate limit', {'error_code': 'rate_limited', 'retry_after': 1}, stream=True, final_status=429)
        check('SSE final status preserves rate-limit details', 'wait 1 seconds' in page.locator('#chatMessages').inner_text())
        page.wait_for_timeout(1100)
        send('Interrupted stream', {}, interrupted=True)
        check('incomplete stream has recovery-specific error', 'response was interrupted' in page.locator('#chatMessages').inner_text())

        weekly = dict(property_a, price='£600/week')
        weekly_state = state('conv-a', 3)
        weekly_state['results'] = [weekly]
        weekly_state['criteria']['max_budget'] = 2166.67
        send('Show weekly rent', {'message': 'Weekly listing.', 'response_type': 'search', 'recommendations': [weekly], 'view_state': weekly_state})
        check('penny budget survives panel fill and unchanged form read', page.get_by_role('spinbutton', name='Budget', exact=True).input_value()=='2166.67' and page.evaluate('panelFormApi.read().max_budget')==2166.67)
        check('weekly listing rent is converted before budget comparison', 'Over Budget' in page.locator('#resultsContent').inner_text())
        check('precise hard requirements use readable English labels', '68 cm wheelchair' in page.locator('.property-evidence').inner_text() and 'written permission for the cat' in page.locator('.property-evidence').inner_text() and 'wheelchair_space_68cm' not in page.locator('.property-evidence').inner_text())
        page.locator('[data-lang=zh]').click()
        evidence = page.locator('.property-evidence').inner_text()
        check('evidence and input labels localize to Chinese', '以下必要条件尚未核实：无台阶通行、电梯' in evidence and '仍需核实通勤' in evidence and page.get_by_role('textbox', name='向 Alex 发送消息', exact=True).count() == 1)
        check('precise hard requirements use readable Chinese labels', '68cm轮椅' in evidence and '猫的书面饲养许可' in evidence and 'cat_written_consent' not in evidence)
        check('cached and budget labels localize to Chinese', '缓存房源可能已变化' in evidence and '超出预算' in page.locator('#resultsContent').inner_text())
        blocked_state = state('conv-a', 4)
        blocked_state.update(results=[], retrieval={'source_status':'blocked','partial':True,
            'retrieval_complete':False,'source_message':'OnTheMarket访问受限（HTTP 403），找房条件已保留。'})
        state_overrides['conv-a'] = blocked_state
        send('检查来源故障显示', {'message':'来源暂不可用。','response_type':'search','recommendations':[], 'view_state':blocked_state})
        check('source refusal has a dedicated results state', '房源来源暂不可用' in page.locator('#resultsContent').inner_text() and '403' in page.locator('#resultsContent').inner_text())
        check('source refusal does not suggest widening the budget', '未找到房源' not in page.locator('#resultsContent').inner_text())
        page.reload(wait_until='domcontentloaded')
        settled()
        check('source refusal remains distinct after refresh', '房源来源暂不可用' in page.locator('#resultsContent').inner_text())
        recovered = dict(blocked_state, revision=5, results=[property_a], retrieval={'source_status':'ok','partial':False})
        state_overrides['conv-a'] = recovered
        send('重新搜索', {'message':'已恢复。','response_type':'search','recommendations':[property_a],'view_state':recovered})
        check('successful retry clears previous refusal', 'Family flat A' in page.locator('#resultsContent').inner_text() and '房源来源暂不可用' not in page.locator('#resultsContent').inner_text())
        # Saved cards explain their historical qualification even when the current
        # conversation has a different commute limit and both original ranks were #1.
        page.evaluate("""() => {
            favoriteItems = [1,2].map(i => ({address:'Saved flat '+i,url:'https://fixtures.invalid/saved-'+i,
                rank:1,price:'£1800 pcm',budget_basis:2000,qualification:{status:'eligible',
                constraints:{max_commute_minutes:45},reasons:[],unknown_reasons:[]}}));
            setActiveTab('favorites'); renderFavorites();
        }""")
        check('saved qualification explicitly names saved criteria', '已验证满足收藏时条件' in page.locator('#resultsContent').inner_text())
        check('saved commute ceiling remains visible', '收藏时通勤上限：45 分钟' in page.locator('#resultsContent').inner_text())
        check('saved ranks reflect saved list order', page.locator('#resultsContent .rank-badge').all_text_contents() == ['#1','#2'])
        page.evaluate("document.getElementById('btn-results').click()")
        page.screenshot(path=str(args.output / 'mobile-repaired.png'), full_page=True)
        for width in [320, 390]:
            page.set_viewport_size({'width': width, 'height': 844})
            check(f'{width}px layout does not overflow horizontally', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))

        # New browser context models process/page restart without sharing JS memory.
        saved_state = context.storage_state()
        context.close()
        context = browser.new_context(storage_state=saved_state, viewport={'width': 390, 'height': 844})
        context.route('**/*', route_request)
        page = context.new_page()
        page.on('pageerror', lambda error: page_errors.append(str(error)))
        page.goto(ORIGIN, wait_until='domcontentloaded')
        settled()
        check('fresh browser context restores exact A focus', page.evaluate('mainPane.focusStack.map(x=>x.url)') == [property_a['url']])
        history['conv-a'] = [{'role': 'assistant', 'content': 'Old choices', 'response_type': 'clarification', 'options': options}, {'role': 'user', 'content': 'Earlier answer'}, {'role': 'assistant', 'content': 'Current choices', 'response_type': 'ask_user', 'options': options}]
        page.reload(wait_until='domcontentloaded')
        settled()
        check('only latest stored clarification remains interactive after refresh', page.locator('.clarification-option').count() == 3)
        count = len(requests)
        queue.append({'payload': {'message': 'Stored choice accepted.', 'response_type': 'chat'}})
        page.get_by_role('button', name=options[0]['label'], exact=True).click()
        settled()
        check('stored ask_user choice sends continuation after refresh', len(requests) == count + 1 and requests[-1]['is_continuation'] and requests[-1]['message'] == options[0]['value'])
        page.evaluate("() => promptDeleteConversation({id:'conv-a'})")
        page.locator('#dialogOverlay .dialog-btn.primary').click()
        settled('conv-b')
        check('deleting a conversation removes its saved focus context', page.evaluate("localStorage.getItem('ukra_property_context:offline-user:conv-a')===null"))
        check('delete fallback restores only the other conversation focus', page.evaluate('mainPane.focusStack.map(x=>x.url)') == [property_b['url']])
        # An identity change using the app's normal switch implementation cannot carry focus.
        page.evaluate("() => applyIdentitySwitch('offline-other-user', 'Other')")
        check('identity switch clears selected and viewed property context', page.evaluate('mainPane.focusStack.length===0 && mainPane.viewedProperties.length===0'))
        check('identity switch does not copy focus under the new identity', not page.evaluate("Object.keys(localStorage).some(k=>k.startsWith('ukra_property_context:offline-other-user:'))"))
        check('no browser JavaScript errors: ' + repr(page_errors), not page_errors)
        context.close()
        browser.close()
    result = {'passed': len(checks), 'checks': checks, 'mock_chat_requests': len(requests), 'external_requests_blocked': len(blocked), 'page_errors': page_errors, 'network': 'Every request intercepted; no application, model or external network contacted.'}
    (args.output / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
