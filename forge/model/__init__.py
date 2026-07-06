"""Model clients — the Warden's reasoning source.

The engine depends only on the `Model` protocol (stream text + tool-use
requests). Real clients are selected from a ``provider:model`` ref by
`build_model` (Anthropic native SDK, or the shared OpenAI-compatible adapter for
OpenAI / Gemini / z.ai / DeepSeek / Ollama) — mirroring Mark VI's engine so the
two systems behave consistently. ScriptedModel is a deterministic stand-in so the
demo and tests exercise the whole loop, Cell, Graphify, and streaming with no API
key and no network. Provider fallback is opt-in and off by default (see factory).
"""
from forge.model.base import Model, TextDelta, ToolUseRequest
from forge.model.scripted import ScriptedModel
from forge.model.providers import OpenAICompatModel, parse_model_ref
from forge.model.factory import build_model

__all__ = ["Model", "TextDelta", "ToolUseRequest", "ScriptedModel",
           "OpenAICompatModel", "parse_model_ref", "build_model"]
