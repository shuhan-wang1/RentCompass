from pathlib import Path
import os,sys,json,sqlite3,time
B=Path('/tmp/rentcompass-fix-live-20261019');R=Path('/home/shuhan/uk_rent_recommendation');sys.path[:0]=[str(B/'harness'),str(R/'src'),str(R/'app')]
os.environ.update(SEARCH_LISTING_CACHE_PATH=str(B/'runtime/listings.sqlite3'),OTM_DESC_CACHE_PATH=str(B/'runtime/descriptions.sqlite3'),OTM_ACCESS_STATE_PATH=str(B/'runtime/otm_access.sqlite3'),SEARCH_CANONICAL_SCRAPE_LIMIT='30',OTM_SEARCH_MAX_PAGES='1',SEARCH_ALLOW_DEMO_FALLBACK='0')
import network_guard
with sqlite3.connect(network_guard.DB) as db:
 db.execute('CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY,n INT,next REAL,paused TEXT)')
 db.execute('INSERT OR IGNORE INTO hosts VALUES(?,?,?,?)',('www.onthemarket.com',4,0,''))
network_guard.install()
from core.scraping.on_demand import get_listings
records=[]
for area in ('Lewisham','Stratford','Leyton'):
 result=get_listings(area,0,100,0,100000000,limit=30,budget_s=20)
 records.append({'area':area,'result':result});(B/'evidence/fresh-source-areas.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
 print(json.dumps({'area':area,'count':len(result.get('rows') or []),'meta':result.get('meta')},ensure_ascii=False),flush=True)
 if result.get('meta',{}).get('source_status') in ('blocked','rate_limited'):break
