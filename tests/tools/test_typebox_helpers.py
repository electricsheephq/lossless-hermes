"""Tests for the TypeBox -> Python dict translation helpers.

Exercises :mod:`lossless_hermes.tools._typebox` directly — each helper
is tested against the corresponding TypeBox idiom from the LCM source.
The per-tool schema match tests in :mod:`test_schemas_match_ts` cover
end-to-end correctness; this file covers the building blocks.

Source: lossless-claw at commit ``1f07fbd``, branch ``pr-613``.
"""

from __future__ import annotations

import pytest

from lossless_hermes.tools._typebox import (
    OptionalField,
    array_field,
    boolean_field,
    number_field,
    object_schema,
    optional,
    string_field,
    tool_schema,
    validate_schema,
)


# ---------------------------------------------------------------------------
# string_field
# ---------------------------------------------------------------------------


class TestStringField:
    """``Type.String({description, enum?})`` -> ``{"type": "string", ...}``."""

    def test_description_only(self) -> None:
        """Bare description matches ``Type.String({description: "..."})``."""
        result = string_field("the pattern")
        assert result == {"type": "string", "description": "the pattern"}

    def test_with_enum(self) -> None:
        """Enum produces a closed-set property dict in declaration order."""
        result = string_field("the mode", enum=["regex", "full_text"])
        assert result == {
            "type": "string",
            "enum": ["regex", "full_text"],
            "description": "the mode",
        }

    def test_enum_only_no_description(self) -> None:
        """``Type.String({enum: [...]})`` without a description is allowed."""
        result = string_field(enum=["leaf", "condensed"])
        assert result == {"type": "string", "enum": ["leaf", "condensed"]}

    def test_empty(self) -> None:
        """``Type.String()`` -> ``{"type": "string"}``."""
        assert string_field() == {"type": "string"}

    def test_key_order_type_enum_description(self) -> None:
        """Keys are inserted in ``type, enum, description`` order.

        Preserves the order TypeBox emits when serialized — keeps
        JSON-output diffs minimal against the upstream fixture.
        """
        result = string_field("a label", enum=["a", "b"])
        assert list(result.keys()) == ["type", "enum", "description"]

    def test_enum_list_is_a_copy(self) -> None:
        """The ``enum`` list is copied, not aliased — mutating the source
        argument must not mutate the returned dict.
        """
        source = ["a", "b"]
        result = string_field(enum=source)
        source.append("c")
        assert result["enum"] == ["a", "b"]


# ---------------------------------------------------------------------------
# number_field
# ---------------------------------------------------------------------------


class TestNumberField:
    """``Type.Number({description, minimum?, maximum?})`` -> dict."""

    def test_full(self) -> None:
        """All four keys populate as expected."""
        result = number_field("max results", minimum=1, maximum=200)
        assert result == {
            "type": "number",
            "minimum": 1,
            "maximum": 200,
            "description": "max results",
        }

    def test_minimum_only(self) -> None:
        """``Type.Number({minimum: 1})`` form."""
        result = number_field(minimum=1)
        assert result == {"type": "number", "minimum": 1}

    def test_maximum_only(self) -> None:
        """``Type.Number({maximum: 50})`` form."""
        assert number_field(maximum=50) == {"type": "number", "maximum": 50}

    def test_float_bounds(self) -> None:
        """``reserveFraction`` uses float bounds — must round-trip cleanly."""
        result = number_field("reserveFraction range", minimum=0.5, maximum=1.0)
        assert result["minimum"] == 0.5
        assert result["maximum"] == 1.0

    def test_no_args(self) -> None:
        """``Type.Number()`` -> ``{"type": "number"}``."""
        assert number_field() == {"type": "number"}

    def test_key_order(self) -> None:
        """``type, minimum, maximum, description``."""
        result = number_field("label", minimum=1, maximum=10)
        assert list(result.keys()) == [
            "type",
            "minimum",
            "maximum",
            "description",
        ]


# ---------------------------------------------------------------------------
# boolean_field
# ---------------------------------------------------------------------------


