"""Vendored APEX 1.1.0 test bootstrap.

The vendored tree has two import roots that its own suite assumes are both
on `sys.path`:

  * ``apex11/``              -> ``apex``, ``abutron_replacement``
  * repository root          -> ``risk_guard`` (imported by the native
                                broker-authority modules)

Some vendored tests also load config through CWD-relative paths
(``load_config("config/deriv_only.yaml")``), so the suite is pinned to the
``apex11`` working directory.

Lets ``pytest vendor/abutron-apex/apex11/tests`` run from the platform repo.
Vendored source and tests are otherwise unmodified.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent          # .../apex11
_REPO = _ROOT.parent                              # .../abutron-apex

for _p in (str(_ROOT), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# CWD-relative config paths in the vendored suite.
os.chdir(_ROOT)
