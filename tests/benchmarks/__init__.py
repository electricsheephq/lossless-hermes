"""Tests for the published benchmarks under ``docs/benchmarks/``.

Currently: the Voyage recall benchmark (issue 09-08). The benchmark's
runnable harness is ``scripts/benchmark_voyage_recall.py`` — a standalone
CLI entry point, not an importable package. :mod:`tests.benchmarks.conftest`
puts the repo-root ``scripts/`` directory on ``sys.path`` so these tests
can ``import benchmark_voyage_recall`` and exercise it with the Voyage
seam mocked (no live API).
"""
