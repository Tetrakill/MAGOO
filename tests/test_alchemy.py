"""Alchemy (v1.4): route derivation, unrefined stock credits, the spare-slot
substitution pass, and reprocess cost-lot bookkeeping.

Same fixture pattern as test_engine: real reference data, temp state DB.
"""

import math
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


def enable_alchemy(conn, yield_=0.55, cap=50):
    conn.execute(
        "UPDATE settings SET alchemy_enabled = 1, "
        "alchemy_reprocess_yield = ?, max_alchemy_jobs_per_type = ?",
        (yield_, cap),
    )
    conn.commit()


class Overlay(dict):
    """Uniform price with per-type overrides."""

    def __init__(self, base=10.0, **_):
        super().__init__()
        self.base = base

    def get(self, key, default=None):
        return dict.get(self, key, self.base)


FUEL_BLOCKS = (
    "Nitrogen Fuel Block", "Hydrogen Fuel Block",
    "Helium Fuel Block", "Oxygen Fuel Block",
)


def snapshot(ref, slots=500, overrides=None):
    prices = FairValuePrices(ref, overrides=overrides)
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=prices,
        adjusted_prices=Overlay(),
    )


def reaction_candidates(ref, plan):
    """Composite reaction plan items that have an alchemy route and won
    direct jobs."""
    routes = ref.alchemy_routes()
    return [
        i
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
        and i.jobs_allocated > 0
        and i.type_id in routes
    ]


def expensive_rare_inputs(ref, plan_items):
    """Price overrides making every routed composite's rare input (direct
    formula inputs the alchemy formula does not share) cost a fortune."""
    routes = ref.alchemy_routes()
    overrides = {}
    for item in plan_items:
        route = routes[item.type_id]
        direct = {m for m, _q in ref.materials(item.blueprint_id, item.activity_id)}
        alchemy = {
            m
            for m, _q in ref.materials(
                route.formula.blueprint_id, route.formula.activity_id
            )
        }
        for rare in direct - alchemy:
            overrides[rare] = 1e6
    return overrides


def alchemy_items(plan):
    return [
        i for i in plan.items.values() if i.alchemy_for_type_id is not None
    ]


# --- Route derivation -------------------------------------------------------


def test_route_derivation(ref):
    routes = ref.alchemy_routes()
    assert len(routes) == 17  # composite alchemy only
    ferro = routes[ref.type_id("Ferrofluid")]
    assert ferro.unrefined_id == ref.type_id("Unrefined Ferrofluid")
    assert ferro.composite_qty == 73
    assert ferro.recovered == ((ref.type_id("Hafnium"), 173),)
    # 6h unrefined run vs 3h direct run
    assert ferro.formula.base_time == 21600
    # Mineral alchemy (randomized reprocess outputs) never qualifies
    assert ref.type_id("Tritanium") not in routes
    assert ref.type_id("Morphite") not in routes


