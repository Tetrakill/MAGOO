"""Price cache and refresh selection (v1.4.2 decoupling) — no network:
the per-type fetcher is monkeypatched."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from magoo import config, market, store


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    yield c
    c.close()


def seed(conn, type_id, price, age_seconds, region=10000002, source="sell"):
    fetched = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    conn.execute(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (type_id, region, source, price, fetched.isoformat()),
    )
    conn.commit()


def test_cached_prices_ignore_age(conn):
    seed(conn, 34, 4.5, age_seconds=999999)  # ancient
    seed(conn, 35, 11.0, age_seconds=10)
    seed(conn, 36, None, age_seconds=10)  # cached "no orders"
    prices = market.cached_prices(conn, 10000002, [34, 35, 36, 37], "sell")
    assert prices == {34: 4.5, 35: 11.0}  # any age; None and missing excluded


def test_refresh_fetches_only_stale(conn, monkeypatch):
    seed(conn, 34, 4.5, age_seconds=10)  # fresh (< 300s)
    seed(conn, 35, 11.0, age_seconds=3600)  # stale
    calls = []

    def fake_fetch(client, region_id, type_id, source, low_budget):
        calls.append(type_id)
        return 42.0

    monkeypatch.setattr(market, "_best_order_price", fake_fetch)
    fetched, skipped, fresh = market.refresh_prices(
        conn, 10000002, [34, 35, 36], "sell"
    )
    assert sorted(calls) == [35, 36]  # stale + missing, never the fresh one
    assert (fetched, skipped, fresh) == (2, 0, 1)
    prices = market.cached_prices(conn, 10000002, [34, 35, 36], "sell")
    assert prices == {34: 4.5, 35: 42.0, 36: 42.0}


def test_refresh_caches_no_orders_as_null(conn, monkeypatch):
    monkeypatch.setattr(
        market, "_best_order_price", lambda *a, **k: None
    )
    fetched, skipped, fresh = market.refresh_prices(conn, 10000002, [34], "sell")
    assert (fetched, skipped, fresh) == (1, 0, 0)
    row = conn.execute(
        "SELECT price FROM market_price WHERE type_id = 34"
    ).fetchone()
    assert row is not None and row["price"] is None
    # and it now counts as fresh — no refetch inside the ESI cache window
    fetched2, _s, fresh2 = market.refresh_prices(conn, 10000002, [34], "sell")
    assert (fetched2, fresh2) == (0, 1)


def test_refresh_skips_on_throttle(conn, monkeypatch):
    def throttled(client, region_id, type_id, source, low_budget):
        raise market._Throttled

    monkeypatch.setattr(market, "_best_order_price", throttled)
    fetched, skipped, fresh = market.refresh_prices(
        conn, 10000002, [34, 35], "sell"
    )
    assert fetched == 0
    assert skipped == 2
    # nothing written — both stay stale for the next attempt
    assert market.cached_prices(conn, 10000002, [34, 35], "sell") == {}


def test_adjusted_price_cache_roundtrip(conn):
    n = market.store_adjusted_prices(conn, [34, 35, 99], {34: 6.0, 35: 12.5})
    assert n == 2
    assert market.cached_adjusted_prices(conn, [34, 35, 99]) == {34: 6.0, 35: 12.5}


def test_forge_prices_filter_to_jita_44():
    """A 1-unit backwater order must not set the Forge snapshot; other
    regions have no hub and stay region-wide (decision 2026-08-20)."""
    import threading

    import httpx

    from magoo import config

    orders = [
        {"price": 5.0, "location_id": 60000001},  # backwater scam
        {"price": 9.0, "location_id": config.JITA_44_STATION_ID},
        {"price": 8.5, "location_id": config.JITA_44_STATION_ID},
    ]

    class FakeClient:
        def get(self, url, params=None, headers=None):
            return httpx.Response(
                200,
                json=orders,
                headers={"X-Pages": "1"},
                request=httpx.Request("GET", url),
            )

    event = threading.Event()
    assert market._best_order_price(
        FakeClient(), config.THE_FORGE_REGION_ID, 34, "sell", event
    ) == 8.5
    assert market._best_order_price(
        FakeClient(), 10000043, 34, "sell", event
    ) == 5.0
    # v1.9: one pull yields both the hub quote and the region-wide best.
    assert market._order_prices(
        FakeClient(), config.THE_FORGE_REGION_ID, 34, "sell", event
    ) == (8.5, 5.0)
    assert market._order_prices(
        FakeClient(), 10000043, 34, "sell", event
    ) == (5.0, 5.0)


def test_mid_pull_404_keeps_collected_pages():
    """Page 1 answering 200 and page 2 answering 404 (the book shrank
    between pages) must keep page 1's orders — not discard the pull and
    cache a liquid type as 'no orders'. A page-1 404 still means none."""
    import threading

    import httpx

    class ShrinkingClient:
        def get(self, url, params=None, headers=None):
            request = httpx.Request("GET", url)
            if params["page"] == 1:
                return httpx.Response(
                    200,
                    json=[{"price": 7.5, "location_id": 60000001}],
                    headers={"X-Pages": "2"},
                    request=request,
                )
            return httpx.Response(404, request=request)

    class EmptyClient:
        def get(self, url, params=None, headers=None):
            return httpx.Response(404, request=httpx.Request("GET", url))

    event = threading.Event()
    assert market._order_prices(
        ShrinkingClient(), 10000043, 34, "sell", event
    ) == (7.5, 7.5)
    assert market._order_prices(
        EmptyClient(), 10000043, 34, "sell", event
    ) == (None, None)


def test_junk_x_pages_header_does_not_crash():
    """A fronting proxy's junk X-Pages must fall back to one page, not
    ValueError out of the worker (which aborted the whole refresh)."""
    import threading

    import httpx

    class JunkHeaderClient:
        def get(self, url, params=None, headers=None):
            return httpx.Response(
                200,
                json=[{"price": 4.0, "location_id": 60000001}],
                headers={"X-Pages": "junk"},
                request=httpx.Request("GET", url),
            )

    assert market._order_prices(
        JunkHeaderClient(), 10000043, 34, "sell", threading.Event()
    ) == (4.0, 4.0)


def test_refresh_skips_type_on_value_error(conn, monkeypatch):
    """A junk 200 body (json.JSONDecodeError subclasses ValueError) skips
    that type like a throttled one instead of escaping at future.result()
    and discarding every fetched price."""
    def fetch(client, region_id, type_id, source, low_budget):
        if type_id == 35:
            raise ValueError("junk body")
        return 42.0

    monkeypatch.setattr(market, "_best_order_price", fetch)
    fetched, skipped, fresh = market.refresh_prices(
        conn, 10000002, [34, 35], "sell"
    )
    assert (fetched, skipped, fresh) == (1, 1, 0)
    assert market.cached_prices(conn, 10000002, [34, 35], "sell") == {34: 42.0}


# --- v1.9 region-wide fallback for raw leaves --------------------------------


def test_raw_leaf_falls_back_to_region_wide_when_no_hub_order(conn, monkeypatch):
    """A fallback-eligible type with no hub order takes the region-wide best
    from the same pull and is cached with hub = 0; other types keep the
    hub-only path and hub = 1."""
    calls = []

    def fake_orders(client, region_id, type_id, source, low_budget):
        calls.append((region_id, type_id))
        return (None, 7.0) if type_id == 5000 else (9.0, 6.0)

    monkeypatch.setattr(market, "_order_prices", fake_orders)
    fetched, skipped, fresh = market.refresh_prices(
        conn, 10000002, [5000, 34], "sell",
        fallback_type_ids={5000}, fallback_region_id=10000002,
    )
    assert (fetched, skipped, fresh) == (2, 0, 0)
    assert market.cached_prices(conn, 10000002, [5000, 34], "sell") == {
        5000: 7.0, 34: 9.0,
    }
    assert market.region_wide_types(conn, 10000002, [5000, 34], "sell") == {5000}
    # same region: one pull per type, no second fetch
    assert sorted(calls) == [(10000002, 34), (10000002, 5000)]


def test_raw_leaf_with_hub_order_stays_hub_priced(conn, monkeypatch):
    monkeypatch.setattr(
        market, "_order_prices", lambda *a, **k: (9.0, 6.0)
    )
    market.refresh_prices(
        conn, 10000002, [5000], "sell",
        fallback_type_ids={5000}, fallback_region_id=10000002,
    )
    assert market.cached_prices(conn, 10000002, [5000], "sell") == {5000: 9.0}
    assert market.region_wide_types(conn, 10000002, [5000], "sell") == set()


def test_fallback_from_a_different_region_costs_one_extra_pull(conn, monkeypatch):
    calls = []

    def fake_orders(client, region_id, type_id, source, low_budget):
        calls.append(region_id)
        return (None, None) if region_id == 10000002 else (3.0, 2.5)

    monkeypatch.setattr(market, "_order_prices", fake_orders)
    market.refresh_prices(
        conn, 10000002, [5000], "sell",
        fallback_type_ids={5000}, fallback_region_id=10000043,
    )
    assert calls == [10000002, 10000043]
    assert market.cached_prices(conn, 10000002, [5000], "sell") == {5000: 2.5}
    assert market.region_wide_types(conn, 10000002, [5000], "sell") == {5000}


def test_non_fallback_types_never_take_region_wide(conn, monkeypatch):
    monkeypatch.setattr(
        market, "_order_prices", lambda *a, **k: (None, 7.0)
    )
    market.refresh_prices(
        conn, 10000002, [34], "sell",
        fallback_type_ids=set(), fallback_region_id=10000002,
    )
    # no hub order and not eligible: cached as NULL ("no orders")
    assert market.cached_prices(conn, 10000002, [34], "sell") == {}
    assert market.region_wide_types(conn, 10000002, [34], "sell") == set()


def test_hub_order_reappearing_clears_region_wide_flag(conn, monkeypatch):
    """A stale hub=0 row is rewritten hub=1 (badge cleared) once the hub has
    an order again — refresh always writes the flag explicitly."""
    from datetime import datetime, timedelta, timezone
    fetched = datetime.now(timezone.utc) - timedelta(seconds=3600)
    conn.execute(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at, hub) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (5000, 10000002, "sell", 7.0, fetched.isoformat(), 0),
    )
    conn.commit()
    assert market.region_wide_types(conn, 10000002, [5000], "sell") == {5000}
    monkeypatch.setattr(market, "_order_prices", lambda *a, **k: (9.0, 6.0))
    market.refresh_prices(
        conn, 10000002, [5000], "sell",
        fallback_type_ids={5000}, fallback_region_id=10000002,
    )
    assert market.cached_prices(conn, 10000002, [5000], "sell") == {5000: 9.0}
    assert market.region_wide_types(conn, 10000002, [5000], "sell") == set()


def test_fallback_with_no_orders_anywhere_caches_null(conn, monkeypatch):
    monkeypatch.setattr(
        market, "_order_prices", lambda *a, **k: (None, None)
    )
    market.refresh_prices(
        conn, 10000002, [5000], "sell",
        fallback_type_ids={5000}, fallback_region_id=10000002,
    )
    assert market.cached_prices(conn, 10000002, [5000], "sell") == {}
    assert market.region_wide_types(conn, 10000002, [5000], "sell") == set()


def test_sustained_throttle_stops_the_pool(conn, monkeypatch):
    """One stays-throttled request must stop the whole refresh — queued
    types skip instead of each sleeping through its own retry ladder."""
    calls = []

    def throttled(client, region_id, type_id, source, low_budget):
        calls.append(type_id)
        raise market._Throttled

    monkeypatch.setattr(market, "_best_order_price", throttled)
    fetched, skipped, fresh = market.refresh_prices(
        conn, 10000002, [34, 35, 36], "sell", workers=1
    )
    assert (fetched, skipped, fresh) == (0, 3, 0)
    assert len(calls) == 1  # pool-wide stop after the first throttle


# --- review 2026-09-05: min_volume on the ladders, buy-side ladder guard ---


def test_hub_ladder_carries_min_volume():
    """Each rung is (price, volume_remain, min_volume): ESI's minimum
    fill rides along (absent or junk = 1) so the walks can skip an order
    that cannot be bought in the needed quantity."""
    station = config.JITA_44_STATION_ID
    orders = [
        {"price": 9.0, "location_id": station, "volume_remain": 40, "min_volume": 10},
        {"price": 8.5, "location_id": station, "volume_remain": 10},
        {"price": 8.0, "location_id": station, "volume_remain": 0, "min_volume": 3},
        {"price": 7.0, "location_id": station, "volume_remain": 4, "min_volume": "junk"},
        {"price": 6.5, "location_id": station, "volume_remain": 4, "min_volume": 0},
        {"price": 1.0, "location_id": 60000001, "volume_remain": 99, "min_volume": 1},
    ]
    assert market._hub_ladder(orders, config.THE_FORGE_REGION_ID) == [
        (6.5, 4, 1), (7.0, 4, 1), (8.5, 10, 1), (9.0, 40, 10),
    ]


def test_refresh_persists_min_volume_and_reads_it_back(conn, monkeypatch):
    monkeypatch.setattr(
        market, "_hub_ladder_quote",
        lambda *a, **k: (8.5, 1, [(8.5, 10, 1), (9.0, 40, 10)]),
    )
    market.refresh_prices(conn, 10000002, [34], "sell", ladder_type_ids=[34])
    assert market.cached_hub_ladders(conn, 10000002, [34]) == {
        34: [(8.5, 10, 1), (9.0, 40, 10)]
    }
    # A fetcher still handing back (price, volume) pairs stores minimum 1
    # (the ladder-less fresh row is refetched regardless of age).
    conn.execute("DELETE FROM hub_sell_order")
    conn.commit()
    monkeypatch.setattr(
        market, "_hub_ladder_quote", lambda *a, **k: (8.0, 1, [(8.0, 5)])
    )
    market.refresh_prices(conn, 10000002, [34], "sell", ladder_type_ids=[34])
    assert market.cached_hub_ladders(conn, 10000002, [34]) == {34: [(8.0, 5, 1)]}
    # Rows stored before the column read back as minimum 1 too.
    conn.execute(
        "INSERT INTO hub_sell_order (region_id, type_id, price, volume_remain) "
        "VALUES (10000002, 35, 2.0, 7)"
    )
    conn.commit()
    assert market.cached_hub_ladders(conn, 10000002, [35]) == {35: [(2.0, 7, 1)]}


def test_sell_ladders_are_empty_for_a_buy_price_source(conn):
    """No hub SELL ladder is ever pulled for the buy side, so the sourcing
    pass gets NO ladders at all — never the structure's alone, which would
    let a dearer C-J6 book fill-price a buy the Jita quote covers
    (review 2026-09-05, P0)."""
    conn.execute(
        "INSERT INTO hub_sell_order (region_id, type_id, price, volume_remain) "
        "VALUES (10000002, 34, 9.0, 10)"
    )
    conn.execute(
        "INSERT INTO structure_sell_order "
        "(structure_id, type_id, price, volume_remain, min_volume) "
        "VALUES (?, 34, 50.0, 100, 2)",
        (config.CJ6_KEEPSTAR_STRUCTURE_ID,),
    )
    conn.commit()
    both = market.sell_ladders(conn, store.get_settings(conn), [34])
    assert both == {
        store.BUY_VENUE_HUB: {34: [(9.0, 10, 1)]},
        store.BUY_VENUE_STRUCTURE: {34: [(50.0, 100, 2)]},
    }
    # A buy-side hub has no sell ladder; the structure keeps its own under
    # its own basis (v1.26 — the engine stands the hub quote in as one
    # unbounded rung, so the dearer book still cannot win a unit).
    conn.execute("UPDATE settings SET price_source = 'buy'")
    conn.commit()
    assert market.sell_ladders(conn, store.get_settings(conn), [34]) == {
        store.BUY_VENUE_HUB: {}, store.BUY_VENUE_STRUCTURE: {34: [(50.0, 100, 2)]},
    }
    # v1.26: a venue priced at its best order for any quantity hands over
    # no ladder at all — per market.
    conn.execute(
        "UPDATE settings SET price_source = 'sell', hub_price_basis = 'ladder', "
        "structure_price_basis = 'min_sell'"
    )
    conn.commit()
    assert market.sell_ladders(conn, store.get_settings(conn), [34]) == {
        store.BUY_VENUE_HUB: {34: [(9.0, 10, 1)]}, store.BUY_VENUE_STRUCTURE: {},
    }
    conn.execute("UPDATE settings SET hub_price_basis = 'min_sell'")
    conn.commit()
    assert market.sell_ladders(conn, store.get_settings(conn), [34]) == {
        store.BUY_VENUE_HUB: {}, store.BUY_VENUE_STRUCTURE: {},
    }


# --- v1.29 (Buy tab): the hub's best BUY order from the same pull -----------


class _BookClient:
    """One or more regions' whole order book, both sides, with the
    order_type of every request recorded — so a test can prove the sell
    side asks ESI for 'all' (one pull, two sides) and the v1.9 fallback
    re-pull stays one-sided."""

    def __init__(self, books):
        self.books = books  # {region_id: [order, ...]}
        self.calls = []

    def get(self, url, params=None, headers=None):
        import httpx

        region_id = int(url.rstrip("/").split("/")[-2])
        order_type = params["order_type"]
        self.calls.append((region_id, order_type))
        orders = self.books.get(region_id, [])
        if order_type == "sell":
            orders = [o for o in orders if not o.get("is_buy_order")]
        elif order_type == "buy":
            orders = [o for o in orders if o.get("is_buy_order")]
        return httpx.Response(
            200,
            json=orders,
            headers={"X-Pages": "1"},
            request=httpx.Request("GET", url),
        )


def _order(price, volume=10, buy=False, station=None):
    return {
        "price": price,
        "location_id": station if station is not None else config.JITA_44_STATION_ID,
        "volume_remain": volume,
        "is_buy_order": buy,
    }


_MIXED_FORGE_BOOK = [
    _order(9.0, volume=40),
    _order(8.5, volume=10),
    _order(5.0, volume=999, station=60000001),  # backwater sell
    _order(7.0, volume=100, buy=True),  # the hub's best live bid
    _order(7.5, volume=0, buy=True),  # nothing left to fill
    _order(6.0, volume=50, buy=True),
    _order(99.0, volume=5, buy=True, station=60000001),  # off-station bid
]


def test_all_pull_keeps_the_sell_quote_and_ladder_sell_only():
    """One order_type='all' pull feeds three answers, and the bids must not
    leak into two of them: the quote is still the cheapest HUB SELL (6.0 is
    a bid, not a price anyone can pay) and the ladder is still the sell
    rungs only."""
    import threading

    client = _BookClient({config.THE_FORGE_REGION_ID: _MIXED_FORGE_BOOK})
    price, hub, ladder, best_buy = market._hub_ladder_quote(
        client, config.THE_FORGE_REGION_ID, 34, "sell", threading.Event(), None,
    )
    assert (price, hub) == (8.5, 1)
    assert ladder == [(8.5, 10, 1), (9.0, 40, 1)]
    # the hub station's highest bid with volume left; zero-volume and
    # off-station bids dropped
    assert best_buy == 7.0
    assert client.calls == [(config.THE_FORGE_REGION_ID, "all")]


def test_best_buy_is_region_wide_where_the_region_has_no_hub():
    """A region with no configured station filter reduces region-wide on
    the buy side exactly as it does on the sell side."""
    import threading

    client = _BookClient({10000043: _MIXED_FORGE_BOOK})
    price, hub, _ladder, best_buy = market._hub_ladder_quote(
        client, 10000043, 34, "sell", threading.Event(), None,
    )
    assert (price, hub, best_buy) == (5.0, 1, 99.0)


def test_region_wide_sell_fallback_still_carries_the_hub_best_buy():
    """A type with no hub SELL order takes the v1.9 region-wide sell quote
    (hub = 0) and the bid from the same pull rides along; the fallback
    re-pull against another region stays on the sell side, since it only
    ever answers a sell question."""
    import threading

    same_region = _BookClient({
        config.THE_FORGE_REGION_ID: [
            _order(5.0, volume=999, station=60000001),  # backwater sell only
            _order(7.0, volume=100, buy=True),
        ]
    })
    price, hub, ladder, best_buy = market._hub_ladder_quote(
        same_region, config.THE_FORGE_REGION_ID, 5000, "sell",
        threading.Event(), config.THE_FORGE_REGION_ID,
    )
    assert (price, hub, ladder, best_buy) == (5.0, 0, [], 7.0)
    assert same_region.calls == [(config.THE_FORGE_REGION_ID, "all")]

    other_region = _BookClient({
        config.THE_FORGE_REGION_ID: [_order(7.0, volume=100, buy=True)],
        10000043: [_order(3.0, volume=5, station=60000001)],
    })
    price, hub, _ladder, best_buy = market._hub_ladder_quote(
        other_region, config.THE_FORGE_REGION_ID, 5000, "sell",
        threading.Event(), 10000043,
    )
    assert (price, hub, best_buy) == (3.0, 0, 7.0)
    assert other_region.calls == [
        (config.THE_FORGE_REGION_ID, "all"), (10000043, "sell"),
    ]


def test_refresh_caches_the_hub_best_buy_for_ladder_types(conn, monkeypatch):
    """The bid is cached under its own source with hub = 1 and the sell
    row's fetched_at, and it is a sidecar: it does not move the counts."""
    monkeypatch.setattr(
        market, "_hub_ladder_quote",
        lambda *a, **k: (8.5, 1, [(8.5, 10, 1)], 7.0),
    )
    fetched, skipped, fresh = market.refresh_prices(
        conn, 10000002, [34], "sell", ladder_type_ids=[34]
    )
    assert (fetched, skipped, fresh) == (1, 0, 0)
    assert market.cached_hub_best_buy(conn, 10000002, [34]) == {34: 7.0}
    row = conn.execute(
        "SELECT price, hub, fetched_at FROM market_price "
        "WHERE type_id = 34 AND region_id = 10000002 AND source = ?",
        (market.HUB_BUY_SOURCE,),
    ).fetchone()
    sell = conn.execute(
        "SELECT fetched_at FROM market_price WHERE type_id = 34 "
        "AND region_id = 10000002 AND source = 'sell'"
    ).fetchone()
    assert (row["price"], row["hub"]) == (7.0, 1)
    assert row["fetched_at"] == sell["fetched_at"]
    # the sell side is untouched by the second write
    assert market.cached_prices(conn, 10000002, [34], "sell") == {34: 8.5}


