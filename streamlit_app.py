"""Shop Apotheke weekly margin dashboard.

Reads the latest snapshot in `exports/` (produced by `weekly_export.py`)
and renders a password-gated multi-tab dashboard.

Margin model in `margin_model.py` mirrors the `Margen Calc pharma` sheet
from Margin_Check_V5.xlsx: 16% commission on gross, country-specific
VAT (pharma-reduced rates), DHL rate-card shipping cost with Nov+Dec
peak surcharge, 3PL rate card (per-order fixed + per-pick variable),
GB COGS × 0.83.
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = REPO_ROOT / "exports"

st.set_page_config(
    page_title="Shop Apotheke — Margin",
    page_icon="💊",
    layout="wide",
)


# ---------- Vanatari brand tokens ----------
# Colours below map 1:1 to the corporate design booklet.
VNA_PLUM = "#3c1826"
VNA_PLUM_60 = "#928589"
VNA_ORANGE = "#ff5c3e"
VNA_ORANGE_60 = "#ff9d8b"
VNA_LIGHT_BLUE = "#e3eef6"
VNA_BEIGE = "#fbf7f2"
VNA_SECONDARY_BLUE = "#6c91cc"
VNA_SECONDARY_PINK = "#f5c0fb"


# ---------- Vanatari CD stylesheet ----------
# Loads Literata (serif, headline face) from Google Fonts and applies it to
# all Streamlit markdown headings. Body copy stays on the system sans-serif
# stack (Satoshi would be the exact brand pair; the Streamlit host doesn't
# ship it, so the closest available geometric sans is used).
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Literata:ital,wght@0,400;0,600;0,700;1,400&display=swap');

    /* Headline face — Literata serif for markdown-rendered headings */
    [data-testid="stMarkdownContainer"] h1,
    [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3 {
        font-family: 'Literata', Georgia, 'Times New Roman', serif !important;
        color: #3c1826 !important;
        letter-spacing: -0.01em;
    }
    /* Metric labels — plum, small caps feel via letter-spacing */
    [data-testid="stMetricLabel"] {
        color: #3c1826 !important;
        font-weight: 600 !important;
    }
    /* Metric value — larger, plum */
    [data-testid="stMetricValue"] {
        color: #3c1826 !important;
    }
    /* Tab labels: plum default, orange when active */
    button[data-baseweb="tab"] { color: #3c1826 !important; }
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #ff5c3e !important;
        border-bottom-color: #ff5c3e !important;
    }
    /* Filter card border (st.container(border=True)) uses beige rather
       than the default grey line, matching Vanatari's element separators. */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: #f0e6d9 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- auth ----------
def _password() -> str | None:
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if pw:
        return pw
    try:
        return st.secrets["DASHBOARD_PASSWORD"]
    except Exception:
        return None


def require_login() -> None:
    expected = _password()
    if not expected:
        st.error("DASHBOARD_PASSWORD is not set. Configure it in the host's "
                 "secrets or environment.")
        st.stop()
    if st.session_state.get("auth_ok"):
        return
    st.title("Shop Apotheke — Margin")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in")
    if ok:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


# ---------- data ----------
def latest_export() -> Path | None:
    if not EXPORTS_DIR.exists():
        return None
    files = sorted(
        list(EXPORTS_DIR.glob("shopapo_export_*.csv"))
        + list(EXPORTS_DIR.glob("shopapo_export_*.csv.gz"))
    )
    return files[-1] if files else None


_FLOAT_COLS = ("gross_revenue", "refunded", "net_revenue", "commission",
               "shipping_cost_net", "product_cost", "dhl_cost",
               "three_pl_cost", "CM1", "CM2", "CM3", "ad_spend",
               "ad_spend_direct", "ad_revenue")
_INT_COLS = ("orders", "units")
_CATEGORICAL_COLS = ("sku", "country", "product_title")


@st.cache_data(show_spinner=False)
def load(path: Path) -> pd.DataFrame:
    """Read the snapshot CSV with downcast dtypes + derived CM%-of-revenue
    columns baked in, so we hold one compact frame in memory per session."""
    df = pd.read_csv(path)
    df["period"] = pd.to_datetime(df["period"], errors="coerce").dt.normalize()
    for c in _FLOAT_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce", downcast="float")
    for c in _INT_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce", downcast="integer")
    for c in _CATEGORICAL_COLS:
        if c in df.columns:
            df[c] = df[c].astype("category")
    # Bake CM%-of-net-revenue once instead of recomputing on every interaction.
    # Use float NaN (not pd.NA) so the division stays in plain float32 — pd.NA
    # produces a nullable Float dtype that won't downcast.
    rev = df["net_revenue"].replace(0, np.nan)
    for cm in ("CM1", "CM2", "CM3"):
        if cm in df.columns:
            df[f"{cm}%"] = (df[cm].astype("float32")
                            / rev.astype("float32") * 100)
    return df


def to_iso_week(s: pd.Series) -> pd.Series:
    iso = s.dt.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)


@st.cache_data(show_spinner=False)
def period_options(periods: tuple[pd.Timestamp, ...], granularity: str) -> list[str]:
    """Memoize the unique period-bucket strings shown in the filter dropdowns.

    Keyed on the (immutable) tuple of unique snapshot dates so the cache
    invalidates only when the snapshot itself changes — not on every rerun."""
    s = pd.to_datetime(pd.Series(periods))
    if granularity == "Day":
        codes = s.dt.strftime("%Y-%m-%d")
    elif granularity == "Week":
        iso = s.dt.isocalendar()
        codes = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    elif granularity == "Month":
        codes = s.dt.strftime("%Y-%m")
    else:  # Quarter
        codes = s.dt.year.astype(str) + "-Q" + s.dt.quarter.astype(str)
    return sorted(codes.unique(), reverse=True)


@st.cache_data(show_spinner=False)
def trailing_30d_sku_sales(periods: tuple[pd.Timestamp, ...],
                            skus: tuple, revenues: tuple[float, ...]
                            ) -> pd.Series:
    """Aggregate trailing-30-day net revenue per SKU.

    Cached against the raw inputs so the eligibility check (Min monthly
    sales threshold) doesn't re-run a full groupby on every keystroke."""
    df = pd.DataFrame({"period": periods, "sku": skus, "rev": revenues})
    df["period"] = pd.to_datetime(df["period"])
    cutoff = df["period"].max() - pd.Timedelta(days=30)
    df = df[df["period"] > cutoff]
    return df.groupby("sku")["rev"].sum()


# ---------- ads CSV upload (browser → GitHub → workflow) ----------
GH_OWNER = "ennoenno98"
GH_REPO = "ShopApo"
GH_BRANCH = "claude/awesome-hamilton-X7w62"
GH_WORKFLOW = "weekly_export.yml"