def test_hulk_chain_contains_routed_composites(conn, ref):
    """The substitution tests below rely on the Hulk chain demanding
    composites that have alchemy routes."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    assert reaction_candidates(ref, plan)


# --- Substitution pass ------------------------------------------------------


def test_alchemy_off_by_default(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    assert not alchemy_items(plan)
    assert all(i.alchemy_output_qty == 0 for i in plan.items.values())


def test_alchemy_not_run_when_pricier(conn, ref):
    """At uniform prices alchemy is strictly worse (same-ish inputs, ~40%
    of the output) — enabling it must change nothing."""
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    assert not alchemy_items(plan)
    # but the comparison was made and recorded on routed composites
    priced = [
        i
        for i in plan.items.values()
        if i.direct_unit_cost is not None and i.alchemy_unit_cost is not None
    ]
    assert priced
    assert all(i.alchemy_unit_cost >= i.direct_unit_cost for i in priced)


def test_alchemy_comparison_prices_landed(conn, ref):
    """Both routes' materials AND the recovered credit carry inbound
    freight (2026-08-24): raising freight_in moves each recorded unit cost
    by exactly the route's net hauled m³ per composite unit. At NPC test
    defaults (ME 0, multiplier 1.0, integer base quantities) the material
    rounding is exact, so the expected delta is runs-independent."""
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn)
    base_plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    rate = 900.0
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = ?", (rate,))
    conn.commit()
    freight_plan = engine.plan_index_run(
        conn, ref, snapshot(ref), persist=False
    )

    def vol(type_id):
        return ref.type_info(type_id).freight_volume

    yield_ = 0.55
    checked = alchemy_hauls_more = 0
    for composite_id, route in ref.alchemy_routes().items():
        before = base_plan.items.get(composite_id)
        after = freight_plan.items.get(composite_id)
        if (
            before is None
            or after is None
            or before.direct_unit_cost is None
            or after.direct_unit_cost is None
        ):
            continue
        direct_m3 = sum(
            q * vol(m)
            for m, q in ref.materials(before.blueprint_id, before.activity_id)
        ) / before.portion_size
        alchemy_m3 = (
            sum(
                q * vol(m)
                for m, q in ref.materials(
                    route.formula.blueprint_id, route.formula.activity_id
                )
            ) / route.formula.portion_size
            - yield_ * sum(q * vol(m) for m, q in route.recovered)
        ) / (route.composite_qty * yield_)
        assert after.direct_unit_cost - before.direct_unit_cost == pytest.approx(
            rate * direct_m3
        )
        assert (
            after.alchemy_unit_cost - before.alchemy_unit_cost
            == pytest.approx(rate * alchemy_m3)
        )
        if alchemy_m3 > direct_m3:
            alchemy_hauls_more += 1
        checked += 1
    assert checked
    # The reason the landed leg matters: freight does not cancel between
    # the routes — the unrefined route hauls more m³ per composite unit.
    assert alchemy_hauls_more


def test_alchemy_substitutes_when_rare_goo_expensive(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    candidates = reaction_candidates(ref, baseline)
    overrides = expensive_rare_inputs(ref, candidates)
    enable_alchemy(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=False
    )
    swapped = alchemy_items(plan)
    assert swapped, "expected alchemy jobs with rare goo at 1M ISK"
    routes = ref.alchemy_routes()
    for alch in swapped:
        composite = plan.items[alch.alchemy_for_type_id]
        # the swap was justified and recorded
        assert composite.alchemy_unit_cost < composite.direct_unit_cost
        # direct jobs were displaced, not stacked on top of
        assert (
            composite.jobs_allocated
            < baseline.items[composite.type_id].jobs_allocated
        )
        # alchemy jobs saturate the window like other reactions
        assert alch.runs_allocated == alch.jobs_allocated * alch.max_runs_per_job
        assert alch.recommended_build_qty == alch.runs_allocated
        route = routes[composite.type_id]
        assert alch.blueprint_id == route.formula.blueprint_id


def test_substitution_preserves_deficit_coverage(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=False
    )
    swapped_composites = [
        i for i in plan.items.values() if i.alchemy_output_qty > 0
    ]
    assert swapped_composites
    for item in swapped_composites:
        produced = (
            item.runs_allocated * item.portion_size + item.alchemy_output_qty
        )
        assert produced >= item.deficit_qty
        assert not item.capacity_limited
        assert item.recommended_buy_qty == 0


def test_reaction_pool_never_exceeded(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    for slots in (500, 40, 25):
        plan = engine.plan_index_run(
            conn, ref, snapshot(ref, slots=slots, overrides=overrides), persist=False
        )
        # The STARTABLE reaction jobs — direct and alchemy — fit the pool;
        # since the stock-aware backfill (2026-09-09) the planned count may
        # not, as jobs stock cannot feed hold no slot.
        startable = sum(
            i.install_jobs or 0
            for i in plan.items.values()
            if i.activity_id == config.ACTIVITY_REACTION
        )
        assert startable <= slots


def _no_fuel_contended(conn, ref, fuel_priced=True, alchemy=True):
    """A contended reaction pool (2026-09-09 ruling) with NO fuel on hand
    and the chain BUILDING its fuel blocks. The manufacturing pool keeps
    its 500 slots so the reaction pool alone is contended (review
    2026-09-10: the first contention tests set BOTH pools to 3 and
    planned no reaction job)."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    if not fuel_priced:
        overrides.update({ref.type_id(n): None for n in FUEL_BLOCKS})
    enable_alchemy(conn)
    pool = sum(
        i.jobs_needed_unconstrained for i in baseline.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    snap = snapshot(ref, overrides=overrides)
    snap.slots_available[config.ACTIVITY_REACTION] = pool
    plan = engine.plan_index_run(conn, ref, snap, persist=False, alchemy=alchemy)
    fuel = [i for i in plan.items.values() if ref.type_info(i.type_id).group_id == 1136]
    assert fuel and all(i.buildable and i.on_hand_qty == 0 for i in fuel)
    return plan, pool


def _reactions(plan, alchemy):
    return [
        i for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
        and bool(i.alchemy_for_type_id) == alchemy
    ]


def test_a_built_fuel_block_short_of_the_draw_is_bought_just_in_time(conn, ref):
    """User ruling 2026-09-12 ("direct reactions first"). A built fuel
    block's own jobs deliver next cycle, so with none on hand no reaction
    could start — direct or alchemy. Now the shortfall of this cycle's
    draw is bought just in time: every fuel row buys at least what the
    installs draw beyond stock, nothing is short of fuel, and the
    reactions whose other inputs are there start — within the pool."""
    plan, pool = _no_fuel_contended(conn, ref, alchemy=False)
    for row in (i for i in plan.items.values() if ref.type_info(i.type_id).group_id == 1136):
        assert row.recommended_buy_qty >= (row.install_draw_qty or 0), row.name
        assert not row.install_short_qty, row.name
        assert all(
            i.install_limited_by != row.type_id for i in plan.items.values()
        ), f"a job is still limited by {row.name}"
    direct = _reactions(plan, alchemy=False)
    assert sum(i.install_jobs or 0 for i in direct) > 0, "fuel-fed reactions start"
    assert sum(i.install_jobs or 0 for i in direct) <= pool


def test_alchemy_runs_on_bought_fuel_after_the_direct_reactions(conn, ref):
    """Direct reactions take the fuel first — they are the ones that can
    start once it is bought, so their slots are not free — and alchemy
    runs only in the slots still free (jobs stuck on other inputs), its
    own fuel bought the same way: every alchemy job can start and the
    startable total fits the pool."""
    plan, pool = _no_fuel_contended(conn, ref)
    alch = _reactions(plan, alchemy=True)
    assert alch, "alchemy runs in the slots the stuck composites leave"
    assert all((i.install_jobs or 0) == i.jobs_allocated for i in alch)
    assert sum(
        i.install_jobs or 0 for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    ) <= pool
    for row in (i for i in plan.items.values() if ref.type_info(i.type_id).group_id == 1136):
        assert not row.install_short_qty, row.name


def test_a_swap_frees_only_the_fuel_its_dropped_job_really_held(conn, ref):
    """Review 2026-09-12 (randomized scan, seed 12 trial 22): a swap that
    drops a startable but FUEL-LIMITED direct job credited a whole job's
    inputs back to the leftover stock, though the item's other jobs still
    drew all but one run of its grant. Alchemy was then planned on fuel
    that was never freed and, at install, took it from direct reactions —
    against "direct reactions first". Only an unpriced BUILT fuel block
    is stock-limited, so the case prices fuel at None."""
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=10)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 0, structure_freight_in_isk_per_m3 = 0")
    conn.commit()
    on_hand = {22544: 6, 17960: 3191, 17959: 1386, 33359: 28687, 4246: 1237, 4247: 2661, 11478: 109,
               4312: 2510, 11531: 84, 16655: 36557, 11535: 1833, 11541: 23911, 16662: 4756, 16664: 1373,
               16665: 1796, 16666: 176, 16667: 3872, 11547: 13, 16669: 651, 16670: 349106, 11545: 15734,
               11553: 5640, 11556: 466, 16680: 183464, 16681: 25638, 16682: 723, 16683: 2281, 17769: 1497,
               16663: 9358, 17317: 516, 4051: 427}
    in_progress = {17959: 3280, 4312: 261, 11541: 1200, 16662: 519, 11545: 10754, 11553: 905,
                   16681: 15322, 16683: 999, 16663: 94}
    prices = {16652: 1e6, 16648: 1e6, 4246: None, 16653: 1e6, 16643: 1e6, 16649: 1e6, 4051: None,
              4312: None, 16647: 1e6, 16641: 1e6, 16644: 1e6, 16651: 1e6, 16650: 1e6, 4247: None,
              33336: 840618.3925655745, 16655: 250032.6452114899, 16665: 750079.3844839016,
              16666: 1495334.4192446293, 16667: 1495334.4192446293, 17769: 1745343.7685596978}
    ladders = {33336: [(560412.261710383, 10695), (1681236.785131149, 5513)],
               16665: [(500052.9229892677, 9272), (1500158.768967803, 11072)]}
    nitrogen = ref.type_id("Nitrogen Fuel Block")
    assert nitrogen == 4051 and on_hand[nitrogen] == 427

    def snap():
        s = Snapshot(
            slots_available={config.ACTIVITY_MANUFACTURING: 60, config.ACTIVITY_REACTION: 153},
            prices=FairValuePrices(ref, overrides=prices),
            adjusted_prices=Overlay(),
            sell_ladders={store.BUY_VENUE_HUB: ladders},
        )
        s.on_hand.update(on_hand)
        s.in_progress.update(in_progress)
        return s

    draw_of = engine._draw_calculator(conn, ref)

    def nitrogen_installed(plan, alchemy):
        return sum(
            engine.install_draw_of(draw_of, i).get(nitrogen, 0)
            for i in _reactions(plan, alchemy=alchemy)
        )

    off = engine.plan_index_run(conn, ref, snap(), persist=False, alchemy=False)
    on = engine.plan_index_run(conn, ref, snap(), persist=False)
    assert nitrogen_installed(off, alchemy=False) > 0
    # Direct reactions keep every unit of fuel they install without alchemy ...
    assert nitrogen_installed(on, alchemy=False) >= nitrogen_installed(off, alchemy=False)
    # ... and no alchemy job is planned on fuel the install check cannot find.
    for i in _reactions(on, alchemy=True):
        assert i.install_limited_by != nitrogen, i.name


