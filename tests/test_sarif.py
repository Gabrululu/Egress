"""SARIF export, CLI CI flags, and scanner precision tweaks (prose strings, inline ignore)."""
import json
import subprocess
import sys
from pathlib import Path

from scanner import scan
from scanner.sarif import to_sarif

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_app"


def test_sarif_structure():
    report = scan(SAMPLE)
    s = to_sarif(report, uri_prefix="sample_app/")
    assert s["version"] == "2.1.0" and len(s["runs"]) == 1
    run = s["runs"][0]
    rules = run["tool"]["driver"]["rules"]
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids))
    assert all(r["properties"]["security-severity"] in {"8.0", "5.0", "2.0"} for r in rules)
    assert len(run["results"]) == len(report["findings"])
    for res in run["results"]:
        assert rules[res["ruleIndex"]]["id"] == res["ruleId"]
        assert res["level"] in {"error", "warning", "note"}
        loc = res["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"].startswith("sample_app/")
        assert loc["region"]["startLine"] >= 1
        assert len(res["partialFingerprints"]["egressFinding/v1"]) == 32
    levels = {r["level"] for r in run["results"]}
    assert {"error", "warning"} <= levels


def test_sarif_marks_mitigated_as_suppressed(tmp_path):
    (tmp_path / "a.py").write_text('import boto3\nc = boto3.client("s3", endpoint_url=U)\n')
    run = to_sarif(scan(tmp_path))["runs"][0]
    assert run["results"] and all("suppressions" in r for r in run["results"])


def test_cli_sarif_and_gate(tmp_path):
    out = tmp_path / "e.sarif"
    r = subprocess.run([sys.executable, "-m", "scanner", str(SAMPLE), "--sarif", str(out), "--fail-above", "50"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 1 and "FAIL" in r.stderr
    assert json.loads(out.read_text())["runs"][0]["results"]
    ok = subprocess.run([sys.executable, "-m", "scanner", str(SAMPLE), "--fail-above", "90", "--summary"],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0


def test_exclude_dir(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "skip" / "deep").mkdir(parents=True)
    (tmp_path / "keep" / "a.py").write_text('import boto3\nboto3.client("sqs")\n')
    (tmp_path / "skip" / "deep" / "b.py").write_text('import boto3\nboto3.client("sns")\n')
    r = scan(tmp_path, exclude_dirs={"skip/deep"})
    assert "SQS" in r["services"] and "SNS" not in r["services"]


def test_multiline_strings_are_prose(tmp_path):
    code = 'PROMPT = """\nUse RDS_ENDPOINT and arn:aws:iam::123 and sqs.eu-west-1.amazonaws.com\n"""\n'
    (tmp_path / "p.py").write_text(code)
    assert scan(tmp_path)["findings"] == []


def test_single_line_string_still_detected(tmp_path):
    (tmp_path / "c.py").write_text('URL = "https://sqs.eu-west-1.amazonaws.com/1/q"\n')
    assert "SQS" in scan(tmp_path)["services"]


def test_inline_ignore(tmp_path):
    (tmp_path / "i.py").write_text('import boto3  # egress: ignore\nq = boto3.client("sqs")  # egress: ignore\n'
                                   'n = boto3.client("sns")\n')
    r = scan(tmp_path)
    assert set(r["services"]) == {"SNS"}


def test_requirements_mitigation_is_per_directory(tmp_path):
    (tmp_path / "new").mkdir()
    (tmp_path / "old").mkdir()
    (tmp_path / "new" / "app.py").write_text('import boto3\nc = boto3.client("s3", endpoint_url=U)\n')
    (tmp_path / "new" / "requirements.txt").write_text("boto3\n")
    (tmp_path / "old" / "app.py").write_text('import boto3\nc = boto3.client("s3")\n')
    (tmp_path / "old" / "requirements.txt").write_text("boto3\n")
    req = {f["file"]: f["mitigated"] for f in scan(tmp_path)["findings"] if f["rule"] == "req-boto3"}
    assert req == {"new/requirements.txt": True, "old/requirements.txt": False}
