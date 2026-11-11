"""Retrieval and plan-design parsing on invented fixture documents (no supplied data)."""
import json

import pytest

from fakes import FIXTURE_KB, KNOWLEDGE, FakeUpstream, claim, make_gateway
from member_services.kb.documents import LoadReport, load_tenant_documents
from member_services.kb.retrieval import TenantIndex, expand
from member_services.llm.base import ToolCall


def run(gw, name, **args):
    return json.loads(gw.execute(ToolCall(id="t", name=name, arguments=args)).content)


def test_misfiled_document_is_quarantined_and_never_indexed():
    k = KNOWLEDGE["tenant_x"]
    assert k.report.quarantined and "does not match folder" in k.report.quarantined[0][1]
    assert all(c.tenant == "tenant_x" for c in k.index.chunks)
    assert not any("$0 copay" in c.text for c in k.index.chunks)


def test_index_refuses_another_tenants_document():
    docs = load_tenant_documents(FIXTURE_KB, "tenant_y", LoadReport())
    with pytest.raises(ValueError, match="belongs to tenant_y"):
        TenantIndex("tenant_x", docs)


def test_plan_design_numbers_are_parsed():
    p = KNOWLEDGE["tenant_x"].plan("GOLD", 2026)
    assert (p.deductible_individual, p.deductible_family, p.oop_max_individual, p.pt_visit_limit) == \
        (1200.0, 2400.0, 5000.0, 25)
    assert KNOWLEDGE["tenant_x"].plan("GOLD", 2027).pt_visit_limit == 22


def test_denial_codes_are_parsed():
    assert "appeal" in KNOWLEDGE["tenant_x"].denial_codes["CO-197"].what_to_tell_member


@pytest.mark.parametrize("year,expected", [(2026, "$45 copay"), (2027, "$55 copay")])
def test_search_is_filtered_to_product_and_year_before_ranking(year, expected):
    hits = KNOWLEDGE["tenant_x"].index.search("specialist visit", product_code="GOLD", plan_year=year)
    assert expected in hits[0].chunk.text
    assert all(h.chunk.product_code in ("GOLD", "ALL") for h in hits)
    assert all(h.chunk.plan_year == year or h.chunk.product_code == "ALL" for h in hits)


def test_other_products_rows_are_never_returned():
    hits = KNOWLEDGE["tenant_y"].index.search("specialist visit", product_code="OTHER", plan_year=2026)
    assert all(h.chunk.product_code == "ALL" for h in hits)


def test_synonyms_map_rep_phrasing_to_plan_wording():
    assert "telehealth" in expand("video visit")
    hits = KNOWLEDGE["tenant_x"].index.search("video visit with a doctor", product_code="GOLD", plan_year=2026)
    assert "Telehealth" in hits[0].chunk.text


def test_virtual_behavioral_health_uses_own_plans_row():
    hits = KNOWLEDGE["tenant_x"].index.search("virtual behavioral health session", product_code="GOLD",
                                              plan_year=2026)
    assert "$30 copay" in hits[0].chunk.text


# ---- through the gateway ---------------------------------------------------------------
@pytest.fixture
def up():
    u = FakeUpstream()
    u.claims["tenant_x"] = {"M100001": [
        claim("C1000000002", "2026-09-01", status="DENIED", processed="2026-06-01",
              denial={"carc": "CO-197", "description": "Precertification absent."})]}
    return u


def test_benefits_include_computed_remaining_amounts(up):
    r = run(make_gateway(up), "get_benefits")["data"]["remaining"]
    assert r["deductible_remaining_individual"] == 950.0  # 1,200 - 250 met
    assert r["out_of_pocket_remaining_individual"] == 4100.0  # 5,000 - 900 met
    assert r["physical_therapy_visits_remaining"] == 21  # 25 - 4 used
    assert r["plan_year"] == 2026 and r["source"] == "tenant_x/gold_2026"


def test_denied_claim_carries_plans_own_guidance(up):
    c = run(make_gateway(up), "get_claim", claim_id="C1000000002")["data"]
    assert "Prior authorization was not on file" in c["denial"]["what_to_tell_member"]


def test_search_tool_uses_members_product_and_cites_sources(up):
    out = run(make_gateway(up), "search_plan_documents", query="specialist visit", plan_year=2027)["data"]
    assert out["product_code"] == "GOLD" and "$55 copay" in out["results"][0]["text"]
    assert all(r["source"].startswith("tenant_x/") for r in out["results"])


def test_search_tool_reports_missing_year(up):
    out = run(make_gateway(up), "search_plan_documents", query="specialist", plan_year=2031)["data"]
    assert out["results"] == [] and "2026" in out["message"]


def test_quarantine_is_recorded_in_trace(up):
    gw = make_gateway(up)
    assert any(e["name"] == "kb_quarantine" for e in gw.tracer.events)
