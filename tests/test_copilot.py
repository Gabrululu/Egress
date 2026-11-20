from pathlib import Path

import pytest

import copilot.generator as gen
from copilot import fallback
from copilot.config import LLMConfig
from copilot.llm import LLMError, parse_json_reply

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_app"
NO_KEY = LLMConfig(api_key="", base_url="https://api.example.com/v1")
KEY = LLMConfig(api_key="test-key", base_url="https://api.example.com/v1", model="m")


def _llm_reply(compose=None):
    return {
        "mapping": [{"aws": "S3", "alternative": "SeaweedFS", "reason": "S3 API"}],
        "docker_compose": compose if compose is not None else fallback.compose_text(),
        "diffs": [{"file": "app.py", "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"}],
        "plan": "# Plan\n\n## Steps\n1. do things\n\n## Risks\n- some\n\n## Effort estimate\n5 days total.",
    }


def test_no_key_uses_fallback(tmp_path):
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    assert r["path"] == "fallback"
    assert r["compose_validation"]["ok"], r["compose_validation"]
    for f in ("docker-compose.yml", "mapping.json", "MIGRATION_PLAN.md", "migration.diff",
              "app/app.py", "app/worker.py", "app/Dockerfile", "seaweedfs/s3.json"):
        assert (tmp_path / f).is_file(), f
    assert {m["aws"] for m in r["mapping"]} >= {"S3", "RDS", "SQS", "Lambda"}
    assert any("lambda_function.py" in d["diff"] for d in r["diffs"])


def test_llm_success(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply())
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY)
    assert r["path"] == "llm", r["errors"]
    assert r["mapping"][0]["alternative"] == "SeaweedFS"


def test_llm_exception_falls_back(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise LLMError("LLM timeout after 60s")
    monkeypatch.setattr(gen, "chat_json", boom)
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY)
    assert r["path"] == "fallback"
    assert "timeout" in r["errors"][0]
    assert r["compose_validation"]["ok"]


def test_llm_invalid_compose_mixed(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply("services: [oops"))
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY)
    assert r["path"] == "mixed"
    assert r["sources"]["compose"] == "fallback" and r["sources"]["mapping"] == "llm"
    assert r["compose_validation"]["ok"]


def test_llm_compose_breaking_contract(tmp_path, monkeypatch):
    bad = "services:\n  web:\n    image: nginx:alpine\n"
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply(bad))
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY)
    assert r["sources"]["compose"] == "fallback"
    assert any("contract" in e for e in r["errors"])


def test_parse_json_reply_fenced():
    assert parse_json_reply('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json_reply("no json here")


def test_regenerate_keeps_gitkeep(tmp_path):
    (tmp_path / ".gitkeep").touch()
    gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    assert (tmp_path / ".gitkeep").exists()
