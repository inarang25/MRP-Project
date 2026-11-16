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

## How to work with the owner
Same as ProcessPro: Claude owns all technical work end to end and proves it with tests; the owner
steers process understanding (planning, pharma), product direction and the showcase. Put product
decisions to the owner at the start or end of a milestone with a recommendation. Sessions are short;
end each with what was done, one commit pushed to GitHub (inarang25/MRP-Project), and the next step.

## Layout
```
planner/config.py       all assumptions: finished goods, BOMs, demand, planning parameters
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
Next: owner reviews DEC-001..009 and the product direction for the app.
