"""The install check (engine Phase 7.6, v1.27.1): can this cycle's planned
jobs be installed from stock on hand, in-flight output and this cycle's
buys — and when not, finals take scarce inputs by return on cost and
intermediates share them in proportion.

Same fixture pattern as test_engine: state in a temp database, reference
data from the real imported SDE.
"""

import math
import sqlite3

import pytest

from magoo import config, costing, engine, store
from magoo.engine import Snapshot
from conftest import FairValuePrices

# (costing.hull_cost / ledger.cost_bases are exercised below for the
# hulls-started rule of v1.27.1.)


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


def rich_snapshot(ref, slots=500, overrides=None):
    prices = FairValuePrices(ref, overrides=overrides)
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=prices,
        adjusted_prices=prices,
    )


def _consumers(plan):
    return [i for i in plan.items.values() if i.runs_allocated > 0]


def _available(item):
    return (
        item.on_hand_qty
        + item.in_progress_qty
        + item.recommended_buy_qty
        + item.compressed_covered_qty
    )


def _per_job(item) -> int:
    return math.ceil(item.runs_allocated / item.jobs_allocated)


def _install_draw(conn, ref, plan) -> dict[int, int]:
    """What the INSTALL figures draw, per material, at the packing they
    describe (engine.install_draw_of) — the figure the check promises
    fits."""
    draw_of = engine._draw_calculator(conn, ref)
    total: dict[int, int] = {}
    for item in _consumers(plan):
        for m, qty in engine.install_draw_of(draw_of, item).items():
            total[m] = total.get(m, 0) + qty
    return total


def _saturating_reaction(ref, item) -> bool:
    return (
        item.activity_id == config.ACTIVITY_REACTION
        and ref.type_info(item.type_id).group_id
        not in config.NON_SATURATING_REACTION_GROUPS
    )


# --- the check itself --------------------------------------------------------


def test_empty_stock_installs_nothing_above_the_raw_tier(conn, ref):
    """From empty, every stage that eats a BUILT input has nothing to
    install: the plan builds the whole chain, but this cycle's build
    output delivers next cycle. Raw inputs are bought just-in-time, so
    the stages that eat only raws (and fuel blocks' minerals) install in
    full, and no raw is ever short."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.jobs_allocated == 8
    assert hulk.install_runs == 0 and hulk.install_jobs == 0
    assert hulk.install_priority == 1
    assert hulk.install_limited_by is not None
    assert plan.items[hulk.install_limited_by].buildable
    consumers = _consumers(plan)
    assert all(i.install_runs is not None for i in consumers)
    full = [i for i in consumers if i.install_runs == i.runs_allocated]
    assert full, "the raw-fed tier installs"
    for item in full:
        assert item.install_limited_by is None
        for m, _q in ref.materials(item.blueprint_id, item.activity_id):
            assert not plan.items[m].buildable or plan.items[m].install_short_qty == 0
    for item in plan.items.values():
        if not item.buildable and item.install_draw_qty is not None:
            assert item.install_short_qty == 0, item.name
        if item.runs_allocated <= 0:
            assert item.install_runs is None and item.install_jobs is None
            assert item.install_priority is None
    # The short list is the built inputs whose draw exceeds availability.
    short = [i for i in plan.items.values() if i.install_short_qty]
    assert short
    for item in short:
        assert item.buildable
        assert item.install_draw_qty - _available(item) == item.install_short_qty


def test_stock_covering_the_draw_installs_everything(conn, ref):
    """Seed every input at what the empty-stock plan's jobs draw; the
    re-plan builds no more than that (stock only shrinks deficits), so
    every planned run installs and nothing is short."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    snap = rich_snapshot(ref)
    for type_id, qty in draw.items():
        snap.on_hand[type_id] = qty
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    consumers = _consumers(plan)
    assert consumers
    for item in consumers:
        assert item.install_runs == item.runs_allocated, item.name
        assert item.install_jobs == item.jobs_allocated, item.name
        assert item.install_limited_by is None, item.name
    assert not any(i.install_short_qty for i in plan.items.values())
    assert plan.items[ref.type_id("Hulk")].install_priority == 1


def test_installable_draw_never_exceeds_availability(conn, ref):
    """The promise behind the figure, at the game's per-job rounding:
    what the installable runs draw fits the stock on hand, in-flight
    output and this cycle's buys — for every material, in a mixed
    partly-stocked world."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    snap = rich_snapshot(ref)
    for n, (type_id, qty) in enumerate(sorted(draw.items())):
        # A spread of coverage: none, a third, two thirds, all.
        snap.on_hand[type_id] = qty * (n % 4) // 3
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    installed = _install_draw(conn, ref, plan)
    for m, qty in installed.items():
        assert qty <= _available(plan.items[m]), plan.items[m].name
    for item in _consumers(plan):
        assert 0 <= item.install_runs <= item.runs_allocated
        per_job = math.ceil(item.runs_allocated / item.jobs_allocated)
        assert item.install_jobs == (
            math.ceil(item.install_runs / per_job) if item.install_runs else 0
        )
        if item.install_runs == item.runs_allocated:
            assert item.install_jobs == item.jobs_allocated
            assert item.install_limited_by is None
        else:
            assert item.install_limited_by is not None
            assert plan.items[item.install_limited_by].install_short_qty > 0


def test_draw_figures_match_planned_consumption(conn, ref):
    """install_draw_qty on a consumed row is exactly what
    _planned_consumption charges the material — one material walk
    behind both (the shared _draw_calculator)."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, plan.items)
    for type_id, qty in draw.items():
        assert plan.items[type_id].install_draw_qty == qty
    for item in plan.items.values():
        if item.type_id not in draw:
            assert item.install_draw_qty is None


# --- finals: highest return first -------------------------------------------


