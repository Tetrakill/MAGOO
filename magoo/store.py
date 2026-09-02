"""Schema creation and state persistence (PROJECT.md §6).

State tables only — reference tables are owned and rebuilt wholesale by
sdeimport.py; nothing here ever drops them, and import never touches these.
ensure_schema() is idempotent and seeds the settings row and one
class_setting row per item class.
"""

import logging
import sqlite3
from dataclasses import dataclass, fields

from magoo import __version__

from . import config
from .industry import BuildSetting, SkillLevels

log = logging.getLogger(__name__)

# Monotonic stamp written to PRAGMA user_version. Bump it whenever
# _MIGRATIONS grows, so an older build meets a clear refusal rather than
# a 'no such column' traceback. Databases written before v1.21 carry 0,
# which reads as 'older' — exactly right, since they predate the stamp.
SCHEMA_VERSION = 5

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline (
    pipeline_id           INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    final_product_type_id INTEGER NOT NULL,
    output_qty_per_run    INTEGER NOT NULL,
    is_active             INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    modified_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A newly authed character contributes NOTHING to planning until the user
-- opts each part in: most industrialists run everything from corporation
-- hangars, so counting a character's own assets/wallet/slots by default
-- silently inflated the plan. Note the column-to-label mapping, which is a
-- leftover from when count_assets was added and is easy to misread:
--   count_assets      -> "Count assets"
--   include_assets    -> "Count wallet"      (NOT assets)
--   include_job_slots -> "Count job slots"
-- These defaults only bind on a FRESH database; SQLite cannot alter a
-- column default, so esi.complete_login writes the zeros explicitly for
-- databases created before this change.
CREATE TABLE IF NOT EXISTS pool_character (
    character_id      INTEGER PRIMARY KEY,
    character_name    TEXT NOT NULL,
    include_assets    INTEGER NOT NULL DEFAULT 0,
    include_job_slots INTEGER NOT NULL DEFAULT 0
);

-- Single row (rowid 1), seeded by ensure_schema.
CREATE TABLE IF NOT EXISTS settings (
    id                             INTEGER PRIMARY KEY CHECK (id = 1),
    -- Fraction, 0.001-0.1 (i.e. 0.1%-10%), applied to intermediates/raw
    stockpile_buffer               REAL NOT NULL DEFAULT 0.05,
    max_run_duration_hours         REAL NOT NULL DEFAULT 24.0,
    ship_batch_multiple            INTEGER NOT NULL DEFAULT 8,
    composite_reaction_extra_runs  INTEGER NOT NULL DEFAULT 1,
    price_region_id                INTEGER NOT NULL DEFAULT 10000002,
    price_source                   TEXT NOT NULL DEFAULT 'sell'
);

CREATE TABLE IF NOT EXISTS tracked_system (
    solar_system_id INTEGER PRIMARY KEY
);

-- Explicit values always beat ESI owned-blueprint data, so the user can plan
-- against research levels not yet achieved.
CREATE TABLE IF NOT EXISTS blueprint_setting (
    blueprint_id INTEGER PRIMARY KEY,
    me_level     INTEGER NOT NULL DEFAULT 0,
    te_level     INTEGER NOT NULL DEFAULT 0
);

-- Global build settings per item class (design change 2026-08-15).
CREATE TABLE IF NOT EXISTS class_setting (
    item_class        TEXT PRIMARY KEY,
    structure_type_id INTEGER,
    security          REAL NOT NULL DEFAULT 1.0,
    me_rig            TEXT NOT NULL DEFAULT 'none'
                      CHECK (me_rig IN ('none','t1','t2','thukker')),
    te_rig            TEXT NOT NULL DEFAULT 'none'
                      CHECK (te_rig IN ('none','t1','t2','thukker')),
    system_cost_index REAL NOT NULL DEFAULT 0.0,
    tax_rate          REAL NOT NULL DEFAULT 0.0025
);

CREATE TABLE IF NOT EXISTS index_run (
    index_run_id  INTEGER PRIMARY KEY,
    run_number    INTEGER NOT NULL,
    planned_start TEXT,
    actual_start  TEXT,
    planned_end   TEXT,
    status        TEXT NOT NULL DEFAULT 'planned'
                  CHECK (status IN ('planned','active','complete'))
);

-- One row per item, merged across all active pipelines. The core output.
CREATE TABLE IF NOT EXISTS index_run_item (
    index_run_item_id         INTEGER PRIMARY KEY,
    index_run_id              INTEGER NOT NULL REFERENCES index_run,
    type_id                   INTEGER NOT NULL,
    on_hand_qty               INTEGER NOT NULL DEFAULT 0,
    in_progress_qty           INTEGER NOT NULL DEFAULT 0,
    target_stock_qty          INTEGER NOT NULL DEFAULT 0,
    deficit_qty               INTEGER NOT NULL DEFAULT 0,
    recommended_action        TEXT,
    blueprint_id              INTEGER,
    activity_id               INTEGER,
    time_per_run              REAL,
    portion_size              INTEGER,
    max_runs_per_job          INTEGER,
    total_runs_needed         INTEGER,
    jobs_needed_unconstrained INTEGER,
    jobs_allocated            INTEGER NOT NULL DEFAULT 0,
    runs_allocated            INTEGER NOT NULL DEFAULT 0,
    recommended_build_qty     INTEGER NOT NULL DEFAULT 0,
    recommended_buy_qty       INTEGER NOT NULL DEFAULT 0,
    build_savings_per_unit    REAL,
    capacity_limited          INTEGER NOT NULL DEFAULT 0,
    low_stock                 INTEGER NOT NULL DEFAULT 0,
    price_snapshot            REAL,
    UNIQUE (index_run_id, type_id)
);
-- (price_region_wide and the other post-v1 columns arrive via _MIGRATIONS)

-- Attributes shared demand back to pipelines.
CREATE TABLE IF NOT EXISTS index_run_item_pipeline (
    index_run_item_id INTEGER NOT NULL REFERENCES index_run_item,
    pipeline_id       INTEGER NOT NULL REFERENCES pipeline,
    qty_attributable  INTEGER NOT NULL,
    -- The item's max depth within THIS pipeline's own chain (v1.5 lag
    -- costing prices each input at its per-pipeline depth; the merged
    -- cross-pipeline max on index_run_item.depth is display-only).
    depth             INTEGER,
    PRIMARY KEY (index_run_item_id, pipeline_id)
);

-- v1.22: the invention economics persisted with each planned run — the
-- VINTAGE lag costing and the run's profit view read (costing.hull_cost
-- reads THIS, never the live pipeline config or today's prices). One row
-- per invention-enabled pipeline that resolved at plan time — since the
-- 2026-09-01 review INCLUDING a final that got no runs, so a starved
-- cycle still replays the invention expectation instead of the manual
-- bpc line. Since v1.23 the run injects NO buy rows and no production
-- sections (sizing/purchasing/copy jobs live on the live Invention tab)
-- and the realized replay prices from probability/runs_per_copy; the
-- informational copies_needed/attempts columns were dropped (schema 5).
CREATE TABLE IF NOT EXISTS index_run_invention (
    index_run_id        INTEGER NOT NULL REFERENCES index_run,
    pipeline_id         INTEGER NOT NULL REFERENCES pipeline,
    t1_blueprint_id     INTEGER NOT NULL,  -- T1 source blueprint OR relic type (T3)
    decryptor_type_id   INTEGER,           -- NULL = no decryptor
    probability         REAL NOT NULL,     -- clamped, skills applied
    invented_me         INTEGER NOT NULL,
    invented_te         INTEGER NOT NULL,
    runs_per_copy       INTEGER NOT NULL,
    datacores           TEXT NOT NULL,     -- json [[type_id, qty_per_attempt, landed_price|null], ...]
    decryptor_unit_price REAL,             -- landed; NULL = unpriced or no decryptor
    invention_fee_per_attempt REAL NOT NULL,
    copy_fee_per_attempt REAL NOT NULL,
    cost_per_run        REAL NOT NULL,     -- attempt_cost / (P x runs_per_copy)
    PRIMARY KEY (index_run_id, pipeline_id)
);

-- Reconciliation between plan and reality. The plan is advisory; ESI is the
-- ledger.
CREATE TABLE IF NOT EXISTS job_link (
    job_id                    INTEGER PRIMARY KEY,  -- ESI industry job ID
    index_run_id              INTEGER REFERENCES index_run,
    type_id                   INTEGER NOT NULL,
    matched_to_recommendation INTEGER NOT NULL DEFAULT 0,
    status                    TEXT NOT NULL DEFAULT 'observed'
                              CHECK (status IN ('observed','complete','reconciled'))
);

-- FIFO vintage costing.
CREATE TABLE IF NOT EXISTS cost_lot (
    lot_id               INTEGER PRIMARY KEY,
    type_id              INTEGER NOT NULL,
    created_index_run_id INTEGER REFERENCES index_run,
    quantity_original    INTEGER NOT NULL,
    quantity_remaining   INTEGER NOT NULL,
    unit_cost            REAL NOT NULL,
    source_type          TEXT NOT NULL CHECK (source_type IN ('purchased','manufactured'))
);
CREATE INDEX IF NOT EXISTS idx_cost_lot_fifo
    ON cost_lot (type_id, lot_id) WHERE quantity_remaining > 0;

-- Genealogy edges, FIFO ordered.
CREATE TABLE IF NOT EXISTS lot_consumption (
    output_lot_id INTEGER NOT NULL REFERENCES cost_lot,
    input_lot_id  INTEGER NOT NULL REFERENCES cost_lot,
    qty_consumed  INTEGER NOT NULL,
    PRIMARY KEY (output_lot_id, input_lot_id)
);

CREATE TABLE IF NOT EXISTS finished_batch (
    finished_batch_id           INTEGER PRIMARY KEY,
    pipeline_id                 INTEGER NOT NULL REFERENCES pipeline,
    index_run_id                INTEGER REFERENCES index_run,
    output_lot_id               INTEGER NOT NULL REFERENCES cost_lot,
    quantity                    INTEGER NOT NULL,
    total_cost_basis            REAL NOT NULL,
    market_value_at_completion  REAL,
    profit                      REAL
);

-- Production blacklist: checked categories and named items are bought, not
-- built (their sub-chains drop out of the plan).
CREATE TABLE IF NOT EXISTS blacklist_category (
    category_key TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS blacklist_item (
    type_id INTEGER PRIMARY KEY
);

-- ESI OAuth tokens (PROJECT.md §8).
CREATE TABLE IF NOT EXISTS esi_token (
    character_id  INTEGER PRIMARY KEY,
    refresh_token TEXT NOT NULL,
    access_token  TEXT,
    expires_at    TEXT,
    scopes        TEXT
);

-- Resolved asset locations (station/structure/container -> solar system).
-- fetched_at marks when a NULL (denied/unknown) answer was cached: 404s
-- are permanent, but a docking 403 is ACL state that changes, so NULL
-- rows are re-probed after a TTL (2026-08-27).
CREATE TABLE IF NOT EXISTS location_system (
    location_id     INTEGER PRIMARY KEY,
    solar_system_id INTEGER,
    fetched_at      TEXT
);

-- Cached market prices per (type, region, source).
CREATE TABLE IF NOT EXISTS market_price (
    type_id    INTEGER NOT NULL,
    region_id  INTEGER NOT NULL,
    source     TEXT NOT NULL,
    price      REAL,
    fetched_at TEXT NOT NULL,
    -- v1.9: 1 = the configured quote (hub station where one exists),
    -- 0 = region-wide fallback for a raw leaf with no hub order
    hub        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (type_id, region_id, source)
);

-- v1.10: the structure market's SELL ladder per wanted type (price,
-- remaining volume), replaced wholesale on every structure refresh.
-- The buy-venue depth check walks it at plan time.
CREATE TABLE IF NOT EXISTS structure_sell_order (
    structure_id  INTEGER NOT NULL,
    type_id       INTEGER NOT NULL,
    price         REAL NOT NULL,
    volume_remain INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS structure_sell_order_type
    ON structure_sell_order (structure_id, type_id);

-- Corporations reachable through the pool: which character answered each
-- corp endpoint family at the last ESI refresh (NULL = no role or skipped),
-- row counts as pull diagnostics, and the per-corp stock opt-out. Rows are
-- upserted on refresh (count_assets survives) and pruned when every member
-- has left the pool.
CREATE TABLE IF NOT EXISTS esi_corp (
    corporation_id   INTEGER PRIMARY KEY,
    corporation_name TEXT,
    count_assets     INTEGER NOT NULL DEFAULT 1,
    count_wallet     INTEGER NOT NULL DEFAULT 1,
    count_jobs       INTEGER NOT NULL DEFAULT 1,
    assets_via       INTEGER,
    jobs_via         INTEGER,
    wallet_via       INTEGER,
    asset_rows       INTEGER,
    job_rows         INTEGER,
    refreshed_at     TEXT
);
"""

# Columns added after the original schema; applied idempotently.
_MIGRATIONS = (
    # v1.21: notify-and-link update check. Enabled by default, but
    # inert until config.GITHUB_REPO is set.
    "ALTER TABLE settings ADD COLUMN update_check_enabled INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE settings ADD COLUMN update_checked_at TEXT",
    "ALTER TABLE settings ADD COLUMN update_etag TEXT",
    "ALTER TABLE settings ADD COLUMN update_latest TEXT",
    "ALTER TABLE settings ADD COLUMN update_dismissed TEXT",
    "ALTER TABLE settings ADD COLUMN esi_client_id TEXT",
    "ALTER TABLE settings ADD COLUMN esi_client_secret TEXT",
    "ALTER TABLE index_run ADD COLUMN wallet_character_isk REAL",
    "ALTER TABLE index_run ADD COLUMN wallet_corporation_isk REAL",
    "ALTER TABLE index_run_item ADD COLUMN depth INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN item_class TEXT",
    # v1.1: manual slot pools + user-entered skill levels (not from ESI)
    "ALTER TABLE settings ADD COLUMN manufacturing_slots INTEGER NOT NULL DEFAULT 10",
    "ALTER TABLE settings ADD COLUMN reaction_slots INTEGER NOT NULL DEFAULT 10",
    "ALTER TABLE settings ADD COLUMN skill_industry INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE settings ADD COLUMN skill_advanced_industry INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE settings ADD COLUMN skill_reactions INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE settings ADD COLUMN skill_adv_ship_construction INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE settings ADD COLUMN skill_starship_engineering INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE settings ADD COLUMN skill_science INTEGER NOT NULL DEFAULT 5",
    # Default ME/TE for intermediates (blueprints without an explicit
    # blueprint_setting row); ships get explicit rows via the pipeline paste.
    "ALTER TABLE settings ADD COLUMN default_intermediate_me INTEGER NOT NULL DEFAULT 10",
    "ALTER TABLE settings ADD COLUMN default_intermediate_te INTEGER NOT NULL DEFAULT 20",
    # Runs available on the final product's blueprint copy — caps runs per
    # job for that pipeline's product. NULL = uncapped (BPO / ample copies).
    "ALTER TABLE pipeline ADD COLUMN runs_per_bpc INTEGER",
    # Buffer became a fraction (0.001-0.1); old percent values clamp to max.
    "ALTER TABLE settings RENAME COLUMN stockpile_buffer_percent TO stockpile_buffer",
    "UPDATE settings SET stockpile_buffer = 0.1 WHERE stockpile_buffer > 0.1",
    # Raw inputs are bought just-in-time: consumption of this cycle's
    # allocated jobs x (1 + margin), net of stock. Fraction.
    "ALTER TABLE settings ADD COLUMN input_purchase_margin REAL NOT NULL DEFAULT 0.05",
    # One cycle's consumption per item (chain view); NULL on older runs.
    "ALTER TABLE index_run_item ADD COLUMN merged_min_qty INTEGER",
    # v1.4 alchemy: spare reaction slots may substitute unrefined-reaction
    # jobs for direct composite reactions when cheaper per unit.
    "ALTER TABLE settings ADD COLUMN alchemy_enabled INTEGER NOT NULL DEFAULT 0",
    # Unrefined items reprocess under scrapmetal rules: flat yield, 55% max
    # (50% structure base x Scrap Metal Processing V); rigs never apply.
    # User-asserted, like the class settings; fold any reprocessing tax in.
    "ALTER TABLE settings ADD COLUMN alchemy_reprocess_yield REAL NOT NULL DEFAULT 0.55",
    # Throttle: max alchemy jobs per unrefined type per cycle (0 = none).
    "ALTER TABLE settings ADD COLUMN max_alchemy_jobs_per_type INTEGER NOT NULL DEFAULT 4",
    # On unrefined plan rows: the composite this alchemy route feeds.
    "ALTER TABLE index_run_item ADD COLUMN alchemy_for_type_id INTEGER",
    # On composite plan rows: the cost comparison that justified (or
    # rejected) alchemy, and the composite units expected from this cycle's
    # alchemy jobs / already in flight as unrefined stock.
    "ALTER TABLE index_run_item ADD COLUMN direct_unit_cost REAL",
    "ALTER TABLE index_run_item ADD COLUMN alchemy_unit_cost REAL",
    "ALTER TABLE index_run_item ADD COLUMN alchemy_output_qty INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE index_run_item ADD COLUMN alchemy_credit_qty INTEGER NOT NULL DEFAULT 0",
    # v1.5 lag-based costing: hypothetical per-unit install fee snapshotted
    # for every buildable at plan time (whether or not jobs were installed),
    # so later runs can cost each stage from the run it was installed at.
    "ALTER TABLE index_run_item ADD COLUMN unit_install_fee REAL",
    # Stamped when the user marks a run executed; costing walks completed
    # runs only, ordered by run_number.
    "ALTER TABLE index_run ADD COLUMN completed_at TEXT",
    # All-in ISK cost to obtain one BPC for this pipeline's final product
    # (bought copy or invention datacores/decryptor/fees). Amortized per
    # hull as bpc_cost_isk / runs_per_bpc. NULL/0 = free (BPO).
    "ALTER TABLE pipeline ADD COLUMN bpc_cost_isk REAL",
    # v1.5 profit page: sell-side fee inputs. Broker fee at an NPC station
    # is computed from Broker Relations + standings; at a player structure
    # the owner's flat rate applies instead. Sales tax comes from
    # Accounting alone. Formulas in costing.py, verified against client.
    "ALTER TABLE settings ADD COLUMN skill_accounting INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE settings ADD COLUMN skill_broker_relations INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE settings ADD COLUMN standing_broker_faction REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE settings ADD COLUMN standing_broker_corp REAL NOT NULL DEFAULT 0.0",
    # (v1.5 also added sell_venue / structure_broker_rate here; removed
    # 2026-08-23 — NPC station is the only sell venue — see the DROP
    # COLUMNs below.)
    # Flat hauling rates (no collateral term by design — see PROJECT.md).
    "ALTER TABLE settings ADD COLUMN freight_in_isk_per_m3 REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE settings ADD COLUMN freight_out_isk_per_m3 REAL NOT NULL DEFAULT 0.0",
    # v1.6 capital pricing: capital-class hulls (CAPITAL_PRICING_GROUPS)
    # sell on a structure market instead of the Jita region — 'cj6' uses
    # the C-J6MT Keepstar preset, 'custom' the user-entered structure id.
    # They get their own fee pair and a fixed per-hull movement cost that
    # replaces ISK/m³ freight-out for them.
    "ALTER TABLE settings ADD COLUMN capital_market_mode TEXT NOT NULL DEFAULT 'cj6'",
    "ALTER TABLE settings ADD COLUMN capital_structure_id INTEGER",
    "ALTER TABLE settings ADD COLUMN capital_sales_tax REAL NOT NULL DEFAULT 0.0337",
    "ALTER TABLE settings ADD COLUMN capital_broker_rate REAL NOT NULL DEFAULT 0.01",
    "ALTER TABLE settings ADD COLUMN capital_movement_cost_isk REAL NOT NULL DEFAULT 0.0",
    # SCC surcharge on capital market sales — flat, unaffected by skills or
    # standings (1.5% since April 2023; user-adjustable like the rest).
    "ALTER TABLE settings ADD COLUMN capital_scc_surcharge REAL NOT NULL DEFAULT 0.015",
    # 2026-08-20: security became a band dropdown stored as a canonical
    # status. Impossible statuses (the review found a stored 2.1 — the
    # nullsec MULTIPLIER — silently reading as highsec) migrate to nullsec;
    # a reactions row in the highsec band migrates to lowsec, whose reaction
    # rig band (x1.0) matches what the old code computed for it.
    "UPDATE class_setting SET security = -0.5 WHERE security > 1.0 OR security < -1.0",
    "UPDATE class_setting SET security = 0.25 WHERE item_class = 'reactions' AND security >= 0.45",
    # 2026-08-20: lag costing prices each input at its depth within the
    # OWNING pipeline's chain, not the cross-pipeline merged max (which
    # made adding an unrelated pipeline shift an existing hull's realized
    # cost). NULL on pre-fix rows -> costing falls back to the merged depth.
    "ALTER TABLE index_run_item_pipeline ADD COLUMN depth INTEGER",
    # 2026-08-20: end dates of active jobs occupying pool slots (json
    # {activity_id: [iso timestamps]}), so planning can net multi-cycle
    # jobs — still running past the next index run — from the slot pool.
    "ALTER TABLE esi_snapshot ADD COLUMN job_ends TEXT",
    # 2026-08-20: two overlapping /run requests both computed MAX+1 and
    # inserted duplicate run numbers, which would corrupt the lag-costing
    # timeline if both were executed. The insert is atomic now; this is
    # the backstop.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_index_run_number "
    "ON index_run (run_number)",
    # 2026-08-21: build savings became the vertically-integrated chain
    # figure; raw leaves with no price cost 0 in it and are counted so the
    # UI can badge the figure as understated.
    "ALTER TABLE index_run_item ADD COLUMN savings_unpriced_inputs INTEGER "
    "NOT NULL DEFAULT 0",
    # 2026-08-21: the 4% job-cost SCC surcharge became a setting (it
    # lives only in server-side config/patch notes, not the SDE, so it
    # is user-adjustable like the other fee constants).
    "ALTER TABLE settings ADD COLUMN industry_scc_surcharge REAL "
    "NOT NULL DEFAULT 0.04",
    # 2026-08-22 (v1.9, structures scope): Outpost Construction gets its
    # own skill level; unpriced raw leaves fall back to a region-wide quote
    # from this region; fitted/deployed corp assets are excluded from stock
    # unless the user opts back in.
    "ALTER TABLE settings ADD COLUMN skill_outpost_construction INTEGER "
    "NOT NULL DEFAULT 5",
    # (v1.9 also added npc_goods_region_id here; merged into
    # price_region_id on 2026-08-23 — see the DROP COLUMN below.)
    "ALTER TABLE settings ADD COLUMN count_fitted_stock INTEGER "
    "NOT NULL DEFAULT 0",
    # 2026-08-22 (v1.9): price provenance — region-wide fallback quotes
    # for raw leaves without a hub-station order are marked hub = 0.
    "ALTER TABLE market_price ADD COLUMN hub INTEGER NOT NULL DEFAULT 1",
    # 2026-08-22 (v1.9): plan-time provenance of price_snapshot so the run
    # page badges the price it shows, not today's cache.
    "ALTER TABLE index_run_item ADD COLUMN price_region_wide INTEGER "
    "NOT NULL DEFAULT 0",
    # 2026-08-22 (v1.10, two-venue buying): inputs are bought from
    # whichever of the Jita hub and the structure market (C-J6MT) is
    # cheaper LANDED — a second flat freight-in rate for the structure leg,
    # a switch for the comparison, and per-item plan-time provenance: the
    # venue price_snapshot came from ('hub' / 'structure', NULL = unpriced)
    # and, for structure buys, how many units of the structure's sell
    # ladder still beat the Jita landed price (the depth flag's numerator).
    "ALTER TABLE settings ADD COLUMN structure_freight_in_isk_per_m3 REAL "
    "NOT NULL DEFAULT 0.0",
    "ALTER TABLE settings ADD COLUMN structure_buy_enabled INTEGER "
    "NOT NULL DEFAULT 1",
    "ALTER TABLE index_run_item ADD COLUMN buy_venue TEXT",
    "ALTER TABLE index_run_item ADD COLUMN structure_units_cheaper INTEGER",
    # 2026-08-23: the NPC-goods fallback region is the price region — one
    # "Price region" input under High Sec Trade Hub Pricing. The v1.9
    # column is dropped (SQLite >= 3.35; fails harmlessly once gone).
    "ALTER TABLE settings DROP COLUMN npc_goods_region_id",
    # 2026-08-23: sub-capital sales always list at an NPC station — the
    # player-structure venue and its flat broker rate are gone.
    "ALTER TABLE settings DROP COLUMN sell_venue",
    "ALTER TABLE settings DROP COLUMN structure_broker_rate",
    # 2026-08-23: the chain cost per unit behind build_savings_per_unit is
    # persisted (savings is now against the LANDED buy price, so
    # price_snapshot − savings no longer recovers it; NULL on older rows).
    "ALTER TABLE index_run_item ADD COLUMN unit_chain_cost REAL",
    # 2026-08-25 (ESI tab): opt a character's PERSONAL hangars into stock
    # (default off — the corp-assets-only scope of 2026-08-20 still holds
    # unless the user flips this per character).
    "ALTER TABLE pool_character ADD COLUMN count_assets INTEGER "
    "NOT NULL DEFAULT 0",
    # 2026-08-25 (ESI tab, second pass): the corp table matches the
    # character pool — per-corp wallet and jobs opt-outs beside the assets
    # one (off = that corp's pull is skipped for the family).
    "ALTER TABLE esi_corp ADD COLUMN count_wallet INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE esi_corp ADD COLUMN count_jobs INTEGER NOT NULL DEFAULT 1",
    # 2026-08-27 (audit): stamp cached location resolutions so denied-403
    # NULL rows can be re-probed after a TTL instead of sticking forever.
    "ALTER TABLE location_system ADD COLUMN fetched_at TEXT",
    # v1.22 T2 invention: per-pipeline decryptor choice. While on, the
    # pipeline's runs_per_bpc and its final blueprint's blueprint_setting
    # ME/TE are MATERIALIZED from the invention math at config time
    # (POST /pipelines/<id>/invention) and bpc_cost_isk is ignored in
    # favor of the computed invention cost. decryptor_type_id NULL with
    # use_invention=1 means "no decryptor".
    "ALTER TABLE pipeline ADD COLUMN use_invention INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pipeline ADD COLUMN decryptor_type_id INTEGER",
    # The user's own runs_per_bpc, stashed while invention OVERWRITES that
    # column with the invented copy's run count: the manual-BPC fallback
    # (pre-invention executed runs, stale configs) divides by THIS, so the
    # toggle can never reprice realized history, and turning invention off
    # restores it. NULL = was uncapped (or pipeline never toggled).
    "ALTER TABLE pipeline ADD COLUMN manual_runs_per_bpc INTEGER",
    # v1.22: the racial "* Encryption Methods" level for the invention
    # chance (weighs /40). The two datacore sciences reuse
    # skill_starship_engineering / skill_science via the name-family
    # router — no separate columns.
    "ALTER TABLE settings ADD COLUMN skill_encryption INTEGER NOT NULL DEFAULT 5",
    # T3 relic invention (2026-08-31): the chosen source for a
    # multi-source final — which relic tier, or which of several T1 BPOs.
    # Stores a ref_invention.blueprint_id (a relic TYPE id for T3, a T1
    # blueprint id for the seven multi-T1-source T2 targets). NULL = auto
    # (single-source pipelines never set it).
    "ALTER TABLE pipeline ADD COLUMN invention_source_blueprint_id INTEGER",
    # v1.23 BPC stockpile overbuild: the live Invention tab sizes
    # invention/copy production to ceil(one cycle's copies × multiplier),
    # netted against tracked BPC stock and in-flight lab jobs — BPCs
    # stocked like any other input material. Fractions 1.0-10.0 (the
    # settings form shows 100%-1000%). T1 covers source-copy jobs, T2
    # the invented copies.
    "ALTER TABLE settings ADD COLUMN t1_bpc_overbuild REAL NOT NULL DEFAULT 4.0",
    "ALTER TABLE settings ADD COLUMN t2_bpc_overbuild REAL NOT NULL DEFAULT 4.0",
    # Schema 5 (review 2026-09-01): the informational copies_needed /
    # attempts vintage columns had no reader once the run pages dropped
    # their invention section (costing replays probability/runs_per_copy);
    # SQLite >= 3.35 drops them, older builds fail harmlessly like the
    # 2026-08-23 drops.
    "ALTER TABLE index_run_invention DROP COLUMN copies_needed",
    "ALTER TABLE index_run_invention DROP COLUMN attempts",
)

# Persisted ESI state so planning is decoupled from the (slow) ESI pull.
_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS esi_snapshot (
    snapshot_id     INTEGER PRIMARY KEY,
    fetched_at      TEXT NOT NULL,
    on_hand         TEXT NOT NULL,   -- json {type_id: qty}
    in_progress     TEXT NOT NULL,   -- json {type_id: qty}
    active_jobs     TEXT NOT NULL,   -- json {activity_id: count}
    character_isk   REAL NOT NULL DEFAULT 0,
    corporation_isk REAL NOT NULL DEFAULT 0,
    job_ends        TEXT             -- json {activity_id: [iso end dates]}
);
"""


def connect() -> sqlite3.Connection:
    """Open the single application database, creating parent dirs if needed.

    WAL journal mode + a long busy timeout guard against "database is
    locked": concurrent requests (Flask's dev server is threaded) and
    OneDrive sync briefly holding file locks both otherwise trip SQLite's
    default 5-second limit."""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    except (OSError, sqlite3.OperationalError) as exc:
        # A packaged user has no terminal, so a bare "unable to open
        # database file" is unactionable. Name the directory instead.
        raise RuntimeError(
            f"cannot open the Magoo database at {config.DB_PATH} ({exc}). "
            f"Check that {config.DATA_DIR} exists and is writable."
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _rebuild_class_setting_for_thukker(conn: sqlite3.Connection) -> None:
    """2026-08-21: the me_rig/te_rig CHECK gained the 'thukker' tier.
    SQLite cannot alter a CHECK constraint, so pre-existing databases get a
    create-copy-swap rebuild (idempotent: keyed off the stored table SQL)."""
    _recover_orphaned_class_setting(conn)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'class_setting'"
    ).fetchone()
    if row is None or "thukker" in row["sql"]:
        return
    # One transaction, or a crash mid-rebuild commits the rename and the
    # empty new table while losing the copy — and the next launch, seeing a
    # 'thukker' CHECK already in place, would silently reseed the user's
    # facility settings to defaults. SQLite DDL is transactional, so this
    # genuinely is all-or-nothing.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE class_setting RENAME TO class_setting_old")
        conn.execute(
        """
        CREATE TABLE class_setting (
            item_class        TEXT PRIMARY KEY,
            structure_type_id INTEGER,
            security          REAL NOT NULL DEFAULT 1.0,
            me_rig            TEXT NOT NULL DEFAULT 'none'
                              CHECK (me_rig IN ('none','t1','t2','thukker')),
            te_rig            TEXT NOT NULL DEFAULT 'none'
                              CHECK (te_rig IN ('none','t1','t2','thukker')),
            system_cost_index REAL NOT NULL DEFAULT 0.0,
            tax_rate          REAL NOT NULL DEFAULT 0.0025
        )
        """
    )
        conn.execute(
            "INSERT INTO class_setting SELECT * FROM class_setting_old"
        )
        conn.execute("DROP TABLE class_setting_old")
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def _recover_orphaned_class_setting(conn: sqlite3.Connection) -> None:
    """Repair a database left half-rebuilt by a pre-v1.21 crash: the user's
    real settings stranded in class_setting_old while class_setting sits
    empty. Untouched databases never match, so this is a no-op for everyone
    whose upgrade completed normally."""
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('class_setting', 'class_setting_old')"
        )
    }
    if tables != {"class_setting", "class_setting_old"}:
        return
    if conn.execute("SELECT COUNT(*) c FROM class_setting").fetchone()["c"]:
        conn.execute("DROP TABLE class_setting_old")  # rebuild had finished
        conn.commit()
        return
    stranded = conn.execute(
        "SELECT COUNT(*) c FROM class_setting_old"
    ).fetchone()["c"]
    if stranded:
        log.warning(
            "recovering %d class_setting rows stranded by an interrupted "
            "upgrade",
            stranded,
        )
        conn.execute(
            "INSERT INTO class_setting SELECT * FROM class_setting_old"
        )
    conn.execute("DROP TABLE class_setting_old")
    conn.commit()


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _check_schema_version(conn: sqlite3.Connection) -> None:
    """Refuse a database written by a newer build.

    Without this, an older Magoo opening a newer database fails deep inside
    a query with "no such column" — unreadable for a packaged user, and the
    kind of thing that invites them to delete their data and start over.
    """
    found = _user_version(conn)
    if found > SCHEMA_VERSION:
        raise RuntimeError(
            f"This database was written by a newer version of Magoo "
            f"(data format {found}; this build understands "
            f"{SCHEMA_VERSION}). Install the latest release to open it. "
            f"Database: {config.DB_PATH}"
        )


