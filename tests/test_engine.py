"""Planning engine against real reference data with synthetic snapshots.

State lives in a temp database per test; reference data comes from the real
imported SDE via Refdata.
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


def add_pipeline(
    conn, ref, product_name: str, qty: int, runs_per_bpc: int | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run, runs_per_bpc) VALUES (?, ?, ?, ?)",
        (product_name, ref.type_id(product_name), qty, runs_per_bpc),
    )
    conn.commit()
    return cur.lastrowid


def rich_snapshot(ref, slots=500, overrides=None):
    """Fair-value prices (builds stay profitable), generous slots, empty
    stockpiles. `overrides` pins individual type prices."""
    prices = FairValuePrices(ref, overrides=overrides)
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=prices,
        adjusted_prices=prices,
    )


# --- Core planning ---------------------------------------------------------


def test_empty_stock_plans_full_chain(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    assert len(plan.items) == 78  # same chain as the BOM anchor
    hulk = plan.items[ref.type_id("Hulk")]
    # Final ship counts are exact: no buffer on the target, and 8 is already
    # a batch multiple
    assert hulk.target_stock_qty == 8
    assert hulk.total_runs_needed == 8
    # >24h per run -> 1 run per job, 8 parallel jobs
    assert hulk.max_runs_per_job == 1
    assert hulk.jobs_needed_unconstrained == 8
    assert hulk.jobs_allocated == 8
    assert hulk.recommended_build_qty == 8
    assert not hulk.capacity_limited
    # Raw minerals are buys
    trit = plan.items[ref.type_id("Tritanium")]
    assert trit.recommended_action == "buy"
    assert trit.recommended_buy_qty == trit.deficit_qty > 0


def test_final_ships_always_build_requested_qty(conn, ref):
    """Final products ignore stock and in-flight jobs — the line advances
    every cycle."""
    add_pipeline(conn, ref, "Hulk", 8)
    snap = rich_snapshot(ref)
    hulk_id = ref.type_id("Hulk")
    snap.on_hand[hulk_id] = 100
    snap.in_progress[hulk_id] = 100
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    hulk = plan.items[hulk_id]
    assert hulk.deficit_qty == 8
    assert hulk.recommended_action == "build"
    assert hulk.recommended_build_qty == 8


def test_stock_and_in_progress_reduce_intermediate_deficit(conn, ref):
    """Intermediates and raw materials still count stock and in-flight
    output."""
    add_pipeline(conn, ref, "Hulk", 8)
    snap = rich_snapshot(ref)
    trit_id = ref.type_id("Tritanium")
    snap.on_hand[trit_id] = 10**10  # ocean of tritanium
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    trit = plan.items[trit_id]
    assert trit.deficit_qty == 0
    assert trit.recommended_buy_qty == 0


def test_steady_state_stages_end_cycle_covered(conn, ref):
    """The pipelined invariant, restated for the consumption feedback pass
    (2026-08-21): with every stage stocked at target, each stage is sized
    to its consumers' ACTUAL draw (saturating reactions overshoot, jobs
    round per install), so every buildable stage projects to end the cycle
    with at least one cycle's consumption on hand — no low-stock flags."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    snap = rich_snapshot(ref)
    for item in empty.items.values():
        snap.on_hand[item.type_id] = item.target_stock_qty
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[ref.type_id("Hulk")].deficit_qty == 8  # finals: exact
    assert not any(i.low_stock for i in plan.items.values())
    draw = engine._planned_consumption(conn, ref, plan.items)
    for item in plan.items.values():
        if not item.buildable or item.type_id == ref.type_id("Hulk"):
            continue
        projected = (
            item.on_hand_qty
            + item.in_progress_qty
            + item.recommended_build_qty
            + item.recommended_buy_qty
            + item.alchemy_output_qty
            - draw.get(item.type_id, 0)
        )
        assert projected >= item.merged_min_qty, item.name


def test_second_pass_covers_catch_up_draw(conn, ref):
    """A drained stage rebuilds more than one cycle's worth this run; the
    feedback pass sizes its SUPPLIERS to that actual draw instead of the
    steady-state figure, so nothing projects below one cycle's
    consumption."""
    add_pipeline(conn, ref, "Hulk", 8)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    snap = rich_snapshot(ref)
    for item in empty.items.values():
        snap.on_hand[item.type_id] = item.target_stock_qty
    snap.on_hand[ref.type_id("Ion Thruster")] = 0  # drained -> catch-up
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    thruster = plan.items[ref.type_id("Ion Thruster")]
    assert thruster.recommended_build_qty >= thruster.merged_min_qty
    assert not any(i.low_stock for i in plan.items.values())