def _gh_token() -> str | None:
    try:
        return st.secrets["GITHUB_TOKEN"]
    except Exception:
        return os.environ.get("GITHUB_TOKEN")


ADS_INPUT_DIR = REPO_ROOT / "inputs" / "shop_apotheke_ads"
MASTER_ADS_FILE = ADS_INPUT_DIR / "master_advertiserreport.csv"

CAMPAIGN_COUNTRY_PREFIXES = {
    "com_": "DE", "at_": "AT", "it_": "IT", "fr_": "FR",
    "nl_": "NL", "be_": "BE", "ch_": "CH", "es_": "ES",
}


def _campaign_country(campaign: str) -> str | None:
    n = str(campaign or "").strip().lower()
    if n.startswith("sold_out_"):
        n = n[len("sold_out_"):]
    for p, c in CAMPAIGN_COUNTRY_PREFIXES.items():
        if n.startswith(p):
            return c
    return None


def _list_ads_files() -> list[tuple[str, datetime, int]]:
    if not ADS_INPUT_DIR.exists():
        return []
    out = []
    for f in ADS_INPUT_DIR.glob("*.csv"):
        stat = f.stat()
        out.append((f.name, datetime.utcfromtimestamp(stat.st_mtime), stat.st_size))
    return sorted(out, key=lambda x: x[1], reverse=True)


def _ads_periods(path_or_bytes) -> set[tuple[str, str]]:
    """Return the set of (country, ISO-date) tuples present in an ads CSV."""
    try:
        df = pd.read_csv(path_or_bytes, sep=";", dtype=str,
                          usecols=["campaign", "date"])
    except Exception:
        return set()
    df.columns = [c.strip().lower() for c in df.columns]
    df["country"] = df["campaign"].map(_campaign_country)
    df["d"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["country", "d"])
    return set(zip(df["country"], df["d"].dt.strftime("%Y-%m-%d")))


def _merge_into_master(new_bytes: bytes) -> tuple[bytes, int, int]:
    """Drop existing master rows for any (country, date) that the new upload
    covers, then append new rows. Returns (merged_csv_bytes, replaced_periods,
    added_rows)."""
    import io
    new_df = pd.read_csv(io.BytesIO(new_bytes), sep=";", dtype=str)
    new_df.columns = [c.strip() for c in new_df.columns]
    # Use lowercased copies only for matching keys, keep originals for output.
    new_country = new_df["campaign"].map(_campaign_country)
    new_date = pd.to_datetime(new_df["date"], dayfirst=True, errors="coerce")
    overlap_keys = set(zip(new_country.dropna(),
                            new_date.dropna().dt.strftime("%Y-%m-%d")))

    if MASTER_ADS_FILE.exists():
        master_df = pd.read_csv(MASTER_ADS_FILE, sep=";", dtype=str)
        master_df.columns = [c.strip() for c in master_df.columns]
        m_country = master_df["campaign"].map(_campaign_country)
        m_date = pd.to_datetime(master_df["date"], dayfirst=True, errors="coerce")
        m_keys = list(zip(m_country, m_date.dt.strftime("%Y-%m-%d")))
        keep = [k not in overlap_keys for k in m_keys]
        master_df = master_df[keep]
    else:
        master_df = pd.DataFrame(columns=new_df.columns)

    merged = pd.concat([master_df, new_df], ignore_index=True, sort=False)
    buf = io.BytesIO()
    merged.to_csv(buf, sep=";", index=False)
    return buf.getvalue(), len(overlap_keys), len(new_df)


def render_upload_widget() -> None:
    token = _gh_token()
    with st.sidebar:
        with st.expander("📤 Ads CSV uploads"):
            files = _list_ads_files()
            if files:
                st.caption(f"**{len(files)} CSV(s) loaded** in `inputs/shop_apotheke_ads/`")
                rows = [
                    {
                        "file": name,
                        "uploaded": mtime.strftime("%Y-%m-%d %H:%M UTC"),
                        "size": f"{size / 1024:.1f} KB",
                    }
                    for name, mtime, size in files[:20]
                ]
                st.dataframe(pd.DataFrame(rows), hide_index=True,
                             use_container_width=True)
                if len(files) > 20:
                    st.caption(f"…and {len(files) - 20} more.")
            else:
                st.caption("No CSVs in `inputs/shop_apotheke_ads/` yet.")
            st.divider()

            if not token:
                st.caption(
                    "Upload disabled — set `GITHUB_TOKEN` in Streamlit secrets to enable."
                )
                return
            uploaded = st.file_uploader(
                "Drop a new sa-tech advertiser report here (one master CSV "
                "covering all marketplaces is enough — country comes from "
                "each row's campaign prefix)",
                type=["csv"],
                accept_multiple_files=True,
                key="ads_upload",
            )
            if not uploaded:
                return
            if not st.button("Upload & refresh dashboard", type="primary"):
                return

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            }
            master_path = "inputs/shop_apotheke_ads/master_advertiserreport.csv"
            # Each upload is merged sequentially into the master, so a second
            # file's (country, period) keys override the first's if they
            # overlap — same precedence as if uploaded one at a time.
            total_replaced = 0
            total_added = 0
            for f in uploaded:
                merged_bytes, replaced, added = _merge_into_master(f.getvalue())
                total_replaced += replaced
                total_added += added
                # Refresh local master so subsequent uploads see the latest.
                MASTER_ADS_FILE.parent.mkdir(parents=True, exist_ok=True)
                MASTER_ADS_FILE.write_bytes(merged_bytes)

            # Fetch SHA of existing master, then PUT the merged content.
            sha = None
            r = requests.get(
                f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{master_path}",
                headers=headers, params={"ref": GH_BRANCH}, timeout=30,
            )
            if r.ok:
                sha = r.json().get("sha")

            r = requests.put(
                f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{master_path}",
                headers=headers, timeout=60,
                json={
                    "message": (f"data: merge {len(uploaded)} upload(s) into "
                                f"master (replaced {total_replaced} period(s), "
                                f"added ≤{total_added} rows)"),
                    "content": base64.b64encode(MASTER_ADS_FILE.read_bytes()).decode(),
                    "branch": GH_BRANCH,
                    **({"sha": sha} if sha else {}),
                },
            )
            if not r.ok:
                st.error(f"Master update failed: {r.status_code} {r.text}")
                return

            dispatch = requests.post(
                (f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}"
                 f"/actions/workflows/{GH_WORKFLOW}/dispatches"),
                headers=headers, timeout=30,
                json={"ref": GH_BRANCH},
            )
            if dispatch.ok:
                st.success(
                    f"Merged {len(uploaded)} file(s) into master "
                    f"(replaced {total_replaced} pre-existing period(s)). "
                    f"Refresh the dashboard in ~3-5 minutes."
                )
            else:
                st.warning(
                    f"Merged, but couldn't trigger the rebuild "
                    f"({dispatch.status_code}). Run it manually at "
                    f"github.com/{GH_OWNER}/{GH_REPO}/actions"
                )