def _backup_before_migrating(conn: sqlite3.Connection) -> None:
    """Snapshot an existing database before its first migration under a new
    build.

    Uses SQLite's backup API rather than copying the file: WAL mode means
    freshly committed data can live only in the -wal sidecar, so a plain
    copy of magoo.sqlite can silently miss the most recent writes.

    A backup that cannot be written must not stop the app from starting —
    the migrations themselves are additive — so failure is logged, not
    raised.
    """
    if _user_version(conn) >= SCHEMA_VERSION:
        return
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'settings'"
    ).fetchone()
    if exists is None:
        return  # brand-new database: nothing to lose
    dest_dir = config.DATA_DIR / "backups"
    dest = dest_dir / f"magoo-pre-{__version__}.sqlite"
    if dest.exists():
        return  # already snapshotted for this version; never overwrite
    try:
        if conn.in_transaction:
            conn.commit()
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            conn.backup(target)
        finally:
            target.close()
        log.info("wrote pre-upgrade backup %s", dest)
        _prune_backups(dest_dir)
    except (OSError, sqlite3.Error) as exc:
        log.warning(
            "could not write a pre-upgrade backup to %s (%s); continuing",
            dest,
            exc,
        )


def _prune_backups(dest_dir, keep: int = 3) -> None:
    try:
        found = sorted(
            dest_dir.glob("magoo-pre-*.sqlite"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in found[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create state tables if missing and seed default rows. Idempotent."""
    _check_schema_version(conn)
    _backup_before_migrating(conn)
    conn.executescript(STATE_SCHEMA)
    conn.executescript(_SNAPSHOT_SCHEMA)
    _rebuild_class_setting_for_thukker(conn)
    settings_columns_before = _columns(conn, "settings")
    for migration in _MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as exc:
            # Swallow only the already-applied cases; anything else (a
            # locked database, disk I/O, a typo in a new migration) must
            # surface here, not as a confusing crash later in the request.
            # "syntax error" tolerates the DROP COLUMN migrations on
            # SQLite < 3.35, which the 2026-08-23 entries deliberately
            # rely on failing harmlessly.
            msg = str(exc).lower()
            if not any(
                s in msg
                for s in (
                    "duplicate column",
                    "no such column",
                    "already exists",
                    "syntax error",
                )
            ):
                raise
    conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
    _seed_class_settings(conn)
    _seed_structure_freight_rate(conn, settings_columns_before)
    _clear_legacy_client_secret(conn)
    conn.commit()
    # Stamped last: only a database that made it through every
    # migration above may claim to be at this version.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _clear_legacy_client_secret(conn: sqlite3.Connection) -> None:
    """Magoo authenticates as a public PKCE client and never sends a client
    secret. A database from before v1.21 may still hold one the user pasted
    in through the retired CLI — wipe it rather than leave a live credential
    sitting in a file that gets synced, backed up and copied around. The
    column stays: dropping it would only make older builds fail harder than
    the user_version guard already makes them fail cleanly.
    """
    if "esi_client_secret" not in _columns(conn, "settings"):
        return
    conn.execute(
        "UPDATE settings SET esi_client_secret = NULL "
        "WHERE esi_client_secret IS NOT NULL"
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_structure_freight_rate(
    conn: sqlite3.Connection, settings_columns_before: set[str]
) -> None:
    """v1.10: on an EXISTING database the new structure freight-in rate is
    seeded as a copy of the user's Jita rate the moment its column is
    added — the structure leg was implicitly hauled at that rate until the
    two venues were told apart — so a configured Jita rate never makes the
    structure market look freight-free by default. One-shot (the column's
    absence before the migrations is the trigger); a fresh database starts
    both at 0, and a deliberate 0 set later is never overwritten."""
    if "structure_freight_in_isk_per_m3" in settings_columns_before:
        return
    if "freight_in_isk_per_m3" not in settings_columns_before:
        return  # fresh database: both columns arrive together at 0
    conn.execute(
        "UPDATE settings SET structure_freight_in_isk_per_m3 = "
        "freight_in_isk_per_m3 WHERE id = 1"
    )


def _seed_class_settings(conn: sqlite3.Connection) -> None:
    """One class_setting row per config.ITEM_CLASSES. A class that is new
    to an EXISTING database is seeded as a copy of the facility it was
    silently planned under until the class existed — 'copying' from the
    'invention' row it used to share (split 2026-08-31), everything else
    from the user's 'other' (Everything Else) row — rather than the
    NPC-station defaults, so adding a class never strips structure/rig
    bonuses from its items. On a fresh database every class starts at the
    defaults."""
    present = {
        row["item_class"]
        for row in conn.execute("SELECT item_class FROM class_setting")
    }
    seed_sources = {"copying": "invention"}
    for cls in config.ITEM_CLASSES:
        if cls in present:
            continue
        source = seed_sources.get(cls, "other")
        if source not in present:
            source = "other"
        if source in present:
            # Lab classes never inherit RIG tiers: the source row's rigs
            # are manufacturing rigs, and the lab tier (a job-cost rig
            # since 2026-08-31) is a separate in-game fitting the user
            # must assert themselves.
            rigs = (
                "'none', 'none'"
                if cls in ("invention", "copying")
                else "me_rig, te_rig"
            )
            conn.execute(
                "INSERT INTO class_setting (item_class, structure_type_id, "
                "security, me_rig, te_rig, system_cost_index, tax_rate) "
                f"SELECT ?, structure_type_id, security, {rigs}, "
                "system_cost_index, tax_rate FROM class_setting "
                "WHERE item_class = ?",
                (cls, source),
            )
        else:
            conn.execute(
                "INSERT INTO class_setting (item_class) VALUES (?)", (cls,)
            )
        present.add(cls)


# ---------------------------------------------------------------------------
# Read helpers for the engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    stockpile_buffer: float  # fraction, 0.001-0.1
    max_run_duration_hours: float
    ship_batch_multiple: int
    composite_reaction_extra_runs: int
    price_region_id: int
    price_source: str
    manufacturing_slots: int = 10
    reaction_slots: int = 10
    skill_industry: int = 5
    skill_advanced_industry: int = 5
    skill_reactions: int = 5
    skill_adv_ship_construction: int = 5
    skill_starship_engineering: int = 5
    skill_science: int = 5
    default_intermediate_me: int = 10
    default_intermediate_te: int = 20
    input_purchase_margin: float = 0.05  # extra bought vs. required, fraction
    alchemy_enabled: bool = False
    alchemy_reprocess_yield: float = 0.55  # scrapmetal cap; user-asserted
    max_alchemy_jobs_per_type: int = 4  # per unrefined type per cycle
    # v1.5 profit page: sell-side fees and hauling (costing.py)
    skill_accounting: int = 5
    skill_broker_relations: int = 5
    standing_broker_faction: float = 0.0
    standing_broker_corp: float = 0.0
    freight_in_isk_per_m3: float = 0.0
    freight_out_isk_per_m3: float = 0.0
    # v1.6 capital pricing (see costing.py)
    capital_market_mode: str = "cj6"  # 'cj6' preset | 'custom'
    capital_structure_id: int | None = None
    capital_sales_tax: float = 0.0337
    capital_broker_rate: float = 0.01
    capital_movement_cost_isk: float = 0.0
    capital_scc_surcharge: float = 0.015
    # SCC surcharge on JOB INSTALLATION cost (distinct from the market
    # sale surcharge above). 4% per EVE University wiki; not in the SDE
    # or ESI, so user-adjustable pending in-client verification.
    industry_scc_surcharge: float = 0.04
    # v1.9 structures scope
    skill_outpost_construction: int = 5
    # ESI stock: count modules/rigs/fuel/cores fitted in structures and
    # anchored structures themselves as on-hand stock (off = excluded).
    count_fitted_stock: bool = False
    # v1.10 two-venue buying: flat ISK/m³ from the structure market to the
    # industry system (freight_in_isk_per_m3 is the Jita leg), and whether
    # inputs may be bought there at all.
    structure_freight_in_isk_per_m3: float = 0.0
    structure_buy_enabled: bool = True
    # v1.22 invention: racial Encryption Methods level (chance weighs /40);
    # the datacore sciences reuse skill_starship_engineering/skill_science.
    skill_encryption: int = 5
    # v1.23: BPC stockpile targets on the Invention tab, as fractions of
    # one cycle's need (4.0 = 400%). T1 covers source-copy jobs, T2 the
    # invented copies.
    t1_bpc_overbuild: float = 4.0
    t2_bpc_overbuild: float = 4.0

    def capital_structure(self) -> int:
        """The structure whose market prices capital-class hulls."""
        if self.capital_market_mode == "custom" and self.capital_structure_id:
            return self.capital_structure_id
        return config.CJ6_KEEPSTAR_STRUCTURE_ID

    def structure_market(self) -> int:
        """The one structure market (v1.10): sells capital-class hulls AND
        quotes inputs for the buy-venue comparison. Same resolution as
        capital_structure(); the venue-neutral name."""
        return self.capital_structure()

    def structure_market_label(self) -> str:
        """Short UI label for the structure market: the preset is the
        C-J6MT Keepstar; a custom structure is named by its id."""
        if self.structure_market() == config.CJ6_KEEPSTAR_STRUCTURE_ID:
            return "C-J6"
        return f"structure {self.structure_market()}"

    def freight_in_rate(self, venue: str | None) -> float:
        """Flat inbound ISK/m³ for a buy venue: 'structure' takes the
        structure leg, anything else (hub, unpriced) the Jita leg."""
        if venue == BUY_VENUE_STRUCTURE:
            return self.structure_freight_in_isk_per_m3
        return self.freight_in_isk_per_m3

    def skill_levels(self) -> SkillLevels:
        """The user-entered levels as industry.SkillLevels (v1.22: lives
        here, not in engine, so costing's invention math can reuse it
        without an import cycle)."""
        return SkillLevels(
            industry=self.skill_industry,
            advanced_industry=self.skill_advanced_industry,
            reactions=self.skill_reactions,
            adv_ship_construction=self.skill_adv_ship_construction,
            starship_engineering=self.skill_starship_engineering,
            science=self.skill_science,
            outpost_construction=self.skill_outpost_construction,
            encryption=self.skill_encryption,
        )


# Buy venues (v1.10): where an input's price_snapshot came from.
BUY_VENUE_HUB = "hub"
BUY_VENUE_STRUCTURE = "structure"


def get_settings(conn: sqlite3.Connection) -> Settings:
    # Constructed by keyword from the row's own column names (they match
    # the field names exactly), so a field added out of order can never
    # silently transpose two same-typed settings. Extra columns (id,
    # un-dropped legacy columns on old SQLite) are filtered out.
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    names = {f.name for f in fields(Settings)}
    kwargs = {k: row[k] for k in row.keys() if k in names}
    for flag in (
        "alchemy_enabled",
        "count_fitted_stock",
        "structure_buy_enabled",
    ):
        kwargs[flag] = bool(kwargs[flag])
    return Settings(**kwargs)


def get_class_settings(conn: sqlite3.Connection) -> dict[str, BuildSetting]:
    return {
        row["item_class"]: BuildSetting(
            structure_type_id=row["structure_type_id"],
            security=row["security"],
            me_rig=row["me_rig"],
            te_rig=row["te_rig"],
            system_cost_index=row["system_cost_index"],
            tax_rate=row["tax_rate"],
        )
        for row in conn.execute("SELECT * FROM class_setting")
    }


def set_blueprint_setting(
    conn: sqlite3.Connection, blueprint_id: int, me: int, te: int
) -> None:
    """Pin a blueprint's ME/TE (upsert). The one writer of blueprint_setting
    (review 2026-09-01: the paste, the invention on/off paths and the
    inline ME/TE edit each carried their own copy of this statement)."""
    conn.execute(
        "INSERT INTO blueprint_setting VALUES (?, ?, ?) "
        "ON CONFLICT (blueprint_id) DO UPDATE SET me_level = "
        "excluded.me_level, te_level = excluded.te_level",
        (blueprint_id, me, te),
    )


def me_te_resolver(conn: sqlite3.Connection):
    """ME/TE per blueprint: explicit blueprint_setting (ships, written by
    the pipeline paste) -> global intermediate defaults from settings.
    Reactions have no ME/TE."""
    settings = get_settings(conn)
    default = (
        settings.default_intermediate_me,
        settings.default_intermediate_te,
    )
    explicit = {
        row["blueprint_id"]: (row["me_level"], row["te_level"])
        for row in conn.execute("SELECT * FROM blueprint_setting")
    }

    def resolve(blueprint_id: int, activity_id: int) -> tuple[int, int]:
        if activity_id == config.ACTIVITY_REACTION:
            return (0, 0)
        return explicit.get(blueprint_id, default)

    return resolve


def active_pipelines(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pipeline WHERE is_active = 1 ORDER BY pipeline_id"
    ).fetchall()


def tracked_systems(conn: sqlite3.Connection) -> set[int]:
    return {
        row["solar_system_id"]
        for row in conn.execute("SELECT solar_system_id FROM tracked_system")
    }


def pool_characters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pool_character").fetchall()


def corp_settings(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """esi_corp rows keyed by corporation_id (a corp with no row yet gets
    the schema defaults — assets count)."""
    return {
        row["corporation_id"]: row
        for row in conn.execute("SELECT * FROM esi_corp")
    }


def upsert_esi_corps(
    conn: sqlite3.Connection, records: list[dict], seen_ids: set[int]
) -> None:
    """Persist per-corp pull results from an ESI refresh. The count_assets
    toggle is user state and survives the upsert; corps whose members have
    all left the pool are pruned."""
    conn.executemany(
        "INSERT INTO esi_corp (corporation_id, corporation_name, "
        "assets_via, jobs_via, wallet_via, asset_rows, job_rows, "
        "refreshed_at) VALUES (:corporation_id, :corporation_name, "
        ":assets_via, :jobs_via, :wallet_via, :asset_rows, :job_rows, "
        "datetime('now')) ON CONFLICT (corporation_id) DO UPDATE SET "
        "corporation_name = excluded.corporation_name, "
        "assets_via = excluded.assets_via, "
        "jobs_via = excluded.jobs_via, "
        "wallet_via = excluded.wallet_via, "
        "asset_rows = excluded.asset_rows, "
        "job_rows = excluded.job_rows, "
        "refreshed_at = excluded.refreshed_at",
        records,
    )
    if seen_ids:
        placeholders = ",".join("?" * len(seen_ids))
        conn.execute(
            f"DELETE FROM esi_corp WHERE corporation_id NOT IN ({placeholders})",
            tuple(seen_ids),
        )
    else:
        conn.execute("DELETE FROM esi_corp")
    conn.commit()


def next_run_number(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(run_number) AS n FROM index_run").fetchone()
    return (row["n"] or 0) + 1


# ---------------------------------------------------------------------------
# Production blacklist
# ---------------------------------------------------------------------------


def blacklist_categories(conn: sqlite3.Connection) -> set[str]:
    return {
        row["category_key"]
        for row in conn.execute("SELECT category_key FROM blacklist_category")
    }


def blacklist_items(conn: sqlite3.Connection) -> set[int]:
    return {
        row["type_id"]
        for row in conn.execute("SELECT type_id FROM blacklist_item")
    }


def set_blacklist_categories(conn: sqlite3.Connection, keys: set[str]) -> None:
    conn.execute("DELETE FROM blacklist_category")
    conn.executemany(
        "INSERT INTO blacklist_category VALUES (?)", [(k,) for k in keys]
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Persisted ESI snapshots (planning is decoupled from the ESI pull)
# ---------------------------------------------------------------------------


def save_esi_snapshot(
    conn: sqlite3.Connection,
    on_hand: dict[int, int],
    in_progress: dict[int, int],
    active_jobs: dict[int, int],
    character_isk: float,
    corporation_isk: float,
    job_ends: dict[int, list] | None = None,
) -> int:
    import json

    cur = conn.execute(
        "INSERT INTO esi_snapshot (fetched_at, on_hand, in_progress, "
        "active_jobs, character_isk, corporation_isk, job_ends) "
        "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)",
        (
            json.dumps(on_hand),
            json.dumps(in_progress),
            json.dumps(active_jobs),
            character_isk,
            corporation_isk,
            json.dumps(job_ends or {}),
        ),
    )
    # Old snapshots are superseded the moment a newer one exists (decision
    # 2026-08-20: prune them; each row holds the full asset dict as JSON).
    conn.execute(
        "DELETE FROM esi_snapshot WHERE snapshot_id NOT IN "
        "(SELECT snapshot_id FROM esi_snapshot "
        " ORDER BY snapshot_id DESC LIMIT 5)"
    )
    conn.commit()
    return cur.lastrowid


def latest_esi_snapshot(conn: sqlite3.Connection):
    """(fetched_at, on_hand, in_progress, active_jobs, char_isk, corp_isk)
    with int keys restored, or None if ESI has never been pulled."""
    import json

    row = conn.execute(
        "SELECT * FROM esi_snapshot ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    intkeys = lambda d: {int(k): v for k, v in json.loads(d).items()}
    try:
        job_ends = intkeys(row["job_ends"] or "{}")
    except (KeyError, IndexError):
        job_ends = {}
    return {
        "fetched_at": row["fetched_at"],
        "on_hand": intkeys(row["on_hand"]),
        "in_progress": intkeys(row["in_progress"]),
        "active_jobs": intkeys(row["active_jobs"]),
        "character_isk": row["character_isk"],
        "corporation_isk": row["corporation_isk"],
        "job_ends": job_ends,
    }
