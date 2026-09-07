"""Fill-aware build-vs-buy and the per-market pricing basis (v1.26).

A buildable intermediate's buy side is judged rung by rung over its cycle
quantity: units the market sells at or below the build cost are bought,
the rest — dearer rungs and units no market holds — is built, and a
capacity shortfall may only be bought from rungs that exist. Each market
prices at its best order for any quantity ('min_sell' / 'max_buy') or by
walking its sell book ('ladder').

Same fixture pattern as test_compressed: real reference data, temp state
DB, FairValuePrices (raws at 10.0; a built item's fair value sits above its
chain cost, so Ion Thruster builds by default in a Hulk plan).
"""

import math
import sqlite3

import pytest

from magoo import config, engine, market, store
from magoo.engine import Snapshot

from conftest import FairValuePrices
from test_alchemy import Overlay, add_pipeline, conn  # noqa: F401 — fixture
from test_web_lifecycle import _settings_form

HUB = store.BUY_VENUE_HUB
STRUCT = store.BUY_VENUE_STRUCTURE


def _snapshot(ref, ladders=None, overrides=None, slots=500, hub_prices=None,
              structure_prices=None):
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=FairValuePrices(ref, overrides=overrides),
        adjusted_prices=Overlay(),
        sell_ladders=ladders or {},
        hub_prices=hub_prices or {},
        structure_prices=structure_prices or {},
    )


def _thruster(conn, ref):
    """A Hulk pipeline with no freight: Ion Thruster is a built intermediate
    whose cycle quantity and chain cost the tests read off a baseline."""
    add_pipeline(conn, ref, "Hulk", 8)
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 0, "
        "structure_freight_in_isk_per_m3 = 0"
    )
    conn.commit()
    thruster = ref.type_id("Ion Thruster")
    baseline = engine.plan_index_run(conn, ref, _snapshot(ref), persist=False)
    item = baseline.items[thruster]
    assert item.recommended_action == "build" and item.jobs_allocated > 0
    units = item.total_runs_needed * item.portion_size
    return thruster, units, item.unit_chain_cost, item.portion_size


# --- Phase 5: the split ------------------------------------------------------


def test_shallow_cheap_book_buys_what_exists_and_builds_the_rest(conn, ref):
    thruster, units, chain, portion = _thruster(conn, ref)
    half = units // 2
    ladders = {HUB: {thruster: [(chain * 0.9, half)]}}
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    item = plan.items[thruster]
    assert item.market_buy_qty == half
    assert item.market_fallback_qty == 0  # nothing dearer on any book
    assert item.build_savings_per_unit is None  # built units have no fallback
    assert item.recommended_action == "both"
    assert item.jobs_allocated > 0 and not item.capacity_limited
    assert item.recommended_build_qty >= units - half
    assert item.recommended_build_qty < units - half + portion
    assert item.recommended_buy_qty == half
    # The sourcing pass fills the bought part off the same cheap rung.
    assert item.price_snapshot == pytest.approx(chain * 0.9)
    assert (item.hub_buy_qty, item.unfilled_qty) == (half, 0)


def test_dearer_rungs_are_built_and_remain_the_only_fallback(conn, ref):
    thruster, units, chain, _portion = _thruster(conn, ref)
    ladders = {HUB: {thruster: [(chain * 0.9, 100), (chain * 1.5, 10**9)]}}
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    item = plan.items[thruster]
    assert item.market_buy_qty == 100
    assert item.market_fallback_qty == units - 100  # the dearer book, capped
    assert item.build_savings_per_unit == pytest.approx(chain * 0.5)
    assert item.recommended_action == "both"
    assert item.recommended_buy_qty == 100 and not item.capacity_limited


