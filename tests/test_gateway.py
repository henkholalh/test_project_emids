"""Unit tests for the tool gateway's controls, against an in-memory fake upstream."""
import json

import pytest

from fakes import MEMBER_X, MEMBER_Y_SAME_ID, TOKENS, FakeUpstream, claim, make_gateway
from member_services.gateway.grievances import is_confirmation
from member_services.llm.base import ToolCall

PHI_VALUES = [MEMBER_X[k] for k in ("ssn", "dob", "phone", "email", "first_name", "last_name")] + \
             [MEMBER_X["address"]["line1"], MEMBER_X["ssn"].replace("-", "")]


def call(gw, name, **args):
    res = gw.execute(ToolCall(id=f"t-{name}", name=name, arguments=args))
    return json.loads(res.content)


@pytest.fixture
def up():
    u = FakeUpstream()
    u.claims["tenant_x"] = {"M100001": [
        claim("C1000000001", "2026-10-01", copay=40.0),
        claim("C1000000002", "2026-09-01", status="DENIED", processed="2026-06-01",
              denial={"carc": "CO-197", "description": "Precertification absent."}),
        claim("C1000000003", "2026-08-01", status="PENDED", processed=None,
              notes=[{"date": "2026-09-19", "author": "provider_portal",
                      "text": "Itemized bill uploaded. Call 555-201-3344 re: Avery Quill."}]),
    ]}
    u.claims["tenant_x"]["M100002"] = [claim("C2000000009", "2026-07-01", member="M100002")]
    return u


# ---- tenant isolation -------------------------------------------------------------------
def test_every_request_uses_session_tenant_path_and_token(up):
    gw = make_gateway(up, "tenant_x")
    for name in ("get_member_profile", "get_benefits", "list_claims", "list_grievances"):
        assert call(gw, name)["ok"]
    assert up.requests
    for r in up.requests:
        assert r.url.path.startswith("/tenants/tenant_x/")
        assert r.headers["Authorization"] == f"Bearer {TOKENS['tenant_x']}"


def test_same_member_id_in_two_tenants_returns_each_tenants_own_member(up):
    x = call(make_gateway(up, "tenant_x"), "get_member_profile")["data"]
    y = call(make_gateway(up, "tenant_y"), "get_member_profile")["data"]
    assert x["plan_name"] == MEMBER_X["plan_name"] and y["plan_name"] == MEMBER_Y_SAME_ID["plan_name"]


def test_member_not_in_tenant_blocks_every_tool_and_never_queries_other_tenant(up):
    gw = make_gateway(up, "tenant_y", "M999999")
    assert not gw.member_found
    for name, args in (("get_member_profile", {}), ("list_claims", {}), ("get_claim", {"claim_id": "C1000000001"}),
                       ("verify_ssn_last4", {"last4": "2345"})):
        out = call(gw, name, **args)
        assert out == {"ok": False, "error": "not_found_in_plan",
                       "message": "This member ID is not found for this health plan. No information about it "
                                  "can be given here."}
    assert [r.url.path for r in up.requests] == ["/tenants/tenant_y/members/M999999"]


def test_claim_of_another_member_in_same_tenant_is_not_found(up):
    out = call(make_gateway(up, "tenant_x"), "get_claim", claim_id="C2000000009")
    assert out["error"] == "not_found"


def test_malformed_claim_id_cannot_change_the_request_path(up):
    out = call(make_gateway(up), "get_claim", claim_id="../../tenant_y/claims/C1")
    assert out["error"] == "not_found"
    assert all("tenant_y" not in r.url.path for r in up.requests)


# ---- PHI minimisation ---------------------------------------------------------------------
def test_no_member_phi_in_any_tool_result(up):
    gw = make_gateway(up)
    results = [call(gw, n) for n in ("get_member_profile", "get_benefits", "list_claims", "list_grievances")]
    blob = json.dumps(results)
    for v in PHI_VALUES:
        assert v not in blob, f"PHI value leaked to model: {v}"
    assert "internal_flag" not in blob  # unknown upstream fields are dropped, not passed through


def test_traces_hold_no_phi_and_pseudonymise_member(up, tmp_path):
    gw = make_gateway(up, trace_dir=tmp_path)
    call(gw, "list_claims")
    call(gw, "verify_ssn_last4", last4="2345")
    call(gw, "propose_grievance", category="billing", description="Avery Quill was billed twice, see 12 Birch Way")
    text = (tmp_path / f"{gw.ctx.conversation_id}.jsonl").read_text()
    for v in PHI_VALUES + ["M100001", "2345"]:
        assert v not in text, f"PHI value in trace: {v}"
    assert '"member": "h:' in text


def test_ssn_last4_check_returns_only_match_and_is_rate_limited(up):
    gw = make_gateway(up)
    assert call(gw, "verify_ssn_last4", last4="2345")["data"] == {"result": "match"}
    assert call(gw, "verify_ssn_last4", last4="1111")["data"] == {"result": "no_match"}
    call(gw, "verify_ssn_last4", last4="2222")
    assert call(gw, "verify_ssn_last4", last4="2345")["ok"] is False