def test_refresh_caches_no_bid_as_null_and_does_not_refetch(conn, monkeypatch):
    """No bid at all is an answer, cached as NULL like a missing sell
    quote — so a fresh type with a ladder and no bid is not refetched on
    every pass."""
    calls = []

    def fetch(*a, **k):
        calls.append(1)
        return 8.5, 1, [(8.5, 10, 1)], None

    monkeypatch.setattr(market, "_hub_ladder_quote", fetch)
    market.refresh_prices(conn, 10000002, [34], "sell", ladder_type_ids=[34])
    assert market.cached_hub_best_buy(conn, 10000002, [34]) == {}
    row = conn.execute(
        "SELECT price FROM market_price WHERE type_id = 34 "
        "AND region_id = 10000002 AND source = ?",
        (market.HUB_BUY_SOURCE,),
    ).fetchone()
    assert row is not None and row["price"] is None
    fetched, _skipped, fresh = market.refresh_prices(
        conn, 10000002, [34], "sell", ladder_type_ids=[34]
    )
    assert (fetched, fresh, len(calls)) == (0, 1, 1)


def test_hub_priced_ladder_type_without_a_bid_row_is_refetched(conn, monkeypatch):
    """A ladder type priced before the Buy tab existed has no bid row at
    all — refetch it inside the cache window, exactly as a missing ladder
    does (one request answers both sides anyway)."""
    seed(conn, 34, 8.5, age_seconds=10)
    conn.execute(
        "INSERT INTO hub_sell_order (region_id, type_id, price, volume_remain) "
        "VALUES (10000002, 34, 8.5, 10)"
    )
    conn.commit()
    monkeypatch.setattr(
        market, "_hub_ladder_quote",
        lambda *a, **k: (8.0, 1, [(8.0, 5, 1)], 6.5),
    )
    fetched, _skipped, fresh = market.refresh_prices(
        conn, 10000002, [34], "sell", ladder_type_ids=[34]
    )
    assert (fetched, fresh) == (1, 0)
    assert market.cached_hub_best_buy(conn, 10000002, [34]) == {34: 6.5}


