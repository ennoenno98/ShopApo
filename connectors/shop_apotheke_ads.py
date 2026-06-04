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

import hashlib
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


# sa-tech encodes the marketplace in the campaign-name prefix
# (com_… = shop-apotheke.com = DE, at_… = AT, it_… = IT, etc.).
# `SOLD_OUT_…` is a wrapper for OOS products and may sit in front of
# any country prefix — we strip it to find the country, but keep it on
# the campaign string so the allocator routes those rows to the
# revenue-share pool rather than to a specific SKU.
CAMPAIGN_COUNTRY_PREFIXES = {
    "com_": "DE",
    "at_":  "AT",
    "it_":  "IT",
    "fr_":  "FR",
    "nl_":  "NL",
    "be_":  "BE",
    "ch_":  "CH",
    "es_":  "ES",
}


def _campaign_country(campaign: str) -> str | None:
    name = str(campaign or "").strip().lower()
    if name.startswith("sold_out_"):
        name = name[len("sold_out_"):]
    for prefix, country in CAMPAIGN_COUNTRY_PREFIXES.items():
        if name.startswith(prefix):
            return country
    return None


def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _load_all() -> pd.DataFrame:
    if not INPUT_DIR.exists():
        raise ConnectorSkipped(f"{INPUT_DIR} does not exist")
    frames = []
    seen_hashes: dict[str, str] = {}
    # Sort ascending so older files are seen first; identical re-uploads
    # then fall under "skip" with a reference to the first copy.
    for f in sorted(INPUT_DIR.glob("*.csv")):
        h = _file_hash(f)
        if h in seen_hashes:
            log.info("Skipping %s — identical content to %s (re-upload)",
                     f.name, seen_hashes[h])
            continue
        seen_hashes[h] = f.name
        df = _read_one(f)
        if df is None or df.empty:
            continue
        if not all(_first_present(df, c) for c in (COL_DATE, COL_CAMPAIGN, COL_SPEND)):
            log.warning("Skipping %s — missing one of date/campaign/spend (cols: %s)",
                        f.name, list(df.columns)[:6])
            continue
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
        "campaign": raw[camp_col].astype(str),
        "spend": _to_number(raw[spend_col]),
        "impressions": _to_number(raw[imp_col]) if imp_col else pd.NA,
        "clicks": _to_number(raw[clk_col]) if clk_col else pd.NA,
        "conversions": _to_number(raw[conv_col]) if conv_col else pd.NA,
        "ad_revenue": _to_number(raw[rev_col]) if rev_col else pd.NA,
        "ean": raw[ean_col].astype(str) if ean_col else pd.NA,
    })
    out["country"] = out["campaign"].map(_campaign_country)
    unmapped = out["country"].isna().sum()
    if unmapped:
        log.warning("Shop Apotheke on-site ads: %d row(s) had unrecognized "
                    "campaign prefix and were dropped", unmapped)
        out = out.dropna(subset=["country"]).copy()

    mask = (out["period"] >= pd.Timestamp(start).normalize()) & \
           (out["period"] <= pd.Timestamp(end).normalize())
    out = out[mask].copy()
    if out.empty:
        return pd.DataFrame()

    # Aggregate. sa-tech sometimes emits multiple legitimately-distinct rows
    # for the same (country, date, campaign, ean) — different daypart/
    # placement buckets that report separately. Summing here is what gives
    # the correct daily spend. (Identical re-uploaded files were already
    # discarded at load time via _file_hash, so we are not double-counting.)
    group_keys = ["country", "period", "campaign"] + (["ean"] if ean_col else [])
    metric_cols = [c for c in ("spend", "impressions", "clicks",
                               "conversions", "ad_revenue")
                   if c in out.columns]
    before = len(out)
    out = out.groupby(group_keys, as_index=False)[metric_cols].sum(min_count=1)
    out["channel"] = "shop_apotheke_onsite"
    if before != len(out):
        log.info("Shop Apotheke on-site ads: collapsed %d → %d rows by "
                 "summing same-key entries", before, len(out))

    log.info("Shop Apotheke on-site ads: %d rows, %s → %s, total spend €%.2f",
             len(out), out["period"].min().date(), out["period"].max().date(),
             out["spend"].sum())
    return out
