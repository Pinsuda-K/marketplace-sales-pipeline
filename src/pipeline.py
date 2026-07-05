"""
Marketplace Sales & GMV Reporting Pipeline
==========================================
Runnable port of a formula-first Google Sheets reporting back-end.

    raw_     (marketplace exports + one internal list)
        →   clean_    typed, dated, business rules applied, quality-flagged
        →   master_   single analysis-ready table, all sources joined
        →   out_      dashboard-ready aggregates

The production pattern lives in Google Sheets (ARRAYFORMULA / SUMIFS /
COUNTUNIQUEIFS / VLOOKUP), where every reporting number traces back through
strict linear references to the raw exports — which are never edited by hand.

This script reproduces the exact metric logic in pandas so it is reproducible
and auditable from code. Every Sheets formula it mirrors is documented in
docs/sheets_formula_crosswalk.md.

Run:  python src/pipeline.py
"""

from __future__ import annotations
import json
import re
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)


# ============================================================
# RAW  — five inputs. Four mirror marketplace seller-center exports;
# the exclusion_list is an internal business rule kept OUT of the raw
# exports on purpose (platform truth vs. internal decisions stay separate).
# ============================================================
def load_raw():
    return (
        pd.read_csv(DATA / "raw_order_transaction.csv"),
        pd.read_csv(DATA / "raw_product_catalog.csv"),
        pd.read_csv(DATA / "raw_voucher_usage.csv"),
        pd.read_csv(DATA / "raw_traffic_funnel.csv"),
        pd.read_csv(DATA / "raw_channel_master.csv"),
        pd.read_csv(DATA / "exclusion_list.csv"),
    )


# ============================================================
# Parsing helpers — one place for every field-level interpretation
# ============================================================
_DDMMYYYY = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})")


def _parse_price(x) -> float:
    """Handle '฿20,456' / '฿6,900' / '2345' / 2345 as float."""
    if pd.isna(x):
        return 0.0
    s = str(x).strip().replace("฿", "").replace(",", "").strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_date(x):
    """Handle mixed ISO and DD/MM/YYYY formats with trailing whitespace."""
    if pd.isna(x):
        return pd.NaT
    s = str(x).strip()
    m = _DDMMYYYY.match(s)
    if m:
        dd, mm, yyyy, hh, mi = m.groups()
        try:
            return pd.Timestamp(f"{yyyy}-{mm}-{dd} {hh}:{mi}:00")
        except Exception:
            return pd.NaT
    try:
        return pd.Timestamp(s)
    except Exception:
        return pd.NaT


# ============================================================
# CLEAN  — type the order export, dedupe, derive reporting flags once.
# Order grain is one row per item; a return adds a negative-quantity
# "reversal" row sharing the order_id.
# ============================================================
def clean_orders(orders, catalog, exclusions, diag):
    df = orders.copy()
    diag["raw_order_rows"] = len(df)

    # Exact-duplicate rows (export bug) — dedupe by order_item_id
    before = len(df)
    df = df.drop_duplicates(subset=["order_item_id"], keep="first")
    diag["duplicates_dropped"] = before - len(df)

    # Types
    df["unit_price_num"] = df["unit_price"].apply(_parse_price)
    df["created_ts"] = df["created_at"].apply(_parse_date)
    df["date"] = df["created_ts"].dt.date
    df["year_month"] = df["created_ts"].dt.strftime("%Y-%m")
    diag["unparseable_dates"] = int(df["created_ts"].isna().sum())

    # Business-rule flags (derived once, filtered downstream)
    excluded_customers = set(exclusions["customer_id"])
    catalog_skus = set(catalog["sku"])

    df["is_blank_sku"] = df["sku"].fillna("").str.strip().eq("")
    df["is_ghost_sku"] = (~df["sku"].isin(catalog_skus)) & (~df["is_blank_sku"])
    zero_priced = (df["unit_price_num"] == 0) & (df["quantity"] > 0)
    df["is_excluded"] = df["customer_id"].isin(excluded_customers) | zero_priced

    df["is_canceled"] = df["status"].eq("canceled")
    df["is_reversal"] = df["status"].eq("reversal")           # exclude reversal from "sale" count
    df["is_sale"] = ~df["is_reversal"]                        # sale-line flag (exclude refund rows)
    df["is_counted"] = (
        ~df["is_canceled"] & ~df["is_excluded"]
        & ~df["is_blank_sku"] & ~df["is_ghost_sku"]
    )

    # Line-level financials — quantity is negative on reversal rows so
    # line_gmv is automatically negative and nets the original sale.
    df["line_gmv"] = df["unit_price_num"] * df["quantity"]
    df["line_cogs"] = df["cost_price"] * df["quantity"]

    diag["blank_sku_rows"] = int(df["is_blank_sku"].sum())
    diag["ghost_sku_rows"] = int(df["is_ghost_sku"].sum())
    diag["excluded_rows"] = int(df["is_excluded"].sum())
    diag["canceled_rows"] = int(df["is_canceled"].sum())
    diag["reversal_rows"] = int(df["is_reversal"].sum())
    return df


