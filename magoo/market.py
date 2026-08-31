"""Price snapshots from ESI's public market endpoints (no auth needed).

Two price sets feed the engine:
- adjusted prices (CCP's smoothed prices) for EIV / job install cost
- regional order prices (min sell / max buy) for cost basis and the MILP
  build-savings objective

Prices are DECOUPLED from planning (v1.4.2, mirroring the v1.1 ESI-snapshot
decoupling): the dashboard's "Refresh Prices" button does the slow pull into
the market_price cache, and planning reads the cache regardless of age — no
network on the planning path. The refresh itself is parallel (worker pool);
CCP's 2025 token-bucket limit for the market-order group is 12,000 tokens /
15 min at 2 tokens per request, so a full ~400-request refresh uses ~7% of
the budget — concurrency compresses wall time without consuming more tokens.
Guards: esi_request's 420/429 Retry-After handling, plus a pool-wide stop
when a request stays throttled or X-Ratelimit-Remaining runs low (skipped
types simply stay stale for the next refresh).

Adjusted prices are cached in the same table under source='adjusted'
(region_id 0) so planning needs no network for EIV either.
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

from . import config, costing, store
from .esi import ESI_BASE, _int_header, esi_request

# ESI's own server-side cache on the orders endpoint is 300s — refetching
# sooner returns identical data, so 300s is the floor worth refreshing at.
ESI_ORDERS_CACHE_SECONDS = 300
FETCH_WORKERS = 12
# Stop the pool when the token bucket runs this low (out of 12,000).
RATELIMIT_STOP_THRESHOLD = 500

ADJUSTED_SOURCE = "adjusted"
ADJUSTED_REGION = 0


class _Throttled(Exception):
    """ESI still rate-limiting after esi_request's internal retries."""


def fetch_adjusted_prices() -> dict[int, float]:
    """CCP adjusted prices for every type (public, one call)."""
    resp = esi_request(f"{ESI_BASE}/markets/prices/")
    resp.raise_for_status()
    return {
        row["type_id"]: row["adjusted_price"]
        for row in resp.json()
        if row.get("adjusted_price") is not None
    }


def _order_prices(
    client: httpx.Client,
    region_id: int,
    type_id: int,
    source: str,
    low_budget: threading.Event,
) -> tuple[float | None, float | None]:
    """(hub price, region-wide price) for one type in one region from ONE
    paged pull: min sell or max buy at the region's hub station where one
    is configured (The Forge -> Jita 4-4), and the same over every station
    in the region. Either is None when no such orders exist. Regions with
    no configured hub return the same value twice.

    Runs on worker threads: network only, no database access. Raises
    _Throttled when ESI keeps answering 420/429 through esi_request's own
    Retry-After handling; flags low_budget when the rate-limit bucket runs
    low so the pool stops starting new work."""
    station = config.PRICE_STATION_FILTERS.get(region_id)
    prices: list[float] = []
    region_prices: list[float] = []
    page = 1
    while True:
        resp = esi_request(
            f"{ESI_BASE}/markets/{region_id}/orders/",
            params={"type_id": type_id, "order_type": source, "page": page},
            client=client,
        )
        if resp.status_code in (420, 429):
            raise _Throttled
        if resp.status_code == 404:
            if page == 1:
                return None, None  # no orders for this type at all
            break  # book shrank mid-pull: keep the pages already collected
        resp.raise_for_status()
        try:
            remaining = int(resp.headers.get("X-Ratelimit-Remaining", 10**6))
        except ValueError:
            remaining = 10**6
        if remaining < RATELIMIT_STOP_THRESHOLD:
            low_budget.set()
        orders = resp.json()
        for o in orders:
            region_prices.append(o["price"])
            if station is None or o.get("location_id") == station:
                prices.append(o["price"])
        if page >= _int_header(resp.headers, "X-Pages", 1):
            break
        page += 1
    best = min if source == "sell" else max
    return (
        best(prices) if prices else None,
        best(region_prices) if region_prices else None,
    )


def _best_order_price(
    client: httpx.Client,
    region_id: int,
    type_id: int,
    source: str,
    low_budget: threading.Event,
) -> float | None:
    """The configured quote for one type: hub-station best order where the
    region has a hub, region-wide otherwise. None when no orders exist
    (illiquid — the engine flags rather than invents)."""
    return _order_prices(client, region_id, type_id, source, low_budget)[0]


