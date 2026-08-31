"""Steady-state Planning tab: engine.plan_steady_state (the two-pass
restock-and-replan) plus the /planning context builders and template.
Same fixture pattern as test_engine."""

import sqlite3

import pytest

from magoo import config, engine, store
from magoo.engine import Snapshot
from conftest import FairValuePrices, template_app


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


def base_snapshot(ref, mfg=500, reaction=500, overrides=None):
    """Prices and slot pools only — plan_steady_state discards stock,
    jobs and wallets, so nothing else matters."""
    prices = FairValuePrices(ref, overrides=overrides)
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: mfg,
            config.ACTIVITY_REACTION: reaction,
        },
        prices=prices,
        adjusted_prices=prices,
    )


def _decisions(plan):
    return {
        i.type_id: (
            i.deficit_qty,
            i.recommended_build_qty,
            i.recommended_buy_qty,
            i.jobs_allocated,
        )
        for i in plan.items.values()
    }


# ---------------------------------------------------------------------------
# engine.plan_steady_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "qty,built",
    [
        (8, 8),  # already a batch multiple
        (6, 8),  # ship_batch_multiple rounds the line's output up
    ],
)
def test_finals_build_the_batch_rounded_line_output(conn, ref, qty, built):
    """Steady state is defined on what the line PRODUCES: batching builds
    8 Hulks per cycle whether 6 or 8 were requested, and the whole chain
    scales to the built quantity."""
    add_pipeline(conn, ref, "Hulk", qty)
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    hulk = plan.items[ref.type_id("Hulk")]
    assert hulk.recommended_build_qty == built
    assert hulk.deficit_qty == built
    # Finals are seeded at ZERO stock in the steady draft (2026-08-28):
    # from empty their dual-role netting is a no-op, so the component
    # share another pipeline consumes is still fully replaced.
    assert hulk.target_stock_qty == built
    assert hulk.on_hand_qty == 0


@pytest.mark.parametrize(
    "qty,runs_per_bpc",
    [
        (8, None),  # no rounding
        (6, None),  # ship batch multiple rounds 6 -> 8
        (8, 5),  # BPC run cap rounds 8 -> 10
    ],
)
def test_intermediates_replace_one_cycle(conn, ref, qty, runs_per_bpc):
    """Every buildable stage sits at target and installs one cycle's
    replacement AT THE BUILT SCALE — batch/BPC rounding must not leave
    deep stages replacing less than the cycle consumes. Raw buys equal
    the cycle's consumption exactly (the purchase margin is carried as
    stock, not re-bought)."""
    add_pipeline(conn, ref, "Hulk", qty, runs_per_bpc)
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    hulk_id = ref.type_id("Hulk")
    draw = engine._planned_consumption(conn, ref, plan.items)
    saw_intermediate = saw_raw = False
    for item in plan.items.values():
        if item.buildable:
            assert not item.capacity_limited, item.name
            if item.type_id != hulk_id:
                # Intermediates sit at target; the final is seeded at
                # zero (2026-08-28) so dual-role netting stays a no-op.
                assert item.on_hand_qty == item.target_stock_qty, item.name
                saw_intermediate = True
                # One cycle's replacement at least (the feedback pass may
                # raise the deficit to the allocation's actual draw).
                assert item.deficit_qty >= item.merged_min_qty, item.name
                # The stage ends the cycle back at (or above) coverage.
                projected = (
                    item.on_hand_qty
                    + item.recommended_build_qty
                    + item.recommended_buy_qty
                    + item.alchemy_output_qty
                    - draw.get(item.type_id, 0)
                )
                assert projected >= item.merged_min_qty, item.name
        elif draw.get(item.type_id, 0) > 0:
            saw_raw = True
            assert item.recommended_buy_qty == draw[item.type_id], item.name
            assert item.recommended_buy_qty == item.deficit_qty > 0, item.name
    assert saw_intermediate and saw_raw


