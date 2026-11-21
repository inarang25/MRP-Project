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
Demand (since lesson 3, DEC-013: forecast, customer orders, tenders in `data/plan/`);
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

## DEC-010: BOM losses are shown openly (lesson 2, 2026-11-19)
Every BOM line is stored as net quantity (what ends up in the product) + expected loss % = gross
quantity (what MRP plans and Odoo holds). Losses: cartons, leaflets, labels, bottles and caps 1%;
forming film and lidding foil 4% (set-up and trimmed edges); bulk tablets 0.5% on the packing line;
capsule shells 2%; shipper cartons and cotton none. Ingredients of bulk have no loss per line: they are
weighed exactly, and the batch loses output instead (DEC-011). Odoo Community BOM lines have no
loss field, so Odoo holds the gross quantity and `data/plan/bom_lines.csv` keeps the split. This
corrected the lidding foil from 17.5 to 17.68 kg per 5,000 packs on seven BOMs.
SAP parallel: component scrap on the BOM item.

## DEC-011: Batch yield and API potency (lesson 2, 2026-11-19)
- **Yield.** A bulk batch gives 97.5-98.5% of its nominal size as good tablets (plain tablets 98%,
  coated 97.5%, capsules 98.5%). MRP plans whole nominal batches and counts only the good output,
  so 2,000 good tablets from 1,000-tablet batches at 98% need 3 batches, not 2.
  SAP parallel: assembly scrap on the material master.
- **Potency.** The recipe states API at 100%. Each API is planned at a planning potency (98.6-99.4%,
  estimates), which adds API and takes the same weight off that product's filler, so the tablet
  weight stays the same. Capsules without a filler get a higher fill weight instead. The actual
  assay of each lot will be recorded on the lot in lesson 4, with retest dates.
  SAP parallel: active ingredient management, batch characteristics.

## DEC-012: Artwork changes and the go-live record (lesson 2, 2026-11-19)
- New artwork = a new material, so old and new can never be mixed. The change control register is
  `planner/config.py` MATERIAL_CHANGES -> `data/plan/material_changes.csv` (change number, old and
  new material, kind, last use date, reason); the new material is created in Odoo as a copy of the old
  one with the same approved printers.
- Planning rule, the same for every change: the old material is never reordered; its stock and the
  orders already placed are used up oldest first, in whole packing batches (a batch never mixes old
  and new); from the first batch it cannot cover, or from the last use date, the new material takes
  over. Whatever is left is reported as a write-off with its value. A gap before an old order
  arrives is reported as a delay to chase, not covered by a new order.
- Two examples: Paracetamol India carton (cosmetic, last use 1 Mar 2027) and EU leaflet (safety
  update, last use 4 Jan 2027). A first version of the rule switched as soon as stock alone ran short
  and ignored orders already placed; it would have scrapped Rs 6.3 lakh of cartons. The current rule
  scraps under Rs 10,000.
- `data/plan/opening_stock.csv` is the record of the go-live count (DEC-003) and is never regenerated.
SAP parallel: discontinuation indicator and follow-up material (MRP 4 view) under an engineering
change number.

## DEC-013: Demand from four sources; orders consume the forecast (lesson 3, 2026-11-21)
Replaces the first draft (flat weekly average +/-15%, `demand.csv`).
- **Forecast** (`forecast.csv`), 52 weeks, so next year's monsoon is in view when buying APIs. Acute
  medicines (Paracetamol India, Amoxicillin) follow a monthly season index: high July to September,
  a smaller winter peak, low in spring, averaging the yearly figure. Chronic medicines (Metformin,
  Atorvastatin, Telmisartan, Omeprazole) are steady. Indices are teaching estimates. The forecast is
  meant to be based on secondary sales, so quarter-end loading of distributors is left out.
- **Customer orders** (`customer_orders.csv`): distributor orders for the next 3 weeks (about 95%,
  60% and 25% of that week's real demand already booked; real demand is within +/-15% of the forecast).
- **Forecast consumption**: each week plans the larger of forecast and orders. Orders stay pegged to
  their order number; the forecast not yet used up is planned as "Forecast". `engine.consume_forecast`.
- **Export contracts**: the US bottle and EU blister ship every 4 weeks in fixed lots, as firm orders
  for 12 weeks and at the contract rate after that. No season.
- Not yet: smoothing into a master production schedule and a frozen period (capacity, lesson 8;
  exceptions, lesson 7); customers' minimum remaining shelf life (lesson 4).
SAP parallel: planned independent requirements (MD61), sales orders (VA01), strategy 40 (planning
with final assembly: orders consume the forecast).

## DEC-014: Tenders are planned only when won; a what-if report until then (lesson 3, 2026-11-21)
Tender register: `planner/config.py` TENDERS -> `data/plan/tenders.csv` (status bid / won / lost,
result date, lots, penalty). Tender packs are their own finished good (F009) with their own carton
(M123, "Government supply, not for sale"), so tender stock cannot be sold in the market.
- **Won**: every lot is a firm order in the plan.
- **Bid**: not in the plan, because putting a tender we may lose into MRP buys for it automatically.
  Instead, every run plans a second time as if it were won and writes `tender_impact.csv`: for each
  material, the extra quantity and value, the first day an extra order must be placed, and whether
  that is before the result.
- **The rule** (from the owner's answer: "start preparing, it's a business"): order before the result
  only what regular products also use (if the tender is lost, normal demand uses it up within weeks,
  so the cost is stock held for a while). Tender-only materials are not bought before the award;
  their artwork is approved and the printer booked in advance, so the order can go out on award day.
  Example TN-MSC-2026-118 (2 lakh packs): Rs 63 lakh of extra materials, Rs 12.4 lakh needed before
  the result, all of it common material; the Government carton can wait until 5 days after the result.
SAP parallel: a quotation, or a simulative MRP run on a planning version (MS01/MS02).
