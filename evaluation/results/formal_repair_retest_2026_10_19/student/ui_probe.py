import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,'/tmp/rentcompass-live-20261019/browser-deps')
from playwright.async_api import async_playwright
ROOT=Path('/tmp/rentcompass-fix-live-20261019/student')
ORIGIN='http://127.0.0.1:15019'
async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
  ctx=await browser.new_context(viewport={'width':1440,'height':1100})
  await ctx.route('**/*',lambda route: route.continue_() if route.request.url.startswith(ORIGIN+'/') or route.request.url==ORIGIN else route.abort())
  page=await ctx.new_page();await page.goto(ORIGIN,wait_until='domcontentloaded');await page.wait_for_timeout(1500)
  await page.screenshot(path=str(ROOT/'00-entry.png'),full_page=True)
  print((await page.locator('body').inner_text())[:14000])
  print('FORMS',await page.locator('input,textarea,select,button').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,type:e.type,placeholder:e.placeholder,text:e.innerText,id:e.id,name:e.name,aria:e.getAttribute("aria-label")}))'))
  await ctx.storage_state(path=str(ROOT/'student-storage.json'))
  await browser.close()
asyncio.run(main())
