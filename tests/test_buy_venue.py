"""v1.10 two-venue buying: every bought input is priced at the cheaper
LANDED of the Jita hub quote and the structure market's (C-J6MT) best sell
order — price plus that venue's flat freight-in on packaged volume — with
a per-item venue stamp and a depth figure (units of the structure's sell
ladder that still beat the Jita landed price) the run page turns into a
"shallow" flag. Decisions 2026-08-22: best price + flag, never a fill
price; tie goes to the hub; finals are never compared; no order splitting.

Same fixture style as the other suites: live SDE read-only, temp state
DB, synthetic prices; the web routes have no test client, so the
templates render through the app's Jinja environment with synthetic
context."""

import sqlite3

import pytest

from magoo import config, costing, engine, market, store
from magoo.engine import Snapshot

from conftest import FairValuePrices, template_app

HUB = store.BUY_VENUE_HUB
STRUCT = store.BUY_VENUE_STRUCTURE


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    # Astrahus planning needs long jobs (see test_structures).
    c.execute("UPDATE settings SET max_run_duration_hours = 2000")
    c.commit()
    yield c
    c.close()


def add_pipeline(conn, ref, name, qty):
    conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run) VALUES (?, ?, ?)",
        (name, ref.type_id(name), qty),
    )
    conn.commit()


def settings_with(**over):
    return store.Settings(0.05, 24.0, 8, 1, 10000002, "sell", **over)


# --- costing.choose_buy_venue (pure) ---------------------------------------


def test_hub_wins_when_structure_landed_is_dearer():
    # hub 100 + 1/m³ × 10 m³ = 110; structure 99 + 5/m³ × 10 m³ = 149
    q = costing.choose_buy_venue(100.0, [(99.0, 10)], 10.0, 1.0, 5.0)
    assert q == costing.BuyQuote(100.0, HUB, None)


def test_structure_wins_and_counts_units_beating_hub_landed():
    # hub landed 100 + 10 × 1 = 110; ladder landed 97 / 107 / 122
    q = costing.choose_buy_venue(
        100.0, [(95.0, 50), (105.0, 30), (120.0, 10)], 1.0, 10.0, 2.0
    )
    assert q.venue == STRUCT
    assert q.price == 95.0  # the BEST order's raw price, not a fill price
    assert q.units_cheaper == 80  # 50 + 30; the 120 order lands above 110


def test_structure_units_cheaper_is_exact_at_the_boundary():
    # hub landed 110; an order landing at exactly 110 still counts ("at or
    # below"), the next one above does not.
    q = costing.choose_buy_venue(
        100.0, [(90.0, 5), (108.0, 7), (108.01, 9)], 1.0, 10.0, 2.0
    )
    assert q.venue == STRUCT and q.units_cheaper == 12


def test_structure_only_quote_counts_the_whole_ladder():
    q = costing.choose_buy_venue(None, [(50.0, 5), (60.0, 7)], 1.0, 0.0, 0.0)
    assert q == costing.BuyQuote(50.0, STRUCT, 12)


def test_hub_only_and_no_quote_at_all():
    assert costing.choose_buy_venue(100.0, [], 1.0, 0.0, 0.0) == costing.BuyQuote(
        100.0, HUB, None
    )
    assert costing.choose_buy_venue(100.0, None, 1.0, 0.0, 0.0).venue == HUB
    assert costing.choose_buy_venue(None, [], 1.0, 0.0, 0.0) == costing.BuyQuote(
        None, None, None
    )


def test_tie_goes_to_the_hub():
    q = costing.choose_buy_venue(100.0, [(100.0, 5)], 1.0, 0.0, 0.0)
    assert q.venue == HUB and q.price == 100.0


def test_freight_rates_swing_the_choice():
    # Same raw prices: hub 100 vs structure 101. Free hauling: hub wins.
    assert costing.choose_buy_venue(100.0, [(101.0, 5)], 1.0, 0.0, 0.0).venue == HUB
    # 5 ISK/m³ from Jita on a 1 m³ unit makes the hub land at 105: C-J6 wins.
    q = costing.choose_buy_venue(100.0, [(101.0, 5)], 1.0, 5.0, 0.0)
    assert q.venue == STRUCT and q.price == 101.0 and q.units_cheaper == 5


