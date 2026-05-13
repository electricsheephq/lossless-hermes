"""Tests for :mod:`lossless_hermes.tools._common` — shared tool helpers.

Pins the acceptance criteria from ``epics/06-tools/06-04-tools-common.md``:

* :func:`tool_result` produces a JSON string (NOT the TS ``{content,
  details}`` shape — Hermes wraps that itself).
* :func:`read_string_param`:
    * empty string after :py:meth:`str.strip` is treated as absent;
    * missing keys raise descriptively when ``required=True``;
    * strip semantics applied to all present values.
* :func:`read_number_param`:
    * numeric coercion of ``int`` / ``float`` / numeric ``str``;
    * ``minimum`` / ``maximum`` clamp the returned value;
    * booleans rejected even though :class:`bool` is an :class:`int`
      subclass in Python.
* :func:`read_bool_param`:
    * real bools pass through;
    * ``"true"``/``"false"`` strings (case-insensitive, stripped) work;
    * ambiguous strings raise.

Source: ``lossless-claw/src/tools/common.ts`` at LCM commit ``1f07fbd``
on branch ``pr-613``.
"""

from __future__ import annotations

import json

import pytest

from lossless_hermes.tools._common import (
    read_bool_param,
    read_number_param,
    read_string_param,
    tool_result,
)


# ---------------------------------------------------------------------------
# tool_result
# ---------------------------------------------------------------------------


class TestToolResult:
    """``tool_result(payload)`` returns ``json.dumps(payload, ensure_ascii=False)``."""

    def test_simple_dict(self) -> None:
        """A plain dict round-trips through JSON."""
        result = tool_result({"hits": 3})
        assert json.loads(result) == {"hits": 3}

    def test_returns_str(self) -> None:
        """The return type is ``str``, not ``dict`` (diverges from TS)."""
        result = tool_result({"hits": 3})
        assert isinstance(result, str)

    def test_empty_dict(self) -> None:
        """Empty payloads serialize to ``'{}'``."""
        assert tool_result({}) == "{}"

    def test_nested_payload(self) -> None:
        """Nested structures (lists, dicts) round-trip."""
        payload = {
            "matches": [
                {"id": 1, "text": "alpha"},
                {"id": 2, "text": "beta"},
            ],
            "truncated": False,
        }
        result = tool_result(payload)
        assert json.loads(result) == payload

    def test_ensure_ascii_false_preserves_unicode(self) -> None:
        """Non-ASCII characters appear LITERALLY, not as ``\\uXXXX`` escapes.

        ``ensure_ascii=False`` matches the TS ``JSON.stringify`` default.
        Verified by asserting the raw character is present in the output.
        """
        result = tool_result({"name": "café"})
        assert "café" in result
        assert "\\u" not in result

    def test_ensure_ascii_false_preserves_cjk(self) -> None:
        """CJK characters preserved verbatim — same rationale as ``café``."""
        result = tool_result({"name": "日本語"})
        assert "日本語" in result

    def test_error_payload(self) -> None:
        """Error-shape payloads (the common ``return jsonResult({error})``)."""
        assert tool_result({"error": "missing pattern"}) == '{"error": "missing pattern"}'

    def test_non_serializable_raises(self) -> None:
        """Per-handler code is responsible for converting datetime / set / etc."""
        with pytest.raises(TypeError):
            tool_result({"timestamp": {1, 2, 3}})  # set is not JSON-serializable


# ---------------------------------------------------------------------------
# read_string_param
# ---------------------------------------------------------------------------


