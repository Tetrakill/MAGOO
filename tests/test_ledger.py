"""Ledger (v1.27.0): the sales pull and the sales determination — every
ESI fetcher monkeypatched at the esi module seam (the test_esi pattern),
real reference data for type names and volumes, a temp state database.

Pull invariants under test: sell side only; idempotent upserts that never
delete; the two-pass contiguous transactions cursor (newest_id moves only
when the top pass joins the stored range; only the backfill pass may set
backfilled); owner normalisation (corp beats character); per-family
degrade (toggle, scope, role, dead token, rate group, ESI down) that never
raises and never spans the write lock across a fetch. Read invariants:
transactions are the units truth, contracts attribute price only when
clean, exclusions, the latest-executed-run cost basis, guarded math,
Top-10 orderings, calendar windows.
"""

import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

from magoo import config, costing, esi, ledger, store

TRACKED = 30000142  # Jita
STATION = 60003760  # Jita 4-4
PLAYER_CORP = 98000001
OTHER_CORP = 98000002
HULK = 22544
MACKINAW = 22548
ORCA = 28606
THANATOS = 23911  # capital-priced
A, B, C = 2001, 2002, 2003
BUYER = 91000001
NOW = "2026-09-07T12:00:00Z"
NOW_DT = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


# --- fixtures and builders ---------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    c.execute("INSERT INTO tracked_system VALUES (?)", (TRACKED,))
    c.execute(
        "INSERT INTO location_system (location_id, solar_system_id) VALUES (?, ?)",
        (STATION, TRACKED),
    )
    c.commit()
    yield c
    c.close()


def add_owner(conn, cid, scopes=esi.REQUESTED_SCOPES, count_sales=1, name=None):
    conn.execute(
        "INSERT INTO pool_character (character_id, character_name, include_assets, "
        "include_job_slots, count_assets, count_sales) VALUES (?, ?, 0, 0, 0, ?)",
        (cid, name or f"char {cid}", count_sales),
    )
    conn.execute(
        "INSERT INTO esi_token (character_id, refresh_token, access_token, expires_at, "
        "scopes) VALUES (?, 'rt', 'at', '2099-01-01T00:00:00+00:00', ?)",
        (cid, " ".join(scopes)),
    )
    conn.commit()


def add_corp(conn, corp=PLAYER_CORP, name="Test Holdings", count_sales=1):
    conn.execute(
        "INSERT INTO esi_corp (corporation_id, corporation_name, count_sales) "
        "VALUES (?, ?, ?)",
        (corp, name, count_sales),
    )
    conn.commit()


def add_pipeline(conn, type_id=HULK, qty=8, name=None, active=1):
    cur = conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, output_qty_per_run, "
        "is_active) VALUES (?, ?, ?, ?)",
        (name or f"pipeline {type_id}", type_id, qty, active),
    )
    conn.commit()
    return cur.lastrowid


def tx(tid, type_id=HULK, qty=1, price=300e6, date="2026-09-06T10:00:00Z",
       is_buy=False, is_personal=True, client_id=BUYER, location_id=STATION):
    return {
        "transaction_id": tid, "type_id": type_id, "quantity": qty,
        "unit_price": price, "date": date, "is_buy": is_buy,
        "is_personal": is_personal, "client_id": client_id,
        "location_id": location_id, "journal_ref_id": tid * 10,
    }


def order(oid, type_id=HULK, price=310e6, total=5, remain=5, state=None,
          issued="2026-09-01T00:00:00Z", duration=90, is_buy=False,
          is_corporation=False, location_id=STATION, **extra):
    row = {
        "order_id": oid, "type_id": type_id, "price": price,
        "volume_total": total, "volume_remain": remain, "issued": issued,
        "duration": duration, "location_id": location_id, "region_id": 10000002,
        "range": "station", "is_corporation": is_corporation,
    }
    if is_buy is not None:
        row["is_buy_order"] = is_buy
    if state is not None:
        row["state"] = state
    row.update(extra)
    return row


def contract(contract_id, issuer=A, corp=PLAYER_CORP, for_corp=False,
             type="item_exchange", status="finished", price=350e6, acceptor=BUYER,
             date_issued="2026-09-01T00:00:00Z", date_completed="2026-09-05T00:00:00Z",
             availability="public", start_location_id=STATION, title=None):
    return {
        "contract_id": contract_id, "issuer_id": issuer,
        "issuer_corporation_id": corp, "for_corporation": for_corp, "type": type,
        "status": status, "price": price, "acceptor_id": acceptor, "assignee_id": 0,
        "availability": availability, "date_issued": date_issued,
        "date_accepted": date_completed, "date_completed": date_completed,
        "date_expired": "2026-09-15T00:00:00Z", "start_location_id": start_location_id,
        "title": title,
    }


def citem(record_id, type_id=HULK, qty=1, included=True, singleton=True):
    return {
        "record_id": record_id, "type_id": type_id, "quantity": qty,
        "raw_quantity": -1 if singleton else qty, "is_included": included,
        "is_singleton": singleton,
    }


def http_error(code, headers=None):
    resp = httpx.Response(code, headers=headers or {}, request=httpx.Request("GET", "https://esi.example/"))
    return httpx.HTTPStatusError(f"http {code}", request=resp.request, response=resp)


def raiser(exc):
    def _raise(*_args, **_kwargs):
        raise exc
    return _raise


def patch_sales(monkeypatch, conn, *, char_orders=None, char_history=None, char_tx=None,
                char_contracts=None, char_items=None, corp_orders=None, corp_history=None,
                corp_tx=None, corp_contracts=None, corp_items=None, corp_of=None, now=NOW):
    """Wire every ledger fetcher to canned data. Character tables are keyed
    by character id; corp orders/history/contracts by character id (None =
    403); corp_tx by (character id, division); items by (character id,
    contract id). Transaction values are a list (the newest page) or a dict
    keyed by from_id (None = the newest page) so cursor passes can be
    scripted; a callable is called (raise from it). Every fetcher asserts
    no write transaction is open. Returns the call log."""
    calls = []

    def guard(c):
        assert not c.in_transaction, "fetch phase ran inside a write transaction"

    def value(table, key, default):
        v = (table or {}).get(key, default)
        return v() if callable(v) else v

    def pages(table, key, from_id, default):
        v = (table or {}).get(key, default)
        if v is None:
            return None
        if callable(v):
            return v(from_id)
        if isinstance(v, dict):
            p = v.get(from_id, [])
            return p(from_id) if callable(p) else p
        return v if from_id is None else []

    def f_char_orders(c, cid):
        guard(c); calls.append(("char_orders", cid)); return value(char_orders, cid, [])

    def f_char_history(c, cid):
        guard(c); calls.append(("char_history", cid)); return value(char_history, cid, [])

    def f_char_tx(c, cid, from_id=None):
        guard(c); calls.append(("char_tx", cid, from_id)); return pages(char_tx, cid, from_id, [])

    def f_char_contracts(c, cid):
        guard(c); calls.append(("char_contracts", cid)); return value(char_contracts, cid, [])

    def f_char_items(c, cid, contract_id):
        guard(c); calls.append(("char_items", cid, contract_id))
        return value(char_items, (cid, contract_id), [])

    def f_corp_orders(c, cid, corp):
        guard(c); calls.append(("corp_orders", cid)); return value(corp_orders, cid, None)

    def f_corp_history(c, cid, corp):
        guard(c); calls.append(("corp_history", cid)); return value(corp_history, cid, None)

    def f_corp_tx(c, cid, corp, division, from_id=None):
        guard(c); calls.append(("corp_tx", cid, division, from_id))
        return pages(corp_tx, (cid, division), from_id, None)

    def f_corp_contracts(c, cid, corp):
        guard(c); calls.append(("corp_contracts", cid)); return value(corp_contracts, cid, None)

    def f_corp_items(c, cid, corp, contract_id):
        guard(c); calls.append(("corp_items", cid, contract_id))
        return value(corp_items, (cid, contract_id), [])

    monkeypatch.setattr(esi, "fetch_character_orders", f_char_orders)
    monkeypatch.setattr(esi, "fetch_character_order_history", f_char_history)
    monkeypatch.setattr(esi, "fetch_character_transactions", f_char_tx)
    monkeypatch.setattr(esi, "fetch_character_contracts", f_char_contracts)
    monkeypatch.setattr(esi, "fetch_character_contract_items", f_char_items)
    monkeypatch.setattr(esi, "fetch_corp_orders", f_corp_orders)
    monkeypatch.setattr(esi, "fetch_corp_order_history", f_corp_history)
    monkeypatch.setattr(esi, "fetch_corp_transactions", f_corp_tx)
    monkeypatch.setattr(esi, "fetch_corp_contracts", f_corp_contracts)
    monkeypatch.setattr(esi, "fetch_corp_contract_items", f_corp_items)

    def corporation_of(cid):
        v = (corp_of or {}).get(cid, PLAYER_CORP)
        return v() if callable(v) else v

    monkeypatch.setattr(esi, "character_corporation_id", corporation_of)
    monkeypatch.setattr(esi, "corporation_name", lambda corp: f"corp {corp}")
    monkeypatch.setattr(esi, "resolve_location", lambda c, cid, loc, memo=None: TRACKED)
    monkeypatch.setattr(ledger, "_now_iso", lambda now_=None: now)
    return calls


def pull_rows(conn, family=None, owner_id=None):
    sql = "SELECT * FROM sales_pull WHERE 1 = 1"
    params = []
    if family:
        sql += " AND family = ?"; params.append(family)
    if owner_id is not None:
        sql += " AND owner_id = ?"; params.append(owner_id)
    return conn.execute(sql + " ORDER BY owner_kind, owner_id, family, division", params).fetchall()


def status_of(conn, kind, oid, family, division=0):
    row = conn.execute(
        "SELECT * FROM sales_pull WHERE owner_kind = ? AND owner_id = ? AND family = ? "
        "AND division = ?", (kind, oid, family, division)
    ).fetchone()
    return row


