"""Schema 14 (2026-10-30): the alchemy blacklist and alchemy for profit.

Same fixture pattern as test_alchemy (real reference data, temp state DB,
FairValuePrices with overrides); the helpers are imported from there.
"""

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from magoo import config, costing, engine, holds, industry, ledger, store
from magoo.engine import Snapshot
from conftest import FairValuePrices
from test_alchemy import (  # noqa: F401 — conn is the fixture
    FUEL_BLOCKS,
    Overlay,
    add_pipeline,
    alchemy_items,
    conn,
    enable_alchemy,
    expensive_rare_inputs,
    reaction_candidates,
    snapshot,
)


def enable_profit(conn):
    conn.execute("UPDATE settings SET alchemy_for_profit = 1")
    conn.commit()


def off_chain_routes(ref, plan):
    """Routes whose composite is no plan row of `plan` — the pure for-sale
    case — with the composite in the saturating reaction groups."""
    return [
        route
        for composite_id, route in ref.alchemy_routes().items()
        if composite_id not in plan.items
        and route.unrefined_id not in plan.items
        and ref.type_info(composite_id).group_id
        not in config.NON_SATURATING_REACTION_GROUPS
    ]


def net_sale(ref, settings, type_id, price):
    return costing.net_proceeds_per_hull(
        price, ref.type_info(type_id).freight_volume, settings,
        capital=False, freight_exempt=costing.freight_out_exempt(type_id),
    )


def reaction_jobs(plan, attr="jobs_allocated"):
    return sum(
        getattr(i, attr) or 0
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )


# --- the blacklist ------------------------------------------------------------


def test_a_blacklisted_route_never_runs_for_savings(conn, ref):
    """The user ticks a route under Never run: however cheap, it gets no
    savings move. The other routes keep theirs."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    swapped = alchemy_items(
        engine.plan_index_run(conn, ref, snapshot(ref, overrides=overrides), persist=False)
    )
    assert len(swapped) >= 2
    blocked = swapped[0].alchemy_for_type_id
    store.set_alchemy_blacklist(conn, {blocked})
    conn.commit()
    plan = engine.plan_index_run(conn, ref, snapshot(ref, overrides=overrides), persist=False)
    after = alchemy_items(plan)
    assert all(a.alchemy_for_type_id != blocked for a in after)
    assert {a.alchemy_for_type_id for a in after} >= {
        a.alchemy_for_type_id for a in swapped[1:]
    }
    # The route's comparison is not even recorded on the composite.
    assert plan.items[blocked].alchemy_unit_cost is None
    assert plan.items[blocked].alchemy_output_qty == 0


def test_a_blacklisted_route_still_credits_unrefined_stock(conn, ref):
    """Blacklisting stops JOBS. Unrefined items already in the hangar are
    stock, and still count toward their composite at the yield."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    target = reaction_candidates(ref, baseline)[0]
    route = ref.alchemy_routes()[target.type_id]
    enable_alchemy(conn)
    store.set_alchemy_blacklist(conn, {route.composite_id})
    conn.commit()
    snap = snapshot(ref)
    snap.on_hand[route.unrefined_id] = 100
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[route.composite_id].alchemy_credit_qty == math.floor(
        100 * route.composite_qty * 0.55
    )


# --- alchemy for profit -------------------------------------------------------


def test_profit_alchemy_fills_spare_slots_with_jobs_for_sale(conn, ref):
    """A composite the chain never touches, quoted at a fortune in Jita,
    fills the spare reaction slots with its unrefined formula — jobs
    marked for sale, judged net of fees against the route's cost, its goo
    bought just in time, the composite never a plan row and the pool
    never exceeded."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = off_chain_routes(ref, baseline)[0]
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides={route.composite_id: 1e6}), persist=False
    )
    rows = [a for a in alchemy_items(plan) if a.alchemy_for_type_id == route.composite_id]
    assert len(rows) == 1
    row = rows[0]
    assert row.jobs_allocated > 0
    assert row.alchemy_profit_jobs == row.jobs_allocated  # every job for sale
    assert row.alchemy_sale_net_unit is not None and row.alchemy_route_unit_cost is not None
    assert row.alchemy_sale_net_unit > row.alchemy_route_unit_cost
    settings = store.get_settings(conn)
    assert row.alchemy_sale_net_unit == pytest.approx(
        net_sale(ref, settings, route.composite_id, 1e6)
    )
    assert route.composite_id not in plan.items  # for sale, not for the chain
    # Jobs saturate the window and the output is the install quantity.
    assert row.recommended_build_qty == row.jobs_allocated * row.max_runs_per_job
    # Its inputs are plan rows Phase 7 buys just in time.
    for m, _q in ref.materials(row.blueprint_id, row.activity_id):
        assert m in plan.items
    # The reaction pool holds every STARTABLE job, alchemy included, and
    # the spare slots were FILLED (ample cap, profitable route, fuel
    # priced): startable direct + for-sale alchemy is exactly the pool.
    # (Planned direct jobs exceed it — jobs stock cannot feed this cycle
    # hold no slot, the 2026-09-12 rule — so the planned total is not
    # the figure.)
    assert reaction_jobs(plan, "install_jobs") == 500
    assert row.install_jobs == row.jobs_allocated


def test_profit_alchemy_needs_both_flags(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = off_chain_routes(ref, baseline)[0]

    def snap():
        return snapshot(ref, overrides={route.composite_id: 1e6})

    # Alchemy on, profit off: nothing for sale.
    enable_alchemy(conn)
    assert not alchemy_items(engine.plan_index_run(conn, ref, snap(), persist=False))
    # Profit on, alchemy off: the pass never runs.
    conn.execute("UPDATE settings SET alchemy_enabled = 0, alchemy_for_profit = 1")
    conn.commit()
    assert not alchemy_items(engine.plan_index_run(conn, ref, snap(), persist=False))


def test_profit_alchemy_never_credits_the_chain(conn, ref):
    """A routed composite the chain BUILDS, quoted at a fortune: the
    for-sale jobs run, but the composite row is exactly as the plan
    without them left it — no output credited, no purchase shrunk, no
    direct job displaced."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    target = reaction_candidates(ref, baseline)[0]
    overrides = {target.type_id: 1e6}
    enable_alchemy(conn, cap=1000)
    without = engine.plan_index_run(conn, ref, snapshot(ref, overrides=overrides), persist=False)
    enable_profit(conn)
    plan = engine.plan_index_run(conn, ref, snapshot(ref, overrides=overrides), persist=False)
    rows = [a for a in alchemy_items(plan) if a.alchemy_for_type_id == target.type_id]
    assert rows and rows[0].alchemy_profit_jobs == rows[0].jobs_allocated > 0
    a, b = plan.items[target.type_id], without.items[target.type_id]
    assert (a.jobs_allocated, a.alchemy_output_qty, a.alchemy_buy_qty) == (
        b.jobs_allocated, 0, 0
    )
    assert a.recommended_buy_qty == b.recommended_buy_qty
    assert a.deficit_qty == b.deficit_qty
    assert a.low_stock == b.low_stock


def test_profit_alchemy_skips_unpriced_and_unprofitable_routes(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    unpriced, cheap = off_chain_routes(ref, baseline)[:2]
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    plan = engine.plan_index_run(
        conn, ref,
        snapshot(ref, overrides={unpriced.composite_id: None, cheap.composite_id: 1.0}),
        persist=False,
    )
    for_sale = {a.alchemy_for_type_id for a in alchemy_items(plan) if a.alchemy_profit_jobs}
    assert unpriced.composite_id not in for_sale
    assert cheap.composite_id not in for_sale
    # Nothing else pays at fair value either (every route's hub net sits
    # under its cost — test_no_profitable_route_lists_no_for_sale_row_at_all):
    # an unprofitable world has no for-sale row at all, not just none for
    # the two steered routes.
    assert for_sale == set()


def test_profit_alchemy_skips_blacklisted_routes(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = off_chain_routes(ref, baseline)[0]
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    store.set_alchemy_blacklist(conn, {route.composite_id})
    conn.commit()
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides={route.composite_id: 1e6}), persist=False
    )
    assert all(a.alchemy_for_type_id != route.composite_id for a in alchemy_items(plan))


