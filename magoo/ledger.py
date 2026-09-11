"""Ledger (v1.27.0): what the pipeline finals actually sold for.

Pull side — a second, independently guarded step of the dashboard's ESI
refresh (web.esi_refresh): for every character and corporation in the pool
with Count sales on, the owner's SELL orders (open + 90-day history), wallet
SALE transactions (corporations: one wallet division at a time) and ISSUED
contracts (with their items) are read and persisted under their global ESI
ids. Every type is stored, sell side only; nothing is ever deleted. Each
owner × family runs in two phases — every ESI call first, then one short
write transaction — so the write lock never spans a network call, and a
failure in one family leaves the others (and the asset snapshot, committed
before this step) untouched.

Read side — the Ledger tab: wallet transactions are the units-sold truth
(order history only explains outcomes and is never summed), clean
item-exchange contracts attribute their whole price to the finals they
contain, mixed or swap contracts count units only. Fees are estimated with costing.net_proceeds_at_venue at the realized
price — the fee pair and movement term follow the sale's ESI location
(NPC station: hub rates + freight-out per m³; structure: the structure-
market rates + the structure leg per m³; capital-class hulls take the
flat movement cost at either venue; a contract without a location keeps
the class rule of net_proceeds_per_hull); each sale's cost basis is
costing.hull_cost on the latest PRICED run executed on or before it
(v1.27.1, ledger.CostVintages), while the unsold listings and the
products table's cost-per-unit column read the latest executed run.
PROJECT.md §2 feature 18 and §6 "Sales ledger" carry the rules.

Direct SQL on the state tables (the engine.py precedent; the DDL lives in
store.py). No Flask, no SDE file shapes.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from . import charts, config, costing, esi, market, store

log = logging.getLogger(__name__)

WINDOWS = (7, 30, 90)
DEFAULT_WINDOW = 30
LEDGER_ROWS_SHOWN = 300
FAMILIES = ("orders", "transactions", "contracts")
# Only a finished item exchange has changed hands for its price; the
# finished_issuer / finished_contractor states are couriers' (verify live).
SOLD_CONTRACT_STATUSES = ("finished",)
OPEN_CONTRACT_STATUSES = ("outstanding", "in_progress")
ITEMS_WANTED_STATUSES = ("outstanding", "in_progress", "finished")
CORP_DIVISIONS = tuple(range(1, 8))
NPC_CORP_MAX = 2_000_000
# ESI rate-limit groups (tokens per 15 minutes) for the endpoints that carry
# one; the orders endpoints have only the error limit.
RATE_GROUPS = {
    ("character", "transactions"): ("char-wallet", 150),
    ("corporation", "transactions"): ("corp-wallet", 300),
    ("character", "contracts"): ("char-contract", 600),
    ("corporation", "contracts"): ("corp-contract", 600),
}
ROLE_HINT = {
    "orders": "Accountant or Trader",
    "transactions": "Accountant or Junior Accountant",
    "contracts": "no corporation role is documented for contracts — verify",
}
FAMILY_LABEL = {
    "orders": "orders",
    "transactions": "wallet",
    "contracts": "contracts",
}


# ---------------------------------------------------------------------------
# Time helpers — ESI's "…Z" shape throughout so text comparison is exact
# ---------------------------------------------------------------------------


def _now_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _placeholders(values) -> str:
    return ",".join("?" * len(values))


# ===========================================================================
# Pull side
# ===========================================================================


@dataclass
class PullSummary:
    new_sales: int = 0
    contracts_finished: int = 0
    open_orders: int = 0
    degraded_feeds: int = 0
    skipped: bool = False


@dataclass(frozen=True)
class _Owner:
    kind: str  # character | corporation
    id: int
    name: str


class _NoRole(Exception):
    """A corp fetcher answered None: this candidate lacks the role."""


class _BudgetHit(Exception):
    """This refresh's share of a rate group is spent."""


@dataclass
class _PullCtx:
    conn: sqlite3.Connection
    ref: object
    now: str
    pool: list
    scopes: dict = field(default_factory=dict)
    corp_of: dict = field(default_factory=dict)
    corp_unknown: set = field(default_factory=set)
    members: dict = field(default_factory=dict)
    corp_prefs: dict = field(default_factory=dict)
    dead: dict = field(default_factory=dict)  # character_id -> re-auth message
    group_calls: dict = field(default_factory=dict)
    group_stop: dict = field(default_factory=dict)
    esi_down: bool = False
    location_memo: dict = field(default_factory=dict)
    items_budget: int = config.LEDGER_CONTRACT_ITEMS_PER_REFRESH
    summary: PullSummary = field(default_factory=PullSummary)


def _group_budget(kind: str, family: str):
    group = RATE_GROUPS.get((kind, family))
    if group is None:
        return None, 0
    name, tokens = group
    return name, int(tokens * config.LEDGER_RATE_BUDGET_FRACTION)


def _spend(ctx: _PullCtx, group: str | None, budget: int) -> None:
    """Account one call against its rate group; raise when the share is
    spent (never probe past a stop either)."""
    if group is None:
        return
    if group in ctx.group_stop:
        raise _BudgetHit(ctx.group_stop[group])
    if ctx.group_calls.get(group, 0) >= budget:
        raise _BudgetHit("rate budget reached — the next refresh continues")
    ctx.group_calls[group] = ctx.group_calls.get(group, 0) + 1


def _build_ctx(conn, ref) -> _PullCtx:
    ctx = _PullCtx(
        conn=conn, ref=ref, now=_now_iso(), pool=store.pool_characters(conn),
        items_budget=config.LEDGER_CONTRACT_ITEMS_PER_REFRESH,
    )
    for row in conn.execute("SELECT character_id, scopes FROM esi_token"):
        ctx.scopes[row["character_id"]] = set((row["scopes"] or "").split())
    ctx.corp_prefs = store.corp_settings(conn)
    for character in ctx.pool:
        cid = character["character_id"]
        ctx.scopes.setdefault(cid, set())
        if ctx.esi_down:
            ctx.corp_unknown.add(cid)
            continue
        try:
            corp = esi.character_corporation_id(cid)
        except httpx.TransportError:
            ctx.esi_down = True
            ctx.corp_unknown.add(cid)
            continue
        except httpx.HTTPStatusError as exc:
            # The same rule as the families: 420 / 5xx is ESI down, not
            # "this character's membership is unknown".
            if exc.response.status_code == 420 or exc.response.status_code >= 500:
                ctx.esi_down = True
            ctx.corp_unknown.add(cid)
            continue
        except httpx.HTTPError:
            ctx.corp_unknown.add(cid)
            continue
        if corp < NPC_CORP_MAX:
            ctx.corp_of[cid] = None
            continue
        ctx.corp_of[cid] = corp
        ctx.members.setdefault(corp, []).append(cid)
    return ctx


def _owners(ctx: _PullCtx) -> list[_Owner]:
    """Corporations first (the ones the pool resolved to this pull, plus
    every esi_corp row, so an ESI outage still stamps their rows), then
    the characters. A corporation every member has left is neither: its
    rows keep their last status and drop out of the counts and notes."""
    owners = []
    known = list(ctx.members)
    for corp in ctx.corp_prefs:
        if corp not in known:
            known.append(corp)
    for corp in known:
        pref = ctx.corp_prefs.get(corp)
        name = (pref["corporation_name"] if pref is not None else None) or f"corporation {corp}"
        owners.append(_Owner("corporation", corp, name))
    for character in ctx.pool:
        owners.append(
            _Owner("character", character["character_id"], character["character_name"])
        )
    return owners


def _sales_on(ctx: _PullCtx, owner: _Owner) -> bool:
    if owner.kind == "character":
        row = next((c for c in ctx.pool if c["character_id"] == owner.id), None)
        return bool(row is None or row["count_sales"])
    pref = ctx.corp_prefs.get(owner.id)
    return bool(pref is None or pref["count_sales"])


def _candidates(ctx: _PullCtx, owner: _Owner, family: str) -> list[int]:
    scope = esi.LEDGER_SCOPES[(owner.kind, family)]
    ids = [owner.id] if owner.kind == "character" else ctx.members.get(owner.id, [])
    return [cid for cid in ids if scope in ctx.scopes.get(cid, set()) and cid not in ctx.dead]


def pull_sales(conn, ref) -> PullSummary:
    """Read every enabled owner's sell-side feeds and persist them. Never
    raises for scope, role, token, rate-limit or network trouble — each
    owner × family records its own status in sales_pull instead."""
    ctx = _build_ctx(conn, ref)
    for owner in _owners(ctx):
        for family in FAMILIES:
            if family == "transactions" and owner.kind == "corporation":
                _pull_corp_transactions(ctx, owner)
            else:
                _pull_family(ctx, owner, family)
    summary = ctx.summary
    summary.open_orders = conn.execute(
        "SELECT COUNT(*) FROM sale_order WHERE state = 'open'"
    ).fetchone()[0]
    owners = current_owners(conn) | {("corporation", c) for c in ctx.members}
    summary.degraded_feeds = sum(
        1
        for r in conn.execute(
            "SELECT owner_kind, owner_id FROM sales_pull WHERE status NOT IN ('ok', 'off')"
        )
        if (r["owner_kind"], r["owner_id"]) in owners
    )
    return summary


def current_owners(conn) -> set[tuple[str, int]]:
    """Owners that still exist: pool characters and esi_corp corporations.
    A removed character's or a left corporation's sales_pull rows are
    history, not a live feed to warn about."""
    owners = {
        ("character", r[0]) for r in conn.execute("SELECT character_id FROM pool_character")
    }
    owners |= {
        ("corporation", r[0]) for r in conn.execute("SELECT corporation_id FROM esi_corp")
    }
    return owners


def _pull_corp_transactions(ctx: _PullCtx, owner: _Owner) -> None:
    """Seven wallet divisions, each with its own cursor row; the candidate
    that answered division 1 is tried first for the rest (the role is
    corp-wide), and a 403 still falls through to the next member. When
    NO member holds the role, divisions 2..7 are stamped no_role without a
    single probe: the Accountant / Junior Accountant requirement is
    corp-wide, and every 403 counts against ESI's error limit."""
    preferred = None
    for index, division in enumerate(CORP_DIVISIONS):
        via, status, message = _pull_family(ctx, owner, "transactions", division, preferred)
        if via is not None:
            preferred = via
        elif status == "no_role":
            for rest in CORP_DIVISIONS[index + 1:]:
                _record_pull(ctx.conn, owner, "transactions", rest, "no_role", message)
            return