def test_no_reaction_starts_where_the_short_fuel_has_no_market(conn, ref):
    """The mirror: a built fuel block with no price on record cannot be
    bought, so with none on hand no reaction can start and no alchemy is
    planned."""
    plan, _pool = _no_fuel_contended(conn, ref, fuel_priced=False)
    assert sum(i.install_jobs or 0 for i in _reactions(plan, alchemy=False)) == 0
    assert not _reactions(plan, alchemy=True)


def test_contended_alchemy_buys_route_only_goo_just_in_time(conn, ref):
    """Review 2026-09-10: an unrefined formula's goo that no direct
    formula demands is not a plan row when the swap is judged — it is
    added afterwards and bought just in time — so it must not gate the
    swap. Stocking that goo on hand therefore changes nothing: the
    alchemy routes taken under contention are the same either way."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    pool = sum(
        i.jobs_needed_unconstrained for i in baseline.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    route_only = set()
    for item in reaction_candidates(ref, baseline):
        route = ref.alchemy_routes()[item.type_id]
        for m, _q in ref.materials(route.formula.blueprint_id, route.formula.activity_id):
            if m not in baseline.items:
                route_only.add(m)
    assert route_only, "some route needs goo the direct chain never buys"

    def plan_with(stock_route_goo: bool):
        snap = snapshot(ref, overrides=overrides)
        snap.slots_available[config.ACTIVITY_REACTION] = pool
        for item in baseline.items.values():
            if ref.type_info(item.type_id).group_id == 1136:  # fuel blocks
                snap.on_hand[item.type_id] = 10**9
        if stock_route_goo:
            for m in route_only:
                snap.on_hand[m] = 10**9
        return engine.plan_index_run(conn, ref, snap, persist=False)

    unstocked, stocked = plan_with(False), plan_with(True)
    taken = lambda plan: {i.type_id: i.jobs_allocated for i in alchemy_items(plan)}
    assert taken(unstocked) and taken(unstocked) == taken(stocked)
    # The goo the chosen routes need is a plan row now, bought just in time.
    for alch in alchemy_items(unstocked):
        for m, _q in ref.materials(alch.blueprint_id, alch.activity_id):
            if m in route_only:
                assert unstocked.items[m].recommended_buy_qty > 0, unstocked.items[m].name


def test_contended_alchemy_runs_in_the_slots_of_other_items_unstartable_jobs(conn, ref):
    """The free slots are ANY unstartable direct job's (user: alchemy
    "follows the original rules"). The routed products are the simple
    reactions (bought goo + fuel: startable with fuel on hand); the
    composites above them are not (their simple inputs are built this
    cycle, none on hand). With the pool sized to exactly the direct need
    (contended), alchemy swaps a startable simple-reaction job for
    alchemy jobs at the original `jobs − 1` net cost, in the slots the
    composites left free; the alchemy jobs can start, and the startable
    total still fits the pool."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    direct_need = [
        i for i in baseline.items.values()
        if i.activity_id == config.ACTIVITY_REACTION and i.jobs_needed_unconstrained
    ]
    pool = sum(i.jobs_needed_unconstrained for i in direct_need)
    snap = snapshot(ref, slots=pool, overrides=overrides)
    for item in baseline.items.values():
        if ref.type_info(item.type_id).group_id == 1136:  # fuel blocks
            snap.on_hand[item.type_id] = 10**9
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    direct = [
        i for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION and not i.alchemy_for_type_id
    ]
    alch = alchemy_items(plan)
    composites = [i for i in direct if ref.type_info(i.type_id).group_id == 429 and i.jobs_allocated]
    assert composites and all((i.install_jobs or 0) == 0 for i in composites)
    assert alch, "alchemy ran in the composites' free slots"
    assert all((i.install_jobs or 0) == i.jobs_allocated for i in alch), "the alchemy jobs can start"
    # A routed simple reaction gave up direct jobs for them.
    swapped = [i for i in direct if i.alchemy_output_qty > 0]
    assert swapped and all(i.jobs_allocated < i.jobs_needed_unconstrained for i in swapped)
    assert sum(i.install_jobs or 0 for i in direct) + sum(i.install_jobs or 0 for i in alch) <= pool


