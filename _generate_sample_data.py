"""
_generate_sample_data.py — TEST FIXTURE, NOT PART OF THE PIPELINE

Purpose
-------
Seeds the data/ folder with realistic sample marketplace exports so
src/pipeline.py has something to work on. This script is intentionally
prefixed with an underscore and lives under scripts/ (not src/) to make it
obvious it is scaffolding, not core code.

If you already have real marketplace exports in data/ shaped like the
raw_*.csv schemas the pipeline reads, you can skip running this entirely.

What the fixture generates
--------------------------
- 6 months of order line items across a specialty coffee equipment storefront
- 3 channels (2 marketplaces + own DTC store) with different commission rates
- ~400 unique customers, ~20 SKUs, ~600 voucher redemptions
- Ten deliberate categories of dirty-data issues seeded in — the pipeline's
  data-quality flags (documented in docs/data_quality_notes.md) are designed
  to catch each one.

Everything else in this file is CSV writing plumbing. The interesting file
is src/pipeline.py.
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

# ---------- Domain: specialty coffee equipment ----------
# Realistic multi-tier catalog: high-price equipment, mid-tier tools, low-price consumables.
CATALOG = [
    # sku, name, category, brand, cost_price, list_price
    ("SKU-ESP-001", "Dual-Boiler Espresso Machine",   "Espresso Machines", "Vertex",  38000, 62000),
    ("SKU-ESP-002", "Single-Boiler Espresso Machine", "Espresso Machines", "Vertex",  22000, 38000),
    ("SKU-ESP-003", "Semi-Auto Espresso Machine",     "Espresso Machines", "Kanto",   28000, 45000),
    ("SKU-GRD-001", "Flat-Burr Grinder 64mm",         "Grinders",          "Vertex",  18500, 32000),
    ("SKU-GRD-002", "Conical-Burr Grinder",           "Grinders",          "Kanto",   12000, 21000),
    ("SKU-GRD-003", "Hand Grinder Titanium",          "Grinders",          "Meridian", 4200,  7800),
    ("SKU-KTL-001", "Variable-Temp Gooseneck Kettle", "Kettles",           "Meridian", 3800,  6900),
    ("SKU-KTL-002", "Stovetop Gooseneck Kettle",      "Kettles",           "Meridian", 1200,  2400),
    ("SKU-SCL-001", "Precision Coffee Scale 2kg",     "Scales",            "Meridian", 2100,  3800),
    ("SKU-FRT-001", "Milk Frothing Pitcher 600ml",    "Accessories",       "Kanto",     420,   890),
    ("SKU-FRT-002", "Milk Frothing Pitcher 350ml",    "Accessories",       "Kanto",     320,   690),
    ("SKU-TMP-001", "58mm Tamper Steel",              "Accessories",       "Vertex",    780,  1650),
    ("SKU-TMP-002", "53mm Tamper Steel",              "Accessories",       "Vertex",    720,  1590),
    ("SKU-PRT-001", "Bottomless Portafilter 58mm",    "Accessories",       "Vertex",   1400,  2790),
    ("SKU-DIS-001", "WDT Distribution Tool",          "Accessories",       "Meridian",  450,   990),
    ("SKU-CLN-001", "Espresso Machine Cleaner 900g",  "Consumables",       "Kanto",     280,   590),
    ("SKU-CLN-002", "Backflush Detergent 500g",       "Consumables",       "Kanto",     220,   450),
    ("SKU-FLT-001", "Paper Filter V60 100ct",         "Consumables",       "Meridian",   85,   180),
    ("SKU-FLT-002", "Paper Filter V60 500ct-Pack",    "Consumables",       "Meridian",  340,   690),
    ("SKU-BAG-001", "Coffee Storage Vault 1kg",       "Accessories",       "Kanto",     680,  1450),
]

CHANNELS = [
    # channel_id, name, commission_rate, is_own_store
    ("CH-MP-A", "Marketplace A",        0.15, False),
    ("CH-MP-B", "Marketplace B",        0.12, False),
    ("CH-OWN",  "Own-Store (DTC Web)",  0.00, True),
]

CUSTOMERS = [f"CUST-{str(i).zfill(4)}" for i in range(1, 401)]  # 400 unique customers

VOUCHERS = [
    ("VCH-WELCOME10",   "Welcome 10% Off",       0.10),
    ("VCH-PAYDAY100",   "Payday ฿100 Off",       100),  # flat amount
    ("VCH-DOUBLE-15",   "Double-Date 15% Off",   0.15),
    ("VCH-FREESHIP",    "Free Shipping",         50),   # shipping subsidy, flat
    ("VCH-BUNDLE-20",   "Bundle 20% Off",        0.20),
]

PROMO_PERIODS = [
    # (label, start_date, end_date, voucher_id)
    ("Payday May",        "2025-05-25", "2025-05-31", "VCH-PAYDAY100"),
    ("Double-6 June",     "2025-06-06", "2025-06-08", "VCH-DOUBLE-15"),
    ("Payday June",       "2025-06-25", "2025-06-30", "VCH-PAYDAY100"),
    ("Double-7 July",     "2025-07-07", "2025-07-09", "VCH-DOUBLE-15"),
    ("Bundle Bonanza",    "2025-08-15", "2025-08-20", "VCH-BUNDLE-20"),
    ("Payday August",     "2025-08-25", "2025-08-31", "VCH-PAYDAY100"),
    ("Double-9 Sept",     "2025-09-09", "2025-09-11", "VCH-DOUBLE-15"),
    ("Payday October",    "2025-10-25", "2025-10-31", "VCH-PAYDAY100"),
]


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def get_active_promo(date):
    for label, s, e, v in PROMO_PERIODS:
        if datetime.strptime(s, "%Y-%m-%d").date() <= date <= datetime.strptime(e, "%Y-%m-%d").date():
            return label, v
    return None, None


def generate_orders():
    """Generate the raw order_transaction export — line-item grain."""
    rows = []
    order_seq = 1000
    item_seq = 10000

    start_date = datetime(2025, 5, 1).date()
    end_date = datetime(2025, 10, 31).date()

    for day in daterange(start_date, end_date):
        promo_label, promo_voucher = get_active_promo(day)
        # Base traffic: 8-15 orders/day, boosted 2-3x on promo days
        n_orders = random.randint(8, 15)
        if promo_label:
            n_orders = int(n_orders * random.uniform(2.0, 3.2))

        for _ in range(n_orders):
            order_seq += 1
            order_id = f"ORD-{order_seq}"
            customer_id = random.choice(CUSTOMERS)
            channel = random.choices(
                CHANNELS,
                weights=[0.42, 0.33, 0.25],  # marketplaces dominate volume
                k=1
            )[0]
            channel_id = channel[0]

            # Order can have 1-3 line items — this is the multi-item order case
            n_items = random.choices([1, 2, 3], weights=[0.72, 0.22, 0.06])[0]
            sampled_skus = random.sample(CATALOG, n_items)

            # Order-level status
            status = random.choices(
                ["delivered", "canceled", "returned"],
                weights=[0.92, 0.05, 0.03]
            )[0]

            for sku_row in sampled_skus:
                item_seq += 1
                sku, name, category, brand, cost, list_price = sku_row

                # Actual selling price: sometimes discounted on promo days
                if promo_label:
                    discount_pct = random.uniform(0.05, 0.20)
                    selling = int(list_price * (1 - discount_pct))
                else:
                    # Marketplaces run own discounts too — small variation
                    if channel_id.startswith("CH-MP"):
                        selling = int(list_price * random.uniform(0.92, 1.00))
                    else:
                        selling = list_price

                qty = random.choices([1, 2, 3], weights=[0.85, 0.12, 0.03])[0]

                # Timestamp within the day
                hour = random.randint(9, 22)
                minute = random.randint(0, 59)
                created_at = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)

                # Intentional messy formatting: mix ISO and locale strings
                if random.random() < 0.85:
                    date_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    # DD/MM/YYYY with trailing space — export bug
                    date_str = created_at.strftime("%d/%m/%Y %H:%M ") + " "

                # Currency string with baht sign on ~15% of rows — export inconsistency
                if random.random() < 0.15:
                    unit_price_str = f"฿{selling:,}"
                else:
                    unit_price_str = str(selling)

                rows.append({
                    "order_item_id": f"ITEM-{item_seq}",
                    "order_id": order_id,
                    "created_at": date_str,
                    "customer_id": customer_id,
                    "channel_id": channel_id,
                    "sku": sku,
                    "item_name": name,
                    "quantity": qty,
                    "unit_price": unit_price_str,
                    "cost_price": cost,
                    "status": status,
                    "promo_label": promo_label or "",
                })

                # If returned: add matching negative-quantity reversal row
                if status == "returned" and random.random() < 0.9:
                    item_seq += 1
                    reversal_date = created_at + timedelta(days=random.randint(3, 10))
                    rows.append({
                        "order_item_id": f"ITEM-{item_seq}",
                        "order_id": order_id,
                        "created_at": reversal_date.strftime("%Y-%m-%d %H:%M:%S"),
                        "customer_id": customer_id,
                        "channel_id": channel_id,
                        "sku": sku,
                        "item_name": name,
                        "quantity": -qty,  # negative
                        "unit_price": unit_price_str,
                        "cost_price": cost,
                        "status": "reversal",
                        "promo_label": "",
                    })

    # --- Seed the deliberate messy issues ---

    # Duplicate row: pick a delivered row, add it again with same order_item_id
    delivered = [r for r in rows if r["status"] == "delivered"]
    dup_source = random.choice(delivered)
    rows.append({**dup_source})  # exact duplicate — pipeline should dedupe

    # Missing SKU: orphan row with blank SKU
    orphan = random.choice(delivered).copy()
    orphan["order_item_id"] = f"ITEM-{item_seq + 1}"
    orphan["sku"] = ""
    orphan["item_name"] = ""
    rows.append(orphan)

    # SKU that isn't in the catalog — broken join test
    ghost = random.choice(delivered).copy()
    ghost["order_item_id"] = f"ITEM-{item_seq + 2}"
    ghost["sku"] = "SKU-XXX-999"
    ghost["item_name"] = "Discontinued Item"
    rows.append(ghost)

    # Internal QA / staff test orders — must be excluded
    for i in range(6):
        item_seq += 10
        qa_date = datetime(2025, random.choice([6, 7, 8, 9]), random.randint(1, 28), 14, 30)
        rows.append({
            "order_item_id": f"ITEM-QA-{i}",
            "order_id": f"ORD-QA-{i:03d}",
            "created_at": qa_date.strftime("%Y-%m-%d %H:%M:%S"),
            "customer_id": "CUST-STAFF-001",
            "channel_id": "CH-OWN",
            "sku": random.choice(CATALOG)[0],
            "item_name": "QA Test",
            "quantity": 1,
            "unit_price": "0",  # zero-price is another signal for QA orders
            "cost_price": 0,
            "status": "delivered",
            "promo_label": "INTERNAL_QA",
        })

    return rows


def generate_catalog():
    """Generate raw_product_catalog — the SKU master."""
    return [
        {
            "sku": sku,
            "item_name": name,
            "category": category,
            "brand": brand,
            "cost_price": cost,
            "list_price": list_price,
        }
        for sku, name, category, brand, cost, list_price in CATALOG
    ]


def generate_channels():
    """Generate raw_channel_master — commission and channel-type reference."""
    return [
        {
            "channel_id": cid,
            "channel_name": name,
            "commission_rate": rate,
            "is_own_store": "TRUE" if own else "FALSE",
        }
        for cid, name, rate, own in CHANNELS
    ]


def generate_vouchers(orders):
    """Generate raw_voucher_usage — a voucher may point at a canceled order (intentional)."""
    rows = []
    # Vouchers only apply to promo periods
    promo_orders = [r for r in orders if r.get("promo_label") and r["promo_label"] != "INTERNAL_QA"]
    # ~35% of promo orders redeemed a voucher
    redemptions = random.sample(promo_orders, k=int(len(promo_orders) * 0.35))
    for r in redemptions:
        # Find the voucher for that promo period
        voucher_id = None
        for label, s, e, v in PROMO_PERIODS:
            if label == r["promo_label"]:
                voucher_id = v
                break
        if not voucher_id:
            continue
        rows.append({
            "voucher_id": voucher_id,
            "order_id": r["order_id"],
            "redeemed_at": r["created_at"] if isinstance(r["created_at"], str) else r["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "customer_id": r["customer_id"],
        })
    return rows


def generate_traffic(orders):
    """Generate raw_traffic_funnel — daily channel-level funnel counts."""
    # Build order counts per day per channel
    from collections import defaultdict
    orders_per = defaultdict(int)
    for r in orders:
        try:
            d = r["created_at"][:10]
            if "/" in d:
                # DD/MM/YYYY
                parts = r["created_at"].split()[0].split("/")
                d = f"{parts[2]}-{parts[1]}-{parts[0]}"
        except Exception:
            continue
        if r["status"] in ("delivered", "returned"):
            orders_per[(d, r["channel_id"])] += 1

    rows = []
    start_date = datetime(2025, 5, 1).date()
    end_date = datetime(2025, 10, 31).date()
    for day in daterange(start_date, end_date):
        d = day.strftime("%Y-%m-%d")
        for cid, name, _, is_own in CHANNELS:
            base_orders = orders_per.get((d, cid), 0)
            # Rough funnel — visitors → views → ATC → paid
            if base_orders == 0:
                # Traffic still exists on zero-order days
                visitors = random.randint(80, 250) if is_own else random.randint(200, 600)
                views = int(visitors * random.uniform(0.35, 0.55))
                atc = int(views * random.uniform(0.05, 0.10))
                paid = 0
            else:
                # Reverse-engineer plausible funnel
                paid = base_orders
                atc = int(paid / random.uniform(0.15, 0.25))
                views = int(atc / random.uniform(0.08, 0.14))
                visitors = int(views / random.uniform(0.25, 0.40))
            rows.append({
                "date": d,
                "channel_id": cid,
                "visitors": visitors,
                "views": views,
                "add_to_cart": atc,
                "paid_orders": paid,
            })
    return rows


def generate_exclusion_list():
    """Internal exclusion list — separate from raw_ files, business-rule filtering."""
    return [
        {"customer_id": "CUST-STAFF-001", "reason": "Internal staff test account"},
        {"customer_id": "CUST-STAFF-002", "reason": "Internal QA account"},
    ]


def write_csv(rows, path, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"  Wrote {len(rows):,} rows → {path}")


def main():
    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Generating mock exports…")

    orders = generate_orders()
    write_csv(orders, out_dir / "raw_order_transaction.csv",
              ["order_item_id","order_id","created_at","customer_id","channel_id","sku",
               "item_name","quantity","unit_price","cost_price","status","promo_label"])

    catalog = generate_catalog()
    write_csv(catalog, out_dir / "raw_product_catalog.csv",
              ["sku","item_name","category","brand","cost_price","list_price"])

    channels = generate_channels()
    write_csv(channels, out_dir / "raw_channel_master.csv",
              ["channel_id","channel_name","commission_rate","is_own_store"])

    vouchers = generate_vouchers(orders)
    write_csv(vouchers, out_dir / "raw_voucher_usage.csv",
              ["voucher_id","order_id","redeemed_at","customer_id"])

    traffic = generate_traffic(orders)
    write_csv(traffic, out_dir / "raw_traffic_funnel.csv",
              ["date","channel_id","visitors","views","add_to_cart","paid_orders"])

    exclusions = generate_exclusion_list()
    write_csv(exclusions, out_dir / "exclusion_list.csv",
              ["customer_id","reason"])

    print("\nMock data generation complete.")
    print(f"  Orders (line items):  {len(orders):,}")
    print(f"  Catalog SKUs:         {len(catalog):,}")
    print(f"  Channels:             {len(channels):,}")
    print(f"  Voucher redemptions:  {len(vouchers):,}")
    print(f"  Traffic rows:         {len(traffic):,}")


if __name__ == "__main__":
    main()
