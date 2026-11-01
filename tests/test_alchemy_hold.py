"""Alchemy for-sale holds (schema 14, user rulings 2026-10-31, rebuilt after
the review of the same day): the hold DERIVED from the job record
(magoo/holds.py) — anchored to the jobs ESI shows installed, frozen once
they appear, delivered and expiring by the run count — the planner hiding
the live piles (the unrefined pool first, then the hangar, less what is
listed), the profit tail's capacity and cap, the install check's for-sale
rationing and the pool-overflow trim, write_plan_holds' four rules, the
wordings (expiry_words in every state, changed_words for the Mark
executed / Reopen flashes), and the upgrade of a database stamped 14 in
the old shape.

Same fixture pattern as test_alchemy (real reference data, temp state DB,
FairValuePrices with overrides). The module-level helpers are shared with
test_alchemy_hold_web.py (the routes) and test_alchemy_hold_ledger.py
(settlement and the Ledger view).
"""

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from magoo import config, engine, holds, industry, ledger, store
from magoo.engine import PlanItem
from test_alchemy import (  # noqa: F401 — conn is the fixture
    FUEL_BLOCKS,
    add_pipeline,
    alchemy_items,
    conn,
    enable_alchemy,
    expensive_rare_inputs,
    reaction_candidates,
    snapshot,
)
from test_alchemy_profit import (
    HUB,
    _formula,
    _landed,
    _route_cost,
    enable_profit,
    off_chain_routes,
    reaction_jobs,
)
from test_ledger import A, add_owner

# A seeded run's opening: well inside ESI's history, before today.
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


# --- shared helpers (imported by the web and ledger hold tests) ---------------


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def reaction_job(job_id, type_id, start, units, status="active", end_date=None, completed_date=None) -> dict:
    """One industry_job row for store.save_industry_jobs: a reaction of
    `units` runs x portion 1. start / end / completed take datetimes or
    either timestamp text."""
    return {
        "job_id": int(job_id), "type_id": int(type_id),
        "activity_id": config.ACTIVITY_REACTION, "start_date": start,
        "runs": int(units), "units": int(units), "status": status,
        "end_date": end_date, "completed_date": completed_date,
    }


