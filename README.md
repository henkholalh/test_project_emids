# Governed multi-tenant member-services assistant

An AI assistant for the member-services representatives of two health plans on one platform (`plan_a`, `plan_b`).
A rep asks about a member's benefits and claims, and can ask the assistant to file a grievance, which happens
only after the rep confirms. A **tool gateway** sits between the model and every system: it enforces tenant
isolation, PHI minimisation, confirm-before-write with exactly-once filing, and per-conversation budgets, and
writes a redacted trace of every step.

| | |
|---|---|
| Design (deployment, Azure mapping, threat model, ADRs) | [`docs/design.md`](docs/design.md) |
| Evaluation report (committed) | [`eval/report.md`](eval/report.md) — gate **PASS**: 40/40 adjusted in each of 3 runs, 0 leaks, 0 PHI exposures, 0 duplicate writes |
| Decisions, data surprises, next steps, AI use | [`NOTES.md`](NOTES.md) |
| Model developed with | **Claude Sonnet 5.5** (`claude-sonnet-5-5`, effort `low`); compared with Claude Haiku 5.5 |

## Requirements

- Python 3.11+ (developed on 3.14; CI runs 3.11 and 3.12)
- The supplied pack (`kb/`, `eval/`, `mock_services/`, `data/README.md`). It is **not** in this repository.
- A model: an Anthropic API key, or any OpenAI-compatible endpoint (for example Ollama)

## Setup

```bash
git clone https://github.com/henkholalh/test_project_emids.git member-services-agent
cd member-services-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # then edit .env, see below
```

Fill in `.env` (never committed):