def test_unsorted_ladder_is_sorted_before_use():
    q = costing.choose_buy_venue(200.0, [(105.0, 30), (95.0, 50)], 1.0, 0.0, 0.0)
    assert q.price == 95.0 and q.units_cheaper == 80


# --- market: ladders, cache, buy_quotes ------------------------------------


def _orders():
    return [
        {"type_id": 34, "price": 100.0, "is_buy_order": False, "volume_remain": 10},
        {"type_id": 34, "price": 90.0, "is_buy_order": False, "volume_remain": 25},
        {"type_id": 34, "price": 1.0, "is_buy_order": True, "volume_remain": 99},
        {"type_id": 34, "price": 80.0, "is_buy_order": False, "volume_remain": 0},
        {"type_id": 35, "price": 5.0, "is_buy_order": False, "volume_remain": 3},
    ]


def test_sell_ladders_skip_buys_empties_and_unwanted():
    ladders = market._sell_ladders(_orders(), {34, 36})
    assert ladders == {34: [(90.0, 25), (100.0, 10)]}  # ascending, buy + 0-vol dropped


def test_refresh_structure_prices_persists_and_replaces_ladders(conn, monkeypatch):
    from magoo import esi

    monkeypatch.setattr(esi, "fetch_structure_orders", lambda c, ch, sid: _orders())
    n = market.refresh_structure_prices(conn, 999, [34, 35, 36], character_id=1)
    assert n == 2
    # Best price still cached for the sell-quote path (v1.6 contract).
    assert market.cached_prices(conn, 999, [34, 35, 36], market.STRUCTURE_SOURCE) == {
        34: 90.0, 35: 5.0,
    }
    assert market.cached_structure_ladders(conn, 999, [34, 35, 36]) == {
        34: [(90.0, 25), (100.0, 10)],
        35: [(5.0, 3)],
    }
    # Another structure's ladder is untouched by this one's refresh.
    conn.execute(
        "INSERT INTO structure_sell_order VALUES (1000, 34, 1.0, 1)"
    )
    conn.commit()
    # Wholesale replacement: a thinner book leaves no stale rungs.
    monkeypatch.setattr(
        esi, "fetch_structure_orders",
        lambda c, ch, sid: [
            {"type_id": 35, "price": 7.0, "is_buy_order": False, "volume_remain": 2}
        ],
    )
    market.refresh_structure_prices(conn, 999, [34, 35, 36], character_id=1)
    assert market.cached_structure_ladders(conn, 999, [34, 35, 36]) == {35: [(7.0, 2)]}
    assert market.cached_structure_ladders(conn, 1000, [34]) == {34: [(1.0, 1)]}
    assert market.cached_structure_ladders(conn, 999, []) == {}


def test_orders_without_volume_field_give_no_quote(conn, monkeypatch):
    """One filter over the book: an order with nothing (or nothing known)
    left to sell is neither a ladder rung nor a best price, so the
    sell-quote cache and the buy-venue ladder can never disagree."""
    from magoo import esi

    monkeypatch.setattr(
        esi, "fetch_structure_orders",
        lambda c, ch, sid: [{"type_id": 34, "price": 90.0, "is_buy_order": False}],
    )
    assert market.refresh_structure_prices(conn, 999, [34], character_id=1) == 0
    assert market.cached_structure_ladders(conn, 999, [34]) == {}
    assert market.cached_prices(conn, 999, [34], market.STRUCTURE_SOURCE) == {}
    assert market._min_sell_by_type(_orders(), {34, 35}) == {34: 90.0, 35: 5.0}


def _seed_hub(conn, type_id, price, hub=1):
    conn.execute(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at, hub) "
        "VALUES (?, 10000002, 'sell', ?, '2026-08-22T00:00:00+00:00', ?)",
        (type_id, price, hub),
    )
    conn.commit()


