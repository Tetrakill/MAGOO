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


# --- review 2026-09-05: compression targets, the zip cache -----------------


def test_compressed_outputs_are_not_duplicated_by_a_shared_target(conn):
    """Several raws (Veldspar and its Concentrated / Dense variants) map
    to ONE compressed type in compressibleTypes; the join must not repeat
    that type's outputs once per raw (it doubled the per-batch yield the
    LP planned on) — SELECT DISTINCT in refdata.compressed_sources."""
    from magoo.refdata import Refdata

    conn.execute("BEGIN IMMEDIATE")
    sdeimport._rebuild_ref_schema(conn)
    conn.execute("INSERT INTO ref_category VALUES (25, 'Asteroid')")
    conn.execute("INSERT INTO ref_group VALUES (462, 25, 'Veldspar')")
    conn.executemany(
        "INSERT INTO ref_type VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1230, "Veldspar", 462, 25, 0.1, None, 1, 100),
            (17470, "Concentrated Veldspar", 462, 25, 0.1, None, 1, 100),
            (17471, "Dense Veldspar", 462, 25, 0.1, None, 1, 100),
            (62516, "Compressed Veldspar", 462, 25, 0.001, None, 1, 100),
            (34, "Tritanium", 18, 4, 0.01, None, 1, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO ref_compressible VALUES (?, ?)",
        [(1230, 62516), (17470, 62516), (17471, 62516)],
    )
    conn.execute("INSERT INTO ref_type_material VALUES (62516, 34, 400)")
    conn.commit()
    sources = Refdata(conn).compressed_sources()
    assert set(sources) == {62516}
    assert sources[62516].outputs == ((34, 400),)
    assert sources[62516].batch_output(2, 34, 0.75) == 600


def test_batch_output_floors_once_over_the_job():
    """2026-09-06 (reversing R3): the client floors a reprocessing run's
    output ONCE per material over the whole job — 415 base at 0.55 is
    228.25 per batch, so 4 batches give floor(913.0) = 913, not 4 × 228.
    The per-batch floor valued every compressed gas (a batch of one
    yielding one) at zero below a 100% yield."""
    from magoo.refdata import CompressedSource

    src = CompressedSource(
        compressed_id=1, kind="ore", portion_size=100, outputs=((34, 415),)
    )
    assert src.batch_output(4, 34, 0.55) == 913
    assert src.batch_output(1, 34, 0.55) == 228
    assert src.batch_output(0, 34, 0.55) == 0
    assert src.batch_output(4, 35, 0.55) == 0  # not an output
    # per_unit stays continuous — it is the LP coefficient, not a count.
    assert src.per_unit(34, 0.55) == pytest.approx(415 / 100 * 0.55)
    # An exactly-integral product survives binary-float noise.
    exact = CompressedSource(1, "ore", 100, ((34, 400),))
    assert exact.batch_output(3, 34, 0.75) == 900
    gas = CompressedSource(1, "gas", 1, ((30375, 1),))
    assert gas.batch_output(1, 30375, 0.95) == 0  # one unit at 95% is 0.95
    assert gas.batch_output(100, 30375, 0.95) == 95
    assert gas.batch_output(7, 30375, 0.55) == 3


def _tiny_archive(path, datasets):
    """A structurally valid SDE zip whose members are all empty."""
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        for name in datasets:
            zf.writestr(f"{name}.jsonl", "")
    return path


def _run_import_offline(monkeypatch, tmp_path, build, datasets):
    """run_import against a temp data dir with the network stubbed out:
    the 'latest' build is ``build`` and its archive is the tiny zip."""
    from magoo import config

    cache = tmp_path / "sde"
    cache.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "magoo.sqlite")
    monkeypatch.setattr(config, "SDE_CACHE_DIR", cache)
    archive = _tiny_archive(
        cache / f"eve-online-static-data-{build}-jsonl.zip", datasets
    )
    monkeypatch.setattr(sdeimport, "fetch_latest_build", lambda client: build)
    monkeypatch.setattr(
        sdeimport, "download_sde_zip", lambda client, b, progress=None: archive
    )
    return sdeimport.run_import(force=True)


