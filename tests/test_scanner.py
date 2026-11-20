import json
import subprocess
import sys
from pathlib import Path

import pytest

from scanner import scan
from scanner.engine import overall_score, score_services

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_app"
FIELDS = {"service", "file", "line", "evidence", "lock_in_reason", "severity"}


@pytest.fixture(scope="module")
def report():
    return scan(SAMPLE)


def test_sample_app_core_services(report):
    for svc in ("S3", "RDS", "SQS", "Lambda"):
        assert svc in report["services"], svc
    assert 0 < report["overall_score"] <= 100


def test_sample_app_extra_services(report):
    for svc in ("IAM", "CloudWatch", "boto3"):
        assert svc in report["services"], svc


def test_finding_shape(report):
    assert report["findings"]
    for f in report["findings"]:
        assert FIELDS <= f.keys()
        assert f["severity"] in {"low", "medium", "high"}
        assert f["line"] >= 1


def _scan_one(tmp_path, name, content):
    (tmp_path / name).write_text(content)
    return scan(tmp_path)


def test_python_dynamodb(tmp_path):
    r = _scan_one(tmp_path, "db.py", 'import boto3\nt = boto3.resource("dynamodb")\n')
    assert "DynamoDB" in r["services"]
    f = next(f for f in r["findings"] if f["service"] == "DynamoDB")
    assert f["line"] == 2 and f["severity"] == "high"


def test_terraform_sns_and_lambda(tmp_path):
    r = _scan_one(tmp_path, "main.tf",
                  'resource "aws_sns_topic" "t" {}\nresource "aws_lambda_function" "f" {}\n')
    assert {"SNS", "Lambda"} <= r["services"].keys()


def test_compose_awslogs(tmp_path):
    r = _scan_one(tmp_path, "docker-compose.yml",
                  "services:\n  a:\n    logging:\n      driver: awslogs\n")
    assert "CloudWatch" in r["services"]


def test_requirements_boto3(tmp_path):
    r = _scan_one(tmp_path, "requirements.txt", "flask\nboto3==1.35.0\n")
    assert r["services"]["boto3"]["findings"] == 1


def test_comments_ignored(tmp_path):
    r = _scan_one(tmp_path, "x.py", '# boto3.client("s3")\n')
    assert r["findings"] == [] and r["overall_score"] == 0


def test_skips_venv(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "a.py").write_text('boto3.client("sqs")\n')
    assert scan(tmp_path)["findings"] == []


def test_scoring_formula():
    # one high S3 finding: depth 1.0 -> 0.5*70 + 0.3*40 + 0.2*90 = 65
    s = score_services([{"service": "S3", "severity": "high"}])
    assert s["S3"]["score"] == 65
    # one low finding: depth 0.3 -> round(65*0.3) = 20
    s = score_services([{"service": "S3", "severity": "low"}])
    assert s["S3"]["score"] == 20
    assert overall_score({"a": {"score": 80}, "b": {"score": 40}}) == 70


def test_missing_path():
    with pytest.raises(FileNotFoundError):
        scan("/nonexistent/egress")


def test_cli_json():
    out = subprocess.run([sys.executable, "-m", "scanner", str(SAMPLE)],
                         capture_output=True, text=True, cwd=ROOT, check=True)
    data = json.loads(out.stdout)
    assert data["overall_score"] > 0
