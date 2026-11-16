"""The MRP calculation itself. Pure Python: no Odoo, no files, so it can be tested by hand.

It does what SAP's MRP run (MD01) does for each material, top of the BOM first:
  1. gross requirements  = demand, plus what parent orders need (BOM explosion)
  2. net requirements    = gross - stock on hand - receipts already on the way, keeping safety stock
  3. lot sizing          = round the shortage up to a batch, a minimum order quantity or a pack
  4. scheduling          = order date = need date - lead time
A shortage that should have been ordered in the past is ordered today and flagged as late.
"""
import math
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class Item:
    code: str
    kind: str                     # "make" (planned production order) or "buy" (purchase proposal)
    lead_days: int = 0
    on_hand: float = 0.0
    safety_stock: float = 0.0
    min_lot: float = 0.0          # smallest order: batch size or supplier minimum order quantity
    multiple: float = 0.0         # order in multiples of this: batch size or pack size (0 = any)
    period_days: int = 1          # one order covers the shortages of this many days


@dataclass
class Bom:
    batch_qty: float
    lines: dict                   # component code -> quantity per batch


@dataclass
class Result:
    orders: list = field(default_factory=list)          # planned orders and purchase proposals
    requirements: list = field(default_factory=list)    # every requirement with where it came from (pegging)
    projection: list = field(default_factory=list)      # stock projection per item and date
    exceptions: list = field(default_factory=list)


def lot_size(quantity, min_lot, multiple):
    quantity = max(quantity, min_lot)
    if multiple:
        quantity = math.ceil(quantity / multiple - 1e-9) * multiple
    return round(quantity, 6)


def low_level_codes(items, boms):
    """How deep each item sits in any BOM (0 = finished goods). Deepest level wins, so an item is
    planned only after every product that uses it (SAP: low-level code)."""
    level = {code: 0 for code in items}

    def descend(code, depth, path):
        if code in path:
            raise ValueError(f"BOM loop through {code}")
        level[code] = max(level.get(code, 0), depth)
        for component in boms[code].lines if code in boms else []:
            descend(component, depth + 1, path | {code})

    for code in boms:
        descend(code, level.get(code, 0), frozenset())
    return level


def run_mrp(items, boms, demand, receipts, today):
    """items: {code: Item}; boms: {code: Bom}; demand: [(code, date, qty, source)];
    receipts: [(code, date, qty, reference)]; today: date. Returns a Result."""
    result = Result()
    requirements = {code: [] for code in items}
    incoming = {code: [] for code in items}
    for code, when, quantity, source in demand:
        requirements[code].append((max(when, today), quantity, source))
    for code, when, quantity, reference in receipts:
        if code in incoming:
            incoming[code].append((max(when, today), quantity, reference))

    levels = low_level_codes(items, boms)
    counter, waiting = 0, set()
    for code in sorted(items, key=lambda c: (levels[c], c)):
        item = items[code]
        needs, arrivals = {}, {}
        for when, quantity, source in requirements[code]:
            needs[when] = needs.get(when, 0.0) + quantity
            result.requirements.append({"item": code, "date": when, "qty": round(quantity, 6), "source": source})
        for when, quantity, _ in incoming[code]:
            arrivals[when] = arrivals.get(when, 0.0) + quantity

        projected = item.on_hand
        dates = sorted(set(needs) | set(arrivals))
        for index, when in enumerate(dates):
            projected += arrivals.get(when, 0.0) - needs.get(when, 0.0)
            planned = 0.0
            if needs.get(when) and projected < item.safety_stock - 1e-9:
                # The earliest a new order can arrive. If that is after the need date the order is late,
                # and it only has to cover what is still missing once it can arrive: receipts already
                # on the way before then are counted first (otherwise a rerun would order twice).
                earliest = max(when, today + timedelta(days=item.lead_days))
                horizon = earliest + timedelta(days=item.period_days)
                balance, worst = projected, item.safety_stock - projected
                for later in dates[index + 1:]:
                    if later >= horizon:
                        break
                    balance += arrivals.get(later, 0.0) - needs.get(later, 0.0)
                    if later <= earliest:
                        worst = item.safety_stock - balance          # position on the earliest arrival day
                    else:
                        worst = max(worst, item.safety_stock - balance)   # period lot-sizing

                if worst <= 1e-9:
                    if code not in waiting:
                        waiting.add(code)
                        result.exceptions.append({
                            "type": "short_until_receipt", "item": code, "order_id": "", "date": when,
                            "detail": f"short by {item.safety_stock - projected:,.0f} from {when} until a receipt "
                                      f"already on the way arrives; chase it"})
                else:
                    planned = lot_size(worst, item.min_lot, item.multiple)
                    order_date, due_date = earliest - timedelta(days=item.lead_days), earliest
                    late_days = (earliest - when).days
                    counter += 1
                    order_id = f"PL{counter:05d}"
                    result.orders.append({
                        "order_id": order_id, "item": code, "kind": item.kind, "qty": planned,
                        "need_date": when, "order_date": order_date, "due_date": due_date,
                        "late_days": late_days})
                    if late_days:
                        result.exceptions.append({
                            "type": "late", "item": code, "order_id": order_id, "date": when,
                            "detail": f"needed {when}, earliest possible {due_date} ({late_days} days late)"})
                    projected += planned

                    # BOM explosion: this order needs its components on the day it starts
                    if code in boms:
                        bom = boms[code]
                        for component, per_batch in bom.lines.items():
                            requirements[component].append(
                                (order_date, per_batch * planned / bom.batch_qty, order_id))
            result.projection.append({
                "item": code, "date": when, "requirements": round(needs.get(when, 0.0), 6),
                "receipts": round(arrivals.get(when, 0.0), 6), "planned": planned,
                "projected": round(projected, 6)})
    return result

