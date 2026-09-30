"""Purchases from ESI (v1.29 revisions 3 and 4, user rulings 2026-09-28).

The Buy tab records nothing by hand any more (R1): the Ledger pull keeps
the pool's wallet BUY transactions (buy_transaction) and the item
exchanges it ACCEPTED (buy_contract / buy_contract_item), and
assign_purchases derives every run's `run_purchase` lines from them, so
realized costing (costing._apply_purchases) prices what was actually paid.

* **Windows (R2, contract review C2/C3).** A purchase belongs to a buying
  CYCLE, not a plan. An executed run (status 'complete') collects
  (lo, completed_at], where lo is the greatest completed_at among the
  executed runs before it (by run_number) — or, with none, when the run
  was first opened (index_run.opened_at, falling back to planned_start on
  runs planned before the column). The newest run (MAX index_run_id), while not executed,
  collects (lo, +inf) by the same lo rule. Every other non-executed run is
  superseded and collects nothing; an executed run with no completed_at
  (pre-v1.5) collects nothing. Bounds are compared as parsed UTC
  datetimes: opened_at / planned_start / completed_at are SQLite 'YYYY-MM-DD
  HH:MM:SS' text and ESI dates end in 'Z', and on the same day a space
  sorts before 'T'. A late ESI pull may still add a row dated inside an
  executed run's closed window — the window is a date bound, not a
  freeze. Reopen / re-execute moves the bound.

  Revision 6 (user ruling R1, 2026-09-28; contract review A3): the open
  run is re-planned IN PLACE on every ESI update, which moves its
  planned_start to "now" each time. The first window therefore opens at
  opened_at — written once when the run is first planned and never moved
  by a re-plan — not at planned_start; otherwise every update would empty
  the open run's window and the first executed run would lose the
  cycle's earlier purchases. planned_start keeps meaning "when the plan
  was last computed" (costing's pre-plan cut). A run superseded by a NEW
  run number (legacy plans, or a plan made before re-planning in place)
  still collects nothing: a purchase made under it lands on the newest
  run only when dated inside that run's window. (Contract review C3
  offered MIN(planned_start) over the earlier plans instead; that reading
  was not adopted.)
* **Toggles and exclusions (R4, C7, C9).** Owners with Count buys off
  contribute nothing (store.buys_enabled_owners, read time). An internal
  transfer is not a purchase: a wallet buy whose seller (client_id), or a
  contract whose issuer is one of ours (ledger.internal_ids) — or which
  was issued on behalf of one of our corporations (for_corporation set
  and issuer_corporation_id ours) — is listed "internal — not costed" and
  writes no line: the alt that buys at Jita and contracts the goods to
  the main at 0 ISK would otherwise record the units twice. ESI names the
  issuer's corporation on EVERY contract, so without the for_corporation
  gate a corpmate outside the pool selling personally would be dropped
  (review 2026-09-28; the rule _contract_owner already follows). A contract in which we
  also GAVE items, or whose price − reward is not positive, is a 'swap'.
* **Venue (R6, revised 2026-09-28).** By where it was bought
  (purchase_venue): the hub station (Jita 4-4, the price region's station)
  is 'hub', the configured structure market 'structure', and ANY other
  location — another NPC station, another structure, a contract whose
  start location is elsewhere, no location — 'other', hauled at the
  Settings default inbound rate (user ruling 2026-09-28, superseding R6's
  "NPC station → hub, structure → structure market, none → hub", which
  followed the Ledger's costing.sale_venue; that rule stays for sales).
  Freight is costing's, at the run's persisted rates. An EXECUTED run's
  purchases are classed against the structure market it was planned
  against (index_run.structure_market_id — review 2026-09-28), so a later
  change of structure in Settings never reclasses history; the open run
  follows the live setting.
* **Unplanned compressed ore (revision 4, user ruling 2026-09-28).** Any
  bought compressed ore / moon ore / gas covers the raws it yields at the
  user's asserted yields, whether or not the plan picked it. A costed
  line of a compressed type the run did NOT choose (no index_run_item row
  with compressed_outputs) is replaced by one 'delivered' line per raw it
  refines into — whole batches only (units // portion_size), outputs
  CompressedSource.batch_output at the ore / gas yield, the ore's landed ISK
  (price × converted units + the run's venue freight + the refining tax
  for ore, never gas) split over the raws by landed plan value — each
  marked via_type_id = the ore; the units short of a batch stay an ore
  line (outside the plan). 'delivered' because the freight is already
  inside (costing hauls nothing for it). Since revision 6 (R3) the plan
  credits hangar compressed ore as the raws it yields, so a via line
  dated before the plan was netted like any other purchase and costing's
  pre-plan rule applies to it as a plain date test (contract review
  A19). Plan-chosen ores keep
  their ore lines (costing re-blends them through compressed_alloc),
  bought surplus included. Ice is never converted, nor a kind whose
  yield is 0; the Settings toggles do not gate it. Per RECORD, not per
  hangar stack: a record under-converts by less than one batch (a record
  below one batch converts nothing), and an output the run does not buy
  is written too (the Buy tab counts it outside the plan; its ISK is not
  costed). An executed run's conversions are frozen once written, and
  kept apart from its lines (run_purchase_refine) so a record that stops
  counting for a while gets the same conversion back (see _RunOres).
* **Contract prices (R7, C10).** P = price − reward is spread over the
  received items by reference price p_i — the cached hub SELL quote
  (region-wide fallbacks dropped), else CCP's adjusted price; a blueprint
  copy (raw_quantity −2) is unpriced, its adjusted price being the BPO's.
  k = P / Σ p_i q_i over the priced items, each item k × p_i, an unpriced
  one 0 (flagged unpriced_items), so Σ k p_i q_i = P exactly. With no
  received item priced the contract is 'no_price' and costs nothing. The
  allocation is frozen on the CONTRACT once every received item that CAN
  be priced is (buy_contract.k / priced_at, buy_contract_item.unit_price)
  and never recomputed — a reopen that moves the contract to another run
  keeps its prices; while such an item is unpriced it is re-derived on
  every pass, so a later cache fill fixes it. A blueprint copy never can
  be, so it counts as priced at 0 for the freeze (still flagged in
  unpriced_items): otherwise a "kit" contract (BPC + minerals) would
  re-split its price by the live quote ratio on every pass, moving an
  executed run's realized cost with the market (review 2026-09-28).
* **Writes (C4, C5).** One BEGIN IMMEDIATE ... COMMIT for the whole pass;
  each run's derived lines are replaced wholesale
  (store.replace_derived_purchases) and a run that fell out of every
  window is cleared, so an ESI purchase is on at most one run. Lines for
  types the run does not buy are stored too (R3: over-buying is stock) —
  the Buy tab counts them. A run whose lines did not change is not
  rewritten, so repeated passes leave run_purchase untouched (the
  comparison includes via_type_id).

The pass reads only local rows. The ESI update route runs it after the
price step (so R7 prices come from this refresh), also when ESI is down;
web.py also runs it after every action that moves a window or changes
whose buys count — planning or discarding a run, run_complete /
run_reopen, a Count buys toggle, removing a character (contract review
C1, widened by the 2026-09-28 review) — and, since revision 4, a
settings save (the venue rule and the ore conversion read settings).
ledger.py never imports this module.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config, costing, ledger, market, store

log = logging.getLogger(__name__)

RUN_EXECUTED = "complete"
# ESI raw_quantity of a blueprint copy (-1 is a singleton).
RAW_QUANTITY_BPC = -2
# Record statuses (PurchaseRecord.status). Only 'costed' writes lines.
STATUS_COSTED = "costed"
STATUS_INTERNAL = store.BUY_EXCLUDED_INTERNAL
STATUS_SWAP = store.BUY_EXCLUDED_SWAP
STATUS_NO_PRICE = store.BUY_EXCLUDED_NO_PRICE
STATUS_BUYS_OFF = "buys_off"       # the owner's Count buys is off
STATUS_NO_ITEMS = "no_items"       # the contract's item list is not read (yet)
STATUS_PENDING = "pending"         # never priced: no pass has run since the pull
NOT_COSTED = (STATUS_INTERNAL, STATUS_SWAP, STATUS_NO_PRICE)
STATUS_LABEL = {
    STATUS_COSTED: "costed",
    STATUS_INTERNAL: "internal — not costed",
    STATUS_SWAP: "swap — not costed",
    STATUS_NO_PRICE: "no reference price — not costed",
    STATUS_BUYS_OFF: "Count buys off — not costed",
    STATUS_NO_ITEMS: "items not read yet — not costed",
    STATUS_PENDING: "not priced yet — the next ESI update prices it",
}
_KIND_ORDER = {store.ESI_KIND_TRANSACTION: 0, store.ESI_KIND_CONTRACT: 1}


# ---------------------------------------------------------------------------
# Buying windows (R2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuyWindow:
    """One run's buying cycle: purchases dated in (lo, hi] belong to it;
    hi None = still open (the newest planned run). lo can reach hi on a
    legacy completed_at inversion — an empty window, never a double
    count."""

    index_run_id: int
    run_number: int
    executed: bool
    lo: datetime
    hi: datetime | None
    # The structure market an EXECUTED run's purchases are classed against
    # (index_run.structure_market_id, persisted at plan time — review
    # 2026-09-28); None = the live setting (the newest planned run, and a
    # run planned before the column).
    structure_market_id: int | None = None

    def contains(self, when: datetime) -> bool:
        return when > self.lo and (self.hi is None or when <= self.hi)


def _ts(text) -> datetime | None:
    """Either timestamp shape (SQLite 'YYYY-MM-DD HH:MM:SS' or ESI '...Z',
    a bare date too) as an aware UTC datetime; None for NULL or text that
    does not parse (logged — the row is then simply not matched)."""
    if not text:
        return None
    try:
        return ledger._parse_ts(str(text))
    except ValueError:
        log.warning("unparseable timestamp %r — not matched to a buying window", text)
        return None


def buying_windows(conn: sqlite3.Connection) -> list[BuyWindow]:
    """Every run that collects purchases, executed runs in run_number
    order, then the newest planned run (R2 as contract review C3 reads it
    in this schema: executed = status 'complete'; superseded = derived,
    every non-executed run but the newest). With no executed run before
    it, a window opens at the run's opened_at (planned_start on runs
    planned before the column), which an in-place re-plan never moves
    (revision 6, contract review A3). An executed window carries the
    structure market its run was planned against; the open window follows
    the live setting, like everything else the newest run re-derives."""
    runs = conn.execute(
        "SELECT index_run_id, run_number, status, completed_at, "
        "COALESCE(opened_at, planned_start) AS opened, "
        "structure_market_id FROM index_run ORDER BY run_number, index_run_id"
    ).fetchall()
    if not runs:
        return []
    windows: list[BuyWindow] = []
    last_done: datetime | None = None  # greatest completed_at so far
    for run in runs:
        if run["status"] != RUN_EXECUTED:
            continue
        hi = _ts(run["completed_at"])
        lo = last_done if last_done is not None else _ts(run["opened"])
        if hi is not None and lo is not None:
            windows.append(BuyWindow(
                run["index_run_id"], run["run_number"], True, lo, hi,
                run["structure_market_id"],
            ))
        if hi is not None:
            last_done = hi if last_done is None else max(last_done, hi)
    newest = max(runs, key=lambda r: r["index_run_id"])
    if newest["status"] != RUN_EXECUTED:
        lo = last_done if last_done is not None else _ts(newest["opened"])
        if lo is not None:
            windows.append(
                BuyWindow(newest["index_run_id"], newest["run_number"], False, lo, None)
            )
    return windows


def run_window(conn: sqlite3.Connection, index_run_id: int) -> BuyWindow | None:
    """The run's buying window, or None when it collects nothing (a
    superseded plan, an executed run with no completed_at, a run with
    neither opened_at nor planned_start)."""
    return next(
        (w for w in buying_windows(conn) if w.index_run_id == index_run_id), None
    )


def collecting_run_id(conn: sqlite3.Connection) -> int | None:
    """The run new purchases land on: the newest run while it is not
    executed — what a superseded run's Buy tab points to (C4). None when
    the newest run is executed (nothing collects until the next plan)."""
    open_window = [w for w in buying_windows(conn) if w.hi is None]
    return open_window[0].index_run_id if open_window else None


def _window_of(windows: list[BuyWindow], when: datetime | None) -> BuyWindow | None:
    """The FIRST window holding the date — the windows are disjoint by
    construction; taking the first keeps a purchase on at most one run
    even on inconsistent data."""
    if when is None:
        return None
    return next((w for w in windows if w.contains(when)), None)


def _floor_iso(windows: list[BuyWindow]) -> str | None:
    """A text lower bound for the SQL prefilter: a day below the earliest
    window, in ESI's shape (exact text order among ESI dates); the exact
    test is _window_of on parsed datetimes."""
    if not windows:
        return None
    return ledger._iso(min(w.lo for w in windows) - timedelta(days=1))


# ---------------------------------------------------------------------------
# Contract price allocation (R7, C10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allocation:
    """R7 for one contract: k, each received record's unit price (0 for an
    unpriced item; all None when nothing is priced) and the unpriced
    count. excluded is 'no_price' when no received item has a reference
    price, else None."""

    k: float | None
    unit_prices: dict
    unpriced: int
    excluded: str | None
    blueprint_copies: int = 0   # received BPCs: in `unpriced`, never priceable

    @property
    def complete(self) -> bool:
        """Every received item that can be priced is: the allocation
        freezes. A blueprint copy is permanently 0 (C10.3), so it never
        holds the freeze back — no cache fill could ever price it, and
        re-deriving would only move the other items with the market."""
        return self.excluded is None and self.unpriced == self.blueprint_copies


def contract_isk(price, reward) -> float:
    """What the acceptor paid: price − reward, a NULL price counting as 0
    (contract review C9)."""
    return float(price or 0.0) - float(reward or 0.0)


def allocate_contract(isk: float, items, ref_prices: dict[int, float]) -> Allocation:
    """Spread `isk` over the RECEIVED items (is_included = 1) of a
    contract by reference price (R7). `items` are buy_contract_item rows
    (record_id, type_id, quantity, raw_quantity, is_included);
    `ref_prices` is {type_id: p} already resolved (hub sell, else
    adjusted). A blueprint copy is never priced (C10.3). The allocation is
    exact: Σ k × p_i × q_i = isk."""
    received = [i for i in items if i["is_included"]]
    ref: dict[int, float | None] = {}
    for item in received:
        p = None
        if item["raw_quantity"] != RAW_QUANTITY_BPC:
            p = ref_prices.get(item["type_id"])
            if p is not None and (not math.isfinite(p) or p <= 0):
                p = None
        ref[item["record_id"]] = p
    total = sum(
        ref[i["record_id"]] * int(i["quantity"])
        for i in received
        if ref[i["record_id"]] is not None
    )
    unpriced = sum(1 for p in ref.values() if p is None)
    copies = sum(1 for i in received if i["raw_quantity"] == RAW_QUANTITY_BPC)
    k = isk / total if total > 0 else None
    if k is None or not math.isfinite(k):
        return Allocation(None, {r: None for r in ref}, len(received), STATUS_NO_PRICE)
    units = {r: (k * p if p is not None else 0.0) for r, p in ref.items()}
    if not all(math.isfinite(u) for u in units.values()):
        return Allocation(None, {r: None for r in ref}, len(received), STATUS_NO_PRICE)
    return Allocation(k, units, unpriced, None, copies)


def reference_prices(conn: sqlite3.Connection, settings, type_ids) -> dict[int, float]:
    """R7's p_i per type from the cache at matching time: the hub SELL
    quote in the price region (market_price source 'sell', a region-wide
    fallback row dropped — C10.2), else CCP's adjusted price. Types with
    neither are absent."""
    ids = sorted({int(t) for t in type_ids})
    if not ids:
        return {}
    out = {
        t: p
        for t, p in market.cached_adjusted_prices(conn, ids).items()
        if p is not None and p > 0
    }
    for t, (p, region_wide) in market.cached_hub_quotes(
        conn, settings.price_region_id, ids, "sell"
    ).items():
        if not region_wide and p is not None and p > 0:
            out[t] = p
    return out


def _exclusion(contract, items, internal: set[int]) -> str | None:
    """Why a bought contract is not a purchase, before any pricing:
    'internal' (issued by us — the issuer in ledger.internal_ids, or
    issued FOR one of our corporations: for_corporation set and the
    issuer's corporation ours, C9 as the 2026-09-28 review narrowed it),
    'swap' (we also gave items, or price − reward <= 0), else None.

    issuer_corporation_id alone proves nothing: ESI fills it on every
    contract, so a corpmate outside the pool selling to us personally
    carries our corporation's id there and is a real purchase."""
    issuer_corp = contract["issuer_corporation_id"]
    if contract["issuer_id"] in internal or (
        contract["for_corporation"]
        and issuer_corp is not None
        and issuer_corp in internal
    ):
        return STATUS_INTERNAL
    if any(not i["is_included"] for i in items):
        return STATUS_SWAP
    if contract_isk(contract["price"], contract["reward"]) <= 0:
        return STATUS_SWAP
    return None


# ---------------------------------------------------------------------------
# Records: what each window holds, with its status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractItem:
    record_id: int
    type_id: int
    quantity: int
    unit_price: float | None   # the allocation (k × p_i, 0 unpriced); None until priced
    received: bool             # False: an item WE gave (a swap)
    blueprint_copy: bool = False


@dataclass(frozen=True)
class PurchaseRecord:
    """One wallet buy or bought contract inside a run's window, as the
    Buy tab's Purchases section lists it. `isk` is what was paid:
    quantity × unit price for a buy, price − reward for a contract.
    Only status 'costed' records write run_purchase lines."""

    kind: str                  # store.ESI_KIND_TRANSACTION | ESI_KIND_CONTRACT
    esi_id: int
    date: str                  # ESI text; a contract's completed, else accepted date
    owner_kind: str
    owner_id: int
    venue: str                 # purchase_venue: BUY_VENUE_HUB | _STRUCTURE | _OTHER
    location_id: int | None
    status: str
    isk: float
    type_id: int | None = None       # a buy's item
    quantity: int | None = None
    unit_price: float | None = None
    title: str | None = None         # a contract's
    k: float | None = None
    unpriced_items: int = 0
    items: tuple = ()                # ContractItem, record_id order

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)

    @property
    def costed(self) -> bool:
        return self.status == STATUS_COSTED

    def received_units(self, type_id: int) -> int:
        """Units of one type this record brought in: a buy's quantity, or
        the sum over a contract's RECEIVED items of that type (the Buy
        tab's Purchases section sets a refined ore's remainder against
        it — RefinedOre)."""
        if self.kind == store.ESI_KIND_TRANSACTION:
            return int(self.quantity or 0) if self.type_id == type_id else 0
        return sum(i.quantity for i in self.items if i.received and i.type_id == type_id)

    def lines(self) -> list[dict]:
        """The record's run_purchase lines as bought
        (store.replace_derived_purchases shape): one for a buy, one per
        received item of a contract. A corrupt ESI row (no units, a
        negative or non-finite price) writes no line rather than failing
        the whole pass in the store's validation.

        These are the lines BEFORE revision 4's ore conversion: the pass
        hands them to _RunOres.lines, which swaps an unplanned compressed
        ore's converted batches for the raws it yields."""
        if not self.costed:
            return []
        common = {
            "venue": self.venue,
            "esi_kind": self.kind,
            "esi_id": self.esi_id,
            "date": self.date,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
        }
        if self.kind == store.ESI_KIND_TRANSACTION:
            parts = [(self.type_id, self.quantity, self.unit_price, None)]
        else:
            parts = [
                (i.type_id, i.quantity, i.unit_price, self.k)
                for i in self.items
                if i.received
            ]
        return [
            dict(common, type_id=t, quantity=int(q), unit_price=float(u), contract_k=k)
            for t, q, u, k in parts
            if q is not None and int(q) > 0 and _sane_price(u)
        ]


