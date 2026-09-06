"""Compressed sourcing (v1.25): buy compressed ore / moon ore / gas and
reprocess it when cheaper landed than the raw minerals, moon materials and
gas, Jita ladder depth walked. Reference data (candidates, portion sizes),
the ladder fill helper, the hub ladder pull and cache, the engine's Phase
7.5 pass, persistence and the realized costing, the templates and the
settings save.

Same fixture pattern as test_alchemy: real reference data (the production
SDE, read-only), temp state DB, FairValuePrices (every raw costs 10.0).
Compressed Veldspar (62516 → 400 Tritanium per 100 units) is the worked
example: at the 0.75 ore yield one unit yields 3 Tritanium, so a rung under
30 ISK beats buying Tritanium direct at 10.
"""

import json
import sqlite3
import threading

import httpx
import pytest

from magoo import config, costing, engine, market, store
from magoo.engine import Snapshot

from conftest import FairValuePrices, template_app
from test_alchemy import Overlay, add_pipeline, conn  # noqa: F401 — fixture
from test_web_lifecycle import _settings_form

HUB = store.BUY_VENUE_HUB
STRUCT = store.BUY_VENUE_STRUCTURE
COMPRESSED_VELDSPAR = 62516
COMPRESSED_BITUMENS = 62454
COMPRESSED_C50 = 62399
COMPRESSED_GLACIAL_MASS = 28438  # ice: never a candidate
TRITANIUM = 34
PYERITE = 35
HYDROCARBONS = 16633
FULLERITE_C50 = 30370


def enable_compressed(conn, ore=0.75, gas=0.60, tax=0.0, groups=None):
    """Turn the group toggles on (all three by default, or the given
    config group ids) with the yields and tax."""
    groups = set(config.COMPRESSED_SOURCE_GROUPS if groups is None else groups)
    conn.execute(
        "UPDATE settings SET compressed_minerals_enabled = ?, "
        "compressed_moon_enabled = ?, compressed_gas_enabled = ?, "
        "compressed_ore_yield = ?, compressed_gas_yield = ?, "
        "compressed_reprocess_tax = ?",
        (
            int(config.COMPRESSED_MINERALS_GROUP in groups),
            int(config.COMPRESSED_MOON_GROUP in groups),
            int(config.COMPRESSED_GAS_SOURCE_GROUP in groups),
            ore, gas, tax,
        ),
    )
    conn.commit()


def snapshot(ref, ladders=None, overrides=None, slots=500):
    """FairValuePrices (raws at 10.0) plus the compressed candidates'
    per-venue sell ladders — {venue: {type_id: [(price, volume), ...]}}."""
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=FairValuePrices(ref, overrides=overrides),
        adjusted_prices=Overlay(),
        sell_ladders=ladders or {},
    )


def compressed_rows(plan):
    return [i for i in plan.items.values() if i.compressed_outputs]


# --- reference data -------------------------------------------------------


def test_candidates_are_data_derived(ref):
    sources = ref.compressed_sources()
    if not sources:
        pytest.skip("reference data imported before v1.25 (no ref_compressible)")
    veldspar = sources[COMPRESSED_VELDSPAR]
    assert veldspar.kind == "ore"
    assert veldspar.portion_size == 100
    assert veldspar.outputs == ((TRITANIUM, 400),)
    assert veldspar.per_unit(TRITANIUM, 0.75) == pytest.approx(3.0)
    assert veldspar.batch_output(2, TRITANIUM, 0.75) == 600
    assert veldspar.per_unit(PYERITE, 0.75) == 0.0
    # Moon ore yields minerals AND moon materials from one batch.
    bitumens = sources[COMPRESSED_BITUMENS]
    outs = dict(bitumens.outputs)
    assert outs[PYERITE] == 6000 and outs[HYDROCARBONS] == 65
    # Compressed gas is 1:1 per single unit.
    c50 = sources[COMPRESSED_C50]
    assert (c50.kind, c50.portion_size, c50.outputs) == (
        "gas", 1, ((FULLERITE_C50, 1),)
    )
    assert c50.per_unit(FULLERITE_C50, 0.6) == pytest.approx(0.6)
    # Ice never qualifies (its outputs are ice products), and the one
    # target with no fixed outputs is absent.
    assert COMPRESSED_GLACIAL_MASS not in ref.compressed_sources_for(
        {TRITANIUM, HYDROCARBONS, FULLERITE_C50}
    )
    assert 90307 not in sources
    kinds = {}
    for s in sources.values():
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    assert kinds["gas"] >= 20 and kinds["ore"] >= 150


