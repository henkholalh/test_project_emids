"""Remaining control paths: budgets (cost, latency), circuit breaker, context validation, judge parsing."""
import httpx
import pytest

from member_services.clients.http import CircuitBreaker, CircuitOpen, RetryPolicy, UpstreamError, send_with_retry
from member_services.config import Budgets
from member_services.context import SessionContext
from member_services.evaluation.judge import Judge
from member_services.gateway.budget import BudgetExceeded, BudgetTracker
from member_services.llm.fake import FakeLLM, text_response


def test_cost_budget_stops_further_steps():
    b = BudgetTracker(Budgets(max_steps=50, max_cost_usd=0.03, max_latency_s=60))
    b.charge_cost(0.031)
    with pytest.raises(BudgetExceeded) as e:
        b.charge_step()
    assert e.value.kind == "cost"


def test_turn_latency_budget_uses_turn_clock():
    now = [100.0]
    b = BudgetTracker(Budgets(max_steps=50, max_cost_usd=1, max_latency_s=10), clock=lambda: now[0])
    b.start_turn()
    now[0] = 109.0
    b.charge_step()
    now[0] = 111.0
    with pytest.raises(BudgetExceeded) as e:
        b.charge_step()
    assert e.value.kind == "latency"
    b.start_turn()  # a new turn gets a fresh time budget
    b.charge_step()


def test_circuit_breaker_opens_after_failures_and_half_opens_after_cooldown():
    now = [0.0]
    br = CircuitBreaker(threshold=2, cooldown_s=30, clock=lambda: now[0])
    br.record("k", ok=False)
    br.record("k", ok=False)
    with pytest.raises(CircuitOpen):
        br.check("k")
    now[0] = 31
    br.check("k")  # half-open: one trial allowed


def test_send_with_retry_gives_up_on_transport_errors_and_opens_breaker():
    def down(_request):
        raise httpx.ConnectError("refused")
    client = httpx.Client(transport=httpx.MockTransport(down))
    br = CircuitBreaker(threshold=1)
    with pytest.raises(UpstreamError) as e:
        send_with_retry(client, client.build_request("GET", "http://x/"), breaker=br, breaker_key="k",
                        policy=RetryPolicy(attempts=2), sleep=lambda _s: None)
    assert e.value.code == "transport"
    with pytest.raises(CircuitOpen):
        br.check("k")


@pytest.mark.parametrize("tenant,member", [("Plan_A", "M1234"), ("plan_a", "m1234"), ("plan_a", "../x")])
def test_session_context_rejects_malformed_ids(tenant, member):
    with pytest.raises(ValueError):
        SessionContext(tenant, member)


def test_judge_parses_verdict_and_handles_garbage():
    j = Judge(FakeLLM(script=[text_response('Sure. {"verdict": true, "reason": "declines"}'),
                              text_response("no json here")]))
    assert j.ask("decline", "q", "a") == (True, "declines")
    assert j.ask("decline", "q", "a") == (None, "judge reply could not be parsed")
    assert Judge(None).ask("decline", "q", "a")[0] is None