class TestReadStringParam:
    """``read_string_param(params, key, *, required, default)``."""

    # --- present values ---------------------------------------------------

    def test_simple_string(self) -> None:
        """A simple string value is returned as-is."""
        assert read_string_param({"pattern": "hello"}, "pattern") == "hello"

    def test_strip_leading_whitespace(self) -> None:
        """Leading whitespace is stripped — TS uses ``raw.trim()`` by default."""
        assert read_string_param({"pattern": "  hello"}, "pattern") == "hello"

    def test_strip_trailing_whitespace(self) -> None:
        """Trailing whitespace is stripped."""
        assert read_string_param({"pattern": "hello  "}, "pattern") == "hello"

    def test_strip_both_sides(self) -> None:
        """Internal whitespace preserved, surrounding stripped."""
        assert read_string_param({"pattern": "  hello world  "}, "pattern") == "hello world"

    def test_strip_newlines_and_tabs(self) -> None:
        """``str.strip()`` covers all whitespace classes including ``\\t``, ``\\n``."""
        assert read_string_param({"pattern": "\t\nhello\n\t"}, "pattern") == "hello"

    # --- absent / null ----------------------------------------------------

    def test_missing_key_returns_none(self) -> None:
        """Absent keys with no default return ``None``."""
        assert read_string_param({}, "pattern") is None

    def test_missing_key_returns_default(self) -> None:
        """The ``default`` kwarg is used when the key is absent."""
        assert read_string_param({}, "pattern", default="fallback") == "fallback"

    def test_none_value_returns_default(self) -> None:
        """Explicit ``None`` value is treated identically to absent."""
        assert read_string_param({"pattern": None}, "pattern", default="x") == "x"

    def test_none_value_no_default_returns_none(self) -> None:
        """Explicit ``None`` value with no default returns ``None``."""
        assert read_string_param({"pattern": None}, "pattern") is None

    # --- empty-after-strip ------------------------------------------------

    def test_empty_string_is_absent(self) -> None:
        """Empty string is treated as absent (matches TS ``allowEmpty: false``)."""
        assert read_string_param({"pattern": ""}, "pattern") is None

    def test_whitespace_only_is_absent(self) -> None:
        """Whitespace-only string strips to empty -> absent."""
        assert read_string_param({"pattern": "   "}, "pattern") is None

    def test_whitespace_only_uses_default(self) -> None:
        """Whitespace-only with a default returns the default."""
        assert read_string_param({"pattern": "   "}, "pattern", default="x") == "x"

    def test_tabs_only_is_absent(self) -> None:
        """Tabs-only -> stripped to empty -> absent."""
        assert read_string_param({"pattern": "\t\t"}, "pattern") is None

    # --- required raises --------------------------------------------------

    def test_missing_required_raises(self) -> None:
        """``required=True`` + absent key raises :class:`ValueError`."""
        with pytest.raises(ValueError, match=r"`pattern` is required"):
            read_string_param({}, "pattern", required=True)

    def test_none_required_raises(self) -> None:
        """``required=True`` + ``None`` value raises."""
        with pytest.raises(ValueError, match=r"`pattern` is required"):
            read_string_param({"pattern": None}, "pattern", required=True)

    def test_empty_required_raises(self) -> None:
        """``required=True`` + empty string raises (post-strip empty == absent)."""
        with pytest.raises(ValueError, match=r"`pattern` is required"):
            read_string_param({"pattern": ""}, "pattern", required=True)

    def test_whitespace_only_required_raises(self) -> None:
        """``required=True`` + whitespace-only raises."""
        with pytest.raises(ValueError, match=r"`pattern` is required"):
            read_string_param({"pattern": "   "}, "pattern", required=True)

    def test_error_message_includes_key_name(self) -> None:
        """Error messages name the missing key (descriptive per AC bullet 5)."""
        with pytest.raises(ValueError) as excinfo:
            read_string_param({}, "conversationId", required=True)
        assert "conversationId" in str(excinfo.value)

    # --- type coercion ----------------------------------------------------

    def test_int_value_coerced_to_string(self) -> None:
        """Integers coerce via ``str(...)`` — handles providers that emit ``42``."""
        assert read_string_param({"limit": 42}, "limit") == "42"

    def test_float_value_coerced_to_string(self) -> None:
        """Floats coerce too."""
        assert read_string_param({"rate": 3.14}, "rate") == "3.14"

    def test_list_value_rejected(self) -> None:
        """Containers raise — coercion would yield garbage like ``"['a']"``."""
        with pytest.raises(ValueError, match=r"`pattern` must be a string"):
            read_string_param({"pattern": ["a", "b"]}, "pattern")

    def test_dict_value_rejected(self) -> None:
        """Dicts raise for the same reason as lists."""
        with pytest.raises(ValueError, match=r"`pattern` must be a string"):
            read_string_param({"pattern": {"a": 1}}, "pattern")


