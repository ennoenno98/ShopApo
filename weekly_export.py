"""Weekly Shop Apotheke margin snapshot.

Pipeline:
  1. Pull orders from the Shop Apotheke (Mirakl) seller API.
  2. Pull ad spend from Bing, TikTok, and the on-site CSV drop.
  3. Read COGS from `inputs/cogs.csv` and the country reference tables.
  4. Apply the ePharma margin model (`margin_model.py`) per order line.
  5. Aggregate to (period, sku) and allocate ad spend.
  6. Write `exports/shopapo_export_YYYY-MM-DD.csv.gz` + `.xlsx`.

Run on the schedule (GitHub Actions, every Monday) or on demand:

    python weekly_export.py --once
    python weekly_export.py --start 2026-04-01 --end 2026-06-01
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
from margin_model import (
    compute_line_margins,
    load_cogs,
    load_dhl,
    load_vat,
)


REPO_ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = REPO_ROOT / "exports"
INPUTS_DIR = REPO_ROOT / "inputs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def load_campaign_map() -> pd.DataFrame:
    path = INPUTS_DIR / "campaign_sku_map.csv"
    if not path.exists():
        return pd.DataFrame(columns=["channel", "campaign", "sku"])
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    return df[["channel", "campaign", "sku"]].dropna()


def _safe(name: str, fn, *args, **kwargs) -> pd.DataFrame:
    try:
        return fn(*args, **kwargs)
    except ConnectorSkipped as e:
        log.warning("Skipping %s: %s", name, e)
    except Exception:
        log.exception("%s failed", name)
    return pd.DataFrame()


def aggregate(line_margins: pd.DataFrame) -> pd.DataFrame:
    g = (
        line_margins.groupby(["period", "sku", "country"], as_index=False)
        .agg(
            product_title=("product_title", "first"),
            orders=("order_id", "nunique"),
            units=("qty", "sum"),
            gross_revenue=("gross_revenue", "sum"),
            refunded=("refunded_amount", "sum"),
            net_revenue=("net_revenue", "sum"),
            product_cost=("product_cost", "sum"),
            commission=("commission", "sum"),
            dhl_cost=("dhl_cost", "sum"),
            shipping_cost_net=("shipping_cost_net", "sum"),
            three_pl_cost=("three_pl_cost", "sum"),
            CM1=("CM1", "sum"),
            CM2=("CM2", "sum"),
        )
    )
    return g


def allocate_ads(
    margin: pd.DataFrame, ads: pd.DataFrame, campaign_map: pd.DataFrame
) -> pd.DataFrame:
    """Spread daily ad spend across SKUs.

    1. (channel, campaign) → SKU rows in the map land 1:1 on that day.
    2. Whatever spend is left over for that day is split across SKUs in
       proportion to their net revenue.
    """
    if ads.empty:
        margin["ad_spend"] = 0.0
        margin["CM3"] = margin["CM2"]
        return margin

    ads = ads.copy()
    ads["period"] = pd.to_datetime(ads["period"]).dt.normalize()

    direct = ads.merge(campaign_map, on=["channel", "campaign"], how="left")
    mapped = direct.dropna(subset=["sku"]).groupby(
        ["period", "sku"], as_index=False
    )["spend"].sum().rename(columns={"spend": "ad_spend_mapped"})

    unmapped_daily = direct[direct["sku"].isna()].groupby(
        "period", as_index=False
    )["spend"].sum().rename(columns={"spend": "ad_spend_pool"})

    out = margin.merge(mapped, on=["period", "sku"], how="left")
    out["ad_spend_mapped"] = out["ad_spend_mapped"].fillna(0)

    daily_rev = out.groupby("period")["net_revenue"].transform("sum").replace(0, pd.NA)
    out["rev_share"] = (out["net_revenue"] / daily_rev).fillna(0)
    out = out.merge(unmapped_daily, on="period", how="left")
    out["ad_spend_pool"] = out["ad_spend_pool"].fillna(0)
    out["ad_spend_unmapped"] = out["ad_spend_pool"] * out["rev_share"]

    out["ad_spend"] = out["ad_spend_mapped"] + out["ad_spend_unmapped"]
    out["CM3"] = out["CM2"] - out["ad_spend"]
    return out.drop(columns=["ad_spend_mapped", "ad_spend_pool", "ad_spend_unmapped", "rev_share"])


def write_snapshot(margin: pd.DataFrame, ads: pd.DataFrame, run_date: datetime) -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = run_date.strftime("%Y-%m-%d")
    raw = EXPORTS_DIR / f"shopapo_export_{date_str}.csv"
    gz = EXPORTS_DIR / f"shopapo_export_{date_str}.csv.gz"
    xlsx = EXPORTS_DIR / f"shopapo_export_{date_str}.xlsx"

    margin.to_csv(raw, index=False)
    with open(raw, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    raw.unlink(missing_ok=True)

    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        margin.to_excel(w, index=False, sheet_name="Margin")
        if not ads.empty:
            ads.to_excel(w, index=False, sheet_name="Ad spend")

    log.info("Wrote %s (%d rows) + %s", gz.name, len(margin), xlsx.name)
    return gz


def cleanup_old(days: int = 60) -> None:
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    for f in EXPORTS_DIR.glob("shopapo_export_*"):
        if pd.Timestamp(f.stat().st_mtime, unit="s") < cutoff:
            f.unlink()
            log.info("Deleted old export: %s", f.name)


def run(start: datetime, end: datetime, run_date: datetime) -> Path:
    log.info("=== Shop Apotheke weekly export: %s → %s ===", start.date(), end.date())

    orders = _safe("shop_apotheke", shop_apotheke.fetch, start, end)
    if orders.empty:
        log.error("No order data — aborting snapshot.")
        sys.exit(1)

    ads_frames = [
        _safe("bing_ads", bing_ads.fetch, start, end),
        _safe("tiktok_ads", tiktok_ads.fetch_chunked, start, end),
        _safe("shop_apotheke_ads", shop_apotheke_ads.fetch, start, end),
    ]
    ads = pd.concat([d for d in ads_frames if not d.empty], ignore_index=True) \
        if any(not d.empty for d in ads_frames) else pd.DataFrame()

    cogs = load_cogs()
    vat = load_vat()
    dhl = load_dhl()
    campaign_map = load_campaign_map()

    line_margins = compute_line_margins(orders, cogs, vat, dhl)
    margin = aggregate(line_margins)
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