def test_candidates_for_a_demand_set(ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    for_trit = ref.compressed_sources_for({TRITANIUM})
    assert COMPRESSED_VELDSPAR in for_trit
    assert all(
        any(m == TRITANIUM for m, _q in s.outputs) for s in for_trit.values()
    )
    # A demand set outside the three groups yields nothing.
    assert ref.compressed_sources_for({ref.type_id("Hulk")}) == {}
    assert ref.compressed_sources_for(set()) == {}


def test_candidates_empty_on_a_pre_v125_database(tmp_path):
    from magoo.refdata import Refdata

    c = sqlite3.connect(tmp_path / "old.sqlite")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE ref_type (type_id INTEGER PRIMARY KEY, name TEXT, "
        "group_id INTEGER, category_id INTEGER, volume REAL, "
        "packaged_volume REAL, published INTEGER);"
        "INSERT INTO ref_type VALUES (34, 'Tritanium', 18, 4, 0.01, NULL, 1);"
    )
    r = Refdata(c)
    assert r.compressed_sources() == {}
    assert r.type_info(34).portion_size == 1
    c.close()


# --- costing: the ladder fill ----------------------------------------------


def test_fill_ladder_walks_cheapest_first():
    ladder = [(12.0, 50), (10.0, 100), (11.0, 30)]
    fill = costing.fill_ladder(ladder, 120)
    assert (fill.units, fill.orders, fill.marginal_price) == (120, 2, 11.0)
    assert fill.cost == pytest.approx(100 * 10.0 + 20 * 11.0)
    assert fill.average == pytest.approx(fill.cost / 120)
    # Short ladder: partial fill, average over what was filled.
    short = costing.fill_ladder(ladder, 1000)
    assert (short.units, short.orders, short.marginal_price) == (180, 3, 12.0)
    empty = costing.fill_ladder([], 5)
    assert (empty.units, empty.cost, empty.orders, empty.marginal_price) == (
        0, 0.0, 0, None,
    )
    assert empty.average is None
    assert costing.fill_ladder(ladder, 0).units == 0


def test_freight_lines_skip_landed_material_lines(ref):
    settings = store.Settings(
        0.05, 24.0, 1, 10000002, "sell", freight_in_isk_per_m3=100.0
    )
    raw = costing.CostLine(
        type_id=TRITANIUM, name="Tritanium", kind="material", depth=1,
        qty_per_hull=1000.0, unit_cost=10.0, lag_runs=0, clamped=False,
        venue=HUB,
    )
    landed = costing.CostLine(
        type_id=PYERITE, name="Pyerite", kind="material", depth=1,
        qty_per_hull=1000.0, unit_cost=9.0, lag_runs=0, clamped=False,
        venue=None, landed=True,
    )
    lines = costing._freight_in_lines(settings, ref, [raw, landed])
    assert len(lines) == 1
    assert lines[0].qty_per_hull == pytest.approx(
        1000.0 * ref.type_info(TRITANIUM).freight_volume
    )


# --- market: the hub ladder ---------------------------------------------------


class _FakeClient:
    def __init__(self, orders):
        self.orders = orders

    def get(self, url, params=None, headers=None):
        return httpx.Response(
            200,
            json=self.orders,
            headers={"X-Pages": "1"},
            request=httpx.Request("GET", url),
        )


def test_hub_ladder_quote_keeps_hub_station_rungs_in_price_order():
    orders = [
        {"price": 5.0, "location_id": 60000001, "volume_remain": 999},  # backwater
        {"price": 9.0, "location_id": config.JITA_44_STATION_ID, "volume_remain": 40},
        {"price": 8.5, "location_id": config.JITA_44_STATION_ID, "volume_remain": 10},
        {"price": 8.0, "location_id": config.JITA_44_STATION_ID, "volume_remain": 0},
    ]
    price, hub, ladder = market._hub_ladder_quote(
        _FakeClient(orders), config.THE_FORGE_REGION_ID, COMPRESSED_VELDSPAR,
        "sell", threading.Event(), None,
    )
    # The quote keeps the ordinary single-price contract (min over every
    # hub order — ESI never lists a zero-volume order in practice); the
    # ladder drops the empty rung.
    assert (price, hub) == (8.0, 1)
    assert ladder == [(8.5, 10, 1), (9.0, 40, 1)]
    assert market._order_prices(
        _FakeClient(orders), config.THE_FORGE_REGION_ID, COMPRESSED_VELDSPAR,
        "sell", threading.Event(),
    ) == (8.0, 5.0)


def test_hub_ladder_is_capped(monkeypatch):
    monkeypatch.setattr(config, "HUB_LADDER_MAX_RUNGS", 2)
    orders = [
        {"price": p, "location_id": config.JITA_44_STATION_ID, "volume_remain": 1}
        for p in (3.0, 1.0, 2.0)
    ]
    assert market._hub_ladder(orders, config.THE_FORGE_REGION_ID) == [
        (1.0, 1, 1), (2.0, 1, 1),
    ]


def test_refresh_persists_ladders_for_ladder_types_only(conn, monkeypatch):
    settings = store.get_settings(conn)
    region = settings.price_region_id
    monkeypatch.setattr(
        market, "_best_order_price", lambda *a, **k: 7.0
    )
    monkeypatch.setattr(
        market,
        "_hub_ladder_quote",
        lambda *a, **k: (8.5, 1, [(8.5, 10, 1), (9.0, 40, 1)]),
    )
    fetched, skipped, fresh = market.refresh_prices(
        conn, region, [TRITANIUM, COMPRESSED_VELDSPAR],
        ladder_type_ids=[COMPRESSED_VELDSPAR],
    )
    assert (fetched, skipped, fresh) == (2, 0, 0)
    assert market.cached_prices(conn, region, [TRITANIUM, COMPRESSED_VELDSPAR], "sell") == {
        TRITANIUM: 7.0, COMPRESSED_VELDSPAR: 8.5,
    }
    ladders = market.cached_hub_ladders(conn, region, [TRITANIUM, COMPRESSED_VELDSPAR])
    assert ladders == {COMPRESSED_VELDSPAR: [(8.5, 10, 1), (9.0, 40, 1)]}
    # Fresh price but no ladder rows yet (the toggle came on inside the
    # cache window): the ladder type is refetched, the other stays fresh.
    conn.execute("DELETE FROM hub_sell_order")
    conn.commit()
    monkeypatch.setattr(
        market, "_hub_ladder_quote", lambda *a, **k: (8.0, 1, [(8.0, 5, 1)])
    )
    fetched, skipped, fresh = market.refresh_prices(
        conn, region, [TRITANIUM, COMPRESSED_VELDSPAR],
        ladder_type_ids=[COMPRESSED_VELDSPAR],
    )
    assert (fetched, fresh) == (1, 1)
    assert market.cached_hub_ladders(conn, region, [COMPRESSED_VELDSPAR]) == {
        COMPRESSED_VELDSPAR: [(8.0, 5, 1)]
    }
    # compressed_ladders: hub always, structure only with the comparison on.
    conn.execute("UPDATE settings SET structure_buy_enabled = 0")
    conn.commit()
    both = market.compressed_ladders(
        conn, store.get_settings(conn), [COMPRESSED_VELDSPAR]
    )
    assert both[HUB] == {COMPRESSED_VELDSPAR: [(8.0, 5, 1)]}
    assert both[STRUCT] == {}


# --- engine: the compressed pass ---------------------------------------------


def _hulk(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)


def test_toggle_off_changes_nothing(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 1_000_000)]}}
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    assert not compressed_rows(plan)
    assert plan.compressed_saving_isk is None
    assert plan.items[TRITANIUM].recommended_buy_qty == baseline.items[TRITANIUM].recommended_buy_qty
    assert plan.items[TRITANIUM].compressed_covered_qty == 0
    assert engine.compressed_candidate_ids(conn, ref) == set()
    assert COMPRESSED_VELDSPAR not in engine.demand_type_ids(conn, ref)


