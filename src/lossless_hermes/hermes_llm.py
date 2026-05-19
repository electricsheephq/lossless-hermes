"""Hermes-backed :class:`~lossless_hermes.summarize.SummarizerDeps` shim.

This module provides :class:`HermesSummarizerDeps` — the concrete
implementation of the :class:`~lossless_hermes.summarize.SummarizerDeps`
Protocol (``summarize.py:885-941``) that wires LCM's summarizer cascade
to Hermes's auxiliary LLM client.

It is PR 1 of the 5-PR compaction-P0 sequence (issue #164). It is a
*pure addition* — no existing module's behavior changes. PRs 2-5 build
the engine wiring (summarizer surface, ``CompactionEngine``,
``compress()`` overflow branch, ADR-032 debt gate) on top of it.

Design — a thin shim, not a resolver
------------------------------------

``HermesSummarizerDeps`` defers *all* provider/model/auth resolution to
Hermes's ``agent.auxiliary_client.call_llm``. It does NOT mirror LCM's
5-layer credential resolution (the rejected ADR-022-style resolver):

* :meth:`complete` — calls ``call_llm``, which self-resolves provider,
  model, timeout, and credentials from the host ``config.yaml`` and the
  environment. It maps the OpenAI-shape response into the provider
  *envelope* the cascade expects (see "Envelope shape" below).
* :meth:`get_api_key` — returns ``None``. ``call_llm`` owns auth
  resolution; the plugin must not resolve an LLM key itself.
* :meth:`is_runtime_managed_auth_provider` — returns ``True``, so the
  cascade's ``skip_model_auth`` direct-key retry path is short-circuited
  (that path would need a key this shim deliberately does not provide).

The ``call_llm`` import is *lazy* (inside :meth:`complete`) — the symbol
only resolves inside a Hermes runtime. ``import lossless_hermes`` must
succeed in a Hermes-less environment (e.g. the test/CI matrix), matching
the deferred-import discipline used across the engine
(``engine/lifecycle.py``) and ``hermes-lcm/escalation.py``.

The ``task`` argument
---------------------

:meth:`complete` calls ``call_llm(task="lcm_summary", ...)``. Hermes's
``call_llm`` reads provider/model/timeout from ``auxiliary.<task>.*`` in
``config.yaml``. ``lcm_summary`` is a *new* auxiliary task, not one of
Hermes's built-in tasks.

This degrades gracefully out-of-the-box. ``call_llm``'s task-config
lookup (``_get_auxiliary_task_config``) returns ``{}`` for an unknown
task — it does NOT raise and does NOT special-case known task names.
With no task config, ``_resolve_task_provider_model`` falls through to
``provider="auto"``, so ``call_llm`` uses its full auto-detection chain;
``_get_task_timeout`` falls back to its 30s default. Summarization
therefore works on a default install with no operator config.

Operators MAY pin the summarizer model/provider/timeout by adding an
``auxiliary.lcm_summary`` entry to ``config.yaml``::

    auxiliary:
      lcm_summary:
        provider: anthropic
        model: claude-haiku-4-5
        timeout: 60

See :doc:`/docs/porting-guides/assembler-compaction.md` ("ADR: Summarizer
LLM client", Option 3) for the rationale.

Envelope shape — the load-bearing detail
-----------------------------------------

The cascade inspects :meth:`complete`'s return value at four points
(``summarize.py``): ``normalize_completion_summary`` (``:1128``) reads
``result["content"]``; ``extract_provider_auth_failure`` (``:1333``,
structural-signal mode) flags an HTTP 401 or ``error.kind ==
"provider_auth"``; ``extract_provider_response_failure`` (``:1447``)
flags an error ``finish_reason`` / status >= 400; and
``extract_incomplete_response_signals`` (``:1529``) flags
``status == "incomplete"``.

``normalize_completion_summary`` -> ``_collect_text_like_fields``
(``:1067``) only reads the ``text``/``output_text`` keys of *dicts* and
recurses *lists*. A bare top-level string is ignored. Therefore the
success envelope MUST shape ``content`` as a **list of content blocks**::

    {"content": [{"type": "text", "text": "<summary>"}]}

A bare ``{"content": "<summary>"}`` string would yield an empty
normalized summary every call, forcing the cascade down its
envelope-extraction -> conservative-retry -> deterministic-fallback path.

The success envelope also carries NO top-level ``status`` integer (it
would be read as an HTTP status by ``_extract_auth_failure_status_code``)
and NO error-set ``finish_reason``. This module sets a benign
``finish_reason: "stop"`` for clarity — harmless (not in the
``{error, failed, cancelled}`` error set).

On an exception from ``call_llm``, :meth:`complete` lets the exception
*propagate* — it does NOT catch it and synthesize an error envelope. The
cascade's caught-error path (``_attempt_summarizer_call``'s
``except Exception``, ``summarize.py:2467``) runs
``extract_provider_auth_failure(exc)`` and ``_coerce_to_dict(exc)``
(``:1291``), which synthesizes a richer dict view from the exception's
attributes (``status``, ``response``, ``code``, ...). Propagating is
strictly better than a hand-rolled error dict.

See:

* ``src/lossless_hermes/summarize.py:885-941`` — the ``SummarizerDeps``
  Protocol contract this class implements.
* ``docs/porting-guides/assembler-compaction.md`` — the summarizer
  porting guide (envelope shape, ``task`` ADR, ``reasoning`` risk #4).
* ``hermes-agent/agent/auxiliary_client.py:4088`` — ``call_llm``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# ---------------------------------------------------------------------------
# Reasoning-block stripping
# ---------------------------------------------------------------------------
#
# Thinking-capable models (DeepSeek R1, Qwen QwQ, GLM, MiniMax, etc.)
# emit inline <think>/<reasoning>/... blocks. If those land in a stored
# summary they pollute later context (the reasoning text often quotes
# the summarizer system prompt verbatim, which then confuses downstream
# query/expansion tools). The cascade ALSO drops blocks whose ``type``
# discriminator is reasoning-like (``summarize.py:_is_reasoning_like_type``),
# but a model can emit the reasoning *inline within a text block's
# string* rather than as a separate typed block — that case is invisible
# to the type-discriminator filter, so it must be stripped here, in the
# text, before the envelope is built.
#
# Reimplemented from understanding of the well-known inline-reasoning tag
# convention (NOT copied — ``hermes-lcm`` carries no LICENSE). The tag
# set mirrors the tags Hermes's own ``run_agent`` strips.

_REASONING_BLOCK_RE = re.compile(
    r"<(?P<tag>think|thinking|reasoning|thought|reasoning_scratchpad)\s*>"
    r".*?"
    r"</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_reasoning_blocks(text: str) -> str:
    """Remove inline ``<think>``/``<reasoning>``/... blocks from *text*.

    Idempotent and safe on text containing no tags. The fast-path bails
    when there is no ``<`` character at all, so the common no-tag case
    pays only a substring scan.
    """
    if not text or "<" not in text:
        return text
    return _REASONING_BLOCK_RE.sub("", text).strip()


def _coerce_content_to_str(content: Any) -> str:
    """Coerce a chat-completion ``message.content`` payload to a string.

    ``call_llm`` returns an OpenAI-shape response; ``choices[0].message
    .content`` is normally a ``str``, but a provider may return ``None``
    (e.g. a tool-only turn) or a list of content parts. This normalizes
    all of those to a plain string so the envelope's ``text`` field is
    always a ``str``.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


