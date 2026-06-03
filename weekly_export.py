"""Weekly Shop Apotheke margin snapshot.

Runs on a schedule (GitHub Actions, every Monday) or on demand:

    python weekly_export.py --once
    python weekly_export.py --start 2026-04-01 --end 2026-06-01

Pipeline:
  1. Pull orders from the Shop Apotheke (Mirakl) seller API
  2. Pull ad spend from Bing, TikTok, and the on-site CSV drop
  3. Read user-provided COGS + shipping from `inputs/`
  4. Compute contribution margin layers per (period, sku)
  5. Write `exports/shopapo_export_YYYY-MM-DD.csv.gz` and `.xlsx`

The dashboard (`streamlit_app.py`) always reads the most recent file in
`exports/`.

Margin definition
-----------------
  net_revenue      = gross_revenue - refunded_amount
  contribution_1   = net_revenue - cogs - inbound_shipping  (gross product margin)
  contribution_2   = contribution_1 - marketplace_commission
                                    - outbound_shipping_cost
                                    + shipping_revenue
  contribution_3   = contribution_2 - allocated_ad_spend     (final P&L margin)

Ad spend is allocated to SKUs in proportion to net revenue per day; if you
maintain a per-campaign → SKU map in `inputs/campaign_sku_map.csv` that
mapping is used first, with unmapped spend falling back to the revenue
allocation.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from connectors import ConnectorSkipped, shop_apotheke, shop_apotheke_ads, bing_ads, tiktok_ads


REPO_ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = REPO_ROOT / "exports"
INPUTS_DIR = REPO_ROOT / "inputs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------- user-provided reference data ----------
def load_cogs() -> pd.DataFrame:
    """`inputs/cogs.csv` → DataFrame with columns: sku, unit_cogs, inbound_shipping."""
    path = INPUTS_DIR / "cogs.csv"
    if not path.exists():
        log.warning("inputs/cogs.csv missing — CM1 will be NaN")
        return pd.DataFrame(columns=["sku", "unit_cogs", "inbound_shipping"])
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    for c in ("unit_cogs", "inbound_shipping"):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df[["sku", "unit_cogs", "inbound_shipping"]]


def load_outbound_shipping() -> pd.DataFrame:
    """`inputs/shipping.csv` → sku, unit_outbound_shipping (carrier cost we pay)."""
    path = INPUTS_DIR / "shipping.csv"
    if not path.exists():
        log.warning("inputs/shipping.csv missing — outbound shipping cost will be 0")
        return pd.DataFrame(columns=["sku", "unit_outbound_shipping"])
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "unit_outbound_shipping" not in df.columns:
        df["unit_outbound_shipping"] = 0.0
    df["unit_outbound_shipping"] = pd.to_numeric(df["unit_outbound_shipping"], errors="coerce").fillna(0)
    return df[["sku", "unit_outbound_shipping"]]


def load_campaign_map() -> pd.DataFrame:
    """`inputs/campaign_sku_map.csv` → channel, campaign, sku (optional)."""
    path = INPUTS_DIR / "campaign_sku_map.csv"
    if not path.exists():
        return pd.DataFrame(columns=["channel", "campaign", "sku"])
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    return df[["channel", "campaign", "sku"]].dropna()


# ---------- safe connector wrapper ----------
def _safe(name: str, fn, *args, **kwargs) -> pd.DataFrame:
    try:
        return fn(*args, **kwargs)
    except ConnectorSkipped as e:
        log.warning("Skipping %s: %s", name, e)
    except Exception:
        log.exception("%s failed", name)
    return pd.DataFrame()


# ---------- aggregation ----------
def aggregate_orders(orders: pd.DataFrame, cogs: pd.DataFrame, shipping: pd.DataFrame) -> pd.DataFrame:
    if orders.empty:
        return pd.DataFrame()
    df = orders.merge(cogs, on="sku", how="left")
    df = df.merge(shipping, on="sku", how="left")
    df["unit_cogs"] = df["unit_cogs"].fillna(0)
    df["inbound_shipping"] = df["inbound_shipping"].fillna(0)
    df["unit_outbound_shipping"] = df["unit_outbound_shipping"].fillna(0)

    df["net_revenue"] = df["gross_revenue"] - df["refunded_amount"]
    df["cogs_total"] = df["unit_cogs"] * df["qty"]
    df["inbound_total"] = df["inbound_shipping"] * df["qty"]
    df["outbound_total"] = df["unit_outbound_shipping"] * df["qty"]

    g = (
        df.groupby(["period", "sku"], as_index=False)
        .agg(
            product_title=("product_title", "first"),
            orders=("order_id", "nunique"),
            units=("qty", "sum"),
            gross_revenue=("gross_revenue", "sum"),
            refunded=("refunded_amount", "sum"),
            net_revenue=("net_revenue", "sum"),
            commission=("commission", "sum"),
            shipping_revenue=("shipping_revenue", "sum"),
            cogs=("cogs_total", "sum"),
            inbound_shipping=("inbound_total", "sum"),
            outbound_shipping=("outbound_total", "sum"),
        )
    )
    g["CM1"] = g["net_revenue"] - g["cogs"] - g["inbound_shipping"]
    g["CM2"] = g["CM1"] - g["commission"] - g["outbound_shipping"] + g["shipping_revenue"]
    return g


def allocate_ads(margin: pd.DataFrame, ads: pd.DataFrame, campaign_map: pd.DataFrame) -> pd.DataFrame:
    """Spread daily ad spend across SKUs.

    Step 1: if a (channel, campaign) row has a SKU in the map, the spend
    goes 1:1 to that SKU on that day.
    Step 2: unmapped daily spend is split across SKUs in proportion to net
    revenue for that day.
    """
    if ads.empty:
        margin["ad_spend"] = 0.0
        margin["CM3"] = margin["CM2"]
        return margin

    ads = ads.copy()
    ads["period"] = pd.to_datetime(ads["period"]).dt.normalize()

    # 1. Direct mapping
    direct = ads.merge(campaign_map, on=["channel", "campaign"], how="left")
    mapped = direct.dropna(subset=["sku"]).groupby(["period", "sku"], as_index=False)["spend"].sum()
    mapped = mapped.rename(columns={"spend": "ad_spend_mapped"})

    # 2. Pool of unmapped spend per day
    unmapped_daily = direct[direct["sku"].isna()].groupby("period", as_index=False)["spend"].sum()
    unmapped_daily = unmapped_daily.rename(columns={"spend": "ad_spend_pool"})

    out = margin.merge(mapped, on=["period", "sku"], how="left")
    out["ad_spend_mapped"] = out["ad_spend_mapped"].fillna(0)

    # Per-day net revenue share, used to split the unmapped pool
    daily_rev = out.groupby("period")["net_revenue"].transform("sum").replace(0, pd.NA)
    out["rev_share"] = (out["net_revenue"] / daily_rev).fillna(0)
    out = out.merge(unmapped_daily, on="period", how="left")
    out["ad_spend_pool"] = out["ad_spend_pool"].fillna(0)
    out["ad_spend_unmapped"] = out["ad_spend_pool"] * out["rev_share"]

    out["ad_spend"] = out["ad_spend_mapped"] + out["ad_spend_unmapped"]
    out["CM3"] = out["CM2"] - out["ad_spend"]
    return out.drop(columns=["ad_spend_mapped", "ad_spend_pool", "ad_spend_unmapped", "rev_share"])


# ---------- main ----------
def write_snapshot(df: pd.DataFrame, ads: pd.DataFrame, run_date: datetime) -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = run_date.strftime("%Y-%m-%d")
    raw = EXPORTS_DIR / f"shopapo_export_{date_str}.csv"
    gz = EXPORTS_DIR / f"shopapo_export_{date_str}.csv.gz"
    xlsx = EXPORTS_DIR / f"shopapo_export_{date_str}.xlsx"

    df.to_csv(raw, index=False)
    with open(raw, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    raw.unlink(missing_ok=True)

    # Workbook with margin + raw ad spend (one tab each) for quick auditing.
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Margin")
        if not ads.empty:
            ads.to_excel(w, index=False, sheet_name="Ad spend")

    log.info("Wrote %s (%d rows) + %s", gz.name, len(df), xlsx.name)
    return gz


def cleanup_old(days: int = 60) -> None:
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    for f in EXPORTS_DIR.glob("shopapo_export_*"):
        if pd.Timestamp(f.stat().st_mtime, unit="s") < cutoff:
            f.unlink()
            log.info("Deleted old export: %s", f.name)


def run(start: datetime, end: datetime, run_date: datetime) -> Path:
    log.info("=== Shop Apotheke weekly export: %s → %s ===",
             start.date(), end.date())

    orders = _safe("shop_apotheke", shop_apotheke.fetch, start, end)
    ads_frames = [
        _safe("bing_ads", bing_ads.fetch, start, end),
        _safe("tiktok_ads", tiktok_ads.fetch_chunked, start, end),
        _safe("shop_apotheke_ads", shop_apotheke_ads.fetch, start, end),
    ]
    ads = pd.concat([d for d in ads_frames if not d.empty], ignore_index=True) \
        if any(not d.empty for d in ads_frames) else pd.DataFrame()

    if orders.empty:
        log.error("No order data — aborting snapshot.")
        sys.exit(1)

    cogs = load_cogs()
    shipping = load_outbound_shipping()
    campaign_map = load_campaign_map()

    margin = aggregate_orders(orders, cogs, shipping)
    margin = allocate_ads(margin, ads, campaign_map)
    margin = margin.sort_values(["period", "net_revenue"], ascending=[False, False])

    path = write_snapshot(margin, ads, run_date)
    cleanup_old()
    return path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true",
                   help="Run a single export for the trailing 12 months and exit.")
    p.add_argument("--start", type=str, help="ISO date (default: 12 months ago)")
    p.add_argument("--end", type=str, help="ISO date (default: today)")
    args = p.parse_args()

    end = pd.to_datetime(args.end).to_pydatetime() if args.end else datetime.utcnow()
    start = (pd.to_datetime(args.start).to_pydatetime() if args.start
             else end - timedelta(days=365))

    run(start, end, run_date=datetime.utcnow())


if __name__ == "__main__":
    main()
