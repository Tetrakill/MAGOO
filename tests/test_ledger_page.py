"""Ledger (v1.27.0) at the request level: the /ledger page on a seeded,
fresh and pre-SDE database; the two-step ESI refresh (snapshot, then
sales) with each step failing independently; the ESI tab's Count sales
toggles, provenance cells, re-login badge and delete clean-up."""

import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from magoo import config, esi, ledger, store

HULK = 22544
MACKINAW = 22548
A = 2001
PLAYER_CORP = 98000001
STATION = 60003760
NOW = "2026-09-07T12:00:00Z"


def _ago(days: int, hour: int = 10) -> str:
    """An ISO Z timestamp `days` days before now — the page route reads the
    real clock, so seeded dates must move with it."""
    when = datetime.now(timezone.utc) - timedelta(days=days)
    return when.replace(hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


class FakeCost:
    lines = ()
    missing_prices = 0
    spin_up = False

    def __init__(self, total):
        self.total = total

    def subtotal(self, kind):
        return {"material": self.total * 0.8, "install": self.total * 0.1}.get(kind, 0.0)


def _seed_sales(c, pipeline_id):
    """A completed run with attributable hulls, two market sales, one clean
    contract, an open order and a closed one, plus provenance rows."""
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, planned_end, status, completed_at) "
        "VALUES (7, '2026-09-01', '2026-09-08', 'complete', '2026-09-02T00:00:00Z')"
    )
    run_id = c.execute("SELECT index_run_id FROM index_run WHERE run_number = 7").fetchone()[0]
    c.execute(
        "INSERT INTO index_run_item (index_run_id, type_id, on_hand_qty, in_progress_qty, "
        "target_stock_qty, deficit_qty, recommended_action, depth) VALUES (?, ?, 0, 0, 8, 8, 'build', 0)",
        (run_id, HULK),
    )
    item_id = c.execute("SELECT index_run_item_id FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
                        (run_id, HULK)).fetchone()[0]
    c.execute(
        "INSERT INTO index_run_item_pipeline (index_run_item_id, pipeline_id, qty_attributable, depth) "
        "VALUES (?, ?, 8, 0)", (item_id, pipeline_id),
    )
    c.execute("INSERT INTO location_system (location_id, solar_system_id) VALUES (?, 30000142)", (STATION,))
    for tid, price, date in ((1, 300e6, _ago(1)), (2, 310e6, _ago(2))):
        c.execute(
            "INSERT INTO sale_transaction (transaction_id, owner_kind, owner_id, division, source_feed, "
            "type_id, quantity, unit_price, date, location_id, client_id, journal_ref_id, fetched_at) "
            "VALUES (?, 'character', ?, NULL, 'character', ?, 1, ?, ?, ?, 91000001, NULL, ?)",
            (tid, A, HULK, price, date, STATION, NOW),
        )
    c.execute(
        "INSERT INTO sale_contract (contract_id, owner_kind, owner_id, issuer_id, issuer_corporation_id, "
        "for_corporation, acceptor_id, type, status, price, date_issued, date_completed, date_expired, "
        "start_location_id, first_seen_at, last_seen_at, items_fetched_at, items_status) VALUES "
        "(5, 'character', ?, ?, ?, 0, 91000002, 'item_exchange', 'finished', 350e6, ?, "
        "?, ?, ?, ?, ?, ?, 'ok')",
        (A, A, PLAYER_CORP, _ago(6), _ago(3), _ago(-8), STATION, NOW, NOW, NOW),
    )
    c.execute(
        "INSERT INTO sale_contract_item (contract_id, record_id, type_id, quantity, raw_quantity, "
        "is_included, is_singleton) VALUES (5, 1, ?, 1, -1, 1, 1)", (HULK,),
    )
    c.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at) VALUES "
        "(11, 'character', ?, 'character', ?, 1e12, 3, 2, ?, 90, ?, 'open', ?, ?)",
        (A, HULK, STATION, _ago(4), NOW, NOW),
    )
    c.execute(
        "INSERT INTO sale_order (order_id, owner_kind, owner_id, source_feed, type_id, price, volume_total, "
        "volume_remain, location_id, duration, issued, state, first_seen_at, last_seen_at, history_seen_at) "
        "VALUES (12, 'character', ?, 'character', ?, 305e6, 2, 0, ?, 90, ?, 'expired', "
        "?, ?, ?)", (A, HULK, STATION, _ago(18), NOW, NOW, _ago(1)),
    )
    for family in ("orders", "transactions", "contracts"):
        c.execute(
            "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, via_character_id, rows, "
            "pulled_at) VALUES ('character', ?, ?, 0, 'ok', ?, 3, ?)", (A, family, A, NOW),
        )
    c.commit()
    return run_id