# ---------------------------------------------------------------------------
# read_number_param
# ---------------------------------------------------------------------------


class TestReadNumberParam:
    """``read_number_param(params, key, *, minimum, maximum, default)``."""

    # --- basic coercion ---------------------------------------------------

    def test_int_value(self) -> None:
        """Integer becomes a :class:`float`."""
        result = read_number_param({"limit": 50}, "limit")
        assert result == 50.0
        assert isinstance(result, float)

    def test_float_value(self) -> None:
        """Float passes through as float."""
        assert read_number_param({"rate": 3.14}, "rate") == 3.14

    def test_numeric_string(self) -> None:
        """Numeric strings (provider stringification) parse as float."""
        assert read_number_param({"limit": "50"}, "limit") == 50.0

    def test_numeric_string_with_decimal(self) -> None:
        """``"3.14"`` parses to ``3.14``."""
        assert read_number_param({"rate": "3.14"}, "rate") == 3.14

    def test_numeric_string_stripped(self) -> None:
        """Whitespace in numeric strings is stripped before parsing."""
        assert read_number_param({"limit": "  50  "}, "limit") == 50.0

    def test_negative_number(self) -> None:
        """Negative numbers pass through (clamping applies later)."""
        assert read_number_param({"delta": -5}, "delta") == -5.0

    def test_zero(self) -> None:
        """Zero is a valid value (NOT absent)."""
        assert read_number_param({"limit": 0}, "limit") == 0.0

    # --- absent / None ----------------------------------------------------

    def test_missing_key_returns_none(self) -> None:
        """Absent keys with no default return ``None``."""
        assert read_number_param({}, "limit") is None

    def test_missing_key_with_default(self) -> None:
        """``default`` returned when key is absent."""
        assert read_number_param({}, "limit", default=50) == 50.0

    def test_none_value_returns_default(self) -> None:
        """Explicit ``None`` value returns the default."""
        assert read_number_param({"limit": None}, "limit", default=10) == 10.0

    def test_none_value_no_default(self) -> None:
        """``None`` value with no default returns ``None``."""
        assert read_number_param({"limit": None}, "limit") is None

    def test_empty_string_returns_default(self) -> None:
        """Empty / whitespace-only string falls back to default (like absent)."""
        assert read_number_param({"limit": ""}, "limit", default=10) == 10.0
        assert read_number_param({"limit": "  "}, "limit", default=10) == 10.0

    def test_empty_string_no_default_returns_none(self) -> None:
        """Empty string + no default = ``None``."""
        assert read_number_param({"limit": ""}, "limit") is None

    # --- clamping (AC bullet 5) -------------------------------------------

    def test_clamp_above_maximum(self) -> None:
        """Value > maximum is clamped to maximum."""
        assert read_number_param({"limit": 99999}, "limit", maximum=200) == 200.0

    def test_clamp_below_minimum(self) -> None:
        """Value < minimum is clamped to minimum."""
        assert read_number_param({"limit": 0}, "limit", minimum=1) == 1.0

    def test_within_range_unchanged(self) -> None:
        """Value within [minimum, maximum] passes through."""
        assert read_number_param({"limit": 50}, "limit", minimum=1, maximum=200) == 50.0

    def test_clamp_at_minimum_boundary(self) -> None:
        """Value == minimum is in-range (inclusive bound)."""
        assert read_number_param({"limit": 1}, "limit", minimum=1, maximum=200) == 1.0

    def test_clamp_at_maximum_boundary(self) -> None:
        """Value == maximum is in-range (inclusive bound)."""
        assert read_number_param({"limit": 200}, "limit", minimum=1, maximum=200) == 200.0

    def test_clamp_only_minimum(self) -> None:
        """``maximum=None`` skips upper clamp."""
        assert read_number_param({"limit": 99999}, "limit", minimum=1) == 99999.0

    def test_clamp_only_maximum(self) -> None:
        """``minimum=None`` skips lower clamp."""
        assert read_number_param({"limit": -5}, "limit", maximum=10) == -5.0

    def test_clamp_numeric_string(self) -> None:
        """Clamping applies to coerced string inputs too."""
        assert read_number_param({"limit": "99999"}, "limit", maximum=200) == 200.0

    def test_default_when_absent_not_clamped(self) -> None:
        """Default returned for absent keys is NOT clamped (caller's responsibility)."""
        # If caller sets a default outside the range, we trust them.
        assert read_number_param({}, "limit", default=500, maximum=200) == 500.0

    # --- type rejection ---------------------------------------------------

    def test_bool_rejected_true(self) -> None:
        """``True`` is NOT coerced to ``1.0`` — bool rejection is load-bearing."""
        with pytest.raises(ValueError, match=r"`limit` must be a number"):
            read_number_param({"limit": True}, "limit")

    def test_bool_rejected_false(self) -> None:
        """``False`` is NOT coerced to ``0.0``."""
        with pytest.raises(ValueError, match=r"`limit` must be a number"):
            read_number_param({"limit": False}, "limit")

    def test_non_numeric_string_raises(self) -> None:
        """``"abc"`` is not a number."""
        with pytest.raises(ValueError, match=r"`limit` must be a number"):
            read_number_param({"limit": "abc"}, "limit")

    def test_list_rejected(self) -> None:
        """Containers raise."""
        with pytest.raises(ValueError, match=r"`limit` must be a number"):
            read_number_param({"limit": [1, 2]}, "limit")

    def test_dict_rejected(self) -> None:
        """Dicts raise."""
        with pytest.raises(ValueError, match=r"`limit` must be a number"):
            read_number_param({"limit": {"v": 1}}, "limit")


