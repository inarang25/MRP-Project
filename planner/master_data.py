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


def build_boms():
    """{parent: (batch quantity, {component: quantity per batch})} for bulks and finished goods."""
    boms = {}
    for code, bulk in config.BULKS.items():
        # mg per tablet x batch (thousand tablets) = grams; / 1000 = kg per batch
        lines = {material: round(mg * bulk["batch"] / 1000, 3) for material, mg in bulk["mg"].items()}
        if "shells" in bulk:
            lines[bulk["shells"]] = round(bulk["batch"] * config.SHELL_LOSS, 3)     # in thousand shells
        boms[code] = (bulk["batch"], lines)
    for code, pack in config.FINISHED.items():
        scale = pack["batch"] / 1000
        lines = {material: round(quantity * scale, 3) for material, quantity in pack["per_1000"].items()}
        # bulk needed, in thousand tablets
        lines[pack["bulk"]] = round(pack["batch"] * pack["units_per_pack"] / 1000 * config.BULK_YIELD, 3)
        boms[code] = (pack["batch"], lines)
    return boms


def daily_usage(boms):
    """Average use per day of every item, from the average weekly demand exploded through the BOMs."""
    usage = {}

    def add(code, quantity):
        usage[code] = usage.get(code, 0.0) + quantity
        if code in boms:
            batch, lines = boms[code]
            for component, per_batch in lines.items():
                add(component, quantity * per_batch / batch)

    for code, pack in config.FINISHED.items():
        add(code, pack["weekly_demand"] / 7)
    return usage


def main():
    os.makedirs(config.PLAN_DIR, exist_ok=True)
    rng = np.random.default_rng(config.SEED)
    boms = build_boms()

    make_items, operations = [], []
    for code, bulk in config.BULKS.items():
        make_items.append({"item": code, "name": bulk["name"], "type": "bulk", "form": bulk["form"],
                           "uom": "THOUSAND", "batch": bulk["batch"], "lead_days": bulk["lead_days"],
                           "shelf_life_months": config.BULK_SHELF_LIFE_MONTHS})
    for code, pack in config.FINISHED.items():
        make_items.append({"item": code, "name": pack["name"], "type": "finished", "form": pack["form"],
                           "uom": "NOS", "batch": pack["batch"], "lead_days": pack["lead_days"],
                           "shelf_life_months": config.FINISHED_SHELF_LIFE_MONTHS})
    for row in make_items:
        for sequence, (name, centre, minutes) in enumerate(config.OPERATIONS[row["form"]], start=1):
            operations.append({"parent": row["item"], "sequence": sequence * 10, "name": name,
                               "work_centre": centre, "minutes": minutes})
    bom_lines = [{"parent": parent, "component": component, "qty_per_batch": quantity}
                 for parent, (_, lines) in boms.items() for component, quantity in lines.items()]

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
                           "period_days": config.ORDER_PERIOD_DAYS[group]})
            opening.append({"item": code, "cover_class": cover_class, "cover_days": round(cover, 1),
                            "target_qty": float(np.floor(per_day * cover / pack) * pack)})

    for name, rows in [("make_items", make_items), ("bom_lines", bom_lines), ("operations", operations),
                       ("demand", demand), ("planning_params", params), ("opening_stock", opening)]:
        pd.DataFrame(rows).to_csv(f"{config.PLAN_DIR}/{name}.csv", index=False)
        print(f"  {name:16} {len(rows):4} rows")
    materials_used = [code for code in usage if code.startswith("M")]
    print(f"Plan written to {config.PLAN_DIR}: {len(config.FINISHED)} finished goods, {len(config.BULKS)} bulks, "
          f"{len(materials_used)} purchased materials planned by MRP")


if __name__ == "__main__":
    main()
