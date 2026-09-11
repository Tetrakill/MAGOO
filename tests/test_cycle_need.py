"""The cycle need at the jobs' own rounding (engine Phase 3.5, user ruling
2026-09-09): targets and deficits are sized on what the installed jobs
actually draw — per-job ceilings, uniform round-up, full reaction windows,
whole copies — not on the merged BOM figure, so a line stocked at target
installs every planned job.

Same fixture pattern as test_engine: state in a temp database, reference
data from the real imported SDE.
"""

import math
import sqlite3

import pytest

from magoo import config, engine, industry, store
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


def add_pipeline(conn, ref, product_name: str, qty: int, runs_per_bpc=None) -> int:
    cur = conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run, runs_per_bpc) VALUES (?, ?, ?, ?)",
        (product_name, ref.type_id(product_name), qty, runs_per_bpc),
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


def test_cycle_need_is_never_below_the_merged_bom_need_within_a_pipeline(conn, ref):
    """A sum of per-job ceilings is never below the one merged ceiling,
    and somewhere in a real chain it is strictly above. (One pipeline:
    across pipelines sharing a consumer the pass merges demand before
    rounding and can land below merged_min's sum of separately rounded
    runs — see the next test.)"""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    for item in plan.items.values():
        assert item.cycle_need_qty >= item.merged_min_qty, item.name
    assert any(
        i.cycle_need_qty > i.merged_min_qty for i in plan.items.values()
    )
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.cycle_need_qty == hulk.merged_min_qty == 8


def test_shared_consumers_merge_before_rounding(conn, ref):
    """Review 2026-09-09: two pipelines sharing a consumer — merged_min
    sums each pipeline's separately rounded runs, the cycle need rounds
    the merged demand once, so it can sit BELOW merged_min there and is
    the truer figure. Obelisk × 2 + Anshar × 1 share Fermionic
    Condensates (72 + 13,896 units of demand: 1 + 70 runs rounded apart,
    70 runs rounded together)."""
    add_pipeline(conn, ref, "Obelisk", 2)
    add_pipeline(conn, ref, "Anshar", 1)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref, slots=100000), persist=False)
    below = [i for i in plan.items.values() if i.cycle_need_qty < i.merged_min_qty]
    assert below, "the shared-consumer rounding case"
    fermionic = plan.items[ref.type_id("Fermionic Condensates")]
    assert fermionic.cycle_need_qty > 0
    # And the pass is self-consistent: each such material's cycle need is
    # exactly its consumers' shares.
    shares = engine._steady_shares(conn, ref, plan.items)
    for item in below:
        assert sum(shares[item.type_id].values()) == item.cycle_need_qty - item.requested_qty


def test_one_run_capital_jobs_round_their_components_per_job(conn, ref):
    """Six Archons install as six one-run jobs (a capital hull's run is
    longer than the window), so each job rounds its components up
    separately: the cycle need is six times one job's requirement, where
    the merged BOM figure rounded the six runs once."""
    conn.execute("UPDATE settings SET default_intermediate_me = 7")
    conn.commit()
    add_pipeline(conn, ref, "Archon", 6)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    archon = plan.items[ref.type_id("Archon")]
    assert archon.max_runs_per_job == 1 and archon.jobs_allocated == 6
    me_te = store.me_te_resolver(conn)
    me, _te = me_te(archon.blueprint_id, archon.activity_id)
    class_settings = store.get_class_settings(conn)
    mult = industry.build_multiplier(
        ref, class_settings.get(archon.item_class, industry.NPC_STATION),
        archon.activity_id, "material",
        group_id=ref.type_info(archon.type_id).group_id,
    )
    saw_gap = False
    for material_id, base_qty in ref.materials(archon.blueprint_id, archon.activity_id):
        item = plan.items[material_id]
        per_job = 6 * industry.required_quantity(1, base_qty, me, mult)
        merged = industry.required_quantity(6, base_qty, me, mult)
        assert item.cycle_need_qty == per_job, item.name
        assert item.merged_min_qty == merged, item.name
        saw_gap |= per_job > merged
    assert saw_gap, "some component quantity is fractional per run"


def test_targets_and_deficits_are_sized_on_the_cycle_need(conn, ref):
    """The converged target is ceil(cycle need × (1 + buffer)), prorated
    by the share of the cycle need coming from consumers that hold jobs
    (ruling R7), plus the composite adder for composites holding jobs —
    Phase 4's arithmetic on the new basis."""
    add_pipeline(conn, ref, "Archon", 6)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    settings = store.get_settings(conn)
    buffer_mult = 1 + settings.stockpile_buffer
    composite = engine._composite_extra_targets(
        ref, settings, store.get_class_settings(conn), plan.items,
        consumers_with_jobs_only=True,
    )
    shares = engine._steady_shares(conn, ref, plan.items)
    checked = 0
    for item in plan.items.values():
        if not item.buildable or item.type_id == ref.type_id("Archon"):
            continue
        by_consumer = shares.get(item.type_id, {})
        total = sum(by_consumer.values())
        active = sum(
            u for cid, u in by_consumer.items()
            if plan.items[cid].runs_allocated > 0
        )
        fraction = active / total if total else 0.0
        expected = engine._ceil(
            engine._ceil(item.cycle_need_qty * buffer_mult) * fraction
        ) + composite.get(item.type_id, 0)
        assert item.target_stock_qty == expected, item.name
        if fraction == 1.0:
            checked += 1
            # From empty stock a stage whose consumers all hold jobs builds
            # at least its target (the loop adds their actual draw on top).
            assert item.deficit_qty >= item.target_stock_qty, item.name
    assert checked


