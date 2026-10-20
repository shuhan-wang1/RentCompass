import json,pathlib,secrets,sys
B=pathlib.Path('/tmp/rentcompass-e2e-repair-20261019');sys.path.insert(0,str(B/'public'));from test_driver import AuditBrowser
b=AuditBrowser(B/'root',profile='auth-ui-confirmed',mobile=True);checks=[];chat_requests=[]
b.context.on('request',lambda r:chat_requests.append(r.url) if '/api/alex' in r.url else None)
def check(case,value):
 checks.append({'case':case,'passed':bool(value)})
 if not value:raise AssertionError(case)
def menu():
 if not b.page.locator('#convSidebar').evaluate("e=>e.classList.contains('open')"):b.page.locator('#menuToggle').click()
 b.page.locator('#userMenuBtn').click()
try:
 menu();b.page.locator('#loginBtn').click();check('login-dialog-via-real-menu',b.page.locator('#dialogOverlay .dialog-input').count()==3)
 b.page.locator('#dialogOverlay .dialog-link').click();username='e2e_ui_'+secrets.token_hex(4);password=secrets.token_hex(18)+'Aa9!'
 inputs=b.page.locator('#dialogOverlay .dialog-input');inputs.nth(0).fill(username);inputs.nth(1).fill(password);inputs.nth(2).fill('Synthetic UI audit');b.page.locator('#dialogOverlay .dialog-btn.primary').click();b.page.wait_for_function('authState.authenticated===true');check('register-real-api-through-ui',b.page.evaluate('authState.authenticated'))
 uid=b.page.evaluate('getUserId()');b.page.reload(wait_until='networkidle');b.page.wait_for_function('authState.authenticated===true');check('auth-survives-browser-reload',b.page.evaluate('authState.authenticated && getUserId()')==uid)
 menu();b.page.locator('#logoutBtn').click();b.page.wait_for_function('!authState.authenticated');check('logout-real-ui',not b.page.evaluate('authState.authenticated'))
 check('strict-api-rejects-after-logout',b.api('/api/conversations')['status']==401)
 menu();b.page.locator('#loginBtn').click();inputs=b.page.locator('#dialogOverlay .dialog-input');inputs.nth(0).fill(username);inputs.nth(1).fill('incorrect-synthetic-password');b.page.locator('#dialogOverlay .dialog-btn.primary').click();b.page.wait_for_function("[...document.querySelectorAll('#dialogOverlay .dialog-message')].some(e=>e.style.color&&e.style.display!=='none'&&e.textContent)");check('wrong-password-visible-and-not-authenticated',not b.page.evaluate('authState.authenticated'))
 inputs.nth(1).fill(password);b.page.locator('#dialogOverlay .dialog-btn.primary').click();b.page.wait_for_function('authState.authenticated===true');check('correct-login-recovers-after-error',b.page.evaluate('getUserId()')==uid)
 b.screenshot('auth-ui-final');check('no-chat-requests',not chat_requests);check('no-browser-runtime-error',not b.errors)
finally:
 b.close();(B/'evidence/auth-ui-checks.json').write_text(json.dumps(checks,indent=2));print(json.dumps({'checks':len(checks),'passed':sum(x['passed'] for x in checks),'failed':[x['case'] for x in checks if not x['passed']]},indent=2))
