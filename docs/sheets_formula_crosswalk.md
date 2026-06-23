# Sheets &harr; pandas formula crosswalk

The production back-end is a **formula-first Google Sheets system** &mdash; no
black-box scripts for the metrics. Every reporting number traces back through
strict linear references to the raw exports, which are never edited by hand.
This file maps each metric to (a) the Google Sheets formula used in the real
workbook and (b) the pandas equivalent in [`src/pipeline.py`](../src/pipeline.py).

**Layer model:** `raw_` &rarr; `clean_` &rarr; `master_` &rarr; `out_`

**Inputs (mirroring marketplace seller-center exports + one internal list):**

| File | Grain | Feeds |
|---|---|---|
| `raw_order_transaction` | one row per order item | GMV, revenue, orders, units |
| `raw_product_catalog` | one row per sku | category, brand, cost &rarr; margin |
| `raw_voucher_usage` | one row per voucher redemption | promotion effect |
| `raw_traffic_funnel` | date &times; sku | conversion funnel |
| `exclusion_list` *(internal)* | one row per excluded order | business-rule filtering |

## Metric map

| Metric | What it does | Google Sheets | pandas |
|---|---|---|---|
| Exclusion flag | internal orders removed | `=ARRAYFORMULA(ISNUMBER(MATCH(orderNumber, exclusion!A:A, 0)))` | `orderNumber.isin(excluded)` |
| Counted flag | valid unless canceled / excluded | `=ARRAYFORMULA((is_canceled=0)*(is_excluded=0))` | `(is_canceled==0)&(is_excluded==0)` |
| Sale-line flag | exclude reversal rows | `=ARRAYFORMULA(status<>"refund")` | `status != "refund"` |
| Line GMV | sellingPrice &times; qty (signed) | `=ARRAYFORMULA(sellingPrice*quantity)` | `sellingPrice * quantity` |
| Category / cost join | sku &rarr; category, cost | `=ARRAYFORMULA(IFERROR(VLOOKUP(sku, catalog!A:E, ...)))` | `clean.merge(catalog, on="sellerSku")` |
| Gross GMV | counted sale lines | `=SUMIFS(line_gmv, counted, 1, sale, 1)` | `sales["line_gmv"].sum()` |
| Net Revenue | counted incl. reversals &rarr; returns net to 0 | `=SUMIF(counted, 1, line_gmv)` | `counted["line_gmv"].sum()` |
| Gross Profit | (sellingPrice &minus; cost) &times; qty | `=SUMIF(counted, 1, line_profit)` | `counted["line_profit"].sum()` |
| Orders | distinct orders, deduped across items | `=COUNTUNIQUEIFS(orderNumber, counted, 1)` | `counted["orderNumber"].nunique()` |
| Promotion effect | promo vs non-promo, by campaign | `=SUMIFS / COUNTUNIQUEIFS by campaign` | order-grain `groupby(["is_promo","campaign"])` |
| Conversion funnel | visitors &rarr; views &rarr; ATC &rarr; paid | `=SUM(...)` per stage | stage counts from `traffic` + orders |

## Why these specific choices

**Raw stays platform-native; flags are derived in `clean_`.** The order export
carries only what the marketplace actually returns (`status`, prices, quantity).
The reporting flags (`is_canceled`, `is_counted`, `is_sale`) are computed in the
clean layer, and `is_excluded` comes from a **separate internal list** &mdash; so
platform truth and internal business decisions never get mixed in the raw data.

**Join and count at item grain (`orderItemId`).** A single order can hold several
items (`ORD-1006` has three). Joining or summing on `orderNumber` would inflate
the grain; order-level figures are recovered with a *distinct* count instead.

**Net Revenue via a signed sum.** A return is a separate negative reversal row
sharing the order number (`ITEM-0005` `+2000`, `ITEM-0005R` `-2000`). Net Revenue
sums all counted rows including the negative, so the sale and its reversal cancel
to zero automatically &mdash; nothing to maintain by hand.

**Promotion aggregated at order grain.** `discountAmount` is order-level; summing
it across item rows would multiply it. The promotion table dedupes to one row per
order first, then groups by campaign.

## Reproducible output (mock data)

`python src/pipeline.py` on the mock exports produces:

| gross_gmv | net_revenue | gross_profit | margin | orders | units | aov |
|---|---|---|---|---|---|---|
| 39,200 | 37,200 | 14,200 | 38.2% | 10 | 19 | 3,720 |

The 2,000 gross-to-net gap is the returned `Ceramic Wash Basin` netting to zero;
`orders = 10` because the canceled order (`ORD-1003`) and the internal QA order
(`ORD-1005`) are dropped. Every headline figure is cross-checked against an
independent recompute from the raw file.
