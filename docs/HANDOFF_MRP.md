# HANDOFF: ProcessPro → MRP planner

Facts about the ProcessPro repo (`P2P Project`, GitHub `inarang25/P2P-ProcessPro`, private), as of 2026-11-16. "Not built" means it doesn't exist; "unknown" means not checked.

## 1. Odoo setup
- **Odoo 17.0 Community** (`odoo:17.0` image) + **PostgreSQL 16** (`postgres:16`), via `docker-compose.yml` in the ProcessPro repo. Docker Desktop doesn't auto-start; launch it first.
- Ports bound to 127.0.0.1 only: Odoo `ODOO_PORT` (default 8069), Postgres `DB_PORT` (default 5432, exposed for the extractor).
- Custom addons mounted from `./odoo/addons` → `/mnt/extra-addons`. Volumes: `pg-data`, `odoo-data`.
- Start: `docker compose up -d`. Stop: `docker compose down`. **Never `docker compose down -v`** (deletes all databases).
- Databases: `p2p` (the full year, what ProcessPro reads), `p2p_probe` (early experiments), `p2p_run1` (a first run with bad timestamps, kept). Others may exist; unknown. Rule (DEC-018): scripts never delete a database and stop if the target exists.
- Company: Aravelle Formulations Pvt. Ltd. (fictional), Ahmedabad, INR, FY ends 31 March, generic chart of accounts, `l10n_in` deliberately NOT installed, flat 18% GST.
- Installed: `purchase`, `stock`, `account`, `purchase_stock`, `product_expiry`, plus custom `p2p_requisition`, `p2p_quality`, `p2p_india` (and their dependencies). **`mrp` is NOT installed.** Odoo's own `purchase_requisition` (agreements) is not installed.
- Config (`scripts/configure_odoo.py`): one warehouse "Ahmedabad Plant", 2-step receipts, Input location renamed **Quarantine** (Quarantine→Stock = QC release or put-away); lots/expiry on; multi-UoM on; PO double validation at ₹11.8 lakh incl. GST (= ₹10 lakh before GST); payment terms; 12 users.
- Rebuild from scratch: `scripts/build_odoo.py` (setup → configure → load master data), then `python -m generator.execute --db p2p` (about 1 hour).

