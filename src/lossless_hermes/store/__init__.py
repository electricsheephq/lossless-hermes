"""Persistence layer for lossless-hermes (epic-01 storage).

Mirrors ``lossless-claw/src/store/`` (ADR-024 §Decision, project tree). v0 of
this package ships the byte-identical message-identity recipe (issue #01-07)
plus the two single-row-per-conversation state-machine stores
(:mod:`.compaction_telemetry` and :mod:`.compaction_maintenance`, issue
#01-10). The ConversationStore + dedup queries land in #01-08, the
SummaryStore in #01-09, and the remaining ports follow per the epic-01 README.

See:

* ADR-024 — 1:1 mirror layout under ``src/lossless_hermes/``.
* ``docs/spike-results/003-identity-hash.md`` — the cross-runtime parity proof
  that pins the SHA-256 recipe used by :mod:`.message_identity`.
* ``docs/porting-guides/storage.md`` §4.3 / §4.4 — contracts for the two
  compaction state-machine stores in #01-10.
* ``docs/reference/lcm-source-map.md`` — TS-to-Python file map for the
  ``store/`` bucket.
"""
