"""The evaluation harness end to end on the real mock with the offline fake model.

The fake model gives canned answers, so the gate must FAIL. This proves the gate catches a bad agent, and
exercises the runner, scoring, metrics and report code without any API cost.
"""
import json

import pytest

from member_services.config import Settings, get_settings
from member_services.evaluation.__main__ import main

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_env(pack_dir, monkeypatch):
    if not Settings().tenant_tokens:
        pytest.skip("TENANT_TOKENS not configured")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_gate_fails_for_an_agent_that_gives_canned_answers(fake_env, tmp_path):
    out = tmp_path / "report.json"
    rc = main(["run", "--runs", "1", "--judge", "off", "--only", "EV-01,EV-16,EV-21", "--out", str(out)])
    assert rc == 1
    report = json.loads(out.read_text())
    by_id = {c["id"]: c for c in report["passes"][0]}
    assert by_id["EV-21"]["passed_adjusted"] is True  # not-in-plan is handled without the model
    assert by_id["EV-01"]["passed_adjusted"] is False and by_id["EV-16"]["passed_adjusted"] is False
    assert "EV-16" in report["metrics"]["M1_safety_cases_failed"]
    md = out.with_suffix(".md").read_text()
    assert "**Gate: FAIL**" in md and "M3_phi_exposures" in md
    assert main(["gate", "--report", str(out)]) == 1
