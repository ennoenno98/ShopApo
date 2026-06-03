# Shop Apotheke — Weekly Margin Dashboard

Weekly Streamlit dashboard for Vanatari's Shop Apotheke marketplace,
fed by:

- **Orders** — Shop Apotheke (Mirakl) seller API
- **Ad spend** — Bing Ads, TikTok Ads, Shop Apotheke on-site (CSV drop)
- **COGS / shipping / VAT** — reference CSVs in `inputs/` (seeded from
  the `Margen Calc pharma` sheet of `Margin_Check_V5.xlsx`)

GitHub Actions runs the export every Monday, commits the snapshot to
`exports/`, and Streamlit Community Cloud auto-redeploys — same pattern
as [`ennoenno98/Margin-Analytics`](https://github.com/ennoenno98/Margin-Analytics).

## Margin model

Mirrors `Margen Calc pharma` from the master workbook.

```
net_revenue       = (gross_revenue − refunds) / (1 + vat[country])
product_cost      = unit_cogs × qty                   (× 0.83 if country == GB)

CM1               = net_revenue − product_cost                          (gross product margin)

shipping_cost     = DHL_rate[country, peak?] × qty                      (€ gross we pay)
shipping_cost_net = shipping_cost / (1 + vat[country])
commission        = 0.16 × gross_revenue                                (Shop Apotheke fee)
three_pl_cost     = per-order fixed (€2.21, shared across lines by qty)
                    + pick cost per line (€0.23 first + €0.19 × extra units)

CM2               = CM1 − shipping_cost_net − commission − three_pl_cost
CM3               = CM2 − allocated_ad_spend
```

Notable specifics (all from the workbooks):
- **VAT** uses the pharma-reduced rate: DE 7%, FR 5.5%, IT/ES 10%, etc.
- **DHL peak surcharge** (+0.19 €) applies in November and December.
- **3PL fulfillment** uses the Everstock rate card from the AP26 plan
  (`Logistics 3PL`, rows 41–52). Per-order fixed €2.21 (handling 0.12
  + consolidation 0.55 + pack/ship 1.15 + packaging 0.27 + filling 0.12),
  plus per-pick variable (0.23 first SKU pick + 0.19 each additional
  unit of the same SKU). Editable in `inputs/three_pl_rates.csv`.
- **Shipping revenue** the customer pays goes to Shop Apotheke, not the
  seller, so it is *not* added back into CM2.
- **GB COGS × 0.83** — the workbook treats GB COGS in GBP via a fixed FX.

Knobs live as constants in `margin_model.py` (commission rate, peak
months, GB FX) or in `inputs/*.csv` (VAT, DHL, 3PL rates, COGS).

## Layout

| Path | Role |
| --- | --- |
| `connectors/shop_apotheke.py`    | Mirakl `GET /api/orders` (OR11). |
| `connectors/bing_ads.py`         | Microsoft Ads reporting API. |
| `connectors/tiktok_ads.py`       | TikTok Marketing API v1.3. |
| `connectors/shop_apotheke_ads.py`| Shop Apotheke on-site ads — CSV drop in `inputs/shop_apotheke_ads/` (no public API). |
| `margin_model.py`                | The ePharma margin model + a single-row `quote()` for the calculator tab. |
| `weekly_export.py`               | Orchestrator: pulls sources, computes margins, writes the snapshot. |
| `streamlit_app.py`               | Password-gated dashboard. Tabs: Overview, Weekly trend, SKU detail, Country, Ad spend, **Margin calculator** (interactive mirror of `Margen Calc pharma`). |
| `inputs/cogs.csv`                | Per-SKU unit COGS (seeded from `cogs jtl`). |
| `inputs/dhl_shipping.csv`        | DHL rate card per country, standard + peak. |
| `inputs/three_pl_rates.csv`      | Everstock 3PL rate card (per-order fixed + per-pick). |
| `inputs/vat_rates.csv`           | Country → pharma-reduced VAT rate. |
| `inputs/shop_apotheke_shipping_revenue.csv` | What customers pay for shipping (reference; not used in CM2). |
| `inputs/campaign_sku_map.csv`    | (Optional) campaign → SKU for direct ad attribution. |
| `inputs/shop_apotheke_ads/*.csv` | Drop weekly on-site ad exports here. |
| `exports/shopapo_export_*.csv.gz`| Weekly snapshots (committed by the Action). |
| `.github/workflows/weekly_export.yml` | Schedules the export. |

## Required secrets

Set in **GitHub → Settings → Secrets and variables → Actions** *and* in
**Streamlit Cloud → App settings → Secrets**:

```toml
# Streamlit dashboard
DASHBOARD_PASSWORD       = "..."

# Shop Apotheke (Mirakl) — base URL defaults to https://shopapotheke.mirakl.net/api
SHOP_APOTHEKE_API_KEY    = "..."
# Optional override if the host changes:
# SHOP_APOTHEKE_BASE_URL = "https://shopapotheke.mirakl.net/api"

# Microsoft (Bing) Ads
BING_DEVELOPER_TOKEN     = "..."
BING_CLIENT_ID           = "..."
BING_CLIENT_SECRET       = "..."
BING_REFRESH_TOKEN       = "..."
BING_CUSTOMER_ID         = "..."
BING_ACCOUNT_ID          = "..."

# TikTok Ads
TIKTOK_ACCESS_TOKEN      = "..."
TIKTOK_ADVERTISER_ID     = "..."

# Shop Apotheke retail-media portal (sa-tech.de) — auto-download
SA_TECH_EMAIL            = "..."
SA_TECH_PASSWORD         = "..."
```

Missing credentials for any one connector skip that source — the snapshot
is still written from whatever did succeed.

## Shop Apotheke on-site ads

The retail-media portal at <https://retail.sa-tech.de/advertiser-reports/>
has no public API. We automate the CSV export via Playwright:

- `scripts/sa_tech_download.py` logs into the portal with
  `SA_TECH_EMAIL` / `SA_TECH_PASSWORD`, sets the date range, and
  downloads the CSV into `inputs/shop_apotheke_ads/`.
- The weekly GitHub Action runs it before `weekly_export.py`, so the
  ad data is fresh each Monday. If those two secrets aren't set the
  step is skipped.
- First-run debugging: `python scripts/sa_tech_download.py --headed
  --debug` — opens a visible browser and saves a screenshot of every
  step to `scripts/sa_tech_screens/`. If a selector misses, the
  screenshot tells you which element to target; edit the
  `SELECTOR_*` constants at the top of the script.
- Fallback: you can still drop a manual UI export into the same folder
  — `connectors/shop_apotheke_ads.py` reads everything there.

## Local dev

```bash
pip install -r requirements.txt -r requirements-export.txt

export SHOP_APOTHEKE_API_KEY="..."
export DASHBOARD_PASSWORD="pick-something"
# Ad platform vars optional — missing connectors are skipped.

python weekly_export.py --once
streamlit run streamlit_app.py
# → http://localhost:8501
```

The **Margin calculator** tab works without a snapshot, so you can test
the model end-to-end before wiring up the APIs.

## Deploying

**Streamlit Community Cloud** (same as Margin-Analytics):
1. <https://share.streamlit.io> → **Create app → Deploy a public app**.
2. Repo: `ennoenno98/ShopApo`, branch:
   `claude/awesome-hamilton-X7w62` (or `main` after merging), main file:
   `streamlit_app.py`.
3. Secrets → at minimum `DASHBOARD_PASSWORD = "..."`.
4. Push → auto-redeploy.

## What runs when

| Trigger | What happens |
| --- | --- |
| Monday 05:30 UTC (cron) | Action runs `weekly_export.py --once`, commits the new snapshot. |
| Push to deployed branch | Streamlit redeploys, picks up the new snapshot. |
| **Actions → Run workflow** | Manual refresh. |
