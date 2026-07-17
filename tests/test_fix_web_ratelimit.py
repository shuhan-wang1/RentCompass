"""Regression test for the spoofable per-IP rate-limit subject in app/app.py.

app.py has heavy module-level startup (RAG/property load), so — mirroring
test_app_input_validation.py — we parse app.py with `ast` and exec ONLY the
`_rate_limit_subject` function in isolation, injecting a fake Flask `request`
and a stubbed `_authed_user_id`. This exercises the ACTUAL source.

The fix: behind our own nginx (which binds the app to loopback and APPENDS the
real client IP as the LAST X-Forwarded-For entry), the subject must derive from
the RIGHTMOST XFF value. A guest can forge leading XFF entries but cannot forge
the final entry nginx writes, so forged leading entries must NOT rotate the
rate-limit bucket.
"""

import ast
import os

import pytest

_APP_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "app.py")
_WANTED = {"_rate_limit_subject"}


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


def _load_subject(remote_addr, xff=None, user_id=None):
    """Exec `_rate_limit_subject` with an injected fake request + auth stub."""
    with open(_APP_PATH, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_APP_PATH)
    picked = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED
    ]
    module = ast.Module(body=picked, type_ignores=[])
    ns = {
        "request": _FakeRequest(remote_addr, xff),
        "_authed_user_id": lambda: user_id,
    }
    exec(compile(module, _APP_PATH, "exec"), ns)  # noqa: S102 - trusted local source
    assert not (_WANTED - ns.keys()), "failed to extract _rate_limit_subject from app.py"
    return ns["_rate_limit_subject"]()


# ---------------------------------------------------------------------------
# The core fix: a forged LEADING XFF entry must not change the subject.
# ---------------------------------------------------------------------------

def test_forged_leading_xff_does_not_change_subject():
    real_client = "203.0.113.7"  # what our own nginx appended (rightmost)
    honest = _load_subject("127.0.0.1", xff=real_client)
    # Attacker prepends arbitrary junk to rotate their bucket; nginx still
    # appends the real client IP last.
    spoofed = _load_subject("127.0.0.1", xff=f"9.9.9.9, 8.8.8.8, {real_client}")
    assert honest == spoofed == f"ip:{real_client}"


def test_rotating_forged_prefix_yields_stable_subject():
    real_client = "198.51.100.42"
    subjects = {
        _load_subject("127.0.0.1", xff=f"{forged}, {real_client}")
        for forged in ("1.1.1.1", "2.2.2.2, 3.3.3.3", "evil, 4.4.4.4")
    }
    # All map to the SAME subject → the 12/min bucket cannot be rotated.
    assert subjects == {f"ip:{real_client}"}


def test_rightmost_entry_is_taken_not_leftmost():
    subject = _load_subject("::1", xff="10.0.0.1, 172.16.0.1, 192.0.2.99")
    assert subject == "ip:192.0.2.99"  # rightmost, not 10.0.0.1


# ---------------------------------------------------------------------------
# Fallback behavior preserved.
# ---------------------------------------------------------------------------

def test_non_loopback_ignores_xff_entirely():
    # A direct (non-proxied) client: trust remote_addr, never the header.
    subject = _load_subject("203.0.113.55", xff="1.2.3.4")
    assert subject == "ip:203.0.113.55"


def test_loopback_without_xff_falls_back_to_remote():
    assert _load_subject("127.0.0.1", xff=None) == "ip:127.0.0.1"
    assert _load_subject("127.0.0.1", xff="") == "ip:127.0.0.1"


def test_authenticated_user_takes_precedence():
    subject = _load_subject("127.0.0.1", xff="1.2.3.4", user_id="alice")
    assert subject == "user:alice"
