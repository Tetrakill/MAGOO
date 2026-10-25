"""The executed run's Profit tab counts the hulls ACTUALLY installed (user
ruling 2026-10-24): the final jobs ESI shows started inside the run's
buying cycle, read from the durable industry_job record (schema 13) that
every ESI update upserts — not the install check's startable count, which
assumed the plan's buys arrived and the listed jobs were installed.

Store layer (utc_text, the upsert and its pull log, the window query, the
pull-log coverage test and the 12 → 13 upgrade), the ESI recording path (statuses, filters, dedup, a
status flip on a later pull), costing.hull_cost (window bounds, cancelled
jobs, installs beyond the plan, the pro-rata share of a dual-role final,
the fallback where the record does not reach) and the Profit tab's
wording. Same fixture style as test_costing: real reference data, a temp
state DB, uniform synthetic prices.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import render_template

from magoo import config, costing, engine, esi, store

from conftest import template_app
from test_costing import add_pipeline, uniform_snapshot
from test_esi import STATION, TRACKED, UNTRACKED, add_character, job, patch_pull


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


MANUFACTURING = config.ACTIVITY_MANUFACTURING


def _track(conn):
    """test_esi's scoping: one tracked system, the station in it (and an
    untracked one for the delivery filter). An EMPTY tracked list means no
    filter, so a refresh test needs this to exercise the filter at all."""
    conn.execute("INSERT INTO tracked_system VALUES (?)", (TRACKED,))
    conn.executemany(
        "INSERT INTO location_system (location_id, solar_system_id) VALUES (?, ?)",
        [(STATION, TRACKED), (70000001, UNTRACKED)],
    )
    conn.commit()


# A pull that reaches every window the costing tests use (2026-09-25 ..
# 2026-09-27): its 90 days run from 2026-07-03. Explicit, never "now" —
# the suite must still pass when "now" is a year on.
PULL = "2026-10-01 00:00:00"


def _job_row(job_id, type_id, start, units=1, status="active", runs=None):
    return {
        "job_id": job_id, "type_id": type_id, "activity_id": MANUFACTURING,
        "start_date": start, "runs": runs if runs is not None else units,
        "units": units, "status": status,
    }


# --- store ------------------------------------------------------------------


def test_utc_text_reads_every_timestamp_shape():
    """ESI's ISO 'Z', SQLite's datetime('now') text, a bare date, aware and
    naive datetimes all land as UTC 'YYYY-MM-DD HH:MM:SS'; an offset is
    converted; junk and None read as None."""
    assert store.utc_text("2026-09-25T09:30:00Z") == "2026-09-25 09:30:00"
    assert store.utc_text("2026-09-25 09:30:00") == "2026-09-25 09:30:00"
    assert store.utc_text("2026-09-25") == "2026-09-25 00:00:00"
    assert store.utc_text("2026-09-25T11:30:00+02:00") == "2026-09-25 09:30:00"
    aware = datetime(2026, 9, 25, 9, 30, tzinfo=timezone(timedelta(hours=-3)))
    assert store.utc_text(aware) == "2026-09-25 12:30:00"
    assert store.utc_text(datetime(2026, 9, 25, 9, 30)) == "2026-09-25 09:30:00"
    assert store.utc_text("") is None
    assert store.utc_text(None) is None
    assert store.utc_text("yesterday") is None


def test_save_industry_jobs_upserts_and_skips_rows_without_a_start(conn):
    """A row per job_id, replaced wholesale on a later save (an active job
    seen cancelled stops counting); a job with no usable start, or no id,
    is skipped — it belongs to no cycle. The start is normalised at write
    time so the window query is a plain text compare."""
    n = store.save_industry_jobs(conn, [
        _job_row(1, 34, "2026-09-25T09:30:00Z", units=10, runs=5),
        _job_row(2, 34, None),
        _job_row(3, 34, "not a date"),
        {**_job_row(4, 34, "2026-09-25T09:30:00Z"), "job_id": None},
    ])
    assert n == 1
    # Every save logs its pull — "now" when none is given, the given one
    # normalised otherwise; a save of NO rows still logs one.
    assert conn.execute("SELECT COUNT(*) FROM industry_job_pull").fetchone()[0] == 1
    store.save_industry_jobs(conn, [], pulled_at="2026-10-01T12:00:00Z")
    pulls = [r[0] for r in conn.execute("SELECT pulled_at FROM industry_job_pull ORDER BY pull_id")]
    assert len(pulls) == 2 and pulls[1] == "2026-10-01 12:00:00"
    assert store.last_job_pull(conn) == max(pulls)
    row = conn.execute("SELECT * FROM industry_job").fetchone()
    assert (row["job_id"], row["type_id"], row["activity_id"]) == (1, 34, MANUFACTURING)
    assert (row["start_date"], row["runs"], row["units"], row["status"]) == (
        "2026-09-25 09:30:00", 5, 10, "active",
    )
    assert row["seen_at"]
    store.save_industry_jobs(conn, [
        _job_row(1, 34, "2026-09-25T09:30:00Z", units=10, runs=5, status="cancelled"),
    ])
    rows = conn.execute("SELECT job_id, status FROM industry_job").fetchall()
    assert [tuple(r) for r in rows] == [(1, "cancelled")]


def test_installed_units_counts_the_window_and_the_counting_statuses(conn):
    """(lo, hi]: a start AT lo is the previous cycle's, AT hi this one's
    (buying.BuyWindow.contains). active / paused / ready / delivered
    count; cancelled / reverted never; other types never. Either bound in
    either text shape; a bound that does not parse empties the window."""
    store.save_industry_jobs(conn, [
        _job_row(1, 34, "2026-09-25T10:00:00Z", units=1),               # == lo: out
        _job_row(2, 34, "2026-09-25T10:00:01Z", units=2),               # in
        _job_row(3, 34, "2026-09-26T10:00:00Z", units=4, status="paused"),     # == hi: in
        _job_row(4, 34, "2026-09-25T12:00:00Z", units=8, status="ready"),
        _job_row(5, 34, "2026-09-25T13:00:00Z", units=16, status="delivered"),
        _job_row(6, 34, "2026-09-25T14:00:00Z", units=32, status="cancelled"),
        _job_row(7, 34, "2026-09-25T15:00:00Z", units=64, status="reverted"),
        _job_row(8, 34, "2026-09-26T10:00:01Z", units=128),             # > hi: out
        _job_row(9, 35, "2026-09-25T16:00:00Z", units=256),             # other type
    ])
    lo, hi = "2026-09-25 10:00:00", "2026-09-26T10:00:00Z"
    assert store.installed_units(conn, 34, lo, hi) == 2 + 4 + 8 + 16
    assert store.installed_units(conn, 35, lo, hi) == 256
    assert store.installed_units(conn, 34, "junk", hi) == 0
    assert store.installed_units(conn, 34, lo, None) == 0
    assert store.JOB_STATUSES_INSTALLED == {"active", "paused", "ready", "delivered"}


def test_jobs_covered_chains_the_pulls_ninety_day_reach_over_the_window(conn):
    """A pull at P speaks for the jobs started in [P − 90 d, P]; the window
    (lo, hi] is covered when the pulls' intervals chain from lo to hi
    without a gap. Review 2026-10-24: judging coverage by any job on
    file read a cycle straddling the first pull's horizon as whole."""
    lo, hi = "2026-09-25 10:00:00", "2026-09-26 10:00:00"
    assert not store.jobs_covered(conn, lo, hi)                 # no pull at all
    assert store.last_job_pull(conn) is None
    # A pull whose reach starts AFTER lo: the window's head is not on file.
    store.save_industry_jobs(conn, [], pulled_at="2026-12-25 10:00:01")   # reach 2026-09-26 10:00:01 > lo
    assert not store.jobs_covered(conn, lo, hi)
    # A pull before hi covers only up to itself: the tail is not on file.
    store.save_industry_jobs(conn, [], pulled_at="2026-09-26 09:00:00")
    assert not store.jobs_covered(conn, lo, hi)
    # Reach exactly at lo and pulled exactly at hi: covered (closed bounds).
    store.save_industry_jobs(conn, [], pulled_at="2026-12-24 10:00:00")   # reach == lo
    assert store.jobs_covered(conn, lo, hi)
    assert store.jobs_covered(conn, lo, "2026-12-24 10:00:00")
    assert not store.jobs_covered(conn, lo, "2026-12-25 10:00:02")          # past the last pull (12-25 10:00:01)
    # Chaining: a window opening in January needs pulls whose reaches
    # overlap all the way to hi; two pulls 112 days apart leave a hole.
    jan = "2026-01-01 00:00:00"
    assert not store.jobs_covered(conn, jan, hi)                          # nothing reaches January
    store.save_industry_jobs(conn, [], pulled_at="2026-03-30 00:00:00")   # reach 2025-12-30 <= jan
    store.save_industry_jobs(conn, [], pulled_at="2026-07-20 00:00:00")   # reach 2026-04-21 > 03-30: a hole
    assert not store.jobs_covered(conn, jan, hi)
    store.save_industry_jobs(conn, [], pulled_at="2026-06-15 00:00:00")   # reach 2026-03-17 <= 03-30: bridged
    assert store.jobs_covered(conn, jan, hi)                              # ... 07-20, 09-26, 12-24 chain on
    # Degenerate windows never read as covered.
    assert not store.jobs_covered(conn, hi, lo)
    assert not store.jobs_covered(conn, lo, lo)
    assert not store.jobs_covered(conn, None, hi)
    assert not store.jobs_covered(conn, lo, "junk")
    assert store.jobs_covered(conn, datetime(2026, 9, 25, 10, tzinfo=timezone.utc), "2026-09-26T10:00:00Z")
    assert store.ESI_JOB_HISTORY_DAYS == 90