def tx_rows(conn):
    return conn.execute("SELECT * FROM sale_transaction ORDER BY transaction_id").fetchall()


def dump(conn, table):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


# --- scopes and fetcher call shapes ---------------------------------------------


def test_scopes_extended_and_missing_scopes_detects_gap():
    assert len(esi.REQUESTED_SCOPES) == 12
    new = (
        "esi-markets.read_character_orders.v1",
        "esi-markets.read_corporation_orders.v1",
        "esi-contracts.read_character_contracts.v1",
        "esi-contracts.read_corporation_contracts.v1",
    )
    assert all(s in esi.REQUESTED_SCOPES for s in new)
    old = " ".join(s for s in esi.REQUESTED_SCOPES if s not in new)
    assert esi.missing_scopes(old) == new
    assert esi.missing_scopes(" ".join(esi.REQUESTED_SCOPES)) == ()
    assert len(esi.missing_scopes(None)) == 12
    assert set(esi.LEDGER_SCOPES.values()) <= set(esi.REQUESTED_SCOPES)


def test_transaction_fetchers_omit_from_id_on_first_page(conn, monkeypatch):
    """httpx renders {"from_id": None} as "?from_id=" and ESI answers 400:
    the first page must carry no from_id at all, later pages the int."""
    add_owner(conn, A)
    seen = []

    def fake_request(url, params=None, headers=None, client=None, **kwargs):
        seen.append((url, params, kwargs.get("retry_429")))
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(esi, "esi_request", fake_request)
    esi.fetch_character_transactions(conn, A)
    esi.fetch_character_transactions(conn, A, 5)
    esi.fetch_corp_transactions(conn, A, PLAYER_CORP, 3)
    esi.fetch_corp_transactions(conn, A, PLAYER_CORP, 3, from_id=7)
    assert seen[0][1] is None
    assert seen[1][1] == {"from_id": 5}
    assert seen[2][1] is None
    assert seen[3][1] == {"from_id": 7}
    assert "/wallets/3/transactions/" in seen[2][0]
    assert all(entry[2] is False for entry in seen)  # a 429 comes straight back to the ledger


def test_esi_request_hands_429_back_when_asked(monkeypatch):
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "600"}, request=httpx.Request("GET", "https://esi.example/")),
        httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://esi.example/")),
    ])
    slept = []
    monkeypatch.setattr(esi.httpx, "get", lambda *a, **k: next(responses))
    monkeypatch.setattr(esi.time, "sleep", lambda s: slept.append(s))
    resp = esi.esi_request("https://esi.example/latest/x/", retry_429=False)
    assert resp.status_code == 429 and slept == []
    # The default keeps the legacy behaviour: sleep, then retry.
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "1"}, request=httpx.Request("GET", "https://esi.example/")),
        httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://esi.example/")),
    ])
    assert esi.esi_request("https://esi.example/latest/x/").status_code == 200 and slept == [2]


# --- transactions: storage, idempotency, the cursor -------------------------------


def test_pull_stores_sell_transactions_and_skips_buys(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5), tx(4, is_buy=True), tx(3, qty=2)]})
    summary = ledger.pull_sales(conn, ref)
    rows = tx_rows(conn)
    assert [r["transaction_id"] for r in rows] == [3, 5]
    assert rows[1]["journal_ref_id"] == 50 and rows[1]["date"] == "2026-09-06T10:00:00Z"
    assert rows[0]["owner_kind"] == "character" and rows[0]["owner_id"] == A
    assert summary.new_sales == 2
    cursor = status_of(conn, "character", A, "transactions")
    assert (cursor["oldest_id"], cursor["newest_id"], cursor["backfilled"]) == (3, 5, 1)
    assert cursor["status"] == "ok" and cursor["via_character_id"] == A


def test_pull_is_idempotent(conn, ref, monkeypatch):
    add_owner(conn, A)
    feeds = dict(
        char_tx={A: [tx(5), tx(3)]},
        char_orders={A: [order(1)]}, char_history={A: [order(2, state="expired", remain=0)]},
        char_contracts={A: [contract(7)]}, char_items={(A, 7): [citem(1)]},
    )
    patch_sales(monkeypatch, conn, **feeds)
    ledger.pull_sales(conn, ref)
    first = {t: dump(conn, t) for t in ("sale_transaction", "sale_order", "sale_contract", "sale_contract_item")}
    ledger.pull_sales(conn, ref)
    second = {t: dump(conn, t) for t in first}
    assert first == second
    assert all(r["rows_new"] == 0 for r in pull_rows(conn) if r["status"] == "ok")


def _pages(*ids_per_page):
    """Descending id pages keyed the way the cursor asks for them: the
    newest page under None, each next page under the previous page's
    lowest id, and an empty page after the last."""
    table = {}
    key = None
    for ids in ids_per_page:
        table[key] = [tx(i) for i in ids]
        key = min(ids)
    table[key] = []
    return table


def test_first_pull_backfills_to_end_of_history_and_sets_cursor(conn, ref, monkeypatch):
    add_owner(conn, A)
    pages = _pages(range(100, 90, -1), range(90, 80, -1), range(80, 70, -1))
    calls = patch_sales(monkeypatch, conn, char_tx={A: pages})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"], row["status"]) == (71, 100, 1, "ok")
    assert len(tx_rows(conn)) == 30
    assert [c[2] for c in calls if c[0] == "char_tx"] == [None, 91, 81, 71]


def test_first_pull_cap_leaves_partial_and_resumes_below_oldest(conn, ref, monkeypatch):
    add_owner(conn, A)
    monkeypatch.setattr(config, "LEDGER_TX_PAGES_PER_REFRESH", 1)
    pages = _pages(range(100, 90, -1), range(90, 80, -1), range(80, 70, -1))
    calls = patch_sales(monkeypatch, conn, char_tx={A: pages})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"], row["status"]) == (81, 100, 0, "partial")
    assert "older history" in row["message"]
    assert len(tx_rows(conn)) == 20
    # Second refresh over IDENTICAL data: the top pass joins on page 1, the
    # backfill resumes at from_id = oldest_id; backfilled does not flip.
    calls.clear()
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert [c[2] for c in calls if c[0] == "char_tx"] == [None, 81]
    assert (row["oldest_id"], row["newest_id"], row["backfilled"]) == (71, 100, 0)
    assert len(tx_rows(conn)) == 30
    # Third: the page below 71 is empty → history exhausted.
    calls.clear()
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert [c[2] for c in calls if c[0] == "char_tx"] == [None, 71]
    assert (row["backfilled"], row["status"]) == (1, "ok")


def test_empty_wallet_first_then_history_backfills_fully(conn, ref, monkeypatch):
    """A wallet first seen empty must not claim a backfilled range: when
    sales appear later and exceed the cap, the backfill has to keep going
    until the end of history instead of stopping at the first cap."""
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_tx={A: {None: []}})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"], row["status"]) == (None, None, 0, "ok")
    monkeypatch.setattr(config, "LEDGER_TX_PAGES_PER_REFRESH", 1)
    pages = _pages(range(100, 90, -1), range(90, 80, -1), range(80, 70, -1))
    patch_sales(monkeypatch, conn, char_tx={A: pages})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"], row["status"]) == (81, 100, 0, "partial")
    ledger.pull_sales(conn, ref)
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"], row["status"]) == (71, 100, 1, "ok")
    assert len(tx_rows(conn)) == 30


def test_incremental_burst_walks_down_to_stored_range(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_tx={A: _pages(range(100, 90, -1))})
    ledger.pull_sales(conn, ref)
    burst = _pages(range(130, 120, -1), range(120, 110, -1), range(110, 100, -1), range(100, 90, -1))
    calls = patch_sales(monkeypatch, conn, char_tx={A: burst})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"], row["status"]) == (91, 130, 1, "ok")
    assert [c[2] for c in calls if c[0] == "char_tx"] == [None, 121, 111, 101]
    assert len(tx_rows(conn)) == 40


def test_incremental_not_connected_keeps_cursor(conn, ref, monkeypatch):
    """The rate budget runs out before the top pass reaches the stored
    range: rows seen are stored, but newest_id must NOT move (the gap
    between page 1's floor and the old newest would otherwise be lost)."""
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_tx={A: _pages(range(100, 90, -1))})
    ledger.pull_sales(conn, ref)
    monkeypatch.setattr(config, "LEDGER_RATE_BUDGET_FRACTION", 0.01)  # 1 call
    burst = _pages(range(130, 120, -1), range(120, 110, -1), range(110, 100, -1), range(100, 90, -1))
    patch_sales(monkeypatch, conn, char_tx={A: burst})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"]) == (91, 100, 1)
    assert row["status"] == "partial" and "budget" in row["message"]
    assert len(tx_rows(conn)) == 20  # page 1 stored anyway; upserts are harmless


def test_all_buy_wallet_finishes_first_pull_and_costs_one_call_after(conn, ref, monkeypatch):
    add_owner(conn, A)
    pages = {None: [tx(9, is_buy=True), tx(8, is_buy=True)], 8: []}
    calls = patch_sales(monkeypatch, conn, char_tx={A: pages})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["oldest_id"], row["newest_id"], row["backfilled"], row["status"]) == (8, 9, 1, "ok")
    assert tx_rows(conn) == []
    calls.clear()
    ledger.pull_sales(conn, ref)
    assert [c for c in calls if c[0] == "char_tx"] == [("char_tx", A, None)]


def test_boundary_only_page_is_end_of_history(conn, ref, monkeypatch):
    """An inclusive from_id that returns just the boundary row is the end,
    not a page to walk again."""
    add_owner(conn, A)
    pages = {None: [tx(50)], 50: [tx(50)]}
    calls = patch_sales(monkeypatch, conn, char_tx={A: pages})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "transactions")
    assert (row["backfilled"], row["status"]) == (1, "ok")
    assert len([c for c in calls if c[0] == "char_tx"]) == 2


