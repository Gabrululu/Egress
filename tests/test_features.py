"""Before/after score, generated secrets, prompt privacy (redaction, file selection), Codespaces URLs."""
import json
import stat
import subprocess
from pathlib import Path

import pytest

import copilot.generator as gen
from copilot import deploy
from copilot.config import LLMConfig
from copilot.llm import build_user_prompt, candidate_files, prompt_preview, redact
from scanner import scan

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_app"
NO_KEY = LLMConfig(api_key="", base_url="https://api.example.com/v1")


@pytest.fixture(autouse=True)
def no_legacy_volume(monkeypatch):
    monkeypatch.setattr(gen, "_volume_exists", lambda name: False)


# ---------------------------------------------------------------- A: mitigation + after score
def test_custom_endpoint_mitigates_s3_findings(tmp_path):
    (tmp_path / "a.py").write_text('import boto3\ns3 = boto3.client("s3", endpoint_url=URL)\ns3.put_object(Bucket="b")\n')
    (tmp_path / "requirements.txt").write_text("boto3\n")
    r = scan(tmp_path)
    assert r["overall_score"] == 0 and r["mitigated"] == 4
    assert all(f["mitigated"] and "Mitigated" in f["lock_in_reason"] for f in r["findings"])


def test_aws_endpoint_is_not_mitigation(tmp_path):
    (tmp_path / "a.py").write_text(
        'import boto3\ns3 = boto3.client("s3", endpoint_url="https://s3.eu-west-1.amazonaws.com")\n')
    r = scan(tmp_path)
    assert r["mitigated"] == 0 and r["overall_score"] > 0


def test_sqs_not_mitigated_by_s3_endpoint(tmp_path):
    (tmp_path / "a.py").write_text('import boto3\nq = boto3.client("sqs")\ns3 = boto3.client("s3", endpoint_url=U)\n')
    r = scan(tmp_path)
    assert "SQS" in r["services"]


def test_sample_app_score_unchanged():
    assert scan(SAMPLE)["overall_score"] == 63


def test_after_scan_drops_to_zero(tmp_path):
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    assert r["after"]["overall_score"] == 0
    assert r["after"]["mitigated"] > 0


# ---------------------------------------------------------------- D: secrets
def test_secrets_generated_and_wired(tmp_path):
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    env = gen._read_env(tmp_path / ".env")
    assert set(gen.SECRET_KEYS) <= env.keys()
    assert all(len(env[k]) >= 20 and env[k] != "egress-demo" for k in gen.SECRET_KEYS)
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    s3 = json.loads((tmp_path / "seaweedfs" / "s3.json").read_text())
    assert s3["identities"][0]["credentials"][0]["secretKey"] == env["S3_SECRET_KEY"]
    assert sorted(r["secrets"]["created"]) == sorted(gen.SECRET_KEYS)
    # secrets never end up in result.json / compose text
    blob = (tmp_path / "result.json").read_text() + r["compose"]
    assert not any(env[k] in blob for k in gen.SECRET_KEYS)
    # docker compose resolves them from output/.env
    out = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=tmp_path,
                         capture_output=True, text=True, check=True).stdout
    cfg = json.loads(out)
    assert cfg["services"]["api"]["environment"]["DB_PASSWORD"] == env["DB_PASSWORD"]
    assert env["RABBITMQ_PASSWORD"] in cfg["services"]["worker"]["environment"]["AMQP_URL"]


def test_secrets_reused_on_regenerate(tmp_path):
    gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    first = gen._read_env(tmp_path / ".env")
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    assert gen._read_env(tmp_path / ".env") == first
    assert r["secrets"]["created"] == []


def test_legacy_db_password_with_existing_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "_volume_exists", lambda name: True)
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    assert gen._read_env(tmp_path / ".env")["DB_PASSWORD"] == gen.LEGACY_DB_PASSWORD
    assert r["secrets"]["legacy_db_password"] is True