def test_no_alchemy_where_every_direct_job_can_start(conn, ref):
    """Every buildable stocked at target: every direct job can start,
    so a pool sized to exactly the direct need is contended with no
    free slot — alchemy stays out (review 2026-09-10: the first version
    stocked everything at 10**9 on a 3-slot pool and planned no reaction
    job at all)."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, empty))
    enable_alchemy(conn)
    stocked = snapshot(ref, overrides=overrides)
    for item in empty.items.values():
        if item.buildable:
            stocked.on_hand[item.type_id] = item.target_stock_qty
    free = engine.plan_index_run(conn, ref, stocked, persist=False, alchemy=False)
    pool = sum(
        i.jobs_needed_unconstrained for i in free.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    assert pool > 0
    stocked.slots_available[config.ACTIVITY_REACTION] = pool
    plan = engine.plan_index_run(conn, ref, stocked, persist=False)
    direct = [
        i for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION and not i.alchemy_for_type_id
    ]
    assert sum(i.jobs_allocated for i in direct) == pool
    assert all((i.install_jobs or 0) == i.jobs_allocated for i in direct)
    assert not alchemy_items(plan)


def test_a_pipeline_selling_an_unrefined_product_keeps_its_plan_row(conn, ref):
    """Review 2026-09-10: the pass ends with `merged[alch.type_id] = alch`,
    which REPLACES any existing row. A pipeline whose final product is one
    of the 17 unrefined route outputs therefore lost its request, cycle
    need and pipeline attribution silently — no unmet flag, and no cost
    basis for that pipeline. Such a route is skipped instead."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    routed = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=False
    )
    taken = alchemy_items(routed)
    assert taken, "the fixture's prices make at least one route win"
    unrefined = taken[0].type_id
    name = ref.type_info(unrefined).name

    # Now sell that very product from a pipeline of its own.
    add_pipeline(conn, ref, name, 500)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=True
    )
    item = plan.items[unrefined]
    assert item.alchemy_for_type_id is None, f"{name} is still the pipeline's row"
    assert item.requested_qty == 500 and item.cycle_need_qty >= 500
    assert item.recommended_build_qty + item.recommended_buy_qty >= 500
    # The route is simply not used; the others still are.
    assert all(i.type_id != unrefined for i in alchemy_items(plan))
    # And the pipeline is attributed, so it has a cost basis.
    pid = conn.execute(
        "SELECT pipeline_id FROM pipeline WHERE final_product_type_id = ?",
        (unrefined,),
    ).fetchone()[0]
    row = conn.execute(
        "SELECT a.qty_attributable FROM index_run_item i "
        "JOIN index_run_item_pipeline a USING (index_run_item_id) "
        "WHERE i.index_run_id = ? AND i.type_id = ? AND a.pipeline_id = ?",
        (plan.index_run_id, unrefined, pid),
    ).fetchone()
    assert row is not None and row["qty_attributable"] >= 500


def _routed_target(conn, ref, overrides):
    """A routed composite the pass costed, whose alchemy route is cheaper
    than building it directly."""
    routed = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=False
    )
    return next(
        i for i in routed.items.values()
        if i.alchemy_unit_cost and i.direct_unit_cost
        and i.alchemy_unit_cost < i.direct_unit_cost
        and i.type_id in ref.alchemy_routes()
    )