# ============================ APP ============================
require_login()
render_upload_widget()

path = latest_export()
if path is None:
    st.warning("No exports yet. Run `python weekly_export.py --once` to produce one.")
    # Calculator can still run without exports.
    show_calculator_only = True
    df = pd.DataFrame()
else:
    show_calculator_only = False
    df = load(path)

# ---------- title bar (Vanatari signature: Literata serif with orange accent word) ----------
st.markdown(
    """
    <div style='display:flex; align-items:baseline; gap:12px;
                margin: 4px 0 2px 0;'>
      <span style="font-family:'Literata',Georgia,serif;
                   font-size:2.3rem; font-weight:600;
                   color:#3c1826; letter-spacing:-0.01em; line-height:1;">
        Margin&nbsp;<span style="color:#ff5c3e;">Analytics.</span>
      </span>
    </div>
    """,
    unsafe_allow_html=True,
)
if not show_calculator_only:
    refreshed = datetime.utcfromtimestamp(path.stat().st_mtime)
    st.caption(
        f"Source: `{path.name}` · last refreshed "
        f"{refreshed:%Y-%m-%d %H:%M UTC} · "
        f"{len(df):,} rows · {df['sku'].nunique():,} SKUs"
    )

# ---------- top filter card ----------
def _bounds(code: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """First and last day of a period code at the selected granularity."""
    if granularity == "Day":
        d = pd.Timestamp(code)
        return d, d
    if granularity == "Week":
        yr, wk = code.split("-W")
        s = pd.Timestamp.fromisocalendar(int(yr), int(wk), 1)
        return s, s + pd.Timedelta(days=6)
    if granularity == "Month":
        s = pd.Timestamp(code + "-01")
        return s, (s + pd.offsets.MonthEnd(0)).normalize()
    # Quarter
    yr, q = code.split("-Q")
    s = pd.Timestamp(year=int(yr), month=(int(q) - 1) * 3 + 1, day=1)
    return s, (s + pd.offsets.QuarterEnd(startingMonth=3)).normalize()


if not show_calculator_only:
    with st.container(border=True):
        r1 = st.columns([1.6, 1.1, 2.2, 2.2, 1.2])

        with r1[0]:
            ALL = "🌍 All countries"
            country_options = [ALL] + sorted(df["country"].dropna().unique())
            country_sel = st.selectbox("Marketplace", country_options)

        with r1[1]:
            granularity = st.radio(
                "Granularity",
                ["Day", "Week", "Month", "Quarter"],
                index=1,
            )

        with r1[2]:
            unique_periods = tuple(df["period"].drop_duplicates().sort_values())
            options = period_options(unique_periods, granularity)
            label = {
                "Day": "Day(s)", "Week": "Calendar week(s)",
                "Month": "Month(s)", "Quarter": "Quarter(s)",
            }[granularity]
            fmt = {
                "Day": lambda x: x,
                "Week": lambda kw: f"KW {int(kw.split('-W')[1])} · {kw.split('-W')[0]}",
                "Month": lambda ym: pd.Timestamp(ym + "-01").strftime("%B %Y"),
                "Quarter": lambda q: f"Q{q.split('-Q')[1]} · {q.split('-Q')[0]}",
            }[granularity]
            # Default to the latest *complete* period — the newest one is
            # usually still running and would compare a few days to a full one.
            # The export runs mid-day, so its run date is itself partial.
            data_end = pd.Timestamp(df["period"].max())
            run_day = pd.to_datetime(path.name[len("shopapo_export_"):][:10],
                                     errors="coerce")
            last_full_day = data_end - pd.Timedelta(days=1) \
                if pd.notna(run_day) and data_end >= run_day else data_end
            complete = [o for o in options if _bounds(o)[1] <= last_full_day]
            period_selected = st.multiselect(
                label, options, default=(complete or options)[:1], format_func=fmt,
                help=f"Pick one or more {label.lower().rstrip('(s)')}. "
                     "Empty = trailing 28 days.",
            )

        with r1[3]:
            sku_query = st.text_input(
                "SKU or Product contains",
                placeholder="e.g. ibuprofen, 12345678",
            )

        with r1[4]:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            top_only = st.toggle("Top sellers only")

        r2 = st.columns([2, 6])
        with r2[0]:
            min_sales = st.number_input(
                "Min monthly sales (€, all countries)",
                min_value=0, value=2500 if top_only else 0, step=500,
                help=("SKU is included only if its trailing-30-day net "
                      "revenue across all countries is at least this amount."),
            )
        with r2[1]:
            sku_30d = trailing_30d_sku_sales(
                tuple(df["period"]),
                tuple(df["sku"]),
                tuple(df["net_revenue"].fillna(0)),
            )
            n_total = sku_30d.shape[0]
            n_clear = int((sku_30d >= max(min_sales, 1)).sum()) if min_sales > 0 else n_total
            highest = sku_30d.max() if not sku_30d.empty else 0
            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.caption(
                f"{n_clear:,} of {n_total:,} SKUs clear €{min_sales:,.0f}/mo "
                f"(all countries). Trailing 30 days; highest is €{highest:,.0f}."
            )

    # ---------- derive filtered dataframe ----------
    if period_selected:
        windows = [_bounds(code) for code in period_selected]
    else:
        end = pd.Timestamp(df["period"].max())
        windows = [(end - pd.Timedelta(days=28), end)]
    start = min(s for s, _ in windows)
    end = max(e for _, e in windows)

    eligible = set(sku_30d[sku_30d >= min_sales].index) if min_sales > 0 else None

    def _scope(wins: list[tuple[pd.Timestamp, pd.Timestamp]]) -> pd.DataFrame:
        """Rows inside `wins` with the marketplace / SKU / min-sales filters
        applied — so every comparison window is filtered identically."""
        mask = pd.Series(False, index=df.index)
        for s, e in wins:
            mask |= (df["period"] >= s) & (df["period"] <= e)
        out = df[mask]
        if country_sel != ALL:
            out = out[out["country"] == country_sel]
        if sku_query:
            q = sku_query.strip().lower()
            out = out[
                out["sku"].astype(str).str.lower().str.contains(q, na=False)
                | out["product_title"].astype(str).str.lower().str.contains(q, na=False)
            ]
        if eligible is not None:
            out = out[out["sku"].isin(eligible)]
        return out.copy()

    f = _scope(windows)

    # Prior equal-length window for KPI deltas.
    span_days = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    prev_end = pd.Timestamp(start) - pd.Timedelta(days=1)
    prev_start = prev_end - pd.Timedelta(days=span_days - 1)
    prev = _scope([(prev_start, prev_end)])

    # Ad data arrives via manual sa-tech uploads and can lag the order data.
    ad_rows = df[df["ad_spend"] > 0] if "ad_spend" in df.columns else df.iloc[0:0]
    last_ad_day = ad_rows["period"].max() if not ad_rows.empty else None
    if last_ad_day is None:
        st.warning("No ad spend data in this snapshot — CM3 equals CM2.", icon="⚠️")
    elif pd.Timestamp(end) > last_ad_day:
        st.warning(
            f"Ad spend data only runs to **{last_ad_day:%d %b %Y}** — days after "
            "that carry no marketing cost, so CM3 is overstated. Upload the newer "
            "sa-tech advertiser reports in the sidebar.",
            icon="⚠️",
        )


# ============================ KPIs ============================
if not show_calculator_only:
    by_sku_kpi = f.groupby("sku", as_index=False).agg(
        net_revenue=("net_revenue", "sum"),
        CM3=("CM3", "sum"),
    )
    by_sku_kpi["CM3%"] = (by_sku_kpi["CM3"]
                          / by_sku_kpi["net_revenue"].replace(0, pd.NA) * 100)

    skus_in_view = int(by_sku_kpi.shape[0])
    total_sales = float(f["net_revenue"].sum())
    pnl_impact = float(f["CM3"].sum())
    avg_cm3 = (pnl_impact / total_sales * 100) if total_sales else 0.0
    skus_below = int((by_sku_kpi["CM3%"] < 20).sum())

    c = st.columns(5)
    with c[0]:
        st.metric("SKUs in view", f"{skus_in_view:,}")
    with c[1]:
        st.metric("Total sales (€)", f"{total_sales:,.0f}")
    with c[2]:
        st.metric("P&L Impact (€)", f"{pnl_impact:,.0f}",
                  help="Sum of CM3 — margin remaining after ad spend.")
    with c[3]:
        st.metric("Avg CM3 %", f"{avg_cm3:.1f}")
    with c[4]:
        st.metric("SKUs below 20% CM3", f"{skus_below:,}")


# ============================ TABS ============================
tab_overview, tab_growth, tab_weekly = st.tabs(
    ["Overview", "SKU growth", "Weekly trend"])

# ---- Overview ----
with tab_overview:
    if show_calculator_only:
        st.info("Snapshot not available — use the Margin calculator tab.")
    else:
        # ---- Estimated P&L until CM3 (€ + % of net revenue) ----
        agg = f[["gross_revenue", "refunded", "net_revenue", "product_cost",
                 "commission", "shipping_cost_net", "three_pl_cost",
                 "CM1", "CM2", "ad_spend", "CM3"]].sum(numeric_only=True)
        nr = agg["net_revenue"] or 1  # avoid div-by-zero; absolutes still meaningful
        # Line-item labels follow the Vegavero Shopify P&L v3 naming so the
        # marketplace dashboard reads the same as the DTC export.
        pnl = pd.DataFrame([
            ("Gross Merchandise Value (GMV)",  agg["gross_revenue"], "+"),
            ("− Returns",                     -agg["refunded"],      ""),
            ("Net Revenue",                    agg["net_revenue"],   "="),
            ("− COGS",                        -agg["product_cost"],  ""),
            ("CM1",                            agg["CM1"],           "="),
            ("− Marketplace Commission (16%)", -agg["commission"],   ""),
            ("− Logistics Shipping Costs",    -agg["shipping_cost_net"], ""),
            ("− Logistics Picking Costs",     -agg["three_pl_cost"], ""),
            ("CM2",                            agg["CM2"],           "="),
            ("− Marketing Costs",             -agg["ad_spend"],      ""),
            ("CM3",                            agg["CM3"],           "="),
        ], columns=["Line", "€", "kind"])
        pnl["%"] = pnl["€"] / nr * 100

        subtotal_idx = set(pnl.index[pnl["kind"] == "="])

        def _row_style(row):
            if row.name in subtotal_idx:
                return ["background:#fbf7f2; font-weight:600"] * len(row)
            return [""] * len(row)

        st.markdown(f"**Estimated P&L until CM3** — window "
                    f"{pd.Timestamp(start).date()} → {pd.Timestamp(end).date()} · "
                    f"% of Net Revenue (€{nr:,.0f})")
        st.dataframe(
            pnl[["Line", "€", "%"]].style.format({
                "€": "€{:+,.0f}", "%": "{:+.1f}%",
            }).apply(_row_style, axis=1),
            use_container_width=True, hide_index=True,
        )

        # ---- Daily revenue × CM trend ----
        daily = f.groupby("period", as_index=False).agg(
            net_revenue=("net_revenue", "sum"),
            CM1=("CM1", "sum"), CM2=("CM2", "sum"), CM3=("CM3", "sum"),
            ad_spend=("ad_spend", "sum"),
        )
        # Vanatari palette: light-blue for the revenue base, Literata-plum /
        # warm-orange / secondary-blue for the three CM lines.
        fig = go.Figure()
        fig.add_bar(x=daily["period"], y=daily["net_revenue"], name="Net revenue",
                    marker_color=VNA_LIGHT_BLUE, opacity=0.9)
        for cm, color in (("CM1", VNA_ORANGE), ("CM2", VNA_PLUM),
                            ("CM3", VNA_SECONDARY_BLUE)):
            fig.add_scatter(x=daily["period"], y=daily[cm], name=cm,
                            mode="lines+markers", line=dict(color=color, width=2))
        fig.update_layout(height=360, hovermode="x unified",
                          legend=dict(orientation="h", y=-0.15),
                          margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

        # ---- Per-country breakdown ----
        st.markdown(
            "**Per-country breakdown** — Country CM3 % = total CM3 € / "
            "total Sales € for that marketplace."
        )
        by_c = f.groupby("country", as_index=False).agg(
            SKUs=("sku", "nunique"),
            Sales=("net_revenue", "sum"),
            Units=("units", "sum"),
            CM3=("CM3", "sum"),
            AdSpend=("ad_spend", "sum"),
        )
        by_c["CM3%"] = by_c["CM3"] / by_c["Sales"].replace(0, pd.NA) * 100
        by_c = by_c.sort_values("Sales", ascending=False)
        by_c = by_c.rename(columns={
            "country": "Marketplace",
            "Sales": "Sales (€)",
            "CM3%": "Country CM3 %",
            "CM3": "P&L Impact (€)",
            "AdSpend": "Ad spend (€)",
        })[["Marketplace", "SKUs", "Sales (€)", "Units",
            "Country CM3 %", "P&L Impact (€)", "Ad spend (€)"]]

        if not by_c.empty:
            total = pd.DataFrame([{
                "Marketplace": "Total",
                "SKUs": by_c["SKUs"].sum(),
                "Sales (€)": by_c["Sales (€)"].sum(),
                "Units": by_c["Units"].sum(),
                "Country CM3 %": (
                    by_c["P&L Impact (€)"].sum() / by_c["Sales (€)"].sum() * 100
                ) if by_c["Sales (€)"].sum() else pd.NA,
                "P&L Impact (€)": by_c["P&L Impact (€)"].sum(),
                "Ad spend (€)": by_c["Ad spend (€)"].sum(),
            }])
            display = pd.concat([by_c, total], ignore_index=True)

            def _cm3_color(v):
                if pd.isna(v):
                    return ""
                if v >= 25:
                    return "background:#C6EFCE; color:#0F5132"  # healthy green
                if v >= 20:
                    return "background:#E2F0D9; color:#1F5026"  # slight green
                if v >= 15:
                    return "background:#FFF2CC; color:#7F6000"  # amber
                return "background:#F8CBAD; color:#7F2D14"      # red

            st.dataframe(
                display.style.format({
                    "Sales (€)": "€{:,.0f}",
                    "P&L Impact (€)": "€{:,.0f}",
                    "Ad spend (€)": "€{:,.0f}",
                    "Country CM3 %": "{:.1f}%",
                    "SKUs": "{:,.0f}",
                    "Units": "{:,.0f}",
                }, na_rep="—").apply(
                    lambda row: ["font-weight:600; background:#fbf7f2"
                                 if row["Marketplace"] == "Total" else ""] * len(row),
                    axis=1,
                ).map(_cm3_color, subset=["Country CM3 %"]),
                use_container_width=True, hide_index=True,
            )
            chart_df = by_c.copy()
            country_fig = go.Figure()
            country_fig.add_bar(x=chart_df["Marketplace"], y=chart_df["Sales (€)"],
                                name="Sales (€)", marker_color=VNA_PLUM)
            country_fig.add_bar(x=chart_df["Marketplace"], y=chart_df["P&L Impact (€)"],
                                name="P&L Impact (€)", marker_color=VNA_ORANGE)
            country_fig.update_layout(barmode="group",
                yaxis_title="€", height=300,
                margin=dict(t=20, b=20, l=10, r=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02))
            st.plotly_chart(country_fig, use_container_width=True)

        # ---- Margin × Volume cluster matrix ----
        st.markdown(
            "**Margin × Volume clusters** — Tier 1 = top third, Tier 3 = bottom third. "
            "*Click a cell to filter the table below.*"
        )

        by_sku = f.groupby("sku", as_index=False).agg(
            product_title=("product_title", "first"),
            units=("units", "sum"),
            net_revenue=("net_revenue", "sum"),
            CM1=("CM1", "sum"), CM2=("CM2", "sum"), CM3=("CM3", "sum"),
            ad_spend=("ad_spend", "sum"),
        )
        by_sku["CM3%"] = by_sku["CM3"] / by_sku["net_revenue"].replace(0, pd.NA) * 100

        def _tiers(s: pd.Series) -> pd.Series:
            s = pd.to_numeric(s, errors="coerce")
            if s.notna().sum() < 3:
                return pd.Series(pd.NA, index=s.index, dtype="Int64")
            ranks = s.rank(method="first", ascending=False)
            try:
                return pd.qcut(ranks, q=3, labels=[1, 2, 3]).astype("Int64")
            except ValueError:
                return pd.Series(pd.NA, index=s.index, dtype="Int64")

        clean = by_sku.dropna(subset=["CM3%", "net_revenue"]).copy()
        clean["Margin Tier"] = _tiers(clean["CM3%"])
        clean["Volume Tier"] = _tiers(clean["net_revenue"])
        clean["Cluster Code"] = (clean["Margin Tier"].astype("string") + "-"
                                  + clean["Volume Tier"].astype("string"))
        cluster_lookup = clean.set_index("sku")[["Margin Tier", "Volume Tier", "Cluster Code"]]
        by_sku = by_sku.join(cluster_lookup, on="sku")

        grid = (clean.groupby(["Margin Tier", "Volume Tier"], observed=True)
                .size().unstack(fill_value=0)
                .reindex(index=[1, 2, 3], columns=[1, 2, 3], fill_value=0))

        if "active_cluster_code" not in st.session_state:
            st.session_state["active_cluster_code"] = None

        # Semantic bands stay green/red but the neutral bucket uses
        # Vanatari LIGHT_BLUE (#e3eef6) instead of the previous ad-hoc tint.
        cluster_bg = {
            "1-1": ("#C6EFCE", True),  "1-2": ("#E2F0D9", False), "1-3": ("#E2F0D9", False),
            "2-1": (VNA_LIGHT_BLUE, False), "2-2": ("#FFFFFF", False),
            "2-3": ("#FFFFFF", False),
            "3-1": (VNA_LIGHT_BLUE, False), "3-2": ("#FFFFFF", False),
            "3-3": ("#F8CBAD", False),
        }
        css_rules = []
        for code, (bg, bold) in cluster_bg.items():
            cls = f"st-key-cell_{code.replace('-', '_')}"
            weight = "font-weight:600;" if bold else ""
            css_rules.append(
                f".{cls} button {{ background:{bg} !important; color:#111 !important; "
                f"border:1px solid #d6d8dc !important; {weight} height:60px !important; "
                f"font-size:0.95rem !important; }}"
            )
        active_code = st.session_state["active_cluster_code"]
        if active_code:
            cls = f"st-key-cell_{active_code.replace('-', '_')}"
            css_rules.append(
                f".{cls} button {{ outline:3px solid {VNA_ORANGE} !important; outline-offset:-3px; }}"
            )
        st.markdown(f"<style>{''.join(css_rules)}</style>", unsafe_allow_html=True)

        sales_lbl = {1: "High sales", 2: "Mid sales", 3: "Low sales"}
        margin_lbl = {1: "High margin", 2: "Mid margin", 3: "Low margin"}
        header = st.columns([1.4, 2, 2, 2], gap="small")
        header[0].markdown("&nbsp;", unsafe_allow_html=True)
        for i, v in enumerate([1, 2, 3]):
            header[i + 1].markdown(
                f"<div style='text-align:center; font-weight:600; padding:6px 0;'>{sales_lbl[v]}</div>",
                unsafe_allow_html=True,
            )
        for m in [1, 2, 3]:
            row = st.columns([1.4, 2, 2, 2], gap="small")
            row[0].markdown(
                f"<div style='font-weight:600; padding:20px 0;'>{margin_lbl[m]}</div>",
                unsafe_allow_html=True,
            )
            for i, v in enumerate([1, 2, 3]):
                code = f"{m}-{v}"
                count = int(grid.loc[m, v])
                badge = " ⭐" if code == "1-1" else (" ⚠️" if code == "3-3" else "")
                with row[i + 1].container(key=f"cell_{m}_{v}"):
                    if st.button(f"{count} SKUs{badge}", key=f"btn_{m}_{v}",
                                 use_container_width=True):
                        st.session_state["active_cluster_code"] = (
                            None if active_code == code else code
                        )
                        st.rerun()

        if active_code:
            st.caption(f"Filtered to cluster **{active_code}** · "
                       f"click the same cell again to clear.")

        # ---- Master SKU table with deltas vs prior window ----
        # Δ CM3% vs equivalent prior window (same length, immediately before).
        prev_by_sku = prev.groupby("sku", as_index=False).agg(
            prev_net_revenue=("net_revenue", "sum"),
            prev_CM3=("CM3", "sum"),
        ) if not prev.empty else pd.DataFrame(
            columns=["sku", "prev_net_revenue", "prev_CM3"]
        )
        prev_by_sku["prev_CM3%"] = (
            prev_by_sku["prev_CM3"] / prev_by_sku["prev_net_revenue"].replace(0, pd.NA) * 100
        )
        by_sku = by_sku.merge(
            prev_by_sku[["sku", "prev_CM3%", "prev_net_revenue"]],
            on="sku", how="left",
        )
        by_sku["Δ CM3%"] = by_sku["CM3%"] - by_sku["prev_CM3%"]
        by_sku["Rev Δ %"] = (
            (by_sku["net_revenue"] - by_sku["prev_net_revenue"])
            / by_sku["prev_net_revenue"].replace(0, pd.NA) * 100
        )

        view = by_sku.copy()
        if active_code:
            view = view[view["Cluster Code"] == active_code]
        view = view.sort_values("net_revenue", ascending=False)

        cols = ["sku", "product_title", "Cluster Code", "units", "net_revenue",
                "CM2", "CM3", "CM3%", "Δ CM3%", "Rev Δ %", "ad_spend"]
        st.dataframe(
            view[cols].style.format({
                "units": "{:,.0f}",
                "net_revenue": "€{:,.0f}", "CM2": "€{:,.0f}", "CM3": "€{:,.0f}",
                "CM3%": "{:.1f}%", "Δ CM3%": "{:+.1f} pp", "Rev Δ %": "{:+.1f}%",
                "ad_spend": "€{:,.0f}",
            }, na_rep="—").apply(
                lambda row: [
                    f"background:{cluster_bg.get(row['Cluster Code'], ('#fff', False))[0]}"
                    if c == "Cluster Code" else ""
                    for c in cols
                ],
                axis=1,
            ),
            use_container_width=True, hide_index=True, height=520,
        )
        st.caption(
            f"{len(view)} SKUs · Δ CM3% in percentage points vs the previous "
            f"{span_days}-day window ({prev_start.date()} → {prev_end.date()})."
        )

# ---- SKU growth ----
GROWTH_STATUSES = ["▲ Growing", "🆕 New", "＝ Flat", "▼ Declining", "⛔ Lost"]
GROWTH_COLORS = {"▲ Growing": VNA_SECONDARY_BLUE, "🆕 New": VNA_SECONDARY_PINK,
                 "＝ Flat": VNA_PLUM_60, "▼ Declining": VNA_ORANGE,
                 "⛔ Lost": VNA_PLUM}

with tab_growth:
    if show_calculator_only:
        st.info("Snapshot not available.")
    else:
        g = st.columns(2)
        with g[0]:
            basis = st.radio("Compare against",
                             ["Previous period", "Same period last year"],
                             horizontal=True, key="growth_basis")
        with g[1]:
            rank_by = st.radio("Rank movers by", ["Net revenue", "CM3", "Units"],
                               horizontal=True, key="growth_rank")

        latest = max(range(len(windows)), key=lambda i: windows[i][1])
        if basis == "Previous period":
            selected_days = sum((e - s).days + 1 for s, e in windows)
            contiguous = selected_days == span_days
            if period_selected and granularity in ("Month", "Quarter"):
                # Calendar periods → the preceding calendar month(s)/quarter(s).
                step = pd.DateOffset(months=1 if granularity == "Month" else 3)
                if contiguous:
                    comp_windows = [(start - step * len(windows),
                                     start - pd.Timedelta(days=1))]
                    latest = 0
                else:
                    comp_windows = [(s - step, s - pd.Timedelta(days=1))
                                    for s, e in windows]
            elif contiguous:
                # One continuous stretch → the stretch right before it.
                comp_windows = [(prev_start, prev_end)]
                latest = 0
            else:
                # Non-adjacent picks (e.g. May + Sep) → each one vs the
                # equally long stretch right before it.
                comp_windows = [(s - (e - s) - pd.Timedelta(days=1),
                                 s - pd.Timedelta(days=1)) for s, e in windows]
        else:
            # 364 days keeps weekdays (and ISO weeks) aligned; calendar
            # months/quarters shift by a calendar year instead.
            if period_selected and granularity in ("Month", "Quarter"):
                shift = lambda d: d - pd.DateOffset(years=1)
            else:
                shift = lambda d: d - pd.Timedelta(days=364)
            comp_windows = [(shift(s), shift(e)) for s, e in windows]

        # A still-running period (e.g. this week) only has a few days of
        # data — compare it with the same number of days, not a full period.
        cur_end_shown = min(pd.Timestamp(end), data_end)
        gap = (pd.Timestamp(end) - data_end).days
        if gap > 0:
            s, e = comp_windows[latest]
            cur_s = windows[latest][0] if len(comp_windows) == len(windows) else start
            comp_windows[latest] = (s, min(e, s + (data_end - cur_s)))
        comp = _scope(comp_windows)

        comp_start = min(s for s, _ in comp_windows)
        comp_end = max(e for _, e in comp_windows)

        def _span_text(wins):
            return " + ".join(f"{s:%d %b %Y} → {e:%d %b %Y}" for s, e in sorted(wins))

        cur_shown = [(s, min(e, cur_end_shown)) for s, e in windows]
        st.caption(f"Current **{_span_text(cur_shown)}** vs "
                   f"**{_span_text(comp_windows)}** · "
                   "marketplace, SKU and min-sales filters apply to both."
                   + (" The current period is still running, so the comparison "
                      "is trimmed to the same days." if gap > 0 else ""))

        data_start = df["period"].min()
        if comp.empty:
            st.info(f"No sales in the comparison window — the snapshot starts "
                    f"{data_start:%d %b %Y}. Pick a later period or compare "
                    "against the previous period.")
        else:
            if comp_start < data_start:
                st.caption(f"⚠️ Comparison window starts before the snapshot "
                           f"({data_start:%d %b %Y}), so it is only partly covered.")

            agg_cols = [c for c in ("units", "net_revenue", "CM3", "ad_spend", "ad_revenue")
                        if c in df.columns]

            def _per_sku(frame: pd.DataFrame) -> pd.DataFrame:
                return frame.groupby("sku", observed=True)[agg_cols].sum()

            gr = _per_sku(f).join(_per_sku(comp), how="outer", rsuffix="_prev").fillna(0)
            titles = (pd.concat([f[["sku", "product_title"]], comp[["sku", "product_title"]]])
                      .astype(str).groupby("sku")["product_title"].first()
                      .replace("nan", ""))
            gr.index = gr.index.astype(str)
            gr = gr[(gr[["net_revenue", "net_revenue_prev", "ad_spend", "ad_spend_prev"]]
                     != 0).any(axis=1)]

            def _pct(num, den):
                return num / den.replace(0, np.nan) * 100

            gr["Δ revenue €"] = gr["net_revenue"] - gr["net_revenue_prev"]
            gr["Δ revenue %"] = _pct(gr["Δ revenue €"], gr["net_revenue_prev"])
            gr["Δ units"] = gr["units"] - gr["units_prev"]
            gr["Δ CM3 €"] = gr["CM3"] - gr["CM3_prev"]
            gr["CM3 %"] = _pct(gr["CM3"], gr["net_revenue"])
            gr["Δ CM3 pp"] = gr["CM3 %"] - _pct(gr["CM3_prev"], gr["net_revenue_prev"])
            gr["Δ ad spend €"] = gr["ad_spend"] - gr["ad_spend_prev"]
            gr["Δ ad spend %"] = _pct(gr["Δ ad spend €"], gr["ad_spend_prev"])
            gr["TACoS %"] = _pct(gr["ad_spend"], gr["net_revenue"])
            gr["Δ TACoS pp"] = gr["TACoS %"] - _pct(gr["ad_spend_prev"], gr["net_revenue_prev"])
            if "ad_revenue" in gr.columns:
                gr["ROAS"] = gr["ad_revenue"] / gr["ad_spend"].replace(0, np.nan)

            pct = gr["Δ revenue %"]
            gr["Status"] = np.select(
                [(gr["net_revenue_prev"] <= 0) & (gr["net_revenue"] > 0),
                 (gr["net_revenue"] <= 0) & (gr["net_revenue_prev"] > 0),
                 pct >= 5, pct <= -5],
                ["🆕 New", "⛔ Lost", "▲ Growing", "▼ Declining"],
                default="＝ Flat",
            )

            # ---- headline numbers ----
            rev_c, rev_p = gr["net_revenue"].sum(), gr["net_revenue_prev"].sum()
            cm3_c, cm3_p = gr["CM3"].sum(), gr["CM3_prev"].sum()
            ad_c, ad_p = gr["ad_spend"].sum(), gr["ad_spend_prev"].sum()
            counts = gr["Status"].value_counts()
            up = int(counts.get("▲ Growing", 0) + counts.get("🆕 New", 0))
            down = int(counts.get("▼ Declining", 0) + counts.get("⛔ Lost", 0))
            k = st.columns(4)
            k[0].metric("Net revenue", f"€{rev_c:,.0f}",
                        f"{(rev_c - rev_p) / rev_p * 100:+.1f}%" if rev_p else None)
            k[1].metric("CM3", f"€{cm3_c:,.0f}", f"{cm3_c - cm3_p:+,.0f} €")
            k[2].metric("SKUs growing / declining", f"{up} / {down}",
                        help="Growing and Declining = revenue moved by 5% or more; "
                             "New / Lost SKUs are counted in with them.")
            if rev_c and rev_p and (ad_c or ad_p):
                k[3].metric("TACoS (ad spend ÷ revenue)", f"{ad_c / rev_c * 100:.1f}%",
                            f"{(ad_c / rev_c - ad_p / rev_p) * 100:+.1f} pp",
                            delta_color="inverse")

            # ---- revenue bridge + top movers ----
            left, right = st.columns(2)
            with left:
                st.markdown("**Where the revenue change came from**")
                bridge = [("▲ Growing", "Growing"), ("🆕 New", "New"),
                          ("▼ Declining", "Declining"), ("⛔ Lost", "Lost"),
                          ("＝ Flat", "Flat")]
                steps = [float(gr.loc[gr["Status"] == s, "Δ revenue €"].sum())
                         for s, _ in bridge]
                wf = go.Figure(go.Waterfall(
                    x=["Comparison"] + [lbl for _, lbl in bridge] + ["Current"],
                    measure=["absolute"] + ["relative"] * len(bridge) + ["total"],
                    y=[rev_p] + steps + [0],
                    text=[f"€{v:,.0f}" for v in [rev_p] + steps + [rev_c]],
                    textposition="outside",
                    increasing={"marker": {"color": VNA_SECONDARY_BLUE}},
                    decreasing={"marker": {"color": VNA_ORANGE}},
                    totals={"marker": {"color": VNA_PLUM}},
                    connector={"line": {"color": VNA_PLUM_60, "width": 1}},
                ))
                wf.update_layout(height=380, showlegend=False, yaxis_title="€",
                                 margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(wf, use_container_width=True)

            with right:
                rank_col = {"Net revenue": "Δ revenue €", "CM3": "Δ CM3 €",
                            "Units": "Δ units"}[rank_by]
                st.markdown(f"**Top movers · {rank_col}**")
                movers = pd.concat([gr.nlargest(8, rank_col), gr.nsmallest(8, rank_col)])
                movers = movers[~movers.index.duplicated() & (movers[rank_col] != 0)]
                movers = movers.sort_values(rank_col)
                labels = [f"{s} · {str(titles.get(s, ''))[:28]}" for s in movers.index]
                mv = go.Figure(go.Bar(
                    x=movers[rank_col], y=labels, orientation="h",
                    marker_color=np.where(movers[rank_col] >= 0,
                                          VNA_SECONDARY_BLUE, VNA_ORANGE),
                    hovertemplate="%{y}<br>%{x:,.0f}<extra></extra>",
                ))
                mv.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10),
                                 xaxis_title=rank_col)
                st.plotly_chart(mv, use_container_width=True)

            # ---- did growth come from ads? ----
            st.markdown("**Did growth come from ads?** Each bubble is a SKU — "
                        "change in ad spend vs change in revenue, sized by current revenue.")
            sc = gr.reset_index(names="SKU")
            sc["Product"] = sc["SKU"].map(titles)
            sc["size"] = sc["net_revenue"].clip(lower=0) + 1
            fig = px.scatter(
                sc, x="Δ ad spend €", y="Δ revenue €", size="size", size_max=38,
                color="Status", color_discrete_map=GROWTH_COLORS,
                category_orders={"Status": GROWTH_STATUSES},
                hover_name="SKU",
                hover_data={"Product": True, "size": False, "TACoS %": ":.1f",
                            "Δ ad spend €": ":,.0f", "Δ revenue €": ":,.0f"},
            )
            fig.add_hline(y=0, line_color=VNA_PLUM_60, line_width=1)
            fig.add_vline(x=0, line_color=VNA_PLUM_60, line_width=1)
            for x, y, txt in [(0.02, 0.98, "Grew while cutting ads ✓"),
                              (0.98, 0.98, "Grew with more ads"),
                              (0.98, 0.02, "More ads, less revenue ✗"),
                              (0.02, 0.02, "Cut ads, lost revenue")]:
                fig.add_annotation(x=x, y=y, xref="paper", yref="paper", text=txt,
                                   showarrow=False, font=dict(color=VNA_PLUM_60, size=11),
                                   xanchor="left" if x < 0.5 else "right",
                                   yanchor="top" if y > 0.5 else "bottom")
            fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10),
                              legend=dict(orientation="h", y=-0.15, title=None))
            st.plotly_chart(fig, use_container_width=True)

            ads_note = []
            if "ad_spend_direct" in f.columns and f["ad_spend"].sum():
                direct = f["ad_spend_direct"].sum() / f["ad_spend"].sum() * 100
                ads_note.append(
                    f"{direct:.0f}% of this window's ad spend is matched to its SKU "
                    "via EAN; the rest (SOLD_OUT campaigns, unmatched EANs) is "
                    "split by revenue share.")
            else:
                ads_note.append(
                    "Ad spend is currently split across SKUs by revenue share — "
                    "per-SKU ad figures become exact after the next weekly run "
                    "(EAN matching).")
            if last_ad_day is None or comp_end > last_ad_day or end > last_ad_day:
                ads_note.append("Ad data doesn't fully cover both windows, so ad "
                                "deltas and TACoS are not like-for-like.")
            st.caption(" ".join(ads_note))

            # ---- SKU table ----
            status_sel = st.multiselect("Show SKUs", GROWTH_STATUSES,
                                        default=GROWTH_STATUSES, key="growth_status")
            view = (gr[gr["Status"].isin(status_sel)]
                    .sort_values(rank_col, ascending=False)
                    .reset_index(names="SKU"))
            view.insert(2, "Product", view["SKU"].map(titles))
            view = view.rename(columns={
                "units": "Units", "net_revenue": "Revenue €",
                "net_revenue_prev": "Prev revenue €", "CM3": "CM3 €",
                "ad_spend": "Ad spend €"})
            cols = ["SKU", "Status", "Product", "Units", "Δ units", "Revenue €",
                    "Prev revenue €", "Δ revenue €", "Δ revenue %", "CM3 €",
                    "Δ CM3 €", "CM3 %", "Δ CM3 pp", "Ad spend €", "Δ ad spend %",
                    "TACoS %", "Δ TACoS pp"] + (["ROAS"] if "ROAS" in view else [])
            num_cols = [c for c in cols if c not in ("SKU", "Status", "Product")]
            view[num_cols] = view[num_cols].astype("float64")  # None → NaN → "—"

            def _good_bad(v, invert=False):
                if pd.isna(v) or v == 0:
                    return ""
                good = (v > 0) != invert
                return f"color:{'#2E7D32' if good else '#C62828'}; font-weight:600"

            styled = (view[cols].style
                      .format({"Units": "{:,.0f}", "Δ units": "{:+,.0f}",
                               "Revenue €": "€{:,.0f}", "Prev revenue €": "€{:,.0f}",
                               "Δ revenue €": "€{:+,.0f}", "Δ revenue %": "{:+.1f}%",
                               "CM3 €": "€{:,.0f}", "Δ CM3 €": "€{:+,.0f}",
                               "CM3 %": "{:.1f}%", "Δ CM3 pp": "{:+.1f} pp",
                               "Ad spend €": "€{:,.0f}", "Δ ad spend %": "{:+.1f}%",
                               "TACoS %": "{:.1f}%", "Δ TACoS pp": "{:+.1f} pp",
                               "ROAS": "{:.2f}×"}, na_rep="—")
                      .map(_good_bad, subset=["Δ units", "Δ revenue €", "Δ revenue %",
                                              "Δ CM3 €", "Δ CM3 pp"])
                      .map(lambda v: _good_bad(v, invert=True), subset=["Δ TACoS pp"])
                      .map(lambda s: f"color:{GROWTH_COLORS.get(s, VNA_PLUM)}; "
                                     "font-weight:600", subset=["Status"]))
            st.dataframe(styled, use_container_width=True, hide_index=True, height=520)
            st.download_button(
                "⬇ Download growth table (CSV)",
                view[cols].to_csv(index=False).encode("utf-8"),
                file_name=f"shopapo_sku_growth_{start:%Y%m%d}_{end:%Y%m%d}.csv",
                mime="text/csv",
            )

