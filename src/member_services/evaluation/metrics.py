"""Metrics M1–M13 over one or more passes, and the regression gate.

Thresholds live in ``eval/thresholds.yaml`` with a written justification each, so changing a threshold is a
reviewed change to a committed file, not an edit buried in code.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = max(0, min(len(v) - 1, math.ceil(p / 100 * len(v)) - 1))
    return float(v[k])


def compute(passes: list[list[dict]], safety_ids: list[str]) -> dict[str, Any]:
    flat = [c for p in passes for c in p]
    n_runs = len(passes)
    per_run = []
    for p in passes:
        facts_total_raw = sum(len(c["facts_expected"]) for c in p)
        facts_total_adj = sum(len(c["facts_supported"] if c["disputed"] else c["facts_expected"]) for c in p)
        per_run.append({
            "accuracy_raw": sum(c["passed_raw"] for c in p) / len(p),
            "accuracy_adjusted": sum(c["passed_adjusted"] for c in p) / len(p),
            "fact_recall_raw": sum(c["facts_found_raw"] for c in p) / max(1, facts_total_raw),
            "fact_recall_adjusted": sum(c["facts_found_adjusted"] for c in p) / max(1, facts_total_adj),
        })
    turns = [t for c in flat for t in c["turns"]]
    lat = [t["latency_ms"] / 1000 for t in turns if t["stopped"] != "not_in_plan"]
    lat_one = [t["latency_ms"] / 1000 for t in turns if t["model_calls"] == 1]
    lat_tools = [t["latency_ms"] / 1000 for t in turns if t["model_calls"] > 1]
    by_case: dict[str, list[dict]] = {}
    for c in flat:
        by_case.setdefault(c["id"], []).append(c)
    need = math.ceil(2 * n_runs / 3)
    unstable = sorted(cid for cid, cs in by_case.items() if sum(x["passed_adjusted"] for x in cs) < need)
    flaky = sorted(cid for cid, cs in by_case.items() if 0 < sum(x["passed_adjusted"] for x in cs) < len(cs))
    safety_fail = sorted({c["id"] for c in flat if c["id"] in safety_ids and not c["passed_adjusted"]})
    write_expected = {c["id"] for c in flat if c["behaviour"] == "confirm_then_file"}
    exactly_once_violations = sorted({c["id"] for c in flat if (c["writes_in_tenant"] != (1 if c["id"] in write_expected else 0))
                                      or c["writes_other_tenants"]})
    costs = [c["cost_usd"] for c in flat]
    return {
        "runs": n_runs,
        "cases": len(passes[0]) if passes else 0,
        "M1_safety_cases_failed": safety_fail,
        "M2_cross_tenant_leaks": sum(len(c["leak_hits"]) for c in flat),
        "M3_phi_exposures": sum(len(c["phi_hits"]) for c in flat),
        "M4_exactly_once_violations": exactly_once_violations,
        "M5_unconfirmed_writes": sum(c["unconfirmed_writes"] for c in flat),
        "M6_accuracy_adjusted_mean": sum(r["accuracy_adjusted"] for r in per_run) / n_runs,
        "M6_accuracy_adjusted_min": min(r["accuracy_adjusted"] for r in per_run),
        "M6_accuracy_raw_mean": sum(r["accuracy_raw"] for r in per_run) / n_runs,
        "M7_fact_recall_adjusted_mean": sum(r["fact_recall_adjusted"] for r in per_run) / n_runs,
        "M7_fact_recall_raw_mean": sum(r["fact_recall_raw"] for r in per_run) / n_runs,
        "M8_retrieval_out_of_scope": sum(c["retrieval_out_of_scope"] for c in flat),
        "M9_latency_p50_s": round(percentile(lat, 50), 2),
        "M9_latency_p95_s": round(percentile(lat, 95), 2),
        "M9_latency_p95_one_call_s": round(percentile(lat_one, 95), 2),
        "M9_latency_p95_with_tools_s": round(percentile(lat_tools, 95), 2),
        "M9_share_turns_under_3s": round(sum(x < 3 for x in lat) / max(1, len(lat)), 3),
        "M9_prefetch_p95_s": round(percentile([c["prefetch_ms"] / 1000 for c in flat], 95), 2),
        "M10_cost_per_conversation_mean_usd": round(sum(costs) / max(1, len(costs)), 5),
        "M10_cost_per_conversation_p95_usd": round(percentile(costs, 95), 5),
        "M11_steps_mean": round(sum(c["steps"] for c in flat) / max(1, len(flat)), 2),
        "M11_budget_stops": sum(c["budget_stops"] for c in flat),
        "M12_unstable_cases": unstable,
        "M12_flaky_cases": flaky,
        "M13_unrecovered_upstream_errors": sum(c["tool_upstream_errors"] for c in flat),
        "errors": sorted({c["id"] for c in flat if c.get("error")}),
        "per_run": per_run,
    }


OPS = {"max": lambda v, t: v <= t, "min": lambda v, t: v >= t, "empty": lambda v, _t: len(v) == 0}


def gate(metrics: dict, thresholds_path: Path) -> list[dict]:
    """Evaluate every threshold. Returns one row per rule with ``ok``."""
    spec = yaml.safe_load(thresholds_path.read_text())
    rows = []
    for rule in spec["rules"]:
        value = metrics.get(rule["metric"])
        ok = value is not None and OPS[rule["op"]](value, rule.get("value"))
        rows.append({"metric": rule["metric"], "op": rule["op"], "threshold": rule.get("value"), "value": value,
                     "ok": bool(ok), "why": rule["why"]})
    return rows
