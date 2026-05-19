"""Path-resolution regression tests for :func:`_resolve_db_path` (issue #65).

Ports the path-security regression coverage from ``hermes-lcm``'s
``tests/test_path_resolution.py`` / ``test_path_containment.py`` /
``test_path_security.py`` set. Those tests were a fix-forward of an
omission ``hermes-lcm`` closed in their #161: DB / storage paths were
built with ``Path(...).expanduser()`` but never ``.resolve()``-d, so a
non-canonical operator-supplied path (relative, ``..``-laden, or routed
through a symlink) reached the DB-open ``mkdir -p`` un-normalized.

``lossless-hermes`` has the *same* omission at
``engine/lifecycle.py``'s :func:`_resolve_db_path` — and, deliberately,
**none** of ``hermes-lcm``'s ``LCM_HERMES_BASE_DIR`` containment-base
machinery (``get_large_output_storage_dir``, ``_state_db_path_for_engine``,
``LCMEngine._state_db_path``). Per issue #65 §"Out of scope" and the
architecture-review comment on the issue, the fix here is the narrow
``.resolve()`` add — not a new containment-enforcement feature. These
tests are therefore reimplemented (not copied) against
``lossless-hermes``'s real surface:

* :func:`_resolve_db_path` is the single path-building site in
  ``lifecycle.py``. Both its branches — the ``config.database_path``
  override and the ``hermes_home`` canonical fallback — must return an
  **absolute** path with **no ``..`` traversal segments** surviving.
* ``database_path`` and ``hermes_home`` are operator-config-controlled
  (env / ``config.yaml`` / constructor arg), *not* attacker input — so
  this is hardening, not an attack-surface fix. The assertions pin the
  ``.resolve()`` invariant so a future refactor that drops it is caught.

Threat-model note (the underlying ``hermes-lcm`` #161 concern, mapped):
the value here is *path canonicalization*, not sandboxing. After
:func:`_resolve_db_path`, the path a DB-bring-up code path reasons about
is stable and absolute regardless of how the operator spelled the
config — ``.`` / ``..`` segments are collapsed and symlinks are
de-referenced. ``lossless-hermes`` intentionally does not (yet) enforce
a containment base, so there is no "rejected because outside allowed
base" test — that ``hermes-lcm`` behaviour is out of scope for #65.

See:

* issue #65 — "Path-security regression tests (port from hermes-lcm)".
* ``hermes-lcm`` ``tests/test_path_resolution.py`` /
  ``test_path_containment.py`` / ``test_path_security.py`` — prior art.
* ``docs/adr/002-plugin-data-directory.md`` §"Option A" — the
  ``$HERMES_HOME/lossless-hermes/lcm.db`` canonical path.
* ``engine/lifecycle.py`` :func:`_resolve_db_path` — the fixed site.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.engine.lifecycle import _resolve_db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_engine(database_path: str, hermes_home: Path) -> SimpleNamespace:
    """Build the minimal duck-typed object :func:`_resolve_db_path` reads.

    :func:`_resolve_db_path` only touches ``engine.config.database_path``
    and ``engine.hermes_home``. A full :class:`LCMEngine` would open the
    DB / fire the Apple-Python guard; a :class:`SimpleNamespace` carrying
    just those two attributes exercises the resolver in isolation —
    mirroring the ``MockEngine`` shim ``hermes-lcm``'s suite used.
    """
    return SimpleNamespace(
        config=SimpleNamespace(database_path=database_path),
        hermes_home=hermes_home,
    )


def _no_traversal_segments(path: Path) -> bool:
    """True if no ``.`` / ``..`` segments survive in ``path``.

    Splits on the OS separator and inspects every component below the
    anchor (``path.parts[0]`` is ``/`` on POSIX). ``hermes-lcm``'s suite
    asserted ``".." not in str(path).split("/")[1:]``; using
    :attr:`Path.parts` here is the platform-neutral equivalent.
    """
    return not any(part in {".", ".."} for part in path.parts[1:])


# ---------------------------------------------------------------------------
# database_path override branch — ``.resolve()`` invariant
# ---------------------------------------------------------------------------


def test_configured_database_path_resolves_to_absolute(tmp_path: Path) -> None:
    """A non-empty ``config.database_path`` yields an absolute path.

    The override branch (``engine/lifecycle.py`` line ~113) ends in
    ``.resolve()``; whatever the operator configured, the returned path
    is absolute so ``open_lcm_db``'s ``mkdir -p`` has a stable target.
    """
    configured = tmp_path / "custom-location" / "my.db"
    engine = _fake_engine(str(configured), tmp_path / ".hermes")

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert resolved == configured.resolve()


def test_configured_relative_database_path_becomes_absolute(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A *relative* ``database_path`` is anchored to an absolute path.

    Pre-#65 the override branch returned ``Path(configured).expanduser()``
    verbatim — a relative string stayed relative, so the DB landed
    wherever the process CWD happened to be. ``.resolve()`` anchors it
    against CWD deterministically. We ``chdir`` into ``tmp_path`` so the
    anchor is the sandbox, not the real working directory.
    """
    monkeypatch.chdir(tmp_path)
    engine = _fake_engine("relative-subdir/lcm.db", tmp_path / ".hermes")

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute(), "relative database_path was not made absolute"
    assert resolved == (tmp_path / "relative-subdir" / "lcm.db").resolve()


