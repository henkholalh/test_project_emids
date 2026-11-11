"""Provider-neutral model interface.

The agent and the gateway only depend on these types. Each provider adapter translates them to and from its own
API, so swapping the model or provider is a configuration change, not a code change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """A tool as presented to the model. Descriptions are written by us, never copied from a vendor."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """One conversation message.

    ``raw`` holds the provider's own content blocks for assistant turns. Adapters send it back unchanged, which
    some providers require (for example to keep reasoning blocks valid across turns).
    """

    role: Literal["user", "assistant"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    raw: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: Usage
    model: str
    raw: list[dict[str, Any]] | None = None

    def as_message(self) -> Message:
        return Message(role="assistant", text=self.text or None, tool_calls=list(self.tool_calls), raw=self.raw)


class LLMError(RuntimeError):
    """A model call failed after the adapter's own retries. Safe to show to the rep in general terms."""


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 1024) -> LLMResponse: ...
