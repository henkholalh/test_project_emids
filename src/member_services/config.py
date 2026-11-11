"""Runtime configuration.

Everything that differs between environments (pack location, mock URL, model provider, secrets, budgets) is read
from environment variables or a local ``.env`` file. Nothing secret has a default, and secrets are held as
``SecretStr`` so they are never printed by ``repr`` or logged by accident.
"""
from __future__ import annotations

import hmac
import json
import secrets
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid. The message never contains secret values."""


class Budgets(BaseSettings):
    """Per-conversation limits enforced by the gateway."""

    model_config = SettingsConfigDict(env_prefix="BUDGET_", env_file=REPO_ROOT / ".env", extra="ignore")

    max_steps: int = Field(12, ge=1, le=50)  # model calls + tool calls per conversation
    max_cost_usd: float = Field(0.03, gt=0)
    max_latency_s: float = Field(10.0, gt=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    pack_dir: Path = Path("../T4_Lead_AI_Architect_A")
    mock_base_url: str = "http://127.0.0.1:8090"

    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_effort: Literal["low", "medium", "high"] = "low"
    llm_timeout_s: float = Field(20.0, gt=0)
    llm_refusal_fallback: bool = True
    anthropic_api_key: SecretStr | None = None

    # NoDecode: parse the JSON ourselves so a malformed value gives a clean error with no value echoed.
    tenant_tokens: Annotated[dict[str, SecretStr], NoDecode] = Field(default_factory=dict)
    directory_api_key: SecretStr | None = None
    # Rep API keys mapped to the tenant they belong to: {"<key>": "plan_a"}. Stand-in for the production identity
    # provider (OIDC), where the tenant comes from a signed claim in the rep's token.
    rep_api_keys: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict, repr=False)

    budgets: Budgets = Field(default_factory=Budgets)

    # "Today" for deadline calculations. Unset in production (system date); the eval pack fixes it.
    as_of_date: date | None = None
    trace_dir: Path | None = Path("traces")
    # Key for pseudonymising identifiers in traces. Set a stable secret in production so audit records correlate.
    trace_hash_key: SecretStr = Field(default_factory=lambda: SecretStr(secrets.token_hex(32)))
    claims_parallel_pages: int = Field(2, ge=0, le=5)

    def today(self) -> date:
        return self.as_of_date or date.today()

    @field_validator("pack_dir")
    @classmethod
    def _resolve_pack_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (REPO_ROOT / v).resolve()

    @field_validator("tenant_tokens", "rep_api_keys", mode="before")
    @classmethod
    def _parse_tokens(cls, v):
        if isinstance(v, str):
            try:
                v = json.loads(v) if v.strip() else {}
            except json.JSONDecodeError as exc:
                raise ValueError("must be a JSON object") from exc
        if not isinstance(v, dict):
            raise ValueError("must be a JSON object")
        return v

    @model_validator(mode="after")
    def _check_provider(self) -> "Settings":
        if self.llm_provider == "anthropic" and not self.llm_model.strip():
            raise ValueError("LLM_MODEL must be set when LLM_PROVIDER=anthropic")
        return self

    # ---- convenience accessors -------------------------------------------------
    @property
    def tenants(self) -> list[str]:
        return sorted(self.tenant_tokens)

    def token_for(self, tenant: str) -> str:
        """Return the API token issued for ``tenant``. The gateway is the only caller."""
        try:
            return self.tenant_tokens[tenant].get_secret_value()
        except KeyError:
            raise ConfigError(f"No API token configured for tenant '{tenant}'") from None

    def tenant_for_rep_key(self, key: str) -> str | None:
        """Constant-time lookup of the tenant a rep API key belongs to."""
        found = None
        for k, tenant in self.rep_api_keys.items():
            if hmac.compare_digest(k.encode(), (key or "").encode()):
                found = tenant
        return found

    def require_runtime(self) -> None:
        """Fail fast, with a clear message, when settings needed to serve traffic are missing."""
        problems = []
        if not self.tenant_tokens:
            problems.append("TENANT_TOKENS is not set")
        if self.directory_api_key is None:
            problems.append("DIRECTORY_API_KEY is not set")
        if not (self.pack_dir / "kb").is_dir():
            problems.append(f"PACK_DIR has no kb/ folder: {self.pack_dir}")
        if problems:
            raise ConfigError("Configuration incomplete: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Report field names and reasons only; never echo input values, which may be secrets.
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"Invalid configuration: {details}") from None
