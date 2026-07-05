# Marketplace Sales & GMV Reporting Pipeline

A layered, formula-first sales and margin reporting pipeline for a specialty coffee equipment storefront that sells across two marketplaces and its own DTC channel.

Every raw export is treated as untouchable. Every downstream number is reproducible from source. Every data-quality issue is surfaced, not hidden.

Mock data only. Six months, 4,031 raw order lines, 20 SKUs, three sales channels.

---

## Why I built this

Marketplace and own-store exports are almost never clean. Reporting pipelines that assume they are — that read `raw.csv` and go straight to a chart — quietly ship wrong numbers. Duplicated rows inflate GMV. Cancellations count as revenue. Locale-mixed dates flip month over month. Baht-prefixed strings silently coerce to `NaN`.

This project is my answer to those problems as a pattern. It's the reporting architecture I actually reach for in day-to-day work, built out on a domain (specialty coffee equipment) I find fun to reason about, with the data quality problems seeded in deliberately so the pipeline has real work to do.

Two questions this project is designed to answer honestly:

1. **Can I design a pipeline where every KPI is traceable back to a specific raw cell?**
2. **Can I surface the ugly parts of the data, rather than clean them away invisibly?**

---

## Architecture

```
raw_       order_transaction  ·  product_catalog  ·  channel_master  ·  voucher_usage  ·  traffic_funnel  ·  exclusion_list
           (exports as they arrive — never mutated by hand)
    │
    ▼
clean_     typed  ·  deduped  ·  flags derived (canceled / excluded / return-reversal / countable)
    │
    ▼
master_    orders ⋈ catalog ⋈ channel ⋈ vouchers  → one analysis-ready table with commission and margin logic
    │
    ▼
out_       channel_perf  ·  product_econ  ·  funnel_retention  ·  monthly  ·  promo_impact
    │
    ▼
Dashboard  interactive HTML terminal at dashboard/index.html
```

The **layers are the point**. Cleaning rules live in exactly one place (the clean layer). Business logic lives in exactly one place (the master and out layers). Nothing is done twice, and nothing is done invisibly.

---

## Design decisions

A few of the choices I made and why. These are the tradeoffs I'd want to defend in an interview.

### 1. Layered pipeline over a single-file script

Cleaning, joining, and reporting logic live in separate stages. Slower to write than a one-shot script, but any downstream question can be answered by walking one step back — the dashboard reads `dashboard_data.json`, which comes from `out_channel_perf.csv`, which comes from `master_orders.csv`, which comes from `clean_orders.csv`, which comes from `raw_order_transaction.csv`. No hidden Excel step in the middle.

### 2. Reporting flags derived in the clean layer, not computed downstream

`is_countable` is a single boolean set once — it's `True` if a row (a) has a valid status, (b) isn't a QA / staff test, (c) has a SKU that resolves, and (d) isn't blank. Every downstream aggregation filters on that same flag. There's no per-chart "am I sure this row should count?" logic scattered across the codebase.

### 3. Returns handled with signed-negative reversal rows, not "subtract returns"

Returned orders generate a matching reversal row with negative quantity. Because `line_gmv = qty × price`, the reversal row's GMV is naturally negative and it nets the original to zero when summed. No manual "revenue minus returns" step. The signed-sum semantics do the accounting.

### 4. Currency and date parsing as first-class helpers, not inline logic

`_parse_price("฿20,456")` and `_parse_date("01/05/2025 10:13  ")` are named functions. They're one-liners in what they do, but by lifting them out of the row loop they become the single source of truth for how these fields are interpreted — and they're testable in isolation.

### 5. Data quality is a run-time output, not a comment

Every pipeline run prints a diagnostics block: how many duplicates were dropped, how many blank-SKU rows, how many orphan vouchers. These same numbers land in `dashboard_data.json` and render on the dashboard. If any of them spike between runs, something has changed in the source data — which is itself important information.

---

## What ran on this dataset

Latest pipeline run, mock data:

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

BUSINESS KPIs
============================================================
  Period:                2025-05-01 → 2025-11-05
  GMV:                   ฿45,036,408
  Commission paid:       ฿4,484,911
  Net revenue:           ฿40,551,497
  Contribution margin:   ฿12,182,527 (27.1%)
  Orders (unique):       2,753
  Unique customers:      400
  Units sold:            4,238
  AOV:                   ฿16,359
```

The data quality diagnostics catch every category of issue seeded into the mock exports. See [`docs/data_quality_notes.md`](docs/data_quality_notes.md) for the full inventory of ten issue patterns and how each is handled.

---

## Dashboard

The dashboard at `dashboard/index.html` reads `dashboard_data.json` and renders three business narratives instead of a category-per-page layout:

- **01 · Channel Performance** — GMV vs net revenue vs contribution margin by channel, monthly trend, marketplace-vs-own-store contribution
- **02 · Product Economics** — top SKUs by revenue and by margin %, full SKU ledger
- **03 · Conversion & Retention** — funnel by channel, promotion impact on AOV, data quality diagnostics inline

Design decisions on the dashboard side: sidebar-as-pages for state, scroll-reveal bands so each visualization gets its own space with a note explaining what to look for, and every chart labels its axes in mono type so cost structures stay legible.

The palette (deep navy `#003087` + safety red `#ED1C24`) and typography (Anton for display, Barlow Condensed for headings, IBM Plex Mono for data) are chosen to feel like an engineered technical spec rather than a marketing report.

To view: open `dashboard/index.html` in a browser after running the pipeline.

---

## Repository layout

```
marketplace-sales-pipeline/
├── src/
│   ├── generate_mock_data.py     # produces the raw_*.csv exports (seeded issues)
│   └── pipeline.py               # raw → clean → master → out
├── data/                         # raw_ exports (generated)
├── output/                       # clean_, master_, out_ CSVs + JSON payloads
├── dashboard/
│   └── index.html                # HTML terminal reading dashboard_data.json
├── docs/
│   └── data_quality_notes.md     # inventory of data issues and how they're handled
└── README.md
```

---

## Running it

```bash
# 1. Install dependencies
pip install pandas

# 2. Generate mock exports (deterministic — same seed, same numbers)
python src/generate_mock_data.py

# 3. Run the pipeline (produces clean_, master_, out_ tables + dashboard_data.json)
python src/pipeline.py

# 4. Open the dashboard
open dashboard/index.html   # or double-click in a file browser
```

---

## What this project is *not*

- Not a production system. There's no orchestration, no cost monitoring, no lineage tracking beyond what the CSV layering provides.
- Not tied to any real business. The domain (specialty coffee equipment) and the SKUs are invented. The three channels are labeled generically. The commission rates are illustrative, not researched.
- Not a data science project. There are no models, no forecasts. It's a reporting pipeline, and the question it answers is "can this be *right*?" — not "what will happen next?"

The intent is to demonstrate the reporting discipline: layered logic, traceable numbers, explicit data quality. Everything else follows from that.

---

## Principle

Every number a stakeholder sees on the dashboard should be reproducible from `raw_*.csv` files by anyone with the code — and the code should surface every assumption it made along the way.
