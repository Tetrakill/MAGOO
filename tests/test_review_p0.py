"""Regression tests for the 2026-09-05 review's P0 findings. Each FAILS on
the current tree and passes once its finding is fixed. Drop into tests/ to
adopt. Fixtures: real SDE (read-only), temp state DB, FairValuePrices.

Run: .venv/Scripts/python.exe -m pytest -q tests/test_review_p0.py
"""

import pytest

from magoo import config, costing, engine, store
from magoo.engine import Snapshot

from conftest import FairValuePrices
from test_alchemy import Overlay, add_pipeline, conn  # noqa: F401 — fixture

HUB, STRUCT = store.BUY_VENUE_HUB, store.BUY_VENUE_STRUCTURE
TRITANIUM = 34


def _snapshot(ref, ladders=None, slots=500):
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=FairValuePrices(ref),
        adjusted_prices=Overlay(),
        sell_ladders=ladders or {},
    )


# P0-1 — invention chance counts Production-group gate skills as datacore
# sciences (costing.py invention_chance).
def test_invention_chance_uses_only_the_two_datacore_sciences(conn, ref):
    settings = store.get_settings(conn)  # all skills V
    # Capital Remote Shield Booster I: datacores x2 + Capital Ship
    # Construction as a GATE skill (group 268, not a science).
    source = ref.invention_source_for_product(
        ref.type_id("Capital Remote Shield Booster II")
    )
    assert source is not None
    chance = costing.invention_chance(ref, settings, source, None)
    assert chance == pytest.approx(
        source.probability * (1 + (5 + 5) / 30 + 5 / 40), rel=1e-9
    )


# P0-2 — a dual-role final's component share is skipped by the consumption
# feedback loop, under-building it (engine.py plan_index_run loop guard).
def test_dual_role_final_builds_at_least_its_consumers_draw(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    add_pipeline(conn, ref, "Crystalline Carbonide", 1000)
    plan = engine.plan_index_run(conn, ref, _snapshot(ref), persist=False)
    draw = engine._planned_consumption(conn, ref, plan.items)
    cc = plan.items[ref.type_id("Crystalline Carbonide")]
    assert cc.recommended_build_qty + cc.recommended_buy_qty >= (
        cc.requested_qty + draw.get(cc.type_id, 0)
    )


# P0-3a — a cached hub quote with no stored hub LADDER must still compete:
# a structure-only ladder cannot route the whole buy to C-J6 at a dearer
# price (engine.py _sourcing_pass has_ladder / costing.fill_merged).
def test_structure_only_ladder_keeps_the_cheaper_hub_quote(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    ladders = {STRUCT: {TRITANIUM: [(50.0, 10**12)]}}  # hub quote is 10.0
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    trit = plan.items[TRITANIUM]
    assert trit.price_snapshot == pytest.approx(10.0)
    assert trit.buy_venue == HUB


# P0-3b — with price_source = 'buy' no hub sell ladder exists at all; the
# pass must not fill from a dearer C-J6 book. v1.26: the hub quote stands in
# as one unbounded synthetic rung, so the walk keeps every unit at Jita's
# quote (no order count: it is a quote, not a walked book) and the dearer
# structure ladder never wins a unit.
def test_buy_price_source_never_fill_prices_from_structure_only(conn, ref):
    conn.execute("UPDATE settings SET price_source = 'buy'")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)
    ladders = {STRUCT: {TRITANIUM: [(50.0, 10**12)]}}
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    trit = plan.items[TRITANIUM]
    assert trit.price_snapshot == pytest.approx(10.0)
    assert trit.buy_venue == HUB
    assert trit.structure_buy_qty == 0 and trit.unfilled_qty == 0
    assert trit.hub_buy_qty == trit.recommended_buy_qty
    assert trit.hub_fill_orders is None  # a quote, not orders walked
