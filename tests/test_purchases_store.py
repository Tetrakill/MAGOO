"""Schema 12 and the run purchase-line helpers (Buy tab, v1.29).

Revision 3 (user rulings 2026-09-28): the buy-side ESI tables, the
run_purchase esi_* / owner_* columns and index, count_buys, the one-time
wallet re-walk, replace_derived_purchases and buys_enabled_owners. The
revision-2 choice table (run_buy_choice) and its helpers are retired, and
so are their tests.

Every test here builds its own temp database and monkeypatches
config.DATA_DIR / config.DB_PATH before calling ensure_schema — the
conftest tripwire (review 2026-09-09) fails by name any test that lets
store._backup_before_migrating write into the user's real data/backups.
"""

import dataclasses
import sqlite3

import pytest

from magoo import config, store


def _db(tmp_path, monkeypatch, name="magoo.sqlite"):
    """A fresh state database at schema 12, fully isolated from the
    production data dir (backups land in tmp_path)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / name)
    conn = sqlite3.connect(tmp_path / name)
    conn.row_factory = sqlite3.Row
    store.ensure_schema(conn)
    return conn


def _seed_run(conn, run_number=1) -> int:
    cur = conn.execute(
        "INSERT INTO index_run (run_number, planned_start, status) "
        "VALUES (?, '2026-09-23', 'planned')",
        (run_number,),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_fresh_database_reaches_schema_12(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert store.SCHEMA_VERSION == 12
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "run_purchase" in tables
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "run_purchase_run" in indexes
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(index_run_item)")}
    assert {
        "compressed_alloc",
        "compressed_landed_isk",
        "compressed_tax_isk",
        "direct_landed_isk",
    } <= columns
    conn.close()


def test_schema_11_database_gains_the_table_and_columns(tmp_path, monkeypatch):
    """An upgraded database: the run_purchase table and the four new
    index_run_item columns arrive on the next ensure_schema, rows planned
    before carry NULLs, and the stamp moves to 12. The pre-upgrade backup
    lands in THIS temp dir."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.sqlite")
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.row_factory = sqlite3.Row
    store.ensure_schema(conn)
    # Wind it back to schema 11: drop the v1.29 additions.
    conn.execute("DROP INDEX run_purchase_run")
    conn.execute("DROP TABLE run_purchase")
    for col in (
        "compressed_alloc",
        "compressed_landed_isk",
        "compressed_tax_isk",
        "direct_landed_isk",
    ):
        conn.execute(f"ALTER TABLE index_run_item DROP COLUMN {col}")
    conn.execute("PRAGMA user_version = 11")
    run_id = _seed_run(conn)
    conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id, recommended_buy_qty) "
        "VALUES (?, 34, 1000)",
        (run_id,),
    )
    conn.commit()

    store.ensure_schema(conn)

    assert list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))  # landed HERE
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 12
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(index_run_item)")}
    assert {
        "compressed_alloc",
        "compressed_landed_isk",
        "compressed_tax_isk",
        "direct_landed_isk",
    } <= columns
    row = conn.execute(
        "SELECT compressed_alloc, compressed_landed_isk, compressed_tax_isk, "
        "direct_landed_isk FROM index_run_item"
    ).fetchone()
    assert tuple(row) == (None, None, None, None)
    # The table is back and usable.
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    conn.commit()
    assert list(store.list_purchases(conn, run_id)) == [34]
    conn.close()


def test_ensure_schema_is_idempotent_at_12(tmp_path, monkeypatch):
    """A second ensure_schema on a schema-12 database keeps the rows (the
    CREATE TABLE / CREATE INDEX carry IF NOT EXISTS)."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    pid = store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    conn.commit()
    store.ensure_schema(conn)
    rows = store.list_purchases(conn, run_id)[34]
    assert [r["purchase_id"] for r in rows] == [pid]
    conn.close()


def test_venue_and_source_constants():
    assert store.BUY_VENUE_DELIVERED == "delivered"
    # Revision 4 (2026-09-28) APPENDS 'other' — never reorder (A2.7).
    assert store.BUY_VENUE_OTHER == "other"
    assert store.PURCHASE_VENUES == ("hub", "structure", "delivered", "other")
    assert store.PURCHASE_SOURCES == ("sell", "split", "buy")
    # A purchase venue only — never a plan venue.
    assert store.BUY_VENUE_DELIVERED not in (
        store.BUY_VENUE_HUB,
        store.BUY_VENUE_STRUCTURE,
        store.BUY_VENUE_SPLIT,
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_add_and_list_purchases(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    first = store.add_purchase(
        conn, run_id, 34, store.BUY_VENUE_HUB, 1_000_000, 5.5,
        source="buy", note="Jita buy order",
    )
    second = store.add_purchase(
        conn, run_id, 34, store.BUY_VENUE_DELIVERED, 500_000, 6.25
    )
    store.add_purchase(conn, run_id, 35, store.BUY_VENUE_STRUCTURE, 10, 9.0)
    conn.commit()

    grouped = store.list_purchases(conn, run_id)
    assert set(grouped) == {34, 35}
    assert [r["purchase_id"] for r in grouped[34]] == [first, second]  # entry order
    row = grouped[34][0]
    assert row["venue"] == "hub"
    assert row["source"] == "buy"
    assert row["quantity"] == 1_000_000
    assert row["unit_price"] == pytest.approx(5.5)
    assert row["note"] == "Jita buy order"
    assert row["created_at"]  # stamped by the DEFAULT
    # source and note are optional.
    assert grouped[34][1]["source"] is None
    assert grouped[34][1]["note"] is None
    conn.close()


def test_list_purchases_is_empty_for_a_run_without_lines(tmp_path, monkeypatch):
    """The zero-purchase case every costing identity rests on."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    assert store.list_purchases(conn, run_id) == {}
    conn.close()


def test_add_purchase_does_not_commit(tmp_path, monkeypatch):
    """Helpers leave the transaction to the caller, like the rest of
    store.py — a rollback must take the line with it."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    conn.commit()
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    conn.rollback()
    assert store.list_purchases(conn, run_id) == {}
    conn.close()


def test_update_purchase_edits_in_place(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    pid = store.add_purchase(
        conn, run_id, 34, store.BUY_VENUE_HUB, 100, 5.0, source="sell", note="x"
    )
    store.update_purchase(
        conn, run_id, pid, store.BUY_VENUE_STRUCTURE, 250, 4.25,
        source="split", note=None,
    )
    conn.commit()
    row = store.list_purchases(conn, run_id)[34][0]
    assert row["purchase_id"] == pid
    assert row["type_id"] == 34  # the item never moves
    assert row["venue"] == "structure"
    assert row["source"] == "split"
    assert row["quantity"] == 250
    assert row["unit_price"] == pytest.approx(4.25)
    assert row["note"] is None
    conn.close()


def test_blank_note_is_stored_as_null(tmp_path, monkeypatch):
    """The web form posts '' for an untouched note; an empty string in the
    cell would render as a stray blank badge."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 5, 1.0, note="")
    conn.commit()
    assert store.list_purchases(conn, run_id)[34][0]["note"] is None
    conn.close()


def test_zero_unit_price_is_allowed(tmp_path, monkeypatch):
    """Free stock (a corp hand-down) is a real purchase line at 0 ISK."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_DELIVERED, 5, 0.0)
    conn.commit()
    assert store.list_purchases(conn, run_id)[34][0]["unit_price"] == 0.0
    conn.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        (dict(venue="split"), "venue"),          # a plan venue, not a purchase venue
        (dict(venue="Jita"), "venue"),
        (dict(venue=None), "venue"),
        (dict(quantity=0), "at least 1"),
        (dict(quantity=-5), "at least 1"),
        (dict(quantity="lots"), "whole number"),
        (dict(unit_price=-0.01), "negative"),
        (dict(unit_price="cheap"), "number"),
        (dict(source="haggle"), "source"),
        # Review 2026-09-23: float() takes these, SQLite does not — NaN
        # binds as NULL (a NOT NULL IntegrityError the routes do not
        # catch) and infinity stores, then poisons every realized cost
        # priced off the run. A 25-digit quantity raises OverflowError
        # inside the INSERT. All three are reachable from the Buy tab's
        # text inputs, so all three refuse here instead.
        (dict(unit_price="nan"), "real number"),
        (dict(unit_price="inf"), "real number"),
        (dict(unit_price="Infinity"), "real number"),
        (dict(unit_price="1e400"), "real number"),
        (dict(unit_price=float("nan")), "real number"),
        (dict(quantity="9" * 25), "larger than Magoo"),
        (dict(quantity=2**63), "larger than Magoo"),
    ],
)
def test_validation_errors(tmp_path, monkeypatch, kwargs, fragment):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    args = dict(venue=store.BUY_VENUE_HUB, quantity=10, unit_price=5.0)
    args.update(kwargs)
    with pytest.raises(ValueError) as excinfo:
        store.add_purchase(conn, run_id, 34, **args)
    assert fragment in str(excinfo.value)
    # Nothing was written.
    assert store.list_purchases(conn, run_id) == {}
    conn.close()


def test_update_validates_the_same_way(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    pid = store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    conn.commit()
    with pytest.raises(ValueError):
        store.update_purchase(conn, run_id, pid, "Jita", 10, 5.0)
    with pytest.raises(ValueError):
        store.update_purchase(conn, run_id, pid, store.BUY_VENUE_HUB, 0, 5.0)
    with pytest.raises(ValueError):
        store.update_purchase(conn, run_id, pid, store.BUY_VENUE_HUB, 10, -1.0)
    row = store.list_purchases(conn, run_id)[34][0]
    assert (row["venue"], row["quantity"], row["unit_price"]) == ("hub", 10, 5.0)
    conn.close()


def test_the_widest_recordable_quantity_still_goes_in(tmp_path, monkeypatch):
    """The bound is SQLite's own 64-bit integer, not a guess."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 2**63 - 1, 1.0)
    conn.commit()
    assert store.list_purchases(conn, run_id)[34][0]["quantity"] == 2**63 - 1
    conn.close()


