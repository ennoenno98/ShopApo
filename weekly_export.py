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

from connectors import (ConnectorSkipped, shop_apotheke, shop_apotheke_ads,
                        shop_apotheke_offers, bing_ads, tiktok_ads)
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
    margin: pd.DataFrame, ads: pd.DataFrame, campaign_map: pd.DataFrame,
    ean_map: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Put daily ad spend (and attributed ad revenue) on SKUs, by country.

    1. (channel, campaign) rows in `campaign_map` land 1:1 on that SKU.
    2. Remaining rows with an EAN in `ean_map` land on that EAN's SKU —
       except SOLD_OUT_ campaigns, which promote out-of-stock products and
       are deliberately treated as shared cost.
    3. Whatever is left for a (period, country) is split across that
       country's SKUs by net-revenue share.

    Directly attributed spend on a SKU-day without sales is kept as a
    zero-revenue row, so the cost still shows up in that SKU's CM3.
    Adds ad_spend, ad_spend_direct (the directly attributed part),
    ad_revenue and CM3.
    """
    if ads.empty:
        for c in ("ad_spend", "ad_spend_direct", "ad_revenue"):
            margin[c] = 0.0
        margin["CM3"] = margin["CM2"]
        return margin

    ads = ads.copy()
    ads["period"] = pd.to_datetime(ads["period"]).dt.normalize()
    ads["spend"] = pd.to_numeric(ads["spend"], errors="coerce").fillna(0)
    ads["ad_revenue"] = (pd.to_numeric(ads["ad_revenue"], errors="coerce").fillna(0)
                         if "ad_revenue" in ads.columns else 0.0)

    # Legacy ads frames may not carry a country column — fall back to DE
    # so the join shape stays consistent.
    if "country" not in ads.columns:
        ads["country"] = "DE"

    ads = ads.merge(campaign_map, on=["channel", "campaign"], how="left")
    if ean_map is not None and not ean_map.empty and "ean" in ads.columns:
        from connectors.shop_apotheke_offers import normalize_ean
        sold_out = ads["campaign"].astype(str).str.lower().str.startswith("sold_out_")
        by_ean = ads["ean"].map(normalize_ean).map(
            ean_map.set_index("ean")["sku"].to_dict())
        ads["sku"] = ads["sku"].fillna(by_ean.where(~sold_out))

    keys = ["period", "country", "sku"]
    direct = (ads.dropna(subset=["sku"]).groupby(keys, as_index=False)
              [["spend", "ad_revenue"]].sum()
              .rename(columns={"spend": "ad_spend_direct",
                               "ad_revenue": "ad_revenue_direct"}))
    pool = (ads[ads["sku"].isna()].groupby(["period", "country"], as_index=False)
            [["spend", "ad_revenue"]].sum()
            .rename(columns={"spend": "spend_pool", "ad_revenue": "ad_revenue_pool"}))

    out = margin.merge(direct, on=keys, how="outer", indicator=True)
    # Spend on SKU-days without any sale: keep as zero-revenue rows.
    orphan = out["_merge"] == "right_only"
    if orphan.any():
        titles = margin.groupby("sku")["product_title"].first()
        out.loc[orphan, "product_title"] = out.loc[orphan, "sku"].map(titles)
        num = [c for c in margin.columns
               if c not in keys + ["product_title"]
               and pd.api.types.is_numeric_dtype(margin[c])]
        out.loc[orphan, num] = 0
        log.info("Ads: %d SKU-day(s) had direct ad spend but no sales "
                 "(€%.0f) — kept as zero-revenue rows",
                 int(orphan.sum()), out.loc[orphan, "ad_spend_direct"].sum())
    out = out.drop(columns="_merge")
    out[["ad_spend_direct", "ad_revenue_direct"]] = (
        out[["ad_spend_direct", "ad_revenue_direct"]].fillna(0))

    daily_rev = (out.groupby(["period", "country"])["net_revenue"]
                 .transform("sum").replace(0, pd.NA))
    out["rev_share"] = (out["net_revenue"] / daily_rev).fillna(0)
    out = out.merge(pool, on=["period", "country"], how="left")
    out[["spend_pool", "ad_revenue_pool"]] = out[["spend_pool", "ad_revenue_pool"]].fillna(0)

    out["ad_spend"] = out["ad_spend_direct"] + out["spend_pool"] * out["rev_share"]
    out["ad_revenue"] = out["ad_revenue_direct"] + out["ad_revenue_pool"] * out["rev_share"]
    out["CM3"] = out["CM2"] - out["ad_spend"]

    total = ads["spend"].sum()
    if total:
        log.info("Ads: €%.0f total · %.0f%% attributed directly to SKUs, "
                 "rest split by revenue share",
                 total, out["ad_spend_direct"].sum() / total * 100)
    return out.drop(columns=["ad_revenue_direct", "spend_pool",
                             "ad_revenue_pool", "rev_share"])


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
    ean_map = shop_apotheke_offers.load_ean_map()

    line_margins = compute_line_margins(orders, cogs, vat, dhl)
    margin = aggregate(line_margins)
    margin = allocate_ads(margin, ads, campaign_map, ean_map)
    margin = margin.sort_values(["period", "net_revenue"], ascending=[False, False])

    path = write_snapshot(margin, ads, run_date)
    cleanup_old()
    return path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true",
                   help="Run a single export for the trailing ~25 months and exit.")
    p.add_argument("--start", type=str, help="ISO date (default: ~25 months ago, so every recent period has a year-ago comparison)")
    p.add_argument("--end", type=str, help="ISO date (default: today)")
    args = p.parse_args()

    end = pd.to_datetime(args.end).to_pydatetime() if args.end else datetime.utcnow()
    start = (pd.to_datetime(args.start).to_pydatetime() if args.start
             else end - timedelta(days=760))

    run(start, end, run_date=datetime.utcnow())


if __name__ == "__main__":
    main()
