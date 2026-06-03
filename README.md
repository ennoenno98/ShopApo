# Shop Apotheke — Weekly Margin Dashboard

Weekly Streamlit dashboard for Vanatari's Shop Apotheke marketplace
business, fed by:

- **Orders** — Shop Apotheke (Mirakl) seller API
- **Ad spend** — Bing Ads, TikTok Ads, Shop Apotheke on-site (CSV drop)
- **COGS / shipping** — user-provided CSVs in `inputs/`

The pattern mirrors [`ennoenno98/Margin-Analytics`](https://github.com/ennoenno98/Margin-Analytics):
GitHub Actions runs the export every Monday, commits the snapshot to
`exports/`, and Streamlit Community Cloud auto-redeploys.

## Layout

| Path | Role |
| --- | --- |
| `connectors/shop_apotheke.py`   | Mirakl OR11 orders endpoint. |
| `connectors/bing_ads.py`        | Microsoft Ads reporting API. |
| `connectors/tiktok_ads.py`      | TikTok Marketing API v1.3. |
| `connectors/shop_apotheke_ads.py` | Shop Apotheke on-site ads — CSV drop in `inputs/shop_apotheke_ads/`. |
| `weekly_export.py`              | Orchestrator: pulls sources, joins with COGS + shipping, writes the snapshot. |
| `inputs/cogs.csv`               | Your per-SKU unit COGS + inbound shipping. |
| `inputs/shipping.csv`           | Your per-SKU outbound shipping cost. |
| `inputs/campaign_sku_map.csv`   | (Optional) campaign → SKU map for direct ad attribution. |
| `exports/shopapo_export_*.csv.gz` | Weekly snapshots (committed by the Action). |
| `streamlit_app.py`              | Password-gated dashboard. |
| `.github/workflows/weekly_export.yml` | Schedules the export. |

## Required secrets

Set these in **GitHub → Settings → Secrets → Actions** (for the workflow)
and in **Streamlit Cloud → App settings → Secrets** (for the dashboard
password):

```
# Streamlit dashboard
DASHBOARD_PASSWORD       = "..."

# Shop Apotheke seller API
SHOP_APOTHEKE_API_KEY    = "..."

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
```

Missing credentials for any connector just skip that source — the
snapshot is still written from whatever did succeed.

## COGS + shipping inputs

Fill these CSVs in `inputs/` with one row per SKU:

`cogs.csv` — landed unit cost:
```
sku,unit_cogs,inbound_shipping
VI-ABC-123,4.50,0.40
```

`shipping.csv` — the carrier cost *you* pay to ship one unit:
```
sku,unit_outbound_shipping
VI-ABC-123,2.20
```

`campaign_sku_map.csv` *(optional)* — pin a campaign's spend to a
specific SKU instead of letting the orchestrator split it across SKUs by
daily net revenue:
```
channel,campaign,sku
tiktok,1234567890,VI-ABC-123
bing,Brand - Vegavero,VI-XYZ-999
```

## Shop Apotheke on-site ads

There's no public API for the Shop Apotheke sponsored-products platform,
so each week:

1. In the Shop Apotheke ads UI: **Reports → Performance → Export → CSV**.
2. Drop the file into `inputs/shop_apotheke_ads/` and commit. Any extra
   columns are ignored; the connector needs `date, campaign, spend`
   plus the optional `impressions, clicks, conversions, revenue`.

When/if an API becomes available, swap the body of
`connectors/shop_apotheke_ads.py` — the orchestrator contract stays the
same.

## Margin definition

```
net_revenue    = gross_revenue − refunded_amount
CM1            = net_revenue − cogs − inbound_shipping            (gross product margin)
CM2            = CM1 − marketplace_commission − outbound_shipping
                     + shipping_revenue                            (operating margin)
CM3            = CM2 − allocated_ad_spend                          (final P&L margin)
```

Ad spend is allocated to SKUs:
1. Direct: anything in `inputs/campaign_sku_map.csv` lands 1:1.
2. Remainder: split daily across SKUs in proportion to net revenue.

## Local dev

```bash
pip install -r requirements.txt -r requirements-export.txt

# Set credentials (or use a .env)
export SHOP_APOTHEKE_API_KEY=...
export DASHBOARD_PASSWORD="pick-something"
# (other ad-platform vars optional — missing connectors are skipped)

python weekly_export.py --once
streamlit run streamlit_app.py
# → http://localhost:8501
```

## Deploying the dashboard

**Streamlit Community Cloud** (same as Margin-Analytics):
1. <https://share.streamlit.io> → **Create app → Deploy a public app**.
2. Repo: `ennoenno98/ShopApo`, branch: `claude/awesome-hamilton-X7w62`
   (or `main` after merging), main file: `streamlit_app.py`.
3. Secrets → paste `DASHBOARD_PASSWORD = "..."`.
4. Push → auto-redeploy.

## What runs when

| Trigger | What happens |
| --- | --- |
| Monday 05:30 UTC (cron) | Action runs `weekly_export.py --once`, commits the new snapshot. |
| Push to deployed branch | Streamlit redeploys, picks up the new snapshot. |
| **Actions → Run workflow** | Same as the cron — manual refresh. |
