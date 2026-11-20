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
          "server -dir=/data -ip=s3 -ip.bind=0.0.0.0 -master.volumeSizeLimitMB=64
          -master.volumePreallocate=false -volume.max=50 -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json",
          volume ./seaweedfs/s3.json:/etc/seaweedfs/s3.json:ro, ports "9000:8333" and "9001:8888"),
      rabbitmq (image rabbitmq:3.13-management-alpine, ports 5672 and 15672, user egress,
          RABBITMQ_DEFAULT_PASS=${RABBITMQ_PASSWORD}),
      api (build: ./app, port "5000:5000"),
      worker (build: ./app, command ["python", "worker.py"]).
    api and worker environment MUST define: S3_ENDPOINT_URL=http://s3:8333, S3_ACCESS_KEY=egress,
      S3_SECRET_KEY=${S3_SECRET_KEY}, S3_BUCKET, AMQP_URL=amqp://egress:${RABBITMQ_PASSWORD}@rabbitmq:5672/%2F,
      QUEUE_NAME, DB_HOST=postgres, DB_NAME, DB_USER, DB_PASSWORD=${DB_PASSWORD}.
    Secrets: NEVER write literal passwords. Use ${DB_PASSWORD}, ${RABBITMQ_PASSWORD} and ${S3_SECRET_KEY}
      (postgres POSTGRES_PASSWORD=${DB_PASSWORD}); they are provided by a generated .env file.
    Use healthchecks and depends_on condition: service_healthy. No AWS services, no awslogs driver.
"diffs": list of {"file": str, "diff": str} -- unified diffs (---/+++/@@) for the code changes:
    boto3 S3 client -> endpoint_url + static keys; SQS send_message -> pika publish to RabbitMQ;
    Lambda handler -> long-running pika consumer worker.py; RDS_ENDPOINT -> DB_HOST.
