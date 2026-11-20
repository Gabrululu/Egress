"""Egress - Streamlit UI: scan -> generate migration -> deploy locally."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from copilot import deploy
from copilot.config import load_config
from copilot.generator import DEFAULT_OUT, generate
from scanner import scan
from scanner.source import SourceError, cleanup, resolve_source

ROOT = Path(__file__).resolve().parent
OUT = DEFAULT_OUT
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
LINKS = {
    "API health": "http://localhost:5000/health",
    "API uploads": "http://localhost:5000/uploads",
    "RabbitMQ UI (egress / egress-demo)": "http://localhost:15672",
    "SeaweedFS filer UI": "http://localhost:9001",
}

st.set_page_config(page_title="Egress", page_icon="🚪", layout="wide")
ss = st.session_state
ss.setdefault("report", None)
ss.setdefault("migration", None)
ss.setdefault("clone_dir", None)
ss.setdefault("scan_path", None)


def color(score: int) -> str:
    return "#d64545" if score >= 70 else "#e0a106" if score >= 40 else "#2e9e5b"


def gauge(score: int, label: str) -> str:
    """Semicircle SVG gauge (no extra deps)."""
    score = max(0, min(100, int(score)))
    length = 251.3  # pi * r (r=80)
    filled = length * score / 100
    c = color(score)
    return f"""
<div style="text-align:center">
<svg viewBox="0 0 200 120" width="260">
  <path d="M20 100 A80 80 0 0 1 180 100" fill="none" stroke="#e6e6e6" stroke-width="18" stroke-linecap="round"/>
  <path d="M20 100 A80 80 0 0 1 180 100" fill="none" stroke="{c}" stroke-width="18" stroke-linecap="round"
        stroke-dasharray="{filled:.1f} {length:.1f}"/>
  <text x="100" y="92" text-anchor="middle" font-size="34" font-weight="700" fill="{c}">{score}</text>
  <text x="100" y="114" text-anchor="middle" font-size="12" fill="#888">/100 · {label} lock-in</text>
</svg></div>"""


def fail(msg: str, exc: Exception | None = None):
    st.error(f"{msg}{': ' + str(exc) if exc else ''}")


# ------------------------------------------------------------------ sidebar
cfg = load_config()
with st.sidebar:
    st.header("LLM")
    if cfg.enabled:
        st.success(f"Configured: `{cfg.model}`")
    else:
        st.warning("No LLM_API_KEY: offline templates will be used.")
    st.caption(f"Endpoint: {cfg.base_url}")
    force_offline = st.checkbox("Force offline templates", value=False)
    st.divider()
    st.caption("Egress · Apache 2.0 · AWS → open-source / EU-sovereign")

# ------------------------------------------------------------------ header
st.title("🚪 Egress")
st.markdown("Detect AWS lock-in, score it, generate a migration to open-source alternatives, "
            "and run the migrated stack locally.")

col_in, col_btn = st.columns([5, 1])
with col_in:
    src = st.text_input("Repository (local path or git URL)", value=str(ROOT / "sample_app"))
with col_btn:
    st.write("")
    st.write("")
    do_scan = st.button("🔍 Scan", type="primary", width="stretch")

# ------------------------------------------------------------------ 1. scan
if do_scan:
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
    st.subheader("1 · Lock-in scan")
    services = report["services"]
    if not report["findings"]:
        st.success(f"No AWS lock-in detected in {report['scanned_files']} relevant files.")
    g, s = st.columns([1, 2])
    with g:
        st.markdown(gauge(report["overall_score"], report["rating"]), unsafe_allow_html=True)
        st.caption(f"{len(report['findings'])} findings · {report['scanned_files']} files scanned")
    with s:
        if services:
            df_s = pd.DataFrame([{"service": k, "score": v["score"], "findings": v["findings"],
                                  "max severity": v["max_severity"], "alternative": v["alternative"]}
                                 for k, v in services.items()])
            st.dataframe(df_s, hide_index=True, width="stretch",
                         column_config={"score": st.column_config.ProgressColumn(
                             "score", min_value=0, max_value=100, format="%d")})
    if report["findings"]:
        df = pd.DataFrame(report["findings"])
        df["_o"] = df["severity"].map(SEV_ORDER)
        df = df.sort_values(["_o", "service", "file", "line"]).drop(columns=["_o", "rule"])
        sev = st.multiselect("Severity filter", ["high", "medium", "low"], default=["high", "medium", "low"])
        st.dataframe(df[df["severity"].isin(sev)], hide_index=True, width="stretch",
                     column_order=["severity", "service", "file", "line", "evidence", "lock_in_reason"])
    with st.expander("Scoring formula"):
        st.code(report["formula"])

    # -------------------------------------------------------------- 2. migrate
    st.subheader("2 · Migration")
    if st.button("🧠 Generate migration", type="primary"):
        with st.spinner("Generating migration (LLM with offline fallback)..."):
            try:
                ss.migration = generate(ss.scan_path, scan_report=report, out_dir=OUT,
                                        use_llm=not force_offline, cfg=cfg)
            except Exception as e:
                ss.migration = None
                fail("Migration generation failed", e)

    m = ss.migration
    if m:
        badge = {"llm": "🤖 LLM", "fallback": "📦 Offline templates", "mixed": "🤖+📦 LLM with template fallback"}
        v = m["compose_validation"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Generation path", badge[m["path"]])
        c2.metric("Compose validation", "✅ valid" if v["ok"] else "❌ invalid", help=f"{v['method']}: {v['message']}")
        c3.metric("Files written", len(m["files"]), help=m["out_dir"])
        st.caption("Per part: " + " · ".join(f"{k}: **{s}**" for k, s in m["sources"].items()))
        for e in m["errors"]:
            st.info(e)

        t_map, t_compose, t_diff, t_plan = st.tabs(["Mapping", "docker-compose.yml", "Code diffs", "Plan"])
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

# ------------------------------------------------------------------ 3. deploy
if (OUT / "docker-compose.yml").is_file():
    st.subheader("3 · Run the migrated stack")
    st.caption(f"`docker compose up -d` in `{OUT}` · first run builds images (~1-2 min)")
    b1, b2, b3, b4 = st.columns(4)
    if b1.button("🚀 Deploy", type="primary", width="stretch"):
        with st.spinner("docker compose up -d --build --wait ..."):
            try:
                deploy.up(OUT)
                st.success("Stack is up and healthy.")
            except deploy.DeployError as e:
                fail("Deploy failed", e)
    if b2.button("⏹ Stop", width="stretch"):
        with st.spinner("docker compose down ..."):
            try:
                deploy.down(OUT)
                st.success("Stack stopped.")
            except deploy.DeployError as e:
                fail("Stop failed", e)
    b3.button("🔄 Refresh status", width="stretch")
    smoke = b4.button("🧪 Smoke test upload", width="stretch")

    try:
        rows = deploy.status(OUT)
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No containers running.")
    except deploy.DeployError as e:
        fail("Could not read container status", e)

    if smoke:
        import time

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

    st.markdown(" · ".join(f"[{k}]({u})" for k, u in LINKS.items()))