def test_short_ladder_records_the_wanted_quantity(conn, ref):
    """Ruling R6 (2026-09-05): when the venue's ladder holds fewer whole
    batches than the LP wanted, the engine keeps the wanted whole-batch
    quantity in compressed_wanted_qty (the web layer's shallow badge)
    and compressed_ladder_units stays the ladder's true depth."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 250)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    assert ore.recommended_buy_qty == 200  # two whole batches of the 250
    assert ore.compressed_ladder_units == 250
    assert ore.compressed_wanted_qty % 100 == 0
    assert ore.compressed_wanted_qty > ore.recommended_buy_qty
    # A three-element (price, volume, min_volume) ladder row walks the
    # same (contract C3).
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 250, 1)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    assert (ore.recommended_buy_qty, ore.compressed_ladder_units) == (200, 250)


def test_cheap_ladder_substitutes_whole_batches(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    assert COMPRESSED_VELDSPAR in engine.compressed_candidate_ids(conn, ref)
    assert COMPRESSED_VELDSPAR in engine.market_type_ids(conn, ref)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    demand = baseline.items[TRITANIUM].recommended_buy_qty
    assert demand > 0
    # Deep enough for the whole demand (a Hulk cycle wants tens of
    # millions of Tritanium — ~9 M ore units).
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    rows = compressed_rows(plan)
    assert [r.type_id for r in rows] == [COMPRESSED_VELDSPAR]
    ore = rows[0]
    assert ore.recommended_action == "buy"
    assert ore.recommended_buy_qty % 100 == 0
    assert ore.price_snapshot == pytest.approx(20.0)
    assert ore.buy_venue == HUB
    assert ore.compressed_fill_orders == 1
    assert ore.compressed_ladder_units == 100_000_000
    (material, out, used), = ore.compressed_outputs
    assert material == TRITANIUM
    assert out == ore.recommended_buy_qty // 100 * 300
    # Whole batches round UP, so the ore covers every unit of demand and
    # the leftover is the rounding surplus.
    trit = plan.items[TRITANIUM]
    assert trit.compressed_covered_qty == demand == used
    assert out - used < 300
    assert trit.recommended_buy_qty == 0
    assert trit.recommended_action is None
    assert trit.deficit_qty == baseline.items[TRITANIUM].deficit_qty
    # Blended landed cost: the ore's landed fill split over the covered
    # units — about 20 / 3 per Tritanium, always below the direct 10.
    assert 6.0 < trit.effective_unit_cost < 7.5
    assert plan.compressed_saving_isk == pytest.approx(
        demand * 10.0 - ore.recommended_buy_qty * 20.0
    )
    assert plan.compressed_saving_isk > 0
    # Nothing else moved: the other raws keep their direct buys.
    for type_id, item in baseline.items.items():
        if type_id != TRITANIUM and not item.buildable:
            assert plan.items[type_id].recommended_buy_qty == item.recommended_buy_qty


def test_dear_or_tied_ladder_keeps_the_raw(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    for price in (40.0, 30.0):  # 13.3 and exactly 10.0 per Tritanium
        ladders = {HUB: {COMPRESSED_VELDSPAR: [(price, 1_000_000)]}}
        plan = engine.plan_index_run(
            conn, ref, snapshot(ref, ladders), persist=False
        )
        assert not compressed_rows(plan), price
        assert plan.items[TRITANIUM].recommended_buy_qty == baseline.items[TRITANIUM].recommended_buy_qty
        assert plan.items[TRITANIUM].effective_unit_cost is None


def test_shallow_ladder_covers_what_it_can(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    demand = baseline.items[TRITANIUM].recommended_buy_qty
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 250), (22.0, 300)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    assert ore.recommended_buy_qty == 500  # 550 on the ladder → 5 whole batches
    assert ore.compressed_fill_orders == 2
    assert ore.price_snapshot == pytest.approx((250 * 20.0 + 250 * 22.0) / 500)
    trit = plan.items[TRITANIUM]
    assert trit.compressed_covered_qty == 1500
    assert trit.recommended_buy_qty == demand - 1500
    assert trit.recommended_action == "buy"
    assert 9.0 < trit.effective_unit_cost < 10.0


def test_moon_ore_covers_minerals_and_goo_in_one_row(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    add_pipeline(conn, ref, "Ishtar", 4)
    enable_compressed(conn)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    assert baseline.items[PYERITE].recommended_buy_qty > 0
    assert baseline.items[HYDROCARBONS].recommended_buy_qty > 0
    # 100 units yield 4,500 Pyerite + 300 Mexallon + 48 Hydrocarbons at
    # 0.75 — worth ~485 at 10 each, so 100 ISK a unit is a bargain.
    ladders = {HUB: {COMPRESSED_BITUMENS: [(100.0, 10_000_000)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    assert ore.type_id == COMPRESSED_BITUMENS
    used = {m: u for m, _o, u in ore.compressed_outputs}
    assert used[PYERITE] > 0 and used[HYDROCARBONS] > 0
    assert plan.items[PYERITE].compressed_covered_qty == used[PYERITE]
    assert plan.items[HYDROCARBONS].compressed_covered_qty == used[HYDROCARBONS]
    # Surplus is uncounted: coverage never exceeds demand, and the
    # accounting reconciles output = used + leftover per material.
    for m, out, u in ore.compressed_outputs:
        assert 0 <= u <= out
        if m in plan.items:
            assert u <= baseline.items[m].recommended_buy_qty


def test_reprocessing_tax_is_charged_on_output_value(conn, ref):
    """Compressed Veldspar at 20 ISK yields 3 Tritanium a unit (6.67 per
    Tritanium). A 20% tax on the 10-ISK outputs adds 2.00 per Tritanium
    (still cheaper); a 50% tax adds 5.00 and loses to buying direct."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]}}
    enable_compressed(conn, tax=0.2)
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    trit = plan.items[TRITANIUM]
    assert trit.compressed_covered_qty == trit.deficit_qty
    assert 8.5 < trit.effective_unit_cost < 9.0  # 6.67 + 2.00, batch-rounded
    assert ore.price_snapshot == pytest.approx(20.0)  # the tax is not a price
    enable_compressed(conn, tax=0.5)
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    assert not compressed_rows(plan)