def _shared_component(ref, a: str, b: str) -> int:
    """A BUILT input both finals' blueprints consume."""
    inputs = []
    for name in (a, b):
        bp = ref.blueprint_for_product(ref.type_id(name))
        inputs.append(
            {
                m
                for m, _q in ref.materials(bp.blueprint_id, bp.activity_id)
                if ref.blueprint_for_product(m) is not None
            }
        )
    shared = inputs[0] & inputs[1]
    assert shared, f"{a} and {b} share no built input"
    return min(shared)


def _two_finals(conn, ref, rich: str, poor: str, overrides=None):
    """Two pipelines whose finals share a built component; the shared
    component's stock covers exactly the RICH final's cycle draw and every
    other built input is plentiful. Returns (plan, shared component id)."""
    add_pipeline(conn, ref, rich, 6)
    add_pipeline(conn, ref, poor, 6)
    shared = _shared_component(ref, rich, poor)
    empty = engine.plan_index_run(
        conn, ref, rich_snapshot(ref, overrides=overrides), persist=False
    )
    draw_of = engine._draw_calculator(conn, ref)
    rich_item = empty.items[ref.type_id(rich)]
    rich_draw = draw_of(
        rich_item, rich_item.runs_allocated, rich_item.jobs_allocated
    )[shared]
    snap = rich_snapshot(ref, overrides=overrides)
    for item in empty.items.values():
        if item.buildable and item.type_id not in (
            ref.type_id(rich), ref.type_id(poor),
        ):
            snap.on_hand[item.type_id] = 10**9
    snap.on_hand[shared] = rich_draw
    return engine.plan_index_run(conn, ref, snap, persist=False), shared


@pytest.mark.parametrize("rich,poor", [("Skiff", "Hulk"), ("Hulk", "Skiff")])
def test_the_higher_return_final_takes_the_scarce_component(conn, ref, rich, poor):
    """Two finals share a component whose stock feeds only one of them:
    the final with the higher return on cost (net proceeds − chain cost,
    over chain cost) installs in full, the other waits — whichever way
    the prices point."""
    rich_id, poor_id = ref.type_id(rich), ref.type_id(poor)
    base = FairValuePrices(ref)
    overrides = {rich_id: base.get(rich_id) * 3, poor_id: base.get(poor_id) * 1.1}
    plan, shared = _two_finals(conn, ref, rich, poor, overrides)
    winner, loser = plan.items[rich_id], plan.items[poor_id]
    assert winner.install_return > loser.install_return
    assert winner.install_priority == 1 and loser.install_priority == 2
    assert winner.install_runs == winner.runs_allocated > 0
    assert winner.install_limited_by is None
    assert loser.install_runs < loser.runs_allocated
    assert loser.install_limited_by == shared
    assert plan.items[shared].install_short_qty > 0
    assert shared in [
        m for m, _q in ref.materials(loser.blueprint_id, loser.activity_id)
    ]


def test_unsourced_buy_units_are_not_available_to_install_with(conn, ref):
    """Review 2026-09-09: units of a buy no stored sell order held (the
    'N unsourced' badge, unfilled_qty) cannot be bought, so they are not
    there to install with — a consumer whose raw is wholly unsourced
    installs nothing, and the raw is short by that many units."""
    add_pipeline(conn, ref, "Hulk", 8)
    snap = rich_snapshot(ref)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    trit = plan.items[ref.type_id("Tritanium")]
    assert trit.recommended_buy_qty > 0 and trit.install_short_qty == 0
    eaters = [
        i for i in _consumers(plan)
        if ref.type_id("Tritanium") in dict(ref.materials(i.blueprint_id, i.activity_id))
        and i.install_runs == i.runs_allocated
    ]
    assert eaters
    # The whole buy unsourced: re-run the check over the same plan.
    trit.unfilled_qty = trit.recommended_buy_qty
    engine._install_check(conn, ref, plan.items, snap)
    # Nothing on hand and nothing buyable: short by the whole draw.
    assert trit.install_short_qty == trit.install_draw_qty > 0
    for i in eaters:
        assert i.install_runs == 0 and i.install_limited_by == trit.type_id, i.name
    # Half unsourced: the check installs what the sourced half feeds.
    trit.unfilled_qty = trit.recommended_buy_qty // 2
    engine._install_check(conn, ref, plan.items, snap)
    assert 0 < trit.install_short_qty < trit.install_draw_qty
    assert any(0 < i.install_runs for i in eaters)
    assert _install_draw(conn, ref, plan)[trit.type_id] <= (
        trit.on_hand_qty + trit.in_progress_qty + trit.recommended_buy_qty
        - trit.unfilled_qty + trit.compressed_covered_qty
    )


def test_finals_with_unpriced_inputs_rank_after_fully_priced_ones(conn, ref):
    """Review 2026-09-09: a chain cost with inputs priced at 0 (the row's
    'N unpriced' badge) understates, so its return reads high — such a
    final ranks after every fully priced one, whatever its figure."""
    add_pipeline(conn, ref, "Skiff", 6)
    add_pipeline(conn, ref, "Hulk", 6)
    skiff, hulk = ref.type_id("Skiff"), ref.type_id("Hulk")
    base = FairValuePrices(ref)
    snap = rich_snapshot(ref, overrides={skiff: base.get(skiff) * 3})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    s, h = plan.items[skiff], plan.items[hulk]
    assert s.install_return > h.install_return
    assert s.install_priority == 1 and h.install_priority == 2
    assert s.savings_unpriced_inputs == 0 and h.savings_unpriced_inputs == 0
    s.savings_unpriced_inputs = 1
    engine._install_check(conn, ref, plan.items, snap)
    assert s.install_return > h.install_return  # the figure itself stands
    assert h.install_priority == 1 and s.install_priority == 2