def test_profit_alchemy_ranks_by_profit_per_slot_and_shares_the_cap(conn, ref):
    """Two for-sale routes compete for four spare slots under a per-type
    cap of three: the one worth more per slot takes three, the other the
    last slot."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    rich, poor = off_chain_routes(ref, baseline)[:2]
    enable_alchemy(conn, cap=3)
    enable_profit(conn)
    snap = snapshot(ref, overrides={rich.composite_id: 1e6, poor.composite_id: 1e5})
    # Every reaction product stocked to the sky: no direct reaction has a
    # deficit, so a pool of four is four spare slots, exactly.
    for i in baseline.items.values():
        if i.activity_id == config.ACTIVITY_REACTION:
            snap.on_hand[i.type_id] = 10**9
    snap.slots_available[config.ACTIVITY_REACTION] = 4
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert reaction_jobs(plan) == 4
    by_composite = {a.alchemy_for_type_id: a for a in alchemy_items(plan)}
    first, second = by_composite[rich.composite_id], by_composite[poor.composite_id]
    assert (first.jobs_allocated, second.jobs_allocated) == (3, 1)
    assert first.alchemy_profit_jobs == 3 and second.alchemy_profit_jobs == 1

    def per_slot(row, route):
        out = math.floor(row.max_runs_per_job * route.composite_qty * 0.55)
        return (row.alchemy_sale_net_unit - row.alchemy_route_unit_cost) * out

    assert per_slot(first, rich) > per_slot(second, poor)
    assert reaction_jobs(plan, "install_jobs") == 4


def test_profit_jobs_follow_the_savings_moves_under_one_cap(conn, ref):
    """A route that wins savings swaps AND sells at a profit: the swaps
    are placed first and credited to the chain, the for-sale jobs take
    the rest of the cap, and the composite is credited the swaps' output
    only."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    # An ample pool, so the savings moves leave spare slots for the tail
    # (at 500 they fill every one), and a cap the swaps stay under.
    cap, slots = 200, 3000
    enable_alchemy(conn, cap=cap)
    savings_only = engine.plan_index_run(
        conn, ref, snapshot(ref, slots=slots, overrides=overrides), persist=False
    )
    # A route whose swaps leave room under the cap (8 Hulks' composite
    # needs make the first swap tens of jobs; some routes hit the cap).
    swapped = next(a for a in alchemy_items(savings_only) if a.jobs_allocated < cap)
    composite_id = swapped.alchemy_for_type_id
    overrides[composite_id] = 1e6
    enable_profit(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, slots=slots, overrides=overrides), persist=False
    )
    row = next(a for a in alchemy_items(plan) if a.alchemy_for_type_id == composite_id)
    assert row.jobs_allocated <= cap
    assert 0 < row.alchemy_profit_jobs < row.jobs_allocated
    savings_jobs = row.jobs_allocated - row.alchemy_profit_jobs
    assert savings_jobs == swapped.jobs_allocated
    out_per_job = math.floor(
        row.max_runs_per_job * ref.alchemy_routes()[composite_id].composite_qty * 0.55
    )
    assert plan.items[composite_id].alchemy_output_qty == savings_jobs * out_per_job


def test_profit_alchemy_is_fuel_gated(conn, ref):
    """Fuel the chain builds with no market and none on hand cannot be
    bought just in time, so no for-sale job can start either."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = off_chain_routes(ref, baseline)[0]
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    overrides = {route.composite_id: 1e6}
    overrides.update({ref.type_id(n): None for n in FUEL_BLOCKS})
    plan = engine.plan_index_run(conn, ref, snapshot(ref, overrides=overrides), persist=False)
    assert not [a for a in alchemy_items(plan) if a.alchemy_profit_jobs]


def test_profit_route_cost_credits_recovered_goo_by_where_it_goes(conn, ref):
    """Recovered inputs the chain buys are credited landed (they offset a
    purchase); ones the chain never draws would have to be sold, so they
    are credited at their net Jita proceeds."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = next(
        r for r in off_chain_routes(ref, baseline)
        if any(m not in baseline.items for m, _q in r.recovered)
    )
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    snap = snapshot(ref, overrides={route.composite_id: 1e6})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    row = next(a for a in alchemy_items(plan) if a.alchemy_for_type_id == route.composite_id)
    settings = store.get_settings(conn)
    setting = store.get_class_settings(conn).get("reactions", industry.NPC_STATION)

    def landed(t):
        return engine._landed_price(ref, settings, snap, t)

    unrefined_cost = engine._unit_build_cost(
        ref, route.formula.blueprint_id, route.formula.activity_id,
        route.formula.portion_size, setting, 0, snap, runs=row.max_runs_per_job,
        scc_surcharge=settings.industry_scc_surcharge, price_of=landed,
    )

    def credit(m):
        demanded = m in plan.items and (plan.items[m].merged_min_qty or 0) > 0
        return landed(m) if demanded else net_sale(ref, settings, m, snap.price(m))

    expected = (
        unrefined_cost - sum(q * 0.55 * credit(m) for m, q in route.recovered)
    ) / (route.composite_qty * 0.55)
    assert row.alchemy_route_unit_cost == pytest.approx(expected)
    # ... and the rule bites: a landed credit for the sold goo would differ.
    landed_only = (
        unrefined_cost - sum(q * 0.55 * landed(m) for m, q in route.recovered)
    ) / (route.composite_qty * 0.55)
    assert row.alchemy_route_unit_cost != pytest.approx(landed_only)


def test_price_ids_cover_every_unblocked_route_with_profit_on(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = off_chain_routes(ref, baseline)[0]
    enable_alchemy(conn)
    # Savings only: an off-chain route is nobody's demand.
    assert route.composite_id not in engine.market_type_ids(conn, ref)
    enable_profit(conn)
    ids = engine.market_type_ids(conn, ref)
    for r in ref.alchemy_routes().values():
        assert r.composite_id in ids and r.unrefined_id in ids
    # A blacklisted route adds nothing of its own.
    store.set_alchemy_blacklist(conn, {route.composite_id})
    conn.commit()
    ids = engine.market_type_ids(conn, ref)
    assert route.composite_id not in ids and route.unrefined_id not in ids


def test_persistence_roundtrip_for_sale_columns(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = off_chain_routes(ref, baseline)[0]
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides={route.composite_id: 1e6}), persist=True
    )
    row = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, route.unrefined_id),
    ).fetchone()
    live = plan.items[route.unrefined_id]
    assert row["alchemy_profit_jobs"] == live.alchemy_profit_jobs > 0
    assert row["alchemy_sale_net_unit"] == pytest.approx(live.alchemy_sale_net_unit)
    assert row["alchemy_route_unit_cost"] == pytest.approx(live.alchemy_route_unit_cost)
    # Composite rows never carry the for-sale figures.
    assert conn.execute(
        "SELECT COUNT(*) FROM index_run_item WHERE index_run_id = ? "
        "AND alchemy_for_type_id IS NULL AND alchemy_profit_jobs > 0",
        (plan.index_run_id,),
    ).fetchone()[0] == 0