def _fallback_price(
    client: httpx.Client,
    region_id: int,
    type_id: int,
    source: str,
    low_budget: threading.Event,
    fallback_region_id: int,
) -> tuple[float | None, int]:
    """(price, hub flag) for a raw leaf: the hub quote when there is one,
    else the region-wide best order from fallback_region_id (v1.9 — NPC-
    seeded goods such as Marines or Janitors rarely sit on the hub). The
    same paged pull serves both when the regions coincide; a different
    fallback region costs one extra pull. hub = 1 for a hub quote (or
    nothing at all), 0 for a region-wide fallback."""
    hub, region_wide = _order_prices(
        client, region_id, type_id, source, low_budget
    )
    if hub is not None:
        return hub, 1
    if fallback_region_id != region_id:
        _, region_wide = _order_prices(
            client, fallback_region_id, type_id, source, low_budget
        )
    if region_wide is None:
        return None, 1
    return region_wide, 0


def cached_prices(conn, region_id: int, type_ids, source: str) -> dict[int, float]:
    """{type_id: price} straight from the cache, ANY age. The planning path —
    never touches the network."""
    result: dict[int, float] = {}
    for type_id in type_ids:
        row = conn.execute(
            "SELECT price FROM market_price "
            "WHERE type_id = ? AND region_id = ? AND source = ?",
            (type_id, region_id, source),
        ).fetchone()
        if row is not None and row["price"] is not None:
            result[type_id] = row["price"]
    return result


def cached_adjusted_prices(conn, type_ids) -> dict[int, float]:
    return cached_prices(conn, ADJUSTED_REGION, type_ids, ADJUSTED_SOURCE)


def cached_hub_quotes(
    conn, region_id: int, type_ids, source: str
) -> dict[int, tuple[float, bool]]:
    """{type_id: (price, region_wide)} straight from the cache, ANY age —
    cached_prices plus the v1.9 provenance bit (hub = 0 → the quote is a
    region-wide fallback). Types with no price are absent."""
    result: dict[int, tuple[float, bool]] = {}
    for type_id in type_ids:
        row = conn.execute(
            "SELECT price, hub FROM market_price "
            "WHERE type_id = ? AND region_id = ? AND source = ?",
            (type_id, region_id, source),
        ).fetchone()
        if row is not None and row["price"] is not None:
            result[type_id] = (row["price"], not row["hub"])
    return result


def region_wide_types(conn, region_id: int, type_ids, source: str) -> set[int]:
    """Types whose cached quote is a region-wide fallback (hub = 0), for
    the 'region price' badges. Cache-only; never touches the network."""
    result: set[int] = set()
    for type_id in type_ids:
        row = conn.execute(
            "SELECT hub FROM market_price "
            "WHERE type_id = ? AND region_id = ? AND source = ? "
            "AND price IS NOT NULL",
            (type_id, region_id, source),
        ).fetchone()
        if row is not None and not row["hub"]:
            result.add(type_id)
    return result


