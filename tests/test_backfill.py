"""Stock-aware slot backfill (engine Phase 6, user ruling 2026-09-09) and
alchemy in the slots of unstartable direct jobs (Phase 6.5): a job stock
cannot feed this cycle holds no slot, so its slot goes — in savings
order — to a contender the pool starved whose inputs ARE there. The
starved jobs stay in the plan (they wait for stock), so a pool's planned
jobs may exceed it; its startable jobs never do — the install check caps
them at the pool.

Same fixture pattern as test_engine; the allocation is steered with a
monkeypatched savings figure the way the MILP tests do, so which tier
wins the pool is deterministic.
"""

import sqlite3

import pytest

from magoo import config, engine, store
from magoo.engine import Snapshot
from conftest import FairValuePrices


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


def add_pipeline(conn, ref, product_name: str, qty: int) -> int:
    cur = conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run) VALUES (?, ?, ?)",
        (product_name, ref.type_id(product_name), qty),
    )
    conn.commit()
    return cur.lastrowid


def rich_snapshot(ref, mfg=500, reaction=500, overrides=None):
    prices = FairValuePrices(ref, overrides=overrides)
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: mfg,
            config.ACTIVITY_REACTION: reaction,
        },
        prices=prices,
        adjusted_prices=prices,
    )


COMPOSITE, SIMPLE = 429, 428  # reaction product groups


def _reactions(plan):
    return [
        i for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION and i.alchemy_for_type_id is None
    ]


def _group(ref, item):
    return ref.type_info(item.type_id).group_id


def _steer_reaction_savings(monkeypatch, ref, low: set):
    """Composites rank highest, simple reactions next, and the simple
    reactions in `low` (filled in by the test) lowest — every reaction
    stays a builder (positive savings); manufacturing keeps the real
    figure."""
    real = engine._build_savings_per_unit

    def fake(ref_, item, chain, buy_cost, snap):
        value = real(ref_, item, chain, buy_cost, snap)
        if item.activity_id != config.ACTIVITY_REACTION:
            return value
        if item.type_id in low:
            return 3.0
        return {COMPOSITE: 300.0, SIMPLE: 30.0}.get(_group(ref, item), 3.0)

    monkeypatch.setattr(engine, "_build_savings_per_unit", fake)


def _fuel_stocked(ref, snap, plan):
    for item in plan.items.values():
        if ref.type_info(item.type_id).group_id == 1136:  # fuel blocks
            snap.on_hand[item.type_id] = 10**9


def _starved_line(conn, ref, monkeypatch, persist=False):
    """Hulk × 8, fuel on hand, everything else empty, a reaction pool that
    holds every composite job and every simple-reaction job but the one
    simple reaction (the smallest, `starved`) ranked lowest. Composites
    win (highest savings) yet cannot start — the other simple inputs
    are being built this cycle, not bought, and none is on hand; the
    other simple reactions start (bought raws + fuel); the starved one
    loses the pool though its inputs are there. Returns
    (plan, pool, starved type id)."""
    add_pipeline(conn, ref, "Hulk", 8)
    low = set()
    _steer_reaction_savings(monkeypatch, ref, low)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    simples = [i for i in _reactions(empty) if _group(ref, i) == SIMPLE]
    composites = [i for i in _reactions(empty) if _group(ref, i) == COMPOSITE]
    assert simples and composites
    starved = min(simples, key=lambda i: (i.jobs_needed_unconstrained, i.name))
    low.add(starved.type_id)
    pool = sum(i.jobs_needed_unconstrained for i in composites + simples) - starved.jobs_needed_unconstrained
    assert starved.jobs_needed_unconstrained <= sum(i.jobs_needed_unconstrained for i in composites)
    snap = rich_snapshot(ref, reaction=pool)
    _fuel_stocked(ref, snap, empty)
    return engine.plan_index_run(conn, ref, snap, persist=persist), pool, starved.type_id