class TestBooleanField:
    """``Type.Boolean({description})``."""

    def test_with_description(self) -> None:
        result = boolean_field("when true, X")
        assert result == {"type": "boolean", "description": "when true, X"}

    def test_without_description(self) -> None:
        assert boolean_field() == {"type": "boolean"}


# ---------------------------------------------------------------------------
# array_field
# ---------------------------------------------------------------------------


class TestArrayField:
    """``Type.Array(Type.X, {description?})`` -> ``{"type": "array", ...}``."""

    def test_array_of_strings_with_enum(self) -> None:
        """The summary_kinds-style array: items is a string with enum."""
        result = array_field(
            string_field(enum=["leaf", "condensed"]),
            description="kinds filter",
        )
        assert result == {
            "type": "array",
            "items": {"type": "string", "enum": ["leaf", "condensed"]},
            "description": "kinds filter",
        }

    def test_array_of_plain_strings(self) -> None:
        """``Type.Array(Type.String())`` — used by ``summaryIds`` etc."""
        result = array_field(string_field(), description="Summary IDs to expand.")
        assert result == {
            "type": "array",
            "items": {"type": "string"},
            "description": "Summary IDs to expand.",
        }

    def test_without_description(self) -> None:
        """Description is optional."""
        result = array_field(string_field())
        assert result == {"type": "array", "items": {"type": "string"}}


# ---------------------------------------------------------------------------
# optional / OptionalField
# ---------------------------------------------------------------------------


class TestOptional:
    """The :func:`optional` marker for ``Type.Optional(X)``."""

    def test_wraps_field_in_marker_class(self) -> None:
        wrapped = optional(string_field("x"))
        assert isinstance(wrapped, OptionalField)

    def test_preserves_field_contents(self) -> None:
        original = string_field("the desc", enum=["a", "b"])
        wrapped = optional(original)
        assert dict(wrapped) == original

    def test_optional_field_is_dict_subclass(self) -> None:
        """``OptionalField`` IS a dict — downstream readers see the inner
        keys directly. Important for consumers that don't care about
        required-ness.
        """
        wrapped = optional(boolean_field("desc"))
        assert wrapped["type"] == "boolean"
        assert wrapped["description"] == "desc"

    def test_optional_input_is_not_mutated(self) -> None:
        """Modifying the wrapped result must not mutate the source."""
        source = string_field("desc")
        wrapped = optional(source)
        wrapped["extra"] = "new key"
        assert "extra" not in source


# ---------------------------------------------------------------------------
# object_schema
# ---------------------------------------------------------------------------


class TestObjectSchema:
    """``Type.Object({...})`` -> object schema with ``required`` array."""

    def test_required_array_includes_non_optional_in_order(self) -> None:
        """Property declaration order is preserved in ``required``.

        TypeBox emits ``required`` in property-declaration order. We
        match that so byte-equality with the TS export holds.
        """
        result = object_schema(
            a=string_field("first"),
            b=string_field("second"),
            c=string_field("third"),
        )
        assert result["required"] == ["a", "b", "c"]

    def test_required_array_omits_optional_properties(self) -> None:
        """``Type.Optional(X)`` properties are excluded from ``required``."""
        result = object_schema(
            pattern=string_field("required"),
            mode=optional(string_field(enum=["regex", "full_text"])),
            limit=optional(number_field(minimum=1)),
        )
        assert result["required"] == ["pattern"]

    def test_properties_include_both_required_and_optional(self) -> None:
        """``properties`` dict has BOTH required and optional fields."""
        result = object_schema(
            a=string_field("required"),
            b=optional(string_field("not required")),
        )
        assert set(result["properties"].keys()) == {"a", "b"}

    def test_optional_field_wrapper_is_stripped(self) -> None:
        """The ``OptionalField`` wrapper is converted to a plain dict in
        ``properties`` — downstream JSON serialization must produce
        ``{"type": "string", ...}`` not ``OptionalField({...})``.
        """
        result = object_schema(b=optional(string_field("desc")))
        assert type(result["properties"]["b"]) is dict
        assert not isinstance(result["properties"]["b"], OptionalField)

    def test_empty_object(self) -> None:
        """``Type.Object({})`` (no properties) is valid — emits an empty
        ``properties`` dict + empty ``required`` array.
        """
        result = object_schema()
        assert result == {"type": "object", "properties": {}, "required": []}

    def test_all_optional_yields_empty_required(self) -> None:
        """When every property is optional, ``required`` is an empty list
        (NOT missing) — TypeBox emits the key unconditionally.
        """
        result = object_schema(
            a=optional(string_field("x")),
            b=optional(string_field("y")),
        )
        assert result["required"] == []
        assert "required" in result

    def test_required_key_always_present(self) -> None:
        """``required`` is ALWAYS in the output (possibly empty)."""
        assert "required" in object_schema()
        assert "required" in object_schema(a=string_field("x"))

    def test_returns_plain_dict(self) -> None:
        """The output is a plain dict, not an OptionalField."""
        result = object_schema(a=string_field("x"))
        assert type(result) is dict

    def test_top_level_optional_object_schema_is_optional_field(self) -> None:
        """An ``object_schema`` wrapped in ``optional`` is still a valid
        OptionalField marker — useful for nested objects in a parent.
        """
        nested = optional(object_schema(a=string_field("x")))
        assert isinstance(nested, OptionalField)


