"""TikTok Ads connector (Marketing API v1.3).

Pulls daily campaign-level spend and performance for [start, end].

Auth uses a long-lived access token issued in TikTok Business Center →
Marketing API → For Developers (no refresh-token flow).

Env vars
--------
TIKTOK_ACCESS_TOKEN     Long-lived access token
TIKTOK_ADVERTISER_ID    Advertiser account id (the one that owns the campaigns)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import requests

from . import ConnectorSkipped

log = logging.getLogger(__name__)

API = "https://business-api.tiktok.com/open_api/v1.3"


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise ConnectorSkipped(f"{name} not set")
    return v


def fetch(start: datetime, end: datetime) -> pd.DataFrame:
    token = _required("TIKTOK_ACCESS_TOKEN")
    advertiser_id = _required("TIKTOK_ADVERTISER_ID")

    headers = {"Access-Token": token}
    rows: list[dict] = []
    page = 1
    while True:
        params = {
            "advertiser_id": advertiser_id,
            "report_type": "BASIC",
            "data_level": "AUC_CAMPAIGN",
            "dimensions": '["campaign_id","stat_time_day"]',
            "metrics": '["spend","impressions","clicks","conversion","complete_payment_roas"]',
            # TikTok caps the window to ~30 days per request; chunk if longer.
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
            "page": page,
            "page_size": 200,
        }
        r = requests.get(f"{API}/report/integrated/get/",
                         params=params, headers=headers, timeout=120)
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"TikTok API error: {payload.get('message')}")
        data = payload.get("data", {})
        for item in data.get("list", []):
            dim = item.get("dimensions", {})
            met = item.get("metrics", {})
            rows.append({
                "period": pd.to_datetime(dim.get("stat_time_day"), errors="coerce").normalize(),
                "channel": "tiktok",
                "campaign": dim.get("campaign_id"),
                "spend": float(met.get("spend") or 0),
                "impressions": float(met.get("impressions") or 0),
                "clicks": float(met.get("clicks") or 0),
                "conversions": float(met.get("conversion") or 0),
                "ad_revenue": float(met.get("complete_payment_roas") or 0)
                    * float(met.get("spend") or 0),
            })
        page_info = data.get("page_info", {})
        if page >= page_info.get("total_page", 1):
            break
        page += 1

    df = pd.DataFrame(rows)
    if df.empty:
        log.info("TikTok: no rows in window %s → %s", start, end)
        return df
    log.info("TikTok Ads: %d daily rows, total spend €%.2f", len(df), df["spend"].sum())
    return df


def fetch_chunked(start: datetime, end: datetime, chunk_days: int = 30) -> pd.DataFrame:
    """Wrapper that splits long ranges into ≤30-day chunks (TikTok API limit)."""
    out = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        out.append(fetch(cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return pd.concat([d for d in out if not d.empty], ignore_index=True) if out else pd.DataFrame()
