"""
(REF) Marketplace Sales & GMV Reporting Pipeline

Runnable port of a formula-first Google Sheets reporting back-end:
    raw_ (4 marketplace exports + 1 internal list)
        >> clean_   (typed, dated, business rules applied)
        >> master_  (single analysis-ready table, all sources joined)
        >> out_     (dashboard-ready aggregates)

Production system lives in Google Sheets (ARRAYFORMULA / COUNTUNIQUEIFS /
SUMIF / SUMIFS) feeding a dashboard. Every reporting number traces back through
linear references to the raw exports, which are never edited by hand. This
script reproduces the exact metric logic on mock data so it is reproducible and
auditable from code. See docs/sheets_formula_crosswalk.md for the formula map.

Run:  python src/pipeline.py
"""

from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)


# RAW (five inputs) Four mirror marketplace seller-center exports; the exclusion list is an internal business rule kept OUT of the raw exports on purpose (platform truth vs. internal decisions stay separate)
def load_raw():
    return (
        pd.read_csv(DATA / "raw_order_transaction.csv"),
        pd.read_csv(DATA / "raw_product_catalog.csv"),
        pd.read_csv(DATA / "raw_voucher_usage.csv"),
        pd.read_csv(DATA / "raw_traffic_funnel.csv"),
        pd.read_csv(DATA / "exclusion_list.csv"),
    )


# CLEAN >>> type/date the order export and derive the reporting flags once
# Order grain is one row per item; a return adds a negative "refund" row sharing the orderNumber. Flags DERIVED here (raw stays platform-native)
def clean_orders(orders, exclusions):
    df = orders.copy()
    df["createTime"] = pd.to_datetime(df["createTime"])
    df["year_month"] = df["createTime"].dt.strftime("%Y-%m")

    excluded = set(exclusions["orderNumber"])
    df["is_excluded"] = df["orderNumber"].isin(excluded).astype(int)      # internal rule
    df["is_canceled"] = (df["status"] == "canceled").astype(int)          # platform state
    df["is_sale"] = (df["status"] != "refund").astype(int)               # exclude reversal rows
    df["is_counted"] = ((df["is_canceled"] == 0) & (df["is_excluded"] == 0)).astype(int)

    # line GMV = sellingPrice * quantity. quantity is negative on refund rows,
    # so the line GMV is automatically negative and nets the original sale
    df["line_gmv"] = df["sellingPrice"] * df["quantity"]
    return df


# MASTER >>> one analysis-ready table. Join catalog on sellerSku (item granular)
# for category/brand/cost, then attach order-level promotion context
def build_master(clean, catalog, vouchers):
    cat = catalog[["sellerSku", "category", "brand", "costPrice"]]
    m = clean.merge(cat, on="sellerSku", how="left")             # item-grain join
    m["line_cost"] = m["costPrice"] * m["quantity"]
    m["line_profit"] = (m["sellingPrice"] - m["costPrice"]) * m["quantity"]

    promo = (vouchers.groupby("orderNumber")
             .agg(discountAmount=("discountAmount", "sum"),
                  campaign=("campaign", "first"))
             .reset_index())
    m = m.merge(promo, on="orderNumber", how="left")             # order-grain attach
    m["is_promo"] = m["campaign"].notna().astype(int)
    m["campaign"] = m["campaign"].fillna("No Promotion")
    m["discountAmount"] = m["discountAmount"].fillna(0)
    return m


# OUT = dashboard-ready aggregates (the dashboard scorecards read these)
def build_out(master, traffic):
    counted = master[master["is_counted"] == 1]
    sales = counted[counted["is_sale"] == 1]

    gross_gmv = sales["line_gmv"].sum()        # SUMIFS(gmv, counted, sale)
    net_revenue = counted["line_gmv"].sum()    # SUMIF(counted) incl. refund rows
    gross_profit = counted["line_profit"].sum()
    orders = counted["orderNumber"].nunique()  # COUNTUNIQUEIFS(orderNumber, counted)
    units = int(sales["quantity"].sum())
    aov = round(net_revenue / orders, 2) if orders else 0.0
    margin_pct = round(100 * gross_profit / net_revenue, 1) if net_revenue else 0.0

    kpi = pd.DataFrame([{
        "gross_gmv": gross_gmv, "net_revenue": net_revenue,
        "gross_profit": gross_profit, "margin_pct": margin_pct,
        "orders": orders, "units_sold": units, "aov": aov,
    }])

    # ---- monthly trend ----
    monthly = (sales.groupby("year_month")
               .agg(gross_gmv=("line_gmv", "sum"), units=("quantity", "sum"))
               .reset_index())
    net_m = counted.groupby("year_month")["line_gmv"].sum().rename("net_revenue").reset_index()
    ord_m = counted.groupby("year_month")["orderNumber"].nunique().rename("orders").reset_index()
    monthly = monthly.merge(net_m, on="year_month").merge(ord_m, on="year_month")

    # ---- product performance (with margin) ----
    product = (sales.groupby(["sellerSku", "itemName", "category"])
               .agg(units=("quantity", "sum"), gross_gmv=("line_gmv", "sum"),
                    gross_profit=("line_profit", "sum"))
               .reset_index())
    product["margin_pct"] = (100 * product["gross_profit"] / product["gross_gmv"]).round(1)
    product = product.sort_values("gross_gmv", ascending=False)

    # ---- promotion effect (dedupe to order grain so discount is not multiplied) ----
    order_frame = (sales.groupby("orderNumber")
                   .agg(gmv=("line_gmv", "sum"), units=("quantity", "sum"),
                        is_promo=("is_promo", "first"), campaign=("campaign", "first"),
                        discount=("discountAmount", "first"))
                   .reset_index())
    promotion = (order_frame.groupby(["is_promo", "campaign"])
                 .agg(orders=("orderNumber", "nunique"), gmv=("gmv", "sum"),
                      units=("units", "sum"), total_discount=("discount", "sum"))
                 .reset_index())
    promotion["aov"] = (promotion["gmv"] / promotion["orders"]).round(2)

    # ---- conversion funnel (traffic -> view -> ATC -> paid) ----
    funnel = pd.DataFrame([
        {"stage": "Visitors", "count": int(traffic["visitors"].sum())},
        {"stage": "Product Views", "count": int(traffic["productViews"].sum())},
        {"stage": "Add to Cart", "count": int(traffic["addToCart"].sum())},
        {"stage": "Orders (Paid)", "count": orders},
    ])

    return kpi, monthly, product, promotion, funnel


def main():
    orders, catalog, vouchers, traffic, exclusions = load_raw()
    clean = clean_orders(orders, exclusions)
    master = build_master(clean, catalog, vouchers)
    kpi, monthly, product, promotion, funnel = build_out(master, traffic)

    clean.to_csv(OUT / "clean_order_item.csv", index=False)
    master.to_csv(OUT / "master_order_item.csv", index=False)
    kpi.to_csv(OUT / "out_kpi_summary.csv", index=False)
    monthly.to_csv(OUT / "out_monthly.csv", index=False)
    product.to_csv(OUT / "out_product.csv", index=False)
    promotion.to_csv(OUT / "out_promotion.csv", index=False)
    funnel.to_csv(OUT / "out_funnel.csv", index=False)

    for name, frame in [("out_kpi_summary", kpi), ("out_monthly", monthly),
                        ("out_product", product), ("out_promotion", promotion),
                        ("out_funnel", funnel)]:
        print(f"=== {name} ===")
        print(frame.to_string(index=False))
        print()


if __name__ == "__main__":
    main()