def _seed_ladder(conn, structure_id, type_id, ladder):
    conn.executemany(
        "INSERT INTO structure_sell_order VALUES (?, ?, ?, ?)",
        [(structure_id, type_id, p, v) for p, v in ladder],
    )
    conn.commit()


def test_buy_quotes_picks_cheaper_landed_per_type(conn, ref):
    trit = ref.type_id("Tritanium")  # 0.01 m³
    pyer = ref.type_id("Pyerite")
    mexa = ref.type_id("Mexallon")
    structure_id = config.CJ6_KEEPSTAR_STRUCTURE_ID  # the default market
    _seed_hub(conn, trit, 5.0)
    _seed_hub(conn, pyer, 10.0)
    # mexa: no hub order at all, structure only
    _seed_ladder(conn, structure_id, trit, [(4.0, 1000), (4.9, 500), (6.0, 50)])
    _seed_ladder(conn, structure_id, pyer, [(11.0, 1000)])
    _seed_ladder(conn, structure_id, mexa, [(100.0, 10)])
    settings = store.get_settings(conn)
    quotes = market.buy_quotes(conn, ref, settings, [trit, pyer, mexa, 999999])
    assert quotes[trit] == costing.BuyQuote(4.0, STRUCT, 1500)
    assert quotes[pyer] == costing.BuyQuote(10.0, HUB, None)
    assert quotes[mexa] == costing.BuyQuote(100.0, STRUCT, 10)
    assert 999999 not in quotes  # no quote anywhere: absent, as before


def test_buy_quotes_honours_switch_exclusions_and_market_setting(conn, ref):
    trit = ref.type_id("Tritanium")
    _seed_hub(conn, trit, 5.0)
    _seed_ladder(conn, config.CJ6_KEEPSTAR_STRUCTURE_ID, trit, [(4.0, 1000)])
    settings = store.get_settings(conn)
    # A final (excluded) keeps the hub quote even when C-J6 is cheaper.
    assert market.buy_quotes(conn, ref, settings, [trit], exclude={trit})[trit] == (
        costing.BuyQuote(5.0, HUB, None)
    )
    # The comparison switch off: hub only.
    conn.execute("UPDATE settings SET structure_buy_enabled = 0")
    conn.commit()
    assert market.buy_quotes(conn, ref, store.get_settings(conn), [trit])[trit].venue == HUB
    # A custom structure with no cached ladder: hub only.
    conn.execute(
        "UPDATE settings SET structure_buy_enabled = 1, "
        "capital_market_mode = 'custom', capital_structure_id = 424242"
    )
    conn.commit()
    assert market.buy_quotes(conn, ref, store.get_settings(conn), [trit])[trit].venue == HUB


def test_buy_quotes_region_wide_follows_the_venue_used(conn, ref):
    """A v1.9 region-wide hub quote keeps its badge while the hub wins and
    loses it once the structure undercuts it (the price shown is no longer
    the region-wide order)."""
    trit = ref.type_id("Tritanium")
    pyer = ref.type_id("Pyerite")
    _seed_hub(conn, trit, 5.0, hub=0)
    _seed_hub(conn, pyer, 5.0, hub=0)
    _seed_ladder(conn, config.CJ6_KEEPSTAR_STRUCTURE_ID, trit, [(4.0, 10)])
    _seed_ladder(conn, config.CJ6_KEEPSTAR_STRUCTURE_ID, pyer, [(6.0, 10)])
    quotes = market.buy_quotes(conn, ref, store.get_settings(conn), [trit, pyer])
    assert quotes[trit].venue == STRUCT and quotes[trit].region_wide is False
    assert quotes[pyer].venue == HUB and quotes[pyer].region_wide is True
    prices, venues, units, region_wide = market.quote_maps(quotes)
    assert prices == {trit: 4.0, pyer: 5.0}
    assert venues == {trit: STRUCT, pyer: HUB}
    assert units == {trit: 10}
    assert region_wide == {pyer}