def _pull_family(
    ctx: _PullCtx, owner: _Owner, family: str, division: int = 0, preferred=None
):
    """One owner × family (× corp wallet division): the toggle, ESI-down and
    dead-token gates, the scope pre-check, the fetch phase over candidate
    tokens, then one short write. Returns (via_character_id, status,
    message); via is None unless a candidate answered."""
    conn = ctx.conn

    def fail(status, message=None):
        _record_pull(conn, owner, family, division, status, message)
        return None, status, message

    if not _sales_on(ctx, owner):
        return fail("off")
    if ctx.esi_down:
        return fail("skipped", "ESI unavailable — the next refresh retries")
    if owner.kind == "character" and owner.id in ctx.dead:
        return fail("error", ctx.dead[owner.id])
    membership_unknown = (
        "corporation membership unknown this pull — ESI /characters/ failed; "
        "retried next refresh"
    )
    if (
        owner.kind == "character"
        and family in ("orders", "transactions")
        and owner.id in ctx.corp_unknown
    ):
        # Its corp-flagged rows could not be attributed; fetching would
        # move the cursor past them for good, so this family waits.
        return fail("error", membership_unknown)
    candidates = _candidates(ctx, owner, family)
    if preferred in candidates:
        candidates.remove(preferred)
        candidates.insert(0, preferred)
    if not candidates:
        if (
            owner.kind == "corporation"
            and ctx.corp_unknown
            and not ctx.members.get(owner.id)
        ):
            # The tokens may well hold the scope — ESI failed to say whose
            # corporation this is; re-login advice would send the user
            # chasing nothing.
            return fail("error", membership_unknown)
        scope = esi.LEDGER_SCOPES[(owner.kind, family)]
        return fail(
            "no_scope",
            f"no logged-in character holds {scope} — log one in again from "
            "the ESI tab",
        )
    group, budget = _group_budget(owner.kind, family)
    if group in ctx.group_stop:
        return fail("skipped", ctx.group_stop[group])
    if group is not None and ctx.group_calls.get(group, 0) >= budget:
        return fail("partial", "rate budget reached — the next refresh continues")

    planner = {
        "orders": _fetch_orders,
        "transactions": _fetch_transactions,
        "contracts": _fetch_contracts,
    }[family]
    bundle = None
    via = None
    status = None
    message = None
    for cid in candidates:
        try:
            bundle = planner(ctx, owner, division, cid)
            via = cid
            break
        except _NoRole:
            continue
        except RuntimeError as exc:
            # A dead or absent refresh token: never this character again
            # this pull; a corp family tries the next member.
            ctx.dead[cid] = str(exc)
            if owner.kind == "character":
                status, message = "error", str(exc)
                break
            continue
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 420 or code >= 500:
                ctx.esi_down = True
                status, message = "skipped", f"ESI unavailable ({code}) — the next refresh retries"
            elif code == 429:
                message = _rate_limited_message(exc.response.headers)
                ctx.group_stop[group or f"{owner.kind}-{family}"] = message
                status = "skipped"
            else:
                status, message = "error", str(exc)
            break
        except httpx.TransportError as exc:
            ctx.esi_down = True
            status = "skipped"
            message = f"ESI unreachable ({exc.__class__.__name__}) — the next refresh retries"
            break
    if bundle is None:
        if status is None:
            status = "no_role"
            message = (
                f"no logged-in member has the corporation role for {family} "
                f"({ROLE_HINT[family]})"
            )
        return fail(status, message)

    writer = {
        "orders": _write_orders,
        "transactions": _write_transactions,
        "contracts": _write_contracts,
    }[family]
    try:
        writer(ctx, owner, division, via, bundle)
    except sqlite3.Error as exc:
        log.exception("ledger write failed for %s %s %s", owner.kind, owner.id, family)
        conn.rollback()
        return fail("error", f"database error: {exc}")
    return via, "ok", None


def _rate_limited_message(headers) -> str:
    retry = esi._int_header(headers, "Retry-After", 900)
    return f"rate limited — retry after {max(1, math.ceil(retry / 60))} min"


def _record_pull(
    conn, owner: _Owner, family: str, division: int, status: str,
    message, via=None, rows=None, rows_new=None, calls=None, cursor=None,
    now: str | None = None, commit: bool = True,
) -> None:
    """Upsert the provenance row. A non-ok write never clobbers an earlier
    pull's cursor: the COALESCE/MAX keep it unless a new one is given."""
    oldest, newest, backfilled = cursor if cursor else (None, None, None)
    conn.execute(
        "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, "
        "message, via_character_id, rows, rows_new, calls, oldest_id, newest_id, "
        "backfilled, pulled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (owner_kind, owner_id, family, division) DO UPDATE SET "
        "status = excluded.status, message = excluded.message, "
        "via_character_id = excluded.via_character_id, rows = excluded.rows, "
        "rows_new = excluded.rows_new, calls = excluded.calls, "
        "oldest_id = COALESCE(excluded.oldest_id, sales_pull.oldest_id), "
        "newest_id = COALESCE(excluded.newest_id, sales_pull.newest_id), "
        "backfilled = MAX(excluded.backfilled, sales_pull.backfilled), "
        "pulled_at = excluded.pulled_at",
        (
            owner.kind, owner.id, family, division, status, message, via, rows,
            rows_new, calls, oldest, newest, backfilled or 0, now or _now_iso(),
        ),
    )
    if commit:
        conn.commit()


def mark_skipped(conn, message: str) -> None:
    """ESI was down before the sales step: say so for every enabled owner ×
    family without a single network call (cursors are preserved); an
    owner whose Count sales is off stays 'off'."""
    now = _now_iso()
    for character in store.pool_characters(conn):
        owner = _Owner("character", character["character_id"], character["character_name"])
        status = "skipped" if character["count_sales"] else "off"
        note = message if status == "skipped" else None
        for family in FAMILIES:
            _record_pull(conn, owner, family, 0, status, note, now=now, commit=False)
    for corp_id, pref in store.corp_settings(conn).items():
        owner = _Owner("corporation", corp_id, pref["corporation_name"] or "")
        status = "skipped" if pref["count_sales"] else "off"
        note = message if status == "skipped" else None
        for family in ("orders", "contracts"):
            _record_pull(conn, owner, family, 0, status, note, now=now, commit=False)
        for division in CORP_DIVISIONS:
            _record_pull(conn, owner, "transactions", division, status, note,
                         now=now, commit=False)
    conn.commit()


def summary_line(summary: PullSummary) -> str:
    if summary.skipped:
        return "sales pull skipped — ESI unavailable"
    line = (
        f"sales: {summary.new_sales} new sales, "
        f"{summary.contracts_finished} contracts finished, "
        f"{summary.open_orders} open orders (all products)"
    )
    if summary.degraded_feeds:
        line += f" ({summary.degraded_feeds} feeds partial — see Ledger)"
    return line


# -- locations ------------------------------------------------------------


def _resolve_locations(ctx: _PullCtx, cid: int, location_ids) -> None:
    """Best-effort station/structure → system resolution during the fetch
    phase (the resolver keeps its own cache and commits itself)."""
    for location_id in {int(l) for l in location_ids if l}:
        if location_id in ctx.location_memo:
            continue
        try:
            esi.resolve_location(ctx.conn, cid, location_id, ctx.location_memo)
        except (httpx.HTTPError, RuntimeError, sqlite3.Error):
            ctx.location_memo.setdefault(location_id, None)


# -- family: orders ---------------------------------------------------------


def _order_owner(ctx: _PullCtx, owner: _Owner, cid: int, row: dict, division: int):
    """Where an order row is stored: a character-feed order flagged
    is_corporation belongs to the character's (current) corporation."""
    if owner.kind == "corporation":
        return "corporation", owner.id, row.get("wallet_division"), row.get("issued_by")
    if row.get("is_corporation") and ctx.corp_of.get(cid):
        return "corporation", ctx.corp_of[cid], None, cid
    return "character", cid, None, cid


def _fetch_orders(ctx: _PullCtx, owner: _Owner, division: int, cid: int) -> dict:
    conn = ctx.conn
    if owner.kind == "character":
        open_rows = esi.fetch_character_orders(conn, cid)
        history = esi.fetch_character_order_history(conn, cid)
    else:
        open_rows = esi.fetch_corp_orders(conn, cid, owner.id)
        if open_rows is None:
            raise _NoRole()
        history = esi.fetch_corp_order_history(conn, cid, owner.id)
        if history is None:
            raise _NoRole()
    sells_open = [r for r in open_rows if not r.get("is_buy_order", False)]
    sells_hist = [r for r in history if not r.get("is_buy_order", False)]
    _resolve_locations(ctx, cid, [r["location_id"] for r in sells_open + sells_hist])
    feed = owner.kind

    def norm(row, state):
        kind, oid, div, issued_by = _order_owner(ctx, owner, cid, row, division)
        return {
            "order_id": int(row["order_id"]),
            "owner_kind": kind,
            "owner_id": oid,
            "division": div,
            "issued_by": issued_by,
            "source_feed": feed,
            "type_id": int(row["type_id"]),
            "price": float(row["price"]),
            "volume_total": int(row["volume_total"]),
            "volume_remain": int(row["volume_remain"]),
            "location_id": int(row["location_id"]),
            "duration": int(row.get("duration", 0)),
            "issued": row["issued"],
            "state": state,
        }

    return {
        "open": [norm(r, "open") for r in sells_open],
        "history": [norm(r, r.get("state") or "expired") for r in sells_hist],
        "seen_ids": {int(r["order_id"]) for r in open_rows} | {int(r["order_id"]) for r in history},
        "calls": 2,
        "both_ok": True,
    }


def _estimated_close(issued: str, duration: int, now: str) -> str:
    try:
        expiry = _parse_ts(issued) + timedelta(days=int(duration or 0))
    except (ValueError, TypeError):
        return now
    return min(now, _iso(expiry))


_OWNER_REOWN = (
    "source_feed = CASE WHEN excluded.source_feed = 'corporation' THEN 'corporation' "
    "ELSE {t}.source_feed END, "
    "owner_kind = CASE WHEN excluded.source_feed = 'corporation' THEN 'corporation' "
    "ELSE {t}.owner_kind END, "
    "owner_id = CASE WHEN excluded.source_feed = 'corporation' THEN excluded.owner_id "
    "ELSE {t}.owner_id END, "
    "division = COALESCE(excluded.division, {t}.division)"
)


