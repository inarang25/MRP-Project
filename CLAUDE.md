# Project: MRP planner for Aravelle Formulations

Second project on the ProcessPro pattern (sibling folder `P2P Project`, repo inarang25/P2P-ProcessPro).
ProcessPro mines the purchase-to-pay process; this project adds the planning side that feeds it:
demand -> MRP run -> production orders and purchase proposals -> requisitions and POs in Odoo.
Read `docs/HANDOFF_MRP.md` first: it describes the Odoo setup, conventions and the integration contract.

## Rules (inherited from ProcessPro)
- Synthetic data only; the company is fictional. Open-source tools only. No secrets in the repo.
- Write to Odoo only through XML-RPC business methods as the real user; read with SQL.
- Never delete a database. `p2p` is read-only for this project; work in `p2p_mrp` (DEC-001).
- Plan first (seeded CSV), then load or execute, then check against the plan with tests.
- Record decisions as DEC-NNN in `docs/decisions.md`. Give the SAP equivalent of every object.
- Money before GST. UI and write-ups in plain language, no codes on screen.

## How to work with the owner (set by the owner 2026-11-16)
- **Learn, then build.** The owner starts with no MRP knowledge, has time, and wants each concept
  understood and digested before moving on. No rushing ahead with code.
- Each lesson: plain-language concept -> worked example on Aravelle's own products -> SAP mapping ->
  the pharma-industry problem it raises -> 2-3 check questions. Wait for the answers. Only then
  build or revise the code for that concept, and show the result in planning terms.
- The tool must handle real pharma planning problems, not just textbook MRP.
- Code stays Claude's job and is explained at a high level only; Claude proves it with tests.
- Milestone 1 (below) was built before the lessons started: treat it as a draft that each lesson
  revisits and may change.
- Every session ends with one commit pushed to GitHub (inarang25/MRP-Project) and the next step.

## Lesson plan
1. Why MRP exists: what, how much, when; independent vs dependent demand
2. Bill of materials: bulk vs pack, levels, batch size, yield and losses
3. Demand: forecast, orders, master production schedule; pharma seasonality, tenders, export packs
4. Netting: stock, receipts, safety stock; quarantine, QC release, expiry, retest dates
5. Lot sizing: batches, MOQ, packs; validated batch sizes; the expiry write-off trade-off
6. Lead times and backward scheduling: import lead, QC time, artwork for printed packaging
7. Exceptions: what a planner does on Monday morning (expedite, postpone, cancel)
8. Capacity: can the plant do it; campaigns, changeovers, beta-lactam segregation
9. MRP into buying: requisitions, POs, approvals, and how ProcessPro sees them
10. Presenting it: the story, the app, the interview answers

## Layout
```
planner/config.py       all assumptions: products, BOMs, yields, potency, artwork changes, demand, tenders
planner/master_data.py  config -> data/plan/*.csv
planner/engine.py       the MRP calculation (pure Python, no Odoo)
planner/run.py          read Odoo with SQL -> engine -> data/mrp/<run id>/
planner/release.py      this week's proposals -> requisitions and POs in Odoo
planner/odoo.py         XML-RPC client (acts as a named user) and SQL connection
scripts/setup_mrp_db.py copy p2p -> p2p_mrp, install Manufacturing, load master data, stock take
tests/                  engine vs hand-worked examples; Odoo vs plan and vs the contract
```

## Commands (Docker Desktop running; Odoo is started from the P2P Project folder)
```
.venv/Scripts/python -m planner.master_data
.venv/Scripts/python scripts/setup_mrp_db.py
.venv/Scripts/python -m planner.run
.venv/Scripts/python -m planner.release
.venv/Scripts/python -m pytest tests -q
```

## Status
Milestone 1 built 2026-11-16 (DEC-001 to DEC-009, owner review pending): `p2p_mrp` created with
Manufacturing, planner user, 7 work centres, 8 finished goods + 6 bulks with BOMs and operations,
go-live stock take. Run 20261116-01: 65 items, 362 production orders, 263 purchase proposals
(Rs 13.6 crore over 26 weeks); 31 released as 31 requisitions and 17 POs (Rs 1.44 crore), 5 waiting
for the Purchase Head. Run 20261116-02 after release: nothing to release, nothing proposed twice.
12 tests pass. Not built yet: production orders in Odoo (planned orders stay in CSV), capacity
check on work centres, the app, the ProcessPro changes in handoff section 7.
Lesson 1 (why MRP exists) done 2026-11-17: answers good; taught why QC cannot be rushed, only prioritised.
Lesson 2 (bill of materials) done 2026-11-19: Q1 right (5 batches); Q2 (potency) taught; Q3 good
instinct, taught use-up vs safety cut-off. Built after it (DEC-010..012): losses shown as net + loss %,
batch yield, API potency with filler compensation, artwork changes (new material, use up stock and
open orders batch by batch, last use date, write-off report). Run 20261119-02: 362 production orders,
241 purchase proposals (Rs 12.5 crore), artwork write-off Rs 9,602. 22 tests pass. Not released.
Known simplification: one India leaflet (M059) is shared by six products; real leaflets are per product.
Lesson 3 (demand) done 2026-11-21: Q1 right (14k, 16k); Q2 right (seasonal follows the disease),
taught building stock before the season; Q3 good business instinct, taught pre-buy common materials
only. Built after it (DEC-013, DEC-014): 52-week seasonal forecast (acute vs chronic), distributor
orders consume the forecast, export contracts firm for 12 weeks, tender register with a Government
supply pack (F009, carton M123) and a what-if report. Run 20261121-03: 646 production orders, 523
purchase proposals (Rs 28.0 crore over 52 weeks), 22 to release (not released); open tender needs
Rs 63 lakh, Rs 12.4 lakh of common materials before the result. 27 tests pass.
Known pharma gap to fix in lesson 8: Amoxicillin (a penicillin) shares work centres with other
products, which GMP does not allow. Next: lesson 4 (netting: quarantine, QC release, expiry, retest dates).
