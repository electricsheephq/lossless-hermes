"""Lifecycle methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the ``on_session_start`` / ``on_session_end`` / ``on_session_reset``
implementations per ADR-027 §Decision "Package structure" — the
``lifecycle.py`` sub-module of ``src/lossless_hermes/engine/``.

This file is a **skeleton at issue 02-01**: it declares the mixin class
and the lifecycle method signatures, but every body is a stub that
re-raises :class:`NotImplementedError` naming the issue that fills it
in (02-03 for ``on_session_start``/``on_session_end``/``on_session_reset``).
The shell class composes this mixin so callers see one class surface;
subsequent Epic 02 issues replace the stubs.

Why a stub-bodied mixin instead of leaving the bodies in
``__init__.py``: per ADR-027 §Decision, "Each sub-module is 1,000-2,000
LOC — review-able. … One class, one mental model — callers see
``engine.ingest(...)`` exactly as before; no factor-out costs." The
import-time hook is mechanical and load-bearing — the shell class needs
``_LifecycleMixin`` on its MRO at 02-01 so subsequent issues can patch
method bodies without touching the shell.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. The methods read/write ``self._db``,
  ``self._conversation_store`` etc. exclusively via the shell class's
  attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If lifecycle work needs ingest behavior, it
  goes through ``self.ingest_batch(...)`` (MRO resolves to
  :class:`_IngestMixin`).
* Underscore-prefixed class name — not exported, only consumed by
  :class:`LCMEngine`.

See:

* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` §"bootstrap" + §"maintain" — the
  TS algorithm that fills these stubs in 02-03.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    pass


class _LifecycleMixin:
    """Lifecycle hook handlers for :class:`LCMEngine`.

    Skeleton at 02-01 — every method raises :class:`NotImplementedError`
    naming the issue that fills it in. Maps to ``engine.ts``
    ``bootstrap()`` (lines 4983-5424), ``handleBeforeReset`` (line 7415),
    ``handleSessionEnd`` (line 7468).

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so subsequent
    issues (02-03 ``on_session_start``, etc.) can replace the bodies
    without touching :class:`LCMEngine` itself.
    """

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Open DB, run migrations, instantiate stores. Body lands in 02-03.

        Maps to engine.ts ``bootstrap()`` (lines 4983-5424). The Python
        port drops every JSONL fast-path (Hermes has no JSONL session
        file) — see ``docs/porting-guides/engine.md`` §"bootstrap" "What
        changes — DROP".

        Args:
            session_id: The Hermes session identifier.
            **kwargs: May include ``boundary_reason``, ``old_session_id``,
                ``hermes_home``, ``model``, ``platform``.

        Raises:
            NotImplementedError: Always at 02-01; body lands in 02-03.
        """
        raise NotImplementedError("on_session_start lands in Epic 02 (engine skeleton)")

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Flush state, close DB connections. Body lands in 02-03.

        Maps to engine.ts ``handleSessionEnd`` (line 7468).

        Args:
            session_id: The Hermes session identifier.
            messages: The final conversation message list.

        Raises:
            NotImplementedError: Always at 02-01; body lands in 02-03.
        """
        raise NotImplementedError("on_session_end lands in Epic 02 (engine skeleton)")

    def on_session_reset(self) -> None:
        """Archive conversation, optionally create replacement. Body lands in 02-03.

        Maps to engine.ts ``handleBeforeReset`` (line 7415).

        Raises:
            NotImplementedError: Always at 02-01; body lands in 02-03.
        """
        raise NotImplementedError("on_session_reset lands in Epic 02 (engine skeleton)")