def test_final_return_is_the_profit_views_margin_over_chain_cost(conn, ref):
    """The ranking figure is net proceeds after sell-side fees minus the
    integrated chain cost, over that chain cost — the same net the
    negative-margin badge and the Profit views use; an unpriced final
    ranks after every priced one."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    settings = store.get_settings(conn)
    hulk = plan.items[ref.type_id("Hulk")]
    net = costing.net_proceeds_per_hull(
        hulk.price_snapshot, ref.type_info(hulk.type_id).freight_volume,
        settings, capital=False, freight_exempt=False,
    )
    expected = (net - hulk.unit_chain_cost) / hulk.unit_chain_cost
    assert hulk.install_return == pytest.approx(expected)
    assert engine.final_return(
        ref, settings, hulk.type_id, hulk.price_snapshot, hulk.unit_chain_cost
    ) == pytest.approx(expected)
    assert engine.final_return(ref, settings, hulk.type_id, None, 1.0) is None
    assert engine.final_return(ref, settings, hulk.type_id, 1.0, 0.0) is None


def test_capital_final_ranks_on_its_structure_sell_quote(conn, ref):
    """A capital hull has no Jita quote (price_snapshot None), so its
    chain cost is stamped regardless and its return comes from the
    snapshot's sell quote — the capital structure's SELL price the run
    route passes (ledger.final_quote), with the capital fee pair and the
    flat movement cost. Without a sell quote it is unpriced and ranks
    last; with a rich one it outranks the sub-capital."""
    add_pipeline(conn, ref, "Hulk", 8)
    add_pipeline(conn, ref, "Archon", 1)
    archon, hulk = ref.type_id("Archon"), ref.type_id("Hulk")
    snap = rich_snapshot(ref, overrides={archon: None})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    a = plan.items[archon]
    assert a.price_snapshot is None and a.unit_chain_cost is not None
    assert a.install_return is None and a.install_priority == 2
    assert plan.items[hulk].install_priority == 1
    # Now with the structure's sell quote: a hull selling at twenty times
    # its chain cost (fair-value prices give the Hulk a few hundred
    # percent itself, since its chain builds what the market marks up).
    snap = rich_snapshot(ref, overrides={archon: None})
    snap.sell_quotes[archon] = a.unit_chain_cost * 20
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    a = plan.items[archon]
    settings = store.get_settings(conn)
    net = costing.net_proceeds_per_hull(
        a.unit_chain_cost * 20, ref.type_info(archon).freight_volume, settings,
        capital=True, freight_exempt=False,
    )
    assert a.install_return == pytest.approx(
        (net - a.unit_chain_cost) / a.unit_chain_cost
    )
    assert a.install_return > plan.items[hulk].install_return
    assert a.install_priority == 1 and plan.items[hulk].install_priority == 2
    assert a.price_snapshot is None  # the sell reference never becomes a buy price
    row = conn.execute(
        "SELECT install_return, install_priority, price_snapshot "
        "FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, archon),
    ).fetchone()
    assert row["install_return"] == pytest.approx(a.install_return)
    assert row["install_priority"] == 1 and row["price_snapshot"] is None


def test_executed_run_profit_counts_the_hulls_the_check_started(conn, ref):
    """The run's Profit tab (costing.hull_cost) counts the hulls the cycle
    STARTED — the install check's figure — as Units and in the cycle
    totals, keeps the per-hull cost on the plan's attribution, and the
    Ledger's cost basis stays the latest executed run even when it
    started none (user ruling 2026-09-09: the basis is a per-hull cost)."""
    from magoo import ledger

    add_pipeline(conn, ref, "Hulk", 8)
    pid = conn.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    settings = store.get_settings(conn)

    def complete(plan):
        conn.execute(
            "UPDATE index_run SET status = 'complete', completed_at = datetime('now') "
            "WHERE index_run_id = ?", (plan.index_run_id,),
        )
        conn.commit()

    # Run 1: stock feeds every planned hull.
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    full = rich_snapshot(ref)
    for t, q in draw.items():
        full.on_hand[t] = q
    plan1 = engine.plan_index_run(conn, ref, full, persist=True)
    complete(plan1)
    cost1 = costing.hull_cost(conn, ref, settings, plan1.index_run_id, pid)
    assert cost1.hulls_planned == 8 and cost1.hulls_per_cycle == 8
    # Run 2: empty stock — the plan wants 8, none can start.
    plan2 = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=True)
    complete(plan2)
    cost2 = costing.hull_cost(conn, ref, settings, plan2.index_run_id, pid)
    assert cost2.hulls_planned == 8 and cost2.hulls_per_cycle == 0
    assert cost2.total > 0  # per-hull cost stands
    totals = costing.cycle_totals([{"cost": cost2, "net": 1.0, "margin": 0.5}])
    assert totals.hulls == 0 and totals.profit == 0
    # The Ledger's cost basis is still the latest executed run — a
    # per-hull cost needs no hull started (user ruling 2026-09-09).
    hulk = ref.type_id("Hulk")
    bases = ledger.cost_bases(
        conn, ref, settings,
        {hulk: [conn.execute("SELECT * FROM pipeline").fetchone()]},
    )
    assert bases[hulk].index_run_id == plan2.index_run_id
    assert bases[hulk].cost.total == pytest.approx(cost2.total)
    # Run 3: half the Hulk's components — the started share follows.
    half = rich_snapshot(ref)
    for t, q in draw.items():
        half.on_hand[t] = q // 2
    plan3 = engine.plan_index_run(conn, ref, half, persist=True)
    complete(plan3)
    h3 = plan3.items[hulk]
    assert 0 < h3.install_runs < h3.runs_allocated
    cost3 = costing.hull_cost(conn, ref, settings, plan3.index_run_id, pid)
    assert cost3.hulls_per_cycle == h3.install_runs * h3.portion_size
    assert cost3.hulls_planned == 8
    bases = ledger.cost_bases(
        conn, ref, settings,
        {hulk: [conn.execute("SELECT * FROM pipeline").fetchone()]},
    )
    assert bases[hulk].index_run_id == plan3.index_run_id


def test_run_route_hands_the_snapshot_every_finals_sell_quote(seeded_client, ref, monkeypatch):
    """POST /run passes {final: ledger.final_quote price} as the
    snapshot's sell quotes — the Hulk's cached hub quote here — so the
    persisted return matches the Planning tab's sell reference."""
    from magoo import engine as engine_mod

    seen = {}
    real = engine_mod.plan_index_run

    def spy(conn, ref_, snapshot, *args, **kwargs):
        seen["sell_quotes"] = dict(snapshot.sell_quotes)
        return real(conn, ref_, snapshot, *args, **kwargs)

    monkeypatch.setattr(engine_mod, "plan_index_run", spy)
    run_id = _plan(seeded_client)
    hulk = ref.type_id("Hulk")
    assert set(seen["sell_quotes"]) == {hulk}
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT install_return, price_snapshot FROM index_run_item "
        "WHERE index_run_id = ? AND type_id = ?", (run_id, hulk),
    ).fetchone()
    c.close()
    assert seen["sell_quotes"][hulk] == pytest.approx(row["price_snapshot"])
    assert row["install_return"] is not None


