"""Egress - Streamlit UI: scan -> generate migration -> deploy locally."""
from __future__ import annotations

import secrets
import time
from pathlib import Path

import pandas as pd
import streamlit as st

import ui_theme as ui
from copilot import access, deploy, runner
from copilot.config import load_config
from copilot.diagram import architecture_dot
from copilot.generator import DEFAULT_OUT, generate
from copilot.llm import candidate_files, prompt_preview
from copilot.report import build_report_md, build_zip
from scanner import scan
from scanner.source import SourceError, cleanup, resolve_source

ROOT = Path(__file__).resolve().parent
OUT = DEFAULT_OUT
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
LINKS = {  # resolved to Codespaces forwarded URLs when running in a Codespace
    "API health": ui.public_url(5000, "/health"),
    "API uploads": ui.public_url(5000, "/uploads"),
    "RabbitMQ UI · user egress, password in output/.env": ui.public_url(15672),
    "SeaweedFS filer": ui.public_url(9001),
}
PATH_BADGE = {"llm": "🤖 LLM", "fallback": "📦 Offline templates", "mixed": "🤖+📦 LLM with template fallback"}

st.set_page_config(page_title="Egress · AWS lock-in scanner", page_icon="🚪", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown(ui.CSS, unsafe_allow_html=True)
ss = st.session_state
ss.setdefault("report", None)
ss.setdefault("migration", None)
ss.setdefault("clone_dir", None)
ss.setdefault("scan_path", None)


def md(html: str):
    st.markdown(html, unsafe_allow_html=True)


def fail(msg: str, exc: Exception | None = None):
    st.error(f"{msg}{': ' + str(exc) if exc else ''}")


cfg = load_config()
PROVIDER = ui.provider_name(cfg.base_url)
ss.setdefault("offline", not cfg.enabled)

# Public demo mode (EGRESS_PUBLIC=1): visitors are sandboxed; the presenter unlocks with ?key=...
PUBLIC = access.public_mode()
_qkey = st.query_params.get("key")
if _qkey:
    if access.is_presenter(_qkey):
        ss.presenter = True
    del st.query_params["key"]  # never leave the key visible in the address bar
ss.setdefault("presenter", not PUBLIC)
PRESENTER = ss.presenter
ss.setdefault("sid", secrets.token_hex(8))
WORK = OUT if PRESENTER else access.visitor_out_dir(ss.sid)  # visitors never touch output/


def llm_panel():
    """Compact LLM status + offline toggle + settings popover, shown next to Generate."""
    with st.container(key="eg_llm"):
        c_status, c_toggle, c_cfg = st.columns([3, 1.3, 1.2], vertical_alignment="center")
        with c_status:
            md(ui.llm_status(PROVIDER, cfg.model, cfg.enabled, ss.offline))
        with c_toggle:
            st.toggle("Offline mode", key="offline", disabled=not cfg.enabled,
                      help="Use the built-in templates only: no network, no code sent to an LLM.")
        with c_cfg, st.popover("⚙ LLM settings", width="stretch"):
            st.markdown(f"**Provider** · {PROVIDER}  \n**Model** · `{cfg.model}`  \n"
                        f"**Endpoint** · `{cfg.base_url}`  \n**Timeout** · {cfg.timeout:.0f}s  \n"
                        f"**API key** · {'set ✅' if cfg.api_key else 'not set'}")
            st.caption("Configure in `.env` (see `.env.example`), then restart `make run`. "
                       "Any OpenAI-compatible endpoint works: Mistral, OpenAI, Ollama, vLLM.")
            st.code("LLM_BASE_URL=https://api.mistral.ai/v1\nLLM_API_KEY=...\n"
                    "LLM_MODEL=mistral-large-latest\nLLM_TIMEOUT=120", language="bash")


# ------------------------------------------------------------------ hero + source
md(ui.hero(f"{PROVIDER} · {cfg.model}" if cfg.enabled else "Offline mode"))
if PUBLIC and not PRESENTER:
    st.info("👋 **Public demo.** Scan the bundled AWS sample app, generate its migration and download it. "
            "LLM answers come from the presenter's cache (or offline templates); deploy controls are "
            "reserved for the presenter. Run your own copy with `git clone` + `make install run`.")
elif PUBLIC:
    st.success("🎤 Presenter mode: full access on this browser session.")

md(ui.section("01 · Scan", "Find the lock-in.",
              "Point Egress at a local folder or a git URL. Rules cover Terraform, compose, Python and requirements."))
with st.container(key="eg_source"):
    col_in, col_btn = st.columns([4, 1], vertical_alignment="bottom")
    with col_in:
        src = st.text_input("Repository: local path or git URL", value=str(ROOT / "sample_app"),
                            disabled=not PRESENTER,
                            help=None if PRESENTER else "Public demo: only the bundled sample app can be scanned.")
    with col_btn:
        do_scan = st.button("Scan  →", type="primary", width="stretch")

# ------------------------------------------------------------------ 1. scan
if do_scan and not PRESENTER and not access.visitor_source_allowed(src):
    fail("Public demo: only the bundled sample app can be scanned")
elif do_scan:
    with st.spinner("Resolving source and scanning..."):
        try:
            cleanup(ss.clone_dir)
            path, ss.clone_dir = resolve_source(src)
            ss.report = scan(path)
            ss.scan_path = str(path)
            ss.migration = None
        except SourceError as e:
            ss.report = None
            fail("Source error", e)
        except Exception as e:  # never show a stack trace
            ss.report = None
            fail("Scan failed", e)

report = ss.report
if report:
    services = report["services"]
    md(ui.section("Result", "Lock-in scan",
                  f"{len(report['findings'])} findings across {len(services)} AWS services."))
    if not report["findings"]:
        st.success(f"No AWS lock-in detected in {report['scanned_files']} relevant files.")

    c_score, c_svc = st.columns([1, 2], gap="medium")
    with c_score, st.container(key="eg_score"):
        md(ui.score_card(report["overall_score"], report["rating"],
                         len(report["findings"]), report["scanned_files"]))
        with st.expander("Scoring formula"):
            st.code(report["formula"], language=None)
    with c_svc, st.container(key="eg_services"):
        st.markdown("**Services detected**")
        if services:
            md(ui.service_chips(services))
            df_s = pd.DataFrame([{"service": k, "score": v["score"], "findings": v["findings"],
                                  "max severity": v["max_severity"], "alternative": v["alternative"]}
                                 for k, v in services.items()])
            st.dataframe(df_s, hide_index=True, width="stretch",
                         column_config={"score": st.column_config.ProgressColumn(
                             "score", min_value=0, max_value=100, format="%d")})
        else:
            st.caption("Nothing to migrate.")

    if report["findings"]:
        df = pd.DataFrame(report["findings"])
        df["_o"] = df["severity"].map(SEV_ORDER)
        df = df.sort_values(["_o", "service", "file", "line"]).drop(columns=["_o", "rule"])
        st.download_button("⬇ Scan report (.md)", data=build_report_md(report), file_name="egress-scan-report.md",
                           mime="text/markdown", on_click="ignore")
        with st.expander("Detected architecture", expanded=False):
            st.graphviz_chart(architecture_dot(services), width="stretch")
        with st.expander(f"All findings ({len(df)})", expanded=False):
            sev = st.multiselect("Severity", ["high", "medium", "low"], default=["high", "medium", "low"])
            st.dataframe(df[df["severity"].isin(sev)], hide_index=True, width="stretch",
                         column_order=["severity", "service", "file", "line", "evidence", "lock_in_reason"])

    # -------------------------------------------------------------- 2. migrate
    md(ui.section("02 · Migrate", "Generate the exit plan.",
                  "Mapping, new docker-compose, code diffs and a plan, from the LLM with validated template fallback."))
    llm_panel()
    files = candidate_files(report)
    inc_key = f"include::{ss.scan_path}"
    with st.expander("🔍 What will be sent to the LLM", expanded=False):
        if ss.offline:
            st.caption("Offline mode: nothing leaves this machine.")
        include = st.multiselect("Files included as context", files, default=files, key=inc_key,
                                 help="Uncheck files you don't want to send. Findings are still summarised.")
        try:
            pv = prompt_preview(report, Path(ss.scan_path), include)
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Files sent", f"{len(pv['files'])}/{len(files)}")
            k2.metric("≈ Input tokens", f"{pv['est_input_tokens']:,}")
            k3.metric("≈ Output tokens", "4k-8k")
            k4.metric("Secrets redacted", pv["redactions"])
            st.caption("Token counts are estimates (~4 characters per token); check your provider's pricing. "
                       "Likely secrets (AWS keys, passwords, tokens, private keys) are masked before sending.")
            with st.popover("View full prompt", width="content"):
                st.markdown("**System prompt**")
                st.code(pv["system"], language="markdown")
                st.markdown("**User prompt** (scan summary + selected files)")
                st.code(pv["user"], language="markdown")
        except Exception as e:
            fail("Could not build the prompt preview", e)
    _job = deploy.current_job(WORK)
    if st.button("Generate migration", type="primary", disabled=bool(_job and _job.running),
                 help="Disabled while a deploy is running." if _job and _job.running else None):
        spin = "Generating from templates..." if ss.offline else f"Asking {PROVIDER} (template fallback ready)..."
        with st.spinner(spin):
            try:
                ss.migration = generate(ss.scan_path, scan_report=report, out_dir=WORK,
                                        use_llm=not ss.offline, cfg=cfg,
                                        include_files=ss.get(inc_key, files),
                                        llm_cache_only=not PRESENTER)
            except Exception as e:
                ss.migration = None
                fail("Migration generation failed", e)

    m = ss.migration
    if m:
        v = m["compose_validation"]
        if m.get("after"):
            with st.container(key="eg_ba"):
                md(ui.before_after(report["overall_score"], report["rating"], m["after"]))
            with st.container(key="eg_arch"):
                st.graphviz_chart(architecture_dot(report["services"]), width="stretch")
                st.graphviz_chart(architecture_dot(report["services"], migrated=True), width="stretch")
        c1, c2, c3 = st.columns(3)
        c1.metric("Generation path", PATH_BADGE[m["path"]])
        c2.metric("Compose validation", "✅ valid" if v["ok"] else "❌ invalid",
                  help=f"{v['method']}: {v['message']}")
        c3.metric("Files written", len(m["files"]), help=m["out_dir"])
        info = " · ".join(f"{k}: **{s}**" for k, s in m["sources"].items())
        llm = m.get("llm") or {}
        if llm.get("cache") or llm.get("calls"):
            info += f" ┃ LLM calls: **{llm.get('calls', 0)}** · cache: **{llm.get('cache') or '-'}**"
            if llm.get("repair"):
                info += f" · repair: **{llm['repair']}**"
        st.caption("Per part: " + info)
        sp = m.get("source_patch") or {}
        if sp:
            applied = sum(x["status"] == "applied" for x in sp["patches"])
            if sp["app_source"] == "patched-repo":
                st.caption(f"🧩 Runnable app built **from your repo** + {applied} applied patches"
                           + (f" · retired: {', '.join(sp['retired'])}" if sp["retired"] else "") + ".")
            else:
                st.caption(f"📦 Runnable app: reference template ({sp['reason']}).")
            with st.expander("Patch details", expanded=False):
                icon = {"applied": "✅", "skipped": "⏭", "failed": "❌"}
                for x in sp["patches"]:
                    st.markdown(f"{icon[x['status']]} `{x['file']}` · {x['status']}"
                                + (f" · {x['detail']}" if x.get("detail") else ""))
                for prob in sp.get("problems", []):
                    st.markdown(f"⚠️ {prob}")
        sec = m.get("secrets") or {}
        if sec:
            msg = "🔐 Credentials live in `output/.env` (gitignored, chmod 600)"
            msg += f": generated {', '.join(sec['created'])}" if sec.get("created") else ": reused existing values"
            st.caption(msg + ".")
            if sec.get("legacy_db_password") and PRESENTER:
                st.warning("An existing Postgres volume was found, so the old demo DB password is kept to stay "
                           "compatible. Use **Stop** with *Wipe data & rotate credentials* to switch to random ones.")
        for e in m["errors"]:
            st.info(e)

        t_map, t_compose, t_diff, t_plan, t_data = st.tabs(
            ["Mapping", "docker-compose.yml", "Code diffs", "Plan", "Data migration"])
        with t_map:
            st.dataframe(pd.DataFrame(m["mapping"]), hide_index=True, width="stretch")
        with t_compose:
            st.code(m["compose"], language="yaml")
        with t_diff:
            for d in m["diffs"]:
                with st.expander(d["file"], expanded=False):
                    st.code(d["diff"], language="diff")
        with t_plan:
            st.markdown(m["plan"])
        with t_data:
            dm = m.get("data_migration") or {}
            ddir = Path(dm.get("dir", ""))
            if (ddir / "README.md").is_file():
                readme = (ddir / "README.md").read_text(encoding="utf-8")
                st.markdown("\n".join("###" + ln if ln.startswith("#") else ln for ln in readme.splitlines()))
                for name in dm.get("scripts", []):
                    with st.expander(f"output/data-migration/{name}", expanded=False):
                        lang = "bash" if name.endswith(".sh") else "python" if name.endswith(".py") else "markdown"
                        st.code((ddir / name).read_text(encoding="utf-8"), language=lang)
            else:
                st.caption("No data migration scripts generated.")

        report_md = build_report_md(report, m)
        d1, d2, _ = st.columns([1, 1, 2])
        d1.download_button("⬇ Migration (.zip)", data=build_zip(WORK, report_md), file_name="egress-migration.zip",
                           mime="application/zip", width="stretch", on_click="ignore",
                           help="compose, app, diffs, plan and REPORT.md. Secrets (.env) are never included.")
        d2.download_button("⬇ Report (.md)", data=report_md, file_name="egress-report.md",
                           mime="text/markdown", width="stretch", on_click="ignore")

else:  # keep the 01 → 02 → 03 story visible before the first scan
    md(ui.section("02 · Migrate", "Generate the exit plan.",
                  "Mapping, new docker-compose, code diffs and a plan, from the LLM with validated template fallback."))
    st.caption("⬆ Run a scan first: the migration is generated from its findings.")

# ------------------------------------------------------------------ 3. deploy
def show_job(job):
    if job is None:
        return
    secs = f"{job.elapsed:.0f}s"
    if job.running:
        st.info(f"⏳ Deploying in the background… {secs}. You can keep using the page.")
        st.code(job.tail() or "starting docker compose…", language=None)
    else:
        outcome = runner.read_outcome(OUT) or {}
        if outcome.get("fallback"):
            st.warning(f"The LLM-written app failed at runtime ({outcome.get('reason')}), so Egress redeployed "
                       "the reference app automatically. The LLM version is kept in `output/app.llm-failed/`.")
            with st.expander("Why the LLM app failed (api/worker logs)"):
                st.code(outcome.get("llm_logs") or "(no logs)", language=None)
        if job.returncode == 0:
            st.success(f"Stack is up and healthy: /health verified ({secs}).")
        else:
            st.error(f"Deploy failed (exit code {job.returncode}, {secs}). Last log lines:")
            st.code(job.tail(), language=None)


def smoke_test():
    import requests  # ships with streamlit
    try:
        r = requests.post("http://localhost:5000/upload",
                          files={"file": ("egress-demo.txt", b"hello from egress\n")}, timeout=10)
        r.raise_for_status()
        st.success(f"Uploaded → `{r.json()}`")
        time.sleep(1.5)
        st.write("Latest uploads (status set by the worker):")
        st.dataframe(pd.DataFrame(requests.get("http://localhost:5000/uploads", timeout=10).json()),
                     hide_index=True, width="stretch")
    except Exception as e:
        fail("Smoke test failed (is the stack deployed?)", e)


def show_status():
    try:
        rows = deploy.status(OUT)
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No containers running.")
    except deploy.DeployError as e:
        fail("Could not read container status", e)


if (OUT / "docker-compose.yml").is_file() and not PRESENTER:
    md(ui.section("03 · Run", "Same app. Zero AWS.", "The presenter's migrated stack, live in this Codespace."))
    with st.container(key="eg_deploy"):
        st.caption("Deploy / Stop are presenter-only. You can watch the containers and run the smoke test.")
        show_status()
        if st.button("Smoke test upload", width="stretch"):
            smoke_test()
elif (OUT / "docker-compose.yml").is_file():
    md(ui.section("03 · Run", "Same app. Zero AWS.",
                  f"docker compose up in {OUT}. First run builds images (about 1-2 min)."))
    job = deploy.current_job(OUT)
    deploying = bool(job and job.running)

    @st.fragment(run_every=2 if deploying else None)
    def live_panel():
        j = deploy.current_job(OUT)
        show_job(j)
        show_status()
        # When the background job finishes, rerun the whole app once to re-enable buttons.
        if j and not j.running and ss.get("deploy_seen") != j.id:
            ss.deploy_seen = j.id
            st.rerun()

    with st.container(key="eg_deploy"):
        b1, b2, b3, b4 = st.columns(4)
        if b1.button("Deploy stack", type="primary", width="stretch", disabled=deploying):
            try:
                deploy.start_up(OUT)
                st.rerun()
            except deploy.DeployError as e:
                fail("Deploy blocked", e)
        wipe = st.checkbox("Wipe data & rotate credentials on Stop",
                           help="docker compose down --volumes, then new random passwords in output/.env.")
        if b2.button("Stop", width="stretch"):
            with st.spinner("docker compose down ..."):
                try:
                    deploy.down(OUT, wipe=wipe)
                    st.success("Stack stopped, data wiped and credentials rotated." if wipe else "Stack stopped.")
                except deploy.DeployError as e:
                    fail("Stop failed", e)
        b3.button("Refresh status", width="stretch")
        smoke = b4.button("Smoke test upload", width="stretch", disabled=deploying)

        live_panel()

        if smoke:
            smoke_test()

        md(ui.links(LINKS))

md(ui.footer())
