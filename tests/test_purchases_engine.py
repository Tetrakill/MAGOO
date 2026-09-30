"""Buy tab (v1.29), engine half: the compressed pass records what the
realized costing needs to re-blend a covered raw from what the user
actually paid.

Four new `index_run_item` columns (schema 12). On a compressed ore row:
`compressed_alloc` (JSON `{raw_type_id: share}`), `compressed_landed_isk`
and `compressed_tax_isk`. On a covered raw row: `direct_landed_isk`. The
identity the Buy tab's re-blend rests on, and which every test here
asserts, is

    effective_unit_cost * (recommended_buy_qty + compressed_covered_qty)
        == direct_landed_isk + Σ_c compressed_landed_isk_c * alloc_c[raw]

Same fixture pattern as `test_compressed`: real reference data (the
production SDE, read-only), temp state DB, FairValuePrices (every raw
costs 10.0), Compressed Veldspar (62516 → 400 Tritanium per 100 units) as
the worked example.
"""

import json

import pytest

from magoo import config, engine, store

from test_alchemy import add_pipeline, conn  # noqa: F401 — fixture
from test_compressed import (
    COMPRESSED_BITUMENS,
    COMPRESSED_C50,
    COMPRESSED_VELDSPAR,
    FULLERITE_C50,
    HUB,
    STRUCT,
    TRITANIUM,
    _hulk,
    compressed_rows,
    enable_compressed,
    snapshot,
)

FOUR_COLUMNS = (
    "compressed_alloc",
    "compressed_landed_isk",
    "compressed_tax_isk",
    "direct_landed_isk",
)


def _skip_without_refdata(ref):
    if not ref.compressed_sources():
        pytest.skip("reference data imported before v1.25 (no ref_compressible)")


def _set_freight(conn, rate: float) -> None:
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = ?", (rate,))
    conn.commit()


def _items(conn, index_run_id: int) -> dict[int, "object"]:
    """The persisted rows by type_id."""
    return {
        row["type_id"]: row
        for row in conn.execute(
            "SELECT * FROM index_run_item WHERE index_run_id = ?", (index_run_id,)
        )
    }


def _alloc(row) -> dict[int, float]:
    """Decode a persisted `compressed_alloc` (JSON object keys are text)."""
    return {int(k): v for k, v in json.loads(row["compressed_alloc"]).items()}


def _assert_identity(rows, raw_type_id: int) -> None:
    """The §2 identity over the PERSISTED numbers, for one covered raw."""
    raw = rows[raw_type_id]
    demand = raw["recommended_buy_qty"] + raw["compressed_covered_qty"]
    assert demand > 0
    allocated = 0.0
    for row in rows.values():
        if row["compressed_alloc"] is None:
            continue
        share = _alloc(row).get(raw_type_id, 0.0)
        allocated += row["compressed_landed_isk"] * share
    assert raw["effective_unit_cost"] * demand == pytest.approx(
        raw["direct_landed_isk"] + allocated
    )


# --- the in-memory plan -------------------------------------------------------


def test_plan_item_defaults_to_none():
    item = engine.PlanItem(type_id=34, name="Tritanium", item_class="raw", depth=1)
    assert item.compressed_alloc is None
    assert item.compressed_landed_isk is None
    assert item.compressed_tax_isk is None
    assert item.direct_landed_isk is None


def test_ore_row_carries_the_shares_landed_cost_and_tax(conn, ref):
    """One ore covering one raw: its whole landed cost is allocated to
    that raw, and the landed cost is the fill plus freight plus the tax
    term (which is NOT in `price_snapshot`)."""
    _skip_without_refdata(ref)
    _hulk(conn, ref)
    enable_compressed(conn, tax=0.2)
    _set_freight(conn, 500.0)
    settings = store.get_settings(conn)
    snap = snapshot(ref, {HUB: {COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]}})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    ore, = compressed_rows(plan)
    assert ore.type_id == COMPRESSED_VELDSPAR

    assert ore.compressed_alloc == {TRITANIUM: pytest.approx(1.0)}
    m3 = ref.type_info(COMPRESSED_VELDSPAR).freight_volume
    order_isk = ore.price_snapshot * ore.recommended_buy_qty
    freight = 500.0 * m3 * ore.recommended_buy_qty
    assert ore.compressed_landed_isk == pytest.approx(
        order_isk + freight + ore.compressed_tax_isk
    )
    # The tax is charged on every output at its landed value (A8), so it
    # is the SDE yield times the raw's landed price times the rate.
    (_m, out, _used), = ore.compressed_outputs
    landed_trit = engine._landed_price(ref, settings, snap, TRITANIUM)
    assert ore.compressed_tax_isk == pytest.approx(0.2 * out * landed_trit)
    assert ore.compressed_tax_isk > 0
    # The ore itself is bought direct: no remainder of its own.
    assert ore.direct_landed_isk is None

    trit = plan.items[TRITANIUM]
    assert trit.direct_landed_isk is not None
    assert trit.compressed_alloc is None
    assert trit.effective_unit_cost * (
        trit.recommended_buy_qty + trit.compressed_covered_qty
    ) == pytest.approx(
        trit.direct_landed_isk + ore.compressed_landed_isk * ore.compressed_alloc[TRITANIUM]
    )


