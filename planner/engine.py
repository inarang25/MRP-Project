"""The MRP calculation itself. Pure Python: no Odoo, no files, so it can be tested by hand.

It does what SAP's MRP run (MD01) does for each material, top of the BOM first:
  1. gross requirements  = demand, plus what parent orders need (BOM explosion)
  2. net requirements    = gross - stock on hand - receipts already on the way, keeping safety stock
  3. lot sizing          = round the shortage up to a batch, a minimum order quantity or a pack
  4. scheduling          = order date = need date - lead time
A shortage that should have been ordered in the past is ordered today and flagged as late.

Pharma additions (lesson 2):
  - batch yield: a made item's batch gives less good output than its nominal size
  - API potency: weaker API lots need more API and less filler (adjust_for_potency)
  - material changes: a discontinued material is never reordered. Its stock and the orders already
    placed are used up, batch by batch, then the follow-up material takes over; never after the last
    use date. A batch never mixes old and new. Leftover old stock is reported as a write-off.
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
    yield_factor: float = 1.0     # made items: good output per nominal batch quantity (0.98 = 98%)
    follow_up: str = None         # discontinued item: the material that replaces it
    last_use: date = None         # discontinued item: not used on or after this date


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


def adjust_for_potency(lines, api, potency_pct, filler=None):
    """BOM lines for API at a given potency: API / potency, and the filler reduced by the extra weight,
    so the tablet keeps its weight. Without a filler (some capsules) the fill weight goes up instead."""
    lines = dict(lines)
    extra = lines[api] * (100 / potency_pct - 1)
    lines[api] = lines[api] + extra
    if filler:
        if lines[filler] < extra:
            raise ValueError(f"not enough {filler} to compensate the potency of {api}")
        lines[filler] = lines[filler] - extra
    return lines


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
    # A follow-up material takes over the requirements of the one it replaces: plan it after that one
    for code, item in items.items():
        if item.follow_up:
            level[item.follow_up] = max(level.get(item.follow_up, 0), level[code])
    return level


def hand_over(item, requirements, arrivals):
    """Split a discontinued item's requirements between the old material and its follow-up.

    The old material covers requirements, oldest first, from its stock plus everything already on order,
    in whole batches only. From the first batch it cannot cover (or the last use date), every later
    requirement goes to the follow-up. Returns (kept, moved, switch date)."""
    kept, moved, switched = [], [], None
    supply = item.on_hand + sum(arrivals.values())
    for when, quantity, source, per_batch in sorted(requirements, key=lambda r: r[0]):
        if switched is None and item.last_use and when >= item.last_use:
            switched = when
        if switched is None:
            batches = round(quantity / per_batch)
            covered = min(batches, math.floor(supply / per_batch + 1e-9))
            if covered:
                kept.append((when, covered * per_batch, source, per_batch))
                supply -= covered * per_batch
            if covered < batches:
                switched = when
                moved.append((when, (batches - covered) * per_batch, source, per_batch))
        else:
            moved.append((when, quantity, source, per_batch))
    return kept, moved, switched


def run_mrp(items, boms, demand, receipts, today):
    """items: {code: Item}; boms: {code: Bom}; demand: [(code, date, qty, source)];
    receipts: [(code, date, qty, reference)]; today: date. Returns a Result."""
    result = Result()
    requirements = {code: [] for code in items}
    incoming = {code: [] for code in items}
    for code, when, quantity, source in demand:
        requirements[code].append((max(when, today), quantity, source, quantity))   # (date, qty, source, batch)
    for code, when, quantity, reference in receipts:
        if code in incoming:
            incoming[code].append((max(when, today), quantity, reference))

    levels = low_level_codes(items, boms)
    counter, waiting = 0, set()
    for code in sorted(items, key=lambda c: (levels[c], 0 if items[c].follow_up else 1, c)):
        item = items[code]
        needs, arrivals = {}, {}
        for when, quantity, _ in incoming[code]:
            arrivals[when] = arrivals.get(when, 0.0) + quantity
        if item.follow_up:
            kept, moved, switched = hand_over(item, requirements[code], arrivals)
            requirements[code] = kept
            requirements[item.follow_up].extend(moved)
            if switched:
                reason = "last use date" if item.last_use and switched >= item.last_use else "old stock used up"
                result.exceptions.append({
                    "type": "changeover", "item": code, "order_id": "", "date": switched,
                    "detail": f"switches to {item.follow_up} from {switched} ({reason})"})
        for when, quantity, source, _ in requirements[code]:
            needs[when] = needs.get(when, 0.0) + quantity
            result.requirements.append({"item": code, "date": when, "qty": round(quantity, 6), "source": source})

        projected = item.on_hand
        dates = sorted(set(needs) | set(arrivals))
        for index, when in enumerate(dates):
            projected += arrivals.get(when, 0.0) - needs.get(when, 0.0)
            planned = 0.0
            if item.follow_up:
                # Never reorder discontinued artwork. A gap before an old order arrives is a delay to chase.
                if projected < -1e-9 and code not in waiting:
                    waiting.add(code)
                    result.exceptions.append({
                        "type": "short_until_receipt", "item": code, "order_id": "", "date": when,
                        "detail": f"short by {-projected:,.0f} from {when} until an order already placed "
                                  f"arrives; chase it"})
            elif needs.get(when) and projected < item.safety_stock - 1e-9:
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
                    batches = planned / (boms[code].batch_qty * item.yield_factor) if code in boms else None
                    result.orders.append({
                        "order_id": order_id, "item": code, "kind": item.kind, "qty": planned,
                        "batches": round(batches, 6) if batches else None,
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
                            requirements[component].append((order_date, per_batch * batches, order_id, per_batch))
            result.projection.append({
                "item": code, "date": when, "requirements": round(needs.get(when, 0.0), 6),
                "receipts": round(arrivals.get(when, 0.0), 6), "planned": planned,
                "projected": round(projected, 6)})
        if item.follow_up and projected > 1e-9:
            switch = next((e["date"] for e in result.exceptions
                           if e["type"] == "changeover" and e["item"] == code), item.last_use)
            if item.last_use and (switch is None or switch > item.last_use):
                switch = item.last_use             # on the last use date whatever is left is scrapped
            late = sum(q for d, q in arrivals.items() if switch and d >= switch)
            detail = f"{projected:,.0f} left after the switch to {item.follow_up}: to be scrapped"
            if late > 1e-9:
                detail += f"; {late:,.0f} of it is still on order and arrives after the switch: reduce or cancel"
            result.exceptions.append({"type": "write_off", "item": code, "order_id": "",
                                      "date": switch or today, "detail": detail, "qty": round(projected, 6)})
    return result