def test_numeric_strings_from_the_form_are_coerced(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, "250", "4.5")
    conn.commit()
    row = store.list_purchases(conn, run_id)[34][0]
    assert row["quantity"] == 250 and row["unit_price"] == pytest.approx(4.5)
    conn.close()


# ---------------------------------------------------------------------------
# Run scoping
# ---------------------------------------------------------------------------


def test_list_purchases_is_scoped_to_the_run(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_a = _seed_run(conn, 1)
    run_b = _seed_run(conn, 2)
    store.add_purchase(conn, run_a, 34, store.BUY_VENUE_HUB, 10, 5.0)
    store.add_purchase(conn, run_b, 34, store.BUY_VENUE_HUB, 99, 1.0)
    conn.commit()
    assert store.list_purchases(conn, run_a)[34][0]["quantity"] == 10
    assert store.list_purchases(conn, run_b)[34][0]["quantity"] == 99
    conn.close()


def test_delete_purchase_is_scoped_to_the_run(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_a = _seed_run(conn, 1)
    run_b = _seed_run(conn, 2)
    keep = store.add_purchase(conn, run_a, 34, store.BUY_VENUE_HUB, 10, 5.0)
    other = store.add_purchase(conn, run_b, 34, store.BUY_VENUE_HUB, 99, 1.0)
    conn.commit()

    store.delete_purchase(conn, run_a, other)  # wrong run: no cross-run delete
    conn.commit()
    assert [r["purchase_id"] for r in store.list_purchases(conn, run_b)[34]] == [other]

    store.delete_purchase(conn, run_a, keep)
    conn.commit()
    assert store.list_purchases(conn, run_a) == {}
    # Already gone: a double-submitted delete is a silent no-op.
    store.delete_purchase(conn, run_a, keep)
    conn.close()


def test_update_purchase_is_scoped_to_the_run(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_a = _seed_run(conn, 1)
    run_b = _seed_run(conn, 2)
    other = store.add_purchase(conn, run_b, 34, store.BUY_VENUE_HUB, 99, 1.0)
    conn.commit()
    with pytest.raises(ValueError) as excinfo:
        store.update_purchase(conn, run_a, other, store.BUY_VENUE_HUB, 1, 2.0)
    assert "not on this run" in str(excinfo.value)
    row = store.list_purchases(conn, run_b)[34][0]
    assert (row["quantity"], row["unit_price"]) == (99, 1.0)
    conn.close()


def test_delete_run_purchases_clears_only_that_run(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_a = _seed_run(conn, 1)
    run_b = _seed_run(conn, 2)
    store.add_purchase(conn, run_a, 34, store.BUY_VENUE_HUB, 10, 5.0)
    store.add_purchase(conn, run_a, 35, store.BUY_VENUE_STRUCTURE, 20, 6.0)
    store.add_purchase(conn, run_b, 34, store.BUY_VENUE_HUB, 99, 1.0)
    conn.commit()

    store.delete_run_purchases(conn, run_a)
    conn.commit()
    assert store.list_purchases(conn, run_a) == {}
    assert set(store.list_purchases(conn, run_b)) == {34}
    # A run that never had lines is fine too.
    store.delete_run_purchases(conn, run_a)
    conn.close()


def test_delete_run_purchases_clears_the_foreign_key_before_a_run_delete(
    tmp_path, monkeypatch
):
    """run_purchase.index_run_id references index_run and connections open
    with PRAGMA foreign_keys = ON, so web.run_delete must clear the lines
    first (contract review A29)."""
    conn = _db(tmp_path, monkeypatch)
    conn.execute("PRAGMA foreign_keys = ON")
    run_id = _seed_run(conn)
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run_id,))
    conn.rollback()

    store.delete_run_purchases(conn, run_id)
    conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# delete_run_purchases_for_type
# ---------------------------------------------------------------------------


def test_delete_run_purchases_for_type_clears_one_item_of_one_run(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path, monkeypatch)
    run_a = _seed_run(conn, 1)
    run_b = _seed_run(conn, 2)
    store.add_purchase(conn, run_a, 34, store.BUY_VENUE_HUB, 10, 5.0)
    store.add_purchase(conn, run_a, 34, store.BUY_VENUE_STRUCTURE, 20, 6.0)
    store.add_purchase(conn, run_a, 35, store.BUY_VENUE_HUB, 30, 7.0)
    store.add_purchase(conn, run_b, 34, store.BUY_VENUE_HUB, 99, 1.0)
    conn.commit()

    store.delete_run_purchases_for_type(conn, run_a, 34)
    conn.commit()

    assert set(store.list_purchases(conn, run_a)) == {35}
    assert store.list_purchases(conn, run_b)[34][0]["quantity"] == 99
    # An item with no lines is fine.
    store.delete_run_purchases_for_type(conn, run_a, 34)
    conn.close()


def test_delete_run_purchases_for_type_does_not_commit(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    conn.commit()
    store.delete_run_purchases_for_type(conn, run_id, 34)
    conn.rollback()
    assert set(store.list_purchases(conn, run_id)) == {34}
    conn.close()


# ---------------------------------------------------------------------------
# Revision 3 schema (user rulings 2026-09-28; contract §2 + review C6)
# ---------------------------------------------------------------------------


def _tables(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _indexes(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }


def _cols(conn, table) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


_BUY_TRANSACTION_COLUMNS = {
    "transaction_id", "owner_kind", "owner_id", "division", "source_feed",
    "type_id", "quantity", "unit_price", "date", "location_id", "client_id",
    "journal_ref_id", "fetched_at",
}
_BUY_CONTRACT_COLUMNS = {
    "contract_id", "owner_kind", "owner_id", "issuer_id",
    "issuer_corporation_id", "acceptor_id", "type", "status", "price",
    "reward", "title", "date_issued", "date_accepted", "date_completed",
    "start_location_id", "via_character_id", "first_seen_at", "last_seen_at",
    "items_fetched_at", "items_status", "items_attempts", "k",
    "unpriced_items", "priced_at", "excluded", "for_corporation",
}
_BUY_CONTRACT_ITEM_COLUMNS = {
    "contract_id", "record_id", "type_id", "quantity", "raw_quantity",
    "is_included", "is_singleton", "unit_price",
}
_RUN_PURCHASE_ESI_COLUMNS = {
    "esi_kind", "esi_id", "contract_k", "date", "owner_kind", "owner_id",
}


def test_fresh_database_has_the_buy_side_tables_at_12(tmp_path, monkeypatch):
    """Schema 12 had not shipped when revision 3 arrived: the three tables
    are CREATE IF NOT EXISTS, the stamp stays 12, and the retired choice
    table is gone."""
    conn = _db(tmp_path, monkeypatch)
    assert store.SCHEMA_VERSION == 12
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    tables = _tables(conn)
    assert {"buy_transaction", "buy_contract", "buy_contract_item"} <= tables
    assert "run_buy_choice" not in tables
    assert _cols(conn, "buy_transaction") == _BUY_TRANSACTION_COLUMNS
    assert _cols(conn, "buy_contract") == _BUY_CONTRACT_COLUMNS
    assert _cols(conn, "buy_contract_item") == _BUY_CONTRACT_ITEM_COLUMNS
    assert _RUN_PURCHASE_ESI_COLUMNS <= _cols(conn, "run_purchase")
    assert {
        "buy_transaction_date",
        "buy_transaction_owner",
        "buy_contract_status",
        "buy_contract_item_type",
        "run_purchase_run",
        "run_purchase_esi",
    } <= _indexes(conn)
    esi_index = [
        r["name"] for r in conn.execute("PRAGMA index_info(run_purchase_esi)")
    ]
    assert esi_index == ["index_run_id", "esi_kind", "esi_id"]
    pk = [
        r["name"]
        for r in sorted(
            conn.execute("PRAGMA table_info(buy_contract_item)"),
            key=lambda r: r["pk"],
        )
        if r["pk"]
    ]
    assert pk == ["contract_id", "record_id"]
    conn.close()


def test_the_esi_index_is_not_in_state_schema():
    """executescript(STATE_SCHEMA) runs before the ALTERs, when
    run_purchase has no esi_kind yet, so the index must be a _MIGRATIONS
    entry placed after the ALTERs (contract review C6.1): revision 3's six
    plus revision 4's via_type_id."""
    assert "run_purchase_esi" not in store.STATE_SCHEMA
    migrations = list(store._MIGRATIONS)
    index_at = next(
        i for i, m in enumerate(migrations) if "run_purchase_esi" in m
    )
    alters = [
        i
        for i, m in enumerate(migrations)
        if m.startswith("ALTER TABLE run_purchase ADD COLUMN")
    ]
    assert len(alters) == 7
    assert any("via_type_id" in migrations[i] for i in alters)
    assert max(alters) < index_at


def test_count_buys_defaults_on_for_characters_and_corporations(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path, monkeypatch)
    assert {"count_sales", "count_buys"} <= _cols(conn, "pool_character")
    assert {"count_sales", "count_buys"} <= _cols(conn, "esi_corp")
    conn.execute(
        "INSERT INTO pool_character (character_id, character_name) "
        "VALUES (1, 'A')"
    )
    conn.execute("INSERT INTO esi_corp (corporation_id) VALUES (98)")
    assert conn.execute("SELECT count_buys FROM pool_character").fetchone()[0] == 1
    assert conn.execute("SELECT count_buys FROM esi_corp").fetchone()[0] == 1
    conn.close()


def test_run_purchase_esi_checks(tmp_path, monkeypatch):
    """esi_kind and owner_kind carry CHECKs that repeat the store's
    constants; NULL (a line that is not derived) is allowed in both."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    assert store.PURCHASE_ESI_KINDS == ("transaction", "contract")
    assert store.ESI_KIND_TRANSACTION == "transaction"
    assert store.ESI_KIND_CONTRACT == "contract"
    assert store.OWNER_KINDS == ("character", "corporation")
    for i, kind in enumerate(store.PURCHASE_ESI_KINDS):
        for j, owner in enumerate(store.OWNER_KINDS):
            conn.execute(
                "INSERT INTO run_purchase (index_run_id, type_id, venue, "
                "quantity, unit_price, esi_kind, esi_id, owner_kind, owner_id) "
                "VALUES (?, 34, 'hub', 1, 1.0, ?, ?, ?, 7)",
                (run_id, kind, 10 * i + j, owner),
            )
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 1, 1.0)  # NULLs
    conn.commit()
    for column, bad in (("esi_kind", "wallet"), ("owner_kind", "alliance")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO run_purchase (index_run_id, type_id, venue, "
                f"quantity, unit_price, {column}) VALUES (?, 34, 'hub', 1, 1.0, ?)",
                (run_id, bad),
            )
        conn.rollback()
    conn.close()


def _insert_buy_contract(conn, contract_id, **over):
    row = dict(
        contract_id=contract_id,
        owner_kind="character",
        owner_id=1,
        issuer_id=500,
        type="item_exchange",
        status="finished",
        date_issued="2026-09-20T00:00:00Z",
        first_seen_at="2026-09-20T01:00:00Z",
        last_seen_at="2026-09-20T01:00:00Z",
    )
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(
        f"INSERT INTO buy_contract ({cols}) VALUES ({marks})", tuple(row.values())
    )


def test_buy_contract_checks_and_defaults(tmp_path, monkeypatch):
    """excluded is Magoo's own vocabulary (CHECK repeats
    BUY_CONTRACT_EXCLUSIONS, NULL = costed); items_status carries
    sale_contract's CHECK; ESI enums (type, status) carry none, so a value
    CCP adds never aborts a pull."""
    conn = _db(tmp_path, monkeypatch)
    assert store.BUY_CONTRACT_EXCLUSIONS == ("internal", "swap", "no_price")
    assert (
        store.BUY_EXCLUDED_INTERNAL,
        store.BUY_EXCLUDED_SWAP,
        store.BUY_EXCLUDED_NO_PRICE,
    ) == store.BUY_CONTRACT_EXCLUSIONS
    for i, reason in enumerate((None, *store.BUY_CONTRACT_EXCLUSIONS)):
        _insert_buy_contract(conn, 100 + i, excluded=reason)
    _insert_buy_contract(conn, 200, type="brand_new_type", status="odd_status")
    conn.commit()
    row = conn.execute(
        "SELECT items_attempts, unpriced_items, k, priced_at, excluded "
        "FROM buy_contract WHERE contract_id = 100"
    ).fetchone()
    assert tuple(row) == (0, 0, None, None, None)
    # ESI's for_corporation flag (review 2026-09-28) defaults to 0.
    assert conn.execute(
        "SELECT for_corporation FROM buy_contract WHERE contract_id = 100"
    ).fetchone()[0] == 0
    for over in (
        {"excluded": "gift"},
        {"items_status": "pending"},
        {"owner_kind": "alliance"},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_buy_contract(conn, 300, **over)
        conn.rollback()
    conn.close()


def test_buy_transaction_checks(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    sql = (
        "INSERT INTO buy_transaction (transaction_id, owner_kind, owner_id, "
        "source_feed, type_id, quantity, unit_price, date, location_id, "
        "fetched_at) VALUES (?, ?, 1, ?, 34, 10, 5.0, "
        "'2026-09-20T10:00:00Z', 60003760, '2026-09-20T11:00:00Z')"
    )
    conn.execute(sql, (1, "character", "character"))
    conn.execute(sql, (2, "corporation", "corporation"))
    conn.commit()
    for owner, feed in (("alliance", "character"), ("character", "structure")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (3, owner, feed))
        conn.rollback()
    conn.close()


def test_ensure_schema_is_idempotent_with_the_buy_side(tmp_path, monkeypatch):
    """A second open keeps every buy-side row and derived line (the
    CREATEs carry IF NOT EXISTS, the ALTERs swallow 'duplicate column',
    the index carries IF NOT EXISTS)."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    _insert_buy_contract(conn, 100)
    store.replace_derived_purchases(conn, run_id, [_line()])
    conn.commit()
    store.ensure_schema(conn)
    store.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM buy_contract").fetchone()[0] == 1
    assert [r["esi_id"] for r in store.list_purchases(conn, run_id)[34]] == [9001]
    conn.close()


def _wind_back_to_revision_2(conn) -> None:
    """Make a fresh database look like a dev database stamped 12 by the
    unshipped revisions 1-2: no buy-side tables, no esi_* columns on
    run_purchase, no count_buys, and the retired choice table present."""
    conn.execute("DROP TABLE buy_contract_item")
    conn.execute("DROP TABLE buy_contract")
    conn.execute("DROP TABLE buy_transaction")
    conn.execute("DROP INDEX run_purchase_esi")
    for col in sorted(_RUN_PURCHASE_ESI_COLUMNS):
        conn.execute(f"ALTER TABLE run_purchase DROP COLUMN {col}")
    conn.execute("ALTER TABLE pool_character DROP COLUMN count_buys")
    conn.execute("ALTER TABLE esi_corp DROP COLUMN count_buys")
    conn.execute(
        "CREATE TABLE run_buy_choice (index_run_id INTEGER NOT NULL "
        "REFERENCES index_run, type_id INTEGER NOT NULL, choice TEXT NOT NULL, "
        "chosen_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "PRIMARY KEY (index_run_id, type_id))"
    )
    conn.commit()


def test_a_revision_2_database_at_12_gains_revision_3(tmp_path, monkeypatch):
    """A dev database already stamped 12 gains the tables, the six
    run_purchase columns, the index and count_buys on the next open, with
    no version bump and no pre-migration backup. Its existing purchase
    lines survive as non-derived rows (esi_kind NULL), and the orphaned
    choice table is dropped so its rows can no longer block a run delete
    through the foreign key."""
    conn = _db(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO pool_character (character_id, character_name) VALUES (1, 'A')"
    )
    run_id = _seed_run(conn)
    _wind_back_to_revision_2(conn)
    old_pid = store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    conn.execute(
        "INSERT INTO run_buy_choice (index_run_id, type_id, choice) "
        "VALUES (?, 34, 'ladder')",
        (run_id,),
    )
    conn.commit()
    assert "esi_kind" not in _cols(conn, "run_purchase")

    store.ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert not list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))
    tables = _tables(conn)
    assert {"buy_transaction", "buy_contract", "buy_contract_item"} <= tables
    assert "run_buy_choice" not in tables
    assert _RUN_PURCHASE_ESI_COLUMNS <= _cols(conn, "run_purchase")
    assert "run_purchase_esi" in _indexes(conn)
    assert conn.execute("SELECT count_buys FROM pool_character").fetchone()[0] == 1
    row = store.list_purchases(conn, run_id)[34][0]
    assert row["purchase_id"] == old_pid
    assert row["esi_kind"] is None and row["esi_id"] is None
    # The run can be deleted: nothing else references it any more.
    conn.execute("PRAGMA foreign_keys = ON")
    store.delete_run_purchases(conn, run_id)
    conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Revision 6 columns (user rulings R1/R3 2026-09-28; contract review A3/A9)