def test_three_tuple_fetcher_leaves_the_buy_cache_untouched(conn, monkeypatch):
    """The _hub_ladder_quote seam stays backward compatible: a fetcher that
    answers (price, hub, ladder) never looked at the buy side, so a cached
    bid must survive rather than be blanked to NULL."""
    seed(conn, 34, 3.0, age_seconds=10, source=market.HUB_BUY_SOURCE)
    monkeypatch.setattr(
        market, "_hub_ladder_quote", lambda *a, **k: (8.5, 1, [(8.5, 10, 1)])
    )
    fetched, skipped, _fresh = market.refresh_prices(
        conn, 10000002, [34], "sell", ladder_type_ids=[34]
    )
    assert (fetched, skipped) == (1, 0)
    assert market.cached_hub_ladders(conn, 10000002, [34]) == {34: [(8.5, 10, 1)]}
    assert market.cached_hub_best_buy(conn, 10000002, [34]) == {34: 3.0}


def test_buy_price_source_writes_no_sidecar(conn, monkeypatch):
    """Under the 'max_buy' basis nothing changes: no ladder type, no sell
    ladder, and the only 'buy' row is the one refresh_prices has always
    written for the plan quote itself."""
    monkeypatch.setattr(market, "_best_order_price", lambda *a, **k: 7.0)
    market.refresh_prices(
        conn, 10000002, [34], market.HUB_BUY_SOURCE, ladder_type_ids=[34]
    )
    assert market.cached_hub_best_buy(conn, 10000002, [34]) == {34: 7.0}
    assert conn.execute(
        "SELECT COUNT(*) FROM market_price WHERE source = ?",
        (market.HUB_BUY_SOURCE,),
    ).fetchone()[0] == 1
    assert market.cached_hub_ladders(conn, 10000002, [34]) == {}


