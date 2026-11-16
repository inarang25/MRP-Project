"""The MRP arithmetic, checked against examples worked out by hand."""
from datetime import date

from planner.engine import Bom, Item, lot_size, low_level_codes, run_mrp

TODAY = date(2026, 1, 5)


def day(n):
    return date(2026, 1, n)


def test_lot_size_rounds_up_to_minimum_and_multiple():
    assert lot_size(30, 100, 25) == 100
    assert lot_size(130, 100, 25) == 150
    assert lot_size(7.5, 0, 0) == 7.5


def test_low_level_code_takes_the_deepest_use():
    items = {c: Item(c, "buy") for c in ["FG", "BULK", "RM"]}
    boms = {"FG": Bom(1, {"BULK": 1, "RM": 1}), "BULK": Bom(1, {"RM": 2})}
    assert low_level_codes(items, boms) == {"FG": 0, "BULK": 1, "RM": 2}


def test_two_level_explosion_netting_and_scheduling():
    # 1 pack needs 2 tablets; 100 tablets need 5 kg of powder. Packs are made in batches of 50.
    items = {
        "PACK": Item("PACK", "make", lead_days=2, on_hand=20, min_lot=50, multiple=50),
        "TAB": Item("TAB", "make", lead_days=3, on_hand=40, min_lot=100, multiple=100),
        "POWDER": Item("POWDER", "buy", lead_days=5, on_hand=2, safety_stock=1, min_lot=10, multiple=5),
    }
    boms = {"PACK": Bom(50, {"TAB": 100}), "TAB": Bom(100, {"POWDER": 5})}
    result = run_mrp(items, boms, [("PACK", day(20), 100, "demand")], [], TODAY)
    orders = {order["item"]: order for order in result.orders}

    # Packs: 100 needed - 20 on hand = 80 short -> 2 batches = 100, start 2 days before the 20th
    assert orders["PACK"]["qty"] == 100 and orders["PACK"]["order_date"] == day(18)
    # Tablets: 200 needed on the 18th - 40 = 160 short -> 200, start on the 15th
    assert orders["TAB"]["qty"] == 200 and orders["TAB"]["order_date"] == day(15)
    # Powder: 10 kg needed on the 15th; 2 on hand, keep 1 -> 9 short -> minimum order 10, order on the 10th
    assert orders["POWDER"]["qty"] == 10 and orders["POWDER"]["order_date"] == day(10)
    assert not result.exceptions


def test_receipt_on_the_way_is_used_before_ordering():
    items = {"RM": Item("RM", "buy", lead_days=5, on_hand=10)}
    result = run_mrp(items, {}, [("RM", day(20), 30, "demand")], [("RM", day(12), 25, "P00001")], TODAY)
    assert not result.orders


def test_period_lot_sizing_groups_the_following_days():
    items = {"RM": Item("RM", "buy", lead_days=2, period_days=7)}
    demand = [("RM", day(12), 10, "a"), ("RM", day(15), 10, "b"), ("RM", day(19), 10, "c")]
    result = run_mrp(items, {}, demand, [], TODAY)
    assert [(order["need_date"], order["qty"]) for order in result.orders] == [(day(12), 20), (day(19), 10)]


def test_late_order_is_placed_today_and_flagged():
    items = {"RM": Item("RM", "buy", lead_days=10)}
    result = run_mrp(items, {}, [("RM", day(8), 10, "demand")], [], TODAY)
    order = result.orders[0]
    assert order["order_date"] == TODAY and order["due_date"] == day(15) and order["late_days"] == 7
    assert result.exceptions[0]["type"] == "late"


def test_rerun_after_ordering_does_not_order_again():
    # The late order above is now a PO arriving on the 15th: MRP must not propose it a second time
    items = {"RM": Item("RM", "buy", lead_days=10)}
    result = run_mrp(items, {}, [("RM", day(8), 10, "demand")], [("RM", day(15), 10, "P00002")], TODAY)
    assert not result.orders
    assert result.exceptions[0]["type"] == "short_until_receipt"
