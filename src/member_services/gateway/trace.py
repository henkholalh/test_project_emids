"""Conversation trace: every model call, tool call and gateway decision as one JSON line.

Every event passes through the session's ``Redactor`` before it is written, and the member id is stored only as a
keyed pseudonym. In production the sink is an append-only store with a 6-year retention lock (NFR-5); here it is
a local JSONL file per conversation plus an in-memory list that tests and the eval harness read.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..context import SessionContext
from .phi import Redactor


@dataclass
class Tracer:
    ctx: SessionContext
    redactor: Redactor
    sink_dir: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, kind: str, name: str, *, parent: str | None = None, **attrs: Any) -> str:
        span_id = uuid.uuid4().hex[:12]
        event = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "conversation_id": self.ctx.conversation_id,
            "tenant": self.ctx.tenant,
            "member": self.redactor.pseudonym(self.ctx.member_id),
            "span_id": span_id,
            "parent": parent,
            "kind": kind,  # model_call | tool_call | gateway_decision | turn | budget
            "name": name,
            "attrs": self.redactor.value(attrs),
        }
        with self._lock:
            self.events.append(event)
            if self.sink_dir is not None:
                self.sink_dir.mkdir(parents=True, exist_ok=True)
                with open(self.sink_dir / f"{self.ctx.conversation_id}.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, default=str) + "\n")
        return span_id

    def decision(self, name: str, allowed: bool, reason: str, **attrs: Any) -> str:
        return self.emit("gateway_decision", name, allowed=allowed, reason=reason, **attrs)

    def timer(self) -> "_Timer":
        return _Timer()


class _Timer:
    def __init__(self):
        self.start = time.perf_counter()

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)
