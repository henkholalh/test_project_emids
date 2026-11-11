"""HTTP API for the rep desktop.

The rep's tenant comes from their credential, never from the request body: a plan_a rep cannot open, read or
continue a plan_b session, and gets the same 404 for another tenant's session as for one that does not exist.

Run:  uvicorn member_services.api:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agent.session import SessionNotFound, SessionStore
from .clients.http import UpstreamError
from .config import ConfigError, get_settings
from .gateway.factory import tenant_knowledge
from .llm import make_llm

log = logging.getLogger("member_services.api")


class OpenSession(BaseModel):
    member_id: str = Field(pattern=r"^[A-Z0-9]{4,20}$", description="The member on the call")


class Turn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    confirm_action_id: str | None = Field(default=None, max_length=64,
                                          description="Set when the rep presses Confirm on a pending action")


def create_app(store: SessionStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        settings.require_runtime()
        app.state.settings = settings
        app.state.store = store if store is not None else SessionStore(settings, lambda: make_llm(settings))
        for tenant in settings.tenants:  # load and validate each tenant's KB at startup, not on the first call
            k = tenant_knowledge(settings.pack_dir / "kb", tenant)
            for path, reason in k.report.quarantined:
                log.warning("KB document quarantined for %s: %s (%s)", tenant, path, reason)
        yield

    app = FastAPI(title="Member services assistant", version="0.1.0", lifespan=lifespan)

    def rep_tenant(request: Request, authorization: str = Header(default="")) -> str:
        key = authorization.removeprefix("Bearer ").strip()
        tenant = request.app.state.settings.tenant_for_rep_key(key)
        if tenant is None:
            raise HTTPException(401, "invalid or missing rep credential")
        return tenant

    @app.exception_handler(ConfigError)
    async def _config_error(_request, exc):
        return JSONResponse(status_code=500, content={"error": "configuration", "message": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    async def open_session(body: OpenSession, request: Request, tenant: str = Depends(rep_tenant)):
        try:
            s = await run_in_threadpool(request.app.state.store.open, tenant, body.member_id)
        except UpstreamError:
            raise HTTPException(503, "member systems are unavailable; try again shortly") from None
        return {"session_id": s.session_id, "member_found": s.agent.gw.member_found}

    @app.post("/sessions/{session_id}/turns")
    async def post_turn(session_id: str, body: Turn, request: Request, tenant: str = Depends(rep_tenant)):
        try:
            s = request.app.state.store.get(session_id, tenant)
        except SessionNotFound:
            raise HTTPException(404, "session not found") from None
        r = await run_in_threadpool(s.turn, body.message, body.confirm_action_id)
        return {"answer": r.answer, "status": r.stopped, "pending_action": r.pending_action,
                "filed_grievance": r.filed_grievance,
                "usage": {"model_calls": r.model_calls, "tools": r.tool_calls, "cost_usd": round(r.cost_usd, 6),
                          "latency_ms": r.latency_ms}}

    @app.get("/sessions/{session_id}/trace")
    def get_trace(session_id: str, request: Request, tenant: str = Depends(rep_tenant)):
        try:
            s = request.app.state.store.get(session_id, tenant)
        except SessionNotFound:
            raise HTTPException(404, "session not found") from None
        return {"conversation_id": s.ctx.conversation_id, "events": s.agent.gw.tracer.events}

    @app.delete("/sessions/{session_id}", status_code=204)
    def close_session(session_id: str, request: Request, tenant: str = Depends(rep_tenant)):
        try:
            request.app.state.store.close(session_id, tenant)
        except SessionNotFound:
            raise HTTPException(404, "session not found") from None

    return app


app = create_app()
