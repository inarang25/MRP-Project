"""All planning assumptions for Aravelle's manufacturing side, in one place.

The purchased materials (M001..M120), vendors and buyers already exist in Odoo: they were built
by the ProcessPro (P2P) project. This file adds what that project does not have: finished goods,
bulk (semi-finished) products, bills of materials, work centres, demand and planning parameters.

Quantities are realistic estimates for teaching and demonstration, not validated formulations.
Change a number here, rerun `python -m planner.master_data`, and the plan changes reproducibly.
"""

SEED = 44                       # ProcessPro uses 42 (master data) and 43 (PO plan)
PLAN_DIR = "data/plan"
RUN_DIR = "data/mrp"

SOURCE_DB = "p2p"               # ProcessPro's database: read-only for us, never changed (DEC-001)
TARGET_DB = "p2p_mrp"           # our copy, with Manufacturing installed

# Planning starts on this Monday. Demand is weekly, due every Monday from the week after.
PLAN_START = "2026-11-16"
DEMAND_WEEKS = 26

# ---------------------------------------------------------------------
# People (added to the 12 ProcessPro users)
# ---------------------------------------------------------------------
PLANNER = ("Pooja Bhatt", "Production Planner, Planning")        # SAP: MRP controller
BUYER_FOR_CATEGORY = {                                           # SAP: purchasing group
    "APIs": "Neha Kulkarni", "Excipients": "Neha Kulkarni",
    "Primary packaging": "Arjun Patel", "Printed packaging": "Arjun Patel",
}
DEFAULT_BUYER = "Sneha Reddy"
STORES_USER = "Suresh Yadav"

# ---------------------------------------------------------------------
# Work centres (SAP: work center, CR01) and the operations that run on them
# ---------------------------------------------------------------------
WORK_CENTRES = {
    "DISP": "Dispensing booth",
    "GRAN": "Granulation and blending suite",
    "COMP": "Tablet compression",
    "COAT": "Film coating",
    "ENCAP": "Capsule filling",
    "BLIS": "Blister packing line",
    "BOTL": "Bottle packing line",
}
# Operation list per dosage form: (name, work centre, minutes per batch)
OPERATIONS = {
    "tablet": [("Dispensing", "DISP", 90), ("Granulation and blending", "GRAN", 300),
               ("Compression", "COMP", 420)],
    "coated tablet": [("Dispensing", "DISP", 90), ("Granulation and blending", "GRAN", 300),
                      ("Compression", "COMP", 420), ("Film coating", "COAT", 240)],
    "capsule": [("Dispensing", "DISP", 90), ("Blending", "GRAN", 180), ("Capsule filling", "ENCAP", 360)],
    "blister": [("Blister packing and cartoning", "BLIS", 300)],
    "bottle": [("Bottle filling and cartoning", "BOTL", 300)],
}

