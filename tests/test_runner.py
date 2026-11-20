"""Runtime verification at deploy: a broken LLM-written app is replaced by the reference app."""
import json
import sys
from pathlib import Path

import pytest

import copilot.generator as gen
from copilot import runner
from copilot.config import LLMConfig

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_copilot import _llm_reply  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "sample_app"
KEY = LLMConfig(api_key="k", base_url="https://api.example.com/v1", model="m")


@pytest.fixture
def llm_out(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply())
    out = tmp_path / "out"
    r = gen.generate(SAMPLE, out_dir=out, cfg=KEY, cache_dir=tmp_path / "c")
    assert r["sources"]["diffs"] == "llm" and r["source_patch"]["app_source"] == "patched-repo"
    (out / "app" / "LLM_MARKER").write_text("llm")
    return out


def _fake_docker(monkeypatch, health):
    """health: list of probe results, one per deploy attempt."""
    seq = iter(health)
    ups = []
    monkeypatch.setattr(runner, "compose_up", lambda out, t: ups.append(out) or 0)
    monkeypatch.setattr(runner, "probe", lambda url, t: next(seq))
    monkeypatch.setattr(runner, "service_logs", lambda out, *a, **k: "Traceback: bucket not found")
    return ups


def test_healthy_llm_app_is_kept(llm_out, monkeypatch):
    ups = _fake_docker(monkeypatch, [True])
    o = runner.run(llm_out)
    assert o["ok"] and not o["fallback"] and len(ups) == 1
    assert (llm_out / "app" / "LLM_MARKER").exists()


def test_broken_llm_app_falls_back_to_reference(llm_out, monkeypatch):
    ups = _fake_docker(monkeypatch, [False, True])
    o = runner.run(llm_out)
    assert o["ok"] and o["fallback"] and len(ups) == 2
    assert "bucket not found" in o["llm_logs"]
    assert (llm_out / "app.llm-failed" / "LLM_MARKER").exists()      # LLM app kept for inspection
    assert not (llm_out / "app" / "LLM_MARKER").exists()             # reference app deployed
    assert (llm_out / "app" / "worker.py").is_file()
    assert json.loads((llm_out / "result.json").read_text())["runtime"]["fallback"] is True
    assert runner.read_outcome(llm_out)["fallback"] is True
    # next deploy does not retry the broken LLM app
    ups2 = _fake_docker(monkeypatch, [False])
    o2 = runner.run(llm_out)
    assert not o2["ok"] and not o2["fallback"] and len(ups2) == 1


def test_reference_app_failure_is_reported_without_fallback(tmp_path, monkeypatch):
    out = tmp_path / "out"
    gen.generate(SAMPLE, out_dir=out, cfg=LLMConfig(api_key="", base_url="https://x/v1"))
    ups = _fake_docker(monkeypatch, [False])
    o = runner.run(out)
    assert not o["ok"] and not o["fallback"] and "not healthy" in o["reason"] and len(ups) == 1


def test_regenerate_clears_failed_app(llm_out, monkeypatch):
    _fake_docker(monkeypatch, [False, True])
    runner.run(llm_out)
    gen.generate(SAMPLE, out_dir=llm_out, cfg=LLMConfig(api_key="", base_url="https://x/v1"))
    assert not (llm_out / "app.llm-failed").exists() and runner.read_outcome(llm_out) is None


def test_template_compose_has_api_healthcheck(tmp_path):
    (tmp_path / "c.yml").write_text(gen.fallback.compose_text())
    assert gen.check_contract(tmp_path / "c.yml") is None
    bad = gen.fallback.compose_text().replace("http://127.0.0.1:5000/health", "http://127.0.0.1:5000/")
    (tmp_path / "b.yml").write_text(bad)
    assert "api needs a healthcheck" in gen.check_contract(tmp_path / "b.yml")
