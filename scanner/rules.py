"""Detection rules and per-service lock-in profiles.

A rule is a regex applied line-by-line to files of a given kind.
File kinds: terraform (*.tf), compose (docker-compose*.y[a]ml / compose*.y[a]ml),
python (*.py), requirements (requirements*.txt).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEVERITY_WEIGHT = {"low": 0.3, "medium": 0.6, "high": 1.0}

# Intrinsic lock-in profile per service, each dimension 0-100:
#   api    = how proprietary the API/programming model is
#   data   = how proprietary the data format / semantics are
#   egress = how expensive it is to move data out (egress fees, volume)
SERVICE_PROFILES: dict[str, dict] = {
    "S3": {"api": 70, "data": 40, "egress": 90,
           "alternative": "MinIO / Garage / Ceph RGW (S3-compatible)"},
    "RDS": {"api": 40, "data": 30, "egress": 70,
            "alternative": "PostgreSQL (self-hosted / EU managed)"},
    "DynamoDB": {"api": 95, "data": 90, "egress": 70,
                 "alternative": "PostgreSQL JSONB / ScyllaDB"},
    "SQS": {"api": 85, "data": 50, "egress": 30,
            "alternative": "RabbitMQ"},
    "SNS": {"api": 85, "data": 40, "egress": 30,
            "alternative": "RabbitMQ fanout / NATS"},
    "Lambda": {"api": 90, "data": 60, "egress": 30,
               "alternative": "Container worker (Docker) / OpenFaaS / Knative"},
    "CloudWatch": {"api": 75, "data": 60, "egress": 40,
                   "alternative": "Prometheus + Grafana + Loki"},
    "IAM": {"api": 90, "data": 20, "egress": 10,
            "alternative": "Keycloak / static service credentials"},
    "boto3": {"api": 60, "data": 10, "egress": 10,
              "alternative": "Vendor-neutral clients (S3-compatible SDK, pika, psycopg2)"},
}


@dataclass(frozen=True)
class Rule:
    id: str
    service: str
    kinds: tuple[str, ...]
    pattern: re.Pattern
    severity: str
    reason: str


def _r(id, service, kinds, pattern, severity, reason, flags=0) -> Rule:
    return Rule(id, service, tuple(kinds), re.compile(pattern, flags), severity, reason)


PY, TF, COMPOSE, REQ = "python", "terraform", "compose", "requirements"
ANY = (PY, TF, COMPOSE)

_client = r"""boto3\.(?:client|resource)\(\s*['"]{svc}['"]"""

RULES: list[Rule] = [
    # ---------------- S3
    _r("tf-s3", "S3", [TF], r'resource\s+"aws_s3_bucket\w*"', "high",
       "S3 bucket managed by Terraform AWS provider; data lives in AWS and incurs egress fees to leave."),
    _r("py-s3-client", "S3", [PY], _client.format(svc="s3"), "high",
       "boto3 S3 client bound to AWS endpoint and IAM credential chain."),
    _r("py-s3-calls", "S3", [PY], r"\.(?:put_object|get_object|upload_file|download_file|generate_presigned_url)\(",
       "low", "S3 API call (portable to S3-compatible stores only via endpoint_url)."),
    _r("any-s3-host", "S3", ANY, r"s3[.-][\w-]*\.?amazonaws\.com", "medium",
       "Hard-coded S3 hostname."),
    # ---------------- RDS
    _r("tf-rds", "RDS", [TF], r'resource\s+"aws_(?:db_instance|rds_cluster\w*)"', "high",
       "Managed RDS instance: backups, parameter groups, snapshots are AWS-specific."),
    _r("any-rds-host", "RDS", ANY, r"\.rds\.amazonaws\.com", "medium",
       "Hard-coded RDS endpoint hostname."),
    _r("py-rds-env", "RDS", [PY, COMPOSE], r"\bRDS_ENDPOINT\b", "low",
       "Configuration coupled to RDS naming."),
    _r("py-rds-client", "RDS", [PY], _client.format(svc="rds(?:-data)?"), "high",
       "boto3 RDS / RDS Data API client (proprietary control/data plane)."),
    # ---------------- DynamoDB
    _r("tf-dynamo", "DynamoDB", [TF], r'resource\s+"aws_dynamodb_\w+"', "high",
       "DynamoDB table: proprietary data model and query API."),
    _r("py-dynamo", "DynamoDB", [PY], _client.format(svc="dynamodb"), "high",
       "boto3 DynamoDB client: proprietary API and item format."),
    _r("py-dynamo-ops", "DynamoDB", [PY], r"\.(?:put_item|get_item|query|scan|batch_write_item)\(\s*(?:TableName|Key|Item)",
       "medium", "DynamoDB item operations."),
    # ---------------- SQS
    _r("tf-sqs", "SQS", [TF], r'resource\s+"aws_sqs_queue\w*"', "high",
       "SQS queue: proprietary queue semantics (visibility timeout, receipt handles)."),
    _r("py-sqs-client", "SQS", [PY], _client.format(svc="sqs"), "high",
       "boto3 SQS client: proprietary messaging API."),
    _r("py-sqs-calls", "SQS", [PY], r"\.(?:send_message|send_message_batch|receive_message|delete_message)\(",
       "medium", "SQS API call."),
    _r("any-sqs-url", "SQS", ANY, r"sqs\.[\w-]+\.amazonaws\.com", "medium",
       "Hard-coded SQS queue URL."),
    # ---------------- SNS
    _r("tf-sns", "SNS", [TF], r'resource\s+"aws_sns_\w+"', "high",
       "SNS topic/subscription: proprietary pub/sub."),
    _r("py-sns", "SNS", [PY], _client.format(svc="sns"), "high",
       "boto3 SNS client."),
    # ---------------- Lambda
    _r("tf-lambda", "Lambda", [TF], r'resource\s+"aws_lambda_function"', "high",
       "Lambda function: AWS-only runtime, packaging and triggers."),
    _r("tf-lambda-esm", "Lambda", [TF], r'resource\s+"aws_lambda_(?:event_source_mapping|permission)"',
       "medium", "Lambda trigger wiring is AWS-specific."),
    _r("py-lambda-handler", "Lambda", [PY], r"def\s+\w*handler\s*\(\s*event\s*,\s*context", "high",
       "Lambda handler(event, context) programming model."),
    _r("py-lambda-records", "Lambda", [PY], r"""event\[\s*['"]Records['"]\s*\]""", "medium",
       "Consumes AWS event envelope (Records)."),
    _r("py-lambda-context", "Lambda", [PY], r"context\.(?:aws_request_id|function_name|get_remaining_time_in_millis)",
       "low", "Uses Lambda context object."),
    _r("py-lambda-client", "Lambda", [PY], _client.format(svc="lambda"), "high",
       "boto3 Lambda client (invoke)."),
    # ---------------- CloudWatch
    _r("tf-cw", "CloudWatch", [TF], r'resource\s+"aws_cloudwatch_\w+"', "medium",
       "CloudWatch logs/metrics/alarms resource."),
    _r("compose-awslogs", "CloudWatch", [COMPOSE], r"driver:\s*['\"]?awslogs|awslogs-group", "high",
       "Container logs shipped to CloudWatch via awslogs driver."),
    _r("py-cw", "CloudWatch", [PY], _client.format(svc="(?:logs|cloudwatch)"), "high",
       "boto3 CloudWatch client."),
    # ---------------- IAM
    _r("tf-iam", "IAM", [TF], r'resource\s+"aws_iam_\w+"', "medium",
       "IAM roles/policies: AWS-specific identity and authorization model."),
    _r("any-iam-arn", "IAM", ANY, r"arn:aws:iam::", "medium",
       "References AWS IAM ARNs."),
    _r("any-aws-keys", "IAM", [COMPOSE, PY], r"\bAWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)\b", "low",
       "AWS credential chain."),
    _r("py-iam", "IAM", [PY], _client.format(svc="(?:iam|sts)"), "high",
       "boto3 IAM/STS client."),
    # ---------------- boto3 / SDK
    _r("py-boto3-import", "boto3", [PY], r"^\s*(?:import\s+boto3|from\s+boto3|import\s+botocore|from\s+botocore)",
       "medium", "Code depends directly on the AWS SDK."),
    _r("req-boto3", "boto3", [REQ], r"^\s*(?:boto3|botocore|aiobotocore|awswrangler)\b", "medium",
       "AWS SDK pinned as dependency.", re.IGNORECASE),
    _r("req-lambda-libs", "Lambda", [REQ], r"^\s*(?:aws-lambda-powertools|mangum|chalice|zappa)\b", "medium",
       "Lambda-specific framework dependency.", re.IGNORECASE),
]