# ---------------------------------------------------------------------------
# read_bool_param
# ---------------------------------------------------------------------------


class TestReadBoolParam:
    """``read_bool_param(params, key, *, default)``."""

    # --- real bools -------------------------------------------------------

    def test_true_passthrough(self) -> None:
        """``True`` passes through unchanged."""
        assert read_bool_param({"flag": True}, "flag") is True

    def test_false_passthrough(self) -> None:
        """``False`` passes through unchanged."""
        assert read_bool_param({"flag": False}, "flag") is False

    # --- absent / None ----------------------------------------------------

    def test_missing_key_returns_default_false(self) -> None:
        """Absent keys return the default (``False`` by default)."""
        assert read_bool_param({}, "flag") is False

    def test_missing_key_default_true(self) -> None:
        """Explicit ``default=True`` returned for absent keys."""
        assert read_bool_param({}, "flag", default=True) is True

    def test_none_value_returns_default(self) -> None:
        """Explicit ``None`` is treated as absent."""
        assert read_bool_param({"flag": None}, "flag", default=True) is True

    # --- string coercion (AC bullet 4) ------------------------------------

    def test_string_true_lowercase(self) -> None:
        """``"true"`` parses as ``True``."""
        assert read_bool_param({"flag": "true"}, "flag") is True

    def test_string_false_lowercase(self) -> None:
        """``"false"`` parses as ``False``."""
        assert read_bool_param({"flag": "false"}, "flag") is False

    def test_string_true_uppercase(self) -> None:
        """``"TRUE"`` parses as ``True`` (case-insensitive)."""
        assert read_bool_param({"flag": "TRUE"}, "flag") is True

    def test_string_false_mixed_case(self) -> None:
        """``"False"`` parses as ``False`` (case-insensitive)."""
        assert read_bool_param({"flag": "False"}, "flag") is False

    def test_string_true_with_whitespace(self) -> None:
        """Whitespace stripped before normalizing."""
        assert read_bool_param({"flag": "  true  "}, "flag") is True

    def test_string_yes_no(self) -> None:
        """``"yes"`` / ``"no"`` are accepted aliases."""
        assert read_bool_param({"flag": "yes"}, "flag") is True
        assert read_bool_param({"flag": "no"}, "flag") is False

    def test_string_on_off(self) -> None:
        """``"on"`` / ``"off"`` are accepted aliases."""
        assert read_bool_param({"flag": "on"}, "flag") is True
        assert read_bool_param({"flag": "off"}, "flag") is False

    def test_string_one_zero(self) -> None:
        """``"1"`` / ``"0"`` parse as ``True`` / ``False``."""
        assert read_bool_param({"flag": "1"}, "flag") is True
        assert read_bool_param({"flag": "0"}, "flag") is False

    # --- int coercion -----------------------------------------------------

    def test_int_one(self) -> None:
        """``1`` -> ``True`` (providers occasionally emit numeric booleans)."""
        assert read_bool_param({"flag": 1}, "flag") is True

    def test_int_zero(self) -> None:
        """``0`` -> ``False``."""
        assert read_bool_param({"flag": 0}, "flag") is False

    def test_other_int_rejected(self) -> None:
        """Other integers raise — ambiguous."""
        with pytest.raises(ValueError, match=r"`flag` must be a boolean"):
            read_bool_param({"flag": 2}, "flag")

    # --- ambiguous strings raise ------------------------------------------

    def test_ambiguous_string_raises(self) -> None:
        """``"maybe"`` is not unambiguously a bool."""
        with pytest.raises(ValueError, match=r"`flag` must be a boolean"):
            read_bool_param({"flag": "maybe"}, "flag")

    def test_empty_string_raises(self) -> None:
        """Empty string is not a bool (would be silently-default otherwise)."""
        with pytest.raises(ValueError, match=r"`flag` must be a boolean"):
            read_bool_param({"flag": ""}, "flag")

    def test_random_string_raises(self) -> None:
        """``"truthy"`` is not in the accepted vocabulary."""
        with pytest.raises(ValueError, match=r"`flag` must be a boolean"):
            read_bool_param({"flag": "truthy"}, "flag")

    def test_list_rejected(self) -> None:
        """Lists raise."""
        with pytest.raises(ValueError, match=r"`flag` must be a boolean"):
            read_bool_param({"flag": [True]}, "flag")

    def test_float_rejected(self) -> None:
        """Floats raise — ``1.0`` is too easy to confuse with truthy coercion."""
        with pytest.raises(ValueError, match=r"`flag` must be a boolean"):
            read_bool_param({"flag": 1.0}, "flag")