def test_feedback_reset_leaves_no_orphan_for_sale_rows(conn, ref):
    """The sizing feedback loop deletes and re-derives alchemy rows each
    pass; for-sale rows and the goo they add ride the same reset."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    route = off_chain_routes(ref, baseline)[0]
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, overrides={route.composite_id: 1e6}), persist=False
    )
    inert = [
        i for i in plan.items.values()
        if not (
            i.buildable
            or i.alchemy_for_type_id is not None
            or i.merged_min_qty > 0
            or i.recommended_buy_qty > 0
        )
    ]
    assert not inert, [i.name for i in inert]
    rows = [a for a in alchemy_items(plan) if a.alchemy_for_type_id == route.composite_id]
    assert len(rows) == 1 and rows[0].alchemy_profit_jobs == rows[0].jobs_allocated


# --- the profit tail after the 2026-10-31 review -------------------------------
#
# User rulings 2026-10-31 (plan "Alchemy for-sale hold", D5): the tail skips
# a route the Production Blacklist names (h) and a composite some pipeline
# sells as its final; its choice and ranking stand at the Phase 1 quote and
# the stored route cost is RE-STAMPED at the fill once Phase 7.5 has priced
# the purchases (f); the sale reference is the Jita quote net of fees and
# freight-out; two routes sharing a stock-limited fuel block share its stock.
# The capacity netting, the installed-jobs cap, the install-check rationing,
# the pool-overflow trim and the extra-targets skip are test_alchemy_hold's.

HUB = store.BUY_VENUE_HUB
STRUCT = store.BUY_VENUE_STRUCTURE


def _route(ref, composite_name):
    return ref.alchemy_routes()[ref.type_id(composite_name)]


def for_sale_world(conn, ref, **kwargs):
    """test_alchemy_hold's shared world (Hulk x 8, the on-chain Prometium
    route quoted, a pull before the run), imported at call time: that
    module imports this one's helpers, so a module-level import would be
    a cycle."""
    from test_alchemy_hold import for_sale_world as world

    return world(conn, ref, **kwargs)


def _for_sale_rows(plan):
    """{composite: unrefined row} over the rows holding for-sale jobs."""
    return {a.alchemy_for_type_id: a for a in alchemy_items(plan) if a.alchemy_profit_jobs}


def _formula(ref, route):
    return dict(ref.materials(route.formula.blueprint_id, route.formula.activity_id))


def _spare_pool_world(conn, ref, overrides, *, slots, cap):
    """Hulk x 8 with every direct reaction product stocked to the sky — no
    direct reaction has a deficit, so a pool of `slots` is `slots` spare
    lines, exactly — alchemy and Alchemy for Profit on at `cap`, the given
    composites quoted (an override of None = no price). Returns (snapshot,
    baseline plan)."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    enable_alchemy(conn, cap=cap)
    enable_profit(conn)
    snap = snapshot(ref, overrides=overrides)
    for i in baseline.items.values():
        if i.activity_id == config.ACTIVITY_REACTION:
            snap.on_hand[i.type_id] = 10**9
    snap.slots_available[config.ACTIVITY_REACTION] = slots
    return snap, baseline


def _landed(ref, settings, snap, type_id):
    """A hub buy's landed price, written out: the quote plus the Jita leg
    on packaged m³."""
    return snap.prices.get(type_id) + settings.freight_in_isk_per_m3 * ref.type_info(
        type_id
    ).freight_volume


def _route_cost(ref, settings, snap, route, runs, price_of, credit_of=None, *,
                mat_mult=1.0, setting=industry.NPC_STATION):
    """The route's cost per composite unit from the formula sheet: each
    input rounded once per job at `runs` and priced by `price_of`, the
    install fee on the EIV (base quantities x adjusted prices) at the
    facility's index, tax and SCC, less the reprocess's recovered goo at
    `credit_of` (default `price_of`) x the yield, over the composite units
    one unrefined unit yields — the Decision Log's "Alchemy for profit"
    figure, independent of engine._route_unit_cost."""
    credit_of = credit_of or price_of
    formula = _formula(ref, route)
    material = sum(
        industry.required_quantity(runs, qty, 0, mat_mult) * price_of(m)
        for m, qty in formula.items()
    )
    eiv = sum(qty * snap.adjusted(m) for m, qty in formula.items()) * runs
    install = eiv * (
        setting.system_cost_index + setting.tax_rate + settings.industry_scc_surcharge
    )
    per_unrefined = (material + install) / (runs * route.formula.portion_size)
    yield_ = settings.alchemy_reprocess_yield
    credit = sum(qty * yield_ * credit_of(m) for m, qty in route.recovered)
    return (per_unrefined - credit) / (route.composite_qty * yield_)


def _bom_closure(ref, type_ids):
    """The types plus everything their build chains draw, to any depth."""
    seen, stack = set(), list(type_ids)
    while stack:
        t = stack.pop()
        if t in seen:
            continue
        seen.add(t)
        blueprint = ref.blueprint_for_product(t)
        if blueprint is not None:
            stack.extend(m for m, _q in ref.materials(blueprint.blueprint_id, blueprint.activity_id))
    return seen


def _venue_snapshot(ref, *, prices, buy_venue, hub_prices, slots=500):
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=FairValuePrices(ref, overrides=prices),
        adjusted_prices=Overlay(),
        buy_venue=buy_venue,
        hub_prices=hub_prices,
    )


# --- the guards ------------------------------------------------------------------


def test_the_production_blacklist_stops_a_for_sale_route_by_item(conn, ref):
    """Ruling (h), 2026-10-31: a type named under the Production Blacklist
    is bought, never built — so its route gets no for-sale job, whether
    the unrefined product or the composite is the one named. The other
    quoted route keeps its jobs, and the chain itself is untouched by a
    name that is no chain row."""
    prometium, mercurite = _route(ref, "Prometium"), _route(ref, "Promethium Mercurite")
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=100)
    enable_profit(conn)
    snap = snapshot(ref, overrides={prometium.composite_id: 1e6, mercurite.composite_id: 1e6})
    both = engine.plan_index_run(conn, ref, snap, persist=False)
    rows = _for_sale_rows(both)
    assert rows[prometium.composite_id].alchemy_profit_jobs == 100
    assert rows[mercurite.composite_id].alchemy_profit_jobs == 100
    # The unrefined product named: its route is out, the other stays.
    conn.execute("INSERT INTO blacklist_item VALUES (?)", (prometium.unrefined_id,))
    conn.commit()
    assert store.blacklist_items(conn) == {prometium.unrefined_id}
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    rows = _for_sale_rows(plan)
    assert set(rows) == {mercurite.composite_id}
    assert rows[mercurite.composite_id].alchemy_profit_jobs == 100
    assert all(a.alchemy_for_type_id != prometium.composite_id for a in alchemy_items(plan))
    composite, before = plan.items[prometium.composite_id], both.items[prometium.composite_id]
    assert (composite.jobs_allocated, composite.recommended_buy_qty, composite.deficit_qty) == (
        before.jobs_allocated, before.recommended_buy_qty, before.deficit_qty
    )
    # The composite named instead (an off-chain one, so nothing else moves).
    conn.execute("DELETE FROM blacklist_item")
    conn.execute("INSERT INTO blacklist_item VALUES (?)", (mercurite.composite_id,))
    conn.commit()
    rows = _for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))
    assert set(rows) == {prometium.composite_id}
    assert rows[prometium.composite_id].alchemy_profit_jobs == 100


def test_the_production_blacklist_category_stops_every_for_sale_route(conn, ref):
    """The 'Intermediate Reactions' category covers group 428 — every
    composite the routes feed AND every unrefined product — so with it
    checked no route lists a for-sale job, however rich the quotes and
    however free the reaction lines."""
    prometium, mercurite = _route(ref, "Prometium"), _route(ref, "Promethium Mercurite")
    groups = {key: group_ids for key, _label, group_ids in config.BLACKLIST_CATEGORIES}
    for route in (prometium, mercurite):
        assert ref.type_info(route.unrefined_id).group_id in groups["intermediate_reactions"]
        assert ref.type_info(route.composite_id).group_id in groups["intermediate_reactions"]
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=100)
    enable_profit(conn)
    snap = snapshot(ref, overrides={prometium.composite_id: 1e6, mercurite.composite_id: 1e6})
    assert len(_for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))) == 2
    store.set_blacklist_categories(conn, {"intermediate_reactions"})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert _for_sale_rows(plan) == {}
    # The lines were there for the taking: only the blacklist stopped them.
    assert reaction_jobs(plan, "install_jobs") < snap.slots_available[config.ACTIVITY_REACTION]


def test_a_pipelines_own_final_is_never_a_for_sale_candidate(conn, ref):
    """Review 2026-10-31 #18: a composite some pipeline sells as its final
    is sold once, as that final — quoted at a fortune it still gets no
    for-sale alchemy row, while the same quote without the pipeline fills
    the lines."""
    route = _route(ref, "Promethium Mercurite")  # off the Hulk chain
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    snap = snapshot(ref, overrides={route.composite_id: 1e6})
    assert _for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))[
        route.composite_id
    ].alchemy_profit_jobs > 0
    add_pipeline(conn, ref, "Promethium Mercurite", 2000)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert route.composite_id not in _for_sale_rows(plan)
    assert all(a.alchemy_for_type_id != route.composite_id for a in alchemy_items(plan))
    final = plan.items[route.composite_id]
    assert final.requested_qty == 2000 and final.jobs_allocated > 0  # built as the final, directly
    assert final.alchemy_output_qty == 0 and final.recommended_buy_qty == 0


