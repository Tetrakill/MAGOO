"""Game data whose tables predate this version is imported again (v1.25).

A v1.24 database carries a current CCP build whose ref tables lack
ref_compressible and ref_type.portion_size. Without these rules the
"Check for updates" button reports "already up to date" and compressed
sourcing silently never has a candidate.
"""

import sqlite3

import pytest

from magoo import config, sdeimport, web

from tests.test_sde_button import _write_tiny_sde_zip


@pytest.fixture()
def offline_app_db(tmp_path, monkeypatch):
    """A temp database with the tiny build imported, plus an app factory."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SDE_CACHE_DIR", tmp_path / "sde")
    monkeypatch.setenv("MAGOO_SECRET", "test-secret")
    archive = _write_tiny_sde_zip(tmp_path / "sde-999.zip")
    monkeypatch.setattr(sdeimport, "fetch_latest_build", lambda client: 999)
    monkeypatch.setattr(
        sdeimport,
        "download_sde_zip",
        lambda client, build, progress=None: archive,
    )
    assert sdeimport.run_import() is True
    return config.DB_PATH


def _age(db_path, *statements):
    """Make the imported build look like an older version wrote it."""
    conn = sqlite3.connect(db_path)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def _drop_column(db_path, table, column):
    """Rebuild `table` without `column` (SQLite's DROP COLUMN refuses a
    DDL that carries comments, which REF_SCHEMA's ref_type does)."""
    conn = sqlite3.connect(db_path)
    keep = [
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
        if row[1] != column
    ]
    assert len(keep) + 1 == len(list(conn.execute(f"PRAGMA table_info({table})")))
    conn.execute(f"CREATE TABLE {table}__old AS SELECT {', '.join(keep)} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}__old RENAME TO {table}")
    conn.commit()
    conn.close()


def test_outdated_is_false_on_a_fresh_install_and_on_a_current_build(
    tmp_path, offline_app_db
):
    fresh = sqlite3.connect(tmp_path / "empty.sqlite")
    fresh.row_factory = sqlite3.Row
    assert sdeimport.ref_schema_outdated(fresh) is False  # no ref tables
    fresh.close()

    conn = sqlite3.connect(offline_app_db)
    conn.row_factory = sqlite3.Row
    assert sdeimport.ref_schema_outdated(conn) is False
    conn.close()


def test_outdated_when_a_table_or_a_column_is_missing(offline_app_db):
    _age(offline_app_db, "DROP TABLE ref_compressible")
    conn = sqlite3.connect(offline_app_db)
    conn.row_factory = sqlite3.Row
    assert sdeimport.ref_schema_outdated(conn) is True
    conn.close()

    assert sdeimport.run_import(force=True) is True  # back to current
    _drop_column(offline_app_db, "ref_type", "portion_size")
    conn = sqlite3.connect(offline_app_db)
    conn.row_factory = sqlite3.Row
    assert sdeimport.ref_schema_outdated(conn) is True
    conn.close()


def test_run_import_reimports_an_outdated_build_without_force(offline_app_db):
    _age(offline_app_db, "DROP TABLE ref_compressible")
    events = []
    assert sdeimport.run_import(progress=events.append) is True
    assert {
        "stage": "resolved", "build": 999, "had": 999, "outdated": True
    } in events
    conn = sqlite3.connect(offline_app_db)
    assert conn.execute(
        "SELECT compressed_type_id FROM ref_compressible WHERE type_id = 1230"
    ).fetchone() == (62516,)
    conn.close()
    # And now it is current again: the plain check no-ops.
    events = []
    assert sdeimport.run_import(progress=events.append) is False
    assert events == [{"stage": "check"}, {"stage": "current", "build": 999}]


def test_status_message_names_the_reimport():
    assert "predates this version" in web._sde_message(
        {"state": "running", "stage": "resolved", "build": 999, "outdated": True}
    )
    assert web._sde_message(
        {"state": "running", "stage": "resolved", "build": 999}
    ) == "build 999 — starting download"


def test_app_reimports_outdated_game_data_on_its_first_request(offline_app_db):
    _age(offline_app_db, "DROP TABLE ref_compressible")
    app = web.create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        assert client.get("/").status_code == 200
        job = app.extensions["sde_import"]
        assert job.wait(10)
        status = client.get("/sde/status").get_json()
        assert (status["state"], status["changed"]) == ("done", True)
        html = client.get("/").get_data(as_text=True)
    assert "needs re-import" not in html
    assert "⟳ Check for updates" in html
    conn = sqlite3.connect(offline_app_db)
    assert conn.execute("SELECT COUNT(*) FROM ref_compressible").fetchone()[0] == 1
    conn.close()


def test_dashboard_flags_an_outdated_build_the_job_did_not_fix(
    offline_app_db, monkeypatch
):
    """The auto-start ran but could not import (offline, say): the step
    turns back into a to-do with the re-import button, the setup panel
    opens even though it was dismissed, and the job was asked once."""
    _age(offline_app_db, "DROP TABLE ref_compressible")
    calls = []

    def fake_run(force=False, progress=None):
        calls.append(force)
        raise RuntimeError("no network")

    monkeypatch.setattr(sdeimport, "run_import", fake_run)
    app = web.create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        client.get("/")
        assert app.extensions["sde_import"].wait(10)
        html = client.get("/").get_data(as_text=True)
    assert calls == [False]
    assert "needs re-import" in html
    assert "↓ Re-import game data" in html
    assert "⟳ Check for updates" not in html
    assert "failed — no network" in html
    # The panel's dismissal is ignored while the data needs attention.
    assert "if (false" in html


def test_app_does_not_start_an_import_on_a_current_build(
    offline_app_db, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        sdeimport, "run_import", lambda force=False, progress=None: calls.append(1)
    )
    app = web.create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        html = client.get("/?setup=1").get_data(as_text=True)
    assert calls == []
    assert "needs re-import" not in html
    assert "⟳ Check for updates" in html
