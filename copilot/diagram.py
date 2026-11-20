"""Before/after architecture diagrams (Graphviz DOT) derived from the scan.

Rendered with `st.graphviz_chart` (client-side, no extra dependency). The topology is the
common shape of the apps Egress targets: an API that stores objects, writes metadata and
enqueues jobs consumed by a function/worker.
"""
from __future__ import annotations

# Data stores / brokers the API talks to (in display order).
STORES = ["S3", "RDS", "DynamoDB", "SQS", "SNS"]
# What the async consumer touches.
WORKER_USES = ["S3", "RDS", "DynamoDB"]

AWS_LABEL = {
    "S3": "Amazon S3\\nobject storage", "RDS": "Amazon RDS\\nPostgreSQL", "DynamoDB": "DynamoDB\\nNoSQL",
    "SQS": "Amazon SQS\\nqueue", "SNS": "Amazon SNS\\npub/sub", "Lambda": "AWS Lambda\\nfunction",
    "CloudWatch": "CloudWatch\\nlogs", "IAM": "IAM\\nroles & policies",
}
OSS_LABEL = {
    "S3": "SeaweedFS\\nS3-compatible", "RDS": "PostgreSQL 16", "DynamoDB": "PostgreSQL JSONB",
    "SQS": "RabbitMQ\\nqueue", "SNS": "RabbitMQ\\nfanout", "Lambda": "worker.py\\ncontainer",
    "CloudWatch": "json-file logs\\n(Loki-ready)", "IAM": ".env credentials\\n(random, chmod 600)",
}
AWS_FILL, AWS_EDGE = "#fff4ec", "#d97757"
OSS_FILL, OSS_EDGE = "#eaf6ff", "#0099ff"
FONT = "Inter, Helvetica, Arial, sans-serif"


def _node(nid: str, label: str, fill: str, border: str, shape: str = "box") -> str:
    return (f'  {nid} [label="{label}", shape={shape}, style="rounded,filled", fillcolor="{fill}", '
            f'color="{border}", penwidth=1.4];')


def architecture_dot(services: list[str] | set[str], migrated: bool = False) -> str:
    """DOT graph for the detected services, as-is (AWS) or migrated (open source)."""
    svc = set(services)
    label = OSS_LABEL if migrated else AWS_LABEL
    fill, border = (OSS_FILL, OSS_EDGE) if migrated else (AWS_FILL, AWS_EDGE)
    title = "After: open-source stack (docker compose)" if migrated else "Before: AWS-managed services"
    lines = [
        "digraph G {",
        f'  graph [rankdir=LR, bgcolor="transparent", fontname="{FONT}", fontsize=16, labelloc=t, '
        f'label="{title}", pad=0.2, nodesep=0.35, ranksep=0.55];',
        f'  node [fontname="{FONT}", fontsize=13, margin="0.22,0.1"];',
        f'  edge [fontname="{FONT}", fontsize=11, color="#9a9a9a", arrowsize=0.7];',
        _node("client", "Client", "#ffffff", "#141414", shape="ellipse"),
        _node("api", "API\\n(Flask container)" if migrated else "API\\n(Flask + boto3)", "#ffffff", "#141414"),
        '  client -> api [label="POST /upload"];',
    ]
    for s in STORES:
        if s in svc:
            lines.append(_node(s.lower(), label[s], fill, border))
            lines.append(f"  api -> {s.lower()};")
    consumer = "Lambda" in svc or "SQS" in svc
    if consumer:
        lines.append(_node("lambda", label["Lambda"], fill, border))
        for q in ("SQS", "SNS"):
            if q in svc:
                lines.append(f'  {q.lower()} -> lambda [label="{"consume" if migrated else "trigger"}"];')
        for s in WORKER_USES:
            if s in svc:
                lines.append(f'  lambda -> {s.lower()} [style=dashed];')
    if "CloudWatch" in svc:
        lines.append(_node("cloudwatch", label["CloudWatch"], fill, border, shape="note"))
        lines.append('  api -> cloudwatch [style=dotted, label="logs"];')
        if consumer:
            lines.append("  lambda -> cloudwatch [style=dotted];")
    if "IAM" in svc:
        lines.append(_node("iam", label["IAM"], fill, border, shape="component"))
        lines.append('  iam -> api [style=dotted, arrowhead=none, label="auth"];')
    lines.append("}")
    return "\n".join(lines)
