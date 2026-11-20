"""Demo "photo upload" service - migrated to open-source backing services.

POST /upload  (multipart, field "file")
  1. stores the file in S3-compatible storage (SeaweedFS/Garage/MinIO via endpoint_url)
  2. writes metadata to PostgreSQL
  3. publishes a processing job to RabbitMQ (consumed by worker.py)
"""
import json
import os
import time
import uuid

import boto3
import pika
import psycopg2
from flask import Flask, jsonify, request

S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "egress-demo-uploads")
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/%2F")
QUEUE_NAME = os.environ.get("QUEUE_NAME", "egress-demo-jobs")
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_NAME = os.environ.get("DB_NAME", "uploads")
DB_USER = os.environ.get("DB_USER", "app")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

app = Flask(__name__)


def s3_client():
    # Same S3 API, vendor-neutral endpoint: only endpoint_url + static keys change.
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        region_name=S3_REGION,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY"),
    )


def publish_job(payload: dict):
    conn = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    try:
        ch = conn.channel()
        ch.queue_declare(queue=QUEUE_NAME, durable=True)
        ch.basic_publish(
            exchange="",
            routing_key=QUEUE_NAME,
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2),
        )
    finally:
        conn.close()


def db_conn():
    return psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode=os.environ.get("DB_SSLMODE", "disable"),
        connect_timeout=5,
    )


def init_backends(retries: int = 30):
    for attempt in range(retries):
        try:
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
            s3 = s3_client()
            existing = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
            if S3_BUCKET not in existing:
                s3.create_bucket(Bucket=S3_BUCKET)
            return
        except Exception as e:  # backends still starting
            print(f"waiting for backends ({attempt + 1}/{retries}): {e}", flush=True)
            time.sleep(2)
    raise SystemExit("backends not reachable")


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/uploads")
def list_uploads():
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, filename, size_bytes, status FROM uploads ORDER BY created_at DESC LIMIT 20")
        rows = [dict(id=str(r[0]), filename=r[1], size=r[2], status=r[3]) for r in cur.fetchall()]
    return jsonify(rows)


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="missing multipart field 'file'"), 400

    upload_id = str(uuid.uuid4())
    key = f"uploads/{upload_id}/{f.filename}"
    body = f.read()

    # 1) Object storage: S3-compatible (SeaweedFS / Garage / MinIO)
    s3_client().put_object(Bucket=S3_BUCKET, Key=key, Body=body)

    # 2) Metadata: PostgreSQL
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO uploads (id, filename, s3_key, size_bytes) VALUES (%s, %s, %s, %s)",
            (upload_id, f.filename, key, len(body)),
        )

    # 3) Async job: RabbitMQ
    publish_job({"upload_id": upload_id, "bucket": S3_BUCKET, "key": key})

    return jsonify(id=upload_id, key=key, size=len(body), status="queued"), 201


if __name__ == "__main__":
    init_backends()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