## 2. How ProcessPro connects
- **Writes** (setup and data generation only): XML-RPC (`/xmlrpc/2/common`, `/xmlrpc/2/object`), acting as each user. Code: `scripts/setup_odoo.py`, `scripts/configure_odoo.py`, `generator/load_master_data.py`, `generator/execute.py`.
- **Reads** (extraction): direct Postgres, read-only SQL with `psycopg` 3. Code: `extractor/extract.py`, `connect()`, which parses `.env` itself (no python-dotenv).
- Env var names: `DB_USER`, `DB_PASSWORD`, `DB_PORT`, `ODOO_PORT`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD`, `P2P_USER_PASSWORD`. `.env` is gitignored; `.env.example` is the template.
- The Streamlit app does NOT connect to Odoo. It reads a committed CSV snapshot (`data/eventlog`, `data/conformance`).

## 3. Data model and event log
- **Case = purchase order** (`purchase_order.name`, e.g. P00001). Not the PO line: one invoice or payment covers several lines.
- Tables read (23): `p2p_requisition`, `purchase_order`, `purchase_order_line`, `mail_message`, `mail_tracking_value`, `ir_model_fields`, `stock_picking`, `stock_picking_type`, `stock_location`, `stock_move`, `stock_move_line`, `stock_lot`, `account_move`, `account_move_line`, `account_payment`, `account_partial_reconcile`, `account_payment_term`, `product_product`, `product_template`, `product_category`, `product_supplierinfo`, `res_partner`, `res_users`.
- Activities, with their timestamp and person:
  - Requisition created: `p2p_requisition.date_requested`, create user. Earliest per PO, count kept.
  - PO created: `purchase_order.create_date`.
  - PO approved / PO changed: `mail_tracking_value` (state 'To Approve'→'Purchase Order'; `amount_untaxed` changes).
  - Goods receipt / QC release / Put-away / Return to vendor: `stock_picking.date_done`, `validated_by_id` (custom field).
  - QC rejection: Quality's note in `mail_message`.
  - Invoice booked: posted `account_move` (`in_invoice`), `create_date`, linked to the PO through lines.
  - Payment: `account_payment` → `account_partial_reconcile` → invoice → PO.
- Output: `data/eventlog/event_log.csv` + `.xes` (case_id, activity, timestamp in IST, resource, document_id, amount, …), object tables (`cases`, `po_lines`, `receipts`, `bills`, `bill_lines`, `requisitions`, `users`), and `manifest.json` (tables, per-activity SQL, 8-point reconciliation, all matching).
- Custom fields:
  - `p2p.requisition`: name (PR#####), requester_id, product_id, product_qty, estimated_price, date_needed, date_requested, state (requested / ordered / cancelled, computed from the PO link), purchase_line_id, purchase_order_id.
  - `product.template`: gmp, moisture_sensitive, pack_size (shelf life uses `product_expiry`).
  - `product.supplierinfo`: avl_status, qualified_on, quality_agreement, preferred.
  - `res.partner`: msme_category, msme_45_day_rule.
  - `stock.picking`: validated_by_id.

## 4. Master data and synthetic data
- `generator/config.py` (SEED 42) → `generator/master_data.py` → `data/plan/` (gitignored), loaded by `generator/load_master_data.py`.
- **120 products, all purchased materials**: APIs 15, Excipients 20, Primary packaging 15, Printed packaging 20, Lab chemicals & standards 15, MRO & engineering 20, Non-GMP indirect 15. **No finished goods, no BOMs, no work centres, no routings, no reorder rules, no sales or demand data.**
- 40 vendors (Domestic MSE / medium / large, imports from China, Germany, US), 328 supplier info records (2–4 vendors per product, price, lead time, MOQ from pack size and minimum order value).
- UoM: Kg, Litre, Units (standard) plus Bottle, Vial, Set, Ream, Box, Pair, Thousand.
- 12 users: 4 requesters, 3 buyers, Purchase Head, Stores, AP clerk, QC analyst, QA manager. No planner or production user.
- Year: `generator/plan.py` (seed 43) → 1,208 POs, FY 2025-04-01 to 2026-03-31, 14 deviation types at known rates, answer key `ground_truth.csv`. `generator/execute.py` replays it through Odoo as each user, then backdates each action with a Postgres function `p2p_backdate` (Odoo stores UTC; the plan is IST).

## 5. Stack and structure
Python (local venv 3.14; hosted 3.12) · pandas 3.1 · numpy 2.5 · pm4py 2.7.23.11 · psycopg 3.3 · streamlit 1.67 · pytest · playwright (screenshots only) · Graphviz in the browser (@viz-js/viz 3.2.4 + svg-pan-zoom 3.6.1).
```
app/          Streamlit app (streamlit_app.py, data.py, theme.py, content.py, process_map.py, graph_view.py, views/)
conformance/  rules.py (14 rules), impact.py (₹ per finding), discover.py (PM4Py)
extractor/    extract.py (SQL → event log + manifest)
generator/    config, master_data, load_master_data, plan, execute
odoo/addons/  p2p_requisition, p2p_quality, p2p_india
scripts/      setup/configure/build Odoo, checks, grade_rules, screenshots
tests/        62 tests: plan, extraction vs plan, rules vs answer key, app, model, maps, data model
docs/         decisions.md (DEC-001..032), brd.md, project_explained.md, case_study.html, launch/
```
- Pipeline: `python -m extractor.extract` → `conformance.rules` → `conformance.impact` → `conformance.discover` → `scripts/grade_rules.py` → `pytest`.
- App: `streamlit run app/streamlit_app.py`. Live at https://p2p-processpro.streamlit.app/ (Streamlit Community Cloud, private repo, `app/requirements.txt`; pushing to main redeploys).

## 6. Conventions to reuse
- Write to Odoo only through XML-RPC business methods, acting as the real user. Read with SQL. Never write Odoo tables with SQL, except the backdating function.
- Plan first (seeded, CSV), then execute, then grade against an answer key. Tests compare against ground truth.
- Record decisions as DEC-NNN in `docs/decisions.md`. Never delete databases.
- Money: all figures before GST (DEC-032). Times: event log in IST.
- Document the SAP equivalent of every Odoo object.
- UI: plain language, no codes on screen; navy #14336B, aqua #00A6A6, Inter Tight / Inter.

## 7. Integration contract for MRP (proposed, not built)
Goal: MRP shortages flow into the P2P process ProcessPro already mines, and can be told apart from manual buying.
1. **Use a separate database** (a copy of `p2p`, e.g. `p2p_mrp`). Adding POs to `p2p` breaks ProcessPro's tests and answer key, which expect exactly the planned 1,208 POs.
2. **Install `mrp`** in that database; create finished goods (tablets, capsules), BOMs on the existing 120 materials, and work centres. None exist today.
3. **Each shortage = one `p2p.requisition`**: product_id, product_qty (rounded up to `pack_size` / supplier MOQ), date_needed, estimated_price (from `product.supplierinfo`), date_requested = MRP run time. Requester = a new planner user (to create, e.g. `mrp.planner`). Without a requisition, ProcessPro's rule "every order starts from a request" flags the PO.
4. **PO created by a buyer** (XML-RPC as the buyer) with standard field `purchase.order.origin = "MRP-<run_id>"` (the generator never sets `origin`). Vendor: an `avl_status = approved` supplier for GMP materials, prefer `preferred = True`. Then link each requisition's `purchase_line_id` to its PO line, and call `button_confirm` (approval above ₹10 lakh before GST happens in Odoo).
5. **Optional:** add `origin` (Char) to `p2p.requisition` so the request itself carries the run ID. This is a module change plus a module upgrade.

ProcessPro changes needed (none made yet):
- extractor: read `purchase_order.origin` (and requisition origin, if added) into `cases`; list it in the manifest.
- app: a "Source: MRP / manual" sidebar filter; optionally an earlier "MRP run" activity per case.
- tests: run against a configurable database; the plan-based extraction tests only apply to `p2p`.
- timestamps: MRP records get today's date unless backdated (the `p2p` year is FY 2025-26).

## 8. Known gaps
- No manufacturing, demand, BOMs, reorder rules, finished goods or sales. Purchase-side only.
- One case notion (PO). An invoice covering several POs isn't supported (that needs an object-centric log). No services buying.
- Split-order detection: 90% precision / 94% recall. The other 13 rules are 100/100 on the synthetic data.
- The app runs on a static snapshot, not a live connection; re-extract and commit to refresh.
- Odoo's PO approval limit is checked including GST, so it's set at ₹11.8 lakh to mean ₹10 lakh.
- Imports are priced in INR (no foreign currency).
- Deviation rates (3–15% each) are deliberately higher than real life.
