"""Shared per-tool result-size budget (Wave-12 W1A1 #2 + W1A8 #3 port).

Ports ``lossless-claw/src/plugin/result-budget.ts`` (LCM commit ``1f07fbd``
on branch ``pr-613``, 132 LOC) to Python with operator-tunable knobs:

* :data:`MAX_RESULT_TOKENS` — the per-tool output token cap. Single source of
  truth for both the per-tool ``MAX_RESULT_CHARS`` truncation AND the
  :func:`needs_compact_gate.estimate_result_tokens` ``HARD_CAP_TOKENS``
  ceiling. Raising one raises the other.
* :data:`MAX_RESULT_CHARS` — derived from :data:`MAX_RESULT_TOKENS` at
  4 chars/token. Updated in lockstep on :func:`apply_result_budget_config`.
* :func:`truncation_notice` — formatter for the truncation prose. Wave-12
  N3: prose is pinned by tests as part of the agent-facing contract.
  The regex ``truncated at ~\\d+ tokens to protect agent context`` is
  load-bearing.

Resolution precedence
---------------------

``LCM_TOOL_RESULT_TOKEN_BUDGET`` env > ``LcmConfig.tool_result_token_budget``
(plugin config) > default (10_000 tokens). Matches every other LCM operator-
tunable knob.

Module-load resolution is env-only (config not yet available). Plugin init
calls :func:`apply_result_budget_config` AFTER config resolution so the
plugin-config value can raise the cap IF env wasn't set. Env always wins.

Floor is 2_000 tokens (8K chars) — anything smaller makes most tools
useless. Default 10_000 tokens (40K chars).

Divergence from TS
------------------

TS uses ESM live bindings (``export let MAX_RESULT_TOKENS``) so consumers
with ``import { MAX_RESULT_TOKENS }`` automatically see the post-init value.
Python lacks live bindings: ``from module import X`` snapshots ``X`` at
import time. Consumers MUST import the module (``import result_budget``)
and read ``result_budget.MAX_RESULT_TOKENS`` at use time, not module-bind.
The :class:`_ResultBudgetState` class encapsulates this so callers can
import the singleton :data:`STATE` and read ``STATE.max_result_tokens`` /
``STATE.max_result_chars`` lazily.

For convenience, the module also exposes :data:`MAX_RESULT_TOKENS` and
:data:`MAX_RESULT_CHARS` as module-level integers that ARE updated by
:func:`apply_result_budget_config` — but the canonical accessor is
:func:`get_max_result_tokens` / :func:`get_max_result_chars`. Tests pin
the lazy-accessor pattern.

References
----------

* TS source: ``lossless-claw/src/plugin/result-budget.ts``
* Porting guide: ``docs/porting-guides/tools.md`` lines 612–617.
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``.
* ADR-029 Wave-12 row for F5 (gate is middleware) and N3 (truncation prose pinned).
"""

from __future__ import annotations

import os
from typing import Final

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Lower bound on the per-tool token budget — anything smaller makes most
#: tools useless (lcm_describe alone emits ~1500 tokens for a single summary
#: with no children).
FLOOR_TOKENS: Final[int] = 2_000

#: Default per-tool token budget when neither env nor config is set.
#: Matches the original pre-W1A1 LCM behavior.
DEFAULT_TOKENS: Final[int] = 10_000

#: Chars-per-token conversion factor used to derive
#: :data:`MAX_RESULT_CHARS` from :data:`MAX_RESULT_TOKENS`. 4 chars/token
#: matches GPT-tokenizer's English-text-average; conservative for code
#: (which is closer to 3 chars/token) — but the cap is for truncation,
#: not billing, so under-estimating is fine.
CHARS_PER_TOKEN: Final[int] = 4

#: Env var name that overrides the per-tool token budget. Operator-tunable
#: per ``docs/porting-guides/tools.md`` lines 612–617. Empty / non-numeric
#: values are ignored (treated as unset).
ENV_VAR_NAME: Final[str] = "LCM_TOOL_RESULT_TOKEN_BUDGET"


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


