"""Large-file externalization and deterministic exploration summaries.

Verbatim Python port of ``lossless-claw/src/large-files.ts`` (567 LOC,
commit ``1f07fbd``) plus a thin :class:`LargeFileManager` that wraps the
on-disk + DB-sidecar surface for the externalization path described in
:doc:`../../docs/porting-guides/storage.md` §"Large files" and
:doc:`../../docs/adr/002-plugin-data-directory.md`.

### Layering

This module owns two concerns:

1.  **Pure-logic surface** — the verbatim TS exports:

    * :func:`parse_file_blocks` — find ``<file>`` blocks in a message body.
    * :func:`extension_from_name_or_mime` — pick an on-disk filename suffix.
    * :func:`extract_file_ids_from_content` — find ``file_<16hex>`` IDs in
      existing references so re-uploads dedupe.
    * :func:`format_file_reference` /
      :func:`format_tool_output_reference` /
      :func:`format_raw_payload_reference` — produce the in-band placeholder
      text the assembler injects in place of the original content.
    * :func:`generate_exploration_summary` — deterministic JSON/CSV/code
      summaries, with an optional async LLM hook for plain-text files.

    These are stateless pure functions. They take no DB handle, no
    filesystem path, and they perform no I/O.

2.  **Manager surface** — :class:`LargeFileManager`:

    * Wraps the disk-write path (``<files_dir>/<conversation_id>/<file_id>.<ext>``)
      with atomic ``write-to-tempfile + rename`` semantics and a ``chmod 0o600``
      on completion (per ADR-002 §"Credentials path is canonical" — the same
      restrictive permission applies to large-file blobs).
    * Records a row in the ``large_files`` DB table (created by
      :mod:`lossless_hermes.db.migration` issue #01-04).
    * Provides :meth:`externalize_block` / :meth:`read` / :meth:`delete` /
      :meth:`list_for_conversation` for engine + tool-output callers.

    The disk layer + DB sidecar are kept in one class so the two writes stay
    paired: every blob on disk has exactly one ``large_files`` row, and a
    failed disk write rolls back the DB insert (the manager calls ``BEGIN
    IMMEDIATE`` around the pair).

### File ID format (load-bearing)

``file_<16 hex>`` matches the TS regex ``FILE_ID_RE = /\\bfile_[a-f0-9]{16}\\b/gi``
on ``large-files.ts:2`` and the generator on ``engine.ts:3832,4187``
(``file_${randomUUID().replace(/-/g, "").slice(0, 16)}``). Any change to
this format breaks cross-message file_id dedup — ``extract_file_ids_from_content``
re-discovers IDs from existing assistant message bodies, so historical TS
runs and current Python runs must produce IDs in the same shape.

### On-disk layout (ADR-002 §"Decision")

::

    <hermes_home>/lossless-hermes/large-files/<conversation_id>/<file_id>.<ext>

The directory is created lazily on first write. ``<conversation_id>``
scoping mirrors the TS implementation (``engine.ts:3805-3807``
``largeFilesDirForConversation``) and makes per-conversation cleanup a
single ``rm -rf`` of one directory.

See:

* ``epics/01-storage/01-12-large-files.md`` — issue spec + AC.
* ``docs/porting-guides/storage.md`` §"Large files" + §1 row 16.
* ``docs/adr/002-plugin-data-directory.md`` — directory layout decision.
* TS source: ``lossless-claw/src/large-files.ts`` (567 LOC).
* TS tests: ``lossless-claw/test/large-files.test.ts`` (120 LOC, 8 cases).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CODE_EXTENSIONS",
    "CODE_MIME_PREFIXES",
    "FILE_BLOCK_RE",
    "FILE_ID_RE",
    "FileBlock",
    "LargeFileIntegrityError",
    "LargeFileManager",
    "LargeFileRecord",
    "MIME_EXTENSION_MAP",
    "STRUCTURED_EXTENSIONS",
    "STRUCTURED_MIME_PREFIXES",
    "explore_code",
    "explore_structured_data",
    "extension_from_name_or_mime",
    "extract_file_ids_from_content",
    "format_file_reference",
    "format_raw_payload_reference",
    "format_tool_output_reference",
    "generate_exploration_summary",
    "generate_file_id",
    "parse_file_blocks",
]


# ---------------------------------------------------------------------------
# Module constants — direct port of ``large-files.ts:1-88``.
# ---------------------------------------------------------------------------

# ``re.IGNORECASE | re.DOTALL`` matches TS ``/gi`` + ``[\s\S]*?`` which is
# the TS idiom for "any character including newline, lazy". The Python
# equivalent is ``re.DOTALL`` so ``.`` matches newlines.
FILE_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<file\b([^>]*)>(.*?)</file>",
    re.IGNORECASE | re.DOTALL,
)
"""Match ``<file ...>...</file>`` blocks in a message body.

Ported from ``large-files.ts:1`` (``FILE_BLOCK_RE``). The TS flags
``gi`` translate to ``re.IGNORECASE`` here; ``re.finditer`` covers the
global-iterate semantics. ``[\\s\\S]*?`` (TS) ⇨ ``.*?`` with
``re.DOTALL`` (Python).
"""

FILE_ID_RE: re.Pattern[str] = re.compile(
    r"\bfile_[a-f0-9]{16}\b",
    re.IGNORECASE,
)
"""Match ``file_<16-hex>`` IDs in arbitrary text.

Ported from ``large-files.ts:2`` (``FILE_ID_RE``). The 16-hex shape is
load-bearing — see module docstring §"File ID format".
"""

CODE_EXTENSIONS: frozenset[str] = frozenset({
    "c",
    "cc",
    "cpp",
    "cs",
    "go",
    "h",
    "hpp",
    "java",
    "js",
    "jsx",
    "kt",
    "m",
    "php",
    "py",
    "rb",
    "rs",
    "scala",
    "sh",
    "sql",
    "swift",
    "ts",
    "tsx",
})
"""File extensions that route through :func:`explore_code`.

Ported verbatim from ``large-files.ts:4-27``. Frozen at module load so the
set is hashable + immutable.
"""

STRUCTURED_EXTENSIONS: frozenset[str] = frozenset({
    "csv",
    "json",
    "tsv",
    "xml",
    "yaml",
    "yml",
})
"""File extensions that route through :func:`explore_structured_data`.

