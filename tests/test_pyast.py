"""AST detection of AWS SDK clients: cases the line regexes miss."""
import pytest

from scanner import scan


def _scan(tmp_path, code):
    (tmp_path / "m.py").write_text(code)
    return scan(tmp_path)


def _svc_lines(r, svc):
    return [f["line"] for f in r["findings"] if f["service"] == svc]


@pytest.mark.parametrize("code, svc, line", [
    ('import boto3\nc = boto3.client(\n    "s3",\n    region_name="eu-west-1",\n)\n', "S3", 2),
    ('import boto3 as b3\nq = b3.client("sqs")\n', "SQS", 2),
    ('from boto3 import client\nt = client("sns")\n', "SNS", 2),
    ('from boto3 import resource as res\nd = res("dynamodb")\n', "DynamoDB", 2),
    ('import boto3\ns = boto3.Session(profile_name="x")\nl = s.client("lambda")\n', "Lambda", 3),
    ('import boto3\nlogs = boto3.session.Session().client("logs")\n', "CloudWatch", 2),
    ('from boto3.session import Session\nsess = Session()\ni = sess.client("iam")\n', "IAM", 3),
    ('import boto3\nr = boto3.client(service_name="rds")\n', "RDS", 2),
])
def test_ast_detects(tmp_path, code, svc, line):
    r = _scan(tmp_path, code)
    assert _svc_lines(r, svc) == [line], r["findings"]


def test_unknown_service_and_dynamic_name(tmp_path):
    r = _scan(tmp_path, 'import boto3\ne = boto3.client("ec2")\nx = boto3.client(name)\n')
    other = [f for f in r["findings"] if f["rule"] == "py-ast-client-other"]
    assert len(other) == 2 and "'ec2'" in other[0]["lock_in_reason"]


def test_no_duplicates_with_regex(tmp_path):
    r = _scan(tmp_path, 'import boto3\nc = boto3.client("s3")\n')
    assert _svc_lines(r, "S3") == [2] and len([f for f in r["findings"] if f["rule"] == "py-boto3-import"]) == 1


def test_per_call_mitigation(tmp_path):
    code = ('import boto3\n'
            'minio = boto3.client("s3", endpoint_url=URL)\n'
            'aws = boto3.client("s3")\n')
    r = _scan(tmp_path, code)
    s3 = {f["line"]: f["mitigated"] for f in r["findings"] if f["service"] == "S3"}
    assert s3 == {2: True, 3: False}
    # the file still talks to real AWS S3, so the SDK import is not mitigated
    imp = next(f for f in r["findings"] if f["rule"] == "py-boto3-import")
    assert imp["mitigated"] is False and r["overall_score"] > 0


def test_endpoint_on_non_s3_service_is_not_mitigation(tmp_path):
    r = _scan(tmp_path, 'import boto3\nq = boto3.client("sqs", endpoint_url=URL)\n')
    assert not any(f["mitigated"] for f in r["findings"]) and "SQS" in r["services"]


def test_unrelated_client_method_ignored(tmp_path):
    r = _scan(tmp_path, 'import requests\nhttp = requests.Session()\nhttp.client("s3")\n')
    assert r["findings"] == []


def test_syntax_error_falls_back_to_regex(tmp_path):
    r = _scan(tmp_path, 'print "py2"\nimport boto3\nc = boto3.client("sqs")\n')
    assert "SQS" in r["services"]
