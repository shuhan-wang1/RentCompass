import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, '/tmp/rentcompass-live-20261019/browser-deps')
from playwright.async_api import async_playwright

ROOT = Path('/tmp/rentcompass-fix-live-20261019/professional')
BASE = 'http://127.0.0.1:15019'
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/rentcompass-live-20261019/browsers'
os.environ['LD_LIBRARY_PATH'] = '/tmp/rentcompass-live-20261019/browser/lib/usr/lib/x86_64-linux-gnu'

def stamp():
    return datetime.now(timezone.utc).isoformat()

def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)

async def main():
    requests = json.loads((ROOT/'request-index.json').read_text()) if (ROOT/'request-index.json').exists() else []
    pending = set()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        options = {'viewport': {'width': 1440, 'height': 1050}}
        if (ROOT/'professional-private-storage.json').exists():
            options['storage_state'] = str(ROOT/'professional-private-storage.json')
        context = await browser.new_context(**options)
        await context.add_init_script("""(() => {
          window.__professionalAuditFetch = [];
          const originalFetch = window.fetch.bind(window);
          window.fetch = async function(input, init) {
            const url = typeof input === 'string' ? input : input.url;
            const response = await originalFetch(input, init);
            if (url && new URL(url,location.href).pathname === '/api/alex') {
              const record = {url, started: new Date().toISOString(), status: response.status, body: null};
              window.__professionalAuditFetch.push(record);
              response.clone().text().then(body => {record.body = body;record.finished=new Date().toISOString();})
                .catch(error => {record.error=String(error);});
            }
            return response;
          };
        })();""")
        async def route_handler(route):
            req = route.request
            parsed = urlparse(req.url)
            if parsed.scheme not in ('http', 'https') or parsed.netloc != '127.0.0.1:15019':
                await route.abort()
                return
            if parsed.path == '/api/alex' and req.method == 'POST':
                if len(requests) >= 7:
                    await route.abort()
                    emit({'blocked': 'chat cap', 'at': stamp()})
                    return
                item = {'number': len(requests)+1, 'started': stamp(), 'url': req.url, 'method': req.method,
                        'post_data': req.post_data, 'status': None, 'finished': None}
                requests.append(item)
                (ROOT/'request-index.json').write_text(json.dumps(requests, indent=2, ensure_ascii=False))
            await route.continue_()
        await context.route('**/*', route_handler)
        async def capture_response(response):
            parsed = urlparse(response.url)
            if parsed.path != '/api/alex' or response.request.method != 'POST':
                return
            item = next((x for x in reversed(requests) if x['status'] is None), None)
            if item is None:
                return
            item['status'] = response.status
            try:
                body = await response.text()
                path = ROOT/f"R{item['number']:02d}-response.txt"
                path.write_text(body)
                item['response_path'] = str(path)
            except Exception as exc:
                item['response_error'] = str(exc)
            item['finished'] = stamp()
            (ROOT/'request-index.json').write_text(json.dumps(requests, indent=2, ensure_ascii=False))
            emit({'response_completed': item['number'], 'status': item['status'], 'at': item['finished']})
        def response_event(response):
            task = asyncio.create_task(capture_response(response))
            pending.add(task)
            task.add_done_callback(pending.discard)
        context.on('response', response_event)
        page = await context.new_page()
        await page.goto(BASE, wait_until='domcontentloaded')
        await page.wait_for_timeout(1500)
        emit({'ready': True, 'url': page.url, 'title': await page.title()})
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                try:
                    cmd = json.loads(line)
                    action = cmd['action']
                    if action == 'inspect':
                        emit({'url': page.url, 'text': (await page.locator('body').inner_text())[:cmd.get('limit',18000)],
                              'inputs': await page.locator('input,textarea').evaluate_all('(xs)=>xs.map(x=>({tag:x.tagName,type:x.type,id:x.id,placeholder:x.placeholder,value:x.value}))'),
                              'buttons': await page.locator('button').evaluate_all('(xs)=>xs.map(x=>({text:x.innerText,aria:x.getAttribute("aria-label"),title:x.title,id:x.id,disabled:x.disabled}))')})
                    elif action == 'click':
                        loc = page.locator(cmd['selector'])
                        await loc.nth(cmd.get('index',0)).click(timeout=15000)
                        await page.wait_for_timeout(cmd.get('wait_ms',500))
                        emit({'clicked': cmd['selector'], 'chat_count': len(requests)})
                    elif action == 'fill':
                        await page.locator(cmd['selector']).nth(cmd.get('index',0)).fill(cmd['text'])
                        emit({'filled': cmd['selector']})
                    elif action == 'send':
                        before = len(requests)
                        if 'text' in cmd:
                            await page.locator(cmd['input']).fill(cmd['text'])
                        await page.locator(cmd['button']).click(timeout=15000)
                        deadline = time.monotonic()+cmd.get('timeout',240)
                        while time.monotonic()<deadline:
                            if len(requests)>before and requests[-1]['finished']:
                                break
                            await asyncio.sleep(0.5)
                        await page.wait_for_timeout(1000)
                        number = len(requests)
                        copies = await page.evaluate('window.__professionalAuditFetch || []')
                        if copies and copies[-1].get('body') is not None:
                            (ROOT/f'R{number:02d}-response.txt').write_text(copies[-1]['body'])
                            requests[-1]['response_path'] = str(ROOT/f'R{number:02d}-response.txt')
                            requests[-1]['capture_method'] = 'fetch_response_clone'
                            requests[-1].pop('response_error',None)
                            (ROOT/'request-index.json').write_text(json.dumps(requests, indent=2, ensure_ascii=False))
                        (ROOT/f'R{number:02d}-dom.txt').write_text(await page.locator('body').inner_text())
                        await page.screenshot(path=str(ROOT/f'R{number:02d}-page.png'), full_page=True)
                        await context.storage_state(path=str(ROOT/'professional-private-storage.json'))
                        emit({'sent': len(requests)>before, 'chat_count': number, 'last':requests[-1] if requests else None,
                              'text': (await page.locator('body').inner_text())[-cmd.get('limit',16000):]})
                    elif action == 'screenshot':
                        name = cmd.get('name','ui')
                        await page.screenshot(path=str(ROOT/f'{name}.png'), full_page=True)
                        (ROOT/f'{name}-dom.txt').write_text(await page.locator('body').inner_text())
                        emit({'saved': name})
                    elif action == 'evaluate':
                        emit({'evaluation': await page.evaluate(cmd['script'])})
                    elif action == 'reload':
                        await page.reload(wait_until='domcontentloaded')
                        await page.wait_for_timeout(1500)
                        emit({'reloaded': True, 'chat_count':len(requests), 'text': (await page.locator('body').inner_text())[-15000:]})
                    elif action == 'visit':
                        await page.goto(BASE+cmd.get('path',''),wait_until='domcontentloaded')
                        await page.wait_for_timeout(1000)
                        emit({'visited':page.url,'chat_count':len(requests)})
                    elif action == 'storage':
                        await context.storage_state(path=str(ROOT/'professional-private-storage.json'))
                        emit({'storage_saved': True, 'chat_count': len(requests)})
                    elif action == 'wait':
                        await page.wait_for_timeout(min(cmd.get('ms',1000),30000))
                        emit({'waited': True})
                    elif action == 'exit':
                        break
                except Exception as exc:
                    emit({'error': str(exc), 'chat_count': len(requests)})
        finally:
            await context.storage_state(path=str(ROOT/'professional-private-storage.json'))
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            (ROOT/'request-index.json').write_text(json.dumps(requests, indent=2, ensure_ascii=False))
            await browser.close()
            emit({'closed': True, 'chat_count':len(requests)})

asyncio.run(main())
