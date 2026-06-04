"""Shop Apotheke on-site (Sponsored Products / Display) ads connector.

Shop Apotheke runs its on-site ads via the SA Retail Media platform at
https://retail.sa-tech.de — no public API. CSVs are produced two ways:

1. Automated: `scripts/sa_tech_download.py` (Playwright login + click
   export). Files land in `inputs/shop_apotheke_ads/`.
2. Manual: Reports → Performance → Export, drop the file into the same
   folder by hand.

This connector reads everything in that folder and normalizes it to the
schema the orchestrator expects.

Native CSV format (matches the actual sa-tech export, June 2026):
    semicolon-separated, German decimals (0,31) and dates (2.6.2026)
    campaign;campaignId;date;Success Metric Type;Success Metric Value;
    conversions;unfilteredImpressions;clicks;ctr;cvr (clicks);
    budgetSpend;gmv;roas;productId;ean;cpc

We map:
    budgetSpend           → spend
    unfilteredImpressions → impressions
    clicks                → clicks
    conversions           → conversions
    gmv                   → ad_revenue
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import ConnectorSkipped

log = logging.getLogger(__name__)

INPUT_DIR = Path("inputs/shop_apotheke_ads")


# Column aliases — both the native sa-tech export and the legacy
# placeholder format we documented earlier are accepted.
COL_DATE = ("date",)
COL_CAMPAIGN = ("campaign",)
COL_SPEND = ("budgetspend", "spend")
COL_IMPRESSIONS = ("unfilteredimpressions", "impressions")
COL_CLICKS = ("clicks",)
COL_CONVERSIONS = ("conversions",)
COL_REVENUE = ("gmv", "revenue", "ad_revenue")
COL_EAN = ("ean",)


def _first_present(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _read_one(path: Path) -> pd.DataFrame | None:
    # sa-tech exports use `;` and German decimals; fall back to `,` for
    # any hand-curated CSV that uses standard formatting.
    for sep in (";", ","):
        try:
            df = pd.read_csv(path, sep=sep, dtype=str)
        except Exception as e:
            log.debug("Failed to read %s with sep=%r: %s", path.name, sep, e)
            continue
        if df.shape[1] > 1:
            df.columns = [c.strip().lower() for c in df.columns]
            return df
    log.warning("Could not parse %s with either ; or ,", path.name)
    return None


def _to_number(series: pd.Series) -> pd.Series:
    """German numbers ('0,31', '1.234,56') → float."""
    s = series.astype(str).str.strip()
    # If a value has both `.` and `,`, treat `.` as thousands and `,` as decimal.
    has_both = s.str.contains(r"\.", regex=True) & s.str.contains(",", regex=False)
    s = s.where(~has_both, s.str.replace(".", "", regex=False))
    s = s.str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


def _to_date(series: pd.Series) -> pd.Series:
    """Accept D.M.YYYY, YYYY-MM-DD, and DD/MM/YYYY in the same column."""
    return pd.to_datetime(series.astype(str).str.strip(), dayfirst=True,
                          errors="coerce").dt.normalize()


KNOWN_COUNTRIES = {"DE", "AT", "IT", "FR", "NL", "BE", "CH", "ES", "GB", "PL", "SE", "IE"}


def _country_from_filename(name: str) -> str:
    """Extract country tag from filenames like `AT_20260604_103000_xxx.csv`.

    Files uploaded via the dashboard get this prefix automatically. Files
    that pre-date the prefix (or were committed manually) default to DE,
    which is what the connector assumed before multi-country support."""
    head = name.split("_", 1)[0].upper()
    return head if head in KNOWN_COUNTRIES else "DE"


def _load_all() -> pd.DataFrame:
    if not INPUT_DIR.exists():
        raise ConnectorSkipped(f"{INPUT_DIR} does not exist")
    frames = []
    # Sort ascending by filename so dashboard-uploaded files (prefixed with
    # `YYYYMMDD_HHMMSS_`) end up last → their rows win during dedup below.
    for f in sorted(INPUT_DIR.glob("*.csv")):
        df = _read_one(f)
        if df is None or df.empty:
            continue
        if not all(_first_present(df, c) for c in (COL_DATE, COL_CAMPAIGN, COL_SPEND)):
            log.warning("Skipping %s — missing one of date/campaign/spend (cols: %s)",
                        f.name, list(df.columns)[:6])
            continue
        df["__country"] = _country_from_filename(f.name)
        frames.append(df)
    if not frames:
        raise ConnectorSkipped("no usable CSVs in inputs/shop_apotheke_ads/")
    return pd.concat(frames, ignore_index=True, sort=False)


def fetch(start: datetime, end: datetime) -> pd.DataFrame:
    raw = _load_all()
    date_col = _first_present(raw, COL_DATE)
    camp_col = _first_present(raw, COL_CAMPAIGN)
    spend_col = _first_present(raw, COL_SPEND)
    imp_col = _first_present(raw, COL_IMPRESSIONS)
    clk_col = _first_present(raw, COL_CLICKS)
    conv_col = _first_present(raw, COL_CONVERSIONS)
    rev_col = _first_present(raw, COL_REVENUE)
    ean_col = _first_present(raw, COL_EAN)

    out = pd.DataFrame({
        "period": _to_date(raw[date_col]),
        "channel": "shop_apotheke_onsite",
        "country": raw["__country"],
        "campaign": raw[camp_col].astype(str),
        "spend": _to_number(raw[spend_col]),
        "impressions": _to_number(raw[imp_col]) if imp_col else pd.NA,
        "clicks": _to_number(raw[clk_col]) if clk_col else pd.NA,
        "conversions": _to_number(raw[conv_col]) if conv_col else pd.NA,
        "ad_revenue": _to_number(raw[rev_col]) if rev_col else pd.NA,
        "ean": raw[ean_col].astype(str) if ean_col else pd.NA,
    })

    mask = (out["period"] >= pd.Timestamp(start).normalize()) & \
           (out["period"] <= pd.Timestamp(end).normalize())
    out = out[mask].copy()
    if out.empty:
        return pd.DataFrame()

    # Dedup overlapping rows across uploaded CSVs. Same (country, period,
    # campaign[, ean]) means the same underlying ad — keep the row from the
    # most recently uploaded file (last in concat order, see _load_all).
    dedup_keys = ["country", "period", "campaign"] + (["ean"] if ean_col else [])
    before = len(out)
    out = out.drop_duplicates(subset=dedup_keys, keep="last")
    if before != len(out):
        log.info("Shop Apotheke on-site ads: dropped %d duplicate row(s) "
                 "across overlapping CSVs", before - len(out))

    log.info("Shop Apotheke on-site ads: %d rows, %s → %s, total spend €%.2f",
             len(out), out["period"].min().date(), out["period"].max().date(),
             out["spend"].sum())
    return out