def test_char_feed_corp_transaction_owned_by_corp_and_corp_feed_wins_owner(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5, is_personal=False), tx(4)]})
    ledger.pull_sales(conn, ref)
    rows = {r["transaction_id"]: r for r in tx_rows(conn)}
    assert (rows[5]["owner_kind"], rows[5]["owner_id"], rows[5]["division"], rows[5]["source_feed"]) == (
        "corporation", PLAYER_CORP, None, "character")
    assert (rows[4]["owner_kind"], rows[4]["owner_id"]) == ("character", A)
    # The corp feed re-owns the row: division learned, source_feed corp.
    corp_tx = {(A, d): ([tx(5, is_personal=False)] if d == 2 else []) for d in range(1, 8)}
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5, is_personal=False), tx(4)]}, corp_tx=corp_tx)
    ledger.pull_sales(conn, ref)
    row = {r["transaction_id"]: r for r in tx_rows(conn)}[5]
    assert (row["owner_kind"], row["owner_id"], row["division"], row["source_feed"]) == (
        "corporation", PLAYER_CORP, 2, "corporation")


def test_corp_divisions_reuse_role_holder_and_fall_back_on_403(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_owner(conn, B)
    corp_tx = {(A, 1): None}
    corp_tx.update({(B, d): [tx(100 + d)] for d in range(1, 8)})
    calls = patch_sales(monkeypatch, conn, corp_tx=corp_tx)
    ledger.pull_sales(conn, ref)
    rows = pull_rows(conn, "transactions", PLAYER_CORP)
    assert [r["division"] for r in rows] == list(range(1, 8))
    assert all(r["via_character_id"] == B and r["status"] == "ok" for r in rows)
    assert [c for c in calls if c[0] == "corp_tx" and c[1] == A] == [("corp_tx", A, 1, None)]
    assert len(tx_rows(conn)) == 7


def test_scope_precheck_skips_calls_and_records_no_scope(conn, ref, monkeypatch):
    old = tuple(s for s in esi.REQUESTED_SCOPES if "contracts" not in s and "orders" not in s)
    add_owner(conn, A, scopes=old)
    calls = patch_sales(monkeypatch, conn, char_tx={A: [tx(1)]})
    ledger.pull_sales(conn, ref)
    assert status_of(conn, "character", A, "orders")["status"] == "no_scope"
    assert status_of(conn, "character", A, "contracts")["status"] == "no_scope"
    assert status_of(conn, "character", A, "transactions")["status"] == "ok"
    assert status_of(conn, "corporation", PLAYER_CORP, "orders")["status"] == "no_scope"
    assert "log one in again" in status_of(conn, "character", A, "orders")["message"]
    assert not [c for c in calls if c[0] in ("char_orders", "char_history", "char_contracts", "corp_orders")]


def test_dead_token_candidate_falls_through_to_next_member(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_owner(conn, B)
    dead = raiser(RuntimeError("EVE SSO rejected character 2001's refresh token — re-authenticate"))
    patch_sales(
        monkeypatch, conn,
        corp_orders={A: dead, B: [order(1)]}, corp_history={A: dead, B: []},
        corp_contracts={A: dead, B: []},
        corp_tx={(B, d): [] for d in range(1, 8)} | {(A, 1): dead},
        char_orders={A: dead}, char_history={A: dead}, char_tx={A: dead}, char_contracts={A: dead},
    )
    ledger.pull_sales(conn, ref)
    for family in ("orders", "contracts"):
        assert status_of(conn, "corporation", PLAYER_CORP, family)["via_character_id"] == B
    assert status_of(conn, "corporation", PLAYER_CORP, "transactions", 1)["via_character_id"] == B
    own = status_of(conn, "character", A, "orders")
    assert own["status"] == "error" and "re-authenticate" in own["message"]


def test_corp_family_all_403_records_no_role_others_still_pull(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_owner(conn, B)
    calls = patch_sales(monkeypatch, conn, corp_orders={A: None, B: None},
                        corp_contracts={A: [contract(7, for_corp=True)]},
                        corp_items={(A, 7): [citem(1)]}, corp_tx={(A, 1): None, (B, 1): None})
    ledger.pull_sales(conn, ref)
    orders = status_of(conn, "corporation", PLAYER_CORP, "orders")
    assert orders["status"] == "no_role" and "Accountant or Trader" in orders["message"]
    rows = pull_rows(conn, "transactions", PLAYER_CORP)
    assert [r["division"] for r in rows] == list(range(1, 8))
    assert all(r["status"] == "no_role" and "Junior Accountant" in r["message"] for r in rows)
    # The wallet role is corp-wide: divisions 2..7 were stamped, never probed.
    assert sorted(c for c in calls if c[0] == "corp_tx") == [("corp_tx", A, 1, None), ("corp_tx", B, 1, None)]
    assert status_of(conn, "corporation", PLAYER_CORP, "contracts")["status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM sale_contract").fetchone()[0] == 1


def test_owner_toggle_off_writes_off_keeps_data_and_read_side_excludes(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_corp(conn)
    add_pipeline(conn)
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5, is_personal=False), tx(4)]})
    ledger.pull_sales(conn, ref)
    assert len(tx_rows(conn)) == 2
    conn.execute("UPDATE esi_corp SET count_sales = 0")
    conn.commit()
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5, is_personal=False), tx(4)]})
    ledger.pull_sales(conn, ref)
    assert status_of(conn, "corporation", PLAYER_CORP, "orders")["status"] == "off"
    assert len(tx_rows(conn)) == 2
    view = ledger.build_view(conn, ref, store.get_settings(conn), "30", NOW_DT)
    hulk = next(p for p in view["products"] if p.type_id == HULK)
    assert hulk.units_sold == 1  # the corp's sale (via the character feed) no longer counts
    assert any("sales off" in n for n in view["notes"])
    conn.execute("UPDATE pool_character SET count_sales = 0")
    conn.commit()
    ledger.pull_sales(conn, ref)
    assert status_of(conn, "character", A, "transactions")["status"] == "off"
    assert len(tx_rows(conn)) == 2


def test_rate_limited_group_stops_only_that_group(conn, ref, monkeypatch):
    add_owner(conn, A)
    limited = raiser(http_error(429, {"Retry-After": "600"}))
    corp_tx = {(A, 1): [tx(11)], (A, 2): limited}
    corp_tx.update({(A, d): [tx(10 + d)] for d in range(3, 8)})
    calls = patch_sales(monkeypatch, conn, corp_tx=corp_tx,
                        corp_contracts={A: [contract(7, for_corp=True)]}, corp_items={(A, 7): [citem(1)]},
                        char_tx={A: [tx(99)]})
    ledger.pull_sales(conn, ref)
    rows = {r["division"]: r for r in pull_rows(conn, "transactions", PLAYER_CORP)}
    assert rows[1]["status"] == "ok"
    assert rows[2]["status"] == "skipped" and "retry after 10 min" in rows[2]["message"]
    assert all(rows[d]["status"] == "skipped" for d in range(3, 8))
    assert not [c for c in calls if c[0] == "corp_tx" and c[2] >= 3]
    assert status_of(conn, "corporation", PLAYER_CORP, "contracts")["status"] == "ok"
    # The character wallet is a different rate group and still pulls.
    assert status_of(conn, "character", A, "transactions")["status"] == "ok"


def test_group_budget_preempts_and_marks_partial(conn, ref, monkeypatch):
    add_owner(conn, A)
    monkeypatch.setattr(config, "LEDGER_RATE_BUDGET_FRACTION", 0.01)  # corp-wallet: 3 calls
    calls = patch_sales(monkeypatch, conn, corp_tx={(A, d): [tx(10 + d)] for d in range(1, 8)})
    ledger.pull_sales(conn, ref)
    rows = {r["division"]: r for r in pull_rows(conn, "transactions", PLAYER_CORP)}
    # 3 calls: division 1 costs two (its page, then the empty page that ends
    # history); division 2's first page is the third and its second call is
    # refused, so its cursor is written without backfilled; 3..7 never call.
    assert rows[1]["status"] == "ok"
    assert rows[2]["status"] == "partial" and "budget" in rows[2]["message"]
    assert (rows[2]["newest_id"], rows[2]["backfilled"]) == (12, 0)
    assert all(rows[d]["status"] == "partial" and "budget" in rows[d]["message"] for d in range(3, 8))
    assert [c for c in calls if c[0] == "corp_tx"] == [
        ("corp_tx", A, 1, None), ("corp_tx", A, 1, 11), ("corp_tx", A, 2, None)]