# --- intermediates: the same share of their plan ----------------------------


def test_a_final_the_plan_gave_no_jobs_starts_no_hull(conn, ref):
    """Review 2026-09-10: `install_runs` is NULL both on a run planned
    before the check AND on any row holding no jobs, so a final the
    allocator could not seat reported a whole cycle started — Units, the
    cycle totals and the Profit tab all counted hulls that were never
    even planned. The plan's build is the fallback for both cases, so a
    jobless final plans 0 and starts 0, and nothing badges `short`."""
    add_pipeline(conn, ref, "Hulk", 8)
    add_pipeline(conn, ref, "Mackinaw", 8)
    settings = store.get_settings(conn)
    # Two slots for two pipelines: one final gets jobs, the other none.
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref, slots=2), persist=True)
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = datetime('now') "
        "WHERE index_run_id = ?", (plan.index_run_id,),
    )
    conn.commit()
    jobless = [
        p for p in store.active_pipelines(conn)
        if plan.items[p["final_product_type_id"]].jobs_allocated == 0
    ]
    assert jobless, "the 2-slot pool starves at least one final"
    for pipeline in jobless:
        cost = costing.hull_cost(
            conn, ref, settings, plan.index_run_id, pipeline["pipeline_id"]
        )
        assert cost.hulls_per_cycle == 0, pipeline["name"]
        assert cost.hulls_planned == 0, pipeline["name"]
        assert cost.total > 0  # the per-hull cost (the Ledger basis) stands
        totals = costing.cycle_totals([{"cost": cost, "net": 1.0, "margin": 0.5}])
        assert totals.hulls == 0 and totals.cost == 0


def test_a_slot_limited_plan_is_not_short_on_stock(conn, ref):
    """Review 2026-09-10: `hulls_planned` was the pipeline's cycle DEMAND,
    so a plan the slot pool sized below that demand badged `short on
    stock` although the install check cut nothing. The plan's own count
    is what it BUILDS, so the badge fires only on a real stock shortage."""
    add_pipeline(conn, ref, "Hulk", 8)
    pid = conn.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    settings = store.get_settings(conn)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    hulk = ref.type_id("Hulk")
    # Stock every input, then squeeze the manufacturing pool so the plan
    # itself sizes fewer hull runs than the cycle demands.
    draw = engine._planned_consumption(conn, ref, empty.items)
    snap = rich_snapshot(ref)
    for t, q in draw.items():
        snap.on_hand[t] = q
    snap.slots_available[config.ACTIVITY_MANUFACTURING] = 1
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = datetime('now') "
        "WHERE index_run_id = ?", (plan.index_run_id,),
    )
    conn.commit()
    item = plan.items[hulk]
    assert item.jobs_allocated > 0
    assert item.recommended_build_qty < item.cycle_need_qty  # the pool cut the plan
    assert item.install_runs == item.runs_allocated  # the check cut nothing
    cost = costing.hull_cost(conn, ref, settings, plan.index_run_id, pid)
    assert cost.hulls_per_cycle == item.recommended_build_qty
    assert cost.hulls_planned == cost.hulls_per_cycle  # no `short` badge


def test_a_shared_finals_share_is_measured_on_the_build(conn, ref):
    """Review 2026-09-10: the pro-rata share divided a BUILD quantity by
    the cycle DEMAND, so any stock of a dual-role final (which shrinks
    the build below the demand) collapsed the share and badged `short`
    although every planned run installed. Both counts are now measured on
    the build, so a run that installs everything it planned never badges,
    and the shares never exceed the hulls the run started."""
    add_pipeline(conn, ref, "Capital Drone Bay", 2)
    add_pipeline(conn, ref, "Archon", 3)
    settings = store.get_settings(conn)
    bay = ref.type_id("Capital Drone Bay")
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    snap = rich_snapshot(ref)
    for t, q in draw.items():
        snap.on_hand[t] = q
    snap.on_hand[bay] = 20  # stock of the dual-role final shrinks its build
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = datetime('now') "
        "WHERE index_run_id = ?", (plan.index_run_id,),
    )
    conn.commit()
    item = plan.items[bay]
    assert item.install_runs == item.runs_allocated  # every planned run installs
    assert item.recommended_build_qty < item.cycle_need_qty  # the stock shrank it
    bay_pid = conn.execute(
        "SELECT pipeline_id FROM pipeline WHERE final_product_type_id = ?", (bay,)
    ).fetchone()[0]
    cost = costing.hull_cost(conn, ref, settings, plan.index_run_id, bay_pid)
    # Its own row was never cut, so the two counts agree: no `short` badge.
    assert cost.hulls_per_cycle == cost.hulls_planned > 0
    # Neither exceeds the pipeline's attributed share of the cycle, and
    # the share is the build's, not the demand's (which was 2 here).
    row = conn.execute(
        "SELECT a.qty_attributable FROM index_run_item i "
        "JOIN index_run_item_pipeline a USING (index_run_item_id) "
        "WHERE i.index_run_id = ? AND i.type_id = ? AND a.pipeline_id = ?",
        (plan.index_run_id, bay, bay_pid),
    ).fetchone()
    assert cost.hulls_planned <= row["qty_attributable"]