# ---------------------------------------------------------------------------


def test_fresh_database_has_the_revision_6_columns(tmp_path, monkeypatch):
    """index_run.opened_at and index_run_item.on_hand_from_ore_qty arrive
    through _MIGRATIONS (not STATE_SCHEMA) and default to NULL — every
    reader treats NULL as 0 / falls back to planned_start."""
    conn = _db(tmp_path, monkeypatch)
    assert "opened_at" in _cols(conn, "index_run")
    assert "on_hand_from_ore_qty" in _cols(conn, "index_run_item")
    run_id = _seed_run(conn)
    conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id) VALUES (?, 34)", (run_id,)
    )
    assert conn.execute(
        "SELECT opened_at FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT on_hand_from_ore_qty FROM index_run_item"
    ).fetchone()[0] is None
    conn.close()


def test_the_revision_6_columns_are_migrations_not_state_schema():
    """A dev database already stamped 12 must gain them on the next open:
    only _MIGRATIONS runs against an existing table (store.py's own rule)."""
    assert "on_hand_from_ore_qty" not in store.STATE_SCHEMA
    assert "opened_at" not in store.STATE_SCHEMA
    migrations = list(store._MIGRATIONS)
    assert (
        "ALTER TABLE index_run_item ADD COLUMN on_hand_from_ore_qty INTEGER"
        in migrations
    )
    assert "ALTER TABLE index_run ADD COLUMN opened_at TEXT" in migrations