# ---- Weekly trend ----
with tab_weekly:
    if show_calculator_only:
        st.info("Snapshot not available.")
    else:
        f2 = f.copy()
        f2["iso_week"] = to_iso_week(f2["period"])
        weekly = f2.groupby("iso_week", as_index=False).agg(
            net_revenue=("net_revenue", "sum"),
            CM1=("CM1", "sum"), CM2=("CM2", "sum"), CM3=("CM3", "sum"),
            ad_spend=("ad_spend", "sum"),
            orders=("orders", "sum"), units=("units", "sum"),
        )
        weekly["CM3%"] = weekly["CM3"] / weekly["net_revenue"].replace(0, pd.NA) * 100
        weekly["ROAS"] = weekly["net_revenue"] / weekly["ad_spend"].replace(0, pd.NA)

        fig = px.bar(weekly, x="iso_week", y=["CM1", "CM2", "CM3"], barmode="group",
                     color_discrete_map={"CM1": VNA_ORANGE, "CM2": VNA_PLUM,
                                        "CM3": VNA_SECONDARY_BLUE})
        fig.update_layout(height=360, xaxis_title="ISO week", yaxis_title="€",
                          legend=dict(orientation="h", y=-0.15),
                          margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            weekly.style.format({
                "net_revenue": "€{:,.0f}", "CM1": "€{:,.0f}", "CM2": "€{:,.0f}",
                "CM3": "€{:,.0f}", "ad_spend": "€{:,.0f}",
                "CM3%": "{:.1f}%", "ROAS": "{:.2f}×",
                "orders": "{:,.0f}", "units": "{:,.0f}",
            }),
            use_container_width=True, hide_index=True,
        )