def test_schema_12_database_gains_the_job_record(tmp_path, monkeypatch):
    """The upgrade: a database stamped 12 gains industry_job and its index
    on the next ensure_schema, the stamp moves to 13 and the pre-upgrade
    backup lands (in THIS temp dir). Everything the old stamp protected
    is still there."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.sqlite")
    c = sqlite3.connect(tmp_path / "old.sqlite")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    c.execute("DROP INDEX industry_job_type_start")
    c.execute("DROP TABLE industry_job")
    c.execute("DROP TABLE industry_job_pull")
    c.execute("PRAGMA user_version = 12")
    c.commit()
    assert not (tmp_path / "backups").exists()

    store.ensure_schema(c)

    assert list((tmp_path / "backups").glob("magoo-pre-*.sqlite"))
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 13
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    indexes = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"industry_job", "industry_job_pull", "run_purchase", "esi_snapshot"} <= tables
    assert "industry_job_type_start" in indexes
    assert store.save_industry_jobs(c, [_job_row(1, 34, "2026-09-25T09:30:00Z")]) == 1
    c.close()


# --- the ESI recording path -------------------------------------------------


def test_refresh_state_records_every_manufacturing_job_it_sees(conn, ref, monkeypatch):
    """One row per manufacturing / reaction job: active, delivered AND
    cancelled (so a status flip is on file), units = runs x portion, the
    start normalised; the same tracked-system filter and job_id dedup as
    job_starts (a corp job seen again through the character feed is one
    row); lab jobs and a job without a start record nothing. The maps
    keep their contract: a cancelled job credits nothing in them."""
    _track(conn)
    add_character(conn, 2001)
    hulk = ref.type_id("Hulk")
    live = job(1, 2001, hulk, runs=2)
    live["start_date"] = "2026-09-25T09:30:00Z"
    done = job(2, 2001, hulk, runs=3)
    done["status"] = "delivered"
    done["start_date"] = "2026-09-25T09:40:00Z"
    dead = job(3, 2001, hulk, runs=4)
    dead["status"] = "cancelled"
    dead["start_date"] = "2026-09-25T09:50:00Z"
    far = job(4, 2001, hulk, runs=5, location=70000001)
    far["start_date"] = "2026-09-25T10:00:00Z"
    no_start = job(5, 2001, hulk, runs=6)
    copy = job(6, 2001, 999, activity=config.ACTIVITY_COPYING, blueprint_type=999)
    copy["start_date"] = "2026-09-25T10:10:00Z"
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [dict(live), dead, far, no_start, copy]},
        corp_assets={2001: []},
        corp_jobs={2001: [live, done]},  # the corp feed claims `live` first
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert state["job_starts"] == {
        hulk: [["2026-09-25T09:30:00Z", 2], ["2026-09-25T09:40:00Z", 3]],
    }
    # The maps' contract is untouched: live jobs credit in_progress (the
    # start-less job too — it is stock in flight, it just belongs to no
    # cycle), the copy job its licensed runs; the cancelled job nothing.
    assert state["in_progress"][hulk] == 2 + 6
    rows = {
        r["job_id"]: dict(r)
        for r in conn.execute("SELECT * FROM industry_job ORDER BY job_id")
    }
    assert set(rows) == {1, 2, 3}
    assert conn.execute("SELECT COUNT(*) FROM industry_job_pull").fetchone()[0] == 1
    assert store.last_job_pull(conn) is not None
    assert (rows[1]["start_date"], rows[1]["units"], rows[1]["runs"], rows[1]["status"]) == (
        "2026-09-25 09:30:00", 2, 2, "active",
    )
    assert (rows[2]["units"], rows[2]["status"]) == (3, "delivered")
    assert (rows[3]["units"], rows[3]["status"]) == (4, "cancelled")
    assert all(r["type_id"] == hulk and r["activity_id"] == MANUFACTURING for r in rows.values())


def test_a_later_pull_flips_the_status_and_keeps_jobs_esi_no_longer_reports(conn, ref, monkeypatch):
    """Pull two: job 1 is now cancelled (its row says so, its units stop
    counting), job 2 has aged out of ESI's history (its row stays — that
    is the point of the record), job 7 is new."""
    _track(conn)
    add_character(conn, 2001)
    hulk = ref.type_id("Hulk")

    def pull(records):
        patch_pull(
            monkeypatch, personal_jobs={2001: records},
            corp_assets={2001: []}, corp_jobs={2001: []}, corp_wallets={2001: 0.0},
        )
        esi.refresh_state(conn, ref)

    one = job(1, 2001, hulk, runs=2)
    one["start_date"] = "2026-09-25T09:30:00Z"
    two = job(2, 2001, hulk, runs=3)
    two["start_date"] = "2026-09-25T09:40:00Z"
    pull([one, two])
    lo, hi = "2026-09-25 00:00:00", "2026-09-26 00:00:00"
    assert store.installed_units(conn, hulk, lo, hi) == 5

    one["status"] = "cancelled"
    seven = job(7, 2001, hulk, runs=4)
    seven["start_date"] = "2026-09-25T09:50:00Z"
    pull([one, seven])
    rows = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM industry_job")}
    assert rows == {1: "cancelled", 2: "active", 7: "active"}
    assert store.installed_units(conn, hulk, lo, hi) == 3 + 4


# --- costing.hull_cost -------------------------------------------------------


OPENED = "2026-09-25 10:00:00"
EXECUTED = "2026-09-26 10:00:00"


def _executed_hulk_run(conn, ref, requested=8, installed_before_plan=0):
    """Hulk x `requested` planned once (with `installed_before_plan` of the
    wave already installed when it was planned, as a re-plan after the
    installs sees) and marked executed, its cycle pinned to
    (OPENED, EXECUTED]. Returns (pipeline_id, run_id, hulk type_id)."""
    pid = add_pipeline(conn, ref, "Hulk", requested)
    hulk = ref.type_id("Hulk")
    snap = uniform_snapshot(10.0)
    cut = None
    if installed_before_plan:
        snap.job_starts = {hulk: [["2026-09-25T10:30:00Z", 1]] * installed_before_plan}
        snap.in_progress = {hulk: installed_before_plan}
        cut = OPENED
    plan = engine.plan_index_run(conn, ref, snap, cycle_cut=cut)
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = ?, "
        "opened_at = ?, planned_start = ? WHERE index_run_id = ?",
        (EXECUTED, OPENED, OPENED, plan.index_run_id),
    )
    conn.commit()
    return pid, plan.index_run_id, hulk


def _starts(hulk, starts, **kw):
    return [_job_row(100 + i, hulk, s, **kw) for i, s in enumerate(starts)]


def test_units_are_the_hulls_esi_shows_started_inside_the_cycle(conn, ref):
    """Hulk x 8 planned, every job listed as startable (the install check
    counts the plan's buys as available) — but ESI shows only 5 Hulk jobs
    started inside the cycle: Units read 5 (hulls_from_esi), the plan's
    8 stay as hulls_planned (the `short` badge and its tooltip), the
    per-hull cost is unchanged and cycle_totals multiply by 5. A job
    started AT the cycle's open is the previous cycle's; one AT Mark
    executed is this one's; one after it is the next cycle's."""
    pid, run_id, hulk = _executed_hulk_run(conn, ref)
    settings = store.get_settings(conn)
    before = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (before.hulls_planned, before.hulls_per_cycle, before.hulls_from_esi) == (8, 8, False)

    store.save_industry_jobs(conn, _starts(hulk, [
        "2026-09-25T10:00:00Z",   # == opened: the previous cycle's
        "2026-09-25T11:00:00Z",
        "2026-09-25T12:00:00Z",
        "2026-09-25T13:00:00Z",
        "2026-09-26T09:00:00Z",
        "2026-09-26T10:00:00Z",   # == Mark executed: this cycle's
        "2026-09-26T10:00:01Z",   # the next cycle's
    ]), pulled_at=PULL)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 5, True)
    assert cost.total == before.total
    totals = costing.cycle_totals([{"cost": cost, "net": 1.0, "margin": 0.5}])
    assert (totals.hulls, totals.cost) == (5, 5 * cost.total)


def test_cancelled_jobs_do_not_count_and_units_follow_runs_times_portion(conn, ref):
    pid, run_id, hulk = _executed_hulk_run(conn, ref)
    store.save_industry_jobs(conn, [
        _job_row(1, hulk, "2026-09-25T11:00:00Z", units=3, runs=3),
        _job_row(2, hulk, "2026-09-25T12:00:00Z", units=4, runs=4, status="cancelled"),
        _job_row(3, hulk, "2026-09-25T13:00:00Z", units=2, runs=2, status="delivered"),
        _job_row(4, hulk, "2026-09-25T14:00:00Z", units=1, runs=1, status="reverted"),
    ], pulled_at=PULL)
    cost = costing.hull_cost(conn, ref, store.get_settings(conn), run_id, pid)
    assert (cost.hulls_per_cycle, cost.hulls_from_esi) == (5, True)


def test_installs_beyond_the_plan_count_as_built(conn, ref):
    """User ruling 2026-10-24: the plan sized 8, ESI shows 10 started (a
    whole-copy batch, or jobs the user added) — Units read 10, not the
    plan's 8; the current-prices cap never applied to installs."""
    pid, run_id, hulk = _executed_hulk_run(conn, ref)
    store.save_industry_jobs(conn, _starts(hulk, ["2026-09-25T11:00:00Z"] * 10), pulled_at=PULL)
    cost = costing.hull_cost(conn, ref, store.get_settings(conn), run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle) == (8, 10)
    assert cost.hulls_per_cycle > cost.hulls_planned  # the template's `over` tooltip


def test_a_wave_installed_before_the_last_plan_is_counted_from_esi_too(conn, ref):
    """A re-plan after 4 installs sizes the other 4 (installed_qty 4,
    runs 4): the plan's count is 8 either way; Units are whatever ESI
    shows started in the cycle — 4 when nothing else was installed, 8
    when the rest went in before Mark executed."""
    pid, run_id, hulk = _executed_hulk_run(conn, ref, installed_before_plan=4)
    row = conn.execute(
        "SELECT installed_qty, runs_allocated FROM index_run_item "
        "WHERE index_run_id = ? AND type_id = ?", (run_id, hulk),
    ).fetchone()
    assert (row["installed_qty"], row["runs_allocated"]) == (4, 4)
    settings = store.get_settings(conn)
    store.save_industry_jobs(conn, _starts(hulk, ["2026-09-25T10:30:00Z"] * 4), pulled_at=PULL)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 4, True)
    store.save_industry_jobs(conn, [
        _job_row(200 + i, hulk, "2026-09-26T08:00:00Z") for i in range(4)
    ], pulled_at=PULL)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle) == (8, 8)


