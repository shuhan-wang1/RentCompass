import collections,hashlib,http.client,http.cookies,json,pathlib,secrets,socket,subprocess,sys,time,urllib.parse,uuid
B=pathlib.Path('/tmp/rentcompass-e2e-tools-20261019')
E=B/'evidence';E.mkdir(exist_ok=True)
class LocalHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(15);self.sock.connect(str(B/'runtime/web.sock'))
class Client:
    def __init__(self,label,cookie=''):self.label=label;self.jar=http.cookies.SimpleCookie();self.jar.load(cookie)
    def clone(self,label):return Client(label,self.jar.output(header='',sep=';').strip())
    def call(self,method,path,data=None,headers=None):
        h={'Content-Type':'application/json','X-Request-Id':'authz-'+uuid.uuid4().hex}
        if self.jar:h['Cookie']=self.jar.output(header='',sep=';').strip()
        h.update(headers or {})
        con=LocalHTTP('localhost');con.request(method,path,body=json.dumps(data) if data is not None else None,headers=h)
        resp=con.getresponse();raw=resp.read().decode();status=resp.status
        for k,value in resp.getheaders():
            if k.lower()=='set-cookie':self.jar.load(value)
        try:body=json.loads(raw)
        except json.JSONDecodeError:body=raw
        rec={'seq':len(requests)+1,'actor':self.label,'method':method,'path':path,'request_id':h['X-Request-Id'],'request':redact(data),'headers':{k:v for k,v in h.items() if k.lower()!='cookie'},'status':status,'body':body,'content_type':resp.getheader('Content-Type')}
        requests.append(rec);con.close();return rec
def redact(value):
    if isinstance(value,dict):return {k:('[REDACTED]' if k.lower() in ('password','authorization','cookie') else redact(v)) for k,v in value.items()}
    if isinstance(value,list):return [redact(v) for v in value]
    return value
requests=[];checks=[]
def check(label,response,expected,condition=True,detail=None):
    expected=[expected] if isinstance(expected,int) else expected
    passed=response['status'] in expected and bool(condition)
    checks.append({'case':label,'actor':response['actor'],'request_seq':response['seq'],'expected_status':expected,'actual_status':response['status'],'passed':passed,'detail':detail})
    return response