def _sane_price(value) -> bool:
    return value is not None and math.isfinite(float(value)) and float(value) >= 0


def purchase_venue(location_id, settings,
                   structure_market_id: int | None = None) -> str:
    """Which inbound freight leg a purchase hauls on, by where it was
    bought (user ruling 2026-09-28; contract review A4):

    * the hub station → 'hub' (the Jita rate). The station is the price
      region's (config.PRICE_STATION_FILTERS), Jita 4-4 when the region
      has none configured — the hub leg is the "from Jita" courier, so
      Jita stays its station whatever region prices the plan;
    * the configured structure market (settings.structure_market(), the
      C-J6 Keepstar by default) → 'structure' (the structure rate);
    * anything else → 'other' (the Settings default inbound rate): another
      NPC station, another structure — a corp Sotiyo in C-J6MT included,
      "C-J" being read as the structure market itself, since a location's
      system is resolved only best-effort — a contract whose start
      location is elsewhere, and no location at all.

    Replaces R6's reading through costing.sale_venue (every NPC station →
    hub, every structure → structure market, none → hub), which stays the
    rule for SALES.

    `structure_market_id` is the structure market an EXECUTED run was
    planned against (BuyWindow.structure_market_id; None = the live
    setting). Review 2026-09-28: without it, pointing Settings at another
    structure reclassed every executed run's buys at the old one to
    'other' on the next pass — hauled at the default rate instead of the
    run's persisted structure rate, out of its Null Sec Market Share, and
    one purchase costed under two rules (its frozen via lines kept the
    old freight, its remainder took the new). The hub station needs no
    vintage: every price region's hub is Jita 4-4 (config.PRICE_STATION_
    FILTERS names only The Forge's)."""
    if location_id is None:
        return store.BUY_VENUE_OTHER
    try:
        lid = int(location_id)
    except (TypeError, ValueError):
        return store.BUY_VENUE_OTHER
    hub = config.PRICE_STATION_FILTERS.get(
        settings.price_region_id, config.JITA_44_STATION_ID
    )
    if lid == hub:
        return store.BUY_VENUE_HUB
    structure = (
        structure_market_id if structure_market_id is not None
        else settings.structure_market()
    )
    if lid == int(structure):
        return store.BUY_VENUE_STRUCTURE
    return store.BUY_VENUE_OTHER


