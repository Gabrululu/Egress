"""J: build the runnable app from the scanned repo + migration diffs (git apply), with fallback."""
import shutil
from pathlib import Path

import pytest

import copilot.apply as ap
import copilot.generator as gen
from copilot import fallback
from copilot.config import LLMConfig

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_app"
NO_KEY = LLMConfig(api_key="", base_url="https://api.example.com/v1")


@pytest.fixture(autouse=True)
def no_volume(monkeypatch):
    monkeypatch.setattr(gen, "_volume_exists", lambda name: False)


def _dest(tmp_path):
    d = tmp_path / "out" / "app"
    shutil.copytree(fallback.TEMPLATES / "app", d)
    (d / "TEMPLATE_MARKER").write_text("x")
    return d


def test_sample_app_patched_from_source(tmp_path):
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=NO_KEY)
    sp = r["source_patch"]
    assert sp["app_source"] == "patched-repo", sp
    statuses = {p["file"]: p["status"] for p in sp["patches"]}
    assert statuses == {"app.py": "applied", "worker.py": "applied", "requirements.txt": "applied",
                        "Dockerfile": "applied", "docker-compose.yml": "skipped"}
    assert set(sp["retired"]) == {"main.tf", "docker-compose.yml"}
    files = {p.name for p in (tmp_path / "app").iterdir()}
    assert files == {"Dockerfile", "app.py", "worker.py", "requirements.txt"}
    assert "lambda_function.py" not in files  # renamed by the patch
    assert r["after"]["overall_score"] == 0
    assert not (tmp_path / ".app-src.tmp").exists()


def test_fallback_diffs_are_git_style():
    d = {x["file"]: x["diff"] for x in fallback.diffs(SAMPLE)}
    assert d["worker.py"].startswith("diff --git a/lambda_function.py b/worker.py\nrename from lambda_function.py")


def test_new_files_for_repo_without_app(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    sp = ap.build_app_from_source(repo, fallback.diffs(repo), _dest(tmp_path))
    assert sp["app_source"] == "patched-repo" and sp["retired"] == ["main.tf"]


def test_failed_patch_keeps_template(tmp_path):
    bad = [{"file": "app.py", "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-not in file\n+y\n"}]
    dest = _dest(tmp_path)
    sp = ap.build_app_from_source(SAMPLE, bad, dest)
    assert sp["app_source"] == "template" and sp["patches"][0]["status"] == "failed"
    assert (dest / "TEMPLATE_MARKER").exists()


def test_patch_that_breaks_syntax_is_rejected(tmp_path):
    diffs = fallback.diffs(SAMPLE)
    for d in diffs:
        if d["file"] == "app.py":
            d["diff"] = d["diff"].replace("+import time\n", "+import time(\n", 1)
    dest = _dest(tmp_path)
    sp = ap.build_app_from_source(SAMPLE, diffs, dest)
    assert sp["app_source"] == "template"
    assert any("does not compile" in p for p in sp["problems"])


def test_untrusted_dockerfile_base_rejected(tmp_path):
    diffs = fallback.diffs(SAMPLE)
    for d in diffs:
        if d["file"] == "Dockerfile":
            # inject a second build stage from an untrusted image (hunk header adjusted: +1 line)
            assert "@@ -2,6 +2,6 @@" in d["diff"]
            d["diff"] = d["diff"].replace("@@ -2,6 +2,6 @@", "@@ -2,6 +2,7 @@", 1).replace(
                "+COPY app.py worker.py ./\n", "+COPY app.py worker.py ./\n+FROM evil/miner:latest\n", 1)
    sp = ap.build_app_from_source(SAMPLE, diffs, _dest(tmp_path))
    assert sp["app_source"] == "template" and any("not allowed" in p for p in sp["problems"])


def test_no_git_keeps_template(tmp_path, monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda name: None)
    sp = ap.build_app_from_source(SAMPLE, fallback.diffs(SAMPLE), _dest(tmp_path))
    assert sp["app_source"] == "template" and "git" in sp["reason"]


def test_llm_diffs_that_dont_apply_fall_back(tmp_path, monkeypatch):
    reply = {"mapping": [{"aws": "S3", "alternative": "SeaweedFS", "reason": "r"}],
             "docker_compose": fallback.compose_text(),
             "diffs": [{"file": "app.py", "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"}],
             "plan": "# Plan\n\n## Steps\n1. a\n\n## Risks\n- b\n\n## Effort estimate\n5 days total for all."}
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: reply)
    monkeypatch.setattr(gen, "CACHE_DIR", tmp_path / "cache")
    r = gen.generate(SAMPLE, out_dir=tmp_path / "o", cfg=LLMConfig(api_key="k", base_url="https://x/v1"))
    assert r["sources"]["diffs"] == "llm" and r["source_patch"]["app_source"] == "template"
    assert (tmp_path / "o" / "app" / "worker.py").exists()  # template still runnable


def test_works_when_output_is_inside_a_git_repo(tmp_path):
    """Regression: output/ lives inside the Egress git repo; git apply must not resolve paths
    against the enclosing repository (it silently skipped files outside the subdirectory)."""
    import subprocess
    repo = tmp_path / "enclosing"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    dest = repo / "output" / "app"
    shutil.copytree(fallback.TEMPLATES / "app", dest)
    sp = ap.build_app_from_source(SAMPLE, fallback.diffs(SAMPLE), dest)
    assert sp["app_source"] == "patched-repo", sp
    assert (dest / "worker.py").exists() and not (dest / "lambda_function.py").exists()
    assert not (repo / "worker.py").exists()  # nothing written at the enclosing repo root
