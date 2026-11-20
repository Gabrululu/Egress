"""docker compose wrappers for deploying the generated stack in output/.

`start_up()` runs `docker compose up --build --wait` in the background so the UI never
freezes; its output goes to output/.deploy.log and can be tailed while it runs. Before
starting, the compose file must pass the security policy and its host ports must be free.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .policy import check_compose_file, normalized_config

LOG_NAME = ".deploy.log"


class DeployError(Exception):
    pass


# --------------------------------------------------------------- helpers
def _compose_file(out_dir: Path) -> Path:
    f = Path(out_dir).resolve() / "docker-compose.yml"
    if not f.is_file():
        raise DeployError("No output/docker-compose.yml yet. Generate the migration first.")
    return f


def _compose(out_dir: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    compose_file = _compose_file(out_dir)
    try:
        return subprocess.run(["docker", "compose", "-f", str(compose_file), *args],
                              cwd=compose_file.parent, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DeployError("docker is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise DeployError(f"docker compose {args[0]} timed out after {timeout}s.")


def _tail(r: subprocess.CompletedProcess, n: int = 15) -> str:
    return "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-n:])


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def published_ports(out_dir: Path) -> dict[int, str]:
    """{host_port: service} from the normalized compose config."""
    cfg = normalized_config(_compose_file(out_dir)) or {}
    out = {}
    for name, svc in (cfg.get("services") or {}).items():
        for p in svc.get("ports") or []:
            try:
                out[int(str(p.get("published")).split("-")[0])] = name
            except (TypeError, ValueError):
                continue
    return out


def port_conflicts(out_dir: Path) -> list[str]:
    """Host ports the stack needs that are taken by something other than this stack."""
    try:
        ours = {int(x.split("->")[0]) for r in status(out_dir) if r["state"] == "running"
                for x in r["ports"].split(", ") if "->" in x}
    except DeployError:
        ours = set()
    return [f"port {port} (needed by '{svc}') is already in use"
            for port, svc in sorted(published_ports(out_dir).items())
            if port not in ours and _port_in_use(port)]


def preflight(out_dir: Path) -> None:
    """Security policy + port check. Raises DeployError with a readable message."""
    violations = check_compose_file(_compose_file(out_dir))
    if violations:
        raise DeployError("Blocked by security policy:\n- " + "\n- ".join(violations))
    conflicts = port_conflicts(out_dir)
    if conflicts:
        raise DeployError("Ports busy, free them or stop the other service:\n- " + "\n- ".join(conflicts))


# --------------------------------------------------------------- background jobs
class DeployJob:
    def __init__(self, proc: subprocess.Popen, log_path: Path, action: str):
        self.proc, self.log_path, self.action = proc, log_path, action
        self.started = time.time()
        self.finished: float | None = None
        self.id = f"{action}-{int(self.started * 1000)}"

    @property
    def running(self) -> bool:
        rc = self.proc.poll()
        if rc is not None and self.finished is None:
            self.finished = time.time()
        return rc is None

    @property
    def returncode(self) -> int | None:
        return self.proc.poll()

    @property
    def elapsed(self) -> float:
        self.running  # refresh finished timestamp
        return (self.finished or time.time()) - self.started

    def tail(self, n: int = 20) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])

    def stop(self) -> None:
        if self.running:
            try:  # the runner spawns docker compose: stop the whole process group
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


_JOBS: dict[str, DeployJob] = {}
_LOCK = threading.Lock()


def _key(out_dir: Path) -> str:
    return str(Path(out_dir).resolve())


def current_job(out_dir: Path) -> DeployJob | None:
    return _JOBS.get(_key(out_dir))


def clear_job(out_dir: Path) -> None:
    job = _JOBS.pop(_key(out_dir), None)
    if job:
        job.stop()


def _spawn(args: list[str], cwd: Path, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "w", encoding="utf-8")  # child keeps its own fd; closed when it exits
    try:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
        return subprocess.Popen(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    except FileNotFoundError:
        raise DeployError("docker is not installed or not on PATH.")
    finally:
        log.close()


def start_up(out_dir: Path, wait_timeout: int = 360) -> DeployJob:
    """Start the deploy runner in the background (non-blocking): `docker compose up --build --wait`,
    a /health probe, and an automatic switch to the reference app if LLM-written code fails."""
    compose_file = _compose_file(out_dir)
    with _LOCK:
        job = current_job(out_dir)
        if job and job.running:
            raise DeployError("A deploy is already running.")
        preflight(out_dir)
        from .runner import RESULT_NAME
        (compose_file.parent / RESULT_NAME).unlink(missing_ok=True)
        args = [sys.executable, "-m", "copilot.runner", str(compose_file.parent), "--wait-timeout", str(wait_timeout)]
        log_path = compose_file.parent / LOG_NAME
        job = DeployJob(_spawn(args, compose_file.parent, log_path), log_path, "up")
        _JOBS[_key(out_dir)] = job
        return job


# --------------------------------------------------------------- synchronous API (CLI / tests)
def up(out_dir: Path, timeout: int = 420) -> str:
    preflight(out_dir)
    r = _compose(out_dir, "up", "-d", "--build", "--force-recreate", "--wait", "--wait-timeout", str(timeout - 60),
                 timeout=timeout)
    if r.returncode != 0:
        raise DeployError(f"docker compose up failed:\n{_tail(r)}")
    return _tail(r)


def down(out_dir: Path, wipe: bool = False) -> str:
    """Stop the stack. wipe=True also deletes data volumes and rotates the generated credentials
    (safe only then: Postgres applies its password when the volume is first created)."""
    clear_job(out_dir)  # cancel an in-flight deploy first
    args = ["down", "--remove-orphans"] + (["--volumes"] if wipe else [])
    r = _compose(out_dir, *args, timeout=120)
    if r.returncode != 0:
        raise DeployError(f"docker compose down failed:\n{_tail(r)}")
    if wipe:
        from .generator import ensure_secrets
        env_file = Path(out_dir).resolve() / ".env"
        if env_file.exists():
            env_file.unlink()
        ensure_secrets(Path(out_dir).resolve())
    return _tail(r)


def parse_ps(stdout: str) -> list[dict]:
    """`docker compose ps --format json` prints a JSON array (old) or NDJSON (new)."""
    stdout = stdout.strip()
    if not stdout:
        return []
    if stdout.startswith("["):
        items = json.loads(stdout)
    else:
        items = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    rows = []
    for c in items:
        ports = sorted({f"{p['PublishedPort']}->{p['TargetPort']}" for p in c.get("Publishers") or []
                        if p.get("PublishedPort")})
        rows.append({"service": c.get("Service", ""), "state": c.get("State", ""),
                     "health": c.get("Health", "") or "-", "status": c.get("Status", ""),
                     "ports": ", ".join(ports)})
    return sorted(rows, key=lambda r: r["service"])


def status(out_dir: Path) -> list[dict]:
    r = _compose(out_dir, "ps", "-a", "--format", "json", timeout=30)
    if r.returncode != 0:
        raise DeployError(f"docker compose ps failed:\n{_tail(r)}")
    return parse_ps(r.stdout)
