"""Sync Shop Apotheke ads CSVs from a OneDrive / SharePoint folder.

Reads everything new from the team-shared folder
(`ONEDRIVE_SHARED_LINK`) and drops it into
`inputs/shop_apotheke_ads/` so the existing connector picks it up.

Used in the weekly GitHub Action so team members can just drop the
sa-tech CSV exports into a OneDrive folder and the dashboard refreshes
itself.

Auth uses the Microsoft Graph app-only (client credentials) flow:
    GRAPH_TENANT_ID
    GRAPH_CLIENT_ID
    GRAPH_CLIENT_SECRET
    ONEDRIVE_SHARED_LINK    full URL of the shared folder

If any of these are missing the script no-ops with a warning, so the
rest of the weekly export keeps running.
"""
from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path

import requests


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "inputs" / "shop_apotheke_ads"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def _encode_share_id(url: str) -> str:
    """Microsoft Graph's share-id encoding: `u!` + base64url-no-pad of the URL."""
    raw = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"u!{raw}"


def _token(tenant: str, client_id: str, client_secret: str) -> str:
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _list_children(token: str, share_id: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem/children",
        headers=headers, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("value", [])


def _download(token: str, item: dict, target: Path) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    download_url = item.get("@microsoft.graph.downloadUrl")
    if download_url:
        # The download URL is a short-lived pre-authenticated SAS link.
        r = requests.get(download_url, timeout=120, stream=True)
    else:
        r = requests.get(
            f"https://graph.microsoft.com/v1.0/drives/{item['parentReference']['driveId']}"
            f"/items/{item['id']}/content",
            headers=headers, timeout=120, stream=True,
        )
    r.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            f.write(chunk)


def main() -> None:
    tenant = os.environ.get("GRAPH_TENANT_ID")
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET")
    share_url = os.environ.get("ONEDRIVE_SHARED_LINK")

    missing = [k for k, v in {
        "GRAPH_TENANT_ID": tenant,
        "GRAPH_CLIENT_ID": client_id,
        "GRAPH_CLIENT_SECRET": client_secret,
        "ONEDRIVE_SHARED_LINK": share_url,
    }.items() if not v]
    if missing:
        log.warning("OneDrive sync skipped — missing env vars: %s", ", ".join(missing))
        return

    log.info("Authenticating with Microsoft Graph (tenant %s)…", tenant[:8] + "…")
    token = _token(tenant, client_id, client_secret)

    share_id = _encode_share_id(share_url)
    log.info("Listing shared folder contents…")
    items = _list_children(token, share_id)
    log.info("Found %d items in shared folder", len(items))

    csv_items = [
        it for it in items
        if not it.get("folder")
        and (it.get("name") or "").lower().endswith((".csv", ".csv.gz", ".xlsx"))
    ]
    if not csv_items:
        log.info("No CSV/Excel files to sync.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = {f.name for f in OUTPUT_DIR.iterdir() if f.is_file()}
    new_count = 0
    for it in csv_items:
        name = it["name"]
        target = OUTPUT_DIR / name
        if name in existing:
            log.info("  · %s — already present, skipping", name)
            continue
        log.info("  · %s — downloading (%.1f KB)…",
                 name, (it.get("size") or 0) / 1024)
        _download(token, it, target)
        new_count += 1
    log.info("Sync complete: %d new file(s) pulled into %s", new_count, OUTPUT_DIR)


if __name__ == "__main__":
    main()
