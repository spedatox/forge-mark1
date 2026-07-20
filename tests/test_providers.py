"""Tests for the multi-provider LLM engine (mirrors Mark VI's llm_client):
ref parsing, model selection, and the Anthropic↔chat-completions translation."""
import asyncio
import types

import pytest

from forge.model.base import TextDelta, ToolUseRequest
from forge.model.providers import (OpenAICompatModel, _to_openai_params,
                                    _translate_message, parse_model_ref)


# ── provider:model routing ───────────────────────────────────────────────────
def test_parse_model_ref():
    assert parse_model_ref("claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")
    assert parse_model_ref("openai:gpt-5.1") == ("openai", "gpt-5.1")
    assert parse_model_ref("gemini:gemini-2.5-flash") == ("gemini", "gemini-2.5-flash")
    # Ollama model tags contain a colon — only the first segment is the provider.
    assert parse_model_ref("ollama:llama3.1:8b") == ("ollama", "llama3.1:8b")
    # Unknown prefix is treated as a bare Anthropic model, not a provider.
    assert parse_model_ref("some-model:v2") == ("anthropic", "some-model:v2")


def test_factory_routes_by_ref():
    from forge.model.factory import build_model
    from forge.model.anthropic_model import AnthropicModel

    class S:
        anthropic_api_key = "sk-test"
        openai_api_key = "sk-openai"
        gemini_api_key = zai_api_key = deepseek_api_key = ""
        ollama_base_url = "http://localhost:11434/v1"
        llm_fallback_chain = ""

    assert isinstance(build_model("claude-sonnet-4-6", S()), AnthropicModel)
    assert isinstance(build_model("openai:gpt-5-mini", S()), OpenAICompatModel)


def test_openai_compat_requires_key():
    class S:
        openai_api_key = ""
        gemini_api_key = zai_api_key = deepseek_api_key = ""
        ollama_base_url = "http://localhost:11434/v1"
    with pytest.raises(RuntimeError):
        OpenAICompatModel("openai", "gpt-5-mini", S())


# ── Anthropic content-block → chat-completions translation ───────────────────
def test_translate_tool_use_and_result():
    assistant = {"role": "assistant", "content": [
        {"type": "text", "text": "let me run it"},
        {"type": "tool_use", "id": "t1", "name": "run_command", "input": {"command": "ls"}},
    ]}
    out = _translate_message(assistant)
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "let me run it"          # never null (GLM would reject null)
    assert out[0]["tool_calls"][0]["function"]["name"] == "run_command"

    user = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt", "is_error": False},
    ]}
    tout = _translate_message(user)
    assert tout[0]["role"] == "tool" and tout[0]["tool_call_id"] == "t1"
    assert tout[0]["content"] == "file.txt"


def test_to_openai_params_tools_and_tokens():
    params = _to_openai_params(
        "openai", "gpt-5-mini", "SYS",
        [{"role": "user", "content": "hi"}],
        [{"name": "echo", "description": "d", "input_schema": {"type": "object"}}],
        max_tokens=1000)
    assert params["messages"][0] == {"role": "system", "content": "SYS"}
    assert params["tools"][0]["type"] == "function"
    assert params["max_completion_tokens"] == 1000     # OpenAI uses max_completion_tokens


def test_deepseek_disables_thinking_when_tools_present():
    params = _to_openai_params(
        "deepseek", "deepseek-v4-pro", "", [{"role": "user", "content": "x"}],
        [{"name": "t", "input_schema": {}}], max_tokens=100)
    assert params["extra_body"] == {"thinking": {"type": "disabled"}}


# ── streaming translation against a fake OpenAI client ───────────────────────
def _chunk(content=None, tool=None, index=0):
    fn = None
    if tool:
        fn = types.SimpleNamespace(name=tool.get("name"), arguments=tool.get("arguments"))
    tcs = [types.SimpleNamespace(index=index, id=tool.get("id"), function=fn)] if tool else None
    delta = types.SimpleNamespace(content=content, tool_calls=tcs)
    choice = types.SimpleNamespace(delta=delta, finish_reason=None)
    return types.SimpleNamespace(choices=[choice])


def test_streaming_yields_text_then_tool_use(monkeypatch):
    chunks = [
        _chunk(content="Hello "),
        _chunk(content="world"),
        _chunk(tool={"id": "call_1", "name": "run_command", "arguments": '{"command":'}),
        _chunk(tool={"arguments": ' "ls"}'}),
    ]

    class FakeStream:
        def __aiter__(self):
            async def gen():
                for c in chunks:
                    yield c
            return gen()
        async def close(self): ...

    class FakeCompletions:
        async def create(self, **kwargs):
            assert kwargs.get("stream") is True
            return FakeStream()

    class FakeClient:
        chat = types.SimpleNamespace(completions=FakeCompletions())

    class S:
        openai_api_key = "sk"
        gemini_api_key = zai_api_key = deepseek_api_key = ""
        ollama_base_url = "x"

    model = OpenAICompatModel("openai", "gpt-5-mini", S())
    model._client = FakeClient()   # swap in the fake transport

    async def collect():
        events = []
        async for ev in model.stream(system="s", messages=[{"role": "user", "content": "hi"}],
                                     tools=[], signal=asyncio.Event()):
            events.append(ev)
        return events

    events = asyncio.run(collect())
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    tools = [e for e in events if isinstance(e, ToolUseRequest)]
    assert "".join(texts) == "Hello world"
    assert len(tools) == 1
    assert tools[0].name == "run_command"
    assert tools[0].input == {"command": "ls"}   # streamed JSON fragments reassembled


