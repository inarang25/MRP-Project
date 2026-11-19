"""Turn planner/config.py into the plan CSVs in data/plan/ (plan first, then load, then check).

  make_items.csv        finished goods and bulk products          (SAP: material master, FERT / HALB)
  bom_lines.csv         what one batch of each consumes           (SAP: BOM, STKO / STPO)
  operations.csv        the steps and work centres per batch      (SAP: routing, PLKO / PLPO)
  demand.csv            weekly demand per finished good           (SAP: planned independent requirements, PBIM / PBED)
  planning_params.csv   safety stock and order period per material (SAP: MRP 1 / MRP 2 views, MARC)
  opening_stock.csv     the stock-take quantities at go-live      (SAP: physical inventory / initial stock, 561)

Material categories come from Odoo (read-only SQL on ProcessPro's database), the rest from config.
Usage: .venv/Scripts/python -m planner.master_data
"""
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

from planner import config
from planner.odoo import connect, sql


def build_bom_lines():
    """Every BOM line as net quantity + expected loss = gross quantity per batch (what Odoo holds)."""
    rows = []

    def add(parent, component, net, loss):
        rows.append({"parent": parent, "component": component, "net_qty": round(net, 3), "loss_pct": loss,
                     "qty_per_batch": round(net * (1 + loss / 100), 3)})

    for code, bulk in config.BULKS.items():
        # mg per tablet x batch (thousand tablets) = grams; / 1000 = kg per batch. Weighed exactly: no
        # loss per ingredient; the batch's losses are its yield.
        for material, mg in bulk["mg"].items():
            add(code, material, mg * bulk["batch"] / 1000, 0.0)
        if "shells" in bulk:
            add(code, bulk["shells"], bulk["batch"], round((config.SHELL_LOSS - 1) * 100, 3))  # thousand shells
    for code, pack in config.FINISHED.items():
        scale = pack["batch"] / 1000
        for material, quantity in pack["per_1000"].items():
            add(code, material, quantity * scale, config.loss_pct(material))
        # bulk needed, in thousand tablets
        add(code, pack["bulk"], pack["batch"] * pack["units_per_pack"] / 1000, config.BULK_LOSS_PCT)
    return rows


def build_boms(bom_lines):
    """{parent: (batch quantity, yield, {component: gross quantity per batch})}."""
    batch = {**{c: b["batch"] for c, b in config.BULKS.items()}, **{c: f["batch"] for c, f in config.FINISHED.items()}}
    boms = {}
    for row in bom_lines:
        parent = row["parent"]
        if parent not in boms:
            boms[parent] = (batch[parent], config.BULKS.get(parent, {}).get("yield", 1.0), {})
        boms[parent][2][row["component"]] = row["qty_per_batch"]
    return boms


def daily_usage(boms):
    """Average use per day of every item, from the average weekly demand exploded through the BOMs."""
    usage = {}

    def add(code, quantity):
        usage[code] = usage.get(code, 0.0) + quantity
        if code in boms:
            batch, yield_factor, lines = boms[code]
            for component, per_batch in lines.items():
                add(component, quantity * per_batch / (batch * yield_factor))

    for code, pack in config.FINISHED.items():
        add(code, pack["weekly_demand"] / 7)
    return usage


