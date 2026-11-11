"""Loading plan documents from ``PACK_DIR/kb/<tenant>/``.

Each document starts with YAML front-matter (tenant, plan_year, effective_date, product_code, product). A document
is only accepted for a tenant when **both** its folder and its front-matter name that tenant. A mismatch means the
document is misfiled; it is quarantined (not loaded for either tenant) and reported, because loading it would put
one client's content in front of another client's reps (NFR-4).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass(frozen=True)
class Document:
    doc_id: str
    tenant: str
    plan_year: int
    product_code: str
    product: str
    effective_date: str
    title: str
    body: str
    path: Path


@dataclass
class LoadReport:
    loaded: list[str] = field(default_factory=list)
    quarantined: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)


def parse_document(path: Path, folder_tenant: str) -> Document:
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    if not m:
        raise ValueError("missing front-matter")
    fm = yaml.safe_load(m.group(1)) or {}
    for key in ("tenant", "plan_year", "product_code", "product", "effective_date"):
        if key not in fm:
            raise ValueError(f"front-matter missing '{key}'")
    if str(fm["tenant"]) != folder_tenant:
        raise ValueError(f"front-matter tenant '{fm['tenant']}' does not match folder '{folder_tenant}'")
    body = text[m.end():]
    title_m = re.search(r"^#\s+(.+)$", body, re.M)
    return Document(doc_id=f"{folder_tenant}/{path.stem}", tenant=folder_tenant, plan_year=int(fm["plan_year"]),
                    product_code=str(fm["product_code"]), product=str(fm["product"]),
                    effective_date=str(fm["effective_date"]), title=title_m.group(1).strip() if title_m else path.stem,
                    body=body, path=path)


def load_tenant_documents(kb_dir: Path, tenant: str, report: LoadReport | None = None) -> list[Document]:
    report = report if report is not None else LoadReport()
    docs = []
    for path in sorted((kb_dir / tenant).glob("*.md")):
        try:
            doc = parse_document(path, tenant)
        except ValueError as exc:
            report.quarantined.append((str(path.relative_to(kb_dir)), str(exc)))
            log.warning("quarantined KB document %s: %s", path.name, exc)
            continue
        docs.append(doc)
        report.loaded.append(doc.doc_id)
    return docs


def table_rows(body: str) -> list[list[str]]:
    """Markdown table rows as lists of cell strings (header separator rows skipped)."""
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("|") and line.endswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows
