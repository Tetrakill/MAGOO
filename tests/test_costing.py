"""Lag-based costing (v1.5): fee snapshots, the completed-run walk with its
min(depth, history) clamp, and the sell-side rate formulas.

Same fixture style as test_engine: real reference data, temp state DB,
uniform synthetic prices — one price level per planned run so the lag is
visible in the numbers.
"""

import sqlite3

import pytest

from magoo import config, costing, engine, industry, market, store
from magoo.engine import Snapshot


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


def add_pipeline(conn, ref, name, qty, runs_per_bpc=None, bpc_cost=None):
    cur = conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run, runs_per_bpc, bpc_cost_isk) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, ref.type_id(name), qty, runs_per_bpc, bpc_cost),
    )
    conn.commit()
    return cur.lastrowid


def uniform_snapshot(price, slots=500):
    class UniformPrices(dict):
        def get(self, key, default=None):
            return price

    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=UniformPrices(),
        adjusted_prices=UniformPrices(),
    )


def plan_completed_run(conn, ref, price) -> int:
    """Plan + persist a run at a uniform price level and mark it executed."""
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(price))
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    return plan.index_run_id


# --- Sell-side rates -------------------------------------------------------


def test_broker_and_tax_rates():
    s = store.Settings(0.05, 24.0, 8, 1, 10000002, "sell")
    # All defaults: Broker Relations V, zero standings, Accounting V.
    assert costing.broker_fee_rate(s) == pytest.approx(0.03 - 0.015)
    assert costing.sales_tax_rate(s) == pytest.approx(0.075 * 0.45)


def test_broker_rate_standings_floor():
    s = store.Settings(
        0.05, 24.0, 8, 1, 10000002, "sell",
        standing_broker_faction=10.0, standing_broker_corp=10.0,
    )
    assert costing.broker_fee_rate(s) == pytest.approx(0.01)
    # 2026-08-23: NPC station is the only sell venue — no structure rate.
    assert not hasattr(s, "sell_venue") and not hasattr(s, "structure_broker_rate")


def test_net_proceeds():
    s = store.Settings(
        0.05, 24.0, 8, 1, 10000002, "sell", freight_out_isk_per_m3=100.0
    )
    # SCC surcharge applies to subcap sales too (decision 2026-08-20).
    rate = (
        1.0
        - costing.sales_tax_rate(s)
        - costing.broker_fee_rate(s)
        - s.capital_scc_surcharge
    )
    assert costing.net_proceeds_per_hull(1000.0, 10.0, s) == pytest.approx(
        1000.0 * rate - 1000.0
    )


# --- Snapshots at plan time ------------------------------------------------


def test_every_buildable_gets_fee_snapshot(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    run_id = plan_completed_run(conn, ref, 10.0)
    rows = conn.execute(
        "SELECT blueprint_id, unit_install_fee, price_snapshot "
        "FROM index_run_item WHERE index_run_id = ?",
        (run_id,),
    ).fetchall()
    assert rows
    for r in rows:
        assert r["price_snapshot"] == 10.0
        if r["blueprint_id"] is not None:
            # Default class settings: cost index 0, tax 0.25%, plus the SCC
            # surcharge — nonzero for anything with a nonzero EIV.
            assert r["unit_install_fee"] is not None
            assert r["unit_install_fee"] > 0


# --- The lag walk ----------------------------------------------------------


def test_hull_cost_lags_by_depth(conn, ref):
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    for price in (10.0, 20.0, 40.0):
        run_id = plan_completed_run(conn, ref, price)

    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert cost is not None
    assert cost.hulls_per_cycle == 8
    assert cost.spin_up  # chain depth 5 > 2 runs of lag available

    materials = [l for l in cost.lines if l.kind == "material"]
    assert materials
    for line in materials:
        expected_lag = min(line.depth, 2)
        assert line.lag_runs == expected_lag
        assert line.unit_cost == {0: 40.0, 1: 20.0, 2: 10.0}[expected_lag]
        assert line.clamped == (line.depth > 2)

    installs = [l for l in cost.lines if l.kind == "install"]
    assert installs
    hulk_line = next(l for l in installs if l.name == "Hulk")
    assert hulk_line.depth == 0 and hulk_line.lag_runs == 0
    assert hulk_line.qty_per_hull == 1.0
    # Adjusted prices doubled between run 1 and run 3; a lag-0 fee reflects
    # the newest snapshot, a clamped deep fee the oldest.
    deep = [l for l in installs if l.clamped]
    assert deep and all(l.lag_runs == 2 for l in deep)


def test_planned_runs_are_invisible(conn, ref):
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    first = plan_completed_run(conn, ref, 10.0)
    # An abandoned replan at a silly price level, never executed.
    engine.plan_index_run(conn, ref, uniform_snapshot(9999.0))
    last = plan_completed_run(conn, ref, 20.0)

    seq = costing.completed_sequence(conn)
    assert [r["index_run_id"] for r in seq] == [first, last]

    cost = costing.hull_cost(conn, ref, settings, last, pid)
    depth1 = [
        l for l in cost.lines if l.kind == "material" and l.depth == 1
    ]
    assert depth1 and all(l.unit_cost == 10.0 for l in depth1)
    # The abandoned plan itself can't be costed.
    abandoned = conn.execute(
        "SELECT index_run_id FROM index_run WHERE status = 'planned'"
    ).fetchone()["index_run_id"]
    assert costing.hull_cost(conn, ref, settings, abandoned, pid) is None


def test_single_run_clamps_everything_to_itself(conn, ref):
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    run_id = plan_completed_run(conn, ref, 10.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert cost.spin_up
    for line in cost.lines:
        if line.kind in ("material", "install"):
            assert line.lag_runs == 0
            assert line.clamped == (line.depth > 0)
    materials = [l for l in cost.lines if l.kind == "material"]
    assert all(l.unit_cost == 10.0 for l in materials)


# --- Freight and BPC lines -------------------------------------------------


def test_freight_in_and_bpc_lines(conn, ref):
    pid = add_pipeline(conn, ref, "Hulk", 8, runs_per_bpc=4, bpc_cost=40e6)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 500")
    conn.commit()
    settings = store.get_settings(conn)
    run_id = plan_completed_run(conn, ref, 10.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)

    freight = [l for l in cost.lines if l.kind == "freight_in"]
    assert len(freight) == 1 and freight[0].unit_cost == 500
    # m³ hauled per hull is the sum over bought inputs only.
    materials = [l for l in cost.lines if l.kind == "material"]
    expected_m3 = sum(
        l.qty_per_hull * ref.type_info(l.type_id).freight_volume
        for l in materials
    )
    assert freight[0].qty_per_hull == pytest.approx(expected_m3)

    bpc = [l for l in cost.lines if l.kind == "bpc"]
    assert len(bpc) == 1
    assert bpc[0].cost_per_hull == pytest.approx(40e6 / 4)


# --- Current-prices view ---------------------------------------------------


class UniformDict(dict):
    def __init__(self, value):
        self.value = value

    def get(self, key, default=None):
        return self.value


def get_pipeline(conn, pid):
    return conn.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()


def test_current_hull_cost_structure(conn, ref):
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    cost = costing.current_hull_cost(
        conn, ref, settings, get_pipeline(conn, pid),
        UniformDict(10.0), UniformDict(10.0),
    )
    assert cost.hulls_per_cycle == 8
    assert cost.index_run_id is None
    assert not cost.spin_up  # nothing lagged, nothing clamped

    materials = [l for l in cost.lines if l.kind == "material"]
    installs = [l for l in cost.lines if l.kind == "install"]
    assert materials and installs
    assert all(l.unit_cost == 10.0 for l in materials)
    assert all(l.unit_cost > 0 for l in installs)
    hulk = next(l for l in installs if l.name == "Hulk")
    assert hulk.depth == 0 and hulk.qty_per_hull == 1.0
    assert cost.total > 0
    # No rates set -> no freight or BPC lines.
    assert not [l for l in cost.lines if l.kind in ("freight_in", "bpc")]


def test_current_hull_cost_blacklist_buys_subchain(conn, ref):
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    pipeline = get_pipeline(conn, pid)
    baseline = costing.current_hull_cost(
        conn, ref, settings, pipeline, UniformDict(10.0), UniformDict(10.0)
    )
    # Blacklist any deep buildable from the chain itself.
    victim = next(
        l for l in baseline.lines if l.kind == "install" and l.depth >= 2
    )
    conn.execute("INSERT INTO blacklist_item VALUES (?)", (victim.type_id,))
    conn.commit()
    cost = costing.current_hull_cost(
        conn, ref, settings, pipeline, UniformDict(10.0), UniformDict(10.0)
    )
    materials = {l.type_id for l in cost.lines if l.kind == "material"}
    installs = {l.type_id for l in cost.lines if l.kind == "install"}
    assert victim.type_id in materials
    assert victim.type_id not in installs


def test_current_hull_cost_flags_missing_prices(conn, ref):
    """A pipeline change adds items with no cached price yet — they cost 0
    but must be flagged, never silently understated."""
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    cost = costing.current_hull_cost(
        conn, ref, settings, get_pipeline(conn, pid), {}, UniformDict(10.0)
    )
    materials = [l for l in cost.lines if l.kind == "material"]
    assert materials
    assert all(l.missing_price and l.unit_cost == 0.0 for l in materials)
    assert cost.missing_prices == len(materials)


def test_packaged_volume_is_hauled_volume(ref):
    hulk = ref.type_info(ref.type_id("Hulk"))
    assert hulk.packaged_volume == 3750.0
    assert hulk.freight_volume == 3750.0
    trit = ref.type_info(ref.type_id("Tritanium"))
    assert trit.freight_volume == 0.01


# --- Lag walk: deeper histories and sequence edits --------------------------


def test_full_history_exact_lags_no_spinup(conn, ref):
    """With more executed runs than the chain is deep, every line gets its
    TRUE lag: depth k priced from exactly k runs back, nothing clamped."""
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    for price in prices:
        run_id = plan_completed_run(conn, ref, price)

    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert not cost.spin_up
    for line in cost.lines:
        if line.kind not in ("material", "install"):
            continue
        assert not line.clamped
        assert line.lag_runs == line.depth
        if line.kind == "material":
            # position = last(6) - depth  ->  price from that run
            assert line.unit_cost == prices[len(prices) - 1 - line.depth]


def test_reopening_a_run_rewires_the_walk(conn, ref):
    """Reopening a mid-sequence run removes it from the timeline: lags walk
    across the gap as if it never executed."""
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    first = plan_completed_run(conn, ref, 10.0)
    middle = plan_completed_run(conn, ref, 20.0)
    last = plan_completed_run(conn, ref, 40.0)

    depth1_price = lambda cost: next(
        l.unit_cost
        for l in cost.lines
        if l.kind == "material" and l.depth == 1
    )
    assert depth1_price(
        costing.hull_cost(conn, ref, settings, last, pid)
    ) == 20.0

    conn.execute(
        "UPDATE index_run SET status = 'planned', completed_at = NULL "
        "WHERE index_run_id = ?",
        (middle,),
    )
    conn.commit()
    assert [r["index_run_id"] for r in costing.completed_sequence(conn)] == [
        first,
        last,
    ]
    assert depth1_price(
        costing.hull_cost(conn, ref, settings, last, pid)
    ) == 10.0
    # The reopened run itself is no longer costable.
    assert costing.hull_cost(conn, ref, settings, middle, pid) is None


def test_install_fees_lag_like_materials(conn, ref):
    """A depth-1 stage's fee comes from the PREVIOUS run's snapshot — the
    run its job was installed at — not from the run being costed."""
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    first = plan_completed_run(conn, ref, 10.0)
    last = plan_completed_run(conn, ref, 20.0)  # adjusted prices doubled

    cost = costing.hull_cost(conn, ref, settings, last, pid)
    fees_run1 = {
        row["type_id"]: row["unit_install_fee"]
        for row in conn.execute(
            "SELECT type_id, unit_install_fee FROM index_run_item "
            "WHERE index_run_id = ?",
            (first,),
        )
    }
    depth1 = [l for l in cost.lines if l.kind == "install" and l.depth == 1]
    assert depth1
    for line in depth1:
        assert line.lag_runs == 1
        assert line.unit_cost == pytest.approx(fees_run1[line.type_id])
        # Fees scale with adjusted prices, so the lagged fee is half the
        # current run's snapshot for the same stage.
        current = conn.execute(
            "SELECT unit_install_fee FROM index_run_item "
            "WHERE index_run_id = ? AND type_id = ?",
            (last, line.type_id),
        ).fetchone()["unit_install_fee"]
        assert line.unit_cost == pytest.approx(current / 2)


def test_second_pipeline_does_not_change_first_pipelines_cost(conn, ref):
    """Attribution is per-pipeline at expansion time, so adding an
    unrelated pipeline must not move an existing hull's cost."""
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    solo_run = plan_completed_run(conn, ref, 10.0)
    solo = costing.hull_cost(conn, ref, settings, solo_run, pid)

    add_pipeline(conn, ref, "Mackinaw", 8)
    shared_run = plan_completed_run(conn, ref, 10.0)
    shared = costing.hull_cost(conn, ref, settings, shared_run, pid)
    # Same uniform prices; run-2 fees lag to run-1 which had identical
    # snapshots, so the totals must match to the isk.
    assert shared.total == pytest.approx(solo.total)

    mack_pid = conn.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Mackinaw'"
    ).fetchone()["pipeline_id"]
    mack = costing.hull_cost(conn, ref, settings, shared_run, mack_pid)
    assert mack is not None and mack.total > 0
    assert mack.hulls_per_cycle == 8


def test_new_chain_items_fall_forward_to_their_first_run(conn, ref):
    """A pipeline added after run 1 has chain items with no earlier
    snapshot — they cost from the first run that knows them, flagged."""
    add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    plan_completed_run(conn, ref, 10.0)

    tengu_pid = add_pipeline(conn, ref, "Tengu", 4)
    run2 = plan_completed_run(conn, ref, 20.0)
    cost = costing.hull_cost(conn, ref, settings, run2, tengu_pid)
    assert cost is not None
    deep = [
        l
        for l in cost.lines
        if l.kind == "material" and l.depth >= 1 and l.unit_cost == 20.0
    ]
    # Tengu-only materials missed run 1 -> priced from run 2, clamped.
    assert deep and all(l.clamped for l in deep)


def test_alchemy_route_rows_are_excluded(conn, ref):
    """Synthetic alchemy row: attribution rows flagged alchemy_for_type_id
    must not add cost lines (the composite row carries the stage)."""
    pid = add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    run_id = plan_completed_run(conn, ref, 10.0)
    before = costing.hull_cost(conn, ref, settings, run_id, pid).total

    # Veldspar is never in a Hulk build chain — no UNIQUE collision.
    cur = conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id, depth, "
        "alchemy_for_type_id, price_snapshot) VALUES (?, ?, 3, 16670, 1e9)",
        (run_id, ref.type_id("Veldspar")),
    )
    conn.execute(
        "INSERT INTO index_run_item_pipeline "
        "(index_run_item_id, pipeline_id, qty_attributable) "
        "VALUES (?, ?, 999999)",
        (cur.lastrowid, pid),
    )
    conn.commit()
    after = costing.hull_cost(conn, ref, settings, run_id, pid).total
    assert after == pytest.approx(before)


def test_bpc_cost_without_runs_per_bpc(conn, ref):
    """No runs-per-BPC (BPO-style) still amortizes sanely: whole cost per
    hull rather than a crash or silent skip."""
    pid = add_pipeline(conn, ref, "Hulk", 8, runs_per_bpc=None, bpc_cost=8e6)
    settings = store.get_settings(conn)
    run_id = plan_completed_run(conn, ref, 10.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    bpc = [l for l in cost.lines if l.kind == "bpc"]
    assert len(bpc) == 1 and bpc[0].cost_per_hull == pytest.approx(8e6)


def test_hull_cost_unknown_pipeline(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    settings = store.get_settings(conn)
    run_id = plan_completed_run(conn, ref, 10.0)
    assert costing.hull_cost(conn, ref, settings, run_id, 424242) is None


# --- Engine fee snapshots: degraded inputs ----------------------------------


def test_fee_snapshot_adjusted_price_fallbacks(conn, ref):
    """Snapshot.adjusted falls back to the market price when no adjusted
    price is cached, so fee snapshots survive a cold adjusted cache; with
    NO prices at all they degrade to 0.0, never NULL."""
    add_pipeline(conn, ref, "Hulk", 8)

    def fee_rows(snapshot):
        plan = engine.plan_index_run(conn, ref, snapshot)
        return conn.execute(
            "SELECT unit_install_fee FROM index_run_item "
            "WHERE index_run_id = ? AND blueprint_id IS NOT NULL",
            (plan.index_run_id,),
        ).fetchall()

    slots = {
        config.ACTIVITY_MANUFACTURING: 500,
        config.ACTIVITY_REACTION: 500,
    }

    class UniformPrices(dict):
        def get(self, key, default=None):
            return 10.0

    # Cold adjusted cache: market price stands in — fees stay nonzero.
    rows = fee_rows(
        Snapshot(
            slots_available=slots,
            prices=UniformPrices(),
            adjusted_prices={},
        )
    )
    assert rows and all(r["unit_install_fee"] > 0 for r in rows)

    # No prices anywhere: EIV 0 -> fee 0.0 (a number, never NULL).
    rows = fee_rows(
        Snapshot(slots_available=slots, prices={}, adjusted_prices={})
    )
    assert rows and all(r["unit_install_fee"] == 0.0 for r in rows)


def test_install_fee_golden_base_quantities_nonuniform_adjusted(conn, ref):
    """Absolute install-fee golden with NON-uniform adjusted prices.

    Every other fee test prices adjusted uniformly, where an
    ME-modified-vs-base-quantity bug cancels out of the comparison. Here
    Construction Blocks (3828) is adjusted-priced at 100 against 10 for
    everything else, and the expected EIV is hand-computed from the Hulk
    blueprint's BASE pre-ME quantities (Construction Blocks base 150 —
    the ME10 quantity 135 must NOT appear even though the default
    intermediate ME of 10 governs the blueprint's consumption)."""
    add_pipeline(conn, ref, "Hulk", 8)
    hulk = ref.type_id("Hulk")
    assert hulk == 22544
    blueprint = ref.blueprint_for_product(hulk)
    assert blueprint.blueprint_id == 22545
    assert blueprint.portion_size == 1  # ships: per-unit fee == per-run fee
    blocks = ref.type_id("Construction Blocks")
    assert blocks == 3828
    materials = dict(
        ref.materials(blueprint.blueprint_id, blueprint.activity_id)
    )
    assert materials[blocks] == 150  # BASE quantity; ME10 consumes 135

    class Overlay(dict):
        def get(self, key, default=None):
            return dict.get(self, key, 10.0)

    adjusted = Overlay({blocks: 100.0})
    snapshot = Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: 500,
            config.ACTIVITY_REACTION: 500,
        },
        prices=UniformDict(10.0),  # market prices; ADJUSTED drives the EIV
        adjusted_prices=adjusted,
    )
    settings = store.get_settings(conn)
    assert settings.default_intermediate_me == 10  # ME really is in play

    plan = engine.plan_index_run(conn, ref, snapshot)
    row = conn.execute(
        "SELECT unit_install_fee, item_class FROM index_run_item "
        "WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, hulk),
    ).fetchone()
    assert row["item_class"] == "t2_ships"

    expected_eiv = sum(
        base_qty * adjusted.get(material_id)
        for material_id, base_qty in materials.items()
    )
    # The engine's exact fee inputs: the t2_ships class setting (fresh
    # state DB defaults) and the NPC cost multiplier.
    setting = store.get_class_settings(conn)["t2_ships"]
    cost_mult = industry.build_multiplier(
        ref, setting, config.ACTIVITY_MANUFACTURING, "cost"
    )
    expected_fee = industry.job_install_cost(
        expected_eiv,
        setting,
        cost_mult,
        scc_surcharge=settings.industry_scc_surcharge,
    )
    assert row["unit_install_fee"] == pytest.approx(expected_fee)
    # Fully-resolved absolute anchor so a mirrored formula bug cannot
    # cancel: defaults are cost index 0, NPC tax 0.25%, SCC 4%.
    assert setting.system_cost_index == 0.0 and setting.tax_rate == 0.0025
    assert expected_fee == pytest.approx(expected_eiv * 0.0425)
    # And the golden really discriminates: pricing Construction Blocks at
    # its ME10 quantity would move the fee by 15 x 100 x 4.25% = 63.75 ISK.
    me10_fee = industry.job_install_cost(
        expected_eiv - 15 * 100.0,
        setting,
        cost_mult,
        scc_surcharge=settings.industry_scc_surcharge,
    )
    assert row["unit_install_fee"] != pytest.approx(me10_fee)


# --- Current-prices view: settings sensitivity ------------------------------


def test_current_hull_cost_me_lowers_material_use(conn, ref):
    """Raising the intermediate ME default must strictly lower the
    current-prices material bill (same prices, same chain)."""
    pid = add_pipeline(conn, ref, "Hulk", 8)
    pipeline = get_pipeline(conn, pid)

    def total_materials(me):
        conn.execute(
            "UPDATE settings SET default_intermediate_me = ?", (me,)
        )
        conn.commit()
        cost = costing.current_hull_cost(
            conn, ref, store.get_settings(conn), pipeline,
            UniformDict(10.0), UniformDict(10.0),
        )
        return cost.subtotal("material")

    assert total_materials(10) < total_materials(0)


def test_current_hull_cost_freight_scales_with_rate(conn, ref):
    pid = add_pipeline(conn, ref, "Hulk", 8)
    pipeline = get_pipeline(conn, pid)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 100")
    conn.commit()
    at_100 = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), pipeline,
        UniformDict(10.0), UniformDict(10.0),
    ).subtotal("freight_in")
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 200")
    conn.commit()
    at_200 = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), pipeline,
        UniformDict(10.0), UniformDict(10.0),
    ).subtotal("freight_in")
    assert at_100 > 0
    assert at_200 == pytest.approx(2 * at_100)