def test_buy_quotes_excludes_active_finals_by_default(conn, ref):
    add_pipeline(conn, ref, "Astrahus", 1)
    astrahus = ref.type_id("Astrahus")
    _seed_hub(conn, astrahus, 1_000_000.0)
    _seed_ladder(conn, config.CJ6_KEEPSTAR_STRUCTURE_ID, astrahus, [(1.0, 5)])
    settings = store.get_settings(conn)
    assert market.buy_quotes(conn, ref, settings, [astrahus])[astrahus] == (
        costing.BuyQuote(1_000_000.0, HUB, None)
    )
    # An explicit (empty) exclusion re-enables the comparison for it.
    assert market.buy_quotes(conn, ref, settings, [astrahus], exclude=()).get(
        astrahus
    ).venue == STRUCT


def test_structure_market_label_and_custom_freight_line_name(conn, ref):
    assert settings_with().structure_market_label() == "C-J6"
    assert settings_with(
        capital_market_mode="custom", capital_structure_id=12345
    ).structure_market_label() == "structure 12345"
    # Custom without an id resolves to the preset, label included.
    assert settings_with(capital_market_mode="custom").structure_market_label() == "C-J6"
    add_pipeline(conn, ref, "Astrahus", 1)
    conn.execute(
        "UPDATE settings SET capital_market_mode = 'custom', "
        "capital_structure_id = 12345, structure_freight_in_isk_per_m3 = 10"
    )
    conn.commit()
    pipeline = conn.execute("SELECT * FROM pipeline").fetchone()
    prices = FairValuePrices(ref)
    cost = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), pipeline, prices, prices,
        venues={ref.type_id("Tritanium"): STRUCT},
    )
    assert "Inbound freight (structure 12345)" in {
        l.name for l in cost.lines if l.kind == "freight_in"
    }


def test_structure_freight_rate_seeds_from_jita_rate_on_upgrade(tmp_path):
    """An existing database gets the new structure freight-in column seeded
    as a copy of its Jita rate (one-shot, at the moment the column is
    added); a fresh database starts both at 0; a later deliberate 0 stays."""
    c = sqlite3.connect(tmp_path / "upgrade.sqlite")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    s = store.get_settings(c)
    assert (s.freight_in_isk_per_m3, s.structure_freight_in_isk_per_m3) == (0.0, 0.0)
    # Rewind to a pre-v1.10 shape with a configured Jita rate.
    c.execute("ALTER TABLE settings DROP COLUMN structure_freight_in_isk_per_m3")
    c.execute("UPDATE settings SET freight_in_isk_per_m3 = 500 WHERE id = 1")
    c.commit()
    store.ensure_schema(c)
    assert store.get_settings(c).structure_freight_in_isk_per_m3 == 500
    # Idempotent: the user's later choice is never overwritten.
    c.execute("UPDATE settings SET structure_freight_in_isk_per_m3 = 0 WHERE id = 1")
    c.commit()
    store.ensure_schema(c)
    assert store.get_settings(c).structure_freight_in_isk_per_m3 == 0
    c.close()


def test_structure_cache_state_reads_structure_rows(conn, monkeypatch):
    from magoo import esi

    assert market.structure_cache_state(conn, 999) == (0, None)
    monkeypatch.setattr(esi, "fetch_structure_orders", lambda c, ch, sid: _orders())
    market.refresh_structure_prices(conn, 999, [34, 35], character_id=1)
    n, latest = market.structure_cache_state(conn, 999)
    assert n == 2 and latest is not None


# --- settings ------------------------------------------------------------------


def test_settings_defaults_and_roundtrip_v110(conn):
    s = store.get_settings(conn)
    assert s.structure_freight_in_isk_per_m3 == 0.0
    assert s.structure_buy_enabled is True
    assert s.structure_market() == s.capital_structure() == config.CJ6_KEEPSTAR_STRUCTURE_ID
    conn.execute(
        "UPDATE settings SET structure_freight_in_isk_per_m3 = 120, "
        "structure_buy_enabled = 0, freight_in_isk_per_m3 = 750 WHERE id = 1"
    )
    conn.commit()
    s = store.get_settings(conn)
    assert s.structure_freight_in_isk_per_m3 == 120
    assert s.structure_buy_enabled is False
    assert s.freight_in_rate(HUB) == 750
    assert s.freight_in_rate(STRUCT) == 120
    assert s.freight_in_rate(None) == 750  # unpriced / legacy rows: Jita leg