def main():
    os.makedirs(config.PLAN_DIR, exist_ok=True)
    rng = np.random.default_rng(config.SEED)
    bom_lines = build_bom_lines()
    boms = build_boms(bom_lines)

    make_items, operations = [], []
    for code, bulk in config.BULKS.items():
        make_items.append({"item": code, "name": bulk["name"], "type": "bulk", "form": bulk["form"],
                           "uom": "THOUSAND", "batch": bulk["batch"], "yield_pct": bulk["yield"] * 100,
                           "filler": bulk["filler"], "lead_days": bulk["lead_days"],
                           "shelf_life_months": config.BULK_SHELF_LIFE_MONTHS})
    for code, pack in config.FINISHED.items():
        make_items.append({"item": code, "name": pack["name"], "type": "finished", "form": pack["form"],
                           "uom": "NOS", "batch": pack["batch"], "yield_pct": 100.0, "filler": None,
                           "lead_days": pack["lead_days"],
                           "shelf_life_months": config.FINISHED_SHELF_LIFE_MONTHS})
    for row in make_items:
        for sequence, (name, centre, minutes) in enumerate(config.OPERATIONS[row["form"]], start=1):
            operations.append({"parent": row["item"], "sequence": sequence * 10, "name": name,
                               "work_centre": centre, "minutes": minutes})

    # Weekly demand, due every Monday from the week after planning starts
    start = date.fromisoformat(config.PLAN_START)
    demand = []
    for code, pack in config.FINISHED.items():
        for week in range(1, config.DEMAND_WEEKS + 1):
            factor = 1 + rng.uniform(-config.DEMAND_NOISE, config.DEMAND_NOISE)
            demand.append({"item": code, "due_date": start + timedelta(weeks=week),
                           "qty": int(round(pack["weekly_demand"] * factor, -2))})

    # Planning parameters and opening stock, from average daily usage
    with connect(config.SOURCE_DB) as connection:
        materials = sql(connection, """
            SELECT t.default_code AS item, c.name AS category, t.pack_size
            FROM product_template t JOIN product_category c ON c.id = t.categ_id
            WHERE t.default_code LIKE 'M%' ORDER BY 1""")
    category = dict(zip(materials["item"], materials["category"]))
    pack_size = dict(zip(materials["item"], materials["pack_size"]))
    usage = daily_usage(boms)

    params, opening = [], []
    classes = list(config.OPENING_COVER_MIX)
    for code in sorted(usage):
        per_day = usage[code]
        if code in config.FINISHED:
            cover = rng.uniform(*config.OPENING_FINISHED_COVER_DAYS)
            params.append({"item": code, "category": "Finished goods", "daily_usage": round(per_day, 3),
                           "safety_stock": round(per_day * config.FINISHED_SAFETY_DAYS, -2), "period_days": 1})
            opening.append({"item": code, "cover_class": "finished", "cover_days": round(cover, 1),
                            "target_qty": round(per_day * cover, -2)})
        elif code in config.BULKS:
            params.append({"item": code, "category": "Bulk", "daily_usage": round(per_day, 3),
                           "safety_stock": 0, "period_days": 1})
        else:
            group = category[code]
            cover_class = rng.choice(classes, p=[config.OPENING_COVER_MIX[c] for c in classes])
            cover = rng.uniform(*config.OPENING_COVER_DAYS[cover_class])
            pack = float(pack_size[code] or 1)
            params.append({"item": code, "category": group, "daily_usage": round(per_day, 3),
                           "safety_stock": float(np.ceil(per_day * config.SAFETY_DAYS[group] / pack) * pack),
                           "period_days": config.ORDER_PERIOD_DAYS[group],
                           "planning_potency_pct": config.PLANNING_POTENCY_PCT.get(code)})
            opening.append({"item": code, "cover_class": cover_class, "cover_days": round(cover, 1),
                            "target_qty": float(np.floor(per_day * cover / pack) * pack)})

    changes = pd.DataFrame(config.MATERIAL_CHANGES)
    for change in config.MATERIAL_CHANGES:
        old = next(row for row in params if row["item"] == change["old"])
        params.append({**old, "item": change["new"]})

    files = [("material_changes", changes.to_dict("records")), ("make_items", make_items),
             ("bom_lines", bom_lines), ("operations", operations), ("demand", demand), ("planning_params", params)]
    # The stock take happened once, at go-live (DEC-003). Its record is history: never regenerated.
    if os.path.exists(f"{config.PLAN_DIR}/opening_stock.csv"):
        print("  opening_stock    kept: the go-live stock take is already done")
    else:
        files.append(("opening_stock", opening))
    for name, rows in files:
        pd.DataFrame(rows).to_csv(f"{config.PLAN_DIR}/{name}.csv", index=False)
        print(f"  {name:16} {len(rows):4} rows")
    materials_used = [code for code in usage if code.startswith("M")]
    print(f"Plan written to {config.PLAN_DIR}: {len(config.FINISHED)} finished goods, {len(config.BULKS)} bulks, "
          f"{len(materials_used)} purchased materials planned by MRP")


if __name__ == "__main__":
    main()
