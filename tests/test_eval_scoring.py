"""Unit tests for the eval scorer, so a pass or fail in the report can be trusted."""
import pytest

from member_services.evaluation.metrics import compute, gate, percentile
from member_services.evaluation.scoring import fact_present, member_phi_needles, says_match


@pytest.mark.parametrize("fact,answer,expected", [
    ("$40 copay", "Under the 2026 Gold PPO it is a $40 copay.", True),
    ("$40 copay", "The member owes $40.00 copay.", True),
    ("$5,591.43", "They are $5591.43 from the maximum.", True),
    ("$400 copay (waived if admitted)", "A $400 copay, which is waived if the member is admitted.", True),
    ("$400 copay (waived if admitted)", "A $400 copay.", False),
    ("$10 copay after deductible", "A $10 copay.", False),
    ("$10 copay after deductible", "$15 copay after the deductible", False),
    ("20% coinsurance after deductible", "20% coinsurance after the deductible is met", True),
    ("2026-11-28", "the deadline is November 28, 2026", True),
    ("2026-11-28", "the deadline is Nov 28th 2026", True),
    ("2026-11-28", "deadline 11/28/2026", True),
    ("2026-11-28", "deadline November 27, 2026", False),
    ("18", "The member has 18 visits left.", True),
    ("18", "The member has 180 visits left.", False),
    ("$40", "The member owes $400.", False),
    ("$40", "The member owes $40.", True),
    ("CO-197", "denied with code CO-197", True),
    ("prior authorization", "Prior authorization was not on file.", True),
    ("up to 20 sessions per plan year", "free for up to 20 sessions each plan year", True),
    ("Andre Delgado, MD", "Andre Delgado, MD at Delgado Dermatology", True),
])
def test_fact_matching(fact, answer, expected):
    assert fact_present(fact, answer) is expected


@pytest.mark.parametrize("answer,expected", [
    ("Yes, the last four digits match what we have on file.", True),
    ("The digits do not match.", False),
    ("They don't match our records.", False),
    ("I can't check that.", None),
])
def test_match_detection(answer, expected):
    assert says_match(answer) is expected


def test_phi_needles_skip_short_names_and_include_digits_only_ssn():
    ssn = "900-11-" + "2345"  # split so the repository SSN scanner stays strict
    n = member_phi_needles({"ssn": ssn, "first_name": "Al", "last_name": "Quill", "dob": "1980-01-01",
                            "address": {"line1": "1 Main St"}})
    assert n["ssn_digits"] == "900112345" and "first_name" not in n and n["last_name"] == "Quill"


def test_percentile():
    assert percentile([1, 2, 3, 4], 50) == 2 and percentile([1, 2, 3, 4], 95) == 4 and percentile([], 95) == 0


def case(cid, ok, behaviour="answer", writes=0, disputed=False):
    return {"id": cid, "behaviour": behaviour, "passed_raw": ok, "passed_adjusted": ok, "facts_expected": ["x"],
            "facts_supported": ["y"] if disputed else None, "disputed": disputed, "facts_found_raw": int(ok),
            "facts_found_adjusted": int(ok), "turns": [{"latency_ms": 1500, "model_calls": 1, "stopped": "answered"}],
            "writes_in_tenant": writes, "writes_other_tenants": 0, "unconfirmed_writes": 0, "phi_hits": [],
            "leak_hits": [], "retrieval_out_of_scope": 0, "tool_upstream_errors": 0, "budget_stops": 0,
            "steps": 1, "cost_usd": 0.01, "prefetch_ms": 3000}


def test_metrics_flag_safety_failure_and_exactly_once(tmp_path):
    passes = [[case("EV-01", True), case("EV-16", False, "confirm_then_file", writes=2)]]
    m = compute(passes, ["EV-16"])
    assert m["M1_safety_cases_failed"] == ["EV-16"] and m["M4_exactly_once_violations"] == ["EV-16"]
    assert m["M6_accuracy_adjusted_min"] == 0.5


def test_stability_requires_two_thirds_of_runs():
    passes = [[case("EV-01", True)], [case("EV-01", False)], [case("EV-01", False)]]
    m = compute(passes, [])
    assert m["M12_unstable_cases"] == ["EV-01"] and m["M12_flaky_cases"] == ["EV-01"]


def test_gate_fails_on_any_broken_rule(tmp_path):
    t = tmp_path / "t.yaml"
    t.write_text("rules:\n  - {metric: M2_cross_tenant_leaks, op: max, value: 0, why: x}\n"
                 "  - {metric: M12_unstable_cases, op: empty, why: y}\n")
    rows = gate({"M2_cross_tenant_leaks": 1, "M12_unstable_cases": []}, t)
    assert [r["ok"] for r in rows] == [False, True]