# --- engine -------------------------------------------------------------------


def test_chain_coster_lands_each_buy_at_its_venues_rate(conn, ref):
    """The MILP's chain cost prices a bought unit at price + ITS venue's
    flat freight-in on packaged volume."""
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 1000, "
        "structure_freight_in_isk_per_m3 = 10"
    )
    conn.commit()
    trit = ref.type_id("Tritanium")
    vol = ref.type_info(trit).freight_volume
    prices = FairValuePrices(ref)
    hub_snap = Snapshot(prices=prices, adjusted_prices=prices)
    struct_snap = Snapshot(
        prices=prices, adjusted_prices=prices, buy_venue={trit: STRUCT}
    )
    hub_chain, hub_buy = engine._chain_coster(conn, ref, hub_snap)
    struct_chain, struct_buy = engine._chain_coster(conn, ref, struct_snap)
    hub_cost, _ = hub_chain(trit)
    struct_cost, _ = struct_chain(trit)
    # The exposed buy leg is the same landed figure a raw leaf's chain is.
    assert hub_buy(trit) == pytest.approx(hub_cost)
    assert struct_buy(trit) == pytest.approx(struct_cost)
    assert hub_cost == pytest.approx(prices.get(trit) + 1000 * vol)
    assert struct_cost == pytest.approx(prices.get(trit) + 10 * vol)
    assert struct_cost < hub_cost


def test_build_savings_is_against_the_landed_buy_price(conn, ref):
    """Savings = landed buy price − chain cost: the item's OWN inbound
    courier cost counts on the buy side (2026-08-23 — it was missing, which
    biased bulky intermediates toward 'buy'). With a courier rate the
    savings of a buildable intermediate rises by rate × its packaged m³
    relative to the zero-rate figure, minus the extra freight its own
    bought inputs now carry; the persisted unit_chain_cost is the chain
    cost itself."""
    add_pipeline(conn, ref, "Astrahus", 1)
    hangar = ref.type_id("Structure Hangar Array")
    prices = FairValuePrices(ref)

    def savings_and_chain(rate):
        conn.execute("UPDATE settings SET freight_in_isk_per_m3 = ?", (rate,))
        conn.commit()
        snap = Snapshot(
            slots_available={config.ACTIVITY_MANUFACTURING: 500,
                             config.ACTIVITY_REACTION: 500},
            prices=prices, adjusted_prices=prices,
        )
        plan = engine.plan_index_run(conn, ref, snap, persist=False)
        item = plan.items[hangar]
        chain, buy_cost = engine._chain_coster(conn, ref, snap)
        return item.build_savings_per_unit, item.unit_chain_cost, chain(hangar)[0], buy_cost(hangar)

    sav0, chain0, chain0_direct, landed0 = savings_and_chain(0.0)
    sav1, chain1, chain1_direct, landed1 = savings_and_chain(1000.0)
    assert chain0 == pytest.approx(chain0_direct)
    assert chain1 == pytest.approx(chain1_direct)
    assert sav0 == pytest.approx(landed0 - chain0)
    assert sav1 == pytest.approx(landed1 - chain1)
    own_freight = 1000.0 * ref.type_info(hangar).freight_volume
    assert landed1 - landed0 == pytest.approx(own_freight)
    # The buy leg gains the item's own freight; the chain gains its inputs'.
    assert sav1 - sav0 == pytest.approx(own_freight - (chain1 - chain0))
    assert own_freight > 0 and chain1 > chain0


def test_plan_persists_unit_chain_cost(conn, ref):
    add_pipeline(conn, ref, "Astrahus", 1)
    store.save_esi_snapshot(conn, {}, {}, {1: 0, 11: 0}, 0.0, 0.0, job_ends={})
    prices = FairValuePrices(ref)
    snap = engine.snapshot_from_state(conn, prices=prices, adjusted=prices)
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    hangar = ref.type_id("Structure Hangar Array")
    row = conn.execute(
        "SELECT unit_chain_cost, build_savings_per_unit FROM index_run_item "
        "WHERE index_run_id = ? AND type_id = ?", (plan.index_run_id, hangar),
    ).fetchone()
    assert row["unit_chain_cost"] == pytest.approx(plan.items[hangar].unit_chain_cost)
    assert row["unit_chain_cost"] > 0
    assert row["build_savings_per_unit"] == pytest.approx(plan.items[hangar].build_savings_per_unit)


