"""Shop Apotheke (Mirakl) offer catalogue → EAN-to-SKU map.

sa-tech advertiser reports identify products by EAN, while Mirakl orders
identify them by our shop SKU. OF21 (`GET /offers`) returns every active
offer with its shop SKU and the product references (EAN/GTIN) it is listed
under, which is exactly the bridge we need to put ad spend on the SKU that
actually earned it.

The result is cached in `inputs/ean_sku_map.csv` (committed by the weekly
workflow). If the API is unavailable the cached file is used as-is, and any
rows added to it by hand are preserved on refresh.

Uses the same env vars as the order connector (SHOP_APOTHEKE_API_KEY,
SHOP_APOTHEKE_SHOP_IDS, SHOP_APOTHEKE_BASE_URL).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from . import ConnectorSkipped
from .shop_apotheke import PAGE_SIZE, _client, _get_with_retry, _parse_shop_ids

log = logging.getLogger(__name__)

MAP_PATH = Path("inputs/ean_sku_map.csv")
EAN_REFERENCE_TYPES = {"EAN", "EAN13", "GTIN", "GTIN13", "EAN-13"}


def normalize_ean(value) -> str | None:
    """'4260557421123', '4260557421123.0', ' 04260557421123' → '4260557421123'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = s.lstrip("0")
    return s if s.isdigit() and len(s) >= 8 else None


def _offer_eans(offer: dict) -> list[str]:
    eans = [
        ref.get("reference")
        for ref in offer.get("product_references", []) or []
        if str(ref.get("reference_type", "")).upper() in EAN_REFERENCE_TYPES
    ]
    # Some Mirakl operators use the EAN itself as the catalogue product SKU.
    eans.append(offer.get("product_sku"))
    return [e for e in (normalize_ean(x) for x in eans) if e]


def _fetch_offers() -> pd.DataFrame:
    session, base = _client()
    shops = _parse_shop_ids(os.environ.get("SHOP_APOTHEKE_SHOP_IDS")) or [(None, None)]
    rows: list[dict] = []
    for country, shop_id in shops:
        offset = 0
        while True:
            params = {"max": PAGE_SIZE, "offset": offset}
            if shop_id:
                params["shop_id"] = shop_id
            r = _get_with_retry(session, f"{base}/offers", params)
            if not r.ok:
                log.error("Mirakl OF21 %s %s — body: %s",
                          r.status_code, r.reason, r.text[:500])
            r.raise_for_status()
            offers = r.json().get("offers", [])
            for o in offers:
                sku = o.get("shop_sku")
                if not sku:
                    continue
                for ean in _offer_eans(o):
                    rows.append({"ean": ean, "sku": sku,
                                 "product_title": o.get("product_title")})
            if len(offers) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        log.info("  · offers %s (%s): %d EAN rows so far", country, shop_id, len(rows))
    return pd.DataFrame(rows, columns=["ean", "sku", "product_title"])


def _read_cache() -> pd.DataFrame:
    if not MAP_PATH.exists():
        return pd.DataFrame(columns=["ean", "sku", "product_title"])
    df = pd.read_csv(MAP_PATH, dtype=str)
    df["ean"] = df["ean"].map(normalize_ean)
    return df.dropna(subset=["ean", "sku"])


def load_ean_map(refresh: bool = True) -> pd.DataFrame:
    """Return a one-row-per-EAN map (ean, sku, product_title).

    API rows win over cached rows for the same EAN; cached-only rows
    (including manual additions) are kept.
    """
    cached = _read_cache()
    fresh = pd.DataFrame(columns=cached.columns)
    if refresh:
        try:
            fresh = _fetch_offers()
        except ConnectorSkipped as e:
            log.warning("EAN map: %s — using cached %s", e, MAP_PATH)
        except Exception:
            log.exception("EAN map: OF21 fetch failed — using cached %s", MAP_PATH)

    merged = (pd.concat([fresh, cached], ignore_index=True)
              .drop_duplicates(subset="ean", keep="first")
              .sort_values("sku"))
    if not fresh.empty:
        MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(MAP_PATH, index=False)
        log.info("EAN map: %d EANs → %d SKUs (written to %s)",
                 len(merged), merged["sku"].nunique(), MAP_PATH)
    return merged