def test_a_database_at_12_without_them_gains_them(tmp_path, monkeypatch):
    """No version bump (schema 12 is unshipped): a revision-5 database at
    12 gains both columns on the next open with no backup, its rows
    carrying NULL, and the open is idempotent afterwards."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id, on_hand_qty) "
        "VALUES (?, 34, 500)",
        (run_id,),
    )
    conn.commit()
    conn.execute("ALTER TABLE index_run_item DROP COLUMN on_hand_from_ore_qty")
    conn.execute("ALTER TABLE index_run DROP COLUMN opened_at")
    conn.commit()
    assert "opened_at" not in _cols(conn, "index_run")

    store.ensure_schema(conn)
    store.ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert not list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))
    row = conn.execute(
        "SELECT i.on_hand_qty, i.on_hand_from_ore_qty, r.opened_at, r.planned_start "
        "FROM index_run_item i JOIN index_run r USING (index_run_id)"
    ).fetchone()
    assert tuple(row) == (500, None, None, "2026-09-23")
    conn.close()


# ---------------------------------------------------------------------------
# The one-time wallet re-walk (contract review C8)
# ---------------------------------------------------------------------------


def _pull_row(conn, owner_kind, owner_id, family, division, oldest, newest, done):
    conn.execute(
        "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, "
        "oldest_id, newest_id, backfilled, pulled_at) "
        "VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, '2026-09-20T00:00:00Z')",
        (owner_kind, owner_id, family, division, oldest, newest, done),
    )


def _cursors(conn):
    return {
        (r["owner_kind"], r["owner_id"], r["family"], r["division"]): (
            r["oldest_id"],
            r["newest_id"],
            r["backfilled"],
        )
        for r in conn.execute("SELECT * FROM sales_pull")
    }


def test_the_open_that_creates_buy_transaction_rewalks_the_wallets(
    tmp_path, monkeypatch
):
    """v1.28.1 cursors already cover the wallet history whose buys the
    Ledger threw away. The open that creates buy_transaction resets every
    transactions cursor so the next pulls re-read it; orders and contracts
    rows are untouched, and nothing but the cursor moves."""
    conn = _db(tmp_path, monkeypatch)
    _pull_row(conn, "character", 1, "transactions", 0, 100, 900, 1)
    _pull_row(conn, "corporation", 98, "transactions", 1, 50, 800, 1)
    _pull_row(conn, "corporation", 98, "transactions", 2, 60, 700, 0)
    _pull_row(conn, "character", 1, "orders", 0, 5, 6, 1)
    _pull_row(conn, "character", 1, "contracts", 0, 7, 8, 1)
    conn.commit()
    conn.execute("DROP TABLE buy_contract_item")
    conn.execute("DROP TABLE buy_contract")
    conn.execute("DROP TABLE buy_transaction")
    conn.commit()

    store.ensure_schema(conn)

    assert _cursors(conn) == {
        ("character", 1, "transactions", 0): (None, None, 0),
        ("corporation", 98, "transactions", 1): (None, None, 0),
        ("corporation", 98, "transactions", 2): (None, None, 0),
        ("character", 1, "orders", 0): (5, 6, 1),
        ("character", 1, "contracts", 0): (7, 8, 1),
    }
    status = conn.execute(
        "SELECT DISTINCT status FROM sales_pull"
    ).fetchall()
    assert [r[0] for r in status] == ["ok"]
    assert "buy_transaction" in _tables(conn)
    conn.close()


def test_the_rewalk_runs_once(tmp_path, monkeypatch):
    """Every open re-runs the migrations; an unguarded reset would re-walk
    every wallet at every start. Once buy_transaction exists, a later open
    leaves the cursors the pulls have rebuilt alone."""
    conn = _db(tmp_path, monkeypatch)
    conn.execute("DROP TABLE buy_contract_item")
    conn.execute("DROP TABLE buy_contract")
    conn.execute("DROP TABLE buy_transaction")
    conn.commit()
    store.ensure_schema(conn)  # the upgrade open
    _pull_row(conn, "character", 1, "transactions", 0, 100, 900, 1)  # rebuilt
    conn.commit()

    store.ensure_schema(conn)
    store.ensure_schema(conn)

    assert _cursors(conn) == {("character", 1, "transactions", 0): (100, 900, 1)}
    conn.close()


def test_a_fresh_database_opens_without_a_rewalk(tmp_path, monkeypatch):
    """Brand new: no sales_pull table exists before the CREATEs, so the
    reset is skipped outright rather than failing."""
    conn = _db(tmp_path, monkeypatch)
    assert conn.execute("SELECT COUNT(*) FROM sales_pull").fetchone()[0] == 0
    conn.close()


def test_a_schema_11_database_rewalks_on_upgrade(tmp_path, monkeypatch):
    """The production path: a v1.28.1 database (schema 11, no buy side,
    no run_purchase) upgrades to 12 in one open — pre-migration backup in
    THIS temp dir, cursors reset, run_purchase complete with the esi_*
    columns and index."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "v1281.sqlite")
    conn = sqlite3.connect(tmp_path / "v1281.sqlite")
    conn.row_factory = sqlite3.Row
    store.ensure_schema(conn)
    conn.execute("DROP TABLE buy_contract_item")
    conn.execute("DROP TABLE buy_contract")
    conn.execute("DROP TABLE buy_transaction")
    conn.execute("DROP TABLE run_purchase")
    conn.execute("ALTER TABLE pool_character DROP COLUMN count_buys")
    conn.execute("ALTER TABLE esi_corp DROP COLUMN count_buys")
    _pull_row(conn, "character", 1, "transactions", 0, 100, 900, 1)
    conn.execute("PRAGMA user_version = 11")
    conn.commit()

    store.ensure_schema(conn)

    assert list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert _cursors(conn) == {("character", 1, "transactions", 0): (None, None, 0)}
    assert _RUN_PURCHASE_ESI_COLUMNS <= _cols(conn, "run_purchase")
    assert "run_purchase_esi" in _indexes(conn)
    assert "count_buys" in _cols(conn, "pool_character")
    conn.close()


# ---------------------------------------------------------------------------
# replace_derived_purchases (contract §2 + review C4/C5/C11)
# ---------------------------------------------------------------------------


def _line(**over):
    line = dict(
        type_id=34,
        venue=store.BUY_VENUE_HUB,
        quantity=1000,
        unit_price=5.5,
        esi_kind=store.ESI_KIND_TRANSACTION,
        esi_id=9001,
        date="2026-09-20T10:00:00Z",
        owner_kind="character",
        owner_id=1,
    )
    line.update(over)
    return line