def test_a_fully_covered_raw_has_a_zero_direct_term(conn, ref):
    """A raw the pass covers outright buys nothing direct, so its
    remainder is 0.0 ISK — persisted as 0.0, not NULL, or the identity
    could not be told apart from a pre-v1.29 row."""
    _skip_without_refdata(ref)
    add_pipeline(conn, ref, "Fulleroferrocene", 20_000)
    _set_freight(conn, 0.0)
    enable_compressed(conn, gas=0.95, tax=0.5)
    snap = snapshot(ref, {HUB: {COMPRESSED_C50: [(8.0, 1_000_000)]}})
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    gas, = compressed_rows(plan)
    raw = plan.items[FULLERITE_C50]
    assert raw.recommended_buy_qty == 0
    assert raw.direct_landed_isk == 0.0
    # A8: decompressing gas is untaxed — 0.0, never NULL.
    assert gas.compressed_tax_isk == 0.0
    assert gas.compressed_landed_isk == pytest.approx(
        gas.price_snapshot * gas.recommended_buy_qty
    )
    assert raw.effective_unit_cost * raw.compressed_covered_qty == pytest.approx(
        gas.compressed_landed_isk * gas.compressed_alloc[FULLERITE_C50]
    )


# --- persistence --------------------------------------------------------------


def test_persisted_identity_for_a_single_ore(conn, ref):
    _skip_without_refdata(ref)
    _hulk(conn, ref)
    enable_compressed(conn, tax=0.05)
    _set_freight(conn, 500.0)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    snap = snapshot(ref, {HUB: {COMPRESSED_VELDSPAR: [(20.0, 100_000_000)]}})
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    rows = _items(conn, plan.index_run_id)

    ore = rows[COMPRESSED_VELDSPAR]
    assert _alloc(ore) == {TRITANIUM: pytest.approx(1.0)}
    assert ore["compressed_landed_isk"] == pytest.approx(
        plan.items[COMPRESSED_VELDSPAR].compressed_landed_isk
    )
    assert ore["compressed_tax_isk"] == pytest.approx(
        plan.items[COMPRESSED_VELDSPAR].compressed_tax_isk
    )
    assert ore["direct_landed_isk"] is None

    trit = rows[TRITANIUM]
    assert trit["direct_landed_isk"] == pytest.approx(
        plan.items[TRITANIUM].direct_landed_isk
    )
    assert trit["compressed_alloc"] is None
    assert trit["compressed_landed_isk"] is None
    assert trit["compressed_tax_isk"] is None
    _assert_identity(rows, TRITANIUM)

    # Nothing the pass did not touch carries any of the four figures.
    for type_id, row in rows.items():
        if type_id in (COMPRESSED_VELDSPAR, TRITANIUM):
            continue
        assert all(row[c] is None for c in FOUR_COLUMNS), type_id


def test_persisted_identity_for_an_ore_covering_several_raws(conn, ref):
    """Compressed Bitumens yields Pyerite, Mexallon and Hydrocarbons in
    one batch: the shares of the raws it actually covers sum to 1.0 and
    the identity holds for each of them."""
    _skip_without_refdata(ref)
    add_pipeline(conn, ref, "Ishtar", 4)
    enable_compressed(conn, tax=0.05)
    _set_freight(conn, 500.0)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    snap = snapshot(ref, {HUB: {COMPRESSED_BITUMENS: [(100.0, 10_000_000)]}})
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    rows = _items(conn, plan.index_run_id)

    ore = rows[COMPRESSED_BITUMENS]
    shares = _alloc(ore)
    covered = {m for m, _out, used in plan.items[COMPRESSED_BITUMENS].compressed_outputs if used > 0}
    assert len(covered) > 1
    assert set(shares) == covered
    assert sum(shares.values()) == pytest.approx(1.0)
    assert all(0.0 <= s <= 1.0 for s in shares.values())
    for raw_type_id in covered:
        assert rows[raw_type_id]["compressed_covered_qty"] > 0
        _assert_identity(rows, raw_type_id)


