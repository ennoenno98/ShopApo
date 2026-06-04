"""ePharma (Shop Apotheke) margin model.

Mirrors the `Margen Calc pharma` sheet from Margin_Check_V5.xlsx, with
the 10% logistics overhead replaced by the 3PL rate card from the
AP26 plan (`Logistics 3PL`, rows 41–52). Rates live in
`inputs/three_pl_rates.csv` so they can be tuned without code changes.

Per-line economics
------------------
  net_price        = gross_price / (1 + vat[country])
  net_revenue      = net_price × qty − refunds_net
  product_cost     = unit_cogs × qty             (× 0.83 if country == GB)

  CM1              = net_revenue − product_cost                  (gross product margin)

  shipping_cost    = dhl_cost(country, peak?), charged ONCE per order
                     and split across an order's lines by units-share
  shipping_cost_net = shipping_cost / (1 + vat[country])
  commission       = COMMISSION_RATE × gross_revenue             (default 16%)
  three_pl_cost    = per-order fixed (€2.21, spread across lines by qty)
                     + pick cost per line (0.23 first + 0.19 × extra units)

  CM2              = CM1 − shipping_cost_net − commission − three_pl_cost
  CM3              = CM2 − allocated_ad_spend

Shipping revenue paid by the customer accrues to Shop Apotheke, not the
seller, so it is *not* added back into CM2.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

# Defaults match the workbook (`Margen Calc pharma` + `Logistics 3PL`).
COMMISSION_RATE = 0.16      # ePharma marketplace commission, 16% of gross
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


def load_3pl_rates() -> dict[str, float]:
    """Per-order and per-pick 3PL rates from `inputs/three_pl_rates.csv`."""
    df = pd.read_csv(INPUTS_DIR / "three_pl_rates.csv")
    return dict(zip(df["component"], df["rate_eur"].astype(float)))


def is_peak(day: pd.Timestamp | date) -> bool:
    return pd.Timestamp(day).month in PEAK_MONTHS


def _three_pl_per_line(qty: pd.Series, order_qty: pd.Series, rates: dict[str, float]) -> pd.Series:
    """Per-order-line 3PL cost.

    Per-order fixed costs (handling, consolidation, pack/ship, packaging,
    filling) are charged once per order and split across that order's
    lines in proportion to their units. Pick costs are line-level:
    one "first pick" per (order, sku) plus N-1 "additional picks" for
    further units of the same SKU.
    """
    per_order_fixed = (
        rates["handling"] + rates["consolidation"] + rates["pack_shipout"]
        + rates["packaging"] + rates["filling"]
    )
    line_share = qty / order_qty.replace(0, pd.NA)
    line_fixed = per_order_fixed * line_share.fillna(0)
    line_picks = rates["pick_first"] + rates["pick_additional"] * (qty - 1).clip(lower=0)
    return line_fixed + line_picks


def compute_line_margins(
    orders: pd.DataFrame,
    cogs: pd.DataFrame,
    vat: dict[str, float],
    dhl: pd.DataFrame,
    *,
    commission_rate: float = COMMISSION_RATE,
    three_pl_rates: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Add per-order-line economics columns to `orders`.

    Inputs:
      orders columns: period, order_id, sku, qty, gross_revenue,
                      refunded_amount, customer_country
      cogs columns:   sku, unit_cogs

    Returns the input frame with extra columns: vat_rate, net_revenue,
    product_cost, dhl_cost, shipping_cost_net, commission, three_pl_cost,
    CM1, CM2.
    """
    if three_pl_rates is None:
        three_pl_rates = load_3pl_rates()

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

    # DHL bills ONE parcel per order, no matter how many units are inside.
    # Split that parcel cost across an order's line items by units-share,
    # same approach we use for per-order 3PL fixed fees below.
    order_qty = df.groupby("order_id")["qty"].transform("sum")
    line_share = (df["qty"] / order_qty.replace(0, pd.NA)).fillna(0)
    df["dhl_cost"] = df["dhl_unit"] * line_share
    df["shipping_cost_net"] = df["dhl_cost"] / (1 + df["vat_rate"])
    df["commission"] = commission_rate * df["gross_revenue"]

    df["three_pl_cost"] = _three_pl_per_line(df["qty"], order_qty, three_pl_rates)

    df["CM2"] = df["CM1"] - df["shipping_cost_net"] - df["commission"] - df["three_pl_cost"]
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
    roas: float | None = None,
) -> dict[str, float]:
    """Mirror of the Excel calculator: returns CM1/CM2/CM3 + %s for one SKU.

    Assumes a single-line order (one SKU): the full per-order fixed 3PL
    cost lands on this line.
    """
    vat = load_vat().get(country, 0.07)
    dhl = load_dhl().set_index("country")
    rates = load_3pl_rates()

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

    per_order_fixed = (rates["handling"] + rates["consolidation"]
                       + rates["pack_shipout"] + rates["packaging"]
                       + rates["filling"])
    pick = rates["pick_first"] + rates["pick_additional"] * max(0, qty - 1)
    three_pl_cost = per_order_fixed + pick

    cm2 = cm1 - ship_net - commission - three_pl_cost

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
        "three_pl_cost": three_pl_cost,
        "CM2": cm2, "CM2%": cm2 / current_net * 100 if current_net else 0,
        "ad_spend": ad_spend,
        "CM3": cm3, "CM3%": cm3 / current_net * 100 if current_net else 0,
    }
