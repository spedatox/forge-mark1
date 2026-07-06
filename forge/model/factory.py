"""Model factory — turns a ``provider:model`` ref into a Warden `Model`.

`build_model` is the single place model selection happens. Anthropic refs get the
native SDK client; every other provider gets the shared OpenAI-compatible adapter.

An optional fallback chain (FORGE_LLM_FALLBACK_CHAIN) mirrors Mark VI: refs tried
in order when *opening* a stream fails (auth / rate-limit / connection / 5xx).
It is OFF by default. The Forge's baseline discipline is 'one model, fail loud';
this is the one operator-configured, opt-in concession — provider redundancy for a
single operator who runs several providers, not an automatic per-turn escalation
ladder. Once tokens are flowing a turn is never restarted on another provider.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from forge.model.base import Model, TextDelta, ToolUseRequest
from forge.model.providers import OpenAICompatModel, parse_model_ref

logger = logging.getLogger("forge.model")


def _build_single(ref: str, settings: Any, max_tokens: int) -> Model:
    provider, model = parse_model_ref(ref)
    if provider == "anthropic":
        from forge.model.anthropic_model import AnthropicModel
        return AnthropicModel(model_id=model, api_key=settings.anthropic_api_key,
                              max_tokens=max_tokens)
    return OpenAICompatModel(provider, model, settings, max_tokens=max_tokens)


def build_model(ref: str, settings: Any, max_tokens: int = 4096) -> Model:
    """Build the model for `ref`, wrapping it in a fallback chain only if one is
    configured. Provider clients are constructed lazily, so an unused fallback
    entry with a missing key costs nothing until it is actually reached."""
    fallback = getattr(settings, "llm_fallback_chain", "") or ""
    extra = [r.strip() for r in fallback.split(",") if r.strip()]
    if not extra:
        return _build_single(ref, settings, max_tokens)
    chain: list[str] = []
    for r in [ref, *extra]:
        if r not in chain:
            chain.append(r)
    return _FallbackModel(chain, settings, max_tokens)


class _FallbackModel:
    """Tries each ref in order; the first that opens a stream wins the turn."""

    def __init__(self, chain: list[str], settings: Any, max_tokens: int) -> None:
        self._chain = chain
        self._settings = settings
        self._max_tokens = max_tokens
        self.model_id = chain[0]

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[TextDelta | ToolUseRequest]:
        last_exc: Exception | None = None
        for ref in self._chain:
            try:
                sub = _build_single(ref, self._settings, self._max_tokens)
            except Exception as e:  # noqa: BLE001 — e.g. missing key; try the next
                last_exc = e
                logger.warning("model_build_failed", extra={"ref": ref, "error": str(e)})
                continue
            gen = sub.stream(system=system, messages=messages, tools=tools, signal=signal)
            try:
                first = await gen.__anext__()      # forces the stream to open
            except StopAsyncIteration:
                return                              # opened, produced nothing — success
            except Exception as e:  # noqa: BLE001 — open failed; fall back
                last_exc = e
                logger.warning("model_open_failed", extra={"ref": ref, "error": str(e)})
                await gen.aclose()
                continue
            # Opened successfully — this ref owns the rest of the turn.
            yield first
            async for ev in gen:
                yield ev
            return
        raise last_exc or RuntimeError("no model in the fallback chain could be built")
