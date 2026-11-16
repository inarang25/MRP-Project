# MRP planner for a pharma plant, on Odoo

Material requirements planning for Aravelle Formulations (a fictional Indian tablet and capsule
maker), built on the same Odoo 17 sandbox as [ProcessPro](https://p2p-processpro.streamlit.app/),
the purchase-to-pay process-mining project. ProcessPro shows how buying actually ran; this project
decides what to make and buy, and sends its shortages into that same buying process.

## What it does
1. **Master data in Odoo**: 8 finished packs, 6 bulk products, two-level bills of materials on 51
   purchased materials, operations on 7 work centres.
2. **MRP run**: reads stock, BOMs, open purchase orders and vendor records from Odoo, explodes 26
   weeks of demand, nets, sizes lots, schedules by lead time, and lists exceptions.
3. **Release**: this week's purchase proposals become requisitions (raised by the planner) and
   purchase orders (created by the buyer, marked `MRP-<run id>`), so ProcessPro can tell planned
   buying from manual buying.

First run on 16 Nov 2026: 362 production orders, 263 purchase proposals worth Rs 13.6 crore, 29
orders already late; 17 purchase orders released, 5 above the approval limit.

## How it maps to SAP
| Here | SAP |
|---|---|
| Finished pack, bulk | Material types FERT, HALB |
| Bill of materials, operations, work centre | BOM (CS01), routing (CA01), work center (CR01) |
| `data/plan/demand.csv` | Planned independent requirements (MD61) |
| `planner/run.py` | MRP run (MD01), stock/requirements list (MD04) |
| `exceptions.csv` | Exception messages (MD06) |
| `requirements.csv` | Pegging (MD09) |
| Requisition, purchase order | Purchase requisition (EBAN), PO (EKKO/EKPO), conversion ME59N |
| Go-live stock take | Physical inventory at cutover |

## Run it
Needs the ProcessPro Odoo sandbox running (Docker) with its `p2p` database, and a `.env` copied from
`.env.example`.
```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m planner.master_data      # plan CSVs
.venv/Scripts/python scripts/setup_mrp_db.py     # copy p2p -> p2p_mrp, load, stock take
.venv/Scripts/python -m planner.run              # MRP run -> data/mrp/<run id>/
.venv/Scripts/python -m planner.release          # requisitions and POs in Odoo
.venv/Scripts/python -m pytest tests -q
```
Decisions and their reasons: [docs/decisions.md](docs/decisions.md). Background:
[docs/HANDOFF_MRP.md](docs/HANDOFF_MRP.md).

Synthetic data only. Formulation quantities are illustrative estimates, not real formulas.