def _add_character(c, scopes=esi.REQUESTED_SCOPES, count_sales=1):
    c.execute(
        "INSERT INTO pool_character (character_id, character_name, include_assets, include_job_slots, "
        "count_assets, count_sales) VALUES (?, 'Seller', 0, 0, 0, ?)", (A, count_sales),
    )
    c.execute(
        "INSERT INTO esi_token (character_id, refresh_token, access_token, expires_at, scopes) "
        "VALUES (?, 'rt', 'at', '2099-01-01T00:00:00+00:00', ?)", (A, " ".join(scopes)),
    )
    c.commit()


def test_ledger_page_never_pulled_and_windows(seeded_client):
    html = seeded_client.get("/ledger").get_data(as_text=True)
    assert 'class="active">Ledger</a>' in html
    assert "No sales data yet" in html
    assert ">30 days</a>" in html and "/ledger?window=all" in html and ">All</a>" in html
    assert "<h1>Ledger</h1>" in html
    for path in ("/ledger?window=all", "/ledger?window=bogus", "/ledger?window=7", "/ledger?rows=all"):
        assert seeded_client.get(path).status_code == 200, path


def test_ledger_page_seeded_sales_render(seeded_client, monkeypatch):
    c = _state()
    _add_character(c)
    pid = c.execute("SELECT pipeline_id FROM pipeline WHERE final_product_type_id = ?", (HULK,)).fetchone()[0]
    run_id = _seed_sales(c, pid)
    monkeypatch.setattr(ledger.costing, "hull_cost", lambda conn, ref, s, r, p: FakeCost(200e6))
    html = seeded_client.get("/ledger?window=30").get_data(as_text=True)
    assert html.count("<svg") == 3 and 'data-buckets="30"' in html
    assert "Revenue &amp; net income" in html and "Cumulative net income" in html
    assert "Top 10 by margin" in html and "Top 10 by quantity" in html and "Top 10 by net income" in html
    assert f'href="/runs/{run_id}?view=profit"' in html and "run 7 ↗" in html
    assert "Hulk" in html
    assert "3 units" in html                          # Top-10 by quantity: 2 market + 1 contract
    assert ">contract</span>" in html and ">market</span>" in html
    assert ">filled</span>" in html                   # order history outcome
    assert ">partial</span>" in html                  # the open order has sold 1 of 3
    assert ">undercut</span>" in html                 # listed far above the seeded Hulk quote
    for label in ("Revenue", "Cost of goods sold", "Net income", "Margin", "Unrealized profit",
                  "Units sold", "Contracts closed"):
        assert f'<span class="label">{label}</span>' in html, label
    assert "Net income (est.)" in html and "Net income / unit" in html
    assert "Est. profit" not in html and "estimated profit" not in html
    assert "sales pulled" in html and "1 owner" in html
    assert "partial data" not in html


def test_ledger_page_pre_sde_renders_guidance(tmp_path, monkeypatch):
    from magoo import web

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MAGOO_SECRET", "test-secret")
    app = web.create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        resp = client.get("/ledger")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Download the game data first" in html
        assert "disabled" in html  # the refresh button waits for the SDE


