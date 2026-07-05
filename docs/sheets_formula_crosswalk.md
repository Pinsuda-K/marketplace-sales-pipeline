# Sheets ↔ pandas formula crosswalk

The production back-end is a **formula-first Google Sheets system** — no black-box scripts for the metrics. Every reporting number traces back through strict linear references to the raw exports, which are never edited by hand.

This file maps each metric to (a) the Google Sheets formula used in the workbook and (b) the pandas equivalent in [`src/pipeline.py`](../src/pipeline.py). The two implementations produce identical numbers.

**Layer model:** `raw_` → `clean_` → `master_` → `out_`

## Inputs

Five raw inputs — four mirror what marketplace seller-center exports look like, plus one internal list kept separate so platform truth and business decisions never get mixed together.

| File | Grain | Feeds |
|---|---|---|
| `raw_order_transaction` | one row per order item | GMV, revenue, orders, units |
| `raw_product_catalog` | one row per SKU | category, brand, cost → margin |
| `raw_channel_master` | one row per channel | commission rate, own-store flag |
| `raw_voucher_usage` | one row per voucher redemption | promotion effect |
| `raw_traffic_funnel` | date × channel | conversion funnel |
| `exclusion_list` *(internal)* | one row per excluded customer | business-rule filtering |

---

## Clean layer — flags derived once, used everywhere

Cleaning does the heavy lifting: parses types, derives every reporting flag from raw fields, and lets everything downstream be a simple `SUMIFS` or `groupby`. The same flag names appear in both implementations.

| Flag | What it means | Google Sheets | pandas |
|---|---|---|---|
| `is_excluded` | Internal / QA / staff order | `=ARRAYFORMULA(ISNUMBER(MATCH(customer_id, exclusion!A:A, 0)))` | `df["customer_id"].isin(excluded_set)` |
| `is_canceled` | Platform state = canceled | `=ARRAYFORMULA(status="canceled")` | `df["status"].eq("canceled")` |
| `is_reversal` | Signed-negative reversal row | `=ARRAYFORMULA(status="reversal")` | `df["status"].eq("reversal")` |
| `is_sale` | Not a reversal row | `=ARRAYFORMULA(status<>"reversal")` | `~df["is_reversal"]` |
| `is_blank_sku` | SKU field is blank | `=ARRAYFORMULA(TRIM(sku)="")` | `df["sku"].fillna("").str.strip().eq("")` |
| `is_ghost_sku` | SKU not in catalog master | `=ARRAYFORMULA(ISNA(MATCH(sku, catalog!A:A, 0))*(sku<>""))` | `~df["sku"].isin(catalog_skus) & ~df["is_blank_sku"]` |
| `is_counted` | Passes ALL business rules | `=ARRAYFORMULA((is_canceled=0)*(is_excluded=0)*(is_blank_sku=0)*(is_ghost_sku=0))` | `~df["is_canceled"] & ~df["is_excluded"] & ~df["is_blank_sku"] & ~df["is_ghost_sku"]` |

---

## Line-level financials — signed sums so returns net automatically

Because reversal rows carry negative quantity, `line_gmv = unit_price × quantity` is naturally negative for a return, and the two rows sum to zero net GMV. No manual "minus returns" step is needed — the signed-sum semantics do the accounting.

| Metric | Google Sheets | pandas |
|---|---|---|
| Line GMV | `=ARRAYFORMULA(unit_price * quantity)` | `df["unit_price_num"] * df["quantity"]` |
| Line COGS | `=ARRAYFORMULA(cost_price * quantity)` | `df["cost_price"] * df["quantity"]` |
| Line commission | `=ARRAYFORMULA(line_gmv * commission_rate)` | `df["line_gmv"] * df["commission_rate"]` |
| Line net revenue | `=ARRAYFORMULA(line_gmv - line_commission)` | `df["line_gmv"] - df["line_commission"]` |
| Line contribution margin | `=ARRAYFORMULA(line_net_revenue - line_cogs)` | `df["line_net_revenue"] - df["line_cogs"]` |

---

