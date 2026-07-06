"""Model clients — the Warden's reasoning source.

The engine depends only on the `Model` protocol (stream text + tool-use
requests). AnthropicModel is the real client; ScriptedModel is a deterministic
stand-in so the demo and tests exercise the whole loop, Cell, Graphify, and
streaming with no API key and no network. No model-fallback ladder (§3, rejected).
"""
from forge.model.base import Model, TextDelta, ToolUseRequest
from forge.model.scripted import ScriptedModel

__all__ = ["Model", "TextDelta", "ToolUseRequest", "ScriptedModel"]
