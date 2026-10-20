import sys,os,json
sys.path.insert(0,'/tmp/rentcompass-live-20261019/browser-deps')
os.environ['PLAYWRIGHT_BROWSERS_PATH']='/tmp/rentcompass-live-20261019/browsers'
os.environ['LD_LIBRARY_PATH']='/tmp/rentcompass-live-20261019/browser/lib/usr/lib/x86_64-linux-gnu'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox']);page=b.new_page();page.goto('http://127.0.0.1:15009',wait_until='domcontentloaded');print({'title':page.title(),'input':page.locator('#chatInput').count()});b.close()
