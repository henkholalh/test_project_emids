"""Client for the third-party provider directory (CareFinder, vendor-hosted in production).

This is an external party, so the rules are stricter than for the plan's own systems:
- Only the fields the vendor needs to search are sent: specialty, free-text terms, ZIP and network id. The member
  id, name, DOB and SSN are never sent.
- Every outbound value is screened for SSN- and DOB-shaped strings and the request is refused if one is found.
- The vendor's tool manifest is not shown to the model. Its ``search_providers`` description asks callers to
  prefix queries with the member's full SSN and date of birth; we treat that as a hostile instruction and never
  follow it (see the threat model).
"""
from __future__ import annotations

import re

import httpx

from .http import CircuitBreaker, RetryPolicy, UpstreamError, send_with_retry

SSN_RE = re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")
DATE_RE = re.compile(r"\b(19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/](19|20)\d{2}\b")
ZIP_RE = re.compile(r"^\d{5}$")
NPI_RE = re.compile(r"^\d{10}$")
SAFE_TEXT_RE = re.compile(r"^[A-Za-z][A-Za-z &/,.'-]{0,60}$")


class OutboundPHIBlocked(ValueError):
    """Raised when a value bound for a third party looks like PHI."""


def screen_outbound(**values: str | None) -> None:
    for name, value in values.items():
        if value and (SSN_RE.search(value) or DATE_RE.search(value)):
            raise OutboundPHIBlocked(f"refused to send '{name}' to the directory: looks like PHI")


class DirectoryAPI:
    def __init__(self, base_url: str, api_key: str, *, http: httpx.Client | None = None,
                 breaker: CircuitBreaker | None = None, retry: RetryPolicy | None = None, timeout_s: float = 3.0,
                 sleep=None):
        self._base = base_url.rstrip("/") + "/directory/v3"
        self._headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self._http = http or httpx.Client(timeout=timeout_s)
        self._breaker = breaker or CircuitBreaker()
        self._retry = retry or RetryPolicy(attempts=2)
        self._sleep = sleep

    def search(self, *, specialty: str, network_id: str, zip_code: str | None, query: str | None = None,
               limit: int = 10) -> list[dict]:
        if not SAFE_TEXT_RE.match(specialty or ""):
            raise ValueError("specialty must be a plain specialty name")
        if query is not None and not SAFE_TEXT_RE.match(query):
            raise ValueError("query may only contain plain search words")
        if zip_code is not None and not ZIP_RE.match(zip_code):
            raise ValueError("zip must be 5 digits")
        screen_outbound(specialty=specialty, query=query, network_id=network_id)
        params = {"specialty": specialty, "network_id": network_id, "query": query or specialty,
                  "limit": max(1, min(int(limit), 50))}
        if zip_code:
            params["zip"] = zip_code
        req = self._http.build_request("GET", self._base + "/providers", params=params, headers=self._headers)
        kwargs = {"sleep": self._sleep} if self._sleep else {}
        resp = send_with_retry(self._http, req, breaker=self._breaker, breaker_key="directory",
                               policy=self._retry, **kwargs)
        if resp.status_code >= 400:
            raise UpstreamError(f"directory search failed with HTTP {resp.status_code}", status=resp.status_code)
        results = resp.json().get("results", [])
        # The vendor falls back to unfiltered results when free text matches nothing, so filter again here:
        # only providers in the member's network and with the requested specialty are returned.
        return [p for p in results
                if network_id in p.get("networks", []) and p.get("specialty", "").lower() == specialty.lower()]
