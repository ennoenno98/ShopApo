# Shop Apotheke Dashboard — Maintainer's Debrief

A short handover for whoever takes over day-to-day operation. For the
deeper architecture (margin model, P&L formulas, file layout) see
`README.md`.

## What this app does

A weekly Streamlit dashboard for our Shop Apotheke marketplace business
(DE / AT / IT under one Mirakl API key, three shop IDs). It pulls:

- **Orders** from the Shop Apotheke Mirakl API (one request per country).
- **On-site ad spend** from sa-tech advertiser reports (CSV uploads).

It rebuilds a snapshot every Monday morning via GitHub Actions and
publishes it to Streamlit Cloud. The dashboard URL is the same one you
already use.

## The two things you'll actually do

### 1. Upload a new sa-tech ads CSV

Open the dashboard → sidebar → **📤 Ads CSV uploads** expander → drop
the file → **Upload & refresh dashboard**.

The widget:
1. Reads the existing `inputs/shop_apotheke_ads/master_advertiserreport.csv`.
2. Drops any rows whose `(country, date)` your new file covers.
3. Appends the new rows.
4. Commits the merged master to GitHub.
5. Triggers the rebuild workflow.

Wait ~3–5 minutes. The dashboard then auto-refreshes with the new data.

**One master file, always.** sa-tech exports include all three
countries (`com_…` = DE, `at_…` = AT, `it_…` = IT in the campaign name)
so one file per export is enough — country comes from each row's
campaign prefix, not the filename.

`SOLD_OUT_*` campaigns are out-of-stock products; their spend gets
pooled and split across SKUs by net-revenue share automatically.

### 2. Manually trigger a rebuild

If you change something in the repo (a code edit, an input file) and
want the dashboard updated without waiting for Monday:

GitHub → Actions → **Shop Apotheke Weekly Export** → **Run workflow**
→ pick the working branch → Run.

Same effect as a dashboard upload, minus the upload.

## Secrets that have to stay set

All in **Settings → Secrets and variables → Actions** on the repo, plus
`GITHUB_TOKEN` and `DASHBOARD_PASSWORD` in **Streamlit Cloud → Settings
→ Secrets**.

| Secret | Where | What it does |
|---|---|---|
| `SHOP_APOTHEKE_API_KEY` | GitHub | Mirakl seller API key (one key, multi-country) |
| `SHOP_APOTHEKE_SHOP_IDS` | GitHub | `DE:3310,AT:3401,IT:3543` — used to query each marketplace separately |
| `SA_TECH_EMAIL` / `SA_TECH_PASSWORD` | GitHub | Login for the (currently broken) Playwright auto-download; safe to leave even if unused |
| `GITHUB_TOKEN` | Streamlit Cloud | Fine-grained PAT for the upload widget. Needs Contents + Actions, repo-scoped. Rotates after 1 year. |
| `DASHBOARD_PASSWORD` | Streamlit Cloud | Single shared password for the login screen |

## When something breaks

**Workflow run failed at the final `git push` step.**
A concurrent run got there first. The step retries with rebase up to
5×, but if a flurry of uploads exceeds that, the latest run wins and
the earlier one's data is already in the repo anyway. Just re-trigger.

**Upload widget says "403 Resource not accessible by personal access
token".**
The `GITHUB_TOKEN` PAT either expired or has the wrong permissions.
Regenerate at https://github.com/settings/personal-access-tokens with
Contents = Read and write, Actions = Read and write, scoped to this
repo. Paste the new value into Streamlit Cloud secrets.

**Dashboard shows old numbers after an upload.**
Streamlit Cloud caches the snapshot. The cache invalidates on file
modification, so if Streamlit didn't redeploy (check the deploy log),
hit "Reboot app" in the Streamlit Cloud settings.

**A new country needs to be added.**
1. Add `<COUNTRY>:<shop_id>` to `SHOP_APOTHEKE_SHOP_IDS`.
2. Add a row to `inputs/vat_rates.csv` and `inputs/dhl_rates.csv`
   (otherwise the margin model falls back to a default 7% VAT).
3. Add the campaign prefix to `CAMPAIGN_COUNTRY_PREFIXES` in both
   `connectors/shop_apotheke_ads.py` and `streamlit_app.py`.
4. Re-run the workflow.

## Where the data actually is

```
inputs/
  shop_apotheke_ads/
    master_advertiserreport.csv   ← single rolling file, updated by uploads
  cogs.csv                        ← unit COGS per SKU (manual)
  vat_rates.csv                   ← per-country VAT
  dhl_rates.csv                   ← per-country DHL shipping
  three_pl_rates.csv              ← Everstock pick rate card
  campaign_map.csv                ← campaign-name → SKU mapping (optional)
exports/
  shopapo_export_YYYY-MM-DD.csv.gz ← what the dashboard reads
  shopapo_export_YYYY-MM-DD.xlsx
```

The dashboard always reads the most recent `shopapo_export_*` file in
`exports/`. The workflow commits a fresh one each run.

## Key code locations if you have to dig

- `streamlit_app.py` — dashboard UI + upload widget. Edit here for
  layout/filter changes.
- `connectors/shop_apotheke.py` — Mirakl orders fetcher.
- `connectors/shop_apotheke_ads.py` — sa-tech CSV parser; campaign-
  prefix country detection lives here.
- `weekly_export.py` — orchestrator. Combines connectors, runs the
  margin model, writes the snapshot.
- `margin_model.py` — VAT / DHL / 3PL / commission formulas.
- `.github/workflows/weekly_export.yml` — Monday cron + manual
  trigger.