def run_opened_at(conn, run_id) -> datetime:
    """COALESCE(opened_at, planned_start) — where the run's buying window
    opens (no executed run before it)."""
    return store._utc_datetime(conn.execute(
        "SELECT COALESCE(opened_at, planned_start) FROM index_run WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()[0])


def install_jobs(
    conn, run_id, type_id, jobs, units_per_job, status="active", first_job_id=90000, start=None,
):
    """ESI shows `jobs` jobs of the formula started one second after the
    run opened (inside its buying window) — or at `start` — each 720 h
    long; the pull that saw them is logged one second after the start.
    Returns (start, end)."""
    start = start or run_opened_at(conn, run_id) + timedelta(seconds=1)
    end = start + timedelta(hours=720)
    rows = [
        reaction_job(first_job_id + n, type_id, start, units_per_job, status, end_date=end)
        for n in range(jobs)
    ]
    store.save_industry_jobs(conn, rows, pulled_at=start + timedelta(seconds=1))
    return start, end


def deliver_jobs(conn, job_ids, completed):
    """ESI shows the jobs delivered at `completed` (completed_date; the
    timer end stays), seen by a pull one second later."""
    ids = [int(j) for j in job_ids]
    when = store.utc_text(completed)
    conn.execute(
        "UPDATE industry_job SET status = 'delivered', completed_date = ?, "
        f"end_date = COALESCE(end_date, ?) WHERE job_id IN ({','.join('?' * len(ids))})",
        (when, when, *ids),
    )
    conn.commit()
    store.save_industry_jobs(
        conn, [], pulled_at=store._utc_datetime(completed) + timedelta(seconds=1)
    )


def installed_snapshot(snap, type_id, start, units):
    """The snapshot after the formula's jobs were installed: their output
    in flight, their start in job_starts."""
    return replace(
        snap,
        in_progress={**snap.in_progress, type_id: units},
        job_starts={type_id: [[_iso(start), units]]},
    )


def seed_hold(conn, run_id, composite_id, *, planned=0, route_unit_cost=None, baseline=0,
              chain_units=0, yield_=0.55):
    """A hold row with its anchors, as the plan's persist writes one."""
    store.upsert_hold(
        conn, run_id, composite_id, planned_qty=planned, route_unit_cost=route_unit_cost,
        baseline_units=baseline, chain_units=chain_units, yield_=yield_,
    )
    conn.commit()


def complete_run(conn, run_id, completed_at):
    """Mark executed, as web.run_complete flips the row."""
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = ? WHERE index_run_id = ?",
        (store.utc_text(completed_at), run_id),
    )
    conn.commit()


def reopen_run(conn, run_id):
    conn.execute(
        "UPDATE index_run SET status = 'planned', completed_at = NULL WHERE index_run_id = ?",
        (run_id,),
    )
    conn.commit()


def seed_run(conn, run_number, opened_at, status="planned", completed_at=None) -> int:
    """An index_run row by hand (the loader tests need windows, not plans)."""
    cur = conn.execute(
        "INSERT INTO index_run (run_number, planned_start, opened_at, status, completed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            run_number, store.utc_text(opened_at), store.utc_text(opened_at), status,
            store.utc_text(completed_at) if completed_at is not None else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def target_route(ref, baseline_plan):
    """(composite PlanItem, AlchemyRoute) of the first routed composite the
    chain BUILDS — on-chain, so the hold's bite is observable."""
    target = reaction_candidates(ref, baseline_plan)[0]
    return target, ref.alchemy_routes()[target.type_id]


def for_sale_world(conn, ref, *, cap=1000, quote=1e6, savings=False, profit=True):
    """Hulk x 8, alchemy on at `cap`, Alchemy for Profit on (unless
    `profit` is False), the on-chain target composite quoted at `quote`,
    a pull logged before the run opens. `savings` prices the target's rare
    goo at a fortune too, so its route wins savings swaps as well: a MIXED
    row (savings jobs first, for-sale jobs after). Returns (snapshot,
    route, settings)."""
    if not conn.execute("SELECT 1 FROM pipeline").fetchone():
        add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    target, route = target_route(ref, baseline)
    overrides = expensive_rare_inputs(ref, [target]) if savings else {}
    overrides[route.composite_id] = quote
    enable_alchemy(conn, cap=cap)
    if profit:
        enable_profit(conn)
    store.save_industry_jobs(conn, [])  # a pull before the run opens
    return snapshot(ref, overrides=overrides), route, store.get_settings(conn)


# --- local helpers -------------------------------------------------------------


def _chain_world(conn, ref):
    """Hulk x 8 with alchemy on (profit off): the target composite is a
    chain row, so a seeded hold's bite shows on it. Returns (snapshot,
    route, baseline plan)."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    _target, route = target_route(ref, baseline)
    enable_alchemy(conn, cap=1000)
    return snapshot(ref), route, baseline


def _a_route(ref):
    """Any route, for loader tests that never plan."""
    return ref.alchemy_routes()[ref.type_id("Ferrofluid")]


def _replan(conn, ref, snap, run_id, cycle_cut="opened"):
    """The ESI update's re-plan of the open run: in place, cut at the
    run's opening (the cycle's buying window)."""
    cut = run_opened_at(conn, run_id) if cycle_cut == "opened" else cycle_cut
    return engine.plan_index_run(
        conn, ref, snap, persist=True, replace_index_run_id=run_id, cycle_cut=cut
    )


def _hold_of(conn, ref, settings, run_id, composite_id):
    [hold] = [
        h for h in holds.load(conn, ref, settings, run_id=run_id)
        if h.composite_type_id == composite_id
    ]
    return hold


def _for_sale_row(plan, route):
    return plan.items.get(route.unrefined_id)


def _job_units(row) -> int:
    return row.max_runs_per_job * row.portion_size


def _sale_units(row) -> int:
    return row.alchemy_profit_jobs * _job_units(row)


def _expected_qty(units, route, yield_=0.55) -> int:
    """Composite units of `units` unrefined at the yield — D1's figure."""
    return math.floor(units * route.composite_qty * yield_)


def _direct_reactions(plan):
    return [
        i for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION and not i.alchemy_for_type_id
    ]


def _plan_shape(plan):
    return {
        t: (i.jobs_allocated, i.install_jobs, i.alchemy_profit_jobs, i.recommended_buy_qty, i.deficit_qty)
        for t, i in plan.items.items()
    }


def _ids(first, count):
    return list(range(first, first + count))


# --- the hold anchored to the installs through the real re-plan path ----------


def test_the_hold_is_anchored_to_the_installs_through_the_re_plan(conn, ref):
    """Plan -> install the listed jobs -> the ESI update's re-plan: the
    hold is what ESI shows installed beyond the chain's share, the
    installed jobs are not listed again (the cap and the pool count them),
    the pile is hidden from the composite's credit, and a second identical
    re-plan changes nothing (the open run's hold never feeds back)."""
    snap, route, settings = for_sale_world(conn, ref)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    row1 = _for_sale_row(plan1, route)
    assert row1.alchemy_profit_jobs == row1.jobs_allocated > 0  # a pure for-sale row
    per = _job_units(row1)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.planned_qty == _expected_qty(_sale_units(row1), route) > 0
    assert (hold.baseline_units, hold.chain_units) == (0, 0)
    assert hold.for_sale_qty == 0 and not hold.live and hold.status == "not installed"
    assert plan1.items[route.composite_id].held_for_sale_qty is None

    start, _end = install_jobs(conn, run1, route.unrefined_id, row1.jobs_allocated, per)
    units = row1.jobs_allocated * per
    plan2 = _replan(conn, ref, installed_snapshot(snap, route.unrefined_id, start, units), run1)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    expected = _expected_qty(_sale_units(row1), route)
    assert hold.for_sale_unrefined == _sale_units(row1)
    assert hold.for_sale_qty == expected == hold.planned_qty
    assert hold.chain_units == (row1.jobs_allocated - row1.alchemy_profit_jobs) * per
    assert hold.in_flight and hold.delivered_at is None and hold.live
    assert hold.status == "in flight"
    composite = plan2.items[route.composite_id]
    assert composite.held_for_sale_qty == expected
    assert composite.held_from_credit_qty == expected
    assert composite.in_progress_qty == _expected_qty(units, route) - expected
    # Installed jobs are not listed again: the pool and the cap count them.
    row2 = _for_sale_row(plan2, route)
    assert (row2.alchemy_profit_jobs if row2 else 0) == 0
    assert reaction_jobs(plan2, "install_jobs") + row1.jobs_allocated <= snap.slots_available[
        config.ACTIVITY_REACTION
    ]
    # The same world again: identical plan, identical hold.
    plan2b = _replan(conn, ref, installed_snapshot(snap, route.unrefined_id, start, units), run1)
    assert _plan_shape(plan2b) == _plan_shape(plan2)
    hold_b = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert (hold_b.for_sale_qty, hold_b.chain_units, hold_b.planned_qty, hold_b.route_unit_cost) == (
        hold.for_sale_qty, hold.chain_units, hold.planned_qty, hold.route_unit_cost
    )


def test_partial_installs_are_read_savings_first(conn, ref):
    """A mixed row's installs are the chain's up to chain_units: with only
    the savings jobs' count installed the hold is 0 (and frozen there by
    the re-plan); the rest installed later are the whole for-sale share."""
    snap, route, settings = for_sale_world(conn, ref, quote=1e7, savings=True)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    row = _for_sale_row(plan1, route)
    assert 0 < row.alchemy_profit_jobs < row.jobs_allocated, "a mixed row"
    savings_jobs = row.jobs_allocated - row.alchemy_profit_jobs
    per = _job_units(row)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.chain_units == savings_jobs * per and hold.baseline_units == 0

    start, _end = install_jobs(conn, run1, route.unrefined_id, savings_jobs, per)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.installed_units == savings_jobs * per
    assert hold.for_sale_unrefined == 0 and hold.for_sale_qty == 0
    assert not hold.live and hold.status == "not installed"
    plan2 = _replan(
        conn, ref, installed_snapshot(snap, route.unrefined_id, start, savings_jobs * per), run1
    )
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.chain_units == savings_jobs * per, "frozen: the installs are beyond the baseline"
    assert hold.for_sale_qty == 0
    assert plan2.items[route.composite_id].held_for_sale_qty is None

    install_jobs(conn, run1, route.unrefined_id, row.alchemy_profit_jobs, per, first_job_id=91000)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.for_sale_unrefined == _sale_units(row)
    assert hold.for_sale_qty == _expected_qty(_sale_units(row), route) == hold.planned_qty
    assert hold.live and hold.status == "in flight"


def test_installs_first_seen_inside_one_update_are_split_savings_first(conn, ref):
    """The ESI update writes the jobs, then re-plans: the row written
    BEFORE the installs is the anchor, so the hold is the for-sale share
    of what was installed (7 jobs -> 20,790 in production), never every
    installed job."""
    snap, route, settings = for_sale_world(conn, ref, quote=1e7, savings=True)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    row = _for_sale_row(plan1, route)
    assert 0 < row.alchemy_profit_jobs < row.jobs_allocated
    per = _job_units(row)
    all_units = row.jobs_allocated * per
    start, _end = install_jobs(conn, run1, route.unrefined_id, row.jobs_allocated, per)
    plan2 = _replan(conn, ref, installed_snapshot(snap, route.unrefined_id, start, all_units), run1)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.installed_units == all_units
    assert hold.for_sale_unrefined == _sale_units(row) < all_units
    assert hold.for_sale_qty == _expected_qty(_sale_units(row), route) == hold.planned_qty
    assert hold.live and hold.in_flight
    composite = plan2.items[route.composite_id]
    assert composite.held_for_sale_qty == hold.for_sale_qty
    assert composite.held_from_credit_qty == hold.for_sale_qty
    assert composite.in_progress_qty == _expected_qty(all_units, route) - hold.for_sale_qty


def test_a_job_installed_before_the_hold_existed_is_baseline(conn, ref):
    """Jobs of the formula already in the window when the row is first
    written (savings jobs installed before Alchemy for Profit was switched
    on, a stray install) are the chain's: baseline, never for sale. Jobs
    installed on top of them are the hold's."""
    snap, route, settings = for_sale_world(conn, ref, profit=False)
    plan0 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan0.index_run_id
    assert store.hold_rows(conn, run1) == []  # profit off: no proposal, no row
    start, _end = install_jobs(conn, run1, route.unrefined_id, 3, 100)  # by hand
    enable_profit(conn)
    settings = store.get_settings(conn)
    plan1 = _replan(conn, ref, installed_snapshot(snap, route.unrefined_id, start, 300), run1)
    row = _for_sale_row(plan1, route)
    assert row.alchemy_profit_jobs > 0
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    savings = (row.jobs_allocated - row.alchemy_profit_jobs) * _job_units(row)
    assert hold.baseline_units == 300 and hold.chain_units == 300 + savings
    assert hold.installed_units == 300 and hold.for_sale_qty == 0 and not hold.live
    assert hold.planned_qty == _expected_qty(_sale_units(row), route) > 0
    assert plan1.items[route.composite_id].held_for_sale_qty is None
    # The listed jobs installed on top of the stray ones — after the plan
    # that proposed them (the row's anchor) — are the hold's.
    install_jobs(
        conn, run1, route.unrefined_id, row.jobs_allocated, _job_units(row), first_job_id=91000,
        start=start + timedelta(hours=1),
    )
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.baseline_units == 300
    assert hold.for_sale_unrefined == row.alchemy_profit_jobs * _job_units(row)
    assert hold.for_sale_qty == hold.planned_qty and hold.live


def test_a_cancelled_for_sale_job_leaves_the_hold(conn, ref):
    """A job ESI shows cancelled (or reverted) will bring no pile: the hold
    drops by its output."""
    snap, route, settings = for_sale_world(conn, ref)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    row = _for_sale_row(plan1, route)
    per = _job_units(row)
    start, end = install_jobs(conn, run1, route.unrefined_id, row.jobs_allocated, per)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.for_sale_qty == _expected_qty(row.jobs_allocated * per, route)
    last = 90000 + row.jobs_allocated - 1
    store.save_industry_jobs(
        conn, [reaction_job(last, route.unrefined_id, start, per, "cancelled", end_date=end)],
        pulled_at=start + timedelta(seconds=2),
    )
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.installed_units == (row.jobs_allocated - 1) * per
    assert hold.for_sale_qty == _expected_qty((row.jobs_allocated - 1) * per, route)
    assert hold.live and hold.in_flight


def test_a_dropped_proposal_keeps_the_row_and_a_later_install_still_counts(conn, ref):
    """A plan never deletes a hold row: the composite dropped (quoted at
    nothing) leaves the row at planned_qty 0, and jobs of the formula
    installed afterwards are still read as the hold's."""
    snap, route, settings = for_sale_world(conn, ref)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    row = _for_sale_row(plan1, route)
    cheap = replace(snap, prices=snapshot(ref, overrides={route.composite_id: 1.0}).prices)
    plan_cheap = _replan(conn, ref, cheap, run1)
    assert not [a for a in alchemy_items(plan_cheap) if a.alchemy_for_type_id == route.composite_id]
    [db_row] = store.hold_rows(conn, run1)
    assert (db_row["planned_qty"], db_row["chain_units"], db_row["baseline_units"]) == (0, 0, 0)
    assert plan_cheap.items[route.composite_id].held_for_sale_qty is None
    # The user installs the jobs the first plan listed anyway.
    per = _job_units(row)
    start, _end = install_jobs(conn, run1, route.unrefined_id, row.jobs_allocated, per)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.planned_qty == 0
    assert hold.for_sale_qty == _expected_qty(row.jobs_allocated * per, route) > 0
    assert hold.live and hold.status == "in flight"
    # And the next cheap re-plan hides exactly that pile (the row is frozen).
    units = row.jobs_allocated * per
    plan3 = _replan(conn, ref, installed_snapshot(cheap, route.unrefined_id, start, units), run1)
    assert plan3.items[route.composite_id].held_for_sale_qty == hold.for_sale_qty
    hold_b = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert (hold_b.planned_qty, hold_b.for_sale_qty) == (0, hold.for_sale_qty)


def test_chain_units_route_cost_and_yield_follow_the_plan_until_installs_freeze_them(conn, ref):
    """Rule 2 then rule 3: before any install a re-plan rewrites the
    anchors (a new yield recomputes the proposal); once the formula's jobs
    are in the record, a later yield and a later price leave them."""
    snap, route, settings = for_sale_world(conn, ref)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    row1 = _for_sale_row(plan1, route)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.yield_ == 0.55 and hold.route_unit_cost == pytest.approx(row1.alchemy_route_unit_cost)
    # Nothing installed: the plan's figures follow the settings.
    conn.execute("UPDATE settings SET alchemy_reprocess_yield = 0.6")
    conn.commit()
    plan2 = _replan(conn, ref, snap, run1)
    row2 = _for_sale_row(plan2, route)
    hold = _hold_of(conn, ref, store.get_settings(conn), run1, route.composite_id)
    assert hold.yield_ == 0.6
    assert hold.planned_qty == _expected_qty(_sale_units(row2), route, 0.6)
    assert hold.route_unit_cost == pytest.approx(row2.alchemy_route_unit_cost)
    frozen_cost = hold.route_unit_cost
    # Installed: a later yield and price change nothing on the row.
    per = _job_units(row2)
    units = row2.jobs_allocated * per
    start, _end = install_jobs(conn, run1, route.unrefined_id, row2.jobs_allocated, per)
    conn.execute("UPDATE settings SET alchemy_reprocess_yield = 0.7")
    conn.commit()
    dear = replace(snap, prices=snapshot(ref, overrides={route.composite_id: 2e6}).prices)
    _replan(conn, ref, installed_snapshot(dear, route.unrefined_id, start, units), run1)
    hold = _hold_of(conn, ref, store.get_settings(conn), run1, route.composite_id)
    assert hold.yield_ == 0.6 and hold.route_unit_cost == pytest.approx(frozen_cost)
    assert hold.chain_units == (row2.jobs_allocated - row2.alchemy_profit_jobs) * per
    assert hold.for_sale_qty == _expected_qty(_sale_units(row2), route, 0.6)
    assert hold.planned_qty == _expected_qty(_sale_units(row2), route, 0.6)


# --- delivery and life, read from the record -----------------------------------


def test_delivered_at_follows_the_statuses(conn, ref):
    """'ready' is finished, not in the hangar; delivery is ESI's
    completed_date (the timer end only where it is missing); with batches
    delivered apart, delivered_at waits for the last one while the
    delivered units already make room for observed sales."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    start, end = install_jobs(conn, run1, route.unrefined_id, 4, 100, status="ready")
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.in_flight and hold.delivered_at is None and hold.delivered_units == 0
    assert hold.status == "in flight" and hold.jobs_end == end
    assert hold.room_observed == 0
    # The first two deliver, a day after their timer end.
    d1 = end + timedelta(days=1)
    deliver_jobs(conn, _ids(90000, 2), d1)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.delivered_units == 200 and hold.in_flight
    assert hold.delivered_at is None and hold.first_delivered == d1
    assert hold.room_observed == hold.delivered_qty == _expected_qty(200, route)
    assert hold.status == "in flight"
    # The rest a day later: delivered_at is the LAST delivery, not the timer end.
    d2 = end + timedelta(days=2)
    deliver_jobs(conn, _ids(90002, 2), d2)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert not hold.in_flight and hold.jobs_end is None
    assert (hold.first_delivered, hold.delivered_at) == (d1, d2)
    assert hold.delivered_units == 400 and hold.status == "delivered"
    assert hold.room_observed == _expected_qty(400, route) == hold.for_sale_qty
    # A record pulled before the column (no completed_date): the timer end stands in.
    conn.execute("UPDATE industry_job SET completed_date = NULL WHERE job_id BETWEEN 90000 AND 90003")
    conn.commit()
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert (hold.first_delivered, hold.delivered_at) == (end, end)


def test_a_hold_lives_from_its_installs_to_the_second_mark_executed_after_delivery(conn, ref):
    """Ruling (b) as a count: live while in flight, live through the run
    its jobs deliver in and the next, dropped by the second Mark executed
    at or after the delivery, revived when that run is reopened. A hold
    whose jobs were never installed is nothing at all."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, planned=500)
    _start, end = install_jobs(conn, run1, route.unrefined_id, 2, 100)

    def life():
        h = _hold_of(conn, ref, settings, run1, route.composite_id)
        return h.live, h.expires_after, h.expired_at

    assert life() == (True, None, None)  # in flight: no end in sight yet
    delivered = end + timedelta(days=1)
    deliver_jobs(conn, _ids(90000, 2), delivered)
    assert life() == (True, 2, None)  # its own run is open: ends with run 2
    c1 = delivered + timedelta(days=1)
    complete_run(conn, run1, c1)
    assert life() == (True, 2, None)
    run2 = seed_run(conn, 2, c1)
    assert life() == (True, 2, None)
    c2 = c1 + timedelta(days=30)
    complete_run(conn, run2, c2)
    assert life() == (False, 2, c2)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.status == "returned to stock" and hold.remaining == _expected_qty(200, route)
    assert [h.index_run_id for h in holds.live(conn, ref, settings)] == []
    assert holds.bites(conn, ref, settings) == {}
    reopen_run(conn, run2)
    assert life() == (True, 2, None)
    assert [h.index_run_id for h in holds.live(conn, ref, settings)] == [run1]
    assert route.composite_id in holds.bites(conn, ref, settings)
    # A proposal nobody installed: no figure, not live, nothing to hide.
    other = off_chain_routes(ref, engine.Plan(index_run_id=0, run_number=0, items={}))[0]
    seed_hold(conn, run2, other.composite_id, planned=500)
    idle = _hold_of(conn, ref, settings, run2, other.composite_id)
    assert (idle.for_sale_qty, idle.remaining, idle.live) == (0, 0, False)
    assert (idle.expires_after, idle.expired_at, idle.status) == (None, None, "not installed")
    assert other.composite_id not in holds.bites(conn, ref, settings)


def test_a_delivery_between_two_runs_windows_counts_the_same_way(conn, ref):
    """Jobs still running at Mark executed deliver before the next run
    exists: the hold lives through the run that opens next (the delivery
    lands in its window) and the one after, by the same count."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    _start, end = install_jobs(conn, run1, route.unrefined_id, 2, 100)
    c1 = end - timedelta(days=1)  # executed while the jobs still run
    complete_run(conn, run1, c1)

    def life():
        h = _hold_of(conn, ref, settings, run1, route.composite_id)
        return h.live, h.expires_after, h.expired_at

    assert life() == (True, None, None)
    delivered = end + timedelta(days=1)  # after run 1 was executed, no run 2 yet
    deliver_jobs(conn, _ids(90000, 2), delivered)
    assert life() == (True, 3, None)  # the next run is 2: ends with run 3
    run2 = seed_run(conn, 2, c1)
    assert life() == (True, 3, None)
    c2 = delivered + timedelta(days=1)
    complete_run(conn, run2, c2)
    assert life() == (True, 3, None)
    run3 = seed_run(conn, 3, c2)
    c3 = c2 + timedelta(days=30)
    complete_run(conn, run3, c3)
    assert life() == (False, 3, c3)


def test_an_executed_run_with_an_uncovered_window_is_a_plan_figure(conn, ref):
    """Mark executed before the next ESI update: the record cannot speak
    for the window, so the plan's proposal stands, badged (as hull_cost
    does for hulls) — until a pull covers the window and shows nothing
    installed. An OPEN run's uncovered window holds nothing, ever."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    store.save_industry_jobs(conn, [], pulled_at=T0 - timedelta(hours=1))  # the last pull predates the run
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, planned=500)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert not hold.covered and not hold.plan_figure
    assert hold.for_sale_qty == 0 and not hold.live
    c1 = T0 + timedelta(days=10)
    complete_run(conn, run1, c1)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.plan_figure and hold.for_sale_qty == hold.planned_qty == 500
    assert (hold.live, hold.expires_after, hold.expired_at) == (True, 2, None)
    assert holds.bites(conn, ref, settings)[route.composite_id].remaining == 500
    # The next pull covers the window: nothing of the proposal was installed.
    store.save_industry_jobs(conn, [], pulled_at=c1 + timedelta(hours=1))
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.covered and not hold.plan_figure
    assert hold.for_sale_qty == 0 and not hold.live
    assert holds.bites(conn, ref, settings) == {}


