"""Verified industry constants, paths, and activity IDs.

Every game-mechanic number here was read from game data (dogma attributes)
or a cited source during the original design pass — see PROJECT.md §5 for
the verification table and the worked Hulk example. Do not "correct" these
from memory; re-verify against dogma attributes in the imported SDE.
"""

import os
import sys
from pathlib import Path

from magoo import __version__

# --- Paths -----------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Presence of this file beside the exe selects data-next-to-the-exe; the
# portable zip ships it, the installer does not.
PORTABLE_MARKER = "magoo-portable.txt"


def _resolve_data_dir() -> Path:
    """Where USER data lives: the database, the SDE zip cache, the session
    secret and the logs.

    Resolved once, here, at import — never patched in by an entry point.
    The three names below derive from it immediately, so a later
    `config.DATA_DIR = x` leaves SDE_CACHE_DIR and DB_PATH pointing at the
    old location; tests/test_sde_button.py sets all three by hand for
    exactly that reason. Doing the work here also keeps the GUI, the
    `python -m magoo.sdeimport` CLI and pytest in agreement.

    Precedence: explicit override, then portable build, then installed
    build, then source checkout (unchanged — the test suite reads the
    developer's real database out of PROJECT_ROOT/data).
    """
    override = os.environ.get("MAGOO_DATA_DIR")
    if override:
        return Path(override).expanduser()

    if getattr(sys, "frozen", False):
        # Packaged. __file__ now points inside the PyInstaller bundle:
        # read-only under Program Files, and a temp dir that is wiped on
        # exit for a onefile build. User data must never derive from it.
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / PORTABLE_MARKER).exists():
            return exe_dir / "data"
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Magoo"
        return Path.home() / ".magoo"

    return PROJECT_ROOT / "data"


DATA_DIR = _resolve_data_dir()
SDE_CACHE_DIR = DATA_DIR / "sde"
DB_PATH = DATA_DIR / "magoo.sqlite"
LOG_DIR = DATA_DIR / "logs"

# --- Project identity ------------------------------------------------------

# GitHub repository, as "owner/name". Drives the update check
# (magoo.update reads <url>/releases.atom), the release links, and the
# contact URL in USER_AGENT.
# contact URL in USER_AGENT. Setting it to None disables the update check
# entirely — nothing is fetched and no banner ever appears.
GITHUB_REPO = "Tetrakill/MAGOO"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}" if GITHUB_REPO else None

# CCP asks third-party apps to identify themselves with an app name, a
# version and a way to reach the maintainer, and uses it to make contact
# about misuse. The repository is that contact point — its issue tracker is
# public and outlives any one address.
USER_AGENT = (
    f"Magoo/{__version__} (EVE industry planner; +{GITHUB_URL})"
    if GITHUB_URL
    else f"Magoo/{__version__} (EVE industry planner)"
)

# --- Local server ----------------------------------------------------------

# ONE fixed port, pinned everywhere: server bind, esi.CALLBACK_PORT, the
# launcher probe and the app registration at developers.eveonline.com. EVE
# SSO exact-matches redirect_uri against that registration, so a floating
# port silently breaks login while the rest of the app keeps working — the
# "SSO state mismatch" dead-end recorded in esi.authorize_url's docstring.
# For the same reason the host is spelled "localhost" everywhere and never
# 127.0.0.1: the two are different strings to CCP.
DEFAULT_PORT = 8765
CALLBACK_PATH = "/sso/callback"
CALLBACK_URL = f"http://localhost:{DEFAULT_PORT}{CALLBACK_PATH}"
APP_URL = f"http://localhost:{DEFAULT_PORT}/"

# --- EVE SSO ---------------------------------------------------------------

# Magoo ships ONE registered EVE application and authenticates as a PUBLIC
# client using PKCE. CCP documents exactly this for native apps: "The Client
# ID is public and can be shared", and PKCE exists "to allow your application
# to ship without its secret key". So this constant is not a secret, and
# there is deliberately no client secret anywhere in the codebase — one
# embedded in a distributed binary would not be a secret anyway.
#
# The callback registered against this application is CALLBACK_URL above,
# matched byte for byte by CCP.
#
# MAGOO_ESI_CLIENT_ID overrides it (esi.client_id) for development against a
# separate registration.
ESI_CLIENT_ID = "841121d474724820b34947491de0942f"

# --- SDE endpoints (CCP official JSONL static data export) -----------------

SDE_LATEST_URL = (
    "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
)
SDE_ZIP_URL_TEMPLATE = (
    "https://developers.eveonline.com/static-data/tranquility/"
    "eve-online-static-data-{build}-jsonl.zip"
)

