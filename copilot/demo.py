"""Demo rehearsal helpers:  python -m copilot.demo {check,warm,reset}

  check  everything needed for a live demo, with a clear verdict (exit 1 on blockers)
  warm   pre-pull pinned images and pre-build the app image so Deploy is fast and offline-safe
  reset  stop the stack, delete its data volumes and output/ (fresh credentials next time);
         images and the LLM cache are kept. Requires --yes.
"""
from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import deploy, fallback
from .config import REPO_ROOT, load_config
from .datamig import PG_IMAGE, RCLONE_IMAGE
from .generator import CACHE_DIR, DEFAULT_OUT, generate
from .policy import ALLOWED_HOST_PORTS

UI_PORT = 8501
MIN_FREE_GB, WARN_FREE_GB = 1.0, 3.0
OK, WARN, FAIL = "✅", "⚠️ ", "❌"


def _run(*args, timeout=30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 127, "", str(e))


def stack_images() -> list[str]:
    """Pinned images the demo needs: compose services + app base image (required)."""
    images = re.findall(r"^\s*image:\s*(\S+)", (fallback.TEMPLATES / "docker-compose.yml").read_text(), re.M)
    images += re.findall(r"^FROM\s+(\S+)", (fallback.TEMPLATES / "app" / "Dockerfile").read_text(), re.M)
    return images


OPTIONAL_IMAGES = [PG_IMAGE, RCLONE_IMAGE]  # only for data-migration scripts


def _image_present(ref: str) -> bool:
    return _run("docker", "image", "inspect", ref).returncode == 0


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


# --------------------------------------------------------------------------- check
def check(out: Path = DEFAULT_OUT, ping_llm: bool = True) -> int:
    rows: list[tuple[str, str, str]] = []
    add = lambda status, item, detail="": rows.append((status, item, detail))  # noqa: E731

    v = sys.version_info
    add(OK if v[:2] == (3, 11) else WARN, "Python", f"{v.major}.{v.minor}.{v.micro}")

    docker_ok = _run("docker", "info", "--format", "{{.ServerVersion}}").returncode == 0
    add(OK if docker_ok else FAIL, "Docker daemon", "reachable" if docker_ok else "not reachable: start Docker")
    comp = _run("docker", "compose", "version", "--short")
    add(OK if comp.returncode == 0 else FAIL, "docker compose", comp.stdout.strip() or "missing (needs v2)")

    free_gb = shutil.disk_usage(REPO_ROOT).free / 1e9
    add(OK if free_gb >= WARN_FREE_GB else WARN if free_gb >= MIN_FREE_GB else FAIL,
        "Free disk", f"{free_gb:.1f} GB")

    if docker_ok:
        missing = [i for i in stack_images() if not _image_present(i)]
        add(OK if not missing else WARN, "Stack images cached",
            "all pinned images present" if not missing else f"{len(missing)} missing: run `make demo-warm`")
        opt = [i.split("@")[0] for i in OPTIONAL_IMAGES if not _image_present(i)]
        add(OK if not opt else WARN, "Data-migration images", "present" if not opt else f"missing {', '.join(opt)}")

    ours: set[int] = set()
    running = healthy = 0
    if (out / "docker-compose.yml").is_file() and docker_ok:
        try:
            rows_ps = deploy.status(out)
            running = sum(r["state"] == "running" for r in rows_ps)
            healthy = sum(r["health"] == "healthy" for r in rows_ps)
            ours = {int(x.split("->")[0]) for r in rows_ps if r["state"] == "running"
                    for x in r["ports"].split(", ") if "->" in x}
        except deploy.DeployError:
            pass
        add(OK, "Migration output", f"{out}/docker-compose.yml")
        add(OK if running else WARN, "Migrated stack",
            f"{running} running, {healthy} healthy" if running else "not running (Deploy in the UI)")
    else:
        add(WARN, "Migration output", "none yet (Generate in the UI)")

    busy = [p for p in sorted(ALLOWED_HOST_PORTS) if p not in ours and _port_busy(p)]
    add(OK if not busy else FAIL, "Stack ports", "free or used by this stack" if not busy
        else f"busy by another process: {busy}")

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{UI_PORT}/_stcore/health", timeout=2)
        add(OK, "Egress UI", f"running on :{UI_PORT}")
    except Exception:
        add(WARN if not _port_busy(UI_PORT) else FAIL, "Egress UI",
            "not running: `make run`" if not _port_busy(UI_PORT) else f"port {UI_PORT} taken by something else")

    cfg = load_config()
    if not cfg.enabled:
        add(WARN, "LLM", "no LLM_API_KEY: offline templates will be used (demo still works)")
    elif ping_llm:
        try:
            import openai
            client = openai.OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "x", timeout=10, max_retries=0)
            client.models.list()
            add(OK, "LLM", f"{cfg.model} @ {cfg.base_url} reachable")
        except Exception as e:
            add(WARN, "LLM", f"configured but unreachable ({e.__class__.__name__}): fallback will be used")
    else:
        add(OK, "LLM", f"{cfg.model} configured (not pinged)")
    cached = len(list(CACHE_DIR.glob("*.json"))) if CACHE_DIR.is_dir() else 0
    add(OK if cached else WARN if cfg.enabled else OK, "LLM cache",
        f"{cached} cached answer(s)" + ("" if cached or not cfg.enabled else ": run Generate once before the demo"))

    width = max(len(r[1]) for r in rows)
    for status, item, detail in rows:
        print(f"{status} {item:<{width}}  {detail}")
    blockers = sum(r[0] == FAIL for r in rows)
    print(f"\n{'READY' if not blockers else 'NOT READY'}: {blockers} blocker(s), "
          f"{sum(r[0] == WARN for r in rows)} warning(s)")
    return 1 if blockers else 0