def test_an_open_run_with_an_uncovered_window_holds_nothing(conn, ref):
    """Jobs the pull log does not reach (the record shows them, the last
    logged pull predates them) are not the open run's: no plan-figure
    fallback for an open run."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, planned=500)
    start = T0 + timedelta(seconds=1)
    store.save_industry_jobs(
        conn,
        [reaction_job(90000 + n, route.unrefined_id, start, 100, end_date=start + timedelta(hours=720))
         for n in range(3)],
        pulled_at=T0 - timedelta(hours=1),
    )
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert not hold.covered and not hold.plan_figure
    assert (hold.installed_units, hold.for_sale_qty, hold.live) == (0, 0, False)
    assert holds.bites(conn, ref, settings) == {}


def test_a_hold_in_flight_outlives_the_runs_marked_executed_meanwhile(conn, ref):
    """Ruling (b) in flight: run 1's for-sale jobs still running (one of
    them 'ready') when run 2 is marked executed and run 3 is planned —
    the hold is live with no end in sight (expires_after None), and run
    3's plan, through the engine, hides its pile out of the unrefined
    credit. The jobs deliver AFTER run 2's completed_at, so run 2 never
    counts: the count starts at the delivery and the end is the open run
    3's successor; Mark executed of run 3 and then run 4 ends it."""
    snap, route, _baseline = _chain_world(conn, ref)
    settings = store.get_settings(conn)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    _start, end = install_jobs(conn, run1, route.unrefined_id, 3, 100)
    install_jobs(conn, run1, route.unrefined_id, 1, 100, status="ready", first_job_id=90003)
    hide = _expected_qty(400, route)
    c1 = T0 + timedelta(days=10)  # executed while every job still runs
    complete_run(conn, run1, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1 + timedelta(hours=1))
    run2 = seed_run(conn, 2, c1)
    c2 = c1 + timedelta(days=10)
    assert c2 < end  # run 2 is executed with the jobs still running too
    complete_run(conn, run2, c2)
    store.save_industry_jobs(conn, [], pulled_at=c2 + timedelta(hours=1))

    def life():
        h = _hold_of(conn, ref, settings, run1, route.composite_id)
        return h.live, h.expires_after, h.expired_at, h.status

    assert life() == (True, None, None, "in flight")
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.covered and not hold.plan_figure and hold.in_flight
    assert hold.for_sale_qty == hide and hold.jobs_end == end and hold.delivered_at is None
    assert [h.index_run_id for h in holds.live(conn, ref, settings)] == [run1]
    bite = holds.bites(conn, ref, settings)[route.composite_id]
    assert (bite.remaining, bite.listed, bite.hide) == (hide, 0, hide)
    # Run 3 planned through the engine: the pile, still unrefined and in
    # flight, is hidden out of the credit; planning changes nothing of the hold.
    run3 = seed_run(conn, 3, c2)
    snap.in_progress[route.unrefined_id] = 400
    plan = _replan(conn, ref, snap, run3)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty == hide and item.held_from_credit_qty == hide
    assert item.in_progress_qty == 0 and item.alchemy_credit_qty == 0
    assert life() == (True, None, None, "in flight")
    # Delivery after run 2's completed_at: run 2 does not count, the open
    # run 3 is the first run completed at or after it — ends with run 4.
    delivered = end + timedelta(days=1)
    deliver_jobs(conn, _ids(90000, 4), delivered)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert not hold.in_flight and hold.delivered_at == delivered and hold.jobs_end is None
    assert life() == (True, 4, None, "delivered")
    # The pile reprocessed into the hangar: the hide now comes off On hand.
    snap.in_progress[route.unrefined_id] = 0
    snap.on_hand[route.composite_id] = hide + 100
    item = engine.plan_index_run(conn, ref, snap, persist=False).items[route.composite_id]
    assert (item.held_for_sale_qty, item.held_from_credit_qty, item.on_hand_qty) == (hide, 0, 100)
    c3 = delivered + timedelta(days=1)
    complete_run(conn, run3, c3)
    store.save_industry_jobs(conn, [], pulled_at=c3 + timedelta(hours=1))
    assert life() == (True, 4, None, "delivered")  # the first Mark executed after the delivery
    run4 = seed_run(conn, 4, c3)
    assert life() == (True, 4, None, "delivered")
    c4 = c3 + timedelta(days=30)
    complete_run(conn, run4, c4)
    assert life() == (False, 4, c4, "returned to stock")
    assert holds.bites(conn, ref, settings) == {}
    item = engine.plan_index_run(conn, ref, snap, persist=False).items[route.composite_id]
    assert item.held_for_sale_qty is None and item.on_hand_qty == hide + 100


def test_an_executed_uncovered_window_is_read_from_the_record_where_it_shows_for_sale_jobs(conn, ref):
    """D1 refined (PROJECT.md §7): Mark executed before the next ESI
    update, the installs already on file — the record is read (the
    installed for-sale units, not the proposal) and the hold is no plan
    figure though no pull covers the window yet."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    store.save_industry_jobs(conn, [], pulled_at=T0 - timedelta(hours=1))
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, planned=500)
    start, _end = install_jobs(conn, run1, route.unrefined_id, 3, 100)  # pulled at start + 1 s
    c1 = start + timedelta(days=5)
    complete_run(conn, run1, c1)  # no pull after
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    installed = _expected_qty(300, route)
    assert installed == 12_045 != 500  # 300 x 73 x 0.55: the record's figure, not the proposal's
    assert not hold.covered and not hold.plan_figure
    assert (hold.installed_units, hold.for_sale_unrefined, hold.for_sale_qty) == (300, 300, installed)
    assert hold.planned_qty == 500
    assert hold.live and hold.in_flight and hold.expires_after is None
    assert hold.status == "in flight"
    assert holds.bites(conn, ref, settings)[route.composite_id].remaining == installed


def test_an_executed_uncovered_window_a_pull_reaches_into_is_read_not_a_plan_figure(conn, ref):
    """D1 refined (review 2026-10-31): the record shows jobs of the formula,
    all the chain's read against chain_units, and a pull inside the window
    covers it up to that pull — so the record speaks (nothing was
    installed for sale before it) and the proposal does NOT stand in,
    though the tail since the pull is unknown; a covering pull later finds
    the same. Only a window no pull reaches into falls back (see
    test_an_open_run_with_no_pull_on_record_holds_nothing)."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    store.save_industry_jobs(conn, [], pulled_at=T0 - timedelta(hours=1))
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, planned=500, chain_units=300)  # three savings jobs planned
    start, _end = install_jobs(conn, run1, route.unrefined_id, 3, 100)  # exactly the savings jobs
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert (hold.installed_units, hold.for_sale_qty, hold.live, hold.status) == (300, 0, False, "not installed")
    c1 = start + timedelta(days=5)
    complete_run(conn, run1, c1)  # no pull after; the jobs' pull sits inside the window
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert not hold.covered and not hold.plan_figure
    assert hold.installed_units == 300 and hold.for_sale_unrefined == 0
    assert (hold.for_sale_qty, hold.live, hold.status) == (0, False, "not installed")
    assert holds.bites(conn, ref, settings) == {}
    # A covering pull: the savings jobs are still all there is.
    store.save_industry_jobs(conn, [], pulled_at=c1 + timedelta(hours=1))
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.covered and not hold.plan_figure
    assert (hold.installed_units, hold.for_sale_qty, hold.live, hold.status) == (300, 0, False, "not installed")
    assert (hold.expires_after, hold.expired_at) == (None, None)
    assert holds.bites(conn, ref, settings) == {}


def test_an_open_run_with_no_pull_on_record_holds_nothing(conn, ref):
    """No pull ever logged and nothing on file: the open run's hold is 0
    and no plan figure (an executed run's falls back the moment it is
    marked executed, the record being silent)."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    assert store.last_job_pull(conn) is None
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, planned=500)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert (hold.covered, hold.plan_figure, hold.for_sale_qty) == (False, False, 0)
    assert (hold.live, hold.expires_after, hold.status) == (False, None, "not installed")
    assert holds.bites(conn, ref, settings) == {}
    complete_run(conn, run1, T0 + timedelta(days=10))
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.plan_figure and hold.for_sale_qty == 500 and hold.live and hold.expires_after == 2


def test_a_discarded_run_takes_its_hold_and_releases_with_it(conn, ref):
    """Rows go only with their run (ON DELETE CASCADE)."""
    route = _a_route(ref)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, planned=500)
    store.add_alchemy_release(conn, run1, route.composite_id, 5, "manual")
    conn.commit()
    conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run1,))
    conn.commit()
    assert store.hold_rows(conn) == [] and store.release_rows(conn) == []


# --- the wordings: expiry_words and changed_words --------------------------------


def test_expiry_words_in_every_state(conn, ref):
    """holds.expiry_words, one state at a time, each built through the
    loader: not installed; in flight with and without a timer end (and
    still in flight through two Mark executed — ruling b); delivered and
    live; ended by the second Mark executed after the delivery, dated;
    a live plan figure; a plan figure a newer executed run ended; a
    superseded plan. The dates are computed by hand from T0 (2026-09-01
    12:00): jobs start a second after their run opens and run 720 h."""
    ferro = _a_route(ref)
    hyper = ref.alchemy_routes()[ref.type_id("Hyperflurite")]
    settings = store.get_settings(conn)

    def words(run_id, route):
        return holds.expiry_words(_hold_of(conn, ref, settings, run_id, route.composite_id))

    store.save_industry_jobs(conn, [], pulled_at=T0 - timedelta(hours=1))
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, ferro.composite_id)
    seed_hold(conn, run1, hyper.composite_id)
    assert words(run1, ferro) == "not installed yet"
    start, end = install_jobs(conn, run1, ferro.unrefined_id, 2, 100)
    assert words(run1, ferro) == (
        "until the run after the one its jobs deliver in is marked executed — jobs end 2026-10-01 12:00"
    )
    # A job on record without a timer end (pulled before the column).
    store.save_industry_jobs(
        conn, [reaction_job(95000, hyper.unrefined_id, start, 100)], pulled_at=start + timedelta(seconds=2)
    )
    assert words(run1, hyper) == "until the run after the one its jobs deliver in is marked executed"
    delivered = end + timedelta(days=1)  # 2026-10-02 12:00:01
    deliver_jobs(conn, _ids(90000, 2), delivered)
    assert words(run1, ferro) == "until run 2 is marked executed"  # its own run is the open one
    c1 = delivered + timedelta(days=1)
    complete_run(conn, run1, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1 + timedelta(hours=1))
    assert words(run1, ferro) == "until run 2 is marked executed"
    c2 = c1 + timedelta(days=30)  # 2026-11-02 12:00:01
    seed_run(conn, 2, c1, status="complete", completed_at=c2)
    assert words(run1, ferro) == "ended when run 2 was marked executed (2026-11-02 12:00)"
    assert words(run1, hyper) == "until the run after the one its jobs deliver in is marked executed"
    # Run 3 executed before a pull covers it: the plan's figure stands
    # in, live; a newer run executed meanwhile ends it.
    run3 = seed_run(conn, 3, c2)
    seed_hold(conn, run3, ferro.composite_id, planned=500)
    c3 = c2 + timedelta(days=10)
    complete_run(conn, run3, c3)  # no pull after
    assert words(run3, ferro) == "until run 4 is marked executed (plan figure)"
    c4 = c3 + timedelta(days=10)
    seed_run(conn, 4, c3, status="complete", completed_at=c4)
    hold3 = _hold_of(conn, ref, settings, run3, ferro.composite_id)
    assert hold3.plan_figure and not hold3.live and hold3.expired_at is None
    assert words(run3, ferro) == "ended — a newer run is executed"
    # A plan neither executed nor the newest has no window: superseded.
    run5 = seed_run(conn, 5, c4)
    seed_hold(conn, run5, ferro.composite_id, planned=500)
    install_jobs(conn, run5, ferro.unrefined_id, 1, 100, first_job_id=97000)
    assert words(run5, ferro) == (
        "until the run after the one its jobs deliver in is marked executed — jobs end 2026-12-22 12:00"
    )
    seed_run(conn, 6, c4 + timedelta(days=1))
    hold5 = _hold_of(conn, ref, settings, run5, ferro.composite_id)
    assert hold5.superseded and not hold5.live and hold5.for_sale_qty == 0
    assert words(run5, ferro) == "superseded — the run was reopened and re-planned"


def _hand_hold(run_id, run_number, composite_id, *, for_sale=0, live=True, plan_figure=False, observed=0):
    """A Hold as the loader hands it, without a database: the figures
    changed_words reads (run, composite, live, plan_figure and remaining
    = for_sale - observed). The run id doubles as its number."""
    hold = holds.Hold(
        index_run_id=run_id, run_number=run_number, run_status="complete", opened_at=None,
        completed_at=None, composite_type_id=composite_id, unrefined_type_id=None, composite_qty=0,
        planned_qty=for_sale, route_unit_cost=None, baseline_units=0, chain_units=0, yield_=0.55,
    )
    hold.for_sale_qty = for_sale
    hold.observed_qty = observed
    hold.live = live
    hold.plan_figure = plan_figure
    return hold


def test_the_stockpile_note_names_the_latest_ending_live_hold_of_a_composite(ref):
    """Review 2026-10-31 (fresh finding, Stockpile 'until' note): with two
    live holds hiding one composite, the note names the one that ends
    LAST — an in-flight hold over a delivered one whatever their runs, a
    delivered hold with the later expires_after, and the newer run only
    as a tie-break; each composite is read apart."""
    hyper = ref.type_id("Hyperflurite")
    ferro = ref.type_id("Ferrofluid")
    older_in_flight = _hand_hold(3, 3, hyper, for_sale=1_000)
    older_in_flight.in_flight = True
    newer_delivered = _hand_hold(4, 4, hyper, for_sale=1_000)
    newer_delivered.delivered_at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    newer_delivered.expires_after = 5
    # Oldest run first, as holds.live hands them: the newer run would
    # otherwise win by order alone.
    best = holds.latest_ending([older_in_flight, newer_delivered])
    assert best[hyper] is older_in_flight
    assert holds.expiry_words(best[hyper]).startswith("until the run after the one its jobs deliver in")
    # Two delivered holds: the later expires_after, not the newer run.
    a = _hand_hold(3, 3, hyper, for_sale=1_000)
    a.delivered_at, a.expires_after = datetime(2026, 10, 1, tzinfo=timezone.utc), 6
    b = _hand_hold(4, 4, hyper, for_sale=1_000)
    b.delivered_at, b.expires_after = datetime(2026, 10, 2, tzinfo=timezone.utc), 5
    assert holds.latest_ending([a, b])[hyper] is a
    # Equal ends: the newer run; another composite is read on its own.
    b.expires_after = 6
    other = _hand_hold(2, 2, ferro, for_sale=10)
    other.delivered_at, other.expires_after = datetime(2026, 9, 1, tzinfo=timezone.utc), 3
    best = holds.latest_ending([other, a, b])
    assert best[hyper] is b and best[ferro] is other
    assert holds.latest_ending([]) == {}


def test_changed_words_for_an_ended_hold(ref):
    """A live hold gone from the live set (absent, or listed and not live)
    is worded ended with its REMAINING units, thousands-separated."""
    hyper = ref.type_id("Hyperflurite")
    before = [_hand_hold(7, 7, hyper, for_sale=17_615)]
    ended = "run 7's for-sale hold ended: 17,615 Hyperflurite unsold now count as stock"
    assert holds.changed_words(ref, before, []) == [ended]
    assert holds.changed_words(ref, before, [_hand_hold(7, 7, hyper, for_sale=17_615, live=False)]) == [ended]
    partly_sold = [_hand_hold(7, 7, hyper, for_sale=17_615, observed=615)]
    assert holds.changed_words(ref, partly_sold, []) == [
        "run 7's for-sale hold ended: 17,000 Hyperflurite unsold now count as stock"
    ]
    # A real hold keeps its wording whichever run was reopened.
    assert holds.changed_words(ref, before, [], reopened=7) == [ended]


def test_changed_words_for_an_ended_plan_figure(ref):
    """A plan-figure hold was never a pile: "count as stock again", never
    "unsold"; the reopened run's own stand-in is "withdrawn" (reopened =
    its index_run_id), any other run's reopen leaves it "ended"."""
    ferro = ref.type_id("Ferrofluid")
    before = [_hand_hold(9, 9, ferro, for_sale=500, plan_figure=True)]
    ended = "run 9's plan-figure hold ended: 500 Ferrofluid count as stock again"
    assert holds.changed_words(ref, before, []) == [ended]
    assert holds.changed_words(ref, before, [], reopened=8) == [ended]
    assert holds.changed_words(ref, before, [], reopened=9) == [
        "run 9's plan-figure hold withdrawn: 500 Ferrofluid count as stock again — "
        "the reopened run holds only what an ESI update shows installed"
    ]
    assert holds.changed_words(ref, before, [_hand_hold(9, 9, ferro, for_sale=500, plan_figure=True, live=False)]) == [
        ended
    ]