def test_replace_derived_purchases_writes_the_lines(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    written = store.replace_derived_purchases(
        conn,
        run_id,
        [
            _line(),
            _line(
                type_id=35,
                venue=store.BUY_VENUE_STRUCTURE,
                quantity=40,
                unit_price=12.25,
                esi_kind=store.ESI_KIND_CONTRACT,
                esi_id=777,
                contract_k=0.97,
                owner_kind="corporation",
                owner_id=98,
            ),
            _line(type_id=35, esi_kind="contract", esi_id=777, contract_k=0.97,
                  quantity=2, unit_price=0.0),
        ],
    )
    conn.commit()
    assert written == 3
    grouped = store.list_purchases(conn, run_id)
    assert set(grouped) == {34, 35}
    t = grouped[34][0]
    assert (t["venue"], t["quantity"], t["unit_price"]) == ("hub", 1000, 5.5)
    assert (t["esi_kind"], t["esi_id"], t["contract_k"]) == ("transaction", 9001, None)
    assert (t["date"], t["owner_kind"], t["owner_id"]) == (
        "2026-09-20T10:00:00Z", "character", 1,
    )
    assert t["source"] is None  # every ESI line (C11)
    c = grouped[35]
    assert [(r["quantity"], r["unit_price"]) for r in c] == [(40, 12.25), (2, 0.0)]
    assert [(r["esi_kind"], r["esi_id"], r["contract_k"]) for r in c] == [
        ("contract", 777, pytest.approx(0.97))
    ] * 2
    assert c[0]["owner_kind"] == "corporation" and c[0]["owner_id"] == 98
    conn.close()


def test_replace_derived_purchases_replaces_only_this_runs_derived_lines(
    tmp_path, monkeypatch
):
    """A second pass rewrites the run's derived lines wholesale; the run's
    non-derived lines (esi_kind NULL) and every other run's lines stay."""
    conn = _db(tmp_path, monkeypatch)
    run_a = _seed_run(conn, 1)
    run_b = _seed_run(conn, 2)
    hand = store.add_purchase(conn, run_a, 34, store.BUY_VENUE_DELIVERED, 5, 9.0)
    store.replace_derived_purchases(conn, run_a, [_line(), _line(esi_id=9002)])
    store.replace_derived_purchases(conn, run_b, [_line(esi_id=9100)])
    conn.commit()

    assert store.replace_derived_purchases(conn, run_a, [_line(esi_id=9003)]) == 1
    conn.commit()

    lines_a = store.list_purchases(conn, run_a)[34]
    assert [(r["purchase_id"] == hand, r["esi_id"]) for r in lines_a] == [
        (True, None),
        (False, 9003),
    ]
    assert [r["esi_id"] for r in store.list_purchases(conn, run_b)[34]] == [9100]
    conn.close()


def test_replace_with_no_lines_clears_a_superseded_run(tmp_path, monkeypatch):
    """What assign_purchases does to a run outside every window (C4), and
    runs_with_derived_purchases is how it finds them."""
    conn = _db(tmp_path, monkeypatch)
    run_a = _seed_run(conn, 1)
    run_b = _seed_run(conn, 2)
    store.add_purchase(conn, run_a, 36, store.BUY_VENUE_HUB, 1, 1.0)  # not derived
    store.replace_derived_purchases(conn, run_a, [_line()])
    store.replace_derived_purchases(conn, run_b, [_line(esi_id=9100)])
    conn.commit()
    assert store.runs_with_derived_purchases(conn) == {run_a, run_b}

    assert store.replace_derived_purchases(conn, run_a, []) == 0
    conn.commit()

    assert store.runs_with_derived_purchases(conn) == {run_b}
    assert set(store.list_purchases(conn, run_a)) == {36}
    # An empty run and an empty iterator (not only a list) are fine.
    assert store.replace_derived_purchases(conn, run_a, iter(())) == 0
    conn.close()


def test_replace_derived_purchases_does_not_commit(tmp_path, monkeypatch):
    """The matcher wraps its whole pass in one transaction (C5)."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.replace_derived_purchases(conn, run_id, [_line()])
    conn.commit()
    store.replace_derived_purchases(conn, run_id, [_line(esi_id=9999)])
    conn.rollback()
    assert [r["esi_id"] for r in store.list_purchases(conn, run_id)[34]] == [9001]
    conn.close()


@pytest.mark.parametrize(
    "over, fragment",
    [
        ({"venue": "jita"}, "venue"),
        ({"quantity": 0}, "at least 1"),
        ({"quantity": 2**63}, "larger than"),
        ({"unit_price": float("nan")}, "real number"),
        ({"unit_price": float("inf")}, "real number"),
        ({"unit_price": -1.0}, "negative"),
        ({"esi_kind": None}, "origin"),
        ({"esi_kind": "journal"}, "origin"),
        ({"esi_id": None}, "esi_id"),
        ({"type_id": "tritanium"}, "type_id"),
        ({"owner_kind": "alliance"}, "owner kind"),
        ({"owner_id": "me"}, "owner id"),
        ({"contract_k": float("nan")}, "scale k"),
        ({"contract_k": -0.5}, "scale k"),
        ({"contract_k": "big"}, "scale k"),
        ({"source": "buy"}, "price source"),
        ({"k": 0.9}, "unknown field"),
    ],
)
def test_a_bad_line_leaves_the_run_untouched(tmp_path, monkeypatch, over, fragment):
    """Every line is validated before the DELETE, so one bad line raises a
    readable ValueError and the run keeps its previous derived lines."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.replace_derived_purchases(conn, run_id, [_line()])
    conn.commit()
    with pytest.raises(ValueError) as excinfo:
        store.replace_derived_purchases(
            conn, run_id, [_line(esi_id=9002), _line(**{"esi_id": 9003, **over})]
        )
    assert fragment in str(excinfo.value)
    assert [r["esi_id"] for r in store.list_purchases(conn, run_id)[34]] == [9001]
    conn.close()


def test_a_line_missing_a_required_field_is_refused(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    line = _line()
    del line["esi_id"]
    with pytest.raises(ValueError) as excinfo:
        store.replace_derived_purchases(conn, run_id, [line])
    assert "lacks esi_id" in str(excinfo.value)
    conn.close()


def test_optional_fields_may_be_left_out_and_source_none_is_tolerated(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    bare = {
        "type_id": 34,
        "venue": "delivered",
        "quantity": "3",  # coerced like add_purchase
        "unit_price": "2.5",
        "esi_kind": "contract",
        "esi_id": "12",
        "source": None,
    }
    store.replace_derived_purchases(conn, run_id, [bare])
    conn.commit()
    row = store.list_purchases(conn, run_id)[34][0]
    assert (row["quantity"], row["unit_price"], row["esi_id"]) == (3, 2.5, 12)
    assert (row["contract_k"], row["date"], row["owner_kind"], row["owner_id"]) == (
        None, None, None, None,
    )
    conn.close()


def test_delete_run_purchases_clears_derived_lines_too(tmp_path, monkeypatch):
    """run_delete's single call clears every line, derived or not, so the
    foreign key never blocks the run delete."""
    conn = _db(tmp_path, monkeypatch)
    conn.execute("PRAGMA foreign_keys = ON")
    run_id = _seed_run(conn)
    store.add_purchase(conn, run_id, 36, store.BUY_VENUE_HUB, 1, 1.0)
    store.replace_derived_purchases(conn, run_id, [_line()])
    conn.commit()
    store.delete_run_purchases(conn, run_id)
    conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM run_purchase").fetchone()[0] == 0
    conn.close()


def test_the_choice_helpers_are_gone():
    """Revision 3 retired click-to-lock (contract §2)."""
    for name in (
        "BUY_CHOICES",
        "BUY_CHOICE_LADDER",
        "get_buy_choices",
        "set_buy_choice",
        "clear_buy_choice",
    ):
        assert not hasattr(store, name), name
    assert "run_buy_choice" not in store.STATE_SCHEMA


# ---------------------------------------------------------------------------
# buys_enabled_owners (R4 + review C6.6)
# ---------------------------------------------------------------------------


def _buy_tx(conn, tid, owner_kind, owner_id):
    conn.execute(
        "INSERT INTO buy_transaction (transaction_id, owner_kind, owner_id, "
        "source_feed, type_id, quantity, unit_price, date, location_id, "
        "fetched_at) VALUES (?, ?, ?, ?, 34, 1, 1.0, '2026-09-20T10:00:00Z', "
        "60003760, '2026-09-20T11:00:00Z')",
        (tid, owner_kind, owner_id, owner_kind),
    )


def test_buys_enabled_owners_follows_count_buys(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    conn.executemany(
        "INSERT INTO pool_character (character_id, character_name, "
        "count_sales, count_buys) VALUES (?, ?, ?, ?)",
        [(1, "A", 1, 1), (2, "B", 1, 0), (3, "C", 0, 1)],
    )
    conn.executemany(
        "INSERT INTO esi_corp (corporation_id, count_sales, count_buys) "
        "VALUES (?, ?, ?)",
        [(90, 1, 1), (91, 1, 0), (92, 0, 1)],
    )
    _pull_row(conn, "corporation", 93, "orders", 0, None, None, 0)  # pull only
    _buy_tx(conn, 1, "corporation", 94)  # seen only as a buyer
    _insert_buy_contract(conn, 1, owner_kind="corporation", owner_id=95)
    _buy_tx(conn, 2, "corporation", 91)  # stored, but its toggle is off
    _buy_tx(conn, 3, "character", 4)  # a character that left the pool
    conn.execute(
        "INSERT INTO sale_transaction (transaction_id, owner_kind, owner_id, "
        "source_feed, type_id, quantity, unit_price, date, location_id, "
        "fetched_at) VALUES (1, 'corporation', 96, 'corporation', 34, 1, 1.0, "
        "'2026-09-20T10:00:00Z', 60003760, '2026-09-20T11:00:00Z')"
    )
    conn.commit()

    assert store.buys_enabled_owners(conn) == {
        ("character", 1),
        ("character", 3),  # Count buys is independent of Count sales
        ("corporation", 90),
        ("corporation", 92),
        ("corporation", 93),
        ("corporation", 94),
        ("corporation", 95),
    }
    conn.close()


def test_buys_enabled_owners_is_empty_on_a_fresh_database(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert store.buys_enabled_owners(conn) == set()
    conn.close()


def test_a_buy_contract_table_without_for_corporation_gains_it(tmp_path, monkeypatch):
    """Review 2026-09-28: for_corporation is an ALTER, not a STATE_SCHEMA
    column, so a dev database whose buy_contract predates it gains the
    column (default 0) on the next open, its rows kept."""
    conn = _db(tmp_path, monkeypatch)
    _insert_buy_contract(conn, 700)
    conn.commit()
    conn.execute("ALTER TABLE buy_contract DROP COLUMN for_corporation")
    conn.commit()
    assert "for_corporation" not in _cols(conn, "buy_contract")
    store.ensure_schema(conn)
    assert "for_corporation" in _cols(conn, "buy_contract")
    assert conn.execute(
        "SELECT contract_id, for_corporation FROM buy_contract"
    ).fetchall()[0][:] == (700, 0)
    conn.close()


# ---------------------------------------------------------------------------
# Revision 4 (user rulings 2026-09-28; contract §2b, §4, amendments A1-A3,
# A9): the 'other' purchase venue and its default freight-in rate, the
# run's persisted default rate, via_type_id, and the run_purchase CHECK
# rebuild for a database already at 12.
# ---------------------------------------------------------------------------


def test_the_default_freight_rate_columns_exist(tmp_path, monkeypatch):
    """settings carries the live rate (NOT NULL, default 0 — the user named
    no figure, A16); index_run carries the rate a run was PLANNED at, NULL
    on a run planned before the column (it then falls back to the live
    setting, the same vintage rule as its two siblings)."""
    conn = _db(tmp_path, monkeypatch)
    cols = {
        r["name"]: r for r in conn.execute("PRAGMA table_info(settings)")
    }
    col = cols["freight_in_default_isk_per_m3"]
    assert col["notnull"] == 1
    assert float(col["dflt_value"]) == 0.0
    assert conn.execute(
        "SELECT freight_in_default_isk_per_m3 FROM settings WHERE id = 1"
    ).fetchone()[0] == 0.0
    assert {
        "freight_in_isk_per_m3",
        "structure_freight_in_isk_per_m3",
        "freight_in_default_isk_per_m3",
    } <= _cols(conn, "index_run")
    run_id = _seed_run(conn)
    assert conn.execute(
        "SELECT freight_in_default_isk_per_m3 FROM index_run "
        "WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()[0] is None
    conn.close()


def test_the_default_freight_rate_round_trips_through_settings(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path, monkeypatch)
    assert store.get_settings(conn).freight_in_default_isk_per_m3 == 0.0
    defaults = {f.name: f.default for f in dataclasses.fields(store.Settings)}
    assert defaults["freight_in_default_isk_per_m3"] == 0.0
    conn.execute(
        "UPDATE settings SET freight_in_isk_per_m3 = 900.0, "
        "structure_freight_in_isk_per_m3 = 300.0, "
        "freight_in_default_isk_per_m3 = 1250.5 WHERE id = 1"
    )
    conn.commit()
    settings = store.get_settings(conn)
    assert settings.freight_in_default_isk_per_m3 == pytest.approx(1250.5)
    # The two siblings are untouched by the new field.
    assert settings.freight_in_isk_per_m3 == pytest.approx(900.0)
    assert settings.structure_freight_in_isk_per_m3 == pytest.approx(300.0)
    conn.close()


def test_freight_in_rate_per_venue(tmp_path, monkeypatch):
    """'other' takes the default rate; 'delivered' is NOT refused — it
    still falls to the Jita leg and the callers guard it themselves
    (contract review A3)."""
    conn = _db(tmp_path, monkeypatch)
    s = dataclasses.replace(
        store.get_settings(conn),
        freight_in_isk_per_m3=900.0,
        structure_freight_in_isk_per_m3=300.0,
        freight_in_default_isk_per_m3=1250.0,
    )
    assert s.freight_in_rate(store.BUY_VENUE_HUB) == 900.0
    assert s.freight_in_rate(store.BUY_VENUE_STRUCTURE) == 300.0
    assert s.freight_in_rate(store.BUY_VENUE_OTHER) == 1250.0
    assert s.freight_in_rate(store.BUY_VENUE_DELIVERED) == 900.0
    assert s.freight_in_rate(None) == 900.0
    # 'other' prices on the hub basis like every non-structure venue.
    assert s.price_basis(store.BUY_VENUE_OTHER) == s.hub_price_basis
    conn.close()


def test_a_database_without_the_default_rate_columns_gains_them(
    tmp_path, monkeypatch
):
    """A dev database at 12 built before revision 4 gains the settings
    column at 0 (no seeding from the Jita rate: A16 leaves it optional and
    the user named no figure) and the index_run column as NULL on the runs
    it already holds."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    conn.execute("ALTER TABLE settings DROP COLUMN freight_in_default_isk_per_m3")
    conn.execute("ALTER TABLE index_run DROP COLUMN freight_in_default_isk_per_m3")
    conn.execute("UPDATE settings SET freight_in_isk_per_m3 = 900.0 WHERE id = 1")
    conn.commit()
    assert "freight_in_default_isk_per_m3" not in _cols(conn, "settings")

    store.ensure_schema(conn)

    assert store.get_settings(conn).freight_in_default_isk_per_m3 == 0.0
    assert store.get_settings(conn).freight_in_isk_per_m3 == 900.0
    assert conn.execute(
        "SELECT freight_in_default_isk_per_m3 FROM index_run "
        "WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()[0] is None
    conn.close()


def test_an_other_venue_line_goes_in_both_ways(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    pid = store.add_purchase(conn, run_id, 34, store.BUY_VENUE_OTHER, 10, 5.0)
    store.replace_derived_purchases(
        conn, run_id, [_line(venue=store.BUY_VENUE_OTHER)]
    )
    conn.commit()
    rows = store.list_purchases(conn, run_id)[34]
    assert [(r["purchase_id"] == pid, r["venue"]) for r in rows] == [
        (True, "other"),
        (False, "other"),
    ]
    # The table's CHECK still refuses a venue outside PURCHASE_VENUES.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run_purchase (index_run_id, type_id, venue, "
            "quantity, unit_price) VALUES (?, 34, 'bogus', 1, 1.0)",
            (run_id,),
        )
    conn.close()


def test_via_type_id_column_and_add_purchase_pass_through(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "via_type_id" in _cols(conn, "run_purchase")
    run_id = _seed_run(conn)
    direct = store.add_purchase(conn, run_id, 34, store.BUY_VENUE_HUB, 10, 5.0)
    via = store.add_purchase(
        conn, run_id, 34, store.BUY_VENUE_DELIVERED, 271, 26.575094,
        via_type_id=62520,
    )
    conn.commit()
    by_pid = {r["purchase_id"]: r for r in store.list_purchases(conn, run_id)[34]}
    assert by_pid[direct]["via_type_id"] is None  # default: a direct line
    assert by_pid[via]["via_type_id"] == 62520
    conn.close()


@pytest.mark.parametrize(
    "venue, via, fragment",
    [
        (store.BUY_VENUE_HUB, 62520, "venue must be 'delivered'"),
        (store.BUY_VENUE_STRUCTURE, 62520, "venue must be 'delivered'"),
        (store.BUY_VENUE_OTHER, 62520, "venue must be 'delivered'"),
        (store.BUY_VENUE_DELIVERED, "ore", "whole-number type id"),
        (store.BUY_VENUE_DELIVERED, 62520.5, "whole-number type id"),
        (store.BUY_VENUE_DELIVERED, True, "whole-number type id"),
        (store.BUY_VENUE_DELIVERED, 0, "whole-number type id"),
        (store.BUY_VENUE_DELIVERED, 2**63, "whole-number type id"),
        (store.BUY_VENUE_DELIVERED, float("inf"), "whole-number type id"),
    ],
)
def test_add_purchase_refuses_a_bad_via_type_id(
    tmp_path, monkeypatch, venue, via, fragment
):
    """A via line is written LANDED (freight and refining tax inside), so
    it is only ever 'delivered' (contract review A9)."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    with pytest.raises(ValueError) as excinfo:
        store.add_purchase(conn, run_id, 34, venue, 10, 5.0, via_type_id=via)
    assert fragment in str(excinfo.value)
    assert store.list_purchases(conn, run_id) == {}
    conn.close()


def test_replace_derived_purchases_writes_via_type_id(tmp_path, monkeypatch):
    """The contract review A7 hand-worked example as lines: the refined
    minerals carry the ore's id, the remainder ore line does not."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    written = store.replace_derived_purchases(
        conn,
        run_id,
        [
            _line(type_id=34, venue=store.BUY_VENUE_DELIVERED, quantity=271,
                  unit_price=26.575094, via_type_id=62520),
            _line(type_id=35, venue=store.BUY_VENUE_DELIVERED, quantity=199,
                  unit_price=66.437736, via_type_id="62520"),
            _line(type_id=62520, quantity=50, unit_price=100.0),
            _line(type_id=36, via_type_id=None),
        ],
    )
    conn.commit()
    assert written == 4
    rows = conn.execute(
        "SELECT type_id, venue, quantity, via_type_id FROM run_purchase "
        "ORDER BY purchase_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (34, "delivered", 271, 62520),
        (35, "delivered", 199, 62520),
        (62520, "hub", 50, None),
        (36, "hub", 1000, None),
    ]
    conn.close()


@pytest.mark.parametrize(
    "over, fragment",
    [
        ({"via_type_id": 62520}, "venue must be 'delivered'"),
        ({"via_type_id": 62520, "venue": "other"}, "venue must be 'delivered'"),
        ({"via_type_id": "ore", "venue": "delivered"}, "whole-number type id"),
        ({"via_type_id": 1.5, "venue": "delivered"}, "whole-number type id"),
    ],
)
def test_a_bad_via_line_leaves_the_run_untouched(
    tmp_path, monkeypatch, over, fragment
):
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    store.replace_derived_purchases(conn, run_id, [_line()])
    conn.commit()
    with pytest.raises(ValueError) as excinfo:
        store.replace_derived_purchases(
            conn, run_id, [_line(esi_id=9002), _line(**{"esi_id": 9003, **over})]
        )
    assert fragment in str(excinfo.value)
    assert [r["esi_id"] for r in store.list_purchases(conn, run_id)[34]] == [9001]
    conn.close()


def test_validate_derived_line_puts_via_type_id_last():
    """buying._line_key / _stored_lines compare against this tuple for the
    no-change check, so a change ONLY in via_type_id must show in it, at
    the one agreed position (contract review A9): the end."""
    base = _line(venue=store.BUY_VENUE_DELIVERED)
    direct = store._validate_derived_line(base)
    via = store._validate_derived_line({**base, "via_type_id": 62520})
    assert len(direct) == len(via) == 12
    assert direct[-1] is None and via[-1] == 62520
    assert direct[:-1] == via[:-1]
    assert direct == (
        34, "delivered", 1000, 5.5, None, "transaction", 9001, None,
        "2026-09-20T10:00:00Z", "character", 1, None,
    )


_OLD_RUN_PURCHASE = """
CREATE TABLE run_purchase (
    purchase_id   INTEGER PRIMARY KEY,
    index_run_id  INTEGER NOT NULL REFERENCES index_run,
    type_id       INTEGER NOT NULL,
    venue         TEXT NOT NULL CHECK (venue IN ('hub','structure','delivered')),
    source        TEXT CHECK (source IN ('sell','split','buy') OR source IS NULL),
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    unit_price    REAL NOT NULL CHECK (unit_price >= 0),
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    esi_kind      TEXT CHECK (esi_kind IN ('transaction','contract') OR esi_kind IS NULL),
    esi_id        INTEGER,
    contract_k    REAL,
    date          TEXT,
    owner_kind    TEXT CHECK (owner_kind IN ('character','corporation') OR owner_kind IS NULL),
    owner_id      INTEGER
)
"""


def _wind_back_to_revision_3(conn) -> None:
    """Make a fresh database look like a dev database stamped 12 by
    revision 3: run_purchase with the three-venue CHECK and no
    via_type_id, both indexes present."""
    conn.execute("DROP INDEX run_purchase_run")
    conn.execute("DROP INDEX run_purchase_esi")
    conn.execute("DROP TABLE run_purchase")
    conn.execute(_OLD_RUN_PURCHASE)
    conn.execute(
        "CREATE INDEX run_purchase_run ON run_purchase (index_run_id, type_id)"
    )
    conn.execute(
        "CREATE INDEX run_purchase_esi "
        "ON run_purchase (index_run_id, esi_kind, esi_id)"
    )
    conn.commit()


def _all_lines(conn) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT purchase_id, index_run_id, type_id, venue, source, quantity, "
            "unit_price, note, created_at, esi_kind, esi_id, contract_k, date, "
            "owner_kind, owner_id FROM run_purchase ORDER BY purchase_id"
        )
    ]


def _run_purchase_sql(conn) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'run_purchase'"
    ).fetchone()[0]


def test_a_revision_3_database_at_12_gets_the_other_venue_check(
    tmp_path, monkeypatch
):
    """SQLite cannot alter a CHECK, and on a database already at 12 the
    old one refuses every 'other' line — one of which would roll back the
    matcher's whole pass. ensure_schema rebuilds the table (contract
    review A2): every row survives verbatim (purchase_id included), both
    indexes exist on the NEW table, the other CHECKs and the foreign key
    still hold, and no version bump or backup happens."""
    conn = _db(tmp_path, monkeypatch)
    conn.execute("PRAGMA foreign_keys = ON")
    run_id = _seed_run(conn)
    _wind_back_to_revision_3(conn)
    assert "'other'" not in _run_purchase_sql(conn)
    # Raw INSERTs: add_purchase now writes via_type_id, which a revision-3
    # table does not have yet.
    conn.execute(
        "INSERT INTO run_purchase (purchase_id, index_run_id, type_id, venue, "
        "source, quantity, unit_price, note) "
        "VALUES (41, ?, 36, 'structure', 'split', 7, 3.25, 'hand line')",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO run_purchase (purchase_id, index_run_id, type_id, venue, "
        "quantity, unit_price, esi_kind, esi_id, contract_k, date, owner_kind, "
        "owner_id) VALUES (57, ?, 34, 'delivered', 1000, 5.5, 'contract', 777, "
        "0.97, '2026-09-20T10:00:00Z', 'corporation', 98)",
        (run_id,),
    )
    conn.commit()
    before = _all_lines(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
            "unit_price) VALUES (?, 34, 'other', 1, 1.0)",
            (run_id,),
        )
    conn.rollback()

    store.ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert not list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))
    sql = _run_purchase_sql(conn)
    assert "('hub','structure','delivered','other')" in sql
    assert "run_purchase_new" not in _tables(conn)
    assert _all_lines(conn) == before
    assert [
        r["via_type_id"]
        for r in conn.execute("SELECT via_type_id FROM run_purchase")
    ] == [None, None]
    assert [
        r["name"] for r in conn.execute("PRAGMA table_info(run_purchase)")
    ] == list(store._RUN_PURCHASE_COLUMNS)
    assert {"run_purchase_run", "run_purchase_esi"} <= _indexes(conn)
    assert [
        r["name"] for r in conn.execute("PRAGMA index_info(run_purchase_run)")
    ] == ["index_run_id", "type_id"]
    assert [
        r["name"] for r in conn.execute("PRAGMA index_info(run_purchase_esi)")
    ] == ["index_run_id", "esi_kind", "esi_id"]
    # Both indexes hang off the rebuilt table, not a dropped one.
    assert {
        r["tbl_name"]
        for r in conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type = 'index' "
            "AND name IN ('run_purchase_run', 'run_purchase_esi')"
        )
    } == {"run_purchase"}

    # 'other' goes in, through the helpers and raw SQL alike.
    store.add_purchase(conn, run_id, 34, store.BUY_VENUE_OTHER, 1, 1.0)
    store.replace_derived_purchases(
        conn, run_id, [_line(venue=store.BUY_VENUE_OTHER, esi_id=9100)]
    )
    conn.commit()
    # ... while every other CHECK still refuses.
    for column, bad in (
        ("venue", "'bogus'"),
        ("esi_kind", "'wallet'"),
        ("owner_kind", "'alliance'"),
        ("source", "'ask'"),
    ):
        cols = "index_run_id, type_id, quantity, unit_price" + (
            "" if column == "venue" else ", venue"
        )
        vals = "?, 34, 1, 1.0" + ("" if column == "venue" else ", 'hub'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO run_purchase ({cols}, {column}) "
                f"VALUES ({vals}, {bad})",
                (run_id,),
            )
        conn.rollback()
    for column, bad in (("quantity", 0), ("unit_price", -1.0)):
        other = {"quantity": 1, "unit_price": 1.0, column: bad}
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO run_purchase (index_run_id, type_id, venue, "
                "quantity, unit_price) VALUES (?, 34, 'hub', ?, ?)",
                (run_id, other["quantity"], other["unit_price"]),
            )
        conn.rollback()
    # created_at keeps its DEFAULT on the rebuilt table.
    assert all(
        r["created_at"]
        for r in conn.execute("SELECT created_at FROM run_purchase")
    )
    # The foreign key still blocks deleting a run that holds lines.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run_id,))
    conn.rollback()
    store.delete_run_purchases(conn, run_id)
    conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run_id,))
    conn.commit()
    conn.close()


def test_the_rebuild_runs_once(tmp_path, monkeypatch):
    """A second ensure_schema finds 'other' in the stored SQL and leaves
    the table (and its rows) exactly as they are."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    _wind_back_to_revision_3(conn)
    conn.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
        "unit_price) VALUES (?, 34, 'hub', 5, 2.0)",
        (run_id,),
    )
    conn.commit()
    store.ensure_schema(conn)
    sql = _run_purchase_sql(conn)
    rows = _all_lines(conn)
    calls = []
    real = conn.execute

    class Spy:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, statement, *args):
            calls.append(statement)
            return real(statement, *args)

    store._rebuild_run_purchase_for_other_venue(Spy())
    assert not any("run_purchase_new" in c for c in calls)
    store.ensure_schema(conn)
    assert _run_purchase_sql(conn) == sql
    assert _all_lines(conn) == rows
    conn.close()


def test_a_fresh_database_needs_no_rebuild(tmp_path, monkeypatch):
    """STATE_SCHEMA already carries the four-venue CHECK, which is what a
    fresh database and the 11 -> 12 upgrade (production) create."""
    assert (
        "CHECK (venue IN ('hub','structure','delivered','other'))"
        in store.STATE_SCHEMA
    )
    conn = _db(tmp_path, monkeypatch)
    assert "'other'" in _run_purchase_sql(conn)
    assert "via_type_id" in _cols(conn, "run_purchase")
    conn.close()


def test_the_rebuild_drops_orphan_lines_and_says_so(
    tmp_path, monkeypatch, caplog
):
    """Connections run with foreign_keys = ON, so copying a line whose run
    is gone would make ensure_schema raise at every start-up (A2.5). The
    rebuild copies only lines whose run exists and logs the rest."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    _wind_back_to_revision_3(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
        "unit_price) VALUES (?, 34, 'hub', 5, 2.0)",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
        "unit_price) VALUES (999, 35, 'hub', 5, 2.0)"
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    with caplog.at_level("WARNING", logger=store.log.name):
        store.ensure_schema(conn)

    assert [
        (r["index_run_id"], r["type_id"])
        for r in conn.execute("SELECT index_run_id, type_id FROM run_purchase")
    ] == [(run_id, 34)]
    assert "dropped 1 run_purchase lines" in caplog.text
    assert "'other'" in _run_purchase_sql(conn)
    conn.close()


def test_a_failed_rebuild_keeps_the_old_table(tmp_path, monkeypatch):
    """One transaction: an error mid-rebuild rolls back to the old table
    with every line, and nothing half-built is left behind."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    _wind_back_to_revision_3(conn)
    conn.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
        "unit_price) VALUES (?, 34, 'hub', 5, 2.0)",
        (run_id,),
    )
    conn.execute("ALTER TABLE run_purchase ADD COLUMN via_type_id INTEGER")
    conn.commit()
    before = _all_lines(conn)
    monkeypatch.setattr(
        store, "_RUN_PURCHASE_COLUMNS", (*store._RUN_PURCHASE_COLUMNS, "nope")
    )
    with pytest.raises(sqlite3.OperationalError):
        store._rebuild_run_purchase_for_other_venue(conn)
    assert not conn.in_transaction
    assert "'other'" not in _run_purchase_sql(conn)
    assert "run_purchase_new" not in _tables(conn)
    assert _all_lines(conn) == before
    assert {"run_purchase_run", "run_purchase_esi"} <= _indexes(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Review 2026-09-28: frozen ore conversions kept apart from the lines, and
# the structure market a run was planned against
# ---------------------------------------------------------------------------


def test_the_refine_freeze_table_and_structure_market_column_exist(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    assert "run_purchase_refine" in _tables(conn)
    assert "structure_market_id" in _cols(conn, "index_run")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    run = _seed_run(conn)
    assert conn.execute(
        "SELECT structure_market_id FROM index_run WHERE index_run_id = ?", (run,)
    ).fetchone()[0] is None
    conn.close()


def test_a_database_at_12_without_them_gains_them(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    conn.execute("DROP TABLE run_purchase_refine")
    conn.execute("ALTER TABLE index_run DROP COLUMN structure_market_id")
    conn.commit()
    assert "run_purchase_refine" not in _tables(conn)
    store.ensure_schema(conn)
    assert "run_purchase_refine" in _tables(conn)
    assert "structure_market_id" in _cols(conn, "index_run")
    conn.close()


def test_the_refine_freeze_round_trips_and_keeps_groups_not_named(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    conn.execute("PRAGMA foreign_keys = ON")
    r1, r2 = _seed_run(conn, 1), _seed_run(conn, 2)
    a = {("transaction", 7, 62520): (None, ((34, 271, 26.5), (35, 199, 66.4)))}
    b = {("contract", 9, 62520): (0.75, ((34, 100, 20.0),))}
    assert store.save_refine_freeze(conn, r1, {**a, **b}) == 2
    assert store.refine_freeze(conn, r1) == {**a, **b}
    # A later save of one group replaces it and leaves the other alone.
    a2 = {("transaction", 7, 62520): (None, ((34, 150, 48.0),))}
    store.save_refine_freeze(conn, r1, a2)
    assert store.refine_freeze(conn, r1) == {**a2, **b}
    store.save_refine_freeze(conn, r2, b)
    store.clear_refine_freeze(conn, [r2])
    assert store.refine_freeze(conn, r1) == {} and store.refine_freeze(conn, r2) == b
    store.clear_refine_freeze(conn, [])
    assert store.refine_freeze(conn, r2) == {}
    # The CHECKs refuse what a derived line could never be.
    for bad in ((("transaction", 7, 1), (None, ((34, 0, 1.0),))),
                (("wallet", 7, 1), (None, ((34, 1, 1.0),))),
                (("transaction", 7, 1), (None, ((34, 1, -1.0),)))):
        with pytest.raises(sqlite3.IntegrityError):
            store.save_refine_freeze(conn, r1, dict([bad]))
    conn.rollback()
    conn.close()


def test_delete_run_purchases_clears_the_runs_frozen_conversions(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    conn.execute("PRAGMA foreign_keys = ON")
    run = _seed_run(conn)
    store.save_refine_freeze(conn, run, {("transaction", 7, 62520): (None, ((34, 1, 1.0),))})
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run,))
    store.delete_run_purchases(conn, run)
    conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (run,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM run_purchase_refine").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Revision 7 (user ruling 2026-09-29): installed_qty and the job_starts
# list format — this cycle's installed finals count, no re-plan stop rule
# ---------------------------------------------------------------------------


def test_installed_qty_is_a_migration_defaulting_to_null(tmp_path, monkeypatch):
    """Schema 12 is unreleased, so installed_qty rides _MIGRATIONS with no
    bump (amendment 10); a row inserted without it reads NULL (planned
    before the column), which every reader treats as 0."""
    assert store.SCHEMA_VERSION == 12
    assert "installed_qty" not in store.STATE_SCHEMA
    assert (
        "ALTER TABLE index_run_item ADD COLUMN installed_qty INTEGER"
        in store._MIGRATIONS
    )
    conn = _db(tmp_path, monkeypatch)
    assert "installed_qty" in _cols(conn, "index_run_item")
    run_id = _seed_run(conn)
    conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id) VALUES (?, 34)", (run_id,)
    )
    assert conn.execute(
        "SELECT installed_qty FROM index_run_item"
    ).fetchone()[0] is None
    conn.close()


def test_a_database_at_12_without_installed_qty_gains_it(tmp_path, monkeypatch):
    """A revision-6 dev database already stamped 12 gains the column on
    the next open — no backup, older rows NULL, idempotent."""
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id, on_hand_qty) "
        "VALUES (?, 34, 7)",
        (run_id,),
    )
    conn.commit()
    conn.execute("ALTER TABLE index_run_item DROP COLUMN installed_qty")
    conn.commit()
    assert "installed_qty" not in _cols(conn, "index_run_item")

    store.ensure_schema(conn)
    store.ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert not list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))
    row = conn.execute(
        "SELECT on_hand_qty, installed_qty FROM index_run_item"
    ).fetchone()
    assert tuple(row) == (7, None)
    conn.close()


