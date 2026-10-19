import os,sys,json,time,hashlib
from pathlib import Path
from urllib.parse import urlsplit
B=Path('/tmp/rentcompass-fix-live-20261019');R=Path('/home/shuhan/uk_rent_recommendation')
sys.path[:0]=[str(R/'app'),str(R/'src')]
os.environ.update(OTM_ACCESS_STATE_PATH=str(B/'runtime/otm_access.sqlite3'),SEARCH_LISTING_CACHE_PATH=str(B/'runtime/listings.sqlite3'),OTM_DESC_CACHE_PATH=str(B/'runtime/descriptions.sqlite3'),SEARCH_CANONICAL_SCRAPE_LIMIT='30',OTM_SEARCH_MAX_PAGES='1',SEARCH_ALLOW_DEMO_FALLBACK='0')
import requests
original=requests.Session.send; records=[]
def send(self,request,**kwargs):
    parts=urlsplit(request.url)
    if parts.hostname!='www.onthemarket.com' or len(records)>=4:
        raise requests.ConnectionError('Bounded verification only allows four OnTheMarket GETs')
    row={'url':request.url,'method':request.method,'user_agent':request.headers.get('User-Agent'),'started':time.time(),'actual_external_request':True};records.append(row)
    try:
        response=original(self,request,**kwargs);row.update(status=response.status_code,bytes=len(response.content),sha256=hashlib.sha256(response.content).hexdigest(),seconds=round(time.time()-row['started'],3))
        if response.status_code in (403,429):
            raise requests.ConnectionError('Origin refused; no further probe requests')
        return response
    finally:
        (B/'evidence/repaired-source-http.json').write_text(json.dumps(records,indent=2))
requests.Session.send=send
from core.scraping.on_demand import get_listings
from core.scraping.onthemarket import fetch_listing_details
cold=get_listings('Camden',0,100,0,100000000,limit=30,budget_s=25)
warm=get_listings('Camden',0,100,0,100000000,limit=30,budget_s=25)
rows=cold.get('rows') or []
url=(rows[0].get('URL') or rows[0].get('url')) if rows else None
detail=fetch_listing_details(url,budget_s=20) if url else None
out={'cold':cold,'warm_meta':warm.get('meta'),'detail':detail,'detail_url':url,'actual_source_requests':len(records)}
(B/'evidence/repaired-source-probe.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str))
print(json.dumps({'cold_count':len(rows),'cold_meta':cold.get('meta'),'warm_source':warm.get('meta',{}).get('source'),'detail_source_status':(detail or {}).get('source_status'),'detail_description_chars':len((detail or {}).get('description','')),'actual_source_requests':len(records)},ensure_ascii=False,default=str))
