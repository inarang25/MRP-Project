"""Release this week's purchase proposals into the P2P process (handoff integration contract).

For every proposal marked "release" in the run:
  1. the planner raises one purchase requisition                  (SAP: MRP creates the PR, EBAN)
  2. the buyer for that material group creates one PO per vendor,
     with Source Document "MRP-<run id>"                          (SAP: ME59N, PR to PO)
  3. each requisition is linked to its PO line, and the buyer confirms the PO.
     A PO of Rs 10 lakh or more before GST then waits for the Purchase Head, as for any PO.

Everything goes through Odoo's own methods, as the planner and the buyers. Running it twice for
the same run is refused, and the next MRP run sees these POs as receipts on the way.

Usage: .venv/Scripts/python -m planner.release [run id, default the latest]
"""
import json
import sys
from datetime import timedelta

import pandas as pd

from planner import config
from planner.odoo import Odoo


def main():
    if len(sys.argv) > 1:
        folder = f"{config.RUN_DIR}/{sys.argv[1]}"
    else:
        folder = f"{config.RUN_DIR}/latest"
    with open(f"{folder}/run.json", encoding="utf-8") as handle:
        run = json.load(handle)
    run_id, origin = run["run_id"], f"MRP-{run['run_id']}"
    folder = f"{config.RUN_DIR}/{run_id}"

    odoo = Odoo(run["database"])
    if odoo.admin("purchase.order", "search_count", [["origin", "=", origin]]):
        sys.exit(f"Run {run_id} was already released (purchase orders with source {origin} exist).")

    orders = pd.read_csv(f"{folder}/planned_orders.csv", parse_dates=["need_date", "order_date", "due_date"])
    proposals = orders[orders["action"] == "release"].copy()
    if proposals.empty:
        sys.exit("Nothing to release in this run.")

    planner = config.PLANNER[0]
    products = odoo.admin("product.product", "search_read", [["default_code", "in", list(proposals["item"])]],
                          fields=["default_code", "gmp"])
    product_id = {row["default_code"]: row["id"] for row in products}
    is_gmp = {row["default_code"]: row["gmp"] for row in products}
    vendors = odoo.admin("res.partner", "search_read", [["ref", "in", list(proposals["vendor_ref"].unique())]],
                         fields=["ref", "property_supplier_payment_term_id"])
    vendor = {row["ref"]: row for row in vendors}

    # 1. Requisitions, raised by the planner
    requisition_id = {}
    for row in proposals.itertuples():
        requisition_id[row.order_id] = odoo.call(planner, "p2p.requisition", "create", {
            "product_id": product_id[row.item], "product_qty": float(row.qty),
            "estimated_price": float(row.unit_price), "date_needed": row.due_date.strftime("%Y-%m-%d")})

    # 2 and 3. One PO per vendor and buyer; link; confirm
    released = []
    for (vendor_ref, buyer), group in proposals.groupby(["vendor_ref", "buyer"], sort=True):
        lines = []
        for row in group.itertuples():
            # The vendor must deliver before the quality check, so the material is released by the due date
            quality_days = config.QC_DAYS_GMP if is_gmp[row.item] else config.PUTAWAY_DAYS
            delivery = row.due_date - timedelta(days=quality_days)
            lines.append((0, 0, {"product_id": product_id[row.item], "product_qty": float(row.qty),
                                 "price_unit": float(row.unit_price),
                                 "date_planned": delivery.strftime("%Y-%m-%d 06:30:00")}))   # noon in India
        term = vendor[vendor_ref]["property_supplier_payment_term_id"]
        po_id = odoo.call(buyer, "purchase.order", "create", {
            "partner_id": vendor[vendor_ref]["id"], "payment_term_id": term[0] if term else False,
            "origin": origin, "order_line": lines})
        line_ids = odoo.admin("purchase.order.line", "search", [["order_id", "=", po_id]], order="sequence, id")
        for row, line_id in zip(group.itertuples(), line_ids):
            odoo.call(buyer, "p2p.requisition", "write", [requisition_id[row.order_id]],
                      {"purchase_line_id": line_id})
        odoo.call(buyer, "purchase.order", "button_confirm", [po_id])
        po = odoo.admin("purchase.order", "read", [po_id], fields=["name", "state", "amount_untaxed"])[0]
        for row in group.itertuples():
            requisition = odoo.admin("p2p.requisition", "read", [requisition_id[row.order_id]], fields=["name"])[0]
            released.append({"order_id": row.order_id, "item": row.item, "qty": row.qty,
                             "requisition": requisition["name"], "po": po["name"], "po_state": po["state"],
                             "po_value": po["amount_untaxed"], "vendor": row.vendor, "buyer": buyer})

    released = pd.DataFrame(released)
    released.to_csv(f"{folder}/released.csv", index=False)
    released.to_csv(f"{config.RUN_DIR}/latest/released.csv", index=False)
    pos = released.drop_duplicates("po")
    waiting = pos[pos["po_state"] == "to approve"]
    print(f"Released run {run_id} as {origin}")
    print(f"  Requisitions raised by {planner}: {len(released)}")
    print(f"  Purchase orders: {len(pos)}  (Rs {pos['po_value'].sum():,.0f} before GST)")
    print(f"  Confirmed: {(pos['po_state'] == 'purchase').sum()}   Waiting for the Purchase Head: {len(waiting)}")
    for row in waiting.itertuples():
        print(f"    {row.po}  {row.vendor}  Rs {row.po_value:,.0f}")


if __name__ == "__main__":
    main()