def test_freed_slots_go_to_starved_contenders_whose_inputs_are_there(conn, ref, monkeypatch):
    plan, pool, starved_id = _starved_line(conn, ref, monkeypatch)
    rx = _reactions(plan)
    composites = [i for i in rx if _group(ref, i) == COMPOSITE]
    simples = [i for i in rx if _group(ref, i) == SIMPLE and i.type_id != starved_id]
    starved = plan.items[starved_id]
    # The MILP filled the pool with the composites and the other simples.
    milp = sum(i.jobs_allocated - i.backfilled_jobs for i in rx)
    assert milp == pool
    assert starved.jobs_allocated - starved.backfilled_jobs == 0
    # Composites cannot start (their simple inputs are built, not
    # bought); the other simple reactions can.
    assert all((i.install_jobs or 0) == 0 for i in composites if i.jobs_allocated)
    assert all((i.install_jobs or 0) == i.jobs_allocated for i in simples)
    # Their slots went to the starved simple reaction, whose inputs are
    # there (bought raws, fuel on hand) — as many as it wanted, or as
    # many as were freed.
    freed = sum(i.jobs_allocated - (i.install_jobs or 0) for i in composites)
    assert starved.backfilled_jobs == min(starved.jobs_needed_unconstrained, freed) > 0
    assert (starved.install_jobs or 0) == starved.backfilled_jobs
    # Startable jobs fit the pool; planned jobs exceed it by the backfill.
    startable = sum(i.install_jobs or 0 for i in rx)
    assert startable <= pool
    assert sum(i.jobs_allocated for i in rx) == pool + starved.backfilled_jobs


def test_backfill_shrinks_the_losers_buy(conn, ref, monkeypatch):
    """A simple reaction bought as a capacity loser buys less once the
    backfill gives it jobs: its purchase is what its jobs still leave,
    at most."""
    plan, _pool, starved_id = _starved_line(conn, ref, monkeypatch)
    starved = plan.items[starved_id]
    assert starved.backfilled_jobs > 0
    covered = starved.runs_allocated * starved.portion_size + starved.alchemy_output_qty
    assert starved.recommended_buy_qty <= max(
        0, starved.total_runs_needed * starved.portion_size - covered
    ) + starved.market_buy_qty
    assert starved.recommended_action in ("build", "both")


def test_no_backfill_without_feedable_contenders(conn, ref, monkeypatch):
    """No fuel on hand: nothing above the raw tier can start, the freed
    slots find no taker, and the plan stays at the pool."""
    add_pipeline(conn, ref, "Hulk", 8)
    _steer_reaction_savings(monkeypatch, ref, set())
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref, reaction=6), persist=False)
    rx = _reactions(plan)
    assert sum(i.jobs_allocated for i in rx) == 6
    assert sum(i.backfilled_jobs for i in rx) == 0
    assert sum(i.install_jobs or 0 for i in rx) <= 6


def test_a_losers_fallback_buy_feeds_its_consumers_and_is_not_backfilled_away(conn, ref, monkeypatch):
    """A tiny pool: composites take every slot, every simple reaction
    loses it and is bought. Those bought units feed the composites —
    which START (fuel on hand, simples bought) — so no slot is freed;
    and had one been, a simple reaction could not have had it: its jobs
    would replace the very buy the composites' start was counted on."""
    add_pipeline(conn, ref, "Hulk", 8)
    _steer_reaction_savings(monkeypatch, ref, set())
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    snap = rich_snapshot(ref, reaction=6)
    _fuel_stocked(ref, snap, empty)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    rx = _reactions(plan)
    composites = [i for i in rx if _group(ref, i) == COMPOSITE and i.jobs_allocated]
    simples = [i for i in rx if _group(ref, i) == SIMPLE]
    assert composites and sum(i.jobs_allocated for i in composites) == 6
    assert all(i.jobs_allocated == 0 and i.recommended_buy_qty > 0 for i in simples if i.total_runs_needed)
    assert all((i.install_jobs or 0) == i.jobs_allocated for i in composites)
    assert sum(i.backfilled_jobs for i in rx) == 0
    assert sum(i.install_jobs or 0 for i in rx) == 6