def test_requested_and_wave_qty_are_migrations_a_12_database_gains(
    tmp_path, monkeypatch
):
    """Revision 7 fix pass: a final's request and the wave the engine
    sized against ride _MIGRATIONS under the unreleased schema 12 like
    installed_qty — a dev database already at 12 gains them on the next
    open, no backup, older rows NULL (readers fall back to the target)."""
    assert store.SCHEMA_VERSION == 12
    for col in ("requested_qty", "wave_qty"):
        assert col not in store.STATE_SCHEMA
        assert (
            f"ALTER TABLE index_run_item ADD COLUMN {col} INTEGER"
            in store._MIGRATIONS
        )
    conn = _db(tmp_path, monkeypatch)
    run_id = _seed_run(conn)
    conn.execute(
        "INSERT INTO index_run_item (index_run_id, type_id) VALUES (?, 34)",
        (run_id,),
    )
    conn.commit()
    conn.execute("ALTER TABLE index_run_item DROP COLUMN wave_qty")
    conn.execute("ALTER TABLE index_run_item DROP COLUMN requested_qty")
    conn.commit()

    store.ensure_schema(conn)
    store.ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert not list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))
    row = conn.execute(
        "SELECT requested_qty, wave_qty FROM index_run_item"
    ).fetchone()
    assert tuple(row) == (None, None)
    conn.close()