def _assert_all_stages_covered_and_converged(conn, ref, plan):
    """The two invariants the iterated feedback loop (2026-08-28)
    guarantees at convergence, checked against the FINAL allocation's
    draw: every buildable stage projects to end the cycle at or above
    one cycle's consumption, and every corrected deficit satisfies its
    own defining identity (deficit = target + draw − stock − in
    flight). The identity is what the run page's deficit dialog
    algebraically inverts — under the old single re-size it held only
    approximately."""
    draw = engine._planned_consumption(conn, ref, plan.items)
    finals = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }
    for item in plan.items.values():
        if not item.buildable or item.type_id in finals:
            continue
        projected = (
            item.on_hand_qty
            + item.in_progress_qty
            + item.recommended_build_qty
            + item.recommended_buy_qty
            + item.alchemy_output_qty
            - draw.get(item.type_id, 0)
        )
        assert projected >= item.merged_min_qty, item.name
        if item.alchemy_for_type_id is None:
            assert item.deficit_qty == max(
                0,
                item.target_stock_qty
                + draw.get(item.type_id, 0)
                - item.on_hand_qty
                - item.in_progress_qty,
            ), item.name


def test_feedback_iterates_multi_tier_catch_up(conn, ref):
    """Run-59 regression (2026-08-28): at production scale one drained
    component grows its reaction suppliers on the FIRST correction, and
    under the old single re-size THEIR depth-3 suppliers stayed sized to
    the pre-correction draw — ending the cycle below one cycle's
    consumption (four processed materials short at this scale). The
    feedback loop iterates until deficits stop moving, reaching however
    deep the catch-up cascades. Long window so the cascade is a pure
    sizing effect — no slot contention (asserted)."""
    add_pipeline(conn, ref, "Hulk", 400)
    conn.execute("UPDATE settings SET max_run_duration_hours = 2000")
    conn.commit()
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    snap = rich_snapshot(ref)
    for item in empty.items.values():
        snap.on_hand[item.type_id] = item.target_stock_qty
    snap.on_hand[ref.type_id("Ion Thruster")] = 0
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert not any(i.capacity_limited for i in plan.items.values())
    _assert_all_stages_covered_and_converged(conn, ref, plan)


def test_capacity_starved_buys_cover_converged_draw(conn, ref, monkeypatch):
    """Slot exhaustion during catch-up (run 59's Ferrofluid failure
    mode): a starved reaction's shortfall flips to a market buy, but the
    old single re-size priced that buy off the stale draft draw and the
    stage still ended below one cycle's consumption. At convergence the
    flipped buys cover the FINAL draw. Also pins that the loop CONVERGED
    rather than exhausting its cap: a converged loop breaks before its
    last allowed re-run, so allocation runs at most 1 + max depth times
    — only a cap-exit reaches 2 + max depth (see
    test_feedback_cap_bounds_non_converging_loop)."""
    add_pipeline(conn, ref, "Hulk", 100)  # 24h window -> reactions starve
    calls = {"n": 0}
    orig_allocate = engine._allocate_slots

    def counting_allocate(*args, **kwargs):
        calls["n"] += 1
        return orig_allocate(*args, **kwargs)

    monkeypatch.setattr(engine, "_allocate_slots", counting_allocate)
    empty = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    snap = rich_snapshot(ref)
    for item in empty.items.values():
        snap.on_hand[item.type_id] = item.target_stock_qty
    snap.on_hand[ref.type_id("Ion Thruster")] = 0
    calls["n"] = 0
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    starved = [i for i in plan.items.values() if i.capacity_limited]
    assert starved  # the scenario genuinely exhausts the reaction pool
    assert all(i.recommended_buy_qty > 0 for i in starved)
    _assert_all_stages_covered_and_converged(conn, ref, plan)
    # Strictly below the structural ceiling of 2 + max_depth: equality
    # there would mean the loop burned every allowed pass without the
    # deficits settling (cap-exit), not convergence.
    max_depth = max(i.depth for i in plan.items.values() if i.buildable)
    assert calls["n"] <= 1 + max_depth


def test_feedback_cap_bounds_non_converging_loop(conn, ref, monkeypatch):
    """The no-fixed-point guard: when the deficits never settle (forced
    here by nudging the measured draw upward on every sweep so a
    correction is always found), planning must still terminate — after
    exactly one initial allocation plus one re-run per allowed pass
    (max buildable depth + 1) — and the last allocation stands as a
    coherent plan instead of iterating forever."""
    add_pipeline(conn, ref, "Hulk", 8)
    victim = ref.type_id("Ion Thruster")
    bump = {"n": 0}
    calls = {"n": 0}
    orig_consumption = engine._planned_consumption
    orig_allocate = engine._allocate_slots

    def perturbed(conn_, ref_, merged):
        draw = orig_consumption(conn_, ref_, merged)
        bump["n"] += 1  # strictly increasing -> never converges
        draw[victim] = draw.get(victim, 0) + bump["n"]
        return draw

    def counting_allocate(*args, **kwargs):
        calls["n"] += 1
        return orig_allocate(*args, **kwargs)

    monkeypatch.setattr(engine, "_planned_consumption", perturbed)
    monkeypatch.setattr(engine, "_allocate_slots", counting_allocate)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    max_depth = max(i.depth for i in plan.items.values() if i.buildable)
    assert calls["n"] == 2 + max_depth  # cap-exit: every allowed pass ran
    # The final allocation still stands: finals keep their exact rule.
    assert plan.items[ref.type_id("Hulk")].recommended_build_qty == 8


