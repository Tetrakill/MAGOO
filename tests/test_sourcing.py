"""Fill pricing for every buy (v1.25, 2026-09-05): each purchase walks
its Jita and C-J6 sell ladders together (cheapest landed rung first), may
split across the two markets, and prices anything beyond the stored
ladders at the last rung walked. Supersedes the 2026-08-22 "best price +
flag, never a fill price" and "no order splitting" rules.

Same fixtures as test_compressed: real reference data, temp state DB,
FairValuePrices (raws at 10.0), Hulk x8 as the demand (tens of millions
of Tritanium per cycle).
"""

import sqlite3

import pytest

from magoo import config, costing, engine, market, store

from conftest import template_app
from test_alchemy import conn  # noqa: F401 — fixture
from test_compressed import (
    COMPRESSED_VELDSPAR,
    TRITANIUM,
    _hulk,
    compressed_rows,
    enable_compressed,
    snapshot,
)

HUB = store.BUY_VENUE_HUB
STRUCT = store.BUY_VENUE_STRUCTURE
SPLIT = store.BUY_VENUE_SPLIT
DEEP = 10**12


def _plan(conn, ref, ladders, persist=False):
    return engine.plan_index_run(
        conn, ref, snapshot(ref, ladders), persist=persist
    )


def _demand(conn, ref):
    return engine.plan_index_run(
        conn, ref, snapshot(ref), persist=False
    ).items[TRITANIUM].recommended_buy_qty


def _invariant(item):
    """Ruling R5 (2026-09-05): unsourced units sit in the row's quantity
    and price blend but in neither venue's quantity."""
    total = (
        (item.hub_buy_qty or 0) * (item.hub_fill_price or 0)
        + (item.structure_buy_qty or 0) * (item.structure_fill_price or 0)
        + item.unfilled_qty * (item.unfilled_price or 0)
    )
    assert total == pytest.approx(item.recommended_buy_qty * item.price_snapshot)
    assert (
        (item.hub_buy_qty or 0) + (item.structure_buy_qty or 0) + item.unfilled_qty
        == item.recommended_buy_qty
    )


# --- costing: the merged walk ------------------------------------------------


def test_fill_merged_walks_both_ladders_cheapest_landed_first():
    hub = [(10.0, 100), (12.0, 100)]
    structure = [(9.0, 50), (11.0, 1000)]
    # Structure freight of 2/unit: landed 11, 13 vs hub 10, 12 — walk
    # order: hub 10 (100), structure 9+2=11 (50), hub 12 (100), then the
    # structure 11+2=13 rung for the rest.
    fill = costing.fill_merged(hub, structure, 300, 0.0, 2.0, 1.0)
    assert (fill.hub_units, fill.hub_orders) == (200, 2)
    assert (fill.structure_units, fill.structure_orders) == (100, 2)
    assert fill.unfilled == 0
    assert fill.landed_cost == pytest.approx(100 * 10 + 50 * 11 + 100 * 12 + 50 * 13)
    assert fill.raw_average == pytest.approx((1000 + 450 + 1200 + 550) / 300)
    # A landed tie goes to the hub.
    tie = costing.fill_merged([(10.0, 5)], [(10.0, 5)], 5, 0.0, 0.0, 1.0)
    assert (tie.hub_units, tie.structure_units) == (5, 0)


def test_fill_merged_remainder_at_last_rung_is_unsourced_unless_truncated():
    """Ruling R5 (2026-09-05): a remainder beyond an EXHAUSTED book (the
    stored Jita ladder was the whole book, or there is none) names no
    venue; only a Jita ladder truncated at config.HUB_LADDER_MAX_RUNGS
    rungs proves the book goes on and carries the remainder."""
    fill = costing.fill_merged([(9.0, 500)], [(8.5, 300)], 2000, 0.0, 0.0, 1.0)
    assert (fill.hub_units, fill.structure_units, fill.unfilled) == (500, 300, 1200)
    assert fill.marginal_price == 9.0  # the last rung walked
    assert fill.remainder_venue is None
    assert fill.landed_cost == pytest.approx(300 * 8.5 + 500 * 9.0 + 1200 * 9.0)
    # A truncated Jita book: exactly the pull cap of rungs stored.
    truncated = [(9.0 + i * 0.01, 1) for i in range(config.HUB_LADDER_MAX_RUNGS)]
    deep = costing.fill_merged(truncated, [(8.5, 300)], 2000, 0.0, 0.0, 1.0)
    assert (deep.hub_units, deep.structure_units) == (config.HUB_LADDER_MAX_RUNGS, 300)
    assert deep.unfilled == 2000 - 300 - config.HUB_LADDER_MAX_RUNGS
    assert deep.remainder_venue == HUB
    assert deep.marginal_price == pytest.approx(truncated[-1][0])
    # No hub ladder at all: the remainder is unsourced too (the structure
    # book is stored whole), landed at the structure rate.
    only = costing.fill_merged(None, [(8.0, 300)], 1000, 0.0, 2.0, 1.0)
    assert (only.structure_units, only.unfilled, only.remainder_venue) == (300, 700, None)
    assert only.marginal_price == 8.0
    assert only.landed_cost == pytest.approx(300 * 10.0 + 700 * 10.0)
    empty = costing.fill_merged(None, None, 10, 0.0, 0.0, 1.0)
    assert (empty.units, empty.raw_average, empty.marginal_price) == (10, None, None)


def test_freight_lines_split_a_split_line_by_hub_fraction(ref):
    settings = store.Settings(
        0.05, 24.0, 1, 10000002, "sell",
        freight_in_isk_per_m3=100.0, structure_freight_in_isk_per_m3=20.0,
    )
    line = costing.CostLine(
        type_id=TRITANIUM, name="Tritanium", kind="material", depth=1,
        qty_per_hull=1000.0, unit_cost=10.0, lag_runs=0, clamped=False,
        venue=SPLIT, hub_fraction=0.25,
    )
    freight = {l.venue: l for l in costing._freight_in_lines(settings, ref, [line])}
    m3 = 1000.0 * ref.type_info(TRITANIUM).freight_volume
    assert freight[HUB].qty_per_hull == pytest.approx(m3 * 0.25)
    assert freight[STRUCT].qty_per_hull == pytest.approx(m3 * 0.75)
    assert line.structure_share() == pytest.approx(0.75)
    cost = costing.HullCost(pipeline_id=1, hulls_per_cycle=1, lines=[line])
    assert cost.structure_priced == 1
    assert cost.structure_material_cost == pytest.approx(10000.0 * 0.75)


# --- engine: the sourcing pass -------------------------------------------------


def test_hub_ladder_fill_prices_a_direct_buy(conn, ref):
    _hulk(conn, ref)
    demand = _demand(conn, ref)
    plan = _plan(conn, ref, {HUB: {TRITANIUM: [(9.0, 1_000_000), (11.0, DEEP)]}})
    trit = plan.items[TRITANIUM]
    assert trit.recommended_buy_qty == demand
    assert (trit.hub_buy_qty, trit.structure_buy_qty) == (demand, 0)
    assert trit.hub_fill_orders == 2
    assert trit.buy_venue == HUB
    assert trit.unfilled_qty == 0 and trit.unfilled_price is None
    assert trit.price_snapshot == pytest.approx(
        (1_000_000 * 9.0 + (demand - 1_000_000) * 11.0) / demand
    )
    assert trit.structure_units_cheaper is None
    _invariant(trit)
    assert plan.compressed_saving_isk is None


