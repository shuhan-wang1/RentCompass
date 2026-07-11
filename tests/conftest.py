"""Test bootstrap for the tests/ tree.

Tests import the flat ``core``/``rag``/``config`` modules that live under
``app/`` alongside the installable ``uk_rent_agent`` package from ``src/``.
Pin both source roots to the front of sys.path so that resolution stays
deterministic regardless of pytest's own sys.path munging.
"""

import os
import sys

_HERE = os.path.dirname(__file__)
_ROOT = os.path.dirname(_HERE)

for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "app")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)  # app ends up first, then src