def test_intermediates_sharing_a_short_input_install_the_same_share(conn, ref):
    """Half the fuel blocks the simple reactions would burn: every reaction
    bound by that fuel block installs the same fraction of its planned
    runs (whole runs, so within one run of each other), the composites
    bound tighter by their own empty inputs install nothing, and the
    installed draw still fits the stock."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    fuel = ref.type_id("Hydrogen Fuel Block")
    assert draw.get(fuel, 0) > 0
    # Unpriced, so the fuel block stays stock-limited: a PRICED built
    # fuel block buys its shortfall just in time (2026-09-12).
    snap = rich_snapshot(ref, overrides={fuel: None})
    snap.on_hand[fuel] = draw[fuel] // 2
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    bound = [i for i in _consumers(plan) if i.install_limited_by == fuel]
    assert len(bound) >= 2, [i.name for i in _consumers(plan)]
    fractions = [i.install_runs / i.runs_allocated for i in bound]
    assert all(0 < f < 1 for f in fractions), fractions
    # One common fraction, seen through whole runs: any two consumers'
    # fractions differ by no more than one run of each (the floor, and
    # the run the fill-up hands back where it fits).
    for a in bound:
        for b in bound:
            gap = abs(
                a.install_runs / a.runs_allocated
                - b.install_runs / b.runs_allocated
            )
            tolerance = 1.0 / a.runs_allocated + 1.0 / b.runs_allocated
            assert gap <= tolerance + 1e-9, (a.name, b.name, gap)
    # Nothing was cut for a material it never uses: a consumer bound by
    # the fuel block eats it.
    for item in bound:
        assert fuel in dict(ref.materials(item.blueprint_id, item.activity_id))
    installed = _install_draw(conn, ref, plan)
    assert installed[fuel] <= _available(plan.items[fuel])
    # Maximal: one more run of any bound consumer would not fit.
    draw_of = engine._draw_calculator(conn, ref)
    slack = _available(plan.items[fuel]) - installed[fuel]
    for item in bound:
        more = engine._packed_draw(draw_of, item, item.install_runs + 1, _per_job(item))[fuel]
        now = engine._packed_draw(draw_of, item, item.install_runs, _per_job(item)).get(fuel, 0)
        assert more - now > slack, item.name


def test_short_reactions_pack_full_jobs_plus_a_remainder_job(conn, ref):
    """User ruling 2026-09-09: a short SATURATING reaction installs full
    jobs at the plan's runs per job and ONE last job with the remainder,
    and the draw is judged at that packing."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    fuel = ref.type_id("Hydrogen Fuel Block")
    # Unpriced, so the fuel block stays stock-limited: a PRICED built
    # fuel block buys its shortfall just in time (2026-09-12).
    snap = rich_snapshot(ref, overrides={fuel: None})
    snap.on_hand[fuel] = draw[fuel] // 2
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    cut = [i for i in _consumers(plan) if 0 < i.install_runs < i.runs_allocated]
    assert cut and all(_saturating_reaction(ref, i) for i in cut)
    saw_remainder = False
    for item in cut:
        per_job = _per_job(item)
        assert item.install_per_job == per_job
        assert item.install_jobs == math.ceil(item.install_runs / per_job)
        last = item.install_runs - (item.install_jobs - 1) * per_job
        assert 0 < last <= per_job
        saw_remainder |= last < per_job
    assert saw_remainder
    # The packing helper itself: 23 runs at 10 per job = 10 + 10 + 3.
    draw_of = engine._draw_calculator(conn, ref)
    item = cut[0]
    packed = engine._packed_draw(draw_of, item, 23, 10)
    expected = {}
    for part, jobs in ((20, 2), (3, 1)):
        for m, q in draw_of(item, part, jobs).items():
            expected[m] = expected.get(m, 0) + q
    assert packed == expected
    assert engine._packed_draw(draw_of, item, 0, 10) == {}


def test_uniform_jobs_round_the_per_job_count_up():
    """(jobs, runs per job) for an intermediate: the jobs its plan's
    per-job count needs, every job the same length, rounded up."""
    assert engine._uniform_jobs(13, 8) == (2, 7)
    assert engine._uniform_jobs(12, 8) == (2, 6)
    assert engine._uniform_jobs(16, 8) == (2, 8)
    assert engine._uniform_jobs(17, 8) == (3, 6)
    assert engine._uniform_jobs(8, 8) == (1, 8)
    assert engine._uniform_jobs(5, 8) == (1, 5)
    assert engine._uniform_jobs(0, 8) == (0, 0)