def test_alchemy_beats_the_buy_price_and_shrinks_the_purchase(conn, ref):
    """User ruling 2026-09-11. A composite the plan decided to BUY never
    reached the pass: the candidate filter demanded direct jobs, and the
    comparison was against the direct BUILD cost, so "would the unrefined
    route beat what I am about to pay the market?" was never asked. Now a
    bought composite is a candidate, judged against its LANDED price and
    ranked by the same savings, and the plan buys less by what the route
    supplies."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    target = _routed_target(conn, ref, overrides)
    # Price it BETWEEN its alchemy cost and its direct build cost: the
    # market beats building it (so the plan buys it) while the route
    # still beats the market.
    priced = dict(overrides)
    priced[target.type_id] = (target.alchemy_unit_cost + target.direct_unit_cost) / 2
    snap = snapshot(ref, overrides=priced)
    with_alchemy = engine.plan_index_run(conn, ref, snap, persist=False)
    without = engine.plan_index_run(conn, ref, snap, persist=False, alchemy=False)
    a, b = with_alchemy.items[target.type_id], without.items[target.type_id]
    # Without the pass the plan buys it outright: no jobs, negative build
    # savings, a real purchase.
    assert b.jobs_allocated == 0 and (b.build_savings_per_unit or 0) <= 0
    assert b.recommended_buy_qty > 0
    # With the pass the route supplies some of those units ...
    assert [
        i for i in with_alchemy.items.values()
        if i.alchemy_for_type_id == target.type_id and i.jobs_allocated
    ], f"no alchemy for the bought {b.name}"
    assert a.alchemy_output_qty > 0
    # ... and the purchase shrinks by them, never below zero.
    assert 0 <= a.recommended_buy_qty < b.recommended_buy_qty
    assert b.recommended_buy_qty - a.recommended_buy_qty <= a.alchemy_output_qty


def test_alchemy_savings_measure_what_the_route_replaced(conn, ref):
    """2026-09-12: the run page's Alchemy Savings cell measured every route
    against the DIRECT reaction — for a composite the plan buys, the route
    replaces a purchase, so the figure overstated (or turned negative for
    a capacity loser). An outright-bought composite is now measured
    against its landed buy price; a swap keeps the direct cost."""
    from magoo import costing, web

    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    target = _routed_target(conn, ref, overrides)
    priced = dict(overrides)
    priced[target.type_id] = (target.alchemy_unit_cost + target.direct_unit_cost) / 2
    plan = engine.plan_index_run(conn, ref, snapshot(ref, overrides=priced), persist=True)
    rows = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ?", (plan.index_run_id,)
    ).fetchall()
    settings = store.get_settings(conn)
    section = {a["item"]["alchemy_for_type_id"]: a for a in web._alchemy_section(ref, settings, rows)}
    bought = section[target.type_id]
    composite = next(r for r in rows if r["type_id"] == target.type_id)
    assert composite["jobs_allocated"] == 0, "bought outright"
    landed = costing.landed_price(
        ref, settings, composite["price_snapshot"], composite["buy_venue"], target.type_id
    )
    assert bought["benchmark_label"] == "landed buy"
    assert bought["benchmark_unit"] == pytest.approx(landed)
    assert bought["benchmark_unit"] > bought["alchemy_unit"]  # the saving is real
    swaps = [
        a for t, a in section.items()
        if next(r for r in rows if r["type_id"] == t)["jobs_allocated"] > 0
        and a["benchmark_label"] == "direct"
    ]
    for a in swaps:
        c = next(r for r in rows if r["type_id"] == a["item"]["alchemy_for_type_id"])
        assert a["benchmark_unit"] == pytest.approx(c["direct_unit_cost"])


def test_a_bought_composite_the_route_cannot_beat_is_left_alone(conn, ref):
    """The mirror: priced BELOW the alchemy route, the market wins and
    the pass leaves the purchase whole."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    target = _routed_target(conn, ref, overrides)
    priced = dict(overrides)
    priced[target.type_id] = target.alchemy_unit_cost * 0.5  # cheaper than the route
    snap = snapshot(ref, overrides=priced)
    with_alchemy = engine.plan_index_run(conn, ref, snap, persist=False)
    without = engine.plan_index_run(conn, ref, snap, persist=False, alchemy=False)
    a, b = with_alchemy.items[target.type_id], without.items[target.type_id]
    assert b.jobs_allocated == 0 and b.recommended_buy_qty > 0
    assert not [
        i for i in with_alchemy.items.values()
        if i.alchemy_for_type_id == target.type_id and i.jobs_allocated
    ]
    assert a.alchemy_buy_qty == 0
    assert a.recommended_buy_qty == b.recommended_buy_qty


def test_candidates_are_ranked_by_isk_saved_per_slot(conn, ref):
    """The v1.4 ranking is unchanged by the buy comparison (user ruling
    2026-09-11, "rank them as we did before"): every candidate scores ISK
    saved on the units its jobs SUPPLY, per spare slot consumed, whether
    it is beating a direct build or a purchase. A clamped buy replacement
    covers only part of its residual, so crediting the whole of it would
    out-rank honest candidates."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn, cap=4)  # a cap tight enough to clamp a buy bite
    target = _routed_target(conn, ref, overrides)
    priced = dict(overrides)
    priced[target.type_id] = (target.alchemy_unit_cost + target.direct_unit_cost) / 2
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=priced), persist=False
    )
    item = plan.items[target.type_id]
    alch = [
        i for i in plan.items.values()
        if i.alchemy_for_type_id == target.type_id and i.jobs_allocated
    ]
    assert alch, "the route still runs under a tight cap"
    # The cap bound the bite, and the output never exceeds what those
    # jobs can make.
    assert alch[0].jobs_allocated <= 4
    assert item.alchemy_output_qty <= alch[0].jobs_allocated * (
        alch[0].max_runs_per_job * ref.alchemy_routes()[target.type_id].composite_qty
    )
    # Every routed composite that won jobs saves against what the plan
    # would otherwise have paid — the ranking's numerator is never
    # negative.
    for i in plan.items.values():
        if i.alchemy_for_type_id and i.jobs_allocated:
            composite = plan.items[i.alchemy_for_type_id]
            assert composite.alchemy_unit_cost < max(
                composite.direct_unit_cost or 0.0,
                composite.price_snapshot or 0.0,
            ), composite.name


def _direct_reactions(plan):
    return [
        i for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION and not i.alchemy_for_type_id
    ]


def test_a_pool_short_of_full_on_paper_still_frees_unstartable_slots(conn, ref):
    """Run 19 (2026-09-12): 523 direct jobs planned on a 540 pool, 79 of
    them unstartable. The pass counted `slots − allocated` whenever the
    pool was not full on paper, saw 17 spare and left 79 slots idle while
    cheaper routes waited. The free slots are the pool less the STARTABLE
    direct jobs however full it is on paper: with the pool two slots above
    the direct need (composites unstartable, their simple inputs built this
    cycle), alchemy must reach beyond those two slots."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    need = sum(i.jobs_needed_unconstrained for i in _direct_reactions(baseline))
    pool = need + 2
    snap = snapshot(ref, slots=pool, overrides=overrides)
    without = engine.plan_index_run(conn, ref, snap, persist=False, alchemy=False)
    direct_without = _direct_reactions(without)
    allocated = sum(i.jobs_allocated for i in direct_without)
    startable = sum(i.install_jobs or 0 for i in direct_without)
    assert allocated < pool, "not full on paper"
    assert startable < allocated, "some direct jobs cannot start"
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    alch = alchemy_items(plan)
    dropped = allocated - sum(i.jobs_allocated for i in _direct_reactions(plan))
    net_slots = sum(i.jobs_allocated for i in alch) - dropped
    assert net_slots > pool - allocated, (net_slots, pool - allocated)
    # Every alchemy job can start, and the startable total fits the pool.
    assert all((i.install_jobs or 0) == i.jobs_allocated for i in alch)
    assert sum(
        i.install_jobs or 0 for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    ) <= pool