def _contract_date(row) -> str | None:
    return row["date_completed"] or row["date_accepted"]


def _contract_items(conn, contract_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM buy_contract_item WHERE contract_id = ? ORDER BY record_id",
        (contract_id,),
    ).fetchall()


def _windowed_contracts(conn, windows):
    """(window, row) for every finished bought item exchange dated inside
    a window."""
    floor = _floor_iso(windows)
    if floor is None:
        return []
    statuses = ledger.SOLD_CONTRACT_STATUSES
    rows = conn.execute(
        f"SELECT * FROM buy_contract WHERE type = 'item_exchange' "
        f"AND status IN ({','.join('?' * len(statuses))}) "
        f"AND COALESCE(date_completed, date_accepted) >= ? "
        f"ORDER BY COALESCE(date_completed, date_accepted), contract_id",
        (*statuses, floor),
    ).fetchall()
    out = []
    for row in rows:
        window = _window_of(windows, _ts(_contract_date(row)))
        if window is not None:
            out.append((window, row))
    return out


def _windowed_transactions(conn, windows):
    floor = _floor_iso(windows)
    if floor is None:
        return []
    out = []
    for row in conn.execute(
        "SELECT * FROM buy_transaction WHERE date >= ? ORDER BY date, transaction_id",
        (floor,),
    ):
        window = _window_of(windows, _ts(row["date"]))
        if window is not None:
            out.append((window, row))
    return out