def test_input_margin_not_rebought_every_cycle(conn, ref):
    """Phase 7 targets raws at consumption × (1 + margin); a perpetual
    cycle buys the margin excess once and carries it, so the steady buy
    list is exactly one cycle's consumption."""
    add_pipeline(conn, ref, "Hulk", 8)
    conn.execute("UPDATE settings SET input_purchase_margin = 0.10")
    conn.commit()
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    draw = engine._planned_consumption(conn, ref, plan.items)
    trit = plan.items[ref.type_id("Tritanium")]
    assert trit.recommended_buy_qty == draw[ref.type_id("Tritanium")] > 0
    # The 10% excess sits on hand, not on the shopping list.
    assert trit.on_hand_qty == trit.target_stock_qty - trit.recommended_buy_qty
    assert trit.on_hand_qty > 0


def test_buffer_cancels_out_of_replacement_work(conn, ref):
    """The stockpile buffer moves targets, not replacement work: at steady
    state on-hand equals target, so the buffer term cancels out of every
    deficit and the plan's decisions are identical."""
    add_pipeline(conn, ref, "Hulk", 8)
    low = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    conn.execute("UPDATE settings SET stockpile_buffer = 0.1")
    conn.commit()
    high = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    assert _decisions(low) == _decisions(high)
    # Sanity: the knob did move the targets themselves.
    assert any(
        high.items[t].target_stock_qty > low.items[t].target_stock_qty
        for t, i in low.items.items()
        if i.buildable and t != ref.type_id("Hulk")
    )
    # Monotonicity, EVERY item: raising the safety knob may never lower
    # any stockpile target anywhere in the chain.
    assert set(high.items) == set(low.items)
    for type_id, item in low.items.items():
        assert (
            high.items[type_id].target_stock_qty >= item.target_stock_qty
        ), item.name


def test_buffer_monotonic_targets_on_the_real_plan_path(conn, ref):
    """Twin of the steady-state monotonicity property on the REAL plan
    path (engine.plan_index_run, empty stock): buffer 0.05 -> 0.10 may
    never lower any item's target_stock_qty across the whole chain —
    buildables scale by the buffer multiplier directly, and raw targets
    follow the (weakly larger) consumption of the allocated jobs."""
    add_pipeline(conn, ref, "Hulk", 8)
    low = engine.plan_index_run(conn, ref, base_snapshot(ref), persist=False)
    conn.execute("UPDATE settings SET stockpile_buffer = 0.1")
    conn.commit()
    high = engine.plan_index_run(conn, ref, base_snapshot(ref), persist=False)
    assert set(high.items) == set(low.items)  # same chain either way
    rose = 0
    for type_id, item in low.items.items():
        assert (
            high.items[type_id].target_stock_qty >= item.target_stock_qty
        ), item.name
        rose += high.items[type_id].target_stock_qty > item.target_stock_qty
    assert rose  # sanity: the knob moved targets, the comparison is live


