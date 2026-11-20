"""Demo rehearsal helpers: check / warm / reset (reset is tested on a scratch dir only)."""
from pathlib import Path

from copilot import demo


def test_stack_images_are_pinned():
    imgs = demo.stack_images()
    assert len(imgs) == 4 and all("@sha256:" in i for i in imgs)
    assert any(i.startswith("python:3.11-slim@") for i in imgs)


def test_reset_requires_confirmation(tmp_path, capsys):
    (tmp_path / "keep.txt").write_text("x")
    assert demo.reset(tmp_path, yes=False) == 2
    assert (tmp_path / "keep.txt").exists() and "--yes" in capsys.readouterr().err


def test_reset_clears_output_but_keeps_gitkeep(tmp_path):
    (tmp_path / ".gitkeep").touch()
    (tmp_path / ".env").write_text("DB_PASSWORD=x\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "app.py").write_text("print()")
    assert demo.reset(tmp_path, yes=True) == 0
    assert [p.name for p in tmp_path.iterdir()] == [".gitkeep"]


def test_check_reports_verdict(tmp_path, capsys):
    rc = demo.check(out=tmp_path, ping_llm=False)
    out = capsys.readouterr().out
    assert rc in (0, 1) and ("READY" in out) and "Docker daemon" in out and "Migration output" in out