def test_plan_persists_buy_venue_and_depth(conn, ref):
    add_pipeline(conn, ref, "Astrahus", 1)
    store.save_esi_snapshot(conn, {}, {}, {1: 0, 11: 0}, 0.0, 0.0, job_ends={})
    trit = ref.type_id("Tritanium")
    hangar = ref.type_id("Structure Hangar Array")
    prices = FairValuePrices(ref, overrides={ref.type_id("Pyerite"): None})
    pyer = ref.type_id("Pyerite")
    snap = engine.snapshot_from_state(
        conn, prices=prices, adjusted=prices,
        buy_venue={trit: STRUCT, pyer: STRUCT},
        structure_units_cheaper={trit: 123},
    )
    assert snap is not None and snap.venue(trit) == STRUCT and snap.venue(hangar) == HUB
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    assert plan.items[trit].buy_venue == STRUCT
    assert plan.items[trit].structure_units_cheaper == 123
    assert plan.items[hangar].buy_venue == HUB
    assert plan.items[hangar].structure_units_cheaper is None
    # Unpriced stays unpriced regardless of the venue map.
    assert plan.items[pyer].price_snapshot is None
    assert plan.items[pyer].buy_venue is None
    rows = {
        r["type_id"]: (r["buy_venue"], r["structure_units_cheaper"])
        for r in conn.execute(
            "SELECT type_id, buy_venue, structure_units_cheaper "
            "FROM index_run_item WHERE index_run_id = ?", (plan.index_run_id,)
        )
    }
    assert rows[trit] == (STRUCT, 123)
    assert rows[hangar] == (HUB, None)
    assert rows[pyer] == (None, None)


# --- costing views ----------------------------------------------------------


def _freight_lines(cost):
    return {l.name: l for l in cost.lines if l.kind == "freight_in"}


def test_hull_cost_splits_inbound_freight_by_persisted_venue(conn, ref):
    add_pipeline(conn, ref, "Astrahus", 1)
    store.save_esi_snapshot(conn, {}, {}, {1: 0, 11: 0}, 0.0, 0.0, job_ends={})
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 100, "
        "structure_freight_in_isk_per_m3 = 10"
    )
    conn.commit()
    trit = ref.type_id("Tritanium")
    prices = FairValuePrices(ref)
    snap = engine.snapshot_from_state(
        conn, prices=prices, adjusted=prices, buy_venue={trit: STRUCT},
        structure_units_cheaper={trit: 1},
    )
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = datetime('now') "
        "WHERE index_run_id = ?", (plan.index_run_id,),
    )
    conn.commit()
    pipeline = conn.execute("SELECT * FROM pipeline").fetchone()
    settings = store.get_settings(conn)
    cost = costing.hull_cost(conn, ref, settings, plan.index_run_id, pipeline["pipeline_id"])
    assert cost is not None
    freight = _freight_lines(cost)
    assert set(freight) == {"Inbound freight (Jita)", "Inbound freight (C-J6)"}
    trit_line = next(l for l in cost.lines if l.type_id == trit)
    assert trit_line.venue == STRUCT
    assert freight["Inbound freight (C-J6)"].unit_cost == 10
    assert freight["Inbound freight (C-J6)"].qty_per_hull == pytest.approx(
        trit_line.qty_per_hull * ref.type_info(trit).freight_volume
    )
    assert freight["Inbound freight (Jita)"].unit_cost == 100
    total_m3 = sum(
        l.qty_per_hull * ref.type_info(l.type_id).freight_volume
        for l in cost.lines if l.kind == "material"
    )
    assert freight["Inbound freight (Jita)"].qty_per_hull + freight[
        "Inbound freight (C-J6)"
    ].qty_per_hull == pytest.approx(total_m3)
    assert cost.structure_priced == 1
    # Pre-v1.10 rows (NULL venue) were hub buys: one Jita line, same m³.
    conn.execute(
        "UPDATE index_run_item SET buy_venue = NULL WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    legacy = _freight_lines(
        costing.hull_cost(conn, ref, settings, plan.index_run_id, pipeline["pipeline_id"])
    )
    assert set(legacy) == {"Inbound freight (Jita)"}
    assert legacy["Inbound freight (Jita)"].qty_per_hull == pytest.approx(total_m3)


