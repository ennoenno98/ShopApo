"""Shop Apotheke on-site (Sponsored Products / Display) ads connector.

Shop Apotheke runs its on-site ads via the SA Retail Media platform at
https://retail.sa-tech.de — no public API. The weekly GitHub Action
runs `scripts/sa_tech_download.py` first (Playwright login + CSV
download) which drops the file in `inputs/shop_apotheke_ads/`; this
connector then reads everything in that folder. If the scraper is not
configured (no SA_TECH_EMAIL / SA_TECH_PASSWORD) you can still drop
the manual UI export in the same folder — the connector doesn't care
which produced it.

CSV format expected (any extra columns are ignored):
    date, campaign, spend, impressions, clicks, conversions, revenue
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import ConnectorSkipped

log = logging.getLogger(__name__)

INPUT_DIR = Path("inputs/shop_apotheke_ads")
EXPECTED = {"date", "campaign", "spend"}


def _load_all() -> pd.DataFrame:
    if not INPUT_DIR.exists():
        raise ConnectorSkipped(f"{INPUT_DIR} does not exist")
    frames = []
    for f in sorted(INPUT_DIR.glob("*.csv")):
        df = pd.read_csv(f)
        df.columns = [c.strip().lower() for c in df.columns]
        if not EXPECTED.issubset(df.columns):
            log.warning("Skipping %s — missing columns %s",
                        f.name, EXPECTED - set(df.columns))
            continue
        frames.append(df)
    if not frames:
        raise ConnectorSkipped("no usable CSVs in inputs/shop_apotheke_ads/")
    return pd.concat(frames, ignore_index=True)


def fetch(start: datetime, end: datetime) -> pd.DataFrame:
    raw = _load_all()
    raw["period"] = pd.to_datetime(raw["date"], errors="coerce").dt.normalize()
    mask = (raw["period"] >= pd.Timestamp(start).normalize()) & \
           (raw["period"] <= pd.Timestamp(end).normalize())
    raw = raw[mask]
    if raw.empty:
        return pd.DataFrame()

    df = pd.DataFrame({
        "period": raw["period"],
        "channel": "shop_apotheke_onsite",
        "campaign": raw["campaign"],
        "spend": pd.to_numeric(raw["spend"], errors="coerce"),
        "impressions": pd.to_numeric(raw.get("impressions"), errors="coerce"),
        "clicks": pd.to_numeric(raw.get("clicks"), errors="coerce"),
        "conversions": pd.to_numeric(raw.get("conversions"), errors="coerce"),
        "ad_revenue": pd.to_numeric(raw.get("revenue"), errors="coerce"),
    })
    log.info("Shop Apotheke on-site ads: %d rows, total spend €%.2f",
             len(df), df["spend"].sum())
    return df