# ── gpt-5.6+ tool calls route to /v1/responses ───────────────────────────────
def test_responses_api_gate():
    from forge.model.providers import (_openai_tools_need_responses_api,
                                       _use_responses_api)

    # 5.6 and newer need it; the older gpt-5 generation does not.
    assert _openai_tools_need_responses_api("gpt-5.6-terra")
    assert _openai_tools_need_responses_api("gpt-5.6-luna")
    assert _openai_tools_need_responses_api("gpt-6.0")       # future releases too
    assert not _openai_tools_need_responses_api("gpt-5.1")
    assert not _openai_tools_need_responses_api("gpt-5-mini")
    assert not _openai_tools_need_responses_api("gpt-4o")

    tools = [{"name": "run_command", "input_schema": {"type": "object"}}]
    assert _use_responses_api("openai", "gpt-5.6-terra", tools)
    # Tool-free calls stay on chat-completions — that endpoint works fine there.
    assert not _use_responses_api("openai", "gpt-5.6-terra", [])
    # Only OpenAI: another provider's model must never be rerouted.
    assert not _use_responses_api("zai", "glm-4.6", tools)


def test_to_responses_params_shape():
    from forge.model.providers import _to_responses_params

    messages = [
        {"role": "user", "content": "list the files"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "run_command",
             "input": {"command": "ls"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "a.py"}]},
    ]
    tools = [{"name": "run_command", "description": "run", "input_schema": {"type": "object"}}]
    p = _to_responses_params("gpt-5.6-terra", "be terse", messages, tools, 4096)

    # System prompt rides `instructions`, not a message.
    assert p["instructions"] == "be terse"
    assert p["max_output_tokens"] == 4096          # not max_tokens
    # Tools are declared flat — no {"type":"function","function":{…}} envelope.
    assert p["tools"][0]["name"] == "run_command"
    assert "function" not in p["tools"][0]
    # Reasoning is native here; sending an effort override is what broke tools.
    assert "reasoning" not in p and "reasoning_effort" not in p

    kinds = [i.get("type") or i.get("role") for i in p["input"]]
    assert kinds == ["user", "function_call", "function_call_output"]
    call = p["input"][1]
    out = p["input"][2]
    # call_id is the pairing handle on both sides.
    assert call["call_id"] == out["call_id"] == "call_1"
    assert call["arguments"] == '{"command": "ls"}'
    assert out["output"] == "a.py"


def test_responses_stream_yields_text_then_tool_use():
    """The /v1/responses event shape must produce the same events as chat."""
    events = [
        types.SimpleNamespace(type="response.output_text.delta", delta="Look"),
        types.SimpleNamespace(type="response.output_text.delta", delta="ing"),
        types.SimpleNamespace(
            type="response.output_item.added", output_index=0,
            item=types.SimpleNamespace(type="function_call", call_id="call_9",
                                       name="run_command", arguments="")),
        types.SimpleNamespace(type="response.function_call_arguments.delta",
                              output_index=0, delta='{"command":'),
        types.SimpleNamespace(type="response.function_call_arguments.delta",
                              output_index=0, delta=' "ls -la"}'),
        types.SimpleNamespace(type="response.completed", response=None),
    ]

    class FakeStream:
        def __aiter__(self):
            async def gen():
                for e in events:
                    yield e
            return gen()
        async def close(self): ...

    class FakeResponses:
        def __init__(self): self.params = None
        async def create(self, **kwargs):
            assert kwargs.get("stream") is True
            self.params = kwargs
            return FakeStream()

    fake_responses = FakeResponses()

    class FakeClient:
        responses = fake_responses
        # Present but must stay untouched: routing the call here is the point.
        chat = types.SimpleNamespace(completions=types.SimpleNamespace(
            create=_unreachable))

    class S:
        openai_api_key = "sk"
        gemini_api_key = zai_api_key = deepseek_api_key = ""
        ollama_base_url = "x"

    model = OpenAICompatModel("openai", "gpt-5.6-terra", S())
    model._client = FakeClient()

    async def collect():
        out = []
        async for ev in model.stream(
                system="s", messages=[{"role": "user", "content": "hi"}],
                tools=[{"name": "run_command", "input_schema": {"type": "object"}}],
                signal=asyncio.Event()):
            out.append(ev)
        return out

    got = asyncio.run(collect())
    assert "".join(e.text for e in got if isinstance(e, TextDelta)) == "Looking"
    calls = [e for e in got if isinstance(e, ToolUseRequest)]
    assert len(calls) == 1
    assert calls[0].id == "call_9"          # call_id, not item.id
    assert calls[0].name == "run_command"
    assert calls[0].input == {"command": "ls -la"}
    assert fake_responses.params["model"] == "gpt-5.6-terra"


async def _unreachable(**_kwargs):          # pragma: no cover - guard only
    raise AssertionError("gpt-5.6 tool calls must not hit chat.completions")
