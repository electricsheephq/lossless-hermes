"""Shared helpers for LCM tool handlers.

Ports ``lossless-claw/src/tools/common.ts`` (LCM commit ``1f07fbd`` on
branch ``pr-613``, 53 LOC) to Python with the contract adjustments
required by Hermes's tool-call surface.

What this module owns
---------------------

1. **:func:`tool_result`** — render a structured payload as a JSON
   string. Hermes's :py:meth:`ContextEngine.handle_tool_call` returns
   a JSON-encoded string (see ``run_agent.py:11249`` and the error path
   at ``11253`` that wraps failures in ``json.dumps({"error": ...})``).
   Per ``docs/porting-guides/tools.md:540-544`` this is the explicit
   divergence from the TS ``jsonResult`` shape — the TS function builds
   a ``{content, details}`` structure for the OpenClaw agent SDK; the
   Python port returns a plain JSON string because Hermes wraps the
   handler output itself.

2. **:func:`read_string_param`** — read a string parameter from a
   ``params`` dict, with ``required`` and ``default`` knobs. Empty
   string after :py:meth:`str.strip` is treated as absent (matches the
   TS ``readStringParam`` semantics: ``allowEmpty`` defaults to
   ``false``, so a whitespace-only value is returned as ``undefined``).

3. **:func:`read_number_param`** — read a numeric parameter with
   optional ``minimum`` / ``maximum`` clamping. Coerces ``int``,
   ``float``, and numeric strings; non-numeric raises
   :class:`ValueError`. Out-of-range values are silently clamped
   (callers want a guaranteed-in-range float, not an exception, when
   an agent provider misbehaves and emits e.g. ``limit=99999`` against
   a ``maximum=200`` schema).

4. **:func:`read_bool_param`** — read a boolean parameter, accepting
   real bools AND the strings ``"true"``/``"false"`` (case-insensitive)
   because some agent providers stringify JSON booleans in tool-call
   arguments.

Divergence from the TS source
-----------------------------

TS ``readStringParam`` accepts an ``options`` object with
``required``, ``trim``, ``allowEmpty``, and ``label`` keys. The
Python port narrows this to ``required`` + ``default`` per the issue
spec (``epics/06-tools/06-04-tools-common.md``):

* ``trim`` is **always on** in the Python port — every caller in TS
  passes the default (``trim != false``), so the parameter is dead
  weight. If a future tool needs untrimmed input, add a
  ``trim: bool = True`` knob in a follow-up.
* ``allowEmpty`` is **always false** in the Python port — empty
  string after strip is treated as absent. This matches the TS
  default and is what every TS call site relies on.
* ``label`` (custom error-message label) is replaced by the parameter
  ``key`` itself in the raised exception. ``ValueError`` rather than
  ``Error`` because Python idioms.

TS ``readStringParam`` is exported but never called from another
module in v4.1 (``grep -rn readStringParam src/`` returns only the
definition). The Python equivalent is forward-looking — Wave 5
per-tool ports (06-07..06-14) will use it.

Type annotations
----------------

``params`` is typed as ``dict[str, Any]`` because Hermes hands tool
handlers an already-parsed JSON dict (the LLM provider's tool-call
``arguments`` field after :py:func:`json.loads`). Hermes does NOT
validate the dict shape against the tool's declared schema — that's
the agent provider's job, and some providers are sloppy. These
helpers are the defensive wall between provider-emitted JSON and the
handler's typed view.

References
----------

* TS source: ``lossless-claw/src/tools/common.ts``
* Porting guide: ``docs/porting-guides/tools.md`` §"common.ts" lines
  540-544.
* Issue spec: ``epics/06-tools/06-04-tools-common.md``.
* Hermes wiring: ``hermes-agent/run_agent.py:11249`` (handle_tool_call
  call site that consumes our return value).
"""

from __future__ import annotations

import json
from typing import Any, Final

# ---------------------------------------------------------------------------
# tool_result
# ---------------------------------------------------------------------------
#
# Hermes expects a JSON-string return from ContextEngine.handle_tool_call
# (see run_agent.py:11249-11253). This helper centralizes the encoding
# so every per-tool handler ends with the same one-line return rather
# than scattered ``return json.dumps(...)`` calls with subtly different
# kwargs.


