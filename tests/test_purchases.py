"""Realized purchases (v1.29 Buy tab) inside lag costing.

What the user actually paid for a run's inputs — one `run_purchase` line
per purchase — wins over the plan's price snapshot inside
`costing._run_snapshot`, so the Profit tab and the Ledger's cost vintages
get it for free. The plan itself is never rewritten.

Runs here are built by direct INSERT into index_run / index_run_item /
index_run_item_pipeline rather than by planning one: the numbers have to
be exact for the identity assertions, and this file must not depend on
the engine's own v1.29 fields landing first. Reference data (packaged
m³) is the real SDE, like the rest of the suite.

The load-bearing promise is the first test: with zero purchase lines
every number is what it was before v1.29, guaranteed by an early return
in `_run_snapshot`, not by arithmetic (contract review A10).

Revision 3 §5 (user ruling 2026-09-28) changed the BASIS the lines are
blended against — max(cycle_need_qty or merged_min_qty, direct +
covered), what the cycle consumes — and dropped the skip of a row the
plan does not buy. No expectation written before revision 3 changed:
`build_run`'s raws set neither cycle_need_qty nor merged_min_qty, so
their basis is exactly revision 2's (the plan's own buy; contract review
C13.6). The tests after "revision 3: the purchase basis" set the need
explicitly to exercise the stock-covered slice and the re-planned
zero-buy row. The lines are still called "locks" here (and `locked` on a
CostLine): the field names predate ESI-derived purchases.

Revision 4 (user ruling 2026-09-28) adds a fourth purchase venue, 'other'
— bought anywhere but Jita 4-4 and the configured structure market —
hauled at the run's default inbound rate on a third freight leg. It also
exempted lines refined from an unplanned ore (`via_type_id`) from the
pre-plan rule; revision 6 (user ruling 2026-09-28) removed that exemption
again — hangar compressed ore now counts as the raws it yields, so
`costing._pre_plan` is a pure date test. Both are at the end of this file.
"""

import json
import sqlite3

import pytest

from magoo import costing, store

HUB = store.BUY_VENUE_HUB
STRUCT = store.BUY_VENUE_STRUCTURE
DELIVERED = store.BUY_VENUE_DELIVERED
SPLIT = store.BUY_VENUE_SPLIT
OTHER = store.BUY_VENUE_OTHER

TRIT = 34
PYERITE = 35
MEXALLON = 36
COMPRESSED_VELDSPAR = 62516
HULK = 22544

# Packaged m³ (asserted below against the SDE so the arithmetic in this
# file stays honest if CCP ever repackages a mineral).
MINERAL_M3 = 0.01
ORE_M3 = 0.001

# Deliberately fat rates: freight has to be visible in every figure.
HUB_RATE = 1000.0
STRUCT_RATE = 200.0

# The compressed fixture, spelled out once (see `build_run`):
#   Mexallon demand 1000 = 200 direct + 800 covered by 100 Compressed
#   Veldspar, which reprocesses into 850 Mexallon.
DIRECT_LANDED = 200 * (5.0 + HUB_RATE * MINERAL_M3)          # 3000.0
ORE_TAX = 0.04 * 850 * 5.0                                   # 170.0
ORE_LANDED = 100 * (5.0 + HUB_RATE * ORE_M3) + ORE_TAX       # 770.0
EFFECTIVE = (DIRECT_LANDED + ORE_LANDED) / 1000              # 3.77


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def settings(conn):
    return store.get_settings(conn)


# --- fixture builders ------------------------------------------------------


def add_pipeline(conn, final_type_id=HULK, output_qty=1):
    cur = conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run) VALUES ('test', ?, ?)",
        (final_type_id, output_qty),
    )
    return cur.lastrowid


def add_run(conn, run_number, hub_rate=HUB_RATE, structure_rate=STRUCT_RATE):
    cur = conn.execute(
        "INSERT INTO index_run (run_number, status, completed_at, "
        "freight_in_isk_per_m3, structure_freight_in_isk_per_m3) "
        "VALUES (?, 'complete', datetime('now'), ?, ?)",
        (run_number, hub_rate, structure_rate),
    )
    return cur.lastrowid


def add_item(
    conn, run_id, type_id, pipeline_id=None, attributable=None, depth=0,
    **columns,
):
    """One index_run_item, optionally attributed to a pipeline. A row with
    no attribution is invisible to hull_cost — which is exactly how a
    compressed ore row sits in a run (contract review A15)."""
    columns = {"index_run_id": run_id, "type_id": type_id, **columns}
    names = ", ".join(columns)
    marks = ", ".join("?" * len(columns))
    cur = conn.execute(
        f"INSERT INTO index_run_item ({names}) VALUES ({marks})",
        tuple(columns.values()),
    )
    if pipeline_id is not None:
        conn.execute(
            "INSERT INTO index_run_item_pipeline (index_run_item_id, "
            "pipeline_id, qty_attributable, depth) VALUES (?, ?, ?, ?)",
            (cur.lastrowid, pipeline_id, attributable, depth),
        )
    return cur.lastrowid


def build_run(conn, run_number=1, pipeline_id=None, unfilled_pyerite=0):
    """One completed run with four bought rows, one of each shape:

    * Tritanium — a plain hub buy, 1000 units at 5.0;
    * Pyerite — a fill-priced SPLIT buy, 600 Jita at 4.0 + 400 C-J6 at
      6.0 (blending to the 4.8 price_snapshot);
    * Mexallon — 200 bought direct, 800 covered by compressed ore, so it
      carries a blended LANDED effective_unit_cost;
    * Compressed Veldspar — the ore covering it, with NO pipeline
      attribution (it reaches a hull only through Mexallon).

    The final (Hulk) is attributed 1 unit, so every material line's
    qty_per_hull is simply its attributable quantity.
    """
    if pipeline_id is None:
        pipeline_id = add_pipeline(conn)
    run_id = add_run(conn, run_number)
    add_item(
        conn, run_id, HULK, pipeline_id, attributable=1, depth=0,
        blueprint_id=987654, portion_size=1, runs_allocated=1,
        install_runs=1, merged_min_qty=1, cycle_need_qty=1,
        unit_install_fee=100.0,
    )
    add_item(
        conn, run_id, TRIT, pipeline_id, attributable=1000, depth=1,
        recommended_buy_qty=1000, recommended_action="buy",
        price_snapshot=5.0, buy_venue=HUB,
    )
    add_item(
        conn, run_id, PYERITE, pipeline_id, attributable=1000, depth=1,
        recommended_buy_qty=1000 + unfilled_pyerite,
        recommended_action="buy",
        price_snapshot=4.8, buy_venue=SPLIT,
        hub_buy_qty=600, structure_buy_qty=400,
        hub_fill_price=4.0, structure_fill_price=6.0,
        unfilled_qty=unfilled_pyerite,
    )
    add_item(
        conn, run_id, MEXALLON, pipeline_id, attributable=1000, depth=1,
        recommended_buy_qty=200, recommended_action="buy",
        price_snapshot=5.0, buy_venue=HUB,
        compressed_covered_qty=800, effective_unit_cost=EFFECTIVE,
        direct_landed_isk=DIRECT_LANDED,
    )
    add_item(
        conn, run_id, COMPRESSED_VELDSPAR,
        recommended_buy_qty=100, recommended_action="buy",
        price_snapshot=5.0, buy_venue=HUB,
        compressed_outputs=json.dumps([[MEXALLON, 850, 800]]),
        compressed_alloc=json.dumps({str(MEXALLON): 1.0}),
        compressed_landed_isk=ORE_LANDED,
        compressed_tax_isk=ORE_TAX,
    )
    conn.commit()
    return pipeline_id, run_id


def lock(conn, run_id, type_id, venue, quantity, price, source=None):
    purchase_id = store.add_purchase(
        conn, run_id, type_id, venue, quantity, price, source=source
    )
    conn.commit()
    return purchase_id


def by_type(cost):
    return {line.type_id: line for line in cost.lines if line.kind == "material"}


def freight(cost):
    return {
        line.name: line.qty_per_hull * line.unit_cost
        for line in cost.lines
        if line.kind == "freight_in"
    }


def fingerprint(cost):
    """Every CostLine field the purchase override could possibly move,
    in a stable order (the item SELECT's row order is not guaranteed)."""
    return sorted(
        (
            (
                line.type_id, line.kind, line.depth,
                round(line.qty_per_hull, 9), round(line.unit_cost, 9),
                line.lag_runs, line.clamped, line.missing_price, line.venue,
                line.landed, line.hub_fraction, line.hub_fill_price,
                line.structure_fill_price, line.structure_fraction,
                line.delivered_fraction, line.delivered_fill_price,
                line.locked, line.approximate,
                # Revision 4: the 'other' share (bought anywhere but Jita
                # 4-4 and the structure market).
                line.other_fraction, line.other_fill_price,
            )
            for line in cost.lines
        ),
        key=lambda row: (row[1], row[0], row[8] or ""),
    )


# --- the identity: nothing locked, nothing moves ---------------------------


def test_packaged_volumes_the_arithmetic_here_assumes(ref):
    assert ref.type_info(TRIT).freight_volume == MINERAL_M3
    assert ref.type_info(PYERITE).freight_volume == MINERAL_M3
    assert ref.type_info(MEXALLON).freight_volume == MINERAL_M3
    assert ref.type_info(COMPRESSED_VELDSPAR).freight_volume == ORE_M3


def test_the_fixture_satisfies_the_plan_time_compressed_identity(conn, ref):
    """effective_unit_cost x demand == direct_landed_isk + the ore's
    landed cost x its allocation share — the identity a v1.29 plan
    persists, and the one the re-blend rebuilds from."""
    build_run(conn)
    row = conn.execute(
        "SELECT * FROM index_run_item WHERE type_id = ?", (MEXALLON,)
    ).fetchone()
    ore = conn.execute(
        "SELECT * FROM index_run_item WHERE type_id = ?",
        (COMPRESSED_VELDSPAR,),
    ).fetchone()
    demand = row["recommended_buy_qty"] + row["compressed_covered_qty"]
    share = json.loads(ore["compressed_alloc"])[str(MEXALLON)]
    assert row["effective_unit_cost"] * demand == pytest.approx(
        row["direct_landed_isk"] + ore["compressed_landed_isk"] * share
    )


def test_a_run_with_no_purchase_lines_costs_exactly_as_before(
    conn, ref, settings
):
    """The bit-identity the feature promises. Both halves: the snapshot
    loader returns the pre-v1.29 rows object-for-object, and every
    CostLine is the number it was — a plain buy, a fill-priced split and
    a compressed-covered raw included."""
    pid, run_id = build_run(conn)
    rates = costing._run_freight_rates(conn, run_id)
    legacy = costing._run_snapshot(conn, run_id)
    assert costing._run_snapshot(
        conn, run_id, ref=ref, rates=rates, settings=settings
    ) == legacy

    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert fingerprint(cost) == [
        # (type, kind, depth, qty/hull, unit cost, lag, clamped,
        #  missing, venue, landed, hub f, hub fill, struct fill,
        #  struct f, deliv f, deliv fill, locked, approx,
        #  other f, other fill)
        (0, "freight_in", 0, 16.0, HUB_RATE, 0, False, False, HUB, False,
         None, None, None, None, None, None, False, False, None, None),
        (0, "freight_in", 0, 4.0, STRUCT_RATE, 0, False, False, STRUCT,
         False, None, None, None, None, None, None, False, False, None,
         None),
        # Depth 0 reads this very run; the depth-1 inputs are clamped
        # (spin-up: one run of history, nothing deeper to walk to).
        (HULK, "install", 0, 1.0, 100.0, 0, False, False, None, False,
         None, None, None, None, None, None, False, False, None, None),
        (TRIT, "material", 1, 1000.0, 5.0, 0, True, False, HUB, False,
         None, None, None, None, None, None, False, False, None, None),
        (PYERITE, "material", 1, 1000.0, 4.8, 0, True, False, SPLIT, False,
         0.6, 4.0, 6.0, None, None, None, False, False, None, None),
        (MEXALLON, "material", 1, 1000.0, EFFECTIVE, 0, True, False, None,
         True, None, None, None, None, None, None, False, False, None,
         None),
    ]
    # The compressed ore row has no pipeline attribution, so it never
    # becomes a line of its own (A15).
    assert COMPRESSED_VELDSPAR not in {line.type_id for line in cost.lines}


def test_lines_on_a_later_run_never_reach_an_earlier_snapshot(
    conn, ref, settings
):
    pid, first = build_run(conn, run_number=1)
    build_run(conn, run_number=2, pipeline_id=pid)
    second = conn.execute(
        "SELECT index_run_id FROM index_run WHERE run_number = 2"
    ).fetchone()[0]
    before = fingerprint(costing.hull_cost(conn, ref, settings, first, pid))
    lock(conn, second, TRIT, HUB, 1000, 0.5)
    assert fingerprint(
        costing.hull_cost(conn, ref, settings, first, pid)
    ) == before
    rates = costing._run_freight_rates(conn, first)
    assert costing._run_snapshot(
        conn, first, ref=ref, rates=rates, settings=settings
    ) == costing._run_snapshot(conn, first)


# --- direct buys (contract section 4.2) ------------------------------------


def test_one_hub_line_at_plan_quantity_replaces_the_plans_price(
    conn, ref, settings
):
    pid, run_id = build_run(conn)
    before = freight(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, TRIT, HUB, 1000, 3.0, source="buy")
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.unit_cost == pytest.approx(3.0)
    assert line.venue == HUB
    assert line.locked and not line.approximate
    # A13: all three shares stated outright, never left to inference.
    assert (line.hub_fraction, line.structure_fraction,
            line.delivered_fraction) == (1.0, 0.0, 0.0)
    assert line.structure_share() == 0.0
    # Same units, same venue: freight does not move.
    assert freight(cost) == before
    # Nothing else on the run moved.
    assert by_type(cost)[PYERITE].unit_cost == pytest.approx(4.8)
    assert by_type(cost)[MEXALLON].unit_cost == pytest.approx(EFFECTIVE)