def test_intermediates_install_uniform_jobs_rounded_up_when_stock_allows(conn, ref):
    """User ruling 2026-09-09: a short manufacturing intermediate installs
    the way Phase 7 sizes it — every job the same length, the per-job
    count rounded up when the inputs allow, else the largest uniform
    count that fits — never a remainder job. A two-hour window gives the
    components several jobs each; one of their composites is short."""
    conn.execute("UPDATE settings SET max_run_duration_hours = 2")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    hulk = ref.type_id("Hulk")
    # A manufacturing intermediate with several jobs and a BUILT input.
    component = next(
        i for i in _consumers(empty)
        if i.activity_id == config.ACTIVITY_MANUFACTURING
        and i.type_id != hulk and i.jobs_allocated >= 2
        and any(
            ref.blueprint_for_product(m) is not None
            for m, _q in ref.materials(i.blueprint_id, i.activity_id)
        )
    )
    composite = next(
        m for m, _q in ref.materials(component.blueprint_id, component.activity_id)
        if ref.blueprint_for_product(m) is not None
    )
    snap = rich_snapshot(ref)
    for item in empty.items.values():
        if item.buildable and item.type_id not in (hulk, component.type_id):
            snap.on_hand[item.type_id] = 10**9
    # Two thirds of what THIS component's planned jobs draw (its sibling
    # components are stocked to the hilt and plan no jobs now).
    own_draw = engine._draw_calculator(conn, ref)(
        component, component.runs_allocated, component.jobs_allocated
    )[composite]
    snap.on_hand[composite] = own_draw * 2 // 3
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    c = plan.items[component.type_id]
    assert c.jobs_allocated >= 2
    assert 0 < c.install_runs < c.runs_allocated, (c.install_runs, c.runs_allocated)
    assert c.install_limited_by == composite
    # Uniform: jobs × per-job == runs, the per-job count at or below the plan's.
    assert c.install_per_job * c.install_jobs == c.install_runs
    assert c.install_per_job <= _per_job(c)
    # The draw at that packing fits...
    installed = _install_draw(conn, ref, plan)
    avail = _available(plan.items[composite])
    assert installed[composite] <= avail
    # ... and one more run on every job would not: the round-up went as
    # far as the inputs allow.
    draw_of = engine._draw_calculator(conn, ref)
    mine = engine.install_draw_of(draw_of, c)[composite]
    more = draw_of(c, c.install_jobs * (c.install_per_job + 1), c.install_jobs)[composite]
    assert installed[composite] - mine + more > avail
    # Persisted the same way.
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    row = conn.execute(
        "SELECT install_runs, install_jobs, install_per_job FROM index_run_item "
        "WHERE index_run_id = ? AND type_id = ?", (plan.index_run_id, component.type_id),
    ).fetchone()
    assert row["install_per_job"] * row["install_jobs"] == row["install_runs"]


def test_a_consumer_bound_elsewhere_leaves_its_share_to_its_siblings(conn, ref):
    """Max-min fairness: a fuel-block consumer with NO stock of another
    input installs nothing, and the fuel it would have taken goes to the
    consumers that can run — they install more than a strict pro-rata
    split of the fuel would give."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, empty.items)
    fuel = ref.type_id("Hydrogen Fuel Block")
    # Unpriced, so the fuel block stays stock-limited: a PRICED built
    # fuel block buys its shortfall just in time (2026-09-12).
    snap = rich_snapshot(ref, overrides={fuel: None})
    # Half the fuel; every other built input stays empty, so the
    # composite reactions (fuel + simple reaction products) are bound by
    # their empty products, not the fuel.
    snap.on_hand[fuel] = draw[fuel] // 2
    plan = engine.plan_index_run(conn, ref, snap, persist=False)

    def eats_fuel(i):
        return fuel in dict(ref.materials(i.blueprint_id, i.activity_id))

    def eats_a_built_input_besides_fuel(i):
        return any(
            ref.blueprint_for_product(m) is not None and m != fuel
            for m, _q in ref.materials(i.blueprint_id, i.activity_id)
        )

    eaters = [i for i in _consumers(plan) if eats_fuel(i)]
    starved = [i for i in eaters if eats_a_built_input_besides_fuel(i)]
    running = [i for i in eaters if not eats_a_built_input_besides_fuel(i)]
    assert starved and running
    for item in starved:
        assert item.install_runs == 0, item.name
        assert item.install_limited_by is not None
        assert item.install_limited_by != fuel, item.name
    # A strict pro-rata split of the fuel over EVERY planned eater would
    # give each this share; the simple reactions get more, because the
    # starved composites' share went back to them.
    pro_rata = _available(plan.items[fuel]) / plan.items[fuel].install_draw_qty
    assert all(
        i.install_runs / i.runs_allocated > pro_rata + 1e-9 for i in running
    ), [(i.name, i.install_runs, i.runs_allocated, pro_rata) for i in running]


# --- persistence and the page ----------------------------------------------


def test_install_columns_persist_and_migrate(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=True)
    row = conn.execute(
        "SELECT install_runs, install_jobs, install_limited_by, "
        "install_priority, install_draw_qty, install_short_qty, "
        "install_return "
        "FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, ref.type_id("Hulk")),
    ).fetchone()
    hulk = plan.items[ref.type_id("Hulk")]
    assert tuple(row) == (
        hulk.install_runs, hulk.install_jobs, hulk.install_limited_by,
        hulk.install_priority, hulk.install_draw_qty, hulk.install_short_qty,
        hulk.install_return,
    )
    assert row["install_priority"] == 1 and row["install_jobs"] == 0
    assert row["install_return"] is not None
    bound = conn.execute(
        "SELECT install_draw_qty, install_short_qty FROM index_run_item "
        "WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, hulk.install_limited_by),
    ).fetchone()
    assert bound["install_short_qty"] > 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 11


def test_schema_9_database_gains_the_columns(tmp_path, monkeypatch):
    """An upgraded database: the nine new columns arrive by migration and
    the stamp moves to the current version; rows planned before carry
    NULLs. The
    pre-upgrade backup goes to THIS temp dir — review 2026-09-09 caught
    the first version of this test writing a 221 KB junk
    magoo-pre-1.27.0.sqlite into the user's real data/backups (the
    conftest tripwire now fails any test that does)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.sqlite")
    c = sqlite3.connect(tmp_path / "old.sqlite")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    for col in (
        "install_runs", "install_jobs", "install_limited_by",
        "install_priority", "install_draw_qty", "install_short_qty",
        "install_return", "install_per_job", "cycle_need_qty",
    ):
        c.execute(f"ALTER TABLE index_run_item DROP COLUMN {col}")
    for col in ("manufacturing_slots_available", "reaction_slots_available"):  # schema 11
        c.execute(f"ALTER TABLE index_run DROP COLUMN {col}")
    c.execute("PRAGMA user_version = 9")
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, status) "
        "VALUES (1, '2026-09-01', 'planned')"
    )
    c.execute(
        "INSERT INTO index_run_item (index_run_id, type_id, jobs_allocated, "
        "runs_allocated) VALUES (1, 22544, 8, 8)"
    )
    c.commit()
    store.ensure_schema(c)
    assert list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))  # landed HERE
    columns = {r["name"] for r in c.execute("PRAGMA table_info(index_run_item)")}
    assert {"install_runs", "install_short_qty", "install_return", "install_per_job", "cycle_need_qty"} <= columns
    run_columns = {r["name"] for r in c.execute("PRAGMA table_info(index_run)")}
    assert {"manufacturing_slots_available", "reaction_slots_available"} <= run_columns
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    row = c.execute("SELECT install_runs, install_priority FROM index_run_item").fetchone()
    assert row["install_runs"] is None and row["install_priority"] is None
    assert c.execute("SELECT manufacturing_slots_available FROM index_run").fetchone()[0] is None
    c.close()


