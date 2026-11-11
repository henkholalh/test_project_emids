"""Builds a fully wired gateway for one rep session from configuration."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import httpx

from ..clients.directory import DirectoryAPI
from ..clients.http import CircuitBreaker
from ..clients.member_api import MemberAPI
from ..config import ConfigError, Settings
from ..context import SessionContext
from ..kb.knowledge import TenantKnowledge, load_knowledge
from .budget import BudgetTracker
from .gateway import ToolGateway
from .phi import Redactor
from .trace import Tracer

# Shared across sessions: breakers must see every session's failures to protect a struggling dependency.
_BREAKER = CircuitBreaker()


@lru_cache(maxsize=16)
def tenant_knowledge(kb_dir: Path, tenant: str) -> TenantKnowledge:
    """Loaded once per tenant and shared read-only by that tenant's sessions only."""
    return load_knowledge(kb_dir, tenant)


def build_gateway(settings: Settings, ctx: SessionContext, *, http: httpx.Client | None = None,
                  directory_http: httpx.Client | None = None, sleep=None) -> ToolGateway:
    if ctx.tenant not in settings.tenant_tokens:
        raise ConfigError(f"tenant '{ctx.tenant}' is not configured")
    redactor = Redactor(settings.trace_hash_key.get_secret_value().encode())
    tracer = Tracer(ctx, redactor, sink_dir=settings.trace_dir)
    api = MemberAPI(ctx, settings.mock_base_url, settings.token_for(ctx.tenant), http=http, breaker=_BREAKER,
                    parallel_pages=settings.claims_parallel_pages, sleep=sleep)
    directory = None
    if settings.directory_api_key is not None:
        directory = DirectoryAPI(settings.mock_base_url, settings.directory_api_key.get_secret_value(),
                                 http=directory_http or http, breaker=_BREAKER, sleep=sleep)
    return ToolGateway(ctx, api=api, directory=directory,
                       knowledge=tenant_knowledge(settings.pack_dir / "kb", ctx.tenant),
                       budget=BudgetTracker(settings.budgets), tracer=tracer, redactor=redactor,
                       today=settings.today())
