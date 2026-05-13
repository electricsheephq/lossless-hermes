"""TypeBox -> Python JSON Schema (Draft-07) translation conventions for LCM tools.

This module is the FOUNDATION for Epic 06 (agent tools): every per-tool
schema port (06-07..06-14) imports the helpers defined here so the
translation from TypeScript ``Type.X({...})`` literals to Python ``dict``
literals is consistent across the eight tool factories.

Per [ADR-016](../../docs/adr/016-typebox-translation.md), schemas are
**hand-translated** from the TypeScript source. The decision rejected
automated generation: TypeBox descriptions are load-bearing prose
(authored by humans, tuned across 12 audit waves), and an automated
converter would silently paraphrase or re-escape strings. The helpers
below codify the mechanical part of the translation — the wire-format
mapping from TypeBox builders to Python dict literals — so each
``LCM_<TOOL>_SCHEMA`` definition stays uniform.

What this module owns
---------------------

1. **The translation table** as executable helpers (one per TypeBox
   builder). Each returns a fresh ``dict`` so callers can compose them
   into ``Type.Object({...})`` and ``Type.Array(Type.X, {...})``.

2. **The ``object_schema`` helper** — wraps the ``Type.Object`` pattern
   and computes the ``required`` array from the kwargs that are NOT
   marked ``Type.Optional``. Mirrors TypeBox's behavior verbatim.

3. **The ``optional`` marker** — a sentinel wrapper produced by
   :func:`optional` that ``object_schema`` recognizes when computing
   ``required``. This is the Python equivalent of ``Type.Optional(X)``
   in TypeBox.

4. **The ``validate_schema`` helper** — runs ``jsonschema.Draft7Validator
   .check_schema(...)`` against an OpenAI-tool-format dict's
   ``parameters`` field. Raises :class:`jsonschema.SchemaError` if the
   schema is malformed. Used by ``tests/tools/test_schemas_wellformed.py``.

5. **The ``get_tool_schemas`` registry** — a list-builder pattern that
   per-tool modules append their schemas to at import time. Hermes's
   :meth:`LCMEngine.get_tool_schemas` returns this list.

Translation conventions (informative; see ``docs/typebox-translation.md``)
--------------------------------------------------------------------------

The TS source uses TypeBox idioms like::

    Type.String({description: "...", enum: ["a", "b"]})
    Type.Number({minimum: 1, maximum: 200, description: "..."})
    Type.Optional(Type.Boolean({description: "..."}))
    Type.Array(Type.String({enum: ["leaf", "condensed"]}), {description: "..."})
    Type.Object({pattern: Type.String(...), mode: Type.Optional(...)})

The Python translation uses the helpers below::

    string_field("...", enum=["a", "b"])
    number_field("...", minimum=1, maximum=200)
    optional(boolean_field("..."))
    array_field(string_field(enum=["leaf", "condensed"]), description="...")
    object_schema(pattern=string_field(...), mode=optional(string_field(...)))

Per-tool modules are STILL free to inline a hand-written dict literal —
nothing forces use of these helpers. They exist so the mechanical part
of the translation is consistent across the eight schemas; the
load-bearing part (description prose) stays inline and **verbatim from
the TS source**.

References
----------

* [ADR-016: TypeBox -> JSON Schema translation approach](../../docs/adr/016-typebox-translation.md)
* [docs/porting-guides/tools.md](../../docs/porting-guides/tools.md)
  lines 708-721 — the canonical translation table.
* [docs/typebox-translation.md](../../docs/typebox-translation.md) — the
  contributor-facing conventions doc this module operationalizes.
* TS source pinned: `/Volumes/LEXAR/Claude/lossless-claw` at commit
  ``1f07fbd`` on branch ``pr-613``. The eight tool factories live under
  ``src/tools/lcm-*.ts``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

# ---------------------------------------------------------------------------
# Optional marker
# ---------------------------------------------------------------------------
#
# TypeBox uses a wrapper `Type.Optional(X)` to mark a property as not
# required. JSON Schema Draft-07 does not have an "optional" keyword —
# instead, the OBJECT's `required: [...]` array enumerates the required
# property names, and properties NOT listed there are optional.
#
# To translate this faithfully without forcing every per-tool schema to
# duplicate a hand-maintained `required = [...]` list, we use an
# OptionalField sentinel: callers wrap a property dict via `optional(X)`,
# and `object_schema` strips the wrapper + omits the key from `required`.
#
# The sentinel is a private subclass of dict so it behaves like a normal
# schema dict for any downstream consumer that just reads `type` and
# `description` (e.g. doc generators, dispatchers that don't care about
# required-ness).


class OptionalField(dict[str, Any]):
    """Sentinel wrapper marking a property as ``Type.Optional`` in TypeBox.

    Subclasses :class:`dict` so it transparently behaves like a schema
    dict for any code that just reads ``type`` / ``description`` /
    ``enum``. The marker class is consumed by :func:`object_schema` when
    computing the ``required`` array.

    Per ADR-016 §Consequences, this is the Python equivalent of
    ``Type.Optional(X)`` in TypeBox. The sentinel approach was chosen
    over a parallel ``required: list[str]`` argument because it keeps
    the optionality colocated with the field definition — matching the
    one-line TS form ``Type.Optional(Type.String({...}))``.
    """


def optional(field: dict[str, Any]) -> OptionalField:
    """Mark ``field`` as optional (``Type.Optional(X)`` in TypeBox).

    Returns an :class:`OptionalField` that :func:`object_schema`
    recognizes when computing the ``required`` array. The wrapped
    dict's content is preserved verbatim (including ``description``,
    ``enum``, ``minimum``, ``maximum``, etc.).

    Args:
        field: A property schema dict (typically the return value of
            :func:`string_field`, :func:`number_field`,
            :func:`boolean_field`, :func:`array_field`, or
            :func:`object_schema`).

    Returns:
        A new :class:`OptionalField` carrying the same keys and values
        as ``field``. Modifying the returned object does NOT mutate the
        original input.

    Examples:
        >>> optional(string_field("a label"))
        {'type': 'string', 'description': 'a label'}
        >>> isinstance(optional(string_field("x")), OptionalField)
        True
    """
    return OptionalField(field)


# ---------------------------------------------------------------------------
# Builder helpers (one per TypeBox primitive)
# ---------------------------------------------------------------------------
#
# Translation table reference — `docs/porting-guides/tools.md` lines
# 708-721. Each helper produces a fresh dict per call so callers can
# safely mutate the result if needed.


def string_field(
    description: str | None = None,
    *,
    enum: Sequence[str] | None = None,
) -> dict[str, Any]:
    """``Type.String({description, enum?})`` -> ``{"type": "string", ...}``.

    The ``enum`` argument is the older TypeBox idiom for closed-set
    string values (e.g. ``Type.String({enum: ["a", "b"]})``). The newer
    idiom ``Type.Union([Type.Literal("a"), ...])`` is rare in the v4.1
    LCM surface and not handled here — see ``docs/typebox-translation.md``
    §"Union vs enum" for the policy if a future schema needs it.

    Args:
        description: Optional model-facing prose. **MUST be verbatim
            from the TS source** (ADR-016 §Consequences).
        enum: Optional list of allowed string literals. Order is
            preserved (matters for the model's tool-choice prior).

    Returns:
        A JSON Schema property dict. Keys are inserted in
        ``type, enum, description`` order to mirror the order TypeBox
        emits when serialized — keeps JSON-output diffs minimal.

    Examples:
        >>> string_field("the pattern")
        {'type': 'string', 'description': 'the pattern'}
        >>> string_field(enum=["leaf", "condensed"])
        {'type': 'string', 'enum': ['leaf', 'condensed']}
        >>> string_field()
        {'type': 'string'}
    """
    out: dict[str, Any] = {"type": "string"}
    if enum is not None:
        out["enum"] = list(enum)
    if description is not None:
        out["description"] = description
    return out


def number_field(
    description: str | None = None,
    *,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
) -> dict[str, Any]:
    """``Type.Number({description, minimum?, maximum?})`` -> dict.

    ``Type.Number`` in TypeBox emits ``"type": "number"`` (which permits
    both integers and floats in JSON Schema Draft-07). The LCM TS
    schemas use ``Type.Number`` for things like ``conversationId``
    (logically an int) and ``reserveFraction`` (logically a float) —
    we preserve the union ``"number"`` rather than narrowing some to
    ``"integer"`` so the schemas stay byte-identical to the TS output.

    Args:
        description: Optional model-facing prose. **MUST be verbatim
            from the TS source** (ADR-016 §Consequences).
        minimum: Optional inclusive lower bound (``minimum`` keyword
            in JSON Schema Draft-07).
        maximum: Optional inclusive upper bound (``maximum`` keyword
            in JSON Schema Draft-07).

    Returns:
        A JSON Schema property dict. Keys are inserted in
        ``type, minimum, maximum, description`` order.

    Examples:
        >>> number_field("max results", minimum=1, maximum=200)
        {'type': 'number', 'minimum': 1, 'maximum': 200, 'description': 'max results'}
    """
    out: dict[str, Any] = {"type": "number"}
    if minimum is not None:
        out["minimum"] = minimum
    if maximum is not None:
        out["maximum"] = maximum
    if description is not None:
        out["description"] = description
    return out


def boolean_field(description: str | None = None) -> dict[str, Any]:
    """``Type.Boolean({description})`` -> ``{"type": "boolean", ...}``.

    Args:
        description: Optional model-facing prose. **MUST be verbatim
            from the TS source** (ADR-016 §Consequences).

    Returns:
        A JSON Schema property dict.

    Examples:
        >>> boolean_field("when true, expand children")
        {'type': 'boolean', 'description': 'when true, expand children'}
    """
    out: dict[str, Any] = {"type": "boolean"}
    if description is not None:
        out["description"] = description
    return out


def array_field(
    items: dict[str, Any],
    *,
    description: str | None = None,
) -> dict[str, Any]:
    """``Type.Array(Type.X, {description?})`` -> ``{"type": "array", ...}``.

    The ``items`` argument is itself a property dict (typically
    :func:`string_field`, :func:`number_field`, etc.). Note the TS
    signature passes the item type as the first POSITIONAL argument
    and the options object as the second — we mirror the same shape
    so callers reading TS and Python side-by-side see the same order.

    Args:
        items: The item schema (e.g. ``string_field(enum=[...])``).
        description: Optional model-facing prose for the array itself.
            **MUST be verbatim from the TS source** (ADR-016
            §Consequences).

    Returns:
        A JSON Schema property dict with ``type: "array"`` and the
        ``items`` sub-schema.

    Examples:
        >>> array_field(string_field(enum=["leaf", "condensed"]),
        ...             description="kinds filter")
        {'type': 'array', 'items': {'type': 'string', 'enum': ['leaf', 'condensed']}, 'description': 'kinds filter'}
    """
    out: dict[str, Any] = {"type": "array", "items": items}
    if description is not None:
        out["description"] = description
    return out


def object_schema(
    **properties: dict[str, Any],
) -> dict[str, Any]:
    """``Type.Object({...})`` -> ``{"type": "object", "properties": ...}``.

    The keyword arguments are the property names; the values are
    property schema dicts. Properties wrapped in :func:`optional` are
    omitted from the ``required`` array — every other property is
    included. This mirrors TypeBox's behavior verbatim:
    ``Type.Object({a: Type.String(), b: Type.Optional(Type.Number())})``
    has ``required: ["a"]``.

    The output's ``required`` array is sorted in insertion order
    (Python's dict-iteration order, guaranteed since 3.7) — same order
    the user declared the keyword arguments. This matters because the
    TS TypeBox compiler emits ``required`` in the same insertion order
    (matching the property-declaration order), so byte-equality of the
    serialized schema requires we follow suit.

    Args:
        **properties: Property name -> schema dict. Wrap optional
            properties in :func:`optional`.

    Returns:
        A JSON Schema property dict with ``type: "object"``,
        ``properties: {...}``, and ``required: [...]``. The
        ``required`` key is ALWAYS present (possibly empty) — TypeBox
        emits it unconditionally.

    Examples:
        >>> object_schema(
        ...     pattern=string_field("the pattern"),
        ...     limit=optional(number_field(minimum=1)),
        ... )
        {'type': 'object', 'properties': {'pattern': {'type': 'string', 'description': 'the pattern'}, 'limit': {'type': 'number', 'minimum': 1}}, 'required': ['pattern']}
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, field in properties.items():
        # Strip OptionalField wrapper but keep the inner dict's keys.
        # We construct a plain dict so downstream JSON serialization
        # produces `{"type": "string", ...}` not `OptionalField({...})`.
        if isinstance(field, OptionalField):
            props[name] = dict(field)
        else:
            props[name] = field
            required.append(name)
    return {
        "type": "object",
        "properties": props,
        "required": required,
    }


# ---------------------------------------------------------------------------
# Tool-schema container (OpenAI function-call format)
# ---------------------------------------------------------------------------
#
# Each tool's `LCM_<TOOL>_SCHEMA` is an OpenAI-format function-call
# descriptor: `{"name": ..., "description": ..., "parameters": <object>}`.
# Hermes's `ContextEngine.get_tool_schemas()` returns a list of these.
# Per `tools.md` and the engine.ts wiring, the `parameters` field is
# what the LLM provider receives as the tool's JSON Schema.


def tool_schema(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Build an OpenAI-format function-call tool schema.

    Args:
        name: The tool name (e.g. ``"lcm_grep"``). Must match the key
            used in ``LCMEngine.TOOL_DISPATCH`` (issue 06-02).
        description: The tool-level description. **MUST be verbatim
            from the TS source** — this is the model-facing prose that
            drives tool selection (ADR-016 §Rationale, Wave-12 retro N3).
        parameters: The JSON Schema for the tool's parameters object
            (typically the return value of :func:`object_schema`).

    Returns:
        A dict in OpenAI function-call format. Hermes serializes the
        whole list to the provider's tool-spec wire format at call
        time.

    Examples:
        >>> tool_schema(
        ...     name="lcm_describe",
        ...     description="Look up an LCM item by ID.",
        ...     parameters=object_schema(id=string_field("the id")),
        ... )["name"]
        'lcm_describe'
    """
    return {
        "name": name,
        "description": description,
        "parameters": parameters,
    }


# ---------------------------------------------------------------------------
# Well-formedness validator
# ---------------------------------------------------------------------------


def validate_schema(schema: dict[str, Any]) -> None:
    """Assert ``schema['parameters']`` is well-formed JSON Schema Draft-07.

    Wraps :meth:`jsonschema.Draft7Validator.check_schema`. The
    parameters sub-schema is what the LLM provider actually receives —
    the outer ``name`` / ``description`` keys are OpenAI-tool-format
    metadata, not part of the validation contract.

    Args:
        schema: An OpenAI-format tool schema dict (output of
            :func:`tool_schema`).

    Raises:
        jsonschema.SchemaError: if ``schema['parameters']`` is not a
            valid JSON Schema document under Draft-07.
        KeyError: if ``schema`` is missing the ``parameters`` key.

    Notes:
        Imported lazily so the module's import-time footprint stays
        zero when callers only want the builders. ``jsonschema`` is a
        dev-dep (``pyproject.toml`` ``[project.optional-dependencies]
        dev``), not a runtime dep.
    """
    # Lazy import — see docstring Notes. `jsonschema` is dev-only.
    from jsonschema import Draft7Validator  # noqa: PLC0415

    Draft7Validator.check_schema(schema["parameters"])


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__: Final = (
    "OptionalField",
    "array_field",
    "boolean_field",
    "number_field",
    "object_schema",
    "optional",
    "string_field",
    "tool_schema",
    "validate_schema",
)
