"""Build the planning database: a copy of ProcessPro's `p2p` with Manufacturing added (DEC-001).

Steps (each one is "find it, or create it", so the script can be run again safely):
  1. copy `p2p` to `p2p_mrp`            the original is never changed and nothing is ever deleted
  2. install the Manufacturing module   (SAP: activating PP)
  3. planner user                       (SAP: MRP controller)
  4. work centres
  5. finished goods and bulk products, with bills of materials and operations
  6. stock take                         brings stock to the go-live quantities, once (DEC-003)
  7. new artwork materials              from the change control register (DEC-012)

Usage: .venv/Scripts/python scripts/setup_mrp_db.py      (after `python -m planner.master_data`)
"""
import os
import sys
import xmlrpc.client

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from planner import config                                   # noqa: E402
from planner.odoo import MASTER_PASSWORD, URL, USER_PASSWORD, Odoo, connect, login_of, sql  # noqa: E402

DB = config.TARGET_DB
STOCK_TAKE_FLAG = "mrp_planner.opening_stock_done"

# ---------------------------------------------------------------------
# 1. Copy the database
# ---------------------------------------------------------------------
db_service = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/db")
if DB in db_service.list():
    print(f"Database '{DB}' already exists: not copying again, continuing with the find-or-create steps.")
else:
    print(f"Copying '{config.SOURCE_DB}' to '{DB}' ...")
    db_service.duplicate_database(MASTER_PASSWORD, config.SOURCE_DB, DB)

odoo = Odoo(DB)
company_id = odoo.admin("res.users", "read", [odoo.uid("admin")], fields=["company_id"])[0]["company_id"][0]

# ---------------------------------------------------------------------
# 2. Manufacturing module, with operations (routings) switched on
# ---------------------------------------------------------------------
module = odoo.admin("ir.module.module", "search_read", [["name", "=", "mrp"]], fields=["state"])[0]
if module["state"] != "installed":
    print("Installing Manufacturing (takes a minute) ...")
    odoo.admin("ir.module.module", "button_immediate_install", [module["id"]])
settings_id = odoo.admin("res.config.settings", "create", {"group_mrp_routings": True})
odoo.admin("res.config.settings", "execute", [settings_id])
print("Manufacturing installed, operations on")

# ---------------------------------------------------------------------
# 3. The planner
# ---------------------------------------------------------------------
name, function = config.PLANNER
login = login_of(name)
values = {"name": name, "login": login, "tz": "Asia/Kolkata", "email": f"{login}@aravelle.example",
          "function": function,
          "groups_id": [(6, 0, [odoo.ref("base.group_user"), odoo.ref("mrp.group_mrp_user")])]}
found = odoo.admin("res.users", "search", [["login", "=", login]])
if found:
    odoo.admin("res.users", "write", found, values)
else:
    odoo.admin("res.users", "create", {**values, "password": USER_PASSWORD})
print(f"Planner: {name} ({function})")

# ---------------------------------------------------------------------
# 4. Work centres
# ---------------------------------------------------------------------
centre_ids = {}
for code, centre_name in config.WORK_CENTRES.items():
    centre_ids[code] = odoo.upsert("mrp.workcenter", [["code", "=", code]], {"name": centre_name, "code": code})
print(f"Work centres: {len(centre_ids)}")

# ---------------------------------------------------------------------
# 5. Finished goods and bulks, BOMs, operations
# ---------------------------------------------------------------------
make_items = pd.read_csv(f"{config.PLAN_DIR}/make_items.csv")
bom_lines = pd.read_csv(f"{config.PLAN_DIR}/bom_lines.csv")
operations = pd.read_csv(f"{config.PLAN_DIR}/operations.csv")

uom_ids = {"NOS": odoo.ref("uom.product_uom_unit"),
           "THOUSAND": odoo.admin("uom.uom", "search", [["name", "=", "Thousand"]])[0]}
all_category_id = odoo.ref("product.product_category_all")
category_ids = {kind: odoo.upsert("product.category", [["name", "=", label]],
                                  {"name": label, "parent_id": all_category_id})
                for kind, label in [("finished", "Finished goods"), ("bulk", "Bulk products")]}
manufacture_route = odoo.ref("mrp.route_warehouse0_manufacture")

