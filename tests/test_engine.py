"""The MRP arithmetic, checked against examples worked out by hand."""
from datetime import date

from planner.engine import Bom, Item, adjust_for_potency, lot_size, low_level_codes, run_mrp

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


# ---------------- lesson 2: yield, potency, material change-overs ----------------

def test_yield_means_more_batches_and_more_ingredients():
    # Nominal batch 1,000 tablets at 98% yield gives 980 good tablets. 2,000 needed -> 3 batches
    # (2 batches give only 1,960). Each batch consumes the full 500 g of powder.
    items = {"TAB": Item("TAB", "make", lead_days=1, min_lot=980, multiple=980, yield_factor=0.98),
             "POWDER": Item("POWDER", "buy", lead_days=1)}
    boms = {"TAB": Bom(1000, {"POWDER": 500})}
    result = run_mrp(items, boms, [("TAB", day(20), 2000, "demand")], [], TODAY)
    tab = next(o for o in result.orders if o["item"] == "TAB")
    powder = next(o for o in result.orders if o["item"] == "POWDER")
    assert tab["qty"] == 2940 and tab["batches"] == 3
    assert powder["qty"] == 1500


def test_potency_adds_api_and_takes_the_same_weight_off_the_filler():
    # 500 kg paracetamol at 98% potency -> 510.20 kg; maize starch 60 -> 49.80 kg; batch weight unchanged
    lines = adjust_for_potency({"API": 500, "STARCH": 60, "TALC": 6}, "API", 98.0, "STARCH")
    assert round(lines["API"], 2) == 510.20 and round(lines["STARCH"], 2) == 49.80
    assert round(sum(lines.values()), 6) == 566
    # No filler: the fill weight goes up instead
    capsule = adjust_for_potency({"API": 287, "TALC": 2.5}, "API", 98.6)
    assert round(capsule["API"], 2) == 291.08 and capsule["TALC"] == 2.5


def test_use_up_switches_when_old_stock_cannot_cover_a_whole_batch():
    # Old cartons: 120 in stock, 50 more already ordered. Batches need 60 each on the 8th, 12th, 16th.
    # 8th: 170 covers 60. 12th: 110 covers 60. 16th: 50 left, short of 60 -> switch;
    # the batch is not split, so all 60 come from the new cartons and the 50 old ones are scrapped.
    items = {"OLD": Item("OLD", "buy", lead_days=2, on_hand=120, follow_up="NEW", last_use=day(31)),
             "NEW": Item("NEW", "buy", lead_days=2)}
    demand = [("OLD", day(8), 60, "b1"), ("OLD", day(12), 60, "b2"), ("OLD", day(16), 60, "b3")]
    result = run_mrp(items, {}, demand, [("OLD", day(9), 50, "PO")], TODAY)
    assert [o["item"] for o in result.orders] == ["NEW"]                 # never reorder the old artwork
    assert result.orders[0]["qty"] == 60 and result.orders[0]["need_date"] == day(16)
    by_type = {e["type"]: e for e in result.exceptions}
    assert by_type["changeover"]["date"] == day(16)
    assert by_type["write_off"]["qty"] == 50


def test_order_of_several_batches_uses_old_stock_for_whole_batches_only():
    # A packing order of 3 batches needs 20 old cartons per batch; 50 are left -> 2 batches use the
    # old cartons, the 3rd uses new ones, and 10 old cartons are scrapped
    items = {"PACK": Item("PACK", "make", lead_days=1, min_lot=10, multiple=10),
             "OLD": Item("OLD", "buy", on_hand=50, follow_up="NEW"),
             "NEW": Item("NEW", "buy", lead_days=1)}
    boms = {"PACK": Bom(10, {"OLD": 20})}
    result = run_mrp(items, boms, [("PACK", day(20), 30, "demand")], [], TODAY)
    assert [(o["item"], o["qty"]) for o in result.orders if o["item"] != "PACK"] == [("NEW", 20)]
    assert next(e for e in result.exceptions if e["type"] == "write_off")["qty"] == 10


def test_discontinued_material_is_never_reordered_and_late_orders_are_flagged():
    # 50 in stock, 100 needed on the 10th, an old order of 100 arrives on the 12th: no new order,
    # a 'chase it' message, the requirement stays on the old material, 50 left over at the end
    items = {"OLD": Item("OLD", "buy", lead_days=1, on_hand=50, follow_up="NEW"),
             "NEW": Item("NEW", "buy", lead_days=1)}
    result = run_mrp(items, {}, [("OLD", day(10), 100, "b1")], [("OLD", day(12), 100, "PO")], TODAY)
    assert not result.orders
    assert {e["type"] for e in result.exceptions} == {"short_until_receipt", "write_off"}


def test_hard_cut_scraps_whatever_is_left_on_the_date():
    # Safety change from the 15th: the batch on the 20th must use the new leaflet even though
    # 100 old ones are in stock; 100 - 30 (used on the 10th) = 70 are scrapped.
    items = {"OLD": Item("OLD", "buy", on_hand=100, follow_up="NEW", last_use=day(15)),
             "NEW": Item("NEW", "buy", lead_days=3)}
    demand = [("OLD", day(10), 30, "b1"), ("OLD", day(20), 30, "b2")]
    result = run_mrp(items, {}, demand, [], TODAY)
    assert [(o["item"], o["qty"]) for o in result.orders] == [("NEW", 30)]
    write_off = next(e for e in result.exceptions if e["type"] == "write_off")
    assert write_off["qty"] == 70 and write_off["date"] == day(15)


def test_follow_up_is_planned_after_the_parent_explodes():
    # The new carton is in no BOM yet; it must still receive the pack's requirements
    items = {"PACK": Item("PACK", "make", lead_days=1, min_lot=10, multiple=10),
             "OLD": Item("OLD", "buy", follow_up="NEW"),
             "NEW": Item("NEW", "buy", lead_days=1)}
    boms = {"PACK": Bom(10, {"OLD": 10})}
    result = run_mrp(items, boms, [("PACK", day(20), 10, "demand")], [], TODAY)
    assert ("NEW", 10) in [(o["item"], o["qty"]) for o in result.orders]
