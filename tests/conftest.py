"""Test bootstrap for the tests/ tree.

The tests/ directory also holds some stale scratch copies of the app packages
(tests/core, tests/rag, ...). Without help, pytest inserts tests/ at the front
of sys.path and `import core...` resolves to those broken copies. Pin the real
source roots to the front so tests exercise local_data_demo/ and src/.
"""

import os
import sys

_HERE = os.path.dirname(__file__)
_ROOT = os.path.dirname(_HERE)

for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "local_data_demo")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)  # local_data_demo ends up first, then src
