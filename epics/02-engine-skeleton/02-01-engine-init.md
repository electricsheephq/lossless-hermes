---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: implement LCMEngine.__init__ shell per ADR-024 / ADR-027'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines: ~1734–1900 (constructor + state initialization) plus `LcmContextEngine` class declaration at line 1730
- Function(s)/class(es): `LcmContextEngine` constructor, `createLcmDependencies`, store/assembler/compaction instantiation

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py`
- Estimated LOC: ~250 (shell class only — body lives in mixins per ADR-027)

## Summary

Implement the `LCMEngine` class shell with `__init__`, mixin composition, store instantiation, and basic identity. This is the bare skeleton that all other 02-* issues plug into.

The class follows ADR-027 (Engine Splitting): one class shell composing `_IngestMixin`, `_AssembleMixin`, `_CompactMixin`, `_LifecycleMixin`. All state lives on the shell class; mixin methods only read/write via `self.<state>`.

Per ADR-024 (Project Layout): `src/lossless_hermes/engine/__init__.py` defines `LCMEngine`; sibling files in `engine/` define the mixins.

## Implementation outline

```python
# src/lossless_hermes/engine/__init__.py

from agent.context_engine import ContextEngine
from collections import defaultdict
import asyncio
import logging
from typing import Any, Callable, Optional

from ..config import LcmConfig
from ..store import (
    ConversationStore,
    SummaryStore,
    CompactionTelemetryStore,
    CompactionMaintenanceStore,
)
from ..db.connection import open_lcm_db
from ..db.migration import run_lcm_migrations
from .ingest import _IngestMixin
from .assemble import _AssembleMixin
from .compact import _CompactMixin
from .lifecycle import _LifecycleMixin

logger = logging.getLogger("lcm.engine")


class LCMEngine(_LifecycleMixin, _CompactMixin, _AssembleMixin, _IngestMixin, ContextEngine):
    """Lossless Context Management engine for Hermes.

    Mixin order (MRO): _LifecycleMixin -> _CompactMixin -> _AssembleMixin -> _IngestMixin -> ContextEngine.
    State (DB, stores, locks, etc.) lives on this shell class. Mixin methods
    exclusively read/write self.<state> declared here. No mixin owns state.
    """

    name = "lcm"
    threshold_percent = 0.75
    protect_first_n = 3
    protect_last_n = 8

    def __init__(
        self,
        hermes_home: str,
        config: Optional[dict | LcmConfig] = None,
        summarizer: Optional[Callable] = None,
    ) -> None:
        super().__init__()

        # Config
        if isinstance(config, dict):
            self.config = LcmConfig.model_validate(config)
        elif config is None:
            self.config = LcmConfig()
        else:
            self.config = config

        # Database
        self.db_path = self._resolve_db_path(hermes_home, self.config.database_path)
        self.db = open_lcm_db(self.db_path, self.config)
        self.migrated = run_lcm_migrations(self.db, log=logger)

        # Stores (Epic 01 deliverables)
        self.conversation_store = ConversationStore(self.db)
        self.summary_store = SummaryStore(self.db)
        self.compaction_telemetry_store = CompactionTelemetryStore(self.db)
        self.compaction_maintenance_store = CompactionMaintenanceStore(self.db)

        # Sub-modules (stubs in Epic 02; bodies in Epic 03/04)
        self.assembler = None  # ContextAssembler — Epic 03
        self.compaction = None  # CompactionEngine — Epic 04
        self.retrieval = None  # RetrievalEngine — Epic 06

        # State fields (issue 02-02)
        self._init_state_fields()

        # Summarizer (injected; LLM client wiring lands in Epic 04)
        self._summarizer = summarizer

        # Background tasks (started by lifecycle hooks)
        self._background_drain_task: Optional[asyncio.Task] = None

        # Optional ABC fields from agent/context_engine.py — set defaults so
        # run_agent.py can read them without AttributeError.
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0

        logger.info(
            "[lcm] LCMEngine initialized (db=%s, threshold=%.2f, migrated=%s)",
            self.db_path,
            self.config.context_threshold,
            self.migrated,
        )

    def _resolve_db_path(self, hermes_home: str, configured: Optional[str]) -> str:
        """Resolve effective DB path: configured > $HERMES_HOME/lossless-hermes/lcm.db."""
        ...

    def _init_state_fields(self) -> None:
        """Initialize all in-memory state. Issue 02-02 fills this in."""
        ...
```

## Dependencies
- Depends on: Epic 00 (`pyproject.toml`, `plugin.yaml`, `pip install -e .` works), Epic 01 (all 4 stores exist and accept a `db` arg)
- Blocks: every other issue in Epic 02

## Acceptance criteria
- [ ] `from lossless_hermes.engine import LCMEngine` succeeds
- [ ] `LCMEngine(hermes_home="/tmp/test")` instantiates without raising; DB file appears at `/tmp/test/lossless-hermes/lcm.db`
- [ ] `engine.name == "lcm"` (selectable via `context.engine: lcm`)
- [ ] `engine.config` is a `LcmConfig` instance with validated defaults
- [ ] `engine.conversation_store`, `engine.summary_store`, `engine.compaction_telemetry_store`, `engine.compaction_maintenance_store` are all instantiated and live
- [ ] `engine.last_prompt_tokens == 0`, `engine.threshold_tokens == 0`, etc. — every ABC class attribute is set before `run_agent.py` reads it
- [ ] MRO test: `LCMEngine.__mro__` is `[LCMEngine, _LifecycleMixin, _CompactMixin, _AssembleMixin, _IngestMixin, ContextEngine, object]`
- [ ] `pytest tests/test_engine_init.py` passes
- [ ] No new mypy errors
- [ ] PR description cites the LCM commit SHA being ported

## Tests
- `tests/test_engine_init.py::test_instantiate_with_default_config` — instantiate with no config, assert defaults applied
- `tests/test_engine_init.py::test_instantiate_with_custom_db_path` — pass `database_path` override, assert DB file appears at the override
- `tests/test_engine_init.py::test_stores_attached` — assert all 4 stores are non-None and respond to a simple read
- `tests/test_engine_init.py::test_abc_class_attrs` — assert `last_prompt_tokens`, `threshold_tokens`, `context_length`, `compression_count` are all `0` after init (per hermes-hooks.md "Required class-level state")
- `tests/test_engine_init.py::test_mro_order` — assert MRO matches ADR-027

## Estimated effort
8 hours

## Confidence
95% — the constructor is mechanical 1:1 once Epic 01 stores land. The mixin pattern is well-trod Python idiom. The only ambiguity is whether sub-modules (`assembler`, `compaction`, `retrieval`) should be `None`-typed-Optional or have stub classes — this issue chooses `None` to make the Epic 03/04 wiring obvious.
