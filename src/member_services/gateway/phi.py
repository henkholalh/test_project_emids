"""PHI minimisation (NFR-5).

Two separate controls:

1. **Projection (what reaches the model).** Each tool returns only allow-listed fields. Anything not listed,
   including fields the upstream API adds in future, is dropped. Identity fields (SSN, DOB, phone, email, street
   address, names of the member) are never projected; identity checks are done by the gateway itself.
2. **Redaction (what reaches logs and traces).** Before anything is written, known PHI values of the member on the
   call are replaced, and generic patterns (SSN, phone, email, DOB-like dates next to "DOB") are masked. Identifiers
   are replaced by keyed hashes so traces can still be correlated for audit without storing the raw id.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

MEMBER_FIELDS_FOR_MODEL = ("product_code", "plan_name", "network_id", "group_number", "relationship",
                           "preferred_language", "coverage_effective", "coverage_termination")
BENEFIT_FIELDS_FOR_MODEL = ("product_code", "plan_name", "network_id", "coverage_status", "coverage_effective",
                            "coverage_termination")
ACCUMULATOR_FIELDS_FOR_MODEL = ("plan_year", "as_of", "deductible_met_individual", "out_of_pocket_met_individual",
                                "physical_therapy_visits_used")
CLAIM_FIELDS_FOR_MODEL = ("claim_id", "service_date", "received_date", "processed_date", "status",
                          "place_of_service", "billed_amount", "allowed_amount", "plan_paid")
GRIEVANCE_FIELDS_FOR_MODEL = ("grievance_id", "category", "status", "received_date", "resolved_date",
                              "expedite_requested", "description")

# Member-record fields whose *values* must never appear in logs, prompts to third parties, or answers.
MEMBER_PHI_FIELDS = ("ssn", "dob", "phone", "email", "first_name", "last_name")

SSN_RE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
PHONE_RE = re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
DOB_CONTEXT_RE = re.compile(r"(?i)\b(dob|date of birth|born)\b[:\s]*\S+")


def pick(record: dict | None, fields: tuple[str, ...]) -> dict:
    record = record or {}
    return {f: record.get(f) for f in fields if f in record}


def project_member(member: dict) -> dict:
    out = pick(member, MEMBER_FIELDS_FOR_MODEL)
    pcp = member.get("pcp")
    if pcp:  # provider details are not member PHI; keep only what a rep needs
        out["pcp"] = {k: pcp.get(k) for k in ("name", "npi") if k in pcp}
    return out


def project_benefits(benefits: dict) -> dict:
    out = pick(benefits, BENEFIT_FIELDS_FOR_MODEL)
    out["accumulators"] = pick(benefits.get("accumulators"), ACCUMULATOR_FIELDS_FOR_MODEL)
    return out


def project_claim(claim: dict) -> dict:
    out = pick(claim, CLAIM_FIELDS_FOR_MODEL)
    prov = claim.get("provider") or {}
    out["provider"] = {k: prov.get(k) for k in ("name", "npi") if k in prov}
    svc = claim.get("service") or {}
    out["service"] = {k: svc.get(k) for k in ("code", "description") if k in svc}
    out["member_responsibility"] = dict(claim.get("member_responsibility") or {})
    denial = claim.get("denial")
    out["denial"] = {k: denial.get(k) for k in ("carc", "description")} if denial else None
    return out


def project_grievance(g: dict) -> dict:
    return pick(g, GRIEVANCE_FIELDS_FOR_MODEL)


class Redactor:
    """Masks PHI in free text and structured values before they are logged."""

    def __init__(self, hash_key: bytes, member: dict | None = None):
        self._key = hash_key
        self._values: list[tuple[str, str]] = []
        if member:
            self.add_member(member)

    def add_member(self, member: dict) -> None:
        for f in MEMBER_PHI_FIELDS:
            v = member.get(f)
            if isinstance(v, str) and len(v) >= 3:
                self._values.append((v, f"[{f.upper()}]"))
                if f == "ssn":
                    self._values.append((v.replace("-", ""), "[SSN]"))
        addr = member.get("address") or {}
        if isinstance(addr.get("line1"), str):
            self._values.append((addr["line1"], "[ADDRESS]"))
        # Longest first so a full value is replaced before any substring of it.
        self._values.sort(key=lambda kv: len(kv[0]), reverse=True)

    def pseudonym(self, value: str) -> str:
        """Stable keyed hash for correlating audit records without storing the raw identifier."""
        return "h:" + hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:16]

    def text(self, s: str) -> str:
        for value, label in self._values:
            s = s.replace(value, label)
        s = SSN_RE.sub("[SSN]", s)
        s = EMAIL_RE.sub("[EMAIL]", s)
        s = PHONE_RE.sub("[PHONE]", s)
        s = DOB_CONTEXT_RE.sub(lambda m: m.group(1) + " [DOB]", s)
        return s

    def value(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {k: self.value(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.value(x) for x in v]
        return v


def contains_phi_pattern(s: str) -> bool:
    return bool(SSN_RE.search(s) or EMAIL_RE.search(s) or PHONE_RE.search(s))
