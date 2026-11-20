# 🚪 Egress — AWS lock-in scanner & exit copilot

Scan a repository, detect AWS hyperscaler lock-in, score it, let an LLM generate a
migration to open-source / EU-sovereign alternatives, and run the migrated stack
locally with `docker compose`, all from one Streamlit page.

## Problem

Teams build on AWS managed services (S3, RDS, SQS, Lambda, ...) because it's fast.
Years later, leaving is expensive: proprietary APIs are spread through the code,
data sits behind egress fees, and nobody knows *how* locked-in they really are.
EU data-sovereignty requirements make this an urgent question.

## Solution

1. **Scan**: rule-based detection over Terraform, docker-compose, Python and
   `requirements.txt`. Every finding has file, line, evidence, reason and severity.
2. **Score**: 0-100 per service and overall, weighted by proprietary API (50%),
   data format (30%) and egress cost (20%).
3. **Migrate**: an OpenAI-compatible LLM (Mistral, OpenAI, Ollama, vLLM...) produces
   a service mapping, a new `docker-compose.yml`, unified diffs and a migration
   plan with risks and effort. **Offline templates take over automatically** if the LLM
   is unavailable or wrong.
4. **Run**: one click runs `docker compose up` on the migrated stack
   (PostgreSQL + S3-compatible storage + RabbitMQ + container worker), then a smoke-test
   upload goes through the whole pipeline.

| AWS | Open-source replacement |
|---|---|
| S3 | SeaweedFS (S3 API); Garage / MinIO also work |
| RDS (Postgres) | PostgreSQL 16 |
| SQS / SNS | RabbitMQ (queue / fanout) |
| Lambda | Long-running container worker (OpenFaaS / Knative if FaaS needed) |
| DynamoDB | PostgreSQL JSONB / ScyllaDB |
| CloudWatch | json-file logs → Prometheus + Grafana + Loki |
| IAM | Static service credentials / Keycloak |

> **Why SeaweedFS and not MinIO?** MinIO no longer publishes community container images
> (Docker Hub / quay.io pulls fail), so the demo uses SeaweedFS (Apache 2.0, S3-compatible).
> The app only talks the S3 API via `endpoint_url`, so Garage (EU, Deuxfleurs) or MinIO can
> be swapped in by changing the `s3` service.

## Architecture

```mermaid
flowchart LR
    U[User] --> UI[Streamlit UI<br/>app.py]
    UI -->|path or git URL| SRC[scanner/source.py<br/>shallow clone]
    SRC --> SC[scanner/<br/>rules + scoring]
    SC -->|scan JSON| CP[copilot/generator.py]
    CP -->|prompt: scan + snippets| LLM[(OpenAI-compatible LLM<br/>Mistral / Ollama / ...)]
    LLM -->|JSON: mapping, compose,<br/>diffs, plan| CP
    CP -->|invalid / timeout / no key| FB[copilot/fallback.py<br/>+ templates/]
    CP --> VAL{docker compose config<br/>+ contract check}
    VAL -->|ok| OUT[output/]
    VAL -->|fail| FB
    FB --> OUT
    OUT --> DEP[copilot/deploy.py<br/>docker compose up]
    DEP --> STACK

    subgraph STACK[Migrated stack]
        API[api :5000] --> PG[(PostgreSQL :5432)]
        API --> S3[(SeaweedFS S3 :9000)]
        API --> MQ[[RabbitMQ :5672]]
        MQ --> W[worker] --> PG
        W --> S3
    end
```

## Setup

Requirements: Linux, Python 3.11, Docker with Compose v2 (usable without sudo),
git, make. Free ports: 8501, 5000, 5432, 9000, 9001, 5672, 15672.

```bash
make install            # creates .venv (python3.11) and installs requirements
cp .env.example .env    # optional: add LLM_API_KEY for the LLM path
make run                # Streamlit on http://localhost:8501
```

Other targets: `make test` (pytest), `make scan` (CLI scan of `sample_app`),
`make clean` (removes .venv, caches and generated output).
Different interpreter: `make install PYTHON=python3.12`.

### LLM configuration (`.env`)

| Var | Example | Notes |
|---|---|---|
| `LLM_BASE_URL` | `https://api.mistral.ai/v1` | Any OpenAI-compatible endpoint |
| `LLM_API_KEY` | `...` | Empty → offline templates (local URLs need no key) |
| `LLM_MODEL` | `mistral-large-latest` | e.g. `llama3.1` for Ollama |
| `LLM_TIMEOUT` | `60` | Seconds before falling back |

Ollama: `LLM_BASE_URL=http://localhost:11434/v1`, `LLM_API_KEY=ollama`, `LLM_MODEL=llama3.1`.

### CLI

```bash
.venv/bin/python -m scanner sample_app --summary        # human table
.venv/bin/python -m scanner sample_app --out scan.json  # JSON report
.venv/bin/python -m copilot sample_app [--no-llm]       # generate output/
cd output && docker compose up -d --build --wait        # run migrated stack
```