def test_changed_words_for_a_revived_hold_and_a_stand_in(ref):
    """A hold live now and not before is revived (Reopen of the run that
    ended it); a plan-figure hold appearing live is the stand-in Mark
    executed puts up before a pull covers the run, not a revival."""
    hyper = ref.type_id("Hyperflurite")
    after = [_hand_hold(7, 7, hyper, for_sale=17_615)]
    revived = "run 7's for-sale hold is live again: 17,615 Hyperflurite hidden from stock"
    assert holds.changed_words(ref, [], after) == [revived]
    assert holds.changed_words(ref, [_hand_hold(7, 7, hyper, for_sale=17_615, live=False)], after) == [revived]
    stand_in = [_hand_hold(8, 8, hyper, for_sale=500, plan_figure=True)]
    stands = (
        "run 8's for-sale hold stands at the plan's figure until the next ESI update: "
        "500 Hyperflurite hidden from stock"
    )
    assert holds.changed_words(ref, [], stand_in) == [stands]
    assert holds.changed_words(ref, [_hand_hold(8, 8, hyper, for_sale=0, live=False)], stand_in) == [stands]


def test_changed_words_say_nothing_for_an_unchanged_or_empty_hold(ref):
    """Live on both sides (its figures moved or not), dead on both sides,
    or nothing remaining: no clause."""
    hyper = ref.type_id("Hyperflurite")
    ferro = ref.type_id("Ferrofluid")
    live = _hand_hold(7, 7, hyper, for_sale=17_615)
    assert holds.changed_words(ref, [live], [live]) == []
    assert holds.changed_words(ref, [live], [_hand_hold(7, 7, hyper, for_sale=17_615, observed=100)]) == []
    dead = _hand_hold(7, 7, hyper, for_sale=17_615, live=False)
    assert holds.changed_words(ref, [dead], [dead]) == []
    assert holds.changed_words(ref, [dead], []) == []
    assert holds.changed_words(ref, [], [dead]) == []
    # Remaining 0: an ended, revived or stand-in hold with nothing left is no event.
    sold_out = [_hand_hold(7, 7, hyper, for_sale=100, observed=100)]
    assert holds.changed_words(ref, sold_out, []) == []
    assert holds.changed_words(ref, [], sold_out) == []
    assert holds.changed_words(ref, [], [_hand_hold(8, 8, ferro, for_sale=0, plan_figure=True)]) == []
    assert holds.changed_words(ref, [_hand_hold(8, 8, ferro, for_sale=500, plan_figure=True, observed=500)], []) == []
    assert holds.changed_words(ref, [], []) == []


def test_changed_words_are_ordered_by_run_then_composite_ended_before_revived(ref):
    """The ended clauses come first, then the revived ones, each group in
    (index_run_id, composite type id) order, whatever order the lists
    came in."""
    hyper, ferro = ref.type_id("Hyperflurite"), ref.type_id("Ferrofluid")
    lo, hi = sorted([hyper, ferro])
    name = {lo: ref.type_info(lo).name, hi: ref.type_info(hi).name}
    assert (lo, hi) == (hyper, ferro) and name == {hyper: "Hyperflurite", ferro: "Ferrofluid"}
    before = [
        _hand_hold(9, 9, hi, for_sale=300),
        _hand_hold(7, 7, hi, for_sale=200),
        _hand_hold(7, 7, lo, for_sale=100),
        _hand_hold(3, 3, lo, for_sale=50, live=False),
        _hand_hold(5, 5, lo, for_sale=60),  # live on both sides: silent
    ]
    after = [
        _hand_hold(5, 5, lo, for_sale=60),
        _hand_hold(3, 3, lo, for_sale=50),
        _hand_hold(2, 2, hi, for_sale=40),
    ]
    assert holds.changed_words(ref, before, after) == [
        f"run 7's for-sale hold ended: 100 {name[lo]} unsold now count as stock",
        f"run 7's for-sale hold ended: 200 {name[hi]} unsold now count as stock",
        f"run 9's for-sale hold ended: 300 {name[hi]} unsold now count as stock",
        f"run 2's for-sale hold is live again: 40 {name[hi]} hidden from stock",
        f"run 3's for-sale hold is live again: 50 {name[lo]} hidden from stock",
    ]


# --- what planning hides -------------------------------------------------------


def test_a_proposal_only_hold_changes_nothing_in_the_composite_row(conn, ref):
    """Before its jobs are installed an open run's hold hides nothing: the
    composite row is exactly what the plan without the tail gives, with
    the row present and through the re-plan."""
    snap, route, _settings = for_sale_world(conn, ref, profit=False)
    control = engine.plan_index_run(conn, ref, snap, persist=False)
    enable_profit(conn)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    assert store.hold_rows(conn, plan1.index_run_id)  # the proposal is on record
    plan2 = _replan(conn, ref, snap, plan1.index_run_id)
    want = control.items[route.composite_id]
    for plan in (plan1, plan2):
        got = plan.items[route.composite_id]
        assert got.held_for_sale_qty is None and got.held_from_credit_qty is None
        assert (got.on_hand_qty, got.in_progress_qty, got.deficit_qty, got.jobs_allocated) == (
            want.on_hand_qty, want.in_progress_qty, want.deficit_qty, want.jobs_allocated
        )


def _executed_pile(conn, ref, route, delivered_jobs, running_jobs=0, per_job=100):
    """Run 1 seeded, its hold installed as `delivered_jobs` delivered jobs
    plus `running_jobs` still in flight, the run executed and covered.
    Returns (run_id, hide) with hide the whole for-sale pile."""
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    _start, end = install_jobs(conn, run1, route.unrefined_id, delivered_jobs + running_jobs, per_job)
    deliver_jobs(conn, _ids(90000, delivered_jobs), end + timedelta(days=1))
    complete_run(conn, run1, end + timedelta(days=2))
    store.save_industry_jobs(conn, [], pulled_at=end + timedelta(days=2, hours=1))
    return run1, _expected_qty((delivered_jobs + running_jobs) * per_job, route)


def test_the_hide_comes_from_the_unrefined_pool_first_then_the_hangar(conn, ref):
    """A hold larger than the credit (part of the pile reprocessed into the
    hangar, the rest still unrefined in flight): the hide takes the
    credited pool first, then the hangar; the deficit grows by what was
    hidden; the recovered goo keeps its credit."""
    snap, route, _baseline = _chain_world(conn, ref)
    # Small jobs: the pile stays below the composite's target, so neither
    # deficit is clamped at 0 and the growth is exactly the hide.
    b1, b2, per = 3, 5, 10
    snap.on_hand[route.composite_id] = _expected_qty(b1 * per, route) + 200
    snap.in_progress[route.unrefined_id] = b2 * per  # still unrefined, in flight
    without = engine.plan_index_run(conn, ref, snap, persist=False)
    recovered_before = {
        m: without.items[m].alchemy_credit_qty for m, _q in route.recovered if m in without.items
    }
    _run1, hide = _executed_pile(conn, ref, route, b1, b2, per)
    pool = _expected_qty(b2 * per, route)  # the credit's own conversion of the in-flight pile
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item, before = plan.items[route.composite_id], without.items[route.composite_id]
    assert before.alchemy_credit_qty == pool and before.in_progress_qty == pool
    assert item.held_for_sale_qty == hide
    assert item.held_from_credit_qty == pool
    assert item.in_progress_qty == 0 and item.alchemy_credit_qty == 0
    assert item.on_hand_qty == snap.on_hand[route.composite_id] - (hide - pool)
    assert before.deficit_qty > 0  # unclamped on both sides
    assert item.deficit_qty == before.deficit_qty + hide
    for m, qty in recovered_before.items():
        assert plan.items[m].alchemy_credit_qty == qty


def test_the_composites_own_output_in_flight_is_never_hidden(conn, ref):
    """The composite's direct reactions in flight are the chain's: the hide
    comes out of the unrefined pool and the hangar only, so with the pool
    short of the hold the direct output stays whole."""
    snap, route, _baseline = _chain_world(conn, ref)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    install_jobs(conn, run1, route.unrefined_id, 4, 100)
    hide = _expected_qty(400, route)
    snap.in_progress[route.unrefined_id] = 200  # the snapshot lags: half the pile
    snap.in_progress[route.composite_id] = 777  # the chain's own direct jobs
    pool = _expected_qty(200, route)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty == pool < hide
    assert item.held_from_credit_qty == pool
    assert item.in_progress_qty == 777 and item.on_hand_qty == 0


def test_listed_units_come_off_the_hide(conn, ref, monkeypatch):
    """Units already on the market have left ESI assets: the hide is the
    remaining less what is listed, never below nothing."""
    snap, route, _baseline = _chain_world(conn, ref)
    _run1, hide = _executed_pile(conn, ref, route, 4)
    snap.on_hand[route.composite_id] = hide + 100
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[route.composite_id].held_for_sale_qty == hide
    lots: dict = {}
    # Lots are dated: a listing issued after the pile delivered (the hold is
    # live, its life open-ended) is the pile's.
    monkeypatch.setattr(
        "magoo.ledger.listed_lots", lambda c, ids: {t: list(v) for t, v in lots.items()}, raising=False,
    )
    lots[route.composite_id] = [("2099-01-01T00:00:00Z", 1000)]
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty == hide - 1000 and item.on_hand_qty == 1100
    lots[route.composite_id] = [("2099-01-01T00:00:00Z", hide + 5)]
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty is None and item.on_hand_qty == hide + 100


