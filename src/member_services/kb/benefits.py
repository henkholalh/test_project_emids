"""Structured views of plan documents: benefit design per product and year, and the plan's denial-code guide.

The numbers used in calculations (deductible, out-of-pocket maximum, visit limits) are parsed from the tables,
so "how much is left on the deductible" is computed exactly instead of being left to the model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .documents import Document, table_rows

_MONEY_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")


class PlanDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class BenefitRow:
    benefit: str
    in_network: str
    out_of_network: str


@dataclass(frozen=True)
class PlanDesign:
    tenant: str
    product_code: str
    product: str
    plan_year: int
    doc_id: str
    deductible_individual: float
    deductible_family: float
    oop_max_individual: float
    oop_max_family: float
    pt_visit_limit: int | None
    rows: tuple[BenefitRow, ...]


def _money_pair(cell: str) -> tuple[float, float]:
    vals = [float(v.replace(",", "")) for v in _MONEY_RE.findall(cell)]
    if len(vals) != 2:
        raise PlanDocumentError(f"expected 'individual / family' amounts, got '{cell}'")
    return vals[0], vals[1]


def is_benefit_summary(doc: Document) -> bool:
    return doc.product_code != "ALL" and "summary of benefits" in doc.title.lower()


def parse_plan_design(doc: Document) -> PlanDesign:
    rows = [r for r in table_rows(doc.body) if len(r) >= 3 and r[0].lower() != "benefit"]
    by_name = {r[0].lower(): r for r in rows}
    try:
        ded = _money_pair(by_name["deductible (individual / family)"][1])
        oop = _money_pair(by_name["out-of-pocket maximum (individual / family)"][1])
    except KeyError as exc:
        raise PlanDocumentError(f"{doc.doc_id}: missing row {exc}") from None
    pt_limit = None
    pt_row = by_name.get("physical therapy visit limit")
    if pt_row:
        m = re.search(r"(\d+)\s+visits", pt_row[1])
        pt_limit = int(m.group(1)) if m else None
    return PlanDesign(tenant=doc.tenant, product_code=doc.product_code, product=doc.product,
                      plan_year=doc.plan_year, doc_id=doc.doc_id, deductible_individual=ded[0],
                      deductible_family=ded[1], oop_max_individual=oop[0], oop_max_family=oop[1],
                      pt_visit_limit=pt_limit, rows=tuple(BenefitRow(r[0], r[1], r[2]) for r in rows))


@dataclass(frozen=True)
class DenialCode:
    carc: str
    description: str
    what_to_tell_member: str
    doc_id: str


def parse_denial_codes(doc: Document) -> dict[str, DenialCode]:
    out = {}
    for r in table_rows(doc.body):
        if len(r) >= 3 and re.match(r"^[A-Z]{2}-\d+$", r[0]):
            out[r[0]] = DenialCode(r[0], r[1], r[2], doc.doc_id)
    return out