def test_nothing_persists(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    engine.plan_steady_state(conn, ref, base_snapshot(ref))
    for table in (
        "index_run",
        "index_run_item",
        "index_run_item_pipeline",
        "cost_lot",
    ):
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        assert count["n"] == 0, table


def test_caller_stock_and_jobs_ignored(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    clean = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    noisy_snapshot = base_snapshot(ref)
    noisy_snapshot.on_hand[ref.type_id("Tritanium")] = 10**10
    noisy_snapshot.on_hand[ref.type_id("Hulk")] = 500
    noisy_snapshot.in_progress[ref.type_id("Pyerite")] = 10**9
    noisy = engine.plan_steady_state(conn, ref, noisy_snapshot)
    assert _decisions(clean) == _decisions(noisy)


def test_pool_pressure_sets_capacity_flags(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref, mfg=2))
    starved = [
        i
        for i in plan.items.values()
        if i.capacity_limited and i.activity_id == config.ACTIVITY_MANUFACTURING
    ]
    assert starved
    assert any(i.jobs_needed_unconstrained > i.jobs_allocated for i in starved)


# --- Alchemy at steady state (test_alchemy's enablement idiom) --------------


def enable_alchemy(conn, yield_=0.55, cap=50):
    conn.execute(
        "UPDATE settings SET alchemy_enabled = 1, "
        "alchemy_reprocess_yield = ?, max_alchemy_jobs_per_type = ?",
        (yield_, cap),
    )
    conn.commit()


def expensive_rare_inputs(ref, plan):
    """Price overrides making every routed composite's rare inputs (direct
    formula inputs the alchemy formula does not share) cost a fortune, so
    the substitution pass swaps."""
    routes = ref.alchemy_routes()
    overrides = {}
    for item in plan.items.values():
        if (
            item.activity_id != config.ACTIVITY_REACTION
            or item.jobs_allocated <= 0
            or item.type_id not in routes
        ):
            continue
        route = routes[item.type_id]
        direct = {
            m for m, _q in ref.materials(item.blueprint_id, item.activity_id)
        }
        alchemy = {
            m
            for m, _q in ref.materials(
                route.formula.blueprint_id, route.formula.activity_id
            )
        }
        for rare in direct - alchemy:
            overrides[rare] = 1e6
    return overrides


def test_alchemy_stays_out_of_the_steady_plan(conn, ref):
    """Decision 2026-08-24: alchemy is assumed OFF for planning — the
    steady cycle runs direct reactions only, even when the setting is on
    and prices favor substitution. The same snapshot on the real plan
    path still substitutes, proving the exclusion is planning-only."""
    add_pipeline(conn, ref, "Hulk", 8)
    enable_alchemy(conn)
    probe = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    assert not any(
        i.alchemy_for_type_id is not None for i in probe.items.values()
    )
    overrides = expensive_rare_inputs(
        ref, engine.plan_index_run(conn, ref, base_snapshot(ref), persist=False)
    )
    assert overrides
    snapshot = base_snapshot(ref, overrides=overrides)
    steady = engine.plan_steady_state(conn, ref, snapshot)
    assert not any(
        i.alchemy_for_type_id is not None for i in steady.items.values()
    )
    real = engine.plan_index_run(conn, ref, snapshot, persist=False)
    assert any(
        i.alchemy_for_type_id is not None and i.recommended_build_qty > 0
        for i in real.items.values()
    )


def test_unrefined_final_gets_no_phantom_credits(conn, ref):
    """Even a pipeline whose FINAL is an unrefined alchemy item must not
    fake reprocess credits into the steady plan: stocking it at target
    would otherwise credit its reprocess outputs and hide a full cycle's
    raw buys. The steady plan is identical whatever the global alchemy
    setting says."""
    add_pipeline(conn, ref, "Unrefined Vanadium Hafnite", 4000)
    off = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    enable_alchemy(conn)
    on = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    assert _decisions(off) == _decisions(on)
    assert not any(i.alchemy_credit_qty for i in on.items.values())
    vanadium = on.items[ref.type_id("Vanadium")]
    assert vanadium.recommended_buy_qty > 0


# ---------------------------------------------------------------------------
# web context builders
# ---------------------------------------------------------------------------


def _ample_settings(conn):
    conn.execute(
        "UPDATE settings SET manufacturing_slots = 500, reaction_slots = 500"
    )
    conn.commit()
    return store.get_settings(conn)


def test_steady_rows_join_names_and_order(conn, ref):
    from magoo import web

    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    rows = web._steady_rows(ref, plan)
    assert len(rows) == len(plan.items)
    assert all(row["category"] for row in rows)  # every EVE group resolved
    assert [
        (row["depth"], row["name"]) for row in rows
    ] == sorted((row["depth"], row["name"]) for row in rows)
    hulk = next(row for row in rows if row["name"] == "Hulk")
    info = ref.type_info(ref.type_id("Hulk"))
    assert hulk["group_id"] == info.group_id
    assert hulk["category_id"] == info.category_id
    assert hulk["activity_id"] == config.ACTIVITY_MANUFACTURING
    assert hulk["recommended_build_qty"] == 8


def test_planning_context_totals_match_the_plan(conn, ref):
    from magoo import web

    add_pipeline(conn, ref, "Hulk", 8)
    settings_ = _ample_settings(conn)
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    ctx = web._planning_context(ref, plan, settings_)
    # Pool is ample: uncapped demand equals the allocation, nothing starves.
    mfg_allocated = sum(
        i.jobs_allocated
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_MANUFACTURING
        and i.recommended_build_qty > 0
    )
    assert ctx["mfg_demand"] == mfg_allocated > 0
    reaction_allocated = sum(
        i.jobs_allocated
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_REACTION
        and i.alchemy_for_type_id is None
        and i.recommended_build_qty > 0
    )
    assert ctx["reaction_demand"] == reaction_allocated > 0
    assert any(
        category == "Composite" for category, _ in ctx["reactions_grouped"]
    )
    assert ctx["capacity_rows"] == []
    assert ctx["buy_total"] == sum(
        i.recommended_buy_qty * i.price_snapshot
        for i in plan.items.values()
        if i.recommended_buy_qty > 0
    )
    assert ctx["buys_unpriced"] == 0
    assert ctx["reason"] is None
    assert ctx["settings"] is settings_


def test_planning_context_demand_is_uncapped_under_a_small_pool(conn, ref):
    """The strip's demand figures must show what the cycle NEEDS, not what
    the pool fit — including rows the pool squeezed to zero jobs."""
    from magoo import web

    add_pipeline(conn, ref, "Hulk", 8)
    conn.execute("UPDATE settings SET manufacturing_slots = 2")
    conn.commit()
    settings_ = store.get_settings(conn)
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref, mfg=2))
    ctx = web._planning_context(ref, plan, settings_)
    assert ctx["capacity_rows"]
    mfg_allocated = sum(
        i.jobs_allocated
        for i in plan.items.values()
        if i.activity_id == config.ACTIVITY_MANUFACTURING
        and i.recommended_build_qty > 0
    )
    assert ctx["mfg_demand"] > settings_.manufacturing_slots >= mfg_allocated
    # Starved rows with zero jobs still count at their unconstrained size.
    assert any(row["jobs_allocated"] == 0 for row in ctx["capacity_rows"])


