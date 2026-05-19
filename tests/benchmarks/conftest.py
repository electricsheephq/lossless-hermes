"""Make the repo-root ``scripts/`` directory importable for benchmark tests.

``scripts/benchmark_voyage_recall.py`` (and the ``run_live_eval`` it
reuses) are standalone CLI entry points, not a package — so
``import benchmark_voyage_recall`` only resolves if ``scripts/`` is on
``sys.path``. This conftest inserts it (idempotently) for the duration
of the test session. Scoped to ``tests/benchmarks/`` so the path tweak
does not leak into the rest of the suite.

Mirrors ``tests/scripts/conftest.py`` — same pattern, different test
package.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/benchmarks/conftest.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