def test_cached_hub_best_buy_is_cache_only_and_any_age(conn):
    seed(conn, 34, 7.0, age_seconds=999999, source=market.HUB_BUY_SOURCE)
    seed(conn, 35, None, age_seconds=10, source=market.HUB_BUY_SOURCE)
    assert market.cached_hub_best_buy(conn, 10000002, [34, 35, 36]) == {34: 7.0}
    # A region-wide 'buy' row (the max_buy basis plus a v1.9 fallback)
    # still reads back — the flag is the caller's badge, not a filter
    # (contract review A21).
    conn.execute(
        "UPDATE market_price SET hub = 0 WHERE type_id = 34 AND source = ?",
        (market.HUB_BUY_SOURCE,),
    )
    conn.commit()
    assert market.cached_hub_best_buy(conn, 10000002, [34]) == {34: 7.0}
    assert market.region_wide_types(
        conn, 10000002, [34], market.HUB_BUY_SOURCE
    ) == {34}


def test_a_sidecar_null_does_not_suppress_the_max_buy_fallback(
    conn, monkeypatch
):
    """Review 2026-09-23. The sidecar shares its source string with the
    max-buy PLAN quote (A21), and it is station filtered: "nobody bids
    at Jita 4-4" is cached as a fresh NULL row. Switch the basis to Max
    Buy Order inside the cache window and that row used to count as a
    current plan quote, so a raw leaf never got its v1.9 region-wide bid
    and planned as unpriced. A buy row with no price refetches."""
    seed(conn, 34, None, age_seconds=10, source=market.HUB_BUY_SOURCE)
    calls = []

    def fallback(client, region_id, type_id, source, low_budget, fallback_region):
        calls.append((type_id, source))
        return 15.0, 0

    monkeypatch.setattr(market, "_fallback_price", fallback)
    fetched, _skipped, fresh = market.refresh_prices(
        conn, 10000002, [34], market.HUB_BUY_SOURCE,
        fallback_type_ids=[34], fallback_region_id=10000043,
    )
    assert (fetched, fresh) == (1, 0)
    assert calls == [(34, market.HUB_BUY_SOURCE)]
    assert market.cached_hub_quotes(
        conn, 10000002, [34], market.HUB_BUY_SOURCE
    ) == {34: (15.0, True)}

    # A PRICED buy row is a real quote whatever wrote it — still fresh.
    seed(conn, 35, 7.0, age_seconds=10, source=market.HUB_BUY_SOURCE)
    fetched, _skipped, fresh = market.refresh_prices(
        conn, 10000002, [35], market.HUB_BUY_SOURCE,
        fallback_type_ids=[35], fallback_region_id=10000043,
    )
    assert (fetched, fresh) == (0, 1)

    # And the sell side keeps caching "no orders" as an answer.
    seed(conn, 36, None, age_seconds=10)
    fetched, _skipped, fresh = market.refresh_prices(
        conn, 10000002, [36], "sell",
        fallback_type_ids=[36], fallback_region_id=10000043,
    )
    assert (fetched, fresh) == (0, 1)
