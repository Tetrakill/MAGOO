"""Schema creation and state persistence (PROJECT.md §6).

State tables only — reference tables are owned and rebuilt wholesale by
sdeimport.py; nothing here ever drops them, and import never touches these.
ensure_schema() is idempotent and seeds the settings row and one
class_setting row per item class.
"""

import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from magoo import __version__

from . import config
from .industry import BuildSetting, SkillLevels

log = logging.getLogger(__name__)

# Monotonic stamp written to PRAGMA user_version. Bump it whenever
# _MIGRATIONS grows, so an older build meets a clear refusal rather than
# a 'no such column' traceback. Databases written before v1.21 carry 0,
# which reads as 'older' — exactly right, since they predate the stamp.
SCHEMA_VERSION = 12

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
    volume_remain INTEGER NOT NULL,
    -- review 2026-09-05: an order's minimum fill; a rung whose min_volume
    -- exceeds the units a walk would take from it cannot be filled there
    min_volume    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS structure_sell_order_type
    ON structure_sell_order (structure_id, type_id);

-- v1.25: the Jita hub station's SELL ladder (price, remaining volume,
-- ascending) for every input the plan may buy, compressed sourcing
-- candidates included, replaced per type on every price refresh. The
-- sourcing pass walks it at plan time for each buy's fill price.
CREATE TABLE IF NOT EXISTS hub_sell_order (
    region_id     INTEGER NOT NULL,
    type_id       INTEGER NOT NULL,
    price         REAL NOT NULL,
    volume_remain INTEGER NOT NULL,
    min_volume    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS hub_sell_order_type
    ON hub_sell_order (region_id, type_id);

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

-- Ledger (schema 9, 2026-09-07): sell-side history persisted so it outlives
-- ESI's windows (contracts 30 d, orders 90 d; transactions back-paged as far
-- as ESI serves). Every type is stored; the Ledger filters to pipeline
-- finals at read time. owner_* names whose sale it is: a corp-wallet sale
-- seen through the selling character's feed is stored under the corporation.
-- ESI timestamps are stored verbatim (...Z); Magoo's own stamps use the same
-- shape (ledger._now_iso) so text comparison is exact. ESI enums (order
-- state, contract type/status) carry no CHECK: a value CCP adds must never
-- abort a pull. Rows are history -- nothing here is ever deleted.
CREATE TABLE IF NOT EXISTS sale_transaction (
    transaction_id  INTEGER PRIMARY KEY,   -- ESI market transaction id (global)
    owner_kind      TEXT NOT NULL CHECK (owner_kind IN ('character','corporation')),
    owner_id        INTEGER NOT NULL,
    division        INTEGER,               -- corp wallet 1..7; NULL = character wallet / not yet known
    source_feed     TEXT NOT NULL CHECK (source_feed IN ('character','corporation')),
    type_id         INTEGER NOT NULL,
    quantity        INTEGER NOT NULL,
    unit_price      REAL NOT NULL,
    date            TEXT NOT NULL,
    location_id     INTEGER NOT NULL,
    client_id       INTEGER,
    journal_ref_id  INTEGER,               -- journal-based fees: follow-up
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sale_transaction_type_date
    ON sale_transaction (type_id, date);
CREATE INDEX IF NOT EXISTS sale_transaction_owner
    ON sale_transaction (owner_kind, owner_id, transaction_id);

-- SELL orders: open, or closed within ESI's 90-day history window.
CREATE TABLE IF NOT EXISTS sale_order (
    order_id        INTEGER PRIMARY KEY,
    owner_kind      TEXT NOT NULL CHECK (owner_kind IN ('character','corporation')),
    owner_id        INTEGER NOT NULL,
    division        INTEGER,
    issued_by       INTEGER,
    source_feed     TEXT NOT NULL CHECK (source_feed IN ('character','corporation')),
    type_id         INTEGER NOT NULL,
    price           REAL NOT NULL,         -- last seen
    volume_total    INTEGER NOT NULL,
    volume_remain   INTEGER NOT NULL,      -- last seen
    location_id     INTEGER NOT NULL,
    duration        INTEGER NOT NULL,
    issued          TEXT NOT NULL,         -- EVE re-issues on a price edit; updated while open
    state           TEXT NOT NULL,         -- 'open' (Magoo) or ESI's history state verbatim
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    history_seen_at TEXT,                  -- first time the history feed reported it (estimated close)
    missing_since   TEXT                   -- open row absent from both feeds of an ok pull (cache skew)
);
CREATE INDEX IF NOT EXISTS sale_order_type_state ON sale_order (type_id, state);

-- Contracts ISSUED by the owner, every type and status (only finished,
-- priced item exchanges containing a final are sales; the rest explain).
CREATE TABLE IF NOT EXISTS sale_contract (
    contract_id           INTEGER PRIMARY KEY,
    owner_kind            TEXT NOT NULL CHECK (owner_kind IN ('character','corporation')),
    owner_id              INTEGER NOT NULL,
    issuer_id             INTEGER NOT NULL,
    issuer_corporation_id INTEGER NOT NULL,
    for_corporation       INTEGER NOT NULL,
    acceptor_id           INTEGER,
    assignee_id           INTEGER,
    availability          TEXT,
    type                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    price                 REAL,            -- ESI omits it on some contracts
    title                 TEXT,
    date_issued           TEXT NOT NULL,
    date_accepted         TEXT,
    date_completed        TEXT,
    date_expired          TEXT NOT NULL,
    start_location_id     INTEGER,
    via_character_id      INTEGER,         -- provenance only (NULLed on character delete)
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    items_fetched_at      TEXT,            -- NULL = items still to fetch
    items_status          TEXT CHECK (items_status IN ('ok','missing','unavailable')),
    items_attempts        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sale_contract_status
    ON sale_contract (type, status, date_completed);

CREATE TABLE IF NOT EXISTS sale_contract_item (
    contract_id  INTEGER NOT NULL REFERENCES sale_contract,
    record_id    INTEGER NOT NULL,
    type_id      INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    raw_quantity INTEGER,                  -- -1 singleton, -2 blueprint copy
    is_included  INTEGER NOT NULL,         -- 1 = issuer gives (sold), 0 = issuer asks (swap)
    is_singleton INTEGER NOT NULL,
    PRIMARY KEY (contract_id, record_id)
);
CREATE INDEX IF NOT EXISTS sale_contract_item_type ON sale_contract_item (type_id);

-- Provenance + cursor, one row per owner x family (x corp wallet division
-- for transactions). Never pruned with the data; a character leaving the
-- pool only NULLs via_character_id. The transactions cursor is ONE
-- contiguous covered id range (buys included): newest_id moves only when
-- a pull's top pass joined the stored range, backfilled flips once the
-- backfill pass reached the end of ESI's history.
CREATE TABLE IF NOT EXISTS sales_pull (
    owner_kind       TEXT NOT NULL CHECK (owner_kind IN ('character','corporation')),
    owner_id         INTEGER NOT NULL,
    family           TEXT NOT NULL CHECK (family IN ('orders','transactions','contracts')),
    division         INTEGER NOT NULL DEFAULT 0,   -- 1..7 for corp transactions, else 0
    status           TEXT NOT NULL CHECK (status IN
                         ('ok','partial','off','no_scope','no_role','error','skipped')),
    message          TEXT,
    via_character_id INTEGER,
    rows             INTEGER,              -- rows seen this pull
    rows_new         INTEGER,              -- COUNT(*) delta inside the write transaction
    calls            INTEGER,              -- ESI calls this pull (budget diagnostics)
    oldest_id        INTEGER,
    newest_id        INTEGER,
    backfilled       INTEGER NOT NULL DEFAULT 0,
    pulled_at        TEXT NOT NULL,
    PRIMARY KEY (owner_kind, owner_id, family, division)
);

-- Buy tab (v1.29, schema 12): what the pool ACTUALLY paid for an input
-- of a run. Several lines per item — that is how a purchase split across
-- venues, owners or orders is recorded. These are run purchase LINES,
-- not the dormant FIFO lots engine.record_purchase writes; the two
-- vocabularies stay apart (contract review A4). Realized costing lets
-- these override the plan's price snapshot; a run with no rows here
-- costs exactly as it did before v1.29. 'delivered' is a purchase venue
-- only (never a plan venue): its unit_price is already landed, so no
-- freight is added to it.
--
-- Revision 3 (user rulings 2026-09-28): the lines are DERIVED from ESI
-- (buy_transaction / buy_contract below) by buying.assign_purchases —
-- esi_kind, esi_id, contract_k, date and owner_* arrive via _MIGRATIONS
-- so a database already stamped 12 gains them; a row with esi_kind NULL
-- is not derived and replace_derived_purchases never touches it.
--
-- Revision 4 (user ruling 2026-09-28): 'other' is a purchase venue — a
-- buy anywhere but the Jita hub station and the configured structure
-- market, hauled at settings.freight_in_default_isk_per_m3. A database
-- already at 12 carries the three-venue CHECK, which SQLite cannot
-- alter: _rebuild_run_purchase_for_other_venue rebuilds it (contract
-- review A2). Keep this CHECK and that rebuild's in step.
CREATE TABLE IF NOT EXISTS run_purchase (
    purchase_id   INTEGER PRIMARY KEY,
    index_run_id  INTEGER NOT NULL REFERENCES index_run,
    type_id       INTEGER NOT NULL,
    venue         TEXT NOT NULL CHECK (venue IN ('hub','structure','delivered','other')),
    source        TEXT CHECK (source IN ('sell','split','buy') OR source IS NULL),
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    unit_price    REAL NOT NULL CHECK (unit_price >= 0),
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS run_purchase_run ON run_purchase (index_run_id, type_id);

-- Buy-side history (v1.29 revision 3, user ruling R1 2026-09-28). The
-- Ledger pull already reads every enabled owner's wallet transactions and
-- contracts; these tables keep the side of them it used to drop, so the
-- Buy tab's purchases come from ESI and nothing is hand-entered. They
-- mirror the sale_* tables above and share their conventions: ESI
-- timestamps verbatim (...Z), no CHECK on ESI enums (contract type /
-- status), rows are history and never deleted. Every owner's buys are
-- STORED; the per-owner Count buys toggle is honoured at READ time only
-- (buys_enabled_owners, the schema-9 precedent — contract review C7), so
-- switching it back on loses nothing. Schema 12 had not shipped when
-- these arrived, so they are plain CREATE IF NOT EXISTS and the stamp
-- does not move.
--
-- Wallet buys (is_buy = true), upserted with the Ledger's owner re-own
-- rule (ledger._OWNER_REOWN): a corp-wallet buy seen through the buying
-- character's feed is stored under the corporation.
CREATE TABLE IF NOT EXISTS buy_transaction (
    transaction_id  INTEGER PRIMARY KEY,   -- ESI market transaction id (global)
    owner_kind      TEXT NOT NULL CHECK (owner_kind IN ('character','corporation')),
    owner_id        INTEGER NOT NULL,
    division        INTEGER,               -- corp wallet 1..7; NULL = character wallet / not yet known
    source_feed     TEXT NOT NULL CHECK (source_feed IN ('character','corporation')),
    type_id         INTEGER NOT NULL,
    quantity        INTEGER NOT NULL,
    unit_price      REAL NOT NULL,
    date            TEXT NOT NULL,
    location_id     INTEGER NOT NULL,
    client_id       INTEGER,               -- the seller (internal-transfer rule, review C9)
    journal_ref_id  INTEGER,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS buy_transaction_date ON buy_transaction (date);
CREATE INDEX IF NOT EXISTS buy_transaction_owner
    ON buy_transaction (owner_kind, owner_id, transaction_id);

-- Item exchanges the pool ACCEPTED (owner_* is the buyer; the issuer is
-- the seller). Never mixed into sale_contract: the Ledger's tables and
-- queries stay byte-identical (§3). The acceptor pays price and receives
-- reward (review C9). The R7 allocation is frozen per CONTRACT, not per
-- run (review C10): once every received item has a reference price, k,
-- priced_at and buy_contract_item.unit_price are written and never
-- recomputed, so a reopen that moves the contract to another run keeps
-- its prices. excluded names why a contract is listed but not costed:
-- 'internal' (issued by the pool itself, or on behalf of one of its
-- corporations — an alt's 0-ISK hand-over is not a purchase; a corpmate
-- outside the pool selling personally is), 'swap' (the acceptor also gives items, or price - reward
-- <= 0) or 'no_price' (no received item has a reference price, so k is
-- NULL); NULL = costed. That one is Magoo's own vocabulary, so it carries
-- a CHECK.
CREATE TABLE IF NOT EXISTS buy_contract (
    contract_id           INTEGER PRIMARY KEY,
    owner_kind            TEXT NOT NULL CHECK (owner_kind IN ('character','corporation')),
    owner_id              INTEGER NOT NULL,
    issuer_id             INTEGER NOT NULL,
    issuer_corporation_id INTEGER,
    acceptor_id           INTEGER,
    type                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    price                 REAL,            -- ESI omits it on some contracts (NULL counts as 0)
    reward                REAL,
    title                 TEXT,
    date_issued           TEXT NOT NULL,
    date_accepted         TEXT,
    date_completed        TEXT,
    start_location_id     INTEGER,
    via_character_id      INTEGER,         -- provenance only (NULLed on character delete)
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    items_fetched_at      TEXT,            -- NULL = items still to fetch
    items_status          TEXT CHECK (items_status IN ('ok','missing','unavailable')),
    items_attempts        INTEGER NOT NULL DEFAULT 0,
    k                     REAL,            -- R7 scale: P / sum(p_i * q_i); NULL until priced
    unpriced_items        INTEGER NOT NULL DEFAULT 0,
    priced_at             TEXT,            -- set once every received item was priced (frozen)
    excluded              TEXT CHECK (excluded IN ('internal','swap','no_price')
                                      OR excluded IS NULL)
);
CREATE INDEX IF NOT EXISTS buy_contract_status
    ON buy_contract (type, status, date_completed);

CREATE TABLE IF NOT EXISTS buy_contract_item (
    contract_id  INTEGER NOT NULL REFERENCES buy_contract,
    record_id    INTEGER NOT NULL,
    type_id      INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    raw_quantity INTEGER,                  -- -1 singleton, -2 blueprint copy (never priced, C10)
    is_included  INTEGER NOT NULL,         -- 1 = the issuer gives (we received it), 0 = we gave it
    is_singleton INTEGER,
    unit_price   REAL,                     -- frozen R7 price k * p_i (0 for an unpriced item)
    PRIMARY KEY (contract_id, record_id)
);
CREATE INDEX IF NOT EXISTS buy_contract_item_type ON buy_contract_item (type_id);

-- An EXECUTED run's unplanned-ore conversions, kept apart from its
-- run_purchase lines (v1.29 revision 4, review 2026-09-28). The freeze
-- (buying._RunOres) re-emits a record's refined mineral lines verbatim on
-- an executed run; reading them only from run_purchase lost them the
-- moment a pass stopped costing the record (Count buys off, a character
-- removed) — replace_derived_purchases drops its lines — so switching it
-- back on re-converted at the CURRENT yield, tax and prices and moved the
-- run's realized cost. One row per refined line: (run, ESI record, ore,
-- raw) → units and landed unit price, plus the contract_k the lines were
-- written under (a contract still re-deriving recomputes). Rows exist
-- only for executed runs: the pass clears every other run's, so a
-- reopen re-derives as before. delete_run_purchases clears a run's.
-- Schema 12 had not shipped when this arrived: plain CREATE IF NOT
-- EXISTS, the stamp does not move.
CREATE TABLE IF NOT EXISTS run_purchase_refine (
    index_run_id INTEGER NOT NULL REFERENCES index_run,
    esi_kind     TEXT NOT NULL CHECK (esi_kind IN ('transaction','contract')),
    esi_id       INTEGER NOT NULL,
    via_type_id  INTEGER NOT NULL,
    type_id      INTEGER NOT NULL,
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    unit_price   REAL NOT NULL CHECK (unit_price >= 0),
    contract_k   REAL,
    PRIMARY KEY (index_run_id, esi_kind, esi_id, via_type_id, type_id)
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
    # {activity_id: [iso timestamps]}). Planning netted multi-cycle jobs
    # from the slot pool with them until v1.29 revision 6 (user ruling R2
    # 2026-09-28); they are still stored, but no planning step reads them.
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
    # v1.29 revision 4 (user ruling 2026-09-28, schema 12 unshipped): the
    # inbound rate for a purchase anywhere but Jita 4-4 and the structure
    # market (another station, another structure, a contract elsewhere,
    # no location). DEFAULT 0 — the user named no figure (contract review
    # A16), so while it is 0 such a purchase carries no freight.
    "ALTER TABLE settings ADD COLUMN freight_in_default_isk_per_m3 REAL "
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
    # Schema 6 (2026-09-05): the global Ship Batch Multiple is gone —
    # runs-per-BPC (pasted, or materialised from the invention choice)
    # is the only ship batch unit; with none set a ship final builds its
    # exact quantity. Same tolerant drop as schema 5; get_settings
    # filters a column an old SQLite could not drop.
    "ALTER TABLE settings DROP COLUMN ship_batch_multiple",
    # Schema 7 (v1.25, 2026-09-05): compressed sourcing — buy compressed
    # ore / moon ore / gas and reprocess it when cheaper landed than the
    # raw minerals / moon materials / gas, Jita ladder depth considered.
    # Two user-asserted yields (refinery for ore and moon ore, gas
    # decompression), reprocessing tax folded in. Defaults are the
    # maintainer's refinery figures (user, 2026-09-06): 90.63% ore,
    # 95% gas, 4% tax — a database that reaches schema 7 later gets
    # them; one already there keeps whatever it holds.
    "ALTER TABLE settings ADD COLUMN compressed_sourcing_enabled "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE settings ADD COLUMN compressed_ore_yield "
    "REAL NOT NULL DEFAULT 0.9063",
    "ALTER TABLE settings ADD COLUMN compressed_gas_yield "
    "REAL NOT NULL DEFAULT 0.95",
    # The reprocessing tax as its own figure (user request 2026-09-05):
    # a fraction of every output's value, charged per compressed buy;
    # the two yields above are then PURE yields. Ore and moon ore
    # refining only — gas decompression is untaxed (2026-09-06).
    "ALTER TABLE settings ADD COLUMN compressed_reprocess_tax "
    "REAL NOT NULL DEFAULT 0.04",
    # One toggle per raw group (user request, same day): minerals, moon
    # materials, gas. The single flag they replace is copied into all
    # three the moment they appear, then dropped — the UPDATE and the
    # DROP both fail harmlessly ("no such column") on every later start.
    "ALTER TABLE settings ADD COLUMN compressed_minerals_enabled "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE settings ADD COLUMN compressed_moon_enabled "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE settings ADD COLUMN compressed_gas_enabled "
    "INTEGER NOT NULL DEFAULT 0",
    "UPDATE settings SET compressed_minerals_enabled = 1, "
    "compressed_moon_enabled = 1, compressed_gas_enabled = 1 "
    "WHERE compressed_sourcing_enabled = 1",
    "ALTER TABLE settings DROP COLUMN compressed_sourcing_enabled",
    # Compressed buy rows: what the purchase is FOR — json
    # [[material_id, units out at the yield, units used to cover demand],
    # …] — plus the ladder depth the fill walked (units on the chosen
    # venue's ladder; orders taken), for the compressed tooltip.
    "ALTER TABLE index_run_item ADD COLUMN compressed_outputs TEXT",
    "ALTER TABLE index_run_item ADD COLUMN compressed_ladder_units INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN compressed_fill_orders INTEGER",
    # Raw rows: units of this cycle's purchase covered by reprocessing
    # compressed buys (recommended_buy_qty is the direct remainder), and
    # the blended LANDED per-unit cost the realized costing prices the
    # raw at (direct share at its landed quote + the allocated compressed
    # cost) — NULL when nothing was covered.
    "ALTER TABLE index_run_item ADD COLUMN compressed_covered_qty "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE index_run_item ADD COLUMN effective_unit_cost REAL",
    # Fill pricing (2026-09-05): every bought input walks its Jita and
    # structure sell ladders and may be SPLIT across them. Per-venue
    # units, average raw fill price and orders taken; the units no
    # stored ladder held (priced at the last rung walked; folded into the
    # hub quantity only when Jita's stored ladder was truncated at the
    # 300-rung cap, otherwise unsourced — review ruling R5) and that
    # marginal price. hub_buy_qty NULL = a
    # row priced before fill pricing (its buy_venue says it all).
    "ALTER TABLE index_run_item ADD COLUMN hub_buy_qty INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN hub_fill_price REAL",
    "ALTER TABLE index_run_item ADD COLUMN hub_fill_orders INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN structure_buy_qty INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN structure_fill_price REAL",
    "ALTER TABLE index_run_item ADD COLUMN structure_fill_orders INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN unfilled_qty "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE index_run_item ADD COLUMN unfilled_price REAL",
    # Review 2026-09-05: the compressed buy the LP WANTED before the ladder
    # shrank it to whole batches it held (equal to the buy since the
    # v1.26.1 re-solve unless the pass cap was hit; the compressed tooltip
    # states it when wanted > recommended_buy_qty); the two freight-in rates a run was
    # planned at, so the realized freight lines keep their vintage; the
    # ladders' min_volume on databases created before the column.
    "ALTER TABLE index_run_item ADD COLUMN compressed_wanted_qty INTEGER",
    "ALTER TABLE index_run ADD COLUMN freight_in_isk_per_m3 REAL",
    "ALTER TABLE index_run ADD COLUMN structure_freight_in_isk_per_m3 REAL",
    # v1.29 revision 4 (2026-09-28): the default ('other' venue) rate the
    # run was planned at, same vintage rule as the two above; NULL on runs
    # planned before it, which fall back to the live setting (contract
    # review A1).
    "ALTER TABLE index_run ADD COLUMN freight_in_default_isk_per_m3 REAL",
    # ... and the structure market it was planned against (review
    # 2026-09-28): an EXECUTED run's purchases are classed hub / structure
    # / other against it (buying.purchase_venue), so pointing Settings at
    # another structure later does not reclass that run's history onto
    # another freight leg. NULL on runs planned before it: the live
    # setting, as for the rates.
    "ALTER TABLE index_run ADD COLUMN structure_market_id INTEGER",
    "ALTER TABLE hub_sell_order ADD COLUMN min_volume "
    "INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE structure_sell_order ADD COLUMN min_volume "
    "INTEGER NOT NULL DEFAULT 1",
    # Landed ISK the compressed pass saved vs buying every covered raw
    # direct, at plan time (informational, the run page's strip badge).
    "ALTER TABLE index_run ADD COLUMN compressed_saving_isk REAL",
    # Schema 8 (v1.26, 2026-09-06): one pricing BASIS per market —
    # 'max_buy' (the best buy order, any quantity), 'min_sell' (the
    # cheapest sell order, any quantity) or 'ladder' (walk the sell
    # book for the quantity bought). The hub basis replaces the old
    # Price Source select: price_source stays as the ESI side the
    # refresh pulls, derived from it ('buy' only for max_buy).
    "ALTER TABLE settings ADD COLUMN hub_price_basis "
    "TEXT NOT NULL DEFAULT 'ladder'",
    "ALTER TABLE settings ADD COLUMN structure_price_basis "
    "TEXT NOT NULL DEFAULT 'ladder'",
    "UPDATE settings SET hub_price_basis = 'max_buy' "
    "WHERE price_source = 'buy' AND hub_price_basis = 'ladder'",
    # Fill-aware build-vs-buy (v1.26): the units of a buildable item
    # the market beat the build cost on (bought; the rest is built)
    # and the units dearer rungs could still supply (the only
    # purchase fallback a capacity shortfall may take).
    "ALTER TABLE index_run_item ADD COLUMN market_buy_qty "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE index_run_item ADD COLUMN market_fallback_qty INTEGER",
    # The bases a run was planned under (vintage for the run pages).
    "ALTER TABLE index_run ADD COLUMN hub_price_basis TEXT",
    "ALTER TABLE index_run ADD COLUMN structure_price_basis TEXT",
    # Schema 9 (2026-09-07): Ledger -- per-owner sales toggles, default on
    # (user ruling): each character and corporation decides whether its
    # sell orders, sale transactions and contracts are pulled and counted.
    # Honoured at READ time as well, so stored sales keep accruing and
    # simply stop counting while an owner is off.
    "ALTER TABLE pool_character ADD COLUMN count_sales INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE esi_corp ADD COLUMN count_sales INTEGER NOT NULL DEFAULT 1",
    # Schema 10 (2026-09-09): the install check (engine Phase 7.6). On a
    # row holding jobs: the runs / jobs stock on hand, in-flight output
    # and this cycle's buys can feed, the material that bound them and
    # (finals) the install priority; on a consumed row: the planned
    # jobs' draw and the units it exceeds availability by. NULL on rows
    # planned before the check existed.
    "ALTER TABLE index_run_item ADD COLUMN install_runs INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN install_jobs INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN install_limited_by INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN install_priority INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN install_draw_qty INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN install_short_qty INTEGER",
    # ... and the return on cost that ranked a final (NULL = unpriced).
    "ALTER TABLE index_run_item ADD COLUMN install_return REAL",
    # ... and the runs per installed job (uniform for an intermediate,
    # the plan's count with a remainder last job for ships / reactions).
    "ALTER TABLE index_run_item ADD COLUMN install_per_job INTEGER",
    # One cycle's consumption at the jobs' own rounding (engine Phase
    # 3.5, user ruling 2026-09-09) — the target and deficit basis; NULL
    # on rows planned before it existed (the Chain tab then shows the
    # merged BOM figure).
    "ALTER TABLE index_run_item ADD COLUMN cycle_need_qty INTEGER",
    # v1.27.1 (schema 11): the slot pools the run was planned against,
    # so the run page measures the plan against the pool it really had;
    # NULL on older runs (the page falls back to the settings' pools).
    # Review 2026-09-10. Until v1.29 they held the settings' pools less
    # the multi-cycle jobs running past the next index run (the
    # 2026-08-20 rule); since v1.29 revision 6 (user ruling R2,
    # 2026-09-28) they hold the SETTINGS' pools unchanged — the plan
    # never subtracts running jobs (their output nets through in-progress
    # stock). Rows written before keep the netted figure they were
    # planned against.
    "ALTER TABLE index_run ADD COLUMN manufacturing_slots_available INTEGER",
    "ALTER TABLE index_run ADD COLUMN reaction_slots_available INTEGER",
    # v1.29 (schema 12): what the compressed pass did, persisted so the
    # Buy tab's purchase lines can RE-BLEND a covered raw instead of
    # re-deriving plan-time arithmetic. On a compressed ore row:
    # compressed_alloc is json {raw_type_id: share} — the share of this
    # pick's landed cost the engine allocated to each covered raw
    # (per_m[m] / total_value; the shares sum to 1.0 whenever
    # total_value > 0, and are all 0.0 in the degenerate case the engine
    # allocates nothing — persisted as used, never normalised, contract
    # review A5) — compressed_landed_isk is the pick's landed cost
    # (order ISK + freight + reprocess tax) and compressed_tax_isk the
    # tax term of it (0.0 for gas, which is untaxed), which stays the
    # plan's even when the ore is re-priced: it is a function of the
    # outputs, not of the ore price. On a covered RAW row:
    # direct_landed_isk is the landed ISK of the direct remainder at
    # plan time. All four are NULL on runs planned before v1.29.
    "ALTER TABLE index_run_item ADD COLUMN compressed_alloc TEXT",
    "ALTER TABLE index_run_item ADD COLUMN compressed_landed_isk REAL",
    "ALTER TABLE index_run_item ADD COLUMN compressed_tax_isk REAL",
    "ALTER TABLE index_run_item ADD COLUMN direct_landed_isk REAL",
    # v1.29 revision 3 (user rulings 2026-09-28, schema 12 still
    # unshipped, so the stamp does not move): run_purchase lines are
    # derived from ESI. esi_kind / esi_id name the buy_transaction or
    # buy_contract a line came from (NULL = not derived: the helpers that
    # rewrite derived lines never touch it); contract_k copies the
    # contract's frozen R7 scale onto each of its lines; date and owner_*
    # are the purchase's own, for the Buy tab's Purchases section. ALTERs,
    # not STATE_SCHEMA columns, so a database already at 12 gains them.
    "ALTER TABLE run_purchase ADD COLUMN esi_kind TEXT "
    "CHECK (esi_kind IN ('transaction','contract') OR esi_kind IS NULL)",
    "ALTER TABLE run_purchase ADD COLUMN esi_id INTEGER",
    "ALTER TABLE run_purchase ADD COLUMN contract_k REAL",
    "ALTER TABLE run_purchase ADD COLUMN date TEXT",
    "ALTER TABLE run_purchase ADD COLUMN owner_kind TEXT "
    "CHECK (owner_kind IN ('character','corporation') OR owner_kind IS NULL)",
    "ALTER TABLE run_purchase ADD COLUMN owner_id INTEGER",
    # v1.29 revision 4 (user ruling 2026-09-28): an unplanned compressed
    # ore purchase is written as the minerals it yields; via_type_id names
    # the ore such a derived mineral line came from (NULL = a direct
    # purchase line). Only ever set on a 'delivered' line — the landed
    # price already carries the ore's freight and refining tax.
    "ALTER TABLE run_purchase ADD COLUMN via_type_id INTEGER",
    # The index MUST follow the ALTERs and must not live in STATE_SCHEMA:
    # executescript(STATE_SCHEMA) runs first, when run_purchase has no
    # esi_kind yet (fresh or at 12), and a CREATE INDEX there would raise
    # 'no such column' inside executescript, which nothing swallows
    # (contract review C6.1).
    "CREATE INDEX IF NOT EXISTS run_purchase_esi "
    "ON run_purchase (index_run_id, esi_kind, esi_id)",
    # Per-owner Count buys (R4), the sibling of count_sales, default on.
    # Honoured at READ time only (buys_enabled_owners): the pull stores
    # every owner's buys, so switching it back on recovers them (C7).
    "ALTER TABLE pool_character ADD COLUMN count_buys INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE esi_corp ADD COLUMN count_buys INTEGER NOT NULL DEFAULT 1",
    # ESI's for_corporation flag on a bought contract (review 2026-09-28):
    # issuer_corporation_id is the issuer's corporation on EVERY contract,
    # so only a contract issued on the corporation's behalf may be called
    # internal by its corporation — a corpmate outside the pool selling
    # personally is a real purchase. The ledger upsert overwrites it on
    # every pull, so a dev row stored before the column heals on the next
    # refresh.
    "ALTER TABLE buy_contract ADD COLUMN for_corporation INTEGER NOT NULL DEFAULT 0",
    # Revisions 1-2's click-to-lock choice table never shipped and
    # revision 3 retired it. A dev database at 12 may still hold it, and
    # its rows reference index_run, so an orphan row would block
    # web.run_delete (foreign_keys = ON) now that delete_run_purchases no
    # longer clears it. IF EXISTS makes this a no-op everywhere else.
    "DROP TABLE IF EXISTS run_buy_choice",
    # v1.29 revision 6 (user rulings 2026-09-28, schema 12 still
    # unshipped, so no version bump; ALTERs, not STATE_SCHEMA columns, so
    # a dev database already stamped 12 gains them — contract review A9).
    # R3: at plan time a raw in config.COMPRESSED_SOURCE_GROUPS is
    # credited with what the hangar's compressed ore / moon ore / gas
    # reprocesses into at the asserted yields (never ice).
    # index_run_item.on_hand_qty holds the stock the plan NETTED (ESI
    # hangar + that credit); on_hand_from_ore_qty is the "incl. N from
    # hangar compressed ore" part of it (A12), so hangar-only stock is
    # on_hand_qty − on_hand_from_ore_qty. NULL = planned before the
    # credit existed; every reader treats NULL as 0.
    "ALTER TABLE index_run_item ADD COLUMN on_hand_from_ore_qty INTEGER",
    # R1 + A3: the open run is re-planned IN PLACE on every ESI update, so
    # planned_start ("when the plan was last computed") moves forward
    # each time. opened_at is when the run was FIRST planned — the
    # engine's INSERT writes datetime('now') and the in-place replace
    # sets COALESCE(opened_at, planned_start) in the same UPDATE that
    # moves planned_start, never touching it otherwise — and it is what
    # buying.buying_windows opens a cycle's first window at, so a
    # re-plan never strands the cycle's earlier purchases. NULL on runs
    # planned before it: readers fall back to planned_start.
    "ALTER TABLE index_run ADD COLUMN opened_at TEXT",
    # Review 2026-09-28, reshaped by the 2026-09-29 ruling (revision 7:
    # final jobs started inside the current buying cycle are this
    # cycle's wave; the plan sizes the rest; every ESI update re-plans
    # the open run — the B1 stop rule is gone): per product, one
    # [start_date, units] pair per manufacturing/reaction job ESI
    # reports (active, paused, ready AND delivered — a delivered job must
    # keep counting or the next re-plan doubles the wave), json
    # {type_id: [[ESI ISO text, runs x portion], ...]}. The engine sums
    # the units of starts after the cycle cut. NULL = not recorded; a
    # value in the pre-2026-09-29 scalar format ({type_id: latest start})
    # exists only in dev/test databases and latest_esi_snapshot reads it
    # as NULL too, so the plan sizes the full wave.
    "ALTER TABLE esi_snapshot ADD COLUMN job_starts TEXT",
    # Revision 7 (2026-09-29; schema 12 is unreleased, so no bump —
    # contract amendment 10): the units of this type whose jobs started
    # inside the current buying cycle (esi_snapshot.job_starts after the
    # cycle cut). On a final it is the part of the wave already
    # installed — the plan sizes only the rest, and costing counts it as
    # built/started so an executed run re-planned after its installs
    # keeps its hull count; on any row it keeps the R7 "holds jobs this
    # cycle" predicate true for a consumer with 0 runs left. 0 = none
    # this cycle; NULL only on rows planned before the column.
    "ALTER TABLE index_run_item ADD COLUMN installed_qty INTEGER",
    # Revision 7 fix pass (2026-09-29, same unreleased schema 12): what a
    # pipeline final's wave was sized against, so the Industry Jobs badge
    # and the "why this quantity" dialog read the engine's figures rather
    # than re-deriving them. requested_qty is the pipelines' direct
    # request (0 on every non-final row); wave_qty is engine._final_wave's
    # wave — the request, or once part of it is installed this cycle the
    # whole blueprint copies it was planned at (contract amendment 8), so
    # "installed 10/10" on 8 requested with 10-run copies rather than
    # "installed 10/8". wave_qty is NULL on non-finals; both are NULL on
    # rows planned before the columns (readers fall back to
    # target_stock_qty).
    "ALTER TABLE index_run_item ADD COLUMN requested_qty INTEGER",
    "ALTER TABLE index_run_item ADD COLUMN wave_qty INTEGER",
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
    job_ends        TEXT,            -- json {activity_id: [iso end dates]}
    job_starts      TEXT             -- json {type_id: [[start, units], ...]}
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


# Every column of run_purchase in table order, for the revision-4 CHECK
# rebuild's copy (contract review A2: an explicit list, never SELECT *).
_RUN_PURCHASE_COLUMNS = (
    "purchase_id", "index_run_id", "type_id", "venue", "source", "quantity",
    "unit_price", "note", "created_at", "esi_kind", "esi_id", "contract_k",
    "date", "owner_kind", "owner_id", "via_type_id",
)


def _rebuild_run_purchase_for_other_venue(conn: sqlite3.Connection) -> None:
    """v1.29 revision 4 (user ruling 2026-09-28): the run_purchase.venue
    CHECK gained 'other' (a purchase anywhere but Jita 4-4 and the
    structure market). SQLite cannot alter a CHECK, and enforcing the new
    venue in _validate_purchase alone cannot work — the table's CHECK
    still refuses every 'other' INSERT, and one such line rolls back the
    matcher's whole pass (contract review A2). So a database already at
    12 (dev, the scratch preview copy) gets a create-copy-swap rebuild;
    a fresh database and the 11 -> 12 upgrade create the table from
    STATE_SCHEMA with the new CHECK and return at the first test.

    Called from ensure_schema AFTER the _MIGRATIONS loop, so the old table
    already carries esi_kind .. owner_id and via_type_id. Idempotent: keyed
    off the stored table SQL. One BEGIN IMMEDIATE ... COMMIT, like
    _rebuild_class_setting_for_thukker, so a crash mid-rebuild can never
    strand the lines.

    SQLite's own order: create run_purchase_new, copy, drop the old
    table, rename, THEN create the indexes. An index keeps its name
    through ALTER TABLE ... RENAME, so a CREATE INDEX IF NOT EXISTS issued
    while a renamed old table still existed would be silently skipped and
    the DROP would then take the index with it (A2.4).

    Connections run with PRAGMA foreign_keys = ON, so only lines whose run
    still exists are copied; an orphan would otherwise make the INSERT —
    and with it every start-up — raise. The dropped count is logged.
    purchase_id is kept (list_purchases orders by it). SCHEMA_VERSION
    stays 12, so no pre-upgrade backup is taken: accepted, the version is
    unshipped and derived lines regenerate (A2.6)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'run_purchase'"
    ).fetchone()
    if row is None or "'other'" in row[0]:
        return
    columns = ", ".join(_RUN_PURCHASE_COLUMNS)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # A leftover from a rebuild that died before its COMMIT cannot
        # exist (DDL is transactional), but IF EXISTS keeps a hand-made
        # one from blocking every start-up.
        conn.execute("DROP TABLE IF EXISTS run_purchase_new")
        conn.execute(
            """
            CREATE TABLE run_purchase_new (
                purchase_id   INTEGER PRIMARY KEY,
                index_run_id  INTEGER NOT NULL REFERENCES index_run,
                type_id       INTEGER NOT NULL,
                venue         TEXT NOT NULL
                              CHECK (venue IN ('hub','structure','delivered','other')),
                source        TEXT CHECK (source IN ('sell','split','buy') OR source IS NULL),
                quantity      INTEGER NOT NULL CHECK (quantity > 0),
                unit_price    REAL NOT NULL CHECK (unit_price >= 0),
                note          TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                esi_kind      TEXT CHECK (esi_kind IN ('transaction','contract')
                                          OR esi_kind IS NULL),
                esi_id        INTEGER,
                contract_k    REAL,
                date          TEXT,
                owner_kind    TEXT CHECK (owner_kind IN ('character','corporation')
                                          OR owner_kind IS NULL),
                owner_id      INTEGER,
                via_type_id   INTEGER
            )
            """
        )
        total = conn.execute("SELECT COUNT(*) FROM run_purchase").fetchone()[0]
        copied = conn.execute(
            f"INSERT INTO run_purchase_new ({columns}) "
            f"SELECT {columns} FROM run_purchase "
            "WHERE index_run_id IN (SELECT index_run_id FROM index_run)"
        ).rowcount
        conn.execute("DROP TABLE run_purchase")
        conn.execute("ALTER TABLE run_purchase_new RENAME TO run_purchase")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS run_purchase_run "
            "ON run_purchase (index_run_id, type_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS run_purchase_esi "
            "ON run_purchase (index_run_id, esi_kind, esi_id)"
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    if total != copied:
        log.warning(
            "dropped %d run_purchase lines whose index run no longer exists "
            "while widening the purchase venue CHECK",
            total - copied,
        )


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
        for path in (stale, _sidecar(stale, "-wal"), _sidecar(stale, "-shm")):
            try:
                path.unlink()
            except OSError:
                pass
    # A read-only open of a WAL-mode backup leaves -wal/-shm sidecars that
    # outlive the file they belong to; sweep the orphans (2026-09-08).
    for sidecar in list(dest_dir.glob("magoo-pre-*.sqlite-wal")) + list(
        dest_dir.glob("magoo-pre-*.sqlite-shm")
    ):
        base = sidecar.with_name(sidecar.name.rsplit("-", 1)[0])
        if not base.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass


def _sidecar(path, suffix: str):
    return path.with_name(path.name + suffix)


# First-run profile (2026-09-05): what a NEW install starts with — the
# author's working configuration, applied by ensure_schema exactly once,
# to a database that has no settings row yet. An existing database is
# never touched (a user's own settings survive every update: the column
# DEFAULTs above stay the neutral engine baseline the tests run on, and
# the profile is only ever layered onto a brand-new row). The app-path
# callers (web, desktop preflight, the ESI CLI) pass it; tests build
# neutral databases by leaving it out.
FIRST_RUN_PROFILE = {
    "settings": {
        "stockpile_buffer": 0.01,
        "max_run_duration_hours": 800.0,
        "composite_reaction_extra_runs": 544,
        "manufacturing_slots": 540,
        "reaction_slots": 540,
        "input_purchase_margin": 0.01,
        "alchemy_enabled": 1,
        "max_alchemy_jobs_per_type": 65,
        "standing_broker_faction": 4.82,
        "standing_broker_corp": 8.09,
        "freight_in_isk_per_m3": 900.0,
        "freight_out_isk_per_m3": 750.0,
        "capital_broker_rate": 0.015,
        "capital_movement_cost_isk": 25_000_000.0,
        "capital_scc_surcharge": 0.005,
        # v1.25: compressed sourcing on at the maintainer's refinery
        # figures (user, 2026-09-06): 90.63% ore, 95% gas, 4% tax.
        "compressed_minerals_enabled": 1,
        "compressed_moon_enabled": 1,
        "compressed_gas_enabled": 1,
        "compressed_ore_yield": 0.9063,
        "compressed_gas_yield": 0.95,
        "compressed_reprocess_tax": 0.04,
    },
    # Every class builds in null-sec (-0.5) structures at index 0.14%:
    # Sotiyo with T2 ME/TE rigs for every manufacturing class, Tatara
    # with T2 rigs for reactions, the lab classes on the Sotiyo with a T2
    # cost rig (the lab tier rides me_rig; te_rig is unused for labs).
    "class_setting": {
        "advanced_components": (config.STRUCTURE_TYPE_SOTIYO, "t2", "t2"),
        "basic_capital_components": (config.STRUCTURE_TYPE_SOTIYO, "t2", "t2"),
        "capital_ships": (config.STRUCTURE_TYPE_SOTIYO, "t2", "t2"),
        "copying": (config.STRUCTURE_TYPE_SOTIYO, "t2", "none"),
        "invention": (config.STRUCTURE_TYPE_SOTIYO, "t2", "none"),
        "other": (config.STRUCTURE_TYPE_SOTIYO, "t2", "t2"),
        "reactions": (config.STRUCTURE_TYPE_TATARA, "t2", "t2"),
        "structures": (config.STRUCTURE_TYPE_SOTIYO, "t2", "t2"),
        "t1_ships": (config.STRUCTURE_TYPE_SOTIYO, "t2", "t2"),
        "t2_ships": (config.STRUCTURE_TYPE_SOTIYO, "t2", "t2"),
    },
    "class_security": -0.5,
    "class_system_cost_index": 0.0014,
    "blacklist_categories": ("tools",),
}


def _apply_first_run_profile(conn: sqlite3.Connection, profile: dict) -> None:
    """Layer the first-run profile onto the freshly seeded rows. Only
    ensure_schema calls this, and only on a database that had no
    settings row before this call."""
    columns = _columns(conn, "settings")
    values = {k: v for k, v in profile["settings"].items() if k in columns}
    if values:
        conn.execute(
            "UPDATE settings SET "
            + ", ".join(f"{k} = ?" for k in values)
            + " WHERE id = 1",
            tuple(values.values()),
        )
    for cls, (structure, me_rig, te_rig) in profile["class_setting"].items():
        conn.execute(
            "UPDATE class_setting SET structure_type_id = ?, security = ?, "
            "me_rig = ?, te_rig = ?, system_cost_index = ? "
            "WHERE item_class = ?",
            (
                structure,
                profile["class_security"],
                me_rig,
                te_rig,
                profile["class_system_cost_index"],
                cls,
            ),
        )
    conn.executemany(
        "INSERT OR IGNORE INTO blacklist_category VALUES (?)",
        [(key,) for key in profile["blacklist_categories"]],
    )


def _has_settings_row(conn: sqlite3.Connection) -> bool:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'settings'"
    ).fetchone()
    if exists is None:
        return False
    return conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone() is not None


def ensure_schema(conn: sqlite3.Connection, profile: dict | None = None) -> None:
    """Create state tables if missing and seed default rows. Idempotent.
    profile (FIRST_RUN_PROFILE): applied on top of the seeded defaults
    when — and only when — the database had no settings row before this
    call; an existing database keeps its own values on every update."""
    _check_schema_version(conn)
    _backup_before_migrating(conn)
    fresh = profile is not None and not _has_settings_row(conn)
    if not _table_exists(conn, "buy_transaction"):
        # Before the CREATE, not after it: executescript COMMITs the
        # pending reset first, so a crash between the two can only repeat
        # the (idempotent) reset on the next open, never skip it for good
        # once buy_transaction exists (contract review C8).
        _rewalk_wallets_for_buys(conn)
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
    # After the loop: the rebuild copies via_type_id and the esi_* columns
    # the ALTERs above add (contract review A2.2).
    _rebuild_run_purchase_for_other_venue(conn)
    conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
    _seed_class_settings(conn)
    if fresh:
        _apply_first_run_profile(conn, profile)
    _seed_structure_freight_rate(conn, settings_columns_before)
    _clear_legacy_client_secret(conn)
    conn.commit()
    # Stamped last: only a database that made it through every
    # migration above may claim to be at this version.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _rewalk_wallets_for_buys(conn: sqlite3.Connection) -> None:
    """Reset every wallet-transactions cursor so the next pulls re-read the
    history whose buys the Ledger used to throw away (contract review C8,
    2026-09-27).

    Up to v1.28.1 ledger.collect dropped every is_buy row, yet the shared
    transactions cursor (sales_pull, family 'transactions') already covers
    that history, so without a reset the current cycle's purchases from
    before the upgrade would never arrive: the Buy tab's Remaining would
    overstate and invite buying twice. ensure_schema calls this only on
    the open that CREATES buy_transaction — every open re-runs the
    migrations, and an unguarded reset would re-walk every wallet at every
    start. The re-walk is safe: the sell upserts are idempotent, and the
    feeds read 'partial' for a few refreshes while the backfill catches
    up. Only the transactions family keeps an id cursor
    (ledger._cursor), so orders and contracts rows are left alone. Does
    not commit."""
    if not _table_exists(conn, "sales_pull"):
        return
    cur = conn.execute(
        "UPDATE sales_pull SET oldest_id = NULL, newest_id = NULL, "
        "backfilled = 0 WHERE family = 'transactions'"
    )
    if cur.rowcount:
        log.info(
            "reset %d wallet-transaction cursors so the next pulls recover "
            "the buys earlier builds dropped",
            cur.rowcount,
        )


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
    composite_reaction_extra_runs: int
    price_region_id: int
    # The ESI order side the hub refresh pulls and caches ('sell' /
    # 'buy'); since v1.26 derived from hub_price_basis on save.
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
    # v1.29 revision 4 (user ruling 2026-09-28): the inbound rate for a
    # PURCHASE anywhere but Jita 4-4 and the structure market — the
    # 'other' purchase venue (freight_in_rate). 0 = such a purchase
    # carries no freight.
    freight_in_default_isk_per_m3: float = 0.0
    structure_buy_enabled: bool = True
    # v1.22 invention: racial Encryption Methods level (chance weighs /40);
    # the datacore sciences reuse skill_starship_engineering/skill_science.
    skill_encryption: int = 5
    # v1.23: BPC stockpile targets on the Invention tab, as fractions of
    # one cycle's need (4.0 = 400%). T1 covers source-copy jobs, T2 the
    # invented copies.
    t1_bpc_overbuild: float = 4.0
    t2_bpc_overbuild: float = 4.0
    # v1.25 compressed sourcing: buy compressed ore / moon ore / gas and
    # reprocess when cheaper landed than the raws (Jita ladder depth
    # considered); two user-asserted PURE yields plus the reprocessing tax.
    compressed_minerals_enabled: bool = False
    compressed_moon_enabled: bool = False
    compressed_gas_enabled: bool = False
    compressed_ore_yield: float = 0.9063
    compressed_gas_yield: float = 0.95
    compressed_reprocess_tax: float = 0.04  # of output value; ore refining only
    # v1.26: pricing basis per market (PRICE_BASES). 'ladder' walks the
    # sell book for the quantity bought (fill pricing, the fill-aware
    # build-vs-buy rule); 'min_sell' / 'max_buy' price any quantity at
    # the best order — the market is then a single unbounded rung.
    hub_price_basis: str = "ladder"
    structure_price_basis: str = "ladder"

    def compressed_groups(self) -> frozenset[int]:
        """The raw groups compressed sourcing may cover — one toggle each
        (2026-09-05): minerals, moon materials, gas."""
        groups = set()
        if self.compressed_minerals_enabled:
            groups.add(config.COMPRESSED_MINERALS_GROUP)
        if self.compressed_moon_enabled:
            groups.add(config.COMPRESSED_MOON_GROUP)
        if self.compressed_gas_enabled:
            groups.add(config.COMPRESSED_GAS_SOURCE_GROUP)
        return frozenset(groups)

    @property
    def compressed_sourcing_enabled(self) -> bool:
        return bool(self.compressed_groups())

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

    def price_basis(self, venue: str | None) -> str:
        """The pricing basis of a buy venue (v1.26): the structure's for
        'structure', the hub's for anything else."""
        if venue == BUY_VENUE_STRUCTURE:
            return self.structure_price_basis
        return self.hub_price_basis

    def walks_ladder(self, venue: str | None) -> bool:
        return self.price_basis(venue) == PRICE_BASIS_LADDER

    def freight_in_rate(self, venue: str | None) -> float:
        """Flat inbound ISK/m³ for a buy venue: 'structure' takes the
        structure leg, 'other' (revision 4, 2026-09-28: a purchase
        anywhere but Jita 4-4 and the structure market) the default
        rate, anything else (hub, unpriced) the Jita leg.

        'delivered' is deliberately NOT refused: it still falls to the
        Jita leg and the callers guard it themselves (costing's
        leg_rate, web._run_buy_rates) — contract review A3."""
        if venue == BUY_VENUE_STRUCTURE:
            return self.structure_freight_in_isk_per_m3
        if venue == BUY_VENUE_OTHER:
            return self.freight_in_default_isk_per_m3
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
# v1.25 fill pricing: a buy split across both venues (per-venue units
# on the index_run_item row).
BUY_VENUE_SPLIT = "split"
# v1.29 Buy tab: a purchase venue only — never a plan venue. The price
# the user paid is already landed (someone else hauled it), so costing
# adds no freight to a delivered line and never resolves a freight rate
# or price basis for it (Settings.freight_in_rate / price_basis would
# silently hand back the HUB leg for an unknown venue).
BUY_VENUE_DELIVERED = "delivered"
# v1.29 revision 4 (user ruling 2026-09-28): a purchase venue only — a
# buy at any location other than the Jita hub station and the configured
# structure market (another NPC station, another structure, a contract
# whose start location is elsewhere, or no location). Hauled at
# settings.freight_in_default_isk_per_m3. run_purchase.venue repeats
# PURCHASE_VENUES in a CHECK: keep the two in step (append, never
# reorder).
BUY_VENUE_OTHER = "other"
PURCHASE_VENUES = (
    BUY_VENUE_HUB, BUY_VENUE_STRUCTURE, BUY_VENUE_DELIVERED, BUY_VENUE_OTHER,
)
# Optional label on a purchase line: which side of which book the user
# actually took. Advisory only — costing prices the line at unit_price.
# Revision 3 derives every line from ESI and stores source NULL on all of
# them (contract review C11); the label survives for non-derived lines.
PURCHASE_SOURCES = ("sell", "split", "buy")
# v1.29 revision 3 (user ruling R1, 2026-09-28): where a derived
# run_purchase line came from — a wallet buy (buy_transaction) or an item
# exchange the pool accepted (buy_contract). run_purchase.esi_kind repeats
# these in a CHECK: keep the two in step.
ESI_KIND_TRANSACTION = "transaction"
ESI_KIND_CONTRACT = "contract"
PURCHASE_ESI_KINDS = (ESI_KIND_TRANSACTION, ESI_KIND_CONTRACT)
# Why a bought contract is listed but not costed (buy_contract.excluded,
# contract review C9/C10; NULL = costed). The column's CHECK repeats them.
BUY_EXCLUDED_INTERNAL = "internal"
BUY_EXCLUDED_SWAP = "swap"
BUY_EXCLUDED_NO_PRICE = "no_price"
BUY_CONTRACT_EXCLUSIONS = (
    BUY_EXCLUDED_INTERNAL,
    BUY_EXCLUDED_SWAP,
    BUY_EXCLUDED_NO_PRICE,
)
OWNER_KINDS = ("character", "corporation")

# v1.26: the pricing basis of a market (settings.hub_price_basis /
# structure_price_basis).
PRICE_BASIS_MAX_BUY = "max_buy"
PRICE_BASIS_MIN_SELL = "min_sell"
PRICE_BASIS_LADDER = "ladder"
PRICE_BASES = (PRICE_BASIS_MAX_BUY, PRICE_BASIS_MIN_SELL, PRICE_BASIS_LADDER)


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
        "compressed_minerals_enabled",
        "compressed_moon_enabled",
        "compressed_gas_enabled",
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


def sales_pull_state(
    conn: sqlite3.Connection,
) -> dict[tuple[str, int], list[sqlite3.Row]]:
    """sales_pull rows grouped by owner -- the ESI tab's provenance cells and
    the Ledger's degrade notes read the same shape."""
    out: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM sales_pull "
        "ORDER BY owner_kind, owner_id, family, division"
    ):
        out.setdefault((row["owner_kind"], row["owner_id"]), []).append(row)
    return out


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
# Run purchase lines (Buy tab, v1.29)
# ---------------------------------------------------------------------------
#
# What the pool ACTUALLY paid for an input of one index run, one line per
# purchase (several per item: that is how a split buy is recorded).
# Realized costing lets these win over the plan's price snapshot, so a run
# with no lines costs exactly as it did before v1.29. These are run
# purchase lines in `run_purchase`, NOT the dormant FIFO lots
# engine.record_purchase writes (contract review A4).
#
# Revision 3 (user ruling R1, 2026-09-28): the lines are derived from ESI
# — buying.assign_purchases matches buy_transaction / buy_contract rows to
# a run's buying window and rewrites that run's derived lines wholesale
# through replace_derived_purchases. A derived line carries esi_kind /
# esi_id; add_purchase and the other per-line helpers stay as a complete
# CRUD seam for lines that are not derived (esi_kind NULL), which no UI
# path writes today.
#
# Every helper is scoped to the run: a purchase_id from another run
# never edits or deletes anything here. Like the rest of store.py the
# write helpers do NOT commit — the caller owns the transaction.


# The widest integer SQLite stores (INTEGER is 64-bit signed); anything
# above it raises OverflowError inside the INSERT rather than a ValueError.
_MAX_SQLITE_INT = 2**63 - 1


def _validate_purchase(
    venue: str, quantity, unit_price, source: str | None
) -> tuple[int, float]:
    """Check a purchase line before it reaches SQLite and return the
    coerced (quantity, unit_price).

    The table's CHECK constraints say the same things, but an
    IntegrityError reaches a packaged user as an unreadable traceback;
    the web layer flashes str(exc) instead, so every message here names
    the field in the user's own words.

    Two refusals the CHECKs cannot make (review 2026-09-23). `float()`
    accepts "nan" / "inf" / 1e400: NaN binds as NULL (a NOT NULL
    IntegrityError the routes do not catch) and infinity stores happily,
    then poisons the Profit tab and every Ledger cost vintage priced off
    the run with inf/nan and no way to notice. And an integer wider than
    SQLite's 64 bits raises OverflowError at the INSERT, which is not a
    ValueError either. No form reaches them any more (revision 3,
    2026-09-28: every line is derived from ESI rows and the R7 contract
    allocation), so the guards are defensive: a corrupt ESI row or a
    k * p_i that overflowed meets a sentence the caller can log or flash
    rather than an IntegrityError or an OverflowError.
    """
    if venue not in PURCHASE_VENUES:
        raise ValueError(
            f"unknown purchase venue {venue!r} "
            f"(expected one of {', '.join(PURCHASE_VENUES)})"
        )
    if source is not None and source not in PURCHASE_SOURCES:
        raise ValueError(
            f"unknown price source {source!r} "
            f"(expected one of {', '.join(PURCHASE_SOURCES)})"
        )
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        raise ValueError("quantity must be a whole number of units") from None
    if qty <= 0:
        raise ValueError("quantity must be at least 1 unit")
    if qty > _MAX_SQLITE_INT:
        raise ValueError("quantity is larger than Magoo can record")
    try:
        price = float(unit_price)
    except (TypeError, ValueError):
        raise ValueError("unit price must be a number") from None
    if not math.isfinite(price):
        raise ValueError("unit price must be a real number")
    if price < 0:
        raise ValueError("unit price cannot be negative")
    return qty, price


def _validate_via_type_id(venue: str, via_type_id) -> int | None:
    """Coerce a line's via_type_id (revision 4, user ruling 2026-09-28:
    the compressed ore an unplanned-ore purchase was refined from) to a
    whole number or None, or raise ValueError.

    Refused on every venue but 'delivered' (contract review A9): the
    matcher writes a via line at its LANDED price — the ore's freight and
    refining tax are already inside — so a via line on a freight-bearing
    venue would haul the same m³ twice."""
    if via_type_id is None:
        return None
    bad = ValueError("the refined-from ore must be a whole-number type id")
    # A bool is an int to Python, and int(34.5) is 34 — another type
    # entirely — so both are refused rather than coerced.
    if isinstance(via_type_id, bool) or (
        isinstance(via_type_id, float) and not via_type_id.is_integer()
    ):
        raise bad
    try:
        via = int(via_type_id)
    except (TypeError, ValueError, OverflowError):
        raise bad from None
    if not 0 < via <= _MAX_SQLITE_INT:
        raise bad
    if venue != BUY_VENUE_DELIVERED:
        raise ValueError(
            f"a line refined from an ore is landed already: its venue must be "
            f"{BUY_VENUE_DELIVERED!r}, not {venue!r}"
        )
    return via


def list_purchases(
    conn: sqlite3.Connection, index_run_id: int
) -> dict[int, list[sqlite3.Row]]:
    """Every purchase line of a run, grouped by type_id, oldest first
    (purchase_id order — the order the lines were written in, which is
    the order the Buy tab lists them in). Items with no lines are
    absent."""
    out: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM run_purchase WHERE index_run_id = ? "
        "ORDER BY purchase_id",
        (index_run_id,),
    ):
        out.setdefault(row["type_id"], []).append(row)
    return out


def add_purchase(
    conn: sqlite3.Connection,
    index_run_id: int,
    type_id: int,
    venue: str,
    quantity: int,
    unit_price: float,
    source: str | None = None,
    note: str | None = None,
    via_type_id: int | None = None,
) -> int:
    """Record one non-derived purchase line (esi_kind NULL); returns its
    purchase_id. Raises ValueError (user-readable) on a bad venue /
    source / quantity / price, or a via_type_id that is not a whole
    number or sits on a venue other than 'delivered'. Does not commit.

    Derived lines go through replace_derived_purchases instead, which
    owns their esi_* / owner_* columns. via_type_id (revision 4,
    2026-09-28) is passed through for the CRUD seam's completeness and
    the costing / Buy-tab tests that build via lines by hand."""
    qty, price = _validate_purchase(venue, quantity, unit_price, source)
    via = _validate_via_type_id(venue, via_type_id)
    cur = conn.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, source, "
        "quantity, unit_price, note, via_type_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (index_run_id, type_id, venue, source, qty, price, note or None, via),
    )
    return cur.lastrowid


def update_purchase(
    conn: sqlite3.Connection,
    index_run_id: int,
    purchase_id: int,
    venue: str,
    quantity: int,
    unit_price: float,
    source: str | None = None,
    note: str | None = None,
) -> None:
    """Edit one purchase line in place (the type_id never moves: a line
    belongs to the item it was booked under).

    Raises ValueError when the line is not on this run — an edit that
    matched nothing would otherwise be reported to the user as a save
    that worked. Does not commit.

    No UI path reaches this: revision 3 (2026-09-28) derives every line
    from ESI, and a derived line edited here would be overwritten by the
    next replace_derived_purchases anyway. Kept as a complete CRUD seam
    for a future per-line editor."""
    qty, price = _validate_purchase(venue, quantity, unit_price, source)
    cur = conn.execute(
        "UPDATE run_purchase SET venue = ?, source = ?, quantity = ?, "
        "unit_price = ?, note = ? "
        "WHERE purchase_id = ? AND index_run_id = ?",
        (venue, source, qty, price, note or None, purchase_id, index_run_id),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"purchase line {purchase_id} is not on this run"
        )


def delete_purchase(
    conn: sqlite3.Connection, index_run_id: int, purchase_id: int
) -> None:
    """Remove one purchase line of this run. Scoped to the run, so an id
    belonging to another run deletes nothing. A line that is already
    gone (double-submitted delete) is a silent no-op — the intent is
    satisfied either way. Does not commit.

    Like update_purchase, off the UI path since revision 3 derives every
    line from ESI (2026-09-28)."""
    conn.execute(
        "DELETE FROM run_purchase WHERE purchase_id = ? AND index_run_id = ?",
        (purchase_id, index_run_id),
    )


def delete_run_purchases_for_type(
    conn: sqlite3.Connection, index_run_id: int, type_id: int
) -> None:
    """Drop every purchase line this run holds for ONE item — derived or
    not. Kept from revision 2 as part of the CRUD seam (contract §2);
    revision 3's writer rewrites a whole run's derived lines through
    replace_derived_purchases instead. Does not commit."""
    conn.execute(
        "DELETE FROM run_purchase WHERE index_run_id = ? AND type_id = ?",
        (index_run_id, type_id),
    )


def delete_run_purchases(conn: sqlite3.Connection, index_run_id: int) -> None:
    """Drop every purchase line of a run, derived or not. run_delete must
    call this before deleting the run: run_purchase.index_run_id is a
    foreign key and connections open with PRAGMA foreign_keys = ON.

    A deleted run's derived lines are simply gone: the ESI rows they came
    from stay in buy_transaction / buy_contract, and the next
    assign_purchases pass matches them to whichever run's window holds
    them now. (Revision 2 also cleared run_buy_choice here; revision 3
    retired that table — contract review C6.5.) Its frozen ore
    conversions (run_purchase_refine, which also references index_run)
    go with them. Does not commit."""
    conn.execute(
        "DELETE FROM run_purchase WHERE index_run_id = ?", (index_run_id,)
    )
    conn.execute(
        "DELETE FROM run_purchase_refine WHERE index_run_id = ?", (index_run_id,)
    )


# The keys a derived line may carry (replace_derived_purchases). A key
# outside this set is refused rather than dropped, so a typo in the
# matcher ('k' for 'contract_k') fails loudly in its tests instead of
# silently storing NULL.
_DERIVED_REQUIRED = (
    "type_id", "venue", "quantity", "unit_price", "esi_kind", "esi_id",
)
_DERIVED_OPTIONAL = (
    "contract_k", "date", "owner_kind", "owner_id", "note", "source",
    "via_type_id",
)


def _validate_derived_line(line: Mapping[str, Any]) -> tuple:
    """One derived line -> the INSERT's values after the run id, in the
    order (type_id, venue, quantity, unit_price, note, esi_kind, esi_id,
    contract_k, date, owner_kind, owner_id, via_type_id), or ValueError
    naming what is wrong. Every ESI line stores source NULL (contract
    review C11): a 'source' key is tolerated only as None.

    via_type_id is LAST (revision 4, 2026-09-28): buying._line_key and
    _stored_lines compare against exactly this tuple for the matcher's
    no-change check, so a change only in via_type_id must show in it
    (contract review A9); it is a whole number or None, and only on a
    'delivered' line (_validate_via_type_id)."""
    keys = set(line.keys())
    missing = [k for k in _DERIVED_REQUIRED if k not in keys]
    if missing:
        raise ValueError(f"derived purchase line lacks {', '.join(missing)}")
    unknown = sorted(keys - set(_DERIVED_REQUIRED) - set(_DERIVED_OPTIONAL))
    if unknown:
        raise ValueError(
            f"derived purchase line has unknown field(s) {', '.join(unknown)}"
        )
    if line.get("source") is not None:
        raise ValueError(
            "a derived purchase line carries no price source (it is NULL "
            "on every ESI line)"
        )
    qty, price = _validate_purchase(
        line["venue"], line["quantity"], line["unit_price"], None
    )
    esi_kind = line["esi_kind"]
    if esi_kind not in PURCHASE_ESI_KINDS:
        raise ValueError(
            f"unknown purchase origin {esi_kind!r} "
            f"(expected one of {', '.join(PURCHASE_ESI_KINDS)})"
        )
    try:
        esi_id = int(line["esi_id"])
        type_id = int(line["type_id"])
    except (TypeError, ValueError):
        raise ValueError(
            "a derived purchase line needs a whole-number esi_id and type_id"
        ) from None
    owner_kind = line.get("owner_kind")
    if owner_kind is not None and owner_kind not in OWNER_KINDS:
        raise ValueError(
            f"unknown purchase owner kind {owner_kind!r} "
            f"(expected one of {', '.join(OWNER_KINDS)})"
        )
    owner_id = line.get("owner_id")
    if owner_id is not None:
        try:
            owner_id = int(owner_id)
        except (TypeError, ValueError):
            raise ValueError("purchase owner id must be a whole number") from None
    k = line.get("contract_k")
    if k is not None:
        try:
            k = float(k)
        except (TypeError, ValueError):
            raise ValueError("contract scale k must be a number") from None
        if not math.isfinite(k) or k < 0:
            raise ValueError("contract scale k must be a finite number >= 0")
    via = _validate_via_type_id(line["venue"], line.get("via_type_id"))
    return (
        type_id,
        line["venue"],
        qty,
        price,
        line.get("note") or None,
        esi_kind,
        esi_id,
        k,
        line.get("date"),
        owner_kind,
        owner_id,
        via,
    )


def replace_derived_purchases(
    conn: sqlite3.Connection,
    index_run_id: int,
    lines: Iterable[Mapping[str, Any]],
) -> int:
    """Rewrite a run's ESI-derived purchase lines wholesale; returns how
    many were written.

    Deletes every line of the run whose esi_kind is set, then inserts
    `lines` in the order given (each a mapping: type_id, venue, quantity,
    unit_price, esi_kind, esi_id required; contract_k, date, owner_kind,
    owner_id, note, via_type_id optional — via_type_id names the ore a
    refined mineral line came from, revision 4). Lines that are not derived (esi_kind NULL)
    are never touched. An empty `lines` clears the run's derived lines —
    what assign_purchases does to a run that became superseded, so the
    same ESI purchase is never on two runs (contract review C4).

    Every line is validated BEFORE anything is deleted (the same
    _validate_purchase guards as add_purchase, plus the esi_* / owner_*
    fields), so a bad line raises ValueError with the run untouched.
    source is stored NULL on every line (C11). Does NOT commit: the
    matcher wraps its whole pass over every run in one BEGIN IMMEDIATE
    ... COMMIT, so a failure can never leave a purchase on two runs or on
    none (C5)."""
    rows = [(index_run_id, *_validate_derived_line(line)) for line in lines]
    conn.execute(
        "DELETE FROM run_purchase WHERE index_run_id = ? "
        "AND esi_kind IS NOT NULL",
        (index_run_id,),
    )
    conn.executemany(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, source, "
        "quantity, unit_price, note, esi_kind, esi_id, contract_k, date, "
        "owner_kind, owner_id, via_type_id) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


# A frozen conversion group: (contract_k, ((raw type_id, units, landed
# unit price), ...) in ascending raw type id) keyed by (esi_kind, esi_id,
# via_type_id) — buying._via_groups' shape.
RefineGroups = dict[tuple[str, int, int], tuple[float | None, tuple]]


def refine_freeze(conn: sqlite3.Connection, index_run_id: int) -> RefineGroups:
    """A run's stored frozen ore conversions (run_purchase_refine), in
    buying._via_groups' shape: {(esi_kind, esi_id, via_type_id):
    (contract_k, ((type_id, quantity, unit_price), ...))}, each group's
    lines by ascending raw type id — the order the matcher writes them."""
    groups: dict[tuple, list] = {}
    scale: dict[tuple, float | None] = {}
    for r in conn.execute(
        "SELECT esi_kind, esi_id, via_type_id, type_id, quantity, unit_price, "
        "contract_k FROM run_purchase_refine WHERE index_run_id = ? "
        "ORDER BY esi_kind, esi_id, via_type_id, type_id",
        (index_run_id,),
    ):
        key = (r[0], int(r[1]), int(r[2]))
        groups.setdefault(key, []).append((int(r[3]), int(r[4]), float(r[5])))
        scale[key] = r[6]
    return {key: (scale[key], tuple(lines)) for key, lines in groups.items()}


def save_refine_freeze(
    conn: sqlite3.Connection, index_run_id: int, groups: RefineGroups
) -> int:
    """Store (replace) the given conversion groups of an executed run;
    groups not named are KEPT — that is the point: a record the pass is
    not costing right now keeps its frozen conversion for when it counts
    again. Returns how many groups were written. Does not commit (the
    matcher's one transaction)."""
    for (kind, esi_id, via), (k, lines) in groups.items():
        conn.execute(
            "DELETE FROM run_purchase_refine WHERE index_run_id = ? "
            "AND esi_kind = ? AND esi_id = ? AND via_type_id = ?",
            (index_run_id, kind, int(esi_id), int(via)),
        )
        conn.executemany(
            "INSERT INTO run_purchase_refine (index_run_id, esi_kind, esi_id, "
            "via_type_id, type_id, quantity, unit_price, contract_k) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (index_run_id, kind, int(esi_id), int(via), int(t), int(q),
                 float(u), None if k is None else float(k))
                for t, q, u in lines
            ],
        )
    return len(groups)


