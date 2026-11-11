"""HTTP resilience shared by the system-of-record clients: timeouts, bounded retries honouring Retry-After, and a
per-endpoint circuit breaker so a failing dependency fails fast instead of consuming the rep's latency budget."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

RETRYABLE_STATUS = {429, 502, 503, 504}


class UpstreamError(RuntimeError):
    """A dependency failed. ``status`` is the HTTP status (None for transport errors)."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class CircuitOpen(UpstreamError):
    pass


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures and stays open for ``cooldown_s``."""

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown_s, self.clock = threshold, cooldown_s, clock
        self._failures: dict[str, int] = {}
        self._opened: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        with self._lock:
            opened = self._opened.get(key)
            if opened is not None and self.clock() - opened < self.cooldown_s:
                raise CircuitOpen(f"{key} temporarily unavailable", status=None, code="circuit_open")
            if opened is not None:  # half-open: allow one trial request
                del self._opened[key]

    def record(self, key: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures[key] = 0
                return
            self._failures[key] = self._failures.get(key, 0) + 1
            if self._failures[key] >= self.threshold:
                self._opened[key] = self.clock()
                self._failures[key] = 0


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", default)))
    except ValueError:
        return default


def send_with_retry(client: httpx.Client, request: httpx.Request, *, breaker: CircuitBreaker, breaker_key: str,
                    policy: RetryPolicy, retry_statuses: set[int] = RETRYABLE_STATUS,
                    sleep: Callable[[float], None] = time.sleep,
                    on_retry: Callable[[int, int | None], None] | None = None) -> httpx.Response:
    """Send ``request`` with bounded retries. Returns the final response (which may be an error status).

    Transport errors and ``retry_statuses`` are retried; 429 waits for ``Retry-After``. Other statuses return
    immediately so the caller can map them (for example 404 to "not found").
    """
    breaker.check(breaker_key)
    last_exc: Exception | None = None
    resp: httpx.Response | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            resp = client.send(request)
        except httpx.TransportError as exc:
            last_exc, resp = exc, None
        if resp is not None and resp.status_code not in retry_statuses:
            breaker.record(breaker_key, ok=resp.status_code < 500)
            return resp
        if attempt == policy.attempts:
            break
        status = resp.status_code if resp is not None else None
        if on_retry:
            on_retry(attempt, status)
        delay = min(policy.max_delay_s, policy.base_delay_s * 2 ** (attempt - 1))
        if resp is not None and resp.status_code == 429:
            delay = min(policy.max_delay_s, _retry_after(resp, delay))
        sleep(delay)
    breaker.record(breaker_key, ok=False)
    if resp is not None:
        return resp
    raise UpstreamError(f"{breaker_key} unreachable ({type(last_exc).__name__})", status=None, code="transport")