def test_no_backfill_when_every_allocated_job_can_start(conn, ref):
    """A line stocked at target: every allocated job starts, so no slot
    is free to hand on — the plan never exceeds the pool."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref, mfg=3000, reaction=3000), persist=False)
    snap = rich_snapshot(ref, mfg=3000, reaction=3000)
    for item in empty.items.values():
        if item.buildable:
            snap.on_hand[item.type_id] = item.target_stock_qty
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert all(i.backfilled_jobs == 0 for i in plan.items.values())
    for activity, pool in ((config.ACTIVITY_MANUFACTURING, 3000), (config.ACTIVITY_REACTION, 3000)):
        assert sum(i.jobs_allocated for i in plan.items.values() if i.activity_id == activity) <= pool


def test_steady_state_planner_never_backfills(conn, ref):
    """Review 2026-09-10: the Slot Planner's what-if has no stock to
    backfill from — plan_steady_state passes backfill=False, so its
    allocation stays within the pool it was given (it exceeded it by
    the backfilled jobs before)."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_steady_state(conn, ref, rich_snapshot(ref, mfg=10, reaction=30))
    assert all(i.backfilled_jobs == 0 for i in plan.items.values())
    for activity, pool in ((config.ACTIVITY_MANUFACTURING, 10), (config.ACTIVITY_REACTION, 30)):
        assert sum(i.jobs_allocated for i in plan.items.values() if i.activity_id == activity) <= pool


def test_backfilled_jobs_draw_as_phase_7_sizes_them(conn, ref):
    """Review 2026-09-10: Phase 7 re-splits a backfilled intermediate's
    runs uniform and rounded UP across ALL its jobs, so the extra jobs'
    real draw exceeds `extra_runs` worth; the backfill sizes them the
    same way (_sized_runs), so every backfilled row's Phase 7 runs are
    what the fit test assumed, and no backfilled row starts short on a
    raw the plan buys for it. Hulk × 8 + Thanatos × 2, a partial stock,
    a mid-sized pool (the reviewer's seed-14 construction)."""
    import random

    for name, qty in (("Hulk", 8), ("Thanatos", 2)):
        add_pipeline(conn, ref, name, qty)
    baseline = engine.plan_index_run(conn, ref, rich_snapshot(ref, mfg=5000, reaction=5000), persist=False)
    finals = {ref.type_id("Hulk"), ref.type_id("Thanatos")}
    full = max(
        sum(i.jobs_needed_unconstrained for i in baseline.items.values() if i.activity_id == a)
        for a in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION)
    )
    # Deterministic seeds; the first one that backfills a non-saturating
    # row is the case under test.
    backfilled = []
    for seed in range(14, 40):
        rng = random.Random(seed)
        slots = max(1, int(full * rng.choice([0.05, 0.15, 0.3, 0.5, 0.7, 0.9, 1.0])))
        snap = rich_snapshot(ref, mfg=slots, reaction=slots)
        for i in baseline.items.values():
            f = rng.choice([0.0, 0.0, 0.0, 0.1, 0.4, 0.8, 1.0, 1.0, 2.5])
            if f:
                snap.on_hand[i.type_id] = int(i.target_stock_qty * f)
        plan = engine.plan_index_run(conn, ref, snap, persist=False)
        backfilled = [i for i in plan.items.values() if i.backfilled_jobs]
        if any(not engine._saturating_reaction(ref, i) for i in backfilled):
            break
    assert backfilled and any(not engine._saturating_reaction(ref, i) for i in backfilled)
    for item in backfilled:
        assert item.runs_allocated == engine._sized_runs(ref, finals, item, item.jobs_allocated), item.name
    for activity in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION):
        assert sum(i.install_jobs or 0 for i in plan.items.values() if i.activity_id == activity) <= slots


