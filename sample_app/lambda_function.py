"""AWS Lambda handler triggered by SQS (event source mapping in main.tf).

Reads the uploaded object from S3, computes a checksum and marks the row
as processed in RDS. The handler(event, context) signature + SQS "Records"
event shape are AWS-specific.
"""
import hashlib
import json
import os

import boto3
import psycopg2

s3 = boto3.client("s3")


def lambda_handler(event, context):
    conn = psycopg2.connect(
        host=os.environ["RDS_ENDPOINT"],
        dbname=os.environ.get("DB_NAME", "uploads"),
        user=os.environ.get("DB_USER", "app"),
        password=os.environ.get("DB_PASSWORD", ""),
    )
    processed = 0
    with conn, conn.cursor() as cur:
        for record in event["Records"]:
            job = json.loads(record["body"])
            obj = s3.get_object(Bucket=job["bucket"], Key=job["key"])
            digest = hashlib.sha256(obj["Body"].read()).hexdigest()
            cur.execute(
                "UPDATE uploads SET status = %s WHERE id = %s",
                (f"processed:{digest[:12]}", job["upload_id"]),
            )
            processed += 1
    print(json.dumps({"processed": processed, "request_id": context.aws_request_id}))
    return {"batchItemFailures": []}