class HermesSummarizerDeps:
    """Concrete :class:`~lossless_hermes.summarize.SummarizerDeps`.

    Backs LCM's summarizer cascade with Hermes's
    ``agent.auxiliary_client.call_llm``. See the module docstring for the
    full design rationale (thin shim, no resolver; the ``task`` choice;
    the envelope shape).

    Stateless — a single instance is safe to share for the process
    lifetime. PR 2 constructs one in ``on_session_start`` and exposes it
    as ``engine.deps``.
    """

    #: Hermes auxiliary task name. ``call_llm`` reads
    #: ``auxiliary.lcm_summary.*`` from ``config.yaml``; an absent entry
    #: degrades gracefully to ``call_llm``'s auto-detection chain (see
    #: the module docstring, "The ``task`` argument").
    _TASK = "lcm_summary"

    #: Sampling temperature for summary calls. Matches the value
    #: ``hermes-lcm`` and Hermes's built-in compressor use — low, for
    #: stable/repeatable summaries.
    _TEMPERATURE = 0.3

    def complete(
        self,
        *,
        provider: str,
        model: str,
        api_key: str | None,
        system: str,
        user_prompt: str,
        max_tokens: int,
        reasoning: str | None = None,
        skip_model_auth: bool = False,
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        """Invoke the Hermes auxiliary LLM and return the provider envelope.

        Builds a two-message OpenAI-shape request (a ``system`` message
        + a ``user`` message), calls ``call_llm``, and maps the response
        into the cascade's expected envelope.

        Parameters mirror the
        :class:`~lossless_hermes.summarize.SummarizerDeps` Protocol:

        * ``provider`` / ``model`` — accepted for Protocol conformance
          but NOT forwarded. ``call_llm`` self-resolves provider/model
          from the ``auxiliary.lcm_summary`` config (see the module
          docstring). The cascade resolves these from LCM's candidate
          layers, but Hermes's auxiliary config is the source of truth.
        * ``api_key`` — forwarded to ``call_llm`` ONLY when truthy.
          :meth:`get_api_key` returns ``None``, so on the normal path
          this is ``None`` and is NOT passed — letting ``call_llm``
          auto-resolve credentials. Passing ``api_key=None`` explicitly
          would not defeat auto-detection, but omitting it is clearer.
        * ``reasoning`` — IGNORED for v0.2.0. ``call_llm`` has no
          ``reasoning`` parameter (porting guide remaining-risk #4); the
          cascade's conservative retry still retries with the same
          settings, which remains useful for transient overload.
        * ``skip_model_auth`` — IGNORED. :meth:`is_runtime_managed_auth_provider`
          returns ``True``, which short-circuits the cascade's
          ``skip_model_auth`` retry path before it ever reaches here.
        * ``timeout_ms`` — converted to seconds for ``call_llm``'s
          ``timeout`` parameter.

        Returns the success envelope ``{"content": [{"type": "text",
        "text": <summary>}], "finish_reason": "stop"}`` — a *block list*
        (mandatory; see the module docstring).

        Raises whatever ``call_llm`` raises — the exception is NOT caught
        here. The cascade's caught-error path synthesizes a richer
        diagnostic view from the exception.
        """
        # Lazy import — ``agent.auxiliary_client`` only resolves inside a
        # Hermes runtime. ``import lossless_hermes`` must succeed without
        # Hermes on the path (test/CI matrix). ``type: ignore`` because
        # Hermes is intentionally not a pip dependency (ADR-007); the
        # same suppression is used in ``hermes_bridge.py``.
        from agent.auxiliary_client import call_llm  # type: ignore[import-not-found]

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]

        call_kwargs: dict[str, Any] = {
            "task": self._TASK,
            "messages": messages,
            "temperature": self._TEMPERATURE,
            "max_tokens": max_tokens,
            "timeout": timeout_ms / 1000.0,
        }
        # Pass an explicit api_key ONLY when one was supplied. On the
        # normal path get_api_key() returns None, so this is omitted and
        # call_llm self-resolves credentials from config/env.
        if api_key:
            call_kwargs["api_key"] = api_key

        # Provider exceptions propagate — see the module docstring.
        response = call_llm(**call_kwargs)

        # OpenAI-shape response: .choices[0].message.content
        content = response.choices[0].message.content
        summary = _strip_reasoning_blocks(_coerce_content_to_str(content))

        # The cascade's normalize_completion_summary() only picks up
        # ``text`` keys of dicts inside a list — so ``content`` MUST be a
        # block list, never a bare string. ``finish_reason: "stop"`` is a
        # benign value (not in the {error, failed, cancelled} error set);
        # NO top-level ``status`` integer (would read as an HTTP status).
        return {
            "content": [{"type": "text", "text": summary}],
            "finish_reason": "stop",
        }

    def get_api_key(
        self,
        provider: str,
        model: str,
        *,
        skip_model_auth: bool = False,
    ) -> str | None:
        """Return ``None`` — Hermes's ``call_llm`` self-resolves auth.

        The plugin deliberately does NOT resolve an LLM credential
        itself (no ADR-022-style resolver). ``call_llm`` reads provider
        credentials from the host ``config.yaml`` and the environment.

        Returning ``None`` for *both* ``skip_model_auth`` values is
        intentional: the cascade treats a ``None`` direct key on the
        ``skip_model_auth=True`` path as "auth retry unavailable" and
        cleanly raises the original auth error rather than attempting a
        bypass — the correct behavior when LCM defers all auth to Hermes.
        """
        del provider, model, skip_model_auth  # resolution deferred to call_llm
        return None

    def is_runtime_managed_auth_provider(self, provider: str) -> bool:
        """Return ``True`` — Hermes manages provider auth.

        With ``True``, the cascade's ``skip_model_auth`` retry path
        (``_retry_without_model_auth``) short-circuits immediately and
        raises the initial auth error without attempting a direct-key
        bypass. That bypass would need a credential :meth:`get_api_key`
        deliberately does not provide; "runtime-managed" is the accurate
        description since ``call_llm`` owns auth resolution.
        """
        del provider  # every provider is runtime-managed under call_llm
        return True