def _snapshot(conn, job_starts):
    store.save_esi_snapshot(
        conn, {34: 10}, {}, {}, 0.0, 0.0, job_ends={}, job_starts=job_starts
    )
    return store.latest_esi_snapshot(conn)


def test_job_starts_list_format_round_trips(tmp_path, monkeypatch):
    """{type_id: [[start, units], ...]} — one pair per job, delivered ones
    included — comes back with int keys and the pairs intact."""
    conn = _db(tmp_path, monkeypatch)
    starts = {
        22544: [["2026-09-25T09:30:00Z", 4], ["2026-09-25T11:00:00Z", 4]],
        11535: [["2026-09-24T08:00:00Z", 100]],
    }
    snap = _snapshot(conn, starts)
    assert snap["job_starts"] == starts
    conn.close()


def test_empty_job_starts_is_recorded_not_unknown(tmp_path, monkeypatch):
    """{} means "no jobs" (recorded); only NULL is "not recorded"."""
    conn = _db(tmp_path, monkeypatch)
    assert _snapshot(conn, {})["job_starts"] == {}
    assert _snapshot(conn, None)["job_starts"] is None
    conn.close()


def test_the_old_scalar_job_starts_format_reads_as_not_recorded(
    tmp_path, monkeypatch
):
    """The pre-2026-09-29 format ({type_id: latest start text}) carries no
    units: latest_esi_snapshot — the one reader that normalises — turns it
    into None, so the plan sizes the full wave until the next ESI update."""
    conn = _db(tmp_path, monkeypatch)
    assert _snapshot(conn, {22544: "2026-09-25T09:30:00Z"})["job_starts"] is None
    # A mix (one scalar value) is the old format too.
    mixed = {22544: "2026-09-25T09:30:00Z", 11535: [["2026-09-24T08:00:00Z", 1]]}
    assert _snapshot(conn, mixed)["job_starts"] is None
    conn.close()