# ---------------------------------------------------------------------------
# Interaction smoke tests (load-bearing: helpers used together in handlers)
# ---------------------------------------------------------------------------


class TestHandlerComposition:
    """Smoke tests for the helpers used together — mirrors a tool handler skeleton."""

    def test_full_handler_skeleton(self) -> None:
        """Helpers compose without surprise — typical handler arg-parse pattern."""
        params: dict[str, object] = {
            "pattern": "  hello  ",
            "limit": "99999",
            "all_conversations": "true",
        }
        pattern = read_string_param(params, "pattern", required=True)
        limit = read_number_param(params, "limit", minimum=1, maximum=200, default=50)
        all_convs = read_bool_param(params, "all_conversations", default=False)
        result = tool_result({"pattern": pattern, "limit": limit, "all": all_convs})
        decoded = json.loads(result)
        assert decoded == {"pattern": "hello", "limit": 200.0, "all": True}

    def test_handler_error_path(self) -> None:
        """Missing-required string -> handler catches ValueError and returns error result."""
        params: dict[str, object] = {}
        try:
            read_string_param(params, "pattern", required=True)
            error_msg = None
        except ValueError as exc:
            error_msg = str(exc)
        assert error_msg is not None
        result = tool_result({"error": error_msg})
        decoded = json.loads(result)
        assert decoded == {"error": "`pattern` is required."}