# ---------------------------------------------------------------------
# Bulk products = tablets or capsules before packing (SAP: material type HALB)
# batch: nominal batch size in thousand tablets/capsules (unit of measure "Thousand"): what the
#        recipe is written for, fixed by the validated process
# yield: share of the nominal batch that comes out as good tablets (SAP: assembly scrap = 1 - yield).
#        Losses happen during granulation, compression and coating, not per ingredient.
# mg:    milligrams of each material per tablet/capsule, at 100% potency
# filler: the excipient reduced when extra API is added for potency (None = fill weight goes up instead)
# shells: capsule shells per capsule (with 2% loss)
# ---------------------------------------------------------------------
BULKS = {
    "B001": {"name": "Paracetamol 500 mg tablets (bulk)", "form": "tablet", "batch": 1000, "yield": 0.98, "filler": "M024",
             "lead_days": 5,
             "mg": {"M001": 500, "M024": 60, "M023": 20, "M022": 15, "M027": 6, "M020": 4}},
    "B002": {"name": "Metformin 500 mg tablets (bulk)", "form": "coated tablet", "batch": 800, "yield": 0.975, "filler": "M016",
             "lead_days": 6,
             "mg": {"M002": 500, "M016": 60, "M023": 25, "M021": 15, "M020": 5, "M028": 10, "M029": 15}},
    "B003": {"name": "Amoxicillin 500 mg capsules (bulk)", "form": "capsule", "batch": 500, "yield": 0.985, "filler": None,
             "lead_days": 4,
             "mg": {"M003": 574, "M020": 6, "M027": 5}, "shells": "M033"},
    "B004": {"name": "Atorvastatin 10 mg tablets (bulk)", "form": "coated tablet", "batch": 1000, "yield": 0.975, "filler": "M018",
             "lead_days": 6,
             "mg": {"M008": 10.85, "M018": 65, "M017": 40, "M031": 20, "M021": 6, "M020": 1, "M026": 0.5,
                    "M029": 4}},
    "B005": {"name": "Telmisartan 40 mg tablets (bulk)", "form": "tablet", "batch": 500, "yield": 0.98, "filler": "M030",
             "lead_days": 5,
             "mg": {"M007": 40, "M030": 160, "M023": 12, "M022": 10, "M020": 4}},
    "B006": {"name": "Omeprazole 20 mg capsules (bulk)", "form": "capsule", "batch": 500, "yield": 0.985, "filler": "M030",
             "lead_days": 5,
             "mg": {"M005": 20, "M030": 120, "M028": 15, "M027": 8, "M032": 1}, "shells": "M034"},
}
SHELL_LOSS = 1.02               # 2% of capsule shells are lost on the filling machine

# ---------------------------------------------------------------------
# Finished goods = saleable packs (SAP: material type FERT)
# batch: packing batch in packs. per_1000: NET materials per 1,000 packs (what ends up in the packs),
# besides the bulk itself. The BOM adds the expected loss on top (SAP: component scrap):
# ---------------------------------------------------------------------
LOSS_PCT_DEFAULT = 1.0          # cartons, leaflets, labels, bottles, caps: spoiled at set-up and by rejects
LOSS_PCT_FOIL = 4.0             # forming film and lidding foil: set-up and the trimmed edges of the web
FOIL = {"M036", "M037", "M038", "M039", "M064", "M065", "M066"}
NO_LOSS = {"M048", "M067", "M068"}                      # cotton coil, shipper cartons: counted, not spoiled
BULK_LOSS_PCT = 0.5             # tablets rejected on the packing line (broken, empty pockets)


def loss_pct(material):
    if material in FOIL:
        return LOSS_PCT_FOIL
    if material in NO_LOSS:
        return 0.0
    return LOSS_PCT_DEFAULT


def blister_pack(bulk, carton, weekly, forming="M036", lidding="M038", leaflet="M059", shipper="M067",
                 extra=None):
    # 10 blisters of 10: about 12.5 kg forming film and 3.4 kg lidding foil per 1,000 packs
    per_1000 = {forming: 12.5, lidding: 3.4, carton: 1000, leaflet: 1000, shipper: 10, "M069": 2000}
    per_1000.update(extra or {})
    return {"bulk": bulk, "units_per_pack": 100, "form": "blister", "batch": 5000, "lead_days": 3,
            "weekly_demand": weekly, "per_1000": per_1000}


FINISHED = {
    "F001": {"name": "Paracetamol 500 mg tablets, 10x10 blister (India)",
             **blister_pack("B001", "M051", 36000, lidding="M064")},
    "F002": {"name": "Paracetamol 500 mg tablets, bottle of 100 (US export)",
             "bulk": "B001", "units_per_pack": 100, "form": "bottle", "batch": 4000, "lead_days": 3,
             "weekly_demand": 8000,
             "per_1000": {"M042": 1000, "M043": 1000, "M045": 1000, "M046": 1000, "M048": 0.5, "M063": 1000,
                          "M057": 1000, "M060": 1000, "M070": 1000, "M068": 21}},
    "F003": {"name": "Metformin 500 mg tablets, 10x10 blister (India)",
             **blister_pack("B002", "M052", 24000, lidding="M065")},
    "F004": {"name": "Metformin 500 mg tablets, 10x10 blister (EU export)",
             **blister_pack("B002", "M058", 6000, forming="M037", leaflet="M061", shipper="M068",
                            extra={"M070": 1000})},
    "F005": {"name": "Amoxicillin 500 mg capsules, 10x10 blister (India)",
             **blister_pack("B003", "M053", 10000, forming="M037")},
    "F006": {"name": "Atorvastatin 10 mg tablets, 10x10 blister (India)",
             **blister_pack("B004", "M054", 15000, lidding="M066")},
    "F007": {"name": "Telmisartan 40 mg tablets, 10x10 alu-alu blister (India)",
             **blister_pack("B005", "M055", 8000, forming="M039")},
    "F008": {"name": "Omeprazole 20 mg capsules, 10x10 alu-alu blister (India)",
             **blister_pack("B006", "M056", 9000, forming="M039")},
}