def test_5xx_or_transport_error_marks_rest_skipped_and_never_raises(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_corp(conn)  # the snapshot refresh always records the corp before the sales step
    patch_sales(monkeypatch, conn, corp_orders={A: [order(1)]}, corp_history={A: []},
                corp_tx={(A, d): [] for d in range(1, 8)}, corp_contracts={A: []},
                char_orders={A: raiser(http_error(502))}, char_tx={A: [tx(1)]})
    ledger.pull_sales(conn, ref)  # must not raise
    assert status_of(conn, "corporation", PLAYER_CORP, "orders")["status"] == "ok"
    own = status_of(conn, "character", A, "orders")
    assert own["status"] == "skipped" and "502" in own["message"]
    assert status_of(conn, "character", A, "transactions")["status"] == "skipped"
    assert tx_rows(conn) == []
    # A transport error while resolving corporations: everything skipped.
    patch_sales(monkeypatch, conn, corp_of={A: raiser(httpx.ConnectError("down"))}, char_tx={A: [tx(1)]})
    ledger.pull_sales(conn, ref)
    assert all(r["status"] == "skipped" for r in pull_rows(conn))
    # A non-transport failure there: membership unknown → orders/transactions
    # wait with an error (no fetch, cursor untouched); contracts still run.
    patch_sales(monkeypatch, conn, corp_of={A: raiser(http_error(404))}, char_tx={A: [tx(1)]},
                char_contracts={A: []})
    ledger.pull_sales(conn, ref)
    own = status_of(conn, "character", A, "transactions")
    assert own["status"] == "error" and "membership unknown" in own["message"]
    assert status_of(conn, "character", A, "contracts")["status"] == "ok"
    assert tx_rows(conn) == []


def test_membership_lookup_5xx_is_esi_down_and_4xx_keeps_corp_families_honest(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_corp(conn)
    patch_sales(monkeypatch, conn, corp_of={A: raiser(http_error(503))}, char_tx={A: [tx(1)]})
    ledger.pull_sales(conn, ref)
    assert all(r["status"] == "skipped" for r in pull_rows(conn))
    patch_sales(monkeypatch, conn, corp_of={A: raiser(http_error(404))}, char_contracts={A: []})
    ledger.pull_sales(conn, ref)
    corp_orders = status_of(conn, "corporation", PLAYER_CORP, "orders")
    assert corp_orders["status"] == "error" and "membership unknown" in corp_orders["message"]
    assert status_of(conn, "character", A, "contracts")["status"] == "ok"


def test_mark_skipped_and_summary_respect_off_owners_and_removed_characters(conn, ref, monkeypatch):
    add_owner(conn, A, count_sales=0)
    add_owner(conn, B)
    add_corp(conn, count_sales=0)
    ledger.mark_skipped(conn, "ESI unavailable — sales pull skipped")
    assert all(r["status"] == "off" for r in pull_rows(conn, owner_id=A))
    assert all(r["status"] == "off" for r in pull_rows(conn, owner_id=PLAYER_CORP))
    assert all(r["status"] == "skipped" for r in pull_rows(conn, owner_id=B))
    # A removed character's stale rows no longer count as degraded feeds.
    patch_sales(monkeypatch, conn, char_tx={B: raiser(http_error(400))})
    summary = ledger.pull_sales(conn, ref)
    assert summary.degraded_feeds == 1
    conn.execute("DELETE FROM pool_character WHERE character_id = ?", (B,))
    conn.execute("DELETE FROM esi_token WHERE character_id = ?", (B,))
    conn.commit()
    patch_sales(monkeypatch, conn)
    summary = ledger.pull_sales(conn, ref)
    assert summary.degraded_feeds == 0
    add_pipeline(conn)
    fake_basis(monkeypatch, {})
    v = view(conn, ref)
    assert not [n for n in v["notes"] if "char 2002" in n]
    assert any("Count sales is off for every character and corporation" in n for n in v["notes"])
    assert v["sales_at"] == NOW


def test_pull_never_deletes(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5), tx(4)]}, char_orders={A: [order(1), order(2)]},
                char_contracts={A: [contract(7), contract(8)]}, char_items={(A, 7): [citem(1)], (A, 8): [citem(1)]})
    ledger.pull_sales(conn, ref)
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5)]}, char_orders={A: []}, char_contracts={A: []})
    ledger.pull_sales(conn, ref)
    patch_sales(monkeypatch, conn)
    ledger.pull_sales(conn, ref)
    assert len(tx_rows(conn)) == 2
    assert conn.execute("SELECT COUNT(*) FROM sale_order").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sale_contract").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sale_contract_item").fetchone()[0] == 2


# --- orders --------------------------------------------------------------------


def test_orders_optional_is_buy_order_and_state_verbatim(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn,
                char_orders={A: [order(1, is_buy=None), order(2, is_buy=True)]},
                char_history={A: [order(3, is_buy=None, state="vanished", remain=1)]})
    ledger.pull_sales(conn, ref)
    rows = {r["order_id"]: r for r in conn.execute("SELECT * FROM sale_order")}
    assert set(rows) == {1, 3}
    assert rows[1]["state"] == "open" and rows[3]["state"] == "vanished"
    assert ledger.order_outcome("vanished", 5, 1) == "vanished"


def test_open_orders_upserted_history_closes_and_never_reopens(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_orders={A: [order(1, price=310e6, remain=5)]})
    ledger.pull_sales(conn, ref)
    patch_sales(monkeypatch, conn, char_orders={A: [order(1, price=305e6, remain=3, issued="2026-09-02T00:00:00Z")]})
    ledger.pull_sales(conn, ref)
    row = conn.execute("SELECT * FROM sale_order WHERE order_id = 1").fetchone()
    assert (row["price"], row["volume_remain"], row["issued"], row["state"]) == (305e6, 3, "2026-09-02T00:00:00Z", "open")
    assert row["first_seen_at"] == NOW and row["history_seen_at"] is None
    # Closed by history with the expiry still in the future → estimated close = now.
    patch_sales(monkeypatch, conn, char_history={A: [order(1, price=305e6, remain=0, state="expired",
                                                             issued="2026-09-02T00:00:00Z", duration=90)]})
    ledger.pull_sales(conn, ref)
    row = conn.execute("SELECT * FROM sale_order WHERE order_id = 1").fetchone()
    assert row["state"] == "expired" and row["volume_remain"] == 0 and row["history_seen_at"] == NOW
    assert ledger.order_outcome(row["state"], row["volume_total"], row["volume_remain"]) == "filled"
    # A stale open listing (1200 s cache) ALONE must not reopen or
    # overwrite it — the history feed says nothing this pull.
    patch_sales(monkeypatch, conn, char_orders={A: [order(1, price=999e6, remain=3, issued="2026-09-03T00:00:00Z",
                                                          duration=7)]},
                char_history={A: []})
    ledger.pull_sales(conn, ref)
    row = conn.execute("SELECT * FROM sale_order WHERE order_id = 1").fetchone()
    assert (row["state"], row["price"], row["volume_remain"], row["issued"], row["duration"]) == (
        "expired", 305e6, 0, "2026-09-02T00:00:00Z", 90)
    assert row["history_seen_at"] == NOW and row["missing_since"] is None
    # An order first seen already closed and long past expiry: close = expiry.
    patch_sales(monkeypatch, conn, char_history={A: [order(2, remain=0, state="expired",
                                                             issued="2026-05-01T00:00:00Z", duration=30)]})
    ledger.pull_sales(conn, ref)
    row = conn.execute("SELECT * FROM sale_order WHERE order_id = 2").fetchone()
    assert row["history_seen_at"] == "2026-05-31T00:00:00Z"


def test_reconcile_only_for_ok_feeds_and_clears_on_sighting(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_orders={A: [order(1)]})
    ledger.pull_sales(conn, ref)
    patch_sales(monkeypatch, conn, char_orders={A: []}, char_history={A: []})
    ledger.pull_sales(conn, ref)
    assert conn.execute("SELECT missing_since FROM sale_order WHERE order_id = 1").fetchone()[0] == NOW
    patch_sales(monkeypatch, conn, char_orders={A: [order(1)]})
    ledger.pull_sales(conn, ref)
    assert conn.execute("SELECT missing_since FROM sale_order WHERE order_id = 1").fetchone()[0] is None
    # ESI down: the owner's feeds are skipped, its open rows untouched.
    patch_sales(monkeypatch, conn, char_orders={A: raiser(httpx.ConnectError("down"))})
    ledger.pull_sales(conn, ref)
    assert conn.execute("SELECT missing_since FROM sale_order WHERE order_id = 1").fetchone()[0] is None


def test_char_feed_corp_order_owned_by_corp(conn, ref, monkeypatch):
    add_owner(conn, A)
    patch_sales(monkeypatch, conn, char_orders={A: [order(1, is_corporation=True), order(2)]})
    ledger.pull_sales(conn, ref)
    rows = {r["order_id"]: r for r in conn.execute("SELECT * FROM sale_order")}
    assert (rows[1]["owner_kind"], rows[1]["owner_id"], rows[1]["issued_by"]) == ("corporation", PLAYER_CORP, A)
    assert (rows[2]["owner_kind"], rows[2]["owner_id"]) == ("character", A)


# --- contracts -------------------------------------------------------------------


def test_contracts_issuer_side_only_with_owner_rule(conn, ref, monkeypatch):
    add_owner(conn, A)
    listing = [
        contract(1, issuer=A),                                   # personal sale
        contract(2, issuer=A, for_corp=True, corp=OTHER_CORP),   # issued for a corp (exact, no corp_of guess)
        contract(3, issuer=BUYER, acceptor=A),                   # we accepted: a purchase, dropped
        contract(4, issuer=A, type="courier", price=0),          # stored, invisible to sales
    ]
    corp_listing = [
        contract(5, issuer=B, for_corp=True, corp=PLAYER_CORP),
        contract(6, issuer=BUYER, corp=OTHER_CORP, for_corp=True, acceptor=A),  # assigned to us
    ]
    patch_sales(monkeypatch, conn, char_contracts={A: listing}, corp_contracts={A: corp_listing},
                char_items={(A, 1): [citem(1)], (A, 2): [citem(1)]}, corp_items={(A, 5): [citem(1)]})
    ledger.pull_sales(conn, ref)
    rows = {r["contract_id"]: r for r in conn.execute("SELECT * FROM sale_contract")}
    assert set(rows) == {1, 2, 4, 5}
    assert (rows[1]["owner_kind"], rows[1]["owner_id"]) == ("character", A)
    assert (rows[2]["owner_kind"], rows[2]["owner_id"]) == ("corporation", OTHER_CORP)
    assert (rows[5]["owner_kind"], rows[5]["owner_id"], rows[5]["via_character_id"]) == ("corporation", PLAYER_CORP, A)
    assert rows[4]["items_status"] is None  # couriers never fetch items
    assert rows[1]["items_status"] == "ok" and rows[1]["items_fetched_at"] == NOW