def drop_refine_groups(
    conn: sqlite3.Connection, index_run_id: int, records: Iterable[tuple[str, int]]
) -> None:
    """Drop a run's frozen conversions of the given (esi_kind, esi_id)
    records — the matcher's call for records that left the run's window
    (a reopen / re-execute moved the bound), so a record that later moves
    back converts fresh rather than reviving a stale freeze. Does not
    commit."""
    conn.executemany(
        "DELETE FROM run_purchase_refine WHERE index_run_id = ? "
        "AND esi_kind = ? AND esi_id = ?",
        [(index_run_id, kind, int(esi_id)) for kind, esi_id in records],
    )


def clear_refine_freeze(conn: sqlite3.Connection, keep: Iterable[int]) -> None:
    """Drop the frozen conversions of every run NOT in `keep` (the executed
    buying windows): a reopened, superseded or window-less run re-derives
    its conversions when it next collects. Does not commit."""
    keep = sorted({int(i) for i in keep})
    conn.execute(
        "DELETE FROM run_purchase_refine "
        f"WHERE index_run_id NOT IN ({','.join('?' * len(keep))})",
        keep,
    )


def runs_with_derived_purchases(conn: sqlite3.Connection) -> set[int]:
    """Every index_run_id that holds at least one ESI-derived line — the
    runs assign_purchases must clear when they fall outside every buying
    window (contract review C4)."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT index_run_id FROM run_purchase "
            "WHERE esi_kind IS NOT NULL"
        )
    }


def buys_enabled_owners(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    """Owners whose purchases count (R4: the Count buys toggle, honoured at
    READ time — every owner's buys are stored, contract review C7).

    Mirrors ledger.enabled_owners on count_buys: a pool character counts
    while its flag is on (a character that left the pool counts no
    longer); a corporation counts unless its esi_corp row turned the flag
    off, so a corporation without an esi_corp row (pruned when its last
    member left the pool, or seen only through a pull) still counts. The
    corporation set is every corporation Magoo has seen as a buyer or a
    pull owner: sales_pull, buy_transaction, buy_contract and esi_corp
    (C6.6) — not the sale_* tables, which only name sellers."""
    on = {
        ("character", r[0])
        for r in conn.execute(
            "SELECT character_id FROM pool_character WHERE count_buys = 1"
        )
    }
    off_corps = {
        r[0]
        for r in conn.execute(
            "SELECT corporation_id FROM esi_corp WHERE count_buys = 0"
        )
    }
    corps = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT owner_id FROM sales_pull "
            "WHERE owner_kind = 'corporation' "
            "UNION SELECT DISTINCT owner_id FROM buy_transaction "
            "WHERE owner_kind = 'corporation' "
            "UNION SELECT DISTINCT owner_id FROM buy_contract "
            "WHERE owner_kind = 'corporation' "
            "UNION SELECT corporation_id FROM esi_corp"
        )
    }
    on |= {("corporation", c) for c in corps if c not in off_corps}
    return on


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
    job_starts: dict[int, list[list]] | None = None,
) -> int:
    """job_starts: {product type_id: [[start_date, units], ...]} — one
    pair per manufacturing/reaction job of that product that ESI
    reports, delivered ones included, units = runs x portion
    (esi.refresh_state; revision 7, 2026-09-29). The engine counts the
    units started after the cycle cut as this cycle's wave. None stores
    NULL — "not recorded", which the plan reads as nothing installed
    (the full wave), not as a known empty list."""
    import json

    cur = conn.execute(
        "INSERT INTO esi_snapshot (fetched_at, on_hand, in_progress, "
        "active_jobs, character_isk, corporation_isk, job_ends, job_starts) "
        "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?)",
        (
            json.dumps(on_hand),
            json.dumps(in_progress),
            json.dumps(active_jobs),
            character_isk,
            corporation_isk,
            json.dumps(job_ends or {}),
            None if job_starts is None else json.dumps(job_starts),
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
    """The newest snapshot as a dict (fetched_at, on_hand, in_progress,
    active_jobs, character_isk, corporation_isk, job_ends, job_starts)
    with int keys restored, or None if ESI has never been pulled.
    job_starts is {type_id: [[start, units], ...]} or None (not recorded,
    or the old scalar format)."""
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
    try:
        raw_starts = row["job_starts"]
    except (KeyError, IndexError):
        raw_starts = None
    job_starts = None if raw_starts is None else intkeys(raw_starts)
    # The one reader that normalises (contract amendment 10): the
    # pre-2026-09-29 scalar format ({type_id: latest start text}) carries
    # no units, so it is "not recorded" — the plan sizes the full wave
    # until the next ESI update writes the list format.
    if job_starts is not None and not all(
        isinstance(v, list) for v in job_starts.values()
    ):
        job_starts = None
    return {
        "fetched_at": row["fetched_at"],
        "on_hand": intkeys(row["on_hand"]),
        "in_progress": intkeys(row["in_progress"]),
        "active_jobs": intkeys(row["active_jobs"]),
        "character_isk": row["character_isk"],
        "corporation_isk": row["corporation_isk"],
        "job_ends": job_ends,
        # None = not recorded (a snapshot saved before the column, by a
        # caller that passed none, or in the old scalar format) —
        # unknown, not "no jobs".
        "job_starts": job_starts,
    }