def test_current_hull_cost_splits_freight_by_venue_map(conn, ref):
    add_pipeline(conn, ref, "Astrahus", 1)
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 100, "
        "structure_freight_in_isk_per_m3 = 10"
    )
    conn.commit()
    pipeline = conn.execute("SELECT * FROM pipeline").fetchone()
    prices = FairValuePrices(ref)
    trit = ref.type_id("Tritanium")
    settings = store.get_settings(conn)
    cost = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices, venues={trit: STRUCT}
    )
    freight = _freight_lines(cost)
    assert set(freight) == {"Inbound freight (Jita)", "Inbound freight (C-J6)"}
    assert cost.structure_priced == 1
    assert next(l for l in cost.lines if l.type_id == trit).venue == STRUCT
    # Structure leg at a zero rate emits no line (nothing to charge).
    conn.execute("UPDATE settings SET structure_freight_in_isk_per_m3 = 0")
    conn.commit()
    cost0 = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), pipeline, prices, prices,
        venues={trit: STRUCT},
    )
    assert set(_freight_lines(cost0)) == {"Inbound freight (Jita)"}
    # No venue map at all: exactly the pre-v1.10 single Jita line.
    plain = costing.current_hull_cost(conn, ref, settings, pipeline, prices, prices)
    assert set(_freight_lines(plain)) == {"Inbound freight (Jita)"}
    assert plain.structure_priced == 0


# --- templates ----------------------------------------------------------------


_app = template_app


def _buy_row(name, type_id, qty, price=1000.0, venue=HUB, units_cheaper=None):
    return {
        "name": name, "type_id": type_id, "group_id": 18, "category": "Mineral",
        "recommended_buy_qty": qty, "price_snapshot": price, "capacity_limited": 0,
        "runs_allocated": 0, "jobs_allocated": 0, "max_runs_per_job": 1,
        "recommended_build_qty": 0, "time_per_run": 0.0, "low_stock": 0,
        "savings_unpriced_inputs": 0, "deficit_qty": qty,
        "buy_venue": venue, "structure_units_cheaper": units_cheaper,
    }


def test_run_detail_renders_venue_column_shallow_flag_and_two_multibuys(ref):
    from flask import render_template

    trit = _buy_row("Tritanium", ref.type_id("Tritanium"), 5000,
                    venue=STRUCT, units_cheaper=1500)
    pyer = _buy_row("Pyerite", ref.type_id("Pyerite"), 12)
    run = {"run_number": 7, "status": "planned", "planned_start": "2026-08-22",
           "index_run_id": 1, "wallet_character_isk": 1e9,
           "wallet_corporation_isk": 2e9, "completed_at": None}
    ctx = dict(
        run=run, items=[trit, pyer], final_net_margin={}, buys=[trit, pyer],
        builds=[], reactions=[], builds_grouped=[], reactions_grouped=[],
        struct_builds=[], struct_buys=[], struct_slots=0, chain_struct=[],
        alchemy=[], alchemy_yield=0.55, chain_rows=[], chain_raws=[],
        chain_mfg=[], chain_reactions=[],
        chain_counts={"covered": 0, "buy": 0, "build": 0, "react": 0, "alchemy": 0},
        unmet=[], low_stock=[], buy_total=5012 * 1000.0, buys_unpriced=0,
        multibuy_hub="Pyerite 12", multibuy_structure="Tritanium 5000",
        structure_buys={trit["type_id"]},
        shallow={trit["type_id"]},
        settings=settings_with(manufacturing_slots=50, reaction_slots=50),
        mfg_slots_used=0, reaction_slots_used=0, alchemy_slots_used=0,
        region_wide=set(),
    )
    app = _app()
    with app.test_request_context("/runs/1"):
        html = render_template("run_detail.html", **ctx)
    assert "<th class=\"cat\">Venue</th>" in html
    assert ">C-J6</span>" in html and ">Jita</span>" in html
    assert "1 via C-J6" in html and "1 shallow" in html
    assert ">shallow</span>" in html
    assert "only 1,500 of 5,000 units" in html
    assert "one block per market" in html
    assert "<p class=\"muted\">Jita</p>" in html
    assert "<p class=\"muted\">C-J6 structure market</p>" in html
    assert ">Tritanium 5000</textarea>" in html
    assert ">Pyerite 12</textarea>" in html


