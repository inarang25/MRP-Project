"""One MRP run: read the planning situation from Odoo, calculate, write the results.

Reads with SQL only (BOMs, stock, open purchase orders, open requisitions, vendor records), the
same way ProcessPro's extractor reads. Demand and planning parameters come from data/plan/:
the forecast, consumed by customer orders, plus tenders already won. A tender still waiting for its
result is not planned; a second calculation shows what winning it would need (tender_impact.csv).
Nothing is written to Odoo here: releasing proposals is a separate step (planner/release.py).

Outputs in data/mrp/<run id>/ and a copy in data/mrp/latest/:
  planned_orders.csv   production orders to start and purchase proposals to place   (SAP: MD04 planned orders / PRs)
  requirements.csv     every requirement and the order or demand that caused it      (SAP: pegging, MD09)
  projection.csv       stock projection per item and date                           (SAP: MD04 stock/requirements list)
  exceptions.csv       what the planner must look at                                (SAP: exception messages, MD06)
  tender_impact.csv    open tenders: what winning needs, and what must be bought before the result
  run.json             when, on which database, counts

Usage: .venv/Scripts/python -m planner.run [--today YYYY-MM-DD]
"""
import argparse
import json
import os
import shutil
from datetime import date, timedelta

import pandas as pd

from planner import config
from planner.engine import Bom, Item, adjust_for_potency, consume_forecast, run_mrp
from planner.odoo import DATABASE, connect, sql

ITEMS = """
    SELECT t.default_code AS item, t.name->>'en_US' AS name, c.name AS category, COALESCE(t.gmp, false) AS gmp,
           COALESCE(t.pack_size, 0)::float8 AS pack_size, u.name->>'en_US' AS uom, t.id AS template_id
    FROM product_template t
    JOIN product_category c ON c.id = t.categ_id
    JOIN uom_uom u ON u.id = t.uom_id
    WHERE t.default_code IS NOT NULL AND t.active"""
BOMS = """
    SELECT parent.default_code AS parent, b.product_qty::float8 AS batch_qty, b.produce_delay::int AS lead_days,
           component.default_code AS component, l.product_qty::float8 AS qty
    FROM mrp_bom b
    JOIN product_template parent ON parent.id = b.product_tmpl_id
    JOIN mrp_bom_line l ON l.bom_id = b.id
    JOIN product_product p ON p.id = l.product_id
    JOIN product_template component ON component.id = p.product_tmpl_id
    WHERE b.active"""
STOCK = """
    SELECT t.default_code AS item, l.complete_name AS location, lot.name AS lot,
           lot.expiration_date::date AS expiry, q.quantity::float8 AS qty
    FROM stock_quant q
    JOIN stock_location l ON l.id = q.location_id AND l.usage = 'internal'
    JOIN product_product p ON p.id = q.product_id
    JOIN product_template t ON t.id = p.product_tmpl_id
    LEFT JOIN stock_lot lot ON lot.id = q.lot_id
    WHERE q.quantity > 0"""
OPEN_ORDERS = """
    SELECT t.default_code AS item, o.name AS po, o.origin, o.state, l.date_planned::date AS due,
           (l.product_qty - l.qty_received)::float8 AS qty
    FROM purchase_order_line l
    JOIN purchase_order o ON o.id = l.order_id
    JOIN product_product p ON p.id = l.product_id
    JOIN product_template t ON t.id = p.product_tmpl_id
    WHERE o.state IN ('to approve', 'purchase', 'done') AND l.product_qty > l.qty_received"""
OPEN_REQUISITIONS = """
    SELECT t.default_code AS item, r.name AS requisition, r.date_needed AS due, r.product_qty::float8 AS qty
    FROM p2p_requisition r
    JOIN product_product p ON p.id = r.product_id
    JOIN product_template t ON t.id = p.product_tmpl_id
    WHERE r.state = 'requested' AND r.purchase_line_id IS NULL AND r.date_needed IS NOT NULL"""