def test_esi_refresh_two_steps_independent_and_next_ledger(seeded_client, monkeypatch):
    from magoo import market

    c = _state()
    _add_character(c)
    calls = []
    # Step 3 (prices) is stubbed off the network; the structure pull is
    # skipped for want of a scoped character.
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (calls.append("prices"), (0, 0, 0))[1])
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)

    def fake_state(conn, ref):
        calls.append("snapshot")
        state = {"on_hand": {424242: len(calls)}, "in_progress": {}, "active_jobs": {}, "job_ends": {},
                 "character_isk": 0.0, "corporation_isk": 0.0}
        store.save_esi_snapshot(conn, state["on_hand"], {}, {}, 0.0, 0.0, {})
        return state

    def fake_pull(conn, ref):
        calls.append("sales")
        return ledger.PullSummary(new_sales=3, contracts_finished=1, open_orders=2)

    monkeypatch.setattr(esi, "refresh_state", fake_state)
    monkeypatch.setattr(ledger, "pull_sales", fake_pull)
    resp = seeded_client.post("/esi/refresh", data={"next": "ledger", "window": "90"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/ledger?window=90")
    assert calls == ["snapshot", "sales", "prices"]
    html = seeded_client.get("/ledger?window=90").get_data(as_text=True)
    assert "ESI refreshed" in html and "sales: 3 new sales, 1 contracts finished, 2 open orders" in html
    assert "prices refreshed in" in html and "structure market skipped" in html

    # Step 3 fails: the snapshot and the sales pull are untouched, the flash says so.
    calls.clear()
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (_ for _ in ()).throw(
        httpx.HTTPStatusError("px", request=httpx.Request("GET", "https://esi.example/"),
                              response=httpx.Response(503))))
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert calls == ["snapshot", "sales"]
    assert "sales: 3 new sales" in html and "price refresh failed" in html
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (calls.append("prices"), (0, 0, 0))[1])

    # Step 1 fails with an HTTP error: the sales step still runs.
    calls.clear()
    monkeypatch.setattr(esi, "refresh_state", lambda conn, ref: (_ for _ in ()).throw(
        httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://esi.example/"),
                              response=httpx.Response(500))))
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    assert resp.status_code == 200 and calls == ["sales", "prices"]  # steps 2 and 3 still run
    html = resp.get_data(as_text=True)
    assert "ESI refresh failed" in html and "sales: 3 new sales" in html and "prices refreshed" in html

    # Step 1 cannot reach ESI at all: the sales step is skipped, rows say so.
    calls.clear()
    monkeypatch.setattr(esi, "refresh_state", lambda conn, ref: (_ for _ in ()).throw(httpx.ConnectError("down")))
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    assert calls == []
    html = resp.get_data(as_text=True)
    assert "sales pull skipped" in html and "prices not refreshed — ESI unavailable" in html
    rows = _state().execute("SELECT status FROM sales_pull WHERE owner_id = ?", (A,)).fetchall()
    assert rows and all(r["status"] == "skipped" for r in rows)

    # Step 2 blows up: the request still redirects and names the failure.
    monkeypatch.setattr(esi, "refresh_state", fake_state)
    monkeypatch.setattr(ledger, "pull_sales", lambda conn, ref: (_ for _ in ()).throw(KeyError("transaction_id")))
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "ESI refreshed" in html and "sales pull failed" in html and "transaction_id" in html
    latest = store.latest_esi_snapshot(_state())
    assert latest["on_hand"].get(424242) == calls.count("snapshot")  # step 1 committed before step 2 blew up