def tool_result(payload: dict[str, Any]) -> str:
    """Encode a tool-result ``payload`` as a JSON string for Hermes.

    Hermes's :py:meth:`ContextEngine.handle_tool_call` returns a
    JSON-encoded string (consumed by ``run_agent.py:11249``). This
    helper is the canonical encoder for every LCM tool handler — use
    it instead of inlining :py:func:`json.dumps` so the encoding
    options stay uniform across the eight tools.

    The TS port returns a structured dict ``{content: [...], details:
    ...}`` matching the OpenClaw agent SDK shape. Hermes builds that
    structure itself, so the Python handler returns the inner payload
    JSON-encoded — see ``docs/porting-guides/tools.md`` §"common.ts"
    for the contract diff.

    Args:
        payload: The structured tool result. Typically the per-handler
            response shape (e.g. ``{"matches": [...], "truncated":
            false}`` for ``lcm_grep``). Empty payloads are valid.

    Returns:
        ``json.dumps(payload, ensure_ascii=False)``. The
        ``ensure_ascii=False`` flag preserves non-ASCII characters
        (entity surface forms, Unicode quotes in user content) as
        their literal bytes — matching the TS ``JSON.stringify``
        default. Hermes serializes the string to the wire as-is.

    Raises:
        TypeError: if ``payload`` contains non-JSON-serializable values
            (e.g. ``datetime``, ``set``). Per-handler code is
            responsible for converting these to primitives before
            calling :func:`tool_result`.

    Examples:
        >>> tool_result({"hits": 3, "items": ["a", "b", "c"]})
        '{"hits": 3, "items": ["a", "b", "c"]}'
        >>> tool_result({"error": "missing pattern"})
        '{"error": "missing pattern"}'
        >>> tool_result({})
        '{}'
        >>> tool_result({"surface": "café"})
        '{"surface": "café"}'
    """
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# read_string_param
# ---------------------------------------------------------------------------
#
# Matches TS readStringParam semantics:
#   - missing key OR null OR undefined => default (None) or raise if required
#   - non-string value => raise (callers must declare the type in the schema)
#   - present value is str(...).strip()'d; empty after strip => absent
#
# The strip-then-empty-is-absent rule is load-bearing: agent providers
# sometimes emit `"  "` for an unset string field instead of omitting
# the key. TS folds this into "absent"; Python does the same.


def read_string_param(
    params: dict[str, Any],
    key: str,
    *,
    required: bool = False,
    default: str | None = None,
) -> str | None:
    """Read a string parameter from ``params``, applying TS semantics.

    Behaviour summary (mirrors
    ``lossless-claw/src/tools/common.ts:22-53``):

    * Key absent OR value is ``None``: return ``default`` (or raise
      :class:`ValueError` if ``required=True``).
    * Value present but not :class:`str` and not :class:`int` /
      :class:`float` / :class:`bool`: raise :class:`ValueError`
      (consistent with TS rejecting non-string raw values).
    * Value present: coerced via ``str(value).strip()``. Empty after
      strip is treated as absent (returns ``default`` or raises if
      required).

    The empty-after-strip-is-absent rule comes from TS
    ``readStringParam`` where ``allowEmpty`` defaults to ``false`` —
    every v4.1 call site relies on it.

    Args:
        params: The tool-call ``arguments`` dict from the LLM provider.
        key: The parameter name to read.
        required: When ``True``, raise :class:`ValueError` if the key
            is absent or the value strips to empty. When ``False``
            (default), return ``default`` in those cases.
        default: Value to return when the key is absent (or strips to
            empty) and ``required`` is ``False``. Default ``None``.

    Returns:
        The stripped string value, or ``default`` when absent /
        empty-after-strip.

    Raises:
        ValueError: if ``required=True`` and the key is absent OR the
            value strips to empty; or if the raw value is present but
            not a primitive type that can sensibly coerce to string
            (e.g. ``list``, ``dict``).

    Examples:
        >>> read_string_param({"pattern": "hello"}, "pattern")
        'hello'
        >>> read_string_param({"pattern": "  hello  "}, "pattern")
        'hello'
        >>> read_string_param({}, "pattern") is None
        True
        >>> read_string_param({"pattern": ""}, "pattern") is None
        True
        >>> read_string_param({"pattern": "   "}, "pattern") is None
        True
        >>> read_string_param({}, "pattern", default="fallback")
        'fallback'
        >>> read_string_param({"pattern": None}, "pattern", default="x")
        'x'

        Missing-required raises with a descriptive message::

            >>> read_string_param({}, "pattern", required=True)
            Traceback (most recent call last):
              ...
            ValueError: `pattern` is required.
    """
    raw = params.get(key)

    # Missing OR null => default-or-raise. The `key not in params` check
    # is technically redundant given `.get(...)` returns None for both
    # cases, but spelling it out keeps the branching obvious for readers
    # who want to distinguish "key absent" from "key present but null".
    if raw is None:
        if required:
            raise ValueError(f"`{key}` is required.")
        return default

    # Reject containers/objects — coercing those to a string via str(...)
    # yields garbage like "{'a': 1}" that no caller wants. Primitive
    # numeric/bool types are allowed because str() is well-defined for
    # them (TS would have rejected those, but Python tool-call providers
    # occasionally emit `"limit": 5` as the *string* "5" and we
    # de-stringify in read_number_param — we accept the symmetric case
    # here for compatibility).
    if not isinstance(raw, (str, int, float, bool)):
        raise ValueError(f"`{key}` must be a string.")

    value = str(raw).strip()
    if not value:
        if required:
            raise ValueError(f"`{key}` is required.")
        return default

    return value