def _begin(conn) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _write_orders(ctx: _PullCtx, owner: _Owner, division: int, via: int, bundle: dict) -> None:
    conn = ctx.conn
    now = ctx.now
    _begin(conn)
    before = conn.execute("SELECT COUNT(*) FROM sale_order").fetchone()[0]
    reown = _OWNER_REOWN.format(t="sale_order")
    conn.executemany(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, division, issued_by, "
        "source_feed, type_id, price, volume_total, volume_remain, location_id, duration, "
        "issued, state, first_seen_at, last_seen_at) VALUES (:order_id, :owner_kind, "
        ":owner_id, :division, :issued_by, :source_feed, :type_id, :price, :volume_total, "
        ":volume_remain, :location_id, :duration, :issued, 'open', :now, :now) "
        "ON CONFLICT (order_id) DO UPDATE SET price = excluded.price, "
        "volume_remain = excluded.volume_remain, issued = excluded.issued, "
        "duration = excluded.duration, last_seen_at = excluded.last_seen_at, "
        "missing_since = NULL, issued_by = COALESCE(excluded.issued_by, sale_order.issued_by), "
        + reown + " WHERE sale_order.state = 'open'",
        [dict(r, now=now) for r in bundle["open"]],
    )
    conn.executemany(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, division, issued_by, "
        "source_feed, type_id, price, volume_total, volume_remain, location_id, duration, "
        "issued, state, first_seen_at, last_seen_at, history_seen_at) VALUES (:order_id, "
        ":owner_kind, :owner_id, :division, :issued_by, :source_feed, :type_id, :price, "
        ":volume_total, :volume_remain, :location_id, :duration, :issued, :state, :now, :now, "
        ":closed) ON CONFLICT (order_id) DO UPDATE SET state = excluded.state, "
        "price = excluded.price, volume_remain = excluded.volume_remain, "
        "last_seen_at = excluded.last_seen_at, missing_since = NULL, "
        "history_seen_at = COALESCE(sale_order.history_seen_at, excluded.history_seen_at), "
        "issued_by = COALESCE(excluded.issued_by, sale_order.issued_by), " + reown,
        [
            dict(r, now=now, closed=_estimated_close(r["issued"], r["duration"], now))
            for r in bundle["history"]
        ],
    )
    if bundle.get("both_ok"):
        seen = bundle["seen_ids"]
        rows = conn.execute(
            "SELECT order_id FROM sale_order WHERE owner_kind = ? AND owner_id = ? "
            "AND state = 'open' AND missing_since IS NULL",
            (owner.kind, owner.id),
        ).fetchall()
        missing = [r["order_id"] for r in rows if r["order_id"] not in seen]
        if missing:
            conn.executemany(
                "UPDATE sale_order SET missing_since = ? WHERE order_id = ?",
                [(now, oid) for oid in missing],
            )
    after = conn.execute("SELECT COUNT(*) FROM sale_order").fetchone()[0]
    _record_pull(
        conn, owner, "orders", division, "ok", None, via,
        rows=len(bundle["open"]) + len(bundle["history"]), rows_new=after - before,
        calls=bundle["calls"], now=now, commit=False,
    )
    conn.commit()


# -- family: transactions ---------------------------------------------------


def _cursor(conn, owner: _Owner, division: int):
    row = conn.execute(
        "SELECT oldest_id, newest_id, backfilled FROM sales_pull "
        "WHERE owner_kind = ? AND owner_id = ? AND family = 'transactions' AND division = ?",
        (owner.kind, owner.id, division),
    ).fetchone()
    if row is None or (row["oldest_id"] is None and row["newest_id"] is None):
        return None
    return row["oldest_id"], row["newest_id"], int(row["backfilled"] or 0)


def _fetch_transactions(ctx: _PullCtx, owner: _Owner, division: int, cid: int) -> dict:
    """The two-pass contiguous-range cursor (PROJECT.md §6): pass A walks
    the newest page down until it joins the stored range (uncapped once a
    range exists — the join is what lets newest_id move); pass B, the only
    pass that may declare history exhausted, back-fills below oldest_id a
    capped number of pages per refresh. Buys count for the cursor and are
    never stored."""
    conn = ctx.conn
    group, budget = _group_budget(owner.kind, "transactions")
    cap = config.LEDGER_TX_PAGES_PER_REFRESH
    cursor = _cursor(conn, owner, division)
    lo, hi, done = cursor if cursor else (None, None, 0)
    calls = 0

    def fetch(from_id):
        nonlocal calls
        _spend(ctx, group, budget)
        calls += 1
        if owner.kind == "character":
            page = esi.fetch_character_transactions(conn, cid, from_id)
        else:
            page = esi.fetch_corp_transactions(conn, cid, owner.id, division, from_id)
            if page is None:
                raise _NoRole()
        return page or []

    def ids_of(page):
        return {int(t["transaction_id"]) for t in page}

    sells: dict[int, dict] = {}
    seen: set[int] = set()

    def collect(page):
        for t in page:
            if t.get("is_buy", False):
                continue
            if owner.kind == "corporation":
                kind, oid, div = "corporation", owner.id, division
            elif t.get("is_personal", True) or not ctx.corp_of.get(cid):
                kind, oid, div = "character", cid, None
            else:
                kind, oid, div = "corporation", ctx.corp_of[cid], None
            sells[int(t["transaction_id"])] = {
                "transaction_id": int(t["transaction_id"]),
                "owner_kind": kind,
                "owner_id": oid,
                "division": div,
                "source_feed": owner.kind,
                "type_id": int(t["type_id"]),
                "quantity": int(t["quantity"]),
                "unit_price": float(t["unit_price"]),
                "date": t["date"],
                "location_id": int(t["location_id"]),
                "client_id": t.get("client_id"),
                "journal_ref_id": t.get("journal_ref_id"),
            }

    pages = 0
    connected = hi is None
    budget_hit = None
    try:
        page = fetch(None)
        if not page:
            # Nothing at all: connected, but no range to call backfilled —
            # a (NULL, NULL, 1) row would outlive the wallet's first sale
            # through the MAX upsert and hide everything below the cap.
            connected = True
        while page:
            ids = ids_of(page)
            collect(page)
            seen |= ids
            if hi is not None and min(ids) <= hi:
                connected = True
                break
            if hi is None and pages >= cap:
                break
            nxt = fetch(min(ids))
            if hi is None:
                pages += 1
            if not nxt or min(ids_of(nxt)) >= min(ids):
                connected = True
                if hi is None:
                    done = 1
                break
            page = nxt
        if connected:
            while not done and lo is not None and pages < cap:
                page = fetch(lo)
                pages += 1
                if not page:
                    done = 1
                    break
                ids = ids_of(page)
                if min(ids) >= lo:
                    done = 1
                    break
                collect(page)
                seen |= ids
                lo = min(ids)
    except _BudgetHit as exc:
        budget_hit = str(exc)

    _resolve_locations(ctx, cid, [s["location_id"] for s in sells.values()])
    if not connected:
        new_cursor = None
        status = "partial"
        message = budget_hit or (
            "newest sales not yet joined to stored history — the next refresh continues"
        )
    else:
        new_lo = min([v for v in (lo, min(seen) if seen else None) if v is not None], default=None)
        new_hi = max([v for v in (hi, max(seen) if seen else None) if v is not None], default=None)
        new_cursor = (new_lo, new_hi, int(done))
        empty = not seen and lo is None and hi is None
        status = "ok" if done or empty else "partial"
        message = None if done or empty else (
            budget_hit or "older history still loading — the next refresh continues"
        )
    return {
        "sells": list(sells.values()),
        "rows": len(seen),
        "calls": calls,
        "cursor": new_cursor,
        "status": status,
        "message": message,
    }


def _write_transactions(ctx: _PullCtx, owner: _Owner, division: int, via: int, bundle: dict) -> None:
    conn = ctx.conn
    now = ctx.now
    _begin(conn)
    before = conn.execute("SELECT COUNT(*) FROM sale_transaction").fetchone()[0]
    conn.executemany(
        "INSERT INTO sale_transaction (transaction_id, owner_kind, owner_id, division, "
        "source_feed, type_id, quantity, unit_price, date, location_id, client_id, "
        "journal_ref_id, fetched_at) VALUES (:transaction_id, :owner_kind, :owner_id, "
        ":division, :source_feed, :type_id, :quantity, :unit_price, :date, :location_id, "
        ":client_id, :journal_ref_id, :now) ON CONFLICT (transaction_id) DO UPDATE SET "
        + _OWNER_REOWN.format(t="sale_transaction"),
        [dict(r, now=now) for r in bundle["sells"]],
    )
    after = conn.execute("SELECT COUNT(*) FROM sale_transaction").fetchone()[0]
    ctx.summary.new_sales += after - before
    _record_pull(
        conn, owner, "transactions", division, bundle["status"], bundle["message"], via,
        rows=bundle["rows"], rows_new=after - before, calls=bundle["calls"],
        cursor=bundle["cursor"], now=now, commit=False,
    )
    conn.commit()


# -- family: contracts ------------------------------------------------------


def _contract_owner(owner: _Owner, cid: int, row: dict):
    """Issuer-side only. Character feed: the character must be the issuer;
    a contract issued on the corporation's behalf belongs to that
    corporation (issuer_corporation_id is exact — no corp_of guess). Corp
    feed: only contracts issued for THIS corporation."""
    if owner.kind == "character":
        if int(row.get("issuer_id", 0)) != cid:
            return None
        if row.get("for_corporation"):
            return "corporation", int(row["issuer_corporation_id"])
        return "character", cid
    if row.get("for_corporation") and int(row.get("issuer_corporation_id", 0)) == owner.id:
        return "corporation", owner.id
    return None


def _wants_items(row) -> bool:
    return row["type"] == "item_exchange" and row["status"] in ITEMS_WANTED_STATUSES