def _records(conn, windows, enabled, internal, settings) -> dict[int, list[PurchaseRecord]]:
    """Every purchase inside a window, keyed by run, with the status the
    STORED contract state gives it (assign_purchases prices first, then
    reads through here, so the lines and the listing agree). `settings`
    resolves each record's venue (purchase_venue: the price region's hub
    station and the structure market — an executed window's own, when its
    run persisted one)."""
    out: dict[int, list[PurchaseRecord]] = {w.index_run_id: [] for w in windows}
    for window, t in _windowed_transactions(conn, windows):
        owner = (t["owner_kind"], t["owner_id"])
        if owner not in enabled:
            status = STATUS_BUYS_OFF
        elif t["client_id"] is not None and t["client_id"] in internal:
            status = STATUS_INTERNAL
        else:
            status = STATUS_COSTED
        out[window.index_run_id].append(PurchaseRecord(
            kind=store.ESI_KIND_TRANSACTION, esi_id=t["transaction_id"], date=t["date"],
            owner_kind=t["owner_kind"], owner_id=t["owner_id"],
            venue=purchase_venue(t["location_id"], settings, window.structure_market_id),
            location_id=t["location_id"],
            status=status,
            isk=t["quantity"] * t["unit_price"], type_id=t["type_id"],
            quantity=t["quantity"], unit_price=t["unit_price"],
        ))
    for window, c in _windowed_contracts(conn, windows):
        items = _contract_items(conn, c["contract_id"])
        owner = (c["owner_kind"], c["owner_id"])
        if c["items_status"] != "ok":
            status = STATUS_NO_ITEMS
        elif owner not in enabled:
            status = STATUS_BUYS_OFF
        elif c["excluded"] is not None:
            status = c["excluded"]
        elif c["k"] is None:
            status = STATUS_PENDING
        else:
            status = STATUS_COSTED
        out[window.index_run_id].append(PurchaseRecord(
            kind=store.ESI_KIND_CONTRACT, esi_id=c["contract_id"], date=_contract_date(c),
            owner_kind=c["owner_kind"], owner_id=c["owner_id"],
            venue=purchase_venue(
                c["start_location_id"], settings, window.structure_market_id
            ),
            location_id=c["start_location_id"],
            status=status, isk=contract_isk(c["price"], c["reward"]), title=c["title"],
            k=c["k"], unpriced_items=c["unpriced_items"] if status == STATUS_COSTED else 0,
            items=tuple(
                ContractItem(
                    record_id=i["record_id"], type_id=i["type_id"],
                    quantity=int(i["quantity"]), unit_price=i["unit_price"],
                    received=bool(i["is_included"]),
                    blueprint_copy=i["raw_quantity"] == RAW_QUANTITY_BPC,
                )
                for i in items
            ),
        ))
    for records in out.values():
        records.sort(key=lambda r: (_ts(r.date) or ledger._DAWN, _KIND_ORDER[r.kind], r.esi_id))
    return out