def test_a_deactivated_pipelines_final_is_no_for_sale_candidate_either(conn, ref):
    """The finals the tail guards are EVERY pipeline's, active or not —
    the Ledger's finals set (ledger.finals), whose pipeline books the
    final's sales (review 2026-10-31 #18 / #123). A pipeline selling
    Promethium Mercurite is switched off the way the Pipelines page does
    (is_active 0): its final is no plan row and the chain is the
    control's, yet the route gets no for-sale job, while the other quoted
    route keeps its hundred and the hundred lines the route held stand
    idle. Switched back on, the final is built as the final and still
    gets none."""
    prometium, mercurite = _route(ref, "Prometium"), _route(ref, "Promethium Mercurite")
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=100)
    enable_profit(conn)
    snap = snapshot(ref, overrides={prometium.composite_id: 1e6, mercurite.composite_id: 1e6})
    control = engine.plan_index_run(conn, ref, snap, persist=False)
    rows = _for_sale_rows(control)
    assert rows[prometium.composite_id].alchemy_profit_jobs == 100
    assert rows[mercurite.composite_id].alchemy_profit_jobs == 100
    assert rows[mercurite.composite_id].install_jobs == 100  # every one startable: the lines are held
    pid = add_pipeline(conn, ref, "Promethium Mercurite", 2000)

    def toggle():
        conn.execute(
            "UPDATE pipeline SET is_active = 1 - is_active, modified_at = datetime('now') "
            "WHERE pipeline_id = ?",
            (pid,),
        )
        conn.commit()

    toggle()
    assert conn.execute("SELECT is_active FROM pipeline WHERE pipeline_id = ?", (pid,)).fetchone()[0] == 0
    assert mercurite.composite_id not in {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }
    assert mercurite.composite_id in ledger.finals(conn)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    rows = _for_sale_rows(plan)
    assert set(rows) == {prometium.composite_id}
    assert rows[prometium.composite_id].alchemy_profit_jobs == 100
    assert all(a.alchemy_for_type_id != mercurite.composite_id for a in alchemy_items(plan))
    assert mercurite.composite_id not in plan.items  # no demand of its own: the guard stopped it

    def direct(p):
        return {
            t: i.jobs_allocated for t, i in p.items.items()
            if i.activity_id == config.ACTIVITY_REACTION and i.alchemy_for_type_id is None
        }

    assert direct(plan) == direct(control)
    assert reaction_jobs(plan, "install_jobs") == reaction_jobs(control, "install_jobs") - 100
    toggle()
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert mercurite.composite_id not in _for_sale_rows(plan)
    assert all(a.alchemy_for_type_id != mercurite.composite_id for a in alchemy_items(plan))
    final = plan.items[mercurite.composite_id]
    assert final.requested_qty == 2000 and final.jobs_allocated > 0


def test_a_pipeline_selling_an_unrefined_product_gets_no_for_sale_row(conn, ref):
    """The overwrite guard (review 2026-09-10) with Alchemy for Profit on:
    the unrefined product a pipeline sells is already a demanded plan row,
    so the tail leaves its route alone — the pipeline's row keeps its
    request, its sizing and its attribution, no for-sale row replaces it
    and no hold is written for it; another quoted route still fills the
    lines."""
    prometium, mercurite = _route(ref, "Prometium"), _route(ref, "Promethium Mercurite")
    add_pipeline(conn, ref, "Hulk", 8)
    add_pipeline(conn, ref, "Unrefined Prometium", 500)
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    snap = snapshot(ref, overrides={prometium.composite_id: 1e6, mercurite.composite_id: 1e6})
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    row = plan.items[prometium.unrefined_id]
    assert row.alchemy_for_type_id is None and row.alchemy_profit_jobs == 0
    assert row.requested_qty == 500 and row.cycle_need_qty >= 500
    assert row.recommended_build_qty + row.recommended_buy_qty >= 500
    rows = _for_sale_rows(plan)
    assert prometium.composite_id not in rows
    assert rows[mercurite.composite_id].alchemy_profit_jobs > 0
    pid = conn.execute(
        "SELECT pipeline_id FROM pipeline WHERE final_product_type_id = ?",
        (prometium.unrefined_id,),
    ).fetchone()[0]
    attributed = conn.execute(
        "SELECT a.qty_attributable FROM index_run_item i "
        "JOIN index_run_item_pipeline a USING (index_run_item_id) "
        "WHERE i.index_run_id = ? AND i.type_id = ? AND a.pipeline_id = ?",
        (plan.index_run_id, prometium.unrefined_id, pid),
    ).fetchone()
    assert attributed is not None and attributed["qty_attributable"] >= 500
    assert {r["composite_type_id"] for r in store.hold_rows(conn, plan.index_run_id)} == {
        mercurite.composite_id
    }


def test_no_profitable_route_lists_no_for_sale_row_at_all(conn, ref):
    """At fair value every route's Jita net sits under its cost (checked
    route by route against the formula-sheet figure): the tail adds
    nothing — no for-sale row, no alchemy row at all — and the plan is
    the one without the flag, its free lines left idle."""
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=1000)
    snap = snapshot(ref)
    control = engine.plan_index_run(conn, ref, snap, persist=False)
    settings = store.get_settings(conn)
    landed = lambda t: _landed(ref, settings, snap, t)  # noqa: E731

    def credit_of(m):
        item = control.items.get(m)
        return landed(m) if item is not None and item.merged_min_qty > 0 else net_sale(
            ref, settings, m, snap.price(m)
        )

    # 24 h window, 6 h formulas at Reactions V (-20%): 4.8 h a run, 5 runs a job.
    runs = 5
    for route in ref.alchemy_routes().values():
        cost = _route_cost(ref, settings, snap, route, runs, landed, credit_of)
        assert net_sale(ref, settings, route.composite_id, snap.price(route.composite_id)) < cost
    enable_profit(conn)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert not alchemy_items(plan)
    assert {t: i.jobs_allocated for t, i in plan.items.items()} == {
        t: i.jobs_allocated for t, i in control.items.items()
    }
    assert reaction_jobs(plan, "install_jobs") < snap.slots_available[config.ACTIVITY_REACTION]


# --- ranking, the cap and the re-stamp --------------------------------------------


def test_the_cap_is_met_exactly_and_the_next_route_takes_the_rest(conn, ref):
    """The shared per-type cap binds the winner at exactly the cap — not
    a job over — and the lines it leaves go to the next route."""
    prometium, fernite = _route(ref, "Prometium"), _route(ref, "Fernite Alloy")
    snap, _baseline = _spare_pool_world(
        conn, ref, {prometium.composite_id: 1e6, fernite.composite_id: 1e5}, slots=6, cap=4
    )
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    rows = _for_sale_rows(plan)
    winner, other = rows[prometium.composite_id], rows[fernite.composite_id]

    def per_slot(row, route):
        return (row.alchemy_sale_net_unit - row.alchemy_route_unit_cost) * math.floor(
            row.max_runs_per_job * route.composite_qty * 0.55
        )

    assert per_slot(winner, prometium) > per_slot(other, fernite)
    assert winner.alchemy_profit_jobs == winner.jobs_allocated == 4
    assert winner.jobs_allocated == store.get_settings(conn).max_alchemy_jobs_per_type
    assert other.alchemy_profit_jobs == other.jobs_allocated == 2
    assert reaction_jobs(plan) == 6 == reaction_jobs(plan, "install_jobs")


