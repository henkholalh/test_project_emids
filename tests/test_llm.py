from types import SimpleNamespace

import anthropic
import httpx
import pytest

from member_services.config import Settings
from member_services.llm import LLMError, Message, ToolCall, ToolResult, ToolSpec, Usage, make_llm
from member_services.llm.anthropic_adapter import FALLBACK_BETA, AnthropicLLM
from member_services.llm.fake import FakeLLM, text_response
from member_services.llm.pricing import cost_usd

TOOL = ToolSpec("get_benefits", "Benefits for the member on this call.", {"type": "object", "properties": {}})


class Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


class StubAPI:
    def __init__(self, response=None, error=None):
        self.calls, self.response, self.error = [], response, error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def stub_client(response=None, error=None):
    api = StubAPI(response, error)
    return SimpleNamespace(messages=api, beta=SimpleNamespace(messages=api)), api


def sample_response():
    return SimpleNamespace(
        content=[Block(type="thinking", thinking="", signature="sig"),
                 Block(type="text", text="Checking benefits."),
                 Block(type="tool_use", id="tu_1", name="get_benefits", input={})],
        stop_reason="tool_use", model="claude-sonnet-5-5",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=3000,
                              cache_creation_input_tokens=0))


def test_request_marks_stable_prefix_for_caching_and_sets_effort():
    client, api = stub_client(sample_response())
    AnthropicLLM("claude-sonnet-5-5", client=client).complete("SYSTEM", [Message("user", text="hi")], [TOOL])
    req = api.calls[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"] == {"effort": "low"}
    assert req["betas"] == [FALLBACK_BETA] and req["fallbacks"] == "default"


def test_fallback_can_be_disabled_and_uses_standard_endpoint():
    client, api = stub_client(sample_response())
    AnthropicLLM("claude-sonnet-5-5", client=client, refusal_fallback=False).complete("S", [], [])
    assert "betas" not in api.calls[0] and "fallbacks" not in api.calls[0] and "tools" not in api.calls[0]


def test_response_parsing_keeps_raw_blocks_for_replay():
    client, _ = stub_client(sample_response())
    r = AnthropicLLM("claude-sonnet-5-5", client=client).complete("S", [Message("user", text="hi")], [TOOL])
    assert r.stop_reason == "tool_use"
    assert r.tool_calls == [ToolCall("tu_1", "get_benefits", {})]
    assert r.text == "Checking benefits."
    assert r.usage == Usage(1200, 80, 3000, 0)
    assert r.raw[0]["type"] == "thinking" and r.raw[0]["signature"] == "sig"


def test_assistant_raw_blocks_and_tool_results_round_trip():
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"},
           {"type": "tool_use", "id": "tu_1", "name": "get_benefits", "input": {}}]
    msgs = [Message("user", text="q"), Message("assistant", raw=raw),
            Message("user", tool_results=[ToolResult("tu_1", '{"ok": true}')])]
    out = AnthropicLLM._messages(msgs)
    assert out[1]["content"] == raw
    assert out[2]["content"] == [{"type": "tool_result", "tool_use_id": "tu_1", "content": '{"ok": true}',
                                  "is_error": False}]


def test_api_error_becomes_llm_error_without_request_content():
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client, _ = stub_client(error=err)
    with pytest.raises(LLMError) as exc:
        AnthropicLLM("claude-sonnet-5-5", client=client).complete("S", [Message("user", text="SSN 123")], [])
    assert "123" not in str(exc.value)


def test_cost_matches_price_table():
    # 1.2k input at $2/M + 80 output at $10/M + 3k cache read at $0.20/M
    assert cost_usd("claude-sonnet-5-5", Usage(1200, 80, 3000, 0)) == pytest.approx(0.0024 + 0.0008 + 0.0006)


def test_unknown_model_price_fails_loudly():
    with pytest.raises(KeyError, match="LLM_PRICES_JSON"):
        cost_usd("some-new-model", Usage(1, 1))


def test_fake_llm_replays_script_and_records_requests():
    fake = FakeLLM(script=[text_response("one")])
    assert fake.complete("S", [Message("user", text="hi")], [TOOL]).text == "one"
    assert fake.complete("S", [], []).stop_reason == "end_turn"
    assert len(fake.requests) == 2 and fake.requests[0].tools == [TOOL]


def test_factory_selects_provider(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert isinstance(make_llm(Settings()), FakeLLM)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    llm = make_llm(Settings())
    assert isinstance(llm, AnthropicLLM) and llm.model == "claude-sonnet-5-5"