# Datasets consumed from the export (PROJECT.md §4, plus the industry
# modifier datasets discovered in the live archive 2026-08-15: CCP now ships
# rig/structure bonus applicability as data — industryModifierSources maps
# each source type to the dogma attributes carrying its bonuses per activity,
# and industryTargetFilters names the category/group sets they apply to).
SDE_DATASETS = (
    "blueprints",
    "types",
    "groups",
    "categories",
    "dogmaAttributes",
    "typeDogma",
    "mapSolarSystems",
    "industryModifierSources",
    "industryTargetFilters",
    "typeMaterials",
)

# --- Activity IDs ----------------------------------------------------------

ACTIVITY_MANUFACTURING = 1
ACTIVITY_COPYING = 5
ACTIVITY_INVENTION = 8
ACTIVITY_REACTION = 11

# SDE activity names -> activity IDs imported into ref_blueprint. Only these
# two produce ITEM rows there; invention (v1.22) is imported separately into
# ref_invention because its products are BLUEPRINT types and one source can
# carry several — merging it here would poison blueprint_for_product.
SDE_ACTIVITY_IDS = {
    "manufacturing": ACTIVITY_MANUFACTURING,
    "reaction": ACTIVITY_REACTION,
}

# Activities whose structure/rig bonuses land in ref_industry_modifier: the
# two above plus the lab activities (v1.22) — invention and copy job fees
# read the engineering complex's cost bonus (strEngCostBonus et al.).
SDE_MODIFIER_ACTIVITY_IDS = {
    **SDE_ACTIVITY_IDS,
    "copying": ACTIVITY_COPYING,
    "invention": ACTIVITY_INVENTION,
}

# --- Category IDs ----------------------------------------------------------

# Skills are prerequisites, not materials. Anything in this category is
# excluded from BOM demand or expansion will stockpile "Industry".
CATEGORY_SKILL = 16
CATEGORY_SHIP = 6
# T3 subsystems. CCP's "Medium T2 Ships" rig target filter (8) spans
# category 32 alongside the T2 cruiser groups, so subsystems share the
# t2_ships facility setup (industry.classify_item, decision 2026-08-31).
CATEGORY_SUBSYSTEM = 32
# v1.9: Upwell structures (Citadels, Engineering Complexes, Refineries,
# FLEX structures), their Standup rigs / service / weapon modules, and the
# Structure Components commodity group — the Upwell/Standup/component
# subset of CCP's "Structures" rig target filter (12), which additionally
# spans Starbase (23), Infrastructure Upgrades (39), Sovereignty Structures
# (40), Fuel Blocks (1136) and Skyhooks (4736); those stay in 'other' by
# decision (2026-08-22). One item class covers the three sets in scope.
CATEGORY_STRUCTURE = 65
CATEGORY_STRUCTURE_MODULE = 66
STRUCTURE_CLASS_CATEGORIES = frozenset(
    {CATEGORY_STRUCTURE, CATEGORY_STRUCTURE_MODULE}
)
STRUCTURE_COMPONENT_GROUPS = frozenset({536})

# --- Skill time bonuses (user-entered levels, not read from ESI) -----------

# Verified from dogma attributes in the live SDE:
#   Industry (3380)           manufacturingTimeBonus            -4%/level
#   Advanced Industry (3388)  advancedIndustrySkillIndustryJobTimeBonus -3%/level
#   Reactions (45746)         reactionTimeBonus                 -4%/level
#   Per-blueprint science/construction skills carry
#   manufactureTimePerLevel = -1%/level and apply only to blueprints that
#   require them (Advanced * Ship Construction, * Starship Engineering,
#   Outpost Construction on Upwell structures / Standup modules / structure
#   fighters, and T2 science skills like Plasma Physics).
SKILL_TIME_INDUSTRY_PCT = -4.0
SKILL_TIME_ADV_INDUSTRY_PCT = -3.0
SKILL_TIME_REACTIONS_PCT = -4.0
# Skills lacking this attribute contribute no time bonus (no fallback).
ATTR_MANUFACTURE_TIME_PER_LEVEL = "manufactureTimePerLevel"
# Outpost Construction (type 3400) gets its own user-entered level (v1.9);
# industry._per_bp_skill_level routes it by exact name.
SKILL_NAME_OUTPOST_CONSTRUCTION = "Outpost Construction"

# --- Structure bonuses -----------------------------------------------------