def test_per_slot_profit_outranks_per_unit_profit(conn, ref):
    """Ranking is profit per SLOT. A 36-unit route earning more per
    composite unit loses to a 73-unit route whose job yields twice the
    units, once the two orders disagree: the capped jobs go to the
    73-unit route."""
    small, large = _route(ref, "Titanium Chromide"), _route(ref, "Promethium Mercurite")
    assert (small.composite_qty, large.composite_qty) == (36, 73)
    snap, _baseline = _spare_pool_world(
        conn, ref, {small.composite_id: 1e6, large.composite_id: 1e6}, slots=4, cap=3
    )
    settings = store.get_settings(conn)
    probe = _for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))
    cost = {c: probe[c].alchemy_route_unit_cost for c in (small.composite_id, large.composite_id)}
    runs = probe[small.composite_id].max_runs_per_job
    assert probe[large.composite_id].max_runs_per_job == runs
    out = {r.composite_id: math.floor(runs * r.composite_qty * 0.55) for r in (small, large)}
    assert out[large.composite_id] > 1.5 * out[small.composite_id]  # 200 vs 99 at 5 runs

    def quote_for(route, margin):
        """The hub price whose net (fees, freight-out) sits `margin` above
        the route's cost — the net is linear in the price."""
        c = route.composite_id
        intercept = net_sale(ref, settings, c, 0.0)
        slope = net_sale(ref, settings, c, 1.0) - intercept
        return (cost[c] + margin - intercept) / slope

    margin = {small.composite_id: 3000.0, large.composite_id: 2000.0}
    assert margin[small.composite_id] > margin[large.composite_id]  # per unit: small wins
    assert margin[large.composite_id] * out[large.composite_id] > (
        margin[small.composite_id] * out[small.composite_id]
    )  # per slot: large wins
    priced = replace(
        snap,
        prices=FairValuePrices(
            ref, overrides={r.composite_id: quote_for(r, margin[r.composite_id]) for r in (small, large)}
        ),
    )
    rows = _for_sale_rows(engine.plan_index_run(conn, ref, priced, persist=False))
    s, l = rows[small.composite_id], rows[large.composite_id]
    for row, route in ((s, small), (l, large)):
        assert row.alchemy_sale_net_unit - row.alchemy_route_unit_cost == pytest.approx(
            margin[route.composite_id]
        )
    assert (l.alchemy_profit_jobs, s.alchemy_profit_jobs) == (3, 1)


def test_the_route_cost_is_re_stamped_at_the_fill_while_the_choice_stands_at_the_quote(conn, ref):
    """Ruling (f), 2026-10-31. A goo the for-sale formula buys sits on a
    thin Jita book — one unit at the quote, the rest a hundred thousand
    times dearer — so Phase 7.5 fill-prices the plan's buy far above the
    Phase 1 quote. The route was chosen and ranked at the quote (the same
    rows, jobs and ranking as with no book, although at the fill it would
    not even pay), but the row's stored cost — the hold's cost of goods,
    on the returned row, the persisted row and the hold alike — is the
    fill-priced LANDED figure; a route not drawing that goo keeps its
    quote figure, and the built fuel block, bought only for its
    shortfall, prices at the quote even with a thin book of its own."""
    prometium, fernite = _route(ref, "Prometium"), _route(ref, "Fernite Alloy")
    chromium, oxygen = ref.type_id("Chromium"), ref.type_id("Oxygen Fuel Block")
    assert chromium in _formula(ref, prometium) and oxygen in _formula(ref, prometium)
    assert chromium not in _formula(ref, fernite)
    # The unstocked chain: the fuel block is BUILT for the stockpile with
    # this cycle's shortfall bought just in time ('both'), and the cap
    # binds the winner so the ranking shows in the job counts. A nominal
    # inbound leg (1 ISK/m³) keeps the landed term visible without
    # pricing the fair-value builds out of the plan.
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=200)
    enable_profit(conn)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 1.0")
    conn.commit()
    settings = store.get_settings(conn)
    snap = snapshot(ref, overrides={prometium.composite_id: 1e6, fernite.composite_id: 1e5})
    control = engine.plan_index_run(conn, ref, snap, persist=False)
    quote_goo, quote_fuel = snap.price(chromium), snap.price(oxygen)
    ladders = {HUB: {
        chromium: [(quote_goo, 1), (quote_goo * 1e5, 10**9)],
        oxygen: [(quote_fuel, 1), (quote_fuel * 3, 10**9)],
    }}
    plan = engine.plan_index_run(conn, ref, replace(snap, sell_ladders=ladders), persist=True)
    before, after = _for_sale_rows(control), _for_sale_rows(plan)
    assert set(before) == set(after) == {prometium.composite_id, fernite.composite_id}
    for c in before:
        assert (after[c].jobs_allocated, after[c].alchemy_profit_jobs, after[c].alchemy_sale_net_unit) == (
            before[c].jobs_allocated, before[c].alchemy_profit_jobs, before[c].alchemy_sale_net_unit
        )
    assert after[prometium.composite_id].alchemy_profit_jobs == 200  # the cap: ranked first
    assert 0 < after[fernite.composite_id].alchemy_profit_jobs < 200  # the lines left
    # The fill the sourcing pass priced the goo at.
    goo = plan.items[chromium]
    bought = goo.recommended_buy_qty
    assert goo.hub_buy_qty == bought > 1 and goo.unfilled_qty == 0 and goo.buy_venue == HUB
    fill_average = (quote_goo + (bought - 1) * quote_goo * 1e5) / bought
    assert goo.hub_fill_price == pytest.approx(fill_average)
    assert goo.price_snapshot == pytest.approx(fill_average)
    # The fuel block: built, its shortfall bought, its own fill above the quote.
    fuel = plan.items[oxygen]
    assert fuel.buildable and fuel.jobs_allocated > 0 and fuel.recommended_action == "both"
    assert fuel.recommended_buy_qty > 1 and fuel.hub_fill_price > quote_fuel
    assert plan.items[oxygen].recommended_action == control.items[oxygen].recommended_action

    def landed(t):
        return _landed(ref, settings, snap, t)

    def filled(t):
        if t == chromium:
            return fill_average + settings.freight_in_isk_per_m3 * ref.type_info(t).freight_volume
        return landed(t)  # the recovered Cadmium (no book) and the built fuel block

    runs = after[prometium.composite_id].max_runs_per_job
    at_quote = _route_cost(ref, settings, snap, prometium, runs, landed)
    at_fill = _route_cost(ref, settings, snap, prometium, runs, filled)
    assert at_fill > at_quote
    assert before[prometium.composite_id].alchemy_route_unit_cost == pytest.approx(at_quote)
    assert after[prometium.composite_id].alchemy_route_unit_cost == pytest.approx(at_fill)
    assert at_fill > after[prometium.composite_id].alchemy_sale_net_unit  # would not pay at the fill
    assert after[fernite.composite_id].alchemy_route_unit_cost == pytest.approx(
        before[fernite.composite_id].alchemy_route_unit_cost
    )
    persisted = conn.execute(
        "SELECT alchemy_route_unit_cost FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, prometium.unrefined_id),
    ).fetchone()[0]
    assert persisted == pytest.approx(at_fill)
    [hold] = [r for r in store.hold_rows(conn, plan.index_run_id) if r["composite_type_id"] == prometium.composite_id]
    assert hold["route_unit_cost"] == pytest.approx(at_fill)


# --- the route's cost and the sale's net -------------------------------------------


def test_recovered_goo_the_chain_buys_is_credited_at_its_landed_price(conn, ref):
    """Promethium Mercurite's reprocess returns Mercury, which the Hulk
    chain buys: the credit is Mercury's LANDED price (it offsets next
    cycle's purchase, freight included) — the row's figure against the
    formula-sheet one, and not what Mercury would fetch sold in Jita."""
    route = _route(ref, "Promethium Mercurite")
    mercury = ref.type_id("Mercury")
    assert route.recovered == ((mercury, 173),)
    add_pipeline(conn, ref, "Hulk", 8)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 900")
    conn.commit()
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    settings = store.get_settings(conn)
    snap = snapshot(ref, overrides={route.composite_id: 1e6})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[mercury].merged_min_qty > 0  # the chain's own demand
    row = _for_sale_rows(plan)[route.composite_id]

    def landed(t):
        return _landed(ref, settings, snap, t)

    expected = _route_cost(ref, settings, snap, route, row.max_runs_per_job, landed)
    assert row.alchemy_route_unit_cost == pytest.approx(expected)
    sold = _route_cost(
        ref, settings, snap, route, row.max_runs_per_job, landed,
        credit_of=lambda t: net_sale(ref, settings, t, snap.price(t)),
    )
    assert row.alchemy_route_unit_cost != pytest.approx(sold)
    # The freight leg is in the credit: 173 x 0.55 units' freight per unrefined unit.
    unfreighted = _route_cost(
        ref, settings, snap, route, row.max_runs_per_job, landed, credit_of=snap.price,
    )
    assert unfreighted - expected == pytest.approx(
        173 * 0.55 * 900 * ref.type_info(mercury).freight_volume / (route.composite_qty * 0.55)
    )