def test_the_plans_started_figure_is_the_fallback_where_the_pulls_do_not_cover(conn, ref):
    """No pull at all (ESI never updated since the upgrade), a pull whose
    90-day reach starts after the cycle opened (a run executed before
    the record existed whose jobs had aged out — or a cycle straddling
    the horizon: review 2026-10-24, no partial count under ESI wording),
    or a pull before Mark executed (the tail not on file yet): Units keep
    the plan's started figure (hulls_from_esi False) rather than reading
    0 or a part. Once a pull covers the whole window the record speaks
    for the cycle, zero installs included."""
    pid, run_id, hulk = _executed_hulk_run(conn, ref)
    settings = store.get_settings(conn)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 8, False)
    # A pull 100 days after the cycle opened: its reach starts inside the
    # window — three Hulk jobs from the tail are on file, five are lost.
    store.save_industry_jobs(
        conn, _starts(hulk, ["2026-09-26T09:00:00Z"] * 3), pulled_at="2027-01-03 10:00:01",
    )
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 8, False)
    # A pull during the cycle: the tail after it is not on file.
    store.save_industry_jobs(conn, [], pulled_at="2026-09-26 09:30:00")
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 8, False)
    # A pull that reaches the whole window: covered — 3 installed.
    store.save_industry_jobs(conn, [], pulled_at=PULL)
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 3, True)
    # Covered and nothing installed reads 0, not the plan's figure.
    conn.execute("DELETE FROM industry_job")
    conn.commit()
    cost = costing.hull_cost(conn, ref, settings, run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 0, True)