def test_thin_structure_ladder_splits_the_buy(conn, ref):
    _hulk(conn, ref)
    demand = _demand(conn, ref)
    plan = _plan(
        conn, ref,
        {HUB: {TRITANIUM: [(10.0, DEEP)]}, STRUCT: {TRITANIUM: [(8.0, 1000)]}},
    )
    trit = plan.items[TRITANIUM]
    assert trit.buy_venue == SPLIT
    assert (trit.structure_buy_qty, trit.structure_fill_price) == (1000, 8.0)
    assert (trit.hub_buy_qty, trit.hub_fill_price) == (demand - 1000, 10.0)
    assert trit.unfilled_qty == 0
    _invariant(trit)


def test_exhausted_ladders_leave_the_remainder_unsourced(conn, ref):
    """Ruling R5 / contract C4 (2026-09-05): units beyond EXHAUSTED books
    stay in the row's quantity, priced at the last rung walked, but
    belong to no venue — the Multibuy blocks list only what the ladders
    held — and the venue is decided from the filled parts alone."""
    _hulk(conn, ref)
    demand = _demand(conn, ref)
    plan = _plan(
        conn, ref,
        {HUB: {TRITANIUM: [(9.0, 500)]}, STRUCT: {TRITANIUM: [(8.5, 300)]}},
    )
    trit = plan.items[TRITANIUM]
    assert trit.recommended_buy_qty == demand
    assert trit.unfilled_qty == demand - 800
    assert trit.unfilled_price == 9.0
    assert (trit.hub_buy_qty, trit.hub_fill_price) == (500, pytest.approx(9.0))
    assert (trit.structure_buy_qty, trit.structure_fill_price) == (300, 8.5)
    assert trit.buy_venue == SPLIT
    assert trit.price_snapshot == pytest.approx(
        (500 * 9.0 + 300 * 8.5 + (demand - 800) * 9.0) / demand
    )
    _invariant(trit)
    # No hub ladder at all: with a hub QUOTE cached (Phase 1 priced the
    # raw at 10.0 on the hub) the quote stands in as an unbounded Jita
    # rung, so nothing is unsourced — the structure's 300 cheaper units
    # split the buy with Jita at 10.0 (finding A2).
    plan = _plan(conn, ref, {STRUCT: {TRITANIUM: [(8.0, 300)]}})
    trit = plan.items[TRITANIUM]
    assert trit.buy_venue == SPLIT
    assert (trit.hub_buy_qty, trit.structure_buy_qty) == (demand - 300, 300)
    assert (trit.hub_fill_price, trit.structure_fill_price) == (10.0, 8.0)
    assert trit.unfilled_qty == 0
    _invariant(trit)
    # No hub quote either (the structure won Phase 1 and no Jita price is
    # known): the remainder beyond the structure's book is unsourced on
    # top of the 300 structure units, and the venue is the structure's.
    snap = snapshot(ref, {STRUCT: {TRITANIUM: [(8.0, 300)]}})
    snap.buy_venue[TRITANIUM] = STRUCT
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    trit = plan.items[TRITANIUM]
    assert trit.buy_venue == STRUCT
    assert (trit.hub_buy_qty, trit.structure_buy_qty) == (0, 300)
    assert trit.unfilled_qty == demand - 300 and trit.unfilled_price == 8.0
    assert trit.price_snapshot == pytest.approx(8.0)
    _invariant(trit)


def test_truncated_hub_book_carries_the_remainder_on_jita(conn, ref):
    """A Jita ladder stored at exactly config.HUB_LADDER_MAX_RUNGS rungs
    was cut short by the pull, so its book continues: the remainder
    folds into the hub quantity at the last rung's price (ruling R5)."""
    _hulk(conn, ref)
    demand = _demand(conn, ref)
    truncated = [(9.0 + i * 0.001, 1) for i in range(config.HUB_LADDER_MAX_RUNGS)]
    plan = _plan(conn, ref, {HUB: {TRITANIUM: truncated}})
    trit = plan.items[TRITANIUM]
    assert trit.unfilled_qty == 0 and trit.unfilled_price is None
    assert (trit.hub_buy_qty, trit.structure_buy_qty) == (demand, 0)
    assert trit.hub_fill_orders == config.HUB_LADDER_MAX_RUNGS
    assert trit.buy_venue == HUB
    marginal = truncated[-1][0]
    assert trit.price_snapshot == pytest.approx(
        (sum(p for p, _v in truncated) + (demand - len(truncated)) * marginal)
        / demand
    )
    _invariant(trit)