template_ids = {}
for _, row in make_items.iterrows():
    template_ids[row["item"]] = odoo.upsert("product.template", [["default_code", "=", row["item"]]], {
        "name": row["name"], "default_code": row["item"], "categ_id": category_ids[row["type"]],
        "detailed_type": "product", "uom_id": uom_ids[row["uom"]], "uom_po_id": uom_ids[row["uom"]],
        "purchase_ok": False, "sale_ok": row["type"] == "finished",
        "route_ids": [(6, 0, [manufacture_route])],
        "gmp": True, "tracking": "lot", "use_expiration_date": True,       # every batch is traceable
        "expiration_time": int(row["shelf_life_months"] * 30)})

# New artwork = a new material, copied from the one it replaces, with the same approved printers.
# (In a real plant QA also approves the printer's proof of the new artwork before the first order.)
changes = pd.read_csv(f"{config.PLAN_DIR}/material_changes.csv")
COPY_FIELDS = ["categ_id", "detailed_type", "uom_id", "uom_po_id", "purchase_method", "standard_price",
               "supplier_taxes_id", "gmp", "moisture_sensitive", "pack_size", "tracking", "use_expiration_date",
               "expiration_time"]
VENDOR_FIELDS = ["partner_id", "price", "currency_id", "delay", "min_qty", "preferred", "quality_agreement",
                 "avl_status", "qualified_on"]
copies = [(c.old, c.new, c.new_name) for c in changes.itertuples()] + \
    [(m["copy_of"], m["new"], m["new_name"]) for m in config.NEW_MATERIALS]     # e.g. the Government supply carton
for old_code, new_code, new_name in copies:
    old = odoo.admin("product.template", "search_read", [["default_code", "=", old_code]], fields=COPY_FIELDS)[0]
    values = {field: (value[0] if isinstance(value, list) and field != "supplier_taxes_id" else value)
              for field, value in old.items() if field != "id"}
    values["supplier_taxes_id"] = [(6, 0, old["supplier_taxes_id"])]
    new_id = odoo.upsert("product.template", [["default_code", "=", new_code]],
                         {**values, "name": new_name, "default_code": new_code})
    for vendor in odoo.admin("product.supplierinfo", "search_read", [["product_tmpl_id", "=", old["id"]]],
                             fields=VENDOR_FIELDS):
        copy = {field: (vendor[field][0] if isinstance(vendor[field], list) else vendor[field]) for field in VENDOR_FIELDS}
        odoo.upsert("product.supplierinfo", [["product_tmpl_id", "=", new_id], ["partner_id", "=", copy["partner_id"]]],
                    {**copy, "product_tmpl_id": new_id})
print("New printed materials: " + ", ".join(f"{new} (copied from {old})" for old, new, _ in copies))

variants = odoo.admin("product.product", "search_read", [["default_code", "!=", False]], fields=["default_code"])
variant_id = {variant["default_code"]: variant["id"] for variant in variants}

bom_changes = []
for _, row in make_items.iterrows():
    code = row["item"]
    lines = bom_lines[bom_lines["parent"] == code]
    steps = operations[operations["parent"] == code]
    values = {"product_tmpl_id": template_ids[code], "product_qty": float(row["batch"]),
              "product_uom_id": uom_ids[row["uom"]], "type": "normal", "code": f"{code} standard batch",
              "produce_delay": int(row["lead_days"])}
    found = odoo.admin("mrp.bom", "search", [["product_tmpl_id", "=", template_ids[code]]], limit=1)
    if found:
        # Existing BOM: correct only the lines whose quantity differs from the plan (Odoo keeps the history)
        planned = dict(zip(lines["component"].map(variant_id), lines["qty_per_batch"]))
        for line in odoo.admin("mrp.bom.line", "search_read", [["bom_id", "=", found[0]]],
                               fields=["product_id", "product_qty"]):
            target = planned.get(line["product_id"][0])
            if target is not None and abs(line["product_qty"] - target) > 1e-6:
                odoo.admin("mrp.bom.line", "write", [line["id"]], {"product_qty": float(target)})
                bom_changes.append((code, line["product_id"][1], line["product_qty"], target))
        continue
    values["bom_line_ids"] = [(0, 0, {"product_id": variant_id[line["component"]],
                                      "product_qty": float(line["qty_per_batch"])}) for _, line in lines.iterrows()]
    values["operation_ids"] = [(0, 0, {"name": step["name"], "workcenter_id": centre_ids[step["work_centre"]],
                                       "sequence": int(step["sequence"]), "time_mode": "manual",
                                       "time_cycle_manual": float(step["minutes"])}) for _, step in steps.iterrows()]
    odoo.admin("mrp.bom", "create", values)