def run_purchase_records(conn: sqlite3.Connection, index_run_id: int,
                         settings=None) -> list[PurchaseRecord]:
    """The Buy tab's Purchases section: every wallet buy and bought
    contract dated inside this run's window, oldest first, costed or not
    (internal transfers, swaps, unpriced contracts, owners with Count buys
    off are listed with their reason). Read-only; [] for a run that
    collects nothing. `settings` (the venue rule's) defaults to the
    stored ones. A refined ore's mineral lines are the run's, not the
    record's: refined_ores reads them."""
    window = run_window(conn, index_run_id)
    if window is None:
        return []
    settings = settings if settings is not None else store.get_settings(conn)
    return _records(
        conn, [window], store.buys_enabled_owners(conn), ledger.internal_ids(conn),
        settings,
    )[index_run_id]


# ---------------------------------------------------------------------------
# Unplanned compressed ore -> the raws it yields (revision 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefinedOre:
    """One ore of one purchase record as the matcher refined it (revision
    4, user ruling 2026-09-28), summed from the run's stored via lines —
    what the Buy tab's Purchases annotation ("→ refined into N Tritanium,
    M Pyerite (landed X ISK incl. freight and refining tax)") and the
    Bought cell's `via <ore>` title read.

    `landed_isk` is Σ quantity × unit_price over the via lines: the ore's
    price for the converted units, its venue freight and, for ore, the
    refining tax together. The tax is not stored apart (contract review
    A13) and is not recomputed: a recomputed figure would drift from an
    executed run's frozen lines. `remainder` is the ore units left as an
    ore line (short of one batch: outside the plan); the converted units
    are PurchaseRecord.received_units(ore_type_id) − remainder."""

    ore_type_id: int
    outputs: tuple[tuple[int, int], ...]   # (raw type_id, units), ascending type id
    landed_isk: float
    remainder: int = 0


def _via_groups(conn, index_run_id: int) -> dict[tuple, tuple]:
    """A run's stored via lines grouped by (esi_kind, esi_id, via_type_id)
    → (contract_k of the group's first line, ((type_id, quantity,
    unit_price), ...) in purchase_id order — the order they were written
    in, ascending raw type id)."""
    groups: dict[tuple, list] = {}
    scale: dict[tuple, float | None] = {}
    for r in conn.execute(
        "SELECT esi_kind, esi_id, via_type_id, type_id, quantity, unit_price, "
        "contract_k FROM run_purchase WHERE index_run_id = ? "
        "AND esi_kind IS NOT NULL AND via_type_id IS NOT NULL ORDER BY purchase_id",
        (index_run_id,),
    ):
        key = (r["esi_kind"], int(r["esi_id"]), int(r["via_type_id"]))
        groups.setdefault(key, []).append(
            (int(r["type_id"]), int(r["quantity"]), float(r["unit_price"]))
        )
        scale.setdefault(key, r["contract_k"])
    return {key: (scale[key], tuple(lines)) for key, lines in groups.items()}


def refined_ores(conn: sqlite3.Connection,
                 index_run_id: int) -> dict[tuple[str, int], tuple[RefinedOre, ...]]:
    """{(esi_kind, esi_id): (RefinedOre, ...)} for every purchase record
    whose unplanned compressed ore this run's derived lines refined, in
    the order the lines were written. Read-only; a record with no via
    lines (nothing converted, or a plan-chosen ore — today's wording) is
    absent. Hand-entered lines (esi_kind NULL) are never read."""
    groups = _via_groups(conn, index_run_id)
    if not groups:
        return {}
    rest = {
        (r["esi_kind"], int(r["esi_id"]), int(r["type_id"])): int(r["units"])
        for r in conn.execute(
            "SELECT esi_kind, esi_id, type_id, SUM(quantity) AS units "
            "FROM run_purchase WHERE index_run_id = ? AND esi_kind IS NOT NULL "
            "AND via_type_id IS NULL GROUP BY esi_kind, esi_id, type_id",
            (index_run_id,),
        )
    }
    out: dict[tuple[str, int], list[RefinedOre]] = {}
    for (kind, esi_id, ore), (_k, lines) in groups.items():
        out.setdefault((kind, esi_id), []).append(RefinedOre(
            ore_type_id=ore,
            outputs=tuple((t, q) for t, q, _u in lines),
            landed_isk=sum(q * u for _t, q, u in lines),
            remainder=rest.get((kind, esi_id, ore), 0),
        ))
    return {key: tuple(ores) for key, ores in out.items()}


def _one_price(parts) -> float:
    """The unit price of one type's lines within a record: a contract's
    items of one type all carry k × p_i, so that price exactly (never a
    q × u / q round trip, which can move it by an ulp and make an
    unchanged pass look changed); a weighted mean only if they differ."""
    prices = {u for _q, u in parts}
    if len(prices) == 1:
        return prices.pop()
    units = sum(q for q, _u in parts)
    return sum(q * u for q, u in parts) / units


class _OrePass:
    """The pass-wide half of the ore conversion: which compressed types
    convert and at what yield — SDE and settings only, so the same for
    every run of a pass."""

    def __init__(self, conn, ref, settings):
        self.conn = conn
        self.ref = ref
        self.settings = settings
        self.sources = ref.compressed_sources()
        self.yields = {
            "ore": float(settings.compressed_ore_yield),
            "gas": float(settings.compressed_gas_yield),
        }
        # engine.tax_of: refining ore pays the facility's tax, gas none.
        self.tax = max(0.0, float(settings.compressed_reprocess_tax))
        self._convertible: dict[int, bool] = {}

    def convertible(self, type_id: int) -> bool:
        """Does this compressed type turn into raw lines (contract review
        A5)? Only when its kind's yield is above 0 (the engine's
        `yields.get(s.kind, 0.0) > 0`) and at least one output is in a
        raw group compressed sourcing covers (config.COMPRESSED_SOURCE_
        GROUPS) — which excludes exactly the compressed ICE types
        ref.compressed_sources() also returns (kind 'ore', outputs only
        Ice Products): ice is out of scope and is not refined at the ore
        yield. The Settings group toggles do NOT gate it: the ruling says
        any bought ore."""
        if type_id not in self._convertible:
            source = self.sources.get(type_id)
            self._convertible[type_id] = (
                source is not None
                and self.yields.get(source.kind, 0.0) > 0
                and any(
                    self._group(m) in config.COMPRESSED_SOURCE_GROUPS
                    for m, _base in source.outputs
                )
            )
        return self._convertible[type_id]

    def _group(self, type_id: int) -> int | None:
        try:
            return self.ref.type_info(type_id).group_id
        except KeyError:
            return None

    def m3(self, type_id: int) -> float:
        """Packaged m³ per unit as hauled (TypeInfo.freight_volume)."""
        try:
            return float(self.ref.type_info(type_id).freight_volume)
        except KeyError:
            return 0.0


