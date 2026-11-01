"""Alchemy for-sale holds, the Ledger side (schema 14, user rulings
2026-10-31, plan D4 / D7 after the review of the same day):
ledger.settle_alchemy_holds matching the observed sales of held composites
to the holds whose for-sale jobs ESI shows DELIVERED — delivered-only,
each hold's own window (first delivery to expiry, contracts by
date_issued), oldest run first, idempotent upserts that let a growing
pile take the rest of a sale onto the same row, the by-hand shrink, the
transaction discipline and the write lock under two connections, the
finals' guard (a hold on a pipeline's final releases by hand alone) and
the expired hold's closing bound — ledger.listed_units, and
ledger.alchemy_sales_view's money against independently computed figures.

Same fixture pattern as test_alchemy_hold (real reference data, temp state
DB); its world builders are imported, never redefined. Worlds are seeded
by hand (seed_run / seed_hold / install_jobs / deliver_jobs): explicit
windows, dates well apart, no plan in the loop.
"""

import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from magoo import config, costing, holds, ledger, store
from magoo.refdata import Refdata
from test_alchemy import add_pipeline, conn  # noqa: F401 — conn is the fixture
from test_alchemy_hold import (
    T0,
    complete_run,
    deliver_jobs,
    install_jobs,
    reaction_job,
    seed_hold,
    seed_run,
)
from test_alchemy_profit import net_sale
from test_ledger import A, B, BUYER, HULK, PLAYER_CORP, STATION, TRACKED, add_corp, add_owner, seed_quote

# An Upwell structure's ESI location id: at or above
# costing.STRUCTURE_LOCATION_MIN, so a sale there pays the structure rates.
STRUCTURE = 1_030_000_000_000
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


# --- helpers ---------------------------------------------------------------------


def _iso(when: datetime) -> str:
    """ESI's timestamp shape, the one every sales table and the settle's
    text comparisons use."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _qty(units: int, route, yield_: float = 0.55) -> int:
    """D1's conversion, written out: composite units of `units` unrefined
    at the yield."""
    return math.floor(units * route.composite_qty * yield_)


def _route(ref, composite_name: str):
    return ref.alchemy_routes()[ref.type_id(composite_name)]


def _sellers(conn):
    """Character A sells with Count sales on; its station is in Jita."""
    add_owner(conn, A)
    conn.execute(
        "INSERT OR IGNORE INTO location_system (location_id, solar_system_id) VALUES (?, ?)",
        (STATION, TRACKED),
    )
    conn.commit()


def _quote(conn, type_id, price):
    """Today's hub quote of `type_id`, where ledger.final_quote reads it."""
    settings = store.get_settings(conn)
    seed_quote(conn, type_id, price, region=settings.price_region_id, source=settings.price_source)


@dataclass
class Pile:
    """One run's hold as the record shows it: its for-sale jobs, their
    composite quantity at the yield (computed here, not read back), and
    the dates the tests reason about."""

    run_id: int
    run_number: int
    composite_id: int
    qty: int
    job_ids: list
    start: datetime
    end: datetime
    delivered: datetime | None = None


def _pile(conn, route, run_id, run_number, *, jobs=1, per_job=10, cost=50.0, first_job_id=90000,
          deliver=True, status="active") -> Pile:
    """A hold on `run_id` (chain_units 0: every installed job is the
    hold's) with `jobs` for-sale jobs of `per_job` units installed in the
    run's window, delivered a day after their timer ends unless `deliver`
    is False."""
    seed_hold(conn, run_id, route.composite_id, route_unit_cost=cost)
    start, end = install_jobs(
        conn, run_id, route.unrefined_id, jobs, per_job, status=status, first_job_id=first_job_id
    )
    ids = list(range(first_job_id, first_job_id + jobs))
    delivered = None
    if deliver:
        delivered = end + DAY
        deliver_jobs(conn, ids, delivered)
    return Pile(run_id, run_number, route.composite_id, _qty(jobs * per_job, route), ids, start, end, delivered)


def _one_run(conn, route, **kwargs) -> Pile:
    """Run 1 open at T0 holding `route`, its pile delivered."""
    return _pile(conn, route, seed_run(conn, 1, T0), 1, **kwargs)


def _two_runs(conn, route, *, cost1=50.0, cost2=60.0, per_job=10):
    """Run 1 holding `route`, delivered, then executed a day later; run 2
    opened at that moment, holding the same composite, delivered a month
    after. Returns (pile1, pile2, c1) — c1 run 1's completed_at."""
    p1 = _one_run(conn, route, cost=cost1, per_job=per_job)
    c1 = p1.delivered + DAY
    complete_run(conn, p1.run_id, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1 + HOUR)  # the window covered
    run2 = seed_run(conn, 2, c1)
    p2 = _pile(conn, route, run2, 2, cost=cost2, per_job=per_job, first_job_id=92000)
    return p1, p2, c1


def _sell(conn, tid, type_id, qty, when, price=30_000.0, *, owner=("character", A), client=BUYER,
          location=STATION):
    """One wallet sell transaction dated `when`."""
    conn.execute(
        "INSERT INTO sale_transaction (transaction_id, owner_kind, owner_id, division, source_feed, "
        "type_id, quantity, unit_price, date, location_id, client_id, journal_ref_id, fetched_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (tid, owner[0], owner[1], owner[0], type_id, qty, price, _iso(when), location, client, _iso(when)),
    )
    conn.commit()


def _contract(conn, cid, items, price, issued, completed=None, *, status="finished", kind="item_exchange",
              acceptor=BUYER, owner=("character", A), items_status="ok", location=STATION, asked=()):
    """A contract we issued: `items` [(type_id, qty), ...] included,
    `asked` [(type_id, qty), ...] wanted back, dated issued / completed
    (completed None: not finished yet)."""
    done = _iso(completed) if completed is not None else None
    conn.execute(
        "INSERT INTO sale_contract (contract_id, owner_kind, owner_id, issuer_id, issuer_corporation_id, "
        "for_corporation, acceptor_id, type, status, price, date_issued, date_accepted, date_completed, "
        "date_expired, start_location_id, first_seen_at, last_seen_at, items_fetched_at, items_status) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, owner[0], owner[1], A, PLAYER_CORP, acceptor, kind, status, price, _iso(issued), done, done,
         _iso(issued + 14 * DAY), location, _iso(issued), _iso(issued), _iso(issued), items_status),
    )
    rows = [(cid, n + 1, t, q, 1) for n, (t, q) in enumerate(items)]
    rows += [(cid, len(rows) + n + 1, t, q, 0) for n, (t, q) in enumerate(asked)]
    conn.executemany(
        "INSERT INTO sale_contract_item (contract_id, record_id, type_id, quantity, raw_quantity, "
        "is_included, is_singleton) VALUES (?, ?, ?, ?, NULL, ?, 0)",
        rows,
    )
    conn.commit()


def _order(conn, order_id, owner_id, type_id, remain, state="open", *, total=None):
    """One sell order of `owner_id`: `remain` units still on the book of
    `total` listed (every unit, unless given — a partly filled order)."""
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, "
        "volume_total, volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at) "
        "VALUES (?, 'character', ?, 'character', ?, 30000.0, ?, ?, ?, 90, '2026-10-01T00:00:00Z', ?, "
        "'2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
        (order_id, owner_id, type_id, total if total is not None else remain, remain, STATION, state),
    )
    conn.commit()


def _settle(conn, ref) -> int:
    """settle_alchemy_holds, with the invariant every caller relies on: no
    transaction is left open behind it."""
    matched = ledger.settle_alchemy_holds(conn, ref, store.get_settings(conn))
    assert not conn.in_transaction
    return matched