print(f"Products: {len(template_ids)} finished and bulk, each with a BOM and operations")
for code, component, before, after in bom_changes:
    print(f"  BOM {code}: {component} {before:g} -> {after:g}")

# ---------------------------------------------------------------------
# 6. Stock take at go-live (once)
# ---------------------------------------------------------------------
# ProcessPro's year received every purchase but never consumed anything, because production was
# not on the system. Before the first MRP run the stores count the stock, as at any go-live.
# Odoo lets only a stock manager approve a count: Stores enters it, the administrator approves it.
if odoo.admin("ir.config_parameter", "get_param", STOCK_TAKE_FLAG):
    print("Stock take already done: skipped")
else:
    opening = pd.read_csv(f"{config.PLAN_DIR}/opening_stock.csv")
    target = dict(zip(opening["item"], opening["target_qty"]))
    with connect(DB) as connection:
        quants = sql(connection, """
            SELECT q.id, t.default_code AS item, q.quantity::float8 AS quantity, q.location_id
            FROM stock_quant q
            JOIN stock_location l ON l.id = q.location_id AND l.usage = 'internal'
            JOIN product_product p ON p.id = q.product_id
            JOIN product_template t ON t.id = p.product_tmpl_id
            LEFT JOIN stock_lot lot ON lot.id = q.lot_id
            WHERE t.default_code = ANY(%s)
            ORDER BY t.default_code, lot.expiration_date DESC NULLS LAST, q.id DESC""",
                     ([code for code in target if code.startswith("M")],))
    stock_location = int(quants["location_id"].mode()[0])
    stores = config.STORES_USER
    counting = {"context": {"inventory_mode": True}}

    # Keep the freshest batches up to the counted quantity; everything older was consumed
    to_zero, partial, short = [], {}, []
    for code, rows in quants.groupby("item"):
        remaining = float(target[code])
        for _, quant in rows.iterrows():
            if remaining >= quant["quantity"]:
                remaining -= quant["quantity"]
            elif remaining > 0:
                partial[int(quant["id"])] = round(remaining, 3)
                remaining = 0
            else:
                to_zero.append(int(quant["id"]))
        if remaining > 0:
            short.append((code, remaining))
    for start in range(0, len(to_zero), 200):
        chunk = to_zero[start:start + 200]
        odoo.call(stores, "stock.quant", "write", chunk, {"inventory_quantity": 0}, **counting)
        odoo.admin("stock.quant", "action_apply_inventory", chunk, **counting)
    for quant_id, quantity in partial.items():
        odoo.call(stores, "stock.quant", "write", [quant_id], {"inventory_quantity": quantity}, **counting)
    if partial:
        odoo.admin("stock.quant", "action_apply_inventory", list(partial), **counting)

    # Finished goods on hand: one opening batch each
    for code, quantity in target.items():
        if code.startswith("M") or quantity <= 0:
            continue
        lot_id = odoo.call(stores, "stock.lot", "create",
                           {"name": f"OPEN-{code}", "product_id": variant_id[code], "company_id": company_id})
        quant_id = odoo.call(stores, "stock.quant", "create",
                             {"product_id": variant_id[code], "location_id": stock_location, "lot_id": lot_id,
                              "inventory_quantity": float(quantity)}, **counting)
        odoo.admin("stock.quant", "action_apply_inventory", [quant_id], **counting)

    odoo.admin("ir.config_parameter", "set_param", STOCK_TAKE_FLAG, "1")
    print(f"Stock take by {stores}: {len(to_zero)} batches counted as used up, {len(partial)} part-used")
    for code, quantity in short:
        print(f"  note: {code} has {quantity:,.0f} less in Odoo than the plan's opening stock; kept what exists")

# ---------------------------------------------------------------------
# Read-back check: Odoo vs plan
# ---------------------------------------------------------------------
print("\nREAD-BACK (Odoo vs plan)")
print(f"  Finished + bulk products {odoo.admin('product.template', 'search_count', [['default_code', 'in', list(template_ids)]]):4}  plan {len(make_items)}")
print(f"  BOMs                     {odoo.admin('mrp.bom', 'search_count', []):4}  plan {len(make_items)}")
print(f"  BOM lines                {odoo.admin('mrp.bom.line', 'search_count', []):4}  plan {len(bom_lines)}")
print(f"  Operations               {odoo.admin('mrp.routing.workcenter', 'search_count', []):4}  plan {len(operations)}")
print(f"  Work centres             {odoo.admin('mrp.workcenter', 'search_count', []):4}  plan {len(config.WORK_CENTRES)}")