def test_missing_compressible_member_does_not_block_the_import(
    monkeypatch, tmp_path, caplog
):
    """compressibleTypes is optional (review 2026-09-05): an archive
    without it imports everything else, logs a warning and leaves
    compressed sourcing inert; every other member stays required."""
    from magoo import config
    from magoo.refdata import Refdata

    datasets = [d for d in config.SDE_DATASETS if d != "compressibleTypes"]
    with caplog.at_level("WARNING", logger="magoo.sdeimport"):
        assert _run_import_offline(monkeypatch, tmp_path, 101, datasets) is True
    assert any("compressibleTypes" in r.getMessage() for r in caplog.records)
    c = sqlite3.connect(tmp_path / "magoo.sqlite")
    c.row_factory = sqlite3.Row
    try:
        assert sdeimport.stored_build(c) == 101
        assert c.execute("SELECT COUNT(*) AS n FROM ref_compressible").fetchone()["n"] == 0
        assert Refdata(c).compressed_sources() == {}
    finally:
        c.close()
    # Any OTHER dataset missing still fails the build — and a failed
    # import prunes nothing: both archives survive for a retry.
    with pytest.raises(FileNotFoundError):
        _run_import_offline(
            monkeypatch, tmp_path, 102,
            [d for d in config.SDE_DATASETS if d != "types"],
        )
    assert sorted(p.name for p in (tmp_path / "sde").iterdir()) == [
        "eve-online-static-data-101-jsonl.zip",
        "eve-online-static-data-102-jsonl.zip",
    ]
    c = sqlite3.connect(tmp_path / "magoo.sqlite")
    c.row_factory = sqlite3.Row
    try:
        assert sdeimport.stored_build(c) == 101  # rolled back to 101
    finally:
        c.close()


def test_successful_import_prunes_the_zip_cache_to_current_plus_one(
    monkeypatch, tmp_path
):
    """After a successful import the cache keeps the current build's
    archive plus ONE previous (the highest other build number); an
    in-flight .part download and unrelated files are never touched
    (review 2026-09-05 — nothing ever pruned the ~100 MB archives)."""
    from magoo import config

    cache = tmp_path / "sde"
    cache.mkdir()
    for build in (100, 200, 300):
        (cache / f"eve-online-static-data-{build}-jsonl.zip").write_bytes(b"x")
    (cache / "eve-online-static-data-150-jsonl.zip.4242.part").write_bytes(b"x")
    (cache / "notes.txt").write_text("keep")
    assert _run_import_offline(
        monkeypatch, tmp_path, 400, list(config.SDE_DATASETS)
    ) is True
    assert sorted(p.name for p in cache.iterdir()) == sorted([
        "eve-online-static-data-300-jsonl.zip",
        "eve-online-static-data-400-jsonl.zip",
        "eve-online-static-data-150-jsonl.zip.4242.part",
        "notes.txt",
    ])


def test_prune_sde_cache_keeps_current_plus_highest_other(tmp_path, monkeypatch):
    from magoo import config

    cache = tmp_path / "sde"
    cache.mkdir()
    monkeypatch.setattr(config, "SDE_CACHE_DIR", cache)
    for build in (100, 200, 300, 400):
        (cache / f"eve-online-static-data-{build}-jsonl.zip").write_bytes(b"x")
    # Re-importing an OLDER build keeps it plus the highest other build.
    sdeimport._prune_sde_cache(200)
    assert sorted(p.name for p in cache.iterdir()) == [
        "eve-online-static-data-200-jsonl.zip",
        "eve-online-static-data-400-jsonl.zip",
    ]
    # Idempotent: nothing left to prune.
    sdeimport._prune_sde_cache(200)
    assert len(list(cache.iterdir())) == 2
    # A current build with no cached archive (downloaded elsewhere) still
    # keeps exactly one previous: the highest other build.
    sdeimport._prune_sde_cache(999)
    assert [p.name for p in cache.iterdir()] == [
        "eve-online-static-data-400-jsonl.zip"
    ]
    # A missing cache directory is a quiet no-op.
    monkeypatch.setattr(config, "SDE_CACHE_DIR", tmp_path / "absent")
    sdeimport._prune_sde_cache(200)
