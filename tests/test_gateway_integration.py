"""Gateway against the real supplied mock and KB (skipped when the pack or tokens are not available).

Member ids and expected values are read from the pack's own eval file at run time, so no supplied data is
copied into this repository.
"""
import json

import pytest

from member_services.config import Settings
from member_services.context import SessionContext
from member_services.gateway.factory import build_gateway
from member_services.kb.documents import LoadReport, load_tenant_documents
from member_services.kb.policies import policy_from_documents
from member_services.llm.base import ToolCall

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(mock_url, tmp_path):
    s = Settings()
    if not s.tenant_tokens or s.directory_api_key is None:
        pytest.skip("TENANT_TOKENS / DIRECTORY_API_KEY not configured")
    return s.model_copy(update={"mock_base_url": mock_url, "trace_dir": tmp_path})


@pytest.fixture(scope="session")
def cases(pack_dir):
    return {c["id"]: c for c in json.loads((pack_dir / "eval" / "conversations.json").read_text())["cases"]}


def find_id(text, prefix):
    return next(w.strip(".,?!()") for w in text.split() if w.strip(".,?!()").startswith(prefix))


def gateway_for(settings, case, tenant=None):
    return build_gateway(settings, SessionContext(tenant or case["tenant"], case["member_id"]))


def run(gw, name, **args):
    return json.loads(gw.execute(ToolCall(id="t", name=name, arguments=args)).content)


def test_kb_misfiled_document_is_quarantined_and_policies_load(pack_dir):
    report = LoadReport()
    a = load_tenant_documents(pack_dir / "kb", "plan_a", report)
    b = load_tenant_documents(pack_dir / "kb", "plan_b", report)
    assert all(d.tenant == "plan_a" for d in a) and all(d.tenant == "plan_b" for d in b)
    assert any("does not match folder" in reason for _, reason in report.quarantined)
    assert policy_from_documents("plan_a", a).grievance_resolution_days != \
        policy_from_documents("plan_b", b).grievance_resolution_days


@pytest.mark.parametrize("case_id", ["EV-21", "EV-39"])
def test_member_from_other_plan_is_not_found_and_nothing_is_returned(settings, cases, case_id):
    gw = gateway_for(settings, cases[case_id])
    assert gw.member_found is False
    assert run(gw, "list_claims")["error"] == "not_found_in_plan"


def test_member_id_present_in_both_tenants_resolves_per_tenant(settings, cases):
    case = cases["EV-30"]
    own = run(gateway_for(settings, case), "get_member_profile")["data"]
    other_tenant = "plan_a" if case["tenant"] == "plan_b" else "plan_b"
    other = run(gateway_for(settings, case, other_tenant), "get_member_profile")["data"]
    assert own["plan_name"] == case["expected"]["facts"][0]
    assert other["plan_name"] != own["plan_name"] and other["network_id"] != own["network_id"]


def test_all_claim_pages_are_read_and_counts_match(settings, cases):
    case = cases["EV-12"]
    data = run(gateway_for(settings, case), "list_claims", service_date_from="2026-01-01",
               service_date_to="2026-12-31", status="DENIED")["data"]
    assert str(data["count"]) == case["expected"]["facts"][0]


@pytest.mark.parametrize("case_id", ["EV-14", "EV-40"])
def test_appeal_window_is_computed_from_tenant_policy(settings, cases, case_id):
    case = cases[case_id]
    claim_id = find_id(case["turns"][0]["text"], "C2")
    appeal = run(gateway_for(settings, case), "get_claim", claim_id=claim_id)["data"]["appeal"]
    expect_open, expect_date = case["expected"]["facts"]
    assert appeal["deadline"] == expect_date and appeal["window_open"] == (expect_open == "yes")


@pytest.mark.parametrize("case_id", ["EV-15", "EV-36"])
def test_grievance_deadline_is_computed_from_tenant_policy(settings, cases, case_id):
    case = cases[case_id]
    gid = find_id(case["turns"][0]["text"], "G26-")
    gs = run(gateway_for(settings, case), "list_grievances")["data"]["grievances"]
    g = next(g for g in gs if g["grievance_id"] == gid)
    expect_past, expect_date = case["expected"]["facts"]
    assert g["resolution_deadline"] == expect_date and g["past_deadline"] == (expect_past == "yes")


def test_directory_returns_in_network_providers_near_member(settings, cases):
    case = cases["EV-20"]
    names = [p["name"] for p in run(gateway_for(settings, case), "find_providers",
                                     specialty="Dermatology")["data"]["providers"]]
    assert sorted(names) == sorted(case["expected"]["facts"])


def test_ssn_last4_check(settings, cases):
    case = cases["EV-18"]
    digits = next(w.strip(".") for w in case["turns"][0]["text"].split() if w.strip(".").isdigit())
    assert run(gateway_for(settings, case), "verify_ssn_last4", last4=digits)["data"]["result"] == "match"


@pytest.mark.parametrize("case_id", ["EV-16", "EV-32"])
def test_confirmed_grievance_is_filed_exactly_once_despite_unacknowledged_write(settings, cases, case_id):
    case = cases[case_id]
    gw = gateway_for(settings, case)
    before = len(run(gw, "list_grievances")["data"]["grievances"])
    gw.on_rep_turn(case["turns"][0]["text"])
    proposal = run(gw, "propose_grievance", category="billing",
                   description="Provider billed the member the full amount for a visit the plan already paid.")
    gw.on_rep_turn(case["turns"][1]["text"])
    filed = run(gw, "file_grievance", pending_action_id=proposal["data"]["pending_action_id"])
    assert filed["ok"], filed
    assert gw.api.retries >= 1  # the system of record did not acknowledge the first attempt
    after = run(gateway_for(settings, case), "list_grievances")["data"]["grievances"]
    assert len(after) == before + 1
    assert [g["category"] for g in after if g["grievance_id"] == filed["data"]["grievance_id"]] == \
        case["expected"]["facts"]


def test_hold_off_files_nothing(settings, cases):
    case = cases["EV-17"]
    gw = gateway_for(settings, case)
    before = len(run(gw, "list_grievances")["data"]["grievances"])
    gw.on_rep_turn(case["turns"][0]["text"])
    pid = run(gw, "propose_grievance", category="billing", description="Member disputes an X-ray bill.")[
        "data"]["pending_action_id"]
    gw.on_rep_turn(case["turns"][1]["text"])
    assert run(gw, "file_grievance", pending_action_id=pid)["ok"] is False
    assert len(run(gateway_for(settings, case), "list_grievances")["data"]["grievances"]) == before


def test_traces_from_real_data_contain_no_member_phi(settings, cases, tmp_path):
    case = cases["EV-13"]
    gw = gateway_for(settings, case)
    for name in ("get_member_profile", "get_benefits", "list_claims", "list_grievances"):
        run(gw, name)
    member = gw._member
    trace = (tmp_path / f"{gw.ctx.conversation_id}.jsonl").read_text()
    for field in ("ssn", "dob", "phone", "email", "first_name", "last_name"):
        assert member[field] not in trace
    assert member["member_id"] not in trace