class _RunOres:
    """One run's half of the ore conversion: which ores the plan chose,
    the run's freight rates, the raws' landed plan prices and — on an
    EXECUTED run — the conversions already written.

    **Freeze (contract review A9).** ESI rows are immutable history (a
    transaction can only be re-owned; a contract's prices freeze with
    priced_at), so "recompute when the ESI row changed" reduces to: on an
    executed run, a record's ore whose via lines are already stored is
    re-emitted verbatim (type, units, landed unit price) — for a contract
    only while the stored contract_k equals the record's current k, so an
    allocation still re-deriving (priced_at NULL) re-derives its raws
    too. Date, owner and contract_k come from the current record, and
    only the remainder is recomputed (portion size and units are fixed).
    A later settings change (yields, tax, rates) or cache refresh
    therefore never reprices an executed run. With no stored group — a
    record new to the run, lines written before revision 4, a reopen
    that moved the record — it converts fresh at the current settings.
    The newest planned run always recomputes.

    The groups are read from the run's via lines AND from
    run_purchase_refine, where _assign keeps every executed run's
    conversions apart from its lines (review 2026-09-28): a record the
    pass stops costing for a while (Count buys off, a character removed
    and re-added) has its lines dropped by replace_derived_purchases, and
    reading the lines alone then re-converted it at the current settings
    once it counted again. The table's groups outlive that — kept while
    the record is in the window, costed or not, dropped once it leaves —
    and the lines win where both exist (they are what costing reads). `emitted` collects
    what this pass wrote, for _assign to store."""

    def __init__(self, ores: _OrePass, window: BuyWindow):
        self.ores = ores
        self.run_id = window.index_run_id
        self.stored: dict = (
            store.refine_freeze(ores.conn, self.run_id) if window.executed else {}
        )
        self.frozen = (
            {**self.stored, **_via_groups(ores.conn, self.run_id)}
            if window.executed else {}
        )
        self.emitted: dict = {}     # (esi_kind, esi_id, ore) -> (contract_k, lines)
        self.refined = 0            # (record, ore) pairs converted this pass
        self._rows: dict[int, sqlite3.Row] | None = None
        self._rates: dict | None = None
        self._prices: dict[int, float] = {}

    def rows(self) -> dict[int, sqlite3.Row]:
        """The run's index_run_item rows by type (read once, on the first
        record that holds a compressed type)."""
        if self._rows is None:
            self._rows = {
                int(r["type_id"]): r
                for r in self.ores.conn.execute(
                    "SELECT * FROM index_run_item WHERE index_run_id = ?",
                    (self.run_id,),
                )
            }
        return self._rows

    def plan_chosen(self, type_id: int) -> bool:
        """Did the PLAN buy this ore to cover raws (contract review A8)?
        An index_run_item row with compressed_outputs set — the Buy tab's
        own test (web._buy_context's `if raw:`). Such an ore keeps its ore
        lines: costing.ore_realized_landed re-blends it into the covered
        raws through compressed_alloc, and it shows in the Buy tab's ore
        sub-table Purchased. Since v1.29 revision 6 (R4, contract review
        A15) the page subtracts nothing for it: once it lands, the next
        re-plan nets it as hangar compressed stock (ruling R3), which is
        what lowers the raws' Remaining. Units bought past the
        plan's ore quantity are not converted either — the existing
        per-unit cap on that path applies (revision 4 §2.4)."""
        row = self.rows().get(type_id)
        return row is not None and bool(row["compressed_outputs"])

    def rate(self, venue: str) -> float:
        """ISK/m³ inbound for a venue at the rate the run was PLANNED at
        (costing._run_freight_rates), the live setting where the run
        predates the column — web._run_buy_rates' resolution. A delivered
        price is landed already."""
        if venue == store.BUY_VENUE_DELIVERED:
            return 0.0
        if self._rates is None:
            self._rates = costing._run_freight_rates(self.ores.conn, self.run_id)
        rate = self._rates.get(venue)
        if rate is None:
            rate = self.ores.settings.freight_in_rate(venue)
        return float(rate)

    def landed_prices(self, type_ids) -> dict[int, float]:
        """Each raw's LANDED plan price p_m on this run (contract review
        A7): (1) the run's row → costing.ladder_landed_unit at the run's
        rates — the covered raw's effective_unit_cost, else the plan blend
        landed; (2) no row, or it priced nothing → reference_prices (the
        hub SELL quote, region-wide rows dropped, else CCP's adjusted
        price) plus the hub leg's freight; (3) else 0, unpriced for the
        split. Every p_m is landed: at 1,000 ISK/m³ a mineral's freight
        can exceed its price, so mixing a landed plan price with an
        unlanded fallback would skew the weights between an output the
        run holds and one it does not."""
        missing = [t for t in type_ids if t not in self._prices]
        if missing:
            hub_rate = self.rate(store.BUY_VENUE_HUB)
            structure_rate = self.rate(store.BUY_VENUE_STRUCTURE)
            rows = self.rows()
            fallback = []
            for t in missing:
                price = None
                if t in rows:
                    price = costing.ladder_landed_unit(
                        rows[t], hub_rate=hub_rate, structure_rate=structure_rate,
                        m3=self.ores.m3(t),
                    )
                if _sane_price(price):
                    self._prices[t] = float(price)
                else:
                    fallback.append(t)
            if fallback:
                quoted = reference_prices(self.ores.conn, self.ores.settings, fallback)
                for t in fallback:
                    self._prices[t] = (
                        quoted[t] + hub_rate * self.ores.m3(t) if t in quoted else 0.0
                    )
        return {t: self._prices[t] for t in type_ids}

    def lines(self, raw: list[dict]) -> list[dict]:
        """One costed record's lines with every unplanned compressed ore
        swapped for what it refines into. A contract's received items of
        one ore type are pooled first (they share k × p_i). Emission order
        (for the no-change check): at the ore's first line, its raw lines
        by ascending type id, then the remainder ore line; every other
        line where it was. An ore that converts to nothing keeps its lines
        exactly as bought."""
        pooled: dict[int, list[tuple[int, float]]] = {}
        for line in raw:
            t = int(line["type_id"])
            if t in self.ores.sources and not self.plan_chosen(t):
                pooled.setdefault(t, []).append(
                    (int(line["quantity"]), float(line["unit_price"]))
                )
        if not pooled:
            return raw
        converted: dict[int, list[dict] | None] = {}
        for line in raw:
            t = int(line["type_id"])
            if t in pooled and t not in converted:
                parts = pooled[t]
                converted[t] = self._convert(
                    line, t, sum(q for q, _u in parts), _one_price(parts)
                )
        out: list[dict] = []
        emitted: set[int] = set()
        for line in raw:
            t = int(line["type_id"])
            new = converted.get(t)
            if new is None:
                out.append(line)
            elif t not in emitted:
                emitted.add(t)
                out.extend(new)
        return out

    def _convert(self, template: dict, ore_id: int, units: int,
                 unit_price: float) -> list[dict] | None:
        """The lines replacing `units` of ore `ore_id` bought at
        `unit_price` (the record's line `template` gives venue, ESI ids,
        date, owner, contract_k): the frozen or freshly refined raw lines,
        then the remainder short of a batch; None when nothing converts."""
        kind, esi_id = template["esi_kind"], int(template["esi_id"])
        portion = self.ores.sources[ore_id].portion_size
        batches = units // portion
        frozen = self.frozen.get((kind, esi_id, ore_id))
        if frozen is not None and (
            kind == store.ESI_KIND_TRANSACTION or frozen[0] == template.get("contract_k")
        ):
            refined = list(frozen[1])
        else:
            refined = self._refine(ore_id, batches, unit_price, template["venue"])
        if not refined:
            return None
        self.refined += 1
        self.emitted[(kind, esi_id, ore_id)] = (
            template.get("contract_k"),
            tuple((int(t), int(q), float(u)) for t, q, u in refined),
        )
        common = {
            key: template.get(key)
            for key in ("esi_kind", "esi_id", "date", "owner_kind", "owner_id",
                        "contract_k")
        }
        out = [
            dict(common, type_id=t, venue=store.BUY_VENUE_DELIVERED, quantity=q,
                 unit_price=u, via_type_id=ore_id)
            for t, q, u in refined
        ]
        remainder = units - batches * portion
        if remainder > 0:
            out.append(dict(template, quantity=remainder, unit_price=unit_price))
        return out

    def _refine(self, ore_id: int, batches: int, unit_price: float,
                venue: str) -> list[tuple[int, int, float]]:
        """[(raw type_id, units, LANDED unit price)] for `batches` whole
        batches of the ore (contract review A7's arithmetic, hand-worked
        in tests/test_buying.py):

            C       = batches × portion_size                  units converted
            out_m   = source.batch_output(batches, m, yield)  (> 0 only)
            ISK     = unit_price × C
                    + rate(venue) × m³(ore) × C
                    + max(0, tax) × Σ out_m × p_m             ore only; gas 0
            alloc_m = ISK × out_m × p_m / Σ out × p           by units if Σ = 0

        each line's unit price being alloc_m / out_m. ISK counts the
        converted units only: the remainder line carries its own. [] when
        the ore does not convert (ice, a zero yield, under one batch,
        nothing whole out) or the result is not a sane price (logged — a
        bad row must never fail the whole pass in the store's
        validation)."""
        if batches <= 0 or not self.ores.convertible(ore_id):
            return []
        source = self.ores.sources[ore_id]
        yield_ = self.ores.yields[source.kind]
        outs = [
            (m, source.batch_output(batches, m, yield_))
            for m in sorted({m for m, _base in source.outputs})
        ]
        outs = [(m, q) for m, q in outs if q > 0]
        if not outs:
            return []
        prices = self.landed_prices([m for m, _q in outs])
        value = {m: q * prices[m] for m, q in outs}
        total = sum(value.values())
        tax = self.ores.tax * total if source.kind == "ore" else 0.0
        converted = batches * source.portion_size
        isk = (
            unit_price * converted
            + self.rate(venue) * self.ores.m3(ore_id) * converted
            + tax
        )
        if total > 0:
            refined = [(m, q, isk * value[m] / total / q) for m, q in outs]
        else:
            units = sum(q for _m, q in outs)
            refined = [(m, q, isk / units) for m, q in outs]
        if not all(_sane_price(u) for _m, _q, u in refined):
            log.warning(
                "ore %s on run %s refines to an unusable price — kept as ore",
                ore_id, self.run_id,
            )
            return []
        return refined


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


