"""B: the LLM returns complete files; Egress computes exact diffs and verifies the code."""
import sys
from pathlib import Path

import copilot.generator as gen
from copilot import fallback
from copilot.codegen import code_problem, files_to_diffs
from copilot.config import LLMConfig
from copilot.llm import MIGRATION_SCHEMA, SYSTEM_PROMPT, message_text
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_copilot import _llm_reply, llm_files  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "sample_app"
KEY = LLMConfig(api_key="k", base_url="https://api.example.com/v1", model="m")


def _swap(files, path, content):
    return [dict(f, content=content) if f["path"] == path else f for f in files]


def test_schema_asks_for_files_not_diffs():
    assert "files" in MIGRATION_SCHEMA["required"] and "diffs" not in MIGRATION_SCHEMA["properties"]
    assert "NEVER diffs" in SYSTEM_PROMPT


def test_complete_files_become_exact_git_diffs():
    diffs, problems = files_to_diffs(llm_files(), SAMPLE)
    assert problems == []
    by = {d["file"]: d["diff"] for d in diffs}
    assert "rename from lambda_function.py\nrename to worker.py" in by["worker.py"]
    assert by["app.py"].startswith("diff --git a/app.py b/app.py\n")
    assert code_problem(diffs, SAMPLE) is None  # applies, compiles, no lock-in left


def test_original_omitted_for_existing_file_is_a_modification():
    files = [dict(f, original="") for f in llm_files()]
    diffs, _ = files_to_diffs(files, SAMPLE)
    assert {d["file"] for d in diffs} >= {"app.py", "requirements.txt"}
    assert "new file mode" not in {d["file"]: d["diff"] for d in diffs}["app.py"]


def test_unsafe_paths_rejected():
    for bad in ("../evil.py", "/etc/passwd", ".git/config", "a\\b.py"):
        _, problems = files_to_diffs([{"path": bad, "original": "", "content": "x = 1\n"}], SAMPLE)
        assert any("unsafe" in p for p in problems), bad


def test_elided_files_rejected():
    files = _swap(llm_files(), "app.py", "import os\n# ... rest of the code unchanged\n")
    _, problems = files_to_diffs(files, SAMPLE)
    assert any("elision" in p for p in problems)


def test_compose_and_terraform_files_ignored():
    files = llm_files() + [{"path": "docker-compose.yml", "original": "", "content": "services: {}\n"},
                           {"path": "main.tf", "original": "main.tf", "content": ""}]
    diffs, problems = files_to_diffs(files, SAMPLE)
    assert problems == [] and not {"docker-compose.yml", "main.tf"} & {d["file"] for d in diffs}


def test_code_that_keeps_sqs_is_rejected():
    app = (fallback.TEMPLATES / "app" / "app.py").read_text() + (
        '\n\ndef legacy():\n    boto3.client("sqs").send_message(QueueUrl="q", MessageBody="x")\n')
    diffs, _ = files_to_diffs(_swap(llm_files(), "app.py", app), SAMPLE)
    problem = code_problem(diffs, SAMPLE)
    assert problem and "remaining AWS lock-in" in problem and "SQS" in problem


def test_code_missing_contract_env_is_rejected():
    app = (fallback.TEMPLATES / "app" / "app.py").read_text().replace("AMQP_URL", "BROKER_URL")
    diffs, _ = files_to_diffs(_swap(llm_files(), "app.py", app), SAMPLE)
    assert "AMQP_URL" in code_problem(diffs, SAMPLE)


def test_repair_round_fixes_code(tmp_path, monkeypatch):
    calls = []

    def fake(system, user, cfg, history=None):
        calls.append(history)
        reply = _llm_reply()
        if history is None:
            reply["files"] = _swap(reply["files"], "worker.py", "def handler(:\n")
        return reply
    monkeypatch.setattr(gen, "chat_json", fake)
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY, cache_dir=tmp_path / "c")
    assert r["llm"]["repair"] == "fixed" and r["llm"]["calls"] == 2 and r["path"] == "llm", r["errors"]
    assert "- files:" in calls[1][-1]["content"] and "worker.py" in calls[1][-1]["content"]
    assert r["source_patch"]["app_source"] == "patched-repo"


