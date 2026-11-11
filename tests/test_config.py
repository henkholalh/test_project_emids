import pytest

from member_services.config import REPO_ROOT, ConfigError, Settings, get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate every test from the developer's real .env and environment."""
    for key in ("PACK_DIR", "MOCK_BASE_URL", "LLM_PROVIDER", "LLM_MODEL", "LLM_EFFORT", "TENANT_TOKENS",
                "DIRECTORY_API_KEY", "ANTHROPIC_API_KEY", "BUDGET_MAX_STEPS", "BUDGET_MAX_COST_USD",
                "BUDGET_MAX_LATENCY_S"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_point_at_sibling_pack_and_sonnet():
    s = Settings()
    assert s.pack_dir == (REPO_ROOT / "../T4_Lead_AI_Architect_A").resolve()
    assert s.llm_model == "claude-sonnet-5-5"
    assert s.budgets.max_cost_usd == 0.03


def test_tenant_tokens_parse_from_json_and_stay_secret(monkeypatch):
    monkeypatch.setenv("TENANT_TOKENS", '{"plan_a": "tok-a", "plan_b": "tok-b"}')
    s = Settings()
    assert s.tenants == ["plan_a", "plan_b"]
    assert s.token_for("plan_a") == "tok-a"
    assert "tok-a" not in repr(s) and "tok-a" not in str(s.model_dump())


def test_unknown_tenant_token_is_a_config_error(monkeypatch):
    monkeypatch.setenv("TENANT_TOKENS", '{"plan_a": "tok-a"}')
    with pytest.raises(ConfigError, match="plan_b"):
        Settings().token_for("plan_b")


def test_invalid_tokens_json_fails_without_echoing_value(monkeypatch):
    monkeypatch.setenv("TENANT_TOKENS", "not-json-super-secret")
    with pytest.raises(ConfigError) as err:
        get_settings()
    assert "super-secret" not in str(err.value)
    assert "tenant_tokens" in str(err.value)


def test_relative_pack_dir_resolves_from_repo_root(monkeypatch, tmp_path):
    monkeypatch.setenv("PACK_DIR", str(tmp_path))
    assert Settings().pack_dir == tmp_path


def test_require_runtime_lists_every_missing_item(monkeypatch, tmp_path):
    monkeypatch.setenv("PACK_DIR", str(tmp_path))
    with pytest.raises(ConfigError) as err:
        Settings().require_runtime()
    msg = str(err.value)
    assert "TENANT_TOKENS" in msg and "DIRECTORY_API_KEY" in msg and "kb/" in msg


def test_budget_env_overrides(monkeypatch):
    monkeypatch.setenv("BUDGET_MAX_STEPS", "4")
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    from member_services.config import Budgets
    monkeypatch.setitem(Budgets.model_config, "env_file", None)
    assert Settings().budgets.max_steps == 4
