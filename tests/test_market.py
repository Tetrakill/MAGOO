"""Price cache and refresh selection (v1.4.2 decoupling) — no network:
the per-type fetcher is monkeypatched."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from magoo import market, store


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