## Master layer — join once, then don't touch raw again

The master layer joins orders to catalog, channel, and voucher tables. In Sheets this is `VLOOKUP` / `INDEX-MATCH` per column; in pandas it's a chain of `merge()`.

| Join | Google Sheets | pandas |
|---|---|---|
| Category / brand from catalog | `=ARRAYFORMULA(IFERROR(VLOOKUP(sku, catalog!A:E, 3, 0)))` | `clean.merge(catalog[["sku","category","brand","list_price"]], on="sku", how="left")` |
| Commission rate from channel | `=ARRAYFORMULA(IFERROR(VLOOKUP(channel_id, channels!A:D, 3, 0)))` | `master.merge(channels[["channel_id","commission_rate"]], on="channel_id", how="left")` |
| Voucher on order | `=ARRAYFORMULA(IFERROR(VLOOKUP(order_id, vouchers_filtered!A:B, 2, 0)))` | `master.merge(vouchers_clean.drop_duplicates("order_id"), on="order_id", how="left")` |

---

## Headline KPIs — SUMIFS ↔ pandas boolean sums

The out layer is where the Sheets vocabulary reads most naturally. Every KPI is a `SUMIFS` in Sheets and a boolean-masked `.sum()` in pandas.

| Metric | Google Sheets | pandas |
|---|---|---|
| Gross GMV | `=SUMIFS(line_gmv, is_counted, 1, is_sale, 1)` | `sales["line_gmv"].sum()` |
| Net revenue (netted for returns) | `=SUMIFS(line_net_revenue, is_counted, 1)` | `counted["line_net_revenue"].sum()` |
| Contribution margin | `=SUMIFS(line_contribution_margin, is_counted, 1)` | `counted["line_contribution_margin"].sum()` |
| Commission paid | `=SUMIFS(line_commission, is_counted, 1)` | `counted["line_commission"].sum()` |
| Unique orders | `=COUNTUNIQUEIFS(order_id, is_counted, 1)` | `counted["order_id"].nunique()` |
| Unique customers | `=COUNTUNIQUEIFS(customer_id, is_counted, 1)` | `counted["customer_id"].nunique()` |
| Units sold | `=SUMIFS(quantity, is_counted, 1, is_sale, 1)` | `sales["quantity"].sum()` |
| AOV | `=gross_gmv / unique_orders` | `gross_gmv / orders` |
| Margin % | `=100 * contribution_margin / gross_gmv` | `100 * contribution_margin / gross_gmv` |

---

## Aggregations — GROUPBY equivalents

| Aggregation | Google Sheets | pandas |
|---|---|---|
| Monthly GMV | `=QUERY(master, "SELECT year_month, SUM(line_gmv) WHERE is_counted=TRUE AND is_sale=TRUE GROUP BY year_month")` | `sales.groupby("year_month")["line_gmv"].sum()` |
| Per-SKU GMV | `=SUMIFS(line_gmv, sku, X, is_counted, 1, is_sale, 1)` | `sales.groupby("sku")["line_gmv"].sum()` |
| Per-channel margin | `=SUMIFS(line_contribution_margin, channel_id, X, is_counted, 1)` | `counted.groupby("channel_id")["line_contribution_margin"].sum()` |
| Promotion vs no-promotion AOV | `=AVERAGEIFS(order_gmv, is_promo, TRUE)` (on order-grain frame) | `order_frame.groupby("is_promo")["gmv"].mean()` |

---

## Why the crosswalk exists

Two reasons.

**First — auditability.** Anyone in the business can open the Sheets version, read the formula in a cell, and know exactly where the number came from. Anyone technical can open `src/pipeline.py`, find the same operation, and verify the two match. There is no way for the two implementations to silently diverge because they use the same flag names and the same aggregation semantics.

**Second — portability.** The same reporting logic runs in whichever surface the stakeholder needs. Sheets for daily manual poking. Pandas for scale, testing, and CI. Both compile down to the same numbers.

Nothing in this file is a translation trick. Every `SUMIFS` above has a native pandas equivalent that expresses the same intent. That equivalence is the point.