def test_contract_items_token_chosen_at_fetch_time_after_character_delete(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_owner(conn, B)
    monkeypatch.setattr(config, "LEDGER_CONTRACT_ITEMS_PER_REFRESH", 0)
    patch_sales(monkeypatch, conn, corp_contracts={A: [contract(7, for_corp=True)], B: None})
    ledger.pull_sales(conn, ref)
    assert status_of(conn, "corporation", PLAYER_CORP, "contracts")["status"] == "partial"
    conn.execute("DELETE FROM pool_character WHERE character_id = ?", (A,))
    conn.execute("DELETE FROM esi_token WHERE character_id = ?", (A,))
    conn.commit()
    monkeypatch.setattr(config, "LEDGER_CONTRACT_ITEMS_PER_REFRESH", 200)
    patch_sales(monkeypatch, conn, corp_contracts={B: [contract(7, for_corp=True)]}, corp_items={(B, 7): [citem(1)]})
    ledger.pull_sales(conn, ref)
    row = conn.execute("SELECT * FROM sale_contract WHERE contract_id = 7").fetchone()
    assert row["items_status"] == "ok"
    assert status_of(conn, "corporation", PLAYER_CORP, "contracts")["status"] == "ok"


def test_contract_items_404_missing_403_next_candidate_attempts_cap(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_owner(conn, B)
    listing = [contract(1, for_corp=True), contract(2, for_corp=True), contract(3, for_corp=True)]
    forbidden = raiser(http_error(403))
    bad = raiser(http_error(400))
    feeds = dict(
        corp_contracts={A: listing},
        corp_items={(A, 1): None, (A, 2): forbidden, (B, 2): [citem(1)], (A, 3): bad},
    )
    calls = patch_sales(monkeypatch, conn, **feeds)
    ledger.pull_sales(conn, ref)
    rows = {r["contract_id"]: r for r in conn.execute("SELECT * FROM sale_contract")}
    assert rows[1]["items_status"] == "missing"
    assert rows[2]["items_status"] == "ok" and ("corp_items", B, 2) in calls
    assert rows[3]["items_status"] is None and rows[3]["items_attempts"] == 1
    for _ in range(2):
        patch_sales(monkeypatch, conn, **feeds)
        ledger.pull_sales(conn, ref)
    row = conn.execute("SELECT * FROM sale_contract WHERE contract_id = 3").fetchone()
    assert row["items_status"] == "missing" and row["items_attempts"] == 3
    calls = patch_sales(monkeypatch, conn, **feeds)
    ledger.pull_sales(conn, ref)
    assert ("corp_items", A, 3) not in calls  # written off: no fourth call


def test_contract_items_429_stops_group_and_charges_no_attempt(conn, ref, monkeypatch):
    add_owner(conn, A)
    limited = raiser(http_error(429, {"Retry-After": "900"}))
    listing = [contract(1), contract(2)]
    calls = patch_sales(monkeypatch, conn, char_contracts={A: listing},
                        char_items={(A, 1): limited, (A, 2): [citem(1)]})
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "contracts")
    assert row["status"] == "partial" and "rate limited" in row["message"]
    rows = {r["contract_id"]: r for r in conn.execute("SELECT * FROM sale_contract")}
    assert rows[1]["items_attempts"] == 0 and rows[1]["items_status"] is None
    assert rows[2]["items_status"] is None  # the group stopped before it
    assert [c for c in calls if c[0] == "char_items"] == [("char_items", A, 1)]
    # Every token refused: the contract is 'unavailable', not retried forever.
    add_owner(conn, B)
    forbidden = raiser(http_error(403))
    calls = patch_sales(monkeypatch, conn, corp_contracts={A: [contract(9, for_corp=True)]},
                        corp_items={(A, 9): forbidden, (B, 9): forbidden})
    ledger.pull_sales(conn, ref)
    row = conn.execute("SELECT * FROM sale_contract WHERE contract_id = 9").fetchone()
    assert row["items_status"] == "unavailable" and row["items_fetched_at"] == NOW
    assert [c for c in calls if c[0] == "corp_items"] == [("corp_items", A, 9), ("corp_items", B, 9)]
    calls = patch_sales(monkeypatch, conn, corp_contracts={A: [contract(9, for_corp=True)]},
                        corp_items={(A, 9): forbidden, (B, 9): forbidden})
    ledger.pull_sales(conn, ref)
    assert not [c for c in calls if c[0] == "corp_items"]


def test_contract_items_budget_leaves_partial_then_drains(conn, ref, monkeypatch):
    add_owner(conn, A)
    monkeypatch.setattr(config, "LEDGER_CONTRACT_ITEMS_PER_REFRESH", 1)
    listing = [contract(1, status="outstanding", date_completed=None), contract(2)]
    feeds = dict(char_contracts={A: listing}, char_items={(A, 1): [citem(1)], (A, 2): [citem(1)]})
    calls = patch_sales(monkeypatch, conn, **feeds)
    ledger.pull_sales(conn, ref)
    row = status_of(conn, "character", A, "contracts")
    assert row["status"] == "partial" and "1 contracts' items still to fetch" in row["message"]
    assert [c for c in calls if c[0] == "char_items"] == [("char_items", A, 2)]  # finished first
    patch_sales(monkeypatch, conn, **feeds)
    ledger.pull_sales(conn, ref)
    assert status_of(conn, "character", A, "contracts")["status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM sale_contract WHERE items_status = 'ok'").fetchone()[0] == 2


def test_mark_skipped_enumerates_and_preserves_cursor(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_corp(conn)
    patch_sales(monkeypatch, conn, char_tx={A: [tx(5)]})
    ledger.pull_sales(conn, ref)
    ledger.mark_skipped(conn, "ESI unavailable — sales pull skipped")
    rows = pull_rows(conn)
    assert all(r["status"] == "skipped" for r in rows)
    assert len([r for r in rows if r["owner_kind"] == "corporation" and r["family"] == "transactions"]) == 7
    cur = status_of(conn, "character", A, "transactions")
    assert (cur["oldest_id"], cur["newest_id"], cur["backfilled"]) == (5, 5, 1)


def test_summary_line_wording():
    line = ledger.summary_line(ledger.PullSummary(14, 2, 6, 3))
    assert line == "sales: 14 new sales, 2 contracts finished, 6 open orders (all products) (3 feeds partial — see Ledger)"
    assert ledger.summary_line(ledger.PullSummary(skipped=True)).startswith("sales pull skipped")


# --- read side --------------------------------------------------------------------


class FakeCost:
    def __init__(self, total, missing=0, clamped=False):
        self.lines = []
        self._total = total
        self.missing_prices = missing
        self.spin_up = clamped

    @property
    def total(self):
        return self._total


def seed_run(conn, run_number, pipeline_id, type_id=HULK, qty=8, status="complete"):
    cur = conn.execute(
        "INSERT INTO index_run (run_number, planned_start, planned_end, status, completed_at) "
        "VALUES (?, '2026-09-01', '2026-09-08', ?, ?)",
        (run_number, status, f"2026-09-0{run_number}T00:00:00Z" if status == "complete" else None),
    )
    run_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id, on_hand_qty, in_progress_qty, "
        "target_stock_qty, deficit_qty, recommended_action, depth) VALUES (?, ?, 0, 0, ?, ?, 'build', 0)",
        (run_id, type_id, qty, qty),
    )
    item_id = cur.lastrowid
    if qty:
        conn.execute(
            "INSERT INTO index_run_item_pipeline (index_run_item_id, pipeline_id, qty_attributable, depth) "
            "VALUES (?, ?, ?, 0)", (item_id, pipeline_id, qty),
        )
    conn.commit()
    return run_id


def seed_tx(conn, tid, type_id=HULK, qty=1, price=300e6, date="2026-09-06T10:00:00Z",
            owner=("character", A), client_id=BUYER, feed="character", division=None):
    conn.execute(
        "INSERT INTO sale_transaction (transaction_id, owner_kind, owner_id, division, source_feed, "
        "type_id, quantity, unit_price, date, location_id, client_id, journal_ref_id, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (tid, owner[0], owner[1], division, feed, type_id, qty, price, date, STATION, client_id, NOW),
    )
    conn.commit()


def seed_contract(conn, contract_id, items, price=350e6, owner=("character", A), status="finished",
                  acceptor=BUYER, type="item_exchange", date_completed="2026-09-05T00:00:00Z",
                  items_status="ok"):
    conn.execute(
        "INSERT INTO sale_contract (contract_id, owner_kind, owner_id, issuer_id, issuer_corporation_id, "
        "for_corporation, acceptor_id, type, status, price, date_issued, date_completed, date_expired, "
        "start_location_id, first_seen_at, last_seen_at, items_fetched_at, items_status) VALUES "
        "(?, ?, ?, ?, ?, 0, ?, ?, ?, ?, '2026-09-01T00:00:00Z', ?, '2026-09-15T00:00:00Z', ?, ?, ?, ?, ?)",
        (contract_id, owner[0], owner[1], A, PLAYER_CORP, acceptor, type, status, price, date_completed,
         STATION, NOW, NOW, NOW if items_status else None, items_status),
    )
    conn.executemany(
        "INSERT INTO sale_contract_item (contract_id, record_id, type_id, quantity, raw_quantity, "
        "is_included, is_singleton) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(contract_id, i["record_id"], i["type_id"], i["quantity"], i["raw_quantity"],
          1 if i["is_included"] else 0, 1 if i["is_singleton"] else 0) for i in items],
    )
    conn.commit()


def seed_quote(conn, type_id, price, region=10000002, source="sell"):
    conn.execute(
        "INSERT OR REPLACE INTO market_price (type_id, region_id, source, price, fetched_at, hub) "
        "VALUES (?, ?, ?, ?, ?, 1)", (type_id, region, source, price, "2026-09-07 10:00:00"),
    )
    conn.commit()


def fake_basis(monkeypatch, totals):
    """hull_cost stand-in keyed by pipeline_id; records the run asked for."""
    asked = []

    def fake(conn, ref, settings, index_run_id, pipeline_id):
        asked.append((index_run_id, pipeline_id))
        total = totals.get(pipeline_id)
        return None if total is None else FakeCost(total)

    monkeypatch.setattr(ledger.costing, "hull_cost", fake)
    return asked


