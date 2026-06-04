"""Shop Apotheke marketplace (Mirakl) order connector.

Shop Apotheke runs its seller marketplace on Mirakl. Sellers authenticate
with a static API key issued in the seller portal:
    Settings → User → API key  →  send as `Authorization: <key>` header.

API base:    https://shopapotheke.mirakl.net/api
Docs (general Mirakl):  https://help.mirakl.net/api/

The endpoint we use is OR11 (`GET /orders`), which returns shipped + paid
orders within a date window, with order lines containing SKU, quantity,
gross price, commissions, and shipping charged to the customer. Output is
one row per order line.

Env vars
--------
SHOP_APOTHEKE_API_KEY   Mirakl seller API key (required)
SHOP_APOTHEKE_BASE_URL  Override base URL (defaults to production)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterator

import pandas as pd
import requests

from . import ConnectorSkipped

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://shopapotheke.mirakl.net/api"
PAGE_SIZE = 100

# Mirakl OR11 returns country names (sometimes full English, sometimes the
# native language) on `customer.shipping_address.country`. Normalize to the
# 2-letter ISO codes the margin model + reference CSVs use.
COUNTRY_TO_ISO = {
    "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "AUSTRIA": "AT", "ÖSTERREICH": "AT", "OESTERREICH": "AT",
    "FRANCE": "FR", "FRANKREICH": "FR",
    "ITALY": "IT", "ITALIA": "IT", "ITALIEN": "IT",
    "SPAIN": "ES", "ESPAÑA": "ES", "ESPANA": "ES", "SPANIEN": "ES",
    "NETHERLANDS": "NL", "NIEDERLANDE": "NL",
    "BELGIUM": "BE", "BELGIEN": "BE", "BELGIQUE": "BE",
    "POLAND": "PL", "POLEN": "PL", "POLSKA": "PL",
    "SWITZERLAND": "CH", "SCHWEIZ": "CH", "SUISSE": "CH",
    "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB",
    "IRELAND": "IE", "IRLAND": "IE",
    "SWEDEN": "SE", "SCHWEDEN": "SE",
}


def _normalize_country(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().upper()
    if len(v) == 2:
        return v
    return COUNTRY_TO_ISO.get(v, v)


def _client():
    key = os.environ.get("SHOP_APOTHEKE_API_KEY")
    if not key:
        raise ConnectorSkipped("SHOP_APOTHEKE_API_KEY not set")
    base = os.environ.get("SHOP_APOTHEKE_BASE_URL", DEFAULT_BASE).rstrip("/")
    s = requests.Session()
    s.headers.update({"Authorization": key, "Accept": "application/json"})
    return s, base


def _get_with_retry(session: requests.Session, url: str, params: dict,
                    max_attempts: int = 5) -> requests.Response:
    """GET that retries on 429 (Mirakl rate limit) and 5xx with backoff.

    Respects the `Retry-After` header when present; otherwise uses
    exponential backoff (5s, 10s, 20s, 40s).
    """
    import time
    delay = 5
    for attempt in range(1, max_attempts + 1):
        r = session.get(url, params=params, timeout=120)
        if r.status_code != 429 and r.status_code < 500:
            return r
        if attempt == max_attempts:
            return r  # let the caller raise_for_status
        wait = int(r.headers.get("Retry-After") or delay)
        log.warning("Mirakl %s (attempt %d/%d) — sleeping %ds",
                    r.status_code, attempt, max_attempts, wait)
        time.sleep(wait)
        delay = min(delay * 2, 60)
    return r


def _iter_orders(session: requests.Session, base: str, start: datetime,
                 end: datetime, shop_id: str | None = None) -> Iterator[dict]:
    """Stream orders from Mirakl OR11 for one shop, paginating until exhausted."""
    offset = 0
    while True:
        params = {
            "start_date": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "max": PAGE_SIZE,
            "offset": offset,
            # Only count revenue once the customer has actually been charged.
            "order_state_codes": ",".join([
                "SHIPPING", "SHIPPED", "TO_COLLECT", "RECEIVED",
                "CLOSED", "REFUSED", "REFUNDED",
            ]),
        }
        if shop_id:
            params["shop_id"] = shop_id
        r = _get_with_retry(session, f"{base}/orders", params)
        if not r.ok:
            # Mirakl returns a JSON body on auth/permission errors that
            # explains *why* (invalid key vs. unauthorized endpoint vs.
            # expired). Surface it before raising so the workflow log
            # tells us what's wrong, not just the status code.
            log.error("Mirakl %s %s — body: %s",
                      r.status_code, r.reason, r.text[:500])
        r.raise_for_status()
        payload = r.json()
        orders = payload.get("orders", [])
        if not orders:
            return
        yield from orders
        if len(orders) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def _parse_shop_ids(raw: str | None) -> list[tuple[str, str]]:
    """Parse 'DE:3310,AT:3401,IT:3543' into [('DE','3310'), ('AT','3401'), ...].

    Returns an empty list when the env var is unset or blank, in which case
    the caller falls back to a single un-filtered query (one default shop)."""
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" not in piece:
            log.warning("SHOP_APOTHEKE_SHOP_IDS: ignoring malformed entry %r "
                        "(expected COUNTRY:ID)", piece)
            continue
        country, shop_id = piece.split(":", 1)
        out.append((country.strip().upper(), shop_id.strip()))
    return out


def fetch(start: datetime, end: datetime) -> pd.DataFrame:
    """Return one row per order line in [start, end].

    When `SHOP_APOTHEKE_SHOP_IDS=DE:3310,AT:3401,IT:3543` is set, queries
    each shop separately and tags rows with the configured country code
    (overriding the customer shipping country, which we want for marketplace-
    level VAT / shipping / COGS calculations).

    Columns: period, order_id, order_line_id, sku, product_title,
    qty, gross_revenue, commission, shipping_revenue, customer_country,
    order_state, refunded_amount.
    """
    session, base = _client()
    shops = _parse_shop_ids(os.environ.get("SHOP_APOTHEKE_SHOP_IDS"))
    if shops:
        log.info("Shop Apotheke: querying %d marketplace(s): %s",
                 len(shops), ", ".join(f"{c}({sid})" for c, sid in shops))
    else:
        log.info("Shop Apotheke: SHOP_APOTHEKE_SHOP_IDS not set — querying "
                 "default shop only (typically DE).")
        shops = [(None, None)]  # one pass, no shop_id filter

    rows: list[dict] = []
    for country_override, shop_id in shops:
        n_before = len(rows)
        for o in _iter_orders(session, base, start, end, shop_id=shop_id):
            created = o.get("created_date") or o.get("date_created")
            period = pd.to_datetime(created, errors="coerce", utc=True)
            customer_country = _normalize_country(
                (o.get("customer", {}) or {}).get("shipping_address", {}).get("country")
            )
            country = country_override or customer_country
            for line in o.get("order_lines", []) or []:
                qty = float(line.get("quantity") or 0)
                unit_price = float(line.get("price_unit") or line.get("price") or 0)
                line_commission = float(
                    sum((c.get("amount") or 0) for c in line.get("commissions", []) or [])
                )
                line_refund = float(
                    sum((r.get("amount") or 0) for r in line.get("refunds", []) or [])
                )
                shipping_revenue = float(line.get("shipping_price") or 0)
                rows.append({
                    "period": period.tz_localize(None) if period is not pd.NaT else pd.NaT,
                    "order_id": o.get("order_id"),
                    "order_line_id": line.get("order_line_id"),
                    "sku": line.get("offer_sku") or line.get("product_sku") or line.get("sku"),
                    "product_title": line.get("product_title"),
                    "qty": qty,
                    "gross_revenue": unit_price * qty,
                    "commission": line_commission,
                    "shipping_revenue": shipping_revenue,
                    "refunded_amount": line_refund,
                    "customer_country": country,
                    "order_state": line.get("order_line_state") or o.get("order_state"),
                })
        if shop_id:
            log.info("  · shop %s (%s): %d order lines",
                     country_override, shop_id, len(rows) - n_before)

    df = pd.DataFrame(rows)
    if df.empty:
        log.info("Shop Apotheke: no orders in window %s → %s", start, end)
        return df
    df["period"] = pd.to_datetime(df["period"]).dt.normalize()
    log.info("Shop Apotheke: %d order lines across %d orders",
             len(df), df["order_id"].nunique())
    return df