def test_gas_is_decompressed_untaxed_and_floored_once(conn, ref):
    """2026-09-06: decompressing gas pays no reprocessing tax in the
    client, and a reprocessing run's output is floored once over the job.
    Fulleroferrocene (reaction) consumes 200 Fullerite-C50 a run;
    Compressed Fullerite-C50 at 8.0 decompresses 1:1 at the 95% yield —
    8.42 per gas unit against 10 direct. A 50% tax would add 4.75 and
    lose if it applied to gas; the per-batch floor would value every unit
    at zero output (floor(0.95)) and drop the candidate entirely."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    add_pipeline(conn, ref, "Fulleroferrocene", 20_000)
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 0")
    conn.commit()
    enable_compressed(conn, gas=0.95, tax=0.5)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    demand = baseline.items[FULLERITE_C50].recommended_buy_qty
    assert demand > 0
    ladders = {HUB: {COMPRESSED_C50: [(8.0, 1_000_000)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    gas, = compressed_rows(plan)
    assert gas.type_id == COMPRESSED_C50
    (material, out, used), = gas.compressed_outputs
    assert material == FULLERITE_C50
    # Whole-job floor: the units bought at 95%, rounded down once.
    import math
    assert out == math.floor(gas.recommended_buy_qty * 0.95)
    assert out >= demand and out - used < 1 / 0.95 + 1
    raw = plan.items[FULLERITE_C50]
    assert raw.compressed_covered_qty == used == demand
    assert raw.recommended_buy_qty == 0
    # No tax on gas: about 8 / 0.95 per unit, nowhere near 8.42 + 4.75.
    assert 8.3 < raw.effective_unit_cost < 8.6
    assert plan.compressed_saving_isk == pytest.approx(
        demand * 10.0 - gas.recommended_buy_qty * 8.0
    )


def test_group_toggles_limit_the_candidates(conn, ref):
    """Each toggle admits the compressed TYPES that yield its group's raws:
    gas-only sourcing never considers Compressed Veldspar, minerals-only
    does."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    assert COMPRESSED_C50 in ref.compressed_sources_for({FULLERITE_C50})
    assert COMPRESSED_C50 not in ref.compressed_sources_for(
        {FULLERITE_C50}, {config.COMPRESSED_MINERALS_GROUP}
    )
    _hulk(conn, ref)
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]}}
    enable_compressed(conn, groups={config.COMPRESSED_GAS_SOURCE_GROUP})
    assert COMPRESSED_VELDSPAR not in engine.compressed_candidate_ids(conn, ref)
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    assert not compressed_rows(plan)
    enable_compressed(conn, groups={config.COMPRESSED_MINERALS_GROUP})
    assert COMPRESSED_VELDSPAR in engine.compressed_candidate_ids(conn, ref)
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    assert [i.type_id for i in compressed_rows(plan)] == [COMPRESSED_VELDSPAR]