def test_listed_units_on_a_real_open_sell_order_come_off_the_hide(conn, ref):
    """The same through ledger.listed_units: an open sell order of a
    Count-sales owner; a closed order and a sales-off owner's count nothing."""
    import magoo.ledger as ledger

    if not hasattr(ledger, "listed_units"):
        pytest.skip("ledger.listed_units is not there yet")
    snap, route, _baseline = _chain_world(conn, ref)
    _run1, hide = _executed_pile(conn, ref, route, 4)
    snap.on_hand[route.composite_id] = hide + 100
    add_owner(conn, A)
    add_owner(conn, A + 1, count_sales=0)

    def order(order_id, owner, remain, state):
        # Issued after the pile delivered: inside the live hold's life
        # (bites attributes each lot by its issue date, review 2026-10-31).
        conn.execute(
            "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, "
            "price, volume_total, volume_remain, location_id, duration, issued, state, "
            "first_seen_at, last_seen_at) VALUES (?, 'character', ?, 'character', ?, 1e6, ?, ?, "
            "60003760, 90, '2099-01-01T00:00:00Z', ?, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
            (order_id, owner, route.composite_id, remain, remain, state),
        )
        conn.commit()

    order(1, A, 1000, "open")
    order(2, A, 999, "cancelled")
    order(3, A + 1, 999, "open")
    assert ledger.listed_units(conn, [route.composite_id]) == {route.composite_id: 1000}
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty == hide - 1000 and item.on_hand_qty == 1100


def test_listed_units_are_attributed_oldest_first(conn, ref, monkeypatch):
    """A listing is attributed by its ISSUE date to the holds whose life
    contains it, oldest first (review 2026-10-31): an expired pile listed
    while it lived is the listing's home, so it does not shrink a live
    pile's hide; a listing issued after that hold ended cannot be its
    pile and comes off the live one."""
    snap, route, _baseline = _chain_world(conn, ref)
    settings = store.get_settings(conn)
    # Run 1: delivered, then expired by two Mark executed.
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    _s, end1 = install_jobs(conn, run1, route.unrefined_id, 3, 100)
    deliver_jobs(conn, _ids(90000, 3), end1 + timedelta(days=1))
    c1 = end1 + timedelta(days=2)
    complete_run(conn, run1, c1)
    c2 = c1 + timedelta(days=30)
    seed_run(conn, 2, c1, status="complete", completed_at=c2)
    store.save_industry_jobs(conn, [], pulled_at=c2 + timedelta(hours=1))
    # Run 3: open, its pile delivered.
    run3 = seed_run(conn, 3, c2)
    seed_hold(conn, run3, route.composite_id)
    _s, end3 = install_jobs(conn, run3, route.unrefined_id, 5, 100, first_job_id=93000)
    deliver_jobs(conn, _ids(93000, 5), end3 + timedelta(days=1))
    expired, live = (_hold_of(conn, ref, settings, r, route.composite_id) for r in (run1, run3))
    assert not expired.live and expired.remaining == _expected_qty(300, route) > 0
    assert live.live and live.remaining == _expected_qty(500, route)
    r1, r3 = expired.remaining, live.remaining
    lots: dict = {}
    monkeypatch.setattr(
        "magoo.ledger.listed_lots", lambda c, ids: {t: list(v) for t, v in lots.items()}, raising=False,
    )
    in_life_1 = store.utc_text(end1 + timedelta(days=1, hours=1))  # while run 1's pile lived
    after_3 = store.utc_text(end3 + timedelta(days=2))  # after run 3's delivery, run 1 long ended
    lots[route.composite_id] = [(in_life_1, r1)]  # the expired pile, listed whole while it lived
    bite = holds.bites(conn, ref, settings)[route.composite_id]
    assert (bite.remaining, bite.listed, bite.hide) == (r3, 0, r3)
    lots[route.composite_id] = [(in_life_1, r1), (after_3, 40)]
    bite = holds.bites(conn, ref, settings)[route.composite_id]
    assert (bite.remaining, bite.listed, bite.hide) == (r3, 40, r3 - 40)
    # Issued after run 1's hold ended: not its pile, whatever its size — the
    # live pile's, up to what it has delivered and not released.
    lots[route.composite_id] = [(after_3, r1)]
    bite = holds.bites(conn, ref, settings)[route.composite_id]
    assert (bite.remaining, bite.listed, bite.hide) == (r3, min(r1, r3), r3 - min(r1, r3))
    lots[route.composite_id] = [(in_life_1, r1), (after_3, 40)]
    snap.on_hand[route.composite_id] = r1 + r3
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[route.composite_id].held_for_sale_qty == r3 - 40


def test_an_unrefined_pile_with_alchemy_off_bites_nothing(conn, ref):
    """With alchemy off the unrefined pile is uncounted stock: the hide
    comes off that uncounted pool, and nothing counted moves."""
    snap, route, _baseline = _chain_world(conn, ref)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    install_jobs(conn, run1, route.unrefined_id, 4, 100)
    conn.execute("UPDATE settings SET alchemy_enabled = 0")
    conn.commit()
    assert holds.bites(conn, ref, store.get_settings(conn))[route.composite_id].hide == _expected_qty(400, route)
    snap.in_progress[route.unrefined_id] = 400
    snap.on_hand[route.composite_id] = 400
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.alchemy_credit_qty == 0 and item.in_progress_qty == 0
    assert item.held_for_sale_qty is None and item.held_from_credit_qty is None
    assert item.on_hand_qty == 400


def test_a_hold_never_hides_more_than_the_stock(conn, ref):
    snap, route, _baseline = _chain_world(conn, ref)
    _run1, hide = _executed_pile(conn, ref, route, 10)
    snap.on_hand[route.composite_id] = 50
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert hide > 50
    assert item.held_for_sale_qty == 50 and item.held_from_credit_qty == 0
    assert item.on_hand_qty == 0 and item.in_progress_qty == 0


def test_two_live_holds_on_one_composite_hide_both_piles(conn, ref):
    """Run N executed and delivered (its pile in the hangar), run N + 1 open
    with its jobs in flight: the hide is both remainders, the in-flight
    one out of the credit."""
    snap, route, _baseline = _chain_world(conn, ref)
    settings = store.get_settings(conn)
    run1, ra = _executed_pile(conn, ref, route, 3)
    c1 = store._utc_datetime(conn.execute(
        "SELECT completed_at FROM index_run WHERE index_run_id = ?", (run1,)
    ).fetchone()[0])
    run2 = seed_run(conn, 2, c1)
    seed_hold(conn, run2, route.composite_id)
    install_jobs(conn, run2, route.unrefined_id, 5, 100, first_job_id=92000)
    rb = _expected_qty(500, route)
    a, b = (_hold_of(conn, ref, settings, r, route.composite_id) for r in (run1, run2))
    assert a.live and a.delivered_at is not None and a.remaining == ra
    assert b.live and b.in_flight and b.remaining == rb
    snap.on_hand[route.composite_id] = ra + 100
    snap.in_progress[route.unrefined_id] = 500
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty == ra + rb
    assert item.held_from_credit_qty == rb
    assert item.on_hand_qty == 100 and item.in_progress_qty == 0


def test_a_release_shrinks_the_hide_and_expiry_ends_it(conn, ref):
    snap, route, _baseline = _chain_world(conn, ref)
    run1, hide = _executed_pile(conn, ref, route, 4)
    snap.on_hand[route.composite_id] = hide + 100
    store.add_alchemy_release(conn, run1, route.composite_id, 150, "manual")
    conn.commit()
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty == hide - 150 and item.on_hand_qty == 250
    # The second Mark executed after the delivery ends the hold.
    c1 = store._utc_datetime(conn.execute(
        "SELECT completed_at FROM index_run WHERE index_run_id = ?", (run1,)
    ).fetchone()[0])
    seed_run(conn, 2, c1, status="complete", completed_at=c1 + timedelta(days=30))
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[route.composite_id]
    assert item.held_for_sale_qty is None and item.on_hand_qty == hide + 100


def test_held_quantities_are_persisted_on_the_run_item(conn, ref):
    snap, route, _baseline = _chain_world(conn, ref)
    b1, b2, per = 2, 3, 100
    _run1, hide = _executed_pile(conn, ref, route, b1, b2, per)
    pool = _expected_qty(b2 * per, route)
    snap.on_hand[route.composite_id] = hide + 50
    snap.in_progress[route.unrefined_id] = b2 * per
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    row = conn.execute(
        "SELECT held_for_sale_qty, held_from_credit_qty, on_hand_qty, in_progress_qty, "
        "alchemy_credit_qty FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, route.composite_id),
    ).fetchone()
    assert row["held_for_sale_qty"] == hide and row["held_from_credit_qty"] == pool
    assert row["on_hand_qty"] == hide + 50 - (hide - pool)
    assert row["in_progress_qty"] == 0 and row["alchemy_credit_qty"] == 0


def test_an_off_chain_hold_beside_a_chain_hold_changes_nothing_on_the_chain_row(conn, ref):
    """A hold on a composite the chain never touches has no row to bite;
    the chain composite's own hide is what it was."""
    snap, route, baseline = _chain_world(conn, ref)
    run1, hide = _executed_pile(conn, ref, route, 4)
    snap.on_hand[route.composite_id] = hide + 100
    before = engine.plan_index_run(conn, ref, snap, persist=False).items[route.composite_id]
    assert before.held_for_sale_qty == hide
    other = off_chain_routes(ref, baseline)[0]
    seed_hold(conn, run1, other.composite_id)
    _s, end = install_jobs(conn, run1, other.unrefined_id, 6, 100, first_job_id=95000)
    deliver_jobs(conn, _ids(95000, 6), end + timedelta(days=1))
    settings = store.get_settings(conn)
    assert _hold_of(conn, ref, settings, run1, other.composite_id).live
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    after = plan.items[route.composite_id]
    assert other.composite_id not in plan.items
    assert (after.held_for_sale_qty, after.held_from_credit_qty, after.on_hand_qty, after.deficit_qty) == (
        before.held_for_sale_qty, before.held_from_credit_qty, before.on_hand_qty, before.deficit_qty
    )


def test_steady_state_ignores_holds(conn, ref):
    snap, route, _baseline = _chain_world(conn, ref)
    _run1, hide = _executed_pile(conn, ref, route, 4)
    snap.on_hand[route.composite_id] = hide + 100
    assert engine.plan_index_run(conn, ref, snap, persist=False).items[
        route.composite_id
    ].held_for_sale_qty == hide
    steady = engine.plan_steady_state(conn, ref, snap)
    assert steady.items[route.composite_id].held_for_sale_qty is None
    assert steady.items[route.composite_id].held_from_credit_qty is None


# --- the profit tail: capacity and cap ------------------------------------------


def test_the_profit_tails_capacity_nets_running_reaction_jobs_started_after_the_cut(conn, ref):
    """Ruling (a): the tail fills the spare lines less the reaction jobs
    this cycle already started (any product) — not the previous cycle's,
    not delivered ones; with no cut (no run yet) every running job counts."""
    snap, route, _settings = for_sale_world(conn, ref)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    base = _for_sale_row(plan1, route).alchemy_profit_jobs
    pool = snap.slots_available[config.ACTIVITY_REACTION]
    assert reaction_jobs(plan1, "install_jobs") == pool  # the tail filled the pool
    cut = run_opened_at(conn, run1)
    other = route.composite_id  # the direct reaction's product: a reaction, not the formula

    def jobs(first, count, start, status="active"):
        return [
            reaction_job(first + n, other, start, 1000, status, end_date=start + timedelta(days=2))
            for n in range(count)
        ]

    store.save_industry_jobs(conn, jobs(70000, 20, cut - timedelta(days=1)), pulled_at=cut + timedelta(seconds=2))
    plan = _replan(conn, ref, snap, run1)
    assert _for_sale_row(plan, route).alchemy_profit_jobs == base  # the previous cycle's lines
    plan = _replan(conn, ref, snap, run1, cycle_cut=None)
    assert _for_sale_row(plan, route).alchemy_profit_jobs == base - 20  # no cut: every one counts
    store.save_industry_jobs(conn, jobs(71000, 10, cut + timedelta(hours=1)), pulled_at=cut + timedelta(hours=2))
    store.save_industry_jobs(conn, jobs(72000, 7, cut + timedelta(hours=1), "delivered"), pulled_at=cut + timedelta(hours=3))
    plan = _replan(conn, ref, snap, run1)
    assert _for_sale_row(plan, route).alchemy_profit_jobs == base - 10  # this cycle's running lines
    assert reaction_jobs(plan, "install_jobs") == pool - 10
    plan = _replan(conn, ref, snap, run1, cycle_cut=None)
    assert _for_sale_row(plan, route).alchemy_profit_jobs == base - 30


def test_installed_jobs_of_the_formula_count_toward_the_cap(conn, ref):
    """With the cap met by installed jobs the formula lists no new job,
    and the hold is the installed ones."""
    snap, route, settings = for_sale_world(conn, ref, cap=5)
    plan1 = engine.plan_index_run(conn, ref, snap, persist=True)
    run1 = plan1.index_run_id
    row = _for_sale_row(plan1, route)
    assert row.alchemy_profit_jobs == row.jobs_allocated == 5
    assert _for_sale_row(_replan(conn, ref, snap, run1), route).alchemy_profit_jobs == 5  # control
    per = _job_units(row)
    start, _end = install_jobs(conn, run1, route.unrefined_id, 5, per)
    plan2 = _replan(conn, ref, installed_snapshot(snap, route.unrefined_id, start, 5 * per), run1)
    row2 = _for_sale_row(plan2, route)
    assert row2 is None or row2.alchemy_profit_jobs == 0
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.for_sale_qty == _expected_qty(5 * per, route) == hold.planned_qty and hold.live