def price_cache_state(conn, region_id: int, source: str):
    """(cached type count, latest fetched_at ISO string or None) — dashboard
    display of how current the price cache is."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(fetched_at) AS latest FROM market_price "
        "WHERE region_id = ? AND source = ?",
        (region_id, source),
    ).fetchone()
    return row["n"], row["latest"]


def refresh_prices(
    conn,
    region_id: int,
    type_ids,
    source: str = "sell",
    max_age_seconds: int = ESI_ORDERS_CACHE_SECONDS,
    workers: int = FETCH_WORKERS,
    fallback_type_ids=(),
    fallback_region_id: int | None = None,
) -> tuple[int, int, int]:
    """Refetch every requested type whose cached price is older than
    max_age_seconds (default: ESI's own 300s server cache — anything fresher
    would return identical data). Network happens on a worker pool; all
    database writes stay on the calling thread. Types skipped because of
    throttling or transport errors remain stale for the next refresh.

    fallback_type_ids (v1.9): raw leaves that may take the region-wide best
    order from fallback_region_id when they have no hub-station quote;
    such rows are cached with hub = 0 (see region_wide_types).

    Returns (fetched, skipped, already_fresh)."""
    type_ids = list(type_ids)
    fallback = set(fallback_type_ids) if fallback_region_id else set()
    now = datetime.now(timezone.utc)
    stale: list[int] = []
    for type_id in type_ids:
        row = conn.execute(
            "SELECT fetched_at FROM market_price "
            "WHERE type_id = ? AND region_id = ? AND source = ?",
            (type_id, region_id, source),
        ).fetchone()
        if row is not None:
            age = (now - datetime.fromisoformat(row["fetched_at"])).total_seconds()
            if age < max_age_seconds:
                continue
        stale.append(type_id)
    already_fresh = len(type_ids) - len(stale)
    if not stale:
        return 0, 0, already_fresh

    low_budget = threading.Event()
    fetched: dict[int, tuple[float | None, int]] = {}
    skipped = 0

    def fetch_one(client: httpx.Client, type_id: int) -> tuple[float | None, int]:
        if low_budget.is_set():
            raise _Throttled
        try:
            if type_id in fallback:
                return _fallback_price(
                    client, region_id, type_id, source, low_budget,
                    fallback_region_id,
                )
            return (
                _best_order_price(client, region_id, type_id, source, low_budget),
                1,
            )
        except _Throttled:
            # A request that STAYED throttled through esi_request's own
            # retries means the error window is tripped — stop the whole
            # pool instead of letting every queued type sleep through its
            # own retry ladder (the promised pool-wide stop).
            low_budget.set()
            raise

    with httpx.Client(timeout=60.0) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(fetch_one, client, type_id): type_id
                for type_id in stale
            }
            for future in as_completed(futures):
                type_id = futures[future]
                try:
                    fetched[type_id] = future.result()
                except (_Throttled, ValueError, httpx.HTTPError):
                    # ValueError covers a junk 200 body (JSONDecodeError
                    # subclasses it) — one bad type skips like a throttled
                    # one instead of aborting the whole refresh at
                    # future.result() and discarding every fetched price.
                    skipped += 1

    now_iso = datetime.now(timezone.utc).isoformat()
    for type_id, (price, hub) in fetched.items():
        # None is cached too: "no orders" is an answer (illiquid), and the
        # engine flags those rather than inventing a price.
        conn.execute(
            "INSERT OR REPLACE INTO market_price "
            "(type_id, region_id, source, price, fetched_at, hub) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (type_id, region_id, source, price, now_iso, hub),
        )
    conn.commit()
    return len(fetched), skipped, already_fresh


STRUCTURE_SOURCE = "structure"


def _sell_ladders(orders, wanted: set[int]) -> dict[int, list[tuple[float, int]]]:
    """Ascending (price, volume_remain) sell ladder per wanted type from a
    structure order dump — the one filter over the book (v1.10): buy
    orders, unwanted types and orders with nothing left to sell are
    dropped; the best price is the first rung. ESI always sends
    volume_remain; a dump without it yields no ladder and no quote."""
    ladders: dict[int, list[tuple[float, int]]] = {}
    for order in orders:
        if order.get("is_buy_order"):
            continue
        type_id = order["type_id"]
        if type_id not in wanted:
            continue
        volume = int(order.get("volume_remain") or 0)
        if volume <= 0:
            continue
        ladders.setdefault(type_id, []).append((order["price"], volume))
    for ladder in ladders.values():
        ladder.sort(key=lambda o: o[0])
    return ladders


def _min_sell_by_type(orders, wanted: set[int]) -> dict[int, float]:
    """Cheapest sell order per wanted type — the first rung of each
    ladder (v1.6 sell-quote contract, v1.10 single source of truth)."""
    return {t: ladder[0][0] for t, ladder in _sell_ladders(orders, wanted).items()}


def refresh_structure_prices(
    conn, structure_id: int, type_ids, character_id: int
) -> int:
    """Cache min-sell prices for the wanted types from one structure's
    market (v1.6 capital pricing), keyed as region_id=structure_id,
    source='structure'. The whole order book comes down in one paginated
    authed pull — the endpoint has no per-type filter — so previous rows
    for the structure are replaced wholesale; a type with no sell order is
    cached as NULL ("no orders" is an answer, matching refresh_prices).

    v1.10: the same pull also persists each wanted type's SELL ladder
    (structure_sell_order, replaced wholesale for the structure) so the
    buy-venue comparison can judge depth at plan time without the network.
    Returns the number of wanted types that had a sell order."""
    from . import esi

    orders = esi.fetch_structure_orders(conn, character_id, structure_id)
    wanted = set(type_ids)
    ladders = _sell_ladders(orders, wanted)
    best = {t: ladder[0][0] for t, ladder in ladders.items()}
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "DELETE FROM market_price WHERE region_id = ? AND source = ?",
        (structure_id, STRUCTURE_SOURCE),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at) VALUES (?, ?, ?, ?, ?)",
        [
            (type_id, structure_id, STRUCTURE_SOURCE, best.get(type_id), now_iso)
            for type_id in wanted
        ],
    )
    conn.execute(
        "DELETE FROM structure_sell_order WHERE structure_id = ?",
        (structure_id,),
    )
    conn.executemany(
        "INSERT INTO structure_sell_order "
        "(structure_id, type_id, price, volume_remain) VALUES (?, ?, ?, ?)",
        [
            (structure_id, type_id, price, volume)
            for type_id, ladder in ladders.items()
            for price, volume in ladder
        ],
    )
    conn.commit()
    return len(best)


def cached_structure_ladders(
    conn, structure_id: int, type_ids
) -> dict[int, list[tuple[float, int]]]:
    """{type_id: ascending [(price, volume_remain), ...]} from the last
    structure refresh — only types with at least one sell order appear.
    Cache-only; never touches the network."""
    wanted = set(type_ids)
    if not wanted:
        return {}
    ladders: dict[int, list[tuple[float, int]]] = {}
    for row in conn.execute(
        "SELECT type_id, price, volume_remain FROM structure_sell_order "
        "WHERE structure_id = ? ORDER BY type_id, price",
        (structure_id,),
    ):
        if row["type_id"] in wanted:
            ladders.setdefault(row["type_id"], []).append(
                (row["price"], row["volume_remain"])
            )
    return ladders


def structure_cache_state(conn, structure_id: int):
    """(cached type count, latest fetched_at ISO string or None) for the
    structure market's cache — dashboard display, like price_cache_state."""
    return price_cache_state(conn, structure_id, STRUCTURE_SOURCE)


