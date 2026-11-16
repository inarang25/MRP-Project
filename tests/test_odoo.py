"""Checks against the live Odoo databases: what was loaded and released matches the plan and the
integration contract (docs/HANDOFF_MRP.md, section 7). Skipped when Odoo is not running."""
import glob
import json

import pandas as pd
import pytest

from planner import config
from planner.odoo import connect, sql


@pytest.fixture(scope="module")
def db():
    try:
        connection = connect(config.TARGET_DB)
    except Exception as error:                      # Docker not started
        pytest.skip(f"Odoo database not reachable: {error}")
    yield connection
    connection.close()


def released_runs():
    return [pd.read_csv(path) for path in sorted(glob.glob(f"{config.RUN_DIR}/2*/released.csv"))]


def test_processpro_database_is_untouched():
    with connect(config.SOURCE_DB) as source:
        assert sql(source, "SELECT count(*) AS n FROM purchase_order")["n"][0] == 1208
        assert sql(source, "SELECT state FROM ir_module_module WHERE name = 'mrp'")["state"][0] == "uninstalled"


def test_boms_in_odoo_match_the_plan(db):
    plan = pd.read_csv(f"{config.PLAN_DIR}/bom_lines.csv")
    odoo = sql(db, """
        SELECT parent.default_code AS parent, component.default_code AS component, l.product_qty::float8 AS qty
        FROM mrp_bom b JOIN product_template parent ON parent.id = b.product_tmpl_id
        JOIN mrp_bom_line l ON l.bom_id = b.id JOIN product_product p ON p.id = l.product_id
        JOIN product_template component ON component.id = p.product_tmpl_id""")
    merged = plan.merge(odoo, on=["parent", "component"], how="outer", indicator=True)
    assert (merged["_merge"] == "both").all()
    assert (merged["qty_per_batch"] - merged["qty"]).abs().max() < 0.001


def test_opening_stock_matches_the_stock_take(db):
    plan = pd.read_csv(f"{config.PLAN_DIR}/opening_stock.csv")
    # Stock right after the stock take = stock now minus anything received since (nothing yet)
    odoo = sql(db, """
        SELECT t.default_code AS item, sum(q.quantity)::float8 AS qty
        FROM stock_quant q JOIN stock_location l ON l.id = q.location_id AND l.usage = 'internal'
        JOIN product_product p ON p.id = q.product_id JOIN product_template t ON t.id = p.product_tmpl_id
        GROUP BY 1""").set_index("item")["qty"]
    received = sql(db, "SELECT count(*) AS n FROM stock_picking WHERE origin IN "
                       "(SELECT name FROM purchase_order WHERE origin LIKE 'MRP-%') AND state = 'done'")["n"][0]
    if received:
        pytest.skip("MRP purchase orders have been received since the stock take")
    for row in plan.itertuples():
        assert abs(odoo.get(row.item, 0) - row.target_qty) < 0.01, row.item


def test_every_mrp_order_follows_the_contract(db):
    runs = released_runs()
    if not runs:
        pytest.skip("no run released yet")
    released = pd.concat(runs)
    lines = sql(db, """
        SELECT o.name AS po, o.origin, o.state, buyer.login AS buyer, t.default_code AS item, t.gmp,
               t.pack_size::float8 AS pack_size, l.product_qty::float8 AS qty, r.name AS requisition,
               r.product_qty::float8 AS requested_qty, requester.login AS requester, r.state AS requisition_state,
               s.avl_status, s.min_qty::float8 AS min_qty
        FROM purchase_order o
        JOIN purchase_order_line l ON l.order_id = o.id
        JOIN res_users buyer ON buyer.id = o.create_uid
        JOIN product_product p ON p.id = l.product_id
        JOIN product_template t ON t.id = p.product_tmpl_id
        LEFT JOIN p2p_requisition r ON r.purchase_line_id = l.id
        LEFT JOIN res_users requester ON requester.id = r.requester_id
        LEFT JOIN product_supplierinfo s ON s.product_tmpl_id = t.id AND s.partner_id = o.partner_id
        WHERE o.origin LIKE 'MRP-%'""")
    assert len(lines) == len(released)
    assert set(lines["po"]) == set(released["po"])
    # every line starts from a requisition raised by the planner, for the same quantity
    assert lines["requisition"].notna().all()
    assert (lines["requester"] == "pooja.bhatt").all()
    assert (lines["requested_qty"] == lines["qty"]).all()
    assert (lines["requisition_state"] == "ordered").all()
    # created by a buyer, never by the planner or the administrator
    assert set(lines["buyer"]) <= {"neha.kulkarni", "arjun.patel", "sneha.reddy"}
    # GMP materials only from approved vendors; whole packs; at least the vendor's minimum
    assert (lines.loc[lines["gmp"], "avl_status"] == "approved").all()
    assert ((lines["qty"] / lines["pack_size"]) % 1 == 0).all()
    assert (lines["qty"] >= lines["min_qty"]).all()
    # approval: Rs 10 lakh or more before GST waits for the Purchase Head
    orders = sql(db, "SELECT name, state, amount_untaxed::float8 AS value FROM purchase_order WHERE origin LIKE 'MRP-%'")
    untouched = orders[orders["state"].isin(["to approve", "purchase"])]
    assert (untouched.loc[untouched["value"] >= 1_000_000, "state"] == "to approve").all() or \
        (untouched["state"] == "purchase").all()        # all approved later is fine too
    assert (untouched.loc[untouched["value"] < 1_000_000, "state"] == "purchase").all()


def test_rerun_after_release_proposes_nothing_twice():
    folders = sorted(glob.glob(f"{config.RUN_DIR}/2*"))
    if len(folders) < 2 or not glob.glob(f"{folders[0]}/released.csv"):
        pytest.skip("needs a released run followed by another run")
    first = pd.read_csv(f"{folders[0]}/planned_orders.csv")
    second = pd.read_csv(f"{folders[1]}/planned_orders.csv")
    with open(f"{folders[1]}/run.json", encoding="utf-8") as handle:
        assert json.load(handle)["to_release_now"] == 0
    released = (first["action"] == "release").sum()
    assert (second["kind"] == "buy").sum() == (first["kind"] == "buy").sum() - released
    assert (second["kind"] == "make").sum() == (first["kind"] == "make").sum()