def _split_target(conn, ref, overrides):
    """A routed composite quoted between its route and its direct build,
    no freight — a cheap rung for part of its quantity makes it a split
    row (the caller sizes the rung)."""
    target = _routed_target(conn, ref, overrides)
    priced = dict(overrides)
    priced[target.type_id] = (target.alchemy_unit_cost + target.direct_unit_cost) / 2
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 0, "
        "structure_freight_in_isk_per_m3 = 0"
    )
    conn.commit()
    return target, priced


def _split_plans(conn, ref):
    """(target, snapshot, plan without alchemy, plan with alchemy) for a
    routed composite that builds part of its need and buys a third of it
    off a cheap rung, with stock covering this cycle's draw."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn, cap=100)
    target, priced = _split_target(conn, ref, overrides)

    def snap_with(held, ladders=None):
        snap = snapshot(ref, overrides=priced)
        snap.sell_ladders = ladders or {}
        snap.on_hand[target.type_id] = held
        return snap

    # Stock covers what this cycle's consumers draw, so the whole bought
    # share refills the stockpile and may be replaced (the timing guard's
    # own test holds stock below the draw). The cheap rung then holds a
    # third of what is left to source.
    probe = engine.plan_index_run(conn, ref, snap_with(0), persist=False, alchemy=False)
    held = probe.items[target.type_id].install_draw_qty or 0
    stocked = engine.plan_index_run(conn, ref, snap_with(held), persist=False, alchemy=False)
    row = stocked.items[target.type_id]
    units = row.total_runs_needed * row.portion_size
    ladders = {store.BUY_VENUE_HUB: {target.type_id: [(priced[target.type_id], units // 3)]}}
    snap = snap_with(held, ladders)
    without = engine.plan_index_run(conn, ref, snap, persist=False, alchemy=False)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    return target, snap, without, plan


def test_alchemy_replaces_the_bought_share_of_a_split_row(conn, ref):
    """User ruling 2026-09-12. A composite that builds part of its need and
    buys the share the market beat (Ferrofluid in run 19: 4 jobs + 903,200
    bought at 30.1k against a 17.3k route) was only ever offered the SWAP,
    so the purchase stood whatever the route cost. The bought share is now
    a buy candidate like an outright purchase: judged against the landed
    price, credited against that share (never the build shortfall too)."""
    target, _snap, without, plan = _split_plans(conn, ref)
    b = without.items[target.type_id]
    assert b.jobs_allocated > 0 and b.market_buy_qty > 0, "a split row"
    assert b.recommended_buy_qty == b.market_buy_qty
    a = plan.items[target.type_id]
    assert a.alchemy_buy_qty > 0, "the route replaced part of the purchase"
    assert a.recommended_buy_qty == a.market_buy_qty - a.alchemy_buy_qty
    assert a.recommended_buy_qty < b.recommended_buy_qty
    # Coverage holds: jobs + purchase + route output reach the deficit,
    # and the replaced share is not also counted against the build part.
    assert (
        a.recommended_build_qty + a.recommended_buy_qty + a.alchemy_output_qty
        >= a.deficit_qty
    )
    swap_output = a.alchemy_output_qty - a.alchemy_buy_qty
    assert not a.capacity_limited or (
        (a.total_runs_needed - a.runs_allocated) * a.portion_size > swap_output
    )


def test_output_replacing_a_purchase_leaves_the_build_shortfall_bought(conn, ref):
    """A split row holds two purchases: the market-beaten share and the
    fallback buy for a build shortfall. Output that replaced the first
    (`alchemy_buy_qty`) must not be credited against the second as well —
    Phase 7 would then under-buy the shortfall by the same units. Both
    crediting sites are checked: Phase 7's own sizing and _fallback_buy_of,
    which the backfill and the alchemy pass size purchases with."""
    target, snap, _without, plan = _split_plans(conn, ref)
    row = plan.items[target.type_id]
    assert row.jobs_allocated > 1 and row.market_buy_qty > 1
    # Re-finalize the row one job short (a build shortfall), with half of
    # its market-beaten share replaced by alchemy output.
    replaced = row.market_buy_qty // 2
    row.jobs_allocated -= 1
    row.market_fallback_qty = None  # the whole shortfall is buyable
    row.alchemy_output_qty = row.alchemy_buy_qty = replaced
    row.recommended_action, row.recommended_buy_qty = "build", 0
    engine._finalize(conn, ref, {row.type_id: row}, snap)
    shortfall = (row.total_runs_needed - row.runs_allocated) * row.portion_size
    assert shortfall > 0
    assert row.recommended_buy_qty == shortfall + row.market_buy_qty - replaced
    assert engine._fallback_buy_of(ref, set(), row) == shortfall


def test_alchemy_never_replaces_units_this_cycles_consumers_draw(conn, ref):
    """User ruling 2026-09-12. A purchase arrives NOW; alchemy output lands
    at the end of the cycle and needs a reprocess, and the install check
    counts the purchase but never the output. Replacing units this cycle's
    startable consumers draw would idle their slots. Every buildable is
    stocked at its target (so consumers can start) except the bought
    composite, held well below what its consumers draw: the route may take
    only the part of the purchase beyond that draw, and no consumer of the
    composite loses a startable job to it."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, empty))
    enable_alchemy(conn, cap=100)
    target = _routed_target(conn, ref, overrides)
    priced = dict(overrides)
    priced[target.type_id] = (target.alchemy_unit_cost + target.direct_unit_cost) / 2

    def stocked(held):
        # An ample pool: the spare slots must not be what stops the
        # route, or the guard is never the binding limit (fuel is bought
        # just in time, 2026-09-12).
        snap = snapshot(ref, slots=5000, overrides=priced)
        for item in empty.items.values():
            if item.buildable:
                snap.on_hand[item.type_id] = item.target_stock_qty
        snap.on_hand[target.type_id] = held
        return snap

    probe = engine.plan_index_run(conn, ref, stocked(0), persist=False, alchemy=False)
    drawn = probe.items[target.type_id].install_draw_qty or 0
    assert drawn > 0, "the composite's consumers draw it this cycle"
    held = drawn // 3
    snap = stocked(held)
    without = engine.plan_index_run(conn, ref, snap, persist=False, alchemy=False)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    b, a = without.items[target.type_id], plan.items[target.type_id]
    assert b.jobs_allocated == 0 and b.recommended_buy_qty > drawn - held
    # The route does take the replaceable part of the purchase ...
    assert a.alchemy_output_qty > 0 and a.recommended_buy_qty < b.recommended_buy_qty
    # ... but the purchase still covers this cycle's draw beyond stock ...
    assert a.recommended_buy_qty >= (b.install_draw_qty or 0) - held
    # ... and no consumer of the composite lost a startable job to alchemy.
    for t, row in without.items.items():
        if row.install_jobs is None or t == target.type_id:
            continue
        uses = any(
            m == target.type_id
            for m, _q in ref.materials(row.blueprint_id, row.activity_id)
        )
        if uses:
            assert (plan.items[t].install_jobs or 0) >= row.install_jobs, row.name