def _fetch_contracts(ctx: _PullCtx, owner: _Owner, division: int, cid: int) -> dict:
    conn = ctx.conn
    group, budget = _group_budget(owner.kind, "contracts")
    _spend(ctx, group, budget)
    calls = 1
    if owner.kind == "character":
        listing = esi.fetch_character_contracts(conn, cid)
    else:
        listing = esi.fetch_corp_contracts(conn, cid, owner.id)
        if listing is None:
            raise _NoRole()
    rows = []
    for c in listing:
        who = _contract_owner(owner, cid, c)
        if who is None:
            continue
        rows.append({
            "contract_id": int(c["contract_id"]),
            "owner_kind": who[0],
            "owner_id": who[1],
            "issuer_id": int(c["issuer_id"]),
            "issuer_corporation_id": int(c["issuer_corporation_id"]),
            "for_corporation": 1 if c.get("for_corporation") else 0,
            "acceptor_id": c.get("acceptor_id"),
            "assignee_id": c.get("assignee_id"),
            "availability": c.get("availability"),
            "type": c.get("type") or "unknown",
            "status": c.get("status") or "unknown",
            "price": c.get("price"),
            "title": c.get("title"),
            "date_issued": c["date_issued"],
            "date_accepted": c.get("date_accepted"),
            "date_completed": c.get("date_completed"),
            "date_expired": c["date_expired"],
            "start_location_id": c.get("start_location_id"),
            "via_character_id": cid,
        })
    _resolve_locations(ctx, cid, [r["start_location_id"] for r in rows])

    # Items: stored rows still waiting, plus the rows listed just now
    # (finished first, then newest), fetched before the family's write.
    settled = set()
    listed_ids = [r["contract_id"] for r in rows]
    for start in range(0, len(listed_ids), 500):
        chunk = listed_ids[start:start + 500]
        settled |= {
            r[0]
            for r in conn.execute(
                f"SELECT contract_id FROM sale_contract WHERE contract_id IN "
                f"({_placeholders(chunk)}) AND (items_fetched_at IS NOT NULL "
                f"OR items_status IS NOT NULL)",
                chunk,
            )
        }
    wanted = {}
    for r in rows:
        if _wants_items(r) and r["contract_id"] not in settled:
            wanted[r["contract_id"]] = r
    for r in conn.execute(
        "SELECT contract_id, type, status, date_issued FROM sale_contract "
        "WHERE owner_kind = ? AND owner_id = ? AND items_fetched_at IS NULL "
        "AND items_status IS NULL AND type = 'item_exchange' "
        "AND status IN ('outstanding', 'in_progress', 'finished')",
        (owner.kind, owner.id),
    ):
        wanted.setdefault(r["contract_id"], dict(r))
    order = sorted(wanted.values(), key=lambda r: r["date_issued"], reverse=True)
    order.sort(key=lambda r: 0 if r["status"] == "finished" else 1)
    tokens = [cid] + [
        m for m in _candidates(ctx, owner, "contracts") if m != cid
    ] if owner.kind == "corporation" else [cid]
    items_out = {}  # contract_id -> ("ok", items) | ("missing" | "unavailable" | "attempt", None)
    remaining = 0
    halted = None
    for r in order:
        if halted or ctx.items_budget <= 0:
            remaining += 1
            continue
        ctx.items_budget -= 1
        result = None
        forbidden = 0
        for token in tokens:
            if token in ctx.dead:
                continue
            try:
                _spend(ctx, group, budget)  # one token per request sent
            except _BudgetHit as exc:
                halted = str(exc)
                break
            calls += 1
            try:
                if owner.kind == "character":
                    items = esi.fetch_character_contract_items(conn, token, r["contract_id"])
                else:
                    items = esi.fetch_corp_contract_items(conn, token, owner.id, r["contract_id"])
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 403:
                    forbidden += 1
                    continue
                if code == 429:
                    halted = _rate_limited_message(exc.response.headers)
                    if group:
                        ctx.group_stop[group] = halted
                    break
                if code == 420 or code >= 500:
                    ctx.esi_down = True
                    halted = f"ESI unavailable ({code}) while fetching contract items"
                    break
                result = ("attempt", None)
                break
            except httpx.TransportError as exc:
                ctx.esi_down = True
                halted = f"ESI unreachable ({exc.__class__.__name__}) while fetching contract items"
                break
            except RuntimeError as exc:
                ctx.dead[token] = str(exc)
                continue
            result = ("missing", None) if items is None else ("ok", items)
            break
        if halted:
            remaining += 1  # ESI trouble is not the contract's fault: retried, no attempt charged
            continue
        if result is None:
            # Every candidate was refused (403) or dead: no token can read it.
            result = ("unavailable", None) if forbidden else ("attempt", None)
        items_out[r["contract_id"]] = result
    return {
        "rows": rows,
        "items": items_out,
        "remaining": remaining,
        "halted": halted,
        "calls": calls,
        "listed_finished": sum(1 for r in rows if r["status"] in SOLD_CONTRACT_STATUSES),
    }


def _write_contracts(ctx: _PullCtx, owner: _Owner, division: int, via: int, bundle: dict) -> None:
    conn = ctx.conn
    now = ctx.now
    _begin(conn)
    finished_before = {
        r["contract_id"]
        for r in conn.execute(
            "SELECT contract_id FROM sale_contract WHERE status = 'finished'"
        )
    }
    before = conn.execute("SELECT COUNT(*) FROM sale_contract").fetchone()[0]
    conn.executemany(
        "INSERT INTO sale_contract (contract_id, owner_kind, owner_id, issuer_id, "
        "issuer_corporation_id, for_corporation, acceptor_id, assignee_id, availability, "
        "type, status, price, title, date_issued, date_accepted, date_completed, "
        "date_expired, start_location_id, via_character_id, first_seen_at, last_seen_at) "
        "VALUES (:contract_id, :owner_kind, :owner_id, :issuer_id, :issuer_corporation_id, "
        ":for_corporation, :acceptor_id, :assignee_id, :availability, :type, :status, "
        ":price, :title, :date_issued, :date_accepted, :date_completed, :date_expired, "
        ":start_location_id, :via_character_id, :now, :now) "
        "ON CONFLICT (contract_id) DO UPDATE SET status = excluded.status, "
        "acceptor_id = COALESCE(excluded.acceptor_id, sale_contract.acceptor_id), "
        "date_accepted = COALESCE(excluded.date_accepted, sale_contract.date_accepted), "
        "date_completed = COALESCE(excluded.date_completed, sale_contract.date_completed), "
        "price = COALESCE(excluded.price, sale_contract.price), "
        "title = COALESCE(excluded.title, sale_contract.title), "
        "last_seen_at = excluded.last_seen_at, "
        "via_character_id = COALESCE(sale_contract.via_character_id, excluded.via_character_id)",
        [dict(r, now=now) for r in bundle["rows"]],
    )
    for contract_id, (kind, items) in bundle["items"].items():
        if kind == "ok":
            conn.executemany(
                "INSERT OR IGNORE INTO sale_contract_item (contract_id, record_id, type_id, "
                "quantity, raw_quantity, is_included, is_singleton) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        contract_id, int(i["record_id"]), int(i["type_id"]),
                        int(i["quantity"]), i.get("raw_quantity"),
                        1 if i.get("is_included") else 0, 1 if i.get("is_singleton") else 0,
                    )
                    for i in items
                ],
            )
            conn.execute(
                "UPDATE sale_contract SET items_fetched_at = ?, items_status = 'ok' "
                "WHERE contract_id = ?",
                (now, contract_id),
            )
        elif kind in ("missing", "unavailable"):
            conn.execute(
                "UPDATE sale_contract SET items_fetched_at = ?, items_status = ? "
                "WHERE contract_id = ?",
                (now, kind, contract_id),
            )
        else:
            conn.execute(
                "UPDATE sale_contract SET items_attempts = items_attempts + 1, "
                "items_status = CASE WHEN items_attempts + 1 >= ? THEN 'missing' "
                "ELSE items_status END, items_fetched_at = CASE WHEN items_attempts + 1 >= ? "
                "THEN ? ELSE items_fetched_at END WHERE contract_id = ?",
                (config.LEDGER_ITEM_ATTEMPTS, config.LEDGER_ITEM_ATTEMPTS, now, contract_id),
            )
    after = conn.execute("SELECT COUNT(*) FROM sale_contract").fetchone()[0]
    finished_after = {
        r["contract_id"]
        for r in conn.execute(
            "SELECT contract_id FROM sale_contract WHERE status = 'finished' "
            "AND type = 'item_exchange'"
        )
    }
    ctx.summary.contracts_finished += len(finished_after - finished_before)
    if bundle["halted"]:
        status, message = "partial", bundle["halted"]
    elif bundle["remaining"]:
        status = "partial"
        message = (
            f"{bundle['remaining']} contracts' items still to fetch — the next "
            "refresh continues"
        )
    else:
        status, message = "ok", None
    _record_pull(
        conn, owner, "contracts", division, status, message, via,
        rows=len(bundle["rows"]), rows_new=after - before, calls=bundle["calls"],
        now=now, commit=False,
    )
    conn.commit()


# ===========================================================================
# Read side
# ===========================================================================


@dataclass(frozen=True)
class Window:
    key: str          # '7' | '30' | '90' | 'all'
    days: int | None
    since: date | None
    until: date
    since_ts: str | None

    @property
    def label(self) -> str:
        return "all time" if self.days is None else f"last {self.days} days"


def window_bounds(window, now: datetime) -> Window:
    """Calendar-aligned UTC windows, today included, so the filter, the
    chart axis and the bucket count agree."""
    today = now.astimezone(timezone.utc).date()
    key = str(window or DEFAULT_WINDOW)
    if key == "all":
        return Window("all", None, None, today, None)
    try:
        days = int(key)
    except ValueError:
        days = DEFAULT_WINDOW
    if days not in WINDOWS:
        days = DEFAULT_WINDOW
    since = today - timedelta(days=days - 1)
    return Window(str(days), days, since, today, since.isoformat() + "T00:00:00Z")


def finals(conn) -> dict[int, list[sqlite3.Row]]:
    """Every pipeline's final, active or not — a deleted pipeline's product
    stops being a final (its stored sales stay, invisible)."""
    out: dict[int, list] = {}
    for row in conn.execute("SELECT * FROM pipeline ORDER BY pipeline_id"):
        out.setdefault(row["final_product_type_id"], []).append(row)
    return out


def internal_ids(conn) -> set[int]:
    """Buyers that are us: a sale to our own character or corporation is
    a transfer, not revenue."""
    ids = {r[0] for r in conn.execute("SELECT character_id FROM pool_character")}
    ids |= {r[0] for r in conn.execute("SELECT corporation_id FROM esi_corp")}
    ids |= {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT owner_id FROM sales_pull WHERE owner_kind = 'corporation'"
        )
    }
    ids |= {
        r[0]
        for r in conn.execute("SELECT DISTINCT issued_by FROM sale_order WHERE issued_by IS NOT NULL")
    }
    return ids


def enabled_owners(conn) -> set[tuple[str, int]]:
    """Owners whose sales count (toggles are honoured at read time; a
    corporation without an esi_corp row counts)."""
    on = {
        ("character", r["character_id"])
        for r in conn.execute("SELECT character_id FROM pool_character WHERE count_sales = 1")
    }
    off_corps = {
        r["corporation_id"]
        for r in conn.execute("SELECT corporation_id FROM esi_corp WHERE count_sales = 0")
    }
    corps = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT owner_id FROM sales_pull WHERE owner_kind = 'corporation' "
            "UNION SELECT DISTINCT owner_id FROM sale_transaction WHERE owner_kind = 'corporation' "
            "UNION SELECT DISTINCT owner_id FROM sale_order WHERE owner_kind = 'corporation' "
            "UNION SELECT DISTINCT owner_id FROM sale_contract WHERE owner_kind = 'corporation' "
            "UNION SELECT corporation_id FROM esi_corp"
        )
    }
    on |= {("corporation", c) for c in corps if c not in off_corps}
    return on


def owner_names(conn) -> dict[tuple[str, int], str]:
    names = {
        ("character", r["character_id"]): r["character_name"]
        for r in conn.execute("SELECT character_id, character_name FROM pool_character")
    }
    for r in conn.execute("SELECT corporation_id, corporation_name FROM esi_corp"):
        if r["corporation_name"]:
            names[("corporation", r["corporation_id"])] = r["corporation_name"]
    return names


def _owner_label(names, kind, oid) -> str:
    return names.get((kind, oid)) or f"{kind} {oid}"


def _type_name(ref, type_id: int) -> str:
    try:
        return ref.type_info(type_id).name
    except KeyError:
        return f"type {type_id}"


def order_outcome(state: str, volume_total: int, volume_remain: int) -> str:
    """Pure classification of a closed order — badges only, never summed."""
    if state == "open":
        return "open"
    if state in ("expired", "cancelled") and volume_remain == 0:
        return "filled"
    if state in ("expired", "cancelled"):
        if 0 < volume_remain < volume_total:
            return "partial"
        return state
    return state


