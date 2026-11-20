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


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Never read/write the real repo cache from tests."""
    monkeypatch.setattr(gen, "CACHE_DIR", tmp_path_factory.mktemp("llm-cache"))


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


def test_chat_json_retries_without_temperature(monkeypatch):
    """Reasoning models (GPT-5 / o-series) reject temperature: client retries without it."""
    import httpx
    import openai

    from copilot.llm import chat_json

    calls = []

    class FakeCompletions:
        def create(self, **kw):
            calls.append(kw)
            if "temperature" in kw:
                raise openai.BadRequestError(
                    "temperature not supported",
                    response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
            msg = type("M", (), {"content": '{"ok": true}'})
            return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    class FakeClient:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    assert chat_json("s", "u", KEY) == {"ok": True}
    assert len(calls) == 2 and "temperature" not in calls[1] and "response_format" in calls[1]


EVIL = fallback.compose_text().replace(
    "    restart: unless-stopped\n\n  worker:",
    "    restart: unless-stopped\n    privileged: true\n    volumes:\n"
    "      - /var/run/docker.sock:/var/run/docker.sock\n\n  worker:", 1)


def test_llm_compose_blocked_by_security_policy(tmp_path, monkeypatch):
    assert "privileged: true" in EVIL
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply(EVIL))
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY)
    assert r["sources"]["compose"] == "fallback"
    assert any("security policy" in e for e in r["errors"])
    assert "privileged" not in (tmp_path / "docker-compose.yml").read_text()
    assert r["llm"]["repair"] == "failed"


def test_repair_loop_fixes_compose(tmp_path, monkeypatch):
    calls = []

    def fake(system, user, cfg, history=None):
        calls.append(history)
        return _llm_reply("services: [oops") if history is None else _llm_reply()
    monkeypatch.setattr(gen, "chat_json", fake)
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY)
    assert r["path"] == "llm" and r["llm"]["repair"] == "fixed" and r["llm"]["calls"] == 2
    assert "rejected by automated checks" in calls[1][-1]["content"]
    assert calls[1][0]["role"] == "assistant"


def test_cache_hit_skips_llm(tmp_path, monkeypatch):
    n = {"calls": 0}

    def fake(*a, **k):
        n["calls"] += 1
        return _llm_reply()
    monkeypatch.setattr(gen, "chat_json", fake)
    cache = tmp_path / "cache"
    r1 = gen.generate(SAMPLE, out_dir=tmp_path / "o1", cfg=KEY, cache_dir=cache)
    r2 = gen.generate(SAMPLE, out_dir=tmp_path / "o2", cfg=KEY, cache_dir=cache)
    assert n["calls"] == 1
    assert r1["llm"]["cache"] == "miss" and r2["llm"]["cache"] == "hit" and r2["path"] == "llm"
    r3 = gen.generate(SAMPLE, out_dir=tmp_path / "o3", cfg=KEY, cache_dir=cache, use_cache=False)
    assert n["calls"] == 2 and r3["llm"]["cache"] == "off"


def test_invalid_results_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply("services: [oops"))
    cache = tmp_path / "cache"
    gen.generate(SAMPLE, out_dir=tmp_path / "o", cfg=KEY, cache_dir=cache)
    assert not cache.exists() or not list(cache.iterdir())


def test_chat_json_prefers_strict_schema(monkeypatch):
    import openai

    from copilot.llm import chat_json

    calls = []

    class FakeCompletions:
        def create(self, **kw):
            calls.append(kw)
            msg = type("M", (), {"content": '{"ok": 1}'})
            return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    class FakeClient:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    chat_json("s", "u", KEY, history=[{"role": "user", "content": "fix"}])
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert calls[0]["messages"][-1]["content"] == "fix"
