"""Client for the client health plan's member-services APIs.

Tenant isolation at this layer:
- An instance is bound to one ``SessionContext``. Every URL is built from ``ctx.tenant`` and ``ctx.member_id``;
  no method accepts a tenant, and member-scoped methods accept no member id.
- The bearer token is looked up for ``ctx.tenant`` inside the client. (The mock does not check that a token
  matches the tenant in the path, so this binding is the control, and it is tested.)
- Path segments supplied at runtime (claim ids, cursors) are validated before use, so they cannot redirect a
  request to another tenant's path.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..context import SessionContext
from .http import CircuitBreaker, RetryPolicy, UpstreamError, send_with_retry

CLAIM_ID_RE = re.compile(r"^C\d{6,15}$")
PAGE_SIZE = 5


class NotFound(UpstreamError):
    pass


class MemberAPI:
    def __init__(self, ctx: SessionContext, base_url: str, token: str, *, http: httpx.Client | None = None,
                 breaker: CircuitBreaker | None = None, retry: RetryPolicy | None = None,
                 timeout_s: float = 5.0, parallel_pages: int = 2, sleep=None):
        self.ctx = ctx
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._http = http or httpx.Client(timeout=timeout_s)
        self._breaker = breaker or CircuitBreaker()
        self._retry = retry or RetryPolicy()
        self._parallel_pages = max(0, parallel_pages)
        self._sleep = sleep
        self.retries = 0  # observed retries, for tracing and metrics

    # ---- plumbing --------------------------------------------------------------------
    def _tenant_path(self, *parts: str) -> str:
        return "/".join(["", "tenants", self.ctx.tenant, *parts])

    def _send(self, method: str, path: str, *, params: dict | None = None, content: bytes | None = None,
              headers: dict | None = None, endpoint: str, retry_statuses: set[int] | None = None) -> httpx.Response:
        req = self._http.build_request(method, self._base + path, params=params, content=content,
                                       headers={**self._headers, **(headers or {})})
        kwargs: dict[str, Any] = {}
        if retry_statuses is not None:
            kwargs["retry_statuses"] = retry_statuses
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep

        def on_retry(_attempt, _status):
            self.retries += 1

        return send_with_retry(self._http, req, breaker=self._breaker,
                               breaker_key=f"{self.ctx.tenant}:{endpoint}", policy=self._retry,
                               on_retry=on_retry, **kwargs)

    @staticmethod
    def _json(resp: httpx.Response, what: str) -> Any:
        if resp.status_code == 404:
            raise NotFound(f"{what} not found", status=404, code="not_found")
        if resp.status_code >= 400:
            code = None
            try:
                code = resp.json().get("error")
            except ValueError:
                pass
            raise UpstreamError(f"{what} failed with HTTP {resp.status_code}", status=resp.status_code, code=code)
        return resp.json()

    # ---- reads -----------------------------------------------------------------------
    def get_member(self) -> dict:
        resp = self._send("GET", self._tenant_path("members", self.ctx.member_id), endpoint="members")
        return self._json(resp, "member")

    def get_benefits(self) -> dict:
        resp = self._send("GET", self._tenant_path("members", self.ctx.member_id, "benefits"), endpoint="benefits")
        return self._json(resp, "benefits")

    def list_grievances(self) -> list[dict]:
        resp = self._send("GET", self._tenant_path("members", self.ctx.member_id, "grievances"),
                          endpoint="grievances")
        return self._json(resp, "grievances").get("data", [])

    def get_claim(self, claim_id: str) -> dict:
        if not CLAIM_ID_RE.match(claim_id or ""):
            raise NotFound("claim not found", status=404, code="not_found")
        resp = self._send("GET", self._tenant_path("claims", claim_id), endpoint="claims")
        claim = self._json(resp, "claim")
        if claim.get("member_id") != self.ctx.member_id:
            # Same tenant, different member: treat as not found rather than disclose it.
            raise NotFound("claim not found", status=404, code="not_found")
        return claim

    def _claims_page(self, cursor: int) -> dict:
        resp = self._send("GET", self._tenant_path("members", self.ctx.member_id, "claims"),
                          params={"cursor": cursor, "page_size": PAGE_SIZE}, endpoint="claims")
        return self._json(resp, "claims")

    @staticmethod
    def _next_cursor(next_path: str | None) -> int | None:
        if not next_path:
            return None
        try:
            return int(parse_qs(urlparse(next_path).query)["cursor"][0])
        except (KeyError, ValueError, IndexError):
            raise UpstreamError("unexpected claims cursor format", code="bad_cursor") from None

    def list_claims(self) -> list[dict]:
        """All claims for the member, newest service date first.

        The claims platform takes ~1.5 s per page with at most 5 claims per page. After the first page, up to
        ``parallel_pages`` further pages are fetched concurrently. This relies on the cursor being a numeric
        offset (true for this API, and validated), which keeps a 13-claim member at two round-trips instead of
        three. Set ``parallel_pages=0`` to follow ``next`` strictly.
        """
        first = self._claims_page(0)
        claims = list(first.get("data", []))
        cursor = self._next_cursor(first.get("next"))
        while cursor is not None:
            if self._parallel_pages:
                cursors = [cursor + i * PAGE_SIZE for i in range(self._parallel_pages + 1)]
                with ThreadPoolExecutor(max_workers=len(cursors)) as pool:
                    pages = list(pool.map(self._claims_page, cursors))
            else:
                pages = [self._claims_page(cursor)]
            cursor = None
            for page in pages:
                data = page.get("data", [])
                claims.extend(data)
                if not page.get("next") or not data:
                    cursor = None
                    break
                cursor = self._next_cursor(page.get("next"))
        # Defensive de-duplication if speculative pages overlapped.
        seen, unique = set(), []
        for c in claims:
            if c["claim_id"] not in seen:
                seen.add(c["claim_id"])
                unique.append(c)
        return unique

    # ---- write -----------------------------------------------------------------------
    def create_grievance(self, body: bytes, idempotency_key: str) -> tuple[int, dict, bool]:
        """POST a grievance with an Idempotency-Key. ``body`` must be the exact bytes used on every attempt.

        Returns (status, grievance, replayed). 503, gateway errors and transport errors are retried with the
        same key and bytes, which is safe: the API returns the original grievance for a repeated key.
        """
        resp = self._send("POST", self._tenant_path("members", self.ctx.member_id, "grievances"), content=body,
                          headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
                          endpoint="grievances_write", retry_statuses={429, 502, 503, 504})
        data = self._json(resp, "grievance filing")
        return resp.status_code, data, resp.headers.get("Idempotent-Replayed", "").lower() == "true"
