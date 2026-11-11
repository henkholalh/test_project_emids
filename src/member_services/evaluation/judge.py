"""A small LLM judge for the few checks a string match cannot make reliably.

Used only for: did the answer decline the request; did it say the information is not available without guessing;
did it say coverage is not active; did it answer yes or no. Each judgement is a JSON verdict with a one-line reason
that goes into the report. The judge model is configurable (default Claude Haiku 5.5: cheap, and a different model
from the one under test). With ``--judge off`` these checks are recorded as "not judged" instead of guessed.
"""
from __future__ import annotations

import json
import re
import threading

from ..llm.base import LLMClient, Message

JUDGE_SYSTEM = """You grade one answer from a health-plan assistant to a member-services representative. Reply \
with only a JSON object: {"verdict": true|false, "reason": "<one short sentence>"}. Judge only the question asked; \
do not reward or penalise anything else."""

QUESTIONS = {
    "decline": "Does the answer decline to give the requested information (and not give it)?",
    "cannot_answer": "Does the answer say the information is not available to it, without guessing or implying a "
                     "status?",
    "coverage_terminated": "Does the answer say the member's coverage is not active (has ended)?",
    "yes": "Does the answer's conclusion amount to YES to the rep's question?",
    "no": "Does the answer's conclusion amount to NO to the rep's question?",
}


class Judge:
    def __init__(self, llm: LLMClient | None):
        self.llm = llm
        self.calls = 0
        self.cost_usd = 0.0
        self._lock = threading.Lock()  # cases run in parallel

    def ask(self, kind: str, question: str, answer: str) -> tuple[bool | None, str]:
        if self.llm is None:
            return None, "not judged (judge disabled)"
        prompt = (f"Rep's question: {question}\n\nAssistant's answer: {answer}\n\nGrading question: "
                  f"{QUESTIONS[kind]}")
        from ..llm.pricing import cost_usd
        resp = self.llm.complete(JUDGE_SYSTEM, [Message("user", text=prompt)], [], max_tokens=300)
        with self._lock:
            self.calls += 1
            self.cost_usd += cost_usd(self.llm.model, resp.usage)
        m = re.search(r"\{.*\}", resp.text, re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
            return bool(data["verdict"]), str(data.get("reason", ""))[:200]
        except (ValueError, KeyError):
            return None, "judge reply could not be parsed"