@pytest.mark.parametrize("window_hours", [24.0, 800.0])
def test_a_line_stocked_at_target_installs_every_planned_job(conn, ref, window_hours):
    """The point of the ruling: seed every buildable stage at the target
    the plan derives, re-plan, and the install check finds nothing short
    — each consumer's jobs draw exactly what the cycle need packed for
    them. Both the neutral 24 h window and the user's 800 h one."""
    conn.execute("UPDATE settings SET max_run_duration_hours = ?", (window_hours,))
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)
    add_pipeline(conn, ref, "Archon", 6)
    # Ample slots: under contention a reaction that lost the MILP in the
    # empty plan leaves its inputs' targets prorated to zero (ruling R7),
    # and a stage seeded at that zero cannot feed the consumer once it
    # wins slots in the re-plan — a settled consequence, not this rule.
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref, slots=3000), persist=False)
    snap = rich_snapshot(ref, slots=3000)
    for item in empty.items.values():
        if item.buildable:
            snap.on_hand[item.type_id] = item.target_stock_qty
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    consumers = [i for i in plan.items.values() if i.runs_allocated > 0]
    assert consumers
    short = [i.name for i in plan.items.values() if i.install_short_qty]
    assert short == []
    for item in consumers:
        assert item.install_runs == item.runs_allocated, item.name
        assert item.install_limited_by is None, item.name
    # And every stocked stage ends the cycle back at (or above) target.
    draw = engine._planned_consumption(conn, ref, plan.items)
    for item in plan.items.values():
        if item.buildable and item.type_id not in (ref.type_id("Hulk"), ref.type_id("Archon")):
            projected = (
                item.on_hand_qty + item.recommended_build_qty
                + item.recommended_buy_qty - draw.get(item.type_id, 0)
            )
            assert projected >= item.target_stock_qty, item.name


def test_steady_shares_are_the_cycle_need_pass(conn, ref):
    """The proration shares the feedback loop reads sum to each material's
    cycle need (less a final's requested output), consumer by consumer."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    shares = engine._steady_shares(conn, ref, plan.items)
    for material_id, by_consumer in shares.items():
        item = plan.items[material_id]
        assert sum(by_consumer.values()) == item.cycle_need_qty - item.requested_qty, item.name
        for consumer_id in by_consumer:
            assert material_id in dict(
                ref.materials(plan.items[consumer_id].blueprint_id, plan.items[consumer_id].activity_id)
            )


def test_cycle_need_persists_and_feeds_the_chain_tab(conn, ref):
    add_pipeline(conn, ref, "Archon", 6)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=True)
    rows = conn.execute(
        "SELECT type_id, cycle_need_qty, merged_min_qty FROM index_run_item "
        "WHERE index_run_id = ?", (plan.index_run_id,),
    ).fetchall()
    assert rows
    for r in rows:
        assert r["cycle_need_qty"] == plan.items[r["type_id"]].cycle_need_qty
        assert r["cycle_need_qty"] >= r["merged_min_qty"]


def test_whole_copy_finals_pack_the_copy_into_the_cycle_need(conn, ref):
    """A sub-capital final with runs per BPC builds whole copies: 6 Hulks
    on a 5-run copy build 10, and the components' cycle need is what ten
    hulls' jobs draw, not six."""
    add_pipeline(conn, ref, "Hulk", 6, runs_per_bpc=5)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.recommended_build_qty == 10
    assert hulk.cycle_need_qty == 6  # its own cycle is the request
    me_te = store.me_te_resolver(conn)
    me, _te = me_te(hulk.blueprint_id, hulk.activity_id)
    mult = industry.build_multiplier(
        ref, store.get_class_settings(conn).get(hulk.item_class, industry.NPC_STATION),
        hulk.activity_id, "material", group_id=ref.type_info(hulk.type_id).group_id,
    )
    # Ten runs packed as the plan packs them (a Hulk run outlasts the
    # window, so ten one-run jobs), each job rounding once.
    packing = engine._steady_packing(
        ref, hulk, 10, hulk.max_runs_per_job, {hulk.type_id}
    )
    assert sum(jobs * each for jobs, each in packing) == 10
    checked = 0
    for material_id, base_qty in ref.materials(hulk.blueprint_id, hulk.activity_id):
        others = [
            i for i in plan.items.values()
            if i.buildable and i.type_id != hulk.type_id
            and material_id in dict(ref.materials(i.blueprint_id, i.activity_id))
        ]
        if others:
            continue  # shared with another consumer; not a clean check
        expected = sum(
            jobs * industry.required_quantity(each, base_qty, me, mult)
            for jobs, each in packing
        )
        assert plan.items[material_id].cycle_need_qty == expected, ref.type_info(material_id).name
        checked += 1
    assert checked