VENDORS = """
    SELECT t.default_code AS item, v.ref AS vendor_ref, v.name AS vendor, s.price::float8 AS price,
           s.delay::int AS lead_days, s.min_qty::float8 AS min_qty, s.avl_status, COALESCE(s.preferred, false) AS preferred
    FROM product_supplierinfo s
    JOIN product_template t ON t.id = s.product_tmpl_id
    JOIN res_partner v ON v.id = s.partner_id"""


def choose_vendor(rows, gmp):
    """GMP materials may only come from an approved vendor; then the preferred one; then the cheapest."""
    if gmp:
        rows = rows[rows["avl_status"] == "approved"]
    if rows.empty:
        return None
    return rows.sort_values(["preferred", "price"], ascending=[False, True]).iloc[0]


def tender_impact(open_tenders, tender_lots, items, boms, demand_rows, receipts, today, base, params, price,
                  products):
    """For each tender waiting for its result: plan again as if it were won, and compare.

    For every purchased material the tender needs more of: how much, worth how much, the first day an
    extra order must be placed, and whether that is before the result. Materials that regular products
    also use are a small risk to buy early (normal demand uses them up if the tender is lost); materials
    only the tender uses are lost money if the tender is lost."""
    columns = ["tender", "item", "name", "category", "regular_use", "extra_qty", "uom", "extra_value",
               "first_order_date", "result_date", "before_result", "decision"]
    rows = []
    regular = {r["item"] for r in base.requirements if r["qty"] > 0}
    base_orders = pd.DataFrame(base.orders)
    base_need = pd.DataFrame(base.requirements).groupby("item")["qty"].sum()
    for tender, lots in open_tenders.groupby("tender"):
        result_date = lots["result_date"].iloc[0].date()
        won = run_mrp(items, boms, demand_rows + tender_lots(lots), receipts, today)
        scenario = pd.DataFrame(won.orders)
        # Extra need = what the tender consumes (requirements), not extra orders, which round up to lots
        need = pd.DataFrame(won.requirements).groupby("item")["qty"].sum().sub(base_need, fill_value=0)
        for code in sorted(set(scenario.loc[scenario["kind"] == "buy", "item"])):
            extra = float(need.get(code, 0))
            if extra <= 1e-6:
                continue                      # no extra quantity (at most an order a few days earlier)
            mine = scenario[scenario["item"] == code].groupby("order_date")["qty"].sum()
            theirs = base_orders[base_orders["item"] == code].groupby("order_date")["qty"].sum()
            both = pd.concat([mine.rename("tender"), theirs.rename("base")], axis=1).fillna(0).sort_index()
            gap = (both["tender"] - both["base"]).cumsum()
            if (gap <= 1e-6).all():
                continue                      # covered by stock and orders already planned
            first = gap[gap > 1e-6].index[0]
            before = first < result_date
            uses_it = code in regular
            per_day = float(params["daily_usage"].get(code, 0) or 0)
            if before and uses_it:
                decision = "order before the result"
                if per_day:
                    decision += f"; if the tender is lost, regular demand uses it up in about {extra / per_day:,.0f} days"
            elif before:
                decision = (f"needed {(result_date - first).days} days before the result and used only by the "
                            f"tender: approve the artwork and book the printer now, order on the award day, and "
                            f"agree the first delivery date with the authority")
            elif uses_it:
                decision = f"wait for the result; order by {first}"
            else:
                decision = (f"wait for the result; order by {first}, {(first - result_date).days} days after it: "
                            f"have the artwork approved and a printer slot booked before the result")
            rows.append({"tender": tender, "item": code, "name": products.loc[code, "name"],
                         "category": products.loc[code, "category"], "regular_use": uses_it,
                         "extra_qty": round(extra, 3), "uom": products.loc[code, "uom"],
                         "extra_value": round(extra * price.get(code, 0), 2), "first_order_date": first,
                         "result_date": result_date, "before_result": before, "decision": decision})
    return pd.DataFrame(rows, columns=columns)


