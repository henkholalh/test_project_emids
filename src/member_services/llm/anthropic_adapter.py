"""Anthropic Claude adapter (Messages API with tool use).

Design choices:
- The stable prefix (system prompt and tool definitions) is marked for prompt caching, so repeated conversations
  pay the cache-read price for it (NFR-7).
- ``effort`` is configurable and defaults to ``low``: this is a short, chat-style route where latency and cost
  matter more than long reasoning (NFR-3, NFR-7).
- Assistant content blocks are passed back exactly as received, which the API requires for reasoning blocks.
- Server-side refusal fallback is enabled by default, so a false-positive safety decline is retried on a fallback
  model inside the same call instead of failing the rep's turn. It can be switched off with
  ``LLM_REFUSAL_FALLBACK=false`` (for example on platforms that do not support it).
"""
from __future__ import annotations

from typing import Any

import anthropic

from .base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_STOP_MAP = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None = None, effort: str = "low", timeout_s: float = 20.0,
                 refusal_fallback: bool = True, client: Any | None = None):
        self.model = model
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        # The SDK resolves credentials from the environment when api_key is None.
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=2)

    # ---- request building --------------------------------------------------------
    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        out = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
        if out:
            out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant":
                if m.raw is not None:
                    content: list[dict[str, Any]] = list(m.raw)
                else:
                    content = ([{"type": "text", "text": m.text}] if m.text else []) + [
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
                out.append({"role": "assistant", "content": content})
            else:
                # All tool results for one assistant turn go back in a single user message.
                content = [{"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
                            "is_error": r.is_error} for r in m.tool_results]
                if m.text:
                    content.append({"type": "text", "text": m.text})
                out.append({"role": "user", "content": content})
        return out

    def build_request(self, system: str, messages: list[Message], tools: list[ToolSpec],
                      max_tokens: int) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._messages(messages),
            "output_config": {"effort": self.effort},
        }
        if tools:
            req["tools"] = self._tools(tools)
        if self.refusal_fallback:
            req["betas"] = [FALLBACK_BETA]
            req["fallbacks"] = "default"
        return req

    # ---- call ----------------------------------------------------------------------
    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 1024) -> LLMResponse:
        req = self.build_request(system, messages, tools, max_tokens)
        api = self._client.beta.messages if self.refusal_fallback else self._client.messages
        try:
            resp = api.create(**req)
        except anthropic.APIError as exc:
            # Never include request content in the error: it may hold member data.
            raise LLMError(f"Model call failed ({type(exc).__name__})") from None
        return self.parse_response(resp)

    @staticmethod
    def parse_response(resp: Any) -> LLMResponse:
        texts: list[str] = []
        calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []
        for block in resp.content:
            raw.append(block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block))
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(block.text)
            elif btype == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        u = resp.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        stop = _STOP_MAP.get(resp.stop_reason or "", "other")
        return LLMResponse(text="".join(texts).strip(), tool_calls=calls, stop_reason=stop, usage=usage,
                           model=getattr(resp, "model", "") or "", raw=raw)