def test_structure_only_ladder_competes_with_the_hub_quote(conn, ref):
    """Finding A2 (2026-09-05): Snapshot.hub_prices carries the Jita
    quote even where the structure won Phase 1, and the pass weighs the
    structure ladder against it as one synthetic unbounded rung —
    structure rungs cheaper than the quote are taken first, dearer ones
    never; all-zero-volume ladders count as absent (finding A11); moving
    off the hub clears the region-wide provenance (finding A16)."""
    _hulk(conn, ref)
    demand = _demand(conn, ref)
    snap = snapshot(
        ref, {STRUCT: {TRITANIUM: [(8.0, 1000), (50.0, DEEP)]}},
        overrides={TRITANIUM: 8.0},  # the structure's best rung won Phase 1
    )
    snap.buy_venue[TRITANIUM] = STRUCT
    snap.hub_prices[TRITANIUM] = 10.0
    snap.region_wide.add(TRITANIUM)
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    trit = plan.items[TRITANIUM]
    assert trit.buy_venue == SPLIT
    assert (trit.structure_buy_qty, trit.structure_fill_price) == (1000, 8.0)
    assert (trit.hub_buy_qty, trit.hub_fill_price) == (demand - 1000, 10.0)
    assert trit.unfilled_qty == 0
    assert trit.price_region_wide is False
    _invariant(trit)
    # A dearer structure-only ladder: the whole buy stays on Jita at the
    # quote (the P0 case), and the ladder's presence never nulls the
    # quote when every rung is empty.
    plan = _plan(conn, ref, {STRUCT: {TRITANIUM: [(50.0, DEEP)]}})
    trit = plan.items[TRITANIUM]
    assert (trit.buy_venue, trit.price_snapshot) == (HUB, pytest.approx(10.0))
    assert (trit.hub_buy_qty, trit.structure_buy_qty) == (demand, 0)
    plan = _plan(conn, ref, {HUB: {TRITANIUM: [(9.0, 0)]}, STRUCT: {TRITANIUM: [(8.0, 0)]}})
    trit = plan.items[TRITANIUM]
    assert trit.hub_buy_qty is None
    assert (trit.buy_venue, trit.price_snapshot) == (HUB, 10.0)


def test_three_tuple_ladder_rows_pass_through_the_engine(conn, ref):
    """Contract C3 (2026-09-05): the cached ladders carry ESI's
    min_volume as a third element; the engine unpacks nothing it does
    not need and the fill skips a rung whose minimum exceeds the take."""
    _hulk(conn, ref)
    demand = _demand(conn, ref)
    plan = _plan(
        conn, ref,
        {HUB: {TRITANIUM: [(9.0, 1000, 1), (9.5, DEEP, 1), (8.0, 10**9, 10**9)]},
         STRUCT: {TRITANIUM: [(8.5, 500, 1)]}},
    )
    trit = plan.items[TRITANIUM]
    # The 8.0 rung demands a billion-unit fill the buy never reaches.
    assert (trit.structure_buy_qty, trit.structure_fill_price) == (500, 8.5)
    assert trit.hub_buy_qty == demand - 500
    assert trit.hub_fill_orders == 2
    assert trit.buy_venue == SPLIT
    _invariant(trit)


