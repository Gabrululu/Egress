"""Demo "photo upload" service built directly on AWS managed services.

POST /upload  (multipart, field "file")
  1. stores the file in Amazon S3          (boto3 s3.put_object)
  2. writes metadata to Amazon RDS Postgres (psycopg2, host = RDS endpoint)
  3. enqueues a processing job on Amazon SQS (boto3 sqs.send_message)

All endpoints/credentials come from env vars, so a migration only needs
config changes plus a few lines (S3 -> MinIO endpoint_url, SQS -> RabbitMQ).
"""
import json
import os
import uuid

import boto3
import psycopg2
from flask import Flask, jsonify, request

AWS_REGION = os.environ.get("AWS_REGION", "eu-west-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "egress-demo-uploads")
SQS_QUEUE_URL = os.environ.get(
    "SQS_QUEUE_URL", "https://sqs.eu-west-1.amazonaws.com/123456789012/egress-demo-jobs"
)
# e.g. egress-demo-db.abcdefgh1234.eu-west-1.rds.amazonaws.com
RDS_ENDPOINT = os.environ.get("RDS_ENDPOINT", "localhost")
DB_NAME = os.environ.get("DB_NAME", "uploads")
DB_USER = os.environ.get("DB_USER", "app")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

app = Flask(__name__)


def s3_client():
    # Proprietary AWS API: credentials resolved via IAM role / env.
    return boto3.client("s3", region_name=AWS_REGION)


def sqs_client():
    return boto3.client("sqs", region_name=AWS_REGION)


def db_conn():
    return psycopg2.connect(
        host=RDS_ENDPOINT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=5,
    )


def init_db():
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS uploads (
                   id UUID PRIMARY KEY,
                   filename TEXT NOT NULL,
                   s3_key TEXT NOT NULL,
                   size_bytes BIGINT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'queued',
                   created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="missing multipart field 'file'"), 400

    upload_id = str(uuid.uuid4())
    key = f"uploads/{upload_id}/{f.filename}"
    body = f.read()

    # 1) Object storage: Amazon S3
    s3_client().put_object(Bucket=S3_BUCKET, Key=key, Body=body)

    # 2) Metadata: Amazon RDS (PostgreSQL engine)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO uploads (id, filename, s3_key, size_bytes) VALUES (%s, %s, %s, %s)",
            (upload_id, f.filename, key, len(body)),
        )

    # 3) Async job: Amazon SQS (consumed by the Lambda in lambda_function.py)
    sqs_client().send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps({"upload_id": upload_id, "bucket": S3_BUCKET, "key": key}),
    )

    return jsonify(id=upload_id, key=key, size=len(body), status="queued"), 201


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
