"""Architecture diagrams + data migration scripts (generation, dry runs, SQS drain semantics)."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

import copilot.generator as gen
from copilot.config import LLMConfig
from copilot.datamig import generate_data_migration, infer_resources
from copilot.diagram import architecture_dot
from scanner import scan

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_app"


@pytest.fixture(autouse=True)
def no_volume(monkeypatch):
    monkeypatch.setattr(gen, "_volume_exists", lambda name: False)


@pytest.fixture(scope="module")
def migdir(tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    (out / ".env").write_text("DB_PASSWORD=dbpw\nRABBITMQ_PASSWORD=rbpw\nS3_SECRET_KEY=s3pw\n")
    meta = generate_data_migration(SAMPLE, scan(SAMPLE), out)
    return Path(meta["dir"]), meta


# ---------------------------------------------------------------- diagrams
def test_before_and_after_diagrams():
    svc = scan(SAMPLE)["services"]
    before, after = architecture_dot(svc), architecture_dot(svc, migrated=True)
    assert before.startswith("digraph") and after.rstrip().endswith("}")
    assert "Amazon S3" in before and "AWS Lambda" in before and "SeaweedFS" not in before
    assert "SeaweedFS" in after and "RabbitMQ" in after and "worker.py" in after and "Amazon" not in after
    assert before.count("{") == before.count("}")


def test_diagram_only_shows_detected_services():
    dot = architecture_dot({"S3"})
    assert "Amazon S3" in dot and "RDS" not in dot and "lambda" not in dot


# ---------------------------------------------------------------- data migration
def test_infer_resources_from_terraform():
    r = infer_resources(SAMPLE)
    assert r["bucket"] == "egress-demo-uploads" and r["queue"] == "egress-demo-jobs"
    assert r["db_name"] == "uploads" and r["db_user"] == "app" and r["region"] == "eu-west-1"
    assert r["queue_url"].endswith("/egress-demo-jobs")


def test_scripts_generated_for_detected_services(migdir):
    d, meta = migdir
    assert meta["scripts"] == ["01_postgres.sh", "02_s3.sh", "03_sqs_to_rabbitmq.py"]
    assert (d / "README.md").is_file() and not (d / "04_dynamodb.md").exists()
    for s in ("01_postgres.sh", "02_s3.sh"):
        assert os.access(d / s, os.X_OK)
        subprocess.run(["bash", "-n", str(d / s)], check=True)
        assert "@sha256:" in (d / s).read_text()  # pinned images
    compile((d / "03_sqs_to_rabbitmq.py").read_text(), "03", "exec")


def test_dynamodb_guide_when_detected(tmp_path):
    meta = generate_data_migration(SAMPLE, {"services": {"DynamoDB": {}}}, tmp_path)
    assert meta["scripts"] == ["04_dynamodb.md"]


def _run(script: Path, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SOURCE_", "TARGET_", "CONFIRM", "AWS_"))}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env={**e, **env}, timeout=30)


def test_postgres_script_requires_inputs_and_defaults_to_dry_run(migdir):
    d, _ = migdir
    r = _run(d / "01_postgres.sh")
    assert r.returncode == 2 and "SOURCE_HOST" in r.stderr
    r = _run(d / "01_postgres.sh", SOURCE_HOST="db.example", SOURCE_PASSWORD="x")
    assert r.returncode == 0 and "[dry-run]" in r.stdout
    assert "app@db.example:5432/uploads" in r.stdout  # inferred defaults; target password from ../.env


def test_s3_script_requires_credentials(migdir):
    d, _ = migdir
    r = _run(d / "02_s3.sh")
    assert r.returncode == 2 and "SOURCE_ACCESS_KEY" in r.stderr


def _load_drain(d: Path):
    spec = importlib.util.spec_from_file_location("drain", d / "03_sqs_to_rabbitmq.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.drain


class FakeSQS:
    def __init__(self, n):
        self.msgs = [{"Body": f"job-{i}", "ReceiptHandle": f"rh-{i}"} for i in range(n)]
        self.deleted = []

    def receive_message(self, **kw):
        live = [m for m in self.msgs if m["ReceiptHandle"] not in self.deleted]
        return {"Messages": live[:kw["MaxNumberOfMessages"]]} if live else {}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


def test_sqs_drain_dry_run_deletes_nothing(migdir):
    drain = _load_drain(migdir[0])
    sqs, sent = FakeSQS(25), []
    stats = drain(sqs, "q", sent.append, execute=False)
    assert stats == {"seen": 10, "moved": 0, "failed": 0} and not sqs.deleted and not sent


def test_sqs_drain_moves_all_messages(migdir):
    drain = _load_drain(migdir[0])
    sqs, sent = FakeSQS(25), []
    stats = drain(sqs, "q", sent.append, execute=True, wait_seconds=0)
    assert stats["moved"] == 25 and len(sqs.deleted) == 25 and sent[0] == b"job-0"


def test_sqs_drain_keeps_unconfirmed_messages(migdir):
    drain = _load_drain(migdir[0])
    sqs = FakeSQS(3)

    def flaky(body):
        if body == b"job-1":
            raise RuntimeError("nack")

    class OnePass(FakeSQS):
        calls = 0

        def receive_message(self, **kw):
            self.calls += 1
            return super().receive_message(**kw) if self.calls == 1 else {}
    sqs = OnePass(3)
    stats = drain(sqs, "q", flaky, execute=True, wait_seconds=0)
    assert stats == {"seen": 3, "moved": 2, "failed": 1} and "rh-1" not in sqs.deleted


def test_generate_includes_data_migration_and_after_ignores_it(tmp_path):
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=LLMConfig(api_key="", base_url="https://x/v1"))
    assert r["data_migration"]["scripts"] == ["01_postgres.sh", "02_s3.sh", "03_sqs_to_rabbitmq.py"]
    assert r["after"]["overall_score"] == 0  # the SQS drain script talks to AWS on purpose