# ---------------------------------------------------------------------------
# read_number_param
# ---------------------------------------------------------------------------
#
# Spec calls for numeric coercion + clamping (NOT raising) on out-of-
# range. Clamping is the defensive choice for tool handlers because:
#   - TypeBox schemas declare minimum/maximum but agent providers
#     don't always validate — `limit: 99999` happens.
#   - Raising would surface a JSON error to the model where a clamp
#     produces a useful result.
#   - The TS handlers use Math.trunc(p.limit) + manual Math.min(...) /
#     Math.max(...) inline at every call site (see grep-tool.ts:244-246);
#     this helper centralizes that pattern.


def read_number_param(
    params: dict[str, Any],
    key: str,
    *,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    default: float | int | None = None,
) -> float | None:
    """Read a numeric parameter from ``params``, clamping to range.

    Coerces :class:`int`, :class:`float`, and numeric strings (e.g.
    ``"42"``, ``"3.14"``) to :class:`float`. Non-numeric raw values
    raise :class:`ValueError`. ``bool`` is **rejected** despite being
    an :class:`int` subclass in Python — agent providers emit booleans
    deliberately, and silently coercing ``True`` to ``1.0`` is a bug
    surface.

    Out-of-range values are silently clamped (not rejected): if
    ``raw < minimum`` the result is ``minimum``; if ``raw > maximum``
    the result is ``maximum``. This matches the TS pattern of
    ``Math.min(Math.max(value, minimum), maximum)`` used inline in
    ``lcm-grep-tool.ts`` and others — see ``grep-tool.ts:244-246`` for
    the VERBATIM_HARD_CAP example.

    Args:
        params: The tool-call ``arguments`` dict from the LLM provider.
        key: The parameter name to read.
        minimum: Optional inclusive lower bound (clamping). When the
            parsed value is less than ``minimum``, the result is
            ``minimum``.
        maximum: Optional inclusive upper bound (clamping). When the
            parsed value exceeds ``maximum``, the result is
            ``maximum``.
        default: Value to return when the key is absent or the value
            is :class:`None`. The default itself is NOT clamped — it's
            the caller's responsibility to make ``default`` lie within
            ``[minimum, maximum]`` if both are set.

    Returns:
        A :class:`float` in ``[minimum, maximum]``, or ``default`` (as
        a :class:`float` if numeric) when absent. ``None`` is returned
        only if ``default`` is ``None`` AND the key is absent.

    Raises:
        ValueError: if the raw value is present but not a number, a
            numeric string, or is a :class:`bool`.

    Examples:
        >>> read_number_param({"limit": 50}, "limit")
        50.0
        >>> read_number_param({"limit": "50"}, "limit")
        50.0
        >>> read_number_param({"limit": 99999}, "limit", maximum=200)
        200.0
        >>> read_number_param({"limit": 0}, "limit", minimum=1)
        1.0
        >>> read_number_param({}, "limit", default=50)
        50.0
        >>> read_number_param({"limit": None}, "limit", default=10)
        10.0
        >>> read_number_param({}, "limit") is None
        True
        >>> read_number_param({"limit": 3.14}, "limit")
        3.14

        Booleans rejected even though ``bool`` extends ``int``::

            >>> read_number_param({"limit": True}, "limit")
            Traceback (most recent call last):
              ...
            ValueError: `limit` must be a number.
    """
    raw = params.get(key)
    if raw is None:
        return float(default) if default is not None else None

    # bool is an int subclass — reject before the int check.
    if isinstance(raw, bool):
        raise ValueError(f"`{key}` must be a number.")

    value: float
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        # Numeric strings: tool-call providers occasionally stringify
        # numbers (Anthropic Claude has historically done this for
        # `"arguments"` values). Strip then float-parse.
        stripped = raw.strip()
        if not stripped:
            return float(default) if default is not None else None
        try:
            value = float(stripped)
        except ValueError as exc:
            raise ValueError(f"`{key}` must be a number.") from exc
    else:
        raise ValueError(f"`{key}` must be a number.")

    # Clamp. Tests pin this behaviour explicitly.
    if minimum is not None and value < minimum:
        value = float(minimum)
    if maximum is not None and value > maximum:
        value = float(maximum)
    return value