| Variable | Value |
|---|---|
| `PACK_DIR` | Path to the pack. Default `../T4_Lead_AI_Architect_A` (relative paths resolve from the repo root) |
| `TENANT_TOKENS` | JSON of tenant to API token, from the pack's `data/README.md`, e.g. `{"plan_a": "...", "plan_b": "..."}` |
| `DIRECTORY_API_KEY` | The provider-directory key from `data/README.md` |
| `AS_OF_DATE` | `2026-11-15` (the pack's "today"); leave unset in production |
| `LLM_PROVIDER` / `LLM_MODEL` | `anthropic` / `claude-sonnet-5-5` (default). See [Changing the model](#changing-the-model-or-provider) |
| `ANTHROPIC_API_KEY` | Your key (Anthropic provider) |
| `REP_API_KEYS` | HTTP API only: JSON of rep key to tenant, e.g. `{"rep-key-a": "plan_a", "rep-key-b": "plan_b"}` |
| `TRACE_HASH_KEY` | A random secret for pseudonymising ids in traces (`python -c "import secrets;print(secrets.token_hex(32))"`) |

Optional: `BUDGET_MAX_STEPS` (12), `BUDGET_MAX_COST_USD` (0.03), `BUDGET_MAX_LATENCY_S` (10), `LLM_EFFORT` (`low`),
`CLAIMS_PARALLEL_PAGES` (2; 0 = follow `next` strictly), `TRACE_DIR` (`traces`).

## Run

**1. Start the mock** (from the pack):

```bash
python ../T4_Lead_AI_Architect_A/mock_services/server.py --port 8090     # or: scripts/run_mock.sh
```

**2a. Talk to the agent in the terminal (CLI):**

```bash
python -m member_services.cli --tenant plan_a --member <MEMBER_ID>            # interactive; /confirm, /trace, /quit
python -m member_services.cli --tenant plan_b --member <MEMBER_ID> --show-usage \
  -m "Please file a billing grievance: the provider billed the member for a visit the plan paid." \
  -m "Confirmed. The member agreed to the summary. File it."
```

**2b. Or run the HTTP API:**

```bash
uvicorn member_services.api:app --port 8000
```

```bash
KEY=rep-key-a   # a key from REP_API_KEYS; it decides the tenant
SID=$(curl -s -X POST localhost:8000/sessions -H "Authorization: Bearer $KEY" \
        -H 'Content-Type: application/json' -d '{"member_id": "<MEMBER_ID>"}' | python -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessions/$SID/turns -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"message": "What does a specialist visit cost?"}'
# To confirm a pending grievance from a UI button: {"message": "Confirmed", "confirm_action_id": "<pending_action_id>"}
curl -s localhost:8000/sessions/$SID/trace -H "Authorization: Bearer $KEY"        # redacted trace
```

Endpoints: `POST /sessions`, `POST /sessions/{id}/turns`, `GET /sessions/{id}/trace`, `DELETE /sessions/{id}`,
`GET /health`. Interactive docs at `http://localhost:8000/docs`.

**3. Run the evaluation and the regression gate:**

```bash
python -m member_services.evaluation run --runs 3                             # writes eval/report.md and .json
python -m member_services.evaluation run --runs 3 --compare claude-haiku-5-5  # plus a comparison run
python -m member_services.evaluation run --runs 1 --only EV-16,EV-32          # selected cases
python -m member_services.evaluation gate                                     # re-check a saved report
```

`run` starts its own fresh mock from `PACK_DIR` for every pass (no need to start one) and exits non-zero when the
gate fails. A full 3-run evaluation on Sonnet 5.5 costs about $0.55 and takes about 2.5 minutes. Thresholds, each
with its justification, are in [`eval/thresholds.yaml`](eval/thresholds.yaml); disputed expectations in the eval
file, with evidence, are in [`eval/disputed_cases.yaml`](eval/disputed_cases.yaml). `--judge off` disables the
small LLM judge (used only for decline / not-available / coverage-ended / ambiguous yes-no checks).

**4. Tests:**

```bash
pytest -q                     # all tests; integration tests run when PACK_DIR and TENANT_TOKENS are set
pytest -q -m "not integration"
```

## Changing the model or provider

Everything is configuration. The gateway's controls do not depend on the model; the eval gate is the acceptance
test for a model change.

| Provider | Settings |
|---|---|
| Anthropic (default) | `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-5-5` or `claude-haiku-5-5`, `ANTHROPIC_API_KEY` |
| OpenAI-compatible (Ollama, vLLM, OpenAI, ...) | `LLM_PROVIDER=openai_compat`, `OPENAI_BASE_URL` (e.g. `http://localhost:11434/v1`), `OPENAI_API_KEY` if needed, `LLM_MODEL=<model>`, and a price: `LLM_PRICES_JSON='{"<model>": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}'` (USD per million tokens) |
| Offline fake (tests, dry runs) | `LLM_PROVIDER=fake` |

The OpenAI-compatible adapter is covered by offline tests; it has not been run against a live non-Claude model.
Prices for Anthropic models are first-party API prices; override them with `LLM_PRICES_JSON` for other platforms.
Adding a provider means one class with a `complete()` method (`src/member_services/llm/base.py`).

## Layout

```
src/member_services/
  config.py            typed settings; secrets never echoed
  context.py           immutable (tenant, member) session context
  clients/             tenant-bound member API client, third-party directory client, retries + circuit breaker
  gateway/             the tool gateway: tools, PHI projection and redaction, grievance workflow, budgets, trace
  kb/                  plan document loading (tenant check), benefit parsing, per-tenant retrieval, policies
  agent/               system prompt, turn loop, sessions
  llm/                 model interface; Anthropic, OpenAI-compatible and fake adapters; price table
  evaluation/          runner, scoring, judge, metrics, gate, report
  api.py, cli.py       HTTP API and terminal client
eval/                  thresholds, disputed cases, committed report
docs/                  design document and architecture diagram source
tests/                 unit tests (fake upstream, invented fixture KB) and integration tests (real mock and KB)
```

## Data handling

The supplied data is treated as real PHI. The pack, `.env`, traces and eval run directories are git-ignored; CI
fails if a pack file, token, API key or SSN-shaped value is committed. Unit tests use invented data only;
integration tests read member ids and expected values from the pack at run time.