@dataclass
class Attribution:
    per_final: dict          # type_id -> (qty, unit_price | None)
    flags: set
    clean: bool
    others: list             # (type_id, qty) included non-finals
    asked: list              # (type_id, qty) items the issuer asked for


def attribute_contract(price, items, quotes: dict, final_ids) -> Attribution:
    """Who gets a contract's price. Items are aggregated by type first
    (two singleton hulls are two records of quantity 1). A clean contract
    (finals only, nothing asked back) splits the whole price across its
    finals pro rata by cached quote × qty — equal per unit, flagged
    'estimated split', when a quote is missing. A mixed or swap contract
    attributes NO price: its finals count as units only."""
    included: dict[int, int] = {}
    asked: dict[int, int] = {}
    for item in items:
        target = included if item["is_included"] else asked
        target[item["type_id"]] = target.get(item["type_id"], 0) + int(item["quantity"])
    finals_in = {t: q for t, q in included.items() if t in final_ids}
    others = [(t, q) for t, q in included.items() if t not in final_ids]
    flags = set()
    if asked:
        flags.add("swap")
    if others:
        flags.add("mixed")
    clean = not flags
    per_final: dict[int, tuple] = {}
    if not finals_in:
        return Attribution(per_final, flags, clean, others, list(asked.items()))
    if not clean or not price or price <= 0:
        for t, q in finals_in.items():
            per_final[t] = (q, None)
        return Attribution(per_final, flags, clean, others, list(asked.items()))
    weights = {t: (quotes.get(t) or 0) * q for t, q in finals_in.items()}
    if any(w <= 0 for w in weights.values()):
        flags.add("estimated split")
        units = sum(finals_in.values())
        for t, q in finals_in.items():
            per_final[t] = (q, price / units)
    else:
        total = sum(weights.values())
        for t, q in finals_in.items():
            per_final[t] = (q, price * weights[t] / total / q)
    return Attribution(per_final, flags, clean, others, list(asked.items()))


@dataclass
class CostBasis:
    pipeline_id: int
    pipeline_name: str
    index_run_id: int
    run_number: int
    completed_at: str | None
    cost: object | None     # costing.HullCost

    @property
    def unit_cost(self):
        if self.cost is None or self.cost.total <= 0:
            return None
        return self.cost.total


_DAWN = datetime.min.replace(tzinfo=timezone.utc)


class CostVintages:
    """Every executed run that can cost a final's sales, per final — the
    runs with attributable hulls of the final, across the pipelines that
    share it — and the two lookups the Ledger makes (v1.27.1, user ruling
    2026-09-09: "the basis marked on the executed run at the time of
    sale"):

    * ``at(type_id, when)`` — the latest run executed on or before the
      sale (ranked by ``completed_at``, then run number, tie → lowest
      pipeline id) that PRICES: a run planned before any price pull
      totals zero and sets no basis, so it is passed over for the one
      before it (review 2026-09-09). Executing a new run therefore never
      re-costs a sale made before it. Where NO priced run had been
      executed by the sale — none at all, or only unpriced ones — it
      takes the earliest priced run, flagged ``pre_history`` (review
      2026-09-10: the page words it as that, not as "before your first
      executed run").
    * ``latest(type_id)`` — the newest executed run, whatever the date:
      the basis of what is still unsold (open orders, outstanding
      contracts) and the products table's cost-per-unit column.

    The basis is a run's PER-HULL cost, so a run whose install check let
    no hull start that cycle still counts (user ruling 2026-09-09). Per
    (run, pipeline) costs are computed once, on demand — a window's
    sales span a few runs, not the whole history. A run executed before
    v1.5 stamped ``completed_at`` sorts as the dawn of time."""

    def __init__(self, conn, ref, settings, finals_map):
        self._conn, self._ref, self._settings = conn, ref, settings
        self._runs: dict[int, list] = {}
        self._memo: dict[tuple[int, int], CostBasis] = {}
        for type_id, pipelines in finals_map.items():
            entries = []
            for p in pipelines:
                for row in conn.execute(
                    "SELECT r.index_run_id, r.run_number, r.completed_at FROM index_run r "
                    "JOIN index_run_item i ON i.index_run_id = r.index_run_id AND i.type_id = ? "
                    "JOIN index_run_item_pipeline a ON a.index_run_item_id = i.index_run_item_id "
                    "  AND a.pipeline_id = ? AND a.qty_attributable > 0 "
                    "WHERE r.status = 'complete'",
                    (type_id, p["pipeline_id"]),
                ):
                    when = _parse_ts(row["completed_at"]) if row["completed_at"] else _DAWN
                    entries.append((when, row["run_number"], -p["pipeline_id"], row, p))
            self._runs[type_id] = entries

    def _basis(self, row, p) -> CostBasis:
        key = (row["index_run_id"], p["pipeline_id"])
        basis = self._memo.get(key)
        if basis is None:
            cost = costing.hull_cost(
                self._conn, self._ref, self._settings, row["index_run_id"], p["pipeline_id"]
            )
            basis = self._memo[key] = CostBasis(
                p["pipeline_id"], p["name"], row["index_run_id"], row["run_number"],
                row["completed_at"], cost,
            )
        return basis

    @staticmethod
    def _newest(entries):
        return max(entries, key=lambda e: (e[1], e[2]))  # run number, then lowest pipeline id

    def latest(self, type_id: int) -> CostBasis | None:
        entries = self._runs.get(type_id) or []
        if not entries:
            return None
        _when, _rn, _pid, row, p = self._newest(entries)
        return self._basis(row, p)

    def at(self, type_id: int, when: str | None) -> tuple[CostBasis | None, bool]:
        """(basis, pre_history) for a sale at ISO ``when``: the run
        executed latest on or before the sale (by execution time, then
        run number, tie → lowest pipeline id) that PRICES — a run planned
        before any price pull (every line unpriced, total 0) sets no
        basis and is passed over for the one before it (review
        2026-09-09). Where NO priced run had been executed by the sale
        (none at all, or only unpriced ones): the earliest priced run by
        execution time, flagged pre_history — the page words it as that,
        not as "before your first executed run" (review 2026-09-10)."""
        entries = self._runs.get(type_id) or []
        if not entries:
            return None, False
        sold = _parse_ts(when) if when else None
        if sold is not None:
            eligible = sorted(
                (e for e in entries if e[0] <= sold),
                key=lambda e: (e[0], e[1], e[2]),
                reverse=True,
            )
            for _when, _rn, _pid, row, p in eligible:
                basis = self._basis(row, p)
                if basis.unit_cost is not None:
                    return basis, False
        for _when, _rn, _pid, row, p in sorted(
            entries, key=lambda e: (e[0], e[1], -e[2])
        ):
            basis = self._basis(row, p)
            if basis.unit_cost is not None:
                return basis, True
        return None, False

    def latest_map(self) -> dict[int, CostBasis]:
        out = {}
        for type_id in self._runs:
            basis = self.latest(type_id)
            if basis is not None:
                out[type_id] = basis
        return out


def cost_bases(conn, ref, settings, finals_map) -> dict[int, CostBasis]:
    """Latest executed run with attributable hulls per final (two pipelines
    sharing a final: the newest run, tie → lowest pipeline id) — the
    CURRENT basis, which the unsold listings and the products table's
    cost-per-unit column read; sales are costed at their own date through
    CostVintages.at. The basis is the run's PER-HULL cost, so a run whose
    install check let no hull start that cycle (hulls_per_cycle 0 — the
    plan wanted them, the stock did not feed them) still stands as the
    basis (user ruling 2026-09-09): its prices are the latest the line
    bought at."""
    return CostVintages(conn, ref, settings, finals_map).latest_map()


def final_quote(conn, ref, settings, type_id: int):
    """(sell price, net proceeds per hull, is_capital) for one final —
    capital-class hulls from the structure market cache with their own
    fees and movement cost, everything else from the hub region cache."""
    capital = costing.is_capital_priced(ref, type_id)
    if capital:
        price = market.cached_prices(
            conn, settings.capital_structure(), [type_id], market.STRUCTURE_SOURCE
        ).get(type_id)
    else:
        price = market.cached_prices(
            conn, settings.price_region_id, [type_id], settings.price_source
        ).get(type_id)
    net = (
        costing.net_proceeds_per_hull(
            price, ref.type_info(type_id).freight_volume, settings,
            capital=capital, freight_exempt=costing.freight_out_exempt(type_id),
        )
        if price is not None
        else None
    )
    return price, net, capital


def _net_for(ref, settings, type_id: int, unit_price: float, quantity: int,
             location_id=None) -> float:
    """Net proceeds for one sale event, fees and freight by WHERE it sold
    (costing.net_proceeds_at_venue); the class rule when ESI gave no
    location."""
    return costing.net_proceeds_at_venue(
        unit_price, ref.type_info(type_id).freight_volume, settings,
        costing.sale_venue(location_id),
        capital=costing.is_capital_priced(ref, type_id),
        freight_exempt=costing.freight_out_exempt(type_id),
    ) * quantity


def venue_label(venue: str | None, settings) -> str:
    """The RATE SET a sale was charged, not the place: any NPC station
    pays the high-sec hub rates, any structure the configured structure
    market's rates (the only structure rates in Settings)."""
    if venue == costing.SALE_VENUE_STRUCTURE:
        return f"a structure (the {settings.structure_market_label()} rates)"
    if venue == costing.SALE_VENUE_HUB:
        return "an NPC station (the high-sec hub rates)"
    return "no location on record (the hull's class rule)"


@dataclass
class Sale:
    when: str
    type_id: int
    name: str
    quantity: int
    unit_price: float | None
    gross: float | None
    net: float | None
    profit: float | None
    source: str                 # market | contract
    owner_kind: str
    owner_id: int
    owner: str
    division: int | None
    location_id: int | None
    system: str | None
    ref_id: int
    flags: tuple
    counted: bool
    priced: bool
    contract_price: float | None = None
    detail: str = ""
    venue: str | None = None        # costing.SALE_VENUE_* or None (no location)
    venue_label: str = ""
    # v1.27.1: the run this sale was costed at — the latest executed on
    # or before the sale (pre_history: none was, so the earliest stands
    # in) — and its per-hull cost; None = no executed run at all.
    basis_run: int | None = None
    unit_cost: float | None = None
    pre_history: bool = False


