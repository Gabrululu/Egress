"""Egress visual theme for Streamlit: monochrome, Inter, big radii, one blue accent.

Fonts load from Google Fonts when online; offline the system font stack is used,
so the demo never breaks.
"""
from __future__ import annotations

import html

BLUE = "#0099ff"
SEV_COLOR = {"high": "#e5484d", "medium": "#ffa600", "low": "#30a46c"}
SERVICE_DOT = {
    "S3": "#d97757", "RDS": "#5684d1", "DynamoDB": "#9168c0", "SQS": "#1ba1e3",
    "SNS": "#ffa600", "Lambda": "#e5484d", "CloudWatch": "#30a46c", "IAM": "#757575",
    "boto3": "#141414",
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');
:root{
  --eg-black:#000; --eg-ink:#141414; --eg-gray:#757575; --eg-line:#e9e9e9;
  --eg-bg:#f5f5f5; --eg-card:#fff; --eg-blue:#0099ff;
  --eg-font: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp button, .stApp textarea,
.stApp h1, .stApp h2, .stApp h3, .stApp h4 { font-family: var(--eg-font); }
.stApp { background: var(--eg-bg); color: var(--eg-black); }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] { max-width: 1180px; padding-top: 1.5rem; padding-bottom: 4rem; }
.stApp h1, .stApp h2, .stApp h3 { letter-spacing: -0.04em; font-weight: 800; }

/* ---------- hero */
.eg-hero { background: var(--eg-black); color: #fff; border-radius: 30px; padding: 48px 48px 40px;
  display: grid; grid-template-columns: 1.25fr 1fr; gap: 40px; align-items: center; margin-bottom: 8px; }
@media (max-width: 900px) { .eg-hero { grid-template-columns: 1fr; padding: 32px 24px; } }
.eg-brand { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 18px; letter-spacing: -0.03em; }
.eg-logo { width: 28px; height: 28px; border-radius: 9px; background: var(--eg-blue); display: grid; place-items: center;
  font-weight: 900; color: #fff; font-size: 16px; }
.eg-hero-title { font-size: clamp(40px, 5.6vw, 68px); font-weight: 900; line-height: .98; letter-spacing: -0.055em;
  margin: 22px 0 16px; color: #fff; }
.eg-hero-title em { font-style: normal; color: var(--eg-blue); }
.eg-hero-sub { color: rgba(255,255,255,.62); font-size: 17px; line-height: 1.5; max-width: 520px; margin: 0; }
.eg-trust { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 26px; }
.eg-pill { display: inline-flex; align-items: center; gap: 8px; padding: 7px 14px; border-radius: 60px;
  background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.12); color: rgba(255,255,255,.85);
  font-size: 13px; font-weight: 500; }
.eg-pill b { color: var(--eg-blue); font-weight: 700; }
.eg-map { display: flex; flex-direction: column; gap: 10px; }
.eg-map-row { display: grid; grid-template-columns: 1fr 24px 1fr; align-items: center; gap: 8px; }
.eg-map-cell { background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.10); border-radius: 18px;
  padding: 12px 16px; font-size: 14px; font-weight: 600; color: #fff; }
.eg-map-cell small { display: block; color: rgba(255,255,255,.45); font-weight: 500; font-size: 11px;
  text-transform: uppercase; letter-spacing: .06em; margin-bottom: 2px; }
.eg-map-cell.to { border-color: rgba(0,153,255,.45); background: rgba(0,153,255,.10); }
.eg-arrow { color: rgba(255,255,255,.4); text-align: center; font-size: 18px; }

/* ---------- sections */
.eg-section { margin: 56px 0 18px; }
.eg-kicker { display: inline-block; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  color: var(--eg-gray); border: 1px solid var(--eg-line); background: #fff; border-radius: 60px; padding: 5px 12px; }
.eg-title { font-size: clamp(30px, 3.6vw, 44px); font-weight: 800; letter-spacing: -0.045em; line-height: 1.05;
  margin: 14px 0 6px; color: var(--eg-black); }
.eg-sub { color: var(--eg-gray); font-size: 16px; margin: 0; }

/* ---------- cards */
.st-key-eg_source, .st-key-eg_llm, .st-key-eg_ba, .st-key-eg_arch, .st-key-eg_score, .st-key-eg_services, .st-key-eg_deploy {
  background: var(--eg-card); border: 1px solid var(--eg-line); border-radius: 30px; padding: 26px 28px; }
.eg-score-num { font-size: 88px; font-weight: 900; letter-spacing: -0.06em; line-height: 1; }
.eg-score-label { color: var(--eg-gray); font-size: 14px; font-weight: 500; }
.eg-badge { display: inline-flex; align-items: center; gap: 6px; border-radius: 60px; padding: 5px 12px;
  font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; }
.eg-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0 2px; }
.eg-chip { display: inline-flex; align-items: center; gap: 8px; background: var(--eg-bg); border-radius: 60px;
  padding: 7px 14px 7px 10px; font-size: 13px; font-weight: 600; color: var(--eg-ink); }
.eg-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.eg-chip span.n { color: var(--eg-gray); font-weight: 500; }

/* ---------- native widgets */
.stDownloadButton > button { border-radius: 60px; font-weight: 600; border: 1px solid var(--eg-line); background: #fff; }
.stDownloadButton > button:hover { border-color: var(--eg-black); color: var(--eg-black); }
.stButton > button { border-radius: 60px; font-weight: 600; padding: .55rem 1.3rem; border: 1px solid var(--eg-line);
  background: #fff; color: var(--eg-black); transition: background .15s, color .15s, transform .15s; }
.stButton > button:hover { border-color: var(--eg-black); color: var(--eg-black); transform: translateY(-1px); }
.stButton > button[kind="primary"], .stButton > button[data-testid="stBaseButton-primary"] {
  background: var(--eg-black); color: #fff; border: 1px solid var(--eg-black); }
.stButton > button[kind="primary"]:hover, .stButton > button[data-testid="stBaseButton-primary"]:hover {
  background: var(--eg-blue); border-color: var(--eg-blue); color: #fff; }
[data-testid="stTextInput"] div[data-baseweb="input"], [data-testid="stTextInput"] div[data-baseweb="base-input"] {
  border-radius: 60px; background: var(--eg-bg); }
[data-testid="stTextInput"] div[data-baseweb="input"] { border: 1px solid var(--eg-line); padding-left: 8px; }
[data-testid="stMetric"] { background: #fff; border: 1px solid var(--eg-line); border-radius: 24px; padding: 18px 22px; }
[data-testid="stMetricValue"] { font-weight: 800; letter-spacing: -0.03em; font-size: 26px; }
[data-testid="stMetricLabel"] p { color: var(--eg-gray); font-weight: 600; text-transform: uppercase;
  letter-spacing: .06em; font-size: 12px; }
[data-testid="stDataFrame"] { border-radius: 20px; overflow: hidden; border: 1px solid var(--eg-line); background: #fff; }
[data-testid="stExpander"] details { border-radius: 20px; background: #fff; border: 1px solid var(--eg-line); }
[data-testid="stAlert"] { border-radius: 20px; }
[data-testid="stCode"] pre, [data-testid="stCodeBlock"] pre { border-radius: 20px; }
[data-testid="stTabs"] [role="tablist"] { gap: 6px; background: #fff; border: 1px solid var(--eg-line);
  border-radius: 60px; padding: 6px; width: fit-content; max-width: 100%; overflow-x: auto; }
[data-testid="stTabs"] [role="tab"] { border-radius: 60px; padding: 8px 18px; height: auto; margin: 0; }
[data-testid="stTabs"] [role="tab"] p { font-weight: 600; font-size: 14px; }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] { background: var(--eg-black); }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] p { color: #fff; }
[data-testid="stTabs"] .react-aria-SelectionIndicator, [data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"] { display: none !important; }
[data-testid="stTabs"] [role="tablist"]::after, [data-testid="stTabs"] [role="tablist"]::before { display: none; }
.stButton > button p { white-space: nowrap; }
[data-testid="stSidebar"] { background: #fff; border-right: 1px solid var(--eg-line); }
[data-testid="stMultiSelect"] span[data-baseweb="tag"] { border-radius: 60px; }

.st-key-eg_llm { padding: 16px 20px; margin-bottom: 14px; }
.eg-llm { display: flex; align-items: center; gap: 14px; }
.eg-llm-icon { width: 42px; height: 42px; border-radius: 14px; display: grid; place-items: center;
  font-size: 20px; flex: none; }
.eg-llm-title { font-weight: 700; font-size: 15px; letter-spacing: -0.01em; color: var(--eg-black); }
.eg-llm-sub { color: var(--eg-gray); font-size: 13px; margin-top: 2px; }
.eg-llm-sub code { background: var(--eg-bg); border-radius: 6px; padding: 1px 6px; font-size: 12px; }
[data-testid="stPopover"] button { border-radius: 60px; font-weight: 600; border: 1px solid var(--eg-line); background: #fff; }
[data-testid="stPopover"] button:hover { border-color: var(--eg-black); color: var(--eg-black); }
.st-key-eg_ba, .st-key-eg_arch { margin-bottom: 14px; }
.eg-ba { display: grid; grid-template-columns: 1fr auto 1fr auto; gap: 28px; align-items: center; }
@media (max-width: 760px) { .eg-ba { grid-template-columns: 1fr; gap: 10px; } .eg-ba-arrow { display: none; } }
.eg-ba-label { color: var(--eg-gray); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; }
.eg-ba-num { font-size: 64px; font-weight: 900; letter-spacing: -0.06em; line-height: 1; margin-top: 6px; }
.eg-ba-num small { font-size: 18px; color: var(--eg-gray); font-weight: 600; letter-spacing: 0; margin-left: 4px; }
.eg-ba-arrow { font-size: 32px; color: #c4c4c4; }
.eg-ba-delta { background: #30a46c1a; color: #30a46c; border-radius: 60px; padding: 10px 18px;
  font-weight: 800; font-size: 18px; white-space: nowrap; }
.eg-ba-note { color: var(--eg-gray); font-size: 13px; margin-top: 6px; }
.eg-links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.eg-links a { text-decoration: none; color: var(--eg-black); background: #fff; border: 1px solid var(--eg-line);
  border-radius: 60px; padding: 7px 14px; font-size: 13px; font-weight: 600; }
.eg-links a:hover { border-color: var(--eg-blue); color: var(--eg-blue); }
.eg-footer { margin-top: 72px; padding-top: 24px; border-top: 1px solid var(--eg-line); color: var(--eg-gray);
  font-size: 13px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
</style>
"""

HERO_MAP = [("S3", "SeaweedFS"), ("RDS", "PostgreSQL"), ("SQS", "RabbitMQ"), ("Lambda", "Container worker")]


def esc(s) -> str:
    return html.escape(str(s))


def hero(llm_label: str) -> str:
    rows = "".join(
        f'<div class="eg-map-row"><div class="eg-map-cell"><small>AWS</small>{a}</div>'
        f'<div class="eg-arrow">→</div><div class="eg-map-cell to"><small>Open source</small>{b}</div></div>'
        for a, b in HERO_MAP)
    return f"""
<div class="eg-hero">
  <div>
    <div class="eg-brand"><div class="eg-logo">E</div>Egress</div>
    <div class="eg-hero-title">Leave AWS.<br><em>Keep your app.</em></div>
    <p class="eg-hero-sub">Scan a repository for hyperscaler lock-in, score it, generate the migration
      to open-source, EU-sovereign services, and run the new stack locally in minutes.</p>
    <div class="eg-trust">
      <span class="eg-pill"><b>●</b> {esc(llm_label)}</span>
      <span class="eg-pill">Offline fallback</span>
      <span class="eg-pill">docker compose</span>
      <span class="eg-pill">Apache 2.0</span>
    </div>
  </div>
  <div class="eg-map">{rows}</div>
</div>"""


PROVIDERS = [("mistral.ai", "Mistral"), ("openai.com", "OpenAI"), ("models.github.ai", "GitHub Models"),
             ("anthropic.com", "Anthropic"), ("11434", "Ollama"), ("ollama", "Ollama"),
             ("localhost", "Local model"), ("127.0.0.1", "Local model")]


def provider_name(base_url: str) -> str:
    u = (base_url or "").lower()
    return next((name for key, name in PROVIDERS if key in u), "Custom endpoint")


def llm_status(provider: str, model: str, enabled: bool, offline: bool) -> str:
    if enabled and not offline:
        icon, bg, title = "🤖", "#0099ff1a", f"{esc(provider)} · <code>{esc(model)}</code>"
        sub = "LLM generates the migration; every part is validated, templates cover failures."
    elif enabled:
        icon, bg, title = "📦", "#f0f0f0", "Offline mode"
        sub = f"Using built-in templates. {esc(provider)} is configured but switched off."
    else:
        icon, bg, title = "📦", "#f0f0f0", "Offline templates"
        sub = "No API key configured: works fully offline. Add one in <code>.env</code> to enable the LLM."
    return (f'<div class="eg-llm"><div class="eg-llm-icon" style="background:{bg}">{icon}</div>'
            f'<div><div class="eg-llm-title">{title}</div><div class="eg-llm-sub">{sub}</div></div></div>')


def section(kicker: str, title: str, sub: str = "") -> str:
    sub_html = f'<p class="eg-sub">{esc(sub)}</p>' if sub else ""
    return (f'<div class="eg-section"><span class="eg-kicker">{esc(kicker)}</span>'
            f'<div class="eg-title">{esc(title)}</div>{sub_html}</div>')


def score_color(score: int) -> str:
    return SEV_COLOR["high"] if score >= 70 else SEV_COLOR["medium"] if score >= 40 else SEV_COLOR["low"]


def score_card(score: int, rating: str, findings: int, files: int) -> str:
    c = score_color(score)
    pct = max(0, min(100, int(score)))
    return f"""
<div class="eg-score-label">Overall lock-in score</div>
<div style="display:flex;align-items:flex-end;gap:10px;margin:8px 0 14px">
  <div class="eg-score-num" style="color:{c}">{pct}</div>
  <div style="color:#757575;font-weight:600;padding-bottom:12px">/100</div>
</div>
<div style="height:10px;background:#f0f0f0;border-radius:60px;overflow:hidden">
  <div style="width:{pct}%;height:100%;background:{c};border-radius:60px"></div>
</div>
<div style="display:flex;justify-content:space-between;align-items:center;margin-top:16px">
  <span class="eg-badge" style="background:{c}1a;color:{c}">{esc(rating)} lock-in</span>
  <span class="eg-score-label">{findings} findings · {files} files</span>
</div>"""


def service_chips(services: dict) -> str:
    chips = "".join(
        f'<span class="eg-chip"><span class="eg-dot" style="background:{SERVICE_DOT.get(k, "#757575")}"></span>'
        f'{esc(k)} <span class="n">{v["score"]}</span></span>'
        for k, v in services.items())
    return f'<div class="eg-chips">{chips}</div>'


def public_url(port: int, path: str = "") -> str:
    """Browser-reachable URL for a local port: Codespaces forwarded URL when available."""
    import os
    name, domain = os.environ.get("CODESPACE_NAME"), os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    if name and domain:
        return f"https://{name}-{port}.{domain}{path}"
    return f"http://localhost:{port}{path}"


def before_after(before: int, before_rating: str, after: dict) -> str:
    a = int(after.get("overall_score", 0))
    delta = before - a
    pct = round(100 * delta / before) if before else 0
    note = (f"{after.get('findings', 0)} findings left · {after.get('mitigated', 0)} mitigated "
            f"(S3 API via custom endpoint)")
    return f"""
<div class="eg-ba">
  <div><div class="eg-ba-label">Before · AWS</div>
    <div class="eg-ba-num" style="color:{score_color(before)}">{before}<small>/100</small></div>
    <div class="eg-ba-note">{esc(before_rating)} lock-in</div></div>
  <div class="eg-ba-arrow">→</div>
  <div><div class="eg-ba-label">After · migrated stack</div>
    <div class="eg-ba-num" style="color:{score_color(a)}">{a}<small>/100</small></div>
    <div class="eg-ba-note">{esc(note)}</div></div>
  <div class="eg-ba-delta">−{delta} pts · {pct}% less lock-in</div>
</div>"""


def links(items: dict) -> str:
    return '<div class="eg-links">' + "".join(
        f'<a href="{esc(u)}" target="_blank">{esc(k)} ↗</a>' for k, u in items.items()) + "</div>"


def footer() -> str:
    return ('<div class="eg-footer"><span>Egress · Apache 2.0 · AWS → open source / EU-sovereign</span>'
            '<span>Scanner rules + LLM copilot with offline templates</span></div>')