def test_startable_jobs_never_exceed_the_pool(conn, ref, monkeypatch):
    """The install check caps a pool's startable jobs at the pool —
    trimming backfilled jobs first, then the lowest-savings
    intermediates, finals last — and names the pool as what stopped
    them (a job that became startable after the allocation, through a
    Phase 7 buy the allocation-time estimate missed, would otherwise
    push a pool over)."""
    plan, pool, starved_id = _starved_line(conn, ref, monkeypatch)
    for activity, size in ((config.ACTIVITY_REACTION, pool), (config.ACTIVITY_MANUFACTURING, 500)):
        assert sum(
            i.install_jobs or 0 for i in plan.items.values() if i.activity_id == activity
        ) <= size
    # Force the cap: re-check the same plan against a pool three jobs
    # smaller than what can start.
    rx = _reactions(plan)
    startable = sum(i.install_jobs or 0 for i in rx)
    assert startable > 3
    snap = rich_snapshot(ref, reaction=startable - 3)
    _fuel_stocked(ref, snap, plan)
    before = {i.type_id: (i.install_jobs, i.install_runs) for i in rx}
    engine._install_check(conn, ref, plan.items, snap)
    assert sum(i.install_jobs or 0 for i in rx) == startable - 3
    trimmed = [i for i in rx if i.install_limited_by == engine._LIMITED_BY_SLOTS]
    assert trimmed and sum(before[i.type_id][0] - i.install_jobs for i in trimmed) == 3
    for i in trimmed:
        assert i.install_runs < before[i.type_id][1]
    # The backfilled jobs (the starved simple reaction's) go first.
    starved = plan.items[starved_id]
    assert starved in trimmed


def test_the_slot_cap_keeps_a_rows_binding_input(conn, ref):
    """Review 2026-09-10: the cap stamped the pool sentinel over
    `install_limited_by`, so a row stock had ALREADY cut lost the name of
    the input to buy, and the page blamed the pool alone for a row stock
    had bound. The rationing does not depend on the pool, so a row's
    binding input is the same whatever the pool is: the cap claims the
    pool only where nothing else bound the row. Partial stock (60 % of
    the planned draw) is what produces rows that are input-bound AND
    still hold jobs the cap can take."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    snap = rich_snapshot(ref)
    for type_id, qty in draw.items():
        snap.on_hand[type_id] = int(qty * 0.6)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    bound = {
        i.type_id: i.install_limited_by
        for i in plan.items.values()
        if (i.install_limited_by or 0) > 0 and (i.install_jobs or 0) > 0
    }
    assert bound, "partial stock binds rows that still hold jobs"
    # Re-check the same plan against pools far too small for what stock
    # can start, so the cap must trim those very rows.
    startable = {
        activity: sum(
            i.install_jobs or 0 for i in plan.items.values()
            if i.activity_id == activity
        )
        for activity in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION)
    }
    tiny = rich_snapshot(
        ref,
        mfg=max(1, startable[config.ACTIVITY_MANUFACTURING] // 2),
        reaction=max(1, startable[config.ACTIVITY_REACTION] // 2),
    )
    tiny.on_hand.update(snap.on_hand)
    engine._install_check(conn, ref, plan.items, tiny)
    assert any(
        i.install_limited_by == engine._LIMITED_BY_SLOTS
        for i in plan.items.values()
    ), "the tiny pools force the cap"
    for type_id, material in bound.items():
        assert plan.items[type_id].install_limited_by == material, (
            f"{plan.items[type_id].name} lost its binding input to the pool"
        )


def test_backfilled_jobs_survive_the_feedback_loop_and_persist(conn, ref, monkeypatch):
    """The backfill runs inside the sizing loop, so the backfilled jobs'
    raw draw is bought just in time, and the persisted run carries the
    same figures the plan does."""
    plan, _pool, starved_id = _starved_line(conn, ref, monkeypatch, persist=True)
    draw = engine._planned_consumption(conn, ref, plan.items)
    starved = plan.items[starved_id]
    assert starved.backfilled_jobs > 0
    for m, _q in ref.materials(starved.blueprint_id, starved.activity_id):
        raw = plan.items[m]
        if not raw.buildable:
            assert raw.on_hand_qty + raw.in_progress_qty + raw.recommended_buy_qty >= draw.get(m, 0), raw.name
    rows = {
        r["type_id"]: r for r in conn.execute(
            "SELECT type_id, jobs_allocated, install_jobs FROM index_run_item "
            "WHERE index_run_id = ? AND activity_id = 11", (plan.index_run_id,),
        )
    }
    for i in _reactions(plan):
        assert rows[i.type_id]["jobs_allocated"] == i.jobs_allocated
        assert rows[i.type_id]["install_jobs"] == i.install_jobs