def test_composite_extra_targets_skip_a_pure_for_sale_row(conn, ref):
    """Review #89: a row whose jobs are all for sale is no chain composite
    reaction, so its fuel block gets no extra-runs adder — the block's
    target is the same with the tail on and off; a mixed row keeps it."""
    snap, route, settings = for_sale_world(conn, ref, profit=False)
    plan_off = engine.plan_index_run(conn, ref, snap, persist=False)
    enable_profit(conn)
    plan_on = engine.plan_index_run(conn, ref, snap, persist=False)
    row = _for_sale_row(plan_on, route)
    assert row.alchemy_profit_jobs == row.jobs_allocated > 0
    formula = dict(ref.materials(row.blueprint_id, row.activity_id))
    fuel = next(m for m in formula if plan_on.items[m].buildable)
    assert plan_on.items[fuel].target_stock_qty == plan_off.items[fuel].target_stock_qty
    class_settings = store.get_class_settings(conn)
    settings = store.get_settings(conn)
    with_row = engine._composite_extra_targets(ref, settings, class_settings, plan_on.items)
    without = {t: i for t, i in plan_on.items.items() if t != route.unrefined_id}
    assert with_row[fuel] == engine._composite_extra_targets(ref, settings, class_settings, without)[fuel]
    # A mixed row is a chain consumer too: one extra runs' worth of the block.
    mixed = dict(plan_on.items)
    mixed[route.unrefined_id] = replace(row, alchemy_profit_jobs=row.jobs_allocated - 1)
    mat_mult = industry.build_multiplier(
        ref, class_settings.get(row.item_class, industry.NPC_STATION), row.activity_id, "material"
    )
    adder = industry.required_quantity(settings.composite_reaction_extra_runs, formula[fuel], 0, mat_mult)
    assert adder > 0
    assert engine._composite_extra_targets(ref, settings, class_settings, mixed)[fuel] == with_row[fuel] + adder


# --- the install check and the trim (ruling g) ----------------------------------


def test_the_install_check_rations_the_chain_first_and_the_for_sale_row_takes_what_is_left(conn, ref):
    """A goo both the chain and the for-sale formula draw, short at the
    install check: the chain consumers install as before and the for-sale
    row gets the runs the remainder feeds, named short by that goo."""
    snap, route, _settings = for_sale_world(conn, ref)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    sale = _for_sale_row(plan, route)
    assert sale.install_runs == sale.runs_allocated > 0
    formula = dict(ref.materials(sale.blueprint_id, sale.activity_id))
    direct = dict(ref.materials(plan.items[route.composite_id].blueprint_id, config.ACTIVITY_REACTION))
    goo = next(m for m in formula if m in direct and not plan.items[m].buildable)
    raw = plan.items[goo]
    assert raw.recommended_buy_qty > 0 and raw.install_short_qty == 0
    draw_of = engine._draw_calculator(conn, ref)
    chain = [i for i in _direct_reactions(plan) if goo in dict(ref.materials(i.blueprint_id, i.activity_id))]
    assert chain
    chain_draw = sum(engine.install_draw_of(draw_of, i).get(goo, 0) for i in chain)
    per_run = draw_of(sale, 1, 1)[goo]
    before = {i.type_id: (i.install_runs, i.install_jobs, i.install_limited_by) for i in _direct_reactions(plan)}
    # The buy unsourced, the hangar holding the chain's draw and one and a half runs more.
    raw.unfilled_qty = raw.recommended_buy_qty
    raw.on_hand_qty = chain_draw + per_run + per_run // 2
    engine._install_check(conn, ref, plan.items, snap)
    after = {i.type_id: (i.install_runs, i.install_jobs, i.install_limited_by) for i in _direct_reactions(plan)}
    assert after == before
    assert sale.install_runs == 1 and sale.install_jobs == 1
    assert sale.install_limited_by == goo
    assert raw.install_short_qty == raw.install_draw_qty - raw.on_hand_qty > 0


def test_the_pool_overflow_trim_cuts_for_sale_jobs_first(conn, ref):
    """A pool below the startable total loses for-sale jobs first (the
    pool named as what stopped them), chain jobs only once none is left."""
    snap, route, _settings = for_sale_world(conn, ref)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    sale = _for_sale_row(plan, route)
    pool = snap.slots_available[config.ACTIVITY_REACTION]
    assert reaction_jobs(plan, "install_jobs") == pool and sale.install_jobs == sale.jobs_allocated
    per = sale.install_per_job
    before = {i.type_id: (i.install_runs, i.install_jobs) for i in _direct_reactions(plan)}
    engine._install_check(conn, ref, plan.items, replace(snap, slots_available={
        config.ACTIVITY_MANUFACTURING: pool, config.ACTIVITY_REACTION: pool - 10,
    }))
    assert {i.type_id: (i.install_runs, i.install_jobs) for i in _direct_reactions(plan)} == before
    assert sale.install_jobs == sale.jobs_allocated - 10
    assert sale.install_runs == sale.runs_allocated - 10 * per
    assert sale.install_limited_by == engine._LIMITED_BY_SLOTS
    assert reaction_jobs(plan, "install_jobs") == pool - 10
    # Below the chain's own count: every for-sale job goes, then chain jobs.
    chain_jobs = sum(j for _r, j in before.values())
    engine._install_check(conn, ref, plan.items, replace(snap, slots_available={
        config.ACTIVITY_MANUFACTURING: pool, config.ACTIVITY_REACTION: chain_jobs - 3,
    }))
    assert (sale.install_jobs or 0) == 0
    assert sum(i.install_jobs or 0 for i in _direct_reactions(plan)) == chain_jobs - 3


def test_alchemy_sale_split_trims_the_for_sale_share_first():
    # 5 jobs of 272 runs, 2 for sale: the chain's 3 jobs come first.
    assert engine.alchemy_sale_split(5, 2, 272, 1, None) == (3 * 272, 2 * 272)
    # Cut to 4 jobs' runs: one for-sale job is gone.
    assert engine.alchemy_sale_split(5, 2, 272, 1, 4 * 272) == (3 * 272, 272)
    # Cut below the chain's share: nothing for sale, the chain takes the cut.
    assert engine.alchemy_sale_split(5, 2, 272, 1, 2 * 272) == (2 * 272, 0)
    # No for-sale jobs: everything is the chain's, cut or not.
    assert engine.alchemy_sale_split(3, 0, 272, 1, None) == (3 * 272, 0)
    assert engine.alchemy_sale_split(3, 0, 272, 1, 100) == (100, 0)


# --- write_plan_holds: the four rules -------------------------------------------


def _write_world(conn, ref):
    """A seeded open run with a pull before it, one route, and a factory
    for the unrefined row as the plan carries it. Returns (run_id, route,
    item)."""
    route = _a_route(ref)
    store.save_industry_jobs(conn, [], pulled_at=T0 - timedelta(hours=1))
    run1 = seed_run(conn, 1, T0)

    def item(profit_jobs, jobs=5, max_runs=10, cost=123.0, install_runs=None):
        return PlanItem(
            type_id=route.unrefined_id, name="unrefined", item_class="reactions", depth=1,
            alchemy_for_type_id=route.composite_id, alchemy_profit_jobs=profit_jobs,
            jobs_allocated=jobs, max_runs_per_job=max_runs, portion_size=1,
            install_runs=install_runs, alchemy_route_unit_cost=cost,
        )

    return run1, route, item


def _row(conn, run_id):
    [row] = store.hold_rows(conn, run_id)
    return dict(row)


def test_write_plan_holds_rule_1_a_new_row_takes_what_is_installed_now_as_baseline(conn, ref):
    run1, route, item = _write_world(conn, ref)
    install_jobs(conn, run1, route.unrefined_id, 2, 10)  # already running when the route turns up
    settings = store.get_settings(conn)
    other = off_chain_routes(ref, engine.Plan(index_run_id=0, run_number=0, items={}))[0]
    idle = replace(item(0), type_id=other.unrefined_id, alchemy_for_type_id=other.composite_id)
    holds.write_plan_holds(conn, ref, settings, run1, {route.unrefined_id: item(2), other.unrefined_id: idle})
    conn.commit()
    row = _row(conn, run1)  # one row: a savings-only route writes none
    assert (row["baseline_units"], row["chain_units"]) == (20, 20 + 3 * 10)
    assert row["planned_qty"] == int(2 * 10 * route.composite_qty * 0.55)
    assert row["route_unit_cost"] == 123.0 and row["yield_"] == 0.55
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.installed_units == 20 and hold.for_sale_qty == 0


def test_write_plan_holds_rule_2_follows_the_plan_while_nothing_new_is_installed(conn, ref):
    run1, route, item = _write_world(conn, ref)
    settings = store.get_settings(conn)
    holds.write_plan_holds(conn, ref, settings, run1, {route.unrefined_id: item(2)})
    conn.commit()
    assert (_row(conn, run1)["chain_units"], _row(conn, run1)["planned_qty"]) == (
        30, int(20 * route.composite_qty * 0.55)
    )
    conn.execute("UPDATE settings SET alchemy_reprocess_yield = 0.6")
    conn.commit()
    settings = store.get_settings(conn)
    # Three for sale now, an install cut to 45 runs (the for-sale share takes it), a new cost.
    holds.write_plan_holds(conn, ref, settings, run1, {route.unrefined_id: item(3, cost=200.0, install_runs=45)})
    conn.commit()
    row = _row(conn, run1)
    assert (row["baseline_units"], row["chain_units"]) == (0, 2 * 10)
    assert row["planned_qty"] == int(25 * route.composite_qty * 0.6)
    assert (row["route_unit_cost"], row["yield_"]) == (200.0, 0.6)
    # A row without a cost keeps the one it had.
    holds.write_plan_holds(conn, ref, settings, run1, {route.unrefined_id: item(3, cost=None)})
    conn.commit()
    assert _row(conn, run1)["route_unit_cost"] == 200.0


def test_write_plan_holds_rule_3_installs_beyond_the_baseline_freeze_the_row(conn, ref):
    run1, route, item = _write_world(conn, ref)
    settings = store.get_settings(conn)
    holds.write_plan_holds(conn, ref, settings, run1, {route.unrefined_id: item(2)})
    conn.commit()
    was = _row(conn, run1)
    install_jobs(conn, run1, route.unrefined_id, 1, 10)  # one job beyond the baseline of 0
    conn.execute("UPDATE settings SET alchemy_reprocess_yield = 0.7")
    conn.commit()
    holds.write_plan_holds(conn, ref, store.get_settings(conn), run1, {route.unrefined_id: item(4, cost=999.0)})
    conn.commit()
    assert _row(conn, run1) == was
    # A dropped composite is frozen too.
    holds.write_plan_holds(conn, ref, store.get_settings(conn), run1, {})
    conn.commit()
    assert _row(conn, run1) == was


def test_write_plan_holds_rule_4_a_dropped_composite_keeps_its_row_at_zero(conn, ref):
    run1, route, item = _write_world(conn, ref)
    settings = store.get_settings(conn)
    holds.write_plan_holds(conn, ref, settings, run1, {route.unrefined_id: item(2)})
    conn.commit()
    holds.write_plan_holds(conn, ref, settings, run1, {})
    conn.commit()
    row = _row(conn, run1)
    assert (row["planned_qty"], row["baseline_units"], row["chain_units"]) == (0, 0, 0)
    assert (row["route_unit_cost"], row["yield_"]) == (123.0, 0.55)  # kept
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.for_sale_qty == 0 and not hold.live
    # Proposed again: the row follows.
    holds.write_plan_holds(conn, ref, settings, run1, {route.unrefined_id: item(1, cost=50.0)})
    conn.commit()
    row = _row(conn, run1)
    assert (row["planned_qty"], row["chain_units"], row["route_unit_cost"]) == (
        int(10 * route.composite_qty * 0.55), 40, 50.0
    )


# --- a database stamped 14 in the old shape -----------------------------------