def _releases(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM alchemy_hold_release ORDER BY release_id")]


def _by_run(conn) -> dict[int, int]:
    """Released units per run."""
    return {
        r["index_run_id"]: int(r["q"])
        for r in conn.execute("SELECT index_run_id, SUM(qty) AS q FROM alchemy_hold_release GROUP BY 1")
    }


def _hold(conn, ref, run_id, composite_id) -> holds.Hold:
    [hold] = [
        h for h in holds.load(conn, ref, store.get_settings(conn), run_id=run_id)
        if h.composite_type_id == composite_id
    ]
    return hold


def _view(conn, ref, window="all", now=None) -> dict:
    now = now or datetime(2027, 1, 1, tzinfo=timezone.utc)
    return ledger.alchemy_sales_view(conn, ref, store.get_settings(conn), ledger.window_bounds(window, now))


def _net_hub(conn, ref, type_id, price, qty) -> float:
    """What the Ledger should book for a station sale, through the fee
    function the whole app shares."""
    return ledger._net_for(ref, store.get_settings(conn), type_id, price, qty, STATION)


# --- delivered-only ----------------------------------------------------------------


def test_an_undelivered_hold_takes_nothing(conn, ref):
    """Ruling (e): an observed sale releases only a hold whose for-sale
    jobs ESI shows delivered. In flight and 'ready' (finished, not in the
    hangar) take nothing; once delivered, only a sale dated from the
    delivery on counts."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route, deliver=False)
    comp = route.composite_id
    _sell(conn, 9001, comp, 10, pile.start + 5 * DAY)
    assert _settle(conn, ref) == 0 and _releases(conn) == []
    store.save_industry_jobs(
        conn,
        [reaction_job(pile.job_ids[0], route.unrefined_id, pile.start, 10, "ready", end_date=pile.end)],
        pulled_at=pile.end + HOUR,
    )
    hold = _hold(conn, ref, pile.run_id, comp)
    assert hold.in_flight and hold.delivered_units == 0 and hold.live
    assert _settle(conn, ref) == 0 and _releases(conn) == []
    delivered = pile.end + DAY
    deliver_jobs(conn, pile.job_ids, delivered)
    _sell(conn, 9002, comp, 10, delivered)  # dated at the delivery itself: the window is closed at both ends
    assert _settle(conn, ref) == 10  # 9001 predates the delivery and stays unassigned
    [row] = _releases(conn)
    assert (row["source"], row["ref_id"], row["qty"], row["index_run_id"]) == ("market", 9002, 10, pile.run_id)
    assert store.alchemy_released_qty(conn, "market", 9001) == 0


# --- each hold's own window ----------------------------------------------------------


def test_a_sale_between_two_deliveries_goes_to_the_delivered_hold_only(conn, ref):
    """D4's window: a sale counts against a hold iff first_delivered <=
    date <= expired_at. Run 1 and run 2 hold one composite, delivered a
    month apart: a sale before the first delivery matches nothing; a sale
    between the two goes to run 1 and its excess stays unassigned (run
    2's pile is not delivered at that date); a sale after both lands on
    the oldest hold with room."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    _sell(conn, 9001, comp, 10, p1.delivered - HOUR)
    _sell(conn, 9002, comp, p1.qty + 50, p1.delivered + DAY)
    assert p1.delivered + DAY < p2.delivered
    assert _settle(conn, ref) == p1.qty
    assert _by_run(conn) == {p1.run_id: p1.qty}
    assert store.alchemy_released_qty(conn, "market", 9001) == 0
    assert store.alchemy_released_qty(conn, "market", 9002) == p1.qty
    _sell(conn, 9003, comp, 30, p2.delivered + 12 * HOUR)
    assert _settle(conn, ref) == 30
    assert _by_run(conn) == {p1.run_id: p1.qty, p2.run_id: 30}
    assert store.alchemy_released_qty(conn, "market", 9002) == p1.qty  # the excess is still nobody's
    assert _hold(conn, ref, p2.run_id, comp).remaining == p2.qty - 30


def test_a_sale_after_a_holds_expiry_is_left_unassigned(conn, ref):
    """Ruling (b) + (e): after the second Mark executed following the
    delivery the hold is over (returned to stock), and a sale dated after
    that moment is an ordinary chain-stock sale — no hold takes it."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1 = _one_run(conn, route)
    c1 = p1.delivered + DAY
    complete_run(conn, p1.run_id, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1 + HOUR)  # run 1's window covered
    run2 = seed_run(conn, 2, c1)
    c2 = c1 + 30 * DAY
    complete_run(conn, run2, c2)
    store.save_industry_jobs(conn, [], pulled_at=c2 + HOUR)  # run 2's too: no uncovered-window rule in play
    hold = _hold(conn, ref, p1.run_id, route.composite_id)
    assert not hold.live and hold.expired_at == c2 and hold.status == "returned to stock"
    _sell(conn, 9001, route.composite_id, 10, c2 + DAY)
    assert _settle(conn, ref) == 0 and _releases(conn) == []
    assert _hold(conn, ref, p1.run_id, route.composite_id).remaining == p1.qty


def test_a_sale_pulled_after_the_next_mark_executed_still_lands_on_its_run(conn, ref):
    """Review 2026-10-31: the settle keys on the sale's DATE, so a sale
    the Ledger pulls late — after run 1's hold has ended — still lands on
    run 1 when it is dated inside run 1's life, while a sale dated after
    the expiry goes to run 2's live hold."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    c2 = p2.delivered + DAY
    complete_run(conn, p2.run_id, c2)  # the second Mark executed after run 1's delivery
    store.save_industry_jobs(conn, [], pulled_at=c2 + HOUR)  # run 2's window covered: the record speaks for both
    h1, h2 = (_hold(conn, ref, p.run_id, comp) for p in (p1, p2))
    assert not h1.live and h1.expired_at == c2
    assert h2.live and h2.expires_after == 3
    _sell(conn, 9001, comp, 40, p1.delivered + 2 * DAY)  # inside run 1's life, pulled only now
    _sell(conn, 9002, comp, 30, c2 + DAY)  # after run 1's expiry
    _sell(conn, 9003, comp, 5, c2)  # dated at the expiry itself: still inside run 1's life
    assert _settle(conn, ref) == 75
    assert _by_run(conn) == {p1.run_id: 45, p2.run_id: 30}
    rows = {r["ref_id"]: r for r in _releases(conn)}
    assert rows[9001]["index_run_id"] == rows[9003]["index_run_id"] == p1.run_id
    assert rows[9002]["index_run_id"] == p2.run_id


def test_a_contract_is_dated_by_its_issue(conn, ref):
    """D4: a contract's items leave the hangar when it is issued, so the
    window test reads date_issued (issued before the delivery, finished
    after: not the pile's) while the release keeps the completion date."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route)
    comp = route.composite_id
    _contract(conn, 9201, [(comp, 20)], 600_000.0, pile.delivered - DAY, pile.delivered + 2 * DAY)
    _contract(conn, 9202, [(comp, 20)], 600_000.0, pile.delivered + DAY, pile.delivered + 3 * DAY)
    assert _settle(conn, ref) == 20
    [row] = _releases(conn)
    assert (row["source"], row["ref_id"], row["qty"]) == ("contract", 9202, 20)
    assert row["unit_price"] == pytest.approx(30_000.0)
    assert row["released_at"] == _iso(pile.delivered + 3 * DAY)
    assert store.alchemy_released_qty(conn, "contract", 9201) == 0


def test_two_sales_against_one_hold_in_one_call_share_its_room(conn, ref):
    """The room a sale took is seen by the next sale of the same call
    (the running total): the earlier-dated sale is served first, the
    later one gets what is left, and the rest stays unassigned for
    later settles, which find nothing new."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route)
    comp = route.composite_id
    _sell(conn, 9001, comp, pile.qty - 5, pile.delivered + DAY)
    _sell(conn, 9002, comp, 20, pile.delivered + 2 * DAY)
    assert _settle(conn, ref) == pile.qty
    rows = {r["ref_id"]: r["qty"] for r in _releases(conn)}
    assert rows == {9001: pile.qty - 5, 9002: 5}
    hold = _hold(conn, ref, pile.run_id, comp)
    assert hold.observed_qty == pile.qty and hold.remaining == 0 and hold.status == "sold"
    assert _settle(conn, ref) == 0
    assert store.alchemy_released_qty(conn, "market", 9002) == 5


def test_two_composites_held_at_once_release_their_own_holds_only(conn, ref):
    """One run holding two composites: a sale of either releases that
    composite's hold and touches the other's nothing."""
    ferro, hyper = _route(ref, "Ferrofluid"), _route(ref, "Hyperflurite")
    _sellers(conn)
    run1 = seed_run(conn, 1, T0)
    pf = _pile(conn, ferro, run1, 1, cost=50.0)
    ph = _pile(conn, hyper, run1, 1, cost=70.0, first_job_id=91000)
    _sell(conn, 9001, ferro.composite_id, 30, pf.delivered + DAY)
    _sell(conn, 9002, hyper.composite_id, 45, ph.delivered + DAY)
    assert _settle(conn, ref) == 75
    rows = {(r["ref_id"], r["composite_type_id"]): r for r in _releases(conn)}
    assert set(rows) == {(9001, ferro.composite_id), (9002, hyper.composite_id)}
    assert rows[(9001, ferro.composite_id)]["qty"] == 30 and rows[(9001, ferro.composite_id)]["unit_cost"] == 50.0
    assert rows[(9002, hyper.composite_id)]["qty"] == 45 and rows[(9002, hyper.composite_id)]["unit_cost"] == 70.0
    assert store.alchemy_released_qty(conn, "market", 9001, hyper.composite_id) == 0
    assert _hold(conn, ref, run1, ferro.composite_id).remaining == pf.qty - 30
    assert _hold(conn, ref, run1, hyper.composite_id).remaining == ph.qty - 45


def test_a_contract_with_two_held_composites_and_a_final_is_priced_pro_rata(conn, ref):
    """Review #7: final_ids = the held composites and the finals, one
    quotes map per settle, so a contract bundling two held composites
    with a Hulk is CLEAN — each composite's units are priced pro rata by
    quote × qty (its weight over the three weights, per unit) and both
    holds are released. The Hulk is the finals' Ledger's, where the same
    contract reads as MIXED and is booked as units only — pinned below as
    a design gap."""
    ferro, hyper = _route(ref, "Ferrofluid"), _route(ref, "Hyperflurite")
    _sellers(conn)
    add_pipeline(conn, ref, "Hulk", 8)
    run1 = seed_run(conn, 1, T0)
    pf = _pile(conn, ferro, run1, 1)
    ph = _pile(conn, hyper, run1, 1, first_job_id=91000)
    q_f, q_h, q_hulk = 30_000.0, 20_000.0, 300e6
    _quote(conn, ferro.composite_id, q_f)
    _quote(conn, hyper.composite_id, q_h)
    _quote(conn, HULK, q_hulk)
    price = 400e6
    issued = max(pf.delivered, ph.delivered) + DAY
    _contract(conn, 9301, [(ferro.composite_id, 10), (hyper.composite_id, 20), (HULK, 1)], price, issued, issued + DAY)
    w_f, w_h, w_hulk = 10 * q_f, 20 * q_h, 1 * q_hulk  # quote × qty, from the seeded quotes
    total = w_f + w_h + w_hulk
    assert _settle(conn, ref) == 30
    rows = {r["composite_type_id"]: r for r in _releases(conn)}
    assert set(rows) == {ferro.composite_id, hyper.composite_id}
    assert rows[ferro.composite_id]["qty"] == 10
    assert rows[ferro.composite_id]["unit_price"] == pytest.approx(price * w_f / total / 10)
    assert rows[hyper.composite_id]["qty"] == 20
    assert rows[hyper.composite_id]["unit_price"] == pytest.approx(price * w_h / total / 20)
    assert all(r["source"] == "contract" and r["ref_id"] == 9301 and r["unit_cost"] == 50.0 for r in rows.values())
    assert _hold(conn, ref, run1, ferro.composite_id).observed_qty == 10
    assert _hold(conn, ref, run1, hyper.composite_id).observed_qty == 20
    # DESIGN GAP (2026-10-31): the finals' view (ledger.sales_rows) attributes
    # with final_ids = the finals ALONE, so there the two held composites are
    # "other" items and the contract is mixed — the Hulk is booked as units
    # only (no price, no revenue, an unpriced contract in the totals), while
    # the settle above priced the composites as if the Hulk took its own
    # share, price × w_hulk / total. Nothing books that share anywhere.
    view = ledger.build_view(conn, ref, store.get_settings(conn), "all", issued + 10 * DAY)
    [hulk] = [s for s in view["sales"] if s.ref_id == 9301]
    assert (hulk.type_id, hulk.quantity, hulk.source, hulk.counted, hulk.priced) == (HULK, 1, "contract", True, False)
    assert (hulk.unit_price, hulk.gross, hulk.net, hulk.profit) == (None, None, None, None)
    assert hulk.flags == ("mixed",) and hulk.contract_price == price
    assert "10× Ferrofluid" in hulk.detail and "20× Hyperflurite" in hulk.detail
    [product] = view["products"]
    assert (product.type_id, product.units_sold, product.units_priced, product.contract_units) == (HULK, 1, 0, 1)
    assert (product.revenue, product.net, product.unpriced_units) == (0.0, 0.0, 1)
    assert (view["totals"].net, view["totals"].revenue, view["totals"].units) == (0.0, 0.0, 1)
    assert (view["totals"].contracts, view["totals"].unpriced_contracts) == (1, 1)


def test_a_bundled_composite_without_a_quote_is_released_unpriced(conn, ref):
    """Settle (5), 2026-10-31: attribute_contract's "estimated split" — an
    item of a multi-type contract has no cached quote — is an equal-per-
    unit guess the finals accept between two hulls, not beside a composite
    (a tenth of the Hulk's price per Ferrofluid unit here), so the
    composite's units are released with unit_price NULL and the Ledger
    shows the row unpriced: net None, nothing in totals.net, its units in
    unpriced_units. A contract of ONE type needs no quote — price / units
    is exact, as test_a_contract_is_dated_by_its_issue's 30,000 already
    shows with no quote on record; pinned again beside the bundle."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    add_pipeline(conn, ref, "Hulk", 8)
    pile = _one_run(conn, route, cost=50.0)
    comp = route.composite_id
    _quote(conn, HULK, 300e6)  # the Hulk quoted, Ferrofluid never
    when = pile.delivered + DAY
    price = 330e6
    _contract(conn, 9301, [(comp, 10), (HULK, 1)], price, when, when + DAY)
    _contract(conn, 9302, [(comp, 20)], 600_000.0, when + HOUR, when + DAY)
    assert _settle(conn, ref) == 30
    rows = {r["ref_id"]: r for r in _releases(conn)}
    assert rows[9301]["qty"] == 10 and rows[9301]["unit_price"] is None  # not the guess, price / 11 per unit
    assert rows[9302]["qty"] == 20 and rows[9302]["unit_price"] == pytest.approx(600_000.0 / 20)
    assert rows[9301]["unit_cost"] == rows[9302]["unit_cost"] == 50.0
    assert all(r["index_run_id"] == pile.run_id and r["source"] == "contract" for r in rows.values())
    view = _view(conn, ref)
    sales = {s["ref_id"]: s for s in view["sales"]}
    bundle, single = sales[9301], sales[9302]
    assert not bundle["priced"] and bundle["unit_price"] is None and bundle["quantity"] == 10
    assert (bundle["gross"], bundle["net"], bundle["cost"], bundle["profit"]) == (None, None, None, None)
    assert single["priced"] and single["net"] == pytest.approx(_net_hub(conn, ref, comp, 30_000.0, 20))
    assert single["cost"] == pytest.approx(20 * 50.0)
    tot = view["totals"]
    assert tot["net"] == pytest.approx(single["net"])  # the unpriced bundle adds nothing
    assert tot["cost"] == pytest.approx(20 * 50.0) and tot["profit"] == pytest.approx(tot["net"] - tot["cost"])
    assert (tot["units"], tot["unpriced"], tot["unpriced_units"], tot["sales"]) == (30, 1, 10, 2)
    hold = _hold(conn, ref, pile.run_id, comp)
    assert hold.observed_qty == 30 and hold.remaining == pile.qty - 30


def test_a_bundle_of_a_delivered_and_an_in_flight_held_composite_is_clean(conn, ref):
    """Settle (5), 2026-10-31: final_ids holds EVERY composite with a hold
    row, in flight included, so a contract bundling run 1's delivered
    Ferrofluid with run 2's Hyperflurite still in its reactors is CLEAN:
    Ferrofluid is released at its pro-rata share by the two quotes (its
    weight over both — not price / 10 as a one-type contract, not None as
    a mixed one) and the Hyperflurite share stays where it is: no delivered
    hold of it exists, and nothing of it spills onto run 1's row. Once the
    Hyperflurite pile delivers, D4's window still keeps that contract off
    run 2's hold — it was ISSUED before the pile's first delivery, so its
    Hyperflurite was the chain's hangar stock, not the pile — and a second
    settle takes nothing. A second bundle issued after the delivery lands
    each composite on its own run's row."""
    ferro, hyper = _route(ref, "Ferrofluid"), _route(ref, "Hyperflurite")
    _sellers(conn)
    p1 = _one_run(conn, ferro, cost=50.0)
    c1 = p1.delivered + DAY
    complete_run(conn, p1.run_id, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1 + HOUR)  # run 1's window covered
    run2 = seed_run(conn, 2, c1)
    p2 = _pile(conn, hyper, run2, 2, cost=70.0, first_job_id=92000, deliver=False)
    q_f, q_h = 30_000.0, 20_000.0
    _quote(conn, ferro.composite_id, q_f)
    _quote(conn, hyper.composite_id, q_h)
    h2 = _hold(conn, ref, run2, hyper.composite_id)
    assert h2.live and h2.in_flight and h2.delivered_units == 0 and h2.first_delivered is None
    price = 1e6
    issued = c1 + 2 * DAY
    assert p1.delivered < issued < p2.end  # Ferrofluid in the hangar, Hyperflurite still cooking
    _contract(conn, 9301, [(ferro.composite_id, 10), (hyper.composite_id, 20)], price, issued, issued + DAY)
    w_f, w_h = 10 * q_f, 20 * q_h
    total = w_f + w_h
    assert _settle(conn, ref) == 10
    [row] = _releases(conn)
    assert (row["index_run_id"], row["composite_type_id"], row["qty"]) == (p1.run_id, ferro.composite_id, 10)
    assert row["unit_price"] == pytest.approx(price * w_f / total / 10)
    assert not row["unit_price"] == pytest.approx(price / 10)  # the Hyperflurite share did not spill onto it
    assert row["unit_cost"] == 50.0
    assert store.alchemy_released_qty(conn, "contract", 9301, hyper.composite_id) == 0
    # The Hyperflurite pile delivers; the contract predates its first delivery.
    delivered2 = p2.end + DAY
    deliver_jobs(conn, p2.job_ids, delivered2)
    h2 = _hold(conn, ref, run2, hyper.composite_id)
    assert h2.live and h2.delivered_at == h2.first_delivered == delivered2 and h2.remaining == p2.qty
    assert _settle(conn, ref) == 0
    assert store.alchemy_released_qty(conn, "contract", 9301, hyper.composite_id) == 0
    assert _hold(conn, ref, run2, hyper.composite_id).remaining == p2.qty
    # A bundle issued after the delivery: each composite on its own run's row, the same shares.
    issued2 = delivered2 + DAY
    _contract(conn, 9302, [(ferro.composite_id, 10), (hyper.composite_id, 20)], price, issued2, issued2 + DAY)
    assert _settle(conn, ref) == 30
    rows = {(r["ref_id"], r["composite_type_id"]): r for r in _releases(conn)}
    assert set(rows) == {(9301, ferro.composite_id), (9302, ferro.composite_id), (9302, hyper.composite_id)}
    assert rows[(9302, ferro.composite_id)]["index_run_id"] == p1.run_id
    assert rows[(9302, ferro.composite_id)]["unit_price"] == pytest.approx(price * w_f / total / 10)
    second = rows[(9302, hyper.composite_id)]
    assert (second["index_run_id"], second["qty"], second["unit_cost"]) == (run2, 20, 70.0)
    assert second["unit_price"] == pytest.approx(price * w_h / total / 20)
    assert second["released_at"] == _iso(issued2 + DAY)
    assert _by_run(conn) == {p1.run_id: 20, run2: 20}
    assert _hold(conn, ref, run2, hyper.composite_id).remaining == p2.qty - 20


def test_settle_is_idempotent(conn, ref):
    """A second call matches nothing new and changes no row."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route)
    comp = route.composite_id
    _sell(conn, 9001, comp, 40, pile.delivered + DAY)
    _contract(conn, 9201, [(comp, 20)], 600_000.0, pile.delivered + DAY, pile.delivered + 2 * DAY)
    assert _settle(conn, ref) == 60
    before = _releases(conn)
    assert len(before) == 2
    assert _settle(conn, ref) == 0
    assert _settle(conn, ref) == 0
    assert _releases(conn) == before


def test_a_hold_whose_room_grows_takes_the_rest_of_a_sale_onto_the_same_row(conn, ref):
    """Two batches: the first delivers and takes what it can of a bigger
    sale; when the second delivers, the next settle adds the rest of that
    sale to the SAME release row (the (source, ref_id, run, composite)
    upsert), never a second row."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, route_unit_cost=50.0)
    _start, end = install_jobs(conn, run1, route.unrefined_id, 2, 10)
    d1 = end + DAY
    deliver_jobs(conn, [90000], d1)
    first = _qty(10, route)
    hold = _hold(conn, ref, run1, route.composite_id)
    assert hold.delivered_qty == first and hold.in_flight and hold.delivered_at is None
    _sell(conn, 9001, route.composite_id, 600, d1 + DAY)
    assert 600 > first
    assert _settle(conn, ref) == first
    [row] = _releases(conn)
    assert row["qty"] == first
    d2 = d1 + 2 * DAY
    deliver_jobs(conn, [90001], d2)
    grown = _qty(20, route) - first
    assert _settle(conn, ref) == min(600 - first, grown)
    [row] = _releases(conn)
    assert (row["ref_id"], row["qty"]) == (9001, first + min(600 - first, grown))
    assert _settle(conn, ref) == 0
    hold = _hold(conn, ref, run1, route.composite_id)
    assert hold.delivered_at == d2 and hold.first_delivered == d1


# --- the Ledger's own rules, applied to the hold ---------------------------------------


def test_only_finished_contracts_release(conn, ref):
    """A contract has changed hands for its price only when finished:
    outstanding, in_progress, expired, deleted and rejected ones release
    nothing, however they are dated."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route)
    comp = route.composite_id
    issued = pile.delivered + DAY
    for n, status in enumerate(("outstanding", "in_progress", "expired", "deleted", "rejected")):
        _contract(conn, 9201 + n, [(comp, 10)], 300_000.0, issued, None, status=status)
    assert _settle(conn, ref) == 0 and _releases(conn) == []
    _contract(conn, 9299, [(comp, 10)], 300_000.0, issued, issued + DAY)
    assert _settle(conn, ref) == 10
    assert [r["ref_id"] for r in _releases(conn)] == [9299]


def test_count_sales_and_the_internal_buyer_rule_apply_to_contracts_too(conn, ref):
    """Known limit #29 as a rule: the Ledger's owner and buyer tests are
    applied at match time to contracts as to wallet sells — an owner with
    Count sales off (a character or a corporation), a contract we
    accepted ourselves, a courier, an unread item list and a 0-ISK
    hand-over release nothing."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    add_owner(conn, B, count_sales=0)
    add_corp(conn, PLAYER_CORP, count_sales=0)
    pile = _one_run(conn, route)
    comp = route.composite_id
    when = pile.delivered + DAY
    _contract(conn, 9201, [(comp, 10)], 300_000.0, when, when + DAY, owner=("character", B))
    _contract(conn, 9202, [(comp, 10)], 300_000.0, when, when + DAY, owner=("corporation", PLAYER_CORP))
    _contract(conn, 9203, [(comp, 10)], 300_000.0, when, when + DAY, acceptor=A)
    _contract(conn, 9204, [(comp, 10)], 300_000.0, when, when + DAY, kind="courier")
    _contract(conn, 9205, [(comp, 10)], 300_000.0, when, when + DAY, items_status="missing")
    _contract(conn, 9206, [(comp, 10)], 0.0, when, when + DAY)
    _sell(conn, 9001, comp, 10, when, owner=("character", B))
    _sell(conn, 9002, comp, 10, when, client=A)
    assert _settle(conn, ref) == 0 and _releases(conn) == []
    # The same contract from a counted owner releases.
    _contract(conn, 9207, [(comp, 10)], 300_000.0, when, when + DAY)
    assert _settle(conn, ref) == 10


def test_a_release_keeps_the_route_cost_it_was_matched_at(conn, ref):
    """D1 / D7: a release stores unit_cost = the hold's route cost at
    match time and the Ledger costs the row at it — a later change to
    the hold's cost leaves history alone; a row written before the
    column (unit_cost NULL) falls back to the hold's current cost."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route, cost=50.0)
    comp = route.composite_id
    _sell(conn, 9001, comp, 40, pile.delivered + DAY)
    assert _settle(conn, ref) == 40
    [row] = _releases(conn)
    assert row["unit_cost"] == 50.0
    conn.execute("UPDATE alchemy_hold SET route_unit_cost = 99.0")
    conn.commit()
    [sale] = _view(conn, ref)["sales"]
    assert sale["unit_cost"] == 50.0 and sale["cost"] == pytest.approx(40 * 50.0)
    assert _view(conn, ref)["totals"]["cost"] == pytest.approx(40 * 50.0)
    conn.execute("UPDATE alchemy_hold_release SET unit_cost = NULL")
    conn.commit()
    [sale] = _view(conn, ref)["sales"]
    assert sale["unit_cost"] == 99.0 and sale["cost"] == pytest.approx(40 * 99.0)


def test_an_observed_sale_replaces_a_by_hand_mark_instead_of_spilling(conn, ref):
    """D4: by-hand marks never block an observed sale (room = delivered −
    observed); when observed + by hand exceed the pile the hand marks
    shrink by the overshoot, newest first, a priced mark keeping its
    price for the part not replaced — one market row on this hold,
    nothing on the next run's. Only units beyond the hold's whole
    delivered pile move on."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    store.add_alchemy_release(conn, p1.run_id, comp, 15, "manual", unit_price=None, unit_cost=50.0)  # older
    store.add_alchemy_release(conn, p1.run_id, comp, 40, "manual", unit_price=31_000.0, unit_cost=50.0)  # newer
    conn.commit()
    when = p2.delivered + DAY  # both holds eligible: a spill would be possible
    _sell(conn, 9001, comp, p1.qty - 50, when)
    assert _settle(conn, ref) == p1.qty - 50  # observed p1 − 50 + 55 by hand: 5 over the pile
    manual = [r for r in _releases(conn) if r["source"] == "manual"]
    assert [(r["qty"], r["unit_price"]) for r in manual] == [(15, None), (35, 31_000.0)]  # the newest shrank
    assert _by_run(conn) == {p1.run_id: p1.qty}
    assert conn.execute(
        "SELECT COUNT(*) FROM alchemy_hold_release WHERE index_run_id = ?", (p2.run_id,)
    ).fetchone()[0] == 0
    hold = _hold(conn, ref, p1.run_id, comp)
    assert (hold.observed_qty, hold.manual_priced_qty, hold.manual_unpriced_qty, hold.remaining) == (
        p1.qty - 50, 35, 15, 0
    )
    assert hold.status == "sold" and hold.room_observed == 50
    # The observed room left is 50: a sale of 60 takes it (both marks go, newest first) and 10 move to run 2.
    _sell(conn, 9002, comp, 60, when + HOUR)
    assert _settle(conn, ref) == 60
    assert not [r for r in _releases(conn) if r["source"] == "manual"]
    assert store.alchemy_released_qty(conn, "market", 9002) == 60
    assert _by_run(conn) == {p1.run_id: p1.qty, p2.run_id: 10}
    hold = _hold(conn, ref, p1.run_id, comp)
    assert (hold.observed_qty, hold.released_qty, hold.remaining, hold.status) == (p1.qty, p1.qty, 0, "sold")


# --- the finals' guard and the expired hold's closing bound (settle, 2026-10-31) ----------------


def test_a_hold_on_a_pipelines_final_is_never_released_by_observed_sales(conn, ref):
    """Known limit (9) / review #123: a composite that is any pipeline's
    final — active or not (ledger.finals lists every pipeline) — is sold
    as a final, so its wallet sells and finished contracts are the finals'
    Ledger's and release no hold: the settle matches nothing while the
    pipeline exists, the hold keeps its whole delivered room, and the
    finals' view counts the very same two sales as the composite's; the
    same world with the pipeline deleted matches them both."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route)
    comp = route.composite_id
    add_pipeline(conn, ref, "Ferrofluid", 100)  # is_active defaults to 1
    assert set(ledger.finals(conn)) == {comp}
    _sell(conn, 9001, comp, 40, pile.delivered + DAY)
    _contract(conn, 9201, [(comp, 20)], 600_000.0, pile.delivered + DAY, pile.delivered + 2 * DAY)
    assert _settle(conn, ref) == 0 and _releases(conn) == []
    hold = _hold(conn, ref, pile.run_id, comp)
    assert hold.live and hold.status == "delivered"
    assert (hold.room_observed, hold.remaining, hold.observed_qty) == (pile.qty, pile.qty, 0)
    # The finals' Ledger books both as sales of the final; the alchemy section lists no sale.
    view = ledger.build_view(conn, ref, store.get_settings(conn), "all", pile.delivered + 10 * DAY)
    assert sorted((s.type_id, s.ref_id, s.quantity, s.counted, s.priced) for s in view["sales"]) == [
        (comp, 9001, 40, True, True), (comp, 9201, 20, True, True)
    ]
    assert view["counted_sales"] == 2 and view["totals"].units == 60
    assert [(p.type_id, p.units_sold) for p in view["products"]] == [(comp, 60)]
    assert view["alchemy"]["sales"] == []
    assert [(h["run_number"], h["status"], h["remaining"]) for h in view["alchemy"]["held"]] == [(1, "delivered", pile.qty)]
    # Deactivated, the pipeline still makes the composite a final.
    conn.execute("UPDATE pipeline SET is_active = 0")
    conn.commit()
    assert set(ledger.finals(conn)) == {comp}
    assert _settle(conn, ref) == 0 and _releases(conn) == []
    # Deleted (the product stops being a final), the same two sales are the pile's.
    conn.execute("DELETE FROM pipeline")
    conn.commit()
    assert ledger.finals(conn) == {}
    assert _settle(conn, ref) == 60
    assert _by_run(conn) == {pile.run_id: 60}
    assert {(r["source"], r["ref_id"], r["qty"]) for r in _releases(conn)} == {
        ("market", 9001, 40), ("contract", 9201, 20)
    }
    assert _hold(conn, ref, pile.run_id, comp).remaining == pile.qty - 60


def test_mark_sold_frees_a_hold_on_a_pipelines_final(conn, ref):
    """Known limit (9), the other half: the hold on a pipeline's final
    releases by hand alone — a 'manual' row (store.add_alchemy_release,
    what Mark sold writes), priced or not, counts against the hold while
    the observed sale of the same composite still matches nothing after
    the marks; the alchemy section books the priced mark as a sale with
    revenue as entered and the unpriced one as manual_units, and the
    observed sale stays the finals' Ledger's."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route, cost=50.0)
    comp = route.composite_id
    add_pipeline(conn, ref, "Ferrofluid", 100)
    _sell(conn, 9001, comp, 40, pile.delivered + DAY)
    store.add_alchemy_release(conn, pile.run_id, comp, 25, "manual", unit_price=31_000.0, unit_cost=50.0)
    store.add_alchemy_release(conn, pile.run_id, comp, 5, "manual", unit_price=None, unit_cost=50.0)
    conn.commit()
    hold = _hold(conn, ref, pile.run_id, comp)
    assert (hold.manual_priced_qty, hold.manual_unpriced_qty, hold.observed_qty) == (25, 5, 0)
    assert (hold.released_qty, hold.remaining, hold.status) == (30, pile.qty - 30, "delivered")
    assert hold.live and hold.room_observed == pile.qty  # hand marks never block an observed sale; none may come
    assert _settle(conn, ref) == 0  # the guard holds after the marks too
    assert [(r["source"], r["ref_id"], r["qty"]) for r in _releases(conn)] == [("manual", None, 25), ("manual", None, 5)]
    assert store.alchemy_released_qty(conn, "market", 9001) == 0
    view = _view(conn, ref)
    assert sorted((s["source"], s["quantity"], s["priced"]) for s in view["sales"]) == [
        ("manual", 5, False), ("manual", 25, True)
    ]
    [priced] = [s for s in view["sales"] if s["priced"]]
    assert priced["net"] == priced["gross"] == pytest.approx(25 * 31_000.0)
    assert priced["cost"] == pytest.approx(25 * 50.0) and priced["run_number"] == 1
    tot = view["totals"]
    assert (tot["units"], tot["sales"], tot["manual_priced_units"], tot["manual_units"]) == (25, 1, 25, 5)
    assert (tot["unpriced"], tot["unpriced_units"]) == (0, 0)
    assert tot["net"] == pytest.approx(25 * 31_000.0) and tot["cost"] == pytest.approx(25 * 50.0)
    assert [(h["run_number"], h["status"], h["remaining"]) for h in view["held"]] == [(1, "delivered", pile.qty - 30)]
    finals_view = ledger.build_view(conn, ref, store.get_settings(conn), "all", pile.delivered + 10 * DAY)
    assert [(s.type_id, s.ref_id, s.counted) for s in finals_view["sales"]] == [(comp, 9001, True)]
    assert finals_view["counted_sales"] == 1 and finals_view["totals"].units == 40


def test_an_expired_hold_closes_its_history_one_run_after_the_one_that_ended_it(conn, ref):
    """Review #30 as a rule: an expired hold is a settle candidate only
    while expires_after >= the highest executed run number − 1. A hold
    ended by run 2's Mark executed still takes a sale dated inside its
    life that the Ledger pulls late while run 3 is the newest executed
    run; once run 4 is executed too its history is closed — a second late
    sale inside its life stays unassigned and its remainder stays
    returned to stock — while run 2's hold (ended by run 3) is still open
    to a late sale of its own. Runs 3 and 4 each carry a hold row of the
    composite (a proposal the plan dropped, kept at 0 — write_plan_holds'
    rule 4); every window is covered."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    c2 = p2.delivered + DAY  # run 2's Mark executed: the second after run 1's delivery
    complete_run(conn, p2.run_id, c2)
    store.save_industry_jobs(conn, [], pulled_at=c2 + HOUR)
    h1 = _hold(conn, ref, p1.run_id, comp)
    assert not h1.live and (h1.expires_after, h1.expired_at) == (2, c2)
    late1, late2 = p1.delivered + 2 * DAY, p1.delivered + 3 * DAY  # inside run 1's life, before run 2's pile
    assert p1.delivered < late1 < late2 < p2.delivered < c2
    # Run 3 opened at c2 and executed a month later, its window covered.
    c3 = c2 + 30 * DAY
    r3 = seed_run(conn, 3, c2)
    seed_hold(conn, r3, comp, planned=0)
    complete_run(conn, r3, c3)
    store.save_industry_jobs(conn, [], pulled_at=c3 + HOUR)
    _sell(conn, 9001, comp, 40, late1)  # pulled only now
    assert _settle(conn, ref) == 40
    assert _by_run(conn) == {p1.run_id: 40}
    assert _hold(conn, ref, p1.run_id, comp).status == "returned to stock"
    # Run 4 executed as well: run 1's history is closed, run 2's is not.
    c4 = c3 + 30 * DAY
    r4 = seed_run(conn, 4, c3)
    seed_hold(conn, r4, comp, planned=0)
    complete_run(conn, r4, c4)
    store.save_industry_jobs(conn, [], pulled_at=c4 + HOUR)
    h2 = _hold(conn, ref, p2.run_id, comp)
    assert not h2.live and (h2.expires_after, h2.expired_at) == (3, c3)
    _sell(conn, 9002, comp, 30, late2)  # inside run 1's life, pulled late again
    _sell(conn, 9003, comp, 20, c2 + 2 * DAY)  # inside run 2's life
    assert p2.delivered < c2 + 2 * DAY < c3
    assert _settle(conn, ref) == 20
    assert _by_run(conn) == {p1.run_id: 40, p2.run_id: 20}
    assert store.alchemy_released_qty(conn, "market", 9002) == 0
    assert store.alchemy_released_qty(conn, "market", 9003) == 20
    h1 = _hold(conn, ref, p1.run_id, comp)
    assert (h1.released_qty, h1.remaining, h1.status) == (40, p1.qty - 40, "returned to stock")
    assert _settle(conn, ref) == 0


# --- the view -----------------------------------------------------------------------------


def test_by_hand_marks_priced_and_unpriced_in_the_view(conn, ref):
    """Ruling (d): a priced hand mark books revenue as entered — net ==
    gross == price × qty, no fee or freight term, no venue — and costs
    its units; an unpriced one is neither revenue nor cost, counted apart
    as manual_units and as an unpriced row, and is no sale in the heading
    count."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route, cost=50.0)
    comp = route.composite_id
    store.add_alchemy_release(conn, pile.run_id, comp, 10, "manual", unit_price=31_000.0, unit_cost=50.0)
    store.add_alchemy_release(conn, pile.run_id, comp, 3, "manual", unit_price=None, unit_cost=50.0)
    conn.commit()
    view = _view(conn, ref)
    priced = [s for s in view["sales"] if s["manual"] and s["priced"]]
    unpriced = [s for s in view["sales"] if s["manual"] and not s["priced"]]
    assert len(priced) == 1 and len(unpriced) == 1
    [p], [u] = priced, unpriced
    assert p["net"] == p["gross"] == pytest.approx(10 * 31_000.0)
    assert p["cost"] == pytest.approx(10 * 50.0) and p["profit"] == pytest.approx(10 * 31_000.0 - 500.0)
    assert p["venue"] is None and p["source"] == "manual" and p["run_number"] == 1
    assert (u["gross"], u["net"], u["cost"], u["profit"]) == (None, None, None, None)
    tot = view["totals"]
    # The no-price mark is neither a sale nor an "unpriced" sale (that word
    # is the contract whose price could not be attributed): manual_units only.
    assert (tot["units"], tot["manual_units"], tot["unpriced"], tot["unpriced_units"], tot["sales"]) == (10, 3, 0, 0, 1)
    assert tot["manual_priced_units"] == 10  # the priced mark's units, told apart inside `units`
    assert tot["net"] == pytest.approx(310_000.0) and tot["cost"] == pytest.approx(500.0)
    assert tot["profit"] == pytest.approx(tot["net"] - tot["cost"])


def test_totals_net_less_route_cost_is_net_income_with_unpriced_rows_present(conn, ref):
    """D7: cost is added only for rows that add net, so Net − Route cost =
    Net income holds with an unpriced hand mark and an unpriced contract
    (a truly mixed one: units, no attributable price) in the list; the
    unpriced units are counted beside the badge."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route, cost=50.0)
    comp = route.composite_id
    _sell(conn, 9001, comp, 40, pile.delivered + DAY)
    _contract(conn, 9201, [(comp, 5), (ref.type_id("Tritanium"), 1000)], 1e6, pile.delivered + DAY, pile.delivered + 2 * DAY)
    assert _settle(conn, ref) == 45
    store.add_alchemy_release(conn, pile.run_id, comp, 7, "manual", unit_price=None, unit_cost=50.0)
    conn.commit()
    view = _view(conn, ref)
    rows = {(s["source"], s["ref_id"]): s for s in view["sales"]}
    assert rows[("contract", 9201)]["net"] is None and rows[("contract", 9201)]["cost"] is None
    assert rows[("contract", 9201)]["quantity"] == 5 and not rows[("contract", 9201)]["priced"]
    tot = view["totals"]
    assert tot["profit"] == pytest.approx(tot["net"] - tot["cost"])
    assert tot["net"] == pytest.approx(_net_hub(conn, ref, comp, 30_000.0, 40))
    assert tot["cost"] == pytest.approx(40 * 50.0)  # the unpriced 5 and 7 cost nothing here
    assert (tot["unpriced"], tot["unpriced_units"], tot["manual_units"], tot["units"]) == (1, 5, 7, 45)
    assert tot["manual_priced_units"] == 0  # no hand mark carries a price here
    assert tot["sales"] == 2  # the wallet sell and the contract; the unpriced hand mark is no sale


def test_totals_net_follows_the_venue_of_each_sale(conn, ref):
    """Review 2026-10-31 (the old test asserted the venue-less net): an
    observed sale is netted where it sold — the hub rates and freight-out
    at an NPC station, the structure rates and the structure leg at an
    Upwell structure (location id >= 64,000,000) — and totals.net is
    their sum."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    conn.execute("UPDATE settings SET freight_out_isk_per_m3 = 750.0, structure_freight_in_isk_per_m3 = 300.0")
    conn.commit()
    pile = _one_run(conn, route, per_job=20)
    comp = route.composite_id
    _sell(conn, 9001, comp, 40, pile.delivered + DAY, price=30_000.0)
    _sell(conn, 9002, comp, 25, pile.delivered + 2 * DAY, price=28_000.0, location=STRUCTURE)
    assert _settle(conn, ref) == 65
    settings = store.get_settings(conn)
    volume = ref.type_info(comp).freight_volume
    structure_net = costing.net_proceeds_at_venue(
        28_000.0, volume, settings, costing.SALE_VENUE_STRUCTURE,
        capital=False, freight_exempt=costing.freight_out_exempt(comp),
    ) * 25
    hub_net = _net_hub(conn, ref, comp, 30_000.0, 40)
    assert structure_net != pytest.approx(
        costing.net_proceeds_at_venue(28_000.0, volume, settings, costing.SALE_VENUE_HUB) * 25
    )
    view = _view(conn, ref)
    rows = {s["ref_id"]: s for s in view["sales"]}
    assert rows[9001]["venue"] == costing.SALE_VENUE_HUB and rows[9001]["net"] == pytest.approx(hub_net)
    assert rows[9002]["venue"] == costing.SALE_VENUE_STRUCTURE and rows[9002]["net"] == pytest.approx(structure_net)
    assert rows[9001]["system_name"] == "Jita"
    assert rows[9001]["gross"] == pytest.approx(40 * 30_000.0) and rows[9002]["gross"] == pytest.approx(25 * 28_000.0)
    tot = view["totals"]
    assert tot["net"] == pytest.approx(hub_net + structure_net)
    assert tot["gross"] == pytest.approx(40 * 30_000.0 + 25 * 28_000.0)
    assert tot["cost"] == pytest.approx(65 * 50.0) and tot["profit"] == pytest.approx(tot["net"] - tot["cost"])
    assert tot["units"] == 65 and tot["sales"] == 2


def test_the_finals_figures_are_the_same_with_and_without_alchemy_releases(conn, ref):
    """User ruling 2026-10-31: the alchemy line is listed apart — the
    finals' totals, products, sales and unrealized figures of build_view
    are identical before and after the holds settle and a hand mark is
    added; only the alchemy section changes."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    add_pipeline(conn, ref, "Hulk", 8)
    pile = _one_run(conn, route)
    comp = route.composite_id
    _quote(conn, HULK, 300e6)
    _sell(conn, 8001, HULK, 2, pile.delivered + DAY, price=310e6)
    _sell(conn, 9001, comp, 40, pile.delivered + DAY)
    _contract(conn, 9201, [(comp, 5), (HULK, 1)], 320e6, pile.delivered + DAY, pile.delivered + 2 * DAY)
    settings = store.get_settings(conn)
    now = pile.delivered + 10 * DAY

    def finals_view():
        v = ledger.build_view(conn, ref, settings, "all", now)
        return (
            v["totals"], v["unrealized"], v["counted_sales"], v["sales_total"],
            [(s.ref_id, s.quantity, s.net, s.counted, s.priced) for s in v["sales"]],
            [(p.type_id, p.units_sold, p.net) for p in v["products"]],
        )

    before = finals_view()
    assert before[2] == 2 and before[0].net > 0  # the Hulk sale and the contract's Hulk are the finals'
    assert not ledger.build_view(conn, ref, settings, "all", now)["alchemy"]["sales"]
    assert _settle(conn, ref) == 45
    store.add_alchemy_release(conn, pile.run_id, comp, 10, "manual", unit_price=31_000.0, unit_cost=50.0)
    conn.commit()
    after = finals_view()
    assert after == before
    alchemy = ledger.build_view(conn, ref, settings, "all", now)["alchemy"]
    assert alchemy["totals"]["units"] == 55 and len(alchemy["sales"]) == 3


def test_the_window_filters_releases_by_their_date(conn, ref):
    """The section's window is the Ledger's: a release dated before it is
    left out (a market row by the sale's date, a contract's by its
    completion, a hand mark by when it was made). A contract ISSUED before
    the window opens and finished inside it is listed — the Ledger dates
    a contract by its completion (the day the ISK arrived), where the
    hold's own window test reads its issue (test_a_contract_is_dated_by_
    its_issue): the two dates are told apart here."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route, per_job=20)
    comp = route.composite_id
    early, late = pile.delivered + HOUR, pile.delivered + 10 * DAY
    _sell(conn, 9001, comp, 40, early)
    _sell(conn, 9002, comp, 30, late)
    _contract(conn, 9201, [(comp, 5)], 150_000.0, early, early + DAY)
    assert _settle(conn, ref) == 75
    # Issued four days before the 7-day window opens, finished a day before it closes.
    _contract(conn, 9202, [(comp, 4)], 120_000.0, pile.delivered + 2 * DAY, late - DAY)
    assert _settle(conn, ref) == 4
    store.add_alchemy_release(conn, pile.run_id, comp, 2, "manual", unit_price=30_000.0, unit_cost=50.0,
                              released_at=_iso(early + 2 * HOUR))
    store.add_alchemy_release(conn, pile.run_id, comp, 3, "manual", unit_price=30_000.0, unit_cost=50.0,
                              released_at=_iso(late + HOUR))
    conn.commit()
    now = late + 2 * DAY
    window = ledger.window_bounds("7", now)
    assert _iso(pile.delivered + 2 * DAY) < window.since_ts <= _iso(late - DAY)  # issued outside, finished inside
    assert {(s["source"], s["ref_id"]) for s in _view(conn, ref, "all", now)["sales"]} == {
        ("market", 9001), ("market", 9002), ("contract", 9201), ("contract", 9202), ("manual", None)
    }
    assert len(_view(conn, ref, "all", now)["sales"]) == 6
    narrow = _view(conn, ref, "7", now)
    assert [(s["source"], s["ref_id"], s["quantity"]) for s in narrow["sales"]] == [
        ("manual", None, 3), ("market", 9002, 30), ("contract", 9202, 4)
    ]
    [listed] = [s for s in narrow["sales"] if s["source"] == "contract"]
    assert listed["when"] == _iso(late - DAY)  # the completion date, not the issue
    assert narrow["totals"]["units"] == 37 and narrow["totals"]["sales"] == 3
    assert narrow["held"] and narrow["held"][0]["released"] == 84  # the hold itself is not windowed


def test_still_held_rows_carry_status_expiry_and_valuation(conn, ref):
    """D7 / ruling (c): the Still held table lists holds with installs —
    a delivered hold with its expiry run and the expected profit of its
    remainder at today's net quote, an in-flight hold worded by when its
    jobs end, an expired hold returned to stock with no estimate — a
    proposal-only open hold in planned_only, and held_totals over the
    live holds alone (a live hold without a quote counted as unquoted)."""
    ferro, hyper = _route(ref, "Ferrofluid"), _route(ref, "Hyperflurite")
    other = next(
        r for c, r in sorted(ref.alchemy_routes().items()) if c not in (ferro.composite_id, hyper.composite_id)
    )
    _sellers(conn)
    # Run 1: delivered, then over (two Mark executed after its delivery).
    r1 = seed_run(conn, 1, T0 - 120 * DAY)
    e = _pile(conn, ferro, r1, 1, cost=40.0, first_job_id=80000)
    c1 = e.delivered + DAY
    complete_run(conn, r1, c1)
    # Run 2: two delivered holds (Ferrofluid quoted, Hyperflurite never), executed once.
    r2 = seed_run(conn, 2, c1)
    d = _pile(conn, ferro, r2, 2, cost=50.0)
    u = _pile(conn, hyper, r2, 2, cost=65.0, first_job_id=93000)
    c2 = max(d.delivered, u.delivered) + DAY
    complete_run(conn, r2, c2)
    store.save_industry_jobs(conn, [], pulled_at=c2 + HOUR)
    # Run 3: open, a Hyperflurite pile in flight, a proposal nobody installed, a dropped one.
    r3 = seed_run(conn, 3, c2)
    f = _pile(conn, hyper, r3, 3, cost=70.0, first_job_id=92000, deliver=False)
    seed_hold(conn, r3, other.composite_id, planned=500)
    seed_hold(conn, r3, ferro.composite_id, planned=0)
    _sell(conn, 9001, ferro.composite_id, 40, c2 + HOUR)  # after run 1's end: run 2's hold takes it
    assert _settle(conn, ref) == 40
    assert _by_run(conn) == {r2: 40}
    _quote(conn, ferro.composite_id, 30_000.0)
    settings = store.get_settings(conn)
    net_unit = net_sale(ref, settings, ferro.composite_id, 30_000.0)
    view = _view(conn, ref)
    held = {(h["run_number"], h["type_id"]): h for h in view["held"]}
    assert set(held) == {
        (1, ferro.composite_id), (2, ferro.composite_id), (2, hyper.composite_id), (3, hyper.composite_id),
    }
    expired, delivered, unquoted, flying = (
        held[(1, ferro.composite_id)], held[(2, ferro.composite_id)], held[(2, hyper.composite_id)],
        held[(3, hyper.composite_id)],
    )
    assert expired["status"] == "returned to stock" and not expired["live"]
    assert expired["remaining"] == e.qty and expired["expected_profit"] is None  # ruling (c): no estimate
    assert expired["expires_after"] == 2
    # Ended by run 2's Mark executed, dated at it (c2 is the completed_at seeded above).
    assert expired["expiry_words"] == f"ended when run 2 was marked executed ({c2:%Y-%m-%d %H:%M})"
    assert delivered["status"] == "delivered" and delivered["live"]
    assert (delivered["planned"], delivered["released"], delivered["remaining"]) == (d.qty, 40, d.qty - 40)
    assert delivered["expires_after"] == 3 and delivered["expiry_words"] == "until run 3 is marked executed"
    assert delivered["net_unit"] == pytest.approx(net_unit) and delivered["route_unit"] == 50.0
    assert delivered["expected_profit"] == pytest.approx((net_unit - 50.0) * (d.qty - 40))
    assert delivered["delivered_at"] == d.delivered and delivered["jobs_end"] is None
    assert unquoted["status"] == "delivered" and unquoted["live"]
    assert unquoted["net_unit"] is None and unquoted["expected_profit"] is None
    assert flying["status"] == "in flight" and flying["live"] and flying["expires_after"] is None
    assert flying["expiry_words"].startswith("until the run after the one its jobs deliver in")
    assert f"jobs end {f.end:%Y-%m-%d %H:%M}" in flying["expiry_words"]
    assert flying["jobs_end"] == f.end and flying["delivered_at"] is None
    assert flying["remaining"] == f.qty and flying["expected_profit"] is None  # no Hyperflurite quote
    assert view["planned_only"] == [{
        "type_id": other.composite_id, "name": ref.type_info(other.composite_id).name,
        "run_number": 3, "planned_qty": 500,
    }]
    tot = view["held_totals"]
    assert tot["units"] == (d.qty - 40) + u.qty + f.qty  # live holds only
    assert tot["value"] == pytest.approx(delivered["expected_profit"])
    assert tot["unquoted"] == 2  # the two live Hyperflurite holds have no quote
    assert view["any"]
    # Quoted, the Hyperflurite remainders are valued too, the in-flight one included.
    _quote(conn, hyper.composite_id, 20_000.0)
    view = _view(conn, ref)
    net_h = net_sale(ref, settings, hyper.composite_id, 20_000.0)
    held = {(h["run_number"], h["type_id"]): h for h in view["held"]}
    assert held[(3, hyper.composite_id)]["expected_profit"] == pytest.approx((net_h - 70.0) * f.qty)
    assert held[(2, hyper.composite_id)]["expected_profit"] == pytest.approx((net_h - 65.0) * u.qty)
    assert view["held_totals"]["unquoted"] == 0
    assert view["held_totals"]["value"] == pytest.approx(
        (net_unit - 50.0) * (d.qty - 40) + (net_h - 70.0) * f.qty + (net_h - 65.0) * u.qty
    )


def test_totals_tell_the_four_kinds_of_row_apart(conn, ref):
    """Ruling (d) and review #100, every total at once: a market sale, an
    unpriced bundle (a contract with a non-final, non-held item — units,
    no attributable price), a priced hand mark and an unpriced hand mark
    on one hold. `units` counts every row but the unpriced hand mark;
    `sales` the market sale, the contract and the PRICED hand mark;
    `manual_priced_units` the priced mark alone; `manual_units` the
    unpriced mark alone; `unpriced` / `unpriced_units` the contract alone
    — and the money is the market sale's venue net plus the hand price
    as entered, costed for those two rows only."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route, cost=50.0)
    comp = route.composite_id
    _sell(conn, 9001, comp, 40, pile.delivered + DAY)
    _contract(conn, 9201, [(comp, 5), (ref.type_id("Tritanium"), 1000)], 1e6, pile.delivered + DAY, pile.delivered + 2 * DAY)
    assert _settle(conn, ref) == 45
    store.add_alchemy_release(conn, pile.run_id, comp, 10, "manual", unit_price=31_000.0, unit_cost=50.0)
    store.add_alchemy_release(conn, pile.run_id, comp, 3, "manual", unit_price=None, unit_cost=50.0)
    conn.commit()
    view = _view(conn, ref)
    assert sorted((s["source"], s["quantity"], s["priced"]) for s in view["sales"]) == [
        ("contract", 5, False), ("manual", 3, False), ("manual", 10, True), ("market", 40, True)
    ]
    tot = view["totals"]
    assert set(tot) == {
        "units", "gross", "net", "cost", "profit", "unpriced", "unpriced_units", "manual_units",
        "manual_priced_units", "sales",
    }
    counts = ("units", "sales", "manual_priced_units", "manual_units", "unpriced", "unpriced_units")
    assert {k: tot[k] for k in counts} == {
        "units": 40 + 5 + 10, "sales": 3, "manual_priced_units": 10, "manual_units": 3,
        "unpriced": 1, "unpriced_units": 5,
    }
    net_market = _net_hub(conn, ref, comp, 30_000.0, 40)
    assert tot["gross"] == pytest.approx(40 * 30_000.0 + 10 * 31_000.0)
    assert tot["net"] == pytest.approx(net_market + 10 * 31_000.0)
    assert tot["cost"] == pytest.approx((40 + 10) * 50.0)  # the contract's 5 and the unpriced 3 cost nothing
    assert tot["profit"] == pytest.approx(tot["net"] - tot["cost"])


# --- transaction discipline ------------------------------------------------------------------


def test_settle_leaves_no_transaction_open_after_a_no_match_pass(conn, ref):
    """BEGIN IMMEDIATE is ended on every path: a pass with nothing to
    match rolls back and releases the write lock (a second writer can
    begin at once)."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    assert _settle(conn, ref) == 0  # no hold at all
    pile = _one_run(conn, route)
    assert _settle(conn, ref) == 0  # a hold, no sale
    _sell(conn, 9001, route.composite_id, 10, pile.delivered - DAY)  # a sale outside the window
    assert _settle(conn, ref) == 0
    assert not conn.in_transaction
    other = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2], timeout=1.0)
    try:
        other.execute("BEGIN IMMEDIATE")  # the lock is free
        other.rollback()
    finally:
        other.close()