# --- v1.6 capital pricing ----------------------------------------------------


def test_capital_pricing_classification(ref):
    capitals = (
        "Rorqual",     # capital industrial
        "Providence",  # freighter
        "Charon",      # freighter
        "Anshar",      # jump freighter
        "Orca",        # industrial command ship
        "Porpoise",    # group 941 too — intentionally capital-priced
        "Archon",      # carrier
        "Naglfar",     # dreadnought
        "Phoenix Navy Issue",  # faction dreadnought
        "Aeon",        # supercarrier
        "Avatar",      # titan
    )
    for name in capitals:
        assert costing.is_capital_priced(ref, ref.type_id(name)), name
    subcaps = (
        "Hulk", "Golem", "Tengu", "Vargur", "Ishtar", "Tritanium",
        # v1.9: structures, rigs and components sell on the sub-capital model
        "Keepstar", "Astrahus", "Raitaru", "Athanor",
        "Standup M-Set Structure Manufacturing Material Efficiency I",
        "Structure Construction Parts",
    )
    for name in subcaps:
        assert not costing.is_capital_priced(ref, ref.type_id(name)), name


def test_structure_freight_out_exemption(ref):
    """XL Upwell hulls waive the per-m³ freight-out; everything else on the
    sub-capital model pays packaged m³ × rate (v1.9)."""
    for name in ("Keepstar", "Upwell Palatine Keepstar", "Sotiyo"):
        assert costing.freight_out_exempt(ref.type_id(name)), name
    for name in ("Astrahus", "Fortizar", "Azbel", "Tatara", "Hulk",
                 "Structure Construction Parts"):
        assert not costing.freight_out_exempt(ref.type_id(name)), name
    s = store.Settings(
        0.05, 24.0, 8, 1, 10000002, "sell",
        skill_accounting=5, skill_broker_relations=5,
        freight_out_isk_per_m3=750.0,
    )
    keepstar = ref.type_info(ref.type_id("Keepstar")).freight_volume
    astrahus = ref.type_info(ref.type_id("Astrahus")).freight_volume
    assert keepstar == 800_000 and astrahus == 8_000
    rate = 1.0 - costing.sales_tax_rate(s) - costing.broker_fee_rate(s) - s.capital_scc_surcharge
    assert costing.net_proceeds_per_hull(
        1e9, keepstar, s, freight_exempt=True
    ) == pytest.approx(1e9 * rate)
    assert costing.net_proceeds_per_hull(
        1e9, astrahus, s, freight_exempt=False
    ) == pytest.approx(1e9 * rate - 8_000 * 750.0)
    # without the flag a Keepstar would lose 600M to freight — the flag
    # is what the two call sites pass via freight_out_exempt()
    assert costing.net_proceeds_per_hull(1e9, keepstar, s) == pytest.approx(
        1e9 * rate - 800_000 * 750.0
    )


