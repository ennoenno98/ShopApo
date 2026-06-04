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
               "three_pl_cost", "CM1", "CM2", "CM3", "ad_spend")
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
    rev = df["net_revenue"].replace(0, pd.NA)
    for cm in ("CM1", "CM2", "CM3"):
        if cm in df.columns:
            df[f"{cm}%"] = (df[cm] / rev * 100).astype("float32")
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

# ---------- title bar ----------
st.markdown(
    "<div style='display:flex; align-items:center; gap:14px; "
    "margin: 4px 0 2px 0;'>"
    "<span style='font-size:2.4rem; line-height:1;'>📊</span>"
    "<span style='font-size:2rem; font-weight:700; color:#1A1A1A;'>"
    "Margin Analytics</span></div>",
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
            period_selected = st.multiselect(
                label, options, default=options[:1], format_func=fmt,
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
    def _bounds(code: str) -> tuple[pd.Timestamp, pd.Timestamp]:
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

    if period_selected:
        mask = pd.Series(False, index=df.index)
        starts, ends = [], []
        for code in period_selected:
            s, e = _bounds(code)
            mask |= (df["period"] >= s) & (df["period"] <= e)
            starts.append(s); ends.append(e)
        f = df[mask].copy()
        start, end = min(starts), max(ends)
    else:
        end = pd.Timestamp(df["period"].max())
        start = end - pd.Timedelta(days=28)
        f = df[(df["period"] >= start) & (df["period"] <= end)].copy()

    if country_sel != ALL:
        f = f[f["country"] == country_sel]

    if sku_query:
        q = sku_query.strip().lower()
        f = f[
            f["sku"].astype(str).str.lower().str.contains(q, na=False)
            | f["product_title"].astype(str).str.lower().str.contains(q, na=False)
        ]

    if min_sales > 0:
        eligible = set(sku_30d[sku_30d >= min_sales].index)
        f = f[f["sku"].isin(eligible)]

    # Prior equal-length window for KPI deltas.
    span_days = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    prev_end = pd.Timestamp(start) - pd.Timedelta(days=1)
    prev_start = prev_end - pd.Timedelta(days=span_days - 1)
    prev = df[(df["period"] >= prev_start) & (df["period"] <= prev_end)].copy()
    if country_sel != ALL:
        prev = prev[prev["country"] == country_sel]
    if sku_query:
        q = sku_query.strip().lower()
        prev = prev[
            prev["sku"].astype(str).str.lower().str.contains(q, na=False)
            | prev["product_title"].astype(str).str.lower().str.contains(q, na=False)
        ]
    if min_sales > 0:
        prev = prev[prev["sku"].isin(eligible)]


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
tab_overview, tab_weekly = st.tabs(["Overview", "Weekly trend"])

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
        pnl = pd.DataFrame([
            ("Gross revenue",        agg["gross_revenue"],     "+"),
            ("− Refunds",           -agg["refunded"],           ""),
            ("Net revenue",          agg["net_revenue"],       "="),
            ("− Product cost",      -agg["product_cost"],      ""),
            ("CM1",                  agg["CM1"],               "="),
            ("− Marketplace commission (16%)", -agg["commission"], ""),
            ("− Outbound shipping (net)",      -agg["shipping_cost_net"], ""),
            ("− 3PL fulfillment (Everstock)",  -agg["three_pl_cost"], ""),
            ("CM2",                  agg["CM2"],               "="),
            ("− Ad spend",          -agg["ad_spend"],           ""),
            ("CM3",                  agg["CM3"],               "="),
        ], columns=["Line", "€", "kind"])
        pnl["%"] = pnl["€"] / nr * 100

        subtotal_idx = set(pnl.index[pnl["kind"] == "="])

        def _row_style(row):
            if row.name in subtotal_idx:
                return ["background:#F2F4F8; font-weight:600"] * len(row)
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
        fig = go.Figure()
        fig.add_bar(x=daily["period"], y=daily["net_revenue"], name="Net revenue",
                    marker_color="#0a8754", opacity=0.4)
        for cm, color in (("CM1", "#1f77b4"), ("CM2", "#ff7f0e"), ("CM3", "#d62728")):
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
            st.dataframe(
                display.style.format({
                    "Sales (€)": "€{:,.0f}",
                    "P&L Impact (€)": "€{:,.0f}",
                    "Ad spend (€)": "€{:,.0f}",
                    "Country CM3 %": "{:.1f}%",
                    "SKUs": "{:,.0f}",
                    "Units": "{:,.0f}",
                }, na_rep="—").apply(
                    lambda row: ["font-weight:600; background:#F2F4F8"
                                 if row["Marketplace"] == "Total" else ""] * len(row),
                    axis=1,
                ),
                use_container_width=True, hide_index=True,
            )
            chart_df = by_c.copy()
            country_fig = go.Figure()
            country_fig.add_bar(x=chart_df["Marketplace"], y=chart_df["Sales (€)"],
                                name="Sales (€)", marker_color="#1f3864")
            country_fig.add_bar(x=chart_df["Marketplace"], y=chart_df["P&L Impact (€)"],
                                name="P&L Impact (€)", marker_color="#74AC2A")
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

        cluster_bg = {
            "1-1": ("#C6EFCE", True),  "1-2": ("#E2F0D9", False), "1-3": ("#E2F0D9", False),
            "2-1": ("#DEEBF7", False), "2-2": ("#FFFFFF", False), "2-3": ("#FFFFFF", False),
            "3-1": ("#DEEBF7", False), "3-2": ("#FFFFFF", False), "3-3": ("#F8CBAD", False),
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
                f".{cls} button {{ outline:3px solid #1f3864 !important; outline-offset:-3px; }}"
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
                     color_discrete_map={"CM1": "#1f77b4", "CM2": "#ff7f0e", "CM3": "#d62728"})
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

