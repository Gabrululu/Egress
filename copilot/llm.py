"""OpenAI-compatible chat client + prompt building (works with Mistral, OpenAI, Ollama, vLLM)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import LLMConfig

MAX_FILE_CHARS = 3500
MAX_SNIPPET_CHARS = 14000
MAX_FINDINGS = 60

SYSTEM_PROMPT = """You are a senior cloud migration engineer. You migrate applications away from
AWS lock-in to open-source, self-hostable, EU-sovereign alternatives.
Reply with ONE JSON object only (no markdown fences, no prose) with exactly these keys:

"mapping": list of {"aws": str, "alternative": str, "reason": str}
    Use: S3 -> SeaweedFS (S3-compatible; Garage/MinIO also fine), RDS -> PostgreSQL,
    SQS -> RabbitMQ, SNS -> RabbitMQ fanout, Lambda -> container worker,
    DynamoDB -> PostgreSQL JSONB or ScyllaDB, CloudWatch -> Prometheus/Grafana/Loki,
    IAM -> Keycloak / static service credentials. Only include services found in the scan.
"docker_compose": str  -- a complete docker-compose.yml (YAML text) that MUST follow this contract:
    top-level `name: egress-migrated`; services named exactly
      postgres (image postgres:16-alpine, port 5432, db uploads, user app),
      s3 (image chrislusf/seaweedfs:3.97, command
          "server -dir=/data -ip=s3 -ip.bind=0.0.0.0 -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json",
          volume ./seaweedfs/s3.json:/etc/seaweedfs/s3.json:ro, ports "9000:8333" and "9001:8888"),
      rabbitmq (image rabbitmq:3.13-management-alpine, ports 5672 and 15672, user egress),
      api (build: ./app, port "5000:5000"),
      worker (build: ./app, command ["python", "worker.py"]).
    api and worker environment MUST define: S3_ENDPOINT_URL=http://s3:8333, S3_ACCESS_KEY=egress,
      S3_SECRET_KEY=egress-demo-secret, S3_BUCKET, AMQP_URL (amqp://egress:<pw>@rabbitmq:5672/%2F),
      QUEUE_NAME, DB_HOST=postgres, DB_NAME, DB_USER, DB_PASSWORD.
    Use healthchecks and depends_on condition: service_healthy. No AWS services, no awslogs driver.
"diffs": list of {"file": str, "diff": str} -- unified diffs (---/+++/@@) for the code changes:
    boto3 S3 client -> endpoint_url + static keys; SQS send_message -> pika publish to RabbitMQ;
    Lambda handler -> long-running pika consumer worker.py; RDS_ENDPOINT -> DB_HOST.
"plan": str -- Markdown migration plan with sections: Steps, Risks, Effort estimate (person-days per service, total).
"""


class LLMError(RuntimeError):
    pass


def build_user_prompt(scan_report: dict, target: Path) -> str:
    compact = {
        "overall_score": scan_report.get("overall_score"),
        "services": {k: {"score": v["score"], "findings": v["findings"]}
                     for k, v in scan_report.get("services", {}).items()},
        "findings": [
            {k: f[k] for k in ("service", "file", "line", "evidence", "severity")}
            for f in scan_report.get("findings", [])[:MAX_FINDINGS]
        ],
    }
    parts = ["## Scan result\n" + json.dumps(compact, indent=1), "## Relevant files"]
    budget = MAX_SNIPPET_CHARS
    base = target if target.is_dir() else target.parent
    for rel in sorted({f["file"] for f in scan_report.get("findings", [])}):
        if budget <= 0:
            parts.append("(more files truncated)")
            break
        try:
            text = (base / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunk = text[: min(MAX_FILE_CHARS, budget)]
        if len(chunk) < len(text):
            chunk += "\n... (truncated)"
        budget -= len(chunk)
        parts.append(f"### {rel}\n```\n{chunk}\n```")
    parts.append("Return the JSON object now.")
    return "\n\n".join(parts)


def parse_json_reply(text: str) -> dict:
    if not text:
        raise LLMError("empty LLM reply")
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("no JSON object in LLM reply")
    try:
        data = json.loads(t[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON from LLM: {e}") from e
    if not isinstance(data, dict):
        raise LLMError("LLM JSON is not an object")
    return data


def chat_json(system: str, user: str, cfg: LLMConfig) -> dict:
    try:
        import openai
    except ImportError as e:
        raise LLMError("openai package not installed") from e
    client = openai.OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "not-needed",
                           timeout=cfg.timeout, max_retries=1)
    kwargs = dict(model=cfg.model, temperature=0.2,
                  messages=[{"role": "system", "content": system},
                            {"role": "user", "content": user}])
    try:
        try:
            resp = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
        except openai.BadRequestError:
            resp = client.chat.completions.create(**kwargs)  # provider without JSON mode
    except openai.APITimeoutError as e:
        raise LLMError(f"LLM timeout after {cfg.timeout:.0f}s") from e
    except openai.APIError as e:
        raise LLMError(f"LLM API error: {e.__class__.__name__}: {str(e)[:200]}") from e
    except Exception as e:  # network, DNS, etc.
        raise LLMError(f"LLM call failed: {e.__class__.__name__}: {str(e)[:200]}") from e
    if not resp.choices:
        raise LLMError("LLM returned no choices")
    return parse_json_reply(resp.choices[0].message.content or "")
