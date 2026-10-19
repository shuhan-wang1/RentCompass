"""Copied credentials must stop working after logout or account erasure."""
from dataclasses import replace
import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

import app as runtime
from uk_rent_agent.web.auth_store import AuthStore
from uk_rent_agent.web.chat_transport import NativeChatEndpoint


def test_sessions_survive_restart_but_not_revocation_or_erasure(tmp_path):
    path = tmp_path / 'auth.sqlite3'
    store = AuthStore(path)
    first = store.register('first', 'password')
    second = store.register('second', 'password')
    sid = store.create_session(first['user_id'], 3600)
    other_device = store.create_session(first['user_id'], 3600)
    unrelated = store.create_session(second['user_id'], 3600)
    restarted = AuthStore(path)
    assert restarted.get_session(sid) == first
    restarted.revoke_session(sid)
    assert store.get_session(sid) is None
    assert store.get_session(other_device) == first
    restarted.delete_user_id(first['user_id'])
    assert AuthStore(path).get_session(other_device) is None
    assert store.get_session(unrelated) == second
    assert store.verify('first', 'password') is None
    with pytest.raises(ValueError):
        store.create_session(first['user_id'], 3600)
    expired = store.create_session(second['user_id'], -1)
    assert store.get_session(expired) is None


@pytest.mark.parametrize('require_auth', [False, True])
def test_copied_cookie_denied_on_http_and_native_chat(tmp_path, monkeypatch, require_auth):
    store = AuthStore(tmp_path / 'auth.sqlite3')
    monkeypatch.setattr(runtime, 'auth_store', store)
    monkeypatch.setattr(runtime, '_runtime_config', replace(
        runtime._runtime_config, require_auth=require_auth, allow_legacy_client_user_id=False))
    monkeypatch.setattr(runtime._api_rate_limiter, 'allow', lambda *a, **k: (True, 0))
    owner = runtime.app.test_client()
    assert owner.post('/api/auth/register', json={'username': 'owner', 'password': 'password'}).status_code == 200
    cookie = owner.get_cookie('session').value
    copy = runtime.app.test_client()
    copy.set_cookie('session', cookie)
    assert copy.get('/api/auth/me').json['authenticated'] is True
    assert owner.post('/api/auth/logout').status_code == 200
    # Do not let /me's cookie cleanup mask the protected-endpoint check.
    assert copy.get('/api/conversations').status_code == 401
    copy.set_cookie('session', cookie)
    assert copy.get('/api/auth/me').json == {'authenticated': False}

    endpoint = NativeChatEndpoint(runtime.app)
    native = Starlette(routes=[Route('/api/alex', endpoint.handle, methods=['POST'])])
    async def check():
        for accept in ('application/json', 'text/event-stream'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=native), base_url='http://test') as client:
                response = await client.post('/api/alex', json={'message': 'hello'}, headers={
                    'Cookie': 'session=' + cookie, 'Accept': accept})
                assert response.status_code == 401
    asyncio.run(check())

    assert owner.post('/api/auth/login', json={'username': 'owner', 'password': 'password'}).status_code == 200
    live_cookie = owner.get_cookie('session').value
    uid = owner.get('/api/auth/me').json['user_id']
    store.delete_user_id(uid)
    copy.set_cookie('session', live_cookie)
    assert copy.post('/api/conversations', json={}).status_code == 401
    copy.set_cookie('session', live_cookie)
    assert copy.get('/api/auth/me').json == {'authenticated': False}
