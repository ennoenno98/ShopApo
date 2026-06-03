"""Shop Apotheke weekly margin dashboard.

Reads the latest snapshot in `exports/` (produced by `weekly_export.py`)
and renders a password-gated multi-tab dashboard.

Margin model in `margin_model.py` mirrors the `Margen Calc pharma` sheet
from Margin_Check_V5.xlsx: 16% commission on gross, country-specific
VAT (pharma-reduced rates), DHL rate-card shipping cost with Nov+Dec
peak surcharge, 10% logistics overhead, GB COGS × 0.83.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from margin_model import (
    COMMISSION_RATE,
    OVERHEAD_RATE,
    PEAK_MONTHS,
    load_cogs,
    load_dhl,
    load_vat,
    quote,
)

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
              "commission", "shipping_cost_net", "product_cost",
              "dhl_cost", "overhead", "CM1", "CM2", "CM3", "ad_spend"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def add_pct(df: pd.DataFrame) -> pd.DataFrame:
    rev = df["net_revenue"].replace(0, pd.NA)
    for cm in ("CM1", "CM2", "CM3"):
        if cm in df.columns:
            df[f"{cm}%"] = df[cm] / rev * 100
    return df


def kpi(label: str, value: float, prev: float | None = None,
        money: bool = True, suffix: str = "") -> None:
    fmt = (lambda x: f"€{x:,.0f}{suffix}") if money else (lambda x: f"{x:,.0f}{suffix}")
    delta = None
    if prev is not None and prev:
        delta = f"{(value - prev) / abs(prev) * 100:+.1f}%"
    st.metric(label, fmt(value), delta=delta)


def to_iso_week(s: pd.Series) -> pd.Series:
    iso = s.dt.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)


# ============================ APP ============================
require_login()

path = latest_export()
if path is None:
    st.warning("No exports yet. Run `python weekly_export.py --once` to produce one.")
    # Calculator can still run without exports.
    show_calculator_only = True
    df = pd.DataFrame()
else:
    show_calculator_only = False
    df = add_pct(load(path))
    st.caption(f"Snapshot: **{path.name}** · {len(df):,} rows · "
               f"{df['period'].min():%Y-%m-%d} → {df['period'].max():%Y-%m-%d}")

# ---------- sidebar filters ----------
if not show_calculator_only:
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
        countries = st.multiselect("Country (empty = all)",
                                   sorted(df["country"].dropna().unique()))

    f = df[(df["period"] >= pd.Timestamp(start)) & (df["period"] <= pd.Timestamp(end))]
    if skus:
        f = f[f["sku"].isin(skus)]
    if countries:
        f = f[f["country"].isin(countries)]

    # Prior equal-length window for KPI deltas.
    span_days = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    prev_end = pd.Timestamp(start) - pd.Timedelta(days=1)
    prev_start = prev_end - pd.Timedelta(days=span_days - 1)
    prev = df[(df["period"] >= prev_start) & (df["period"] <= prev_end)]
    if skus:
        prev = prev[prev["sku"].isin(skus)]
    if countries:
        prev = prev[prev["country"].isin(countries)]


# ============================ KPIs ============================
if not show_calculator_only:
    totals = f[["net_revenue", "CM1", "CM2", "CM3", "ad_spend", "orders", "units"]]\
        .sum(numeric_only=True)
    ptotals = prev[["net_revenue", "CM1", "CM2", "CM3", "ad_spend"]]\
        .sum(numeric_only=True) if not prev.empty else None

    c = st.columns(6)
    with c[0]: kpi("Net revenue", totals["net_revenue"],
                   ptotals["net_revenue"] if ptotals is not None else None)
    with c[1]: kpi("CM1", totals["CM1"],
                   ptotals["CM1"] if ptotals is not None else None)
    with c[2]: kpi("CM2", totals["CM2"],
                   ptotals["CM2"] if ptotals is not None else None)
    with c[3]: kpi("CM3", totals["CM3"],
                   ptotals["CM3"] if ptotals is not None else None)
    with c[4]: kpi("Ad spend", totals["ad_spend"],
                   ptotals["ad_spend"] if ptotals is not None else None)
    with c[5]:
        roas = totals["net_revenue"] / totals["ad_spend"] if totals["ad_spend"] else 0
        st.metric("ROAS", f"{roas:,.2f}×")

    st.caption(f"vs. previous {span_days}-day window "
               f"({prev_start.date()} → {prev_end.date()})")


# ============================ TABS ============================
tab_overview, tab_weekly, tab_skus, tab_countries, tab_ads, tab_calc = st.tabs(
    ["Overview", "Weekly trend", "SKU detail", "Country", "Ad spend", "Margin calculator"]
)

# ---- Overview ----
with tab_overview:
    if show_calculator_only:
        st.info("Snapshot not available — use the Margin calculator tab.")
    else:
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

# ---- SKU detail ----
with tab_skus:
    if show_calculator_only:
        st.info("Snapshot not available.")
    else:
        by_sku = f.groupby(["sku"], as_index=False).agg(
            product_title=("product_title", "first"),
            units=("units", "sum"),
            net_revenue=("net_revenue", "sum"),
            product_cost=("product_cost", "sum"),
            commission=("commission", "sum"),
            shipping_cost_net=("shipping_cost_net", "sum"),
            overhead=("overhead", "sum"),
            ad_spend=("ad_spend", "sum"),
            CM1=("CM1", "sum"), CM2=("CM2", "sum"), CM3=("CM3", "sum"),
        )
        by_sku["CM3%"] = by_sku["CM3"] / by_sku["net_revenue"].replace(0, pd.NA) * 100
        by_sku = by_sku.sort_values("net_revenue", ascending=False)

        st.dataframe(
            by_sku.style.format({
                "units": "{:,.0f}",
                "net_revenue": "€{:,.0f}", "product_cost": "€{:,.0f}",
                "commission": "€{:,.0f}", "shipping_cost_net": "€{:,.0f}",
                "overhead": "€{:,.0f}", "ad_spend": "€{:,.0f}",
                "CM1": "€{:,.0f}", "CM2": "€{:,.0f}", "CM3": "€{:,.0f}",
                "CM3%": "{:.1f}%",
            }),
            use_container_width=True, hide_index=True, height=520,
        )

# ---- Country ----
with tab_countries:
    if show_calculator_only:
        st.info("Snapshot not available.")
    else:
        by_c = f.groupby("country", as_index=False).agg(
            orders=("orders", "sum"), units=("units", "sum"),
            net_revenue=("net_revenue", "sum"),
            CM1=("CM1", "sum"), CM2=("CM2", "sum"), CM3=("CM3", "sum"),
            ad_spend=("ad_spend", "sum"),
        )
        by_c["CM3%"] = by_c["CM3"] / by_c["net_revenue"].replace(0, pd.NA) * 100
        by_c = by_c.sort_values("net_revenue", ascending=False)
        st.dataframe(
            by_c.style.format({
                "orders": "{:,.0f}", "units": "{:,.0f}",
                "net_revenue": "€{:,.0f}", "CM1": "€{:,.0f}",
                "CM2": "€{:,.0f}", "CM3": "€{:,.0f}",
                "ad_spend": "€{:,.0f}", "CM3%": "{:.1f}%",
            }),
            use_container_width=True, hide_index=True,
        )
        fig = px.bar(by_c, x="country", y=["CM1", "CM2", "CM3"], barmode="group",
                     color_discrete_map={"CM1": "#1f77b4", "CM2": "#ff7f0e", "CM3": "#d62728"})
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                          legend=dict(orientation="h", y=-0.15))
        st.plotly_chart(fig, use_container_width=True)

# ---- Ad spend ----
with tab_ads:
    if show_calculator_only:
        st.info("Snapshot not available.")
    else:
        xlsx_path = Path(str(path).replace(".csv.gz", ".xlsx").replace(".csv", ".xlsx"))
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
                st.info(f"Per-channel ad detail unavailable ({e}).")
        else:
            st.info("Per-channel ad detail requires the .xlsx companion in exports/.")

# ---- Margin calculator (mirror of Margen Calc pharma) ----
with tab_calc:
    st.subheader("What-if margin — single SKU")
    cogs_df = load_cogs()
    vat = load_vat()
    dhl = load_dhl()
    countries = sorted(set(vat.keys()) & set(dhl["country"]))

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        skus_avail = sorted(cogs_df["sku"].dropna().unique())
        sku = st.selectbox("SKU", skus_avail,
                           index=skus_avail.index("BJ-E223-8EKW")
                           if "BJ-E223-8EKW" in skus_avail else 0)
    with c2:
        country = st.selectbox("Country", countries,
                               index=countries.index("DE") if "DE" in countries else 0)
    with c3:
        peak = st.checkbox("Peak (Nov+Dec)",
                           value=pd.Timestamp.now().month in PEAK_MONTHS)

    row = cogs_df[cogs_df["sku"] == sku].iloc[0]
    default_cogs = float(row["unit_cogs"])
    product_name = row.get("product_name", "")
    st.caption(product_name)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        gross_price = st.number_input("Gross price (€)", min_value=0.0,
                                      value=29.90, step=0.50)
    with c2:
        unit_cogs = st.number_input("Unit COGS (€)", min_value=0.0,
                                    value=default_cogs, step=0.10)
    with c3:
        discount = st.slider("Discount %", 0, 60, 0) / 100
    with c4:
        roas = st.number_input("Target ROAS (× net price)", min_value=0.0,
                               value=1.44, step=0.1,
                               help="Used to derive ad spend in CM3. "
                                    "Leave 0 to ignore.")

    q = quote(
        gross_price=gross_price,
        unit_cogs=unit_cogs,
        country=country,
        discount=discount,
        peak=peak,
        roas=roas if roas > 0 else None,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("CM1", f"€{q['CM1']:.2f}", f"{q['CM1%']:.1f}%")
    c2.metric("CM2", f"€{q['CM2']:.2f}", f"{q['CM2%']:.1f}%")
    c3.metric("CM3", f"€{q['CM3']:.2f}", f"{q['CM3%']:.1f}%")

    breakdown = pd.DataFrame([
        ("Gross price",         q["gross_price"]),
        ("Net price",           q["net_price"]),
        ("Sales price net",     q["sales_price_net"]),
        ("− Product cost",     -q["product_cost"]),
        ("= CM1",               q["CM1"]),
        (f"− Shipping (DHL, {'peak' if peak else 'std'})",
                                -q["shipping_cost_net"]),
        (f"− Commission ({COMMISSION_RATE:.0%} gross)",
                                -q["commission"]),
        (f"− Logistics overhead ({OVERHEAD_RATE:.0%})",
                                -q["overhead"]),
        ("= CM2",               q["CM2"]),
        ("− Ad spend (1/ROAS)", -q["ad_spend"]),
        ("= CM3",               q["CM3"]),
    ], columns=["Item", "€"])
    st.dataframe(
        breakdown.style.format({"€": "€{:,.2f}"}),
        use_container_width=True, hide_index=True,
    )

    with st.expander("Model assumptions"):
        st.write(f"""
        - **VAT (pharma-reduced rate):** {q['vat_rate']:.1%} for {country}
        - **Commission:** {COMMISSION_RATE:.0%} of gross (Shop Apotheke marketplace fee)
        - **Logistics overhead:** {OVERHEAD_RATE:.0%} of (net sales + net shipping cost)
        - **Shipping cost:** DHL rate card by country, +0.19 € peak surcharge in Nov+Dec
        - **GB COGS adjustment:** × 0.83 (EUR→GBP)
        - **Shipping revenue** charged to the customer accrues to Shop Apotheke, not the seller, so it is not added back into CM2.
        """)