def test_two_ores_covering_one_raw_split_the_allocation(conn, ref):
    """v1.26.1's pin case: Compressed Veldspar is pinned to one market and
    Compressed Scordite covers the rest of the Tritanium. Each ore carries
    its own share of that raw and the identity sums over both."""
    _skip_without_refdata(ref)
    _hulk(conn, ref)
    enable_compressed(conn)
    _set_freight(conn, 500.0)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    baseline = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    demand = baseline.items[TRITANIUM].recommended_buy_qty
    scordite = ref.type_id("Compressed Scordite")
    part = -(-int(demand * 0.6 / 3) // 100) * 100
    ladders = {
        HUB: {COMPRESSED_VELDSPAR: [(20.0, part)], scordite: [(11.0, 10**8)]},
        STRUCT: {COMPRESSED_VELDSPAR: [(19.5, part)]},
    }
    plan = engine.plan_index_run(conn, ref, snapshot(ref, ladders), persist=True)
    rows = _items(conn, plan.index_run_id)
    ore_ids = [r.type_id for r in compressed_rows(plan)]
    assert set(ore_ids) == {COMPRESSED_VELDSPAR, scordite}
    for type_id in ore_ids:
        assert 0.0 < _alloc(rows[type_id])[TRITANIUM] <= 1.0
    _assert_identity(rows, TRITANIUM)


def test_no_compressed_coverage_leaves_every_column_null(conn, ref):
    """With the pass off (or no candidate cheap enough) not one row in the
    run carries any of the four columns — the pre-v1.29 shape."""
    _skip_without_refdata(ref)
    _hulk(conn, ref)
    _set_freight(conn, 500.0)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    # Toggles off entirely.
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=True)
    rows = _items(conn, plan.index_run_id)
    assert rows
    for type_id, row in rows.items():
        assert all(row[c] is None for c in FOUR_COLUMNS), type_id

    # On, but the ladder is dearer landed than the raw: no ore stands.
    # Freight-free so the comparison is the bare 13.33 per Tritanium
    # against the direct 10 (compressed ore hauls cheaper per mineral than
    # the mineral does, so 40.0 wins once freight is charged).
    enable_compressed(conn)
    _set_freight(conn, 0.0)
    snap = snapshot(ref, {HUB: {COMPRESSED_VELDSPAR: [(40.0, 1_000_000)]}})
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    assert not compressed_rows(plan)
    rows = _items(conn, plan.index_run_id)
    for type_id, row in rows.items():
        assert all(row[c] is None for c in FOUR_COLUMNS), type_id


# --- the plan-time freight rates (v1.29 revision 4, §2b) ---------------------


def test_run_persists_all_three_plan_time_freight_rates(conn, ref):
    """User ruling 2026-09-28: a purchase anywhere but Jita 4-4 or the
    structure market hauls at the default inbound rate. That rate rides
    the run next to the Jita and structure rates (contract C2's vintage
    rule), so a later settings change never re-prices a planned run."""
    _skip_without_refdata(ref)
    _hulk(conn, ref)
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 500, "
        "structure_freight_in_isk_per_m3 = 300, "
        "freight_in_default_isk_per_m3 = 750"
    )
    conn.commit()
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=True)

    # The live setting moves after planning; the run keeps its vintage.
    conn.execute("UPDATE settings SET freight_in_default_isk_per_m3 = 1234")
    conn.commit()
    row = conn.execute(
        "SELECT freight_in_isk_per_m3, structure_freight_in_isk_per_m3, "
        "freight_in_default_isk_per_m3 FROM index_run WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchone()
    assert tuple(row) == (500.0, 300.0, 750.0)


def test_run_persists_a_zero_default_rate_as_zero_not_null(conn, ref):
    """The setting's DEFAULT 0 is persisted as 0.0: NULL is reserved for
    runs planned before the column existed (the readers' live-setting
    fallback), so a zero-rate run must stay distinguishable from one."""
    _skip_without_refdata(ref)
    _hulk(conn, ref)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    assert store.get_settings(conn).freight_in_default_isk_per_m3 == 0.0
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=True)
    (rate,) = conn.execute(
        "SELECT freight_in_default_isk_per_m3 FROM index_run "
        "WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchone()
    assert rate == 0.0
    assert rate is not None


def test_run_persists_the_structure_market_it_was_planned_against(conn, ref):
    """Review 2026-09-28: the structure market rides the run like the
    rates, so an executed run's purchases keep their venue class
    (buying.purchase_venue) after Settings point at another structure."""
    _skip_without_refdata(ref)
    _hulk(conn, ref)
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=True)
    conn.execute(
        "UPDATE settings SET capital_market_mode = 'custom', "
        "capital_structure_id = 1035466617946"
    )
    conn.commit()
    (sid,) = conn.execute(
        "SELECT structure_market_id FROM index_run WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchone()
    assert sid == config.CJ6_KEEPSTAR_STRUCTURE_ID
    assert store.get_settings(conn).structure_market() == 1035466617946