# Structure bonus VALUES come exclusively from the imported SDE via
# industryModifierSources (industry.structure_multiplier) — the hardcoded
# multiplier tables that used to live here were referenced by nothing and
# were deleted 2026-08-20; tests pin the SDE-derived values instead
# (Raitaru 0.85/0.99, Azbel 0.80, Sotiyo 0.70, Tatara 0.75, cost
# 0.97/0.96/0.95).
STRUCTURE_TYPE_RAITARU = 35825
STRUCTURE_TYPE_AZBEL = 35826
STRUCTURE_TYPE_SOTIYO = 35827
STRUCTURE_TYPE_ATHANOR = 35835
STRUCTURE_TYPE_TATARA = 35836

# --- Item classes (per-class global build settings) ------------------------

# The user configures structure/rig/security/index/tax once per item class,
# not per facility (design change 2026-08-15). Classification precedence is
# implemented in industry.classify_item.
ITEM_CLASSES = (
    "capital_ships",
    "t2_ships",
    "t1_ships",
    "basic_capital_components",
    "advanced_components",
    "structures",  # v1.9: categories 65/66 + Structure Components (536)
    "reactions",
    "other",
    # v1.22: the labs where invention and T1 copy jobs are installed.
    # Never returned by industry.classify_item — no PRODUCT classifies
    # here; the invention cost math looks them up by name. Split into two
    # rows 2026-08-31 (user request): copying has its own per-system cost
    # index in game. On an existing database the copying row seeds from
    # the invention row it used to share (store._seed_class_settings).
    "invention",
    "copying",
)

# Capital hulls: CCP's "Capital Ships" rig target filter (Dreadnought,
# Carrier, Capital Industrial, Force Auxiliary, Lancer Dreadnought, Command
# Carrier) plus Titans (30) and Supercarriers (659). Note: Freighters (513)
# are "Large T1 Ships" and Jump Freighters (902) "Large T2 Ships" per CCP,
# so they classify as t1/t2 ships here, not capitals.
CAPITAL_SHIP_GROUPS = frozenset({30, 485, 547, 659, 883, 1538, 4594, 5120})

BASIC_CAPITAL_COMPONENT_GROUPS = frozenset({873})  # Capital Construction Comp.

# Construction Components (334, T2), Advanced Capital Construction
# Components (913), Hybrid Tech Components (964, T3).
ADVANCED_COMPONENT_GROUPS = frozenset({334, 913, 964})

# Ships built in EXACT quantities — never rounded up to the ship batch
# multiple: capitals, Freighters (513), Jump Freighters (902).
EXACT_QTY_SHIP_GROUPS = CAPITAL_SHIP_GROUPS | {513, 902}

# v1.6 capital pricing: hulls sell-priced from the secondary (structure)
# market instead of the Jita region — capitals plus Freighters, Jump
# Freighters, and Industrial Command Ships (941: Orca/Porpoise). Buy-side
# pricing is unaffected.
CAPITAL_PRICING_GROUPS = EXACT_QTY_SHIP_GROUPS | {941}

# The "C-J6MT" preset of the capital market toggle: the C-J6MT Keepstar
# (type 35834). Users who trade elsewhere pick "custom" in Settings and
# paste their own structure id instead.
CJ6_KEEPSTAR_STRUCTURE_ID = 1049588174021

# v1.9 structure pricing: Upwell structures, rigs and components sell on
# the sub-capital model (hub quote, standings fees, ISK/m³ freight-out on
# packaged volume) — except the 800,000 m³ XL hulls, which are not hauled
# per m³: freight-out is waived for them (decision 2026-08-22).
FREIGHT_OUT_EXEMPT_TYPES = frozenset(
    {
        35834,  # Keepstar
        40340,  # Upwell Palatine Keepstar
        35827,  # Sotiyo
    }
)

ATTR_TECH_LEVEL = "techLevel"

# Price snapshots for The Forge come from Jita 4-4 only (decision
# 2026-08-20): a 1-unit scam/stale order in a backwater station must not
# set the cost basis or the MILP savings objective. Other regions have no
# canonical hub and stay region-wide.
JITA_44_STATION_ID = 60003760
THE_FORGE_REGION_ID = 10000002
PRICE_STATION_FILTERS = {THE_FORGE_REGION_ID: JITA_44_STATION_ID}

# Composite reaction product groups (CCP's "Composite Reactions" target
# filter): their INPUTS get composite_reaction_extra_runs of extra buffer.
COMPOSITE_REACTION_GROUPS = frozenset({428, 429, 4932})