def test_two_routes_sharing_a_stock_limited_fuel_block_split_its_stock(conn, ref):
    """A built fuel block with no market cannot be bought just in time, so
    the blocks on hand are all the tail has: the better route takes its
    cap, the second the jobs the remaining blocks feed — it sees the
    first's debit — and together they never draw past the stock; the
    install check finds the same blocks and starts every job. An
    unpriced input the plan would BUY stops a route (review 2026-10-31
    #27, test_an_unpriced_bought_goo_stops_its_route_until_the_price_is_back);
    the block is exempt because the chain BUILDS it — a build row with no
    price — which is the case this world exercises."""
    first, second = _route(ref, "Promethium Mercurite"), _route(ref, "Neo Mercurite")
    helium = ref.type_id("Helium Fuel Block")
    for route in (first, second):
        assert _formula(ref, route)[helium] == 5
    snap, _baseline = _spare_pool_world(
        conn, ref, {first.composite_id: 1e6, second.composite_id: 9e5, helium: None},
        slots=500, cap=4,
    )
    assert snap.price(helium) is None
    snap.on_hand[helium] = 10**9
    probe = _for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))
    runs = probe[first.composite_id].max_runs_per_job
    assert probe[second.composite_id].max_runs_per_job == runs
    assert probe[first.composite_id].alchemy_profit_jobs == 4  # ample fuel: both at the cap
    assert probe[second.composite_id].alchemy_profit_jobs == 4
    per_job = industry.required_quantity(runs, 5, 0, 1.0)  # 25 blocks a job at 5 runs
    snap.on_hand[helium] = 7 * per_job + 2
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    rows = _for_sale_rows(plan)
    a, b = rows[first.composite_id], rows[second.composite_id]
    assert a.alchemy_profit_jobs == a.jobs_allocated == 4
    assert b.alchemy_profit_jobs == b.jobs_allocated == 3
    drawn = (a.jobs_allocated + b.jobs_allocated) * per_job
    assert drawn <= snap.on_hand[helium] < drawn + per_job
    assert (a.install_jobs, b.install_jobs) == (4, 3)
    assert plan.items[helium].install_short_qty == 0
    assert all(i.install_limited_by != helium for i in alchemy_items(plan))
    # The exemption at work (_route_unit_cost: a buildable row that is no
    # buy): the unpriced block is the chain's to BUILD — here the 177 on
    # hand cover the 175 the jobs draw, so the row has nothing to do this
    # cycle — and neither route was disqualified for it.
    fuel = plan.items[helium]
    assert fuel.buildable and fuel.recommended_action != "buy" and fuel.price_snapshot is None
    assert fuel.install_draw_qty == 7 * per_job and fuel.recommended_buy_qty == 0


def test_the_sale_reference_is_the_hub_quote_never_the_structure_price(conn, ref):
    """The composite's Phase 1 price may be the structure venue's; the
    for-sale judgement nets the Jita quote (the Profit tab's and the
    Ledger's sell reference) — and with no Jita quote the route gets no
    job, however the structure prices it."""
    route = _route(ref, "Promethium Mercurite")
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=1000)
    enable_profit(conn)
    settings = store.get_settings(conn)

    def snap(hub_quote):
        return _venue_snapshot(
            ref, prices={route.composite_id: 2e6}, buy_venue={route.composite_id: STRUCT},
            hub_prices={route.composite_id: hub_quote} if hub_quote else {},
        )

    row = _for_sale_rows(engine.plan_index_run(conn, ref, snap(1e6), persist=False))[route.composite_id]
    assert row.alchemy_sale_net_unit == pytest.approx(net_sale(ref, settings, route.composite_id, 1e6))
    assert row.alchemy_sale_net_unit != pytest.approx(net_sale(ref, settings, route.composite_id, 2e6))
    plan = engine.plan_index_run(conn, ref, snap(None), persist=False)
    assert route.composite_id not in _for_sale_rows(plan)
    assert all(a.alchemy_for_type_id != route.composite_id for a in alchemy_items(plan))


def test_the_sale_net_deducts_fees_and_freight_out_per_cubic_metre(conn, ref):
    """What a unit banks in Jita, written out: the quote less sales tax
    (Accounting V: 7.5% x 0.45), the NPC broker fee (Broker Relations V,
    no standings: 1.5%) and the SCC surcharge, less freight-out on its
    0.2 m³ — and exactly 0.2 x the rate under the no-freight figure."""
    snap, route, settings = for_sale_world(conn, ref)
    assert (settings.skill_accounting, settings.skill_broker_relations) == (5, 5)
    assert (settings.standing_broker_faction, settings.standing_broker_corp) == (0.0, 0.0)
    assert settings.capital_scc_surcharge == 0.015 and settings.freight_out_isk_per_m3 == 0.0
    assert ref.type_info(route.composite_id).freight_volume == 0.2
    free = _for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))[route.composite_id]
    assert free.alchemy_sale_net_unit == pytest.approx(1e6 * (1 - 0.075 * 0.45 - 0.015 - 0.015))
    conn.execute("UPDATE settings SET freight_out_isk_per_m3 = 900")
    conn.commit()
    row = _for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))[route.composite_id]
    assert row.alchemy_sale_net_unit == pytest.approx(1e6 * (1 - 0.075 * 0.45 - 0.015 - 0.015) - 0.2 * 900)
    assert free.alchemy_sale_net_unit - row.alchemy_sale_net_unit == pytest.approx(180.0)
    assert row.alchemy_sale_net_unit == pytest.approx(936_070.0)


def test_the_route_cost_carries_the_reactions_class_material_bonus(conn, ref):
    """The reactions class builds in a Tatara (a time bonus only) with a
    T1 material rig in low-sec: -2.0% x the reaction rig's low-sec band
    (1.0) on every formula input, rounded once per job; the install fee
    takes the structure's cost index and tax. The row's figure against
    the formula-sheet one — and against the same facility without the
    rig, so the bonus is seen to bite."""
    conn.execute(
        "UPDATE class_setting SET structure_type_id = ?, security = 0.3, me_rig = 't1', "
        "te_rig = 'none', system_cost_index = 0.05 WHERE item_class = 'reactions'",
        (config.STRUCTURE_TYPE_TATARA,),
    )
    conn.commit()
    snap, route, settings = for_sale_world(conn, ref)
    setting = store.get_class_settings(conn)["reactions"]
    assert (setting.structure_type_id, setting.me_rig, setting.tax_rate) == (
        config.STRUCTURE_TYPE_TATARA, "t1", 0.0025
    )
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    row = _for_sale_rows(plan)[route.composite_id]

    def landed(t):
        return _landed(ref, settings, snap, t)

    runs = row.max_runs_per_job
    goo = next(m for m, q in _formula(ref, route).items() if q == 100)
    assert industry.required_quantity(runs, 100, 0, 0.98) < 100 * runs, "the bonus shows on the goo"
    expected = _route_cost(ref, settings, snap, route, runs, landed, mat_mult=0.98, setting=setting)
    assert row.alchemy_route_unit_cost == pytest.approx(expected)
    unrigged = _route_cost(ref, settings, snap, route, runs, landed, mat_mult=1.0, setting=setting)
    assert expected < unrigged
    assert row.alchemy_route_unit_cost != pytest.approx(unrigged)
    assert goo in plan.items


# --- an input with no price (review 2026-10-31 #27) ---------------------------------------
#
# A cost that counted an unpriced input at 0 would invent a profit, so a
# route whose formula BUYS a goo with no cached price is skipped until a
# price refresh; an input the chain BUILDS (a fuel block with a build row)
# is exempt and counts 0 unquoted, as it always has. The re-stamp runs the
# same cost with the fills: None re-stamps nothing, the Phase 1 figure
# stands.