def stable(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def save_private(state):
    state['cookies']={k:c.jar.output(header='',sep=';').strip() for k,c in clients.items()}
    path=B/'private/clients.json';dump(path,state);path.chmod(0o600)
def finish(phase,state):
    save_private(state)
    with (E/f'{phase}-requests.jsonl').open('w') as f:
        for rec in requests:f.write(json.dumps(rec,ensure_ascii=False)+'\n')
    dump(E/f'{phase}-checks.json',checks)
    summary={'phase':phase,'requests':len(requests),'checks':len(checks),'passed':sum(c['passed'] for c in checks),'failed':[c for c in checks if not c['passed']]}
    dump(E/f'{phase}-summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))
def await_ready():
    for n in range(25):
        try:
            c=LocalHTTP('localhost');c.request('GET','/health');r=c.getresponse();r.read();c.close()
            if r.status==200:return
        except OSError:pass
        time.sleep(1)
    raise RuntimeError('Isolated service not ready')
await_ready();phase=sys.argv[1]
if phase=='guest':
    state={'actors':{},'credentials':{}};clients={k:Client(k) for k in ('A','B','G','H')}
    for label,client in clients.items():
        marker='SYNTHETIC_AUTHZ_'+label+'_'+secrets.token_hex(6)
        if label in ('A','B'):
            credentials={'username':'authz_'+label.lower()+'_'+secrets.token_hex(4),'password':secrets.token_hex(16)+'Aa9!'}
            state['credentials'][label]=credentials
            r=client.call('POST','/api/auth/register',credentials);check('register-'+label,r,200,r['body'].get('authenticated') is True)
        r=client.call('POST','/api/conversations',{'title':marker});check('create-synthetic-'+label,r,201)
        state['actors'][label]={'cid':r['body']['conversation']['id'],'marker':marker,'url':'https://rental-fixture.invalid/'+label.lower()}
        r=client.call('POST','/api/favorites',{'url':state['actors'][label]['url'],'address':marker+' FAVORITE','price':'£1/month'});check('create-favorite-'+label,r,200)
    seed=subprocess.run(['docker','exec','-i','rentcompass-e2e-tools-test','python','/audit/harness/seed.py'],input=json.dumps(state['actors']),text=True,capture_output=True,check=True)
    for label,info in json.loads(seed.stdout).items():state['actors'][label].update(info)
    data=state['actors']
    for label,client in clients.items():
        own=data[label]
        for tail in ('messages','turns','version_map'):
            r=client.call('GET',f"/api/conversations/{own['cid']}/{tail}");check('owner-'+tail,r,200,own['marker'] in json.dumps(r['body']) if tail=='messages' else True)
    baseline={label:stable(clients[label].call('GET',f"/api/conversations/{d['cid']}/messages")['body']) for label,d in data.items()}
    # Every non-owner direction includes authenticated users and two independent guests.
    for actor,client in clients.items():
        for owner,target in data.items():
            if actor==owner:continue
            cid=target['cid'];foreign=target['marker']
            ops=[('GET',f'/api/conversations/{cid}/messages',None),('GET',f'/api/conversations/{cid}/turns',None),('GET',f'/api/conversations/{cid}/version_map',None),('PATCH',f'/api/conversations/{cid}',{'title':'SYNTHETIC_CROSS_RENAME'}),('DELETE',f'/api/conversations/{cid}',None),('POST',f'/api/conversations/{cid}/fork',{'after_turn_id':target['turns'][0],'title':'SYNTHETIC_CROSS_FORK'}),('POST',f'/api/conversations/{cid}/edit_turn',{'turn_id':target['turns'][1]}),('POST','/api/clear_history',{'conversation_id':cid})]
            for method,path,body in ops:
                r=client.call(method,path,body);check(f'cross-{actor}-to-{owner}-{method}-{path.rsplit("/",1)[-1]}',r,404,foreign not in json.dumps(r['body']))
    for label,client in clients.items():
        own=data[label]
        r=client.call('GET',f"/api/conversations/{own['cid']}/messages");check('denied-writes-preserve-owner-'+label,r,200,stable(r['body'])==baseline[label])
        r=client.call('GET','/api/conversations');own_ids={x['id'] for x in r['body']['conversations']};check('list-owner-only-'+label,r,200,own_ids=={own['cid']})
        r=client.call('GET','/api/favorites');check('favorite-list-owner-only-'+label,r,200,len(r['body']['favorites'])==1 and own['marker'] in json.dumps(r['body']))
        r=client.call('PATCH',f"/api/conversations/{own['cid']}",{'title':own['marker']+' RENAMED'});check('owner-rename-'+label,r,200)
        for action,body in [('fork',{'after_turn_id':own['turns'][0]}),('edit_turn',{'turn_id':own['turns'][1]})]:
            r=client.call('POST',f"/api/conversations/{own['cid']}/{action}",body);check('owner-'+action+'-'+label,r,201)
            if r['status']!=201:continue
            child=r['body']['conversation']['id'];m=client.call('GET',f'/api/conversations/{child}/messages');check('owner-branch-content-'+label,m,200,own['marker'] in json.dumps(m['body']))
            for other,visitor in clients.items():
                if other==label:continue
                x=visitor.call('GET',f'/api/conversations/{child}/messages');check('child-not-readable-'+label+'-by-'+other,x,404,own['marker'] not in json.dumps(x['body']))
        foreign=data['B' if label!='B' else 'A'];r=client.call('POST',f"/api/conversations/{own['cid']}/fork",{'after_turn_id':foreign['turns'][0]});check('foreign-turn-in-own-conversation-'+label,r,400,foreign['marker'] not in json.dumps(r['body']))
    # Body/header/query identities must not replace the signed session identity.
    for actor in ('B','G'):
        client=clients[actor];target=data['A']
        for where in ('header','query','body'):
            path=f"/api/conversations/{target['cid']}/messages";headers={};body=None
            if where=='header':headers['X-User-Id']=target['uid']
            elif where=='query':path+='?user_id='+target['uid']
            else:body={'user_id':target['uid']}
            r=client.call('GET',path,body,headers);check('spoof-'+actor+'-'+where,r,404,target['marker'] not in json.dumps(r['body']))
        r=client.call('GET','/api/conversations?user_id='+target['uid'],headers={'X-User-Id':target['uid']});check('spoof-list-'+actor,r,200,target['marker'] not in json.dumps(r['body']) and data[actor]['marker'] in json.dumps(r['body']))
        r=client.call('POST','/api/conversations',{'user_id':target['uid'],'title':data[actor]['marker']+' SPOOF_TEST'});check('spoof-create-stays-caller-'+actor,r,201);created=r['body']['conversation']['id']
        r=clients['A'].call('GET',f'/api/conversations/{created}/messages');check('spoof-created-not-owned-by-target-'+actor,r,404)
        r=client.call('DELETE',f"/api/favorites/{urllib.parse.quote(target['url'],safe='')}");check('foreign-favorite-delete-'+actor,r,404)
    for label in ('A','B'):
        c=clients[label];d=data[label]
        r=c.call('POST','/api/alex',{'message':'SYNTHETIC replay only','conversation_id':data['B' if label=='A' else 'A']['cid']},headers={'X-Request-Id':'authz-shared-replay-1'});check('request-id-replay-scoped-'+label,r,200,d['marker'] in json.dumps(r['body']) and r['body'].get('conversation_id')==d['cid'])
        r=c.call('POST','/api/alex',{'message':'SYNTHETIC replay SSE only'},headers={'X-Request-Id':'authz-shared-replay-2','Accept':'text/event-stream'});check('native-sse-replay-scoped-'+label,r,200,d['marker'] in json.dumps(r['body']) and data['B' if label=='A' else 'A']['marker'] not in json.dumps(r['body']))
    # Equal public URL in two namespaces must remain two independent favorites.
    shared='https://rental-fixture.invalid/shared'
    for label in ('A','B'):
        r=clients[label].call('POST','/api/favorites',{'url':shared,'address':data[label]['marker']+' SHARED_URL'});check('save-same-url-'+label,r,200)
    r=clients['B'].call('DELETE','/api/favorites/'+urllib.parse.quote(shared,safe=''));check('delete-own-shared-url-B',r,200)
    r=clients['A'].call('GET','/api/favorites');check('B-delete-preserves-A-favorite',r,200,any(x['url']==shared for x in r['body']['favorites']))
    r=clients['B'].call('GET','/api/conversations/'+data['A']['cid']+'/messages');known=r['body'];r=clients['B'].call('GET','/api/conversations/'+uuid.uuid4().hex+'/messages');check('unknown-and-unowned-same-error',r,404,r['body']==known)
    for actor in ('G','H'):
        c=clients[actor];d=data[actor];r=c.call('POST','/api/clear_history',{'conversation_id':d['cid'],'user_id':data['A']['uid']});check('clear-caller-only-with-spoof-'+actor,r,200)
        r=clients['A'].call('GET',f"/api/conversations/{data['A']['cid']}/messages");check('caller-clear-preserves-A-'+actor,r,200,stable(r['body'])==baseline['A'])
    finish(phase,state)
elif phase=='strict':
    state=json.loads((B/'private/clients.json').read_text());clients={k:Client(k,cookie) for k,cookie in state['cookies'].items()};data=state['actors']
    for actor in ('A','B'):
        c=clients[actor];r=c.call('GET','/api/auth/me');check('account-survives-config-restart-'+actor,r,200,r['body'].get('authenticated') is True)
        r=c.call('GET',f"/api/conversations/{data[actor]['cid']}/messages");check('strict-owner-read-'+actor,r,200,data[actor]['marker'] in json.dumps(r['body']))
        target=data['B' if actor=='A' else 'A'];r=c.call('GET',f"/api/conversations/{target['cid']}/messages");check('strict-cross-account-denied-'+actor,r,404,target['marker'] not in json.dumps(r['body']))
    for actor in ('G','H'):
        c=clients[actor]
        ops=[('GET','/api/conversations',None),('GET',f"/api/conversations/{data['A']['cid']}/messages",None),('GET','/api/favorites',None),('PATCH',f"/api/conversations/{data['A']['cid']}",{'title':'SYNTHETIC_UNAUTH_CHANGE'}),('POST',f"/api/conversations/{data['A']['cid']}/fork",{}),('POST','/api/clear_history',{'conversation_id':data['A']['cid']}),('POST','/api/forget_me',{'user_id':data['A']['uid']}),('POST','/api/alex',{'message':'SYNTHETIC unauthenticated request'})]
        for method,path,body in ops:
            r=c.call(method,path,body);check('strict-guest-gate-'+actor+'-'+path,r,401,data['A']['marker'] not in json.dumps(r['body']))
    corrupt=clients['A'].clone('TAMPERED_A');name=next(iter(corrupt.jar));value=corrupt.jar[name].value;prefix,sig=value.rsplit('.',1);corrupt.jar[name]=prefix+'.'+('B' if sig[0]=='A' else 'A')+sig[1:]
    r=corrupt.call('GET',f"/api/conversations/{data['A']['cid']}/messages");check('tampered-cookie-denied',r,401,data['A']['marker'] not in json.dumps(r['body']))
    r=corrupt.call('GET','/api/auth/me');check('tampered-cookie-not-authenticated',r,200,r['body'].get('authenticated') is False)
    # A second copy of OUR OWN synthetic session models a second tab/device.
    bcopy=clients['B'].clone('B_SESSION_COPY')
    r=clients['B'].call('POST','/api/auth/logout',{});check('logout-current-client',r,200,r['body'].get('authenticated') is False)
    r=clients['B'].call('GET','/api/conversations');check('logout-current-client-blocked',r,401)
    r=bcopy.call('GET','/api/conversations');check('logout-revokes-existing-copy',r,401,detail='Controlled copy of synthetic B cookie only; if accepted this is a session-revocation design limitation, not a signature bypass.')
    r=clients['B'].call('POST','/api/auth/login',state['credentials']['B']);check('login-B-restores-own',r,200,r['body'].get('user_id')==data['B']['uid'])
    b_before=stable(clients['B'].call('GET',f"/api/conversations/{data['B']['cid']}/messages")['body'])
    acopy=clients['A'].clone('A_SESSION_COPY')
    r=clients['A'].call('POST','/api/forget_me',{'user_id':data['B']['uid']});check('erase-bound-to-caller-not-body-user',r,200,r['body'].get('forgotten') is True)
    r=clients['B'].call('GET',f"/api/conversations/{data['B']['cid']}/messages");check('erasure-preserves-other-account',r,200,stable(r['body'])==b_before)
    r=clients['A'].call('GET','/api/auth/me');check('erased-current-client-logged-out',r,200,r['body'].get('authenticated') is False)
    r=clients['A'].call('POST','/api/auth/login',state['credentials']['A']);check('erased-password-no-longer-works',r,401)
    r=acopy.call('GET','/api/auth/me');check('erased-account-old-session-revoked-auth-state',r,200,r['body'].get('authenticated') is False)
    r=acopy.call('GET','/api/conversations');check('erased-account-old-session-revoked-read',r,401)
    r=acopy.call('POST','/api/conversations',{'title':'SYNTHETIC_DELETED_ACCOUNT_REENTRY'});check('erased-account-old-session-revoked-write',r,401)
    if r['status']==201:
        cid=r['body']['conversation']['id'];s=clients['B'].call('GET',f'/api/conversations/{cid}/messages');check('deleted-user-reentry-not-leaking-to-B',s,404)
    finish(phase,state)
elif phase=='reproduce':
    state=json.loads((B/'private/clients.json').read_text());clients={k:Client(k,cookie) for k,cookie in state['cookies'].items()}
    c=Client('C_FRESH_PUBLIC_API');credentials={'username':'authz_c_'+secrets.token_hex(4),'password':secrets.token_hex(16)+'Aa9!'}
    r=c.call('POST','/api/auth/register',credentials);check('fresh-C-register',r,200,r['body'].get('authenticated') is True);uid=r['body']['user_id']
    r=c.call('POST','/api/conversations',{'title':'SYNTHETIC_FRESH_C_BEFORE_DELETE'});check('fresh-C-create',r,201);cid=r['body']['conversation']['id']
    old=c.clone('C_PREDELETE_SESSION_COPY')
    state['deleted_C_cookie']=old.jar.output(header='',sep=';').strip()
    r=c.call('POST','/api/forget_me',{});check('fresh-C-delete-complete',r,200,r['body'].get('forgotten') is True)
    r=c.call('POST','/api/auth/login',credentials);check('fresh-C-password-revoked',r,401)
    r=old.call('GET',f'/api/conversations/{cid}/messages');check('fresh-C-old-data-was-deleted',r,401)
    r=old.call('GET','/api/auth/me');check('fresh-C-old-cookie-auth-revoked',r,200,r['body'].get('authenticated') is False)
    r=old.call('POST','/api/conversations',{'title':'SYNTHETIC_FRESH_C_AFTER_DELETE'});check('fresh-C-old-cookie-write-revoked',r,401)
    recreated=r['body'].get('conversation',{}).get('id') if isinstance(r['body'],dict) else None
    state['fresh_deleted_C']={'uid':uid,'before_cid':cid,'after_cid':recreated}
    clients['C_COPY']=old
    if recreated:
        r=clients['B'].call('GET',f'/api/conversations/{recreated}/messages');check('fresh-C-new-data-still-isolated-from-B',r,404)
    finish(phase,state)
elif phase=='after-restart':
    state=json.loads((B/'private/clients.json').read_text());clients={k:Client(k,cookie) for k,cookie in state['cookies'].items()};c=clients['C_COPY'];d=state['fresh_deleted_C']
    r=c.call('GET','/api/auth/me');check('restart-deleted-account-not-authenticated',r,200,r['body'].get('authenticated') is False)
    r=c.call('GET','/api/conversations');check('restart-deleted-account-gate',r,401)
    if d['after_cid']:
        r=c.call('GET',f"/api/conversations/{d['after_cid']}/messages");check('restart-deleted-account-resource-access',r,401)
    r=clients['B'].call('GET','/api/auth/me');check('restart-independent-B-still-valid',r,200,r['body'].get('authenticated') is True)
    for method,path,body in [('GET','/api/auth/me',None),('GET','/api/conversations',None),('POST','/api/conversations',{'title':'SYNTHETIC_INTACT_REVOKED_COOKIE'})]:
        stale=Client('C_INTACT_PREDELETE_COOKIE',state['deleted_C_cookie'])
        r=stale.call(method,path,body)
        check('restart-intact-revoked-cookie-'+method+'-'+path,r,200 if path=='/api/auth/me' else 401,r['body'].get('authenticated') is False if path=='/api/auth/me' else True)

    finish(phase,state)
else:raise ValueError(phase)