# Reactions that do NOT saturate the full cycle window: Hybrid Polymers
# (974) and Molecular-Forged Materials (4096) size their jobs to the
# stockpile deficit exactly like manufactured items.
NON_SATURATING_REACTION_GROUPS = frozenset({974, 4096})

# Production blacklist categories: (key, label, product group ids). Checked
# categories are bought instead of built and their sub-chains disappear
# from the plan. "t1_hulls" is special-cased: T1 ships appearing as
# intermediates (never final products).
BLACKLIST_CATEGORIES = (
    ("fuel_blocks", "Fuel Blocks", frozenset({1136})),
    ("tools", "Tools (R.A.M. modules)", frozenset({332})),
    ("t1_hulls", "Tech 1 Hulls (as components)", frozenset()),
    ("capital_components", "Capital Components", frozenset({873})),
    ("advanced_components", "Advanced Components", frozenset({334})),
    (
        "advanced_capital_components",
        "Advanced Capital Components",
        frozenset({913}),
    ),
    ("hybrid_components", "Hybrid Tech Components", frozenset({964})),
    ("intermediate_reactions", "Intermediate Reactions", frozenset({428})),
    ("composite_reactions", "Composite Reactions", frozenset({429})),
    ("hybrid_reactions", "Hybrid Polymer Reactions", frozenset({974})),
    ("biochemical_reactions", "Biochemical Reactions", frozenset({712})),
    (
        "molecular_forged_reactions",
        "Molecular-Forged Reactions",
        frozenset({4096}),
    ),
)

# --- Rig bonuses -----------------------------------------------------------

# Universal rig magnitudes, verified against the live SDE for both
# engineering (attributeEngRigMatBonus/TimeBonus) and reaction rigs
# (RefRigMatBonus/RefRigTimeBonus): T1 -2.0% / -20%, T2 -2.4% / -24%.
RIG_ME_PERCENT = {"none": 0.0, "t1": -2.0, "t2": -2.4}
RIG_TE_PERCENT = {"none": 0.0, "t1": -20.0, "t2": -24.0}

# Lab rig JOB-COST magnitudes (v1.22, verified live SDE 2026-08-31):
# every Standup Invention / Blueprint Copy / Laboratory Optimization rig —
# M, L and XL alike — carries attributeEngRigCostBonus -10.0 (T1) / -12.0
# (T2) on the standard engineering security bands (1.0/1.9/2.1). The
# manufacturing/reaction rig families carry 0 there, which is why cost
# rigs are modeled only for the lab activities.
LAB_RIG_COST_PERCENT = {"none": 0.0, "t1": -10.0, "t2": -12.0}

# Thukker component rigs (added 2026-08-21, verified live SDE): a lowsec
# specialist family with its own security bands (0.1 highsec / 1.9 lowsec /
# 0.1 null-WH) and a single tier. Their ME bonus splits by covered group:
# attributeThukkerEngRigMatBonus -3.7% pairs with the capital-component
# filters (Capital Components 873, Advanced Capital Construction Components
# 913), while plain T2/T3 components (334/964) get the standard
# attributeEngRigMatBonus -2.0% — both scaled by the Thukker bands. Time is
# the standard -20% on Thukker bands. No cost bonus.
THUKKER_RIG_ME_PERCENT_CAPITAL = -3.7
THUKKER_RIG_ME_PERCENT_STANDARD = -2.0
THUKKER_RIG_TE_PERCENT = -20.0
THUKKER_FULL_BONUS_GROUPS = frozenset({873, 913})
THUKKER_RIG_SEC_MULT_LOWSEC = 1.9
THUKKER_RIG_SEC_MULT_ELSEWHERE = 0.1
# Item classes whose products Thukker rigs actually cover — the settings UI
# offers the 'thukker' tier only here. The XL Thukker rig also carries the
# "Structures" filter at the standard -2.0 leg (verified live SDE), so the
# structures class may assert it too.
THUKKER_CLASSES = ("basic_capital_components", "advanced_components", "structures")


def thukker_security_multiplier(security: float) -> float:
    """Thukker rigs are tuned for lowsec: full band there, nearly dead
    (x0.1) in both highsec and null-WH."""
    if 0.0 < security < 0.45:
        return THUKKER_RIG_SEC_MULT_LOWSEC
    return THUKKER_RIG_SEC_MULT_ELSEWHERE