def test_repair_keeps_good_parts_when_only_one_is_fixed(tmp_path, monkeypatch):
    def fake(system, user, cfg, history=None):
        reply = _llm_reply("services: [oops")  # compose never fixed
        if history is None:
            reply["files"] = _swap(reply["files"], "app.py", "def x(:\n")
        return reply
    monkeypatch.setattr(gen, "chat_json", fake)
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY, cache_dir=tmp_path / "c")
    assert r["llm"]["repair"] == "partial"
    assert r["sources"]["diffs"] == "llm" and r["sources"]["compose"] == "fallback"


def test_message_text_drops_thinking_blocks():
    blocks = [{"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
              {"type": "text", "text": '{"a": '}, {"type": "text", "text": "1}"}]
    assert message_text(blocks) == '{"a": 1}'
    assert message_text("plain") == "plain" and message_text(None) == ""


def test_app_prefix_from_build_context_is_normalised():
    files = [dict(f, path="app/" + f["path"]) for f in llm_files()]
    diffs, problems = files_to_diffs(files, SAMPLE)
    assert problems == [] and {d["file"] for d in diffs} == {"app.py", "worker.py", "requirements.txt", "Dockerfile"}
    assert code_problem(diffs, SAMPLE) is None


def test_chat_json_retries_once_on_unreadable_reply(monkeypatch):
    import openai

    from copilot.llm import chat_json

    replies = iter(["Sorry, here is the plan in prose.", '{"ok": 1}'])
    calls = []

    class FakeCompletions:
        def create(self, **kw):
            calls.append(kw)
            msg = type("M", (), {"content": next(replies)})
            return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    class FakeClient:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    assert chat_json("s", "u", KEY) == {"ok": 1} and len(calls) == 2


def test_egress_managed_and_secret_files_ignored():
    extra = [{"path": "seaweedfs/s3.json", "original": "", "content": '{"secretKey": "literal"}\n'},
             {"path": "app/seaweedfs/s3.json", "original": "", "content": "{}\n"},
             {"path": ".env", "original": "", "content": "DB_PASSWORD=x\n"}]
    diffs, problems = files_to_diffs(llm_files() + extra, SAMPLE)
    assert problems == [] and all("seaweedfs" not in d["file"] and ".env" not in d["file"] for d in diffs)


def test_contract_rejects_bind_mounted_data(tmp_path):
    bad = fallback.compose_text().replace("- s3data:/data", "- ./seaweedfs/data:/data")
    assert bad != fallback.compose_text()
    (tmp_path / "c.yml").write_text(bad)
    assert "named volumes" in gen.check_contract(tmp_path / "c.yml")
    (tmp_path / "ok.yml").write_text(fallback.compose_text())
    assert gen.check_contract(tmp_path / "ok.yml") is None


def _contract(tmp_path, text):
    (tmp_path / "c.yml").write_text(text)
    return gen.check_contract(tmp_path / "c.yml")


def test_contract_rejects_unset_variables(tmp_path):
    bad = fallback.compose_text().replace("S3_BUCKET: egress-demo-uploads", "S3_BUCKET: ${S3_BUCKET}")
    assert bad != fallback.compose_text()
    assert "S3_BUCKET" in _contract(tmp_path, bad)
    ok = fallback.compose_text().replace("S3_BUCKET: egress-demo-uploads", "S3_BUCKET: ${S3_BUCKET:-b}")
    assert _contract(tmp_path, ok) is None


def test_contract_requires_filer_healthcheck(tmp_path):
    bad = fallback.compose_text().replace("http://127.0.0.1:8888/", "http://localhost:8333/")
    assert bad != fallback.compose_text() and "healthcheck" in _contract(tmp_path, bad)


def test_llm_images_pinned_to_template_digests(tmp_path, monkeypatch):
    unpinned = __import__("re").sub(r"@sha256:[0-9a-f]{64}", "", fallback.compose_text())
    monkeypatch.setattr(gen, "chat_json", lambda *a, **k: _llm_reply(unpinned))
    r = gen.generate(SAMPLE, out_dir=tmp_path, cfg=KEY, cache_dir=tmp_path / "c")
    assert r["sources"]["compose"] == "llm", r["errors"]
    assert r["compose"].count("@sha256:") == fallback.compose_text().count("@sha256:")
