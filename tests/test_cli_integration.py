"""CLI plumbing against the real mock, with the offline fake model (no API cost)."""
import json

import pytest

from member_services import cli
from member_services.config import Settings, get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def cli_env(mock_url, monkeypatch, tmp_path):
    if not Settings().tenant_tokens:
        pytest.skip("TENANT_TOKENS not configured")
    monkeypatch.setenv("MOCK_BASE_URL", mock_url)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("TRACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_cli_runs_a_scripted_conversation(cli_env, pack_dir, capsys):
    case = json.loads((pack_dir / "eval" / "conversations.json").read_text())["cases"][0]
    rc = cli.main(["--tenant", case["tenant"], "--member", case["member_id"], "-m", "hello", "--show-usage"])
    out = capsys.readouterr().out
    assert rc == 0 and "member found" in out and "assistant>" in out and "model calls 1" in out
    assert list(cli_env.glob("*.jsonl"))  # a trace was written


def test_cli_reports_member_not_found(cli_env, capsys):
    assert cli.main(["--tenant", "plan_a", "--member", "M000000", "-m", "hi"]) == 0
    assert "NOT found" in capsys.readouterr().out


def test_cli_rejects_unknown_tenant(cli_env, capsys):
    assert cli.main(["--tenant", "plan_z", "--member", "M000000", "-m", "hi"]) == 2
    assert "not configured" in capsys.readouterr().err