def clean_vouchers(vouchers, orders_clean, diag):
    """Flag voucher redemptions that point at a now-canceled order."""
    v = vouchers.copy()
    canceled_ids = set(orders_clean.loc[orders_clean["is_canceled"], "order_id"])
    v["orphan_voucher"] = v["order_id"].isin(canceled_ids)
    diag["orphan_vouchers"] = int(v["orphan_voucher"].sum())
    return v


# ============================================================
# MASTER  — one analysis-ready table. Join catalog on sku, channel on
# channel_id, and attach voucher on order_id (excluding orphans).
# ============================================================
def build_master(clean_orders, catalog, channels, vouchers_clean):
    m = clean_orders.merge(
        catalog[["sku", "category", "brand", "list_price"]], on="sku", how="left"
    )
    m = m.merge(
        channels[["channel_id", "channel_name", "commission_rate", "is_own_store"]],
        on="channel_id", how="left",
    )
    v = vouchers_clean.loc[~vouchers_clean["orphan_voucher"], ["order_id", "voucher_id"]]
    m = m.merge(v.drop_duplicates(subset=["order_id"]), on="order_id", how="left")

    m["is_own_store_bool"] = m["is_own_store"].astype(str).str.upper().eq("TRUE")
    m["line_commission"] = m["line_gmv"] * m["commission_rate"].fillna(0)
    m["line_net_revenue"] = m["line_gmv"] - m["line_commission"]
    m["line_gross_profit"] = m["line_gmv"] - m["line_cogs"]
    m["line_contribution_margin"] = m["line_net_revenue"] - m["line_cogs"]
    return m


# ============================================================
# OUT  — dashboard-ready aggregates. Mirror the SUMIFS / COUNTUNIQUEIFS
# shape of the Sheets back-end. See docs/sheets_formula_crosswalk.md.
# ============================================================
def build_out(master, traffic, channels):
    counted = master.loc[master["is_counted"]].copy()
    sales = counted.loc[counted["is_sale"]].copy()

    # ---- kpi_summary  (SUMIFS on counted × sale; COUNTUNIQUEIFS on counted) ----
    gross_gmv = float(sales["line_gmv"].sum())
    net_revenue = float(counted["line_net_revenue"].sum())  # includes commissions AND reversals
    gross_profit = float(counted["line_gross_profit"].sum())
    contribution_margin = float(counted["line_contribution_margin"].sum())
    commission_paid = float(counted["line_commission"].sum())
    orders = int(counted["order_id"].nunique())
    units = int(sales["quantity"].sum())
    aov = round(gross_gmv / orders, 2) if orders else 0.0
    margin_pct = round(100 * contribution_margin / gross_gmv, 1) if gross_gmv else 0.0

    kpi = pd.DataFrame([{
        "period_start": str(counted["date"].min()),
        "period_end":   str(counted["date"].max()),
        "gross_gmv": gross_gmv,
        "commission_paid": commission_paid,
        "net_revenue": net_revenue,
        "gross_profit": gross_profit,
        "contribution_margin": contribution_margin,
        "margin_pct": margin_pct,
        "orders": orders,
        "unique_customers": int(counted["customer_id"].nunique()),
        "units_sold": units,
        "aov": aov,
    }])

    # ---- monthly trend ----
    monthly = (sales.groupby("year_month")
               .agg(gross_gmv=("line_gmv", "sum"),
                    units=("quantity", "sum"))
               .reset_index())
    net_m = (counted.groupby("year_month")["line_net_revenue"].sum()
             .rename("net_revenue").reset_index())
    margin_m = (counted.groupby("year_month")["line_contribution_margin"].sum()
                .rename("contribution_margin").reset_index())
    ord_m = (counted.groupby("year_month")["order_id"].nunique()
             .rename("orders").reset_index())
    monthly = (monthly.merge(net_m, on="year_month")
                      .merge(margin_m, on="year_month")
                      .merge(ord_m, on="year_month"))

    # ---- product performance ----
    product = (sales.groupby(["sku", "item_name", "category"])
               .agg(units=("quantity", "sum"),
                    gross_gmv=("line_gmv", "sum"),
                    net_revenue=("line_net_revenue", "sum"),
                    commission=("line_commission", "sum"),
                    contribution_margin=("line_contribution_margin", "sum"))
               .reset_index())
    product["margin_pct"] = (100 * product["contribution_margin"] /
                             product["gross_gmv"].replace(0, pd.NA)).fillna(0).round(1)
    product = product.sort_values("gross_gmv", ascending=False).reset_index(drop=True)

    # ---- channel performance ----
    channel = (counted.groupby(["channel_id", "channel_name", "is_own_store_bool"])
               .agg(gross_gmv=("line_gmv", "sum"),
                    commission=("line_commission", "sum"),
                    net_revenue=("line_net_revenue", "sum"),
                    contribution_margin=("line_contribution_margin", "sum"),
                    orders=("order_id", "nunique"),
                    units=("quantity", "sum"))
               .reset_index())
    channel["margin_pct"] = (100 * channel["contribution_margin"] /
                             channel["gross_gmv"].replace(0, pd.NA)).fillna(0).round(1)
    channel["aov"] = (channel["gross_gmv"] / channel["orders"].replace(0, pd.NA)).fillna(0).round(2)

    # ---- promotion effect (dedupe to order grain so discount is not multiplied) ----
    order_frame = (sales.groupby("order_id")
                   .agg(gmv=("line_gmv", "sum"),
                        units=("quantity", "sum"),
                        promo_label=("promo_label", "first"))
                   .reset_index())
    order_frame["is_promo"] = order_frame["promo_label"].fillna("").astype(str).str.strip().ne("")
    promotion = (order_frame.groupby("is_promo")
                 .agg(orders=("order_id", "nunique"),
                      gmv=("gmv", "sum"),
                      units=("units", "sum"))
                 .reset_index())
    promotion["aov"] = (promotion["gmv"] / promotion["orders"]).round(2)
    promotion["label"] = promotion["is_promo"].map({True: "Promotion", False: "No Promotion"})
    promotion = promotion[["label", "orders", "gmv", "units", "aov"]]

    # ---- conversion funnel (traffic → view → ATC → paid) ----
    total_paid = int(traffic["paid_orders"].sum())
    funnel = pd.DataFrame([
        {"stage": "Visitors",      "count": int(traffic["visitors"].sum())},
        {"stage": "Product Views", "count": int(traffic["views"].sum())},
        {"stage": "Add to Cart",   "count": int(traffic["add_to_cart"].sum())},
        {"stage": "Orders (Paid)", "count": total_paid if total_paid else orders},
    ])

    return kpi, monthly, product, channel, promotion, funnel


