"""Public demo mode: visitors are sandboxed, the presenter unlocks with ?key=..."""
from pathlib import Path

import pytest

import copilot.generator as gen
from copilot import access
from copilot.config import LLMConfig
from copilot.llm import LLMError

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_app"


@pytest.fixture
def public(monkeypatch, tmp_path):
    monkeypatch.setenv("EGRESS_PUBLIC", "1")
    monkeypatch.setenv("EGRESS_ADMIN_KEY", "s3cret-presenter-key")
    monkeypatch.setattr(access, "VISITOR_ROOT", tmp_path / "visitors")
    return tmp_path


def test_private_mode_everyone_is_presenter(monkeypatch):
    monkeypatch.setenv("EGRESS_PUBLIC", "0")
    assert access.is_presenter(None)


def test_public_mode_requires_key(public):
    assert not access.is_presenter(None) and not access.is_presenter("wrong")
    assert access.is_presenter("s3cret-presenter-key")


def test_generated_key_is_private_file(monkeypatch, tmp_path):
    monkeypatch.delenv("EGRESS_ADMIN_KEY", raising=False)
    monkeypatch.setattr(access, "KEY_FILE", tmp_path / "k")
    k = access.admin_key()
    assert len(k) >= 20 and access.admin_key() == k
    assert oct((tmp_path / "k").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("src,ok", [
    (str(SAMPLE), True), (str(SAMPLE) + "/", True), (str(SAMPLE / ".." / "sample_app"), True),
    (str(ROOT), False), ("/", False), ("/home", False), ("https://github.com/x/y.git", False),
    (str(SAMPLE / "app.py"), False)])
def test_visitor_sources(src, ok):
    assert access.visitor_source_allowed(src) is ok


def test_visitor_dirs_are_isolated(public):
    a, b = access.visitor_out_dir("abc"), access.visitor_out_dir("../../etc")
    assert a != b and a.parent == b.parent == public / "visitors"


def test_cache_only_never_calls_the_llm(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM must not be called")
    monkeypatch.setattr(gen, "chat_json", boom)
    cfg = LLMConfig(api_key="k", base_url="https://api.example.com/v1", model="m")
    r = gen.generate(SAMPLE, out_dir=tmp_path / "o", cfg=cfg, cache_dir=tmp_path / "c", llm_cache_only=True)
    assert r["path"] == "fallback" and any("presenter key" in e for e in r["errors"])


def test_cache_only_serves_cached_answers(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_copilot import _llm_reply
    cfg = LLMConfig(api_key="k", base_url="https://api.example.com/v1", model="m")
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply())
    gen.generate(SAMPLE, out_dir=tmp_path / "p", cfg=cfg, cache_dir=tmp_path / "c")  # presenter fills cache

    def boom(*a, **k):
        raise LLMError("no calls")
    monkeypatch.setattr(gen, "chat_json", boom)
    r = gen.generate(SAMPLE, out_dir=tmp_path / "v", cfg=cfg, cache_dir=tmp_path / "c", llm_cache_only=True)
    assert r["path"] == "llm" and r["llm"]["cache"] == "hit"


def _app(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(gen, "DEFAULT_OUT", tmp_path / "presenter")
    (tmp_path / "presenter").mkdir()
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)


def test_visitor_ui_is_sandboxed(public, monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path).run()
    assert not at.exception
    assert at.text_input[0].disabled
    assert any("Public demo" in str(i.value) for i in at.info)
    at.button[0].click().run()  # Scan (sample app)
    next(b for b in at.button if "Generate" in b.label).click().run()
    assert not at.exception and not at.error
    assert not (tmp_path / "presenter" / "docker-compose.yml").exists()  # presenter output untouched
    assert list((public / "visitors").glob("*/docker-compose.yml"))
    assert not any(b.label == "Deploy stack" for b in at.button)


def test_presenter_key_unlocks_and_is_removed_from_url(public, monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path)
    at.query_params["key"] = "s3cret-presenter-key"
    at.run()
    assert not at.exception
    assert not at.text_input[0].disabled
    assert any("Presenter mode" in str(s.value) for s in at.success)
    assert "key" not in at.query_params
