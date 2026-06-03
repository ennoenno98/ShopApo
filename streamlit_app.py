"""Shop Apotheke weekly margin dashboard.

Reads the latest snapshot in `exports/` (produced by `weekly_export.py`)
and renders a password-gated multi-tab dashboard.

Run locally:
    pip install -r requirements.txt
    python weekly_export.py --once          # populate exports/
    export DASHBOARD_PASSWORD="choose-something"
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
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


@st.cache_data(show_spinner=False)
def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["period"] = pd.to_datetime(df["period"], errors="coerce").dt.normalize()
    for c in ("orders", "units", "gross_revenue", "refunded", "net_revenue",
              "commission", "shipping_revenue", "cogs", "inbound_shipping",
              "outbound_shipping", "CM1", "CM2", "CM3", "ad_spend"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # Derive %s on the fly so they update when the user filters / adjusts.
    return df


def add_pct(df: pd.DataFrame) -> pd.DataFrame:
    rev = df["net_revenue"].replace(0, pd.NA)
    for cm in ("CM1", "CM2", "CM3"):
        if cm in df.columns:
            df[f"{cm}%"] = df[cm] / rev * 100
    return df


def kpi(label: str, value: float, prev: float | None = None, money: bool = True, suffix: str = "") -> None:
    fmt = (lambda x: f"€{x:,.0f}{suffix}") if money else (lambda x: f"{x:,.0f}{suffix}")
    delta = None
    if prev is not None and prev:
        delta = f"{(value - prev) / abs(prev) * 100:+.1f}%"
    st.metric(label, fmt(value), delta=delta)


# ---------- iso week helpers ----------
def to_iso_week(s: pd.Series) -> pd.Series:
    iso = s.dt.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)


# ============================ APP ============================
require_login()

path = latest_export()
if path is None:
    st.warning("No exports yet. Run `python weekly_export.py --once` to "
               "produce one.")
    st.stop()

df = add_pct(load(path))
st.caption(f"Snapshot: **{path.name}** · {len(df):,} rows · "
           f"{df['period'].min():%Y-%m-%d} → {df['period'].max():%Y-%m-%d}")

# ---------- sidebar filters ----------
with st.sidebar:
    st.header("Filter")
    max_date = df["period"].max().date()
    default_start = max_date - timedelta(days=28)
    period_range = st.date_input(
        "Period",
        value=(default_start, max_date),
        min_value=df["period"].min().date(),
        max_value=max_date,
    )
    if isinstance(period_range, tuple) and len(period_range) == 2:
        start, end = period_range
    else:
        start, end = default_start, max_date
    skus = st.multiselect("SKU (empty = all)", sorted(df["sku"].dropna().unique()))

f = df[(df["period"] >= pd.Timestamp(start)) & (df["period"] <= pd.Timestamp(end))]
if skus:
    f = f[f["sku"].isin(skus)]

# Comparison window: the equal-length window immediately preceding `start`.
span_days = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
prev_end = pd.Timestamp(start) - pd.Timedelta(days=1)
prev_start = prev_end - pd.Timedelta(days=span_days - 1)
prev = df[(df["period"] >= prev_start) & (df["period"] <= prev_end)]
if skus:
    prev = prev[prev["sku"].isin(skus)]

# ============================ KPIs ============================
totals = f[["net_revenue", "CM1", "CM2", "CM3", "ad_spend", "orders", "units"]].sum(numeric_only=True)
ptotals = prev[["net_revenue", "CM1", "CM2", "CM3", "ad_spend"]].sum(numeric_only=True) if not prev.empty else None

c = st.columns(6)
with c[0]: kpi("Net revenue", totals["net_revenue"], ptotals["net_revenue"] if ptotals is not None else None)
with c[1]: kpi("CM1", totals["CM1"], ptotals["CM1"] if ptotals is not None else None)
with c[2]: kpi("CM2", totals["CM2"], ptotals["CM2"] if ptotals is not None else None)
with c[3]: kpi("CM3", totals["CM3"], ptotals["CM3"] if ptotals is not None else None)
with c[4]: kpi("Ad spend", totals["ad_spend"], ptotals["ad_spend"] if ptotals is not None else None)
with c[5]:
    roas = totals["net_revenue"] / totals["ad_spend"] if totals["ad_spend"] else 0
    st.metric("ROAS", f"{roas:,.2f}×")

st.caption(f"vs. previous {span_days}-day window "
           f"({prev_start.date()} → {prev_end.date()})")

# ============================ TABS ============================
tab_overview, tab_weekly, tab_skus, tab_ads = st.tabs(
    ["Overview", "Weekly trend", "SKU detail", "Ad spend"]
)

# ---- Overview ----
with tab_overview:
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
    fig.update_layout(height=420, hovermode="x unified",
                      legend=dict(orientation="h", y=-0.15),
                      margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)

# ---- Weekly trend ----
with tab_weekly:
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

# ---- SKU detail ----
with tab_skus:
    by_sku = f.groupby(["sku"], as_index=False).agg(
        product_title=("product_title", "first"),
        units=("units", "sum"),
        net_revenue=("net_revenue", "sum"),
        cogs=("cogs", "sum"), commission=("commission", "sum"),
        outbound_shipping=("outbound_shipping", "sum"),
        ad_spend=("ad_spend", "sum"),
        CM1=("CM1", "sum"), CM2=("CM2", "sum"), CM3=("CM3", "sum"),
    )
    by_sku["CM3%"] = by_sku["CM3"] / by_sku["net_revenue"].replace(0, pd.NA) * 100
    by_sku = by_sku.sort_values("net_revenue", ascending=False)

    st.dataframe(
        by_sku.style.format({
            "units": "{:,.0f}",
            "net_revenue": "€{:,.0f}", "cogs": "€{:,.0f}",
            "commission": "€{:,.0f}", "outbound_shipping": "€{:,.0f}",
            "ad_spend": "€{:,.0f}",
            "CM1": "€{:,.0f}", "CM2": "€{:,.0f}", "CM3": "€{:,.0f}",
            "CM3%": "{:.1f}%",
        }),
        use_container_width=True, hide_index=True, height=520,
    )

# ---- Ad spend ----
with tab_ads:
    # The orchestrator writes the raw ad data as a separate xlsx tab; for the
    # dashboard, infer per-channel totals from the allocated ad_spend column
    # via the snapshot's xlsx if present, otherwise fall back to allocated.
    xlsx_path = path.with_suffix("").with_suffix(".xlsx")
    if xlsx_path.exists():
        try:
            ads_raw = pd.read_excel(xlsx_path, sheet_name="Ad spend")
            ads_raw["period"] = pd.to_datetime(ads_raw["period"]).dt.normalize()
            ads_raw = ads_raw[(ads_raw["period"] >= pd.Timestamp(start))
                              & (ads_raw["period"] <= pd.Timestamp(end))]
            by_channel = ads_raw.groupby("channel", as_index=False).agg(
                spend=("spend", "sum"),
                impressions=("impressions", "sum"),
                clicks=("clicks", "sum"),
                conversions=("conversions", "sum"),
                ad_revenue=("ad_revenue", "sum"),
            )
            by_channel["CPC"] = by_channel["spend"] / by_channel["clicks"].replace(0, pd.NA)
            by_channel["ROAS"] = by_channel["ad_revenue"] / by_channel["spend"].replace(0, pd.NA)
            st.dataframe(
                by_channel.style.format({
                    "spend": "€{:,.0f}", "ad_revenue": "€{:,.0f}",
                    "CPC": "€{:.2f}", "ROAS": "{:.2f}×",
                    "impressions": "{:,.0f}", "clicks": "{:,.0f}",
                    "conversions": "{:,.1f}",
                }),
                use_container_width=True, hide_index=True,
            )
            daily_ads = ads_raw.groupby(["period", "channel"], as_index=False)["spend"].sum()
            fig = px.area(daily_ads, x="period", y="spend", color="channel")
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10),
                              legend=dict(orientation="h", y=-0.15))
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.info(f"Per-channel ad detail unavailable in this snapshot ({e}).")
    else:
        st.info("Per-channel ad detail requires the .xlsx companion file in exports/.")