def buy_quotes(
    conn, ref, settings, type_ids, exclude=None
) -> dict[int, costing.BuyQuote]:
    """The plan-time buy quote per type (v1.10): the hub cache (configured
    region/source, incl. v1.9 region-wide fallbacks) against the structure
    market's cached sell ladder, the cheaper LANDED venue winning
    (costing.choose_buy_venue). Cache-only. With structure_buy_enabled off
    — or for excluded types — every type is a hub quote. ``exclude``
    defaults to the active pipelines' finals: never bought, and their
    price_snapshot doubles as the run page's sell reference. Types with no
    quote anywhere are absent. ``BuyQuote.region_wide`` carries the v1.9
    provenance of a HUB quote (false once the structure undercuts it — the
    badge follows the venue actually used)."""
    type_ids = list(type_ids)
    if exclude is None:
        exclude = {
            p["final_product_type_id"] for p in store.active_pipelines(conn)
        }
    exclude = set(exclude)
    hub = cached_hub_quotes(
        conn, settings.price_region_id, type_ids, settings.price_source
    )
    compare = [
        t for t in type_ids
        if settings.structure_buy_enabled and t not in exclude
    ]
    ladders = cached_structure_ladders(
        conn, settings.structure_market(), compare
    )
    hub_rate = settings.freight_in_rate(store.BUY_VENUE_HUB)
    structure_rate = settings.freight_in_rate(store.BUY_VENUE_STRUCTURE)
    quotes: dict[int, costing.BuyQuote] = {}
    for type_id in type_ids:
        ladder = ladders.get(type_id)
        hub_price, hub_region_wide = hub.get(type_id, (None, False))
        if hub_price is None and not ladder:
            continue  # no quote anywhere: absent, as before v1.10
        # Packaged volume only matters when there is a comparison to make.
        volume = ref.type_info(type_id).freight_volume if ladder else 0.0
        quote = costing.choose_buy_venue(
            hub_price, ladder, volume, hub_rate, structure_rate
        )
        quotes[type_id] = costing.BuyQuote(
            quote.price, quote.venue, quote.units_cheaper,
            region_wide=hub_region_wide and quote.venue == store.BUY_VENUE_HUB,
        )
    return quotes


def quote_maps(quotes) -> tuple[dict, dict, dict, set]:
    """Unzip buy_quotes into the Snapshot / costing inputs:
    (prices, buy_venue, structure_units_cheaper, region_wide)."""
    prices = {t: q.price for t, q in quotes.items()}
    venues = {t: q.venue for t, q in quotes.items()}
    units = {
        t: q.units_cheaper
        for t, q in quotes.items()
        if q.venue == store.BUY_VENUE_STRUCTURE
    }
    region_wide = {t for t, q in quotes.items() if q.region_wide}
    return prices, venues, units, region_wide


def store_adjusted_prices(conn, type_ids, adjusted: dict[int, float]) -> int:
    """Cache CCP adjusted prices for the demand set (source='adjusted',
    region 0) so the planning path needs no network for EIV."""
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (type_id, ADJUSTED_REGION, ADJUSTED_SOURCE, adjusted[type_id], now_iso)
        for type_id in type_ids
        if type_id in adjusted
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