def test_run_persists_and_shows_the_pool_it_was_planned_against(seeded_client, ref):
    """Review 2026-09-10: the engine allocates and caps against the
    settings' pools LESS the multi-cycle jobs running past the next run
    (engine.snapshot_from_state), so the run page must measure the plan
    against that pool, not the settings' figure — it is persisted on the
    run (schema 11). 480 far-future manufacturing job ends on a 500-slot
    pool leave 20 slots."""
    import json

    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        "UPDATE esi_snapshot SET job_ends = ?",
        (json.dumps({"1": ["2099-01-01T00:00:00Z"] * 480}),),
    )
    c.commit()
    assert engine.snapshot_from_state(c).slots_available[config.ACTIVITY_MANUFACTURING] == 20
    run_id = _plan(seeded_client)
    run = c.execute("SELECT * FROM index_run WHERE index_run_id = ?", (run_id,)).fetchone()
    assert run["manufacturing_slots_available"] == 20 and run["reaction_slots_available"] == 500
    mfg = c.execute(
        "SELECT SUM(jobs_allocated) AS j, SUM(install_jobs) AS n FROM index_run_item "
        "WHERE index_run_id = ? AND activity_id = 1 AND recommended_build_qty > 0",
        (run_id,),
    ).fetchone()
    c.close()
    html = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert f">{mfg['n']}<span class=\"sub\">/ 20" in html
    assert "20 of the 500 configured slots" in html
    assert mfg["n"] <= 20  # the startable jobs never exceed the pool the run had
    if mfg["j"] > 20:
        assert f"{mfg['j'] - 20} more than the pool" in html
    else:
        assert "more than the pool" not in html