# ---------------------------------------------------------------------------
# tool_schema (OpenAI function-call format)
# ---------------------------------------------------------------------------


class TestToolSchema:
    """The container that wraps name + description + parameters."""

    def test_shape(self) -> None:
        result = tool_schema(
            name="lcm_test",
            description="A test tool.",
            parameters=object_schema(x=string_field("the x")),
        )
        assert result["name"] == "lcm_test"
        assert result["description"] == "A test tool."
        assert result["parameters"]["type"] == "object"
        assert result["parameters"]["properties"] == {
            "x": {"type": "string", "description": "the x"}
        }
        assert result["parameters"]["required"] == ["x"]

    def test_keys_in_canonical_order(self) -> None:
        """``name, description, parameters`` — the OpenAI function-call
        spec order.
        """
        result = tool_schema(
            name="t",
            description="d",
            parameters=object_schema(),
        )
        assert list(result.keys()) == ["name", "description", "parameters"]


# ---------------------------------------------------------------------------
# validate_schema (Draft-07 well-formedness)
# ---------------------------------------------------------------------------


class TestValidateSchema:
    """:func:`validate_schema` wraps :class:`jsonschema.Draft7Validator`."""

    def test_well_formed_passes(self) -> None:
        """A correctly-translated tool schema validates."""
        schema = tool_schema(
            name="lcm_test",
            description="A test.",
            parameters=object_schema(
                pattern=string_field("the pattern"),
                limit=optional(number_field("max", minimum=1, maximum=100)),
            ),
        )
        # No exception means well-formed.
        validate_schema(schema)

    def test_array_with_enum_items_passes(self) -> None:
        """The ``summaryKinds``-style array shape is well-formed."""
        schema = tool_schema(
            name="lcm_test",
            description="A test.",
            parameters=object_schema(
                kinds=optional(
                    array_field(
                        string_field(enum=["leaf", "condensed"]),
                        description="kinds filter",
                    )
                ),
            ),
        )
        validate_schema(schema)

    def test_object_with_no_properties_passes(self) -> None:
        """An empty-parameters tool (e.g. ``lcm_compact`` if all-optional)
        is still well-formed.
        """
        schema = tool_schema(name="lcm_test", description="A test.", parameters=object_schema())
        validate_schema(schema)

    def test_malformed_schema_raises(self) -> None:
        """A malformed parameters object raises :class:`SchemaError`."""
        # `minimum` must be a number — passing a string triggers a
        # Draft-07 metaschema violation.
        bad_schema = {
            "name": "lcm_test",
            "description": "...",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "number", "minimum": "not a number"}},
                "required": ["x"],
            },
        }
        from jsonschema import SchemaError  # noqa: PLC0415

        with pytest.raises(SchemaError):
            validate_schema(bad_schema)

    def test_missing_parameters_key_raises(self) -> None:
        """Missing ``parameters`` is a programmer error — raises KeyError."""
        with pytest.raises(KeyError):
            validate_schema({"name": "lcm_test", "description": "..."})
