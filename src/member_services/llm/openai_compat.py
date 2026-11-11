"""Adapter for any OpenAI-compatible Chat Completions endpoint with tool calling.

Covers local open-weights models through Ollama (``OPENAI_BASE_URL=http://localhost:11434/v1``), vLLM, OpenAI and
other compatible gateways. It is a plain HTTP client (no vendor SDK), so it adds no dependency.

Status: covered by offline tests against a stub endpoint; not yet run against a live non-Claude model. Models
differ in how well they follow tool-use and safety instructions; run the evaluation gate before relying on one.
The gateway's controls do not depend on the model and apply unchanged.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage

_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


class OpenAICompatLLM:
    def __init__(self, model: str, base_url: str, api_key: str | None = None, timeout_s: float = 20.0,
                 http: httpx.Client | None = None):
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._http = http or httpx.Client(timeout=timeout_s)

    @staticmethod
    def build_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                         for c in m.tool_calls]
                out.append(msg)
            else:
                out += [{"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in m.tool_results]
                if m.text:
                    out.append({"role": "user", "content": m.text})
        return out

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 1024) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens,
                                "messages": self.build_messages(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in tools]
        try:
            resp = self._http.post(self._url, json=body, headers=self._headers)
        except httpx.TransportError as exc:
            raise LLMError(f"Model call failed ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            raise LLMError(f"Model call failed (HTTP {resp.status_code})")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_invalid_json": True}  # rejected by the gateway's argument check
            calls.append(ToolCall(id=c.get("id") or fn.get("name", "call"), name=fn.get("name", ""),
                                  arguments=args if isinstance(args, dict) else {}))
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        usage = Usage(input_tokens=max(0, (u.get("prompt_tokens") or 0) - cached),
                      output_tokens=u.get("completion_tokens") or 0, cache_read_tokens=cached)
        stop = "tool_use" if calls else _STOP.get(choice.get("finish_reason") or "", "other")
        return LLMResponse(text=(msg.get("content") or "").strip(), tool_calls=calls, stop_reason=stop, usage=usage,
                           model=data.get("model") or self.model)
