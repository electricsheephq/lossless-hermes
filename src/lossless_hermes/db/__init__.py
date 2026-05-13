"""Database-side and operator-config modules for lossless-hermes.

Mirrors ``lossless-claw/src/db/`` (ADR-024 §Decision, project tree lines
83-88). Epic 00 shipped the config skeleton (issue #00-07); Epic 01 lands
the connection helper, the migration ladder, and feature detection.

Modules in this package:

* :mod:`lossless_hermes.db.config` — operator config schema (skeleton at v0;
  filled in alongside each subsystem PR).
* :mod:`lossless_hermes.db.connection` — single sanctioned SQLite connection
  factory (ports ``lossless-claw/src/db/connection.ts``). Issue #01-01.
* (planned) ``features.py`` — FTS5/trigram capability probe (issue #01-03).
* (planned) ``migration.py`` — full schema ladder (issues #01-04+).

See:

* ADR-024 — 1:1 mirror layout under ``src/lossless_hermes/``.
* ``docs/reference/lcm-source-map.md`` — TS-to-Python file map for the
  ``db/`` bucket (config.ts / connection.ts / features.ts / migration.ts).
"""