def test_characters_page_toggles_provenance_relogin_badge_and_delete_nulls_via(seeded_client):
    c = _state()
    old = tuple(s for s in esi.REQUESTED_SCOPES if "contracts" not in s)
    _add_character(c, scopes=old)
    c.execute("INSERT INTO esi_corp (corporation_id, corporation_name) VALUES (?, 'Test Holdings')", (PLAYER_CORP,))
    c.execute(
        "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, via_character_id, rows, pulled_at) "
        "VALUES ('corporation', ?, 'orders', 0, 'ok', ?, 4, ?)", (PLAYER_CORP, A, NOW),
    )
    for d in range(1, 8):
        c.execute(
            "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, via_character_id, rows, "
            "pulled_at) VALUES ('corporation', ?, 'transactions', ?, 'ok', ?, 1, ?)", (PLAYER_CORP, d, A, NOW),
        )
    c.execute(
        "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, message, pulled_at) "
        "VALUES ('corporation', ?, 'contracts', 0, 'no_scope', 'no logged-in character holds x', ?)",
        (PLAYER_CORP, NOW),
    )
    c.execute(
        "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, via_character_id, rows, pulled_at) "
        "VALUES ('character', ?, 'transactions', 0, 'partial', ?, 9, ?)", (A, A, NOW),
    )
    c.execute(
        "INSERT INTO sale_contract (contract_id, owner_kind, owner_id, issuer_id, issuer_corporation_id, "
        "for_corporation, type, status, date_issued, date_expired, via_character_id, first_seen_at, last_seen_at) "
        "VALUES (77, 'corporation', ?, ?, ?, 1, 'item_exchange', 'outstanding', ?, ?, ?, ?, ?)",
        (PLAYER_CORP, A, PLAYER_CORP, NOW, NOW, A, NOW, NOW),
    )
    c.commit()
    html = seeded_client.get("/characters").get_data(as_text=True)
    assert html.count("<th>Count sales</th>") == 2
    assert "re-login needed" in html and "esi-contracts.read_character_contracts.v1" in html
    assert "via Seller" in html and "(7 divisions)" in html and "(4 rows)" in html
    assert ">no scope</span>" in html
    assert ">partial</span>" in html
    assert "not pulled yet" in html  # the character's orders/contracts families

    resp = seeded_client.post(f"/characters/{A}/toggle/count_sales")
    assert resp.status_code == 302
    assert _state().execute("SELECT count_sales FROM pool_character WHERE character_id = ?", (A,)).fetchone()[0] == 0
    resp = seeded_client.post(f"/corps/{PLAYER_CORP}/toggle/count_sales")
    assert resp.status_code == 302
    assert _state().execute("SELECT count_sales FROM esi_corp WHERE corporation_id = ?", (PLAYER_CORP,)).fetchone()[0] == 0
    assert seeded_client.post(f"/corps/{PLAYER_CORP}/toggle/bogus").status_code == 400

    resp = seeded_client.post(f"/characters/{A}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert "reading the sales feeds of Test Holdings" in resp.get_data(as_text=True)
    s = _state()
    assert s.execute("SELECT COUNT(*) FROM sales_pull WHERE via_character_id IS NOT NULL").fetchone()[0] == 0
    assert s.execute("SELECT COUNT(*) FROM sale_contract WHERE via_character_id IS NOT NULL").fetchone()[0] == 0
    assert s.execute("SELECT COUNT(*) FROM sales_pull").fetchone()[0] == 10  # rows stay
    assert s.execute("SELECT COUNT(*) FROM sale_contract").fetchone()[0] == 1


def test_schema_9_migration_from_v8_shape(tmp_path, monkeypatch):
    """A pre-Ledger database: the toggle columns arrive with default 1 and
    every existing row survives; the five tables appear; user_version
    moves to the current stamp (9 at the time; 11 since the install check
    and the persisted slot pools).
    The pre-upgrade backup goes to THIS temp dir, never to data/backups."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.sqlite")
    c = sqlite3.connect(tmp_path / "old.sqlite")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    for col in ("count_sales",):
        c.execute(f"ALTER TABLE pool_character DROP COLUMN {col}")
        c.execute(f"ALTER TABLE esi_corp DROP COLUMN {col}")
    for t in ("sale_transaction", "sale_order", "sale_contract_item", "sale_contract", "sales_pull"):
        c.execute(f"DROP TABLE {t}")
    c.execute("PRAGMA user_version = 8")
    c.execute(
        "INSERT INTO pool_character (character_id, character_name, include_assets, include_job_slots, "
        "count_assets) VALUES (1, 'Old', 1, 1, 0)"
    )
    c.execute("INSERT INTO esi_corp (corporation_id, corporation_name, count_wallet) VALUES (9, 'Old Corp', 0)")
    c.commit()
    store.ensure_schema(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))  # the backup landed here
    row = c.execute("SELECT * FROM pool_character").fetchone()
    assert row["count_sales"] == 1 and row["include_assets"] == 1
    row = c.execute("SELECT * FROM esi_corp").fetchone()
    assert row["count_sales"] == 1 and row["count_wallet"] == 0
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"sale_transaction", "sale_order", "sale_contract", "sale_contract_item", "sales_pull"} <= tables
    store.ensure_schema(c)  # idempotent
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_prices_refresh_caches_every_final_from_the_structure_book(seeded_client, monkeypatch):
    """The structure pull keeps the SELL quote of every pipeline final,
    sub-caps and inactive pipelines included, so the Ledger can judge an
    order sitting at C-J6 against that book (Planning keeps pricing
    sub-caps at Jita)."""
    from magoo import market

    c = _state()
    c.execute(
        "INSERT INTO pipeline (name, final_product_type_id, output_qty_per_run, is_active) "
        "VALUES ('retired mack line', ?, 4, 0)", (MACKINAW,),
    )
    c.commit()
    wanted = []
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (0, 0, 0))
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: 2001)

    def fake_structure(conn, structure_id, type_ids, character_id):
        wanted.extend(type_ids)
        return 0

    monkeypatch.setattr(market, "refresh_structure_prices", fake_structure)
    resp = seeded_client.post("/prices/refresh", follow_redirects=True)
    assert resp.status_code == 200
    assert HULK in wanted and MACKINAW in wanted
    assert "sub-capital hulls quoted there for the Ledger" in resp.get_data(as_text=True)
