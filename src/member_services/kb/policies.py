"""Tenant rules the gateway computes with, read from each tenant's member services guide.

Deadlines are calculated in code, not by the model: a wrong date on an appeal or grievance deadline is a
compliance error, and date arithmetic is exactly what models get wrong. The numbers come from the tenant's own
document, so a plan that changes its timeframe only changes its document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .documents import Document, table_rows


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class TenantPolicy:
    tenant: str
    grievance_resolution_days: int
    appeal_window_days: int
    source_doc: str

    def grievance_deadline(self, received: str) -> date:
        return date.fromisoformat(received) + timedelta(days=self.grievance_resolution_days)

    def appeal_deadline(self, processed: str) -> date:
        return date.fromisoformat(processed) + timedelta(days=self.appeal_window_days)


def _days(rows: list[list[str]], topic: str) -> int:
    for row in rows:
        if len(row) >= 2 and row[0].lower() == topic.lower():
            m = re.search(r"(\d+)\s+calendar days", row[1])
            if m:
                return int(m.group(1))
    raise PolicyError(f"'{topic}' not found in member services guide")


def policy_from_documents(tenant: str, docs: list[Document]) -> TenantPolicy:
    guides = [d for d in docs if d.tenant == tenant and "member services guide" in d.title.lower()]
    if len(guides) != 1:
        raise PolicyError(f"expected one member services guide for {tenant}, found {len(guides)}")
    rows = table_rows(guides[0].body)
    return TenantPolicy(tenant=tenant,
                        grievance_resolution_days=_days(rows, "Grievance resolution timeframe"),
                        appeal_window_days=_days(rows, "Claim appeal filing window"),
                        source_doc=guides[0].doc_id)
