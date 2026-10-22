"""Test bootstrap for the tests/ tree.

Tests import the flat ``core``/``rag``/``config`` modules that live under
``app/`` alongside the installable ``uk_rent_agent`` package from ``src/``.
Pin both source roots to the front of sys.path so that resolution stays
deterministic regardless of pytest's own sys.path munging.
"""

import os
import sys
import tempfile

_HERE = os.path.dirname(__file__)
_ROOT = os.path.dirname(_HERE)
_TEST_RUNTIME = tempfile.mkdtemp(prefix="rentcompass_pytest_")
os.environ.setdefault(
    "RATE_LIMIT_DB_PATH", os.path.join(_TEST_RUNTIME, "rate_limits.sqlite3")
)
# Route-integration modules historically set this immediately before importing
# the flat ``app`` module. Collection order can import that module earlier, so
# establish the test-only identity mode once, before any test module import.
# Production Compose explicitly sets this to false.
# Every other runtime state file whose DEFAULT lives under the repo's .runtime/. On the
# production box the checkout's .runtime/ is bind-mounted into the live pools, so a local
# test run used to write test rows into LIVE state: f5c-session-* pending memory writes
# ("user_clearance=admin"), empty area-recommendation cache entries for UCL, the OTM pacing
# row (2026-10-19). CI's isolated-DB env list does not cover these; this does, for every
# runner.
for _name, _file in (
    ("MEMORY_GATE_DB", "memory_gate.sqlite3"),
    ("OTM_ACCESS_STATE_PATH", "otm_access.sqlite3"),
    ("AREA_RECO_CACHE_PATH", "area_reco_cache.sqlite3"),
    ("OTM_DESC_CACHE_PATH", "otm_desc_cache.sqlite3"),
    ("IDEMPOTENCY_DB", "idempotency.sqlite3"),
):
    os.environ.setdefault(_name, os.path.join(_TEST_RUNTIME, _file))
os.environ.setdefault("ALLOW_LEGACY_CLIENT_USER_ID", "1")
os.environ.setdefault("CANARY_LOG_PATH", "off")
# One deterministic canary HMAC key for the whole session. Without a stable secret every
# canary record carries a null hash + "unkeyed_no_stable_secret" (an instrumentation HOLD),
# so tests asserting a clean or keyed record depended on whichever OTHER module happened
# to set a key at import time, and failed when run on their own (r6e). Assigned, not
# setdefault: an ambient developer key must not change test results. A test of the
# unkeyed path deletes it with monkeypatch.delenv.
os.environ["CANARY_USER_HASH_KEY"] = "rentcompass-pytest-session-canary-key"

# Keep developer/production app/.env tuning out of mechanism tests.  Individual
# tests can still monkeypatch a value; these are the product-code defaults.
_FC_TEST_DEFAULTS = {
    "FC_TOOL_OFFLOAD_WORKERS": "32",
    "FC_BATCH_TOOL_BUDGET_S": "20",
    "FC_TURN_TOOL_BUDGET_S": "40",
    "FC_LOOP_SOFT_CAP": "6",
    "FC_TURN_CEILING_S": "30.0",
    "FC_FINAL_RESERVE_S": "6.5",
    "FC_MIN_BATCH_S": "2.0",
    "FC_WRAP_CRITIC_RESERVE_S": "0.5",
    "FC_WRAP_MIN_ATTEMPT_S": "2.0",
    "FC_DIMENSION_FANOUT_MAX": "3",
}
for _name, _value in _FC_TEST_DEFAULTS.items():
    os.environ.setdefault(_name, _value)

for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)  # app ends up first, then src
