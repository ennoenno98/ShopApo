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
   `scripts/sa_tech_screens/`.
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
# Start at the root so any redirect (e.g. to /auth/signin) is followed
# automatically; /login is also handled if no redirect happens.
LOGIN_URL = BASE_URL
REPORTS_URL = f"{BASE_URL}/advertiser-reports/"

# Login form selectors — match the most common patterns first.
SELECTOR_EMAIL = 'input[type="email"], input[name="email"], input[name="username"], input[id*="mail" i]'
SELECTOR_PASSWORD = 'input[type="password"]'
SELECTOR_LOGIN_SUBMIT = 'button[type="submit"], button:has-text("Login"), button:has-text("Anmelden"), button:has-text("Sign in")'

# Reports page selectors — sa-tech uses MUI X DateRangePicker; each side
# has 3 contenteditable spinbuttons (Day / Month / Year) marked with
# data-range-position. We target those directly rather than chasing a
# nonexistent <input>.
SELECTOR_DATE_START_DAY = '[data-range-position="start"][aria-label="Day"]'
SELECTOR_DATE_START_MONTH = '[data-range-position="start"][aria-label="Month"]'
SELECTOR_DATE_START_YEAR = '[data-range-position="start"][aria-label="Year"]'
SELECTOR_DATE_END_DAY = '[data-range-position="end"][aria-label="Day"]'
SELECTOR_DATE_END_MONTH = '[data-range-position="end"][aria-label="Month"]'
SELECTOR_DATE_END_YEAR = '[data-range-position="end"][aria-label="Year"]'
SELECTOR_RUN_REPORT = (
    'button:has-text("Apply"), button:has-text("Run"), button:has-text("Search"), '
    'button:has-text("Anwenden"), button:has-text("Suchen"), '
    'button:has-text("Aktualisieren"), button:has-text("Ausführen")'
)
# Download button: literal text in DE/EN, plus icon-button aria-labels.
SELECTOR_DOWNLOAD = (
    'a:has-text("Download"), a:has-text("Export"), a:has-text("CSV"), '
    'a:has-text("Herunterladen"), a:has-text("Exportieren"), '
    'button:has-text("Download"), button:has-text("Export"), button:has-text("CSV"), '
    'button:has-text("Herunterladen"), button:has-text("Exportieren"), '
    'button[aria-label*="Download" i], button[aria-label*="Export" i], '
    'button[aria-label*="herunterladen" i]'
)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "inputs" / "shop_apotheke_ads"
# Plain dir name (no leading dot) so `actions/upload-artifact` picks it up
# without needing include-hidden-files.
SCREEN_DIR = Path(__file__).resolve().parent / "sa_tech_screens"


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


def _dump_inputs(page) -> None:
    """Log every <input>/<button> on the page so we can fix selectors after a miss."""
    try:
        info = page.evaluate(
            """() => {
              const fields = Array.from(document.querySelectorAll('input,button,a')).map(el => ({
                tag: el.tagName,
                type: el.getAttribute('type'),
                name: el.getAttribute('name'),
                id: el.id,
                placeholder: el.getAttribute('placeholder'),
                ariaLabel: el.getAttribute('aria-label'),
                text: (el.innerText || '').slice(0, 60),
              }));
              return fields;
            }"""
        )
        log.info("--- Input / button / link elements on this page ---")
        for f in info:
            log.info("  %s", f)
        log.info("--- End element dump ---")
    except Exception as e:
        log.warning("Could not dump page inputs: %s", e)


def _dump_html(page, name: str) -> None:
    """Save raw HTML alongside the screenshot so we can grep selectors later."""
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    target = SCREEN_DIR / f"{datetime.utcnow():%H%M%S}_{name}.html"
    try:
        target.write_text(page.content(), encoding="utf-8")
        log.info("  · html → %s", target)
    except Exception as e:
        log.warning("  · html dump failed: %s", e)