def test_candidate_outputs_of_other_groups_still_count(conn, ref):
    """Moon ore on, minerals off: Compressed Bitumens is a candidate for
    its Hydrocarbons and still covers the Pyerite and Mexallon it yields
    (user ruling 2026-09-05)."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    add_pipeline(conn, ref, "Ishtar", 4)
    enable_compressed(conn, groups={config.COMPRESSED_MOON_GROUP})
    assert COMPRESSED_BITUMENS in engine.compressed_candidate_ids(conn, ref)
    assert COMPRESSED_VELDSPAR not in engine.compressed_candidate_ids(conn, ref)
    ladders = {HUB: {COMPRESSED_BITUMENS: [(100.0, 10_000_000)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    used = {m: u for m, _o, u in ore.compressed_outputs}
    assert used[HYDROCARBONS] > 0 and used[PYERITE] > 0
    assert plan.items[PYERITE].compressed_covered_qty == used[PYERITE]
    assert plan.items[PYERITE].effective_unit_cost is not None


def test_zero_yield_disables_that_kind(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn, ore=0.0)
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(1.0, 1_000_000)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    assert not compressed_rows(plan)


def test_structure_ladder_wins_when_cheaper_landed(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    ladders = {
        HUB: {COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]},
        STRUCT: {COMPRESSED_VELDSPAR: [(19.0, 100_000_000)]},
    }
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    assert ore.buy_venue == STRUCT
    assert ore.price_snapshot == pytest.approx(19.0)
    # A structure freight leg that erases the edge sends it back to Jita.
    conn.execute(
        "UPDATE settings SET structure_freight_in_isk_per_m3 = ?",
        (10.0 / ref.type_info(COMPRESSED_VELDSPAR).freight_volume,),
    )
    conn.commit()
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=False)
    ore, = compressed_rows(plan)
    assert ore.buy_venue == HUB


def test_steady_state_never_sources_compressed(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 1_000_000)]}}
    plan = engine.plan_steady_state(conn, ref, snapshot(ref, ladders))
    assert not compressed_rows(plan)
    assert plan.items[TRITANIUM].compressed_covered_qty == 0


def test_persistence_and_realized_costing(conn, ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    _hulk(conn, ref)
    enable_compressed(conn)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    ladders = {HUB: {COMPRESSED_VELDSPAR: [(20.0, 1_000_000)]}}
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=True)
    run = conn.execute(
        "SELECT * FROM index_run WHERE index_run_id = ?", (plan.index_run_id,)
    ).fetchone()
    assert run["compressed_saving_isk"] == pytest.approx(plan.compressed_saving_isk)
    ore = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, COMPRESSED_VELDSPAR),
    ).fetchone()
    outputs = json.loads(ore["compressed_outputs"])
    assert outputs[0][0] == TRITANIUM and outputs[0][2] > 0
    assert ore["compressed_ladder_units"] == 1_000_000
    assert ore["compressed_fill_orders"] == 1
    assert ore["recommended_buy_qty"] == plan.items[COMPRESSED_VELDSPAR].recommended_buy_qty
    # Ruling R6: the LP-wanted whole-batch quantity is persisted beside
    # the buy; a deep ladder filled every wanted unit.
    assert ore["compressed_wanted_qty"] == plan.items[COMPRESSED_VELDSPAR].compressed_wanted_qty
    assert ore["compressed_wanted_qty"] == ore["recommended_buy_qty"] > 0
    trit = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (plan.index_run_id, TRITANIUM),
    ).fetchone()
    assert trit["compressed_covered_qty"] == plan.items[TRITANIUM].compressed_covered_qty
    assert trit["effective_unit_cost"] == pytest.approx(
        plan.items[TRITANIUM].effective_unit_cost
    )
    assert trit["compressed_outputs"] is None
    # The compressed row belongs to no pipeline — the realized costing
    # prices Tritanium at the blended landed figure instead, freight-free.
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 500.0")
    conn.commit()
    pipeline_id = conn.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    cost = costing.hull_cost(
        conn, ref, store.get_settings(conn), plan.index_run_id, pipeline_id
    )
    line = next(l for l in cost.lines if l.type_id == TRITANIUM)
    assert line.landed is True
    assert line.unit_cost == pytest.approx(plan.items[TRITANIUM].effective_unit_cost)
    assert line.venue is None
    assert not any(l.type_id == COMPRESSED_VELDSPAR for l in cost.lines)
    freight = [l for l in cost.lines if l.kind == "freight_in"]
    trit_m3 = line.qty_per_hull * ref.type_info(TRITANIUM).freight_volume
    for f in freight:
        # Tritanium's m³ is not in any freight line.
        assert f.qty_per_hull < trit_m3 or trit_m3 == 0


# --- web: buy context, templates, settings -----------------------------------


def test_buy_context_decodes_compressed_rows(ref):
    from magoo.web import _buy_context, _compressed_section

    ore = {
        "type_id": COMPRESSED_VELDSPAR, "name": "Compressed Veldspar",
        "recommended_buy_qty": 500, "price_snapshot": 21.0,
        "recommended_build_qty": 0, "activity_id": None, "group_id": 462,
        "buy_venue": HUB, "structure_units_cheaper": None,
        "price_region_wide": 0, "jobs_allocated": 0,
        "compressed_outputs": json.dumps([[TRITANIUM, 1500, 1200]]),
        "compressed_ladder_units": 400, "compressed_fill_orders": 2,
        "compressed_covered_qty": 0, "effective_unit_cost": None,
        # Ruling R6: the LP wanted 600, the ladder shrank the buy to 500.
        "compressed_wanted_qty": 600,
    }
    trit = {
        "type_id": TRITANIUM, "name": "Tritanium", "recommended_buy_qty": 10,
        "price_snapshot": 10.0, "recommended_build_qty": 0, "activity_id": None,
        "group_id": 18, "buy_venue": HUB, "structure_units_cheaper": None,
        "price_region_wide": 0, "jobs_allocated": 0, "compressed_outputs": None,
        "compressed_ladder_units": None, "compressed_fill_orders": None,
        "compressed_covered_qty": 1200, "effective_unit_cost": 7.0,
        "compressed_wanted_qty": None,
    }
    bc = _buy_context([ore, trit], ref)
    assert bc["compressed"] == {
        COMPRESSED_VELDSPAR: [(TRITANIUM, "Tritanium", 1500, 1200)]
    }
    assert bc["compressed_covered"] == {TRITANIUM: 1200}
    assert bc["compressed_shallow"] == {COMPRESSED_VELDSPAR}  # wanted 600 > 500 bought
    assert "Compressed Veldspar 500" in bc["multibuy_hub"]
    section = _compressed_section(ref, [ore, trit], bc["compressed"])
    assert section[0]["outputs"] == [
        {"name": "Tritanium", "out": 1500, "used": 1200, "leftover": 300}
    ]


def test_run_detail_renders_compressed_section_and_badges(ref):
    from flask import render_template

    from test_buy_venue import _buy_row, settings_with

    ore = _buy_row("Compressed Veldspar", COMPRESSED_VELDSPAR, 500, price=21.0)
    ore.update(
        compressed_outputs=json.dumps([[TRITANIUM, 1500, 1200]]),
        compressed_ladder_units=400, compressed_fill_orders=2,
        compressed_covered_qty=0, effective_unit_cost=None,
        compressed_wanted_qty=600,  # ruling R6: the LP wanted 600
    )
    trit = _buy_row("Tritanium", TRITANIUM, 10)
    trit.update(
        compressed_outputs=None, compressed_ladder_units=None,
        compressed_fill_orders=None, compressed_covered_qty=1200,
        effective_unit_cost=7.0,
    )
    run = {"run_number": 7, "status": "planned", "planned_start": "2026-09-05",
           "index_run_id": 1, "wallet_character_isk": 1e9,
           "wallet_corporation_isk": 2e9, "completed_at": None,
           "compressed_saving_isk": 1500.0}
    settings = settings_with(
        manufacturing_slots=50, reaction_slots=50,
        compressed_minerals_enabled=True,
    )
    ctx = dict(
        run=run, items=[ore, trit], final_net_margin={}, buys=[ore, trit],
        builds=[], reactions=[], builds_grouped=[], reactions_grouped=[],
        struct_builds=[], struct_buys=[], struct_slots=0, chain_struct=[],
        alchemy=[], alchemy_yield=0.55, chain_rows=[], chain_raws=[],
        chain_mfg=[], chain_reactions=[],
        chain_counts={"covered": 0, "buy": 0, "build": 0, "react": 0, "alchemy": 0},
        unmet=[], low_stock=[], buy_total=500 * 21.0 + 100.0, buys_unpriced=0,
        multibuy_hub="Compressed Veldspar 500\nTritanium 10",
        multibuy_structure="", structure_buys=set(), shallow=set(),
        settings=settings, mfg_slots_used=0, reaction_slots_used=0,
        alchemy_slots_used=0, region_wide=set(),
        compressed={COMPRESSED_VELDSPAR: [(TRITANIUM, "Tritanium", 1500, 1200)]},
        compressed_covered={TRITANIUM: 1200},
        compressed_shallow={COMPRESSED_VELDSPAR},
        compressed_section=[{
            "item": ore,
            "outputs": [{"name": "Tritanium", "out": 1500, "used": 1200, "leftover": 300}],
        }],
        compressed_saving=1500.0,
    )
    app = template_app()
    with app.test_request_context("/runs/1"):
        html = render_template("run_detail.html", **ctx)
    assert "1 via compressed" in html
    assert ">compressed</span>" in html
    assert "covers 1,200 Tritanium; leftover 300 Tritanium" in html
    assert "1,200 via compressed" in html
    assert ">shallow</span>" in html
    assert "the ladder held only 500 of the 600 units the plan wanted" in html
    assert "Compressed sourcing" in html
    assert "Reprocess <b>500 Compressed Veldspar</b>" in html
    assert "~1,200 Tritanium" in html and "leftover +300 Tritanium" in html
    # The v1.25.1 defaults, rendered exactly (the pct filter — 90.63, not
    # a whole-percent rounding): 90.63% ore, 95% gas, 4% tax on refined
    # ore only.
    assert "90.63% for ore" in html and "95% for gas" in html
    assert "4% reprocessing tax on every refined-ore output" in html
    assert ">Compressed Veldspar 500\nTritanium 10</textarea>" in html


def test_settings_save_and_render(seeded_client, ref):
    page = seeded_client.get("/settings").get_data(as_text=True)
    assert 'id="compressed_minerals_enabled"' in page
    assert 'id="compressed_gas_enabled"' in page
    assert "Compressed Sourcing" in page
    resp = seeded_client.post(
        "/settings",
        data=_settings_form(
            compressed_minerals_enabled="1",
            compressed_gas_enabled="1",
            compressed_ore_yield_pct="82.5",
            compressed_gas_yield_pct="150",  # clamped to 100%
            compressed_tax_pct="2.5",
        ),
    )
    assert resp.status_code == 302
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    s = store.get_settings(c)
    assert s.compressed_sourcing_enabled is True
    assert (s.compressed_minerals_enabled, s.compressed_moon_enabled, s.compressed_gas_enabled) == (True, False, True)
    assert s.compressed_groups() == {18, 711}
    assert s.compressed_ore_yield == pytest.approx(0.825)
    assert s.compressed_gas_yield == pytest.approx(1.0)
    assert s.compressed_reprocess_tax == pytest.approx(0.025)
    seeded_client.post("/settings", data=_settings_form())
    assert store.get_settings(c).compressed_sourcing_enabled is False
    c.close()


def test_run_route_sources_compressed_end_to_end(seeded_client, ref):
    """POST /run with the toggle on and a seeded Jita ladder: the plan
    persists a compressed row and the run page shows the section."""
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25")
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    enable_compressed(c)
    settings = store.get_settings(c)
    # A cheap ladder plus a cached best price for the candidate.
    c.execute(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at, hub) "
        "VALUES (?, ?, ?, ?, datetime('now'), 1)",
        (COMPRESSED_VELDSPAR, settings.price_region_id, settings.price_source, 20.0),
    )
    c.execute(
        "INSERT INTO hub_sell_order (region_id, type_id, price, volume_remain) "
        "VALUES (?, ?, 20.0, 1000000)",
        (settings.price_region_id, COMPRESSED_VELDSPAR),
    )
    c.commit()
    assert seeded_client.post("/run").status_code == 302
    run_id = c.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]
    ore = c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, COMPRESSED_VELDSPAR),
    ).fetchone()
    assert ore is not None and ore["recommended_buy_qty"] % 100 == 0
    page = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "via compressed" in page
    assert "Reprocess <b>" in page and "Compressed Veldspar" in page
    chain = seeded_client.get(f"/runs/{run_id}?view=chain").get_data(as_text=True)
    assert ">compressed</span>" in chain
    c.close()