def test_split_lines_carry_per_venue_fractions_fill_prices_and_freight(
    conn, ref, settings
):
    pid, run_id = build_run(conn)
    lock(conn, run_id, TRIT, HUB, 600, 3.0)
    lock(conn, run_id, TRIT, STRUCT, 400, 7.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.venue == SPLIT
    assert line.unit_cost == pytest.approx((600 * 3.0 + 400 * 7.0) / 1000)
    assert line.hub_fraction == pytest.approx(0.6)
    assert line.structure_fraction == pytest.approx(0.4)
    assert line.delivered_fraction == pytest.approx(0.0)
    assert (line.hub_fill_price, line.structure_fill_price) == (3.0, 7.0)
    # The structure's units at the structure's own price, not the blend.
    assert line.structure_cost_per_hull() == pytest.approx(400 * 7.0)
    # Freight follows the units, at the RUN's persisted rates: Tritanium
    # now hauls 6 m3 through Jita and 4 through C-J6, on top of Pyerite's
    # unchanged 6 / 4.
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx(12 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(8 * STRUCT_RATE),
    }


def test_a_delivered_line_hauls_nothing_and_is_not_a_compressed_line(
    conn, ref, settings
):
    """A delivered price is already landed, so no freight rides it — and
    it must NOT carry `landed=True`, which is the compressed marker the
    profit cards badge (contract review A12)."""
    pid, run_id = build_run(conn)
    lock(conn, run_id, TRIT, DELIVERED, 1000, 20.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.venue == DELIVERED
    assert line.unit_cost == pytest.approx(20.0)
    assert line.landed is False
    assert (line.hub_fraction, line.structure_fraction,
            line.delivered_fraction) == (0.0, 0.0, 1.0)
    assert line.delivered_fill_price == pytest.approx(20.0)
    assert line.structure_share() == 0.0
    # Only Pyerite's 6 / 4 m3 remain.
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx(6 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(4 * STRUCT_RATE),
    }


def test_a_delivered_share_mixed_with_a_hub_share(conn, ref, settings):
    pid, run_id = build_run(conn)
    lock(conn, run_id, TRIT, DELIVERED, 500, 20.0)
    lock(conn, run_id, TRIT, HUB, 500, 4.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.venue == SPLIT
    assert line.unit_cost == pytest.approx((500 * 20.0 + 500 * 4.0) / 1000)
    assert line.hub_fraction == pytest.approx(0.5)
    assert line.structure_fraction == pytest.approx(0.0)
    assert line.delivered_fraction == pytest.approx(0.5)
    assert line.structure_share() == 0.0
    # Half the Tritanium hauls (5 m3), the delivered half does not.
    assert freight(cost)["Inbound freight (Jita)"] == pytest.approx(
        11 * HUB_RATE
    )
    # Null Sec Market Share counts Pyerite's structure units only — the
    # delivered share is NOT "everything that is not Jita".
    assert cost.structure_material_cost == pytest.approx(400 * 6.0)


def test_partial_lock_prices_the_remainder_at_the_plans_split(
    conn, ref, settings
):
    """400 of Pyerite's 1000 bought for real; the other 600 keep the
    plan's 60/40 venue split and the plan's own per-venue prices."""
    pid, run_id = build_run(conn)
    lock(conn, run_id, PYERITE, HUB, 400, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[PYERITE]
    hub_isk = 400 * 3.0 + 360 * 4.0
    structure_isk = 240 * 6.0
    assert line.unit_cost == pytest.approx((hub_isk + structure_isk) / 1000)
    assert line.hub_fraction == pytest.approx(0.76)
    assert line.structure_fraction == pytest.approx(0.24)
    assert line.hub_fill_price == pytest.approx(hub_isk / 760)
    assert line.structure_fill_price == pytest.approx(6.0)
    assert not line.missing_price
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx((10 + 7.6) * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(2.4 * STRUCT_RATE),
    }


def test_partial_lock_of_an_unsourced_row_prices_the_remainder_at_the_blend(
    conn, ref, settings
):
    """With unsourced units the per-venue fill prices do NOT average to
    price_snapshot (the unsourced units sit at the marginal rung), so the
    remainder's ISK is remainder x price_snapshot and only its quantities
    split by venue (contract review A11)."""
    pid, run_id = build_run(conn, unfilled_pyerite=200)
    lock(conn, run_id, PYERITE, HUB, 200, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[PYERITE]
    # plan qty 1200, locked 200, remainder 1000 split 60/40 by quantity,
    # every remainder unit at the 4.8 blended snapshot.
    assert line.unit_cost == pytest.approx(
        (200 * 3.0 + 1000 * 4.8) / 1200
    )
    assert line.hub_fraction == pytest.approx((200 + 600) / 1200)
    assert line.structure_fraction == pytest.approx(400 / 1200)


def test_over_locking_prices_everything_at_what_was_paid(
    conn, ref, settings
):
    pid, run_id = build_run(conn)
    lock(conn, run_id, TRIT, HUB, 1500, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.unit_cost == pytest.approx(3.0)
    assert line.hub_fraction == pytest.approx(1.0)
    row = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, TRIT),
    ).fetchone()
    blend = costing.blend_purchases(
        store.list_purchases(conn, run_id)[TRIT],
        costing.plan_buy_from_row(row),
    )
    assert blend.status == "over"
    assert (blend.locked_qty, blend.remainder_qty) == (1500, 0)


def test_an_unpriced_plan_row_partly_locked_counts_the_rest_at_zero(
    conn, ref, settings
):
    pid = add_pipeline(conn)
    run_id = add_run(conn, 1)
    add_item(
        conn, run_id, HULK, pid, attributable=1, depth=0,
        blueprint_id=987654, portion_size=1, runs_allocated=1,
        install_runs=1, merged_min_qty=1, cycle_need_qty=1,
        unit_install_fee=100.0,
    )
    add_item(
        conn, run_id, TRIT, pid, attributable=1000, depth=1,
        recommended_buy_qty=1000, recommended_action="buy", buy_venue=HUB,
    )
    conn.commit()
    unlocked = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert unlocked[TRIT].unit_cost == 0.0
    assert unlocked[TRIT].missing_price

    lock(conn, run_id, TRIT, HUB, 400, 3.0)
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[TRIT]
    assert line.unit_cost == pytest.approx(400 * 3.0 / 1000)
    assert line.missing_price
    assert line.locked


def test_a_purchase_of_a_built_item_is_recorded_but_moves_no_hull(
    conn, ref, settings
):
    """Revision 3 §5's documented known limit (contract review C13.5).
    Revision 2 skipped every row with no plan buy, and this test used to
    say "the Buy tab offers no other row". That skip is gone — purchases
    come from ESI now, and a re-planned row can carry them (see the
    zero-buy raw test below) — so the Hulk's own snapshot IS repriced
    from the line. The plan BUILDS the Hulk, though, and hull_cost's
    install branch wins over the material branch: realized costing still
    expands the build, and the million-ISK purchase moves no hull."""
    pid, run_id = build_run(conn)
    before = fingerprint(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, HULK, HUB, 1, 1_000_000.0)
    assert fingerprint(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    ) == before
    snapshot = costing._run_snapshot(
        conn, run_id, ref=ref,
        rates=costing._run_freight_rates(conn, run_id), settings=settings,
    )
    # Recorded and repriced over its basis (cycle need 1) ...
    assert snapshot[HULK].locked
    assert snapshot[HULK].price == pytest.approx(1_000_000.0)
    # ... but the install fee, which is all the hull reads, is the plan's.
    assert snapshot[HULK].fee == 100.0


# --- compressed re-blend (contract section 4.3) ----------------------------


def test_locking_the_ore_reprices_only_the_raws_it_covers(
    conn, ref, settings
):
    """100 Compressed Veldspar bought at 3.0 instead of the plan's 5.0:
    the ore's landed cost falls to 100 x (3.0 + freight) + the plan's
    refining tax, and Mexallon's blended effective cost falls with it."""
    pid, run_id = build_run(conn)
    before = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[MEXALLON]
    realized_ore = 100 * (3.0 + HUB_RATE * ORE_M3) + ORE_TAX
    assert line.unit_cost == pytest.approx(
        (DIRECT_LANDED + realized_ore) / 1000
    )
    assert line.landed and line.locked and not line.approximate
    assert line.venue is None
    # Everything else is untouched, freight included (the re-blended line
    # is landed, so it hauls nothing of its own).
    for type_id in (TRIT, PYERITE):
        assert by_type(cost)[type_id].unit_cost == pytest.approx(
            before[type_id].unit_cost
        )
        assert not by_type(cost)[type_id].locked
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx(16 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(4 * STRUCT_RATE),
    }


def test_a_partial_ore_lock_keeps_the_plans_price_for_the_rest(
    conn, ref, settings
):
    """60 of the 100 ore bought at 3.0; the other 40 stay at the plan's
    EX-TAX landed cost pro rata, and the refining tax stays whole — it
    is a function of the outputs, not of what the ore cost."""
    pid, run_id = build_run(conn)
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 60, 3.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    realized_ore = (
        60 * (3.0 + HUB_RATE * ORE_M3)
        + (ORE_LANDED - ORE_TAX) * 40 / 100
        + ORE_TAX
    )
    assert line.unit_cost == pytest.approx(
        (DIRECT_LANDED + realized_ore) / 1000
    )


def test_locking_a_covered_raw_moves_only_its_direct_share(
    conn, ref, settings
):
    """Mexallon's 200 direct units bought at 2.0: the direct term becomes
    the blended LANDED unit cost x the plan quantity, the ore's
    contribution stays the plan's."""
    pid, run_id = build_run(conn)
    lock(conn, run_id, MEXALLON, HUB, 200, 2.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    direct = 200 * (2.0 + HUB_RATE * MINERAL_M3)
    assert line.unit_cost == pytest.approx((direct + ORE_LANDED) / 1000)
    assert line.locked and not line.approximate

    # Both sides locked: both terms move, nothing double-counts.
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    realized_ore = 100 * (3.0 + HUB_RATE * ORE_M3) + ORE_TAX
    assert line.unit_cost == pytest.approx(
        (direct + realized_ore) / 1000
    )


def test_a_delivered_ore_line_adds_no_freight_to_the_reblend(
    conn, ref, settings
):
    pid, run_id = build_run(conn)
    lock(conn, run_id, COMPRESSED_VELDSPAR, DELIVERED, 100, 6.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert line.unit_cost == pytest.approx(
        (DIRECT_LANDED + 100 * 6.0 + ORE_TAX) / 1000
    )


def test_a_prev129_run_reblends_approximately_and_says_so(
    conn, ref, settings
):
    """A run planned before v1.29 has none of the four new columns. The
    shares are then inferred and the ore's plan cost knows no refining
    tax, so the line is badged `approximate`. The tax cancels out of the
    DELTA a lock makes, which is why the number still lands on the exact
    one here — the divergence shows up in the share weighting (see the
    two-raw test below)."""
    pid, run_id = build_run(conn)
    conn.execute(
        "UPDATE index_run_item SET compressed_alloc = NULL, "
        "compressed_landed_isk = NULL, compressed_tax_isk = NULL "
        "WHERE index_run_id = ? AND type_id = ?",
        (run_id, COMPRESSED_VELDSPAR),
    )
    conn.execute(
        "UPDATE index_run_item SET direct_landed_isk = NULL "
        "WHERE index_run_id = ? AND type_id = ?",
        (run_id, MEXALLON),
    )
    conn.commit()
    # Untouched, the pre-v1.29 plan value still stands verbatim: the
    # fallback must not run when nothing is locked (A10).
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert line.unit_cost == pytest.approx(EFFECTIVE)
    assert not line.locked and not line.approximate

    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert line.locked and line.approximate
    realized_ore = 100 * (3.0 + HUB_RATE * ORE_M3)
    plan_ore = 100 * (5.0 + HUB_RATE * ORE_M3)
    assert line.unit_cost == pytest.approx(
        EFFECTIVE + (realized_ore - plan_ore) / 1000
    )


def _prev129(conn, run_id):
    """Strip a fixture run of the four v1.29 columns — what every run
    planned before this release looks like."""
    conn.execute(
        "UPDATE index_run_item SET compressed_alloc = NULL, "
        "compressed_landed_isk = NULL, compressed_tax_isk = NULL "
        "WHERE index_run_id = ? AND type_id = ?",
        (run_id, COMPRESSED_VELDSPAR),
    )
    conn.execute(
        "UPDATE index_run_item SET direct_landed_isk = NULL "
        "WHERE index_run_id = ? AND type_id = ?",
        (run_id, MEXALLON),
    )
    conn.commit()


def test_a_prev129_covered_raw_locked_to_its_own_plan_fill_is_a_no_op(
    conn, ref, settings
):
    """Regression (review 2026-09-24). The Buy tab's `ladder` option books
    exactly what the plan said — 200 units, hub, 5.0 — and R6 promises
    that costs the plan's effective cost, full stop. Backing the direct
    remainder out of the persisted identity buried the ore's refining tax
    in that term, so the no-op silently dropped 170 ISK (0.17/unit, 4.5%
    of the row). The tax belongs in `residual`, which no lock touches."""
    pid, run_id = build_run(conn)
    _prev129(conn, run_id)
    lock(conn, run_id, MEXALLON, HUB, 200, 5.0)
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert line.unit_cost == pytest.approx(EFFECTIVE)
    assert line.locked and line.approximate


def test_a_prev129_covered_raw_moves_by_exactly_what_the_lock_changed(
    conn, ref, settings
):
    """The same lock 1.0 ISK/unit under the plan moves the row by 200
    units x 1.0, and by nothing else — not by the tax as well."""
    pid, run_id = build_run(conn)
    _prev129(conn, run_id)
    lock(conn, run_id, MEXALLON, HUB, 200, 4.0)
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert line.unit_cost == pytest.approx(EFFECTIVE - 200 * 1.0 / 1000)


def test_a_fully_covered_prev129_raw_keeps_the_slack_it_cannot_explain(
    conn, ref, settings
):
    """No direct remainder at all, so the whole of the identity's slack is
    the ore's refining tax. Buying the raw direct drives f to 1 (the ore
    leaves the route entirely, Z_c = 0), and the row then reads what its
    lines paid PLUS that carried-through slack — the residual doctrine
    applied evenly. It used to read the lines alone, the tax having been
    thrown away with the reconstructed direct term."""
    pid = add_pipeline(conn)
    run_id = add_run(conn, 1)
    add_item(
        conn, run_id, HULK, pid, attributable=1, depth=0,
        blueprint_id=987654, portion_size=1, runs_allocated=1,
        install_runs=1, merged_min_qty=1, cycle_need_qty=1,
        unit_install_fee=100.0,
    )
    add_item(
        conn, run_id, MEXALLON, pid, attributable=1000, depth=1,
        recommended_buy_qty=0, recommended_action="buy",
        price_snapshot=5.0, buy_venue=HUB,
        compressed_covered_qty=1000, effective_unit_cost=ORE_LANDED / 1000,
    )
    add_item(
        conn, run_id, COMPRESSED_VELDSPAR,
        recommended_buy_qty=100, recommended_action="buy",
        price_snapshot=5.0, buy_venue=HUB,
        compressed_outputs=json.dumps([[MEXALLON, 1000, 1000]]),
    )
    conn.commit()
    # Untouched: the plan's own figure, to the last bit.
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert line.unit_cost == pytest.approx(ORE_LANDED / 1000)

    lock(conn, run_id, MEXALLON, HUB, 1000, 0.6)
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    paid = 1000 * (0.6 + HUB_RATE * MINERAL_M3)      # 10,600
    inferred_ore = 100 * (5.0 + HUB_RATE * ORE_M3)   # 600, tax-free
    residual = ORE_LANDED - inferred_ore             # 170 = the tax
    assert line.unit_cost == pytest.approx((paid + residual) / 1000)


def test_inferred_shares_differ_from_the_engines_and_are_flagged(
    conn, ref, settings
):
    """One ore covering two raws: the engine weights the split by the
    landed value the coverage DISPLACES, which the pre-v1.29 fallback
    can only approximate by units x price. The two disagree, so the
    fallback is badged."""
    pid = add_pipeline(conn)
    run_id = add_run(conn, 1)
    add_item(
        conn, run_id, HULK, pid, attributable=1, depth=0,
        blueprint_id=987654, portion_size=1, runs_allocated=1,
        install_runs=1, merged_min_qty=1, cycle_need_qty=1,
        unit_install_fee=100.0,
    )
    # Both raws fully covered by one ore; 0.7 / 0.3 by displaced value,
    # 2/3 / 1/3 by units x price.
    for type_id, covered, effective in (
        (MEXALLON, 800, 0.7 * ORE_LANDED / 800),
        (TRIT, 400, 0.3 * ORE_LANDED / 400),
    ):
        add_item(
            conn, run_id, type_id, pid, attributable=covered, depth=1,
            recommended_buy_qty=0, price_snapshot=5.0, buy_venue=HUB,
            compressed_covered_qty=covered, effective_unit_cost=effective,
            direct_landed_isk=0.0,
        )
    add_item(
        conn, run_id, COMPRESSED_VELDSPAR,
        recommended_buy_qty=100, recommended_action="buy",
        price_snapshot=5.0, buy_venue=HUB,
        compressed_outputs=json.dumps(
            [[MEXALLON, 850, 800], [TRIT, 500, 400]]
        ),
        compressed_alloc=json.dumps({str(MEXALLON): 0.7, str(TRIT): 0.3}),
        compressed_landed_isk=ORE_LANDED,
        compressed_tax_isk=ORE_TAX,
    )
    conn.commit()
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    realized_ore = 100 * (3.0 + HUB_RATE * ORE_M3) + ORE_TAX

    exact = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert exact[MEXALLON].unit_cost == pytest.approx(
        0.7 * realized_ore / 800
    )
    assert exact[TRIT].unit_cost == pytest.approx(0.3 * realized_ore / 400)
    assert not exact[MEXALLON].approximate

    conn.execute(
        "UPDATE index_run_item SET compressed_alloc = NULL "
        "WHERE index_run_id = ? AND type_id = ?",
        (run_id, COMPRESSED_VELDSPAR),
    )
    conn.commit()
    inferred = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert inferred[MEXALLON].approximate and inferred[TRIT].approximate
    # Units x price weights the split 800 x 5.0 : 400 x 5.0 — two thirds
    # to Mexallon where the engine gave it seven tenths. The DELTA the
    # lock makes is split by the inferred shares; the plan's own cost,
    # which the identity explains, is carried through untouched.
    for type_id, covered, share in ((MEXALLON, 800, 2 / 3), (TRIT, 400, 1 / 3)):
        plan_unit = 0.7 if type_id == MEXALLON else 0.3
        assert inferred[type_id].unit_cost == pytest.approx(
            (plan_unit * ORE_LANDED + share * (realized_ore - ORE_LANDED))
            / covered
        )
    assert inferred[MEXALLON].unit_cost != pytest.approx(
        exact[MEXALLON].unit_cost
    )


def test_lines_on_a_fully_covered_raw_price_the_units_they_displace(
    conn, ref, settings
):
    """R7 (user ruling 2026-09-24) overturns A30: a raw the compressed
    pass covered in full is a normal Buy tab row at its full demand, so
    lines on it are priced — there is no direct remainder for them to
    displace, so they displace COVERED units straight away.

    500 of the 1000 covered units bought at 99.0 → f = 0.5. The ore is
    still bought whole (the plan still needs the other 500 covered
    units, and no sibling raw is left to carry it), so its 770 ISK stays
    on the row on top of the 500 units at 109.0 landed. Buying twice
    reads as paying twice; only f = 1 across every covered raw drops the
    ore, which is the pair of tests further down."""
    pid, run_id = build_run(conn)
    conn.execute(
        "UPDATE index_run_item SET recommended_buy_qty = 0, "
        "compressed_covered_qty = 1000, direct_landed_isk = 0.0, "
        "effective_unit_cost = ? WHERE index_run_id = ? AND type_id = ?",
        (ORE_LANDED / 1000, run_id, MEXALLON),
    )
    conn.commit()
    lock(conn, run_id, MEXALLON, HUB, 500, 99.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    displaced = 500 * (99.0 + HUB_RATE * MINERAL_M3)
    assert line.unit_cost == pytest.approx((displaced + ORE_LANDED) / 1000)
    assert line.locked


def test_locking_exactly_what_the_plan_said_changes_no_number(
    conn, ref, settings
):
    """The override's sanity check: purchase lines that reproduce the
    plan (same quantities, same venues, same prices) must reproduce the
    plan's cost to the last decimal — on a plain buy, a fill-priced
    split, a compressed ore and a compressed-covered raw alike. Only the
    `locked` badge and the now-explicit venue shares differ."""
    pid, run_id = build_run(conn)
    def costs(cost):
        # Keyed on kind + venue too: both freight lines are type_id 0.
        return {
            (line.type_id, line.kind, line.venue): line.cost_per_hull
            for line in cost.lines
        }

    before = costs(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, TRIT, HUB, 1000, 5.0)
    lock(conn, run_id, PYERITE, HUB, 600, 4.0)
    lock(conn, run_id, PYERITE, STRUCT, 400, 6.0)
    lock(conn, run_id, MEXALLON, HUB, 200, 5.0)
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 5.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    after = costs(cost)
    assert set(after) == set(before)
    for key, value in after.items():
        assert value == pytest.approx(before[key]), key
    assert all(line.locked for line in by_type(cost).values())
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx(16 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(4 * STRUCT_RATE),
    }


def test_the_reblend_carries_through_what_the_identity_cannot_explain(
    conn, ref, settings
):
    """A persisted row whose pieces do not add up to its effective cost
    (an older vintage, a column the engine never wrote) must still move
    by exactly what the lock changed — never snap to the reconstruction."""
    pid, run_id = build_run(conn)
    # Knock the identity out: the row now claims 1 ISK/unit more than its
    # direct + compressed terms account for.
    conn.execute(
        "UPDATE index_run_item SET effective_unit_cost = ? "
        "WHERE index_run_id = ? AND type_id = ?",
        (EFFECTIVE + 1.0, run_id, MEXALLON),
    )
    conn.commit()
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    realized_ore = 100 * (3.0 + HUB_RATE * ORE_M3) + ORE_TAX
    assert line.unit_cost == pytest.approx(
        EFFECTIVE + 1.0 + (realized_ore - ORE_LANDED) / 1000
    )


# --- R7: locked units displace the covered ones ----------------------------
#
# The fixture's Mexallon: 200 direct + 800 covered = 1000 demand, the
# plan's 3.77/unit landed. Every lock below is at 2.0 in Jita, i.e.
# 12.0 landed per unit (2.0 + 1000 ISK/m3 x 0.01 m3), so the hand-worked
# numbers read straight off the ruling:
#
#   f = clamp((locked - 200) / 800, 0, 1)   — the covered share bought direct
#   cost = 12.0 x min(locked, 200)          — the direct part
#        + 12.0 x f x 800                   — the spill part
#        + (1 - f > 0 ? 770 : 0)            — the ore, still bought whole
#        + the plan's price for the direct units nobody locked


@pytest.mark.parametrize(
    "locked, expected, why",
    [
        (100, (100 * 12.0 + 100 * (5.0 + HUB_RATE * MINERAL_M3) + ORE_LANDED)
         / 1000, "below direct: the unlocked direct units stay at the plan"),
        (200, (200 * 12.0 + ORE_LANDED) / 1000,
         "exactly direct: nothing displaced, f = 0"),
        (600, (600 * 12.0 + ORE_LANDED) / 1000,
         "half the covered units displaced, the ore still bought whole"),
        (1000, 12.0, "the whole demand bought direct: f = 1, no ore"),
        (1200, 12.0, "over-locked: the surplus is next cycle's stock"),
    ],
)
def test_locked_units_displace_the_direct_remainder_then_the_covered_ones(
    conn, ref, settings, locked, expected, why
):
    pid, run_id = build_run(conn)
    lock(conn, run_id, MEXALLON, HUB, locked, 2.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert line.unit_cost == pytest.approx(expected), why
    assert line.locked and not line.approximate


def test_the_ore_leaves_the_buy_list_only_when_every_covered_unit_does(
    conn, ref, settings
):
    """The discontinuity is a ruling, not arithmetic: one covered unit
    short of the whole demand still buys the ore, and pays for it."""
    pid, run_id = build_run(conn)
    lock(conn, run_id, MEXALLON, HUB, 999, 2.0)
    nearly = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert nearly.unit_cost == pytest.approx(
        (999 * 12.0 + ORE_LANDED) / 1000
    )
    conn.execute("DELETE FROM run_purchase")
    lock(conn, run_id, MEXALLON, HUB, 1000, 2.0)
    whole = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert whole.unit_cost == pytest.approx(12.0)
    # Buying that last covered unit direct costs 12 ISK and drops the
    # whole 770 ISK ore from the list: the step is the ruling, not a
    # rounding artefact.
    assert (nearly.unit_cost - whole.unit_cost) * 1000 == pytest.approx(
        ORE_LANDED - 12.0
    )


def build_shared_ore_run(conn):
    """One ore covering TWO fully covered raws — Mexallon takes 0.7 of
    its landed cost over 800 units, Tritanium 0.3 over 400."""
    pid = add_pipeline(conn)
    run_id = add_run(conn, 1)
    add_item(
        conn, run_id, HULK, pid, attributable=1, depth=0,
        blueprint_id=987654, portion_size=1, runs_allocated=1,
        install_runs=1, merged_min_qty=1, cycle_need_qty=1,
        unit_install_fee=100.0,
    )
    for type_id, covered, share in ((MEXALLON, 800, 0.7), (TRIT, 400, 0.3)):
        add_item(
            conn, run_id, type_id, pid, attributable=covered, depth=1,
            recommended_buy_qty=0, price_snapshot=5.0, buy_venue=HUB,
            compressed_covered_qty=covered,
            effective_unit_cost=share * ORE_LANDED / covered,
            direct_landed_isk=0.0,
        )
    add_item(
        conn, run_id, COMPRESSED_VELDSPAR,
        recommended_buy_qty=100, recommended_action="buy",
        price_snapshot=5.0, buy_venue=HUB,
        compressed_outputs=json.dumps(
            [[MEXALLON, 850, 800], [TRIT, 500, 400]]
        ),
        compressed_alloc=json.dumps({str(MEXALLON): 0.7, str(TRIT): 0.3}),
        compressed_landed_isk=ORE_LANDED,
        compressed_tax_isk=ORE_TAX,
    )
    conn.commit()
    return pid, run_id


def test_a_raw_that_stays_on_the_route_absorbs_the_share_the_other_gave_up(
    conn, ref, settings
):
    """Mexallon's 800 covered units all bought direct: it leaves the
    ore's route (f = 1, share 0), and the ore — still bought whole for
    Tritanium — lands its WHOLE 770 ISK on Tritanium, which nobody
    locked. The total is deliberately not conserved: that is what "the
    ore is still bought" costs."""
    pid, run_id = build_shared_ore_run(conn)
    before = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert before[TRIT].unit_cost == pytest.approx(0.3 * ORE_LANDED / 400)

    lock(conn, run_id, MEXALLON, HUB, 800, 1.0)
    after = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert after[MEXALLON].unit_cost == pytest.approx(
        1.0 + HUB_RATE * MINERAL_M3
    )
    assert after[TRIT].unit_cost == pytest.approx(ORE_LANDED / 400)
    # Tritanium moved without a line of its own, so it is badged locked.
    assert after[TRIT].locked and not after[TRIT].approximate
    total_before = sum(
        before[t].unit_cost * covered
        for t, covered in ((MEXALLON, 800), (TRIT, 400))
    )
    total_after = sum(
        after[t].unit_cost * covered
        for t, covered in ((MEXALLON, 800), (TRIT, 400))
    )
    assert total_before == pytest.approx(ORE_LANDED)
    assert total_after == pytest.approx(800 * 11.0 + ORE_LANDED)


def test_when_every_covered_raw_leaves_the_ore_costs_nobody_anything(
    conn, ref, settings
):
    """Z_c = 0: nothing is left to buy the ore for, so it is not bought
    and its 770 ISK lands nowhere."""
    pid, run_id = build_shared_ore_run(conn)
    lock(conn, run_id, MEXALLON, HUB, 800, 1.0)
    lock(conn, run_id, TRIT, HUB, 400, 2.0)
    after = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert after[MEXALLON].unit_cost == pytest.approx(11.0)
    assert after[TRIT].unit_cost == pytest.approx(12.0)


def test_a_partly_displaced_sibling_keeps_its_re_normalised_share(
    conn, ref, settings
):
    """Half of Mexallon's covered units bought direct, Tritanium
    untouched: the ore's shares scale by (1 - f) and re-normalise back
    to the total they carried, so 0.7 x 0.5 : 0.3 becomes 7/13 : 6/13 of
    the same 770 ISK — the ore is bought whole either way."""
    pid, run_id = build_shared_ore_run(conn)
    lock(conn, run_id, MEXALLON, HUB, 400, 1.0)
    after = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    z = 0.7 * 0.5 + 0.3
    assert after[MEXALLON].unit_cost == pytest.approx(
        (400 * 11.0 + (0.35 / z) * ORE_LANDED) / 800
    )
    assert after[TRIT].unit_cost == pytest.approx((0.3 / z) * ORE_LANDED / 400)
    assert (0.35 / z) + (0.3 / z) == pytest.approx(1.0)


def test_an_ore_whose_shares_are_all_zero_survives_a_raw_leaving(
    conn, ref, settings
):
    """A5's degenerate ore (the coverage displaced no value, so every
    persisted share is 0.0) must not be re-normalised into something
    else when a raw leaves — Σ share is 0 for a reason that has nothing
    to do with R7 (B28). The ore's unattributed ISK stays where the
    residual put it: on the plan's own effective cost."""
    pid, run_id = build_run(conn)
    conn.execute(
        "UPDATE index_run_item SET compressed_alloc = ? "
        "WHERE index_run_id = ? AND type_id = ?",
        (json.dumps({str(MEXALLON): 0.0}), run_id, COMPRESSED_VELDSPAR),
    )
    conn.commit()
    lock(conn, run_id, MEXALLON, HUB, 1000, 2.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    # 1000 units at 12.0 landed, plus the 770 ISK the persisted identity
    # explains with no share to hang it on.
    assert line.unit_cost == pytest.approx((1000 * 12.0 + ORE_LANDED) / 1000)


def test_a_covered_raw_with_nothing_covered_never_divides_by_zero(
    conn, ref, settings
):
    """B29: the engine writes compressed_covered_qty and
    effective_unit_cost together, so f_r's division is only at risk on
    an older or hand-edited row. Nothing covered, nothing displaced."""
    pid, run_id = build_run(conn)
    conn.execute(
        "UPDATE index_run_item SET compressed_covered_qty = 0, "
        "effective_unit_cost = ? WHERE index_run_id = ? AND type_id = ?",
        (DIRECT_LANDED / 200, run_id, MEXALLON),
    )
    conn.execute(
        "UPDATE index_run_item SET compressed_outputs = ? "
        "WHERE index_run_id = ? AND type_id = ?",
        (json.dumps([[MEXALLON, 850, 0]]), run_id, COMPRESSED_VELDSPAR),
    )
    conn.commit()
    lock(conn, run_id, MEXALLON, HUB, 500, 2.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    # 200 units of demand, priced per unit at what the 500 cost landed.
    assert line.unit_cost == pytest.approx(12.0)


# --- the lag walk ----------------------------------------------------------


def test_the_lag_walk_takes_the_override_from_the_run_it_lags_to(
    conn, ref, settings
):
    pid, first = build_run(conn, run_number=1)
    build_run(conn, run_number=2, pipeline_id=pid)
    second = conn.execute(
        "SELECT index_run_id FROM index_run WHERE run_number = 2"
    ).fetchone()[0]
    lock(conn, first, TRIT, HUB, 1000, 3.0)
    lock(conn, second, TRIT, HUB, 1000, 99.0)

    # Tritanium sits at pipeline depth 1, so a hull delivered at run 2
    # was built from what run 1 bought.
    line = by_type(costing.hull_cost(conn, ref, settings, second, pid))[TRIT]
    assert line.unit_cost == pytest.approx(3.0)
    assert (line.lag_runs, line.locked) == (1, True)
    # Run 2's own snapshot still carries its own lines.
    rates = costing._run_freight_rates(conn, second)
    snapshot = costing._run_snapshot(
        conn, second, ref=ref, rates=rates, settings=settings
    )
    assert snapshot[TRIT].price == pytest.approx(99.0)


def test_each_runs_purchases_haul_at_that_runs_freight_rates(
    conn, ref, settings
):
    """The re-blend is a LANDED figure, so the ore's freight must come
    from the vintage the lock belongs to, not from today's setting."""
    pid, run_id = build_run(conn)
    conn.execute(
        "UPDATE index_run SET freight_in_isk_per_m3 = 4000 "
        "WHERE index_run_id = ?",
        (run_id,),
    )
    conn.commit()
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert line.unit_cost == pytest.approx(
        (DIRECT_LANDED + 100 * (3.0 + 4000 * ORE_M3) + ORE_TAX) / 1000
    )


def test_a_run_with_no_persisted_rate_falls_back_to_the_live_setting(
    conn, ref, settings
):
    pid, run_id = build_run(conn)
    conn.execute(
        "UPDATE index_run SET freight_in_isk_per_m3 = NULL "
        "WHERE index_run_id = ?",
        (run_id,),
    )
    conn.commit()
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    live = settings.freight_in_rate(HUB)
    assert line.unit_cost == pytest.approx(
        (DIRECT_LANDED + 100 * (3.0 + live * ORE_M3) + ORE_TAX) / 1000
    )


# --- the shared seam the Buy tab reuses ------------------------------------


def test_blend_purchases_is_the_buy_tabs_locked_landed_total(conn, ref):
    """The Buy tab's "locked landed total" is this same call: actual
    lines landed per venue (delivered hauls nothing) plus the remainder
    at the plan's landed price."""
    _pid, run_id = build_run(conn)
    lock(conn, run_id, PYERITE, HUB, 400, 3.0)
    lock(conn, run_id, PYERITE, DELIVERED, 100, 9.0)
    row = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, PYERITE),
    ).fetchone()
    blend = costing.blend_purchases(
        store.list_purchases(conn, run_id)[PYERITE],
        costing.plan_buy_from_row(row),
        hub_rate=HUB_RATE, structure_rate=STRUCT_RATE, m3=MINERAL_M3,
    )
    assert blend.status == "partial"
    assert (blend.plan_qty, blend.locked_qty, blend.remainder_qty) == (
        1000, 500, 500
    )
    hub_units, structure_units = 400 + 300, 200
    order = 400 * 3.0 + 100 * 9.0 + 300 * 4.0 + 200 * 6.0
    landed = order + (
        hub_units * HUB_RATE + structure_units * STRUCT_RATE
    ) * MINERAL_M3
    assert blend.landed_total == pytest.approx(landed)
    assert blend.landed_unit == pytest.approx(landed / 1000)
    assert blend.unit_cost == pytest.approx(order / 1000)
    assert blend.venue == SPLIT
    assert blend.delivered_fill_price == pytest.approx(9.0)


def test_blend_purchases_edges():
    """Nothing to price, and a legacy single-venue plan row."""
    empty = costing.blend_purchases((), costing.PlanBuy(qty=0, price=None))
    assert empty.quantity == 0.0 and empty.venue is None
    assert empty.status == "none"
    legacy = costing.blend_purchases(
        (), costing.PlanBuy(qty=100, price=7.0, venue=STRUCT),
        structure_rate=STRUCT_RATE, m3=MINERAL_M3,
    )
    assert legacy.structure_fraction == 1.0 and legacy.hub_fraction == 0.0
    assert legacy.landed_total == pytest.approx(
        100 * 7.0 + 100 * STRUCT_RATE * MINERAL_M3
    )
    # A pre-v1.10 row carries no venue at all: those were hub buys.
    pre_v110 = costing.blend_purchases(
        (), costing.PlanBuy(qty=100, price=7.0),
        hub_rate=HUB_RATE, m3=MINERAL_M3,
    )
    assert pre_v110.venue == HUB


def test_plan_buy_from_row_reads_an_index_run_item(conn, ref):
    _pid, run_id = build_run(conn)
    row = conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, PYERITE),
    ).fetchone()
    plan = costing.plan_buy_from_row(row)
    assert plan == costing.PlanBuy(
        qty=1000, price=4.8, venue=SPLIT, hub_qty=600, structure_qty=400,
        hub_price=4.0, structure_price=6.0, unfilled_qty=0,
    )


def test_over_locking_an_ore_charges_this_cycle_for_the_plans_units_only(
    conn, ref, settings
):
    """Review 2026-09-23: an ore bought in a bigger lot than the plan
    asked for is priced PER UNIT, exactly as an over-locked direct buy
    is. Locking 150 of the 100 the plan buys at the plan's own price
    must not move Mexallon at all — the surplus reaches the next plan as
    on-hand stock, and charging this cycle for it counted it twice (and
    swallowed a real saving: 150 bought 40% cheap used to read as the
    plan's own number)."""
    pid, run_id = build_run(conn)
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 150, 5.0)
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert line.unit_cost == pytest.approx(EFFECTIVE)

    # And a cheaper over-lock lands where the same price on the exact
    # quantity lands — the price is what moved, not the lot size.
    conn.execute("DELETE FROM run_purchase")
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 150, 3.0)
    over = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    conn.execute("DELETE FROM run_purchase")
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    exact = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert over.unit_cost == pytest.approx(exact.unit_cost)
    assert over.unit_cost == pytest.approx(
        (DIRECT_LANDED + 100 * (3.0 + HUB_RATE * ORE_M3) + ORE_TAX) / 1000
    )


def test_over_locking_a_direct_buy_is_the_same_rule(conn, ref, settings):
    """The rule the ore now follows, on the shape that always had it —
    the two over-lock paths must not disagree."""
    pid, run_id = build_run(conn)
    before = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, TRIT, HUB, 1500, 5.0)   # 1000 planned, plan price
    after = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert after[TRIT].unit_cost == pytest.approx(before[TRIT].unit_cost)


def test_covered_raw_costs_is_the_seam_the_buy_tab_reads(conn, ref, settings):
    """The Buy tab's Ladder / Locked / Δ cells on a compressed-covered
    raw are this call (contract review B32): the row's quantity there is
    the full DEMAND, which blend_purchases — sized to the direct
    remainder — cannot price. Whatever it says, hull_cost carries, so
    the tab and the Profit tab cannot drift apart."""
    pid, run_id = build_run(conn)

    def seam():
        rows = conn.execute(
            "SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,)
        ).fetchall()
        return costing.covered_raw_costs(
            rows,
            store.list_purchases(conn, run_id),
            ref,
            costing._run_freight_rates(conn, run_id),
            settings,
        )

    # Only the covered raw is in it — not the ore, not the plain buys.
    covered = seam()
    assert set(covered) == {MEXALLON}
    idle = covered[MEXALLON]
    assert (idle.demand, idle.direct_qty, idle.covered_qty) == (1000, 200, 800)
    assert (idle.locked_qty, idle.displaced_fraction) == (0, 0.0)
    assert idle.plan_landed == EFFECTIVE * 1000
    # Untouched: the plan's own float, so the tab's delta is a hard zero.
    assert idle.realized_landed == idle.plan_landed
    assert idle.delta == 0.0
    assert not idle.locked

    lock(conn, run_id, MEXALLON, HUB, 600, 2.0)
    locked = seam()[MEXALLON]
    assert locked.locked and locked.locked_qty == 600
    assert locked.displaced_fraction == pytest.approx(0.5)
    assert locked.displaced_qty == pytest.approx(400)
    assert locked.realized_landed == pytest.approx(600 * 12.0 + ORE_LANDED)
    assert locked.delta == pytest.approx(
        600 * 12.0 + ORE_LANDED - EFFECTIVE * 1000
    )
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert line.unit_cost == pytest.approx(locked.unit_cost)


# --- revision 3: the purchase basis (contract section 5, review C13) -------
#
# The lines are blended against what the cycle CONSUMES:
#
#   basis = max(cycle_need_qty or merged_min_qty, direct + covered)
#
# min(bought, basis) units at what was paid, the rest of the basis at the
# plan's price and venue split. Every hand-worked number below is at the
# fixture's rates: Tritanium's plan lands at 5.0 + 10.0 freight = 15.0 per
# unit, a Jita purchase at 2.0 lands at 12.0, one at 3.0 at 13.0.

ISOGEN = 37  # a mineral build_run's runs never hold


def set_columns(conn, run_id, type_id, **columns):
    """Rewrite one fixture row's columns in place (e.g. give it a need)."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE index_run_item SET {assignments} "
        "WHERE index_run_id = ? AND type_id = ?",
        (*columns.values(), run_id, type_id),
    )
    conn.commit()


def item_row(conn, run_id, type_id):
    return conn.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, type_id),
    ).fetchone()


@pytest.mark.parametrize(
    "row, basis, why",
    [
        ({"cycle_need_qty": 1000, "merged_min_qty": 900,
          "recommended_buy_qty": 400}, 1000, "the cycle need wins"),
        ({"cycle_need_qty": None, "merged_min_qty": 900,
          "recommended_buy_qty": 400}, 900, "pre-v1.28 row: merged demand"),
        ({"cycle_need_qty": 0, "merged_min_qty": 900,
          "recommended_buy_qty": 400}, 900, "a zero need falls through too"),
        ({"cycle_need_qty": 1000, "recommended_buy_qty": 1100},
         1100, "a buffered buy past the need is never cut back"),
        ({"cycle_need_qty": 1000, "recommended_buy_qty": 200,
          "compressed_covered_qty": 1100}, 1300,
         "direct + covered counts as bought"),
        ({"recommended_buy_qty": 400, "compressed_covered_qty": 300},
         700, "a dict row without the columns: revision 2's basis"),
        ({"recommended_buy_qty": None, "cycle_need_qty": None}, 0,
         "NULLs read as 0, never TypeError"),
        ({}, 0, "nothing at all"),
    ],
)
def test_purchase_basis(row, basis, why):
    assert costing.purchase_basis(row) == basis, why


def test_the_fixture_rows_keep_revision_2s_basis(conn):
    """C13.6: build_run's raws carry no need, so the basis is the plan's
    own buy (direct + covered) and no older expectation in this file
    moved. The Hulk's cycle need is its only need column."""
    _pid, run_id = build_run(conn)
    assert {
        type_id: costing.purchase_basis(item_row(conn, run_id, type_id))
        for type_id in (TRIT, PYERITE, MEXALLON, COMPRESSED_VELDSPAR, HULK)
    } == {
        TRIT: 1000, PYERITE: 1000, MEXALLON: 200 + 800,
        COMPRESSED_VELDSPAR: 100, HULK: 1,
    }


@pytest.mark.parametrize("need_column", ["cycle_need_qty", "merged_min_qty"])
@pytest.mark.parametrize(
    "bought, expected, why",
    [
        (400, (400 * 3.0 + 600 * 5.0) / 1000,
         "the plan's buy: the 600 stock units stay at the plan's 5.0 "
         "(revision 2 read 3.0 - it priced the stock at the purchase)"),
        (700, (700 * 3.0 + 300 * 5.0) / 1000,
         "past the plan's buy: bought units displace the stock slice"),
        (1000, 3.0, "the whole basis bought"),
        (1500, 3.0, "over-bought: the surplus is next cycle's stock (R3)"),
    ],
)
def test_a_stock_covered_slice_stays_at_the_plans_price(
    conn, ref, settings, need_column, bought, expected, why
):
    """Tritanium: the cycle consumes 1000, the plan buys 400 and draws
    600 from on-hand stock bought in an earlier cycle."""
    pid, run_id = build_run(conn)
    set_columns(
        conn, run_id, TRIT, recommended_buy_qty=400, **{need_column: 1000}
    )
    before = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert by_type(before)[TRIT].unit_cost == 5.0  # the early return

    lock(conn, run_id, TRIT, HUB, bought, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.unit_cost == pytest.approx(expected), why
    assert line.locked and line.venue == HUB
    assert line.hub_fraction == pytest.approx(1.0)
    # Same units, same venue: freight does not move.
    assert freight(cost) == freight(before)


def test_a_stock_slice_keeps_the_plans_venue_split_and_fill_prices(
    conn, ref, settings
):
    """Pyerite's plan buys 1000 (600 Jita at 4.0 + 400 C-J6 at 6.0) and
    the cycle consumes 1250. All 1000 bought in Jita at 3.0: the 250
    stock units keep the plan's 60/40 split at the plan's own per-venue
    prices (A11: nothing unsourced, so the fill prices are exact) -
    150 at 4.0 in Jita, 100 at 6.0 in C-J6."""
    pid, run_id = build_run(conn)
    set_columns(conn, run_id, PYERITE, cycle_need_qty=1250)
    lock(conn, run_id, PYERITE, HUB, 1000, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[PYERITE]
    hub_isk = 1000 * 3.0 + 150 * 4.0
    structure_isk = 100 * 6.0
    assert line.unit_cost == pytest.approx(
        (hub_isk + structure_isk) / 1250
    )                                                        # 3.36
    assert line.venue == SPLIT
    assert line.hub_fraction == pytest.approx(1150 / 1250)
    assert line.structure_fraction == pytest.approx(100 / 1250)
    assert line.hub_fill_price == pytest.approx(hub_isk / 1150)
    assert line.structure_fill_price == pytest.approx(6.0)
    # Pyerite's 10 m3 per hull now splits 9.2 / 0.8; Tritanium's 10 m3
    # still all goes through Jita.
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx((10 + 9.2) * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(0.8 * STRUCT_RATE),
    }


def test_a_replanned_row_with_no_buy_prices_its_purchases(
    conn, ref, settings
):
    """R2: a purchase belongs to the buying cycle, not the plan. The user
    bought 400 Tritanium in C-J6, then re-planned; the new plan covers the
    whole 1000 from stock and buys none. Revision 2 skipped such a row
    (its lines cost nothing); now the 400 are priced at what was paid and
    the other 600 at the plan's 5.0 - at the plan's own venue, since a
    row that buys nothing has no fill split (the legacy single-venue
    remainder)."""
    pid, run_id = build_run(conn)
    set_columns(
        conn, run_id, TRIT, recommended_buy_qty=0, recommended_action=None,
        cycle_need_qty=1000,
    )
    before = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert before[TRIT].unit_cost == 5.0 and not before[TRIT].locked

    lock(conn, run_id, TRIT, STRUCT, 400, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.unit_cost == pytest.approx((400 * 3.0 + 600 * 5.0) / 1000)
    assert line.locked and line.venue == SPLIT
    assert (line.hub_fraction, line.structure_fraction) == (
        pytest.approx(0.6), pytest.approx(0.4)
    )
    assert line.structure_cost_per_hull() == pytest.approx(400 * 3.0)
    # The 400 C-J6 units haul at the structure rate now: Tritanium 6 / 4
    # m3, on top of Pyerite's unchanged 6 / 4.
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx(12 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(8 * STRUCT_RATE),
    }


def test_esi_lines_price_like_any_line_and_clearing_them_restores_the_plan(
    conn, ref, settings
):
    """Revision 3's lines come from ESI through
    store.replace_derived_purchases (a wallet transaction, a contract
    item priced by R7's k). Costing reads venue / quantity / unit_price
    only, so they price exactly as hand-entered lines — and a run whose
    derived lines are cleared (it became superseded, C4) is back on the
    early return, bit-identical to a run nothing was bought for."""
    pid, run_id = build_run(conn)
    legacy = fingerprint(costing.hull_cost(conn, ref, settings, run_id, pid))
    store.replace_derived_purchases(conn, run_id, [
        {"type_id": TRIT, "venue": HUB, "quantity": 300, "unit_price": 3.0,
         "esi_kind": "transaction", "esi_id": 9001,
         "date": "2026-09-27T10:00:00Z", "owner_kind": "character",
         "owner_id": 90000001},
        {"type_id": TRIT, "venue": STRUCT, "quantity": 100, "unit_price": 4.0,
         "esi_kind": "contract", "esi_id": 7001, "contract_k": 0.8,
         "date": "2026-09-27T11:00:00Z", "owner_kind": "character",
         "owner_id": 90000001},
    ])
    conn.commit()
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[TRIT]
    # 300 + 100 bought, the other 600 of the 1000 at the plan's 5.0 (hub).
    assert line.unit_cost == pytest.approx(
        (300 * 3.0 + 100 * 4.0 + 600 * 5.0) / 1000
    )
    assert (line.hub_fraction, line.structure_fraction) == (
        pytest.approx(0.9), pytest.approx(0.1)
    )
    assert line.locked

    store.replace_derived_purchases(conn, run_id, [])
    conn.commit()
    assert fingerprint(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    ) == legacy


def test_an_empty_basis_keeps_the_plans_price(conn, ref, settings):
    """A row that neither consumes nor buys anything (no need recorded,
    no buy) has a basis of 0: min(bought, 0) = 0 units are priced at
    actual, so its lines move nothing and the plan's price stands."""
    pid, run_id = build_run(conn)
    set_columns(conn, run_id, TRIT, recommended_buy_qty=0)
    before = fingerprint(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, TRIT, HUB, 400, 3.0)
    assert fingerprint(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    ) == before


def test_lines_on_a_type_the_run_does_not_hold_touch_no_row(
    conn, ref, settings
):
    """C13.2: an ESI buy outside the plan (R3: it is stock) is stored on
    the run, arms the purchase pass - and prices nothing."""
    pid, run_id = build_run(conn)
    before = fingerprint(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, ISOGEN, HUB, 5000, 50.0)
    assert fingerprint(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    ) == before
    rates = costing._run_freight_rates(conn, run_id)
    assert costing._run_snapshot(
        conn, run_id, ref=ref, rates=rates, settings=settings
    ) == costing._run_snapshot(conn, run_id)


# The fixture's Mexallon with a stock slice: the cycle consumes 1250 -
# 200 bought direct, 800 covered by the ore, 250 drawn from stock at the
# plan's LANDED 3.77 (effective_unit_cost, never price_snapshot: that
# would drop the slice's freight). Lines at 2.0 in Jita land at 12.0.
STOCK_SLICE = 250 * EFFECTIVE                                # 942.5


@pytest.mark.parametrize(
    "bought, expected, why",
    [
        (200, (200 * 12.0 + ORE_LANDED + STOCK_SLICE) / 1250,
         "the direct buy: ore and stock at the plan"),
        (600, (600 * 12.0 + ORE_LANDED + STOCK_SLICE) / 1250,
         "half the covered units displaced (f = 0.5), ore bought whole"),
        (1000, (1000 * 12.0 + STOCK_SLICE) / 1250,
         "the whole demand bought: f = 1, no ore, stock still at plan"),
        (1100, (1100 * 12.0 + 150 * EFFECTIVE) / 1250,
         "past the demand: 100 units displace the stock slice (C13.4)"),
        (1250, 12.0, "the whole basis bought"),
        (1500, 12.0, "over-bought: capped at the basis, the rest is stock"),
    ],
)
def test_a_covered_raw_displaces_direct_then_covered_then_its_stock_slice(
    conn, ref, settings, bought, expected, why
):
    pid, run_id = build_run(conn)
    set_columns(conn, run_id, MEXALLON, cycle_need_qty=1250)
    untouched = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    assert untouched[MEXALLON].unit_cost == EFFECTIVE

    lock(conn, run_id, MEXALLON, HUB, bought, 2.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    assert line.unit_cost == pytest.approx(expected), why
    assert line.locked and line.landed and not line.approximate


def test_an_ore_purchase_leaves_a_covered_raws_stock_slice_at_the_plan(
    conn, ref, settings
):
    """The ore reprices the 800 covered units only; the 250 stock units
    were bought in an earlier cycle and keep the plan's landed price.
    Revision 2 (no slice) read (3000 + 570) / 1000 = 3.57."""
    pid, run_id = build_run(conn)
    set_columns(conn, run_id, MEXALLON, cycle_need_qty=1250)
    lock(conn, run_id, COMPRESSED_VELDSPAR, HUB, 100, 3.0)
    line = by_type(
        costing.hull_cost(conn, ref, settings, run_id, pid)
    )[MEXALLON]
    realized_ore = 100 * (3.0 + HUB_RATE * ORE_M3) + ORE_TAX   # 570
    assert line.unit_cost == pytest.approx(
        (DIRECT_LANDED + realized_ore + STOCK_SLICE) / 1250
    )                                                           # 3.61


def test_covered_raw_costs_reports_the_stock_slice(conn, ref, settings):
    """The seam carries the slice beside the revision-2 demand figures,
    which keep their meaning; unit_cost / delta are over the basis."""
    pid, run_id = build_run(conn)
    set_columns(conn, run_id, MEXALLON, cycle_need_qty=1250)

    def seam():
        rows = conn.execute(
            "SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,)
        ).fetchall()
        return costing.covered_raw_costs(
            rows, store.list_purchases(conn, run_id), ref,
            costing._run_freight_rates(conn, run_id), settings,
        )[MEXALLON]

    idle = seam()
    assert (idle.demand, idle.basis, idle.stock_qty) == (1000, 1250, 250)
    assert idle.plan_stock_landed == idle.realized_stock_landed
    assert idle.delta == 0.0 and not idle.locked

    lock(conn, run_id, MEXALLON, HUB, 1100, 2.0)
    cost = seam()
    assert cost.stock_displaced_qty == 100
    assert cost.displaced_fraction == 1.0
    assert cost.realized_landed == pytest.approx(1000 * 12.0)
    assert cost.plan_stock_landed == pytest.approx(STOCK_SLICE)
    assert cost.realized_stock_landed == pytest.approx(
        100 * 12.0 + 150 * EFFECTIVE
    )
    assert cost.realized_total == pytest.approx(13765.5)
    assert cost.plan_total == pytest.approx(EFFECTIVE * 1250)
    assert cost.delta == pytest.approx(13765.5 - EFFECTIVE * 1250)
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert line.unit_cost == pytest.approx(cost.unit_cost)
    assert cost.unit_cost == pytest.approx(13765.5 / 1250)


def test_buying_exactly_the_plan_on_a_bigger_basis_changes_no_number(
    conn, ref, settings
):
    """The sanity check again, now with stock slices on a plain row and
    on a covered raw: buying what the plan bought, where and at what the
    plan said, reproduces the plan's cost."""
    pid, run_id = build_run(conn)
    set_columns(
        conn, run_id, TRIT, recommended_buy_qty=400, cycle_need_qty=1000
    )
    set_columns(conn, run_id, MEXALLON, cycle_need_qty=1250)
    before = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    lock(conn, run_id, TRIT, HUB, 400, 5.0)
    lock(conn, run_id, MEXALLON, HUB, 200, 5.0)
    after = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
    for type_id in (TRIT, MEXALLON):
        assert after[type_id].locked
        assert after[type_id].unit_cost == pytest.approx(
            before[type_id].unit_cost
        ), type_id


# --- revision 3: the Buy tab's per-unit cells (R8, review C14.5) -----------


def test_bought_cell_on_a_plain_row(conn):
    """Tritanium (1000 at 5.0 in Jita, ladder 15.0 landed): 400 bought in
    Jita at 3.0, 100 in C-J6 at 4.0, 50 delivered at 20.0."""
    _pid, run_id = build_run(conn)
    lines = [
        {"venue": HUB, "quantity": 400, "unit_price": 3.0},
        {"venue": STRUCT, "quantity": 100, "unit_price": 4.0},
        {"venue": DELIVERED, "quantity": 50, "unit_price": 20.0},
    ]
    cell = costing.bought_cell(
        item_row(conn, run_id, TRIT), lines, HUB_RATE, STRUCT_RATE,
        MINERAL_M3,
    )
    landed = 400 * 13.0 + 100 * (4.0 + 2.0) + 50 * 20.0          # 6800
    assert cell.need == 1000 and cell.bought_qty == 550
    assert cell.bought_landed == pytest.approx(landed)
    assert cell.bought_unit == pytest.approx(landed / 550)
    assert cell.venue == SPLIT
    assert (cell.hub_qty, cell.structure_qty, cell.delivered_qty) == (
        400, 100, 50
    )
    assert cell.ladder_unit == pytest.approx(15.0)
    assert cell.delta == pytest.approx(landed - 15.0 * 550)       # -1450
    assert cell.remaining == 450
    # C14.5's own recipe, spelled out: the lines against a zero plan.
    assert cell.bought_landed == costing.blend_purchases(
        lines, costing.PlanBuy(qty=0, price=None), HUB_RATE, STRUCT_RATE,
        MINERAL_M3,
    ).landed_total


def test_ladder_landed_unit_per_row_shape(conn):
    _pid, run_id = build_run(conn)

    def ladder(type_id, m3=MINERAL_M3):
        return costing.ladder_landed_unit(
            item_row(conn, run_id, type_id), HUB_RATE, STRUCT_RATE, m3
        )

    # A fill-priced split: 60% at 4.0 + 10 freight, 40% at 6.0 + 2.
    pyerite = item_row(conn, run_id, PYERITE)
    assert ladder(PYERITE) == pytest.approx(0.6 * 14.0 + 0.4 * 8.0)  # 11.6
    assert ladder(PYERITE) == costing.blend_purchases(
        (), costing.plan_buy_from_row(pyerite), HUB_RATE, STRUCT_RATE,
        MINERAL_M3,
    ).landed_unit
    # A covered raw: its landed effective cost, as persisted.
    assert ladder(MEXALLON) == EFFECTIVE
    # The ore: its own raw price landed (the refining tax is not paid at
    # the market, so a purchase is compared ex-tax).
    assert ladder(COMPRESSED_VELDSPAR, ORE_M3) == pytest.approx(6.0)
    # A re-planned row that buys nothing still has a ladder.
    set_columns(conn, run_id, TRIT, recommended_buy_qty=0)
    assert ladder(TRIT) == pytest.approx(15.0)
    # Nothing priced, nothing to compare against.
    set_columns(conn, run_id, TRIT, price_snapshot=None)
    assert ladder(TRIT) is None
    assert costing.ladder_landed_unit(None) is None


def test_bought_cell_edges(conn):
    _pid, run_id = build_run(conn)
    hub_line = [{"venue": HUB, "quantity": 300, "unit_price": 2.0}]

    # A covered raw: Need is direct + covered, the ladder its effective
    # cost; bought units count against the whole Need.
    covered = costing.bought_cell(
        item_row(conn, run_id, MEXALLON), hub_line, HUB_RATE, STRUCT_RATE,
        MINERAL_M3,
    )
    assert (covered.need, covered.remaining) == (1000, 700)
    assert covered.delta == pytest.approx(300 * 12.0 - EFFECTIVE * 300)

    # Nothing bought: no unit, no venue, no delta, the whole Need left.
    idle = costing.bought_cell(
        item_row(conn, run_id, TRIT), [], HUB_RATE, STRUCT_RATE, MINERAL_M3
    )
    assert (idle.bought_qty, idle.bought_landed) == (0, 0.0)
    assert idle.bought_unit is None and idle.venue is None
    assert idle.delta is None and idle.remaining == 1000

    # A re-planned zero-buy row: Need 0, Remaining 0, still a delta.
    set_columns(conn, run_id, TRIT, recommended_buy_qty=0)
    replanned = costing.bought_cell(
        item_row(conn, run_id, TRIT), hub_line, HUB_RATE, STRUCT_RATE,
        MINERAL_M3,
    )
    assert (replanned.need, replanned.remaining) == (0, 0)
    assert replanned.delta == pytest.approx(300 * 12.0 - 15.0 * 300)

    # A type the run does not hold (outside the plan): landed, no ladder.
    outside = costing.bought_cell(
        None, hub_line, HUB_RATE, STRUCT_RATE, MINERAL_M3
    )
    assert outside.need == 0 and outside.ladder_unit is None
    assert outside.bought_landed == pytest.approx(300 * 12.0)
    assert outside.delta is None


# --- review 2026-09-28: pre-plan lines on a covered raw -----------------------
#
# A buying window can open before its run was planned (the newest plan's
# opens at the last execution; a superseded plan's purchases move to its
# successor). Units bought then were on hand when the plan was made: the
# plan netted them out of direct + covered into the stock slice. They
# fill that slice first and never displace direct or covered units, or a
# sibling raw is charged the ore twice.

PLANNED_START = "2026-09-10 12:00:00"


def _covered_pair_rows():
    """Two raws fully covered by one ore at shares 0.5 / 0.5, landed 4000,
    freight 0. Tritanium's cycle consumes 1000, of which the plan saw 600
    on hand: direct 0, covered 400, stock slice 600."""
    base = dict.fromkeys((
        "price_snapshot", "buy_venue", "hub_buy_qty", "structure_buy_qty",
        "hub_fill_price", "structure_fill_price", "unfilled_qty",
        "compressed_outputs", "compressed_alloc", "compressed_landed_isk",
        "compressed_tax_isk", "direct_landed_isk", "cycle_need_qty",
        "merged_min_qty", "effective_unit_cost", "compressed_covered_qty",
        "recommended_buy_qty",
    ))
    trit = dict(base, type_id=TRIT, recommended_buy_qty=0, compressed_covered_qty=400,
                effective_unit_cost=5.0, direct_landed_isk=0.0, cycle_need_qty=1000,
                price_snapshot=5.0, buy_venue=HUB)
    pye = dict(base, type_id=PYERITE, recommended_buy_qty=0, compressed_covered_qty=400,
               effective_unit_cost=5.0, direct_landed_isk=0.0, cycle_need_qty=400,
               price_snapshot=5.0, buy_venue=HUB)
    ore = dict(base, type_id=COMPRESSED_VELDSPAR, recommended_buy_qty=10,
               price_snapshot=400.0, buy_venue=HUB,
               compressed_outputs=json.dumps([[TRIT, 400, 400], [PYERITE, 400, 400]]),
               compressed_alloc=json.dumps({str(TRIT): 0.5, str(PYERITE): 0.5}),
               compressed_landed_isk=4000.0, compressed_tax_isk=0.0)
    return [trit, pye, ore]


def _dated(quantity, price, date):
    return {"venue": HUB, "quantity": quantity, "unit_price": price, "date": date}


def _pair_costs(ref, lines, planned_start=PLANNED_START):
    return costing.covered_raw_costs(
        _covered_pair_rows(), {TRIT: lines}, ref,
        {HUB: 0.0, STRUCT: 0.0}, None, planned_start,
    )


def test_pre_plan_units_fill_the_stock_slice_and_leave_the_sibling_alone(ref):
    """The review's worked case: 600 Tritanium at 4.0 bought before the
    plan, which netted them into the slice. Revision 3 read Tritanium 4400
    AND Pyerite 4000 (8400 for 6400 spent); the ore is now counted once."""
    costs = _pair_costs(ref, [_dated(600, 4.0, "2026-09-10T11:00:00Z")])
    trit, pye = costs[TRIT], costs[PYERITE]
    assert trit.displaced_fraction == 0.0 and trit.prefilled_qty == 600
    assert trit.stock_displaced_qty == 600
    assert trit.realized_stock_landed == pytest.approx(600 * 4.0)
    assert trit.realized_total == pytest.approx(4400.0)
    assert not pye.locked and pye.realized_total == 2000.0
    assert trit.realized_total + pye.realized_total == pytest.approx(600 * 4.0 + 4000.0)


@pytest.mark.parametrize("date", [
    "2026-09-10T12:00:01Z",   # after the plan — even on the same day (text order trap)
    None,                     # a hand-entered line keeps the displacement order
])
def test_lines_after_the_plan_still_displace_direct_then_covered(ref, date):
    """Revision 2's R7 stands for units bought after the plan: they left
    the ore's route, which pushes its cost onto the sibling (a ruling)."""
    costs = _pair_costs(ref, [_dated(600, 4.0, date)])
    trit, pye = costs[TRIT], costs[PYERITE]
    assert trit.displaced_fraction == 1.0 and trit.prefilled_qty == 0
    assert trit.realized_total == pytest.approx(4400.0)
    assert pye.realized_total == pytest.approx(4000.0)


def test_a_line_dated_exactly_at_planned_start_is_pre_plan(ref):
    costs = _pair_costs(ref, [_dated(600, 4.0, "2026-09-10T12:00:00Z")])
    assert costs[TRIT].prefilled_qty == 600 and costs[TRIT].displaced_fraction == 0.0


def test_no_planned_start_keeps_the_displacement_order(ref):
    costs = _pair_costs(ref, [_dated(600, 4.0, "2026-09-01T00:00:00Z")], planned_start=None)
    assert costs[TRIT].displaced_fraction == 1.0 and costs[TRIT].prefilled_qty == 0


def test_a_pre_plan_surplus_past_the_slice_joins_the_displacement_order(ref):
    """900 bought before the plan, slice 600: 600 fill the slice, the 300
    past it displace covered units (the line is split into whole units).
    Every ISK spent is counted once: 900 × 4 + the ore's 4000."""
    costs = _pair_costs(ref, [_dated(500, 4.0, "2026-09-09T00:00:00Z"),
                              _dated(400, 4.0, "2026-09-10T00:00:00Z")])
    trit, pye = costs[TRIT], costs[PYERITE]
    assert trit.prefilled_qty == 600
    assert trit.displaced_fraction == pytest.approx(300 / 400)
    assert trit.locked_qty == 900
    assert trit.realized_total + pye.realized_total == pytest.approx(900 * 4.0 + 4000.0)
    assert pye.realized_total == pytest.approx(0.8 * 4000.0)


def test_pre_plan_prefill_is_oldest_first(ref):
    costs = _pair_costs(ref, [_dated(400, 9.0, "2026-09-10T00:00:00Z"),
                              _dated(400, 1.0, "2026-09-09T00:00:00Z")])
    trit = costs[TRIT]
    # The 400 @ 1.0 (older) and 200 of the 400 @ 9.0 fill the slice.
    assert trit.realized_stock_landed == pytest.approx(400 * 1.0 + 200 * 9.0)
    assert trit.displaced_fraction == pytest.approx(200 / 400)


def test_the_snapshot_reads_the_runs_planned_start(conn, ref, settings):
    """End to end through hull_cost: the fixture's Mexallon with a 250-unit
    slice. 250 units dated before planned_start fill the slice (the demand
    stays at plan: (3770 + 250 × 12) / 1250); the same line dated after it
    displaces the direct buy instead, as revision 3 did."""
    pid, run_id = build_run(conn)
    set_columns(conn, run_id, MEXALLON, cycle_need_qty=1250)
    conn.execute("UPDATE index_run SET planned_start = ? WHERE index_run_id = ?",
                 (PLANNED_START, run_id))
    conn.commit()

    def mex_after(date):
        store.replace_derived_purchases(conn, run_id, [dict(
            type_id=MEXALLON, venue=HUB, quantity=250, unit_price=2.0,
            esi_kind="transaction", esi_id=1, date=date)])
        conn.commit()
        return by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]

    pre = mex_after("2026-09-10T11:59:59Z")
    assert pre.unit_cost == pytest.approx((EFFECTIVE * 1000 + 250 * 12.0) / 1250)
    post = mex_after("2026-09-10T12:00:01Z")
    assert post.unit_cost == pytest.approx(EFFECTIVE)


# --- review 2026-09-28: the Buy tab's cell knows which side of the plan -------
#
# The same pre-plan units, seen from the Buy tab: the plan netted them as
# on-hand stock, so its Need already leaves them out. They are still what
# the cycle paid (Bought, Δ), but taking them off Remaining again would
# read "nothing left" while the plan still needs its whole Need.


def test_bought_cell_takes_only_post_plan_units_off_remaining(conn):
    """Tritanium: Need 1000, ladder 15.0 landed (5.0 + 10 hub freight).
    450 units bought at or before PLANNED_START (400 at 11:00Z the same
    day — after it as TEXT, before it parsed — and 50 at the exact
    second), 130 after it (100 at C-J6 at 12:00:01Z, 30 on a hand line
    with no date). Bought and Δ count all 580; Remaining = 1000 − 130;
    the per-venue units feeding the Multibuy reduction are the 130's."""
    _pid, run_id = build_run(conn)
    lines = [
        _dated(400, 3.0, "2026-09-10T11:00:00Z"),
        _dated(50, 3.0, "2026-09-10T12:00:00Z"),
        {"venue": STRUCT, "quantity": 100, "unit_price": 4.0,
         "date": "2026-09-10T12:00:01Z"},
        {"venue": HUB, "quantity": 30, "unit_price": 3.0},
    ]
    row = item_row(conn, run_id, TRIT)
    cell = costing.bought_cell(
        row, lines, HUB_RATE, STRUCT_RATE, MINERAL_M3,
        planned_start=PLANNED_START,
    )
    landed = 480 * 13.0 + 100 * (4.0 + 2.0)                       # 6840
    assert (cell.bought_qty, cell.pre_plan_qty, cell.post_plan_qty) == (
        580, 450, 130
    )
    assert cell.bought_landed == pytest.approx(landed)
    assert cell.bought_unit == pytest.approx(landed / 580)
    assert cell.delta == pytest.approx(landed - 15.0 * 580)       # -1860
    assert (cell.hub_qty, cell.structure_qty, cell.delivered_qty) == (
        30, 100, 0
    )
    assert cell.remaining == 870

    # No planned_start: every line is post-plan — the cell as it was.
    plain = costing.bought_cell(row, lines, HUB_RATE, STRUCT_RATE, MINERAL_M3)
    assert (plain.pre_plan_qty, plain.post_plan_qty) == (0, 580)
    assert (plain.hub_qty, plain.structure_qty) == (480, 100)
    assert plain.remaining == 420
    assert (plain.bought_landed, plain.delta) == (cell.bought_landed, cell.delta)


def test_bought_cell_pre_plan_units_never_push_remaining_past_need(conn):
    """Everything bought before the plan: Remaining is the whole Need, and
    a post-plan over-buy floors it at 0."""
    _pid, run_id = build_run(conn)
    row = item_row(conn, run_id, TRIT)
    pre = [_dated(1500, 3.0, "2026-09-01T00:00:00Z")]
    cell = costing.bought_cell(row, pre, planned_start=PLANNED_START)
    assert (cell.pre_plan_qty, cell.post_plan_qty, cell.remaining) == (
        1500, 0, 1000
    )
    over = pre + [_dated(1200, 3.0, "2026-09-11T00:00:00Z")]
    cell = costing.bought_cell(row, over, planned_start=PLANNED_START)
    assert (cell.post_plan_qty, cell.remaining) == (1200, 0)


def test_bought_cell_and_the_prefill_split_share_one_boundary():
    """One predicate (costing._pre_plan): the lines the Buy tab calls
    pre-plan are exactly the lines realized costing lets fill a covered
    raw's stock slice when the slice has room — so the tab and the
    realized cost can never disagree on which side of the plan a
    purchase fell."""
    lines = [
        _dated(1, 1.0, "2026-09-10T11:59:59Z"),
        _dated(2, 1.0, "2026-09-10T12:00:00Z"),
        _dated(4, 1.0, "2026-09-10T12:00:01Z"),
        _dated(8, 1.0, "2026-09-09T23:00:00Z"),
        _dated(16, 1.0, None),
        _dated(32, 1.0, "not a date"),
    ]
    prefill, rest = costing._prefill_split(
        lines, 10**9, costing._when(PLANNED_START)
    )
    cell = costing.bought_cell(None, lines, planned_start=PLANNED_START)
    assert cell.pre_plan_qty == sum(int(l["quantity"]) for l in prefill) == 11
    assert cell.post_plan_qty == sum(int(l["quantity"]) for l in rest) == 52


# --- revision 4: purchases elsewhere, the 'other' venue (§2b) ----------------
#
# User ruling 2026-09-28: only Jita 4-4 hauls at the Jita rate and only the
# configured structure market at the structure rate; a purchase ANYWHERE
# else (another station, another structure, a contract starting elsewhere)
# is store.BUY_VENUE_OTHER and hauls at the default inbound rate — the one
# the run was PLANNED at (index_run.freight_in_default_isk_per_m3), the
# live setting on a run planned before that column. At the fixture's
# rates a Tritanium unit (0.01 m3) bought elsewhere at DEFAULT_RATE hauls
# 5.0 ISK.

DEFAULT_RATE = 500.0
OTHER_LEG = costing.OTHER_FREIGHT_LINE_NAME


def set_run_default_rate(conn, run_id, rate):
    conn.execute(
        "UPDATE index_run SET freight_in_default_isk_per_m3 = ? "
        "WHERE index_run_id = ?",
        (rate, run_id),
    )
    conn.commit()


def live_default_rate(conn, rate):
    """The Settings default rate set to ``rate``; returns fresh Settings."""
    conn.execute(
        "UPDATE settings SET freight_in_default_isk_per_m3 = ?", (rate,)
    )
    conn.commit()
    return store.get_settings(conn)


def test_run_freight_rates_reads_the_default_leg(conn):
    """The third leg's vintage: NULL on a run planned before the column
    (the caller falls back to the live setting), the plan-time figure
    once persisted."""
    _pid, run_id = build_run(conn)
    assert costing._run_freight_rates(conn, run_id) == {
        HUB: HUB_RATE, STRUCT: STRUCT_RATE, OTHER: None,
    }
    set_run_default_rate(conn, run_id, DEFAULT_RATE)
    assert costing._run_freight_rates(conn, run_id)[OTHER] == DEFAULT_RATE


def test_a_purchase_elsewhere_is_costed_at_the_runs_default_rate(
    conn, ref, settings
):
    """All 1000 Tritanium bought at another station at 3.0: the line is
    priced at 3.0 with every share stated (other 1.0), its 10 m3 leave
    the Jita leg (16 → 6 m3, Pyerite's) and ride the third leg at the
    RUN's 500 ISK/m3, whatever the live setting says."""
    pid, run_id = build_run(conn)
    before = costing.hull_cost(conn, ref, settings, run_id, pid)
    set_run_default_rate(conn, run_id, DEFAULT_RATE)
    settings = live_default_rate(conn, 9999.0)  # must not reprice the run
    lock(conn, run_id, TRIT, OTHER, 1000, 3.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.unit_cost == pytest.approx(3.0)
    assert line.venue == OTHER
    assert line.locked and not line.landed
    assert (line.hub_fraction, line.structure_fraction,
            line.delivered_fraction, line.other_fraction) == (
        0.0, 0.0, 0.0, 1.0
    )
    assert line.other_fill_price == pytest.approx(3.0)
    # Never Null Sec Market Share, even if "elsewhere" is a null-sec
    # structure: that share is the configured market's alone.
    assert line.structure_share() == 0.0
    assert cost.structure_material_cost == pytest.approx(400 * 6.0)
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx(6 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(4 * STRUCT_RATE),
        OTHER_LEG: pytest.approx(10 * DEFAULT_RATE),
    }
    leg = next(l for l in cost.lines if l.name == OTHER_LEG)
    assert (leg.kind, leg.venue, leg.qty_per_hull, leg.unit_cost) == (
        "freight_in", OTHER, pytest.approx(10.0), DEFAULT_RATE
    )
    # Tritanium was 1000 × 5.0 + 10 m3 × 1000 = 15,000 landed; now
    # 1000 × 3.0 + 10 m3 × 500 = 8,000.
    assert cost.total == pytest.approx(before.total - 7000.0)


def test_the_default_leg_falls_back_to_the_live_setting_on_an_older_run(
    conn, ref, settings
):
    """A run planned before the column carries NULL there: the third leg
    takes the live setting. While that is 0 — its default — a purchase
    elsewhere hauls for free and the leg is not emitted at all."""
    pid, run_id = build_run(conn)
    lock(conn, run_id, TRIT, OTHER, 1000, 3.0)
    free = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert settings.freight_in_default_isk_per_m3 == 0.0
    assert freight(free) == {
        "Inbound freight (Jita)": pytest.approx(6 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(4 * STRUCT_RATE),
    }
    settings = live_default_rate(conn, 750.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert freight(cost)[OTHER_LEG] == pytest.approx(10 * 750.0)
    assert by_type(cost)[TRIT].unit_cost == pytest.approx(3.0)


def test_a_split_between_jita_and_elsewhere_hauls_each_share_at_its_leg(
    conn, ref, settings
):
    """600 Tritanium in Jita at 3.0, 400 elsewhere at 4.0: a SPLIT line,
    6 m3 on the Jita leg (plus Pyerite's 6) and 4 m3 on the third."""
    pid, run_id = build_run(conn)
    set_run_default_rate(conn, run_id, DEFAULT_RATE)
    lock(conn, run_id, TRIT, HUB, 600, 3.0)
    lock(conn, run_id, TRIT, OTHER, 400, 4.0)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    line = by_type(cost)[TRIT]
    assert line.venue == SPLIT
    assert line.unit_cost == pytest.approx((600 * 3.0 + 400 * 4.0) / 1000)
    assert (line.hub_fraction, line.structure_fraction,
            line.delivered_fraction, line.other_fraction) == (
        pytest.approx(0.6), 0.0, 0.0, pytest.approx(0.4)
    )
    assert (line.hub_fill_price, line.other_fill_price) == (3.0, 4.0)
    assert line.structure_share() == 0.0
    assert freight(cost) == {
        "Inbound freight (Jita)": pytest.approx(12 * HUB_RATE),
        f"Inbound freight ({settings.structure_market_label()})":
            pytest.approx(4 * STRUCT_RATE),
        OTHER_LEG: pytest.approx(4 * DEFAULT_RATE),
    }
    assert cost.structure_material_cost == pytest.approx(400 * 6.0)


def test_a_default_rate_moves_nothing_on_a_run_bought_only_at_the_markets(
    conn, ref, settings
):
    """The bit-identity promise, extended: a default rate (on the run and
    live) changes no number of a run with no purchase lines, nor of one
    bought only at Jita and the structure market — whose purchase-priced
    line states its 'other' share as an explicit 0.0 (a plan-priced line
    leaves it None)."""
    pid, run_id = build_run(conn)
    idle = fingerprint(costing.hull_cost(conn, ref, settings, run_id, pid))
    legacy = costing._run_snapshot(conn, run_id)
    set_run_default_rate(conn, run_id, 5000.0)
    rich = live_default_rate(conn, 5000.0)
    assert fingerprint(costing.hull_cost(conn, ref, rich, run_id, pid)) == idle
    assert costing._run_snapshot(
        conn, run_id, ref=ref, rates=costing._run_freight_rates(conn, run_id),
        settings=rich,
    ) == legacy
    assert all(
        line.other_fraction is None and line.other_fill_price is None
        for line in costing.hull_cost(conn, ref, rich, run_id, pid).lines
    )

    lock(conn, run_id, TRIT, HUB, 600, 3.0)
    lock(conn, run_id, TRIT, STRUCT, 400, 7.0)
    with_rate = fingerprint(costing.hull_cost(conn, ref, rich, run_id, pid))
    set_run_default_rate(conn, run_id, 0.0)
    plain = live_default_rate(conn, 0.0)
    cost = costing.hull_cost(conn, ref, plain, run_id, pid)
    assert fingerprint(cost) == with_rate
    line = by_type(cost)[TRIT]
    assert (line.other_fraction, line.other_fill_price) == (0.0, None)
    assert OTHER_LEG not in freight(cost)


def test_blend_purchases_carries_a_fourth_bucket():
    """Hand-worked, at the fixture's rates and 500 ISK/m3 elsewhere:
    100 Jita @ 2.0, 50 elsewhere @ 4.0, 50 delivered @ 9.0.
    order 200 + 200 + 450 = 850; freight (100 × 1000 + 50 × 500) × 0.01
    = 1250; landed 2100 over 200 units."""
    lines = [
        {"venue": HUB, "quantity": 100, "unit_price": 2.0},
        {"venue": OTHER, "quantity": 50, "unit_price": 4.0},
        {"venue": DELIVERED, "quantity": 50, "unit_price": 9.0},
    ]
    nothing = costing.PlanBuy(qty=0, price=None)
    blend = costing.blend_purchases(
        lines, nothing, HUB_RATE, STRUCT_RATE, MINERAL_M3,
        other_rate=DEFAULT_RATE,
    )
    assert blend.unit_cost == pytest.approx(850 / 200)
    assert blend.landed_total == pytest.approx(2100.0)
    assert blend.landed_unit == pytest.approx(10.5)
    assert (blend.hub_fraction, blend.structure_fraction,
            blend.delivered_fraction, blend.other_fraction) == (
        0.5, 0.0, 0.25, 0.25
    )
    assert blend.other_fill_price == pytest.approx(4.0)
    assert blend.venue == SPLIT
    # A positional caller written before the keyword existed: the
    # 'other' units haul at its default of 0.
    assert costing.blend_purchases(
        lines, nothing, HUB_RATE, STRUCT_RATE, MINERAL_M3
    ).landed_total == pytest.approx(1850.0)
    # Bought wholly elsewhere: that is the venue.
    alone = costing.blend_purchases(
        [{"venue": OTHER, "quantity": 10, "unit_price": 1.0}], nothing,
        m3=MINERAL_M3, other_rate=DEFAULT_RATE,
    )
    assert alone.venue == OTHER
    assert alone.landed_total == pytest.approx(10 * (1.0 + 5.0))
    # The plan's remainder never lands elsewhere: 40 bought elsewhere,
    # the other 60 of a 100-unit hub plan at the plan's 7.0 via Jita.
    partial = costing.blend_purchases(
        [{"venue": OTHER, "quantity": 40, "unit_price": 5.0}],
        costing.PlanBuy(qty=100, price=7.0, venue=HUB),
        HUB_RATE, STRUCT_RATE, MINERAL_M3, other_rate=DEFAULT_RATE,
    )
    assert (partial.hub_fraction, partial.other_fraction) == (0.6, 0.4)
    assert partial.landed_total == pytest.approx(
        40 * 5.0 + 60 * 7.0 + (60 * HUB_RATE + 40 * DEFAULT_RATE) * MINERAL_M3
    )
    # No 'other' units: the default rate cannot move a single bit.
    markets = lines[:1] + [{"venue": STRUCT, "quantity": 30, "unit_price": 6.0}]
    assert costing.blend_purchases(
        markets, costing.PlanBuy(qty=500, price=5.0, venue=HUB),
        HUB_RATE, STRUCT_RATE, MINERAL_M3, other_rate=123456.0,
    ) == costing.blend_purchases(
        markets, costing.PlanBuy(qty=500, price=5.0, venue=HUB),
        HUB_RATE, STRUCT_RATE, MINERAL_M3,
    )
    assert costing._EMPTY_BLEND.other_fraction == 0.0


def test_bought_cell_counts_other_units_and_lands_them_at_the_default_rate(
    conn,
):
    """Tritanium (Need 1000, ladder 15.0 landed): 400 Jita @ 3.0, 100
    C-J6 @ 4.0, 50 delivered @ 20.0 and 200 elsewhere @ 3.5 at 500
    ISK/m3. landed = 400 × 13 + 100 × 6 + 50 × 20 + 200 × 8.5 = 8500."""
    _pid, run_id = build_run(conn)
    row = item_row(conn, run_id, TRIT)
    lines = [
        {"venue": HUB, "quantity": 400, "unit_price": 3.0},
        {"venue": STRUCT, "quantity": 100, "unit_price": 4.0},
        {"venue": DELIVERED, "quantity": 50, "unit_price": 20.0},
        {"venue": OTHER, "quantity": 200, "unit_price": 3.5},
    ]
    cell = costing.bought_cell(
        row, lines, HUB_RATE, STRUCT_RATE, MINERAL_M3,
        other_rate=DEFAULT_RATE,
    )
    assert cell.bought_qty == cell.post_plan_qty == 750
    assert cell.bought_landed == pytest.approx(8500.0)
    assert (cell.hub_qty, cell.structure_qty, cell.delivered_qty,
            cell.other_qty) == (400, 100, 50, 200)
    assert (cell.hub_qty + cell.structure_qty + cell.delivered_qty
            + cell.other_qty) == cell.post_plan_qty
    assert cell.venue == SPLIT
    assert cell.remaining == 250
    assert cell.delta == pytest.approx(8500.0 - 15.0 * 750)          # -2750
    # Without the keyword the 'other' units haul for free.
    assert costing.bought_cell(
        row, lines, HUB_RATE, STRUCT_RATE, MINERAL_M3
    ).bought_landed == pytest.approx(7500.0)
    elsewhere = costing.bought_cell(
        row, lines[3:], HUB_RATE, STRUCT_RATE, MINERAL_M3,
        other_rate=DEFAULT_RATE,
    )
    assert (elsewhere.venue, elsewhere.other_qty) == (OTHER, 200)


def test_a_plan_chosen_ore_bought_elsewhere_lands_at_the_default_rate(
    conn, ref, settings
):
    """ore_realized_landed's per-line freight goes through venue_rate: the
    fixture's 100 Compressed Veldspar bought elsewhere at 3.0 on a run
    planned at 4000 ISK/m3 lands at 3.0 + 4.0 per unit — the same line in
    Jita (1000 ISK/m3) would land at 3.0 + 1.0."""
    pid, run_id = build_run(conn)
    set_run_default_rate(conn, run_id, 4000.0)
    lock(conn, run_id, COMPRESSED_VELDSPAR, OTHER, 100, 3.0)
    line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))[MEXALLON]
    assert line.unit_cost == pytest.approx(
        (DIRECT_LANDED + 100 * (3.0 + 4000.0 * ORE_M3) + ORE_TAX) / 1000
    )                                                             # 3.87


def test_a_pre_plan_stock_fill_bought_elsewhere_lands_at_the_default_rate(ref):
    """The covered raw's pre-plan stock slice prices each line through the
    same venue_rate: 600 Tritanium elsewhere at 4.0, at 500 ISK/m3, fill
    the slice at 4.0 + 5.0."""
    costs = costing.covered_raw_costs(
        _covered_pair_rows(),
        {TRIT: [dict(_dated(600, 4.0, "2026-09-10T11:00:00Z"), venue=OTHER)]},
        ref, {HUB: 0.0, STRUCT: 0.0, OTHER: DEFAULT_RATE}, None,
        PLANNED_START,
    )
    assert costs[TRIT].prefilled_qty == 600
    assert costs[TRIT].realized_stock_landed == pytest.approx(600 * 9.0)


def test_a_plan_priced_split_never_hauls_on_the_default_leg(settings, ref):
    """_freight_in_lines' third share is the line's other_fraction; a
    plan-priced split carries None there, so the leg stays empty however
    high the default rate."""
    split = costing.CostLine(
        type_id=PYERITE, name="Pyerite", kind="material", depth=1,
        qty_per_hull=1000.0, unit_cost=4.8, lag_runs=0, clamped=False,
        venue=SPLIT, hub_fraction=0.6,
    )
    rates = {HUB: HUB_RATE, STRUCT: STRUCT_RATE, OTHER: 5000.0}
    legs = {
        l.venue: l.qty_per_hull
        for l in costing._freight_in_lines(settings, ref, [split], rates)
    }
    assert legs == {HUB: pytest.approx(6.0), STRUCT: pytest.approx(4.0)}
    bought = costing.CostLine(
        type_id=PYERITE, name="Pyerite", kind="material", depth=1,
        qty_per_hull=1000.0, unit_cost=4.8, lag_runs=0, clamped=False,
        venue=SPLIT, hub_fraction=0.5, structure_fraction=0.2,
        delivered_fraction=0.1, other_fraction=0.2, locked=True,
    )
    legs = {
        l.venue: (l.qty_per_hull, l.unit_cost, l.name)
        for l in costing._freight_in_lines(settings, ref, [bought], rates)
    }
    assert legs[OTHER] == (pytest.approx(2.0), 5000.0, OTHER_LEG)
    assert legs[HUB][0] == pytest.approx(5.0)
    assert legs[STRUCT][0] == pytest.approx(2.0)


# --- revision 6: via-ore lines are dated like any other ---------------------
#
# A mineral line refined from an UNPLANNED compressed ore (via_type_id
# set; buying.assign_purchases writes it as a delivered, already-landed
# line). Revision 4 §3 made it post-plan whatever its date, because the
# plan then ignored hangar compressed stock. Revision 6 (user ruling
# 2026-09-28, R3/R4) counts hangar compressed ore as the raws it
# reprocesses into, so ore bought before the plan was netted like any
# stock: costing._pre_plan is a pure date test again, and a pre-plan via
# line fills the stock slice; a post-plan one still displaces direct →
# covered → stock.

COMPRESSED_SCORDITE = 62520


def _via(quantity, price, date, ore=COMPRESSED_SCORDITE):
    return {"venue": DELIVERED, "quantity": quantity, "unit_price": price,
            "date": date, "via_type_id": ore}


def test_pre_plan_is_a_pure_date_test_for_a_via_ore_line():
    cut = costing._when(PLANNED_START)
    early = "2026-09-01T00:00:00Z"
    late = "2026-09-11T00:00:00Z"
    assert costing._pre_plan(_via(1, 1.0, early), cut) is True
    assert costing._pre_plan(_via(1, 1.0, "2026-09-10T12:00:00Z"), cut)
    assert costing._pre_plan(_via(1, 1.0, late), cut) is False
    assert costing._pre_plan(_via(1, 1.0, None), cut) is False
    assert costing._pre_plan(_via(1, 1.0, early), None) is False
    # The via column changes nothing: the same line as a direct purchase
    # falls on the same side.
    for date in (early, late):
        assert costing._pre_plan(_via(1, 1.0, date), cut) == costing._pre_plan(
            _dated(1, 1.0, date), cut
        )
    # The slice takes a pre-plan via line like any other, oldest first.
    prefill, rest = costing._prefill_split(
        [_via(5, 1.0, early), _dated(3, 1.0, early), _via(7, 1.0, late)],
        10**9, cut,
    )
    assert [int(l["quantity"]) for l in prefill] == [5, 3]
    assert [int(l["quantity"]) for l in rest] == [7]


def test_a_via_ore_line_is_stock_before_the_plan_and_displaces_after(
    conn, ref, settings
):
    """The fixture's Mexallon with a 250-unit stock slice (cycle need
    1250 = 200 direct + 800 covered + 250 stock) and 250 Mexallon at 2.0
    landed refined from an unplanned ore, written as the matcher writes
    them (store.replace_derived_purchases, so the line is a sqlite3.Row
    carrying via_type_id).

    Dated BEFORE planned_start the plan counted the ore as stock (R3):
    the 250 fill the slice, exactly as a plain delivered purchase does:
        (3770 + 250 × 2.0) / 1250 = 3.416.
    Dated AFTER it they are fresh supply and displace the 200 direct and
    50 of the 800 covered (f = 50 / 800) at 2.0; the ore is still bought
    whole for the 750 left on its route (770 landed), and the slice stays
    at the plan's 3.77:
        (200 × 2.0 + 50 × 2.0 + 770 + 250 × 3.77) / 1250 = 1.77."""
    pid, run_id = build_run(conn)
    set_columns(conn, run_id, MEXALLON, cycle_need_qty=1250)
    conn.execute(
        "UPDATE index_run SET planned_start = ? WHERE index_run_id = ?",
        (PLANNED_START, run_id),
    )
    conn.commit()

    def mexallon(date, **extra):
        store.replace_derived_purchases(conn, run_id, [dict(
            type_id=MEXALLON, venue=DELIVERED, quantity=250, unit_price=2.0,
            esi_kind="transaction", esi_id=1, date=date, **extra,
        )])
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,)
        ).fetchall()
        seam = costing.covered_raw_costs(
            rows, store.list_purchases(conn, run_id), ref,
            costing._run_freight_rates(conn, run_id), settings, PLANNED_START,
        )[MEXALLON]
        line = by_type(costing.hull_cost(conn, ref, settings, run_id, pid))
        return seam, line[MEXALLON]

    before = "2026-09-10T11:59:59Z"
    seam, line = mexallon(before, via_type_id=COMPRESSED_SCORDITE)
    assert store.list_purchases(conn, run_id)[MEXALLON][0]["via_type_id"] == (
        COMPRESSED_SCORDITE
    )
    assert seam.prefilled_qty == 250 and seam.displaced_fraction == 0.0
    stock_fill = (EFFECTIVE * 1000 + 250 * 2.0) / 1250            # 3.416
    assert line.unit_cost == pytest.approx(stock_fill)
    assert line.locked

    seam, line = mexallon(before)
    assert seam.prefilled_qty == 250 and seam.displaced_fraction == 0.0
    assert line.unit_cost == pytest.approx(stock_fill)

    seam, line = mexallon("2026-09-10T12:00:01Z", via_type_id=COMPRESSED_SCORDITE)
    assert seam.prefilled_qty == 0
    assert seam.displaced_fraction == pytest.approx(50 / 800)
    assert line.unit_cost == pytest.approx(
        (200 * 2.0 + 50 * 2.0 + ORE_LANDED + 250 * EFFECTIVE) / 1250
    )                                                             # 1.77


def test_bought_cell_counts_a_pre_plan_via_ore_line_as_pre_plan(conn):
    """Tritanium (Need 1000): 300 refined from an unplanned ore and 100
    bought in Jita, both dated before the plan — all 400 are pre-plan
    (the plan netted the hangar ore at its yield, R3), so no venue share
    counts them and ``remaining`` (unused by the page since revision 6)
    stays the whole Need. The Purchased figures still count every line
    (delivered: the 300 haul nothing)."""
    _pid, run_id = build_run(conn)
    early = "2026-09-10T11:00:00Z"
    lines = [_via(300, 2.0, early), _dated(100, 3.0, early)]
    cell = costing.bought_cell(
        item_row(conn, run_id, TRIT), lines, HUB_RATE, STRUCT_RATE,
        MINERAL_M3, planned_start=PLANNED_START, other_rate=DEFAULT_RATE,
    )
    assert (cell.bought_qty, cell.pre_plan_qty, cell.post_plan_qty) == (
        400, 400, 0
    )
    assert (cell.hub_qty, cell.structure_qty, cell.delivered_qty,
            cell.other_qty) == (0, 0, 0, 0)
    assert cell.remaining == 1000
    assert cell.bought_landed == pytest.approx(300 * 2.0 + 100 * 13.0)
    assert cell.bought_unit == pytest.approx((300 * 2.0 + 100 * 13.0) / 400)
