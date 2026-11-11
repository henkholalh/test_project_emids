"""HTTP API: tenant from the rep credential, session isolation, multi-turn confirmation."""
import pytest
from fastapi.testclient import TestClient

from fakes import FIXTURE_KB, FakeUpstream, claim, make_gateway
from member_services import api as api_module
from member_services.agent.session import SessionStore
from member_services.config import Settings
from member_services.llm.base import LLMResponse, ToolCall, Usage
from member_services.llm.fake import FakeLLM, text_response

KEYS = {"key-x": "tenant_x", "key-y": "tenant_y"}


@pytest.fixture
def client(monkeypatch):
    up = FakeUpstream()
    up.claims["tenant_x"] = {"M100001": [claim("C1000000001", "2026-10-01", copay=40.0)]}
    settings = Settings(rep_api_keys=KEYS, tenant_tokens={"tenant_x": "t", "tenant_y": "t"},
                        directory_api_key="d", pack_dir=FIXTURE_KB.parent, trace_dir=None)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    llm = FakeLLM()
    store = SessionStore(settings, lambda: llm,
                         gateway_factory=lambda ctx: make_gateway(up, ctx.tenant, ctx.member_id))
    app = api_module.create_app(store)
    with TestClient(app) as c:
        c.up, c.llm = up, llm
        yield c


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_requests_without_valid_rep_credential_are_rejected(client):
    assert client.post("/sessions", json={"member_id": "M100001"}).status_code == 401
    assert client.post("/sessions", json={"member_id": "M100001"}, headers=auth("nope")).status_code == 401


def test_tenant_comes_from_credential_not_body(client):
    r = client.post("/sessions", json={"member_id": "M100001", "tenant": "tenant_y"}, headers=auth("key-x"))
    assert r.status_code == 201
    paths = {req.url.path.split("/")[2] for req in client.up.requests if req.url.path.startswith("/tenants/")}
    assert paths == {"tenant_x"}


def test_other_tenants_session_is_indistinguishable_from_missing(client):
    sid = client.post("/sessions", json={"member_id": "M100001"}, headers=auth("key-x")).json()["session_id"]
    for method, path in (("post", f"/sessions/{sid}/turns"), ("get", f"/sessions/{sid}/trace"),
                         ("delete", f"/sessions/{sid}")):
        kw = {"json": {"message": "hi"}} if method == "post" else {}
        r = getattr(client, method)(path, headers=auth("key-y"), **kw)
        assert r.status_code == 404 and r.json()["detail"] == "session not found"
    assert client.post("/sessions/nonexistent/turns", json={"message": "hi"},
                       headers=auth("key-y")).json()["detail"] == "session not found"


def test_member_not_in_tenant_is_reported_on_open(client):
    r = client.post("/sessions", json={"member_id": "M999999"}, headers=auth("key-y"))
    assert r.status_code == 201 and r.json()["member_found"] is False


def test_invalid_member_id_is_rejected(client):
    assert client.post("/sessions", json={"member_id": "../x"}, headers=auth("key-x")).status_code == 422


def test_multi_turn_grievance_with_explicit_confirm(client):
    sid = client.post("/sessions", json={"member_id": "M100001"}, headers=auth("key-x")).json()["session_id"]
    client.llm.script += [
        LLMResponse("", [ToolCall("t1", "propose_grievance", {"category": "billing", "description": "Billed twice"})],
                    "tool_use", Usage(), "fake"),
        text_response("Please confirm with the member."),
    ]
    r1 = client.post(f"/sessions/{sid}/turns", json={"message": "File a billing grievance"},
                     headers=auth("key-x")).json()
    pid = r1["pending_action"]["pending_action_id"]
    assert r1["status"] == "answered" and not client.up.grievances["tenant_x"]

    client.llm.script += [LLMResponse("", [ToolCall("t2", "file_grievance", {"pending_action_id": pid})],
                                      "tool_use", Usage(), "fake"), text_response("Filed.")]
    r2 = client.post(f"/sessions/{sid}/turns", json={"message": "ok", "confirm_action_id": pid},
                     headers=auth("key-x")).json()
    assert r2["filed_grievance"]["category"] == "billing" and len(client.up.grievances["tenant_x"]) == 1
    assert r2["usage"]["tools"] == ["file_grievance"]


def test_trace_endpoint_returns_redacted_events(client):
    sid = client.post("/sessions", json={"member_id": "M100001"}, headers=auth("key-x")).json()["session_id"]
    client.llm.script.append(text_response("ok"))
    client.post(f"/sessions/{sid}/turns", json={"message": "hi"}, headers=auth("key-x"))
    events = client.get(f"/sessions/{sid}/trace", headers=auth("key-x")).json()["events"]
    kinds = {e["kind"] for e in events}
    assert {"turn", "model_call", "gateway_decision"} <= kinds
    assert all(e["member"].startswith("h:") for e in events)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