def test_an_unpriced_bought_goo_stops_its_route_until_the_price_is_back(conn, ref):
    """Fernite Alloy's formula draws Scandium, which the Hulk chain never
    buys (no plan row — nothing the chain builds either): with Scandium's
    quote gone the route lists no job and no row at all — no just-in-time
    Scandium buy either — while Promethium Mercurite, drawing none of it,
    keeps its three jobs at the cap and the three freed lines stand idle;
    the quote back, Fernite's row returns with the figures it had. A
    BUILT input is exempt:
    test_two_routes_sharing_a_stock_limited_fuel_block_split_its_stock
    runs two routes on an unpriced built fuel block."""
    fernite, mercurite = _route(ref, "Fernite Alloy"), _route(ref, "Promethium Mercurite")
    scandium = ref.type_id("Scandium")
    assert scandium in _formula(ref, fernite) and scandium not in _formula(ref, mercurite)
    assert ref.type_info(scandium).group_id not in config.FUEL_BLOCK_GROUPS
    quotes = {fernite.composite_id: 1e6, mercurite.composite_id: 1e6}
    snap, baseline = _spare_pool_world(conn, ref, quotes, slots=6, cap=3)
    assert scandium not in baseline.items  # the chain neither buys nor builds it
    with_price = engine.plan_index_run(conn, ref, snap, persist=False)
    priced = _for_sale_rows(with_price)
    assert {c: r.alchemy_profit_jobs for c, r in priced.items()} == {
        fernite.composite_id: 3, mercurite.composite_id: 3
    }
    goo = with_price.items[scandium]
    assert not goo.buildable and goo.recommended_buy_qty > 0  # bought just in time for the route
    unpriced = replace(snap, prices=FairValuePrices(ref, overrides={**quotes, scandium: None}))
    assert unpriced.price(scandium) is None
    plan = engine.plan_index_run(conn, ref, unpriced, persist=False)
    rows = _for_sale_rows(plan)
    assert set(rows) == {mercurite.composite_id}
    assert all(a.alchemy_for_type_id != fernite.composite_id for a in alchemy_items(plan))
    assert scandium not in plan.items
    assert reaction_jobs(plan) == 3 < snap.slots_available[config.ACTIVITY_REACTION]
    figures = ("alchemy_profit_jobs", "jobs_allocated", "alchemy_route_unit_cost", "alchemy_sale_net_unit")

    def shape(row):
        return tuple(getattr(row, f) for f in figures)

    assert shape(rows[mercurite.composite_id]) == shape(priced[mercurite.composite_id])
    back = _for_sale_rows(engine.plan_index_run(conn, ref, snap, persist=False))
    assert set(back) == set(priced)
    for c in priced:
        assert shape(back[c]) == shape(priced[c]), ref.type_info(c).name


def test_a_goo_unpriced_at_the_re_stamp_alone_keeps_the_phase_1_figure(conn, ref, monkeypatch):
    """Fernite Alloy at a fortune, its Hydrocarbons on a thin book (one
    unit at the quote, the rest a hundred thousand times dearer) so the
    fill moves the figure, its Scandium stocked so the plan buys none and
    the re-stamp prices it off the snapshot's quote — where a patch
    applied to the re-stamp alone finds no price. The control re-stamps
    the row to the fill-priced figure; the patched run plans the same
    jobs at the same fills (the choice stood at the quote) and keeps the
    Phase 1 quote figure on the row, the persisted row and the hold alike
    — not the fill figure, not a cost with Scandium at 0, not None."""
    fernite = _route(ref, "Fernite Alloy")
    scandium, hydrocarbons = ref.type_id("Scandium"), ref.type_id("Hydrocarbons")
    formula = _formula(ref, fernite)
    assert scandium in formula and hydrocarbons in formula
    assert fernite.recovered == ((scandium, 164),)
    snap, _baseline = _spare_pool_world(conn, ref, {fernite.composite_id: 1e6}, slots=4, cap=4)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 1.0")
    conn.commit()
    settings = store.get_settings(conn)
    snap.on_hand[scandium] = 10**9
    quote = snap.price(hydrocarbons)
    thin = replace(snap, sell_ladders={HUB: {hydrocarbons: [(quote, 1), (quote * 1e5, 10**9)]}})
    control = engine.plan_index_run(conn, ref, thin, persist=False)
    before = _for_sale_rows(control)[fernite.composite_id]
    assert before.alchemy_profit_jobs == before.jobs_allocated == 4
    goo = control.items[hydrocarbons]
    bought = goo.recommended_buy_qty
    assert goo.hub_buy_qty == bought > 1 and goo.unfilled_qty == 0 and goo.buy_venue == HUB
    fill_average = (quote + (bought - 1) * quote * 1e5) / bought
    assert goo.hub_fill_price == pytest.approx(fill_average)
    assert control.items[scandium].recommended_buy_qty == 0  # stocked: no fill to price it at

    def landed(t):
        return _landed(ref, settings, snap, t)

    def filled(t):
        if t == hydrocarbons:
            return fill_average + settings.freight_in_isk_per_m3 * ref.type_info(t).freight_volume
        return landed(t)

    def sold(m):  # the recovered Scandium is nobody's demand: credited at its Jita net
        return net_sale(ref, settings, m, snap.price(m))

    runs = before.max_runs_per_job
    at_quote = _route_cost(ref, settings, snap, fernite, runs, landed, sold)
    at_fill = _route_cost(ref, settings, snap, fernite, runs, filled, sold)
    at_zero = _route_cost(
        ref, settings, snap, fernite, runs, lambda t: 0.0 if t == scandium else filled(t), sold
    )
    assert at_quote < at_fill and at_zero < at_fill
    assert at_quote != pytest.approx(at_zero)
    assert before.alchemy_route_unit_cost == pytest.approx(at_fill)  # the control re-stamped to the fill
    real_restamp, real_landed = engine._restamp_route_costs, engine._landed_price

    def landed_without_scandium(ref_, settings_, snapshot_, type_id):
        return None if type_id == scandium else real_landed(ref_, settings_, snapshot_, type_id)

    def restamp_with_scandium_unpriced(conn_, ref_, merged, snapshot_):
        engine._landed_price = landed_without_scandium
        try:
            real_restamp(conn_, ref_, merged, snapshot_)
        finally:
            engine._landed_price = real_landed

    monkeypatch.setattr(engine, "_landed_price", real_landed)  # restored at teardown whatever happens
    monkeypatch.setattr(engine, "_restamp_route_costs", restamp_with_scandium_unpriced)
    plan = engine.plan_index_run(conn, ref, thin, persist=True)
    after = _for_sale_rows(plan)[fernite.composite_id]
    assert (after.jobs_allocated, after.alchemy_profit_jobs, after.alchemy_sale_net_unit) == (
        before.jobs_allocated, before.alchemy_profit_jobs, before.alchemy_sale_net_unit
    )
    assert plan.items[hydrocarbons].hub_fill_price == pytest.approx(fill_average)  # the same fills
    assert after.alchemy_route_unit_cost is not None
    assert after.alchemy_route_unit_cost == pytest.approx(at_quote)
    assert after.alchemy_route_unit_cost != pytest.approx(at_fill)
    assert after.alchemy_route_unit_cost != pytest.approx(at_zero)
    persisted = conn.execute(
        "SELECT alchemy_route_unit_cost FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, fernite.unrefined_id),
    ).fetchone()[0]
    assert persisted == pytest.approx(at_quote)
    [hold] = [
        r for r in store.hold_rows(conn, plan.index_run_id) if r["composite_type_id"] == fernite.composite_id
    ]
    assert hold["route_unit_cost"] == pytest.approx(at_quote)


# --- what the tail leaves alone --------------------------------------------------------


def test_demand_type_ids_cover_every_routes_inputs_and_recovered_goo_with_profit_on(conn, ref):
    """The cache-read price set with the flag on holds, for every unblocked
    route, the composite (its Jita quote is the sale reference), the
    unrefined product, the formula's inputs and the recovered goo; a
    savings-only world leaves an off-chain route's own goo out, and so
    does the alchemy blacklist."""
    tungsten = _route(ref, "Rolled Tungsten Alloy")  # off the chain, with goo of its own
    own = {tungsten.composite_id, tungsten.unrefined_id, ref.type_id("Tungsten")}
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn)
    assert not own & engine.demand_type_ids(conn, ref)
    enable_profit(conn)
    ids = engine.demand_type_ids(conn, ref)
    for route in ref.alchemy_routes().values():
        want = {route.composite_id, route.unrefined_id}
        want.update(m for m, _q in route.recovered)
        want.update(_formula(ref, route))
        assert want <= ids, ref.type_info(route.composite_id).name
    assert engine.market_type_ids(conn, ref) <= ids
    store.set_alchemy_blacklist(conn, {tungsten.composite_id})
    conn.commit()
    assert not own & engine.demand_type_ids(conn, ref)


