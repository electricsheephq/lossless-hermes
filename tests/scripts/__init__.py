"""Tests for the standalone helper scripts under the repo-root ``scripts/``.

``scripts/`` is not an importable package (the files are CLI entry points,
not library modules). :mod:`tests.scripts.conftest` puts the ``scripts/``
directory on ``sys.path`` so these tests can ``import run_live_eval`` and
exercise its functions directly with mocked Voyage + Anthropic.
"""