def sales_rows(conn, ref, settings, finals_map, window: Window, internal, quotes,
               enabled, vintages, names) -> list[Sale]:
    """Every sale event of a final in the window, newest first: wallet
    transactions (the units truth) and finished item-exchange contracts,
    each costed at its own date through ``vintages`` (CostVintages)."""
    final_ids = set(finals_map)
    if not final_ids:
        return []
    ph = _placeholders(final_ids)
    params: list = list(final_ids)
    ts_clause = ""
    if window.since_ts:
        ts_clause = " AND t.date >= ?"
        params.append(window.since_ts)
    sales: list[Sale] = []

    def cost_at(type_id: int, when: str | None):
        basis, pre = vintages.at(type_id, when)
        if basis is None:
            return None, None, False
        return basis.unit_cost, basis.run_number, pre

    for t in conn.execute(
        f"SELECT t.*, l.solar_system_id FROM sale_transaction t "
        f"LEFT JOIN location_system l ON l.location_id = t.location_id "
        f"WHERE t.type_id IN ({ph}){ts_clause} ORDER BY t.date DESC, t.transaction_id DESC",
        params,
    ):
        flags = []
        counted = (t["owner_kind"], t["owner_id"]) in enabled
        if not counted:
            flags.append("sales off")
        if t["client_id"] is not None and t["client_id"] in internal:
            flags.append("internal")
            counted = False
        gross = t["unit_price"] * t["quantity"]
        net = _net_for(ref, settings, t["type_id"], t["unit_price"], t["quantity"],
                       t["location_id"])
        venue = costing.sale_venue(t["location_id"])
        uc, basis_run, pre = cost_at(t["type_id"], t["date"])
        profit = net - t["quantity"] * uc if uc is not None else None
        sales.append(Sale(
            when=t["date"], type_id=t["type_id"], name=_type_name(ref, t["type_id"]),
            quantity=t["quantity"], unit_price=t["unit_price"], gross=gross, net=net,
            profit=profit, source="market", owner_kind=t["owner_kind"],
            owner_id=t["owner_id"], owner=_owner_label(names, t["owner_kind"], t["owner_id"]),
            division=t["division"], location_id=t["location_id"],
            system=_system_name(ref, t["solar_system_id"]), ref_id=t["transaction_id"],
            flags=tuple(flags), counted=counted, priced=True,
            venue=venue, venue_label=venue_label(venue, settings),
            basis_run=basis_run, unit_cost=uc, pre_history=pre,
        ))
    # Contracts: finished item exchanges whose item list names a final.
    cparams: list = list(SOLD_CONTRACT_STATUSES) + list(final_ids)
    cts = ""
    if window.since_ts:
        cts = " AND COALESCE(c.date_completed, c.date_accepted, c.date_issued) >= ?"
        cparams.append(window.since_ts)
    for c in conn.execute(
        f"SELECT c.*, l.solar_system_id FROM sale_contract c "
        f"LEFT JOIN location_system l ON l.location_id = c.start_location_id "
        f"WHERE c.type = 'item_exchange' AND c.status IN ({_placeholders(SOLD_CONTRACT_STATUSES)}) "
        f"AND c.items_status = 'ok' AND EXISTS (SELECT 1 FROM sale_contract_item i "
        f"WHERE i.contract_id = c.contract_id AND i.is_included = 1 AND i.type_id IN ({ph}))"
        f"{cts} ORDER BY COALESCE(c.date_completed, c.date_accepted, c.date_issued) DESC",
        cparams,
    ):
        items = [dict(i) for i in conn.execute(
            "SELECT * FROM sale_contract_item WHERE contract_id = ?", (c["contract_id"],)
        )]
        attribution = attribute_contract(c["price"], items, quotes, final_ids)
        when = c["date_completed"] or c["date_accepted"] or c["date_issued"]
        flags = sorted(attribution.flags)
        counted = (c["owner_kind"], c["owner_id"]) in enabled
        if not counted:
            flags.append("sales off")
        if c["acceptor_id"] is not None and c["acceptor_id"] in internal:
            flags.append("internal")
            counted = False
        if not c["price"] or c["price"] <= 0:
            flags.append("no price")
            counted = False
        detail_bits = []
        if attribution.others:
            detail_bits.append(
                "bundled with " + ", ".join(
                    f"{q}× {_type_name(ref, t)}" for t, q in attribution.others
                )
            )
        if attribution.asked:
            detail_bits.append(
                "traded for " + ", ".join(
                    f"{q}× {_type_name(ref, t)}" for t, q in attribution.asked
                )
            )
        if c["title"]:
            detail_bits.append(f"“{c['title']}”")
        detail = "; ".join(detail_bits)
        venue = costing.sale_venue(c["start_location_id"])
        for type_id, (qty, unit_price) in attribution.per_final.items():
            priced = unit_price is not None
            gross = unit_price * qty if priced else None
            net = (
                _net_for(ref, settings, type_id, unit_price, qty, c["start_location_id"])
                if priced else None
            )
            uc, basis_run, pre = cost_at(type_id, when)
            profit = net - qty * uc if (priced and uc is not None) else None
            sales.append(Sale(
                when=when, type_id=type_id, name=_type_name(ref, type_id), quantity=qty,
                unit_price=unit_price, gross=gross, net=net, profit=profit,
                source="contract", owner_kind=c["owner_kind"], owner_id=c["owner_id"],
                owner=_owner_label(names, c["owner_kind"], c["owner_id"]), division=None,
                location_id=c["start_location_id"],
                system=_system_name(ref, c["solar_system_id"]), ref_id=c["contract_id"],
                flags=tuple(flags), counted=counted, priced=priced,
                contract_price=c["price"], detail=detail,
                venue=venue, venue_label=venue_label(venue, settings),
                basis_run=basis_run, unit_cost=uc, pre_history=pre,
            ))
    sales.sort(key=lambda s: (s.when, s.ref_id), reverse=True)
    return sales


def _system_name(ref, system_id) -> str | None:
    if not system_id:
        return None
    row = ref.solar_system(int(system_id))
    return row["name"] if row is not None else None


@dataclass
class Product:
    type_id: int
    name: str
    units_sold: int = 0
    units_priced: int = 0
    revenue: float = 0.0
    net: float = 0.0
    sales_count: int = 0
    contract_units: int = 0
    basis: CostBasis | None = None
    quote: float | None = None
    quote_net: float | None = None
    capital: bool = False
    quote_sell_side: bool = True   # False under the Max Buy basis: not comparable
    quote_age: str | None = None
    badges: list = field(default_factory=list)
    # v1.27.1: cost of goods sold at each sale's own vintage — the units
    # it covers, the ISK, the runs it drew on and the units sold before
    # the first executed run (costed at that run).
    cost_units: int = 0
    cogs: float = 0.0
    runs_used: set = field(default_factory=set)
    pre_history_units: int = 0
    # The run numbers those units were costed at (the earliest priced
    # run by execution time — not necessarily the lowest run number;
    # review 2026-09-10).
    pre_history_runs: set = field(default_factory=set)

    @property
    def unpriced_units(self) -> int:
        return self.units_sold - self.units_priced

    @property
    def avg_price(self):
        return self.revenue / self.units_priced if self.units_priced else None

    @property
    def unit_cost(self):
        """Cost per hull at the CURRENT basis (the latest executed run)."""
        return self.basis.unit_cost if self.basis else None

    @property
    def cost_of_units(self):
        """Cost of goods sold: every priced unit at the basis of its own
        sale date (v1.27.1). None unless EVERY priced unit got a vintage
        (review 2026-09-09: a partly costed product would count the
        uncosted units' whole net as profit) — or with nothing priced."""
        if not self.units_priced or self.cost_units < self.units_priced:
            return None  # no basis, or nothing priced to cost: profit is unknown
        return self.cogs

    @property
    def avg_unit_cost(self):
        """Cost of goods sold per unit — what the window's sales were
        costed at on average."""
        return self.cogs / self.cost_units if self.cost_units else None

    @property
    def runs_label(self) -> str:
        runs = sorted(self.runs_used)
        if not runs:
            return ""
        if len(runs) == 1:
            return f"run {runs[0]}"
        return f"runs {runs[0]}–{runs[-1]}" if runs[-1] - runs[0] == len(runs) - 1 else "runs " + ", ".join(str(r) for r in runs)

    @property
    def profit(self):
        return self.net - self.cost_of_units if self.cost_of_units is not None else None

    @property
    def profit_per_unit(self):
        return self.profit / self.units_priced if self.profit is not None and self.units_priced else None

    @property
    def margin_pct(self):
        return self.profit / self.cost_of_units * 100 if self.cost_of_units else None

    @property
    def vs_quote_pct(self):
        if self.quote and self.avg_price is not None and self.quote_sell_side:
            return (self.avg_price / self.quote - 1) * 100
        return None


def products(conn, ref, settings, finals_map, sales, bases, quotes_full, window: Window,
             quote_age=None) -> list[Product]:
    by_type: dict[int, Product] = {}
    quote_age = quote_age or {}
    for type_id in finals_map:
        price, net, capital = quotes_full.get(type_id, (None, None, False))
        by_type[type_id] = Product(
            type_id, _type_name(ref, type_id), basis=bases.get(type_id),
            quote=price, quote_net=net, capital=capital,
            quote_sell_side=capital or settings.price_source == "sell",
            quote_age=quote_age.get(type_id),
        )
    for s in sales:
        if not s.counted:
            continue
        p = by_type[s.type_id]
        p.units_sold += s.quantity
        p.sales_count += 1
        if s.source == "contract":
            p.contract_units += s.quantity
        if s.priced:
            p.units_priced += s.quantity
            p.revenue += s.gross
            p.net += s.net
            if s.unit_cost is not None:
                p.cost_units += s.quantity
                p.cogs += s.quantity * s.unit_cost
                p.runs_used.add(s.basis_run)
                if s.pre_history:
                    p.pre_history_units += s.quantity
                    p.pre_history_runs.add(s.basis_run)
    out = []
    for p in by_type.values():
        if p.units_sold == 0 and window.days is not None:
            continue
        p.badges = _product_badges(p)
        out.append(p)
    out.sort(key=lambda p: (p.profit is None, -(p.profit or 0), p.name))
    return out


def _product_badges(p: Product) -> list:
    badges = []
    if p.capital:
        badges.append(("capital", "accent", "capital-class hull: sell-quoted from the structure market; fees follow where each unit sold, with the flat movement cost at either venue"))
    if p.basis is None:
        badges.append(("no executed run", "warn", "no executed index run is attributed to this pipeline — a deleted pipeline loses its run history; the next executed run sets the basis"))
    else:
        cost = p.basis.cost
        if cost is None or cost.total <= 0:
            if p.cost_units:
                badges.append(("latest unpriced", "warn", f"run {p.basis.run_number}, the latest executed, was planned before any price pull — every cost line is unpriced, so it sets no cost per unit; the window's sales are costed at the priced runs executed before them"))
            else:
                badges.append(("no executed run", "warn", f"run {p.basis.run_number} was planned before any price pull — every cost line is unpriced, so it sets no basis"))
        else:
            if cost.missing_prices:
                badges.append((f"{cost.missing_prices} unpriced", "warn", f"{cost.missing_prices} cost lines of run {p.basis.run_number} had no price on record and count as 0"))
            if cost.spin_up:
                badges.append(("spin-up", "warn", "cost history is shorter than the chain depth — some inputs are priced from the first run instead of their true vintage"))
    if p.unpriced_units:
        badges.append((f"{p.unpriced_units} not priced", "warn", f"{p.unpriced_units} units sold by contracts bundled with other items or traded for items — their price cannot be attributed, so they count as units only"))
    if p.pre_history_units and p.pre_history_runs:
        runs = ", ".join(str(r) for r in sorted(p.pre_history_runs))
        badges.append(("pre-history", "", f"{p.pre_history_units} units sold before any priced run had been executed are costed at the earliest priced run (run {runs}), the earliest vintage there is"))
    if p.profit is not None and p.profit < 0:
        badges.append(("negative margin", "fill bad", "net income is below zero at the realized prices"))
    return badges


