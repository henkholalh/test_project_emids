"""Per-conversation budgets: steps and cost across the conversation, latency per rep turn.

The agent loop charges every model call and the gateway charges every tool call. When a limit would be passed the
gateway refuses further work and the agent tells the rep plainly, instead of running on or failing silently.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import Budgets


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # steps | cost | latency


@dataclass
class BudgetTracker:
    limits: Budgets
    clock: Callable[[], float] = time.monotonic
    steps: int = 0
    cost_usd: float = 0.0
    _turn_started: float = field(default=0.0)

    def start_turn(self) -> None:
        self._turn_started = self.clock()

    @property
    def turn_elapsed_s(self) -> float:
        return self.clock() - self._turn_started if self._turn_started else 0.0

    def check(self) -> None:
        if self.steps >= self.limits.max_steps:
            raise BudgetExceeded("steps", f"step budget of {self.limits.max_steps} reached")
        if self.cost_usd >= self.limits.max_cost_usd:
            raise BudgetExceeded("cost", f"cost budget of ${self.limits.max_cost_usd:.2f} reached")
        if self._turn_started and self.turn_elapsed_s >= self.limits.max_latency_s:
            raise BudgetExceeded("latency", f"turn time budget of {self.limits.max_latency_s:.0f}s reached")

    def charge_step(self) -> None:
        self.check()
        self.steps += 1

    def charge_cost(self, usd: float) -> None:
        self.cost_usd += usd

    def snapshot(self) -> dict:
        return {"steps": self.steps, "cost_usd": round(self.cost_usd, 6),
                "turn_elapsed_s": round(self.turn_elapsed_s, 3)}
