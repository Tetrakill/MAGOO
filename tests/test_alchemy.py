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
        used = sum(
            i.jobs_allocated
            for i in plan.items.values()
            if i.activity_id == config.ACTIVITY_REACTION
        )
        assert used <= slots


def test_no_alchemy_under_contention(conn, ref):
    """A contended reaction pool has no spare slots — alchemy stays out."""
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    overrides = expensive_rare_inputs(ref, reaction_candidates(ref, baseline))
    enable_alchemy(conn)
    plan = engine.plan_index_run(
        conn, ref, snapshot(ref, slots=3, overrides=overrides), persist=False
    )
    assert not alchemy_items(plan)


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

    # Sanity anchor: the reaction-pool bound still holds under pressure.
    used = sum(
        i.jobs_allocated
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    assert used <= reaction_slots