def _resolve_from_env() -> int | None:
    """Read :data:`ENV_VAR_NAME` from the environment.

    Returns ``None`` when the env var is unset, empty after strip, or not
    a positive integer. Non-numeric values are silently ignored — there's
    no operator hint when this happens (TS does the same; matches the
    "soft env override" pattern across LCM operator knobs).
    """
    raw = os.environ.get(ENV_VAR_NAME, "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return parsed


def _clamp_to_floor(n: int | None) -> int:
    """Clamp ``n`` to the :data:`FLOOR_TOKENS` floor.

    ``None`` and non-positive values resolve to :data:`DEFAULT_TOKENS`,
    then are clamped — so the floor wins over the default when an
    operator misconfigures (e.g. ``LCM_TOOL_RESULT_TOKEN_BUDGET=500``
    snaps up to ``2000``, not down to ``500``).
    """
    tokens = n if isinstance(n, int) and n > 0 else DEFAULT_TOKENS
    return max(FLOOR_TOKENS, tokens)


# ---------------------------------------------------------------------------
# Module-level state (mutable; updated by apply_result_budget_config)
# ---------------------------------------------------------------------------
#
# These are module-level integers — NOT constants. ``apply_result_budget_config``
# rewrites them when plugin config raises the cap and env isn't set. Tests pin
# the read-after-write contract; production callers should read these at use
# time, not import-bind a copy.

_ENV_VALUE_AT_LOAD: int | None = _resolve_from_env()

#: Resolved token cap. Identity for the estimator's HARD_CAP_TOKENS.
#: Updated by :func:`apply_result_budget_config` if env wasn't set at
#: module load. Read at use time via ``result_budget.MAX_RESULT_TOKENS``.
MAX_RESULT_TOKENS: int = _clamp_to_floor(_ENV_VALUE_AT_LOAD)

#: Per-tool char-truncation cap. Tools loop their accumulator and emit a
#: truncation notice line when crossed. Live-tied to
#: :data:`MAX_RESULT_TOKENS` via :func:`apply_result_budget_config`.
MAX_RESULT_CHARS: int = MAX_RESULT_TOKENS * CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# apply_result_budget_config
# ---------------------------------------------------------------------------


def apply_result_budget_config(
    tool_result_token_budget_from_config: int | None,
) -> None:
    """Raise the per-tool token budget from plugin config.

    Plugin init hook. Called from the plugin bootstrap after
    :class:`LcmConfig` resolves so plugin-config can raise the cap when
    env wasn't set.

    Env wins over config: if :data:`ENV_VAR_NAME` was set at module load
    time, this function is a no-op (the env value is already the active
    cap). Otherwise the plugin-config value (clamped to the floor) takes
    effect.

    Idempotent. Safe to call multiple times; subsequent calls re-apply
    the same value when env is unset.

    Args:
        tool_result_token_budget_from_config: The plugin-config value.
            ``None`` / non-positive values keep the current cap (no-op).
    """
    global MAX_RESULT_TOKENS, MAX_RESULT_CHARS  # noqa: PLW0603 — module state

    # Env at module load wins. If env was set, the value at load is
    # already correct; ignore config.
    if _ENV_VALUE_AT_LOAD is not None:
        return

    if (
        isinstance(tool_result_token_budget_from_config, int)
        and tool_result_token_budget_from_config > 0
    ):
        MAX_RESULT_TOKENS = _clamp_to_floor(tool_result_token_budget_from_config)
        MAX_RESULT_CHARS = MAX_RESULT_TOKENS * CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Accessor helpers — read at use time
# ---------------------------------------------------------------------------
#
# Python does NOT have ESM live bindings: ``from module import X`` snapshots
# ``X`` at import time. Callers that bind at import (``from result_budget
# import MAX_RESULT_TOKENS``) would see the load-time value forever, even
# after :func:`apply_result_budget_config` rewrites the module attribute.
# These accessor helpers exist so callers can either:
#   (a) ``from result_budget import get_max_result_tokens; get_max_result_tokens()``
#       — always reads the current value, or
#   (b) ``import result_budget; result_budget.MAX_RESULT_TOKENS``
#       — also reads the current value via attribute lookup.
# Tests pin both patterns.


def get_max_result_tokens() -> int:
    """Return the current per-tool token budget.

    Reads the module-level :data:`MAX_RESULT_TOKENS`, which is updated by
    :func:`apply_result_budget_config`. Use this accessor instead of
    binding ``MAX_RESULT_TOKENS`` at import time.
    """
    return MAX_RESULT_TOKENS


def get_max_result_chars() -> int:
    """Return the current per-tool char-truncation budget."""
    return MAX_RESULT_CHARS


# ---------------------------------------------------------------------------
# truncation_notice
# ---------------------------------------------------------------------------
#
# Wave-12 retro N3 — agent-facing contract.
# ``test_truncation_notice_format.py`` pins the regex:
#     r"truncated at ~\d+ tokens to protect agent context"
# Tool description prose (``lcm-grep-tool.ts`` line ~208 in TS;
# ``LCM_GREP_SCHEMA["description"]`` in Python) documents the regex to
# agents. Cosmetic edits to the format string will silently break the
# regex pin AND will surprise agents that may be regex-matching the
# prose for "did this tool truncate?" detection.

#: Format-string template for the truncation notice. The
#: ``{tokens}`` placeholder is the current :data:`MAX_RESULT_TOKENS`
#: at format time; the ``{reason}`` placeholder is the caller-supplied
#: hint string. The literal substring
#: ``"truncated at ~<N> tokens to protect agent context"`` is pinned by
#: regex tests. Cosmetic edits to this template will break those tests.
TRUNCATION_NOTICE_FORMAT: Final[str] = (
    "*(truncated at ~{tokens} tokens to protect agent context — {reason}; "
    "raise LCM_TOOL_RESULT_TOKEN_BUDGET env or LcmConfig.toolResultTokenBudget "
    "to increase the cap)*"
)


def truncation_notice(reason_hint: str) -> str:
    """Build the truncation-notice line emitted by tools when they cap.

    Wave-12 N3: The output prose of this function is part of the
    agent-facing contract. The regex
    ``r"truncated at ~\\d+ tokens to protect agent context"`` is pinned
    by ``test_truncation_notice_format.py`` and the prose is also
    documented to agents in tool description strings.

    Cosmetic edits to the format ("~10000 tokens" -> "10K tokens" etc.)
    will silently break the test regex AND will surprise agents that may
    be regex-matching the prose for "did this tool truncate?" detection.
    Don't edit this string for cosmetic reasons.

    Args:
        reason_hint: Short verb phrase (e.g. ``"narrow query, lower limit"``)
            that's tool-specific. Format-inserted in the middle of the
            prose. The leading capital is not enforced — TS uses
            lowercase fragments, so do we.

    Returns:
        The formatted notice string. Tools append this verbatim when
        their accumulator passes :data:`MAX_RESULT_CHARS`.
    """
    return TRUNCATION_NOTICE_FORMAT.format(
        tokens=MAX_RESULT_TOKENS,
        reason=reason_hint,
    )


# ---------------------------------------------------------------------------
# Test-only helpers
# ---------------------------------------------------------------------------


def __resolve_result_token_budget_from_env_for_testing() -> int:
    """Re-resolve from env, ignoring any config-applied overrides.

    Tests compare with the live module attribute to verify
    :func:`apply_result_budget_config` propagation. Public for test use
    only — leading double-underscore is the convention this codebase uses
    for "test-only" exports.
    """
    return _clamp_to_floor(_resolve_from_env())


def __reset_result_budget_for_testing() -> None:
    """Reset module-level bindings back to env-only state.

    Use in ``finally`` blocks (or pytest fixtures) when a test calls
    :func:`apply_result_budget_config`. Without this, test ordering can
    leak budget overrides into unrelated tests.
    """
    global MAX_RESULT_TOKENS, MAX_RESULT_CHARS  # noqa: PLW0603 — module state
    MAX_RESULT_TOKENS = _clamp_to_floor(_ENV_VALUE_AT_LOAD)
    MAX_RESULT_CHARS = MAX_RESULT_TOKENS * CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__: Final = (
    "CHARS_PER_TOKEN",
    "DEFAULT_TOKENS",
    "ENV_VAR_NAME",
    "FLOOR_TOKENS",
    "MAX_RESULT_CHARS",
    "MAX_RESULT_TOKENS",
    "TRUNCATION_NOTICE_FORMAT",
    "apply_result_budget_config",
    "get_max_result_chars",
    "get_max_result_tokens",
    "truncation_notice",
)