def test_an_inverted_legacy_window_keeps_the_plans_figure(conn, ref):
    """buying_windows hands a legacy completed_at inversion (a later run
    number executed earlier) an empty window; such a run keeps the plan's
    started figure rather than reading 0 from ESI."""
    pid, run_id, hulk = _executed_hulk_run(conn, ref)
    conn.execute(
        "UPDATE index_run SET completed_at = ?, opened_at = ?, planned_start = ? "
        "WHERE index_run_id = ?",
        (OPENED, EXECUTED, EXECUTED, run_id),
    )
    conn.commit()
    store.save_industry_jobs(conn, _starts(hulk, ["2026-09-25T11:00:00Z"] * 3), pulled_at=PULL)
    cost = costing.hull_cost(conn, ref, store.get_settings(conn), run_id, pid)
    assert (cost.hulls_planned, cost.hulls_per_cycle, cost.hulls_from_esi) == (8, 8, False)


def test_no_hull_counts_on_two_executed_runs(conn, ref):
    """A Hulk job started after run 1's Mark executed is run 2's, however
    run 1's last plan listed it — the window rule, not the plan, decides;
    each executed run counts its own cycle's installs once."""
    pid, run1, hulk = _executed_hulk_run(conn, ref)
    plan2 = engine.plan_index_run(conn, ref, uniform_snapshot(11.0))
    run2 = plan2.index_run_id
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = ?, "
        "opened_at = ?, planned_start = ? WHERE index_run_id = ?",
        ("2026-09-27 10:00:00", EXECUTED, EXECUTED, run2),
    )
    conn.commit()
    store.save_industry_jobs(conn, _starts(hulk, [
        "2026-09-25T12:00:00Z", "2026-09-25T13:00:00Z",   # run 1's cycle
        "2026-09-26T10:00:01Z", "2026-09-26T18:00:00Z", "2026-09-27T10:00:00Z",  # run 2's
    ]), pulled_at=PULL)
    settings = store.get_settings(conn)
    first = costing.hull_cost(conn, ref, settings, run1, pid)
    second = costing.hull_cost(conn, ref, settings, run2, pid)
    assert (first.hulls_per_cycle, second.hulls_per_cycle) == (2, 3)
    assert first.hulls_from_esi and second.hulls_from_esi


