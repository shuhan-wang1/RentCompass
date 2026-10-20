import asyncio,json,re,sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,'/tmp/rentcompass-live-20261019/browser-deps')
from playwright.async_api import async_playwright
ROOT=Path('/tmp/rentcompass-fix-live-20261019/student');ORIGIN='http://127.0.0.1:15019'
async def main():
 async with async_playwright() as p:
  b=await p.chromium.launch(headless=True,args=['--no-sandbox'])
  c=await b.new_context(storage_state=str(ROOT/'student-storage.json'),viewport={'width':1440,'height':1100},accept_downloads=True)
  async def route(r):
   u=urlsplit(r.request.url)
   if f'{u.scheme}://{u.netloc}'!=ORIGIN or u.path=='/api/alex':await r.abort()
   else:await r.continue_()
  await c.route('**/*',route)
  page=await c.new_page();await page.goto(ORIGIN,wait_until='domcontentloaded');await page.wait_for_timeout(1000)
  await page.locator('#userMenuBtn').click();await page.locator('#loginBtn').click();await page.wait_for_timeout(400)
  body=await page.locator('body').inner_text();(ROOT/'05-export-dialog-dom.txt').write_text(body)
  await page.screenshot(path=str(ROOT/'05-export-dialog.png'),full_page=True)
  buttons=await page.locator('button').evaluate_all('(es)=>es.filter(e=>e.offsetParent!==null).map(e=>({text:e.innerText,id:e.id,aria:e.getAttribute("aria-label")}))')
  print('VISIBLE_DIALOG_TAIL',body[-2400:]);print('DIALOG_BUTTONS',buttons[-12:])
  button=page.get_by_role('button',name=re.compile('访客研究备份|guest research',re.I))
  if await button.count():
   async with page.expect_download(timeout=10000) as pending:await button.click()
   download=await pending.value
   out=ROOT/'05-guest-research.json';await download.save_as(str(out))
   print('DOWNLOAD',out.name,'bytes',out.stat().st_size)
   try:
    doc=json.loads(out.read_text());print('BACKUP_TOP_KEYS',list(doc) if isinstance(doc,dict) else type(doc).__name__)
   except Exception as e:print('BACKUP_PARSE',type(e).__name__)
  else:print('NO_GUEST_BACKUP_BUTTON')
  await c.storage_state(path=str(ROOT/'student-storage.json'));await b.close()
asyncio.run(main())
