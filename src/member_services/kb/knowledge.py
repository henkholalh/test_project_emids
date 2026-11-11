"""Everything the gateway knows about one tenant's plans, loaded once and shared read-only across sessions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .benefits import DenialCode, PlanDesign, is_benefit_summary, parse_denial_codes, parse_plan_design
from .documents import LoadReport, load_tenant_documents
from .policies import TenantPolicy, policy_from_documents
from .retrieval import TenantIndex


@dataclass
class TenantKnowledge:
    tenant: str
    policy: TenantPolicy
    index: TenantIndex
    plans: dict[tuple[str, int], PlanDesign] = field(default_factory=dict)
    denial_codes: dict[str, DenialCode] = field(default_factory=dict)
    report: LoadReport = field(default_factory=LoadReport)

    def plan(self, product_code: str, plan_year: int) -> PlanDesign | None:
        return self.plans.get((product_code, plan_year))


def load_knowledge(kb_dir: Path, tenant: str) -> TenantKnowledge:
    report = LoadReport()
    docs = load_tenant_documents(kb_dir, tenant, report)
    plans, codes = {}, {}
    for d in docs:
        if is_benefit_summary(d):
            p = parse_plan_design(d)
            plans[(p.product_code, p.plan_year)] = p
        elif "denial codes" in d.title.lower():
            codes.update(parse_denial_codes(d))
    return TenantKnowledge(tenant=tenant, policy=policy_from_documents(tenant, docs), index=TenantIndex(tenant, docs),
                           plans=plans, denial_codes=codes, report=report)
