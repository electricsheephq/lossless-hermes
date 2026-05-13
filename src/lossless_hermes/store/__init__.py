"""Persistence layer for lossless-hermes (epic-01 storage).

Mirrors ``lossless-claw/src/store/`` (ADR-024 §Decision, project tree). v0 of
this package ships the byte-identical message-identity recipe (issue #01-07);
the ConversationStore + dedup queries land in #01-08, and the remaining ports
follow per the epic-01 README.

See:

* ADR-024 — 1:1 mirror layout under ``src/lossless_hermes/``.
* ``docs/spike-results/003-identity-hash.md`` — the cross-runtime parity proof
  that pins the SHA-256 recipe used by :mod:`.message_identity`.
* ``docs/reference/lcm-source-map.md`` — TS-to-Python file map for the
  ``store/`` bucket.
"""