def test_planning_context_renders_the_template(conn, ref):
    from flask import render_template

    from magoo import web

    add_pipeline(conn, ref, "Hulk", 8)
    settings_ = _ample_settings(conn)
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    ctx = web._planning_context(ref, plan, settings_)
    app = template_app()
    with app.test_request_context("/planning?view=slots"):
        html = render_template("planning_slots.html", **ctx)
    assert "Hulk" in html
    assert "Replacement materials" in html
    assert "Tritanium" in html
    # Alchemy is assumed off for planning — no trace of it.
    assert "Alchemy" not in html
    # Live analysis only — no run-page execution artifacts.
    assert "<textarea" not in html
    assert "Mark executed" not in html
    assert "wallet" not in html
    # The tab's own nav link, marked active for this request.
    assert 'class="active">Planning</a>' in html
    # The view subnav: Profit link, Slot Planner active.
    assert 'class="active">Slot Planner</a>' in html
    assert ">Profit</a>" in html


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------

_app = template_app


def settings_with(**over):
    return store.Settings(0.05, 24.0, 8, 1, 10000002, "sell", **over)


def _ctx(**over):
    ctx = dict(
        rows=[],
        reason=None,
        buys=[],
        buy_total=0.0,
        buys_unpriced=0,
        structure_buys=set(),
        shallow=set(),
        region_wide=set(),
        builds_grouped=[],
        reactions_grouped=[],
        struct_builds=[],
        struct_buys=[],
        struct_slots=0,
        capacity_rows=[],
        mfg_demand=0,
        reaction_demand=0,
        settings=settings_with(manufacturing_slots=28, reaction_slots=50),
    )
    ctx.update(over)
    return ctx


