"""CLI entrypoints for lossless-hermes.

This subpackage hosts the ``lossless-hermes`` console script and the
corresponding ``/lcm`` slash-command bridges.

Top-level command surface (per ADR-025 and the Epic 08 roadmap):

* ``lossless-hermes import-openclaw [--from PATH] [--to PATH] [--force]
  [--validate-rows N] [--dry-run]`` — one-shot OpenClaw → Hermes
  migration. See :mod:`lossless_hermes.cli.import_openclaw`.

The :func:`main` function in this module is the entry resolved by the
``[project.scripts]`` table in ``pyproject.toml`` (key
``lossless-hermes``). It accepts ``argv`` for test ergonomics — when
``None``, ``sys.argv[1:]`` is consumed normally.

Each subcommand module exposes its own ``main(argv)`` callable and a
slash-command ``run_slash(parsed)`` bridge consumed by
:class:`lossless_hermes.plugin.commands.LcmCommandDispatcher`.
"""

from __future__ import annotations

import argparse
import sys

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """``lossless-hermes`` CLI entrypoint.

    Dispatches on the first positional argument to a subcommand main.
    Currently only ``import-openclaw`` is wired — Epic 08-13/16/17 will
    add ``eval-runner``, ``rotate``, and ``worker-status`` as needed.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]`` when
            ``None``). Exposed for unit-test ergonomics.

    Returns:
        POSIX exit code from the dispatched subcommand main.
    """
    parser = argparse.ArgumentParser(
        prog="lossless-hermes",
        description=(
            "lossless-hermes operator CLI. Subcommands provide one-shot "
            "operations that don't need a running Hermes session "
            "(import-openclaw, etc.). Per-session commands live under "
            "the /lcm slash-command tree inside Hermes."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    sub.required = True

    # Subcommand: import-openclaw. We register only the name here; the
    # subcommand's own argparse surface is consumed inside its module.
    # ``add_parser(..., add_help=False)`` so ``--help`` flows to the
    # subcommand parser (which has its own --help). The subcommand
    # parses everything after its own name.
    sub.add_parser(
        "import-openclaw",
        help="Migrate an existing OpenClaw ~/.openclaw/ tree to Hermes.",
        add_help=False,
    )

    # Split argv at the subcommand name so the subcommand sees only its
    # own flags. ``sys.argv[1:]`` is ``["import-openclaw", "--from", "..."]``;
    # we want to pass ``["--from", "..."]`` to the subcommand main.
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        parser.print_help()
        return 2
    name = raw_argv[0]
    rest = raw_argv[1:]
    if name in ("-h", "--help"):
        parser.print_help()
        return 0
    if name == "import-openclaw":
        from lossless_hermes.cli.import_openclaw import main as import_main

        return import_main(rest)
    parser.error(f"unknown subcommand: {name!r}")
    return 2  # pragma: no cover - argparse.error raises SystemExit