def test_wipe_rotates_credentials(tmp_path, monkeypatch):
    gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    before = gen._read_env(tmp_path / ".env")
    calls = []
    monkeypatch.setattr(deploy, "_compose", lambda out, *a, **k: calls.append(a) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    deploy.down(tmp_path, wipe=True)
    assert "--volumes" in calls[0]
    after = gen._read_env(tmp_path / ".env")
    assert all(after[k] != before[k] for k in gen.SECRET_KEYS)
    s3 = json.loads((tmp_path / "seaweedfs" / "s3.json").read_text())
    assert s3["identities"][0]["credentials"][0]["secretKey"] == after["S3_SECRET_KEY"]


# ---------------------------------------------------------------- G: prompt privacy
@pytest.mark.parametrize("text, hidden", [
    ('key = "AKIAIOSFODNN7EXAMPLE"', "AKIAIOSFODNN7EXAMPLE"),
    ('password = "hunter2secret"', "hunter2secret"),
    ("  POSTGRES_PASSWORD: supersecret123", "supersecret123"),
    ("API_TOKEN=abcd1234efgh", "abcd1234efgh"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "MIIabc"),
])
def test_redaction_hides_secrets(text, hidden):
    out, n = redact(text)
    assert hidden not in out and n >= 1


@pytest.mark.parametrize("text", [
    'DB_PASSWORD = os.environ.get("DB_PASSWORD", "")',
    "  DB_PASSWORD: ${DB_PASSWORD}",
    "  password = var.db_password",
])
def test_redaction_keeps_code_references(text):
    assert redact(text) == (text, 0)


def test_include_files_controls_context():
    report = scan(SAMPLE)
    files = candidate_files(report)
    assert "main.tf" in files and "app.py" in files
    only = build_user_prompt(report, SAMPLE, ["app.py"])
    assert "### app.py" in only and "### main.tf" not in only and "excluded by the user" in only
    pv = prompt_preview(report, SAMPLE, ["app.py"])
    assert len(pv["files"]) == 1 and pv["est_input_tokens"] > 0
    assert len(prompt_preview(report, SAMPLE)["files"]) == len(files)


def test_mitigated_files_not_sent(tmp_path):
    (tmp_path / "ok.py").write_text('import boto3\nc = boto3.client("s3", endpoint_url=U)\n')
    (tmp_path / "bad.py").write_text('import boto3\nq = boto3.client("sqs")\n')
    assert candidate_files(scan(tmp_path)) == ["bad.py"]


# ---------------------------------------------------------------- B: Codespaces URLs
def test_public_url(monkeypatch):
    import ui_theme as ui
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    assert ui.public_url(5000, "/health") == "http://localhost:5000/health"
    monkeypatch.setenv("CODESPACE_NAME", "my-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    assert ui.public_url(15672) == "https://my-space-15672.app.github.dev"


# ---------------------------------------------------------------- C: downloads
def test_report_and_zip_exclude_secrets(tmp_path):
    import io
    import zipfile

    from copilot.report import build_report_md, build_zip

    report = scan(SAMPLE)
    m = gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY, scan_report=report)
    md = build_report_md(report, m)
    assert "Lock-in score (before):** 63/100" in md and "(after migration):** 0/100" in md
    assert "## Findings" in md and "## Migration plan" in md and "`main.tf:" in md

    env = gen._read_env(tmp_path / ".env")
    data = build_zip(tmp_path, md)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert "egress-migration/docker-compose.yml" in names and "egress-migration/REPORT.md" in names
    assert "egress-migration/app/app.py" in names and "egress-migration/migration.diff" in names
    assert not any(n.endswith("/.env") or n.endswith(".deploy.log") for n in names)
    everything = b"".join(z.read(n) for n in names)
    assert not any(env[k].encode() in everything for k in gen.SECRET_KEYS)
    assert b"CHANGE_ME" in z.read("egress-migration/seaweedfs/s3.json")


def test_report_without_migration():
    from copilot.report import build_report_md
    md = build_report_md(scan(SAMPLE))
    assert "## Services" in md and "Migration plan" not in md
