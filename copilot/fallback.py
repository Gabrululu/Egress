"""Built-in, offline migration templates (used when the LLM is unavailable or invalid)."""
from __future__ import annotations

import difflib
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "templates"

MAPPING = {
    "S3": ("SeaweedFS (S3-compatible API; Garage or MinIO also work)",
           "Same S3 API via endpoint_url, self-hosted, no egress fees."),
    "RDS": ("PostgreSQL 16 (container / EU-hosted managed Postgres)",
            "RDS already runs Postgres: pg_dump/restore, only host + TLS settings change."),
    "DynamoDB": ("PostgreSQL JSONB (or ScyllaDB for very high write volumes)",
                 "Proprietary item model must be remapped to tables/JSONB."),
    "SQS": ("RabbitMQ (AMQP 0-9-1, via pika)",
            "Open protocol; durable queues + acks replace visibility timeout."),
    "SNS": ("RabbitMQ fanout exchange (or NATS)",
            "Pub/sub via exchanges instead of topics/subscriptions."),
    "Lambda": ("Long-running container worker (Docker); OpenFaaS/Knative if FaaS is needed",
               "Plain process consuming the queue; no event envelope or Lambda context."),
    "CloudWatch": ("Docker json-file logs now; Prometheus + Grafana + Loki for production",
                   "Open formats and self-hosted retention."),
    "IAM": ("Static service credentials via env/secrets; Keycloak for user identity",
            "Removes AWS role/policy model; least privilege kept per service."),
    "boto3": ("Keep boto3 for S3-compatible storage only; pika + psycopg2 for the rest",
              "SDK no longer bound to AWS endpoints or credential chain."),
}

EFFORT_DAYS = {"S3": 0.5, "RDS": 1.0, "DynamoDB": 5.0, "SQS": 1.5, "SNS": 1.0,
               "Lambda": 2.0, "CloudWatch": 1.0, "IAM": 1.5, "boto3": 0.5}

RISKS = {
    "S3": "Presigned URLs, bucket policies and lifecycle rules need re-implementation; plan a bulk copy (rclone) and a cut-over window.",
    "RDS": "Data migration downtime (pg_dump/restore or logical replication); backups/PITR become your responsibility.",
    "DynamoDB": "Data model rewrite; query patterns (GSIs, conditional writes) must be redesigned.",
    "SQS": "At-least-once semantics differ (acks vs visibility timeout); make consumers idempotent; drain SQS before cut-over.",
    "SNS": "Subscription filters and HTTP/email targets have no 1:1 equivalent.",
    "Lambda": "Scaling to zero and auto-scaling are lost unless KEDA/Knative is added; cold-start assumptions disappear.",
    "CloudWatch": "Alarms and dashboards must be rebuilt in Grafana/Alertmanager.",
    "IAM": "Credential rotation and secret storage must be provided (Vault / Docker secrets).",
    "boto3": "Low: library stays, only configuration changes.",
}

# (template file, target-repo file to diff against)
DIFF_PAIRS = [
    ("app/app.py", "app.py"),
    ("app/worker.py", "lambda_function.py"),
    ("app/requirements.txt", "requirements.txt"),
    ("docker-compose.yml", "docker-compose.yml"),
]


def mapping(services: list[str]) -> list[dict]:
    rows = [{"aws": s, "alternative": MAPPING[s][0], "reason": MAPPING[s][1]}
            for s in services if s in MAPPING]
    return rows or [{"aws": "-", "alternative": "-", "reason": "No AWS services detected."}]


def compose_text() -> str:
    return (TEMPLATES / "docker-compose.yml").read_text(encoding="utf-8")


def diffs(target: Path) -> list[dict]:
    base = target if target.is_dir() else target.parent
    out = []
    for tmpl, orig_name in DIFF_PAIRS:
        new = (TEMPLATES / tmpl).read_text(encoding="utf-8").splitlines(keepends=True)
        orig_path = base / orig_name
        old = orig_path.read_text(encoding="utf-8").splitlines(keepends=True) if orig_path.is_file() else []
        new_name = Path(tmpl).name if tmpl.startswith("app/") else tmpl
        d = "".join(difflib.unified_diff(old, new,
                                         fromfile=f"a/{orig_name}" if old else "/dev/null",
                                         tofile=f"b/{new_name}"))
        if d:
            out.append({"file": new_name, "diff": d})
    return out


def plan(scan_report: dict) -> str:
    services = [s for s in scan_report.get("services", {}) if s in MAPPING]
    total = sum(EFFORT_DAYS[s] for s in services)
    lines = [
        "# Migration plan (template)",
        "",
        f"Current lock-in score: **{scan_report.get('overall_score', 0)}/100** "
        f"({scan_report.get('rating', 'n/a')}). Target after migration: ~0 (no AWS-only APIs).",
        "",
        "## Steps",
        "1. Stand up the open-source stack locally (`docker compose up -d` in `output/`).",
        "2. Apply the code diffs: S3 client gets `endpoint_url`, SQS calls become pika publishes, "
        "the Lambda handler becomes `worker.py`.",
        "3. Move configuration to env vars (`S3_ENDPOINT_URL`, `AMQP_URL`, `DB_HOST`).",
        "4. Migrate data: `pg_dump | pg_restore` for RDS, `rclone sync` for S3 buckets, drain SQS.",
        "5. Run integration tests against the new stack, then cut over DNS/traffic.",
        "6. Decommission AWS resources (`terraform destroy`) after a rollback window.",
        "",
        "## Risks",
    ]
    lines += [f"- **{s}**: {RISKS[s]}" for s in services] or ["- None detected."]
    lines += ["", "## Effort estimate", "", "| Service | Person-days |", "|---|---|"]
    lines += [f"| {s} | {EFFORT_DAYS[s]} |" for s in services]
    lines += [f"| **Total** | **{total:g}** |", "",
              "Estimate assumes a small service like the sample app; add 30-50% for testing and rollout."]
    return "\n".join(lines) + "\n"