def test_raw_buys_only_for_allocated_jobs(conn, ref):
    """Raw inputs are just-in-time: zero slots -> zero jobs -> zero raw
    material purchases, regardless of the demand tree."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref, slots=0), persist=False)
    raws = [i for i in plan.items.values() if not i.buildable]
    assert raws
    assert all(i.recommended_buy_qty == 0 for i in raws)


def test_raw_buys_track_consumption_with_margin(conn, ref):
    """Raw purchases = allocated-job consumption x (1 + margin), net of
    stock — not the full stockpile target."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    trit = plan.items[ref.type_id("Tritanium")]
    assert trit.recommended_buy_qty == trit.target_stock_qty > 0
    # Doubling the margin from settings raises the buy proportionally.
    conn.execute("UPDATE settings SET input_purchase_margin = 0.10")
    conn.commit()
    plan2 = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    trit2 = plan2.items[ref.type_id("Tritanium")]
    assert trit2.recommended_buy_qty > trit.recommended_buy_qty
    base = round(trit.recommended_buy_qty / 1.05)
    assert trit2.recommended_buy_qty == pytest.approx(base * 1.10, abs=2)


def test_raw_stock_nets_against_consumption(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    baseline = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    trit_id = ref.type_id("Tritanium")
    need = baseline.items[trit_id].recommended_buy_qty
    snap = rich_snapshot(ref)
    snap.on_hand[trit_id] = need // 2
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[trit_id].recommended_buy_qty == need - need // 2


def test_shared_demand_merges_across_pipelines(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    add_pipeline(conn, ref, "Skiff", 8)  # shares Covetor-era reaction chain
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    trit = plan.items[ref.type_id("Tritanium")]
    assert len(trit.pipeline_share) == 2
    assert sum(trit.pipeline_share.values()) == trit.merged_min_qty


def _contender(type_id, total_runs, max_runs):
    item = engine.PlanItem(
        type_id=type_id,
        name=f"item-{type_id}",
        item_class="other",
        depth=1,
        blueprint_id=type_id,
        activity_id=config.ACTIVITY_MANUFACTURING,
        portion_size=1,
    )
    item.recommended_action = "build"
    item.total_runs_needed = total_runs
    item.max_runs_per_job = max_runs
    item.jobs_needed_unconstrained = -(-total_runs // max_runs)
    return item


def test_milp_weights_last_job_at_residual_runs(conn, ref, monkeypatch):
    """One free slot: item A needs 1 run of a 10-run window at 5 ISK/unit
    (5 ISK really at stake); item B fills a whole job at 4 ISK/unit (40
    ISK). The slot must go to B — a full-window weight for A's nearly
    empty job used to invert this."""
    a = _contender(1001, 1, 10)
    b = _contender(1002, 10, 10)
    monkeypatch.setattr(
        engine,
        "_build_savings_per_unit",
        lambda ref, item, chain, buy_cost, snap: {1001: 5.0, 1002: 4.0}[
            item.type_id
        ],
    )
    snap = Snapshot(slots_available={config.ACTIVITY_MANUFACTURING: 1})
    engine._allocate_slots(conn, ref, {a.type_id: a, b.type_id: b}, snap)
    assert a.jobs_allocated == 0
    assert b.jobs_allocated == 1


def test_unpriced_contender_is_preallocated_under_contention(
    conn, ref, monkeypatch
):
    """An item with no market price cannot be bought back in Phase 7, so it
    must not lose the pool to epsilon-savings buyable items."""
    a = _contender(1001, 100, 10)  # unpriced -> no purchase fallback
    b = _contender(1002, 100, 10)  # buyable at 0.01 ISK/unit savings
    monkeypatch.setattr(
        engine,
        "_build_savings_per_unit",
        lambda ref, item, chain, buy_cost, snap: {1001: None, 1002: 0.01}[
            item.type_id
        ],
    )
    snap = Snapshot(slots_available={config.ACTIVITY_MANUFACTURING: 10})
    engine._allocate_slots(conn, ref, {a.type_id: a, b.type_id: b}, snap)
    assert a.jobs_allocated == 10
    assert b.jobs_allocated == 0


def test_milp_solver_failure_raises(conn, ref, monkeypatch):
    """A solver breakdown must fail loudly, not silently flip the whole
    pool to market buys."""
    a = _contender(1001, 100, 10)
    b = _contender(1002, 100, 10)
    monkeypatch.setattr(
        engine, "_build_savings_per_unit", lambda *args: 1.0
    )

    class Failed:
        success = False
        message = "numerical breakdown"

    monkeypatch.setattr(engine, "milp", lambda *args, **kwargs: Failed())
    snap = Snapshot(slots_available={config.ACTIVITY_MANUFACTURING: 10})
    with pytest.raises(RuntimeError, match="MILP failed"):
        engine._allocate_slots(conn, ref, {a.type_id: a, b.type_id: b}, snap)


def test_contention_respects_slot_caps(conn, ref):
    """Under contention no pool exceeds its slot budget, and unprofitable
    items lose their slots to higher-savings work."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref, slots=5), persist=False)
    for activity in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION):
        used = sum(
            i.jobs_allocated
            for i in plan.items.values()
            if i.activity_id == activity
        )
        assert used <= 5


def test_negative_savings_bought_even_with_idle_slots(conn, ref):
    """An INTERMEDIATE the market undercuts is bought in BOTH branches —
    idle slots don't justify building at a loss — and that is a market
    choice, not a capacity limit. Pipeline FINALS are exempt (2026-08-21):
    they are built to sell, whatever the single-stage paper margin says
    (run 41 told the user to market-buy 19 of 26 finals)."""
    add_pipeline(conn, ref, "Hulk", 8)
    snap = rich_snapshot(
        ref,
        overrides={
            ref.type_id("Ion Thruster"): 1.0,  # market undercuts the build
            ref.type_id("Hulk"): 1.0,  # final: exempt from the rule
        },
    )
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    thruster = plan.items[ref.type_id("Ion Thruster")]
    assert thruster.jobs_allocated == 0
    assert thruster.recommended_action == "buy"
    assert not thruster.capacity_limited
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.recommended_action == "build"
    assert hulk.recommended_build_qty == 8
    assert not hulk.capacity_limited


def test_slot_contention_milp(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref, slots=5), persist=False)
    builders = [
        i
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_MANUFACTURING
        and i.jobs_allocated > 0
    ]
    used = sum(i.jobs_allocated for i in builders)
    assert 0 < used <= 5
    # The final takes its slots FIRST (2026-08-21: finals never flip to
    # buy); intermediate losers are flagged and covered by buys.
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.jobs_allocated == 5  # all manufacturing slots
    for item in plan.items.values():
        if item.buildable and item.recommended_action in ("build", "both", "buy"):
            if item.runs_allocated < item.total_runs_needed:
                assert item.capacity_limited
                if item.type_id == ref.type_id("Hulk"):
                    assert item.recommended_buy_qty == 0
                    assert item.recommended_action == "build"
                else:
                    assert (
                        item.recommended_buy_qty
                        == (item.total_runs_needed - item.runs_allocated)
                        * item.portion_size
                    )


def test_finals_never_flip_to_buy_under_contention(conn, ref):
    """Even a NEGATIVE-margin final under heavy slot contention takes its
    slots first and keeps its shortfall as a flagged unmet build — a plan
    must never tell the user to market-buy their own product
    (decision 2026-08-21)."""
    add_pipeline(conn, ref, "Hulk", 8)
    snap = rich_snapshot(
        ref, slots=3, overrides={ref.type_id("Hulk"): 1.0}
    )
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.jobs_allocated == 3  # pre-allocated ahead of the MILP
    assert hulk.recommended_action == "build"
    assert hulk.recommended_buy_qty == 0
    assert hulk.capacity_limited  # the shortfall is honestly flagged


def test_reactions_saturate_full_window(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    reactions = [
        i
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
        and i.jobs_allocated > 0
        and ref.type_info(i.type_id).group_id
        not in config.NON_SATURATING_REACTION_GROUPS
    ]
    assert reactions
    for item in reactions:
        # A reaction slot always runs the full cycle window
        assert item.runs_allocated == item.jobs_allocated * item.max_runs_per_job
        assert item.recommended_build_qty >= item.deficit_qty


def test_polymer_reactions_size_to_deficit(conn, ref):
    """Hybrid polymers and molecular-forged materials do NOT saturate —
    they build only what the stockpile needs, like manufactured items."""
    add_pipeline(conn, ref, "Tengu", 3)  # T3 chain pulls hybrid polymers
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    polymers = [
        i
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
        and ref.type_info(i.type_id).group_id
        in config.NON_SATURATING_REACTION_GROUPS
        and i.jobs_allocated > 0
    ]
    assert polymers, "expected hybrid polymer reactions in a T3 chain"
    for item in polymers:
        # Uniform per-job rounding, but never window saturation:
        # allocated runs stay within one job of the actual need.
        assert item.runs_allocated >= item.total_runs_needed
        assert item.runs_allocated - item.total_runs_needed < item.jobs_allocated


def test_composite_inputs_get_extra_buffer(conn, ref):
    import math

    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    # Composite reactions consume moon materials; those inputs' targets must
    # exceed plain merged_min x (1 + buffer).
    buffer_mult = 1 + store.get_settings(conn).stockpile_buffer
    boosted = [
        i
        for i in plan.items.values()
        if i.target_stock_qty > math.ceil(i.merged_min_qty * buffer_mult)
    ]
    assert boosted


def test_persistence_roundtrip(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=True)
    assert plan.index_run_id is not None
    n_items = conn.execute(
        "SELECT COUNT(*) AS n FROM index_run_item WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchone()["n"]
    assert n_items == len(plan.items)
    n_shares = conn.execute(
        "SELECT COUNT(*) AS n FROM index_run_item_pipeline"
    ).fetchone()["n"]
    assert n_shares == sum(len(i.pipeline_share) for i in plan.items.values())


def test_capitals_build_exact_quantities(conn, ref):
    """Capitals never round up to the ship batch multiple."""
    add_pipeline(conn, ref, "Revelation", 3)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    rev = plan.items[ref.type_id("Revelation")]
    # exact final count (no buffer), NOT rounded up to 8
    assert rev.total_runs_needed == 3


def test_bpc_runs_cap_slots_and_rounding(conn, ref):
    """Runs-per-BPC caps runs per job (slot math) and becomes the batch
    rounding unit for subcap ships (whole blueprint copies only). The
    window is widened so the BPC cap is what binds — at 24h an Ishtar run
    (~31h) already gave 1 run/job and the cap arm could never bite."""
    conn.execute("UPDATE settings SET max_run_duration_hours = 2000")
    conn.commit()
    add_pipeline(conn, ref, "Ishtar", 45, runs_per_bpc=10)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    ishtar = plan.items[ref.type_id("Ishtar")]
    # exact target 45 runs -> rounded to whole 10-run BPCs = 50
    assert ishtar.total_runs_needed == 50
    # window fits 64 runs; the copy licenses 10 -> 5 whole-BPC jobs
    assert ishtar.max_runs_per_job == 10
    assert ishtar.jobs_needed_unconstrained == 5
    assert ishtar.recommended_build_qty == 50


def test_bpc_cap_none_keeps_global_batching(conn, ref):
    add_pipeline(conn, ref, "Ishtar", 45)  # no BPC cap
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    ishtar = plan.items[ref.type_id("Ishtar")]
    assert ishtar.total_runs_needed == 48  # exact 45 -> ceil(45/8)*8


def test_manufacturing_jobs_capped_at_30_days_modified_time(conn, ref):
    """A manufacturing job accepts as many runs as fit 30 days of MODIFIED
    time (user-verified in client 2026-08-21; the SDE's maxProductionLimit
    is a copy-runs concept and does NOT cap manufacturing). Capital
    Propulsion Engine at NPC defaults: 16,000s x 0.80 (TE20) x 0.68
    (skills) = 8,704 s/run -> ceil(2,592,000 / 8,704) = 298 runs in a
    window larger than 30 days (last-run overhang). Reaction formulas keep their own verified
    cap arm (Meta-Operant Neurolink Enhancer: 100 < the flat 544)."""
    conn.execute("UPDATE settings SET max_run_duration_hours = 2000")
    conn.commit()
    add_pipeline(conn, ref, "Revelation", 3)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    engine_part = plan.items[ref.type_id("Capital Propulsion Engine")]
    assert engine_part.time_per_run == pytest.approx(8_704.0)
    # ceil(2,592,000 / 8,704) = 298: 297 runs sit at 29d 21:48 -> the
    # 298th still installs (last-run overhang)
    assert engine_part.max_runs_per_job == 298
    enhancer = plan.items[ref.type_id("Meta-Operant Neurolink Enhancer")]
    assert enhancer.max_runs_per_job == 100


def test_all_jobs_of_an_item_run_uniform_counts(conn, ref):
    """Runs are rounded UP so every job of an item is identical — no short
    last job, slight overbuild allowed."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    for item in plan.items.values():
        if item.jobs_allocated > 0:
            assert item.runs_allocated % item.jobs_allocated == 0
            per_job = item.runs_allocated // item.jobs_allocated
            assert per_job <= item.max_runs_per_job


def test_job_time_anchors_include_skill_multipliers(conn, ref):
    """Hand-computed time anchors: deleting the skill factor from
    _size_jobs used to leave all 72 planning tests green while shifting
    ~190 output fields. Hulk at all-V defaults: 240,000 x 0.80 (TE20) x
    0.80 (Industry V) x 0.85 (Adv Industry V) x 0.95 x 0.95 (Exhumers-line
    science skills) = 117,830.4s. A composite reaction: 10,800 x 0.80
    (Reactions V) = 8,640 s/run -> 10 runs in the 24h window."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.time_per_run == pytest.approx(117_830.4)
    alloy = plan.items[ref.type_id("Crystallite Alloy")]
    assert alloy.time_per_run == pytest.approx(8_640.0)
    assert alloy.max_runs_per_job == 10


def test_finals_never_overbuild(conn, ref):
    """16 requested Hulks at 3 runs/job must build exactly 16 (6 jobs, the
    last two short), not 18 — the uniform round-up applies to
    intermediates only, since finals ignore stock and the extra hulls
    would never net off."""
    conn.execute("UPDATE settings SET max_run_duration_hours = 120")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 16)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.max_runs_per_job >= 2  # window fits multiple builds
    assert hulk.total_runs_needed == 16
    assert hulk.runs_allocated == 16
    assert hulk.recommended_build_qty == 16


def test_reaction_jobs_capped_at_30_days_modified_time(conn, ref):
    """Even with a huge cycle window, a reaction job holds the in-game
    30-days-of-modified-time ceiling with the last-run overhang
    (user-verified 2026-08-21): at NPC test defaults a 10,800s formula runs
    8,640s/run with Reactions V -> ceil(2,592,000 / 8,640) = 300 runs.
    The formula's own maxProductionLimit still applies where lower."""
    conn.execute("UPDATE settings SET max_run_duration_hours = 2000")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    reactions = [
        i
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION and i.jobs_allocated > 0
    ]
    assert reactions
    for item in reactions:
        blueprint = ref.blueprint_for_product(item.type_id)
        if blueprint.base_time == 10_800:
            assert item.max_runs_per_job == min(
                300, blueprint.max_runs or 300
            )
        if blueprint.max_runs:
            assert item.max_runs_per_job <= blueprint.max_runs


# --- Production blacklist --------------------------------------------------


def test_blacklist_category_buys_instead_of_builds(conn, ref):
    conn.execute(
        "INSERT INTO blacklist_category VALUES ('capital_components')"
    )
    conn.commit()
    add_pipeline(conn, ref, "Revelation", 3)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    cpe = plan.items[ref.type_id("Capital Propulsion Engine")]
    assert not cpe.buildable
    assert cpe.recommended_action == "buy"
    assert cpe.recommended_buy_qty > 0
    # Its sub-chain no longer generates demand: no capital-component jobs
    assert not any(
        i.jobs_allocated
        for i in plan.items.values()
        if ref.type_info(i.type_id).group_id == 873
    )


def test_blacklist_t1_hulls_spares_final_products(conn, ref):
    conn.execute("INSERT INTO blacklist_category VALUES ('t1_hulls')")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)       # needs Covetor hulls
    add_pipeline(conn, ref, "Retriever", 8)  # a T1 final itself
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    covetor = plan.items[ref.type_id("Covetor")]
    assert not covetor.buildable  # intermediate T1 hull -> buy
    assert covetor.recommended_action == "buy"
    retriever = plan.items[ref.type_id("Retriever")]
    assert retriever.buildable  # final products always build
    assert retriever.recommended_build_qty > 0


def test_blacklist_final_exemption_is_order_independent(conn, ref):
    """A pipeline final that another (earlier-id) pipeline consumes as a
    blacklisted intermediate must stay buildable — the exemption spans all
    active pipelines, not just the one being expanded."""
    conn.execute("INSERT INTO blacklist_category VALUES ('t1_hulls')")
    conn.commit()
    # Hulk gets the lower pipeline_id, so it expands first and reaches
    # Covetor as a blacklisted T1-hull intermediate before the Covetor
    # pipeline declares it a final.
    add_pipeline(conn, ref, "Hulk", 8)
    add_pipeline(conn, ref, "Covetor", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    covetor = plan.items[ref.type_id("Covetor")]
    assert covetor.buildable
    assert covetor.recommended_action == "build"
    assert covetor.recommended_build_qty >= 16  # 8 finals + Hulk's 8


def test_blacklist_individual_item(conn, ref):
    carbonide = ref.type_id("Crystalline Carbonide")
    conn.execute("INSERT INTO blacklist_item VALUES (?)", (carbonide,))
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    item = plan.items[carbonide]
    assert not item.buildable
    assert item.recommended_action == "buy"


# --- Cost lots (Phase 8) ---------------------------------------------------


def test_fifo_vintage_costing(conn, ref):
    trit = ref.type_id("Tritanium")
    pye = ref.type_id("Pyerite")
    # Two purchase lots at different vintage prices
    engine.record_purchase(conn, None, trit, 100, 4.0)
    engine.record_purchase(conn, None, trit, 100, 6.0)
    engine.record_purchase(conn, None, pye, 50, 10.0)
    # Job consumes 150 trit (100@4 + 50@6) + 50 pye@10 + 100 install fee
    lot = engine.complete_job(
        conn, None, ref.type_id("Ion Thruster"), 10, [(trit, 150), (pye, 50)], 100.0
    )
    row = conn.execute(
        "SELECT * FROM cost_lot WHERE lot_id = ?", (lot,)
    ).fetchone()
    expected_total = 100 * 4.0 + 50 * 6.0 + 50 * 10.0 + 100.0
    assert row["unit_cost"] == pytest.approx(expected_total / 10)
    # FIFO drew the cheap lot dry first
    first = conn.execute(
        "SELECT quantity_remaining FROM cost_lot WHERE unit_cost = 4.0"
    ).fetchone()
    assert first["quantity_remaining"] == 0


def test_finished_batch_profit(conn, ref):
    trit = ref.type_id("Tritanium")
    engine.record_purchase(conn, None, trit, 1000, 5.0)
    lot = engine.complete_job(
        conn, None, ref.type_id("Hulk"), 2, [(trit, 1000)], 0.0
    )
    pipeline_id = add_pipeline(conn, ref, "Hulk", 8)
    batch_id = engine.record_finished_batch(conn, pipeline_id, None, lot, 2, 5000.0)
    row = conn.execute(
        "SELECT * FROM finished_batch WHERE finished_batch_id = ?", (batch_id,)
    ).fetchone()
    assert row["total_cost_basis"] == pytest.approx(5000.0)  # 1000 x 5
    assert row["market_value_at_completion"] == pytest.approx(10000.0)
    assert row["profit"] == pytest.approx(5000.0)


def test_low_stock_suppressed_until_primed(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    snap = rich_snapshot(ref, slots=0)  # nothing can build -> everything short
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert not any(i.low_stock for i in plan.items.values())
    # Prime the pipeline by executing a run (the v1.5 truth signal), then
    # flags may fire.
    primed_plan = engine.plan_index_run(conn, ref, snap, persist=True)
    conn.execute(
        "UPDATE index_run SET status = 'complete' WHERE index_run_id = ?",
        (primed_plan.index_run_id,),
    )
    conn.commit()
    snap2 = Snapshot(
        slots_available={config.ACTIVITY_MANUFACTURING: 0, config.ACTIVITY_REACTION: 0}
    )
    plan2 = engine.plan_index_run(conn, ref, snap2, persist=False)
    assert any(i.low_stock for i in plan2.items.values())


def test_thukker_class_setting_flows_into_planning(conn, ref):
    """Asserting the Thukker tier on basic_capital_components (lowsec)
    must shrink capital-component material demand versus a T2 rig
    (-3.7 x 1.9 beats -2.4 x 1.9) — proving the product group threads
    through bom/engine to the rig math."""
    conn.execute(
        "UPDATE class_setting SET structure_type_id = ?, security = 0.25, "
        "me_rig = 'thukker' WHERE item_class = 'basic_capital_components'",
        (config.STRUCTURE_TYPE_AZBEL,),
    )
    conn.commit()
    add_pipeline(conn, ref, "Revelation", 1)
    thukker = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    conn.execute(
        "UPDATE class_setting SET me_rig = 't2' "
        "WHERE item_class = 'basic_capital_components'"
    )
    conn.commit()
    t2 = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)

    def demand(plan, name):
        return plan.items[ref.type_id(name)].merged_min_qty

    # Minerals feed the group-873 components: strictly less under Thukker
    # (-3.7 x 1.9 beats -2.4 x 1.9; exact multiplier anchors live in
    # test_industry). Everything below the components shrinks with them,
    # so no unchanged-control assert is possible inside this chain.
    assert demand(thukker, "Tritanium") < demand(t2, "Tritanium")
    # The final itself is unaffected by component ME.
    assert thukker.items[ref.type_id("Revelation")].total_runs_needed == 1


# --- Vertically-integrated build savings (2026-08-21) ----------------------


def test_chain_savings_matches_profit_page_model(conn, ref):
    """Where both models build everything (fair-value prices, no freight,
    no BPC cost), the savings chain cost must equal the Profit page's
    what-if cost — two independent walks validating each other."""
    pid = add_pipeline(conn, ref, "Hulk", 1)
    snap = rich_snapshot(ref)
    chain, _buy_cost = engine._chain_coster(conn, ref, snap)
    hulk = ref.type_id("Hulk")
    chain_cost, unpriced = chain(hulk)
    assert unpriced == 0
    pipeline = conn.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    from magoo import costing, store as store_mod

    whatif = costing.current_hull_cost(
        conn, ref, store_mod.get_settings(conn), pipeline,
        snap.prices, snap.prices,
    )
    assert chain_cost == pytest.approx(whatif.total, rel=1e-9)
    # and the persisted savings figure uses it
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[hulk]
    # savings = LANDED buy price − chain cost (2026-08-23); the fixture's
    # courier rate is 0, so landed == raw here — assert via the buy leg so
    # the test pins the formula, not the coincidence.
    _chain, buy_cost = engine._chain_coster(conn, ref, snap)
    assert buy_cost(hulk) == pytest.approx(snap.prices.get(hulk))
    assert item.build_savings_per_unit == pytest.approx(
        buy_cost(hulk) - chain_cost, rel=1e-9
    )
    assert item.unit_chain_cost == pytest.approx(chain_cost, rel=1e-9)
    assert item.build_savings_per_unit > 0  # integrated line is profitable


def test_chain_cost_buys_a_stage_the_market_undercuts(conn, ref):
    """min(buy, build) at every edge: a component sold below its own build
    cost is bought in the parent's chain figure, RAISING the parent's
    savings versus the build-everything model."""
    add_pipeline(conn, ref, "Hulk", 1)
    hulk = ref.type_id("Hulk")
    baseline = engine._chain_coster(conn, ref, rich_snapshot(ref))[0](hulk)[0]
    cheap = engine._chain_coster(
        conn, ref,
        rich_snapshot(ref, overrides={ref.type_id("Fusion Reactor Unit"): 1.0}),
    )[0](hulk)[0]
    assert cheap < baseline


def test_chain_cost_includes_freight_in_and_bpc(conn, ref):
    """Bought units carry inbound freight; finals amortize their BPC cost."""
    pid = add_pipeline(conn, ref, "Hulk", 1)
    hulk = ref.type_id("Hulk")
    base = engine._chain_coster(conn, ref, rich_snapshot(ref))[0](hulk)[0]
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 100.0")
    conn.commit()
    with_freight = engine._chain_coster(conn, ref, rich_snapshot(ref))[0](hulk)[0]
    assert with_freight > base
    conn.execute(
        "UPDATE pipeline SET bpc_cost_isk = 1e9, runs_per_bpc = 10 "
        "WHERE pipeline_id = ?",
        (pid,),
    )
    conn.commit()
    with_bpc = engine._chain_coster(conn, ref, rich_snapshot(ref))[0](hulk)[0]
    assert with_bpc == pytest.approx(with_freight + 1e8)


def test_chain_savings_counts_unpriced_raw_leaves(conn, ref):
    """A raw leaf with no price costs 0 in the figure and is counted so
    the UI can badge the savings as understated."""
    add_pipeline(conn, ref, "Hulk", 1)
    snap = rich_snapshot(ref, overrides={ref.type_id("Tritanium"): None})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.build_savings_per_unit is not None
    assert hulk.savings_unpriced_inputs > 0


# --- Audit 2026-08-27 regressions -------------------------------------------


def test_consumption_counts_short_last_job_runs(conn, ref):
    """Finals skip the uniform round-up, so their runs need not divide
    evenly across jobs — 16 Hulks at 3 runs/job install 16 runs over 6
    jobs. The old floor split charged 6 x floor(16/6) = 12 runs of every
    material; every run must be charged."""
    conn.execute("UPDATE settings SET max_run_duration_hours = 120")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 16)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.runs_allocated == 16
    assert hulk.runs_allocated % hulk.jobs_allocated != 0  # short last jobs
    draw = engine._planned_consumption(conn, ref, plan.items)
    # Covetor is consumed only by the Hulk blueprint, 1 per run: the draw
    # must cover all 16 runs (the floor split counted 12).
    assert draw[ref.type_id("Covetor")] == 16


def test_dual_role_final_nets_component_share_against_stock(conn, ref):
    """A final consumed as another pipeline's intermediate (ruling
    2026-08-27): the requested share always builds, the cross-pipeline
    component share nets against stock like any other stage."""
    add_pipeline(conn, ref, "Covetor", 5)
    add_pipeline(conn, ref, "Hulk", 8)  # consumes 1 Covetor per run
    covetor_id = ref.type_id("Covetor")

    bare = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    assert bare.items[covetor_id].merged_min_qty == 13
    assert bare.items[covetor_id].deficit_qty == 13  # no stock: full demand

    snap = rich_snapshot(ref)
    snap.on_hand[covetor_id] = 20
    stocked = engine.plan_index_run(conn, ref, snap, persist=False)
    # Stock covers the 8-unit component share; the requested 5 still build.
    assert stocked.items[covetor_id].deficit_qty == 5

    snap = rich_snapshot(ref)
    snap.on_hand[covetor_id] = 3
    partial = engine.plan_index_run(conn, ref, snap, persist=False)
    assert partial.items[covetor_id].deficit_qty == 5 + (8 - 3)


def test_buffered_target_guards_float_noise(conn, ref):
    """ceil(100 * 1.1) must be 110, not 111 — binary-float noise
    (110.00000000000001) is rounded away before the ceil."""
    conn.execute("UPDATE settings SET stockpile_buffer = 0.10")
    conn.commit()
    trit = ref.type_id("Tritanium")
    merged = {
        trit: engine.PlanItem(
            type_id=trit,
            name="Tritanium",
            item_class="other",
            depth=1,
            merged_min_qty=100,
        )
    }
    engine._apply_targets(conn, ref, merged, rich_snapshot(ref))
    assert merged[trit].target_stock_qty == 110
