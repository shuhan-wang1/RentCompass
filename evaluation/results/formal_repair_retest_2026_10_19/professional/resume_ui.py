"""Read-only browser recovery check. Never sends model or search requests."""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, '/tmp/rentcompass-live-20261019/browser-deps')
from playwright.async_api import async_playwright

ROOT = Path('/tmp/rentcompass-fix-live-20261019/professional')
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/rentcompass-live-20261019/browsers'
os.environ['LD_LIBRARY_PATH'] = '/tmp/rentcompass-live-20261019/browser/lib/usr/lib/x86_64-linux-gnu'

async def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    prefix = f'final-restore-{stamp}'
    model_attempts = []
    api_responses = []
    tasks = set()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            storage_state=str(ROOT/'professional-private-storage.json'),
            viewport={'width':1440,'height':1050})
        async def route_handler(route):
            req=route.request
            url=urlparse(req.url)
            if url.netloc!='127.0.0.1:15019' or url.scheme not in ('http','https'):
                await route.abort(); return
            if url.path in ('/api/alex','/api/search_direct'):
                model_attempts.append({'url':req.url,'method':req.method})
                await route.abort(); return
            await route.continue_()
        await context.route('**/*',route_handler)
        async def capture(response):
            url=urlparse(response.url)
            if response.request.method=='GET' and url.path.startswith('/api/conversations'):
                try:
                    body=await response.text()
                    api_responses.append({'url':response.url,'status':response.status,'body':body})
                except Exception as exc:
                    api_responses.append({'url':response.url,'status':response.status,'capture_error':str(exc)})
        def observer(response):
            task=asyncio.create_task(capture(response));tasks.add(task);task.add_done_callback(tasks.discard)
        context.on('response',observer)
        page=await context.new_page()
        await page.goto('http://127.0.0.1:15019/',wait_until='domcontentloaded')
        await page.wait_for_timeout(2500)
        initial=await page.evaluate("""({url:location.href,criteria:typeof currentCriteria!=='undefined'?currentCriteria:null,
            results:typeof currentResults!=='undefined'?currentResults:null,
            savedTab:document.querySelector('#btn-favorites')?.innerText,
            conversationButtons:Array.from(document.querySelectorAll('button')).filter(x=>x.innerText.includes("I'm moving to London for a job near Bank")).map(x=>x.innerText),
            text:document.body.innerText})""")
        await page.screenshot(path=str(ROOT/f'{prefix}-search.png'),full_page=True)
        await page.locator('#btn-favorites').click()
        await page.wait_for_timeout(800)
        saved=await page.locator('body').inner_text()
        await page.screenshot(path=str(ROOT/f'{prefix}-saved.png'),full_page=True)
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        result={'checked_at':stamp,'initial':initial,'saved_dom':saved,'api_responses':api_responses,
                'blocked_model_or_search_attempts':model_attempts,'real_model_calls':0}
        (ROOT/f'{prefix}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        (ROOT/'latest-restore-filename.txt').write_text(f'{prefix}.json\n')
        await context.storage_state(path=str(ROOT/'professional-private-storage.json'))
        os.chmod(ROOT/'professional-private-storage.json',0o600)
        await browser.close()
        print(json.dumps({'saved':f'{prefix}.json','model_calls':0,'blocked_attempts':len(model_attempts),
                          'criteria':initial['criteria'],'saved_tab':initial['savedTab']},ensure_ascii=False))

asyncio.run(main())
