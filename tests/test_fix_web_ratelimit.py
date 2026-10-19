"""Regression test for the spoofable per-IP rate-limit subject in app/app.py.

app.py has heavy module-level startup (RAG/property load), so — mirroring
test_app_input_validation.py — we parse app.py with `ast` and exec ONLY the
`_rate_limit_subject` / `_trusted_proxy_ips` functions in isolation, injecting a
fake Flask `request` and a stubbed `_authed_user_id`. This exercises the ACTUAL
source.

WHAT THIS FILE USED TO ASSERT, AND WHY IT WAS WRONG
---------------------------------------------------
The previous contract was: "behind our own nginx the subject must derive from the
RIGHTMOST XFF value, because a guest can forge leading entries but cannot forge the
final entry nginx writes." The rightmost-element half of that is right. The half that
was wrong — and that made it a live bypass (2026-09-03 T3 低-3: 15 requests with 15
rotated `X-Forwarded-For` values all passed a limiter that had already been tripped)
— is WHO the header was believed FROM: the old rule read it from any peer whose
address was loopback.

"The peer is loopback" is not evidence of nginx. It is equally true of anything else
that can reach the port, and such a caller writes the WHOLE header, final element
included, so it picks its own bucket at will.

The contract now: `X-Forwarded-For` is believed ONLY from a peer named in
`TRUSTED_PROXY_IPS` (**empty by default**), and from such a peer the RIGHTMOST element
is taken — the address that trusted hop itself wrote. A forged prefix cannot move the
bucket, because the client controls every element except the last.

Rightmost is correct under both nginx configurations in play, which is why the rule is
not "accept only a single address":

  * the nginx INSTALLED on this box
    (`/etc/nginx/sites-available/rentcompass.co.uk.conf:49`, and
    `/etc/nginx/proxy_params:3`) sets `X-Forwarded-For $proxy_add_x_forwarded_for` —
    an APPEND, so the header arrives as `<what the client sent>, <real peer>`;
  * `deploy/nginx/*.conf` IN THIS REPO sets `X-Forwarded-For $remote_addr` — a
    REPLACE, so the header is exactly one address.

Both put the trustworthy value last. (An earlier revision of this fix rejected
multi-element chains, which would have silently fallen back to `remote_addr` for every
request behind the appending config that is actually deployed.)

In the deployed configuration nothing is lost by the empty default: docker-compose
runs uvicorn with `--proxy-headers`, so `remote_addr` has ALREADY been rewritten to
the real client address before Flask sees the request.
"""

import ast
import os

import pytest

_APP_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "app.py")
_WANTED = {"_rate_limit_subject", "_trusted_proxy_ips"}


class _FakeHeaders:
    def __init__(self, xff):
        self._xff = xff

    def get(self, name, default=""):
        if name == "X-Forwarded-For":
            return self._xff if self._xff is not None else default
        return default


class _FakeRequest:
    def __init__(self, remote_addr, xff=None):
        self.remote_addr = remote_addr
        self.headers = _FakeHeaders(xff)


def _load_subject(remote_addr, xff=None, user_id=None, trusted=""):
    """Exec `_rate_limit_subject` with an injected fake request + auth stub.

    ``trusted`` is the TRUSTED_PROXY_IPS value the process would have been started
    with; it is parsed by app.py's own `_trusted_proxy_ips`, so the test exercises
    the real env grammar rather than a re-implementation of it.
    """
    with open(_APP_PATH, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_APP_PATH)
    picked = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED
    ]
    module = ast.Module(body=picked, type_ignores=[])
    ns = {
        "os": os,
        "request": _FakeRequest(remote_addr, xff),
        "_authed_user_id": lambda: user_id,
    }
    exec(compile(module, _APP_PATH, "exec"), ns)  # noqa: S102 - trusted local source
    assert not (_WANTED - ns.keys()), "failed to extract the helpers from app.py"
    prior = os.environ.get("TRUSTED_PROXY_IPS")
    os.environ["TRUSTED_PROXY_IPS"] = trusted
    try:
        ns["_TRUSTED_PROXY_IPS"] = ns["_trusted_proxy_ips"]()
        return ns["_rate_limit_subject"]()
    finally:
        if prior is None:
            os.environ.pop("TRUSTED_PROXY_IPS", None)
        else:
            os.environ["TRUSTED_PROXY_IPS"] = prior


# ---------------------------------------------------------------------------
# The fix: with no configured proxy, a rotated XFF cannot rotate the bucket.
# ---------------------------------------------------------------------------

