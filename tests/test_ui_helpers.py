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
    assert any("Lock-in scan" in str(x.value) for x in at.markdown)
    gen_btn = next(b for b in at.button if "Generate" in b.label)
    gen_btn.click().run()
    assert not at.exception and not at.error
    assert any("Offline templates" in str(mt.value) for mt in at.metric)


def test_provider_name():
    import ui_theme as ui
    assert ui.provider_name("https://api.mistral.ai/v1") == "Mistral"
    assert ui.provider_name("https://api.openai.com/v1") == "OpenAI"
    assert ui.provider_name("http://localhost:11434/v1") == "Ollama"
    assert ui.provider_name("https://llm.example.eu/v1") == "Custom endpoint"


# ---------------------------------------------------------------- background deploy
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from copilot import deploy  # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_conflict_detected(tmp_path):
    port = _free_port()
    (tmp_path / "docker-compose.yml").write_text(
        f"name: egress-test-ports\nservices:\n  db:\n    image: postgres:16-alpine\n    ports: ['{port}:5432']\n")
    with socket.socket() as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen()
        conflicts = deploy.port_conflicts(tmp_path)
    assert any(str(port) in c for c in conflicts), conflicts


def test_start_up_blocked_by_policy(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  x:\n    image: postgres:16-alpine\n    privileged: true\n")
    with pytest.raises(deploy.DeployError, match="security policy"):
        deploy.start_up(tmp_path)
    assert deploy.current_job(tmp_path) is None


def test_background_job_lifecycle(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services:\n  x:\n    image: postgres:16-alpine\n")
    script = "import time; print('building...', flush=True); time.sleep(1); print('done')"

    def fake_spawn(args, cwd, log_path):
        assert args[1:3] == ["-m", "copilot.runner"] and "--wait-timeout" in args
        log = open(log_path, "w")
        return subprocess.Popen([sys.executable, "-c", script], stdout=log, stderr=subprocess.STDOUT)
    monkeypatch.setattr(deploy, "_spawn", fake_spawn)

    job = deploy.start_up(tmp_path)
    assert job.running
    with pytest.raises(deploy.DeployError, match="already running"):
        deploy.start_up(tmp_path)
    job.proc.wait(timeout=10)
    assert not job.running and job.returncode == 0
    assert "done" in job.tail()
    deploy.clear_job(tmp_path)
    assert deploy.current_job(tmp_path) is None


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_colours_meet_wcag_aa():
    import re

    import ui_theme as ui
    gray = re.search(r"--eg-gray:(#[0-9a-f]{6})", ui.CSS).group(1)
    for colour in [*ui.SEV_TEXT.values(), ui.BLUE_TEXT, gray]:
        for bg in ("#ffffff", "#f5f5f5"):
            assert _contrast(colour, bg) >= 4.5, (colour, bg)
    assert _contrast("#ffffff", ui.BLUE_TEXT) >= 4.5  # primary button hover
    for score in (90, 55, 10):
        assert ui.score_text_color(score) in ui.score_card(score, "x", 1, 1)