def test_settle_rolls_back_and_re_raises_on_a_failure_mid_way(conn, ref, monkeypatch):
    """Review #8 / #34: a failure after the first release was written
    rolls everything back, closes the transaction and propagates — no
    partial release survives, and the next settle starts clean."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route)
    comp = route.composite_id
    _sell(conn, 9001, comp, 20, pile.delivered + DAY)
    _sell(conn, 9002, comp, 30, pile.delivered + 2 * DAY)
    real = store.add_alchemy_release
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("injected")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "add_alchemy_release", boom)
    with pytest.raises(RuntimeError, match="injected"):
        ledger.settle_alchemy_holds(conn, ref, store.get_settings(conn))
    assert len(calls) == 2  # the first row had been written before the failure
    assert not conn.in_transaction
    assert _releases(conn) == []
    assert store.alchemy_released_qty(conn, "market", 9001) == 0
    monkeypatch.setattr(store, "add_alchemy_release", real)
    assert _settle(conn, ref) == 50
    assert {r["ref_id"]: r["qty"] for r in _releases(conn)} == {9001: 20, 9002: 30}


def _refdata() -> Refdata:
    """A Refdata of its own for a worker thread (sqlite3 connections are
    thread-bound; the SDE is opened read-only, as conftest does)."""
    rc = sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True)
    rc.row_factory = sqlite3.Row
    return Refdata(rc)


def test_two_overlapping_settles_apply_a_sale_once(conn, ref, monkeypatch):
    """Review #8: the settle holds the write lock from its reads to its
    commit, so a second connection settling at the same moment waits on
    BEGIN IMMEDIATE and then reads the sale as applied — one release
    row, one of the two calls matching it."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    pile = _one_run(conn, route)
    comp = route.composite_id
    _sell(conn, 9001, comp, 10, pile.delivered + DAY)
    settings = store.get_settings(conn)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2, timeout=60)
    results: dict[str, int] = {}
    errors: list[BaseException] = []
    real = store.add_alchemy_release
    gate = {"open": False}

    def gated(*args, **kwargs):
        if not gate["open"]:
            gate["open"] = True
            barrier.wait()  # B starts its settle now...
            time.sleep(0.5)  # ...and waits on the lock this connection holds
        return real(*args, **kwargs)

    def other():
        c2 = sqlite3.connect(path, timeout=60.0)
        c2.row_factory = sqlite3.Row
        ref2 = _refdata()
        try:
            barrier.wait()
            results["b"] = ledger.settle_alchemy_holds(c2, ref2, settings)
            assert not c2.in_transaction
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)
        finally:
            c2.close()
            ref2.close()

    monkeypatch.setattr(store, "add_alchemy_release", gated)
    worker = threading.Thread(target=other, name="settle-b")
    worker.start()
    results["a"] = ledger.settle_alchemy_holds(conn, ref, settings)
    worker.join(120)
    assert not worker.is_alive(), "the second settle never finished"
    assert not errors, errors
    assert not conn.in_transaction
    assert (results["a"], results["b"]) == (10, 0)
    rows = _releases(conn)
    assert len(rows) == 1 and (rows[0]["ref_id"], rows[0]["qty"]) == (9001, 10)
    assert store.alchemy_released_qty(conn, "market", 9001) == 10