def test_rotating_xff_on_loopback_no_longer_bypasses_the_limiter():
    """T3 低-3, reproduced: 15 rotated values, one bucket."""
    subjects = {
        _load_subject("127.0.0.1", xff=f"203.0.113.{n}")
        for n in range(1, 16)
    }
    assert subjects == {"ip:127.0.0.1"}


def test_a_forged_chain_on_loopback_is_ignored_entirely():
    """No configured proxy -> the header is not read AT ALL, chain or not. Loopback
    is not evidence of nginx."""
    assert _load_subject("127.0.0.1", xff="9.9.9.9, 8.8.8.8, 203.0.113.7") == "ip:127.0.0.1"
    assert _load_subject("::1", xff="10.0.0.1, 172.16.0.1, 192.0.2.99") == "ip:::1"


def test_non_loopback_ignores_xff_entirely():
    # A direct (non-proxied) client: trust remote_addr, never the header.
    assert _load_subject("203.0.113.55", xff="1.2.3.4") == "ip:203.0.113.55"


def test_loopback_without_xff_falls_back_to_remote():
    assert _load_subject("127.0.0.1", xff=None) == "ip:127.0.0.1"
    assert _load_subject("127.0.0.1", xff="") == "ip:127.0.0.1"


# ---------------------------------------------------------------------------
# Opt-in: an operator who really does front the app with a header-rewriting proxy
# names it, and then only nginx's OWN single-address form is honoured.
# ---------------------------------------------------------------------------

def test_a_configured_proxy_hop_is_believed_for_a_single_address():
    """The REPLACE shape: `deploy/nginx/*.conf`'s `$remote_addr`, one element."""
    subject = _load_subject("127.0.0.1", xff="203.0.113.7", trusted="127.0.0.1")
    assert subject == "ip:203.0.113.7"


def test_a_configured_proxy_hop_is_believed_at_the_end_of_an_appended_chain():
    """The APPEND shape: the installed nginx's `$proxy_add_x_forwarded_for`.

    The client sent `9.9.9.9`; nginx appended the real peer. The last element is the
    only one nginx wrote, so it is the only one read.
    """
    subject = _load_subject("127.0.0.1", xff="9.9.9.9, 203.0.113.7", trusted="127.0.0.1")
    assert subject == "ip:203.0.113.7"


def test_a_forged_prefix_cannot_move_the_bucket_behind_a_trusted_hop():
    """Rotating everything the CLIENT controls leaves one bucket, because only the
    element the trusted hop appended is read."""
    subjects = {
        _load_subject("127.0.0.1", xff=f"{forged}, 198.51.100.42", trusted="127.0.0.1")
        for forged in ("1.1.1.1", "2.2.2.2, 3.3.3.3", "evil, 4.4.4.4", "")
    }
    assert subjects == {"ip:198.51.100.42"}


def test_an_appended_chain_from_an_untrusted_hop_is_ignored_entirely():
    """The same header the installed nginx would produce, sent by something that is
    NOT the configured proxy: the whole chain is the caller's, so none of it is read.
    This is the T3 低-3 bypass, in the exact shape the deployed nginx emits."""
    subjects = {
        _load_subject("127.0.0.1", xff=f"9.9.9.9, 203.0.113.{n}")
        for n in range(1, 16)
    }
    assert subjects == {"ip:127.0.0.1"}
    # ...and from a non-loopback peer too.
    assert _load_subject("10.0.0.9", xff="9.9.9.9, 203.0.113.7") == "ip:10.0.0.9"


def test_an_unconfigured_peer_is_not_believed_even_with_a_list_present():
    subject = _load_subject("10.0.0.9", xff="203.0.113.7", trusted="127.0.0.1")
    assert subject == "ip:10.0.0.9"


@pytest.mark.parametrize("value", ["127.0.0.1,::1", " 127.0.0.1 , ::1 ", "::1,127.0.0.1"])
def test_the_trusted_list_accepts_a_comma_separated_value(value):
    assert _load_subject("::1", xff="203.0.113.7", trusted=value) == "ip:203.0.113.7"


# ---------------------------------------------------------------------------
# Unchanged behaviour.
# ---------------------------------------------------------------------------

def test_authenticated_user_takes_precedence():
    subject = _load_subject("127.0.0.1", xff="1.2.3.4", user_id="alice")
    assert subject == "user:alice"


def test_a_missing_remote_addr_is_a_stable_subject():
    assert _load_subject(None) == "ip:unknown"
