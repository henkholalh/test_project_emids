"""The identity of one rep conversation.

A ``SessionContext`` is created once, when the rep opens the call, from the rep's authenticated identity (tenant)
and the member on screen. It is immutable: no tool argument or model output can change which tenant or member a
conversation is about. Every downstream component takes the context, never a free tenant or member string.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

_TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_MEMBER_RE = re.compile(r"^[A-Z0-9]{4,20}$")


@dataclass(frozen=True)
class SessionContext:
    tenant: str
    member_id: str
    conversation_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        if not _TENANT_RE.match(self.tenant):
            raise ValueError("invalid tenant id")
        if not _MEMBER_RE.match(self.member_id):
            raise ValueError("invalid member id")
