"""Make the repo-root ``scripts/`` directory importable for these tests.

The files under ``scripts/`` are standalone CLI entry points, not a
package — so ``import run_live_eval`` only resolves if ``scripts/`` is on
``sys.path``. This conftest inserts it (idempotently) for the duration of
the test session. Scoped to ``tests/scripts/`` so the path tweak does not
leak into the rest of the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/scripts/conftest.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
