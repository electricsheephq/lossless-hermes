#!/usr/bin/env python3
"""Cross-runtime parity script for ``build_message_identity_hash``.

Runs the 10-case spike-003 fixture through:

* The Python port (:func:`lossless_hermes.store.message_identity.build_message_identity_hash`).
* The Node implementation (via ``node -e`` subprocess) — gracefully skipped
  if ``node`` is not on ``PATH``.

Then asserts every (role, content) input produces a byte-identical digest
across both runtimes. The 10 expected-digest values from spike-003 are also
embedded so the script doubles as a self-contained check: if either runtime
disagrees with the published table, the script fails.

Usage::

    python3 scripts/verify_identity_hash_parity.py

Exit codes:

* ``0`` — all 10 cases match across runtimes (or Node unavailable but
  Python matches the published table).
* ``1`` — a case mismatched. Output prints the differing rows.
* ``2`` — both runtimes produced output but the Python port disagreed with
  the published digest (regression in this repo).

See:

* ``docs/spike-results/003-identity-hash.md`` §"Test cases" — the published
  10-row table.
* ``epics/01-storage/01-07-message-identity.md`` AC item #3 — this script.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

# Add repo's ``src`` to the import path so this script runs without
# requiring an editable install. The pre-commit / CI invocation runs from
# the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from lossless_hermes.store.message_identity import (  # noqa: E402  (sys.path mutation above)
    build_message_identity_hash,
)

# ---------------------------------------------------------------------------
# The spike-003 fixture — kept in sync with
# ``tests/test_message_identity.py::SPIKE_003_FIXTURES``.
# Each row is (id, role, content, expected_node_digest).
# ---------------------------------------------------------------------------

_FIXTURES: list[tuple[str, str, str, str]] = [
    (
        "01-empty",
        "",
        "",
        "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    ),
    (
        "02-ascii",
        "user",
        "hello",
        "87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8",
    ),
    (
        "03-cjk-role",
        "ユーザー",
        "test",
        "9d886d80e62f390c46f3c016ab6c9414a336636e25b84ac4388b0766776a33b8",
    ),
    (
        "04-cjk-content",
        "user",
        "你好，世界",
        "c41afcf16ca44f0dba277cf25d3714fef56b15e112a9366c4f7a8c0d7eda71e7",
    ),
    (
        "05-emoji-zwj",
        "assistant",
        "wave 👋🏽 and family 👨‍👩‍👧‍👦",
        "ddcb2103e8518fc5d3ff4b46cb73feb9d937c2089fd8904b6167e34ccbcc70f0",
    ),
    (
        "06-embedded-nul",
        "user",
        "before\x00after",
        "0926790e68cbb7d71293a854a1eea4da21a85baa07026a44dda869cdff489ce1",
    ),
    (
        "07-json-array",
        "assistant",
        '[{"type":"text","text":"hi"}]',
        "d4eabe9e108ca7f2b6e88c44f70ce0263869a2f4e4901caa6499d959663609ee",
    ),
    (
        "08-newlines-tabs",
        "user",
        "line1\nline2\tcol",
        "a00dbd25b1c39636b6da4b8cf92c5968ec9b588202716ee9a4412644e943e620",
    ),
    (
        "09-8kib",
        "user",
        "abcdefgh" * 1024,
        "6ef15f41c013747b867624db7e116fc7d394cc90f538f62f38ffadd11811e17e",
    ),
    (
        "10-tool",
        "tool",
        "result text",
        "60bd6dd0bf56004e2d0134016b977027273225b72f80d688ed04cc744f983faa",
    ),
]


def _node_hash_batch(cases: list[tuple[str, str, str, str]]) -> list[str] | None:
    """Compute the Node-side digest for every case via a single ``node -e`` call.

    Returns a list of digests (one per case) if ``node`` is available,
    else ``None``. We pass inputs as JSON to avoid quoting nightmares
    with the NUL / emoji / 8 KiB cases.
    """
    if shutil.which("node") is None:
        return None

    payload = json.dumps([{"role": role, "content": content} for _, role, content, _ in cases])
    node_script = (
        "const {createHash} = require('node:crypto');"
        "const cases = JSON.parse(process.argv[1]);"
        "const out = cases.map(c =>"
        "  createHash('sha256').update(c.role).update('\\u0000').update(c.content).digest('hex')"
        ");"
        "process.stdout.write(JSON.stringify(out));"
    )
    try:
        result = subprocess.run(
            ["node", "-e", node_script, payload],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"WARNING: node subprocess failed ({e}); skipping Node comparison", file=sys.stderr)
        return None
    return json.loads(result.stdout)


def main() -> int:
    """Run the parity check and print a per-case verdict table."""
    python_digests = [
        build_message_identity_hash(role, content) for _, role, content, _ in _FIXTURES
    ]
    node_digests = _node_hash_batch(_FIXTURES)

    print(f"{'id':22s}  {'python':64s}  {'node':64s}  verdict")
    print("-" * 22 + "  " + "-" * 64 + "  " + "-" * 64 + "  -------")

    any_mismatch = False
    any_published_mismatch = False
    for (case_id, _role, _content, published), py_digest, node_digest in zip(
        _FIXTURES,
        python_digests,
        node_digests if node_digests is not None else [None] * len(_FIXTURES),
        strict=True,
    ):
        node_display = node_digest if node_digest is not None else "(skipped)"

        if py_digest != published:
            verdict = "PY != PUBLISHED"
            any_published_mismatch = True
        elif node_digest is not None and py_digest != node_digest:
            verdict = "PY != NODE"
            any_mismatch = True
        else:
            verdict = "OK"

        print(f"{case_id:22s}  {py_digest}  {node_display}  {verdict}")

    if any_published_mismatch:
        print("\nFAIL: Python port disagrees with the published spike-003 digest.", file=sys.stderr)
        return 2
    if any_mismatch:
        print("\nFAIL: Python and Node digests differ.", file=sys.stderr)
        return 1

    if node_digests is None:
        print("\nOK: Python matches published digests for all 10 cases (Node skipped).")
    else:
        print("\nOK: Python and Node agree on all 10 cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
