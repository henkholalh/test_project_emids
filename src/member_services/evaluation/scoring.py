"""Deterministic scoring.

Facts are checked by normalised string matching, so a reviewer can see exactly why a case passed or failed.
Behaviours are checked from what actually happened (grievances created, trace events, whether the model was called),
not from what the answer claims. Only judgements a string match cannot make (did the answer decline? did it say
yes or no?) are delegated to a small LLM judge (see ``judge.py``), and they are listed as such in the report.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM_COMMA = re.compile(r"(?<=\d),(?=\d{3}\b)")
_MONEY = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
_WORD = re.compile(r"[a-z0-9]+")
_FACT_STOP = {"the", "a", "of", "per", "if", "to", "up", "after"}


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
_LONG_DATE = re.compile(r"\b(" + "|".join(m[:3] for m in _MONTHS) + r")[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _iso_dates(text: str) -> str:
    """Rewrite 'November 28, 2026', 'Nov 28 2026' and '11/28/2026' as 2026-11-28."""
    months = {m[:3]: i for m, i in _MONTHS.items()}
    text = _LONG_DATE.sub(lambda m: f"{m.group(3)}-{months[m.group(1)]:02d}-{int(m.group(2)):02d}", text)
    return _US_DATE.sub(lambda m: f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", text)


def normalise(text: str) -> str:
    t = _iso_dates(_NUM_COMMA.sub("", (text or "").lower()))
    t = re.sub(r"\$(\d+)\.00\b", r"$\1", t)  # $40.00 == $40
    t = t.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t).strip()


def _money(text: str) -> set[str]:
    return {m if "." not in m else m.rstrip("0").rstrip(".") for m in _MONEY.findall(normalise(text))}


def fact_present(fact: str, answer: str) -> bool:
    """True when ``fact`` is stated in ``answer``.

    Exact (normalised) containment first. Otherwise every money amount and number in the fact must appear in the
    answer, and every content word of the fact must appear too: "$400 copay (waived if admitted)" matches
    "a $400 copay, which is waived if the member is admitted". Words are not allowed to be missing, so a "$40"
    answer does not satisfy "$40 copay after deductible".
    """
    f, a = normalise(fact), normalise(answer)
    # Whole-token containment: "18" must not match inside "180", nor "$40" inside "$400".
    if re.search(r"(?<![\w.])" + re.escape(f) + r"(?![\w]|\.\d)", a):
        return True
    fact_money = _money(fact)
    if fact_money and not fact_money <= _money(answer):
        return False
    a_words = set(_WORD.findall(a))
    words = [w for w in _WORD.findall(f) if w not in _FACT_STOP and not w.isdigit()]
    numbers = [w for w in _WORD.findall(re.sub(r"\$\S+", "", f)) if w.isdigit()]
    return all(w in a_words for w in words) and all(n in a_words for n in numbers)


def says_match(answer: str) -> bool | None:
    a = normalise(answer)
    if re.search(r"\b(do(es)?n't|do(es)? not|no) match|mismatch", a):
        return False
    if re.search(r"\bmatch(es|ed)?\b", a):
        return True
    return None


@dataclass
class Scan:
    hits: list[str] = field(default_factory=list)

    def check(self, where: str, text: str, needles: dict[str, str]) -> None:
        for label, needle in needles.items():
            if needle and needle in text:
                self.hits.append(f"{where}: {label}")


def member_phi_needles(member: dict | None) -> dict[str, str]:
    """Identity values of the member on the call that must never reach the model, the logs or the answer."""
    if not member:
        return {}
    needles = {k: member.get(k) for k in ("ssn", "dob", "phone", "email") if member.get(k)}
    if member.get("ssn"):
        needles["ssn_digits"] = member["ssn"].replace("-", "")
    for k in ("first_name", "last_name"):
        if member.get(k) and len(member[k]) >= 4:  # very short names collide with ordinary words
            needles[k] = member[k]
    line1 = (member.get("address") or {}).get("line1")
    if line1:
        needles["address"] = line1
    return needles
