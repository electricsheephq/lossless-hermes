"""Schema-wellformedness CI gate for every LCM tool schema.

Loads :func:`lossless_hermes.tools.get_tool_schemas` and runs
:meth:`jsonschema.Draft7Validator.check_schema` over each entry's
``parameters`` field. Per [ADR-016 §Consequences](../../docs/adr/016-typebox-translation.md):

> A CI test (``tests/tools/test_schemas_wellformed.py``) loads each
> schema and asserts ``jsonschema.Draft7Validator.check_schema(s) is
> None`` — catches typos at PR time.

This test catches **manual-transcription typos** (the documented
failure mode of the hand-translate policy). Examples it catches:

* ``"type": "stirng"`` (typo'd type keyword)
* ``"minimum": "1"`` (wrong value type for a constraint keyword)
* ``"enum": "not-a-list"`` (wrong shape for a closed-set marker)
* malformed nested ``items`` in an array schema

It does NOT catch:

* description-text drift (covered by ``test_schemas_match_ts``)
* missing required properties (covered by per-tool handler tests)
* dispatch-table omissions (covered by ``test_tool_dispatch`` in 06-02)

Parametrization
---------------

The test is parametrized over :func:`get_tool_schemas`. The registry
is **empty at issue 06-01** — per-tool ports (06-07..06-14) append
schemas as they land. The empty-registry case is handled explicitly
via :class:`pytest.mark.parametrize`'s built-in "no-test-collected"
fallback: when there are zero parametrized cases, pytest skips the
parametrized test (with a warning). To keep the CI gate present even
when the registry is empty, a sentinel ``test_registry_is_a_list``
assertion runs unconditionally.

Source: lossless-claw at commit ``1f07fbd``, branch ``pr-613``.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft7Validator

from lossless_hermes.tools import get_tool_schemas


# ---------------------------------------------------------------------------
# Sentinel: the registry is always a list, even when empty
# ---------------------------------------------------------------------------


def test_registry_is_a_list() -> None:
    """:func:`get_tool_schemas` always returns a list.

    Runs unconditionally — guards the contract even before per-tool
    ports land. Without this test, an empty registry would mean no
    parametrized test cases collect, and CI would silently pass on
    a broken registry.
    """
    schemas = get_tool_schemas()
    assert isinstance(schemas, list)


def test_registry_returns_fresh_list() -> None:
    """:func:`get_tool_schemas` returns a fresh list per call.

    Callers must NOT be able to mutate the registry by appending to
    the returned list. Internal :data:`TOOL_SCHEMAS` is the source of
    truth; the public function returns a copy.
    """
    first = get_tool_schemas()
    first.append({"name": "should-not-appear"})
    second = get_tool_schemas()
    assert second != first
    assert not any(s.get("name") == "should-not-appear" for s in second)


# ---------------------------------------------------------------------------
# Per-schema well-formedness — parametrized over the registry
# ---------------------------------------------------------------------------


def _schema_id(schema: dict[str, Any]) -> str:
    """pytest-id helper — show the tool name in the test ID."""
    name = schema.get("name")
    return name if isinstance(name, str) else "unnamed"


@pytest.mark.parametrize("schema", get_tool_schemas(), ids=_schema_id)
def test_schema_parameters_are_well_formed_draft7(
    schema: dict[str, Any],
) -> None:
    """Each tool schema's ``parameters`` is well-formed JSON Schema Draft-07.

    Per ADR-016 §Consequences — this is the CI gate against manual
    transcription typos. Failure mode:
    :class:`jsonschema.SchemaError` (or a metaschema validation error
    re-raised by :meth:`Draft7Validator.check_schema`).

    Notes:
        The registry is empty until issues 06-07..06-14 land. This
        test will then auto-cover every schema as it's appended.
    """
    assert "parameters" in schema, (
        f"Tool schema {schema.get('name')!r} is missing the "
        f"'parameters' key (OpenAI function-call format requires it)."
    )
    # check_schema returns None on success; raises on metaschema
    # violation. We don't assert the return value because the
    # contract is "no exception".
    Draft7Validator.check_schema(schema["parameters"])


# ---------------------------------------------------------------------------
# Per-schema OpenAI-function-call shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", get_tool_schemas(), ids=_schema_id)
def test_schema_has_required_openai_keys(schema: dict[str, Any]) -> None:
    """Every entry has ``name``, ``description``, ``parameters``.

    Per [docs/porting-guides/tools.md](../../docs/porting-guides/tools.md):
    Hermes's ``ContextEngine.get_tool_schemas()`` returns a list of
    OpenAI-format dicts. The three keys are mandatory for the
    provider to advertise the tool.
    """
    assert "name" in schema, "missing 'name' (OpenAI function-call format)"
    assert "description" in schema, (
        "missing 'description' (load-bearing model-facing prose; see ADR-016 §Rationale)"
    )
    assert "parameters" in schema, "missing 'parameters' (the JSON Schema)"
    assert isinstance(schema["name"], str)
    assert isinstance(schema["description"], str)
    assert isinstance(schema["parameters"], dict)


@pytest.mark.parametrize("schema", get_tool_schemas(), ids=_schema_id)
def test_schema_name_uses_lcm_prefix(schema: dict[str, Any]) -> None:
    """Tool names use the ``lcm_`` prefix.

    Per the LCM TS source (see e.g. ``lcm-grep-tool.ts:193``) and the
    Hermes dispatch convention — names like ``lcm_grep``,
    ``lcm_describe``, ``lcm_compact``. The prefix lets Hermes's
    ``_context_engine_tool_names`` set (``run_agent.py:11249``)
    discriminate LCM tools from other plugins' tools.
    """
    assert schema["name"].startswith("lcm_"), (
        f"Tool name {schema['name']!r} does not start with 'lcm_'. "
        f"All LCM tools use the lcm_ prefix per the TS source."
    )


@pytest.mark.parametrize("schema", get_tool_schemas(), ids=_schema_id)
def test_schema_description_is_non_empty(schema: dict[str, Any]) -> None:
    """The tool-level ``description`` is non-empty.

    Per [ADR-016 §Rationale](../../docs/adr/016-typebox-translation.md):
    the description is load-bearing model-facing prose. An empty
    description would silently degrade tool selection — fail fast.
    """
    desc = schema["description"]
    assert isinstance(desc, str) and len(desc.strip()) > 0, (
        f"Tool {schema['name']!r} has an empty/whitespace-only "
        f"description. Tool-level descriptions drive model "
        f"tool-selection (ADR-016 §Rationale); they must not be empty."
    )


# ---------------------------------------------------------------------------
# Aggregate: the registry has unique names
# ---------------------------------------------------------------------------


def test_registry_names_are_unique() -> None:
    """No two tools register the same ``name``.

    Duplicate names would silently shadow each other in the dispatch
    table (issue 06-02). Fail fast at collection time.
    """
    schemas = get_tool_schemas()
    names = [s.get("name") for s in schemas]
    assert len(names) == len(set(names)), f"Duplicate tool names in registry: {names}"
