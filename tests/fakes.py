"""An in-memory stand-in for the member-services and directory APIs, for fast offline unit tests.

All values here are invented for the tests; none come from the supplied pack. The fake records every request so
tests can assert on exactly what left the gateway (paths, tokens, headers, bodies).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from member_services.clients.directory import DirectoryAPI
from member_services.clients.http import CircuitBreaker, RetryPolicy
from member_services.clients.member_api import MemberAPI
from member_services.config import Budgets
from member_services.context import SessionContext
from member_services.gateway.budget import BudgetTracker
from member_services.gateway.gateway import ToolGateway
from member_services.gateway.phi import Redactor
from member_services.gateway.trace import Tracer
from member_services.kb.policies import TenantPolicy

TOKENS = {"tenant_x": "tok-x", "tenant_y": "tok-y"}
DIR_KEY = "dir-key"

# SSNs are split into two literals so the repository SSN scanner stays strict for real code.
MEMBER_X = {
    "member_id": "M100001", "first_name": "Avery", "last_name": "Quill", "dob": "1980-04-12", "sex": "F",
    "ssn": "900-11-" + "2345", "phone": "555-201-3344", "email": "avery.quill@example.test",
    "address": {"line1": "12 Birch Way", "city": "Springfield", "state": "OH", "zip": "43001"},
    "preferred_language": "en", "relationship": "subscriber", "product_code": "GOLD", "plan_name": "X Gold PPO",
    "network_id": "X-PPO", "group_number": "G-1", "coverage_effective": "2026-01-01", "coverage_termination": None,
    "pcp": None,
}
MEMBER_Y_SAME_ID = {**MEMBER_X, "first_name": "Blake", "last_name": "Rowe", "ssn": "900-22-" + "6789",
                    "product_code": "VAL", "plan_name": "Y Value HDHP", "network_id": "Y-VAL"}


def claim(cid, sdate, status="PAID", processed="2026-03-01", denial=None, notes=None, copay=0.0, member="M100001"):
    return {"claim_id": cid, "member_id": member, "service_date": sdate, "received_date": sdate,
            "processed_date": processed, "status": status, "provider": {"name": "Dr Test", "npi": "1234567890"},
            "place_of_service": "11 Office", "service": {"code": "99213", "description": "Office visit"},
            "billed_amount": 100.0, "allowed_amount": 80.0, "plan_paid": 80.0 - copay,
            "member_responsibility": {"deductible": 0.0, "copay": copay, "coinsurance": 0.0},
            "denial": denial, "notes": notes or []}


@dataclass
class FakeUpstream:
    members: dict = field(default_factory=lambda: {"tenant_x": {"M100001": MEMBER_X},
                                                   "tenant_y": {"M100001": MEMBER_Y_SAME_ID}})
    claims: dict = field(default_factory=dict)
    grievances: dict = field(default_factory=lambda: {"tenant_x": [], "tenant_y": []})
    providers: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    fail_next: list = field(default_factory=list)  # (method, path_substring, status, headers, create_anyway)
    idem: dict = field(default_factory=dict)
    seq: int = 0

    def _reply(self, status, body, headers=None):
        return httpx.Response(status, json=body, headers=headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = urlparse(str(request.url))
        parts = [p for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        for i, (method, sub, status, headers, create_anyway) in enumerate(self.fail_next):
            if method == request.method and sub in url.path:
                self.fail_next.pop(i)
                if create_anyway:
                    self._create(parts[1], parts[3], request)
                return self._reply(status, {"error": "x", "message": "injected"}, headers)
        if parts[:2] == ["directory", "v3"]:
            if request.headers.get("X-Api-Key") != DIR_KEY:
                return self._reply(401, {"error": "unauthorized"})
            res = [p for p in self.providers if q.get("network_id") in p["networks"]] or self.providers
            return self._reply(200, {"results": res, "count": len(res)})
        tenant = parts[1]
        if request.headers.get("Authorization") != f"Bearer {TOKENS[tenant]}":
            return self._reply(401, {"error": "unauthorized"})
        rest = parts[2:]
        if rest[0] == "claims":
            c = next((c for cl in self.claims.get(tenant, {}).values() for c in cl if c["claim_id"] == rest[1]), None)
            return self._reply(200, c) if c else self._reply(404, {"error": "not_found"})
        m = self.members.get(tenant, {}).get(rest[1])
        if m is None:
            return self._reply(404, {"error": "not_found"})
        if len(rest) == 2:
            return self._reply(200, m)
        if rest[2] == "benefits":
            return self._reply(200, {"member_id": m["member_id"], "product_code": m["product_code"],
                                     "plan_name": m["plan_name"], "network_id": m["network_id"],
                                     "coverage_status": "TERMINATED" if m["coverage_termination"] else "ACTIVE",
                                     "coverage_effective": m["coverage_effective"],
                                     "coverage_termination": m["coverage_termination"],
                                     "accumulators": {"plan_year": 2026, "as_of": "2026-11-15",
                                                      "deductible_met_individual": 250.0,
                                                      "out_of_pocket_met_individual": 900.0,
                                                      "physical_therapy_visits_used": 4, "internal_flag": "x"}})
        if rest[2] == "claims":
            items = self.claims.get(tenant, {}).get(m["member_id"], [])
            cur, size = int(q.get("cursor", 0)), int(q.get("page_size", 5))
            nxt = (f"/tenants/{tenant}/members/{m['member_id']}/claims?cursor={cur + size}&page_size={size}"
                   if cur + size < len(items) else None)
            return self._reply(200, {"data": items[cur:cur + size], "page_size": size, "next": nxt})
        if rest[2] == "grievances" and request.method == "GET":
            return self._reply(200, {"data": [g for g in self.grievances[tenant] if g["member_id"] == m["member_id"]]})
        if rest[2] == "grievances" and request.method == "POST":
            key = request.headers.get("Idempotency-Key")
            fp = hashlib.sha256(request.content).hexdigest()
            if key and (tenant, key) in self.idem:
                prior_fp, g = self.idem[(tenant, key)]
                if prior_fp != fp:
                    return self._reply(422, {"error": "idempotency_mismatch"})
                return self._reply(200, g, {"Idempotent-Replayed": "true"})
            return self._reply(201, self._create(tenant, m["member_id"], request))
        return self._reply(404, {"error": "not_found"})

    def _create(self, tenant, member_id, request):
        self.seq += 1
        body = json.loads(request.content)
        g = {"grievance_id": f"G-{self.seq}", "member_id": member_id, "status": "RECEIVED",
             "received_date": "2026-11-15", "channel": "api", **body}
        self.grievances[tenant].append(g)
        key = request.headers.get("Idempotency-Key")
        if key:
            self.idem[(tenant, key)] = (hashlib.sha256(request.content).hexdigest(), g)
        return g

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url="http://fake")


POLICY = {"tenant_x": TenantPolicy("tenant_x", 30, 180, "tenant_x/guide"),
          "tenant_y": TenantPolicy("tenant_y", 45, 180, "tenant_y/guide")}


def make_gateway(up: FakeUpstream, tenant="tenant_x", member="M100001", *, max_steps=20,
                 trace_dir: Path | None = None) -> ToolGateway:
    ctx = SessionContext(tenant, member)
    http = up.client()
    redactor = Redactor(b"test-key")
    tracer = Tracer(ctx, redactor, sink_dir=trace_dir)
    no_sleep = lambda _s: None  # noqa: E731
    api = MemberAPI(ctx, "http://fake", TOKENS[tenant], http=http, breaker=CircuitBreaker(),
                    retry=RetryPolicy(attempts=3), sleep=no_sleep)
    directory = DirectoryAPI("http://fake", DIR_KEY, http=http, breaker=CircuitBreaker(), sleep=no_sleep)
    budget = BudgetTracker(Budgets(max_steps=max_steps, max_cost_usd=1.0, max_latency_s=60))
    return ToolGateway(ctx, api=api, directory=directory, policy=POLICY[tenant], budget=budget, tracer=tracer,
                       redactor=redactor, today=date(2026, 11, 15))