def test_per_type_job_cap(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    for cap in (50, 2):
        enable_alchemy(conn, cap=cap)
        plan = engine.plan_index_run(
            conn, ref, snapshot(ref, overrides=overrides), persist=False
        )
        for alch in alchemy_items(plan):
            assert alch.jobs_allocated <= cap


def test_alchemy_inputs_join_jit_purchasing(conn, ref):
    """The alchemy formula's own inputs (e.g. the cheap goo the chain never
    otherwise demands) must show up as just-in-time buys."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=False
    )
    routes = ref.alchemy_routes()
    checked = 0
    for alch in alchemy_items(plan):
        for material_id, _qty in ref.materials(
            alch.blueprint_id, alch.activity_id
        ):
            material = plan.items[material_id]
            if material.buildable:
                continue  # fuel blocks etc. handled by their own planning
            assert material.recommended_buy_qty > 0
            checked += 1
    assert checked


def test_persistence_roundtrip_with_alchemy(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=True
    )
    rows = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? "
        "AND alchemy_for_type_id IS NOT NULL",
        (plan.index_run_id,),
    ).fetchall()
    assert len(rows) == len(alchemy_items(plan))
    for row in rows:
        composite = conn.execute(
            "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
            (plan.index_run_id, row["alchemy_for_type_id"]),
        ).fetchone()
        assert composite["alchemy_unit_cost"] < composite["direct_unit_cost"]
        assert composite["alchemy_output_qty"] > 0


def test_alchemy_jobs_capped_at_30_days_modified_time(conn, ref):
    """The in-game ceiling is 30 days of MODIFIED time with the last-run
    overhang (user-verified 2026-08-21; the earlier verified 272 was this
    same rule at the user's Tatara + T2 rig: 9,538.56s/run). At NPC test
    defaults an unrefined formula runs 17,280s/run with Reactions V ->
    ceil(2,592,000 / 17,280) = 150 runs."""
    conn.execute("UPDATE settings SET max_run_duration_hours = 2000")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=False
    )
    swapped = alchemy_items(plan)
    assert swapped
    for alch in swapped:
        # 2000h window >> the cap, so every alchemy job pins to the
        # 30-day ceiling at test defaults
        assert alch.max_runs_per_job == 150


# --- Unrefined stock / in-progress credits ----------------------------------


def test_unrefined_stock_credits_composite_and_recovered(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    target = reaction_candidates(ref, baseline)[0]
    route = ref.alchemy_routes()[target.type_id]
    enable_alchemy(conn)

    snap = snapshot(ref)
    snap.on_hand[route.unrefined_id] = 60
    snap.in_progress[route.unrefined_id] = 40
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    composite = plan.items[route.composite_id]
    credit = math.floor(100 * route.composite_qty * 0.55)
    assert composite.alchemy_credit_qty == credit
    # credited as in-progress (a manual reprocess still stands between the
    # unrefined items and usable stock)
    assert composite.in_progress_qty == credit
    # the deficit shrinks accordingly
    assert composite.deficit_qty <= baseline.items[composite.type_id].deficit_qty
    for material_id, base_qty in route.recovered:
        if material_id in plan.items:
            recovered = plan.items[material_id]
            assert recovered.alchemy_credit_qty == math.floor(
                100 * base_qty * 0.55
            )


def test_no_credits_when_alchemy_disabled(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    target = reaction_candidates(ref, baseline)[0]
    route = ref.alchemy_routes()[target.type_id]
    snap = snapshot(ref)
    snap.on_hand[route.unrefined_id] = 100
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[route.composite_id].alchemy_credit_qty == 0
    assert plan.items[route.composite_id].in_progress_qty == 0


# --- Phase 8: reprocess bookkeeping -----------------------------------------


def test_reprocess_unrefined_cost_lots(conn, ref):
    routes = ref.alchemy_routes()
    route = routes[ref.type_id("Ferrofluid")]
    hafnium, base_qty = route.recovered[0]
    # 10 unrefined at 1,000 ISK each entered the pipeline
    engine.record_purchase(conn, None, route.unrefined_id, 10, 1000.0)
    composite_out = math.floor(10 * route.composite_qty * 0.55)  # 401
    recovered_out = math.floor(10 * base_qty * 0.55)  # 951
    lot = engine.reprocess_unrefined(
        conn,
        None,
        route.unrefined_id,
        10,
        route.composite_id,
        composite_out,
        recovered=[(hafnium, recovered_out, 5.0)],
    )
    row = conn.execute(
        "SELECT * FROM cost_lot WHERE lot_id = ?", (lot,)
    ).fetchone()
    assert row["type_id"] == route.composite_id
    # composite carries the residual: total in minus recovered credit
    expected = (10 * 1000.0 - recovered_out * 5.0) / composite_out
    assert row["unit_cost"] == pytest.approx(expected)
    # recovered lot exists at its credit price
    rec = conn.execute(
        "SELECT * FROM cost_lot WHERE type_id = ?", (hafnium,)
    ).fetchone()
    assert rec["quantity_remaining"] == recovered_out
    assert rec["unit_cost"] == pytest.approx(5.0)
    # the unrefined lot was drained FIFO
    unref = conn.execute(
        "SELECT quantity_remaining FROM cost_lot WHERE type_id = ?",
        (route.unrefined_id,),
    ).fetchone()
    assert unref["quantity_remaining"] == 0


def test_reprocess_conserves_isk_when_credit_exceeds_cost(conn, ref):
    """Recovered credit above the ISK actually drawn is scaled down —
    the lot genealogy must never hold more ISK than entered it (the old
    zero-clamp left the full credit in the recovered lots and conjured
    the difference)."""
    routes = ref.alchemy_routes()
    route = routes[ref.type_id("Ferrofluid")]
    hafnium, base_qty = route.recovered[0]
    engine.record_purchase(conn, None, route.unrefined_id, 10, 1000.0)
    composite_out = math.floor(10 * route.composite_qty * 0.55)
    recovered_out = math.floor(10 * base_qty * 0.55)  # 951
    credit = recovered_out * 12.0  # 11,412 > the 10,000 drawn
    lot = engine.reprocess_unrefined(
        conn,
        None,
        route.unrefined_id,
        10,
        route.composite_id,
        composite_out,
        recovered=[(hafnium, recovered_out, 12.0)],
    )
    composite = conn.execute(
        "SELECT unit_cost FROM cost_lot WHERE lot_id = ?", (lot,)
    ).fetchone()
    recovered = conn.execute(
        "SELECT quantity_remaining, unit_cost FROM cost_lot WHERE type_id = ?",
        (hafnium,),
    ).fetchone()
    scale = 10_000.0 / credit
    assert composite["unit_cost"] == pytest.approx(0.0)
    assert recovered["unit_cost"] == pytest.approx(12.0 * scale)
    held = (
        composite_out * composite["unit_cost"]
        + recovered["quantity_remaining"] * recovered["unit_cost"]
    )
    assert held == pytest.approx(10_000.0)  # conservation


# --- Feedback-pass reset: no orphan rows -------------------------------------


def test_feedback_reset_leaves_no_orphan_rows(conn, ref):
    """The consumption feedback pass replans from a reset state; when the
    revised pass drops an alchemy route that pass 1 picked, the route's
    raw formula inputs (added by the alchemy pass with no BOM demand)
    must be deleted with it — the audit's instrumented probe saw them
    persist as inert rows (target 0, deficit 0, no action, not buildable)
    at reaction slot counts 350/315/311/305/300/280/240/230/225/222/220/
    212/208 before the fix. Repro shape: Hulk x 8, alchemy on, rare goo
    at 1M ISK so alchemy wins pass 1, reaction pool at 311.

    The no-inert-row invariant is the test: it guards the whole orphan
    class even though, post-fix, the revised pass at THIS slot count
    drops the routes cleanly (the final plan carries no alchemy rows at
    all — precisely the drop that used to strand their raw inputs). The
    reaction-pool bound rides along as a sanity anchor; that alchemy
    genuinely wins at these prices is anchored at an ample pool."""
    reaction_slots = 311
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    assert overrides  # rare goo really overridden to 1M ISK
    enable_alchemy(conn)
    ample = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides=overrides), persist=False
    )
    assert alchemy_items(ample)  # the price setup makes alchemy win
    snap = Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: 500,
            config.ACTIVITY_REACTION: reaction_slots,
        },
        prices=FairValuePrices(ref, overrides=overrides),
        adjusted_prices=Overlay(),
    )
    plan = engine.plan_index_run(conn, ref, snap, persist=False)

    inert = [
        i
        for i in plan.items.values()
        if not (
            i.buildable
            or i.alchemy_for_type_id is not None
            or i.merged_min_qty > 0
            or i.deficit_qty > 0
            or i.recommended_action is not None
        )
    ]
    assert inert == [], [i.name for i in inert]

    # Sanity anchor: the reaction-pool bound still holds under pressure —
    # on the STARTABLE jobs; since the stock-aware backfill the planned
    # count may exceed the pool, as jobs stock cannot feed hold no slot
    # (and alchemy runs in those slots on bought fuel, 2026-09-12).
    used = sum(
        i.install_jobs or 0
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    assert used <= reaction_slots
