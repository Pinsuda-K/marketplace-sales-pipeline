# Data Quality Notes

Real marketplace exports are almost never clean. Reporting pipelines that assume they are — that go straight from `raw.csv` into a dashboard chart — quietly ship wrong numbers.

This project treats data quality as a first-class concern. Every raw export in `data/` has deliberate issues seeded in, patterned on the kinds of problems that show up in production marketplace data. The pipeline in `src/pipeline.py` is designed to detect each one, count it in the diagnostics output, and handle it correctly.

Below is the inventory of issues in the mock data, the failure mode each would cause if ignored, and how the pipeline handles it.

---

## Issue 1 — Multi-item orders

**Pattern.** A single `order_id` appears across multiple `order_item_id` rows because the customer bought more than one product in the same cart.

**Failure if ignored.** Counting rows as orders inflates the order count. A cart with 3 items would report as 3 orders — a ~15–25% inflation on the total, depending on basket size.

**Handled by.** Order counts use `order_id.nunique()`, not row counts. GMV and revenue are line-item sums (which is correct — those *are* per-line). AOV is `total_gmv / unique_order_count`.

---

## Issue 2 — Cancellations

**Pattern.** ~5% of orders have `status = 'canceled'`. The row is present with a real price and quantity, but the sale never actually completed.

**Failure if ignored.** GMV, revenue, and margin all overstate by the value of canceled orders.

**Handled by.** `is_canceled` flag derived in the clean layer. The `is_countable` flag excludes any row where status is `'canceled'`. All out-layer marts filter on `is_countable`.

---

## Issue 3 — Returns and reversal rows

**Pattern.** Returned orders (~3%) generate a matching row with `status = 'reversal'` and **negative quantity** several days after the original delivery.

**Failure if ignored.** If reversal rows are dropped, returns don't net out — you overstate revenue by the full return amount. If reversal rows are kept but the negative-quantity semantics aren't understood, sums get corrupted in weird ways.

**Handled by.** The pipeline keeps both the original returned row *and* the reversal row. Because reversal quantity is negative, `line_gmv = qty × price` is naturally negative for the reversal, and the two rows sum to zero net GMV. No manual "minus returns" step is needed — the signed-sum logic handles it.

---

## Issue 4 — Internal QA / staff test orders

**Pattern.** Six rows are internal QA tests, tagged in raw with `customer_id = 'CUST-STAFF-001'` and `unit_price = 0`. Some real production systems mark these with a status flag; others rely on a separate exclusion list.

**Failure if ignored.** Order and unit counts get inflated by internal test volume. Because prices are ฿0, GMV isn't affected — but the extra "orders" make AOV drop artificially and skew customer counts.

**Handled by.** Two independent checks: (1) `customer_id in exclusion_list.csv`, and (2) `unit_price == 0 with quantity > 0`. Either triggers `is_excluded`. Belt-and-braces because in real data one or the other is often missing.

---

## Issue 5 — Exact-duplicate rows

**Pattern.** One order-item row appears twice with the same `order_item_id` — an export bug where the seller-center system re-emitted the same record.

**Failure if ignored.** Double-counting on that specific row: GMV, orders, and units all get inflated.

**Handled by.** `drop_duplicates(subset=['order_item_id'], keep='first')` in the clean layer. Duplicates dropped are reported in diagnostics so this can be monitored over time (a spike in duplicates is itself a signal that something's wrong upstream).

---

## Issue 6 — Mixed date formats

**Pattern.** ~15% of rows use `DD/MM/YYYY HH:MM` with trailing whitespace instead of the standard `YYYY-MM-DD HH:MM:SS`. Locale-inconsistent export bug.

**Failure if ignored.** Naïve `pd.to_datetime` parses `05/01/2025` as May 1st in some locales and January 5th in others — the same date string yields different months. Silent, invisible, and catastrophic for month-over-month trend charts.

**Handled by.** `_parse_date()` in the pipeline first tries a strict `DD/MM/YYYY` regex, then falls back to `pd.Timestamp`. Trailing whitespace is stripped. Unparseable dates are counted separately in diagnostics.

---

## Issue 7 — Currency strings with locale prefix

**Pattern.** ~15% of `unit_price` values arrive as strings like `"฿20,456"` or `"฿6,900"` — with the baht sign and thousand-separator. The rest are plain numbers.

**Failure if ignored.** Pandas reads the whole `unit_price` column as `object` (string). Any arithmetic silently produces NaN or errors out.

**Handled by.** `_parse_price()` strips `฿`, commas, and whitespace before casting to float. Empty strings become `0.0`. This is a one-line helper but it eliminates a whole category of downstream errors.

---

## Issue 8 — Blank SKU rows

**Pattern.** Occasional rows arrive with `sku = ""` — could be a scanner error, a manual data entry from a phone order, or a corrupted export field.

**Failure if ignored.** These rows can't be joined to the catalog (no `sku` to match on), so they'd disappear silently in an inner join — but they'd still count as orders and inflate GMV if not filtered elsewhere.

**Handled by.** `is_blank_sku` flag set explicitly. Rows with blank SKU are excluded from `is_countable` and reported in diagnostics.

---

## Issue 9 — SKUs missing from the catalog

**Pattern.** An order references `SKU-XXX-999` — a valid-looking SKU that isn't in `raw_product_catalog.csv`. Could be a recently discontinued product, a catalog sync lag, or a data entry error.

**Failure if ignored.** A `LEFT JOIN` fills in `category`, `brand`, `cost_price` as null — so cost-based metrics (margin, COGS) silently misreport for that row.

**Handled by.** `is_ghost_sku` flag catches this. Ghost-SKU rows are excluded from countable metrics *and* surfaced in diagnostics so someone can investigate and either add the SKU to the catalog or delete the row.

---

## Issue 10 — Orphan vouchers

**Pattern.** A voucher redemption points at an `order_id` that was later canceled. The redemption row is still there — the platform doesn't retroactively remove voucher records when the parent order cancels.

**Failure if ignored.** Voucher cost gets counted for orders that were never actually delivered. Promotion ROI calculations overstate voucher spend.

**Handled by.** In the clean layer, orphan vouchers are flagged (`orphan_voucher = True`). The master layer only joins the non-orphan subset. Orphan counts are surfaced in diagnostics.

---

## Diagnostics output

Every pipeline run produces a diagnostic block, so these numbers are visible on every execution — not hidden in code comments:

```
DATA QUALITY DIAGNOSTICS
============================================================
  Raw rows read:            4,031
  Exact duplicates dropped: 1
  Unparseable dates:        0
  Blank-SKU rows:           1
  Ghost-SKU rows (no cat):  1
  Excluded customer rows:   6
  Orphan vouchers:          27
```

Sudden changes in any of these — a spike in duplicates, a jump in ghost SKUs — is itself a signal that something's changed upstream in the exports. In a production version this would be alerted on.

---

## Principle

The point of surfacing all of this isn't to show off the mess. It's to demonstrate the reporting principle that made me want to build the pipeline this way:

**Every number a stakeholder sees on the dashboard should be reproducible from `raw_*.csv` files by anyone with the code — and the code should surface every assumption it made along the way.**

If a manager questions a GMV figure, the trail runs: dashboard → `dashboard_data.json` → `out_channel_perf.csv` → `master_orders.csv` → `clean_orders.csv` → the specific `raw_order_transaction.csv` row. No hidden manual "cleaned it in Excel" step in the middle.