_OLD_HOLD_TABLES = """
CREATE TABLE alchemy_hold (
    index_run_id        INTEGER NOT NULL REFERENCES index_run ON DELETE CASCADE,
    composite_type_id   INTEGER NOT NULL,
    planned_qty         INTEGER NOT NULL DEFAULT 0,
    route_unit_cost     REAL,
    PRIMARY KEY (index_run_id, composite_type_id)
);
CREATE TABLE alchemy_hold_release (
    release_id          INTEGER PRIMARY KEY,
    index_run_id        INTEGER NOT NULL,
    composite_type_id   INTEGER NOT NULL,
    qty                 INTEGER NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('market', 'contract', 'manual')),
    ref_id              INTEGER,
    unit_price          REAL,
    location_id         INTEGER,
    released_at         TEXT NOT NULL,
    FOREIGN KEY (index_run_id, composite_type_id)
        REFERENCES alchemy_hold ON DELETE CASCADE
);
CREATE INDEX alchemy_hold_release_ref ON alchemy_hold_release (source, ref_id);
CREATE TABLE industry_job (
    job_id         INTEGER PRIMARY KEY,
    type_id        INTEGER NOT NULL,
    activity_id    INTEGER NOT NULL,
    start_date     TEXT NOT NULL,
    runs           INTEGER NOT NULL,
    units          INTEGER NOT NULL,
    status         TEXT NOT NULL,
    seen_at        TEXT NOT NULL
);
"""


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_a_fourteen_stamped_database_in_the_old_shape_gains_the_columns_and_the_index(
    conn, ref, tmp_path, monkeypatch
):
    """A dev copy stamped 14 before the anchors: ensure_schema adds the
    columns (no bump, so no backup), backfills the yield and the release's
    cost, puts the UNIQUE index on, and keeps the hold and its release; a
    hold without chain_units is a plan figure once its run is executed."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    route = _a_route(ref)
    run1 = seed_run(conn, 1, T0, status="complete", completed_at=T0 + timedelta(days=10))
    conn.executescript(
        "DROP TABLE alchemy_hold_release; DROP TABLE alchemy_hold; DROP TABLE industry_job;"
        + _OLD_HOLD_TABLES
    )
    conn.execute(
        "INSERT INTO alchemy_hold (index_run_id, composite_type_id, planned_qty, route_unit_cost) "
        "VALUES (?, ?, 500, 12.5)", (run1, route.composite_id),
    )
    conn.execute(
        "INSERT INTO alchemy_hold_release (index_run_id, composite_type_id, qty, source, ref_id, "
        "unit_price, released_at) VALUES (?, ?, 40, 'market', 9001, 30000.0, '2026-09-20T00:00:00Z')",
        (run1, route.composite_id),
    )
    conn.execute(
        "INSERT INTO industry_job (job_id, type_id, activity_id, start_date, runs, units, status, "
        "seen_at) VALUES (1, ?, 11, '2026-09-02 00:00:00', 10, 10, 'delivered', '2026-09-20 00:00:00')",
        (route.unrefined_id,),
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 14")
    for table, missing in (
        ("alchemy_hold", {"baseline_units", "chain_units", "yield_"}),
        ("alchemy_hold_release", {"unit_cost"}),
        ("industry_job", {"end_date", "completed_date"}),
    ):
        assert not _columns(conn, table) & missing
    store.ensure_schema(conn)
    assert {"baseline_units", "chain_units", "yield_"} <= _columns(conn, "alchemy_hold")
    assert "unit_cost" in _columns(conn, "alchemy_hold_release")
    assert {"end_date", "completed_date"} <= _columns(conn, "industry_job")
    indexes = {r["name"]: r for r in conn.execute("PRAGMA index_list(alchemy_hold_release)")}
    assert indexes["alchemy_hold_release_sale"]["unique"] == 1
    assert "alchemy_hold_release_hold" in indexes
    assert not (tmp_path / "backups").exists()  # no bump, no snapshot
    row = _row(conn, run1)
    assert (row["planned_qty"], row["route_unit_cost"], row["chain_units"]) == (500, 12.5, None)
    assert row["yield_"] == store.get_settings(conn).alchemy_reprocess_yield
    [release] = store.release_rows(conn, run1)
    assert (release["qty"], release["ref_id"], release["unit_cost"]) == (40, 9001, 12.5)
    assert conn.execute("SELECT status, end_date FROM industry_job").fetchone()[0] == "delivered"
    # The UNIQUE index holds: the same sale applied again adds to the row.
    store.add_alchemy_release(conn, run1, route.composite_id, 10, "market", ref_id=9001, unit_price=30000.0)
    conn.commit()
    [release] = store.release_rows(conn, run1)
    assert release["qty"] == 50
    # Without chain_units the executed run's hold is the plan's figure, net of its release.
    hold = _hold_of(conn, ref, store.get_settings(conn), run1, route.composite_id)
    assert hold.plan_figure and hold.for_sale_qty == 500
    assert hold.observed_qty == 50 and hold.remaining == 450 and hold.live


# --- the review's rules (2026-10-31): the anchor, stale running jobs, dropped
# --- proposals, real listed lots, the fill-stamped cost, the for-sale hand-back


def test_a_late_install_of_the_previous_run_is_baseline_though_seen_only_after_the_plan(conn, ref):
    """Rule (A): a row is ANCHORED at its first write — the later of the
    run's planned_start and the latest job pull. A savings job of the
    formula started after run 1 was marked executed but before run 2's
    plan, on the record only after that plan, started at or before the
    anchor: the chain's (pre_anchor_units), never for sale. The jobs
    started after the anchor are read savings first (chain_units −
    baseline_units), the rest are the hold's, and they freeze the row: a
    later plan leaves it and Discard refuses it. The same record on a row
    without an anchor (legacy) reads by the stored baseline alone, so the
    late job would be the hold's."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    store.save_industry_jobs(conn, [], pulled_at=T0 - timedelta(hours=1))
    run1 = seed_run(conn, 1, T0)
    c1 = T0 + timedelta(days=10)
    complete_run(conn, run1, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1)  # the update Mark executed followed
    late_start = c1 + timedelta(minutes=10)  # a savings job installed late: not on record yet
    p2 = c1 + timedelta(minutes=30)
    run2 = seed_run(conn, 2, p2)  # planned_start = p2, the window opens at c1
    item = PlanItem(
        type_id=route.unrefined_id, name="unrefined", item_class="reactions", depth=1,
        alchemy_for_type_id=route.composite_id, alchemy_profit_jobs=2, jobs_allocated=5,
        max_runs_per_job=10, portion_size=1, alchemy_route_unit_cost=123.0,
    )
    holds.write_plan_holds(conn, ref, settings, run2, {route.unrefined_id: item})
    conn.commit()
    was = _row(conn, run2)
    assert was["anchored_at"] == store.utc_text(p2)  # the plan, later than the pull at c1
    assert (was["baseline_units"], was["chain_units"]) == (0, 30)
    assert was["planned_qty"] == int(20 * route.composite_qty * 0.55)
    # The pull after the plan shows the late job AND the five jobs installed from the plan.
    start = p2 + timedelta(hours=1)
    store.save_industry_jobs(
        conn,
        [reaction_job(89000, route.unrefined_id, late_start, 100, end_date=late_start + timedelta(hours=720))]
        + [
            reaction_job(90000 + n, route.unrefined_id, start, 10, end_date=start + timedelta(hours=720))
            for n in range(5)
        ],
        pulled_at=start + timedelta(seconds=1),
    )
    hold = _hold_of(conn, ref, settings, run2, route.composite_id)
    assert (hold.installed_units, hold.pre_anchor_units) == (150, 100)
    assert hold.for_sale_unrefined == 50 - 30 == 20  # five new jobs less the three savings jobs' units
    assert hold.for_sale_qty == _expected_qty(20, route) == math.floor(20 * route.composite_qty * 0.55)
    assert hold.frozen and hold.live and hold.in_flight and hold.status == "in flight"
    assert (hold.baseline_units, hold.chain_units) == (0, 30)
    # A plan after that leaves the row: proposal, savings share, yield and cost alike.
    conn.execute("UPDATE settings SET alchemy_reprocess_yield = 0.7")
    conn.commit()
    holds.write_plan_holds(
        conn, ref, store.get_settings(conn), run2,
        {route.unrefined_id: replace(item, alchemy_profit_jobs=4, alchemy_route_unit_cost=999.0)},
    )
    conn.commit()
    assert _row(conn, run2) == was
    assert [h.composite_type_id for h in holds.holds_blocking_delete(conn, ref, settings, run2)] == [
        route.composite_id
    ]
    # A legacy row (anchored_at NULL) reads the same record by its stored baseline.
    conn.execute("UPDATE alchemy_hold SET anchored_at = NULL WHERE index_run_id = ?", (run2,))
    conn.commit()
    legacy = _hold_of(conn, ref, settings, run2, route.composite_id)
    assert (legacy.pre_anchor_units, legacy.for_sale_unrefined, legacy.frozen) == (0, 150 - 30, True)


def test_a_running_job_the_latest_pull_no_longer_reports_counts_delivered_at_its_timer_end(conn, ref):
    """Rule (B): a for-sale job still 'active' on record whose seen_at is
    more than an hour before the latest pull AND whose end_date is before
    that pull has delivered out of sight (its installer left the pool):
    delivered at the timer end, no longer in flight, live under the count
    rule. Seen at the pull (or exactly an hour before it) the job stays in
    flight, as does a stale job whose timer runs past the pull."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    _start, end = install_jobs(conn, run1, route.unrefined_id, 2, 100)
    pull = end + timedelta(days=5)
    store.save_industry_jobs(conn, [], pulled_at=pull)  # the jobs not reported: seen_at stands

    def seen(when):
        conn.execute(
            "UPDATE industry_job SET seen_at = ? WHERE job_id IN (90000, 90001)", (store.utc_text(when),)
        )
        conn.commit()
        return _hold_of(conn, ref, settings, run1, route.composite_id)

    hold = seen(pull)
    assert hold.in_flight and hold.delivered_at is None and hold.jobs_end == end
    assert hold.status == "in flight" and hold.delivered_units == 0
    hold = seen(pull - timedelta(hours=1))  # exactly the slack: not stale
    assert hold.in_flight and hold.delivered_at is None
    hold = seen(pull - timedelta(hours=2))
    assert not hold.in_flight and hold.jobs_end is None
    assert (hold.first_delivered, hold.delivered_at) == (end, end)
    assert hold.delivered_units == 200 and hold.delivered_qty == _expected_qty(200, route) == hold.for_sale_qty
    assert hold.status == "delivered" and (hold.live, hold.expires_after, hold.expired_at) == (True, 2, None)
    assert holds.bites(conn, ref, settings)[route.composite_id].remaining == hold.for_sale_qty
    # A stale job whose timer end is after the pull is still running for all the record knows.
    late_end = pull + timedelta(days=1)
    conn.execute(
        "UPDATE industry_job SET end_date = ? WHERE job_id IN (90000, 90001)", (store.utc_text(late_end),)
    )
    conn.commit()
    hold = seen(pull - timedelta(hours=2))
    assert hold.in_flight and hold.jobs_end == late_end and hold.delivered_at is None
    assert hold.delivered_units == 0 and hold.status == "in flight"


def test_a_dropped_proposal_on_an_executed_uncovered_window_is_no_plan_figure(conn, ref):
    """Rule (C): a row the plan dropped (planned_qty 0) has nothing to fall
    back to — not a plan figure, not live, absent from bites — while a
    sibling proposal on the same uncovered executed run stands in."""
    ferro, hyper = _a_route(ref), ref.alchemy_routes()[ref.type_id("Hyperflurite")]
    settings = store.get_settings(conn)
    store.save_industry_jobs(conn, [], pulled_at=T0 - timedelta(hours=1))
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, ferro.composite_id, planned=0)
    seed_hold(conn, run1, hyper.composite_id, planned=500)
    complete_run(conn, run1, T0 + timedelta(days=10))  # no pull reaches into the window
    dropped = _hold_of(conn, ref, settings, run1, ferro.composite_id)
    assert not dropped.covered and not dropped.plan_figure
    assert (dropped.for_sale_qty, dropped.live, dropped.expires_after, dropped.status) == (
        0, False, None, "not installed"
    )
    sibling = _hold_of(conn, ref, settings, run1, hyper.composite_id)
    assert sibling.plan_figure and sibling.for_sale_qty == 500 and sibling.live
    bites = holds.bites(conn, ref, settings)
    assert ferro.composite_id not in bites and bites[hyper.composite_id].remaining == 500


def _sell_order(conn, order_id, type_id, remain, issued, owner=A):
    """An open sell order of `owner` on the market, issued at `issued`."""
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, "
        "volume_total, volume_remain, location_id, duration, issued, state, first_seen_at, "
        "last_seen_at) VALUES (?, 'character', ?, 'character', ?, 1e6, ?, ?, 60003760, 90, ?, "
        "'open', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
        (order_id, owner, type_id, remain, remain, issued),
    )
    conn.commit()


def test_real_listed_lots_are_attributed_by_issue_date_up_to_the_delivered_unreleased_room(conn, ref):
    """Rule (D) through the real ledger.listed_lots: an open sell order
    issued inside a live hold's life reduces its hide; one issued before
    its first delivery is nobody's; a lot larger than the hold's delivered
    unreleased room (delivered_qty − released_qty) lands there up to the
    room and the rest on the next delivered hold whose life holds the
    date — or on nobody while that hold is still in flight; a lot without
    a readable issue date goes to the live holds (sale_order.issued is
    NOT NULL, so an empty text stands for the unparseable date)."""
    route = _a_route(ref)
    settings = store.get_settings(conn)
    add_owner(conn, A)
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id)
    _s1, end1 = install_jobs(conn, run1, route.unrefined_id, 4, 100)
    d1 = end1 + timedelta(days=1)
    deliver_jobs(conn, _ids(90000, 4), d1)
    c1 = d1 + timedelta(days=1)
    complete_run(conn, run1, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1 + timedelta(hours=1))
    store.add_alchemy_release(conn, run1, route.composite_id, 100, "manual")
    conn.commit()
    run2 = seed_run(conn, 2, c1)
    seed_hold(conn, run2, route.composite_id)
    _s2, end2 = install_jobs(conn, run2, route.unrefined_id, 2, 100, first_job_id=92000)
    q1, q2 = _expected_qty(400, route), _expected_qty(200, route)
    room1 = q1 - 100
    one, two = (_hold_of(conn, ref, settings, r, route.composite_id) for r in (run1, run2))
    assert one.live and one.first_delivered == d1 and one.expired_at is None
    assert (one.delivered_qty, one.released_qty, one.remaining) == (q1, 100, room1)
    assert two.live and two.in_flight and two.first_delivered is None and two.remaining == q2

    def bite():
        b = holds.bites(conn, ref, settings)[route.composite_id]
        return b.remaining, b.listed, b.hide

    assert bite() == (room1 + q2, 0, room1 + q2)
    # Issued inside hold 1's life: its pile.
    in_life = _iso(d1 + timedelta(hours=1))
    _sell_order(conn, 1, route.composite_id, 1000, in_life)
    assert ledger.listed_lots(conn, [route.composite_id]) == {route.composite_id: [(in_life, 1000)]}
    assert ledger.listed_units(conn, [route.composite_id]) == {route.composite_id: 1000}
    assert bite() == (room1 + q2, 1000, room1 + q2 - 1000)
    # Issued before its first delivery: nobody's pile.
    conn.execute("UPDATE sale_order SET issued = ? WHERE order_id = 1", (_iso(T0 + timedelta(days=1)),))
    conn.commit()
    assert bite() == (room1 + q2, 0, room1 + q2)
    # Larger than the room: the room is hold 1's, the rest the next hold's — not delivered yet: nobody's.
    conn.execute(
        "UPDATE sale_order SET issued = ?, volume_remain = ? WHERE order_id = 1", (in_life, room1 + 1000)
    )
    conn.commit()
    assert bite() == (room1 + q2, room1, q2)
    # Hold 2 delivered and the lot issued after that: the rest is hold 2's.
    d2 = end2 + timedelta(days=1)
    deliver_jobs(conn, _ids(92000, 2), d2)
    conn.execute("UPDATE sale_order SET issued = ? WHERE order_id = 1", (_iso(d2 + timedelta(hours=1)),))
    conn.commit()
    two = _hold_of(conn, ref, settings, run2, route.composite_id)
    assert two.live and two.first_delivered == d2 and two.delivered_qty == q2
    assert bite() == (room1 + q2, room1 + 1000, q2 - 1000)
    # Issued between the two deliveries: hold 2's life has not begun — the rest is nobody's again.
    conn.execute("UPDATE sale_order SET issued = ? WHERE order_id = 1", (_iso(d2 - timedelta(hours=1)),))
    conn.commit()
    assert bite() == (room1 + q2, room1, q2)
    # No readable issue date: the live holds', oldest first.
    conn.execute("DELETE FROM sale_order")
    conn.commit()
    _sell_order(conn, 2, route.composite_id, 300, "")
    assert ledger.listed_lots(conn, [route.composite_id]) == {route.composite_id: [("", 300)]}
    assert bite() == (room1 + q2, 300, room1 + q2 - 300)


def _fill_world(conn, ref):
    """Hulk x 8, alchemy at cap 200 with Alchemy for Profit on, a nominal
    inbound leg (1 ISK/m³), Prometium quoted at 1e6, a pull before the run
    — and Prometium's Chromium on a thin Jita book (one unit at the quote,
    the rest 1e5 x dearer) so a plan that BUYS it fill-prices it far above
    the quote (test_alchemy_profit's re-stamp world). Returns (snapshot,
    route, chromium, cadmium, chromium's quote)."""
    prometium = ref.alchemy_routes()[ref.type_id("Prometium")]
    chromium, cadmium = ref.type_id("Chromium"), ref.type_id("Cadmium")
    assert {chromium, cadmium} <= set(_formula(ref, prometium))
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=200)
    enable_profit(conn)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 1.0")
    conn.commit()
    store.save_industry_jobs(conn, [])
    snap = snapshot(ref, overrides={prometium.composite_id: 1e6})
    quote = snap.price(chromium)
    thin = replace(snap, sell_ladders={HUB: {chromium: [(quote, 1), (quote * 1e5, 10**9)]}})
    return thin, prometium, chromium, cadmium, quote


