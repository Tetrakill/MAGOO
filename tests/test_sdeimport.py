"""SDE re-import atomicity: the drop-and-recreate of the ref tables and the
data inserts share ONE transaction, so a mid-import failure (CCP schema
drift, power loss) rolls back to the previous working build instead of
leaving every ref table committed-empty."""

import sqlite3

import pytest

from magoo import sdeimport


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ref.sqlite")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def seed_build_100(conn):
    conn.execute("BEGIN IMMEDIATE")
    sdeimport._rebuild_ref_schema(conn)
    conn.execute("INSERT INTO ref_category VALUES (6, 'Ship')")
    conn.execute(
        "INSERT INTO ref_sde_build VALUES (100, datetime('now'))"
    )
    conn.commit()


def test_failed_reimport_preserves_previous_build(conn):
    seed_build_100(conn)

    # Re-import of build 101 crashes after the rebuild, mid-insert —
    # replicating run_import's single-transaction call sequence.
    with pytest.raises(KeyError):
        conn.execute("BEGIN IMMEDIATE")
        sdeimport._rebuild_ref_schema(conn)
        conn.execute("INSERT INTO ref_category VALUES (8, 'Charge')")
        raise KeyError("groupID")  # CCP schema drift mid-dataset
    conn.rollback()

    row = conn.execute("SELECT * FROM ref_category").fetchall()
    assert [(r["category_id"], r["name"]) for r in row] == [(6, "Ship")]
    build = conn.execute("SELECT build_number FROM ref_sde_build").fetchone()
    assert build["build_number"] == 100


def test_failed_reimport_survives_connection_close(conn, tmp_path):
    """The run_import finally-block closes the connection on failure — the
    implicit rollback must restore the old build for the next open."""
    seed_build_100(conn)
    conn.execute("BEGIN IMMEDIATE")
    sdeimport._rebuild_ref_schema(conn)
    conn.close()  # crash: nothing committed

    reopened = sqlite3.connect(tmp_path / "ref.sqlite")
    reopened.row_factory = sqlite3.Row
    try:
        build = reopened.execute(
            "SELECT build_number FROM ref_sde_build"
        ).fetchone()
        assert build["build_number"] == 100
        rows = reopened.execute("SELECT COUNT(*) AS n FROM ref_category").fetchone()
        assert rows["n"] == 1
    finally:
        reopened.close()
