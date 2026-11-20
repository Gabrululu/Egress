"""Container worker replacing the AWS Lambda: consumes jobs from RabbitMQ.

Same business logic as lambda_function.lambda_handler, but a plain long-running
process: no event envelope, no Lambda context, runs anywhere Docker runs.
"""
import hashlib
import json
import os
import time

import boto3
import pika
import psycopg2

AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@localhost:5672/%2F")
QUEUE_NAME = os.environ.get("QUEUE_NAME", "egress-demo-jobs")

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000"),
    region_name=os.environ.get("S3_REGION", "us-east-1"),
    aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
    aws_secret_access_key=os.environ.get("S3_SECRET_KEY"),
)


def db_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        dbname=os.environ.get("DB_NAME", "uploads"),
        user=os.environ.get("DB_USER", "app"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def process_job(job: dict) -> str:
    obj = s3.get_object(Bucket=job["bucket"], Key=job["key"])
    digest = hashlib.sha256(obj["Body"].read()).hexdigest()
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE uploads SET status = %s WHERE id = %s",
            (f"processed:{digest[:12]}", job["upload_id"]),
        )
    return digest


def on_message(ch, method, properties, body):
    try:
        job = json.loads(body)
        digest = process_job(job)
        print(json.dumps({"processed": job["upload_id"], "sha256": digest[:12]}), flush=True)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"job failed: {e}", flush=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def main():
    while True:
        try:
            conn = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
            ch = conn.channel()
            ch.queue_declare(queue=QUEUE_NAME, durable=True)
            ch.basic_qos(prefetch_count=10)
            ch.basic_consume(queue=QUEUE_NAME, on_message_callback=on_message)
            print(f"worker consuming from {QUEUE_NAME}", flush=True)
            ch.start_consuming()
        except pika.exceptions.AMQPConnectionError as e:
            print(f"waiting for rabbitmq: {e}", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
