"""
Marketplace Sales & GMV Reporting Pipeline
==========================================
Layered, formula-first data pipeline for a specialty coffee equipment storefront.

Layers
------
raw_    → exports as they came from the marketplace / own-store systems (untouched)
clean_  → typed, deduped, flag-derived (canceled / excluded / return-reversal / sale)
master_ → analysis-ready join (orders ⋈ catalog ⋈ channel ⋈ vouchers)
out_    → business marts: channel_perf, product_econ, funnel_retention

Why layered
-----------
- Raw is never mutated by hand → any downstream number is reproducible from source.
- Cleaning rules live in ONE place → easy to audit and change.
- Business logic lives in the master + out layers → not tangled with cleanup.

Data quality issues this pipeline is designed to handle
-------------------------------------------------------
See docs/data_quality_notes.md for the full inventory. Summary:
  1. Multi-item orders (order_id repeats across line items)
  2. Cancellations (row present but must not count as revenue)
  3. Returns with signed-negative reversal rows
  4. Internal QA / staff orders (via exclusion_list.csv)
  5. Exact-duplicate rows (export bug)
  6. Mixed date formats (ISO / DD-MM-YYYY / trailing whitespace)
  7. Currency strings with "฿" prefix and thousand-separators
  8. Blank SKU rows
  9. Orders referencing SKUs that don't exist in the catalog
 10. Voucher redemptions attached to canceled orders

The pipeline reports counts for each caught issue in the summary.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

# ------------------------- paths -------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
OUT.mkdir(parents=True, exist_ok=True)


# ------------------------- helpers -----------------------
def _parse_price(x) -> float:
    """Handle '฿20,456' / '฿6,900' / '2345' / 2345 / '  ฿1,200 ' all as float."""
    if pd.isna(x):
        return 0.0
    s = str(x).strip().replace("฿", "").replace(",", "").strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


_DDMMYYYY = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})")


def _parse_date(x) -> pd.Timestamp:
    """Handle '2025-05-01 10:37:00' and '01/05/2025 10:13  ' (with trailing whitespace)."""
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


# ------------------------- clean layer -----------------------
def build_clean(diag: dict) -> dict[str, pd.DataFrame]:
    """
    Read raw_ files, type them, dedupe, and derive reporting flags.
    Returns a dict of clean_ frames.
    """
    raw_orders = pd.read_csv(DATA / "raw_order_transaction.csv")
    raw_catalog = pd.read_csv(DATA / "raw_product_catalog.csv")
    raw_channels = pd.read_csv(DATA / "raw_channel_master.csv")
    raw_vouchers = pd.read_csv(DATA / "raw_voucher_usage.csv")
    raw_traffic = pd.read_csv(DATA / "raw_traffic_funnel.csv")
    exclusions = pd.read_csv(DATA / "exclusion_list.csv")

    diag["raw_order_rows"] = len(raw_orders)

    # ---- orders: dedup exact duplicates ----
    before = len(raw_orders)
    raw_orders = raw_orders.drop_duplicates(subset=["order_item_id"], keep="first")
    diag["duplicates_dropped"] = before - len(raw_orders)

    # ---- orders: parse types ----
    raw_orders["unit_price_num"] = raw_orders["unit_price"].apply(_parse_price)
    raw_orders["created_ts"] = raw_orders["created_at"].apply(_parse_date)
    raw_orders["date"] = raw_orders["created_ts"].dt.date
    diag["unparseable_dates"] = int(raw_orders["created_ts"].isna().sum())

    # ---- orders: flag rows we won't count ----
    # Blank SKU rows
    raw_orders["is_blank_sku"] = raw_orders["sku"].fillna("").str.strip().eq("")
    diag["blank_sku_rows"] = int(raw_orders["is_blank_sku"].sum())

    # SKUs not in catalog (broken join)
    catalog_skus = set(raw_catalog["sku"])
    raw_orders["is_ghost_sku"] = ~raw_orders["sku"].isin(catalog_skus) & ~raw_orders["is_blank_sku"]
    diag["ghost_sku_rows"] = int(raw_orders["is_ghost_sku"].sum())

    # Internal / excluded customers
    excluded_ids = set(exclusions["customer_id"])
    raw_orders["is_excluded"] = raw_orders["customer_id"].isin(excluded_ids)

    # Zero-price rows are also QA signals (belt-and-braces with the exclusion list)
    zero_priced = (raw_orders["unit_price_num"] == 0) & (raw_orders["quantity"] > 0)
    raw_orders["is_excluded"] = raw_orders["is_excluded"] | zero_priced
    diag["excluded_customer_rows"] = int(raw_orders["is_excluded"].sum())

    # Cancellation and return flags — driven by status
    raw_orders["is_canceled"] = raw_orders["status"].eq("canceled")
    raw_orders["is_reversal"] = raw_orders["status"].eq("reversal")
    raw_orders["is_returned_order"] = raw_orders["status"].eq("returned")

    # A row "counts as a sale" iff:
    #   - status is delivered OR returned (returned still generated revenue then netted)
    #   - not canceled, not internal QA, not blank/ghost SKU
    # Reversal rows have negative qty and count as sales too (they NET the return)
    raw_orders["is_countable"] = (
        raw_orders["status"].isin(["delivered", "returned", "reversal"])
        & ~raw_orders["is_excluded"]
        & ~raw_orders["is_blank_sku"]
        & ~raw_orders["is_ghost_sku"]
    )

    # Line-level financials
    raw_orders["line_gmv"] = raw_orders["quantity"] * raw_orders["unit_price_num"]
    raw_orders["line_cogs"] = raw_orders["quantity"] * raw_orders["cost_price"]
    raw_orders["line_gross_profit"] = raw_orders["line_gmv"] - raw_orders["line_cogs"]

    clean_orders = raw_orders

    # ---- vouchers: keep, but flag ones that point at now-canceled orders ----
    canceled_ids = set(clean_orders.loc[clean_orders["is_canceled"], "order_id"])
    raw_vouchers["orphan_voucher"] = raw_vouchers["order_id"].isin(canceled_ids)
    diag["orphan_vouchers"] = int(raw_vouchers["orphan_voucher"].sum())

    # ---- traffic + catalog + channels are already typed; keep as-is ----
    return {
        "clean_orders": clean_orders,
        "clean_catalog": raw_catalog,
        "clean_channels": raw_channels,
        "clean_vouchers": raw_vouchers,
        "clean_traffic": raw_traffic,
    }


# ------------------------- master layer ---------------------
def build_master(clean: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Join orders ⋈ catalog ⋈ channels ⋈ vouchers into one analysis-ready table."""
    o = clean["clean_orders"]
    cat = clean["clean_catalog"][["sku", "category", "brand", "list_price"]]
    ch = clean["clean_channels"][["channel_id", "channel_name", "commission_rate", "is_own_store"]]
    v = clean["clean_vouchers"].loc[~clean["clean_vouchers"]["orphan_voucher"], ["order_id", "voucher_id"]]

    m = o.merge(cat, on="sku", how="left")
    m = m.merge(ch, on="channel_id", how="left")
    v_dedup = v.drop_duplicates(subset=["order_id"], keep="first")
    m = m.merge(v_dedup, on="order_id", how="left")

    # Channel margin logic — commission is charged on GMV for marketplaces
    m["is_own_store_bool"] = m["is_own_store"].astype(str).str.upper().eq("TRUE")
    m["line_commission"] = m["line_gmv"] * m["commission_rate"].fillna(0)
    m["line_net_revenue"] = m["line_gmv"] - m["line_commission"]
    m["line_contribution_margin"] = m["line_net_revenue"] - m["line_cogs"]

    return m