def test_configured_database_path_traversal_collapsed(tmp_path: Path) -> None:
    """``..`` segments in ``database_path`` are collapsed by ``.resolve()``.

    This is the core ``hermes-lcm`` #161 regression, mapped onto the
    override branch: an operator path like ``<base>/../../etc/lcm.db``
    must not reach ``open_lcm_db`` with the literal ``..`` components
    still present. ``.resolve()`` normalizes them away.
    """
    base = tmp_path / "a" / "b"
    base.mkdir(parents=True, exist_ok=True)
    traversal = base / ".." / ".." / "elsewhere" / "lcm.db"
    engine = _fake_engine(str(traversal), tmp_path / ".hermes")

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert _no_traversal_segments(resolved), f"'..'/'.' segment survived resolution: {resolved}"
    # The collapsed path is the literal ``..``-walk applied: a/b/../../X
    # == X under tmp_path.
    assert resolved == (tmp_path / "elsewhere" / "lcm.db").resolve()


def test_configured_database_path_symlink_dereferenced(tmp_path: Path) -> None:
    """A symlinked directory in ``database_path`` is de-referenced.

    ``.resolve()`` follows symlinks, so two operator paths that differ
    only by routing through a symlink resolve to the **same** canonical
    DB path. Pre-#65 (``expanduser()`` only) the symlink spelling was
    preserved, so the same physical DB could be reached under two
    distinct ``Path`` values — a subtle aliasing hazard for any code
    that keys off the path string.
    """
    real_dir = tmp_path / "real-data-dir"
    real_dir.mkdir()
    link_dir = tmp_path / "link-to-data"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    via_link = _fake_engine(str(link_dir / "lcm.db"), tmp_path / ".hermes")
    via_real = _fake_engine(str(real_dir / "lcm.db"), tmp_path / ".hermes")

    resolved_via_link = _resolve_db_path(via_link)
    resolved_via_real = _resolve_db_path(via_real)

    assert resolved_via_link == resolved_via_real, (
        "symlinked and real database_path did not resolve to one canonical path"
    )
    # The canonical form is the real directory, not the symlink.
    assert resolved_via_link == (real_dir / "lcm.db").resolve()


