"""Grievance filing: human confirmation before the write, and exactly one grievance per confirmed request (NFR-6).

Lifecycle of a request:

1. ``propose`` (model tool call): validate, build the exact request bytes once, derive the idempotency key, and
   return a summary for the rep to read back to the member. Nothing is written.
2. ``on_rep_turn`` (called by the agent loop with the next rep message, before the model sees it):
   - the rep confirms (explicit ``confirm_action_id`` from the UI, or an unambiguous affirmative message with no
     hold/cancel words) -> the proposal becomes *confirmed* for this turn;
   - anything else -> the proposal is cancelled. Silence or a new topic never counts as consent.
3. ``file`` (model tool call): only allowed for a proposal confirmed in the *current* turn. The same bytes and key
   are sent on every attempt, so retries after a timeout or 503 cannot create a duplicate. If the outcome is still
   unknown after retries, the member's grievances are re-read to reconcile before reporting.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field

from ..clients.http import UpstreamError
from ..clients.member_api import MemberAPI
from ..context import SessionContext
from .phi import contains_phi_pattern

CATEGORIES = ("billing", "quality_of_care", "access", "customer_service", "other")
MAX_DESCRIPTION = 2000

_AFFIRM_RE = re.compile(r"(?i)\b(yes|yep|confirm(ed|s)?|go ahead|proceed|file it|please file|agreed?|agrees|approved?|"
                        r"correct|that's right)\b")
_HOLD_RE = re.compile(r"(?i)\b(no|not|don't|do not|hold|wait|stop|cancel|never ?mind|actually|instead|"
                      r"change|edit|first)\b")


def is_confirmation(text: str) -> bool:
    """Conservative: affirmative words present and no hold/cancel/change words. False negatives only cost the rep
    one more 'yes'; a false positive would file a grievance the member did not agree to."""
    return bool(_AFFIRM_RE.search(text or "")) and not _HOLD_RE.search(text or "")


class GrievanceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Pending:
    action_id: str
    category: str
    description: str
    expedite_requested: bool
    claim_id: str | None
    body: bytes
    idempotency_key: str
    proposed_turn: int
    confirmed_turn: int | None = None
    status: str = "proposed"  # proposed | confirmed | filed | cancelled | unconfirmed
    grievance: dict | None = None
    possible_duplicates: list[str] = field(default_factory=list)


class GrievanceWorkflow:
    def __init__(self, ctx: SessionContext, api: MemberAPI):
        self.ctx = ctx
        self.api = api
        self.turn = 0
        self.pending: Pending | None = None
        self.filed: list[Pending] = []

    # ---- turn boundary -------------------------------------------------------------
    def on_rep_turn(self, text: str, confirm_action_id: str | None = None) -> str | None:
        """Advance the turn and resolve any open proposal. Returns 'confirmed', 'cancelled' or None."""
        self.turn += 1
        p = self.pending
        if p is None or p.status not in ("proposed", "confirmed", "unconfirmed"):
            return None
        if p.status == "unconfirmed":
            return None  # an unknown-outcome filing stays open so it can be retried with the same key
        explicit = confirm_action_id is not None and confirm_action_id == p.action_id
        if explicit or (confirm_action_id is None and is_confirmation(text)):
            p.status, p.confirmed_turn = "confirmed", self.turn
            return "confirmed"
        p.status = "cancelled"
        self.pending = None
        return "cancelled"

    # ---- tools ---------------------------------------------------------------------
    def propose(self, category: str, description: str, expedite_requested: bool = False,
                claim_id: str | None = None, existing: list[dict] | None = None) -> Pending:
        if category not in CATEGORIES:
            raise GrievanceError("invalid_category", f"category must be one of {', '.join(CATEGORIES)}")
        description = (description or "").strip()
        if not description:
            raise GrievanceError("invalid_description", "description is required")
        if len(description) > MAX_DESCRIPTION:
            raise GrievanceError("invalid_description", f"description must be at most {MAX_DESCRIPTION} characters")
        if contains_phi_pattern(description):
            raise GrievanceError("phi_in_description",
                                 "description must not contain SSNs, phone numbers or email addresses")
        if self.pending and self.pending.status == "unconfirmed":
            raise GrievanceError("filing_outcome_unknown",
                                 "a previous filing has an unknown outcome; retry it before proposing another")
        action_id = uuid.uuid4().hex[:12]
        body = json.dumps({"category": category, "description": description,
                           "expedite_requested": bool(expedite_requested)},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = hashlib.sha256(f"{self.ctx.tenant}:{self.ctx.member_id}:{action_id}".encode()).hexdigest()[:40]
        dups = []
        for g in existing or []:
            same_claim = claim_id and claim_id in (g.get("description") or "")
            if g.get("status") != "RESOLVED" and g.get("category") == category and (same_claim or not claim_id):
                dups.append(g.get("grievance_id"))
        self.pending = Pending(action_id=action_id, category=category, description=description,
                               expedite_requested=bool(expedite_requested), claim_id=claim_id, body=body,
                               idempotency_key=key, proposed_turn=self.turn, possible_duplicates=dups)
        return self.pending

    def file(self, action_id: str) -> Pending:
        p = self.pending
        if p is None or p.action_id != action_id:
            already = next((f for f in self.filed if f.action_id == action_id), None)
            if already:
                return already  # repeated tool call after success: same grievance, no new write
            raise GrievanceError("no_such_proposal", "no open proposal with that id; propose the grievance first")
        if p.status == "proposed":
            raise GrievanceError("not_confirmed",
                                 "the rep has not confirmed this grievance; read the summary back and ask the rep")
        if p.status == "confirmed" and p.confirmed_turn != self.turn:
            raise GrievanceError("confirmation_expired", "confirmation must be given in the current turn")
        try:
            _status, grievance, _replayed = self.api.create_grievance(p.body, p.idempotency_key)
        except UpstreamError as exc:
            if exc.status == 422:
                raise GrievanceError("rejected", "the grievance system rejected the request") from None
            grievance = self._reconcile(p)
            if grievance is None:
                p.status = "unconfirmed"
                raise GrievanceError("filing_outcome_unknown",
                                     "the grievance system did not confirm the filing. Do not file again; it will "
                                     "be retried safely with the same request id") from None
        p.status, p.grievance = "filed", grievance
        self.filed.append(p)
        self.pending = None
        return p

    def retry_unconfirmed(self) -> Pending | None:
        p = self.pending
        if p is None or p.status != "unconfirmed":
            return None
        p.status, p.confirmed_turn = "confirmed", self.turn
        return self.file(p.action_id)

    def _reconcile(self, p: Pending) -> dict | None:
        """After an unknown outcome, look for the grievance on file (the write may have landed)."""
        try:
            items = self.api.list_grievances()
        except UpstreamError:
            return None
        matches = [g for g in items if g.get("description") == p.description and g.get("category") == p.category
                   and g.get("channel") == "api"]
        return matches[-1] if matches else None
