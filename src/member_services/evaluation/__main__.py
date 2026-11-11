"""Evaluation CLI.

    python -m member_services.evaluation run [--runs 3] [--compare claude-haiku-5-5] [--judge claude-haiku-5-5|off]
    python -m member_services.evaluation gate [--report eval/report.json]

``run`` writes ``eval/report.json`` and ``eval/report.md`` and exits non-zero if the gate fails. ``gate`` re-checks
a saved report against ``eval/thresholds.yaml`` (used in CI where the pack or an API key is not available).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import REPO_ROOT, get_settings
from ..llm import make_llm
from ..llm.anthropic_adapter import AnthropicLLM
from ..llm.fake import FakeLLM
from .judge import Judge
from .metrics import compute, gate
from .report import render
from .runner import SAFETY_DEFAULT, load_cases, load_disputed, run_pass, to_dict

EVAL_DIR = REPO_ROOT / "eval"


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _llm_factory(settings, model: str | None):
    if model is None:
        return lambda: make_llm(settings)
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    return lambda: AnthropicLLM(model=model, api_key=key, effort=settings.llm_effort,
                                timeout_s=settings.llm_timeout_s, refusal_fallback=settings.llm_refusal_fallback)


def cmd_run(args) -> int:
    settings = get_settings()
    settings.require_runtime()
    cases_path = Path(args.cases) if args.cases else settings.pack_dir / "eval" / "conversations.json"
    cases = load_cases(cases_path)
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only.split(","))]
    disputed = load_disputed(EVAL_DIR / "disputed_cases.yaml")
    judge_llm = None
    if args.judge != "off":
        if settings.llm_provider == "fake":
            judge_llm = FakeLLM()
        elif settings.anthropic_api_key is None:
            print("note: no ANTHROPIC_API_KEY for the judge; judged checks are recorded as 'not judged'")
        else:
            judge_llm = AnthropicLLM(model=args.judge, api_key=settings.anthropic_api_key.get_secret_value(),
                                     effort="low", refusal_fallback=False)
    judge = Judge(judge_llm)
    run_dir = EVAL_DIR / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    model = "fake" if settings.llm_provider == "fake" else settings.llm_model

    def do_pass(i: int, model_override=None) -> list[dict]:
        t0 = time.time()
        res = run_pass(settings, cases, _llm_factory(settings, model_override), judge, disputed,
                       workers=args.workers, trace_dir=run_dir / f"traces-{model_override or model}-{i}")
        rows = [to_dict(r) for r in res]
        ok = sum(r["passed_adjusted"] for r in rows)
        print(f"  pass {i + 1} ({model_override or model}): {ok}/{len(rows)} adjusted, "
              f"{sum(r['passed_raw'] for r in rows)}/{len(rows)} raw, ${sum(r['cost_usd'] for r in rows):.3f}, "
              f"{time.time() - t0:.0f}s", flush=True)
        return rows

    print(f"Running {len(cases)} cases x {args.runs} on {model}", flush=True)
    passes = [do_pass(i) for i in range(args.runs)]
    metrics = compute(passes, SAFETY_DEFAULT)
    comparison = {model: metrics}
    compare_passes = {}
    for other in args.compare or []:
        compare_passes[other] = [do_pass(0, other)]
        comparison[other] = compute(compare_passes[other], SAFETY_DEFAULT)
    gate_rows = gate(metrics, EVAL_DIR / "thresholds.yaml")
    agent_spend = sum(c["cost_usd"] for p in passes for c in p) + sum(
        c["cost_usd"] for ps in compare_passes.values() for p in ps for c in p)
    meta = {"model": model, "effort": settings.llm_effort, "judge": args.judge, "as_of": settings.today().isoformat(),
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": _commit(),
            "agent_spend_usd": agent_spend, "judge_spend_usd": judge.cost_usd,
            "spend_usd": agent_spend + judge.cost_usd, "cases_file": cases_path.name}
    out = {"meta": meta, "metrics": metrics, "gate": gate_rows, "comparison": comparison,
           "passes": passes, "comparison_passes": compare_passes}
    report_json = Path(args.out) if args.out else EVAL_DIR / "report.json"
    report_json.write_text(json.dumps(out, indent=1, default=str))
    report_json.with_suffix(".md").write_text(
        render(meta, metrics, gate_rows, passes, comparison if len(comparison) > 1 else None, disputed))
    failed = [r for r in gate_rows if not r["ok"]]
    print(f"Gate: {'PASS' if not failed else 'FAIL'}  ->  {report_json.with_suffix('.md')}")
    for r in failed:
        print(f"  FAIL {r['metric']}: {r['value']} (rule {r['op']} {r['threshold']})")
    return 0 if not failed else 1


def cmd_gate(args) -> int:
    report = json.loads(Path(args.report).read_text())
    rows = gate(report["metrics"], Path(args.thresholds))
    for r in rows:
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['metric']} = {r['value']} ({r['op']} {r['threshold']})")
    return 0 if all(r["ok"] for r in rows) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m member_services.evaluation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the eval cases and write the report")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--cases", help="path to conversations.json (default: PACK_DIR/eval/conversations.json)")
    r.add_argument("--only", help="comma-separated case ids")
    r.add_argument("--compare", action="append", help="also run once on this model, for comparison")
    r.add_argument("--judge", default="claude-haiku-5-5", help="judge model id, or 'off'")
    r.add_argument("--out", help="report path (default eval/report.json)")
    g = sub.add_parser("gate", help="check a saved report against the thresholds")
    g.add_argument("--report", default=str(EVAL_DIR / "report.json"))
    g.add_argument("--thresholds", default=str(EVAL_DIR / "thresholds.yaml"))
    args = ap.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_gate(args)


if __name__ == "__main__":
    sys.exit(main())