# --- listed units -----------------------------------------------------------------------------


def test_listed_units_count_what_has_left_the_hangar_unsold(conn, ref):
    """D3: open sell orders of Count-sales owners (summed), outstanding or
    in-progress item-exchange and courier contracts' included items; a
    cancelled order, a sales-off owner's order, an asked-for item and a
    finished contract count nothing; every requested type is a key."""
    ferro, hyper = _route(ref, "Ferrofluid"), _route(ref, "Hyperflurite")
    _sellers(conn)
    add_owner(conn, B, count_sales=0)
    comp = ferro.composite_id
    _order(conn, 1, A, comp, 1000)
    _order(conn, 2, A, comp, 500)
    _order(conn, 3, A, comp, 999, state="cancelled")
    _order(conn, 4, B, comp, 999)
    _order(conn, 5, A, hyper.composite_id, 7)
    when = datetime(2026, 10, 20, tzinfo=timezone.utc)
    _contract(conn, 9201, [(comp, 9)], 270_000.0, when, None, status="outstanding", asked=[(comp, 100)])
    _contract(conn, 9202, [(comp, 6)], 0.0, when, None, status="in_progress", kind="courier")
    _contract(conn, 9203, [(comp, 11)], 330_000.0, when, when + DAY)  # finished: sold, not listed
    _contract(conn, 9204, [(comp, 13)], 390_000.0, when, None, status="outstanding", owner=("character", B))
    _contract(conn, 9205, [(comp, 17)], 510_000.0, when, None, status="outstanding", items_status="missing")
    assert ledger.listed_units(conn, [comp, hyper.composite_id, 34]) == {
        comp: 1000 + 500 + 9 + 6, hyper.composite_id: 7, 34: 0,
    }
    assert ledger.listed_units(conn, []) == {}


