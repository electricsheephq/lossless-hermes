"""Entity / procedure extraction worker layer.

Currently houses the entity-coreference tick (issue 07-02). The LLM-side
extractor (Protocol :class:`ExtractEntitiesFn`) is defined alongside this
tick body and consumed via callable-injection so the worker is decoupled
from any single provider — see ``docs/porting-guides/entity-extraction.md``
§"Dequeue + worker loop" for the load-bearing wiring.
"""

from __future__ import annotations

from lossless_hermes.extraction.coreference import (
    DEFAULT_PER_TICK_LIMIT,
    MAX_ATTEMPTS,
    CoreferenceTickOptions,
    CoreferenceTickResult,
    ExtractedEntity,
    ExtractEntitiesFn,
    PerItemDetail,
    count_pending_extractions,
    run_coreference_tick,
    surface_hash_for_id,
)

__all__ = [
    "DEFAULT_PER_TICK_LIMIT",
    "MAX_ATTEMPTS",
    "CoreferenceTickOptions",
    "CoreferenceTickResult",
    "ExtractEntitiesFn",
    "ExtractedEntity",
    "PerItemDetail",
    "count_pending_extractions",
    "run_coreference_tick",
    "surface_hash_for_id",
]
