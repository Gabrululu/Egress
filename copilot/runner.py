"""Background deploy with runtime verification:  python -m copilot.runner <out_dir>

1. `docker compose up -d --build --force-recreate --wait` (the api has a /health healthcheck)
2. probe the API's /health from the host
3. if the app was written by the LLM and it does not come up healthy: save its logs, move it
   to app.llm-failed/, rebuild the app from the reference template diffs and deploy again.

Static gates (compile, apply, scan, contract) cannot catch logic bugs such as a startup check
that waits forever for a bucket the code only creates later; this catches them at runtime and
keeps the demo working. Everything goes to stdout (= output/.deploy.log); the outcome is
written to output/.deploy-result.json for the UI.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HEALTH_URL = "http://127.0.0.1:5000/health"
RESULT_NAME = ".deploy-result.json"
FAILED_APP = "app.llm-failed"


def log(msg: str) -> None:
    print(f"[egress] {msg}", flush=True)


def compose_up(out: Path, wait_timeout: int) -> int:
    args = ["docker", "compose", "-f", str(out / "docker-compose.yml"), "up", "-d", "--build",
            "--force-recreate", "--wait", "--wait-timeout", str(wait_timeout)]
    sys.stdout.flush()
    return subprocess.run(args, cwd=out, stdout=sys.stdout, stderr=subprocess.STDOUT).returncode


def probe(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(2)


def service_logs(out: Path, services=("api", "worker"), tail: int = 40) -> str:
    try:
        r = subprocess.run(["docker", "compose", "-f", str(out / "docker-compose.yml"), "logs",
                            "--no-color", "--tail", str(tail), *services],
                           cwd=out, capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr).strip()[-6000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(could not read logs: {e})"


def _read_result(out: Path) -> dict:
    try:
        return json.loads((out / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def llm_written_app(out: Path) -> bool:
    """True if output/app is the scanned repo patched with LLM-written code."""
    r = _read_result(out)
    return (r.get("sources", {}).get("diffs") == "llm"
            and r.get("source_patch", {}).get("app_source") == "patched-repo"
            and not r.get("runtime", {}).get("fallback"))


def use_reference_app(out: Path) -> str:
    """Move the LLM app aside and rebuild output/app from the template diffs. Returns app source."""
    from . import fallback
    from .apply import build_app_from_source

    app, failed = out / "app", out / FAILED_APP
    shutil.rmtree(failed, ignore_errors=True)
    if app.exists():
        app.rename(failed)
    target = Path(_read_result(out).get("source_patch", {}).get("target") or "")
    if not target.is_dir():
        try:
            target = Path(json.loads((out / "scan.json").read_text(encoding="utf-8"))["target"])
        except (OSError, KeyError, json.JSONDecodeError):
            target = Path("/nonexistent")
    source = "template"
    if target.is_dir():
        res = build_app_from_source(target, fallback.diffs(target), app)
        source = res["app_source"]
    if not app.is_dir():
        shutil.copytree(fallback.TEMPLATES / "app", app)
    return "reference diffs on the scanned repo" if source == "patched-repo" else "reference template"


def run(out: Path, wait_timeout: int = 360, health_url: str = HEALTH_URL, health_timeout: float = 60) -> dict:
    out = Path(out).resolve()
    outcome = {"ok": False, "fallback": False, "reason": "", "llm_logs": "", "finished": None}
    llm_app = llm_written_app(out)
    log(f"deploying ({'LLM-written app' if llm_app else 'reference app'})")
    rc = compose_up(out, wait_timeout)
    healthy = rc == 0 and probe(health_url, health_timeout)
    if not healthy and llm_app:
        outcome["reason"] = (f"docker compose up exited {rc}" if rc else f"{health_url} not healthy")
        log(f"LLM-written app failed at runtime ({outcome['reason']}); saving its logs")
        outcome["llm_logs"] = service_logs(out)
        print(outcome["llm_logs"], flush=True)
        how = use_reference_app(out)
        log(f"redeploying with the {how} (LLM app kept in {FAILED_APP}/)")
        outcome["fallback"] = True
        rc = compose_up(out, wait_timeout)
        healthy = rc == 0 and probe(health_url, health_timeout)
    outcome["ok"] = healthy
    if not healthy and not outcome["reason"]:
        outcome["reason"] = f"docker compose up exited {rc}" if rc else f"{health_url} not healthy"
    outcome["finished"] = time.time()
    (out / RESULT_NAME).write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    if outcome["fallback"]:  # remember it, so the next deploy does not retry the broken app
        r = _read_result(out)
        if r:
            r["runtime"] = {"fallback": True, "reason": outcome["reason"]}
            (out / "result.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    log("stack is up and healthy" if healthy else f"deploy failed: {outcome['reason']}")
    return outcome


def read_outcome(out: Path) -> dict | None:
    try:
        return json.loads((Path(out) / RESULT_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m copilot.runner")
    ap.add_argument("out_dir")
    ap.add_argument("--wait-timeout", type=int, default=360)
    a = ap.parse_args(argv)
    return 0 if run(Path(a.out_dir), a.wait_timeout)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
