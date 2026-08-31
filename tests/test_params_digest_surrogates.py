"""A lone surrogate in a model-authored tool argument must not crash the digest.

``_params_digest`` is computed before any tool runs, on both architectures; a strict
UTF-8 encode turned one malformed argument into an uncaught UnicodeEncodeError inside
execute_tools (audit review_3 F2 side-note). Digests of well-formed payloads must stay
byte-identical so the no-progress guard and artifact identity are unchanged.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.langgraph_agent import _DIGEST_VOLATILE_KEYS, _params_digest  # noqa: E402


def test_lone_surrogate_argument_yields_a_digest_not_an_exception():
    digest = _params_digest("web_search", {"query": "Lon\ud800don rent"})
    assert len(digest) == 16
    assert digest == _params_digest("web_search", {"query": "Lon\ud800don rent"})


def test_well_formed_payload_digest_is_unchanged():
    params = {"city": "London", "max_price": 1800, "_deadline_monotonic": 1.0}
    stable = {k: v for k, v in params.items() if k not in _DIGEST_VOLATILE_KEYS}
    payload = "search_properties|" + json.dumps(
        stable, sort_keys=True, ensure_ascii=False, default=str)
    expected = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    assert _params_digest("search_properties", params) == expected
