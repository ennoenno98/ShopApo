"""Generate a synthetic snapshot so the dashboard is usable before the
first real Mirakl pull. Reproducible (fixed seed). Run from repo root:

    python scripts/seed_demo_snapshot.py

Synthesizes order lines for the last 60 days using the real cogs.csv as
the SKU master, routes them through the real margin model, allocates
synthetic ad spend, and writes exports/shopapo_export_DEMO.csv.gz +
.xlsx. Delete those files when the first real snapshot lands.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from margin_model import compute_line_margins, load_cogs, load_dhl, load_vat
from weekly_export import aggregate, allocate_ads, write_snapshot


RNG = np.random.default_rng(42)
COUNTRY_MIX = {"DE": 0.62, "AT": 0.12, "FR": 0.08, "IT": 0.07, "ES": 0.06, "NL": 0.05}


def synth_orders(days: int = 60) -> pd.DataFrame:
    cogs = load_cogs()
    skus = cogs["sku"].tolist()
    # Each SKU gets a stable retail price as markup over COGS.
    markups = dict(zip(cogs["sku"], RNG.uniform(3.5, 6.5, size=len(skus))))
    prices = {s: round(c * markups[s], 2)
              for s, c in zip(cogs["sku"], cogs["unit_cogs"])}
    titles = dict(zip(cogs["sku"], cogs["product_name"]))

    countries = list(COUNTRY_MIX.keys())
    weights = list(COUNTRY_MIX.values())
    end = datetime.utcnow().date()
    rows = []
    order_seq = 1
    for d in range(days):
        day = end - timedelta(days=days - 1 - d)
        n_orders = int(RNG.integers(15, 55))
        for _ in range(n_orders):
            order_id = f"DEMO-{order_seq:06d}"
            order_seq += 1
            country = RNG.choice(countries, p=weights)
            n_lines = int(RNG.integers(1, 4))
            chosen = RNG.choice(skus, size=n_lines, replace=False)
            for sku in chosen:
                qty = int(RNG.integers(1, 4))
                price = prices[sku]
                gross = round(price * qty, 2)
                # ~3% of lines get a partial refund
                refund = round(gross * float(RNG.uniform(0.2, 0.6)), 2) \
                    if RNG.random() < 0.03 else 0.0
                rows.append({
                    "period": pd.Timestamp(day),
                    "order_id": order_id,
                    "order_line_id": f"{order_id}-{sku}",
                    "sku": sku,
                    "product_title": titles.get(sku, ""),
                    "qty": qty,
                    "gross_revenue": gross,
                    "commission": 0.0,           # filled in by the model
                    "shipping_revenue": 0.0,
                    "refunded_amount": refund,
                    "customer_country": country,
                    "order_state": "SHIPPED",
                })
    return pd.DataFrame(rows)


def synth_ads(days: int = 60) -> pd.DataFrame:
    end = datetime.utcnow().date()
    rows = []
    for d in range(days):
        day = pd.Timestamp(end - timedelta(days=days - 1 - d))
        for channel, daily_mean, n_campaigns in [
            ("bing", 95.0, 3),
            ("tiktok", 220.0, 4),
            ("shop_apotheke_onsite", 140.0, 5),
        ]:
            for i in range(n_campaigns):
                spend = max(0.0, float(RNG.normal(daily_mean / n_campaigns,
                                                  daily_mean / n_campaigns / 3)))
                clicks = int(spend / max(0.4, float(RNG.uniform(0.3, 0.9))))
                impressions = clicks * int(RNG.integers(40, 120))
                conversions = float(clicks) * float(RNG.uniform(0.005, 0.025))
                ad_revenue = float(conversions) * float(RNG.uniform(35, 75))
                rows.append({
                    "period": day,
                    "channel": channel,
                    "campaign": f"{channel}-camp-{i+1}",
                    "spend": round(spend, 2),
                    "impressions": impressions,
                    "clicks": clicks,
                    "conversions": round(conversions, 1),
                    "ad_revenue": round(ad_revenue, 2),
                })
    return pd.DataFrame(rows)


def main() -> None:
    print("Synthesizing 60 days of orders + ad spend (seed=42)…")
    orders = synth_orders()
    ads = synth_ads()
    print(f"  {len(orders):,} order lines · {orders['order_id'].nunique():,} orders")
    print(f"  {len(ads):,} ad rows · €{ads['spend'].sum():,.0f} total spend")

    line_margins = compute_line_margins(
        orders, load_cogs(), load_vat(), load_dhl()
    )
    margin = aggregate(line_margins)
    margin = allocate_ads(margin, ads, pd.DataFrame(columns=["channel", "campaign", "sku"]))
    margin = margin.sort_values(["period", "net_revenue"], ascending=[False, False])

    # Stamp the run_date so the filename ends in -DEMO, easy to spot/delete.
    run_date = datetime.utcnow()
    path = write_snapshot(margin, ads, run_date)
    # Rename to make demo origin obvious.
    demo_path = path.with_name(path.name.replace(
        f"shopapo_export_{run_date:%Y-%m-%d}",
        f"shopapo_export_{run_date:%Y-%m-%d}-DEMO",
    ))
    xlsx = path.with_suffix("").with_suffix(".xlsx")
    demo_xlsx = demo_path.with_suffix("").with_suffix(".xlsx")
    path.rename(demo_path)
    if xlsx.exists():
        xlsx.rename(demo_xlsx)
    print(f"Wrote {demo_path.name} and {demo_xlsx.name}")
    print("Delete those two files when the first real run lands.")


if __name__ == "__main__":
    main()