def test_price_ids_keep_a_held_composite_whatever_the_flags_say(conn, ref):
    """Review 2026-10-31 #16: the Profit tab and the Ledger price a hold's
    unsold units, its by-hand marks without a price and its observed
    releases the settlement could not price at TODAY's quote, so the
    order-book refresh keeps pulling the composite while a hold still
    values it — live, or holding such units — whatever Alchemy for
    Profit, Never run or Enable Alchemy say now; an expired hold with
    every unit sold at a price adds nothing, and nothing else of the
    route is added for a hold. Promethium Mercurite is off the Hulk
    chain, so with Alchemy for Profit off it is nobody's demand: the hold
    alone decides."""
    from test_alchemy_hold import T0, complete_run, deliver_jobs, install_jobs, seed_hold, seed_run

    route = _route(ref, "Promethium Mercurite")
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn)
    settings = store.get_settings(conn)

    def ids():
        return engine.market_type_ids(conn, ref)

    assert route.composite_id not in ids() and route.unrefined_id not in ids()
    run1 = seed_run(conn, 1, T0)
    seed_hold(conn, run1, route.composite_id, route_unit_cost=50.0)
    _start, end = install_jobs(conn, run1, route.unrefined_id, 2, 100)
    [hold] = holds.live(conn, ref, settings)
    assert hold.composite_type_id == route.composite_id and hold.in_flight
    pile = math.floor(200 * route.composite_qty * 0.55)
    assert hold.for_sale_qty == pile == 8030
    # (a) Alchemy for Profit off, (b) the route under Never run, (c) alchemy off:
    # the composite is in the set each time, the rest of the route never.
    assert route.composite_id in ids() and route.unrefined_id not in ids()
    store.set_alchemy_blacklist(conn, {route.composite_id})
    conn.commit()
    assert route.composite_id in ids() and route.unrefined_id not in ids()
    conn.execute("UPDATE settings SET alchemy_enabled = 0")
    conn.commit()
    assert route.composite_id in ids() and route.unrefined_id not in ids()
    assert route.composite_id in engine.demand_type_ids(conn, ref)  # the cache-read list too
    conn.execute("UPDATE settings SET alchemy_enabled = 1")
    store.set_alchemy_blacklist(conn, set())
    conn.commit()
    # Delivered, then the second Mark executed at or after the delivery: expired.
    delivered = end + timedelta(days=1)
    deliver_jobs(conn, [90000, 90001], delivered)
    c1 = delivered + timedelta(days=1)
    complete_run(conn, run1, c1)
    store.save_industry_jobs(conn, [], pulled_at=c1 + timedelta(hours=1))
    seed_run(conn, 2, c1, status="complete", completed_at=c1 + timedelta(days=30))
    [hold] = holds.load(conn, ref, settings)
    assert not hold.live and hold.status == "returned to stock" and hold.remaining == pile
    assert route.composite_id not in ids()
    # Every unit sold at a price (an observed market sale): nothing left to price.
    store.add_alchemy_release(
        conn, run1, route.composite_id, pile, "market", ref_id=9001, unit_price=30_000.0, unit_cost=50.0
    )
    conn.commit()
    [hold] = holds.load(conn, ref, settings)
    assert hold.status == "sold" and hold.remaining == 0 and hold.observed_qty == pile
    assert route.composite_id not in ids()
    # An item-exchange contract the settlement could not price: valued at
    # today's quote, expired or not.
    store.add_alchemy_release(
        conn, run1, route.composite_id, 5, "contract", ref_id=5001, unit_price=None, unit_cost=50.0
    )
    conn.commit()
    assert route.composite_id in ids() and route.unrefined_id not in ids()
    conn.execute("DELETE FROM alchemy_hold_release WHERE source = 'contract'")
    conn.commit()
    assert route.composite_id not in ids()
    # A by-hand mark without a price: in; the same mark priced: out.
    store.add_alchemy_release(conn, run1, route.composite_id, 5, "manual", unit_price=None, unit_cost=50.0)
    conn.commit()
    [hold] = holds.load(conn, ref, settings)
    assert hold.manual_unpriced_qty == 5 and not hold.live
    assert route.composite_id in ids()
    conn.execute("UPDATE alchemy_hold_release SET unit_price = 30000.0 WHERE source = 'manual'")
    conn.commit()
    [hold] = holds.load(conn, ref, settings)
    assert hold.manual_unpriced_qty == 0 and hold.manual_priced_qty == 5
    assert route.composite_id not in ids()


def test_profit_on_moves_only_the_routes_own_inputs(conn, ref):
    """Beside the chain the tail changes nothing it does not draw: every
    row outside the for-sale formula's build closure keeps its target and
    its buy with Alchemy for Profit on, no row's price moves (the same
    ladders — here none), the goo the chain and the formula share keeps
    its quote while its buy grows by the just-in-time purchase, and the
    rows whose targets do move are the formula's inputs and what the
    extra fuel blocks draw."""
    snap, route, _settings = for_sale_world(conn, ref, profit=False)
    off = engine.plan_index_run(conn, ref, snap, persist=False)
    enable_profit(conn)
    on = engine.plan_index_run(conn, ref, snap, persist=False)
    row = _for_sale_rows(on)[route.composite_id]
    assert row.alchemy_profit_jobs == row.jobs_allocated > 0
    formula = list(_formula(ref, route))
    touched = _bom_closure(ref, formula)
    assert set(on.items) - set(off.items) == {route.unrefined_id}
    moved = {
        t for t, before in off.items.items()
        if before.target_stock_qty != on.items[t].target_stock_qty
    }
    assert moved and moved <= touched
    for t, before in off.items.items():
        after = on.items[t]
        assert after.price_snapshot == before.price_snapshot, after.name
        if t not in touched:
            assert after.target_stock_qty == before.target_stock_qty, after.name
            assert after.recommended_buy_qty == before.recommended_buy_qty, after.name
    shared = [m for m in formula if off.items[m].merged_min_qty > 0 and not off.items[m].buildable]
    assert shared, "the formula draws goo the chain buys too"
    for m in shared:
        assert on.items[m].price_snapshot == off.items[m].price_snapshot
        assert on.items[m].recommended_buy_qty > off.items[m].recommended_buy_qty
    composite = on.items[route.composite_id]
    assert composite.alchemy_output_qty == 0 and composite.held_for_sale_qty is None


def test_a_bought_composite_keeps_its_purchase_beside_its_for_sale_jobs(conn, ref):
    """A routed composite the plan BUYS outright (its structure price
    under the build cost) while Jita quotes it at a fortune: the for-sale
    jobs run on the Jita figure and the purchase is exactly the plan's
    without the flag — nothing credited, nothing shrunk, no savings
    move."""
    route = _route(ref, "Prometium")
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn, cap=1000)
    probe = engine.plan_index_run(conn, ref, snapshot(ref), persist=False).items[route.composite_id]
    assert probe.jobs_allocated > 0 and probe.direct_unit_cost > 0
    low = probe.direct_unit_cost * 0.5

    def snap():
        return _venue_snapshot(
            ref, prices={route.composite_id: low}, buy_venue={route.composite_id: STRUCT},
            hub_prices={route.composite_id: 1e6},
        )

    control = engine.plan_index_run(conn, ref, snap(), persist=False)
    b = control.items[route.composite_id]
    assert b.jobs_allocated == 0 and b.recommended_action == "buy" and b.recommended_buy_qty > 0
    assert not alchemy_items(control)  # the structure price beats the route: no savings move
    enable_profit(conn)
    plan = engine.plan_index_run(conn, ref, snap(), persist=False)
    row = _for_sale_rows(plan)[route.composite_id]
    assert row.alchemy_profit_jobs == row.jobs_allocated > 0
    a = plan.items[route.composite_id]
    assert (a.recommended_buy_qty, a.market_buy_qty, a.recommended_action, a.jobs_allocated) == (
        b.recommended_buy_qty, b.market_buy_qty, "buy", 0
    )
    assert a.alchemy_buy_qty == 0 and a.alchemy_output_qty == 0
    assert a.buy_venue == STRUCT and a.price_snapshot == low
    assert a.deficit_qty == b.deficit_qty