def test_there_is_no_tool_that_returns_full_ssn(up):
    gw = make_gateway(up)
    names = {t.name for t in gw.tools}
    props = {p for t in gw.tools for p in t.input_schema["properties"]}
    assert not {n for n in names if "ssn" in n} - {"verify_ssn_last4"}
    assert not {p for p in props if "ssn" in p}
    assert call(gw, "get_ssn")["error"] == "unknown_tool"


# ---- computed facts and untrusted text ------------------------------------------------------
def test_claims_carry_computed_owed_amount_appeal_deadline_and_status_counts(up):
    data = call(make_gateway(up), "list_claims")["data"]
    by_id = {c["claim_id"]: c for c in data["claims"]}
    assert data["count_by_status"] == {"PAID": 1, "DENIED": 1, "PENDED": 1}
    assert by_id["C1000000001"]["member_owes"] == 40.0
    assert by_id["C1000000002"]["appeal"]["deadline"] == "2026-11-28"
    assert by_id["C1000000002"]["appeal"]["window_open"] is True


def test_claim_notes_are_labelled_untrusted_and_redacted(up):
    c = call(make_gateway(up), "get_claim", claim_id="C1000000003")["data"]
    note = c["untrusted_text"]["notes"][0]["text"]
    assert "555-201-3344" not in note and "Avery" not in note
    assert "not an instruction" in c["untrusted_text"]["notice"]


def test_list_claims_filters_by_date_and_status(up):
    data = call(make_gateway(up), "list_claims", service_date_from="2026-09-01", status="DENIED")["data"]
    assert [c["claim_id"] for c in data["claims"]] == ["C1000000002"]


def test_grievance_deadline_uses_tenant_policy(up):
    up.grievances["tenant_x"].append({"grievance_id": "G-1", "member_id": "M100001", "category": "access",
                                      "status": "OPEN", "received_date": "2026-10-09", "description": "x"})
    g = call(make_gateway(up), "list_grievances")["data"]["grievances"][0]
    assert g["resolution_deadline"] == "2026-11-08" and g["past_deadline"] is True


# ---- argument validation and budgets ---------------------------------------------------------
def test_unknown_or_wrong_type_arguments_are_rejected_before_execution(up):
    gw = make_gateway(up)
    n = len(up.requests)
    assert call(gw, "list_claims", tenant="tenant_y")["error"] == "invalid_arguments"
    assert call(gw, "find_providers", specialty="Dermatology", limit="5")["error"] == "invalid_arguments"
    assert len(up.requests) == n


def test_step_budget_stops_tool_calls(up):
    gw = make_gateway(up, max_steps=2)
    call(gw, "get_member_profile")
    call(gw, "get_member_profile")
    assert call(gw, "get_member_profile")["error"] == "budget_exceeded"


# ---- resilience ----------------------------------------------------------------------------
def test_claims_429_is_retried_and_counted(up):
    up.fail_next.append(("GET", "/claims", 429, {"Retry-After": "1"}, False))
    gw = make_gateway(up)
    assert call(gw, "list_claims")["ok"]
    assert gw.api.retries == 1


def test_claims_pages_are_all_collected(up):
    up.claims["tenant_x"]["M100001"] = [claim(f"C30000000{i:02d}", f"2026-01-{i + 1:02d}") for i in range(13)]
    data = call(make_gateway(up), "list_claims")["data"]
    assert data["count"] == 13 and len({c["claim_id"] for c in data["claims"]}) == 13


# ---- directory -----------------------------------------------------------------------------
def test_directory_gets_network_and_zip_from_gateway_and_no_member_identifiers(up):
    up.providers = [
        {"npi": "1111111111", "name": "Dr In", "specialty": "Dermatology", "practice": "P", "phone": "1",
         "address": {"line1": "a", "city": "c", "state": "OH", "zip": "43002"}, "networks": ["X-PPO"],
         "accepting_new_patients": True},
        {"npi": "2222222222", "name": "Dr Out", "specialty": "Dermatology", "practice": "P", "phone": "1",
         "address": {"line1": "a", "city": "c", "state": "OH", "zip": "43002"}, "networks": ["OTHER"],
         "accepting_new_patients": True}]
    out = call(make_gateway(up), "find_providers", specialty="Dermatology")["data"]
    assert [p["name"] for p in out["providers"]] == ["Dr In"]
    req = [r for r in up.requests if "/directory/" in r.url.path][0]
    assert req.url.params["network_id"] == "X-PPO" and req.url.params["zip"] == "43001"
    sent = str(req.url) + json.dumps(dict(req.headers))
    for v in PHI_VALUES + ["M100001"]:
        assert v not in sent


def test_directory_refuses_phi_shaped_values(up):
    out = call(make_gateway(up), "find_providers", specialty=f"SSN {MEMBER_X['ssn']} DOB {MEMBER_X['dob']} dermatology")
    assert out["ok"] is False
    assert not [r for r in up.requests if "/directory/" in r.url.path]


