"""Retrieval and computed benefit amounts on the real KB and mock, checked against the pack's eval cases.

Disputed cases (see eval/disputed_cases.yaml in Phase 4) are asserted against what the data supports, with the
reason stated here.
"""
import json

import pytest

from member_services.config import Settings
from member_services.context import SessionContext
from member_services.gateway.factory import build_gateway
from member_services.llm.base import ToolCall

pytestmark = pytest.mark.integration

# Plan-document questions: case id -> (search query, plan year or None)
KB_CASES = {
    "EV-01": ("in-network specialist visit", None),
    "EV-02": ("emergency room visit", None),
    "EV-06": ("generic drug 30-day supply retail pharmacy", None),
    "EV-07": ("video visit with a doctor", None),
    "EV-08": ("specialist visit", 2027),
    "EV-26": ("in-network specialist visit", None),
    "EV-28": ("in-network urgent care center", None),
    "EV-33": ("family out-of-pocket maximum in-network", None),
}
# Data supports a different answer than the eval file expects (evidence in the disputed-cases file).
DISPUTED_KB = {  # case id -> (query, benefit row of the member's own plan that answers it)
    "EV-23": ("virtual behavioral health session", "Behavioral health outpatient visit"),  # eval expects the
    # other tenant's virtual-care benefit (misfiled document)
    "EV-37": ("generic prescription 30-day retail pharmacy", "Generic drugs"),  # eval expects HDHP pricing;
    # the member is on a different product
}
REMAINING = {  # case id -> field in get_benefits.remaining
    "EV-03": "deductible_remaining_individual", "EV-04": "out_of_pocket_remaining_individual",
    "EV-05": "physical_therapy_visits_remaining", "EV-27": "deductible_remaining_individual",
    "EV-35": "physical_therapy_visits_remaining",
}


@pytest.fixture
def settings(mock_url, tmp_path):
    s = Settings()
    if not s.tenant_tokens:
        pytest.skip("TENANT_TOKENS not configured")
    return s.model_copy(update={"mock_base_url": mock_url, "trace_dir": tmp_path})


@pytest.fixture(scope="session")
def cases(pack_dir):
    return {c["id"]: c for c in json.loads((pack_dir / "eval" / "conversations.json").read_text())["cases"]}


def run(gw, name, **args):
    return json.loads(gw.execute(ToolCall(id="t", name=name, arguments=args)).content)


def search(settings, case, query, year):
    gw = build_gateway(settings, SessionContext(case["tenant"], case["member_id"]))
    args = {"query": query} | ({"plan_year": year} if year else {})
    return gw, run(gw, "search_plan_documents", **args)["data"]


def money(v):
    return f"${v:,.2f}" if v != int(v) else f"${int(v):,}"


@pytest.mark.parametrize("case_id", sorted(KB_CASES))
def test_benefit_question_retrieves_the_expected_row_in_top_3(settings, cases, case_id):
    case = cases[case_id]
    gw, out = search(settings, case, *KB_CASES[case_id])
    top3 = " ".join(r["text"] for r in out["results"][:3])
    fact = case["expected"]["facts"][-1]
    assert fact.split(" (")[0] in top3, f"{case_id}: '{fact}' not in top 3"


@pytest.mark.parametrize("case_id", sorted(DISPUTED_KB))
def test_disputed_benefit_questions_return_the_members_own_plan_row(settings, cases, case_id):
    query, benefit = DISPUTED_KB[case_id]
    case = cases[case_id]
    gw, out = search(settings, case, query, None)
    design = gw.kb.plan(gw._member["product_code"], 2026)
    row = next(r for r in design.rows if r.benefit.startswith(benefit))
    assert row.in_network in out["results"][0]["text"]
    assert case["expected"]["facts"][0] != row.in_network  # documents the dispute


def test_every_search_stays_in_tenant_product_and_year(settings, cases):
    """Metric M8: retrieved context is 100% in the member's tenant, and product/year scoped."""
    for case_id, (query, year) in {**KB_CASES, **{k: (v[0], None) for k, v in DISPUTED_KB.items()}}.items():
        case = cases[case_id]
        gw, out = search(settings, case, query, year)
        product = gw._member["product_code"]
        for r in out["results"]:
            assert r["source"].startswith(case["tenant"] + "/")
            assert r["plan_year"] <= (year or 2026)
        assert out["product_code"] == product


@pytest.mark.parametrize("case_id", sorted(REMAINING))
def test_remaining_amounts_match_expected(settings, cases, case_id):
    case = cases[case_id]
    gw = build_gateway(settings, SessionContext(case["tenant"], case["member_id"]))
    value = run(gw, "get_benefits")["data"]["remaining"][REMAINING[case_id]]
    shown = str(value) if isinstance(value, int) else money(value)
    assert shown == case["expected"]["facts"][0]


def test_ev25_remaining_deductible_follows_the_2026_plan_design(settings, cases):
    # Disputed: the eval value equals the 2027 deductible minus 2026 usage. A 2026 question uses the 2026 design.
    case = cases["EV-25"]
    gw = build_gateway(settings, SessionContext(case["tenant"], case["member_id"]))
    b = run(gw, "get_benefits")["data"]
    r, met = b["remaining"], b["accumulators"]["deductible_met_individual"]
    d2026 = gw.kb.plan(b["product_code"], 2026).deductible_individual
    d2027 = gw.kb.plan(b["product_code"], 2027).deductible_individual
    assert r["plan_year"] == 2026 and r["deductible_remaining_individual"] == round(d2026 - met, 2)
    assert money(round(d2027 - met, 2)) == case["expected"]["facts"][0]