def login(page, *, debug: bool) -> None:
    email = _required_env("SA_TECH_EMAIL")
    password = _required_env("SA_TECH_PASSWORD")

    log.info("Loading login page %s", LOGIN_URL)
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # SPA login pages often hydrate after DOMContentLoaded.
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PWTimeout:
        log.warning("networkidle never reached — page may be making background requests forever; proceeding anyway")

    log.info("After navigation: url=%s  title=%r  html_len=%d",
             page.url, page.title(), len(page.content()))
    if debug:
        _shoot(page, "01_login_loaded")
        _dump_html(page, "01_login_loaded")

    # The landing page shows a "LOGIN" CTA button (and "Set new password"
    # / "INTERNAL LOGIN" links). Click it to reveal the email/password
    # modal/form. If the inputs are already visible, this click is a
    # no-op — the next wait succeeds either way.
    try:
        login_button = page.get_by_role("button", name="LOGIN", exact=True)
        if login_button.count() > 0:
            login_button.first.click(timeout=5_000)
            page.wait_for_load_state("networkidle", timeout=10_000)
            if debug:
                _shoot(page, "01b_after_login_button")
    except PWTimeout:
        log.info("LOGIN button click didn't transition the page — assuming inputs are already in the DOM.")

    try:
        page.wait_for_selector(SELECTOR_EMAIL, timeout=15_000)
    except PWTimeout:
        log.error("Email selector %r did not match anything on the login page.",
                  SELECTOR_EMAIL)
        _shoot(page, "02_login_no_email_field")
        _dump_html(page, "02_login_no_email_field")
        _dump_inputs(page)
        log.info("Frames on this page:")
        for fr in page.frames:
            log.info("  · %s (url=%s)", fr.name or "<main>", fr.url)
        # Try the much broader 'wait for any input' so we know if it's a timing
        # issue versus the page genuinely not having a form.
        try:
            page.wait_for_selector("input", timeout=5_000)
            log.info("`input` selector eventually matched — the form is hydrating slow.")
        except PWTimeout:
            log.info("`input` never matched either — page has no form inputs at all.")
        raise

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
            "look at the screenshot in scripts/sa_tech_screens/ to "
            "identify the actual submit button selector."
        )
    log.info("Logged in.")


def go_to_reports(page, *, debug: bool) -> None:
    log.info("Opening reports page %s", REPORTS_URL)
    page.goto(REPORTS_URL, wait_until="networkidle")
    if debug:
        _shoot(page, "04_reports_loaded")


def _fill_mui_date_section(page, selector: str, value: int, width: int) -> None:
    """Focus a MUI DateRangePicker section and type the numeric value.

    MUI sections are contenteditable spinbuttons; selecting all + typing
    replaces the visible value cleanly.
    """
    el = page.locator(selector).first
    el.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(str(value).zfill(width), delay=20)


def apply_date_range(page, start: datetime, end: datetime, *, debug: bool) -> None:
    log.info("Setting date range %s → %s", start.date(), end.date())

    try:
        page.wait_for_selector(SELECTOR_DATE_START_DAY, timeout=10_000)
    except PWTimeout:
        log.warning(
            "MUI date sections not found — leaving the report at its "
            "default window. Inspect the page and adjust the "
            "SELECTOR_DATE_* constants if the report period is wrong."
        )
        if debug:
            _shoot(page, "05_dates_not_found")
        return

    try:
        # Start side: Day → Month → Year
        _fill_mui_date_section(page, SELECTOR_DATE_START_DAY, start.day, 2)
        _fill_mui_date_section(page, SELECTOR_DATE_START_MONTH, start.month, 2)
        _fill_mui_date_section(page, SELECTOR_DATE_START_YEAR, start.year, 4)
        # End side
        _fill_mui_date_section(page, SELECTOR_DATE_END_DAY, end.day, 2)
        _fill_mui_date_section(page, SELECTOR_DATE_END_MONTH, end.month, 2)
        _fill_mui_date_section(page, SELECTOR_DATE_END_YEAR, end.year, 4)
        # Confirm the input so the table reloads.
        page.keyboard.press("Tab")
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception as e:
        log.warning("Failed to type into MUI date sections: %s", e)
    if debug:
        _shoot(page, "05_dates_filled")

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
    if debug:
        _shoot(page, "06b_before_download")
        _dump_html(page, "06b_before_download")
    try:
        with page.expect_download(timeout=60_000) as info:
            page.click(SELECTOR_DOWNLOAD)
        download = info.value
    except PWTimeout:
        log.error("Download/Export button not found. Page elements:")
        _dump_inputs(page)
        if debug:
            _shoot(page, "07_download_not_found")
        raise

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
        # Anti-bot-detection: drop the most obvious Playwright/headless
        # fingerprints, run with a real Chrome (not chrome-headless-shell),
        # set a normal UA + DE locale + viewport so the SPA boots.
        browser = p.chromium.launch(
            headless=not headed,
            channel="chrome" if not headed else None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        # Strip `navigator.webdriver` so the page can't see we're automated.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
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
