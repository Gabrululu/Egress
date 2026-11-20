"""Build the runnable app from the *scanned repo*: copy it, apply the migration diffs with
`git apply`, retire AWS-only infrastructure files, and verify the result.

If every app-level patch applies cleanly and the result passes the checks, output/app is the
patched repo; otherwise the reference template stays in place (demo never breaks) and the
reasons are reported.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

# Files handled at stack level (the generated docker-compose.yml), not inside the app image.
STACK_LEVEL = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
# AWS-only infrastructure retired by the migration (decommissioned per the plan).
RETIRE = ("*.tf", "*.tfvars", "*.tfstate", "*.tfstate.*", ".terraform.lock.hcl") + STACK_LEVEL
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                                     ".terraform", "output", ".cache", "*.pyc", ".env", ".env.*")
# The generated compose runs `python app.py` (api) and `python worker.py` (worker) from ./app.
REQUIRED = ("Dockerfile", "app.py", "worker.py")
ALLOWED_BASE_IMAGES = ("python",)
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE | re.MULTILINE)


def _matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _target_files(diff: str) -> set[str]:
    """Paths a unified/git diff touches (b/ side, plus rename sources)."""
    names = set()
    for line in diff.splitlines():
        if line.startswith("+++ ") and "/dev/null" not in line:
            names.add(line[4:].split("\t")[0].strip().removeprefix("b/"))
        elif line.startswith("rename to "):
            names.add(line[len("rename to "):].strip())
    return names


def _git_apply(work: Path, diff: str, check: bool) -> tuple[bool, str]:
    patch = work / ".egress.patch"
    patch.write_text(diff if diff.endswith("\n") else diff + "\n", encoding="utf-8")
    try:
        args = ["git", "apply", "--whitespace=nowarn"] + (["--check"] if check else []) + [patch.name]
        # Never let git discover an enclosing repository (e.g. output/ inside the Egress repo):
        # inside a repo, `git apply` resolves paths from the repo root and silently skips the rest.
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(work.parent)}
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        r = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=30, env=env)
        lines = (r.stderr or r.stdout).strip().splitlines()
        return r.returncode == 0, lines[-1] if lines else ""
    finally:
        patch.unlink(missing_ok=True)


def _verify(work: Path) -> list[str]:
    problems = [f"missing {name}" for name in REQUIRED if not (work / name).is_file()]
    for py in work.rglob("*.py"):
        try:
            compile(py.read_text(encoding="utf-8"), str(py.relative_to(work)), "exec")
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            problems.append(f"{py.relative_to(work)}: does not compile ({e.__class__.__name__}: {e})")
    df = work / "Dockerfile"
    if df.is_file():
        for image in _FROM.findall(df.read_text(encoding="utf-8")):
            ref = image.split("@")[0]
            head, _, last = ref.rpartition("/")
            repo = (head + "/" if head else "") + last.split(":")[0]
            repo = repo.removeprefix("docker.io/").removeprefix("library/")
            if repo not in ALLOWED_BASE_IMAGES:
                problems.append(f"Dockerfile base image '{image}' not allowed (expected python)")
    return problems


def build_app_from_source(target: Path, diffs: list[dict], dest: Path) -> dict:
    """Try to replace `dest` (output/app) with the patched scanned repo. Never raises."""
    target, dest = Path(target).resolve(), Path(dest).resolve()
    result = {"app_source": "template", "patches": [], "retired": [], "problems": [], "reason": ""}
    if not target.is_dir():
        result["reason"] = "scan target is not a directory"
        return result
    if shutil.which("git") is None:
        result["reason"] = "git not available to apply patches"
        return result

    work = dest.parent / ".app-src.tmp"
    shutil.rmtree(work, ignore_errors=True)
    try:
        shutil.copytree(target, work, ignore=COPY_IGNORE)
        failed = False
        for d in diffs:
            files = _target_files(d["diff"]) or {d["file"]}
            entry = {"file": d["file"]}
            if all(_matches(Path(f).name, STACK_LEVEL) for f in files):
                entry.update(status="skipped", detail="stack-level: replaced by the generated docker-compose.yml")
            else:
                ok, msg = _git_apply(work, d["diff"], check=True)
                if ok:
                    ok, msg = _git_apply(work, d["diff"], check=False)
                entry.update(status="applied" if ok else "failed", detail=msg if not ok else "")
                failed |= not ok
            result["patches"].append(entry)

        for p in sorted(work.rglob("*")):
            if p.is_file() and _matches(p.name, RETIRE):
                result["retired"].append(str(p.relative_to(work)))
                p.unlink()

        result["problems"] = _verify(work)
        if failed:
            result["reason"] = "some patches did not apply cleanly"
        elif result["problems"]:
            result["reason"] = "patched repo failed verification"
        else:
            shutil.rmtree(dest, ignore_errors=True)
            work.rename(dest)
            result["app_source"] = "patched-repo"
    except Exception as e:  # copy/IO errors: keep the template
        result["reason"] = f"could not build from source: {e.__class__.__name__}: {e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result