@dataclass
class AssignSummary:
    runs: int = 0              # runs with a buying window
    runs_with_lines: int = 0   # ... that now hold at least one derived line
    lines: int = 0             # derived lines now on those runs
    transactions: int = 0      # wallet buys costed
    contracts: int = 0         # bought contracts costed
    not_costed: int = 0        # listed, not costed: internal / swap / no price
    unpriced_items: int = 0    # received items of costed contracts priced at 0
    changed_runs: int = 0      # runs whose derived lines were rewritten
    cleared_runs: int = 0      # runs outside every window whose lines were removed (C4)
    run_numbers: list = field(default_factory=list)  # runs holding lines
    refined: int = 0           # (record, unplanned ore) pairs written as raw lines (rev. 4)


def _price_contracts(conn, settings, windows, enabled, internal, now_iso) -> None:
    """Derive each in-window contract's exclusion and, unless frozen, its
    R7 allocation, and store them on buy_contract / buy_contract_item.
    Owners with Count buys off and contracts whose items are not read yet
    are left alone. 'internal' is re-derived even on a frozen contract
    (the pool can grow); a frozen allocation itself never moves."""
    todo = []
    wanted_types: set[int] = set()
    for _window, c in _windowed_contracts(conn, windows):
        if c["items_status"] != "ok" or (c["owner_kind"], c["owner_id"]) not in enabled:
            continue
        items = _contract_items(conn, c["contract_id"])
        excluded = _exclusion(c, items, internal)
        if c["priced_at"] is not None:
            conn.execute(
                "UPDATE buy_contract SET excluded = ? WHERE contract_id = ?",
                (excluded, c["contract_id"]),
            )
            continue
        if excluded is not None:
            conn.execute(
                "UPDATE buy_contract SET excluded = ?, k = NULL, unpriced_items = 0 "
                "WHERE contract_id = ?",
                (excluded, c["contract_id"]),
            )
            conn.execute(
                "UPDATE buy_contract_item SET unit_price = NULL WHERE contract_id = ?",
                (c["contract_id"],),
            )
            continue
        todo.append((c, items))
        wanted_types |= {
            i["type_id"] for i in items
            if i["is_included"] and i["raw_quantity"] != RAW_QUANTITY_BPC
        }
    prices = reference_prices(conn, settings, wanted_types)
    for c, items in todo:
        alloc = allocate_contract(contract_isk(c["price"], c["reward"]), items, prices)
        conn.execute(
            "UPDATE buy_contract SET k = ?, unpriced_items = ?, excluded = ?, "
            "priced_at = ? WHERE contract_id = ?",
            (alloc.k, alloc.unpriced, alloc.excluded,
             now_iso if alloc.complete else None, c["contract_id"]),
        )
        conn.executemany(
            "UPDATE buy_contract_item SET unit_price = ? "
            "WHERE contract_id = ? AND record_id = ?",
            [(u, c["contract_id"], r) for r, u in alloc.unit_prices.items()],
        )