# --------------------------------------------------------------------------- warm
def warm(include_optional: bool = True) -> int:
    images = stack_images() + (OPTIONAL_IMAGES if include_optional else [])
    failed = 0
    for ref in images:
        if _image_present(ref):
            print(f"{OK} cached  {ref.split('@')[0]}")
            continue
        r = _run("docker", "pull", "-q", ref, timeout=600)
        print(f"{OK if r.returncode == 0 else FAIL} pulled  {ref.split('@')[0]}")
        failed += r.returncode != 0
    # Pre-build the api/worker image with the same compose project name used by Deploy.
    with tempfile.TemporaryDirectory(prefix="egress-warm-") as tmp:
        generate(REPO_ROOT / "sample_app", out_dir=tmp, use_llm=False)
        r = _run("docker", "compose", "-f", str(Path(tmp) / "docker-compose.yml"), "build", "-q", timeout=900)
        print(f"{OK if r.returncode == 0 else FAIL} built   app image (api/worker)")
        failed += r.returncode != 0
    return 1 if failed else 0


# --------------------------------------------------------------------------- reset
def reset(out: Path = DEFAULT_OUT, yes: bool = False, clear_cache: bool = False) -> int:
    if not yes:
        print("reset deletes the stack's data volumes and everything in output/ (except .gitkeep).\n"
              "Re-run with --yes (or `make demo-reset CONFIRM=yes`).", file=sys.stderr)
        return 2
    out = Path(out)
    if (out / "docker-compose.yml").is_file():
        try:
            deploy.clear_job(out)
            r = _run("docker", "compose", "-f", str(out / "docker-compose.yml"), "down", "--volumes",
                     "--remove-orphans", timeout=180)
            print(f"{OK if r.returncode == 0 else WARN} stack stopped, volumes removed")
        except deploy.DeployError as e:
            print(f"{WARN} {e}")
    removed = 0
    if out.is_dir():
        for p in out.iterdir():
            if p.name == ".gitkeep":
                continue
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            removed += 1
    print(f"{OK} output/ cleared ({removed} entries); next Generate creates fresh random credentials")
    if clear_cache and CACHE_DIR.is_dir():
        shutil.rmtree(CACHE_DIR)
        print(f"{OK} LLM cache cleared")
    else:
        print(f"{OK} kept: Docker images, LLM cache")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m copilot.demo", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="verify the demo is ready")
    c.add_argument("--no-llm-ping", action="store_true")
    w = sub.add_parser("warm", help="pre-pull images and pre-build the app image")
    w.add_argument("--stack-only", action="store_true", help="skip data-migration images")
    r = sub.add_parser("reset", help="clean slate: stop stack, wipe volumes and output/")
    r.add_argument("--yes", action="store_true")
    r.add_argument("--clear-cache", action="store_true", help="also delete cached LLM answers")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return check(ping_llm=not a.no_llm_ping)
    if a.cmd == "warm":
        return warm(include_optional=not a.stack_only)
    return reset(yes=a.yes, clear_cache=a.clear_cache)


if __name__ == "__main__":
    sys.exit(main())