# Security-band multipliers applied to rig bonuses, from each rig family's
# own hiSecModifier / lowSecModifier / nullSecModifier attributes. The bands
# differ by family (verified live SDE 2026-08-20): ENGINEERING rigs carry
# 1.0 / 1.9 / 2.1, but REACTION (refinery) rigs carry lowSecModifier=1.0,
# nullSecModifier=1.1, and no hiSecModifier at all (refineries cannot deploy
# in highsec — the 1.0 fallback is unreachable in practice).
RIG_SEC_MULT_HIGHSEC = 1.0
RIG_SEC_MULT_LOWSEC = 1.9
RIG_SEC_MULT_NULLSEC = 2.1  # also wormhole space

REACTION_RIG_SEC_MULT_LOWSEC = 1.0
REACTION_RIG_SEC_MULT_NULLSEC = 1.1  # also wormhole space


def rig_security_multiplier(
    security: float, activity_id: int | None = None
) -> float:
    """Security-band multiplier for a system's true security value, per the
    rig family the activity uses (engineering vs reaction rigs)."""
    if activity_id == ACTIVITY_REACTION:
        if security >= 0.45:
            return 1.0  # unreachable: no refineries in highsec
        if security > 0.0:
            return REACTION_RIG_SEC_MULT_LOWSEC
        return REACTION_RIG_SEC_MULT_NULLSEC
    if security >= 0.45:
        return RIG_SEC_MULT_HIGHSEC
    if security > 0.0:
        return RIG_SEC_MULT_LOWSEC
    return RIG_SEC_MULT_NULLSEC


# Dogma attribute names consumed by rig/structure math.
ATTR_ENG_RIG_MAT_BONUS = "attributeEngRigMatBonus"
ATTR_ENG_RIG_TIME_BONUS = "attributeEngRigTimeBonus"
ATTR_ENG_RIG_COST_BONUS = "attributeEngRigCostBonus"
ATTR_HISEC_MODIFIER = "hiSecModifier"
ATTR_LOWSEC_MODIFIER = "lowSecModifier"
ATTR_NULLSEC_MODIFIER = "nullSecModifier"

# In-game per-job ceiling, ONE rule for manufacturing AND reactions
# (user-verified in client 2026-08-21): runs can keep being added while the
# job's total MODIFIED time is still under 30 days, so the LAST run may
# overhang — max runs = ceil(30 days / modified time per run), and a single
# run longer than 30 days still installs as 1 run. The user's earlier
# verified reaction caps were this same rule at their facility: 543 runs =
# 29d 23:21:59 -> a 544th is allowed (tpr 4,769.28s), alchemy 272 = ceil at
# 9,538.56s/run. Skills/structure/rig bonuses increase the fit. Note: the
# SDE's maxProductionLimit is the max licensed runs per blueprint COPY (a
# copying concept) and does NOT cap manufacturing jobs; whether it caps
# reaction jobs (formulas: 1000/100) is still unverified in client — it is
# kept as an additional reaction ceiling where lower.
MAX_JOB_SECONDS = 30 * 24 * 3600

# The ceiling scales with the formula's BASE time, not per formula: alchemy
# jobs (21,600s base, exactly 2x the 10,800s of direct composite reactions)
# cap at 272 runs — user-verified in game 2026-08-18, exactly half of 544.
# --- Job installation cost -------------------------------------------------

SCC_SURCHARGE = 0.04  # 4% (EVE University wiki)
NPC_STATION_FACILITY_TAX = 0.0025  # 0.25% (EVE University wiki)

# --- Invention (v1.22) -----------------------------------------------------

# Invention and copying job fees use 2% of the T1 blueprint's manufacturing
# EIV as the fee base (EVE University wiki; pending in-client verification
# like SCC_SURCHARGE) — the caller scales EIV before job_install_cost.
JOB_FEE_EIV_FRACTION = 0.02

# An invented copy starts at ME 2 / TE 4; the decryptor's modifiers add to
# these and to the base run count (invention product quantity).
INVENTED_BASE_ME = 2
INVENTED_BASE_TE = 4

DATACORE_GROUP = 333
DECRYPTOR_GROUP = 1304  # the 8 generic decryptors (published)

# Decryptor dogma attribute NAMES, verbatim from the SDE — including CCP's
# "Propability" typo. Do not "fix" the spelling.
ATTR_INVENTION_PROB_MULT = "inventionPropabilityMultiplier"
ATTR_INVENTION_ME_MOD = "inventionMEModifier"
ATTR_INVENTION_TE_MOD = "inventionTEModifier"
ATTR_INVENTION_RUN_MOD = "inventionMaxRunModifier"

# The racial "* Encryption Methods" skills weigh /40 in the invention chance
# (datacore sciences weigh /30) and get their own user-entered level;
# industry._per_bp_skill_level routes them by this name suffix.
SKILL_SUFFIX_ENCRYPTION = "Encryption Methods"
