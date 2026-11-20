import json
from pathlib import Path

import pytest

from copilot.deploy import DeployError, parse_ps, status
from scanner.source import SourceError, is_git_url, resolve_source

ROOT = Path(__file__).resolve().parent.parent


def test_git_url_detection():
    assert is_git_url("https://github.com/org/repo")
    assert is_git_url("https://gitlab.com/org/sub/repo.git")
    assert is_git_url("git@github.com:org/repo.git")
    assert not is_git_url("sample_app")
    assert not is_git_url("file:///etc")


def test_resolve_local_path():
    p, tmp = resolve_source(str(ROOT / "sample_app"))
    assert p.is_dir() and tmp is None


@pytest.mark.parametrize("bad", ["", "/nope/egress", "ftp://x/y"])
def test_resolve_errors(bad):
    with pytest.raises(SourceError):
        resolve_source(bad)


def test_parse_ps_ndjson_and_array():
    c = {"Service": "api", "State": "running", "Health": "", "Status": "Up",
         "Publishers": [{"PublishedPort": 5000, "TargetPort": 5000}]}
    assert parse_ps(json.dumps(c))[0]["ports"] == "5000->5000"
    assert parse_ps(json.dumps([c]))[0]["health"] == "-"
    assert parse_ps("") == []


def test_status_without_compose(tmp_path):
    with pytest.raises(DeployError):
        status(tmp_path)


def test_streamlit_scan_and_generate(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    import copilot.generator as gen
    monkeypatch.setattr(gen, "DEFAULT_OUT", tmp_path)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    at.button[0].click().run()  # Scan
    assert not at.exception and not at.error
    assert any("Lock-in scan" in s.value for s in at.subheader)
    gen_btn = next(b for b in at.button if "Generate" in b.label)
    gen_btn.click().run()
    assert not at.exception and not at.error
    assert any("Offline templates" in str(mt.value) for mt in at.metric)