def test_a_dual_role_finals_installs_are_shared_pro_rata_uncapped(conn, ref):
    """Covetor x 5 sold AND consumed by Hulk x 8: the Covetor pipeline's
    Units are its demand share of the Covetor jobs ESI shows started,
    rounded up — and not capped at its 5 when more were installed."""
    covetor_pid = add_pipeline(conn, ref, "Covetor", 5)
    add_pipeline(conn, ref, "Hulk", 8)
    covetor = ref.type_id("Covetor")
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(10.0))
    run_id = plan.index_run_id
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = ?, "
        "opened_at = ?, planned_start = ? WHERE index_run_id = ?",
        (EXECUTED, OPENED, OPENED, run_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT a.qty_attributable, COALESCE(i.cycle_need_qty, i.merged_min_qty) AS whole "
        "FROM index_run_item i JOIN index_run_item_pipeline a USING (index_run_item_id) "
        "WHERE i.index_run_id = ? AND i.type_id = ? AND a.pipeline_id = ?",
        (run_id, covetor, covetor_pid),
    ).fetchone()
    attributed, whole = row["qty_attributable"], row["whole"]
    assert attributed == 5 and whole > attributed  # the Hulks' draw is in the demand
    settings = store.get_settings(conn)

    def units_for(installed):
        store.save_industry_jobs(conn, [
            _job_row(300 + i, covetor, "2026-09-25T12:00:00Z", status="active")
            for i in range(installed)
        ] + [
            _job_row(300 + i, covetor, "2026-09-25T12:00:00Z", status="cancelled")
            for i in range(installed, 60)
        ], pulled_at=PULL)
        return costing.hull_cost(conn, ref, settings, run_id, covetor_pid).hulls_per_cycle

    assert units_for(whole) == 5
    assert units_for(1) == 1                      # rounded UP: a started hull never reads 0
    assert units_for(2 * whole) == 10             # beyond the plan: uncapped