def test_every_unit_cheaper_on_the_market_is_bought_at_the_fill(conn, ref):
    thruster, units, chain, _portion = _thruster(conn, ref)
    ladders = {HUB: {thruster: [(chain * 0.8, units // 3), (chain * 0.99, 10**9)]}}
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    item = plan.items[thruster]
    assert item.recommended_action == "buy"
    assert item.jobs_allocated == 0 and item.market_buy_qty == 0
    assert item.build_savings_per_unit is not None and item.build_savings_per_unit < 0
    assert item.recommended_buy_qty == units
    assert item.hub_fill_orders == 2


def test_a_cheap_best_order_over_a_dear_book_no_longer_buys_the_lot(conn, ref):
    """The pre-v1.26 rule judged the best single order: one cheap rung
    would have flipped the whole quantity to buy."""
    thruster, units, chain, _portion = _thruster(conn, ref)
    ladders = {HUB: {thruster: [(chain * 0.5, 1), (chain * 2.0, 10**9)]}}
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    item = plan.items[thruster]
    assert item.market_buy_qty == 1
    assert item.jobs_allocated > 0
    assert item.recommended_buy_qty == 1


def test_no_ladder_keeps_the_single_quote_rule(conn, ref):
    thruster, units, chain, _portion = _thruster(conn, ref)
    plan = engine.plan_index_run(
        conn, ref, _snapshot(ref, overrides={thruster: chain * 0.9}), persist=False
    )
    item = plan.items[thruster]
    assert item.recommended_action == "buy" and item.market_buy_qty == 0
    assert item.market_fallback_qty is None
    plan = engine.plan_index_run(
        conn, ref, _snapshot(ref, overrides={thruster: chain * 1.1}), persist=False
    )
    item = plan.items[thruster]
    assert item.recommended_action == "build" and item.market_fallback_qty is None


def test_capacity_shortfall_buys_only_what_the_book_holds(conn, ref):
    """A manufacturing pool exactly the size of the Hulk's own jobs: the
    finals take every slot first, so every manufactured intermediate is a
    capacity loser. With a dear book holding fewer units than the
    shortfall, only those are bought and the rest is unmet; with no ladder
    the whole shortfall is bought as before."""
    thruster, units, chain, _portion = _thruster(conn, ref)
    hulk = ref.type_id("Hulk")
    base = engine.plan_index_run(conn, ref, _snapshot(ref), persist=False)
    pool = base.items[hulk].jobs_needed_unconstrained
    ladders = {HUB: {thruster: [(chain * 1.5, 250)]}}
    snap = _snapshot(ref, ladders)
    snap.slots_available[config.ACTIVITY_MANUFACTURING] = pool
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    assert plan.items[hulk].jobs_allocated == pool
    item = plan.items[thruster]
    assert item.capacity_limited and item.jobs_allocated == 0
    assert item.market_fallback_qty == 250
    assert item.recommended_buy_qty == 250 and item.recommended_action == "buy"
    # Legacy: no ladder anywhere buys the whole shortfall.
    snap = _snapshot(ref)
    snap.slots_available[config.ACTIVITY_MANUFACTURING] = pool
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[thruster]
    assert item.capacity_limited and item.jobs_allocated == 0
    assert item.market_fallback_qty is None
    assert item.recommended_buy_qty == units


def test_steady_state_never_splits(conn, ref):
    thruster, units, chain, _portion = _thruster(conn, ref)
    plan = engine.plan_steady_state(conn, ref, _snapshot(ref))
    item = plan.items[thruster]
    assert (item.market_buy_qty, item.market_fallback_qty) == (0, None)


def test_saturating_reaction_buys_only_what_whole_jobs_leave(conn, ref):
    """Crystallite Alloy (a saturating composite reaction) runs the full
    window per job. A 200-unit cheap rung must not be bought on top of jobs
    that already overshoot the need (verifier counterexample 2026-09-06);
    a large cheap rung is bought only for what whole jobs leave uncovered,
    so built + bought never exceeds what the jobs alone made before."""
    add_pipeline(conn, ref, "Hulk", 8)
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 0, "
        "structure_freight_in_isk_per_m3 = 0, alchemy_enabled = 0"
    )
    conn.commit()
    alloy = ref.type_id("Crystallite Alloy")
    base = engine.plan_index_run(conn, ref, _snapshot(ref), persist=False)
    item = base.items[alloy]
    assert item.recommended_action == "build" and item.jobs_allocated > 0
    built0, deficit = item.recommended_build_qty, item.deficit_qty
    units, chain = item.total_runs_needed * item.portion_size, item.unit_chain_cost
    assert built0 >= units >= deficit
    # A sliver the jobs' overshoot already makes: nothing is bought.
    ladders = {HUB: {alloy: [(chain * 0.9, 200)]}}
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    item = plan.items[alloy]
    assert item.market_buy_qty == 0 and item.recommended_buy_qty == 0
    assert item.recommended_build_qty == built0 and item.recommended_action == "build"
    # Half the quantity cheap: bought only up to what whole jobs leave.
    ladders = {HUB: {alloy: [(chain * 0.9, units // 2)]}}
    plan = engine.plan_index_run(conn, ref, _snapshot(ref, ladders), persist=False)
    item = plan.items[alloy]
    assert item.market_buy_qty > 0
    covered = item.recommended_build_qty + item.recommended_buy_qty
    assert deficit <= covered <= built0
    assert item.recommended_build_qty + item.market_buy_qty >= units


def test_plan_tab_unmet_matches_the_chain_tab():
    """The Plan tab's Unmet list and the Chain tab's '+unmet' badge read
    the same rule: capacity-limited and jobs + purchase + alchemy < deficit
    (v1.26: a partly-bought capacity loser can still be short)."""
    from magoo.web import _unmet_qty, _unmet_row

    row = {"capacity_limited": 1, "deficit_qty": 562, "recommended_build_qty": 0,
           "recommended_buy_qty": 531, "alchemy_output_qty": 0}
    assert _unmet_row(row) and _unmet_qty(row) == 31
    assert not _unmet_row({**row, "recommended_buy_qty": 562})
    assert not _unmet_row({**row, "capacity_limited": 0})
    assert _unmet_row({**row, "recommended_buy_qty": 0, "recommended_build_qty": 100})
    assert not _unmet_row({**row, "recommended_buy_qty": 0, "alchemy_output_qty": 562})


# --- the pricing basis --------------------------------------------------------


def test_min_sell_hub_stands_in_as_one_rung_beside_a_structure_ladder(conn, ref):
    thruster, units, chain, _portion = _thruster(conn, ref)
    conn.execute("UPDATE settings SET hub_price_basis = 'min_sell'")
    conn.commit()
    half = units // 2
    # Structure cheaper for half the quantity; the hub quote (fair value,
    # above chain) prices the rest for any quantity.
    ladders = {STRUCT: {thruster: [(chain * 0.9, half)]}}
    snap = _snapshot(ref, ladders)
    snap.hub_prices = {thruster: snap.prices.get(thruster)}
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[thruster]
    # Phase 5: the structure's cheap units are bought, the rest built (the
    # hub quote is dearer than building, an unbounded fallback).
    assert item.market_buy_qty == half
    assert item.market_fallback_qty == units - half
    assert item.recommended_action == "both"
    assert item.structure_buy_qty == half and item.hub_buy_qty == 0
    assert item.structure_fill_orders == 1


def test_min_sell_on_both_markets_prices_any_quantity_at_the_quote(conn, ref):
    thruster, units, chain, _portion = _thruster(conn, ref)
    conn.execute(
        "UPDATE settings SET hub_price_basis = 'min_sell', "
        "structure_price_basis = 'max_buy'"
    )
    conn.commit()
    snap = _snapshot(ref, overrides={thruster: chain * 0.9})
    snap.hub_prices = {thruster: chain * 0.9}
    snap.structure_prices = {thruster: chain * 0.95}
    plan = engine.plan_index_run(conn, ref, snap, persist=False)
    item = plan.items[thruster]
    assert item.recommended_action == "buy" and item.recommended_buy_qty == units
    assert item.buy_venue == HUB and item.hub_buy_qty == units
    assert item.hub_fill_orders is None  # a quote, not orders walked
    assert item.unfilled_qty == 0 and item.price_snapshot == pytest.approx(chain * 0.9)


def test_structure_refresh_caches_the_best_buy_order(conn, monkeypatch):
    from magoo import esi
    from test_buy_venue import _orders

    monkeypatch.setattr(esi, "fetch_structure_orders", lambda c, ch, sid: _orders())
    market.refresh_structure_prices(conn, 999, [34, 35, 36], character_id=1)
    assert market.cached_structure_quotes(conn, 999, [34, 35, 36], "max_buy") == {34: 1.0}
    assert market.cached_structure_quotes(conn, 999, [34, 35, 36], "min_sell") == {
        34: 90.0, 35: 5.0,
    }
    assert market.cached_structure_quotes(conn, 999, [34], "ladder") == {34: 90.0}


def test_buy_quotes_use_the_structure_buy_order_under_max_buy(conn, ref, monkeypatch):
    from magoo import esi
    from test_buy_venue import _orders

    settings = store.get_settings(conn)
    sid = settings.structure_market()
    monkeypatch.setattr(esi, "fetch_structure_orders", lambda c, ch, sid: _orders())
    market.refresh_structure_prices(conn, sid, [34], character_id=1)
    conn.execute(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at, hub) "
        "VALUES (34, ?, 'sell', 50.0, datetime('now'), 1)",
        (settings.price_region_id,),
    )
    conn.execute("UPDATE settings SET structure_price_basis = 'max_buy'")
    conn.commit()
    quote = market.buy_quotes(conn, ref, store.get_settings(conn), [34])[34]
    assert (quote.price, quote.venue, quote.units_cheaper) == (1.0, STRUCT, None)
    conn.execute("UPDATE settings SET structure_price_basis = 'ladder'")
    conn.commit()
    quote = market.buy_quotes(conn, ref, store.get_settings(conn), [34])[34]
    assert (quote.price, quote.venue) == (50.0, HUB)  # 90 sell loses to 50


# --- settings + persistence + pages -----------------------------------------


def test_settings_page_offers_the_two_bases_and_derives_the_side(seeded_client):
    page = seeded_client.get("/settings").get_data(as_text=True)
    assert 'name="hub_price_basis"' in page and 'name="structure_price_basis"' in page
    assert 'name="source"' not in page
    r = seeded_client.post(
        "/settings",
        data=_settings_form(hub_price_basis="max_buy", structure_price_basis="min_sell"),
    )
    assert r.status_code == 302
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    s = store.get_settings(c)
    assert (s.hub_price_basis, s.structure_price_basis) == ("max_buy", "min_sell")
    assert s.price_source == "buy"
    seeded_client.post("/settings", data=_settings_form(hub_price_basis="bogus"))
    s = store.get_settings(c)
    assert (s.hub_price_basis, s.price_source) == ("ladder", "sell")
    c.close()


def test_split_persists_and_the_run_pages_badge_it(seeded_client, ref):
    """POST /run with a cheap, shallow Jita ladder for a built intermediate:
    the item is part-bought, the columns land, the job table and the
    Chain tab badge the split."""
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 0, "
        "structure_freight_in_isk_per_m3 = 0"
    )
    c.commit()
    thruster = ref.type_id("Ion Thruster")
    assert seeded_client.post("/run").status_code == 302
    run_id = c.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]
    row = c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, thruster),
    ).fetchone()
    if row is None or row["recommended_action"] != "build":
        pytest.skip("seeded prices do not build Ion Thruster")
    units = row["total_runs_needed"] * row["portion_size"]
    chain = row["unit_chain_cost"]
    settings = store.get_settings(c)
    c.execute(
        "INSERT INTO hub_sell_order (region_id, type_id, price, volume_remain) "
        "VALUES (?, ?, ?, ?)",
        (settings.price_region_id, thruster, chain * 0.9, units // 2),
    )
    c.commit()
    assert seeded_client.post("/run").status_code == 302
    run_id = c.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]
    run = c.execute("SELECT * FROM index_run WHERE index_run_id = ?", (run_id,)).fetchone()
    assert (run["hub_price_basis"], run["structure_price_basis"]) == ("ladder", "ladder")
    row = c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, thruster),
    ).fetchone()
    assert row["market_buy_qty"] == units // 2
    assert row["market_fallback_qty"] == 0
    assert row["recommended_action"] == "both"
    assert row["recommended_buy_qty"] == units // 2
    page = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "cheaper on the market than building" in page
    chain_page = seeded_client.get(f"/runs/{run_id}?view=chain").get_data(as_text=True)
    assert "fill-aware build-vs-buy" in chain_page
    c.close()