def view(conn, ref, window="30", now=NOW_DT):
    return ledger.build_view(conn, ref, store.get_settings(conn), window, now)


def net_of(conn, ref, type_id, price, qty=1):
    settings = store.get_settings(conn)
    return costing.net_proceeds_per_hull(
        price, ref.type_info(type_id).freight_volume, settings,
        capital=costing.is_capital_priced(ref, type_id),
        freight_exempt=costing.freight_out_exempt(type_id),
    ) * qty


CJ6 = 1049588174021  # the C-J6MT Keepstar (config.CJ6_KEEPSTAR_STRUCTURE_ID)
KEEPSTAR = 35834     # freight-exempt XL hull (config.FREIGHT_OUT_EXEMPT_TYPES)


def test_net_follows_where_the_hull_sold(conn, ref, monkeypatch):
    """Fees and freight come from the sale's own location: an NPC station
    pays the hub rates + freight-out per m³, a structure the null-sec
    market rates + the structure leg per m³ (capitals: the flat movement
    cost); a contract with no location keeps the class rule."""
    add_owner(conn, A)
    pid = add_pipeline(conn)
    add_pipeline(conn, THANATOS)
    add_pipeline(conn, KEEPSTAR)
    conn.execute(
        "UPDATE settings SET freight_out_isk_per_m3 = 750, structure_freight_in_isk_per_m3 = 600, "
        "capital_movement_cost_isk = 25e6, capital_sales_tax = 0.03, capital_broker_rate = 0.02, "
        "capital_scc_surcharge = 0.015 WHERE id = 1"
    )
    conn.commit()
    fake_basis(monkeypatch, {pid: 200e6})
    settings = store.get_settings(conn)
    hulk_m3 = ref.type_info(HULK).freight_volume
    than_m3 = ref.type_info(THANATOS).freight_volume
    hub_rate = 1 - costing.sales_tax_rate(settings) - costing.broker_fee_rate(settings) - 0.015
    structure_rate = 1 - 0.03 - 0.02 - 0.015
    seed_tx(conn, 1, HULK, price=300e6)                       # NPC station
    seed_tx(conn, 2, HULK, price=300e6)
    conn.execute("UPDATE sale_transaction SET location_id = ? WHERE transaction_id = 2", (CJ6,))
    seed_tx(conn, 3, THANATOS, price=2.5e9)                   # capital at an NPC station
    seed_tx(conn, 4, THANATOS, price=2.5e9)
    conn.execute("UPDATE sale_transaction SET location_id = ? WHERE transaction_id = 4", (CJ6,))
    seed_contract(conn, 5, [citem(1)], price=350e6)          # contract at the station
    seed_contract(conn, 6, [citem(1)], price=350e6)
    conn.execute("UPDATE sale_contract SET start_location_id = NULL WHERE contract_id = 6")
    seed_tx(conn, 7, KEEPSTAR, price=200e9)                    # freight-exempt XL hull, station
    seed_tx(conn, 8, KEEPSTAR, price=200e9)
    conn.execute("UPDATE sale_transaction SET location_id = ? WHERE transaction_id = 8", (CJ6,))
    conn.commit()
    rows = {s.ref_id: s for s in view(conn, ref)["sales"]}
    assert rows[1].net == pytest.approx(300e6 * hub_rate - hulk_m3 * 750)
    assert rows[2].net == pytest.approx(300e6 * structure_rate - hulk_m3 * 600)
    assert rows[3].net == pytest.approx(2.5e9 * hub_rate - 25e6)         # capitals fly: no per-m³ term
    assert rows[4].net == pytest.approx(2.5e9 * structure_rate - 25e6)
    assert rows[5].net == pytest.approx(350e6 * hub_rate - hulk_m3 * 750)
    assert rows[6].net == pytest.approx(net_of(conn, ref, HULK, 350e6))  # class rule
    assert rows[7].net == pytest.approx(200e9 * hub_rate)                 # exempt: no freight either way
    assert rows[8].net == pytest.approx(200e9 * structure_rate)
    assert (rows[1].venue, rows[2].venue, rows[6].venue) == ("hub", "structure", None)
    assert rows[2].venue_label == "a structure (the C-J6 rates)"
    assert rows[1].venue_label == "an NPC station (the high-sec hub rates)"
    assert rows[6].venue_label.startswith("no location on record")
    # Product figures aggregate the per-sale nets.
    hulk = next(p for p in view(conn, ref)["products"] if p.type_id == HULK)
    assert hulk.net == pytest.approx(rows[1].net + rows[2].net + rows[5].net + rows[6].net)
    assert costing.sale_venue(60003760) == "hub" and costing.sale_venue(CJ6) == "structure"
    assert costing.sale_venue(None) is None


def test_order_outcome_classification():
    assert ledger.order_outcome("open", 5, 5) == "open"
    assert ledger.order_outcome("expired", 5, 0) == "filled"
    assert ledger.order_outcome("expired", 5, 2) == "partial"
    assert ledger.order_outcome("cancelled", 5, 2) == "partial"
    assert ledger.order_outcome("expired", 5, 5) == "expired"
    assert ledger.order_outcome("cancelled", 5, 5) == "cancelled"
    assert ledger.order_outcome("cancelled", 5, 0) == "filled"  # nothing remained, whatever closed it


def test_attribute_contract_rules():
    finals = {HULK, MACKINAW}
    quotes = {HULK: 300e6, MACKINAW: 200e6}
    # Two singleton records of one final aggregate; unit price = price / units.
    a = ledger.attribute_contract(700e6, [citem(1), citem(2)], quotes, finals)
    assert a.clean and a.per_final == {HULK: (2, 350e6)} and not a.flags
    # Two finals: pro rata by quote × qty.
    a = ledger.attribute_contract(1000e6, [citem(1, HULK, 2, singleton=False), citem(2, MACKINAW)], quotes, finals)
    hulk_qty, hulk_price = a.per_final[HULK]
    mack_qty, mack_price = a.per_final[MACKINAW]
    assert hulk_qty == 2 and mack_qty == 1
    assert hulk_price * 2 + mack_price == pytest.approx(1000e6)
    assert hulk_price / mack_price == pytest.approx(300 / 200)
    # A missing quote → equal per unit, flagged.
    a = ledger.attribute_contract(900e6, [citem(1, HULK, 2, singleton=False), citem(2, MACKINAW)], {}, finals)
    assert a.per_final == {HULK: (2, 300e6), MACKINAW: (1, 300e6)} and "estimated split" in a.flags and a.clean
    # Mixed (a non-final included): units only.
    a = ledger.attribute_contract(2300e6, [citem(1), citem(2, ORCA)], quotes, finals)
    assert a.per_final == {HULK: (1, None)} and a.flags == {"mixed"} and not a.clean and a.others == [(ORCA, 1)]
    # Swap (an item asked back): units only.
    a = ledger.attribute_contract(100e6, [citem(1), citem(2, MACKINAW, included=False)], quotes, finals)
    assert a.per_final == {HULK: (1, None)} and a.flags == {"swap"} and a.asked == [(MACKINAW, 1)]
    # No final inside: nothing.
    assert ledger.attribute_contract(1e6, [citem(1, ORCA)], quotes, finals).per_final == {}
    # No price: units only even when clean.
    assert ledger.attribute_contract(None, [citem(1)], quotes, finals).per_final == {HULK: (1, None)}


def test_units_sold_come_from_transactions_not_orders(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    seed_run(conn, 1, pid)
    fake_basis(monkeypatch, {pid: 200e6})
    seed_tx(conn, 1, qty=1, price=300e6)
    seed_tx(conn, 2, qty=1, price=310e6)
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at, history_seen_at) "
        "VALUES (9, 'character', ?, 'character', ?, 305e6, 2, 0, ?, 90, '2026-09-01T00:00:00Z', 'expired', ?, ?, ?)",
        (A, HULK, STATION, NOW, NOW, NOW),
    )
    conn.commit()
    v = view(conn, ref)
    hulk = next(p for p in v["products"] if p.type_id == HULK)
    assert hulk.units_sold == 2 and hulk.revenue == pytest.approx(610e6)
    assert hulk.net == pytest.approx(net_of(conn, ref, HULK, 300e6) + net_of(conn, ref, HULK, 310e6))
    assert hulk.cost_of_units == pytest.approx(400e6)
    assert hulk.profit == pytest.approx(hulk.net - 400e6)
    assert hulk.margin_pct == pytest.approx(hulk.profit / 400e6 * 100)
    assert [h.outcome for h in v["history"]] == ["filled"]
    assert v["totals"].units == 2 and v["totals"].revenue == pytest.approx(610e6)


def test_no_fallback_when_transactions_degraded(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    seed_run(conn, 1, pid)
    fake_basis(monkeypatch, {pid: 200e6})
    conn.execute(
        "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, message, pulled_at) "
        "VALUES ('character', ?, 'transactions', 0, 'no_role', 'no role', ?)", (A, NOW),
    )
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at, history_seen_at) "
        "VALUES (9, 'character', ?, 'character', ?, 305e6, 2, 0, ?, 90, '2026-09-01T00:00:00Z', 'expired', ?, ?, ?)",
        (A, HULK, STATION, NOW, NOW, NOW),
    )
    conn.commit()
    v = view(conn, ref)
    assert v["products"] == [] and v["totals"].units == 0
    assert [h.outcome for h in v["history"]] == ["filled"]
    assert any("wallet: no_role" in n for n in v["notes"])