# ---------------------------------------------------------------------------
# read_bool_param
# ---------------------------------------------------------------------------
#
# Agent providers sometimes stringify booleans. The spec calls out
# "true"/"false" string handling explicitly.

_TRUE_STRINGS: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})


def read_bool_param(
    params: dict[str, Any],
    key: str,
    *,
    default: bool = False,
) -> bool:
    """Read a boolean parameter from ``params``, accepting stringified bools.

    Accepts:

    * Real :class:`bool` values — passed through unchanged.
    * :class:`str` values matching the common true/false vocabularies
      (case-insensitive, stripped): ``true``/``false``, ``1``/``0``,
      ``yes``/``no``, ``on``/``off``. Other strings raise.
    * :class:`int` ``0`` or ``1`` — handled because some providers
      emit numeric booleans. Other integers raise.

    Absent / ``None`` returns ``default`` (does not raise — bool params
    are never "required" in the LCM tool surface; callers always set a
    default).

    Args:
        params: The tool-call ``arguments`` dict from the LLM provider.
        key: The parameter name to read.
        default: Value to return when the key is absent or ``None``.
            Default ``False`` — matches TypeBox's
            ``Type.Optional(Type.Boolean({default: false}))`` idiom.

    Returns:
        A :class:`bool`. Never ``None``.

    Raises:
        ValueError: if the raw value is present and cannot be
            unambiguously parsed as a boolean.

    Examples:
        >>> read_bool_param({"flag": True}, "flag")
        True
        >>> read_bool_param({"flag": False}, "flag")
        False
        >>> read_bool_param({"flag": "true"}, "flag")
        True
        >>> read_bool_param({"flag": "FALSE"}, "flag")
        False
        >>> read_bool_param({"flag": "  yes  "}, "flag")
        True
        >>> read_bool_param({"flag": 1}, "flag")
        True
        >>> read_bool_param({"flag": 0}, "flag")
        False
        >>> read_bool_param({}, "flag")
        False
        >>> read_bool_param({}, "flag", default=True)
        True
        >>> read_bool_param({"flag": None}, "flag", default=True)
        True

        Ambiguous strings rejected::

            >>> read_bool_param({"flag": "maybe"}, "flag")
            Traceback (most recent call last):
              ...
            ValueError: `flag` must be a boolean.
    """
    raw = params.get(key)
    if raw is None:
        return default

    if isinstance(raw, bool):
        # bool MUST come before int — bool is an int subclass.
        return raw

    if isinstance(raw, int):
        if raw == 1:
            return True
        if raw == 0:
            return False
        raise ValueError(f"`{key}` must be a boolean.")

    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
        raise ValueError(f"`{key}` must be a boolean.")

    raise ValueError(f"`{key}` must be a boolean.")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__: Final = (
    "read_bool_param",
    "read_number_param",
    "read_string_param",
    "tool_result",
)
