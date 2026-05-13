"""Database-side and operator-config modules for lossless-hermes.

Mirrors ``lossless-claw/src/db/`` (ADR-024 §Decision, project tree lines
83-88). v0 ships only the config skeleton (issue #00-07); migration ladder,
connection helper, and feature-detection land in Epic 01.

See:

* ADR-024 — 1:1 mirror layout under ``src/lossless_hermes/``.
* ``docs/reference/lcm-source-map.md`` — TS-to-Python file map for the
  ``db/`` bucket (config.ts / connection.ts / features.ts / migration.ts).
"""
