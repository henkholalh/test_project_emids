"""Model adapters. ``make_llm`` picks one from configuration."""
from __future__ import annotations

from ..config import ConfigError, Settings
from .base import LLMClient, LLMError, LLMResponse, Message, ToolCall, ToolResult, ToolSpec, Usage

__all__ = ["LLMClient", "LLMError", "LLMResponse", "Message", "ToolCall", "ToolResult", "ToolSpec", "Usage",
           "make_llm"]


def make_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "fake":
        from .fake import FakeLLM
        return FakeLLM()
    from .pricing import price_table
    if settings.llm_model not in price_table():
        # Budgets and the cost metric need a price; fail at startup, not on the first conversation.
        raise ConfigError(f"No price configured for model '{settings.llm_model}'. Set LLM_PRICES_JSON, e.g. "
                          f'\'{{"{settings.llm_model}": {{"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}}}\'')
    if settings.llm_provider == "openai_compat":
        from .openai_compat import OpenAICompatLLM
        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        return OpenAICompatLLM(model=settings.llm_model, base_url=settings.openai_base_url, api_key=key,
                               timeout_s=settings.llm_timeout_s)
    if settings.llm_provider == "anthropic":
        from .anthropic_adapter import AnthropicLLM
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        return AnthropicLLM(model=settings.llm_model, api_key=key, effort=settings.llm_effort,
                            timeout_s=settings.llm_timeout_s, refusal_fallback=settings.llm_refusal_fallback)
    raise ValueError(f"Unsupported LLM_PROVIDER '{settings.llm_provider}'")