"plan": str -- Markdown migration plan with sections: Steps, Risks, Effort estimate (person-days per service, total).
"""


class LLMError(RuntimeError):
    pass


# ------------------------------------------------------------------ privacy: secret redaction
REDACTED = "[REDACTED]"
_SECRET_PATTERNS = [
    # AWS access key ids
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), lambda m: REDACTED),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     lambda m: REDACTED),
    # name = "literal" / name: 'literal' where name looks secret (code, JSON, quoted YAML)
    (re.compile(r"""(?i)(\b[\w.-]*(?:secret|password|passwd|pwd|token|api[_-]?key|access[_-]?key)[\w.-]*['"]?\s*[:=]\s*)(['"])([^'"\n]{4,})\2"""),
     lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}"),
    # unquoted YAML / .env values (skips ${VAR} references)
    (re.compile(r"""(?im)^(\s*-?\s*[\w.-]*(?:secret|password|passwd|pwd|token|api[_-]?key)[\w.-]*\s*[:=]\s*)(?!\$\{|(?:var|local|data|module|each|self)\.)([^\s'"#{}(),]{4,})(?=\s*(?:\#.*)?$)"""),
     lambda m: f"{m.group(1)}{REDACTED}"),
]


def redact(text: str) -> tuple[str, int]:
    """Mask likely secrets before anything leaves the machine. Returns (text, count)."""
    total = 0
    for pattern, repl in _SECRET_PATTERNS:
        text, n = pattern.subn(repl, text)
        total += n
    return text, total


def candidate_files(scan_report: dict) -> list[str]:
    """Files with (unmitigated) findings: the only code that may be sent to the LLM."""
    return sorted({f["file"] for f in scan_report.get("findings", []) if not f.get("mitigated")})


def _prompt_parts(scan_report: dict, target: Path, include_files: list[str] | None):
    findings = [f for f in scan_report.get("findings", []) if not f.get("mitigated")]
    compact = {
        "overall_score": scan_report.get("overall_score"),
        "services": {k: {"score": v["score"], "findings": v["findings"]}
                     for k, v in scan_report.get("services", {}).items()},
        "findings": [{k: f[k] for k in ("service", "file", "line", "evidence", "severity")}
                     for f in findings[:MAX_FINDINGS]],
    }
    parts = ["## Scan result\n" + json.dumps(compact, indent=1), "## Relevant files"]
    files_meta = []
    budget = MAX_SNIPPET_CHARS
    base = target if target.is_dir() else target.parent
    wanted = candidate_files(scan_report) if include_files is None else include_files
    for rel in sorted(set(wanted) & set(candidate_files(scan_report))):
        if budget <= 0:
            parts.append("(more files truncated)")
            files_meta.append({"file": rel, "chars": 0, "status": "skipped (budget)"})
            continue
        try:
            text = (base / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunk = text[: min(MAX_FILE_CHARS, budget)]
        truncated = len(chunk) < len(text)
        if truncated:
            chunk += "\n... (truncated)"
        budget -= len(chunk)
        parts.append(f"### {rel}\n```\n{chunk}\n```")
        files_meta.append({"file": rel, "chars": len(chunk), "status": "truncated" if truncated else "full"})
    if len(wanted) < len(candidate_files(scan_report)):
        parts.append("(some files were excluded by the user; infer their changes from the scan findings)")
    parts.append("Return the JSON object now.")
    text, n_redacted = redact("\n\n".join(parts))
    return text, files_meta, n_redacted


def build_user_prompt(scan_report: dict, target: Path, include_files: list[str] | None = None) -> str:
    return _prompt_parts(scan_report, Path(target), include_files)[0]


def prompt_preview(scan_report: dict, target: Path, include_files: list[str] | None = None) -> dict:
    """Exactly what would be sent, plus rough size estimates (~4 chars per token)."""
    user, files_meta, n_redacted = _prompt_parts(scan_report, Path(target), include_files)
    chars = len(SYSTEM_PROMPT) + len(user)
    return {"system": SYSTEM_PROMPT, "user": user, "files": files_meta, "redactions": n_redacted,
            "chars": chars, "est_input_tokens": round(chars / 4),
            "est_output_tokens": "4k-8k (compose + diffs + plan)"}


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


_STR = {"type": "string"}
MIGRATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mapping", "docker_compose", "diffs", "plan"],
    "properties": {
        "mapping": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["aws", "alternative", "reason"],
            "properties": {"aws": _STR, "alternative": _STR, "reason": _STR}}},
        "docker_compose": _STR,
        "diffs": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "diff"],
            "properties": {"file": _STR, "diff": _STR}}},
        "plan": _STR,
    },
}
JSON_SCHEMA_FORMAT = {"type": "json_schema",
                      "json_schema": {"name": "egress_migration", "strict": True, "schema": MIGRATION_SCHEMA}}
JSON_OBJECT_FORMAT = {"type": "json_object"}


def chat_json(system: str, user: str, cfg: LLMConfig, history: list[dict] | None = None) -> dict:
    """One chat call returning a JSON object. `history` = extra turns appended after the user
    prompt (used by the repair loop: previous assistant reply + validation feedback)."""
    try:
        import openai
    except ImportError as e:
        raise LLMError("openai package not installed") from e
    client = openai.OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "not-needed",
                           timeout=cfg.timeout, max_retries=1)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}, *(history or [])]
    base = dict(model=cfg.model, messages=messages)
    # Strictest first, then progressively drop what the provider rejects:
    # strict JSON Schema -> JSON mode -> plain text; reasoning models (OpenAI GPT-5 / o-series)
    # also reject a custom temperature.
    attempts = [
        {"response_format": JSON_SCHEMA_FORMAT, "temperature": 0.2},
        {"response_format": JSON_SCHEMA_FORMAT},
        {"response_format": JSON_OBJECT_FORMAT},
        {},
    ]
    try:
        resp = None
        for i, extra in enumerate(attempts):
            try:
                resp = client.chat.completions.create(**base, **extra)
                break
            except openai.BadRequestError:
                if i == len(attempts) - 1:
                    raise
    except openai.APITimeoutError as e:
        raise LLMError(f"LLM timeout after {cfg.timeout:.0f}s") from e
    except openai.APIError as e:
        raise LLMError(f"LLM API error: {e.__class__.__name__}: {str(e)[:200]}") from e
    except Exception as e:  # network, DNS, etc.
        raise LLMError(f"LLM call failed: {e.__class__.__name__}: {str(e)[:200]}") from e
    if not resp.choices:
        raise LLMError("LLM returned no choices")
    return parse_json_reply(resp.choices[0].message.content or "")
