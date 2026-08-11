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
os.environ.setdefault("ALLOW_LEGACY_CLIENT_USER_ID", "1")
os.environ.setdefault("CANARY_LOG_PATH", "off")

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