# ============================================================
# ORCHESTRATE
# ============================================================
def _round_numeric(df):
    for c in df.select_dtypes(include="number").columns:
        df[c] = df[c].round(2)
    return df


def main():
    diag: dict = {}
    print("[1/4] Loading raw exports…")
    orders, catalog, vouchers, traffic, channels, exclusions = load_raw()

    print("[2/4] Cleaning + deriving flags…")
    clean = clean_orders(orders, catalog, exclusions, diag)
    vouchers_c = clean_vouchers(vouchers, clean, diag)
    clean.to_csv(OUT / "clean_order_item.csv", index=False)

    print("[3/4] Building master + out layers…")
    master = build_master(clean, catalog, channels, vouchers_c)
    master.to_csv(OUT / "master_order_item.csv", index=False)

    kpi, monthly, product, channel, promotion, funnel = build_out(master, traffic, channels)
    _round_numeric(kpi).to_csv(OUT / "out_kpi_summary.csv", index=False)
    _round_numeric(monthly).to_csv(OUT / "out_monthly.csv", index=False)
    _round_numeric(product).to_csv(OUT / "out_product.csv", index=False)
    _round_numeric(channel).to_csv(OUT / "out_channel.csv", index=False)
    _round_numeric(promotion).to_csv(OUT / "out_promotion.csv", index=False)
    funnel.to_csv(OUT / "out_funnel.csv", index=False)

    # ---- dashboard payload ----
    payload = {
        "kpi": kpi.iloc[0].to_dict(),
        "monthly": monthly.to_dict(orient="records"),
        "product": product.to_dict(orient="records"),
        "channel": channel.to_dict(orient="records"),
        "promotion": promotion.to_dict(orient="records"),
        "funnel": funnel.to_dict(orient="records"),
        "data_quality": diag,
    }
    (OUT / "dashboard_data.json").write_text(json.dumps(payload, indent=2, default=str))

    print("\n[4/4] Pipeline complete.\n")
    print("=" * 60)
    print("DATA QUALITY DIAGNOSTICS")
    print("=" * 60)
    for k, v in diag.items():
        print(f"  {k:<22} {v:>8,}")
    print()
    print("=" * 60)
    print("KPI SUMMARY")
    print("=" * 60)
    k = kpi.iloc[0]
    print(f"  Period:              {k['period_start']} → {k['period_end']}")
    print(f"  Gross GMV:           ฿{k['gross_gmv']:>15,.2f}")
    print(f"  Commission paid:     ฿{k['commission_paid']:>15,.2f}")
    print(f"  Net revenue:         ฿{k['net_revenue']:>15,.2f}")
    print(f"  Gross profit:        ฿{k['gross_profit']:>15,.2f}")
    print(f"  Contribution margin: ฿{k['contribution_margin']:>15,.2f}  ({k['margin_pct']}%)")
    print(f"  Orders (unique):     {int(k['orders']):>16,}")
    print(f"  Unique customers:    {int(k['unique_customers']):>16,}")
    print(f"  Units sold:          {int(k['units_sold']):>16,}")
    print(f"  AOV:                 ฿{k['aov']:>15,.2f}")
    print(f"\nOutputs written to: {OUT}")


if __name__ == "__main__":
    main()
