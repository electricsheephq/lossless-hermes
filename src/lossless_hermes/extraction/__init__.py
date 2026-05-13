"""Entity / procedure extraction worker layer.

Houses the entity-coreference tick (issue 07-02) and the LLM-side
entity extractor (issue 07-03). The
:class:`~lossless_hermes.extraction.coreference.ExtractEntitiesFn`
Protocol is defined alongside the tick body in
:mod:`lossless_hermes.extraction.coreference`; the LLM-backed concrete
implementation lives in :mod:`lossless_hermes.extraction.extractor` and
is consumed by the worker via callable-injection so the tick stays
decoupled from any single provider — see
``docs/porting-guides/entity-extraction.md`` §"Dequeue + worker loop"
for the load-bearing wiring.
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
from lossless_hermes.extraction.extractor import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    HARD_CAP,
    LlmCompleteFn,
    build_extraction_prompt,
    create_entity_extractor_llm,
    parse_entity_extraction_response,
)

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_PER_TICK_LIMIT",
    "DEFAULT_TIMEOUT_SECONDS",
    "HARD_CAP",
    "MAX_ATTEMPTS",
    "CoreferenceTickOptions",
    "CoreferenceTickResult",
    "ExtractEntitiesFn",
    "ExtractedEntity",
    "LlmCompleteFn",
    "PerItemDetail",
    "build_extraction_prompt",
    "count_pending_extractions",
    "create_entity_extractor_llm",
    "parse_entity_extraction_response",
    "run_coreference_tick",
    "surface_hash_for_id",
]
