"""Model adapters. ``make_llm`` picks one from configuration."""
from __future__ import annotations

from ..config import Settings
from .base import LLMClient, LLMError, LLMResponse, Message, ToolCall, ToolResult, ToolSpec, Usage

__all__ = ["LLMClient", "LLMError", "LLMResponse", "Message", "ToolCall", "ToolResult", "ToolSpec", "Usage",
           "make_llm"]


def make_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "fake":
        from .fake import FakeLLM
        return FakeLLM()
    if settings.llm_provider == "anthropic":
        from .anthropic_adapter import AnthropicLLM
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        return AnthropicLLM(model=settings.llm_model, api_key=key, effort=settings.llm_effort,
                            timeout_s=settings.llm_timeout_s, refusal_fallback=settings.llm_refusal_fallback)
    raise ValueError(f"Unsupported LLM_PROVIDER '{settings.llm_provider}'")