def test_run_page_names_the_pool_for_a_slot_trimmed_row(seeded_client, ref):
    """Review 2026-09-10: a row Phase 7.6 trimmed for want of a SLOT
    (install_limited_by = −1) is not short on stock — its badge reads
    `no slot`, its tooltips name the pool, and nothing points at a
    short-on-stock panel that is not on the page."""
    run_id = _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    # No input short anywhere; every job runs as planned ...
    c.execute(
        "UPDATE index_run_item SET install_short_qty = 0, install_draw_qty = NULL, "
        "install_limited_by = NULL, install_runs = runs_allocated, install_jobs = jobs_allocated "
        "WHERE index_run_id = ?", (run_id,),
    )
    # ... except one manufacturing row the pool could not seat in full.
    row = c.execute(
        "SELECT index_run_item_id, jobs_allocated, runs_allocated FROM index_run_item "
        "WHERE index_run_id = ? AND activity_id = 1 AND jobs_allocated > 1 "
        "ORDER BY jobs_allocated DESC LIMIT 1", (run_id,),
    ).fetchone()
    per_job = -(-row["runs_allocated"] // row["jobs_allocated"])
    c.execute(
        "UPDATE index_run_item SET install_jobs = ?, install_runs = ?, install_per_job = ?, "
        "install_limited_by = -1 WHERE index_run_item_id = ?",
        (row["jobs_allocated"] - 1, per_job * (row["jobs_allocated"] - 1), per_job, row["index_run_item_id"]),
    )
    c.commit()
    c.close()
    html = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert ">no slot</span>" in html
    assert "the pool has no free slot this cycle for 1 of the" in html
    assert "have a slot this cycle" in html
    assert "short on stock" not in html and "see Short on stock" not in html
    assert "short on the slot pool" not in html and ">short</span>" not in html


def _plan(client) -> int:
    resp = client.post("/run")
    assert resp.status_code == 302, resp.status_code
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def test_run_page_shows_the_install_column_and_the_short_panel(seeded_client, ref):
    """Empty stock, Hulk x 8: the Plan tab's job tables show the jobs to
    run now (no Install now column — user ruling 2026-09-09), the Hulk
    row reads 0 jobs with a 'short' badge, the totals strip counts the
    jobs to run beside the plan's allocation, and the short-on-stock
    panel names what is short and the finals order."""
    run_id = _plan(seeded_client)
    html = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "Install now" not in html
    assert "short on stock" in html
    assert "Finals in install order — 1" in html
    assert "<td>Hulk</td>" in html and "0 / 8</td>" in html
    assert ">short</span>" in html
    assert "Held back" in html
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    mfg = c.execute(
        "SELECT SUM(jobs_allocated) AS j, SUM(install_jobs) AS n "
        "FROM index_run_item WHERE index_run_id = ? AND activity_id = 1 "
        "AND recommended_build_qty > 0",
        (run_id,),
    ).fetchone()
    c.close()
    assert mfg["n"] < mfg["j"]
    # The Mfg slots stat: jobs to run now, the plan's allocation in the sub.
    assert f">{mfg['n']}<span class=\"sub\">/ 500 · plan {mfg['j']}" in html
    # The Hulk job row: 0 jobs of the 8 planned, the plan in the tooltip.
    assert "the plan wanted 8 job(s)" in html
    # The Chain tab and the Slot Planner are untouched by the check.
    assert seeded_client.get(f"/runs/{run_id}?view=chain").status_code == 200
    slots = seeded_client.get("/planning?view=slots").get_data(as_text=True)
    assert "short</span>" not in slots and "· plan " not in slots


def test_slot_planner_shows_the_plan_not_the_rationed_jobs(ref, seeded_client):
    """The Slot Planner is a what-if and must show the PLAN's jobs
    whatever the install check made of the steady draft: _steady_rows
    drops the install figures, so its section stats sum jobs_allocated.
    (Until the cycle need was sized at the jobs' rounding, the stocked
    draft rationed its saturating reactions; now it installs in full —
    either way the rows carry no install figures.)"""
    from magoo import engine as engine_mod, web as web_mod
    from magoo.engine import Snapshot

    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    prices = FairValuePrices(ref)
    snap = Snapshot(
        slots_available={config.ACTIVITY_MANUFACTURING: 500, config.ACTIVITY_REACTION: 500},
        prices=prices, adjusted_prices=prices,
    )
    plan = engine_mod.plan_steady_state(c, ref, snap)
    c.close()
    checked = [i for i in plan.items.values() if i.install_runs is not None]
    assert checked, "the steady draft went through the install check"
    # Pretend the check cut something, as it did before the cycle need
    # was sized at the jobs' rounding: the rows must still show the plan.
    victim = next(i for i in checked if i.runs_allocated > 1)
    victim.install_runs, victim.install_jobs = 1, 1
    rows = web_mod._steady_rows(ref, plan)
    by = {r["type_id"]: r for r in rows}
    for item in checked:
        row = by[item.type_id]
        assert row["install_runs"] is None and row["install_jobs"] is None
        assert web_mod._jobs_to_run(row) == item.jobs_allocated
        assert web_mod._build_qty_to_run(row) == item.recommended_build_qty


def test_a_packed_cut_rows_jobs_tooltip_names_the_short_last_job(seeded_client, ref):
    """Review 2026-09-10: the Jobs cell took its 'every planned job runs'
    branch whenever the check kept the job count, and that branch assumed
    the cut shows as a SHORTER per-job count. An exact-quantity ship or a
    saturating reaction keeps the plan's per-job length and shortens only
    the LAST job, so the sentence printed the same number twice ('at 10
    runs each rather than the plan's 10') and denied the cut its own
    Runs/job cell shows as '· last N'."""
    run_id = _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT index_run_item_id, jobs_allocated, runs_allocated FROM index_run_item "
        "WHERE index_run_id = ? AND jobs_allocated > 1 AND runs_allocated >= 4 * jobs_allocated "
        "ORDER BY runs_allocated DESC LIMIT 1", (run_id,),
    ).fetchone()
    assert row, "the seeded line has a multi-job row to pack"
    per_job = row["runs_allocated"] // row["jobs_allocated"]
    # Every job at the plan's length, the last one short: _packed_draw's shape.
    install_runs = per_job * (row["jobs_allocated"] - 1) + max(1, per_job // 2)
    c.execute(
        "UPDATE index_run_item SET install_jobs = ?, install_runs = ?, "
        "install_per_job = ?, install_limited_by = NULL WHERE index_run_item_id = ?",
        (row["jobs_allocated"], install_runs, per_job, row["index_run_item_id"]),
    )
    c.commit()
    c.close()
    html = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    last = max(1, per_job // 2)  # the cells render thousands separators
    assert f"the last is short at {last:,} run" in html
    assert f"at {per_job:,} runs each rather than the plan's {per_job:,}" not in html


def test_the_slot_pool_clause_does_not_blame_overhang_for_a_settings_change(
    seeded_client, ref
):
    """Review 2026-09-10: the clause compared the run's PERSISTED plan-time
    pool (schema 11) with TODAY's setting and blamed any difference on
    multi-cycle jobs, so lowering the setting after planning rendered the
    arithmetically impossible '500 of the 20 configured slots'. Overhang
    can only REDUCE the pool, so only that direction earns the
    explanation."""
    run_id = _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    pool = c.execute(
        "SELECT manufacturing_slots_available FROM index_run WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()[0]
    assert pool == 500
    c.execute("UPDATE settings SET manufacturing_slots = 20, reaction_slots = 20")
    c.commit()
    c.close()
    html = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "500 of the 20 configured slots" not in html
    assert "the pool this run was planned against; Settings now reads 20" in html


def test_run_planned_before_the_check_renders_without_it(seeded_client, ref):
    """NULLs throughout (a pre-v1.27.1 run): no panel, no badges, the
    job tables and the strip read the plan's own figures."""
    run_id = _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    c.execute(
        "UPDATE index_run_item SET install_runs = NULL, install_jobs = NULL, "
        "install_per_job = NULL, install_limited_by = NULL, "
        "install_priority = NULL, install_return = NULL, "
        "install_draw_qty = NULL, install_short_qty = NULL "
        "WHERE index_run_id = ?",
        (run_id,),
    )
    c.commit()
    c.close()
    html = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "short on stock" not in html
    assert ">short</span>" not in html
    assert "· plan " not in html
    assert "the plan wanted" not in html
    assert '<span class="sub">/ 500</span>' in html