def test_broker_fee_never_negative():
    # Absurd inputs (rates beyond any real standings) must clamp at the
    # NPC floor, never below zero.
    s_npc = store.Settings(
        0.05, 24.0, 8, 1, 10000002, "sell",
        skill_broker_relations=5,
        standing_broker_faction=10.0,
        standing_broker_corp=10.0,
    )
    assert costing.broker_fee_rate(s_npc) >= 0.0


def test_sales_tax_at_zero_accounting():
    s = store.Settings(0.05, 24.0, 8, 1, 10000002, "sell", skill_accounting=0)
    assert costing.sales_tax_rate(s) == pytest.approx(costing.SALES_TAX_BASE)


def test_refresh_structure_prices_empty_book(conn, monkeypatch):
    from magoo import esi

    monkeypatch.setattr(
        esi, "fetch_structure_orders", lambda c, char_id, sid: []
    )
    n = market.refresh_structure_prices(conn, 999, [34, 35], character_id=1)
    assert n == 0
    # Both wanted types cached as NULL: "no orders" is an answer.
    assert (
        market.cached_prices(conn, 999, [34, 35], market.STRUCTURE_SOURCE)
        == {}
    )
    rows = conn.execute(
        "SELECT COUNT(*) n FROM market_price WHERE region_id = 999"
    ).fetchone()
    assert rows["n"] == 2