def _line_key(line: dict) -> tuple:
    """A derived line as run_purchase stores it (store._validate_derived_line's
    coercions and column order), for the no-change check. via_type_id is
    LAST, as in that tuple (revision 4, contract review A9): a change only
    in which ore a raw line came from must count as a change."""
    k = line.get("contract_k")
    via = line.get("via_type_id")
    return (
        int(line["type_id"]), line["venue"], int(line["quantity"]),
        float(line["unit_price"]), None, line["esi_kind"], int(line["esi_id"]),
        None if k is None else float(k), line.get("date"), line.get("owner_kind"),
        None if line.get("owner_id") is None else int(line["owner_id"]),
        None if via is None else int(via),
    )


def _stored_lines(conn, index_run_id: int) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT type_id, venue, quantity, unit_price, note, esi_kind, esi_id, "
            "contract_k, date, owner_kind, owner_id, via_type_id FROM run_purchase "
            "WHERE index_run_id = ? AND esi_kind IS NOT NULL ORDER BY purchase_id",
            (index_run_id,),
        )
    ]


def _assign(conn, ref, settings, now_iso: str) -> AssignSummary:
    summary = AssignSummary()
    windows = buying_windows(conn)
    enabled = store.buys_enabled_owners(conn)
    internal = ledger.internal_ids(conn)
    _price_contracts(conn, settings, windows, enabled, internal, now_iso)
    by_run = _records(conn, windows, enabled, internal, settings)
    summary.runs = len(windows)
    # Revision 4: with no reference data there is no conversion at all
    # (contract review A6) — every line is written as bought.
    ores = _OrePass(conn, ref, settings) if ref is not None else None
    for window in windows:
        records = by_run[window.index_run_id]
        # Built (and a frozen run's via lines read) BEFORE this run's
        # lines are replaced below.
        run_ores = _RunOres(ores, window) if ores is not None else None
        lines = []
        for r in records:
            raw = r.lines()
            lines.extend(run_ores.lines(raw) if run_ores is not None and raw else raw)
        if run_ores is not None:
            summary.refined += run_ores.refined
        for r in records:
            if r.costed and r.kind == store.ESI_KIND_TRANSACTION:
                summary.transactions += 1
            elif r.costed:
                summary.contracts += 1
                summary.unpriced_items += r.unpriced_items
            elif r.status in NOT_COSTED:
                summary.not_costed += 1
        if lines:
            summary.runs_with_lines += 1
            summary.run_numbers.append(window.run_number)
        summary.lines += len(lines)
        if [_line_key(line) for line in lines] != _stored_lines(conn, window.index_run_id):
            store.replace_derived_purchases(conn, window.index_run_id, lines)
            summary.changed_runs += 1
        if run_ores is not None and window.executed:
            # Keep this pass's conversions apart from the lines (review
            # 2026-09-28): only groups that changed are written, and a
            # group whose record is not costed this pass is left alone.
            changed = {
                key: group for key, group in run_ores.emitted.items()
                if run_ores.stored.get(key) != group
            }
            if changed:
                store.save_refine_freeze(conn, window.index_run_id, changed)
            # A group is kept while its record is IN the window, costed or
            # not; one whose record left the window (a moved bound) goes.
            present = {(r.kind, r.esi_id) for r in records}
            gone = {(k, i) for k, i, _ore in run_ores.stored} - present
            if gone:
                store.drop_refine_groups(conn, window.index_run_id, gone)
    # Only an executed run keeps frozen conversions: a reopened or
    # superseded run re-derives when it next collects.
    store.clear_refine_freeze(conn, (w.index_run_id for w in windows if w.executed))
    in_window = {w.index_run_id for w in windows}
    for index_run_id in sorted(store.runs_with_derived_purchases(conn) - in_window):
        store.replace_derived_purchases(conn, index_run_id, [])
        summary.cleared_runs += 1
    return summary


def assign_purchases(conn: sqlite3.Connection, ref=None, settings=None,
                     now: datetime | None = None) -> AssignSummary:
    """Match the stored ESI purchases to the runs' buying windows and
    rewrite each run's derived run_purchase lines (module docstring).

    Idempotent and cheap: it reads only local rows, and a pass over
    unchanged data writes nothing but the (identical) contract pricing of
    contracts still unpriced. One BEGIN IMMEDIATE ... COMMIT (C5): on any
    error everything rolls back and the exception propagates — the
    caller's guard logs it. `ref` (revision 4) supplies the compressed
    sources, portion sizes, m³ and raw groups the unplanned-ore conversion
    needs; with `ref` None nothing is converted and every line is written
    as bought (contract review A6 — the venue rule still applies, it needs
    only settings). `settings` defaults to the stored ones: price_region_id
    picks R7's hub quotes and the hub station, the structure market the
    structure venue, and the conversion reads the yields, the refining tax
    and — for a run planned before a freight column — the live rates.
    `now` stamps buy_contract.priced_at."""
    settings = settings if settings is not None else store.get_settings(conn)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ledger._begin(conn)
    try:
        summary = _assign(conn, ref, settings, ledger._iso(now))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return summary


def summary_line(summary: AssignSummary) -> str:
    """The pass's clause of the ESI refresh flash."""
    if not summary.transactions and not summary.contracts:
        line = "purchases: none in the current buying windows"
    else:
        runs = ", ".join(str(n) for n in summary.run_numbers)
        line = (
            f"purchases: {summary.transactions} buys, {summary.contracts} contracts "
            f"matched (run{'s' if len(summary.run_numbers) != 1 else ''} {runs})"
        )
    extras = []
    if summary.unpriced_items:
        extras.append(f"{summary.unpriced_items} contract items with no reference price")
    if summary.not_costed:
        extras.append(f"{summary.not_costed} not costed — internal, swap or no price")
    if extras:
        line += " (" + "; ".join(extras) + ")"
    return line