def test_lp_failure_falls_back_to_direct_fill_pricing(conn, ref, monkeypatch):
    """Finding A10 (2026-09-05): a compressed-sourcing LP breakdown
    chooses nothing and the run still plans with fill-priced direct buys
    — the optimisation is not the plan."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    demand = _demand(conn, ref)

    class Broken:
        success = False
        message = "simulated solver breakdown"

    monkeypatch.setattr(engine, "linprog", lambda *a, **k: Broken())
    plan = _plan(
        conn, ref,
        {HUB: {TRITANIUM: [(9.0, DEEP)], COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]}},
    )
    assert not compressed_rows(plan)
    assert plan.compressed_saving_isk is None
    trit = plan.items[TRITANIUM]
    assert trit.recommended_buy_qty == demand
    assert (trit.hub_buy_qty, trit.price_snapshot) == (demand, 9.0)


def test_no_ladder_anywhere_keeps_the_single_quote(conn, ref):
    _hulk(conn, ref)
    plan = _plan(conn, ref, {HUB: {ref.type_id("Pyerite"): [(9.0, DEEP)]}})
    trit = plan.items[TRITANIUM]
    assert trit.hub_buy_qty is None
    assert trit.price_snapshot == 10.0
    assert trit.buy_venue == HUB
    assert trit.unfilled_qty == 0
    pyer = plan.items[ref.type_id("Pyerite")]
    assert pyer.hub_buy_qty == pyer.recommended_buy_qty
    assert pyer.price_snapshot == 9.0


def test_structure_freight_moves_the_split(conn, ref):
    _hulk(conn, ref)
    ladders = {HUB: {TRITANIUM: [(10.0, DEEP)]}, STRUCT: {TRITANIUM: [(9.5, DEEP)]}}
    plan = _plan(conn, ref, ladders)
    assert plan.items[TRITANIUM].buy_venue == STRUCT
    conn.execute(
        "UPDATE settings SET structure_freight_in_isk_per_m3 = ?",
        (1.0 / ref.type_info(TRITANIUM).freight_volume,),  # +1.0 landed
    )
    conn.commit()
    plan = _plan(conn, ref, ladders)
    assert plan.items[TRITANIUM].buy_venue == HUB


def test_compressed_beats_only_the_dear_rungs(conn, ref):
    """With Tritanium's own ladder in play, compressed Veldspar (6.67 per
    Tritanium at 20 ISK) displaces the 12-ISK rungs but not the 5-ISK
    ones: the direct buy keeps the cheap million, the rest goes compressed."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    demand = _demand(conn, ref)
    plan = _plan(
        conn, ref,
        {HUB: {TRITANIUM: [(5.0, 1_000_000), (12.0, DEEP)],
               COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]}},
    )
    ore, = compressed_rows(plan)
    trit = plan.items[TRITANIUM]
    assert trit.compressed_covered_qty >= demand - 1_000_000 - 300
    assert 0 < trit.recommended_buy_qty <= 1_000_000
    assert trit.hub_buy_qty == trit.recommended_buy_qty
    assert trit.price_snapshot == pytest.approx(5.0)
    # The effective cost blends the cheap direct rung with the ore.
    assert 5.0 < trit.effective_unit_cost < 7.0
    assert plan.compressed_saving_isk > 0
    _invariant(trit)


def test_steady_state_never_fill_prices(conn, ref):
    _hulk(conn, ref)
    plan = engine.plan_steady_state(
        conn, ref, snapshot(ref, {HUB: {TRITANIUM: [(9.0, DEEP)]}})
    )
    assert plan.items[TRITANIUM].hub_buy_qty is None
    assert plan.items[TRITANIUM].price_snapshot == 10.0


