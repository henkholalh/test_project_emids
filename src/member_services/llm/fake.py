"""Deterministic offline model for tests and CI dry runs.

It replays a script of responses (or calls a function that decides the next response from the conversation), and
records every request so tests can assert exactly what would have been sent to a real model, for example that
no PHI is present.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .base import LLMResponse, Message, ToolSpec, Usage

Responder = Callable[[str, list[Message], list[ToolSpec]], LLMResponse]


@dataclass
class RecordedRequest:
    system: str
    messages: list[Message]
    tools: list[ToolSpec]


@dataclass
class FakeLLM:
    script: list[LLMResponse] | Responder = field(default_factory=list)
    model: str = "fake"
    requests: list[RecordedRequest] = field(default_factory=list)

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 1024) -> LLMResponse:
        self.requests.append(RecordedRequest(system, list(messages), list(tools)))
        if callable(self.script):
            return self.script(system, messages, tools)
        if not self.script:
            return text_response("I don't have a scripted answer for that.")
        return self.script.pop(0)


def text_response(text: str, usage: Usage | None = None) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn", usage=usage or Usage(), model="fake")
