"""State schema and settings integrity.

get_settings builds the Settings dataclass POSITIONALLY, so a drifting
column/field order silently mis-assigns every later value — the roundtrip
tests here are the tripwire for that.
"""

import sqlite3

import pytest

from magoo import config, store


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


def test_ensure_schema_idempotent(conn):
    store.ensure_schema(conn)  # second run: no error, no duplicate seeds
    store.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) n FROM settings").fetchone()["n"] == 1
    # Migrations reapplied idempotently — settings still readable.
    assert store.get_settings(conn) is not None


def test_settings_defaults_fresh_db(conn):
    s = store.get_settings(conn)
    assert s.stockpile_buffer == 0.05
    assert s.price_region_id == 10000002
    assert s.skill_accounting == 5
    assert s.capital_market_mode == "cj6"
    assert s.capital_structure_id is None
    assert s.capital_scc_surcharge == 0.015
    assert s.freight_in_isk_per_m3 == 0.0
    # v1.9
    assert s.skill_outpost_construction == 5
    assert s.count_fitted_stock is False
    # 2026-08-23: the NPC-goods fallback region IS the price region — the
    # separate v1.9 column is gone (and dropped from upgraded databases).
    assert not hasattr(s, "npc_goods_region_id")
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
    assert "npc_goods_region_id" not in columns


def test_settings_roundtrip_v19_columns(conn):
    conn.execute(
        "UPDATE settings SET skill_outpost_construction = 2, "
        "count_fitted_stock = 1 WHERE id = 1"
    )
    conn.commit()
    s = store.get_settings(conn)
    assert s.skill_outpost_construction == 2
    assert s.count_fitted_stock is True


def test_upgrade_drops_retired_settings_columns(conn):
    """A pre-2026-08-23 database carries npc_goods_region_id, sell_venue and
    structure_broker_rate; ensure_schema drops them and settings still
    load (the price region covers the fallback; NPC station is the only
    sell venue)."""
    conn.execute(
        "ALTER TABLE settings ADD COLUMN npc_goods_region_id INTEGER "
        "NOT NULL DEFAULT 10000002"
    )
    conn.execute(
        "ALTER TABLE settings ADD COLUMN sell_venue TEXT NOT NULL DEFAULT 'npc'"
    )
    conn.execute(
        "ALTER TABLE settings ADD COLUMN structure_broker_rate REAL "
        "NOT NULL DEFAULT 0.01"
    )
    conn.commit()
    store.ensure_schema(conn)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
    assert not columns & {"npc_goods_region_id", "sell_venue", "structure_broker_rate"}
    assert store.get_settings(conn).price_region_id == 10000002


def test_new_item_class_seeds_from_other_row(conn):
    """A class added to config.ITEM_CLASSES after a database exists is
    seeded as a copy of the user's 'other' row (the facility those items
    were planned under until the class existed), not the NPC defaults."""
    conn.execute(
        "UPDATE class_setting SET structure_type_id = 35827, security = -0.5, "
        "me_rig = 't1', te_rig = 't2', system_cost_index = 0.0014, "
        "tax_rate = 0.003 WHERE item_class = 'other'"
    )
    conn.execute("DELETE FROM class_setting WHERE item_class = 'structures'")
    conn.commit()
    store.ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM class_setting WHERE item_class = 'structures'"
    ).fetchone()
    assert row is not None
    assert row["structure_type_id"] == 35827
    assert row["security"] == -0.5
    assert row["me_rig"] == "t1" and row["te_rig"] == "t2"
    assert row["system_cost_index"] == 0.0014
    assert row["tax_rate"] == 0.003
    # and every configured class is present exactly once
    classes = [
        r["item_class"]
        for r in conn.execute("SELECT item_class FROM class_setting")
    ]
    assert sorted(classes) == sorted(config.ITEM_CLASSES)


def test_settings_roundtrip_v15_v16_columns(conn):
    conn.execute(
        "UPDATE settings SET skill_accounting = 4, skill_broker_relations = 3, "
        "standing_broker_faction = 5.5, standing_broker_corp = -2.0, "
        "freight_in_isk_per_m3 = 400, freight_out_isk_per_m3 = 750, "
        "capital_market_mode = 'custom', capital_structure_id = 12345, "
        "capital_sales_tax = 0.036, capital_broker_rate = 0.02, "
        "capital_movement_cost_isk = 75000000, capital_scc_surcharge = 0.0225 "
        "WHERE id = 1"
    )
    conn.commit()
    s = store.get_settings(conn)
    assert s.skill_accounting == 4
    assert s.skill_broker_relations == 3
    assert s.standing_broker_faction == 5.5
    assert s.standing_broker_corp == -2.0
    assert s.freight_in_isk_per_m3 == 400
    assert s.freight_out_isk_per_m3 == 750
    assert s.capital_market_mode == "custom"
    assert s.capital_structure_id == 12345
    assert s.capital_sales_tax == 0.036
    assert s.capital_broker_rate == 0.02
    assert s.capital_movement_cost_isk == 75000000
    assert s.capital_scc_surcharge == 0.0225
    assert s.capital_structure() == 12345