def test_min_sell_by_type_edge_inputs():
    assert market._min_sell_by_type([], {34}) == {}
    only_buys = [
        {"type_id": 34, "price": 5.0, "is_buy_order": True, "volume_remain": 9}
    ]
    assert market._min_sell_by_type(only_buys, {34}) == {}


def test_net_proceeds_capital_branch():
    s = store.Settings(
        0.05, 24.0, 8, 1, 10000002, "sell",
        freight_out_isk_per_m3=750.0,  # must be ignored for capitals
        capital_sales_tax=0.02,
        capital_broker_rate=0.01,
        capital_movement_cost_isk=50_000_000.0,
        capital_scc_surcharge=0.015,
    )
    net = costing.net_proceeds_per_hull(5e9, 1_300_000.0, s, capital=True)
    assert net == pytest.approx(5e9 * (1 - 0.02 - 0.01 - 0.015) - 50e6)
    # Subcap branch: standings rates + the shared SCC + per-m³ freight.
    sub = costing.net_proceeds_per_hull(1000.0, 10.0, s)
    rate = (
        1.0
        - costing.sales_tax_rate(s)
        - costing.broker_fee_rate(s)
        - s.capital_scc_surcharge
    )
    assert sub == pytest.approx(1000.0 * rate - 7500.0)


def test_capital_structure_toggle():
    s = store.Settings(0.05, 24.0, 8, 1, 10000002, "sell")
    assert s.capital_structure() == config.CJ6_KEEPSTAR_STRUCTURE_ID
    s2 = store.Settings(
        0.05, 24.0, 8, 1, 10000002, "sell",
        capital_market_mode="custom", capital_structure_id=12345,
    )
    assert s2.capital_structure() == 12345
    # Custom mode without an ID falls back to the preset.
    s3 = store.Settings(
        0.05, 24.0, 8, 1, 10000002, "sell", capital_market_mode="custom"
    )
    assert s3.capital_structure() == config.CJ6_KEEPSTAR_STRUCTURE_ID


def test_min_sell_by_type():
    orders = [
        {"type_id": 34, "price": 12.0, "is_buy_order": False, "volume_remain": 3},
        {"type_id": 34, "price": 9.0, "is_buy_order": False, "volume_remain": 3},
        {"type_id": 34, "price": 1.0, "is_buy_order": True, "volume_remain": 3},  # buy
        {"type_id": 35, "price": 5.0, "is_buy_order": False, "volume_remain": 3},  # unwanted
        {"type_id": 34, "price": 2.0, "is_buy_order": False, "volume_remain": 0},  # empty
    ]
    assert market._min_sell_by_type(orders, {34, 36}) == {34: 9.0}


def test_refresh_structure_prices_cache(conn, monkeypatch):
    from magoo import esi

    monkeypatch.setattr(
        esi,
        "fetch_structure_orders",
        lambda c, char_id, sid: [
            {"type_id": 34, "price": 100.0, "is_buy_order": False, "volume_remain": 1},
            {"type_id": 34, "price": 90.0, "is_buy_order": False, "volume_remain": 1},
        ],
    )
    n = market.refresh_structure_prices(conn, 999, [34, 35], character_id=1)
    assert n == 1
    cached = market.cached_prices(conn, 999, [34, 35], market.STRUCTURE_SOURCE)
    assert cached == {34: 90.0}  # 35 cached as NULL -> absent from lookup

    # A later refresh replaces the book wholesale — no stale leftovers.
    monkeypatch.setattr(
        esi,
        "fetch_structure_orders",
        lambda c, char_id, sid: [
            {"type_id": 35, "price": 70.0, "is_buy_order": False, "volume_remain": 1},
        ],
    )
    market.refresh_structure_prices(conn, 999, [34, 35], character_id=1)
    cached = market.cached_prices(conn, 999, [34, 35], market.STRUCTURE_SOURCE)
    assert cached == {35: 70.0}


# --- Per-pipeline depth (2026-08-20) ---------------------------------------