def test_listed_units_read_the_remaining_volume_and_both_open_contract_kinds(conn, ref):
    """D3, the cases the test above leaves implicit: a PARTLY filled open
    sell order counts what is still on the book (400 of the 1,000 listed —
    the 600 sold are the wallet's), an in-progress item exchange and an
    outstanding courier we issued count their included units, a finished
    contract of either kind counts nothing (sold, or hauled and handed
    over), and a sales-off owner's partly filled order counts nothing —
    the hold's Count-sales rule."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    add_owner(conn, B, count_sales=0)
    comp = route.composite_id
    _order(conn, 1, A, comp, 400, total=1000)
    _order(conn, 2, B, comp, 250, total=1000)  # Count sales off
    when = datetime(2026, 10, 20, tzinfo=timezone.utc)
    _contract(conn, 9201, [(comp, 9)], 270_000.0, when, None, status="in_progress")
    _contract(conn, 9202, [(comp, 6)], 0.0, when, None, status="outstanding", kind="courier")
    _contract(conn, 9203, [(comp, 11)], 330_000.0, when, when + DAY)  # finished exchange: sold
    _contract(conn, 9204, [(comp, 13)], 0.0, when, when + DAY, kind="courier")  # finished courier: handed over
    assert ledger.listed_units(conn, [comp]) == {comp: 400 + 9 + 6}


# --- carried over from the first hold suite (review 2026-10-31), on the new API ---------------


def test_settle_matches_sales_oldest_run_first_once(conn, ref):
    """Two delivered holds of one composite: a sale dated after both
    fills run 1's room, then run 2's; a second call matches nothing; a
    sale beyond every hold's room stays unassigned."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    when = p2.delivered + 12 * HOUR
    _sell(conn, 9001, comp, p1.qty + 50, when)
    assert _settle(conn, ref) == p1.qty + 50
    h1, h2 = (_hold(conn, ref, p.run_id, comp) for p in (p1, p2))
    assert h1.released_qty == p1.qty and h1.remaining == 0
    assert h2.released_qty == 50 and h2.remaining == p2.qty - 50
    rows = conn.execute(
        "SELECT index_run_id, qty, unit_price, unit_cost FROM alchemy_hold_release "
        "WHERE source = 'market' AND ref_id = 9001 ORDER BY index_run_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(p1.run_id, p1.qty, 30_000.0, 50.0), (p2.run_id, 50, 30_000.0, 60.0)]
    # Idempotent.
    assert _settle(conn, ref) == 0
    assert store.alchemy_released_qty(conn, "market", 9001) == p1.qty + 50
    # A sale beyond every hold's room stays unassigned.
    _sell(conn, 9002, comp, p2.qty, when + HOUR)
    assert _settle(conn, ref) == p2.qty - 50
    assert store.alchemy_released_qty(conn, "market", 9002) == p2.qty - 50
    assert [(h.index_run_id, h.remaining) for h in holds.load(conn, ref, store.get_settings(conn))] == [
        (p1.run_id, 0), (p2.run_id, 0)
    ]


def test_settle_ignores_sales_the_ledger_would_not_count(conn, ref):
    """The Ledger's exclusions, applied to the holds: a sale before the
    delivery, a sale to ourselves, a sales-off owner's sale, another
    type, a courier, a contract we accepted and a 0-ISK contract release
    nothing."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    add_owner(conn, B, count_sales=0)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    when = p2.delivered + DAY
    _sell(conn, 9101, comp, 10, p1.delivered - 10 * DAY)  # before the holds
    _sell(conn, 9102, comp, 10, when, client=A)  # to ourselves
    _sell(conn, 9103, comp, 10, when, owner=("character", B))  # sales off
    _sell(conn, 9104, HULK, 10, when)  # another type
    _contract(conn, 9201, [(comp, 10)], 1e6, when, when + DAY, kind="courier")
    _contract(conn, 9202, [(comp, 10)], 1e6, when, when + DAY, acceptor=A)  # accepted by us
    _contract(conn, 9203, [(comp, 10)], 0.0, when, when + DAY)  # no price
    _contract(conn, 9204, [(comp, 10)], 1e6, p1.delivered - 10 * DAY, when)  # issued before the holds
    _contract(conn, 9205, [(comp, 10)], 1e6, when, when + DAY, owner=("character", B))  # sales off
    assert _settle(conn, ref) == 0
    assert conn.execute("SELECT COUNT(*) FROM alchemy_hold_release").fetchone()[0] == 0


def test_settle_takes_contracts_clean_and_mixed(conn, ref):
    """A clean contract attributes its price per unit; one bundling the
    composite with a Hulk is clean too now (final_ids holds the finals:
    pro rata by quote); a truly mixed one — a non-final, non-held item
    included — counts units with no attributable price."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    add_pipeline(conn, ref, "Hulk", 8)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    q_c, q_hulk = 30_000.0, 300e6
    _quote(conn, comp, q_c)
    _quote(conn, HULK, q_hulk)
    when = p2.delivered + DAY
    _contract(conn, 9301, [(comp, 20)], 600_000.0, when, when + DAY)
    _contract(conn, 9302, [(comp, 5), (HULK, 1)], 400e6, when + HOUR, when + DAY)
    _contract(conn, 9303, [(comp, 5), (ref.type_id("Tritanium"), 1000)], 400e6, when + 2 * HOUR, when + DAY)
    assert _settle(conn, ref) == 30
    rows = {r["ref_id"]: r for r in _releases(conn)}
    assert rows[9301]["qty"] == 20 and rows[9301]["unit_price"] == pytest.approx(30_000.0)
    assert rows[9301]["source"] == "contract" and rows[9301]["index_run_id"] == p1.run_id
    assert rows[9302]["qty"] == 5
    assert rows[9302]["unit_price"] == pytest.approx(400e6 * q_c / (5 * q_c + q_hulk))
    assert rows[9303]["qty"] == 5 and rows[9303]["unit_price"] is None
    assert _hold(conn, ref, p1.run_id, comp).observed_qty == 30


def test_an_expired_hold_keeps_its_releases_and_takes_no_more(conn, ref):
    """Rulings (b), (c): the second Mark executed after the delivery
    ends run 1's hold — its releases stay as history, its remainder is
    returned to stock, and a sale dated after its end goes to run 2's
    hold. (A sale dated inside its life still lands on it: the window is
    the hold's own, see the late-pull test.)"""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1, p2, _c1 = _two_runs(conn, route)
    comp = route.composite_id
    _sell(conn, 9401, comp, 30, p2.delivered + 12 * HOUR)
    assert _settle(conn, ref) == 30  # run 1 takes 30
    c2 = p2.delivered + DAY
    complete_run(conn, p2.run_id, c2)  # run 1's hold expires
    store.save_industry_jobs(conn, [], pulled_at=c2 + HOUR)  # run 2's window covered
    assert [h.index_run_id for h in holds.live(conn, ref, store.get_settings(conn))] == [p2.run_id]
    _sell(conn, 9402, comp, 30, c2 + DAY)
    assert _settle(conn, ref) == 30
    assert _by_run(conn) == {p1.run_id: 30, p2.run_id: 30}  # run 1's history stays; the new sale went to run 2
    hold1 = _hold(conn, ref, p1.run_id, comp)
    assert hold1.released_qty == 30 and hold1.remaining == p1.qty - 30
    assert hold1.status == "returned to stock" and hold1.expired_at == c2


def test_alchemy_sales_view_totals_held_and_manual(conn, ref):
    """The view's figures against independent money: a sale split over
    two holds is two rows and ONE sale, netted at the station it sold
    at and costed at each hold's own route cost; an unpriced hand mark
    is counted apart; Still held carries each hold's remainder and
    expiry run; held_totals sums the live holds."""
    route = _route(ref, "Ferrofluid")
    _sellers(conn)
    p1, p2, _c1 = _two_runs(conn, route, cost1=50.0, cost2=60.0)
    comp = route.composite_id
    sold = p1.qty + 40
    _sell(conn, 9501, comp, sold, p2.delivered + DAY)
    assert _settle(conn, ref) == sold
    store.add_alchemy_release(conn, p2.run_id, comp, 10, "manual", unit_cost=60.0)
    conn.commit()
    view = _view(conn, ref)
    assert view["any"]
    market_rows = sorted((s for s in view["sales"] if not s["manual"]), key=lambda s: s["run_number"])
    assert [(s["run_number"], s["quantity"], s["unit_cost"]) for s in market_rows] == [
        (1, p1.qty, 50.0), (2, 40, 60.0)
    ]
    assert all(s["ref_id"] == 9501 and s["source"] == "market" and s["system_name"] == "Jita" for s in market_rows)
    net1, net2 = _net_hub(conn, ref, comp, 30_000.0, p1.qty), _net_hub(conn, ref, comp, 30_000.0, 40)
    assert market_rows[0]["net"] == pytest.approx(net1) and market_rows[1]["net"] == pytest.approx(net2)
    tot = view["totals"]
    assert tot["units"] == sold and tot["sales"] == 1  # one sale, two rows
    assert tot["gross"] == pytest.approx(sold * 30_000.0)
    assert tot["net"] == pytest.approx(net1 + net2)
    assert tot["cost"] == pytest.approx(p1.qty * 50.0 + 40 * 60.0)
    assert tot["profit"] == pytest.approx(tot["net"] - tot["cost"])
    assert tot["manual_units"] == 10 and tot["unpriced"] == 0 and tot["unpriced_units"] == 0
    assert tot["manual_priced_units"] == 0  # the hand mark has no price: manual_units alone
    [manual] = [s for s in view["sales"] if s["manual"]]
    assert manual["net"] is None and manual["cost"] is None and manual["run_number"] == 2
    held = {h["run_number"]: h for h in view["held"]}
    assert held[1]["remaining"] == 0 and held[1]["status"] == "sold" and held[1]["expires_after"] == 2
    assert held[2]["remaining"] == p2.qty - 50 and held[2]["status"] == "delivered" and held[2]["expires_after"] == 3
    assert view["held_totals"]["units"] == p2.qty - 50
    assert view["held_totals"]["unquoted"] == 1  # no quote on record: the remainder cannot be valued
    assert view["planned_only"] == []