def _render(ctx):
    from flask import render_template

    app = _app()
    with app.test_request_context("/planning?view=slots"):
        return render_template("planning_slots.html", **ctx)


def test_template_over_pool_demand_shows_red():
    html = _render(_ctx(mfg_demand=34, reaction_demand=12))
    assert 'class="value bad"' in html
    assert ">34<" in html and "/ 28" in html
    # The reaction pool holds: no second red value.
    assert html.count('class="value bad"') == 1


def test_template_within_pool_is_calm():
    html = _render(_ctx(mfg_demand=20, reaction_demand=12))
    assert 'class="value bad"' not in html
    assert ">12<" in html and "/ 50" in html


def test_template_empty_state_shows_reason():
    html = _render(_ctx(rows=None, reason="no active pipelines — add one"))
    assert "no active pipelines" in html
    assert "Mfg slots" not in html


def test_template_buy_row_badges(ref):
    tid = ref.type_id("Pyerite")
    row = {
        "type_id": tid,
        "name": "Pyerite",
        "category": "Mineral",
        "recommended_buy_qty": 12,
        "price_snapshot": 1000.0,
        "capacity_limited": False,
        "structure_units_cheaper": 5,
        "jobs_needed_unconstrained": 0,
        "jobs_allocated": 0,
    }
    html = _render(
        _ctx(
            rows=[row],
            buys=[row],
            buy_total=12000.0,
            structure_buys={tid},
            shallow={tid},
        )
    )
    assert ">C-J6</span>" in html
    assert "shallow" in html


def test_template_capacity_panel_lists_starved_items():
    starved = {
        "name": "Capital Propulsion Engine",
        "jobs_needed_unconstrained": 9,
        "jobs_allocated": 4,
    }
    html = _render(_ctx(capacity_rows=[starved]))
    assert "over pool" in html
    assert "Capital Propulsion Engine" in html


def test_template_profit_view_renders_with_subnav():
    """The today's-prices what-if moved from the old top-level Profit page
    to Planning → Profit (its content tests live on in test_structures /
    test_buy_venue against planning_profit.html)."""
    from flask import render_template

    from magoo import costing

    app = _app()
    with app.test_request_context("/planning"):
        html = render_template(
            "planning_profit.html",
            cards=[],
            totals=costing.cycle_totals([]),
            prices_at=None,
            structure_prices_at=None,
            broker_rate=0.01,
            sales_tax=0.03,
            settings=settings_with(),
        )
    assert 'class="active">Profit</a>' in html
    assert ">Slot Planner</a>" in html
    assert "No active pipelines" in html
    assert 'class="active">Planning</a>' in html
    assert "at today's prices" in html


def test_steady_state_dual_role_final_replaces_component_share(conn, ref):
    """Steady state with a dual-role final (diff review 2026-08-28): the
    stocked-at-target draft must still replace the FULL component share
    another pipeline consumes each cycle. Seeding finals' stock at target
    would let the dual-role netting (ruling 2026-08-27) cancel the
    component share, draining the stockpile every cycle and understating
    the whole subtree's steady buy list — finals are seeded at zero."""
    add_pipeline(conn, ref, "Covetor", 5)
    add_pipeline(conn, ref, "Hulk", 8)  # consumes 1 Covetor per run
    plan = engine.plan_steady_state(conn, ref, base_snapshot(ref))
    covetor = plan.items[ref.type_id("Covetor")]
    # From zero seeded stock the netting is a no-op: the steady cycle
    # builds the requested share plus everything the Hulk jobs draw.
    assert covetor.deficit_qty == covetor.merged_min_qty
    draw = engine._planned_consumption(conn, ref, plan.items)
    consumed = draw.get(ref.type_id("Covetor"), 0)
    sold = covetor.requested_qty
    assert covetor.recommended_build_qty >= consumed + sold
