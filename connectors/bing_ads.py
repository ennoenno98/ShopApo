"""Microsoft (Bing) Ads connector.

Pulls campaign-level spend, impressions, clicks, and conversions for the
requested window using the Microsoft Advertising Reporting API v13.

Auth uses OAuth2 refresh-token flow (long-lived refresh token stored as a
secret; we exchange it for an access token on each run).

Env vars
--------
BING_DEVELOPER_TOKEN     Microsoft Ads developer token
BING_CLIENT_ID           Azure AD app client id
BING_CLIENT_SECRET       Azure AD app client secret (confidential client)
BING_REFRESH_TOKEN       Refresh token obtained via interactive consent
BING_CUSTOMER_ID         Microsoft Ads customer id (CID)
BING_ACCOUNT_ID          Microsoft Ads account id
"""
from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from datetime import datetime
from typing import Any

import pandas as pd
import requests

from . import ConnectorSkipped

log = logging.getLogger(__name__)

OAUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
REPORTING_URL = "https://reporting.api.bingads.microsoft.com/Reporting/v13"
SCOPES = "https://ads.microsoft.com/msads.manage offline_access"


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise ConnectorSkipped(f"{name} not set")
    return v


def _access_token() -> str:
    r = requests.post(
        OAUTH_URL,
        data={
            "client_id": _required("BING_CLIENT_ID"),
            "client_secret": _required("BING_CLIENT_SECRET"),
            "refresh_token": _required("BING_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
            "scope": SCOPES,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "DeveloperToken": _required("BING_DEVELOPER_TOKEN"),
        "CustomerId": _required("BING_CUSTOMER_ID"),
        "CustomerAccountId": _required("BING_ACCOUNT_ID"),
        "Content-Type": "application/json",
    }


def _submit_report(headers: dict[str, str], start: datetime, end: datetime) -> str:
    body: dict[str, Any] = {
        "ReportRequest": {
            "Type": "CampaignPerformanceReportRequest",
            "Aggregation": "Daily",
            "Format": "Csv",
            "ReturnOnlyCompleteData": False,
            "Scope": {
                "AccountIds": [int(_required("BING_ACCOUNT_ID"))],
            },
            "Time": {
                "CustomDateRangeStart": {"Day": start.day, "Month": start.month, "Year": start.year},
                "CustomDateRangeEnd": {"Day": end.day, "Month": end.month, "Year": end.year},
                "ReportTimeZone": "GreenwichMeanTimeDublinEdinburghLisbonLondon",
            },
            "Columns": [
                "TimePeriod", "AccountName", "CampaignName", "CampaignId",
                "Impressions", "Clicks", "Spend", "Conversions", "Revenue",
            ],
        }
    }
    r = requests.post(f"{REPORTING_URL}/GenerateReport/Submit",
                      json=body, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()["ReportRequestId"]


def _poll_until_ready(headers: dict[str, str], request_id: str) -> str:
    for _ in range(60):
        r = requests.post(
            f"{REPORTING_URL}/GenerateReport/Poll",
            json={"ReportRequestId": request_id},
            headers=headers, timeout=60,
        )
        r.raise_for_status()
        status = r.json()["ReportRequestStatus"]
        state = status.get("Status")
        if state == "Success":
            return status["ReportDownloadUrl"]
        if state == "Error":
            raise RuntimeError(f"Bing report failed: {status}")
        time.sleep(5)
    raise TimeoutError("Bing report did not finish in 5 minutes")


def _download_csv(url: str) -> pd.DataFrame:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    # Reports come back as a zip with a single CSV inside.
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            # Bing prepends a comment line before the header.
            return pd.read_csv(fh, skiprows=0)


def fetch(start: datetime, end: datetime) -> pd.DataFrame:
    """Return daily campaign-level Bing spend for [start, end].

    Columns: period, channel, campaign, spend, impressions, clicks,
    conversions, ad_revenue.
    """
    token = _access_token()
    headers = _headers(token)
    request_id = _submit_report(headers, start, end)
    url = _poll_until_ready(headers, request_id)
    raw = _download_csv(url)
    if raw.empty:
        return raw

    df = pd.DataFrame({
        "period": pd.to_datetime(raw["TimePeriod"], errors="coerce").dt.normalize(),
        "channel": "bing",
        "campaign": raw["CampaignName"],
        "spend": pd.to_numeric(raw["Spend"], errors="coerce"),
        "impressions": pd.to_numeric(raw["Impressions"], errors="coerce"),
        "clicks": pd.to_numeric(raw["Clicks"], errors="coerce"),
        "conversions": pd.to_numeric(raw["Conversions"], errors="coerce"),
        "ad_revenue": pd.to_numeric(raw.get("Revenue"), errors="coerce"),
    })
    log.info("Bing Ads: %d daily rows, total spend €%.2f", len(df), df["spend"].sum())
    return df
