"""Markdown report. Contains case ids, outcomes, metrics and (for failed checks only) the assistant's answer as
returned to the rep, which has already passed the output guard. No member ids, names or identity data."""
from __future__ import annotations

from typing import Any


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.3f}" if v < 1 else f"{v:.2f}"
    if isinstance(v, list):
        return ", ".join(map(str, v)) if v else "none"
    return str(v)


def render(meta: dict, metrics: dict, gate_rows: list[dict], passes: list[list[dict]],
           comparison: dict | None = None, disputed: dict | None = None) -> str:
    ok = all(r["ok"] for r in gate_rows)
    lines = [
        "# Evaluation report",
        "",
        f"- **Gate: {'PASS' if ok else 'FAIL'}**",
        f"- Model under test: `{meta['model']}` (effort `{meta['effort']}`); judge: `{meta['judge']}`",
        f"- Runs: {metrics['runs']} x {metrics['cases']} cases; as-of date {meta['as_of']}; generated {meta['generated']}",
        f"- Commit: `{meta.get('commit', 'unknown')}`",
        f"- Eval spend: ${meta['spend_usd']:.2f} (agent ${meta['agent_spend_usd']:.2f}, judge ${meta['judge_spend_usd']:.3f})",
        "",
        "## Gate",
        "",
        "| Metric | Rule | Value | Result |",
        "|---|---|---|---|",
    ]
    for r in gate_rows:
        rule = {"max": f"<= {r['threshold']}", "min": f">= {r['threshold']}", "empty": "none"}[r["op"]]
        lines.append(f"| {r['metric']} | {rule} | {_fmt(r['value'])} | {'pass' if r['ok'] else '**FAIL**'} |")
    lines += ["", "Why each threshold is set where it is: see `eval/thresholds.yaml`.", "", "## Headline metrics", "",
              "| Metric | Value |", "|---|---|"]
    keys = ["M6_accuracy_adjusted_mean", "M6_accuracy_raw_mean", "M7_fact_recall_adjusted_mean",
            "M7_fact_recall_raw_mean", "M9_latency_p50_s", "M9_latency_p95_s", "M9_latency_p95_one_call_s",
            "M9_latency_p95_with_tools_s", "M9_share_turns_under_3s", "M9_prefetch_p95_s",
            "M10_cost_per_conversation_mean_usd", "M10_cost_per_conversation_p95_usd", "M11_steps_mean",
            "M12_flaky_cases"]
    lines += [f"| {k} | {_fmt(metrics[k])} |" for k in keys]
    lines += ["", "Per run: " + "; ".join(
        f"run {i + 1}: adjusted {r['accuracy_adjusted']:.1%}, raw {r['accuracy_raw']:.1%}"
        for i, r in enumerate(metrics["per_run"])), ""]
    if comparison:
        lines += ["## Model comparison (1 run each)", "", "| Model | Adjusted accuracy | Raw accuracy | p95 latency (s) | "
                  "p95 one-call (s) | Cost / conversation | Safety failures |", "|---|---|---|---|---|---|---|"]
        for model, m in comparison.items():
            lines.append(f"| `{model}` | {m['M6_accuracy_adjusted_mean']:.1%} | {m['M6_accuracy_raw_mean']:.1%} | "
                         f"{m['M9_latency_p95_s']} | {m['M9_latency_p95_one_call_s']} | "
                         f"${m['M10_cost_per_conversation_mean_usd']:.4f} | {_fmt(m['M1_safety_cases_failed'])} |")
        lines.append("")
    lines += ["## Cases", "", "Pass counts are over all runs. `adj` = scored against supported facts for disputed cases.",
              "", "| Case | Tenant | Behaviour | Raw | Adjusted | Calls | Tools | p50 turn (s) | Cost |",
              "|---|---|---|---|---|---|---|---|---|"]
    by_id: dict[str, list[dict]] = {}
    for p in passes:
        for c in p:
            by_id.setdefault(c["id"], []).append(c)
    for cid in sorted(by_id):
        cs = by_id[cid]
        c0 = cs[0]
        lat = sorted(t["latency_ms"] for c in cs for t in c["turns"])
        tools = sorted({t for c in cs for turn in c["turns"] for t in turn["tools"]})
        calls = sum(t["model_calls"] for c in cs for t in c["turns"]) / len(cs)
        lines.append(f"| {cid}{' (disputed)' if c0['disputed'] else ''} | {c0['tenant']} | {c0['behaviour']} | "
                     f"{sum(c['passed_raw'] for c in cs)}/{len(cs)} | {sum(c['passed_adjusted'] for c in cs)}/{len(cs)} | "
                     f"{calls:.1f} | {', '.join(tools) or '-'} | {lat[len(lat) // 2] / 1000:.1f} | "
                     f"${sum(c['cost_usd'] for c in cs) / len(cs):.4f} |")
    if disputed:
        lines += ["", "## Disputed expectations", ""]
        for cid, d in sorted(disputed.items()):
            lines += [f"- **{cid}** - expected `{'; '.join(by_id.get(cid, [{}])[0].get('facts_expected', []))}`, "
                      f"data supports `{'; '.join(d['supported_facts'])}`. {d['reason']}"]
    failures = [(i, c) for i, p in enumerate(passes) for c in p if not c["passed_adjusted"]]
    lines += ["", "## Failed checks (adjusted)", ""]
    if not failures:
        lines.append("None.")
    for i, c in failures:
        bad = [ch for ch in c["checks"] if ch["ok"] is not True]
        lines.append(f"- **{c['id']}** (run {i + 1}): " + "; ".join(
            f"{ch['check']} -> {ch['ok']}{' (judged: ' + ch['detail'] + ')' if ch['judged'] else ''}" for ch in bad))
        lines.append(f"  - Answer: {' / '.join(a.replace(chr(10), ' ') for a in c['answers'])[:600]}")
    judged = sorted({ch["check"] for p in passes for c in p for ch in c["checks"] if ch["judged"]})
    lines += ["", "## Method", "",
              "- Each run starts a fresh mock; every case runs through the real gateway and agent.",
              "- Writes are counted from the grievance API for the member in every tenant, before and after.",
              "- PHI and cross-tenant scans cover every request sent to the model, the full trace and every answer.",
              "- Facts are matched deterministically after normalising money and spacing.",
              f"- LLM-judged checks (only where a string match cannot decide): {', '.join(judged) or 'none'}.",
              ""]
    return "\n".join(lines)