def test_feed_overlap_suspects_tripwire_is_window_scoped(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_pipeline(conn)
    fake_basis(monkeypatch, {})
    same = dict(type_id=HULK, qty=1, price=300e6, date="2026-09-06T10:00:00Z", owner=("corporation", PLAYER_CORP))
    seed_tx(conn, 1, feed="character", **same)
    seed_tx(conn, 2, feed="corporation", **same)
    old = dict(same, date="2026-07-01T10:00:00Z")
    seed_tx(conn, 3, feed="character", **old)
    seed_tx(conn, 4, feed="corporation", **old)
    seed_tx(conn, 5, feed="character", date="2026-09-05T10:00:00Z")   # same feed twice: not a suspect
    seed_tx(conn, 6, feed="character", date="2026-09-05T10:00:00Z")
    assert [n for n in view(conn, ref)["notes"] if "two ids" in n] == ["1 sales appear under two ids — verify feed identity"]
    assert [n for n in view(conn, ref, "all")["notes"] if "two ids" in n] == ["2 sales appear under two ids — verify feed identity"]


def test_contract_mixed_and_swap_count_units_only(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    add_pipeline(conn, MACKINAW)
    seed_run(conn, 1, pid)
    fake_basis(monkeypatch, {pid: 200e6})
    seed_quote(conn, HULK, 300e6)
    seed_contract(conn, 1, [citem(1), citem(2, ORCA)], price=2300e6)              # bundle
    seed_contract(conn, 2, [citem(1), citem(2, MACKINAW, included=False)], price=10e6)  # trade
    seed_contract(conn, 3, [citem(1)], price=350e6)                               # clean
    v = view(conn, ref)
    hulk = next(p for p in v["products"] if p.type_id == HULK)
    assert hulk.units_sold == 3 and hulk.units_priced == 1 and hulk.unpriced_units == 2
    assert hulk.revenue == pytest.approx(350e6)
    assert hulk.contract_units == 3
    assert any(b[0] == "2 not priced" for b in hulk.badges)
    assert hulk.profit == pytest.approx(net_of(conn, ref, HULK, 350e6) - 200e6)
    # Remove the clean contract: only unpriced units remain → no profit at
    # all, no Top-10 by profit, and the strip counts a basis, not a gap.
    conn.execute("DELETE FROM sale_contract_item WHERE contract_id = 3")
    conn.execute("DELETE FROM sale_contract WHERE contract_id = 3")
    conn.commit()
    v2 = view(conn, ref)
    only = next(p for p in v2["products"] if p.type_id == HULK)
    assert only.units_sold == 2 and only.units_priced == 0
    assert only.cost_of_units is None and only.profit is None and only.margin_pct is None
    assert only not in v2["top"]["profit"] and only not in v2["top"]["margin"]
    assert v2["totals"].no_basis == 0 and v2["totals"].unpriced_contracts == 2
    rows = {s.ref_id: s for s in v["sales"]}
    assert "mixed" in rows[1].flags and rows[1].gross is None and rows[1].contract_price == 2300e6
    assert "swap" in rows[2].flags and rows[2].priced is False
    assert "Orca" in rows[1].detail and "Mackinaw" in rows[2].detail
    assert v["totals"].unpriced_contracts == 2 and v["totals"].contracts == 3
    assert hulk in v["top"]["profit"] and hulk.profit > 0


def test_contracts_excluded_internal_no_price_no_items_auction_noted(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_owner(conn, B)
    pid = add_pipeline(conn)
    fake_basis(monkeypatch, {pid: 200e6})
    seed_contract(conn, 1, [citem(1)], acceptor=B)                    # sold to ourselves
    seed_contract(conn, 2, [citem(1)], price=None)                     # no price
    seed_contract(conn, 3, [], items_status="missing")                 # no item list
    seed_contract(conn, 4, [citem(1)], type="auction", price=1e6)      # auction
    seed_tx(conn, 5, client_id=B)                                      # internal market sale
    assert view(conn, ref)["products"] == []  # nothing counted → no row in a bounded window
    v = view(conn, ref, "all")
    hulk = next(p for p in v["products"] if p.type_id == HULK)
    assert hulk.units_sold == 0
    rows = {s.ref_id: s for s in v["sales"]}
    assert "internal" in rows[1].flags and "no price" in rows[2].flags and "internal" in rows[5].flags
    assert any("no item list" in n for n in v["notes"])
    assert any("auctions" in n for n in v["notes"])


def test_cost_basis_latest_executed_run_with_hulls(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r1 = seed_run(conn, 1, pid)
    r2 = seed_run(conn, 2, pid)
    seed_run(conn, 3, pid, qty=0)              # executed, but no attributable hulls
    seed_run(conn, 4, pid, status="planned")   # not executed
    asked = fake_basis(monkeypatch, {pid: 200e6})
    seed_tx(conn, 1)
    v = view(conn, ref)
    assert asked == [(r2, pid)]
    assert v["bases"][HULK].run_number == 2
    conn.execute("UPDATE index_run SET status = 'planned', completed_at = NULL WHERE index_run_id = ?", (r2,))
    conn.commit()
    asked.clear()
    v = view(conn, ref)
    assert asked == [(r1, pid)] and v["bases"][HULK].run_number == 1
    # Two pipelines sharing a final: newest run; tie → lowest pipeline id.
    pid2 = add_pipeline(conn, HULK, name="second hulk line")
    r5 = seed_run(conn, 5, pid2)
    asked.clear()
    fake_basis(monkeypatch, {pid: 200e6, pid2: 250e6})
    v = view(conn, ref)
    assert v["bases"][HULK].pipeline_id == pid2 and v["bases"][HULK].unit_cost == 250e6
    item = conn.execute("SELECT index_run_item_id FROM index_run_item WHERE index_run_id = ?", (r5,)).fetchone()[0]
    conn.execute("INSERT INTO index_run_item_pipeline (index_run_item_id, pipeline_id, qty_attributable, depth) "
                 "VALUES (?, ?, 4, 0)", (item, pid))
    conn.commit()
    v = view(conn, ref)
    assert v["bases"][HULK].pipeline_id == pid and v["bases"][HULK].run_number == 5


def test_product_math_guards_and_margin_definition(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    add_pipeline(conn, THANATOS)
    add_pipeline(conn, MACKINAW)
    seed_run(conn, 1, pid)
    fake_basis(monkeypatch, {pid: 0.0})  # a basis whose lines are all unpriced: no basis
    seed_tx(conn, 1)
    seed_tx(conn, 2, THANATOS, price=2.5e9)
    v = view(conn, ref, "all")
    by = {p.type_id: p for p in v["products"]}
    assert set(by) == {HULK, THANATOS, MACKINAW}  # zero-unit row listed under 'all'
    assert by[MACKINAW].units_sold == 0 and by[MACKINAW].avg_price is None and by[MACKINAW].margin_pct is None
    assert by[HULK].unit_cost is None and by[HULK].profit is None and by[HULK].margin_pct is None
    assert any(b[0] == "no executed run" for b in by[HULK].badges)
    # A capital sold at an NPC station pays the hub rates + freight-out, not the class rule.
    assert by[THANATOS].capital and by[THANATOS].net == pytest.approx(costing.net_proceeds_at_venue(
        2.5e9, ref.type_info(THANATOS).freight_volume, store.get_settings(conn), "hub",
        capital=True, freight_exempt=False))
    assert by[THANATOS].net != pytest.approx(net_of(conn, ref, THANATOS, 2.5e9))
    assert v["totals"].no_basis == 2 and v["totals"].units == 2
    assert v["totals"].cost == 0 and v["totals"].margin_pct is None
    v30 = view(conn, ref, "30")
    assert {p.type_id for p in v30["products"]} == {HULK, THANATOS}


def test_totals_strip_reconciles(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    pid2 = add_pipeline(conn, MACKINAW)
    seed_run(conn, 1, pid)
    fake_basis(monkeypatch, {pid: 200e6})
    seed_tx(conn, 1, price=300e6)
    seed_tx(conn, 2, MACKINAW, price=150e6)  # no basis: counts in revenue/net/units, not in cost/profit
    seed_contract(conn, 3, [citem(1)], price=350e6)
    seed_contract(conn, 4, [citem(1), citem(2, MACKINAW), citem(3, ORCA)], price=2e9)  # two finals, one throw-in
    v = view(conn, ref)
    t = v["totals"]
    by = {p.type_id: p for p in v["products"]}
    assert t.revenue == pytest.approx(by[HULK].revenue + by[MACKINAW].revenue)
    assert t.net == pytest.approx(by[HULK].net + by[MACKINAW].net)
    assert t.cost == pytest.approx(by[HULK].cost_of_units)
    assert t.profit == pytest.approx(by[HULK].net - by[HULK].cost_of_units)
    assert t.margin_pct == pytest.approx(t.profit / t.cost * 100)
    assert t.units == 5 and t.products == 2 and t.no_basis == 1
    assert t.contracts == 2 and t.unpriced_contracts == 1  # one mixed contract, two finals inside


def test_unrealized_profit_counts_open_orders_and_outstanding_contracts_only(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    add_pipeline(conn, MACKINAW)                       # no executed run: no basis
    seed_run(conn, 1, pid)
    fake_basis(monkeypatch, {pid: 200e6})
    seed_quote(conn, HULK, 300e6)
    settings = store.get_settings(conn)
    conn.execute(
        "UPDATE settings SET freight_out_isk_per_m3 = 750, structure_freight_in_isk_per_m3 = 600 WHERE id = 1"
    )
    conn.commit()
    settings = store.get_settings(conn)
    m3 = ref.type_info(HULK).freight_volume

    def order_row(oid, type_id, price, total, remain, state, location):
        conn.execute(
            "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
            "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at, history_seen_at) "
            "VALUES (?, 'character', ?, 'character', ?, ?, ?, ?, ?, 90, '2026-09-01T00:00:00Z', ?, ?, ?, ?)",
            (oid, A, type_id, price, total, remain, location, state, NOW, NOW, NOW if state != "open" else None),
        )

    order_row(1, HULK, 320e6, 5, 3, "open", STATION)          # 3 hulls listed at Jita
    order_row(2, HULK, 305e6, 2, 0, "expired", STATION)       # closed: not unrealized
    order_row(3, MACKINAW, 150e6, 1, 1, "open", STATION)      # no basis: left out
    conn.commit()
    seed_contract(conn, 4, [citem(1, HULK, 2, singleton=False)], price=700e6, status="outstanding",
                  date_completed=None)
    conn.execute("UPDATE sale_contract SET start_location_id = ? WHERE contract_id = 4", (CJ6,))
    seed_contract(conn, 5, [citem(1), citem(2, ORCA)], price=2e9, status="outstanding", date_completed=None)  # mixed
    seed_contract(conn, 6, [citem(1)], price=350e6)                                                 # finished: a sale
    conn.commit()
    v = view(conn, ref)
    u = v["unrealized"]
    hub = costing.net_proceeds_at_venue(320e6, m3, settings, "hub") * 3
    structure = costing.net_proceeds_at_venue(350e6, m3, settings, "structure") * 2
    assert u.units == 5 and u.orders == 2 and u.contracts == 2
    assert u.value == pytest.approx(hub + structure)
    assert u.cost == pytest.approx(5 * 200e6)
    assert u.profit == pytest.approx(hub + structure - 1e9)
    assert u.units_unpriced == 2   # the Mackinaw order + the mixed contract's hull
    assert v["totals"].contracts == 1 and v["totals"].contracts_open == 2
    # Nothing listed: no figure, not zero.
    conn.execute("DELETE FROM sale_order"); conn.execute("DELETE FROM sale_contract_item"); conn.execute("DELETE FROM sale_contract")
    conn.commit()
    assert view(conn, ref)["unrealized"].profit is None


def test_window_calendar_aligned_and_buckets_agree(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    fake_basis(monkeypatch, {pid: 200e6})
    seed_tx(conn, 1, date="2026-09-07T11:55:00Z")   # 5 minutes ago → last bucket
    seed_tx(conn, 2, date="2026-08-09T00:00:01Z")   # first second of the 30-day window
    seed_tx(conn, 3, date="2026-08-08T23:59:59Z")   # just outside
    for key, days, buckets in (("7", 7, 7), ("30", 30, 30), ("90", 90, 13)):
        v = view(conn, ref, key)
        assert v["window"].days == days and v["charts"][0].buckets == buckets
    v = view(conn, ref, "30")
    assert v["window"].since.isoformat() == "2026-08-09"
    assert {s.ref_id for s in v["sales"]} == {1, 2}
    units = v["charts"][1]
    stacked = [s for s in units.series if s.kind == "stack"][0]
    assert stacked.bars[0].label.startswith("2026-08-09")
    assert v["charts"][0].series[0].bars[-1].label.startswith("2026-09-07")
    assert view(conn, ref, "bogus")["window"].days == 30
    assert view(conn, ref, "all")["window"].days is None
    assert {s.ref_id for s in view(conn, ref, "all")["sales"]} == {1, 2, 3}


def test_top10_orderings_and_ties(conn, ref, monkeypatch):
    add_owner(conn, A)
    pids = {t: add_pipeline(conn, t) for t in (HULK, MACKINAW, ORCA)}
    for i, t in enumerate(pids):
        seed_run(conn, i + 1, pids[t], type_id=t)
    fake_basis(monkeypatch, {pids[HULK]: 200e6, pids[MACKINAW]: 100e6, pids[ORCA]: None})
    seed_tx(conn, 1, HULK, qty=3, price=300e6)      # margin ~ 45%
    seed_tx(conn, 2, MACKINAW, qty=3, price=160e6)  # margin ~ 50%; same units as the Hulk
    seed_tx(conn, 3, ORCA, qty=5, price=1e9)        # no basis: quantity list only
    v = view(conn, ref)
    top = v["top"]
    by = {p.type_id: p for p in v["products"]}
    assert [p.type_id for p in top["quantity"]] == [ORCA, HULK, MACKINAW]  # equal units: revenue decides
    assert by[HULK].revenue > by[MACKINAW].revenue
    assert [p.type_id for p in top["margin"]] == [MACKINAW, HULK]
    lead = HULK if by[HULK].profit > by[MACKINAW].profit else MACKINAW
    assert [p.type_id for p in top["profit"]] == [lead, MACKINAW if lead == HULK else HULK]


def test_open_orders_undercut_guard_and_badges(conn, ref, monkeypatch):
    add_owner(conn, A)
    add_pipeline(conn)
    add_pipeline(conn, THANATOS)
    fake_basis(monkeypatch, {})
    seed_quote(conn, HULK, 300e6)
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at, missing_since) "
        "VALUES (1, 'character', ?, 'character', ?, 310e6, 5, 3, ?, 90, '2026-09-01T00:00:00Z', 'open', ?, ?, ?)",
        (A, HULK, STATION, NOW, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at) "
        "VALUES (2, 'character', ?, 'character', ?, 2e9, 1, 1, ?, 1, '2026-09-07T00:00:00Z', 'open', ?, ?)",
        (A, THANATOS, STATION, NOW, NOW),
    )
    conn.commit()
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at) "
        "VALUES (3, 'character', ?, 'character', ?, 310e6, 1, 1, ?, 90, '2026-09-01T00:00:00Z', 'open', ?, ?)",
        (A, HULK, CJ6, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at) "
        "VALUES (4, 'character', ?, 'character', ?, 310e6, 1, 1, 1030000000000, 90, '2026-09-01T00:00:00Z', "
        "'open', ?, ?)", (A, HULK, NOW, NOW),
    )
    conn.commit()
    v = view(conn, ref)
    rows = {o.order_id: o for o in v["open_orders"]}
    names = {b[0] for b in rows[1].badges}
    assert {"undercut", "unconfirmed", "partial"} <= names
    assert rows[1].system == "Jita" and rows[1].expires == "2026-11-30T00:00:00Z"
    assert {b[0] for b in rows[2].badges} == {"no quote"} and rows[2].expires_soon
    # An order in the structure market compares with the STRUCTURE book, never Jita's.
    assert {b[0] for b in rows[3].badges} == {"no quote"} and "C-J6" in rows[3].badges[0][2]
    seed_quote(conn, HULK, 305e6, region=CJ6, source="structure")
    rows = {o.order_id: o for o in view(conn, ref)["open_orders"]}
    assert "undercut" in {b[0] for b in rows[3].badges} and "C-J6" in [b for b in rows[3].badges if b[0] == "undercut"][0][2]
    # An order in some other structure has no book to compare with: no verdict.
    assert [b[0] for b in rows[4].badges] == ["no quote"] and "does not cache" in rows[4].badges[0][2]
    # Under the Max Buy basis the hub quote is a buy price: no undercut verdict.
    conn.execute("UPDATE settings SET price_source = 'buy', hub_price_basis = 'max_buy'")
    conn.commit()
    seed_quote(conn, HULK, 250e6, source="buy")
    v = view(conn, ref)
    row = {o.order_id: o for o in v["open_orders"]}[1]
    assert "undercut" not in {b[0] for b in row.badges} and "not comparable" in row.quote_note


def test_degrade_notes_relogin_dedupes_family_rows(conn, ref, monkeypatch):
    old = tuple(s for s in esi.REQUESTED_SCOPES if "contracts" not in s and "orders" not in s)
    add_owner(conn, A, scopes=old)
    add_pipeline(conn)
    patch_sales(monkeypatch, conn, char_tx={A: [tx(1)]})
    ledger.pull_sales(conn, ref)
    fake_basis(monkeypatch, {})
    v = view(conn, ref)
    relogin = [n for n in v["notes"] if "re-login needed" in n]
    assert len(relogin) == 1 and "char 2001" in relogin[0]
    assert not [n for n in v["notes"] if "no_scope" in n]
    assert v["sales_at"] == NOW and v["ever_pulled"] and v["owners_count"] == 1  # its corp has no esi_corp row here


def test_chart_data_reconciles_with_strip(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    add_pipeline(conn, MACKINAW)
    seed_run(conn, 1, pid)
    fake_basis(monkeypatch, {pid: 200e6})
    seed_tx(conn, 1, HULK, price=300e6, date="2026-09-06T10:00:00Z")
    seed_tx(conn, 2, MACKINAW, price=150e6, date="2026-09-06T11:00:00Z")   # no basis: revenue yes, profit no
    seed_contract(conn, 3, [citem(1)], price=350e6, date_completed="2026-09-05T00:00:00Z")
    v = view(conn, ref)
    revenue, profit = v["charts"][0].series

    def value(label):  # "2026-09-06 · revenue 450,000,000 ISK" -> 450000000.0
        return float(label.rsplit(" ", 2)[-2].replace(",", ""))

    by_day_rev = {b.label[:10]: value(b.label) for b in revenue.bars}
    by_day_profit = {b.label[:10]: value(b.label) for b in profit.bars}
    by = {p.type_id: p for p in v["products"]}
    assert by_day_rev["2026-09-06"] == pytest.approx(450e6)   # both hulls' sales, basis or not
    assert by_day_rev["2026-09-05"] == pytest.approx(350e6)
    assert sum(by_day_rev.values()) == pytest.approx(v["totals"].revenue)
    assert sum(by_day_profit.values()) == pytest.approx(by[HULK].profit)  # the Mackinaw has no basis
    assert by_day_profit["2026-09-06"] == pytest.approx(net_of(conn, ref, HULK, 300e6) - 200e6)
    assert sum(b.h > 0 for b in revenue.bars) == 2
    total_revenue = v["totals"].revenue
    market, contract = v["charts"][1].series
    assert sum(1 for b in market.bars if b.h > 0) == 1 and sum(1 for b in contract.bars if b.h > 0) == 1
    line = v["charts"][2].series[0]
    assert line.kind == "line" and line.points[-1].y <= line.points[0].y  # cumulative profit rises (y grows downward)
    assert total_revenue == pytest.approx(800e6)
    assert v["charts"][0].caption.endswith("cost basis only")