def test_persistence_and_realized_freight_split(conn, ref):
    _hulk(conn, ref)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 500.0, "
        "structure_freight_in_isk_per_m3 = 100.0"
    )
    conn.commit()
    plan = _plan(
        conn, ref,
        {HUB: {TRITANIUM: [(10.0, DEEP)]}, STRUCT: {TRITANIUM: [(8.0, 1000)]}},
        persist=True,
    )
    trit = plan.items[TRITANIUM]
    row = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, TRITANIUM),
    ).fetchone()
    assert row["buy_venue"] == SPLIT
    assert (row["hub_buy_qty"], row["structure_buy_qty"]) == (
        trit.hub_buy_qty, trit.structure_buy_qty,
    )
    assert row["hub_fill_price"] == pytest.approx(10.0)
    assert row["structure_fill_price"] == pytest.approx(8.0)
    assert (row["hub_fill_orders"], row["structure_fill_orders"]) == (1, 1)
    assert row["unfilled_qty"] == 0 and row["unfilled_price"] is None
    assert row["price_snapshot"] == pytest.approx(trit.price_snapshot)
    # Contract C2 (2026-09-05): the plan-time courier rates ride the run.
    run = conn.execute(
        "SELECT freight_in_isk_per_m3, structure_freight_in_isk_per_m3 "
        "FROM index_run WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchone()
    assert (run[0], run[1]) == (500.0, 100.0)
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    pipeline_id = conn.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    cost = costing.hull_cost(
        conn, ref, store.get_settings(conn), plan.index_run_id, pipeline_id
    )
    line = next(l for l in cost.lines if l.type_id == TRITANIUM)
    assert line.venue == SPLIT
    fraction = trit.hub_buy_qty / trit.recommended_buy_qty
    assert line.hub_fraction == pytest.approx(fraction)
    assert line.unit_cost == pytest.approx(trit.price_snapshot)
    freight = {l.venue: l for l in cost.lines if l.kind == "freight_in"}
    structure_m3 = sum(
        l.qty_per_hull * ref.type_info(l.type_id).freight_volume * l.structure_share()
        for l in cost.lines if l.kind == "material"
    )
    assert freight[STRUCT].qty_per_hull == pytest.approx(structure_m3)
    # The structure share is priced at the structure FILL price on
    # record (costing review 2026-09-05), not the blended average.
    assert cost.structure_material_cost == pytest.approx(
        line.qty_per_hull * (1 - fraction) * trit.structure_fill_price
    )


# --- market ------------------------------------------------------------------


def test_region_wide_type_without_rungs_is_not_refetched(conn, monkeypatch):
    settings = store.get_settings(conn)
    region = settings.price_region_id
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return 5.0, 0, []  # region-wide fallback: no hub rungs

    monkeypatch.setattr(market, "_hub_ladder_quote", fake)
    market.refresh_prices(
        conn, region, [TRITANIUM], ladder_type_ids=[TRITANIUM],
        fallback_type_ids=[TRITANIUM], fallback_region_id=region,
    )
    fetched, _skipped, fresh = market.refresh_prices(
        conn, region, [TRITANIUM], ladder_type_ids=[TRITANIUM],
        fallback_type_ids=[TRITANIUM], fallback_region_id=region,
    )
    assert (fetched, fresh, len(calls)) == (0, 1, 1)


def test_schema_adds_the_fill_columns(conn):
    store.ensure_schema(conn)  # twice: idempotent
    cols = {r[1] for r in conn.execute("PRAGMA table_info(index_run_item)")}
    assert {"hub_buy_qty", "structure_fill_price", "unfilled_qty"} <= cols


# --- web -----------------------------------------------------------------------


def _row(name, type_id, qty, price, venue=HUB, **extra):
    row = {
        "name": name, "type_id": type_id, "group_id": 18, "category": "Mineral",
        "recommended_buy_qty": qty, "price_snapshot": price, "capacity_limited": 0,
        "runs_allocated": 0, "jobs_allocated": 0, "max_runs_per_job": 1,
        "recommended_build_qty": 0, "time_per_run": 0.0, "low_stock": 0,
        "savings_unpriced_inputs": 0, "deficit_qty": qty, "activity_id": None,
        "buy_venue": venue, "structure_units_cheaper": None,
        "price_region_wide": 0, "compressed_outputs": None,
        "compressed_ladder_units": None, "compressed_fill_orders": None,
        "compressed_covered_qty": 0, "effective_unit_cost": None,
        "hub_buy_qty": None, "hub_fill_price": None, "hub_fill_orders": None,
        "structure_buy_qty": None, "structure_fill_price": None,
        "structure_fill_orders": None, "unfilled_qty": 0, "unfilled_price": None,
    }
    row.update(extra)
    return row


def test_buy_context_splits_multibuy_by_venue_quantities(ref):
    from magoo.web import _buy_context

    split = _row(
        "Tritanium", TRITANIUM, 1500, 9.4, venue=SPLIT,
        hub_buy_qty=1000, hub_fill_price=10.0, hub_fill_orders=2,
        structure_buy_qty=500, structure_fill_price=8.2, structure_fill_orders=1,
        unfilled_qty=200, unfilled_price=10.0,
    )
    legacy = _row("Pyerite", 35, 12, 20.0, venue=STRUCT, structure_units_cheaper=5)
    bc = _buy_context([split, legacy], ref)
    assert bc["venue_qty"] == {TRITANIUM: (1000, 500), 35: (0, 12)}
    assert bc["split_buys"] == {TRITANIUM}
    assert bc["structure_buys"] == {TRITANIUM, 35}
    assert bc["shallow"] == {TRITANIUM, 35}  # unfilled units; the legacy rule
    assert bc["multibuy_hub"] == "Tritanium 1000"
    assert bc["multibuy_structure"] == "Tritanium 500\nPyerite 12"
    assert bc["buy_total"] == pytest.approx(1500 * 9.4 + 12 * 20.0)


def test_run_detail_renders_split_venue_fill_tooltip_and_shallow(ref):
    from flask import render_template

    from test_buy_venue import settings_with

    split = _row(
        "Tritanium", TRITANIUM, 1500, 9.4, venue=SPLIT,
        hub_buy_qty=1000, hub_fill_price=10.0, hub_fill_orders=2,
        structure_buy_qty=500, structure_fill_price=8.2, structure_fill_orders=1,
        unfilled_qty=200, unfilled_price=10.0,
    )
    run = {"run_number": 7, "status": "planned", "planned_start": "2026-09-05",
           "index_run_id": 1, "wallet_character_isk": 1e9,
           "wallet_corporation_isk": 2e9, "completed_at": None,
           "compressed_saving_isk": None}
    ctx = dict(
        run=run, items=[split], final_net_margin={}, buys=[split],
        builds=[], reactions=[], builds_grouped=[], reactions_grouped=[],
        struct_builds=[], struct_buys=[], struct_slots=0, chain_struct=[],
        alchemy=[], alchemy_yield=0.55, chain_rows=[], chain_raws=[],
        chain_mfg=[], chain_reactions=[],
        chain_counts={"covered": 0, "buy": 0, "build": 0, "react": 0, "alchemy": 0},
        unmet=[], low_stock=[], buy_total=1500 * 9.4, buys_unpriced=0,
        multibuy_hub="Tritanium 1000", multibuy_structure="Tritanium 500",
        structure_buys={TRITANIUM}, split_buys={TRITANIUM},
        venue_qty={TRITANIUM: (1000, 500)}, shallow={TRITANIUM},
        settings=settings_with(manufacturing_slots=50, reaction_slots=50),
        mfg_slots_used=0, reaction_slots_used=0, alchemy_slots_used=0,
        region_wide=set(), compressed={}, compressed_covered={},
        compressed_shallow=set(), compressed_section=[], compressed_saving=None,
    )
    app = template_app()
    with app.test_request_context("/runs/1"):
        html = render_template("run_detail.html", **ctx)
    assert "Jita 1,000 · C-J6 500" in html
    assert "1 split" in html and "1 shallow" in html
    assert "Jita: 1,000 units at 10 avg over 2 orders" in html
    assert "C-J6: 500 units at 8 avg over 1 order" in html
    assert "200 units beyond the stored ladders" in html
    assert "only 1,300 of 1,500 units were on the stored Jita / C-J6 sell ladders" in html
    # Ruling R5: the badge says the remainder is unsourced.
    assert "unsourced" in html
    assert ">Tritanium 1000</textarea>" in html
    assert ">Tritanium 500</textarea>" in html
