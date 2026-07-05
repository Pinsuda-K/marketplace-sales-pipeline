# Data quality notes

Real marketplace exports are almost never clean. Reporting pipelines that assume they are — that go straight from `raw.csv` into a chart — quietly ship wrong numbers.

The sample data in `data/` includes ten deliberate categories of dirty-data issues, patterned on the kinds of problems that show up in production marketplace exports. The pipeline in `src/pipeline.py` catches each one, counts it in the diagnostics output, and handles it correctly.

## The ten issues

1. **Multi-item orders** — one `order_id` across multiple line items. If order counts sum row-by-row instead of by unique `order_id`, order count inflates. Handled by `order_id.nunique()`.

2. **Cancellations** — status = `canceled` rows have real prices but never converted. Handled by `is_canceled` flag; excluded from `is_counted`.

3. **Returns and reversal rows** — returned orders generate a matching `reversal` row with negative quantity. Because `line_gmv = qty × price`, the reversal's GMV is naturally negative and nets the original when summed. No manual "minus returns" step.

4. **Internal QA / staff test orders** — tagged with `customer_id = CUST-STAFF-001` and `unit_price = 0`. Belt-and-braces: caught by both the exclusion list AND the zero-price heuristic.

5. **Exact-duplicate rows** — same `order_item_id` appearing twice (export bug). Handled by `drop_duplicates(subset=["order_item_id"])`.

6. **Mixed date formats** — ~15% of rows use `DD/MM/YYYY HH:MM` with trailing whitespace instead of ISO. Silent locale flip would corrupt month-over-month. Handled by an explicit `_parse_date()` helper.

7. **Currency strings with baht prefix** — ~15% of `unit_price` values arrive as `"฿20,456"`. Naïve parsing yields NaN. Handled by `_parse_price()`.

8. **Blank SKU rows** — occasional rows with `sku = ""`. Can't join to catalog. Caught by `is_blank_sku`; excluded from `is_counted`.

9. **Ghost SKUs** — SKU present in orders but missing from catalog. A `LEFT JOIN` would silently fill nulls for `cost_price`, corrupting margin math. Caught by `is_ghost_sku`; excluded from `is_counted`.

10. **Orphan vouchers** — voucher redemption pointing at a now-canceled order. Would overstate voucher spend. Caught by `orphan_voucher` flag in the voucher clean layer; excluded from the master join.

## Diagnostics on every run

Every pipeline execution prints a diagnostics block, and every count lands in `output/dashboard_data.json` under `data_quality`. Sudden changes in any of these — a spike in duplicates, a jump in ghost SKUs — is itself a signal that something changed upstream.

```
DATA QUALITY DIAGNOSTICS
============================================================
  raw_order_rows            4,031
  duplicates_dropped            1
  unparseable_dates             0
  blank_sku_rows                1
  ghost_sku_rows                1
  excluded_rows                 6
  canceled_rows               175
  reversal_rows               123
  orphan_vouchers              27
```

## Principle

Every number on the dashboard should be reproducible from `raw_*.csv` files by anyone with the code — and the code should surface every assumption it made along the way.