def test_configured_database_path_expands_user(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """``~`` in ``database_path`` is still expanded (pre-#65 behaviour kept).

    The #65 change *adds* ``.resolve()`` after the existing
    ``.expanduser()`` — it must not regress the tilde expansion. Point
    ``$HOME`` at the sandbox so ``~`` expands into ``tmp_path``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = _fake_engine("~/lcm-home/lcm.db", tmp_path / ".hermes")

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert "~" not in str(resolved), "tilde was not expanded"
    assert resolved == (tmp_path / "lcm-home" / "lcm.db").resolve()


# ---------------------------------------------------------------------------
# hermes_home canonical-fallback branch — ``.resolve()`` invariant
# ---------------------------------------------------------------------------


def test_canonical_fallback_resolves_to_absolute(tmp_path: Path) -> None:
    """An empty ``database_path`` falls back to the ADR-002 canonical path.

    The fallback branch (``engine/lifecycle.py`` line ~114) builds
    ``hermes_home / "lossless-hermes" / "lcm.db"`` and ends in
    ``.resolve()``. The returned path is absolute.
    """
    engine = _fake_engine("", tmp_path / ".hermes")

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert resolved == (tmp_path / ".hermes" / "lossless-hermes" / "lcm.db").resolve()


def test_canonical_fallback_traversal_collapsed(tmp_path: Path) -> None:
    """``..`` segments in ``hermes_home`` are collapsed on the fallback path.

    ``hermes_home`` is operator-controlled (the ``HERMES_HOME`` env var
    or the ``LCMEngine`` constructor arg), so the canonical-fallback
    branch gets the same ``.resolve()`` hardening as the override
    branch. A ``..``-laden ``hermes_home`` must not survive into the
    DB path.
    """
    nested = tmp_path / "x" / "y"
    nested.mkdir(parents=True, exist_ok=True)
    traversal_home = nested / ".." / ".." / "hermes-root"
    engine = _fake_engine("", traversal_home)

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert _no_traversal_segments(resolved), f"'..'/'.' segment survived resolution: {resolved}"
    assert resolved == (tmp_path / "hermes-root" / "lossless-hermes" / "lcm.db").resolve()


def test_canonical_fallback_whitespace_only_database_path(tmp_path: Path) -> None:
    """A whitespace-only ``database_path`` takes the canonical fallback.

    :func:`_resolve_db_path` ``.strip()``-s ``database_path`` before the
    truthiness check, so ``"   "`` is treated as "unset" and the
    ``hermes_home`` canonical branch fires — and that branch is
    ``.resolve()``-d too.
    """
    engine = _fake_engine("   ", tmp_path / ".hermes")

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert resolved == (tmp_path / ".hermes" / "lossless-hermes" / "lcm.db").resolve()


# ---------------------------------------------------------------------------
# Real LCMEngine — end-to-end via the public constructor
# ---------------------------------------------------------------------------


def test_engine_resolves_canonical_path_via_constructor(tmp_path: Path) -> None:
    """A real :class:`LCMEngine` resolves its canonical DB path.

    Exercises the resolver through the production object (not the
    duck-typed shim) with a bare :class:`LcmConfig` (``database_path``
    defaults to ``""``). No DB is opened — only :func:`_resolve_db_path`
    is invoked — so the Apple-Python sqlite guard never fires and no
    skip marker is needed.
    """
    engine = LCMEngine(hermes_home=tmp_path / ".hermes", config=LcmConfig())

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert _no_traversal_segments(resolved)
    assert resolved == (tmp_path / ".hermes" / "lossless-hermes" / "lcm.db").resolve()


def test_engine_resolves_traversal_hermes_home_via_constructor(
    tmp_path: Path,
) -> None:
    """A ``..``-laden ``hermes_home`` constructor arg is collapsed.

    Regression guard at the public-API level: passing a non-canonical
    ``hermes_home`` to :class:`LCMEngine` must still yield a collapsed,
    absolute DB path. ``LCMEngine.__init__`` stores ``hermes_home`` as a
    bare ``Path(...)`` (no ``.resolve()`` there); :func:`_resolve_db_path`
    is the component that canonicalizes it.
    """
    nested = tmp_path / "deep" / "nest"
    nested.mkdir(parents=True, exist_ok=True)
    traversal_home = nested / ".." / ".." / "engine-home"
    engine = LCMEngine(hermes_home=traversal_home, config=LcmConfig())

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert _no_traversal_segments(resolved), f"'..'/'.' segment survived resolution: {resolved}"
    assert resolved == (tmp_path / "engine-home" / "lossless-hermes" / "lcm.db").resolve()


def test_engine_resolves_database_path_override_via_constructor(
    tmp_path: Path,
) -> None:
    """A real :class:`LCMEngine` with a ``..``-laden ``database_path``.

    End-to-end on the override branch: ``LcmConfig(database_path=...)``
    carrying traversal segments resolves to a collapsed absolute path
    through the production object.
    """
    base = tmp_path / "cfg" / "dir"
    base.mkdir(parents=True, exist_ok=True)
    traversal = base / ".." / ".." / "db-root" / "lcm.db"
    engine = LCMEngine(
        hermes_home=tmp_path / ".hermes",
        config=LcmConfig(database_path=str(traversal)),
    )

    resolved = _resolve_db_path(engine)

    assert resolved.is_absolute()
    assert _no_traversal_segments(resolved), f"'..'/'.' segment survived resolution: {resolved}"
    assert resolved == (tmp_path / "db-root" / "lcm.db").resolve()
