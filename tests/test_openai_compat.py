"""OpenAI-compatible adapter against a stub endpoint (offline)."""
import json

import httpx
import pytest

from member_services.config import ConfigError, Settings
from member_services.llm import LLMError, Message, ToolCall, ToolResult, ToolSpec, make_llm
from member_services.llm.openai_compat import OpenAICompatLLM

TOOL = ToolSpec("get_benefits", "Benefits.", {"type": "object", "properties": {}})


def stub(response: dict, status: int = 200):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(status, json=response)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_request_shape_and_tool_call_parsing():
    http, seen = stub({"model": "qwen3:14b", "choices": [{"finish_reason": "tool_calls", "message": {
        "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                       "function": {"name": "get_benefits", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 100}}})
    llm = OpenAICompatLLM("qwen3:14b", "http://x/v1", http=http)
    history = [Message("user", text="q"), Message("assistant", tool_calls=[ToolCall("c0", "get_benefits", {})]),
               Message("user", tool_results=[ToolResult("c0", '{"ok":true}')])]
    r = llm.complete("SYS", history, [TOOL])
    body = seen[0]
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "get_benefits"
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": '{"ok":true}'}
    assert body["tools"][0]["function"]["parameters"] == TOOL.input_schema
    assert r.stop_reason == "tool_use" and r.tool_calls == [ToolCall("c1", "get_benefits", {})]
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens) == (20, 100, 9)


def test_text_answer_and_invalid_tool_arguments():
    http, _ = stub({"choices": [{"finish_reason": "stop", "message": {"content": " A $40 copay. "}}]})
    assert OpenAICompatLLM("m", "http://x/v1", http=http).complete("S", [], []).text == "A $40 copay."
    http, _ = stub({"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "get_claim",
                                                                                      "arguments": "{bad"}}]}}]})
    r = OpenAICompatLLM("m", "http://x/v1", http=http).complete("S", [], [])
    assert r.tool_calls[0].arguments == {"_invalid_json": True}  # the gateway rejects unknown arguments


def test_http_error_becomes_llm_error():
    http, _ = stub({"error": "x"}, status=500)
    with pytest.raises(LLMError):
        OpenAICompatLLM("m", "http://x/v1", http=http).complete("S", [Message("user", text="SSN 1")], [])


def test_factory_requires_a_price_for_unknown_models(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.delenv("LLM_PRICES_JSON", raising=False)
    s = Settings(llm_provider="openai_compat", llm_model="qwen3:14b")
    with pytest.raises(ConfigError, match="LLM_PRICES_JSON"):
        make_llm(s)
    monkeypatch.setenv("LLM_PRICES_JSON", '{"qwen3:14b": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}')
    assert isinstance(make_llm(s), OpenAICompatLLM)