# ---------------------------------------------------------------------
# API potency (SAP: active ingredient management)
# The recipe states API at 100%. Real lots test a little lower (assay on the certificate of analysis),
# so each batch needs a little more API, and the filler is reduced by the same weight.
# Planning potency = typical assay of recent lots from the approved vendors. Estimates; lot-level
# assay comes with the lesson on stock and quality status.
# ---------------------------------------------------------------------
PLANNING_POTENCY_PCT = {"M001": 99.3, "M002": 99.1, "M003": 98.6, "M005": 99.0, "M007": 99.4, "M008": 98.8}

# ---------------------------------------------------------------------
# Change control register for packaging materials (SAP: discontinuation in MRP 4 + follow-up material,
# under an engineering change number). New artwork = a new material, so old and new are never mixed.
# Planning is the same for every change: the old material is never reordered; its stock and the orders
# already placed are used up, then the new one takes over; never after last_use, when what is left is
# scrapped. What differs is how the company sets last_use:
# kind "cosmetic": the company picks a generous date, so old stock can run out naturally.
# kind "safety":   the regulator sets the date; old material left on that day is scrapped, whatever it cost.
# A packing batch never mixes old and new: if old stock can't cover a whole batch, the batch switches.
# ---------------------------------------------------------------------
MATERIAL_CHANGES = [
    {"change": "CC-2026-031", "old": "M051", "new": "M121",
     "new_name": "Printed carton Paracetamol 500 mg (new artwork)", "kind": "cosmetic", "last_use": "2027-03-01",
     "reason": "New carton design and updated company address. No safety impact: use up the old cartons."},
    {"change": "CC-2026-034", "old": "M061", "new": "M122",
     "new_name": "Package leaflet export EU (safety update)", "kind": "safety", "last_use": "2027-01-04",
     "reason": "New side-effect warning required by the EU authority. Old leaflets may not be used from "
               "4 Jan 2027."},
]

DEMAND_NOISE = 0.15             # weekly demand varies +/-15% around the average
FINISHED_SHELF_LIFE_MONTHS = 36
BULK_SHELF_LIFE_MONTHS = 6      # bulk hold time before packing

# ---------------------------------------------------------------------
# Planning parameters (SAP: MRP 1 / MRP 2 views of the material master)
# ---------------------------------------------------------------------
QC_DAYS_GMP = 3                 # quarantine to stock: sampling, testing, release
PUTAWAY_DAYS = 1                # non-GMP: quarantine to stock without testing
SAFETY_DAYS = {"APIs": 10, "Excipients": 7, "Primary packaging": 7, "Printed packaging": 10}
ORDER_PERIOD_DAYS = {"APIs": 14, "Excipients": 28, "Primary packaging": 28, "Printed packaging": 28}
FINISHED_SAFETY_DAYS = 5
RELEASE_WINDOW_DAYS = 7         # proposals that must be ordered within this many days are released
OVERDUE_IGNORE_DAYS = 30        # an open PO this late is no longer trusted as a future receipt

# Opening stock (the stock take at go-live, DEC-003): days of cover on hand per material.
# Most materials are healthy; some are short, which is what gives the first MRP run work to do.
OPENING_COVER_DAYS = {"healthy": (25, 60), "low": (5, 18), "critical": (0, 4)}
OPENING_COVER_MIX = {"healthy": 0.55, "low": 0.30, "critical": 0.15}
OPENING_FINISHED_COVER_DAYS = (4, 14)
