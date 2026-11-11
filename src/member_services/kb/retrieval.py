"""Retrieval over one tenant's plan documents.

Why this design (ADR "retrieval"):
- **One index per tenant.** An index object is built from one tenant's documents and checks every chunk's tenant
  on the way in. There is no shared index that a missing filter could leak across (NFR-4).
- **Filter first, rank second.** Candidates are restricted to the member's product (or ``ALL``) and the requested
  plan year *before* scoring, so a better-matching row from the wrong product or year can never win.
- **Lexical ranking over table rows.** The KB is a small set of structured tables where answers are exact strings
  ("$40 copay"). BM25 over rows, with a short synonym list for rep phrasing, is precise, explainable and free to
  run. Production can swap in pgvector embeddings behind the same interface and keep the same filters.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .documents import Document, table_rows

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "for", "to", "in", "is", "what", "does", "do", "this", "that", "member", "members",
         "would", "will", "pay", "cost", "costs", "how", "much", "and", "or", "with", "at", "on", "per", "my",
         "their", "they", "s", "be", "it", "after", "before"}

# Rep phrasing -> plan document wording. Kept small and reviewable.
SYNONYMS = {
    "video": ["telehealth"], "virtual": ["telehealth", "virtual"], "telemedicine": ["telehealth"],
    "er": ["emergency", "room"], "emergency": ["emergency", "room"],
    "generic": ["generic", "tier", "1"], "brand": ["brand", "tier"], "prescription": ["drugs"], "rx": ["drugs"],
    "drug": ["drugs"], "medication": ["drugs"], "pharmacy": ["retail"],
    "pcp": ["primary", "care"], "doctor": ["primary", "care", "visit"], "checkup": ["preventive"],
    "specialist": ["specialist"], "therapy": ["therapy"], "pt": ["physical", "therapy"],
    "mental": ["behavioral", "health"], "counseling": ["behavioral", "health"], "psychotherapy": ["behavioral"],
    "mri": ["advanced", "imaging"], "ct": ["advanced", "imaging"], "scan": ["imaging"],
    "hospital": ["inpatient", "hospital"], "surgery": ["surgery"], "oop": ["out", "pocket", "maximum"],
}


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN_RE.findall(text.lower()):
        if t in _STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def expand(query: str) -> list[str]:
    q = tokens(query)
    extra = [s for t in q for s in SYNONYMS.get(t, [])]
    return q + tokens(" ".join(extra))


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    tenant: str
    product_code: str
    product: str
    plan_year: int
    title: str
    text: str
    kind: str  # table_row | section


def chunk_document(doc: Document) -> list[Chunk]:
    chunks: list[Chunk] = []
    header: list[str] | None = None
    section = doc.title
    buf: list[str] = []

    def flush():
        if buf:
            chunks.append(Chunk(f"{doc.doc_id}#{len(chunks)}", doc.doc_id, doc.tenant, doc.product_code, doc.product,
                                doc.plan_year, doc.title, f"{section}: " + " ".join(buf), "section"))
            buf.clear()

    for line in doc.body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            flush()
            section = s.lstrip("#").strip()
            header = None
        elif s.startswith("|"):
            flush()
            cells = table_rows(s)
            if not cells:
                continue  # separator row
            if header is None:
                header = cells[0]
                continue
            row = cells[0]
            text = " | ".join(f"{h}: {v}" for h, v in zip(header, row))
            chunks.append(Chunk(f"{doc.doc_id}#{len(chunks)}", doc.doc_id, doc.tenant, doc.product_code,
                                doc.product, doc.plan_year, doc.title, f"{section}. {text}", "table_row"))
        elif s:
            header = None
            buf.append(s.lstrip("- ").strip())
        else:
            header = None
            flush()
    flush()
    return chunks


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


class TenantIndex:
    def __init__(self, tenant: str, documents: list[Document]):
        self.tenant = tenant
        self.chunks: list[Chunk] = []
        for d in documents:
            if d.tenant != tenant:
                raise ValueError(f"document {d.doc_id} belongs to {d.tenant}, not {tenant}")
            self.chunks.extend(chunk_document(d))
        self._tf = [Counter(tokens(c.text)) for c in self.chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def years(self, product_code: str) -> list[int]:
        return sorted({c.plan_year for c in self.chunks if c.product_code == product_code})

    def _eligible(self, c: Chunk, product_code: str, plan_year: int) -> bool:
        if c.product_code == product_code:
            return c.plan_year == plan_year
        if c.product_code == "ALL":
            # Plan-wide documents (guides, claim codes) stay in force for later years until replaced.
            return c.plan_year <= plan_year
        return False

    def search(self, query: str, *, product_code: str, plan_year: int, k: int = 5) -> list[Hit]:
        q = expand(query)
        hits = []
        for i, c in enumerate(self.chunks):
            assert c.tenant == self.tenant  # invariant, enforced at build time
            if not self._eligible(c, product_code, plan_year):
                continue
            tf, length = self._tf[i], self._len[i]
            score = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    score += self._idf.get(t, 0) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * length / self._avg))
            if score > 0:
                hits.append(Hit(c, score))
        hits.sort(key=lambda h: (-h.score, h.chunk.chunk_id))
        return hits[:k]
