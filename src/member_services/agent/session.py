"""Sessions: one per rep call, holding the bound context, its gateway and the agent's conversation.

The slice keeps sessions in memory with an idle timeout. The store sits behind a small interface so production can
move conversation state to Redis (or similar) without touching the agent. A session can only be used by a rep of
the tenant that created it; for anyone else it does not exist.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from ..config import Settings
from ..context import SessionContext
from ..gateway.factory import build_gateway
from ..llm.base import LLMClient
from .agent import Agent, TurnResult


class SessionNotFound(KeyError):
    pass


@dataclass
class Session:
    session_id: str
    ctx: SessionContext
    agent: Agent
    created: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def turn(self, message: str, confirm_action_id: str | None = None) -> TurnResult:
        with self.lock:  # one turn at a time per conversation; turns of different sessions run in parallel
            self.last_used = time.monotonic()
            return self.agent.turn(message, confirm_action_id)


class SessionStore:
    def __init__(self, settings: Settings, llm_factory, *, idle_timeout_s: float = 1800, max_sessions: int = 2000,
                 gateway_factory=None):
        self.settings = settings
        self._llm_factory = llm_factory
        self._gateway_factory = gateway_factory or (lambda ctx: build_gateway(settings, ctx))
        self.idle_timeout_s = idle_timeout_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def open(self, tenant: str, member_id: str) -> Session:
        self._evict()
        ctx = SessionContext(tenant, member_id)
        gw = self._gateway_factory(ctx)
        gw.prefetch()
        llm: LLMClient = self._llm_factory()
        session = Session(session_id=uuid.uuid4().hex, ctx=ctx, agent=Agent(gw, llm))
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("too many open sessions")
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str, tenant: str) -> Session:
        with self._lock:
            s = self._sessions.get(session_id)
        if s is None or s.ctx.tenant != tenant:
            raise SessionNotFound(session_id)  # another tenant's session is indistinguishable from none
        return s

    def close(self, session_id: str, tenant: str) -> None:
        self.get(session_id, tenant)
        with self._lock:
            self._sessions.pop(session_id, None)

    def _evict(self) -> None:
        now = time.monotonic()
        with self._lock:
            for sid in [k for k, s in self._sessions.items() if now - s.last_used > self.idle_timeout_s]:
                del self._sessions[sid]

    def __len__(self) -> int:
        return len(self._sessions)