def test_second_pipeline_does_not_shift_first_pipelines_vintages(conn, ref):
    """Lag costing prices each input at its depth within the OWNING
    pipeline's chain. Adding a Hulk pipeline (which consumes Covetor as an
    intermediate, deepening the merged tree) must not move the Covetor
    pipeline's own realized cost — with DISTINCT price levels per run, the
    old merged-max depth halved it."""
    covetor = add_pipeline(conn, ref, "Covetor", 1)
    for price in (10.0, 20.0, 40.0):
        run_id = plan_completed_run(conn, ref, price)
    solo = costing.hull_cost(
        conn, ref, store.get_settings(conn), run_id, covetor
    )
    assert solo is not None

    add_pipeline(conn, ref, "Hulk", 1)
    for price in (10.0, 20.0, 40.0):
        run_id2 = plan_completed_run(conn, ref, price)
    merged = costing.hull_cost(
        conn, ref, store.get_settings(conn), run_id2, covetor
    )
    assert merged is not None
    # Same price history shape, same pipeline -> same per-hull total, and
    # the Covetor final's own install line stays at depth 0 even though the
    # merged tree now holds it at depth 1.
    assert merged.total == pytest.approx(solo.total)
    final_lines = [l for l in merged.lines if l.type_id == ref.type_id("Covetor")]
    assert final_lines and all(l.depth == 0 for l in final_lines)


# -- cycle totals (profit-page header strip) ----------------------------


def _card(total, hulls, net, kind="material"):
    cost = costing.HullCost(
        pipeline_id=1,
        hulls_per_cycle=hulls,
        lines=[
            costing.CostLine(
                type_id=1, name="x", kind=kind, depth=0,
                qty_per_hull=1, unit_cost=total, lag_runs=0,
                clamped=False,
            )
        ],
    )
    return {
        "cost": cost,
        "net": net,
        "margin": (net - cost.total) if net is not None else None,
    }


def test_cycle_totals_sums_per_hull_figures_times_hulls():
    cards = [_card(100.0, 3, 150.0), _card(50.0, 2, 40.0)]
    t = costing.cycle_totals(cards)
    assert t.cost == 300 + 100
    assert t.proceeds == 450 + 80
    assert t.profit == 150 - 20
    assert t.hulls == 5
    assert t.priced == 2 and t.unpriced == 0
    assert t.margin_pct == 130 / 400 * 100


def test_cycle_totals_skips_unquoted_cards_entirely():
    cards = [_card(100.0, 3, 150.0), _card(999.0, 7, None)]
    t = costing.cycle_totals(cards)
    assert t.cost == 300 and t.profit == 150 and t.hulls == 3
    assert t.priced == 1 and t.unpriced == 1


def test_cycle_totals_empty_has_no_margin_pct():
    t = costing.cycle_totals([])
    assert t.cost == 0 and t.profit == 0 and t.margin_pct is None
    assert t.structure_share_pct is None


def test_null_sec_market_share_per_hull_and_per_cycle():
    """Null Sec Market Share = structure-venue material ISK ÷ all material
    ISK, per hull and summed over the cycle (hulls-weighted)."""
    def line(type_id, qty, unit, venue):
        return costing.CostLine(
            type_id=type_id, name=str(type_id), kind="material", depth=1,
            qty_per_hull=qty, unit_cost=unit, lag_runs=0, clamped=False,
            venue=venue,
        )
    install = costing.CostLine(
        type_id=9, name="fee", kind="install", depth=0, qty_per_hull=1,
        unit_cost=50.0, lag_runs=0, clamped=False,
    )
    a = costing.HullCost(1, 2, [line(1, 10, 2.0, "structure"), line(2, 10, 6.0, "hub"), install])
    b = costing.HullCost(2, 1, [line(3, 1, 100.0, "hub"), install])
    assert a.structure_material_cost == 20.0
    assert a.structure_material_share_pct == pytest.approx(25.0)  # 20 of 80
    assert b.structure_material_share_pct == 0.0
    assert costing.HullCost(3, 1, [install]).structure_material_share_pct is None
    cards = [
        {"cost": a, "net": 500.0, "margin": 500.0 - a.total},
        {"cost": b, "net": 500.0, "margin": 500.0 - b.total},
    ]
    t = costing.cycle_totals(cards)
    assert t.materials == 80 * 2 + 100
    assert t.structure_materials == 20 * 2
    assert t.structure_share_pct == pytest.approx(40 / 260 * 100)
