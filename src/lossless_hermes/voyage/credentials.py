"""Voyage API key resolver — engine-init contract per ADR-022.

This module exposes the **strict** three-tier resolver that ``LCMEngine.__init__``
uses to obtain a Voyage API key. Resolution order (strict, first non-empty
wins after stripping whitespace):

1. ``config.voyage_api_key`` — inline in ``~/.hermes/config.yaml``
   (supports ``${VOYAGE_API_KEY}`` interpolation at YAML load time).
2. ``env["VOYAGE_API_KEY"]`` — standard CI / 12-factor mechanism.
3. ``$HERMES_HOME/lossless-hermes/credentials/voyage-api-key`` — file
   contents; mirrors the OpenClaw ``~/.openclaw/credentials/voyage-api-key``
   layout for migration friction.

If all three tiers are empty, :func:`resolve_voyage_api_key` raises
:class:`~lossless_hermes.voyage.client.VoyageError` with ``kind="auth"``.
The message lists every tier the operator can populate.

### Engine-init contract vs. pure lookup

A sibling helper :func:`lossless_hermes.db.config.resolve_voyage_api_key`
performs the same three-tier lookup but returns ``str | None`` for
operator-facing diagnostics (``hermes doctor`` / config introspection)
where the absence of a key is *information*, not an error. The function
in **this** module is the engine-init contract: callers that construct a
:class:`VoyageClient` need a guaranteed-non-empty string.

This split is deliberate. ADR-022 specifies the raising contract for
engine init; the pure-lookup helper grew out of the early-Wave config
work (PR #14, issue 01-02) before the engine-init wiring landed. Both
share the same precedence rules and the same env/hermes_home overrides
for testability; the only difference is the missing-everything behavior.

### Why not collapse to one function?

A single function with an ``raise_on_missing: bool = True`` flag would
work but obscures the contract. The diagnostic path *expects* ``None``
and would silently lose the error if a future caller forgot the flag.
The two-function split makes the contract obvious at the import site.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.db.config import (
    resolve_voyage_api_key as _lookup_voyage_api_key,
)
from lossless_hermes.voyage.client import VoyageError

__all__ = ["resolve_voyage_api_key"]


def resolve_voyage_api_key(
    config: LcmConfig,
    *,
    env: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
) -> str:
    """Resolve the Voyage API key per ADR-022; raise if all tiers empty.

    Strict three-tier order (first non-empty after :meth:`str.strip` wins):

    1. ``config.voyage_api_key`` — YAML inline (supports
       ``${VOYAGE_API_KEY}`` interpolation upstream).
    2. ``(env or os.environ)["VOYAGE_API_KEY"]``.
    3. ``($HERMES_HOME or ~/.hermes) / "lossless-hermes" / "credentials"
       / "voyage-api-key"`` — file contents.

    Args:
        config: The resolved :class:`LcmConfig` (tier-1 source).
        env: Optional env mapping. Defaults to :data:`os.environ`. When
            ``hermes_home`` is also ``None``, ``env["HERMES_HOME"]`` (if
            set) drives the tier-3 path.
        hermes_home: Optional explicit base directory for tier 3. Useful
            for tests; in production the engine never passes this.

    Returns:
        The first non-empty trimmed value from any tier.

    Raises:
        VoyageError: ``kind="auth"`` if every tier yielded an empty value.
            The message names every tier so the operator knows where to
            put the key.

    Notes:
        The resolved key is opaque — never log it. The caller stores it
        once on :class:`VoyageClient` and the only on-the-wire echo is in
        the ``Authorization: Bearer ...`` request header.
    """
    if env is None:
        env = os.environ
    # Delegate the actual three-tier lookup. ``_lookup_voyage_api_key``
    # already handles whitespace-stripping and the file-doesn't-exist
    # fall-through; here we just convert the absence-signal (``None``) into
    # the engine-init contract (a structured ``VoyageError``).
    key = _lookup_voyage_api_key(config, env=env, hermes_home=hermes_home)
    if key:
        return key

    # All three tiers empty — surface an actionable error. The message must
    # name every tier so the operator immediately knows the remediation
    # options without reading docs. Matches LCM ``client.ts:415`` style
    # ("voyage_auth: ..." prefix) and ADR-022 §Consequences.
    raise VoyageError(
        "auth",
        "voyage_auth: no API key found in "
        "config.lossless_hermes.voyage_api_key, "
        "$VOYAGE_API_KEY, or "
        "$HERMES_HOME/lossless-hermes/credentials/voyage-api-key",
    )