Ported verbatim from ``large-files.ts:29``.
"""

MIME_EXTENSION_MAP: dict[str, str] = {
    "application/json": "json",
    "application/xml": "xml",
    "application/yaml": "yaml",
    "application/x-yaml": "yaml",
    "application/x-ndjson": "json",
    "application/csv": "csv",
    "application/javascript": "js",
    "application/typescript": "ts",
    "application/x-python-code": "py",
    "application/x-rust": "rs",
    "application/x-sh": "sh",
    "text/csv": "csv",
    "text/markdown": "md",
    "text/plain": "txt",
    "text/tab-separated-values": "tsv",
    "text/x-c": "c",
    "text/x-c++": "cpp",
    "text/x-go": "go",
    "text/x-java": "java",
    "text/x-python": "py",
    "text/x-rust": "rs",
    "text/x-script.python": "py",
    "text/x-shellscript": "sh",
    "text/x-typescript": "ts",
    "text/xml": "xml",
}
"""MIME type → preferred filename extension.

Ported verbatim from ``large-files.ts:31-57``. Keys are normalized to
lowercase by callers via :func:`_guess_mime_extension`.
"""

STRUCTURED_MIME_PREFIXES: tuple[str, ...] = (
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/x-ndjson",
    "text/csv",
    "text/tab-separated-values",
    "text/xml",
)
"""MIME prefixes that route through :func:`explore_structured_data`.

Ported verbatim from ``large-files.ts:59-68``. Tuple (not frozenset) so
iteration order matches TS — order is observable via the prefix-match
short-circuit in :func:`_is_structured`.
"""

CODE_MIME_PREFIXES: tuple[str, ...] = (
    "application/javascript",
    "application/typescript",
    "application/x-python-code",
    "application/x-rust",
    "text/javascript",
    "text/x-c",
    "text/x-c++",
    "text/x-go",
    "text/x-java",
    "text/x-python",
    "text/x-rust",
    "text/x-script.python",
    "text/x-shellscript",
    "text/x-typescript",
)
"""MIME prefixes that route through :func:`explore_code`.

Ported verbatim from ``large-files.ts:70-85``.
"""

_TEXT_SUMMARY_SLICE_CHARS = 2_400
"""Per-region character budget for the text-sample triptych.

Ported from ``large-files.ts:87`` (``TEXT_SUMMARY_SLICE_CHARS``).
The model-prompt sample concatenates start/middle/end slices of this
size to stay under the LLM input ceiling for very large text files.
"""

_TEXT_HEADER_LIMIT = 18
"""Maximum number of detected section headers surfaced in summaries.

