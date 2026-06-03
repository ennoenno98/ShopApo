"""ePharma (Shop Apotheke) margin model.

Mirrors the `Margen Calc pharma` sheet from Margin_Check_V5.xlsx so the
weekly dashboard and the interactive calculator both compute the same
numbers.

Per-line economics
------------------
  net_price        = gross_price / (1 + vat[country])
  net_revenue      = net_price × qty − refunds_net
  product_cost     = unit_cogs × qty             (× 0.83 if country == GB)

  CM1              = net_revenue − product_cost                  (gross product margin)

  shipping_cost    = dhl_cost(country, peak?) × qty              (€ gross, charged to us)
  shipping_cost_net = shipping_cost / (1 + vat[country])
  commission       = COMMISSION_RATE × gross_revenue             (default 16%)
  overhead         = (net_revenue + shipping_cost_net) × OVERHEAD_RATE   (10%)

  CM2              = CM1 − shipping_cost_net − commission − overhead
  CM3              = CM2 − allocated_ad_spend

Shipping revenue paid by the customer accrues to Shop Apotheke, not the
seller, so it is *not* added back into CM2.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

# Defaults match the workbook (`Margen Calc pharma`).
COMMISSION_RATE = 0.16      # ePharma marketplace commission, 16% of gross
OVERHEAD_RATE = 0.10        # Logistics overhead, 10% of (net sales + net shipping cost)
GB_COGS_FX = 0.83           # EUR→GBP adjustment applied to COGS when country == GB
PEAK_MONTHS = {11, 12}      # November + December → DHL peak surcharge


REPO_ROOT = Path(__file__).resolve().parent
INPUTS_DIR = REPO_ROOT / "inputs"


def load_vat() -> dict[str, float]:
    df = pd.read_csv(INPUTS_DIR / "vat_rates.csv")
    return dict(zip(df["country"], df["vat_rate"].astype(float)))


def load_dhl() -> pd.DataFrame:
    return pd.read_csv(INPUTS_DIR / "dhl_shipping.csv")


def load_cogs() -> pd.DataFrame:
    """One row per SKU: sku, unit_cogs, product_name."""
    df = pd.read_csv(INPUTS_DIR / "cogs.csv")
    df.columns = [c.strip().lower() for c in df.columns]
    df["unit_cogs"] = pd.to_numeric(df["unit_cogs"], errors="coerce").fillna(0)
    # Some SKUs appear multiple times in the JTL export (e.g. UMGEBUCHT
    # entries); keep the latest cost.
    return df.drop_duplicates(subset=["sku"], keep="last")


def load_shipping_revenue() -> dict[str, float]:
    df = pd.read_csv(INPUTS_DIR / "shop_apotheke_shipping_revenue.csv")
    return dict(zip(df["country"], df["shipping_revenue_gross"].astype(float)))


def is_peak(day: pd.Timestamp | date) -> bool:
    return pd.Timestamp(day).month in PEAK_MONTHS


def compute_line_margins(
    orders: pd.DataFrame,
    cogs: pd.DataFrame,
    vat: dict[str, float],
    dhl: pd.DataFrame,
    *,
    commission_rate: float = COMMISSION_RATE,
    overhead_rate: float = OVERHEAD_RATE,
) -> pd.DataFrame:
    """Add per-order-line economics columns to `orders`.

    Inputs:
      orders columns: period, order_id, sku, qty, gross_revenue,
                      refunded_amount, customer_country
      cogs columns:   sku, unit_cogs

    Returns the input frame with extra columns: vat_rate, net_revenue,
    product_cost, dhl_cost, shipping_cost_net, commission, overhead,
    CM1, CM2.
    """
    df = orders.merge(cogs[["sku", "unit_cogs"]], on="sku", how="left")
    df["unit_cogs"] = df["unit_cogs"].fillna(0)

    df["country"] = df["customer_country"].fillna("DE").str.upper()
    df["vat_rate"] = df["country"].map(vat).fillna(vat.get("DE", 0.07))
    df["is_peak"] = df["period"].apply(is_peak)

    dhl_lookup = dhl.set_index("country")
    def _dhl(country: str, peak: bool) -> float:
        if country not in dhl_lookup.index:
            return float(dhl_lookup.loc["DE", "cost_standard"])
        return float(dhl_lookup.loc[country, "cost_peak" if peak else "cost_standard"])

    df["dhl_unit"] = [
        _dhl(c, p) for c, p in zip(df["country"], df["is_peak"])
    ]

    df["net_revenue"] = (df["gross_revenue"] - df["refunded_amount"]) / (1 + df["vat_rate"])
    fx = df["country"].eq("GB").map({True: GB_COGS_FX, False: 1.0})
    df["product_cost"] = df["unit_cogs"] * df["qty"] * fx

    df["CM1"] = df["net_revenue"] - df["product_cost"]

    df["dhl_cost"] = df["dhl_unit"] * df["qty"]
    df["shipping_cost_net"] = df["dhl_cost"] / (1 + df["vat_rate"])
    df["commission"] = commission_rate * df["gross_revenue"]
    df["overhead"] = overhead_rate * (df["net_revenue"] + df["shipping_cost_net"])

    df["CM2"] = df["CM1"] - df["shipping_cost_net"] - df["commission"] - df["overhead"]
    return df


# Single-row "what-if" calc used by the dashboard's Margin Calculator tab.
def quote(
    *,
    gross_price: float,
    unit_cogs: float,
    country: str,
    qty: int = 1,
    discount: float = 0.0,
    target_price: float | None = None,
    peak: bool = False,
    commission_rate: float = COMMISSION_RATE,
    overhead_rate: float = OVERHEAD_RATE,
    roas: float | None = None,
) -> dict[str, float]:
    """Mirror of the Excel calculator: returns CM1/CM2/CM3 + %s for one SKU."""
    vat = load_vat().get(country, 0.07)
    dhl = load_dhl().set_index("country")

    if country == "GB":
        unit_cogs = unit_cogs * GB_COGS_FX

    eff_gross = target_price if target_price else gross_price * (1 - discount)
    sales_net = eff_gross / (1 + vat) * qty
    current_net = gross_price / (1 + vat) * qty  # denominator for % margins

    product_cost = unit_cogs * qty
    cm1 = sales_net - product_cost

    dhl_unit = float(dhl.loc[country, "cost_peak" if peak else "cost_standard"]
                     if country in dhl.index else dhl.loc["DE", "cost_standard"])
    dhl_cost = dhl_unit * qty
    ship_net = dhl_cost / (1 + vat)
    commission = commission_rate * gross_price * qty
    overhead = overhead_rate * (sales_net + ship_net)

    cm2 = cm1 - ship_net - commission - overhead

    ad_spend = (sales_net / roas) if roas else 0.0
    cm3 = cm2 - ad_spend

    return {
        "gross_price": gross_price,
        "vat_rate": vat,
        "net_price": gross_price / (1 + vat),
        "sales_price_net": sales_net,
        "product_cost": product_cost,
        "CM1": cm1, "CM1%": cm1 / current_net * 100 if current_net else 0,
        "dhl_cost": dhl_cost,
        "shipping_cost_net": ship_net,
        "commission": commission,
        "overhead": overhead,
        "CM2": cm2, "CM2%": cm2 / current_net * 100 if current_net else 0,
        "ad_spend": ad_spend,
        "CM3": cm3, "CM3%": cm3 / current_net * 100 if current_net else 0,
    }