def test_run_detail_without_structure_buys_keeps_single_multibuy(ref):
    from flask import render_template

    pyer = _buy_row("Pyerite", ref.type_id("Pyerite"), 12)
    run = {"run_number": 7, "status": "planned", "planned_start": "2026-08-22",
           "index_run_id": 1, "wallet_character_isk": 1e9,
           "wallet_corporation_isk": 2e9, "completed_at": None}
    ctx = dict(
        run=run, items=[pyer], final_net_margin={}, buys=[pyer],
        builds=[], reactions=[], builds_grouped=[], reactions_grouped=[],
        struct_builds=[], struct_buys=[], struct_slots=0, chain_struct=[],
        alchemy=[], alchemy_yield=0.55, chain_rows=[], chain_raws=[],
        chain_mfg=[], chain_reactions=[],
        chain_counts={"covered": 0, "buy": 0, "build": 0, "react": 0, "alchemy": 0},
        unmet=[], low_stock=[], buy_total=12 * 1000.0, buys_unpriced=0,
        multibuy_hub="Pyerite 12", multibuy_structure="",
        structure_buys=set(), shallow=set(),
        settings=settings_with(manufacturing_slots=50, reaction_slots=50),
        mfg_slots_used=0, reaction_slots_used=0, alchemy_slots_used=0,
        region_wide=set(),
    )
    app = _app()
    with app.test_request_context("/runs/1"):
        html = render_template("run_detail.html", **ctx)
    assert "via C-J6" not in html and "shallow" not in html
    assert "one block per market" not in html
    assert html.count("<textarea") == 1
    assert "C-J6 structure market" not in html


def test_profit_template_renders_via_cj6_badges(conn, ref):
    from flask import render_template

    add_pipeline(conn, ref, "Astrahus", 1)
    pipeline = conn.execute("SELECT * FROM pipeline").fetchone()
    prices = FairValuePrices(ref)
    trit = ref.type_id("Tritanium")
    conn.execute("UPDATE settings SET structure_freight_in_isk_per_m3 = 10")
    conn.commit()
    settings = store.get_settings(conn)
    cost = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices, venues={trit: STRUCT}
    )
    card = {"pipeline": pipeline, "name": "Astrahus", "cost": cost,
            "price": cost.total * 1.3, "net": cost.total * 1.2, "capital": False,
            "margin": cost.total * 0.2}
    app = _app()
    with app.test_request_context("/profit"):
        html = render_template(
            "planning_profit.html", cards=[card], totals=costing.cycle_totals([card]),
            prices_at=None, structure_prices_at=None, broker_rate=0.01,
            sales_tax=0.03, settings=settings,
        )
    assert "1 via C-J6</span>" not in html  # the per-card count badge is gone
    assert ">C-J6</span>" in html  # breakdown line badge
    # Null Sec Market Share: per-product in the breakdown, overall in the strip
    share = cost.structure_material_share_pct
    assert share is not None and 0 < share < 100
    assert f"({share:.1f}% null-sec market)" in html
    assert "Null Sec Market Share" in html
    assert f"{costing.cycle_totals([card]).structure_share_pct:.1f}%" in html
    assert "Inbound freight (C-J6)" in html
    assert "structure market never pulled" in html