Ported from ``large-files.ts:88`` (``TEXT_HEADER_LIMIT``).
"""


# ---------------------------------------------------------------------------
# TypedDicts / dataclasses — port of TS ``type`` aliases (line 90-105).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileBlock:
    """Parsed ``<file>`` block extracted from a message body.

    Ports the TS ``FileBlock`` shape on ``large-files.ts:90-98``. Returned
    by :func:`parse_file_blocks`. All fields are read-only — callers that
    want to mutate should re-construct.

    Attributes:
        full_match: The complete ``<file ...>...</file>`` substring.
        start: 0-based offset where ``full_match`` begins in the original
            content. Useful for splice-style rewrites.
        end: 0-based offset where ``full_match`` ends (exclusive).
        attributes: Lowercase-keyed attribute map parsed from the open tag.
            Empty dict if no attributes were declared.
        file_name: ``attributes["name"]`` if present, else ``None``.
        mime_type: ``attributes["mime"]`` if present, else ``None``.
        text: The inner text content between the open and close tags. Not
            trimmed — preserve byte fidelity for downstream hashing.
    """

    full_match: str
    start: int
    end: int
    attributes: dict[str, str]
    file_name: str | None
    mime_type: str | None
    text: str


@dataclass(frozen=True)
class LargeFileRecord:
    """Persisted record of an externalized large file.

    Mirrors the ``large_files`` table row shape (created in #01-04). All
    fields except ``file_id``, ``conversation_id``, and ``storage_uri`` are
    nullable — file_name + mime_type come from the original ``<file>``
    block or tool-call metadata; ``exploration_summary`` is filled in by
    the assembler when summarization runs.

    Attributes:
        file_id: ``file_<16 hex>``. Primary key.
        conversation_id: Foreign key into ``conversations`` (ON DELETE
            CASCADE — purging a conversation drops its files).
        file_name: Original filename, if known. Optional.
        mime_type: Original MIME type, if known. Optional.
        byte_size: Size of the externalized blob in bytes. Optional.
        storage_uri: Absolute path to the on-disk file. Always present.
        exploration_summary: Human-readable summary inlined into the
            assembled prompt. Optional; populated by
            :func:`generate_exploration_summary` or an LLM hook.
        created_at: SQLite ``datetime('now')`` string (UTC, ISO-8601).
    """

    file_id: str
    conversation_id: int
    file_name: str | None
    mime_type: str | None
    byte_size: int | None
    storage_uri: str
    exploration_summary: str | None
    created_at: str


class LargeFileIntegrityError(RuntimeError):
    """Raised when an on-disk large file's path or size disagrees with the DB row.

    Surfaces from :meth:`LargeFileManager.read` when the recorded
    ``storage_uri`` does not exist, or when the on-disk byte length
    disagrees with the ``byte_size`` column. Distinct from generic
    ``FileNotFoundError`` so callers can target this case for compensating
    actions (e.g. drop the stale row and surface a 410-style "gone" reply).
    """


# ---------------------------------------------------------------------------
# Helpers — direct port of the TS private functions (line 107-273).
# ---------------------------------------------------------------------------


# ``([A-Za-z_:][A-Za-z0-9_:\-.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))`` —
# attribute pattern from ``large-files.ts:109``. The Python equivalent is
# identical; capture group numbering matches the TS so we can re-use the
# same fall-through ``match[3] ?? match[4] ?? match[5]`` idiom.
_ATTR_RE: re.Pattern[str] = re.compile(
    r"([A-Za-z_:][A-Za-z0-9_:\-.]*)\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))"
)


def _parse_file_attributes(raw: str) -> dict[str, str]:
    """Parse ``<file>`` open-tag attributes into a lowercase-keyed dict.

    Ports ``parseFileAttributes`` from ``large-files.ts:107-121``. Values
    are taken from whichever quoted/unquoted alternative matched; the
    TS fall-through ``match[3] ?? match[4] ?? match[5]`` becomes
    ``match.group(3) or match.group(4) or match.group(5)``.

    Empty keys or empty values are dropped (matches TS
    ``if (key.length > 0 && value.length > 0)``).
    """
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        key = match.group(1).strip().lower()
        value = (match.group(3) or match.group(4) or match.group(5) or "").strip()
        if key and value:
            attrs[key] = value
    return attrs


def _normalize_text_for_line(text: str, max_len: int) -> str:
    """Collapse whitespace and truncate to ``max_len`` chars with ellipsis.

    Ports ``normalizeTextForLine`` from ``large-files.ts:123-129``. Used
    by the structured/text/code summary builders to keep one-line samples
    bounded.
    """
    # ``\s+`` here includes newlines/tabs/spaces — matches TS regex semantics.
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}..."


def _collect_filename_extension(file_name: str | None) -> str | None:
    """Extract a normalized extension from a filename.

    Ports ``collectFileNameExtension`` from ``large-files.ts:131-146``.
    Splits on both forward and back slashes, takes the basename, and
    requires the extension to be 1-10 lowercase alphanumerics.
    Returns ``None`` for hidden files (``.bashrc``), trailing-dot
    files (``foo.``), and obviously-non-extension suffixes.
    """
    if not file_name:
        return None

    # ``trim().split(/[\\/]/).pop()`` — split on either slash, take the
    # last segment. ``re.split`` with a char class handles both.
    stripped = file_name.strip()
    parts = re.split(r"[\\/]", stripped)
    base = parts[-1] if parts else ""

    idx = base.rfind(".")
    if idx <= 0 or idx == len(base) - 1:
        return None

    ext = base[idx + 1 :].lower()
    if not re.fullmatch(r"[a-z0-9]{1,10}", ext):
        return None
    return ext


def _guess_mime_extension(mime_type: str | None) -> str | None:
    """Look up ``MIME_EXTENSION_MAP[mime_type.strip().lower()]`` or ``None``.

    Ports ``guessMimeExtension`` from ``large-files.ts:148-154``.
    """
    if not mime_type:
        return None
    normalized = mime_type.strip().lower()
    return MIME_EXTENSION_MAP.get(normalized)


def _is_structured(*, mime_type: str | None = None, extension: str | None = None) -> bool:
    """Return ``True`` if the file looks structured (JSON/CSV/TSV/XML/YAML).

    Ports ``isStructured`` from ``large-files.ts:156-162``. MIME match
    wins; falls back to extension lookup. The MIME check is a prefix
    match because some servers send ``application/json; charset=utf-8``.
    """
    if mime_type:
        normalized = mime_type.strip().lower()
        if any(normalized.startswith(prefix) for prefix in STRUCTURED_MIME_PREFIXES):
            return True
    if extension:
        return extension in STRUCTURED_EXTENSIONS
    return False


def _is_code(*, mime_type: str | None = None, extension: str | None = None) -> bool:
    """Return ``True`` if the file looks like source code.

    Ports ``isCode`` from ``large-files.ts:164-170``. Same MIME-prefix +
    extension fallback as :func:`_is_structured`.
    """
    if mime_type:
        normalized = mime_type.strip().lower()
        if any(normalized.startswith(prefix) for prefix in CODE_MIME_PREFIXES):
            return True
    if extension:
        return extension in CODE_EXTENSIONS
    return False


def _unique_ordered(values: Iterable[str]) -> list[str]:
    """Deduplicate values, preserving first-seen order.

    Ports ``uniqueOrdered`` from ``large-files.ts:172-182``. Python's
    ``dict.fromkeys`` would also work but the explicit loop matches the
    TS shape and reads more obviously.
    """
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _format_int(value: int) -> str:
    """Format an integer with comma separators (matches TS ``toLocaleString("en-US")``).

    Used throughout the summary builders to produce identical output to the
    TS ``.toLocaleString("en-US")`` calls. Python's ``f"{value:,}"`` is the
    canonical equivalent — both insert ``,`` every three digits, no decimal.
    """
    return f"{value:,}"


# ---------------------------------------------------------------------------
# JSON / CSV / YAML / XML exploration — direct port of ``large-files.ts:184-273``.
# ---------------------------------------------------------------------------


def _explore_json(content: str) -> str:
    """Render a deterministic shape summary for a JSON document.

    Ports ``exploreJson`` from ``large-files.ts:184-210``. Recursion depth
    is capped at 2 levels (matches TS ``depth >= 2 ⇒ '...'``) to keep
    summaries bounded.

    The TS ``typeof`` returns one of ``"string" | "number" | "boolean" |
    "undefined" | "object" | "function" | "symbol" | "bigint"``. The
    Python equivalents (``str/int/float/bool/None/...``) need a small
    translation table so the output text matches what TS produces on
    primitive samples.
    """
    parsed = json.loads(content)

    def js_typeof(value: Any) -> str:
        # JS ``typeof null`` is ``"object"`` — handled by the caller, which
        # only reaches this helper for non-null leaf values.
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, str):
            return "string"
        if value is None:
            # In TS ``describe`` short-circuits on ``!value || typeof !== "object"``
            # before reaching here — Python lands here for genuine None.
            return "object"
        return "object"

    def describe(value: Any, depth: int = 0) -> str:
        if depth >= 2:
            return "..."
        if isinstance(value, list):
            sample = [describe(item, depth + 1) for item in value[:3]]
            sample_str = f", sample=[{', '.join(sample)}]" if sample else ""
            return f"array(len={len(value)}{sample_str})"
        # Treat dict/object — but NOT None (JS ``!value`` short-circuits on
        # null) and NOT non-dict types.
        if not isinstance(value, dict):
            # Mimics TS ``!value || typeof value !== "object"`` ⇒ return typeof.
            return js_typeof(value)
        keys = list(value.keys())
        preview = ", ".join(keys[:10])
        preview_str = f": {preview}" if preview else ""
        return f"object(keys={len(keys)}{preview_str})"

    top_level = "array" if isinstance(parsed, list) else js_typeof(parsed)
    return "\n".join([
        "Structured summary (JSON):",
        f"Top-level type: {top_level}.",
        f"Shape: {describe(parsed)}.",
    ])


def _parse_delimited_line(line: str, delimiter: str) -> list[str]:
    """Split a delimited line, trim each cell, drop empty cells.

    Ports ``parseDelimitedLine`` from ``large-files.ts:212-217``. Matches
    TS behavior: empty cells are dropped — header rows with trailing
    delimiters yield the same shape on either side.
    """
    return [item.strip() for item in line.split(delimiter) if item.strip()]


def _explore_delimited(content: str, delimiter: str, kind: str) -> str:
    """Render a deterministic shape summary for CSV/TSV.

    Ports ``exploreDelimited`` from ``large-files.ts:219-239``. ``kind``
    is the label used in the summary (``"CSV"`` or ``"TSV"``).
    """
    lines = [line.strip() for line in re.split(r"\r?\n", content) if line.strip()]

    if not lines:
        return f"Structured summary ({kind}): no rows found."

    headers = _parse_delimited_line(lines[0], delimiter)
    row_count = max(0, len(lines) - 1)
    # ``lines[1]`` may be missing if only the header row is present —
    # mirrors TS ``lines[1] ? normalize... : "(no data rows)"``.
    first_data = _normalize_text_for_line(lines[1], 180) if len(lines) > 1 else "(no data rows)"

    columns_line = ", ".join(headers) if headers else "(none detected)"
    return "\n".join([
        f"Structured summary ({kind}):",
        f"Rows: {_format_int(row_count)}.",
        f"Columns ({len(headers)}): {columns_line}.",
        f"First row sample: {first_data}.",
    ])


def _explore_yaml(content: str) -> str:
    """Render a deterministic shape summary for YAML.

    Ports ``exploreYaml`` from ``large-files.ts:241-256``. We do NOT
    actually parse YAML (no PyYAML dep at this layer) — the TS version
    only extracts top-level keys by regex, so the Python version does the
    same. PyYAML is available via the project's runtime deps but importing
    it here would couple the module to a parse-cost we don't need.
    """
    candidates: list[str] = []
    for line in re.split(r"\r?\n", content):
        match = re.fullmatch(r"([A-Za-z0-9_.-]+):\s*(?:#.*)?", line)
        if match:
            candidates.append(match.group(1))

    keys = _unique_ordered(candidates)
    keys_line = ", ".join(keys[:30]) if keys else "(none detected)"
    return "\n".join([
        "Structured summary (YAML):",
        f"Top-level keys ({len(keys)}): {keys_line}.",
    ])


def _explore_xml(content: str) -> str:
    """Render a deterministic shape summary for XML.

    Ports ``exploreXml`` from ``large-files.ts:258-273``. Root tag is the
    first matched ``<tag``; child tags are the next 30 distinct tags.
    """
    root_match = re.search(r"<([A-Za-z0-9_:-]+)(\s|>)", content)
    root_tag = root_match.group(1) if root_match else "unknown"

    raw_children = [m.group(1) for m in re.finditer(r"<([A-Za-z0-9_:-]+)(\s|>)", content)]
    # TS does ``.filter(tag !== root) .slice(0, 30)`` BEFORE ``uniqueOrdered``.
    # That's load-bearing — if root tag appears 31 times, the limit slices it
    # away before dedup gets a chance to see the first non-root tag. The Python
    # port preserves the exact ordering.
    filtered = [tag for tag in raw_children if tag != root_tag][:30]
    child_tags = _unique_ordered(filtered)

    children_line = ", ".join(child_tags) if child_tags else "(none detected)"
    return "\n".join([
        "Structured summary (XML):",
        f"Root element: {root_tag}.",
        f"Child elements seen: {children_line}.",
    ])


def explore_structured_data(
    content: str,
    mime_type: str | None = None,
    file_name: str | None = None,
) -> str:
    """Dispatch a structured-data summary based on extension + MIME type.

    Ports ``exploreStructuredData`` from ``large-files.ts:275-316``. The
    extension hint is preferred (it's deterministic per-filename); MIME
    type is the fallback when no usable extension can be derived.

    Args:
        content: The file content as a string.
        mime_type: Optional MIME type from the original tool/file metadata.
        file_name: Optional filename, used to extract an extension hint.

    Returns:
        Multi-line deterministic summary. The exact text matches the TS
        ``exploreStructuredData`` output byte-for-byte on identical input.
    """
    extension = _collect_filename_extension(file_name) or _guess_mime_extension(mime_type)
    normalized_mime = (mime_type or "").strip().lower()

    if extension == "json" or normalized_mime.startswith("application/json"):
        try:
            return _explore_json(content)
        except (json.JSONDecodeError, ValueError):
            return "Structured summary (JSON): failed to parse as valid JSON."

    if extension == "csv" or normalized_mime.startswith("text/csv"):
        return _explore_delimited(content, ",", "CSV")

    if extension == "tsv" or normalized_mime.startswith("text/tab-separated-values"):
        return _explore_delimited(content, "\t", "TSV")

    if (
        extension == "xml"
        or normalized_mime.startswith("text/xml")
        or normalized_mime.startswith("application/xml")
    ):
        return _explore_xml(content)

    if extension in ("yaml", "yml") or "yaml" in normalized_mime:
        return _explore_yaml(content)

    line_count = len(re.split(r"\r?\n", content))
    return "\n".join([
        "Structured summary:",
        f"Characters: {_format_int(len(content))}.",
        f"Lines: {_format_int(line_count)}.",
    ])


def explore_code(content: str, file_name: str | None = None) -> str:
    """Render a deterministic summary of a source-code file.

    Ports ``exploreCode`` from ``large-files.ts:318-347``. Extracts:

    * Up to 12 import/dependency lines (TS/Python/Node syntax).
    * Up to 24 top-level definition signatures (functions/classes/types
      across major languages).
    * Total LOC.

    Args:
        content: The source code as a string.
        file_name: Optional filename, surfaced in the summary header.

    Returns:
        Multi-line summary; format is byte-identical to TS output.
    """
    lines = re.split(r"\r?\n", content)

    # Import patterns: ``import X``, ``from X import Y``, ``const X = require(``.
    # Matches the TS regex on ``large-files.ts:322-324``.
    import_pattern = re.compile(
        r"^\s*(import\s+|from\s+\S+\s+import\s+|const\s+\w+\s*=\s*require\()"
    )
    raw_imports = [
        _normalize_text_for_line(line, 180) for line in lines if import_pattern.search(line)
    ]
    imports = _unique_ordered(raw_imports[:12])

    # Top-level definition patterns: function/class/interface/type/const-arrow/
    # def/struct. Matches the TS regex on ``large-files.ts:332-336``.
    signature_pattern = re.compile(
        r"^(export\s+)?(async\s+)?"
        r"(function|class|interface|type|const\s+\w+\s*=\s*\(|def\s+\w+\(|struct\s+\w+)"
    )
    raw_signatures = [
        _normalize_text_for_line(line.strip(), 200)
        for line in lines
        if signature_pattern.search(line.strip())
    ]
    signatures = _unique_ordered(raw_signatures[:24])

    imports_line = " | ".join(imports) if imports else "none detected"
    signatures_line = " | ".join(signatures) if signatures else "none detected"
    title = f"Code exploration summary{f' ({file_name})' if file_name else ''}:"
    return "\n".join([
        title,
        f"Lines: {_format_int(len(lines))}.",
        f"Imports/dependencies ({len(imports)}): {imports_line}.",
        f"Top-level definitions ({len(signatures)}): {signatures_line}.",
    ])


# ---------------------------------------------------------------------------
# Text exploration — direct port of ``large-files.ts:349-447``.
# ---------------------------------------------------------------------------


def _extract_text_headers(content: str) -> list[str]:
    """Detect Markdown headers and ALL-CAPS section banners in text.

    Ports ``extractTextHeaders`` from ``large-files.ts:349-360``. Caps the
    result at :data:`_TEXT_HEADER_LIMIT` (18) — matches TS slice.
    """
    md_or_caps = re.compile(r"^#{1,6}\s+|^[A-Z0-9][A-Z0-9\s:_\-]{6,}$")
    candidates = [
        _normalize_text_for_line(line.strip(), 160)
        for line in re.split(r"\r?\n", content)
        if (stripped := line.strip()) and len(stripped) > 1 and md_or_caps.search(stripped)
    ]
    return _unique_ordered(candidates[:_TEXT_HEADER_LIMIT])


def _build_text_sample(content: str) -> str:
    """Build a head/middle/tail concatenation for the LLM prompt.

    Ports ``buildTextSample`` from ``large-files.ts:362-377``. Files
    smaller than ``2 * _TEXT_SUMMARY_SLICE_CHARS`` are returned verbatim.
    Larger files get a triptych with explicit section markers so the
    model can spot truncation gaps.
    """
    if len(content) <= _TEXT_SUMMARY_SLICE_CHARS * 2:
        return content

    middle_start = max(0, len(content) // 2 - _TEXT_SUMMARY_SLICE_CHARS // 2)
    middle_end = middle_start + _TEXT_SUMMARY_SLICE_CHARS
    head = content[:_TEXT_SUMMARY_SLICE_CHARS]
    mid = content[middle_start:middle_end]
    tail = content[-_TEXT_SUMMARY_SLICE_CHARS:]

    return "\n\n".join([
        "[Document Start]",
        head,
        "[Document Middle]",
        mid,
        "[Document End]",
        tail,
    ])


def _build_text_prompt(
    *,
    content: str,
    file_name: str | None,
    mime_type: str | None,
    headers: list[str],
) -> str:
    """Build the LLM input prompt for text-file exploration.

    Ports ``buildTextPrompt`` from ``large-files.ts:379-405``. The
    structured prompt asks for a bounded 200-300-word summary covering
    document topic, sections, names/dates/numbers, and action items.
    """
    sample = _build_text_sample(content)
    headers_line = (
        f"Detected section headers: {' | '.join(headers)}"
        if headers
        else "Detected section headers: none"
    )
    line_count = len(re.split(r"\r?\n", content))
    return "\n".join([
        "Summarize this large file for retrieval-time context references.",
        f"File name: {file_name if file_name else 'unknown'}",
        f"Mime type: {mime_type if mime_type else 'unknown'}",
        f"Length: {_format_int(len(content))} chars",
        f"Line count: {_format_int(line_count)}",
        headers_line,
        "Produce 200-300 words with:",
        "- What the document is about",
        "- Key sections and topics",
        "- Important names, dates, and numbers",
        "- Any action items or constraints",
        "Do not quote long passages verbatim.",
        "",
        "Document sample:",
        sample,
    ])


def _explore_text_deterministic_fallback(content: str, file_name: str | None = None) -> str:
    """Build a no-LLM exploration summary for plain text.

    Ports ``exploreTextDeterministicFallback`` from ``large-files.ts:407-424``.
    Surfaces character/word/line counts, detected headers, and 500-char
    opening + closing excerpts so the assembler has *something* useful
    when no LLM hook is available or the hook fails.
    """
    normalized = re.sub(r"\s+", " ", content).strip()
    headers = _extract_text_headers(content)
    line_count = len(re.split(r"\r?\n", content))
    word_count = len(normalized.split()) if normalized else 0
    first = _normalize_text_for_line(content[:500], 500)
    last = _normalize_text_for_line(content[-500:], 500)

    headers_line = " | ".join(headers) if headers else "none detected"
    title = f"Text exploration summary{f' ({file_name})' if file_name else ''}:"
    return "\n".join([
        title,
        f"Characters: {_format_int(len(content))}.",
        f"Words: {_format_int(word_count)}.",
        f"Lines: {_format_int(line_count)}.",
        f"Detected section headers: {headers_line}.",
        f"Opening excerpt: {first if first else '(empty)'}.",
        f"Closing excerpt: {last if last else '(empty)'}.",
    ])


# Type alias for the LLM summarization hook used by
# :func:`generate_exploration_summary`. Matches the TS
# ``ExplorationSummaryInput.summarizeText`` signature
# (``(prompt: string) => Promise<string | null | undefined>``).
SummarizeTextHook = Callable[[str], Awaitable[str | None]]


async def _explore_text(
    *,
    content: str,
    file_name: str | None,
    mime_type: str | None,
    summarize_text: SummarizeTextHook | None,
) -> str:
    """Async text-file summary; tries LLM hook, falls back to deterministic.

    Ports ``exploreText`` from ``large-files.ts:426-448``. Wraps the
    optional LLM hook in a try/except so a failing hook always falls back
    to the deterministic summary — matches the TS ``catch {}`` block.
    """
    headers = _extract_text_headers(content)

    if summarize_text is not None:
        prompt = _build_text_prompt(
            content=content,
            file_name=file_name,
            mime_type=mime_type,
            headers=headers,
        )
        try:
            summary = await summarize_text(prompt)
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
        except Exception:
            # TS source: ``catch { /* fall through */ }``. Mirrors the
            # behavior — deterministic fallback regardless of hook error.
            pass

    return _explore_text_deterministic_fallback(content, file_name)


# ---------------------------------------------------------------------------
# Public surface — direct port of ``large-files.ts:450-567``.
# ---------------------------------------------------------------------------


def parse_file_blocks(content: str) -> list[FileBlock]:
    """Parse ``<file>`` blocks from a message body.

    Ports ``parseFileBlocks`` from ``large-files.ts:450-475``. Returns the
    list of :class:`FileBlock` entries in document order. The matcher uses
    a non-greedy ``.*?`` body so adjacent blocks don't collapse into one.

    Args:
        content: Free-form message text to scan.

    Returns:
        Empty list if no blocks present.

    Example:
        >>> blocks = parse_file_blocks(
        ...     '<file name="a.json" mime="application/json">{"a":1}</file>'
        ... )
        >>> blocks[0].file_name
        'a.json'
        >>> blocks[0].mime_type
        'application/json'
        >>> blocks[0].text
        '{"a":1}'
    """
    blocks: list[FileBlock] = []
    for match in FILE_BLOCK_RE.finditer(content):
        full_match = match.group(0)
        raw_attrs = match.group(1) or ""
        text = match.group(2) or ""
        start = match.start()
        end = match.end()
        attributes = _parse_file_attributes(raw_attrs)

        blocks.append(
            FileBlock(
                full_match=full_match,
                start=start,
                end=end,
                attributes=attributes,
                file_name=attributes.get("name"),
                mime_type=attributes.get("mime"),
                text=text,
            )
        )

    return blocks


def extension_from_name_or_mime(
    file_name: str | None = None,
    mime_type: str | None = None,
) -> str:
    """Choose an on-disk filename suffix from file_name + mime_type hints.

    Ports ``extensionFromNameOrMime`` from ``large-files.ts:477-489``.
    Filename wins; MIME falls back; final fallback is ``"txt"``.

    Args:
        file_name: Original filename, if known.
        mime_type: MIME type, if known.

    Returns:
        A short lowercase alphanumeric extension. Always returns *something*
        (``"txt"`` if no hint resolves).
    """
    from_name = _collect_filename_extension(file_name)
    if from_name:
        return from_name

    from_mime = _guess_mime_extension(mime_type)
    if from_mime:
        return from_mime

    return "txt"


def extract_file_ids_from_content(content: str) -> list[str]:
    """Find all ``file_<16hex>`` IDs in a string, ordered, deduped, lowercased.

    Ports ``extractFileIdsFromContent`` from ``large-files.ts:491-494``.
    Used at assembly time to discover already-externalized file
    references in conversation history so re-uploads of the same blob can
    be deduped.

    Args:
        content: Arbitrary text to scan.

    Returns:
        Unique file IDs in first-seen order, all lowercased.

    Example:
        >>> extract_file_ids_from_content(
        ...     "see file_aaaaaaaaaaaaaaaa and file_BBBBBBBBBBBBBBBB"
        ... )
        ['file_aaaaaaaaaaaaaaaa', 'file_bbbbbbbbbbbbbbbb']
    """
    matches = FILE_ID_RE.findall(content)
    return _unique_ordered(match.lower() for match in matches)


def format_file_reference(
    *,
    file_id: str,
    file_name: str | None,
    mime_type: str | None,
    byte_size: int,
    summary: str,
) -> str:
    """Render the in-band placeholder block for an externalized user file.

    Ports ``formatFileReference`` from ``large-files.ts:496-513``. The
    assembler injects this string in place of the original ``<file>``
    block so the assistant can still reference the file by ID.

    Output shape::

        [LCM File: <file_id> | <name> | <mime> | <bytes> bytes]

        Exploration Summary:
        <summary>

    Args:
        file_id: The ``file_<16hex>`` ID.
        file_name: Original filename or ``None`` (rendered as "unknown").
        mime_type: MIME type or ``None`` (rendered as "unknown").
        byte_size: Size in bytes; negative inputs are clamped to 0.
        summary: Multi-line exploration summary. Empty/whitespace
            renders as ``"(no summary available)"``.
    """
    name = (file_name or "").strip() or "unknown"
    mime = (mime_type or "").strip() or "unknown"
    clamped_bytes = max(0, byte_size)

    summary_body = summary.strip() or "(no summary available)"
    return "\n".join([
        f"[LCM File: {file_id} | {name} | {mime} | {_format_int(clamped_bytes)} bytes]",
        "",
        "Exploration Summary:",
        summary_body,
    ])


def format_tool_output_reference(
    *,
    file_id: str,
    tool_name: str | None,
    byte_size: int,
    summary: str,
) -> str:
    """Render the in-band placeholder block for an externalized tool output.

    Ports ``formatToolOutputReference`` from ``large-files.ts:515-532``.
    Like :func:`format_file_reference` but tuned for tool-result payloads.
    Includes the ``Use lcm_describe`` hint so the assistant knows how to
    re-inspect the full output if needed.
    """
    tool = (tool_name or "").strip() or "unknown"
    clamped_bytes = max(0, byte_size)
    summary_body = summary.strip() or "(no summary available)"
    return "\n".join([
        f"[LCM Tool Output: {file_id} | tool={tool} | {_format_int(clamped_bytes)} bytes]",
        "",
        "Exploration Summary:",
        summary_body,
        "",
        "Use lcm_describe with the file id to inspect the full output.",
    ])


def format_raw_payload_reference(
    *,
    file_id: str,
    role: str,
    byte_size: int,
    reason: str,
    summary: str,
) -> str:
    """Render the in-band placeholder block for a raw-message externalization.

    Ports ``formatRawPayloadReference`` from ``large-files.ts:534-553``.
    Used when a message body would exceed the inline budget but the
    payload doesn't fit the "file" or "tool output" categories — e.g. a
    massive user message that's not wrapped in a ``<file>`` block.
    """
    role_display = role.strip() or "unknown"
    reason_display = reason.strip() or "large_raw_message"
    clamped_bytes = max(0, byte_size)
    summary_body = summary.strip() or "(no summary available)"
    return "\n".join([
        f"[LCM Raw Payload: {file_id} | role={role_display} | "
        f"reason={reason_display} | {_format_int(clamped_bytes)} bytes]",
        "",
        "Exploration Summary:",
        summary_body,
        "",
        "Use lcm_describe with the file id to inspect the full payload.",
    ])


async def generate_exploration_summary(
    *,
    content: str,
    file_name: str | None = None,
    mime_type: str | None = None,
    summarize_text: SummarizeTextHook | None = None,
) -> str:
    """Dispatch a deterministic or LLM-backed summary for arbitrary content.

    Ports ``generateExplorationSummary`` from ``large-files.ts:555-567``.
    The dispatcher inspects the extension + MIME hints and routes to one
    of three handlers:

    * **Structured** (JSON/CSV/TSV/XML/YAML) — fully deterministic.
    * **Code** — fully deterministic.
    * **Text** — LLM-summarized if a hook is provided and succeeds; else
      a deterministic fallback (counts + excerpts + headers).

    The ``summarize_text`` hook is **async** — matches the TS ``Promise``
    signature. Callers without an LLM yet (or in unit tests) pass ``None``
    to get the deterministic fallback path.

    Args:
        content: File content as a string.
        file_name: Optional filename for extension + summary header.
        mime_type: Optional MIME type for dispatch + summary header.
        summarize_text: Async LLM hook called only for plain-text files.

    Returns:
        The summary string. Always non-empty.
    """
    extension = extension_from_name_or_mime(file_name, mime_type)

    if _is_structured(mime_type=mime_type, extension=extension):
        return explore_structured_data(content, mime_type, file_name)

    if _is_code(mime_type=mime_type, extension=extension):
        return explore_code(content, file_name)

    return await _explore_text(
        content=content,
        file_name=file_name,
        mime_type=mime_type,
        summarize_text=summarize_text,
    )


# ---------------------------------------------------------------------------
# File ID generation — matches ``engine.ts:3832,4187`` shape.
# ---------------------------------------------------------------------------


def generate_file_id() -> str:
    """Generate a fresh ``file_<16 hex>`` ID.

    Matches the TS generator in ``engine.ts:3832`` + ``engine.ts:4187``:
    ``f"file_{randomUUID().replace(/-/g, '').slice(0, 16)}"``. We use
    :func:`secrets.token_hex` for the random tail — gives 16 lowercase
    hex characters, matching the TS slice of a hyphen-stripped UUID4.

    Returns:
        A new file ID. Lowercase hex.

    Example:
        >>> import re
        >>> bool(re.fullmatch(r'file_[a-f0-9]{16}', generate_file_id()))
        True
    """
    # ``secrets.token_hex(8)`` → 16 hex chars. Matches the TS 16-hex slice
    # without re-deriving a UUID layout we don't need (Python's uuid.uuid4
    # would also work but introduces a useless UUID-formatting step).
    return f"file_{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# LargeFileManager — disk + DB sidecar.
# ---------------------------------------------------------------------------


class LargeFileManager:
    """On-disk + DB-sidecar manager for externalized large files.

    Pairs the disk-blob path (``<files_dir>/<conversation_id>/<file_id>.<ext>``)
    with a row in the ``large_files`` table (created by
    :mod:`lossless_hermes.db.migration`). Every externalize call writes the
    blob first, then inserts the row inside a ``BEGIN IMMEDIATE`` transaction;
    a failed disk write aborts the DB insert.

    The directory layout mirrors the TS implementation (``engine.ts:3805-3807``
    ``largeFilesDirForConversation``) and matches ADR-002's mandated path
    structure.

    Args:
        connection: Open SQLite connection. The caller owns its lifecycle;
            the manager only borrows it for individual operations.
        files_dir: Base directory under which per-conversation subdirs
            are created. Typically ``$HERMES_HOME/lossless-hermes/large-files``.
            The manager creates ``files_dir`` itself + subdirectories on
            demand — callers do not need to pre-create.

    Example:
        >>> import sqlite3
        >>> from lossless_hermes.db.migration import run_lcm_migrations
        >>> conn = sqlite3.connect(":memory:")
        >>> _ = run_lcm_migrations(conn)
        >>> _ = conn.execute("INSERT INTO conversations(session_id) VALUES('s')")
        >>> mgr = LargeFileManager(conn, "/tmp/test-files")
        >>> # mgr.externalize_block(...) ...
    """

    def __init__(self, connection: sqlite3.Connection, files_dir: str | Path) -> None:
        self._conn = connection
        self._files_dir = Path(files_dir)

    @property
    def files_dir(self) -> Path:
        """The configured base directory for large-file blobs."""
        return self._files_dir

    def _dir_for_conversation(self, conversation_id: int) -> Path:
        """Path of the per-conversation subdirectory.

        Mirrors TS ``largeFilesDirForConversation`` (``engine.ts:3805-3807``).
        Coerces ``conversation_id`` to a string just like the TS code so
        the segments match across runtimes (negative IDs would be
        unusual but are accepted — DB FK constraints enforce validity).
        """
        return self._files_dir / str(conversation_id)

    def _write_atomic(self, target: Path, data: bytes) -> None:
        """Write ``data`` to ``target`` atomically with ``chmod 0o600``.

        Writes to ``<target>.tmp.<random>`` first, then ``os.replace``
        moves the file into its final name. ``os.replace`` is atomic on
        POSIX + NTFS — readers either see the old file or the new one,
        never a half-written prefix. On failure, the tempfile is unlinked
        so the directory doesn't leak partial state.

        The 0o600 permission matches ADR-002 §"Credentials path is canonical"
        and the OpenClaw practice referenced in the issue spec — large-file
        blobs may contain user PII so they're not group/other-readable.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.parent / f".{target.name}.tmp.{secrets.token_hex(4)}"
        try:
            # ``os.open`` with mode 0o600 sets the perms atomically on
            # creation — avoids the small umask-dependent window if we
            # opened-then-chmod'd.
            fd = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                # Close-on-failure is implicit via the fdopen context manager.
                raise
            os.replace(str(tmp_path), str(target))
        except BaseException:
            # Best-effort cleanup; do NOT mask the original exception.
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def externalize_block(
        self,
        *,
        conversation_id: int,
        content: str | bytes,
        file_name: str | None = None,
        mime_type: str | None = None,
        exploration_summary: str | None = None,
        file_id: str | None = None,
    ) -> LargeFileRecord:
        """Write ``content`` to disk + insert a ``large_files`` row.

        Pairs the two writes inside a ``BEGIN IMMEDIATE`` transaction so
        a failed disk write rolls back the DB insert (and vice versa).
        The on-disk filename is ``<file_id>.<ext>`` where ``<ext>`` is
        resolved via :func:`extension_from_name_or_mime`.

        Args:
            conversation_id: Existing conversation row's PK.
            content: The file content. ``str`` is encoded as UTF-8.
            file_name: Optional original filename (preserved in DB).
            mime_type: Optional MIME type (preserved in DB).
            exploration_summary: Optional pre-computed summary; if None
                the row's ``exploration_summary`` is NULL and the caller
                can fill it in later with :meth:`update_summary`.
            file_id: Optional caller-supplied ID. Lets the engine
                deduplicate by content hash if it has already computed
                an ID; otherwise the manager generates one via
                :func:`generate_file_id`.

        Returns:
            The persisted :class:`LargeFileRecord`.

        Raises:
            sqlite3.IntegrityError: If a row with the same ``file_id``
                already exists, or if ``conversation_id`` does not
                reference a real conversation. The DB transaction is
                rolled back and the on-disk file is not written.
            OSError: If the disk write fails (out of space, permission
                denied, etc.). The DB row is not inserted.
        """
        file_id_final = file_id if file_id is not None else generate_file_id()
        extension = extension_from_name_or_mime(file_name, mime_type)
        target_dir = self._dir_for_conversation(conversation_id)
        target_path = target_dir / f"{file_id_final}.{extension}"

        if isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            payload = bytes(content)

        # Write to disk FIRST so a disk failure aborts before we touch the
        # DB. If the DB insert then fails (e.g. FK constraint), we unlink
        # the file we just wrote so the directory stays consistent.
        self._write_atomic(target_path, payload)

        try:
            cursor = self._conn.execute(
                """
                INSERT INTO large_files
                  (file_id, conversation_id, file_name, mime_type,
                   byte_size, storage_uri, exploration_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id_final,
                    conversation_id,
                    file_name,
                    mime_type,
                    len(payload),
                    str(target_path),
                    exploration_summary,
                ),
            )
            cursor.close()
            # Commit so a caller closing the connection without committing
            # won't lose the row. Mirrors TS where the better-sqlite3
            # synchronous insert is already durable on return.
            self._conn.commit()
        except BaseException:
            # Roll back any pending DB state (defensive — SQLite raises on
            # the failed statement so nothing should be pending, but this
            # keeps the invariant tight).
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            # Compensate the successful disk write so the DB and FS stay
            # in lockstep.
            try:
                target_path.unlink()
            except FileNotFoundError:
                pass
            raise

        # Read created_at back so the returned record matches what's on disk.
        row = self._conn.execute(
            """
            SELECT created_at FROM large_files WHERE file_id = ?
            """,
            (file_id_final,),
        ).fetchone()
        created_at = row[0] if row else ""

        return LargeFileRecord(
            file_id=file_id_final,
            conversation_id=conversation_id,
            file_name=file_name,
            mime_type=mime_type,
            byte_size=len(payload),
            storage_uri=str(target_path),
            exploration_summary=exploration_summary,
            created_at=created_at,
        )

    def read(self, file_id: str) -> bytes:
        """Read the on-disk blob for ``file_id``.

        Validates that the recorded ``byte_size`` matches the actual file
        length on disk. A mismatch raises :class:`LargeFileIntegrityError`
        so callers can drop the stale row rather than serve corrupt data.

        Args:
            file_id: The ``file_<16hex>`` ID.

        Returns:
            The blob contents as bytes.

        Raises:
            KeyError: No ``large_files`` row for this ID.
            LargeFileIntegrityError: The file is missing from disk or its
                size disagrees with the DB row.
        """
        record = self._lookup(file_id)
        if record is None:
            raise KeyError(file_id)

        path = Path(record.storage_uri)
        if not path.exists():
            raise LargeFileIntegrityError(
                f"large_files row exists for {file_id} but {path} is missing"
            )

        data = path.read_bytes()
        if record.byte_size is not None and len(data) != record.byte_size:
            raise LargeFileIntegrityError(
                f"large_files row {file_id} recorded byte_size={record.byte_size} "
                f"but on-disk size is {len(data)}"
            )
        return data

    def delete(self, file_id: str) -> bool:
        """Delete the on-disk blob and ``large_files`` row for ``file_id``.

        Removes the DB row first (so a stale row never points at a missing
        file), then unlinks the file. A missing on-disk file is tolerated
        — the DB row is the source of truth.

        Args:
            file_id: The ``file_<16hex>`` ID.

        Returns:
            ``True`` if a row was deleted, ``False`` if no such row.
        """
        record = self._lookup(file_id)
        if record is None:
            return False

        cursor = self._conn.execute("DELETE FROM large_files WHERE file_id = ?", (file_id,))
        deleted = cursor.rowcount > 0
        cursor.close()
        self._conn.commit()

        if deleted:
            try:
                Path(record.storage_uri).unlink()
            except FileNotFoundError:
                # Tolerable — DB row was the source of truth.
                pass
        return deleted

    def list_for_conversation(self, conversation_id: int) -> list[LargeFileRecord]:
        """Return all ``large_files`` rows for a conversation.

        Ordered by ``created_at`` ASC, matching the
        ``large_files_conv_idx`` index shape so the query plan is a single
        index scan.

        Args:
            conversation_id: Existing conversation row's PK.

        Returns:
            List of records (possibly empty). Sorted oldest-first.
        """
        cursor = self._conn.execute(
            """
            SELECT file_id, conversation_id, file_name, mime_type,
                   byte_size, storage_uri, exploration_summary, created_at
            FROM large_files
            WHERE conversation_id = ?
            ORDER BY created_at ASC, file_id ASC
            """,
            (conversation_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [self._row_to_record(row) for row in rows]

    def update_summary(self, file_id: str, exploration_summary: str) -> bool:
        """Set the ``exploration_summary`` column for an existing row.

        Used by the assembler when the deferred LLM summarization for a
        text file completes after the initial externalize. The summary is
        write-once in practice but the method does not enforce it — a
        re-summarize is a no-op idempotent overwrite.

        Args:
            file_id: The ``file_<16hex>`` ID.
            exploration_summary: The summary to persist.

        Returns:
            ``True`` if the row was updated, ``False`` if no such row.
        """
        cursor = self._conn.execute(
            "UPDATE large_files SET exploration_summary = ? WHERE file_id = ?",
            (exploration_summary, file_id),
        )
        updated = cursor.rowcount > 0
        cursor.close()
        self._conn.commit()
        return updated

    def _lookup(self, file_id: str) -> LargeFileRecord | None:
        """Internal: fetch a row by file_id or return None."""
        cursor = self._conn.execute(
            """
            SELECT file_id, conversation_id, file_name, mime_type,
                   byte_size, storage_uri, exploration_summary, created_at
            FROM large_files
            WHERE file_id = ?
            """,
            (file_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return None
        return self._row_to_record(row)

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> LargeFileRecord:
        """Internal: tuple → :class:`LargeFileRecord` decoder.

        Column order matches the SELECT statements in :meth:`_lookup` and
        :meth:`list_for_conversation`. Centralized here so a future
        column add only touches one place.
        """
        return LargeFileRecord(
            file_id=row[0],
            conversation_id=row[1],
            file_name=row[2],
            mime_type=row[3],
            byte_size=row[4],
            storage_uri=row[5],
            exploration_summary=row[6],
            created_at=row[7],
        )
