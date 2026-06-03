"""Headless downloader for Shop Apotheke retail-media ads reports.

Logs into https://retail.sa-tech.de via Playwright, opens
/advertiser-reports/, downloads the CSV for [start, end], and saves it
to `inputs/shop_apotheke_ads/`. The existing connector
(`connectors/shop_apotheke_ads.py`) reads everything in that folder.

Usage
-----
    # one-off, last 30 days
    python scripts/sa_tech_download.py --days 30

    # explicit window
    python scripts/sa_tech_download.py --start 2026-05-01 --end 2026-05-31

    # first run / debugging: visible browser + screenshots at each step
    python scripts/sa_tech_download.py --days 30 --headed --debug

Env vars
--------
SA_TECH_EMAIL           your login email
SA_TECH_PASSWORD        your login password
SA_TECH_BASE_URL        override (default: https://retail.sa-tech.de)

Selector notes
--------------
The CSS selectors below are best-guess (the site sits behind a login,
so they couldn't be inspected from the dev sandbox). If a step fails:

1. Re-run with `--headed --debug`. Each step writes a screenshot to
   `scripts/.sa_tech_screens/`.
2. Open the failing screenshot, right-click the element you'd click,
   copy its CSS selector or accessible name.
3. Edit the corresponding SELECTOR_* constant at the top of this file.

You can also let Playwright record an interactive session:
    playwright codegen https://retail.sa-tech.de
and paste the generated selectors in.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.stderr.write(
        "Playwright is not installed. Run:\n"
        "    pip install playwright\n"
        "    playwright install chromium\n"
    )
    sys.exit(1)


# ---------- adjust these if the login / reports page changes ----------
BASE_URL = os.environ.get("SA_TECH_BASE_URL", "https://retail.sa-tech.de").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login"
REPORTS_URL = f"{BASE_URL}/advertiser-reports/"

# Login form selectors — match the most common patterns first.
SELECTOR_EMAIL = 'input[type="email"], input[name="email"], input[name="username"], input[id*="mail" i]'
SELECTOR_PASSWORD = 'input[type="password"]'
SELECTOR_LOGIN_SUBMIT = 'button[type="submit"], button:has-text("Login"), button:has-text("Anmelden"), button:has-text("Sign in")'

# Reports page selectors.
SELECTOR_DATE_FROM = 'input[name*="from" i], input[name*="start" i], input[placeholder*="from" i], input[placeholder*="start" i]'
SELECTOR_DATE_TO = 'input[name*="to" i], input[name*="end" i], input[placeholder*="to" i], input[placeholder*="end" i]'
SELECTOR_RUN_REPORT = 'button:has-text("Apply"), button:has-text("Run"), button:has-text("Search"), button:has-text("Anwenden"), button:has-text("Suchen")'
SELECTOR_DOWNLOAD = 'a:has-text("Download"), a:has-text("Export"), button:has-text("Download"), button:has-text("Export"), button:has-text("CSV"), a:has-text("CSV")'

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "inputs" / "shop_apotheke_ads"
SCREEN_DIR = Path(__file__).resolve().parent / ".sa_tech_screens"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def _shoot(page, name: str) -> None:
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREEN_DIR / f"{datetime.utcnow():%H%M%S}_{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("  · screenshot → %s", path)
    except Exception as e:
        log.warning("  · screenshot failed: %s", e)


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(f"{name} is not set in the environment.\n")
        sys.exit(2)
    return v


def login(page, *, debug: bool) -> None:
    email = _required_env("SA_TECH_EMAIL")
    password = _required_env("SA_TECH_PASSWORD")

    log.info("Loading login page %s", LOGIN_URL)
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    if debug:
        _shoot(page, "01_login_loaded")

    page.wait_for_selector(SELECTOR_EMAIL, timeout=15_000)
    page.fill(SELECTOR_EMAIL, email)
    page.fill(SELECTOR_PASSWORD, password)
    if debug:
        _shoot(page, "02_login_filled")
    page.click(SELECTOR_LOGIN_SUBMIT)

    # Wait for navigation away from /login.
    try:
        page.wait_for_url(re.compile(rf"^(?!.*/login).*"), timeout=20_000)
    except PWTimeout:
        _shoot(page, "03_login_stuck")
        raise RuntimeError(
            "Did not navigate away from /login. Check credentials, or "
            "look at the screenshot in scripts/.sa_tech_screens/ to "
            "identify the actual submit button selector."
        )
    log.info("Logged in.")


def go_to_reports(page, *, debug: bool) -> None:
    log.info("Opening reports page %s", REPORTS_URL)
    page.goto(REPORTS_URL, wait_until="networkidle")
    if debug:
        _shoot(page, "04_reports_loaded")


def apply_date_range(page, start: datetime, end: datetime, *, debug: bool) -> None:
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    log.info("Setting date range %s → %s", start_str, end_str)

    try:
        page.wait_for_selector(SELECTOR_DATE_FROM, timeout=10_000)
        page.fill(SELECTOR_DATE_FROM, start_str)
        page.fill(SELECTOR_DATE_TO, end_str)
        if debug:
            _shoot(page, "05_dates_filled")
    except PWTimeout:
        log.warning(
            "Date inputs not found by selector — leaving the report at "
            "its default window. Adjust SELECTOR_DATE_FROM / _TO at the "
            "top of this file if the report period is wrong."
        )
        return

    # Some UIs auto-apply, others need a click.
    try:
        page.click(SELECTOR_RUN_REPORT, timeout=5_000)
        page.wait_for_load_state("networkidle")
        if debug:
            _shoot(page, "06_report_run")
    except PWTimeout:
        log.info("No explicit 'Run' button — assuming auto-apply.")


def download_csv(page, *, debug: bool) -> Path:
    log.info("Triggering CSV download")
    with page.expect_download(timeout=60_000) as info:
        page.click(SELECTOR_DOWNLOAD)
    download = info.value

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suggested = download.suggested_filename or "shop_apotheke_ads.csv"
    # Stamp the filename so weekly downloads don't overwrite.
    stamped = f"{datetime.utcnow():%Y-%m-%d}_{suggested}"
    if not stamped.lower().endswith(".csv"):
        stamped += ".csv"
    target = OUTPUT_DIR / stamped
    download.save_as(target)
    if debug:
        _shoot(page, "07_download_done")
    log.info("Saved → %s (%d bytes)", target, target.stat().st_size)
    return target


def run(start: datetime, end: datetime, *, headed: bool, debug: bool) -> Path:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            login(page, debug=debug)
            go_to_reports(page, debug=debug)
            apply_date_range(page, start, end, debug=debug)
            target = download_csv(page, debug=debug)
        finally:
            browser.close()
    return target


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7,
                   help="Trailing window size in days (default 7)")
    p.add_argument("--start", type=str, help="ISO date; overrides --days")
    p.add_argument("--end", type=str, help="ISO date; defaults to today")
    p.add_argument("--headed", action="store_true",
                   help="Show the browser (debugging)")
    p.add_argument("--debug", action="store_true",
                   help="Save a screenshot at each step")
    args = p.parse_args()

    end = datetime.fromisoformat(args.end) if args.end else datetime.utcnow()
    if args.start:
        start = datetime.fromisoformat(args.start)
    else:
        start = end - timedelta(days=args.days - 1)

    run(start, end, headed=args.headed, debug=args.debug)


if __name__ == "__main__":
    main()