def top10(products_list: list[Product]) -> dict[str, list]:
    by_margin = sorted(
        [p for p in products_list if p.margin_pct is not None and p.units_priced > 0],
        key=lambda p: (-p.margin_pct, -(p.profit or 0), p.name),
    )[:10]
    by_quantity = sorted(
        [p for p in products_list if p.units_sold > 0],
        key=lambda p: (-p.units_sold, -p.revenue, p.name),
    )[:10]
    by_profit = sorted(
        [p for p in products_list if p.profit is not None and p.units_priced > 0],
        key=lambda p: (-p.profit, -p.units_sold, p.name),
    )[:10]
    return {"margin": by_margin, "quantity": by_quantity, "profit": by_profit}


@dataclass
class Totals:
    revenue: float = 0.0
    net: float = 0.0
    cost: float = 0.0
    net_basis: float = 0.0
    units: int = 0
    products: int = 0
    contracts: int = 0
    contracts_open: int = 0
    no_basis: int = 0
    unpriced_contracts: int = 0

    @property
    def profit(self):
        return self.net_basis - self.cost

    @property
    def margin_pct(self):
        return self.profit / self.cost * 100 if self.cost else None


def totals(products_list, sales, contracts_open: int) -> Totals:
    t = Totals(contracts_open=contracts_open)
    for p in products_list:
        t.revenue += p.revenue
        t.net += p.net
        t.units += p.units_sold
        if p.units_sold:
            t.products += 1
        if p.cost_of_units is not None:
            t.cost += p.cost_of_units
            t.net_basis += p.net
        elif p.units_sold and not p.cost_units and p.unit_cost is None:
            t.no_basis += 1
    seen_contracts = set()
    unpriced = set()
    for s in sales:
        if s.source == "contract" and s.counted:
            seen_contracts.add(s.ref_id)
            if not s.priced:
                unpriced.add(s.ref_id)
    t.contracts = len(seen_contracts)
    t.unpriced_contracts = len(unpriced)
    return t


# -- unrealized profit ------------------------------------------------------


@dataclass
class Unrealized:
    """Hulls on the market but not yet sold: open sell orders (units
    remaining at the listed price) and outstanding item-exchange contracts
    (at their price), after the estimated fees of that venue, minus the
    cost basis. Hangar stock is NOT included (user ruling 2026-09-08). A
    point-in-time figure: it ignores the window."""
    value: float = 0.0        # net proceeds if every listing sells as listed
    cost: float = 0.0         # units × cost basis, priced units with a basis
    units: int = 0            # units counted in value and cost
    units_unpriced: int = 0   # units left out: no cost basis or no attributable price
    orders: int = 0
    contracts: int = 0

    @property
    def profit(self):
        return self.value - self.cost if self.units else None


def unrealized(conn, ref, settings, finals_map, bases, quotes, enabled) -> Unrealized:
    final_ids = set(finals_map)
    u = Unrealized()
    if not final_ids:
        return u
    unit_costs = {t: b.unit_cost for t, b in bases.items()}
    ph = _placeholders(final_ids)
    for o in conn.execute(
        f"SELECT * FROM sale_order WHERE state = 'open' AND type_id IN ({ph})",
        list(final_ids),
    ):
        if (o["owner_kind"], o["owner_id"]) not in enabled or o["volume_remain"] <= 0:
            continue
        u.orders += 1
        uc = unit_costs.get(o["type_id"])
        if uc is None:
            u.units_unpriced += o["volume_remain"]
            continue
        u.value += _net_for(ref, settings, o["type_id"], o["price"], o["volume_remain"],
                            o["location_id"])
        u.cost += o["volume_remain"] * uc
        u.units += o["volume_remain"]
    for c in conn.execute(
        f"SELECT c.* FROM sale_contract c WHERE c.type = 'item_exchange' "
        f"AND c.status IN ({_placeholders(OPEN_CONTRACT_STATUSES)}) AND c.items_status = 'ok' "
        f"AND EXISTS (SELECT 1 FROM sale_contract_item i WHERE i.contract_id = c.contract_id "
        f"AND i.is_included = 1 AND i.type_id IN ({ph}))",
        list(OPEN_CONTRACT_STATUSES) + list(final_ids),
    ):
        if (c["owner_kind"], c["owner_id"]) not in enabled:
            continue
        items = [dict(i) for i in conn.execute(
            "SELECT * FROM sale_contract_item WHERE contract_id = ?", (c["contract_id"],)
        )]
        attribution = attribute_contract(c["price"], items, quotes, final_ids)
        if not attribution.per_final:
            continue
        u.contracts += 1
        for type_id, (qty, unit_price) in attribution.per_final.items():
            uc = unit_costs.get(type_id)
            if unit_price is None or uc is None:
                u.units_unpriced += qty
                continue
            u.value += _net_for(ref, settings, type_id, unit_price, qty, c["start_location_id"])
            u.cost += qty * uc
            u.units += qty
    return u


# -- open orders, open contracts, history ----------------------------------


@dataclass
class OpenOrder:
    order_id: int
    type_id: int
    name: str
    volume_remain: int
    volume_total: int
    price: float
    quote: float | None
    quote_note: str
    issued: str
    expires: str
    expires_soon: bool
    owner: str
    system: str | None
    badges: list


def open_orders(conn, ref, settings, finals_map, enabled, quotes_full, quote_age, names, now: datetime) -> list[OpenOrder]:
    final_ids = set(finals_map)
    if not final_ids:
        return []
    out = []
    now_iso = _iso(now)
    soon = _iso(now + timedelta(hours=24))
    for o in conn.execute(
        f"SELECT o.*, l.solar_system_id FROM sale_order o "
        f"LEFT JOIN location_system l ON l.location_id = o.location_id "
        f"WHERE o.state = 'open' AND o.type_id IN ({_placeholders(final_ids)}) "
        f"ORDER BY o.issued DESC",
        list(final_ids),
    ):
        if (o["owner_kind"], o["owner_id"]) not in enabled:
            continue
        # Compare with the book the order actually sits in: the hub cache
        # for an NPC station, the structure cache for the configured
        # structure market, nothing for any other structure.
        venue = costing.sale_venue(o["location_id"])
        if venue == costing.SALE_VENUE_HUB:
            price = market.cached_prices(
                conn, settings.price_region_id, [o["type_id"]], settings.price_source
            ).get(o["type_id"])
            sell_side = settings.price_source == "sell"
            book = "Jita"
        elif o["location_id"] == settings.structure_market():
            price = market.cached_prices(
                conn, settings.structure_market(), [o["type_id"]], market.STRUCTURE_SOURCE
            ).get(o["type_id"])
            sell_side = True
            book = settings.structure_market_label()
        else:
            price, sell_side, book = None, True, None
        badges = []
        quote_note = ""
        if book is None:
            badges.append(("no quote", "", "this order sits in a structure whose book Magoo does not cache — no undercut verdict"))
        elif price is None:
            badges.append(("no quote", "warn", f"no cached {book} quote for this product — refresh prices"))
        elif not sell_side:
            quote_note = f"best buy (Max Buy basis — not comparable)"
        elif o["price"] > price:
            badges.append((
                "undercut", "warn",
                f"listed at {o['price']:,.2f}; the cached best {book} sell is {price:,.2f}"
                f" (quotes {quote_age.get(o['type_id']) or 'of unknown age'}) — lower the order or wait",
            ))
        if o["missing_since"]:
            badges.append(("unconfirmed", "warn", f"not listed by ESI's open or history feed since {o['missing_since']} — cache skew, resolves within the hour"))
        if o["volume_remain"] < o["volume_total"]:
            badges.append(("partial", "", f"{o['volume_total'] - o['volume_remain']} of {o['volume_total']} sold so far"))
        try:
            expires = _iso(_parse_ts(o["issued"]) + timedelta(days=int(o["duration"] or 0)))
        except (ValueError, TypeError):
            expires = ""
        out.append(OpenOrder(
            o["order_id"], o["type_id"], _type_name(ref, o["type_id"]), o["volume_remain"],
            o["volume_total"], o["price"], price, quote_note, o["issued"], expires,
            bool(expires and now_iso <= expires <= soon), _owner_label(names, o["owner_kind"], o["owner_id"]),
            _system_name(ref, o["solar_system_id"]), badges,
        ))
    return out


@dataclass
class OpenContract:
    contract_id: int
    products: str
    quantity: int
    price: float | None
    issued: str
    expires: str
    expired: bool
    owner: str
    title: str | None


def open_contracts(conn, ref, finals_map, enabled, names, now: datetime) -> list[OpenContract]:
    final_ids = set(finals_map)
    if not final_ids:
        return []
    ph = _placeholders(final_ids)
    out = []
    now_iso = _iso(now)
    for c in conn.execute(
        f"SELECT c.* FROM sale_contract c WHERE c.type = 'item_exchange' "
        f"AND c.status IN ({_placeholders(OPEN_CONTRACT_STATUSES)}) AND EXISTS ("
        f"SELECT 1 FROM sale_contract_item i WHERE i.contract_id = c.contract_id "
        f"AND i.is_included = 1 AND i.type_id IN ({ph})) ORDER BY c.date_issued DESC",
        list(OPEN_CONTRACT_STATUSES) + list(final_ids),
    ):
        if (c["owner_kind"], c["owner_id"]) not in enabled:
            continue
        items = conn.execute(
            f"SELECT type_id, SUM(quantity) AS q FROM sale_contract_item "
            f"WHERE contract_id = ? AND is_included = 1 AND type_id IN ({ph}) GROUP BY type_id",
            [c["contract_id"]] + list(final_ids),
        ).fetchall()
        out.append(OpenContract(
            c["contract_id"],
            ", ".join(f"{i['q']}× {_type_name(ref, i['type_id'])}" for i in items),
            sum(i["q"] for i in items), c["price"], c["date_issued"], c["date_expired"],
            c["date_expired"] < now_iso, _owner_label(names, c["owner_kind"], c["owner_id"]),
            c["title"],
        ))
    return out


@dataclass
class HistoryRow:
    order_id: int
    name: str
    sold: int
    volume_total: int
    price: float
    outcome: str
    closed: str | None
    owner: str


