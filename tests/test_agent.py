"""Agent loop behaviour with a scripted fake model (no API cost, deterministic)."""
import json

import pytest

from fakes import MEMBER_X, FakeUpstream, claim, make_gateway
from member_services.agent.agent import Agent
from member_services.llm.base import LLMError, LLMResponse, ToolCall, Usage
from member_services.llm.fake import FakeLLM, text_response


def tool_response(*calls):
    return LLMResponse(text="", tool_calls=[ToolCall(f"tu{i}", n, a) for i, (n, a) in enumerate(calls)],
                       stop_reason="tool_use", usage=Usage(100, 20), model="fake")


@pytest.fixture
def up():
    u = FakeUpstream()
    u.claims["tenant_x"] = {"M100001": [claim("C1000000001", "2026-10-01", copay=40.0)]}
    return u


def make_agent(up, script, **kw):
    gw = make_gateway(up, **kw)
    gw.prefetch()
    return Agent(gw, FakeLLM(script=script))


def test_first_message_carries_projected_session_context_and_no_phi(up):
    agent = make_agent(up, [text_response("A $45 copay in 2026.")])
    r = agent.turn("What does a specialist visit cost?")
    assert r.answer == "A $45 copay in 2026." and r.stopped == "answered" and r.model_calls == 1
    sent = agent.llm.requests[0]
    first = sent.messages[0].text
    assert "Session context" in first and "plan_benefits_current_year" in first and "C1000000001" in first
    blob = json.dumps([m.text for m in sent.messages]) + sent.system
    for k in ("ssn", "dob", "phone", "email", "first_name", "last_name"):
        assert MEMBER_X[k] not in blob
    assert "Test Plan X" in sent.system  # client name from the tenant's own guide


def test_session_context_is_sent_once(up):
    agent = make_agent(up, [text_response("one"), text_response("two")])
    agent.turn("q1")
    agent.turn("q2")
    texts = [m.text or "" for m in agent.llm.requests[1].messages]
    assert sum("Session context" in t for t in texts) == 1


def test_tool_calls_go_through_gateway_and_results_return_to_model(up):
    agent = make_agent(up, [tool_response(("get_claim", {"claim_id": "C1000000001"})),
                            text_response("The member owes $40.")])
    r = agent.turn("How much does the member owe on C1000000001?")
    assert r.tool_calls == ["get_claim"] and r.model_calls == 2
    result = agent.llm.requests[1].messages[-1].tool_results[0]
    assert json.loads(result.content)["data"]["member_owes"] == 40.0
    assert any(e["kind"] == "tool_call" and e["name"] == "get_claim" for e in agent.gw.tracer.events)


def test_not_in_plan_answers_without_calling_the_model(up):
    gw = make_gateway(up, "tenant_y", "M999999")
    agent = Agent(gw, FakeLLM())
    r = agent.turn("Pull up recent claims")
    assert r.stopped == "not_in_plan" and r.model_calls == 0 and not agent.llm.requests
    assert "can't find this member ID" in r.answer


def test_budget_stops_runaway_tool_loop_gracefully(up):
    loop = lambda *_: tool_response(("get_benefits", {}))  # noqa: E731 - a model that never stops
    agent = make_agent(up, loop, max_steps=5)
    r = agent.turn("q")
    assert r.stopped == "budget" and "limits" in r.answer
    assert agent.gw.budget.steps == 5


def test_model_outage_gives_safe_message(up):
    def boom(*_):
        raise LLMError("Model call failed (APIConnectionError)")
    r = make_agent(up, boom).turn("q")
    assert r.stopped == "unavailable" and "temporarily unavailable" in r.answer


def test_refusal_is_handled(up):
    refusal = LLMResponse(text="", tool_calls=[], stop_reason="refusal", usage=Usage(), model="fake")
    r = make_agent(up, [refusal]).turn("q")
    assert r.stopped == "refusal"


def test_output_guard_masks_member_identity_values_in_answers(up):
    r = make_agent(up, [text_response(f"SSN on file is {MEMBER_X['ssn']}, DOB {MEMBER_X['dob']}.")]).turn("q")
    assert MEMBER_X["ssn"] not in r.answer and MEMBER_X["dob"] not in r.answer


def test_grievance_flow_across_turns_with_gateway_status(up):
    script = [
        tool_response(("propose_grievance", {"category": "billing", "description": "Billed in full for C1000000001."})),
        text_response("Here is the summary. Please confirm with the member."),
    ]
    agent = make_agent(up, script)
    r1 = agent.turn("Please file a billing grievance.")
    pid = r1.pending_action["pending_action_id"]
    assert r1.pending_action["category"] == "billing" and not up.grievances["tenant_x"]

    agent.llm.script += [tool_response(("file_grievance", {"pending_action_id": pid})),
                         text_response("Filed.")]
    r2 = agent.turn("Yes, the member agrees. File it.")
    assert "CONFIRMED" in agent.llm.requests[2].messages[-1].text
    assert r2.filed_grievance["category"] == "billing" and len(up.grievances["tenant_x"]) == 1
    assert r2.pending_action is None


def test_model_filing_without_confirmation_is_blocked_by_gateway(up):
    script = [
        tool_response(("propose_grievance", {"category": "billing", "description": "x"})),
        tool_response(("file_grievance", {"pending_action_id": "WILL_BE_WRONG"})),
        text_response("done"),
    ]
    agent = make_agent(up, script)

    def patch(system, messages, tools):  # file the real pending id in the same turn as the proposal
        r = script.pop(0)
        if r.tool_calls and r.tool_calls[0].name == "file_grievance":
            pid = agent.gw.grievances.pending.action_id
            r = tool_response(("file_grievance", {"pending_action_id": pid}))
        return r

    agent.llm.script = patch
    agent.turn("file a billing grievance")
    assert not up.grievances["tenant_x"]
    assert any(e["name"] == "write" and e["attrs"]["allowed"] is False for e in agent.gw.tracer.events)


def test_cancel_turn_tells_model_and_files_nothing(up):
    agent = make_agent(up, [tool_response(("propose_grievance", {"category": "billing", "description": "x"})),
                            text_response("Please confirm."), text_response("Understood, not filed.")])
    agent.turn("file a billing grievance")
    r = agent.turn("Actually, hold off.")
    assert "CANCELLED" in agent.llm.requests[-1].messages[-1].text
    assert r.answer == "Understood, not filed." and not up.grievances["tenant_x"]


def test_turn_trace_records_model_usage_without_prompt_text(up):
    agent = make_agent(up, [text_response("ok", usage=Usage(1000, 50, 2000, 0))])
    agent.turn("What does a specialist visit cost?")
    calls = [e for e in agent.gw.tracer.events if e["kind"] == "model_call"]
    assert calls and calls[0]["attrs"]["input_tokens"] == 1000
    assert "specialist" not in json.dumps(calls)
