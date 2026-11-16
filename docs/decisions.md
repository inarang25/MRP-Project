# Decisions

Format follows ProcessPro: one entry per decision, with the reason and the SAP parallel where there is one.
Status: all proposed by Claude on 2026-11-16 while building milestone 1; owner review pending.

## DEC-001: Separate database `p2p_mrp`, copied from `p2p`
ProcessPro's tests and answer key expect exactly 1,208 POs in `p2p`. The planner works on a copy
(`scripts/setup_mrp_db.py`), never deletes a database, and a test checks `p2p` is unchanged.
SAP parallel: a separate client copied from the golden client.

## DEC-002: Our own MRP calculation, reading Odoo, instead of Odoo's scheduler
Odoo Community has no MRP run in the SAP sense: it has reorder rules and make-to-order chains, and
no master production schedule (that is Enterprise). The planner therefore does the classic
calculation itself (`planner/engine.py`): BOM explosion by low-level code, netting against stock and
open orders, lot sizing, lead-time scheduling, exception messages. It reads everything from Odoo
with SQL and writes back only through Odoo's business methods. No reorder rules are created, so
Odoo's own scheduler never raises orders behind the planner's back.
SAP parallel: MD01 / MD04 / MD06.

## DEC-003: Go-live stock take
ProcessPro's year received every purchase and consumed nothing (production was not on the system),
leaving about a year of stock. Before the first run, Stores counts the stock and the administrator
approves the count (Odoo allows only a stock manager to approve). Counted quantity = a seeded number
of days of cover per material: 55% healthy (25-60 days), 30% low (5-18), 15% critical (0-4). The
freshest batches are kept. Finished goods get one opening batch each.
SAP parallel: physical inventory at cutover (MI01/MI07), initial stock entry 561.

## DEC-004: Two-level BOMs: bulk, then pack
Finished pack (FERT) <- bulk tablets or capsules (HALB) + packaging; bulk <- API + excipients.
8 finished goods, 6 bulks, 51 of the 120 purchased materials. Two packs can share one bulk
(Paracetamol India blister and US bottle), which is what makes netting at bulk level interesting.
Formulation quantities are estimates for teaching, not validated formulas.

## DEC-005: Demand and planning parameters live in the plan files, not in Odoo
Weekly demand per finished good (26 weeks, +/-15% around an average) is `data/plan/demand.csv`;
safety stock and order period are `data/plan/planning_params.csv`. Odoo Community has no place for
independent requirements, and the only place for safety stock is a reorder rule, which would
switch on Odoo's own scheduler (DEC-002). A small custom module could hold both later.
SAP parallel: planned independent requirements (MD61, PBIM/PBED); MRP 1/2 views (MARC).

## DEC-006: Lot sizing and lead times
- Made items: whole batches (the BOM quantity), one order per requirement date. Lead time = the
  BOM's manufacturing lead time. SAP: fixed lot size FX.
- Bought items: one order covers the shortages of the next 14 days (APIs) or 28 days (others),
  rounded up to the vendor's minimum order quantity and to whole packs. SAP: period lot size.
- Purchase lead time = vendor lead time + quality check (3 days GMP, 1 day put-away otherwise),
  because stock is usable only after release from Quarantine. SAP: planned delivery time + GR
  processing time.
- Safety stock: 7-10 days of average use for materials, 5 days for finished goods, none for bulk.

## DEC-007: Late orders and receipts on the way
If the order date falls in the past, the order is placed today, arrives late, and is flagged. A late
order only covers what is still missing on the day it can arrive, so a rerun after release sees the
new PO and proposes nothing twice (tested). Open POs more than 30 days overdue are not trusted as
future receipts and are listed for the buyer to close or chase (73 lines from the P2P year).

## DEC-008: Vendor choice
GMP materials: approved vendors only (never provisional or blocked); then the preferred vendor; then
the lowest price. This keeps MRP buying clean against ProcessPro's AVL rule.
SAP parallel: source list / quota arrangement with the Q-info record.

## DEC-009: Release into P2P follows the handoff contract
One requisition per proposal, raised by the planner (Pooja Bhatt). One PO per vendor and buyer,
created by the buyer for that material group, Source Document `MRP-<run id>`, requisitions linked to
PO lines, then confirmed. Rs 10 lakh or more before GST waits for the Purchase Head, like any PO.
Only proposals whose order date is within 7 days are released; the rest stay as a plan.
Not done: the optional `origin` field on the requisition (handoff 7.5) and the ProcessPro changes
(extractor, app filter), which belong to the ProcessPro repo.