def order_history(conn, ref, finals_map, enabled, window: Window, names) -> list[HistoryRow]:
    final_ids = set(finals_map)
    if not final_ids:
        return []
    params: list = list(final_ids)
    clause = ""
    if window.since_ts:
        clause = " AND o.history_seen_at >= ?"
        params.append(window.since_ts)
    out = []
    for o in conn.execute(
        f"SELECT * FROM sale_order o WHERE o.state != 'open' "
        f"AND o.type_id IN ({_placeholders(final_ids)}){clause} "
        f"ORDER BY o.history_seen_at DESC, o.order_id DESC",
        params,
    ):
        if (o["owner_kind"], o["owner_id"]) not in enabled:
            continue
        out.append(HistoryRow(
            o["order_id"], _type_name(ref, o["type_id"]),
            o["volume_total"] - o["volume_remain"], o["volume_total"], o["price"],
            order_outcome(o["state"], o["volume_total"], o["volume_remain"]),
            o["history_seen_at"], _owner_label(names, o["owner_kind"], o["owner_id"]),
        ))
    return out


# -- degrade notes ----------------------------------------------------------


def feed_overlap_suspects(conn, finals_map, window: Window) -> int:
    """Tripwire for the assumption that one sale carries the same
    transaction_id on both feeds: identical sales under two ids."""
    final_ids = set(finals_map)
    if not final_ids:
        return 0
    params: list = list(final_ids)
    clause = ""
    if window.since_ts:
        clause = " AND date >= ?"
        params.append(window.since_ts)
    row = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM sale_transaction "
        f"WHERE type_id IN ({_placeholders(final_ids)}){clause} "
        f"GROUP BY owner_kind, owner_id, type_id, date, quantity, unit_price, location_id "
        f"HAVING COUNT(DISTINCT source_feed) > 1 AND COUNT(*) > 1)",
        params,
    ).fetchone()
    return row[0]


def degrade_notes(conn, ref, finals_map, window: Window, enabled, sales, names) -> list[str]:
    notes: list[str] = []
    pool = {r["character_id"]: r for r in store.pool_characters(conn)}
    tokens = {r["character_id"]: r["scopes"] for r in conn.execute("SELECT character_id, scopes FROM esi_token")}
    owners = current_owners(conn)
    relogin = set()
    for cid, row in pool.items():
        missing = esi.missing_scopes(tokens.get(cid))
        if not missing:
            continue
        relogin.add(cid)
        own = sorted({
            FAMILY_LABEL[family]
            for (kind, family), scope in esi.LEDGER_SCOPES.items()
            if kind == "character" and scope in missing
        }, key=list(FAMILY_LABEL.values()).index)
        corp = sorted({
            FAMILY_LABEL[family]
            for (kind, family), scope in esi.LEDGER_SCOPES.items()
            if kind == "corporation" and scope in missing
        }, key=list(FAMILY_LABEL.values()).index)
        effects = []
        if own:
            effects.append(f"its {' and '.join(own)} are not read")
        if corp:
            effects.append(f"it cannot read corporation {' or '.join(corp)}")
        notes.append(
            f"{row['character_name']} — re-login needed (lacks: {', '.join(missing)}) — "
            + "; ".join(effects)
        )
    for (kind, oid), rows in store.sales_pull_state(conn).items():
        if (kind, oid) not in owners:
            continue  # a removed character or a corporation every member left
        label = _owner_label(names, kind, oid)
        seen = set()
        for r in rows:
            if r["status"] in ("ok", "off"):
                continue
            if r["status"] == "no_scope" and (
                (kind == "character" and oid in relogin) or (kind == "corporation" and relogin)
            ):
                continue  # the re-login line above already says what to do
            key = (r["family"], r["status"], r["message"])
            if key in seen:
                continue
            seen.add(key)
            notes.append(
                f"{label} — {FAMILY_LABEL[r['family']]}: {r['status']} "
                f"({r['message'] or 'no detail'}) — "
                + {
                    "orders": "its open orders and order history are not current",
                    "transactions": "some of its sales may be missing",
                    "contracts": "its contract sales may be missing",
                }[r["family"]]
            )
    off_owners = {(s.owner_kind, s.owner_id) for s in sales if "sales off" in s.flags}
    for kind, oid in sorted(off_owners):
        notes.append(
            f"{_owner_label(names, kind, oid)} — sales off (ESI tab) — its rows are stored but not counted"
        )
    final_ids = set(finals_map)
    if final_ids:
        ph = _placeholders(final_ids)
        n = conn.execute(
            "SELECT COUNT(*) FROM sale_contract WHERE type = 'item_exchange' "
            "AND status = 'finished' AND price > 0 AND items_status IN ('missing', 'unavailable')"
        ).fetchone()[0]
        if n:
            notes.append(
                f"{n} finished contracts have no item list from ESI — their price is not attributed"
            )
        n = conn.execute(
            f"SELECT COUNT(*) FROM sale_contract c WHERE c.type = 'auction' AND c.status = 'finished' "
            f"AND EXISTS (SELECT 1 FROM sale_contract_item i WHERE i.contract_id = c.contract_id "
            f"AND i.is_included = 1 AND i.type_id IN ({ph}))",
            list(final_ids),
        ).fetchone()[0]
        if n:
            notes.append(f"{n} finished auctions are not read — the winning bid is unavailable")
    n = feed_overlap_suspects(conn, finals_map, window)
    if n:
        notes.append(f"{n} sales appear under two ids — verify feed identity")
    return notes


# -- charts -----------------------------------------------------------------


def chart_data(sales, products_by_type, window: Window, since: date, until: date, fmt_isk, fmt_qty):
    step = charts.step_days((until - since).days + 1)
    edges = charts.bucket_edges(since, until, step)
    n = len(edges)
    labels = charts.x_labels(edges, step)
    revenue = [0.0] * n
    profit = [0.0] * n
    units_market = [0.0] * n
    units_contract = [0.0] * n
    for s in sales:
        if not s.counted:
            continue
        try:
            idx = charts.bucket_of(s.when, since, step)
        except ValueError:
            continue
        if not 0 <= idx < n:
            continue
        if s.source == "market":
            units_market[idx] += s.quantity
        else:
            units_contract[idx] += s.quantity
        if s.priced:
            revenue[idx] += s.gross
            if s.profit is not None:
                profit[idx] += s.profit
    cumulative = []
    running = 0.0
    for v in profit:
        running += v
        cumulative.append(running)
    caption_step = {1: "daily buckets", 7: "7-day buckets", 30: "30-day buckets"}[step]
    if (until - since).days + 1 != n * step:
        caption_step += ", last one partial"
    return [
        charts.build_chart(
            "Revenue & net income", labels,
            [("revenue", "accent", "bar", revenue), ("net income", "good", "bar", profit)],
            fmt=fmt_isk, caption=f"{caption_step} · net income covers products with a cost basis only",
            x_dates=edges,
        ),
        charts.build_chart(
            "Units sold", labels,
            [("market", "accent", "stack", units_market), ("contract", "dim", "stack", units_contract)],
            fmt=fmt_qty, caption=caption_step, x_dates=edges,
        ),
        charts.build_chart(
            "Cumulative net income", labels,
            [("cumulative", "good", "line", cumulative)],
            fmt=fmt_isk, caption=f"{caption_step} · products with a cost basis", x_dates=edges,
        ),
    ]


# -- the view ---------------------------------------------------------------


def _default_isk(v) -> str:
    return f"{v:,.0f} ISK"


def _default_qty(v) -> str:
    return f"{v:,.0f}"


def build_view(conn, ref, settings, window, now: datetime | None = None,
               fmt_isk=None, fmt_qty=None, show_all_rows: bool = False) -> dict:
    """Everything ledger.html needs, recomputed on every load; nothing
    here writes."""
    now = now or datetime.now(timezone.utc)
    fmt_isk = fmt_isk or _default_isk
    fmt_qty = fmt_qty or _default_qty
    win = window_bounds(window, now)
    finals_map = finals(conn)
    names = owner_names(conn)
    enabled = enabled_owners(conn)
    internal = internal_ids(conn)
    quotes_full = {t: final_quote(conn, ref, settings, t) for t in finals_map}
    quotes = {t: q[0] for t, q in quotes_full.items()}
    # v1.27.1: every sale is costed at the latest run executed on or
    # before it (CostVintages.at); the unsold listings and the products
    # table's cost-per-unit column read the current basis.
    vintages = CostVintages(conn, ref, settings, finals_map)
    bases = vintages.latest_map()
    sales = sales_rows(conn, ref, settings, finals_map, win, internal, quotes, enabled, vintages, names)
    _hub_n, hub_at = market.price_cache_state(conn, settings.price_region_id, settings.price_source)
    _s_n, structure_at = market.structure_cache_state(conn, settings.capital_structure())
    quote_age = {
        t: (structure_at if q[2] else hub_at) for t, q in quotes_full.items()
    }
    prods = products(conn, ref, settings, finals_map, sales, bases, quotes_full, win,
                     quote_age=quote_age)
    contracts_open_rows = open_contracts(conn, ref, finals_map, enabled, names, now)
    tot = totals(prods, sales, len(contracts_open_rows))
    unreal = unrealized(conn, ref, settings, finals_map, bases, quotes, enabled)
    orders = open_orders(conn, ref, settings, finals_map, enabled, quotes_full,
                         {t: (f"from {a}" if a else None) for t, a in quote_age.items()}, names, now)
    history = order_history(conn, ref, finals_map, enabled, win, names)
    notes = degrade_notes(conn, ref, finals_map, win, enabled, sales, names)
    counted = [s for s in sales if s.counted]
    if win.since is not None:
        since = win.since
    elif counted:
        since = min(_parse_ts(s.when).date() for s in counted)
    else:
        since = win.until
    chart_list = chart_data(sales, {p.type_id: p for p in prods}, win, since, win.until, fmt_isk, fmt_qty)
    pulled = conn.execute("SELECT MAX(pulled_at) FROM sales_pull").fetchone()[0]
    ever_pulled = conn.execute("SELECT COUNT(*) FROM sales_pull").fetchone()[0] > 0
    owners_pulled = {
        (r["owner_kind"], r["owner_id"])
        for r in conn.execute("SELECT DISTINCT owner_kind, owner_id FROM sales_pull")
    } & current_owners(conn)
    if ever_pulled and not (owners_pulled & enabled):
        notes.append(
            "Count sales is off for every character and corporation (ESI tab) — "
            "nothing is read"
        )
    rows_total = len(sales)
    shown = sales if show_all_rows else sales[:LEDGER_ROWS_SHOWN]
    return {
        "window": win,
        "windows": WINDOWS,
        "since": since,
        "sales_at": pulled,
        "ever_pulled": ever_pulled,
        "owners_count": len(owners_pulled & enabled),
        "has_pipelines": bool(finals_map),
        "notes": notes,
        "totals": tot,
        "unrealized": unreal,
        "products": prods,
        "top": top10(prods),
        "charts": chart_list,
        "sales": shown,
        "sales_total": rows_total,
        "sales_truncated": rows_total > len(shown),
        "open_orders": orders,
        "open_contracts": contracts_open_rows,
        "history": history,
        "bases": bases,
        "hub_at": hub_at,
        "structure_at": structure_at,
        "counted_sales": len(counted),
    }