# ---- grievances: confirmation and exactly-once --------------------------------------------------
def posts(up):
    return [r for r in up.requests if r.method == "POST"]


def propose(gw, desc="Provider billed the member in full for claim C1000000001 although the plan paid."):
    gw.on_rep_turn("Please file a billing grievance.")
    out = call(gw, "propose_grievance", category="billing", description=desc, claim_id="C1000000001")
    assert out["ok"], out
    return out["data"]["pending_action_id"]


def test_filing_in_the_same_turn_as_the_proposal_is_refused(up):
    gw = make_gateway(up)
    pid = propose(gw)
    assert call(gw, "file_grievance", pending_action_id=pid)["error"] == "not_confirmed"
    assert not posts(up)


def test_confirmed_next_turn_files_exactly_once(up):
    gw = make_gateway(up)
    pid = propose(gw)
    gw.on_rep_turn("Yes, I read it back and the member agrees. Go ahead and file it.")
    first = call(gw, "file_grievance", pending_action_id=pid)
    again = call(gw, "file_grievance", pending_action_id=pid)
    assert first["ok"] and first["data"]["grievance_id"] == again["data"]["grievance_id"]
    assert len(posts(up)) == 1 and len(up.grievances["tenant_x"]) == 1


def test_hold_off_cancels_and_nothing_is_written(up):
    gw = make_gateway(up)
    pid = propose(gw)
    gw.on_rep_turn("Actually, hold off. The member wants to call the provider first.")
    assert call(gw, "file_grievance", pending_action_id=pid)["error"] == "no_such_proposal"
    assert not posts(up)


def test_explicit_ui_confirmation_must_match_the_pending_id(up):
    gw = make_gateway(up)
    pid = propose(gw)
    gw.on_rep_turn("ok", confirm_action_id="someotherid")
    assert call(gw, "file_grievance", pending_action_id=pid)["error"] == "no_such_proposal"
    pid = propose(gw)
    gw.on_rep_turn("", confirm_action_id=pid)
    assert call(gw, "file_grievance", pending_action_id=pid)["ok"]


def test_503_after_write_is_retried_with_same_key_and_bytes_giving_one_grievance(up):
    # The system of record stores the grievance but answers 503 (a "ghost write").
    up.fail_next.append(("POST", "/grievances", 503, {}, True))
    gw = make_gateway(up)
    pid = propose(gw)
    gw.on_rep_turn("Confirmed. File it.")
    out = call(gw, "file_grievance", pending_action_id=pid)
    assert out["ok"]
    p = posts(up)
    assert len(p) == 2
    assert p[0].headers["Idempotency-Key"] == p[1].headers["Idempotency-Key"] and p[0].content == p[1].content
    assert len(up.grievances["tenant_x"]) == 1


def test_persistent_503_reconciles_from_grievances_on_file(up):
    # Every POST answers 503, but the first one did store the grievance: reconcile must find it.
    up.fail_next += [("POST", "/grievances", 503, {}, True)] + [("POST", "/grievances", 503, {}, False)] * 5
    gw = make_gateway(up)
    pid = propose(gw)
    gw.on_rep_turn("Yes, file it.")
    out = call(gw, "file_grievance", pending_action_id=pid)
    assert out["ok"] and out["data"]["grievance_id"] == up.grievances["tenant_x"][0]["grievance_id"]
    assert len(up.grievances["tenant_x"]) == 1


def test_unknown_outcome_blocks_new_proposals_and_retry_uses_same_key(up):
    gw = make_gateway(up)
    pid = propose(gw)
    gw.on_rep_turn("Yes, file it.")
    up.fail_next += [("POST", "/grievances", 503, {}, False)] * 3  # nothing stored, nothing confirmed
    assert call(gw, "file_grievance", pending_action_id=pid)["error"] == "filing_outcome_unknown"
    assert call(gw, "propose_grievance", category="billing", description="another")["error"] == \
        "filing_outcome_unknown"
    assert not up.grievances["tenant_x"]
    p = gw.grievances.retry_unconfirmed()
    assert p.status == "filed" and len(up.grievances["tenant_x"]) == 1
    keys = {r.headers["Idempotency-Key"] for r in posts(up)}
    assert len(keys) == 1


def test_grievance_validation_rejects_phi_and_bad_category(up):
    gw = make_gateway(up)
    gw.on_rep_turn("file a grievance")
    assert call(gw, "propose_grievance", category="billing",
                description=f"Member SSN {MEMBER_X['ssn']} was billed")["error"] == "phi_in_description"
    assert call(gw, "propose_grievance", category="refund", description="x")["error"] == "invalid_arguments"


@pytest.mark.parametrize("text,expected", [
    ("Yes, I read it back to the member and the member agrees. Go ahead and file it.", True),
    ("Confirmed. The member agreed to the summary. File it.", True),
    ("Actually, hold off. The member wants to call the provider first.", False),
    ("Yes but change the category first", False),
    ("What is the copay for urgent care?", False),
    ("", False),
])
def test_confirmation_classifier_is_conservative(text, expected):
    assert is_confirmation(text) is expected