# ------------------------- out layer ------------------------
def build_out(master: pd.DataFrame, clean: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Build the three business marts."""
    cnt = master.loc[master["is_countable"]].copy()

    # ---------- Mart 1: Channel Performance ----------
    channel_perf = cnt.groupby(
        ["channel_id", "channel_name", "is_own_store_bool"], as_index=False
    ).agg(
        gmv=("line_gmv", "sum"),
        net_revenue=("line_net_revenue", "sum"),
        commission_paid=("line_commission", "sum"),
        cogs=("line_cogs", "sum"),
        contribution_margin=("line_contribution_margin", "sum"),
        units_sold=("quantity", "sum"),
        orders=("order_id", "nunique"),
    )
    channel_perf["margin_pct"] = (
        channel_perf["contribution_margin"] / channel_perf["gmv"].replace(0, pd.NA)
    ).fillna(0) * 100
    channel_perf["aov"] = (
        channel_perf["gmv"] / channel_perf["orders"].replace(0, pd.NA)
    ).fillna(0)

    # ---------- Mart 2: Product Economics ----------
    prod_econ = cnt.groupby(
        ["sku", "item_name", "category", "brand"], as_index=False
    ).agg(
        gmv=("line_gmv", "sum"),
        net_revenue=("line_net_revenue", "sum"),
        commission_paid=("line_commission", "sum"),
        cogs=("line_cogs", "sum"),
        contribution_margin=("line_contribution_margin", "sum"),
        units_sold=("quantity", "sum"),
    )
    prod_econ["margin_pct"] = (
        prod_econ["contribution_margin"] / prod_econ["gmv"].replace(0, pd.NA)
    ).fillna(0) * 100
    prod_econ = prod_econ.sort_values("net_revenue", ascending=False).reset_index(drop=True)

    # ---------- Mart 3: Funnel + Retention ----------
    traffic = clean["clean_traffic"].copy()
    traffic["date"] = pd.to_datetime(traffic["date"]).dt.date

    funnel = traffic.groupby("channel_id", as_index=False).agg(
        visitors=("visitors", "sum"),
        views=("views", "sum"),
        add_to_cart=("add_to_cart", "sum"),
        paid_orders=("paid_orders", "sum"),
    )
    funnel = funnel.merge(
        clean["clean_channels"][["channel_id", "channel_name"]], on="channel_id", how="left"
    )
    funnel["view_rate"] = (funnel["views"] / funnel["visitors"].replace(0, pd.NA)).fillna(0) * 100
    funnel["atc_rate"] = (funnel["add_to_cart"] / funnel["views"].replace(0, pd.NA)).fillna(0) * 100
    funnel["conv_rate"] = (funnel["paid_orders"] / funnel["visitors"].replace(0, pd.NA)).fillna(0) * 100

    # Customer retention: repeat-order rate per channel
    cust = cnt.groupby(["channel_id", "customer_id"], as_index=False)["order_id"].nunique()
    cust.rename(columns={"order_id": "orders_per_customer"}, inplace=True)
    retention = cust.groupby("channel_id", as_index=False).agg(
        total_customers=("customer_id", "nunique"),
        repeat_customers=("orders_per_customer", lambda s: int((s >= 2).sum())),
    )
    retention["repeat_rate_pct"] = (
        retention["repeat_customers"] / retention["total_customers"].replace(0, pd.NA)
    ).fillna(0) * 100

    funnel = funnel.merge(retention, on="channel_id", how="left")

    # ---------- Monthly summary (used by dashboards) ----------
    cnt["month"] = pd.to_datetime(cnt["date"]).dt.to_period("M").astype(str)
    monthly = cnt.groupby("month", as_index=False).agg(
        gmv=("line_gmv", "sum"),
        net_revenue=("line_net_revenue", "sum"),
        contribution_margin=("line_contribution_margin", "sum"),
        orders=("order_id", "nunique"),
    )

    # ---------- Promo impact ----------
    cnt["is_promo"] = cnt["promo_label"].fillna("").astype(str).str.strip().ne("")
    promo_impact = cnt.groupby("is_promo", as_index=False).agg(
        gmv=("line_gmv", "sum"),
        orders=("order_id", "nunique"),
        units=("quantity", "sum"),
    )
    promo_impact["aov"] = (
        promo_impact["gmv"] / promo_impact["orders"].replace(0, pd.NA)
    ).fillna(0)
    promo_impact["label"] = promo_impact["is_promo"].map({True: "Promotion", False: "No Promotion"})

    return {
        "out_channel_perf": channel_perf,
        "out_product_econ": prod_econ,
        "out_funnel_retention": funnel,
        "out_monthly": monthly,
        "out_promo_impact": promo_impact[["label", "gmv", "orders", "units", "aov"]],
    }


# ------------------------- orchestrate ---------------------
def _round_numeric(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.select_dtypes(include="number").columns:
        df[c] = df[c].round(2)
    return df


def main():
    diag: dict = {}
    print("[1/4] Reading raw + building clean layer…")
    clean = build_clean(diag)
    for name, df in clean.items():
        df.to_csv(OUT / f"{name}.csv", index=False)

    print("[2/4] Building master layer…")
    master = build_master(clean)
    master.to_csv(OUT / "master_orders.csv", index=False)

    print("[3/4] Building out layer…")
    outs = build_out(master, clean)
    for name, df in outs.items():
        _round_numeric(df).to_csv(OUT / f"{name}.csv", index=False)

    # ---------- Summary KPIs (used by dashboards) ----------
    cnt = master.loc[master["is_countable"]]
    kpi = {
        "period": {
            "start": str(cnt["date"].min()),
            "end": str(cnt["date"].max()),
        },
        "gmv": float(cnt["line_gmv"].sum()),
        "net_revenue": float(cnt["line_net_revenue"].sum()),
        "contribution_margin": float(cnt["line_contribution_margin"].sum()),
        "commission_paid": float(cnt["line_commission"].sum()),
        "orders": int(cnt["order_id"].nunique()),
        "unique_customers": int(cnt["customer_id"].nunique()),
        "units_sold": int(cnt["quantity"].sum()),
        "margin_pct": float(cnt["line_contribution_margin"].sum() / cnt["line_gmv"].sum() * 100),
        "aov": float(cnt["line_gmv"].sum() / cnt["order_id"].nunique()),
        "data_quality": diag,
    }
    (OUT / "kpi_summary.json").write_text(json.dumps(kpi, indent=2, default=str))

    # ---------- Data for dashboards ----------
    dash_payload = {
        "kpi": kpi,
        "channel_perf": outs["out_channel_perf"].to_dict(orient="records"),
        "product_econ": outs["out_product_econ"].to_dict(orient="records"),
        "funnel_retention": outs["out_funnel_retention"].to_dict(orient="records"),
        "monthly": outs["out_monthly"].to_dict(orient="records"),
        "promo_impact": outs["out_promo_impact"].to_dict(orient="records"),
    }
    (OUT / "dashboard_data.json").write_text(json.dumps(dash_payload, indent=2, default=str))

    # ---------- Print the summary ----------
    print("\n[4/4] Pipeline complete.\n")
    print("=" * 60)
    print("DATA QUALITY DIAGNOSTICS")
    print("=" * 60)
    print(f"  Raw rows read:            {diag['raw_order_rows']:,}")
    print(f"  Exact duplicates dropped: {diag['duplicates_dropped']:,}")
    print(f"  Unparseable dates:        {diag['unparseable_dates']:,}")
    print(f"  Blank-SKU rows:           {diag['blank_sku_rows']:,}")
    print(f"  Ghost-SKU rows (no cat):  {diag['ghost_sku_rows']:,}")
    print(f"  Excluded customer rows:   {diag['excluded_customer_rows']:,}")
    print(f"  Orphan vouchers:          {diag['orphan_vouchers']:,}")
    print()
    print("=" * 60)
    print("BUSINESS KPIs")
    print("=" * 60)
    print(f"  Period:                {kpi['period']['start']} → {kpi['period']['end']}")
    print(f"  GMV:                   ฿{kpi['gmv']:>15,.2f}")
    print(f"  Commission paid:       ฿{kpi['commission_paid']:>15,.2f}")
    print(f"  Net revenue:           ฿{kpi['net_revenue']:>15,.2f}")
    print(f"  Contribution margin:   ฿{kpi['contribution_margin']:>15,.2f}  ({kpi['margin_pct']:.1f}%)")
    print(f"  Orders (unique):       {kpi['orders']:>16,}")
    print(f"  Unique customers:      {kpi['unique_customers']:>16,}")
    print(f"  Units sold:            {kpi['units_sold']:>16,}")
    print(f"  AOV:                   ฿{kpi['aov']:>15,.2f}")
    print()
    print(f"Outputs written to: {OUT}")


if __name__ == "__main__":
    main()