## 2-minute demo script

| Time | Do | Say |
|---|---|---|
| 0:00 | Open `localhost:8501`; show `sample_app/` (Flask + boto3 + `main.tf`) | "A typical upload service: S3, RDS, SQS, a Lambda, CloudWatch logs." |
| 0:15 | Click **🔍 Scan** | "32 findings across 7 services, each with file, line and the *reason* it locks you in. Overall score 63/100." |
| 0:35 | Point at service table + gauge, open *Scoring formula* | "Proprietary API weighs 50%, data format 30%, egress 20%. Lambda and S3 hurt most." |
| 0:50 | Click **🧠 Generate migration** | "The LLM gets the scan and code snippets and returns mapping, compose, diffs and plan." |
| 1:05 | Show badge (LLM / Offline / Mixed), then tabs *Mapping → Code diffs → Plan* | "Every part is validated. If the LLM fails, built-in templates take over, so the demo never breaks. The diff is tiny: `endpoint_url`, pika instead of SQS, Lambda → worker." |
| 1:25 | Click **🚀 Deploy** | "`docker compose up`: Postgres, S3-compatible storage, RabbitMQ, API and worker, all open-source." |
| 1:45 | Click **🧪 Smoke test upload** | "Upload → object storage → Postgres → queue → worker marks it processed. Same app, zero AWS." |
| 1:55 | Click **⏹ Stop** | "Egress: from lock-in to running locally in two minutes." |

Tip: deploy once before presenting so images are cached (first build ≈ 1-2 min).
Toggle **Force offline templates** in the sidebar if the network is unreliable.

## Fallback behavior

Everything that depends on the network or the LLM has an offline path:

| Situation | Behavior |
|---|---|
| No `LLM_API_KEY` (and non-local URL) | LLM skipped → all parts from templates (`path: fallback`) |
| Timeout / API / network error | Error recorded → templates (`fallback`) |
| Reply is not valid JSON | Templates (`fallback`) |
| A single part invalid (e.g. bad diffs) | Only that part from templates (`path: mixed`) |
| LLM compose fails `docker compose config` | Saved as `docker-compose.llm.yml` for inspection; template compose used |
| LLM compose breaks the contract (service names, env vars, AWS references) | Template compose used |
| Docker not installed | Compose validated as YAML only; deploy shows an error |
| git clone fails / times out (90s) | Clear error in the UI, no stack trace |

The UI shows the path used (🤖 LLM / 📦 Offline templates / 🤖+📦 mixed), the
source of each part and the reason for each fallback. `output/result.json` records the same.

Note: the runnable code in `output/app/` always comes from the templates (written for
`sample_app`). For other repos the mapping, diffs and plan are tailored, but the runnable
stack is the reference implementation.

## Lock-in scoring formula

Each detected service has an intrinsic profile (0-100) on three dimensions
(see `scanner/rules.py::SERVICE_PROFILES`):

| Dimension | Weight | Meaning |
|---|---|---|
| `api` | 50% | How proprietary the API / programming model is |
| `data` | 30% | How proprietary the data format / semantics are |
| `egress` | 20% | Cost of moving data out (egress fees, volume) |

```
base          = 0.5*api + 0.3*data + 0.2*egress
depth         = min(1, Σ severity_weight)     # low=0.3, medium=0.6, high=1.0
service_score = round(base * depth)           # 0-100
overall       = round(0.5*max(service_scores) + 0.5*mean(service_scores))
rating        = high (>=70) | medium (>=40) | low
```

`depth` rewards evidence: a single low-severity hint scores far less than a
Terraform resource plus SDK calls. The overall score blends the worst service with the
average, so one deeply locked-in service is never hidden by many shallow ones.

Example: S3 with one high finding → `0.5*70 + 0.3*40 + 0.2*90 = 65`.

## Repository layout

```
scanner/      rules.py (regex rules + service profiles), engine.py (walk + score),
              source.py (path / git clone), __main__.py (CLI)
copilot/      config.py (env/.env), llm.py (client + prompt), generator.py (LLM → validate → fallback),
              fallback.py + templates/ (offline mapping, compose, migrated app), deploy.py (docker compose)
app.py        Streamlit UI
sample_app/   Demo app on AWS: Flask + boto3 (S3, RDS, SQS), Lambda handler, main.tf, compose
output/       Generated migration (gitignored except .gitkeep)
tests/        pytest: scanner rules/scoring, copilot paths, UI helpers + Streamlit AppTest
```

## Security notes

No secrets in the repo: only `.env.example`; `.env` and `output/` are gitignored. The
generated stack uses **demo-only credentials** (override `DB_PASSWORD` / `RABBITMQ_PASSWORD`
via env). Scanned code is sent to the configured LLM endpoint; use a self-hosted model
(Ollama) for sensitive repos.

## License

Apache License 2.0, see [LICENSE](LICENSE).
