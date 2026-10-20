from pathlib import Path
import requests,json,hashlib
B=Path('/tmp/rentcompass-fix-live-20261019');U='http://127.0.0.1:15019'
storage=json.loads((B/'student/student-storage.json').read_text());a=requests.Session();b=requests.Session()
for cookie in storage.get('cookies',[]):a.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie.get('path','/'))
conv=a.get(U+'/api/conversations',timeout=10);items=conv.json().get('conversations')or[];assert items,'student conversation missing';cid=items[0]['id']
b.get(U+'/api/conversations',timeout=10)
checks=[]
def digest(response):return hashlib.sha256(response.content).hexdigest()
baseline=a.get(U+'/api/favorites',timeout=10);count=len(baseline.json().get('favorites')or[])
for suffix in ('/messages','/version_map'):
 response=b.get(U+'/api/conversations/'+cid+suffix,timeout=10)
 checks.append({'case':'other_signed_identity_cannot_read_conversation'+suffix,'status':response.status_code,'passed':response.status_code in (403,404)})
uid=next((item['value'] for origin in storage.get('origins',[])for item in origin.get('localStorage',[])if item['name']=='ukra_user_id'),None)
response=b.get(U+'/api/favorites',params={'user_id':uid},timeout=10)
checks.append({'case':'query_user_id_cannot_override_signed_identity','status':response.status_code,'favorite_count':len(response.json().get('favorites')or[]),'passed':response.status_code==200 and not response.json().get('favorites')})
marker='https://fixtures.invalid/audit-identity-marker';response=b.post(U+'/api/favorites',json={'user_id':uid,'url':marker,'address':'Synthetic isolation marker','price':'£1 pcm'},timeout=10)
after=a.get(U+'/api/favorites',timeout=10);own=b.get(U+'/api/favorites',timeout=10)
checks.append({'case':'body_user_id_cannot_write_another_identity_favorites','post_status':response.status_code,'passed':response.status_code==200 and digest(after)==digest(baseline) and any(row.get('url')==marker for row in own.json().get('favorites')or[])})
b.delete(U+'/api/favorites/'+requests.utils.quote(marker,safe=''),timeout=10)
tampered=requests.Session()
for cookie in storage.get('cookies',[]):
 value=cookie['value'];value=value[:-1]+('A' if value[-1:]!='A' else 'Z')
 tampered.cookies.set(cookie['name'],value,domain=cookie['domain'],path=cookie.get('path','/'))
response=tampered.get(U+'/api/conversations/'+cid+'/messages',timeout=10)
checks.append({'case':'tampered_signed_cookie_cannot_read_student','status':response.status_code,'passed':response.status_code in (401,403,404)})
(B/'evidence/identity-checks.json').write_text(json.dumps(checks,indent=2));p=B/'private/identity-b-cookies.json';p.write_text(json.dumps(requests.utils.dict_from_cookiejar(b.cookies)));p.chmod(0o600)
print(json.dumps({'passed':sum(row['passed']for row in checks),'total':len(checks),'student_favorites_unchanged':len(after.json().get('favorites')or[])==count}))