def test_index_run_status_check_constraint(conn):
    conn.execute(
        "INSERT INTO index_run (run_number, status) VALUES (1, 'complete')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO index_run (run_number, status) VALUES (2, 'bogus')"
        )


def test_market_price_pk_upsert(conn):
    conn.execute(
        "INSERT OR REPLACE INTO market_price (type_id, region_id, source, price, fetched_at) "
        "VALUES (34, 999, 'structure', 90.0, '2026-01-01')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO market_price (type_id, region_id, source, price, fetched_at) "
        "VALUES (34, 999, 'structure', 80.0, '2026-01-02')"
    )
    rows = conn.execute(
        "SELECT price FROM market_price WHERE type_id = 34 AND region_id = 999"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["price"] == 80.0


def test_me_te_resolver_precedence(conn):
    """Explicit blueprint_setting beats the global intermediate defaults;
    reactions are hardwired to (0, 0) — no test ever asserted this."""
    conn.execute(
        "UPDATE settings SET default_intermediate_me = 10, "
        "default_intermediate_te = 20"
    )
    conn.execute("INSERT INTO blueprint_setting VALUES (999, 5, 12)")
    conn.commit()
    resolve = store.me_te_resolver(conn)
    assert resolve(999, 1) == (5, 12)  # explicit row
    assert resolve(999, 11) == (0, 0)  # reactions have no ME/TE research
    assert resolve(1000, 1) == (10, 20)  # global defaults


def test_run_number_unique_backstop(conn):
    conn.execute(
        "INSERT INTO index_run (run_number, planned_start, status) "
        "VALUES (1, datetime('now'), 'planned')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO index_run (run_number, planned_start, status) "
            "VALUES (1, datetime('now'), 'planned')"
        )


def test_class_setting_accepts_thukker_tier(conn):
    conn.execute(
        "UPDATE class_setting SET me_rig = 'thukker', te_rig = 'thukker' "
        "WHERE item_class = 'basic_capital_components'"
    )
    row = conn.execute(
        "SELECT me_rig, te_rig FROM class_setting "
        "WHERE item_class = 'basic_capital_components'"
    ).fetchone()
    assert (row["me_rig"], row["te_rig"]) == ("thukker", "thukker")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE class_setting SET me_rig = 'bogus' "
            "WHERE item_class = 'other'"
        )


def test_class_setting_thukker_rebuild_preserves_rows(tmp_path):
    """A pre-thukker database (old CHECK constraint) gets a create-copy-swap
    rebuild on ensure_schema, keeping every stored class row."""
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE class_setting (
            item_class        TEXT PRIMARY KEY,
            structure_type_id INTEGER,
            security          REAL NOT NULL DEFAULT 1.0,
            me_rig            TEXT NOT NULL DEFAULT 'none'
                              CHECK (me_rig IN ('none','t1','t2')),
            te_rig            TEXT NOT NULL DEFAULT 'none'
                              CHECK (te_rig IN ('none','t1','t2')),
            system_cost_index REAL NOT NULL DEFAULT 0.0,
            tax_rate          REAL NOT NULL DEFAULT 0.0025
        )
        """
    )
    conn.execute(
        "INSERT INTO class_setting VALUES "
        "('capital_ships', 35826, -0.5, 't2', 't2', 0.0014, 0.0025)"
    )
    conn.commit()
    store.ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM class_setting WHERE item_class = 'capital_ships'"
    ).fetchone()
    assert row["structure_type_id"] == 35826
    assert row["security"] == -0.5
    assert (row["me_rig"], row["te_rig"]) == ("t2", "t2")
    # and the rebuilt table accepts the new tier
    conn.execute(
        "UPDATE class_setting SET me_rig = 'thukker' "
        "WHERE item_class = 'capital_ships'"
    )
    conn.close()


def test_industry_scc_surcharge_setting_roundtrip(conn):
    assert store.get_settings(conn).industry_scc_surcharge == 0.04  # default
    conn.execute("UPDATE settings SET industry_scc_surcharge = 0.025")
    conn.commit()
    assert store.get_settings(conn).industry_scc_surcharge == 0.025