def _hold_row(conn, run_id, composite_id):
    [row] = [r for r in store.hold_rows(conn, run_id) if r["composite_type_id"] == composite_id]
    return dict(row)


def _stocked(snap, plan, goo_ids, prices=None):
    """The next snapshot: the goo the plan bought has landed, ten times
    over (nothing of it to buy), at `prices` where given."""
    on_hand = {**snap.on_hand, **{g: 10 * plan.items[g].recommended_buy_qty for g in goo_ids}}
    return replace(snap, on_hand=on_hand, prices=prices if prices is not None else snap.prices)


def test_a_fill_stamped_route_cost_survives_a_plan_that_buys_nothing_of_the_route(conn, ref):
    """Rule (F): the plan that BOUGHT the route's goo on the thin book
    stamped the hold's route_unit_cost at the fill-priced landed figure
    (cost_from_fills 1). The next snapshot shows the goo on hand — nothing
    bought, recommended_buy_qty 0 — and Chromium's quote a fifth higher:
    the plan's own row prices at the new quote (not from fills), but the
    hold keeps the figure the goo was paid at, while the rest of the row
    follows the plan."""
    thin, route, chromium, cadmium, quote = _fill_world(conn, ref)
    settings = store.get_settings(conn)
    plan1 = engine.plan_index_run(conn, ref, thin, persist=True)
    run1 = plan1.index_run_id
    row1 = _for_sale_row(plan1, route)
    assert row1.alchemy_profit_jobs == row1.jobs_allocated > 0
    goo = plan1.items[chromium]
    bought = goo.recommended_buy_qty
    assert goo.hub_buy_qty == bought > 1 and goo.unfilled_qty == 0 and goo.buy_venue == HUB
    fill_average = (quote + (bought - 1) * quote * 1e5) / bought
    assert goo.price_snapshot == pytest.approx(fill_average)

    def landed(t):
        return _landed(ref, settings, thin, t)

    def filled(t):
        if t == chromium:
            return fill_average + settings.freight_in_isk_per_m3 * ref.type_info(t).freight_volume
        return landed(t)

    runs = row1.max_runs_per_job
    at_quote = _route_cost(ref, settings, thin, route, runs, landed)
    at_fill = _route_cost(ref, settings, thin, route, runs, filled)
    assert at_fill > at_quote
    assert row1.alchemy_cost_from_fills and row1.alchemy_route_unit_cost == pytest.approx(at_fill)
    row = _hold_row(conn, run1, route.composite_id)
    assert row["cost_from_fills"] == 1 and row["route_unit_cost"] == pytest.approx(at_fill)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.cost_from_fills and hold.route_unit_cost == pytest.approx(at_fill)
    # The goo landed, Chromium's quote up a fifth, nothing of the route bought.
    dearer = snapshot(ref, overrides={route.composite_id: 1e6, chromium: quote * 1.2})
    stocked = _stocked(thin, plan1, (chromium, cadmium), prices=dearer.prices)
    plan2 = _replan(conn, ref, stocked, run1)
    row2 = _for_sale_row(plan2, route)
    assert row2.alchemy_profit_jobs > 0
    for m in (chromium, cadmium):
        assert plan2.items[m].recommended_buy_qty == 0 and plan2.items[m].compressed_covered_qty == 0
    quote_now = _route_cost(
        ref, settings, stocked, route, row2.max_runs_per_job, lambda t: _landed(ref, settings, stocked, t)
    )
    assert quote_now != pytest.approx(at_fill)
    assert not row2.alchemy_cost_from_fills and row2.alchemy_route_unit_cost == pytest.approx(quote_now)
    row = _hold_row(conn, run1, route.composite_id)
    assert row["cost_from_fills"] == 1 and row["route_unit_cost"] == pytest.approx(at_fill)
    assert row["planned_qty"] == _expected_qty(_sale_units(row2), route)  # the proposal follows the plan
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert hold.route_unit_cost == pytest.approx(at_fill) and hold.planned_qty == row["planned_qty"]


def test_a_quote_stamped_route_cost_follows_the_next_plan(conn, ref):
    """Rule (F), the control: where no plan ever bought the route's goo
    (on hand in both plans, so both cost at the Phase 1 quote), the hold's
    route_unit_cost follows the second plan's figure when the quote moves
    — cost_from_fills stays 0."""
    thin, route, chromium, cadmium, quote = _fill_world(conn, ref)
    settings = store.get_settings(conn)
    control = engine.plan_index_run(conn, ref, thin, persist=False)
    stocked = _stocked(thin, control, (chromium, cadmium))
    plan1 = engine.plan_index_run(conn, ref, stocked, persist=True)
    run1 = plan1.index_run_id
    row1 = _for_sale_row(plan1, route)
    assert row1.alchemy_profit_jobs > 0
    for m in (chromium, cadmium):
        assert plan1.items[m].recommended_buy_qty == 0
    at_quote = _route_cost(
        ref, settings, stocked, route, row1.max_runs_per_job, lambda t: _landed(ref, settings, stocked, t)
    )
    assert not row1.alchemy_cost_from_fills and row1.alchemy_route_unit_cost == pytest.approx(at_quote)
    row = _hold_row(conn, run1, route.composite_id)
    assert row["cost_from_fills"] == 0 and row["route_unit_cost"] == pytest.approx(at_quote)
    dearer = snapshot(ref, overrides={route.composite_id: 1e6, chromium: quote * 1.2})
    stocked2 = replace(stocked, prices=dearer.prices)
    plan2 = _replan(conn, ref, stocked2, run1)
    row2 = _for_sale_row(plan2, route)
    quote_now = _route_cost(
        ref, settings, stocked2, route, row2.max_runs_per_job, lambda t: _landed(ref, settings, stocked2, t)
    )
    assert quote_now > at_quote
    assert not row2.alchemy_cost_from_fills and row2.alchemy_route_unit_cost == pytest.approx(quote_now)
    row = _hold_row(conn, run1, route.composite_id)
    assert row["cost_from_fills"] == 0 and row["route_unit_cost"] == pytest.approx(quote_now)
    hold = _hold_of(conn, ref, settings, run1, route.composite_id)
    assert not hold.cost_from_fills and hold.route_unit_cost == pytest.approx(quote_now)


def test_the_for_sale_pass_hands_whole_runs_back_until_the_short_goo_is_spent(conn, ref):
    """Rule (F), _ration's third pass: two for-sale routes drawing one
    short goo share it in proportion, the shares floored to whole runs —
    and the slack the flooring left is handed back a whole run at a time
    while it fits, so the startable runs are the maximal feasible count
    (no further run of either route fits the goo left), not the floored
    one. The draws are the game's own per-job figures (_draw_calculator /
    _packed_draw); the chain's consumers of the goo install whole."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    _target, route = target_route(ref, baseline)
    formula = set(_formula(ref, route)) - set(FUEL_BLOCKS)
    other, goo = next(
        (r, m)
        for r in off_chain_routes(ref, baseline)
        if r.composite_id != route.composite_id
        for m in sorted(formula & set(_formula(ref, r)))
    )
    enable_alchemy(conn, cap=150)
    enable_profit(conn)
    store.save_industry_jobs(conn, [])
    snap = snapshot(ref, overrides={route.composite_id: 1e6, other.composite_id: 1e6})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    x, y = plan.items[route.unrefined_id], plan.items[other.unrefined_id]
    for sale in (x, y):
        assert sale.alchemy_profit_jobs == sale.jobs_allocated > 0  # pure for-sale rows
        assert sale.install_runs == sale.runs_allocated > 0
    raw = plan.items[goo]
    assert not raw.buildable and raw.recommended_buy_qty > 0 and raw.install_short_qty == 0
    draw_of = engine._draw_calculator(conn, ref)

    def sale_draw(item, runs):
        return engine._packed_draw(draw_of, item, runs, item.max_runs_per_job).get(goo, 0) if runs else 0

    rx, ry = x.runs_allocated, y.runs_allocated
    assert sale_draw(x, rx) == draw_of(x, rx, x.jobs_allocated)[goo]  # full jobs: the plan's own split
    assert sale_draw(y, ry) == draw_of(y, ry, y.jobs_allocated)[goo]
    total = sale_draw(x, rx) + sale_draw(y, ry)
    chain_draw = sum(
        engine.install_draw_of(draw_of, i).get(goo, 0)
        for i in plan.items.values()
        if i.buildable and i.alchemy_for_type_id is None
    )
    assert chain_draw > 0

    def floors(k):
        f = k / total
        return min(rx, math.floor(round(f * rx, 9))), min(ry, math.floor(round(f * ry, 9)))

    def leaves_slack(k):
        # The floored shares leave at least one more run's draw unused,
        # with both fractions well clear of a whole run (no float edge).
        fx, fy = floors(k)
        slack = k - sale_draw(x, fx) - sale_draw(y, fy)
        fracs = ((k / total * rx) % 1, (k / total * ry) % 1)
        return slack >= min(sale_draw(x, 1), sale_draw(y, 1)) and all(0.2 <= p <= 0.8 for p in fracs)

    k = next(k for k in range(total // 2, total) if leaves_slack(k))
    fx, fy = floors(k)
    before = {
        i.type_id: (i.install_runs, i.install_jobs)
        for i in plan.items.values() if i.buildable and i.alchemy_for_type_id is None
    }
    raw.unfilled_qty = raw.recommended_buy_qty  # the buy unsourced
    raw.on_hand_qty = chain_draw + k  # the chain's draw and k units for the for-sale pass
    engine._install_check(conn, ref, plan.items, snap)
    assert {
        i.type_id: (i.install_runs, i.install_jobs)
        for i in plan.items.values() if i.buildable and i.alchemy_for_type_id is None
    } == before
    gx, gy = x.install_runs, y.install_runs
    assert (gx, gy) >= (fx, fy) and gx + gy > fx + fy, "the floored shares were not the end of it"
    assert sale_draw(x, gx) + sale_draw(y, gy) <= k
    assert gx == rx or sale_draw(x, gx + 1) + sale_draw(y, gy) > k  # maximal: no run of x fits
    assert gy == ry or sale_draw(x, gx) + sale_draw(y, gy + 1) > k  # ... nor of y
    assert (gx, gy) != (rx, ry)
    for sale, got in ((x, gx), (y, gy)):
        assert sale.install_jobs == -(-got // sale.max_runs_per_job)
        if got < sale.runs_allocated:
            assert sale.install_limited_by == goo
    assert raw.install_short_qty == raw.install_draw_qty - raw.on_hand_qty > 0