def next_run_id(today):
    prefix = today.strftime("%Y%m%d")
    existing = [name for name in os.listdir(config.RUN_DIR) if name.startswith(prefix)] \
        if os.path.isdir(config.RUN_DIR) else []
    return f"{prefix}-{len(existing) + 1:02d}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", default=None, help="planning date, default the real date")
    arguments = parser.parse_args()
    today = date.fromisoformat(arguments.today) if arguments.today else date.today()

    with connect() as connection:
        products = sql(connection, ITEMS)
        bom_rows = sql(connection, BOMS)
        stock = sql(connection, STOCK)
        open_orders = sql(connection, OPEN_ORDERS)
        open_requisitions = sql(connection, OPEN_REQUISITIONS)
        vendors = sql(connection, VENDORS)
    forecast = pd.read_csv(f"{config.PLAN_DIR}/forecast.csv", parse_dates=["due_date"])
    customer_orders = pd.read_csv(f"{config.PLAN_DIR}/customer_orders.csv", parse_dates=["due_date"])
    tenders = pd.read_csv(f"{config.PLAN_DIR}/tenders.csv", parse_dates=["due_date", "result_date"])
    params = pd.read_csv(f"{config.PLAN_DIR}/planning_params.csv").set_index("item")
    make_plan = pd.read_csv(f"{config.PLAN_DIR}/make_items.csv").set_index("item")
    changes = pd.read_csv(f"{config.PLAN_DIR}/material_changes.csv", parse_dates=["last_use"]).set_index("old")
    products = products.set_index("item")
    exceptions = []

    # BOMs, and the set of items MRP plans: everything made, and everything those consume
    boms = {parent: Bom(float(rows["batch_qty"].iloc[0]), dict(zip(rows["component"], rows["qty"])))
            for parent, rows in bom_rows.groupby("parent")}
    make_lead = dict(zip(bom_rows["parent"], bom_rows["lead_days"]))

    # Potency: plan each API at its planning potency, taking the extra weight off the filler (lesson 2)
    for parent, bom in boms.items():
        for component in list(bom.lines):
            potency = params["planning_potency_pct"].get(component) if "planning_potency_pct" in params else None
            if pd.notna(potency) and potency:
                filler = make_plan["filler"].get(parent)
                bom.lines = adjust_for_potency(bom.lines, component, float(potency),
                                               filler if isinstance(filler, str) else None)
    planned_codes = set(boms) | set(bom_rows["component"])
    planned_codes |= {changes.loc[old, "new"] for old in changes.index if old in planned_codes}

    # Stock: released stock counts now, stock in quality check counts once released, expired stock never
    stock = stock[stock["item"].isin(planned_codes)]
    expired = stock[stock["expiry"].notna() & (stock["expiry"] < today)]
    for code, rows in expired.groupby("item"):
        exceptions.append({"type": "expired_stock", "item": code, "order_id": "", "date": today,
                           "detail": f"{rows['qty'].sum():,.0f} in {len(rows)} expired batches, not counted"})
    usable = stock.drop(expired.index)
    released = usable[usable["location"].str.startswith("WH/Stock")].groupby("item")["qty"].sum()
    in_check = usable[~usable["location"].str.startswith("WH/Stock")].groupby("item")["qty"].sum()

    items, chosen = {}, {}
    for code in sorted(planned_codes):
        product = products.loc[code]
        safety = float(params["safety_stock"].get(code, 0))
        on_hand = float(released.get(code, 0))
        change = {}
        if code in changes.index:          # discontinued artwork: no safety stock, hands over to the new one
            row = changes.loc[code]
            change = {"follow_up": row["new"], "last_use": row["last_use"].date()}
            safety = 0.0
        if code in boms:
            good = boms[code].batch_qty * float(make_plan["yield_pct"].get(code, 100)) / 100
            items[code] = Item(code, "make", lead_days=int(make_lead[code]), on_hand=on_hand, safety_stock=safety,
                               min_lot=good, multiple=good, yield_factor=good / boms[code].batch_qty)
            continue
        vendor = choose_vendor(vendors[vendors["item"] == code], product["gmp"])
        quality_days = config.QC_DAYS_GMP if product["gmp"] else config.PUTAWAY_DAYS
        if vendor is None:
            exceptions.append({"type": "no_approved_vendor", "item": code, "order_id": "", "date": today,
                               "detail": "no approved vendor on the list: proposals cannot be released"})
            items[code] = Item(code, "buy", lead_days=quality_days, on_hand=on_hand, safety_stock=safety, **change)
            continue
        chosen[code] = vendor
        items[code] = Item(code, "buy", lead_days=int(vendor["lead_days"]) + quality_days, on_hand=on_hand,
                           safety_stock=safety, min_lot=float(vendor["min_qty"]), multiple=product["pack_size"],
                           period_days=int(params["period_days"].get(code, 7)), **change)

    def quality_days_of(code):
        return config.QC_DAYS_GMP if products.loc[code, "gmp"] else config.PUTAWAY_DAYS

    # Receipts already on the way
    receipts = []
    for code, quantity in in_check.items():
        receipts.append((code, today + timedelta(days=quality_days_of(code)), float(quantity), "In quality check"))
    counted_orders = 0
    for _, row in open_orders[open_orders["item"].isin(planned_codes)].iterrows():
        if row["due"] < today - timedelta(days=config.OVERDUE_IGNORE_DAYS):
            exceptions.append({"type": "overdue_order_ignored", "item": row["item"], "order_id": row["po"],
                               "date": row["due"],
                               "detail": f"{row['qty']:,.0f} still open on {row['po']}, due {row['due']}: "
                                         f"not counted as a future receipt; close or chase it"})
            continue
        counted_orders += 1
        receipts.append((row["item"], max(row["due"], today) + timedelta(days=quality_days_of(row["item"])),
                         row["qty"], row["po"]))
    for _, row in open_requisitions[open_requisitions["item"].isin(planned_codes)].iterrows():
        receipts.append((row["item"], max(row["due"], today), row["qty"], row["requisition"]))

    # Demand: orders consume the forecast; a won tender is a firm order for each of its lots
    def tender_lots(rows):
        return [(row.item, row.due_date.date(), float(row.qty), f"{row.tender} lot {row.lot}")
                for row in rows.itertuples()]
    fc = [(row.item, row.due_date.date(), float(row.qty)) for row in forecast.itertuples()]
    booked = [(row.item, row.due_date.date(), float(row.qty), row.order) for row in customer_orders.itertuples()]
    demand_rows = [line for line in consume_forecast(fc, booked + tender_lots(tenders[tenders["status"] == "won"]))
                   if line[0] in items]

    result = run_mrp(items, boms, demand_rows, receipts, today)
    price = {code: float(vendor["price"]) for code, vendor in chosen.items()}
    impact = tender_impact(tenders[tenders["status"] == "bid"], tender_lots, items, boms, demand_rows, receipts,
                           today, result, params, price, products)

    # ----- results -----
    run_id = next_run_id(today)
    orders = pd.DataFrame(result.orders)
    orders["name"] = orders["item"].map(products["name"])
    orders["category"] = orders["item"].map(products["category"])
    orders["uom"] = orders["item"].map(products["uom"])
    for column, key in [("vendor_ref", "vendor_ref"), ("vendor", "vendor"), ("unit_price", "price")]:
        orders[column] = orders["item"].map(lambda code, key=key: chosen[code][key] if code in chosen else None)
    orders["value"] = (orders["qty"] * orders["unit_price"]).round(2)            # before GST
    orders["buyer"] = orders.apply(
        lambda row: config.BUYER_FOR_CATEGORY.get(row["category"], config.DEFAULT_BUYER) if row["kind"] == "buy" else "",
        axis=1)
    window_end = today + timedelta(days=config.RELEASE_WINDOW_DAYS)
    orders["action"] = orders.apply(
        lambda row: "plan" if row["kind"] == "make"
        else "release" if row["order_date"] <= window_end and pd.notna(row["vendor_ref"]) else "later", axis=1)

    exceptions = pd.DataFrame(exceptions + result.exceptions,
                              columns=["type", "item", "order_id", "date", "detail", "qty"])
    exceptions["name"] = exceptions["item"].map(products["name"])
    # Value of what will be scrapped, at the vendor price (before GST)
    exceptions["value"] = (exceptions["qty"] * exceptions["item"].map(price)).round(2)
    requirements = pd.DataFrame(result.requirements)
    projection = pd.DataFrame(result.projection)

    buy = orders[orders["kind"] == "buy"]
    release = buy[buy["action"] == "release"]
    summary = {
        "run_id": run_id, "database": DATABASE, "planning_date": str(today),
        "items_planned": len(items), "finished_goods": int((products.loc[list(boms), "category"] == "Finished goods").sum()),
        "demand_lines": len(demand_rows), "open_orders_counted": counted_orders,
        "production_orders": int((orders["kind"] == "make").sum()),
        "purchase_proposals": len(buy), "purchase_value": round(float(buy["value"].sum()), 2),
        "to_release_now": len(release), "to_release_value": round(float(release["value"].sum()), 2),
        "late_orders": int((orders["late_days"] > 0).sum()),
        "write_off_value": round(float(exceptions.loc[exceptions["type"] == "write_off", "value"].sum()), 2),
        "exceptions": exceptions["type"].value_counts().to_dict(),
        "demand_by_source": {
            "forecast": round(sum(d[2] for d in demand_rows if d[3] == "Forecast")),
            "customer_orders": round(sum(d[2] for d in demand_rows if d[3].startswith("SO-"))),
            "tenders_won": round(sum(d[2] for d in demand_rows if " lot " in d[3]))},
        "open_tenders": {
            tender: {"extra_value": round(float(rows["extra_value"].sum()), 2),
                     "to_buy_before_result": round(float(rows.loc[rows["before_result"], "extra_value"].sum()), 2),
                     "of_which_tender_only": round(float(rows.loc[rows["before_result"] & ~rows["regular_use"],
                                                                   "extra_value"].sum()), 2)}
            for tender, rows in impact.groupby("tender")},
    }

    folder = f"{config.RUN_DIR}/{run_id}"
    os.makedirs(folder)
    orders.to_csv(f"{folder}/planned_orders.csv", index=False)
    requirements.to_csv(f"{folder}/requirements.csv", index=False)
    projection.to_csv(f"{folder}/projection.csv", index=False)
    exceptions.to_csv(f"{folder}/exceptions.csv", index=False)
    impact.to_csv(f"{folder}/tender_impact.csv", index=False)
    with open(f"{folder}/run.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    latest = f"{config.RUN_DIR}/latest"
    shutil.rmtree(latest, ignore_errors=True)
    shutil.copytree(folder, latest)

    print(f"MRP run {run_id} on {DATABASE}, planning date {today}")
    print(f"  Items planned          {summary['items_planned']}")
    print(f"  Production orders      {summary['production_orders']}")
    print(f"  Purchase proposals     {summary['purchase_proposals']}  (Rs {summary['purchase_value']:,.0f} before GST)")
    print(f"  To release this week   {summary['to_release_now']}  (Rs {summary['to_release_value']:,.0f})")
    print(f"  Late orders            {summary['late_orders']}")
    print(f"  Artwork write-off      Rs {summary['write_off_value']:,.0f}")
    for kind, count in summary["exceptions"].items():
        print(f"  Exception: {kind:24} {count}")
    for tender, figures in summary["open_tenders"].items():
        print(f"  Open tender {tender}: winning needs Rs {figures['extra_value']:,.0f} more materials; "
              f"Rs {figures['to_buy_before_result']:,.0f} must be ordered before the result "
              f"(Rs {figures['of_which_tender_only']:,.0f} of it usable only for the tender)")
    print(f"Results in {folder}")


if __name__ == "__main__":
    main()
