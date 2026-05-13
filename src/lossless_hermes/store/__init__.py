"""Persistence layer for lossless-hermes (epic-01 storage).

Mirrors ``lossless-claw/src/store/`` (ADR-024 §Decision, project tree). v0 of
this package ships the byte-identical message-identity recipe (issue #01-07),
the two single-row-per-conversation state-machine stores (#01-10:
:mod:`.compaction_telemetry`, :mod:`.compaction_maintenance`), the SummaryStore
+ Phase-0 helpers (this issue, #01-09), and the ConversationStore (#01-08).
The remaining ports follow per the epic-01 README.

See:

* ADR-024 — 1:1 mirror layout under ``src/lossless_hermes/``.
* ``docs/spike-results/003-identity-hash.md`` — the cross-runtime parity proof
  that pins the SHA-256 recipe used by :mod:`.message_identity`.
* ``docs/porting-guides/storage.md`` §4.3 / §4.4 — contracts for the two
  compaction state-machine stores in #01-10.
* ``docs/reference/lcm-source-map.md`` — TS-to-Python file map for the
  ``store/`` bucket.
"""

from .conversation_scope import append_conversation_scope_constraint
from .fts5_sanitize import sanitize_fts5_query
from .full_text_fallback import (
    LikeSearchPlan,
    build_like_search_plan,
    contains_cjk,
    create_fallback_snippet,
)
from .full_text_sort import AGE_DECAY_RATE, SearchSort, build_fts_order_by
from .message_identity import build_message_identity_hash, build_message_identity_key
from .parse_utc_timestamp import parse_utc_timestamp, parse_utc_timestamp_or_null
from .summary import (
    ContextItemRecord,
    ContextItemType,
    ConversationBootstrapStateRecord,
    CreateLargeFileInput,
    CreateSummaryInput,
    LargeFileRecord,
    MessageLeafSummaryLinkRecord,
    ReplaceContextRangeInput,
    SummaryKind,
    SummaryRecord,
    SummarySearchInput,
    SummarySearchResult,
    SummaryStore,
    SummarySubtreeNodeRecord,
    TranscriptGcCandidateRecord,
    UpsertConversationBootstrapStateInput,
)

__all__ = [
    "AGE_DECAY_RATE",
    "ContextItemRecord",
    "ContextItemType",
    "ConversationBootstrapStateRecord",
    "CreateLargeFileInput",
    "CreateSummaryInput",
    "LargeFileRecord",
    "LikeSearchPlan",
    "MessageLeafSummaryLinkRecord",
    "ReplaceContextRangeInput",
    "SearchSort",
    "SummaryKind",
    "SummaryRecord",
    "SummarySearchInput",
    "SummarySearchResult",
    "SummaryStore",
    "SummarySubtreeNodeRecord",
    "TranscriptGcCandidateRecord",
    "UpsertConversationBootstrapStateInput",
    "append_conversation_scope_constraint",
    "build_fts_order_by",
    "build_like_search_plan",
    "build_message_identity_hash",
    "build_message_identity_key",
    "contains_cjk",
    "create_fallback_snippet",
    "parse_utc_timestamp",
    "parse_utc_timestamp_or_null",
    "sanitize_fts5_query",
]