# --- the Profit tab ----------------------------------------------------------


def _render(hulls, planned, from_esi):
    app = template_app()
    cost = SimpleNamespace(
        pipeline_id=1, lines=[], total=100.0, hulls_per_cycle=hulls,
        hulls_planned=planned, hulls_from_esi=from_esi, spin_up=False,
        missing_prices=0, region_priced=0, structure_material_share_pct=None,
        structure_material_cost=0.0, subtotal=lambda kind: 0.0,
    )
    card = {"name": "Hulk", "cost": cost, "price": 200.0, "net": 190.0,
            "capital": False, "margin": 90.0}
    with app.test_request_context("/runs/1?view=profit"):
        return render_template(
            "run_profit.html",
            run={"index_run_id": 1, "run_number": 1, "status": "complete",
                 "completed_at": "2026-09-26 10:00:00"},
            superseded=False, cards=[card], totals=costing.cycle_totals([card]),
            completed_history=1, broker_rate=0.02, sales_tax=0.03,
            jobs_as_of="2026-09-26 09:00:00" if from_esi else None,
            settings=store.Settings(
                stockpile_buffer=1.0, max_run_duration_hours=168.0,
                composite_reaction_extra_runs=0, price_region_id=10000002,
                price_source="sell",
            ),
        )


def test_profit_tab_says_units_are_the_esi_installs():
    html = _render(5, 8, True)
    assert ">5</td>" in html and ">short</span>" in html
    assert "ESI shows jobs for 5 of the 8 planned hulls started this cycle" in html
    assert "the plan sized 8 this cycle; ESI shows jobs for 5 started inside it" in html
    assert "Units are the hulls whose jobs ESI shows started inside this run" in html
    assert "as of the last ESI update" in html and " ago)" in html
    assert "the plan wanted" not in html and "short on stock" not in html
    assert "Units / cycle" in html


def test_profit_tab_tooltips_installs_beyond_the_plan_without_a_badge():
    html = _render(10, 8, True)
    assert ">10</td>" in html
    # The badge itself (with its title) is absent — the "How this is
    # computed" note still names it as an example, like spin-up.
    assert 'planned hulls started this cycle">short</span>' not in html
    assert 'could start — Units and the cycle figures count those; cost per unit is unchanged">short</span>' not in html
    assert "ESI shows jobs for 10 started inside it as of the last ESI update — more than the plan sized" in html


def test_profit_tab_keeps_the_older_wording_on_the_fallback():
    html = _render(6, 8, False)
    assert ">short</span>" in html
    assert "short on stock when this run was planned" in html
    assert "The ESI job record does not reach back to this run's cycle" in html
    assert "Units are the plan's started figure" in html
    assert "no chain of ESI updates covers it" in html
    assert "ESI shows jobs for" not in html and " ago)" not in html
