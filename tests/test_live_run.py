"""One live run per buying cycle (v1.29 revision 6, user rulings R1 and R2
2026-09-28): engine.plan_index_run(replace_index_run_id=...) re-plans the
OPEN run in place — same id and run number, planned_start moved, items
rewritten, the cycle's purchases kept, all in one transaction — and the
plan's slot pools are always the Settings' pools, whatever is running.

Same fixture pattern as test_engine: real reference data (the production
SDE, read-only), a temp state DB, FairValuePrices. Planning inputs come
through engine.snapshot_from_state (the ESI snapshot row plus the
Settings), the path both the Plan button and the ESI update take.
"""

import json
import sqlite3

import pytest

from magoo import config, engine, store

from conftest import FairValuePrices
from test_engine import add_pipeline

TRITANIUM = 34
LEGACY_START = "2026-01-01 00:00:00"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    c.execute("UPDATE settings SET manufacturing_slots = 500, reaction_slots = 500")
    c.commit()
    yield c
    c.close()


def _snap(conn, ref):
    prices = FairValuePrices(ref)
    return engine.snapshot_from_state(conn, prices=prices, adjusted=prices)


def _hulk_run(conn, ref, on_hand=None):
    """Hulk × 8 planned from an ESI snapshot holding `on_hand`."""
    add_pipeline(conn, ref, "Hulk", 8)
    store.save_esi_snapshot(conn, on_hand or {}, {}, {}, 0.0, 0.0)
    return engine.plan_index_run(conn, ref, _snap(conn, ref))


def _run(conn, run_id):
    return conn.execute(
        "SELECT * FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()


def _items(conn, run_id) -> dict:
    return {
        r["type_id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,)
        )
    }


def _pipeline_rows(conn, run_id) -> list:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT i.type_id, p.pipeline_id, p.qty_attributable, p.depth "
            "FROM index_run_item_pipeline p JOIN index_run_item i "
            "USING (index_run_item_id) WHERE i.index_run_id = ? "
            "ORDER BY i.type_id, p.pipeline_id",
            (run_id,),
        )
    ]


def _state(conn, run_id):
    """Everything a failed replace must leave exactly as it was."""
    return (
        dict(_run(conn, run_id)),
        _items(conn, run_id),
        _pipeline_rows(conn, run_id),
        conn.execute("SELECT COUNT(*) FROM index_run").fetchone()[0],
    )


def _age(conn, run_id):
    """Push the run's timestamps into the past so 'now' is observably
    later, as it is for a run planned at the start of the cycle."""
    conn.execute(
        "UPDATE index_run SET planned_start = ?, opened_at = ? "
        "WHERE index_run_id = ?",
        (LEGACY_START, LEGACY_START, run_id),
    )
    conn.commit()


# --- the replace -----------------------------------------------------------


def test_replace_rewrites_the_open_run_in_place(conn, ref):
    """R1: an ESI update (or the Plan button) while the run is open
    re-plans it from current stock under the SAME id and run number;
    planned_start moves to now, opened_at does not (A3 — the cycle's
    first buying window), the items / attribution / invention rows are
    the new plan's only, and the cycle's purchases stay on the run."""
    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    row = _run(conn, run_id)
    # A fresh run opens when it is first planned.
    assert row["opened_at"] == row["planned_start"] is not None
    _age(conn, run_id)
    pipeline_id = conn.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    # What a cycle accumulates on its run: a purchase line, an executed
    # conversion group, and an invention vintage the new plan will not
    # produce (a Hulk invents nothing).
    conn.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
        "unit_price) VALUES (?, ?, 'hub', 1000, 5.0)",
        (run_id, TRITANIUM),
    )
    conn.execute(
        "INSERT INTO run_purchase_refine (index_run_id, esi_kind, esi_id, "
        "via_type_id, type_id, quantity, unit_price) "
        "VALUES (?, 'transaction', 1, 62516, ?, 300, 6.0)",
        (run_id, TRITANIUM),
    )
    conn.execute(
        "INSERT INTO index_run_invention (index_run_id, pipeline_id, "
        "t1_blueprint_id, probability, invented_me, invented_te, "
        "runs_per_copy, datacores, invention_fee_per_attempt, "
        "copy_fee_per_attempt, cost_per_run) "
        "VALUES (?, ?, 1, 0.5, 2, 4, 10, '[]', 0, 0, 1.0)",
        (run_id, pipeline_id),
    )
    conn.commit()
    # The world moves: 5,000 Tritanium has landed in the hangar.
    store.save_esi_snapshot(conn, {TRITANIUM: 5000}, {}, {}, 0.0, 0.0)

    again = engine.plan_index_run(
        conn, ref, _snap(conn, ref), replace_index_run_id=run_id
    )

    assert (again.index_run_id, again.run_number) == (run_id, first.run_number)
    assert conn.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 1
    assert not conn.in_transaction  # committed
    row = _run(conn, run_id)
    assert row["status"] == "planned"
    assert row["planned_start"] > LEGACY_START  # SQLite's own shape, now
    assert len(row["planned_start"]) == len(LEGACY_START)
    assert row["opened_at"] == LEGACY_START  # never moved by a re-plan
    assert (row["manufacturing_slots_available"], row["reaction_slots_available"]) == (500, 500)
    # The items are exactly the new plan's — nothing of the old plan left.
    items = _items(conn, run_id)
    assert set(items) == set(again.items)
    assert conn.execute("SELECT COUNT(*) FROM index_run_item").fetchone()[0] == len(again.items)
    trit = items[TRITANIUM]
    assert trit["on_hand_qty"] == 5000
    assert trit["target_stock_qty"] == first.items[TRITANIUM].target_stock_qty
    assert trit["recommended_buy_qty"] == again.items[TRITANIUM].recommended_buy_qty
    assert trit["recommended_buy_qty"] == first.items[TRITANIUM].recommended_buy_qty - 5000
    # Attribution: one row per (item, pipeline) of the new plan, no orphans.
    assert len(_pipeline_rows(conn, run_id)) == sum(
        len(i.pipeline_share) for i in again.items.values()
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM index_run_item_pipeline p LEFT JOIN "
        "index_run_item i USING (index_run_item_id) "
        "WHERE i.index_run_item_id IS NULL"
    ).fetchone()[0] == 0
    # The old invention vintage went with the old plan.
    assert conn.execute(
        "SELECT COUNT(*) FROM index_run_invention WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] == 0
    # The cycle's purchases are keyed by the run, which was never deleted.
    assert [tuple(r) for r in conn.execute(
        "SELECT type_id, quantity FROM run_purchase WHERE index_run_id = ?", (run_id,)
    )] == [(TRITANIUM, 1000)]
    assert conn.execute(
        "SELECT COUNT(*) FROM run_purchase_refine WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] == 1


def test_replace_keeps_a_legacy_runs_opening(conn, ref):
    """A run planned before opened_at existed (NULL) opens at its
    ORIGINAL planned_start: the replace's COALESCE reads the old row in
    the same UPDATE that moves planned_start (contract review A3)."""
    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    conn.execute(
        "UPDATE index_run SET planned_start = ?, opened_at = NULL "
        "WHERE index_run_id = ?",
        (LEGACY_START, run_id),
    )
    conn.commit()
    engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=run_id)
    row = _run(conn, run_id)
    assert row["opened_at"] == LEGACY_START
    assert row["planned_start"] > LEGACY_START


def test_replace_dates_planned_start_at_the_snapshot_it_read(conn, ref):
    """Review 2026-09-28: planned_start is costing._pre_plan's cut — a
    purchase dated at or before it counts as stock the plan netted. A
    ▶ Plan re-plans from the STORED snapshot, which can be hours old, so
    the replace dates the run at that snapshot's fetched_at, not at now:
    a purchase made after the snapshot stays post-plan. A snapshot dated
    in the future (clock skew) or a hand-built Snapshot with no
    fetched_at falls back to now; a CREATE still stamps now."""
    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    _age(conn, run_id)
    conn.execute("UPDATE esi_snapshot SET fetched_at = '2026-03-01 09:00:00'")
    conn.commit()
    snap = _snap(conn, ref)
    assert snap.fetched_at == "2026-03-01 09:00:00"
    engine.plan_index_run(conn, ref, snap, replace_index_run_id=run_id)
    row = _run(conn, run_id)
    assert row["planned_start"] == "2026-03-01 09:00:00"
    assert row["opened_at"] == LEGACY_START
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    conn.execute("UPDATE esi_snapshot SET fetched_at = '2999-01-01 00:00:00'")
    conn.commit()
    engine.plan_index_run(
        conn, ref, _snap(conn, ref), replace_index_run_id=run_id
    )
    assert now <= _run(conn, run_id)["planned_start"] < "2999"
    _age(conn, run_id)
    import dataclasses

    engine.plan_index_run(
        conn, ref, dataclasses.replace(_snap(conn, ref), fetched_at=None),
        replace_index_run_id=run_id,
    )
    assert now <= _run(conn, run_id)["planned_start"] < "2999"


def test_a_replan_never_moves_the_cycles_buying_window(conn, ref):
    """Contract review A3 across the seam: buying.buying_windows opens
    the first cycle's window at opened_at, which the replace never moves
    — so a re-plan (every ESI update) cannot strand the purchases made
    since the cycle opened, neither on the open run nor, after Mark
    executed, on the first executed run. The open run the replace
    targets is the one buying.collecting_run_id names (A1)."""
    from datetime import datetime, timezone

    from magoo import buying

    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    _age(conn, run_id)
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert buying.collecting_run_id(conn) == run_id
    engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=run_id)
    assert buying.collecting_run_id(conn) == run_id
    window = buying.run_window(conn, run_id)
    assert (window.lo, window.hi, window.executed) == (opened, None, False)
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = datetime('now') "
        "WHERE index_run_id = ?",
        (run_id,),
    )
    conn.commit()
    window = buying.run_window(conn, run_id)
    assert window.executed and window.lo == opened
    assert buying.collecting_run_id(conn) is None


def test_replace_refuses_an_executed_run(conn, ref):
    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = datetime('now') "
        "WHERE index_run_id = ?",
        (run_id,),
    )
    conn.commit()
    before = _state(conn, run_id)
    with pytest.raises(ValueError, match=f"run {first.run_number} is executed"):
        engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=run_id)
    assert _state(conn, run_id) == before
    assert not conn.in_transaction


def test_replace_refuses_anything_but_the_open_run(conn, ref):
    """Only the NEWEST run, while not executed, is the open run
    (buying.collecting_run_id, contract review A1): an older plan still
    marked planned is superseded, never re-planned. A missing run and a
    replace without persisting are refused too — all before planning."""
    conn.execute(
        "INSERT INTO index_run (run_number, planned_start, status) "
        "VALUES (1, ?, 'planned')",
        (LEGACY_START,),
    )
    conn.commit()
    older = conn.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]
    newer = _hulk_run(conn, ref)
    assert newer.run_number == 2
    before = _state(conn, older)
    with pytest.raises(ValueError, match="run 1 is not the newest run"):
        engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=older)
    assert _state(conn, older) == before
    with pytest.raises(ValueError, match="does not exist"):
        engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=9999)
    with pytest.raises(ValueError, match="persist"):
        engine.plan_index_run(
            conn, ref, _snap(conn, ref), persist=False,
            replace_index_run_id=newer.index_run_id,
        )
    assert conn.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 2


def test_the_executed_test_sits_inside_the_write(conn, ref, monkeypatch):
    """A plan takes ~1.6 s; a Mark executed that lands while it computes
    (after the fail-fast check passed) must still stop the replace — the
    guarded UPDATE re-tests the status inside the write transaction."""
    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    real_pass = engine._invention_pass

    def executed_meanwhile(conn_, *args, **kwargs):
        conn_.execute(
            "UPDATE index_run SET status = 'complete', "
            "completed_at = datetime('now') WHERE index_run_id = ?",
            (run_id,),
        )
        conn_.commit()
        return real_pass(conn_, *args, **kwargs)

    monkeypatch.setattr(engine, "_invention_pass", executed_meanwhile)
    before_items = _items(conn, run_id)
    before_start = _run(conn, run_id)["planned_start"]
    with pytest.raises(ValueError, match="is executed"):
        engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=run_id)
    assert _items(conn, run_id) == before_items
    row = _run(conn, run_id)
    assert (row["status"], row["planned_start"]) == ("complete", before_start)
    assert not conn.in_transaction


def test_a_newer_run_planned_meanwhile_stops_the_replace(conn, ref, monkeypatch):
    """The other half of the in-write guard: a run created while this
    plan computes (a second Plan press on a stale page) makes this one
    superseded — the replace must not rewrite a run that is no longer
    the open one."""
    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    real_pass = engine._invention_pass

    def newer_meanwhile(conn_, *args, **kwargs):
        conn_.execute(
            "INSERT INTO index_run (run_number, planned_start, status) "
            "VALUES (?, datetime('now'), 'planned')",
            (first.run_number + 1,),
        )
        conn_.commit()
        return real_pass(conn_, *args, **kwargs)

    monkeypatch.setattr(engine, "_invention_pass", newer_meanwhile)
    before = _items(conn, run_id), dict(_run(conn, run_id))
    with pytest.raises(ValueError, match="is not the newest run"):
        engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=run_id)
    assert (_items(conn, run_id), dict(_run(conn, run_id))) == before
    assert not conn.in_transaction


def test_a_refused_replace_fails_before_planning(conn, ref, monkeypatch):
    """The read-only check runs first, so a refused replace (an executed
    run — say, the ESI update racing Mark executed) costs no plan."""
    first = _hulk_run(conn, ref)
    conn.execute(
        "UPDATE index_run SET status = 'complete' WHERE index_run_id = ?",
        (first.index_run_id,),
    )
    conn.commit()

    def no_planning(*_a, **_k):
        raise AssertionError("planned before refusing")

    monkeypatch.setattr(engine, "_expand_and_merge", no_planning)
    with pytest.raises(ValueError, match="is executed"):
        engine.plan_index_run(
            conn, ref, _snap(conn, ref), replace_index_run_id=first.index_run_id
        )


def test_a_failed_persist_rolls_everything_back(conn, ref, monkeypatch):
    """One transaction from the run row to the commit (contract review
    A2): a write that fails after the old items were deleted leaves the
    run exactly as it was — and nothing open for the purchase matcher's
    next commit to write through. A failed create leaves no run behind."""
    first = _hulk_run(conn, ref)
    run_id = first.index_run_id
    _age(conn, run_id)
    before = _state(conn, run_id)
    pipeline_id = conn.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    bad_vintage = {
        pipeline_id: {
            "pipeline_id": pipeline_id, "t1_blueprint_id": 1,
            "decryptor_type_id": None,
            "probability": None,  # NOT NULL: the invention INSERT fails
            "invented_me": 2, "invented_te": 4, "runs_per_copy": 10,
            "datacores": json.dumps([]), "decryptor_unit_price": None,
            "invention_fee_per_attempt": 0.0, "copy_fee_per_attempt": 0.0,
            "cost_per_run": 1.0,
        }
    }
    monkeypatch.setattr(engine, "_invention_pass", lambda *a, **k: bad_vintage)
    store.save_esi_snapshot(conn, {TRITANIUM: 5000}, {}, {}, 0.0, 0.0)
    with pytest.raises(sqlite3.IntegrityError):
        engine.plan_index_run(conn, ref, _snap(conn, ref), replace_index_run_id=run_id)
    assert not conn.in_transaction
    assert _state(conn, run_id) == before
    # The create path rolls back the same way: no half-written run 2.
    conn.execute("UPDATE index_run SET status = 'complete' WHERE index_run_id = ?", (run_id,))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        engine.plan_index_run(conn, ref, _snap(conn, ref))
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM index_run_item WHERE index_run_id != ?", (run_id,)
    ).fetchone()[0] == 0


# --- pools (ruling R2) -----------------------------------------------------


def test_pools_are_the_settings_pools_with_jobs_running(conn, ref):
    """R2: the plan sizes against the Settings' pools and never subtracts
    running jobs — not even multi-cycle ones ending past the next index
    run (the 2026-08-20 rule this drops). 480 far-future manufacturing
    jobs on a 500-slot pool: the plan still has, and persists, 500.
    Jobs installed this cycle drop out through stock instead: their
    output is in progress."""
    add_pipeline(conn, ref, "Hulk", 8)
    store.save_esi_snapshot(
        conn, {}, {}, {config.ACTIVITY_MANUFACTURING: 480}, 0.0, 0.0,
        job_ends={
            config.ACTIVITY_MANUFACTURING: ["2099-01-01T00:00:00Z"] * 480,
            config.ACTIVITY_REACTION: ["2099-01-01T00:00:00Z"] * 7,
        },
    )
    snap = _snap(conn, ref)
    assert snap.slots_available == {
        config.ACTIVITY_MANUFACTURING: 500,
        config.ACTIVITY_REACTION: 500,
    }
    plan = engine.plan_index_run(conn, ref, snap)
    row = _run(conn, plan.index_run_id)
    assert (row["manufacturing_slots_available"], row["reaction_slots_available"]) == (500, 500)
    # The Settings are read at snapshot time: a smaller pool is the pool.
    conn.execute("UPDATE settings SET manufacturing_slots = 12, reaction_slots = 3")
    conn.commit()
    assert _snap(conn, ref).slots_available == {
        config.ACTIVITY_MANUFACTURING: 12,
        config.ACTIVITY_REACTION: 3,
    }


# --- this cycle's installed wave (revision 7) ------------------------------
#
# User ruling 2026-09-29: final jobs started inside the current buying
# cycle are this cycle's wave; the plan sizes the rest; every ESI update
# re-plans the open run. The cut is the open run's buying window floor
# (buying.run_window(...).lo, the figure web passes); the job starts come
# from the ESI snapshot's job_starts ({type_id: [[ESI start, units], ...]}).

AFTER_CUT = "2026-01-01T06:00:00Z"  # _age puts opened_at at LEGACY_START
BEFORE_CUT = "2025-12-31T20:00:00Z"  # the previous cycle's wave


def _cut(conn, run_id):
    from magoo import buying

    return buying.run_window(conn, run_id).lo


def _steady_hulk_run(conn, ref):
    """Hulk x 8 planned (and persisted as the open run) from a hangar with
    every non-final stage at its target (the line in steady state), with
    the purchase margin at 0 so a raw's target is exactly its draw.
    Returns (plan, the stocked hangar, a draw_of for the Hulk's jobs)."""
    conn.execute("UPDATE settings SET input_purchase_margin = 0")
    conn.commit()
    add_pipeline(conn, ref, "Hulk", 8)
    hulk = ref.type_id("Hulk")
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    empty = engine.plan_index_run(conn, ref, _snap(conn, ref), persist=False)
    steady = {
        t: i.target_stock_qty
        for t, i in empty.items.items()
        if t != hulk and i.target_stock_qty > 0
    }
    store.save_esi_snapshot(conn, steady, {}, {}, 0.0, 0.0)
    plan = engine.plan_index_run(conn, ref, _snap(conn, ref))
    _age(conn, plan.index_run_id)
    return plan, steady, engine._draw_calculator(conn, ref)


def _installed_from(steady, draw):
    """The hangar after jobs drawing `draw` were installed from it."""
    return {m: q - draw.get(m, 0) for m, q in steady.items()}


def _stage_figures(plan, type_ids):
    return {
        t: (
            plan.items[t].target_stock_qty,
            plan.items[t].deficit_qty,
            plan.items[t].runs_allocated,
            plan.items[t].jobs_allocated,
        )
        for t in type_ids
    }


def test_a_replan_after_half_the_wave_plans_the_other_half(conn, ref):
    """Four of the eight Hulks installed this cycle (and an older wave's
    job, started before the cut, still in ESI's 90-day history): the
    in-place re-plan builds the other four; every intermediate keeps the
    pre-install plan's target, deficit and jobs, because they are the
    next wave's stock (contract amendment 12; without amendment 3 the
    R7 proration collapses them); and the raws the Hulk draws directly
    are sized to its four remaining jobs only."""
    first, steady, draw_of = _steady_hulk_run(conn, ref)
    run_id = first.index_run_id
    hulk = ref.type_id("Hulk")
    h0 = first.items[hulk]
    assert (h0.runs_allocated, h0.jobs_allocated, h0.installed_qty) == (8, 8, 0)
    four = draw_of(h0, 4, 4)
    store.save_esi_snapshot(
        conn, _installed_from(steady, four), {hulk: 4}, {}, 0.0, 0.0,
        job_starts={hulk: [[AFTER_CUT, 1]] * 4 + [[BEFORE_CUT, 8]]},
    )

    again = engine.plan_index_run(
        conn, ref, _snap(conn, ref), replace_index_run_id=run_id,
        cycle_cut=_cut(conn, run_id),
    )

    h = again.items[hulk]
    assert h.installed_qty == 4  # the older wave's 8 are not this cycle's
    assert h.target_stock_qty == h.cycle_need_qty == 8  # the wave, whole
    assert (h.deficit_qty, h.total_runs_needed, h.runs_allocated) == (4, 4, 4)
    stages = [t for t, i in first.items.items() if i.buildable and t != hulk]
    assert _stage_figures(again, stages) == _stage_figures(first, stages)
    raws = [m for m in four if not first.items[m].buildable]
    assert raws  # Construction Blocks, Morphite
    for m in raws:
        # The four remaining jobs draw `four`; the installed four already
        # drew theirs from the hangar.
        assert again.items[m].target_stock_qty == (
            first.items[m].target_stock_qty - four[m]
        ), ref.type_info(m).name
        assert again.items[m].recommended_buy_qty == 0
    # Persisted: the final's installed count, and 0 (never NULL) elsewhere.
    items = _items(conn, run_id)
    assert items[hulk]["installed_qty"] == 4
    assert (items[hulk]["deficit_qty"], items[hulk]["runs_allocated"]) == (4, 4)
    assert all(r["installed_qty"] == 0 for t, r in items.items() if t != hulk)
    # Fix pass: the wave the engine sized against rides the row (no
    # runs-per-BPC here, so it is the request); non-finals carry none.
    assert (items[hulk]["requested_qty"], items[hulk]["wave_qty"]) == (8, 8)
    assert all(
        (r["requested_qty"], r["wave_qty"]) == (0, None)
        for t, r in items.items()
        if t != hulk
    )


def test_a_replan_after_the_whole_wave_plans_no_more_hulls(conn, ref):
    """The doubling case (review B1): all eight Hulks installed, then an
    ESI update re-plans the open run. The Hulk plans 0 runs; every
    intermediate is still planned exactly as before the installs. That
    is NOT "no inputs": the stages built this cycle feed the next wave,
    and a consumer whose jobs are all installed still holds jobs for its
    suppliers' targets (contract amendment 3). Only the Hulk's own runs
    and its direct raw draw leave the plan."""
    first, steady, draw_of = _steady_hulk_run(conn, ref)
    run_id = first.index_run_id
    hulk = ref.type_id("Hulk")
    eight = draw_of(first.items[hulk], 8, 8)
    store.save_esi_snapshot(
        conn, _installed_from(steady, eight), {hulk: 8}, {}, 0.0, 0.0,
        job_starts={hulk: [[AFTER_CUT, 1]] * 8},
    )

    again = engine.plan_index_run(
        conn, ref, _snap(conn, ref), replace_index_run_id=run_id,
        cycle_cut=_cut(conn, run_id),
    )

    h = again.items[hulk]
    assert (h.installed_qty, h.target_stock_qty) == (8, 8)
    assert (h.deficit_qty, h.runs_allocated, h.recommended_build_qty) == (0, 0, 0)
    assert h.recommended_action is None
    stages = [t for t, i in first.items.items() if i.buildable and t != hulk]
    assert _stage_figures(again, stages) == _stage_figures(first, stages)
    total = lambda p: sum(i.runs_allocated for i in p.items.values())
    assert total(again) == total(first) - 8
    for m, q in eight.items():
        if not first.items[m].buildable:
            assert again.items[m].target_stock_qty == (
                first.items[m].target_stock_qty - q
            )
    assert _items(conn, run_id)[hulk]["installed_qty"] == 8


def test_a_slot_limited_final_relists_its_rest_against_the_configured_pool(
    conn, ref
):
    """Known limit, pending the user's ruling (review of revision 7,
    2026-09-29; PROJECT.md §11 "Busy lines after the installs"). The
    pools are the configured pools (ruling R2, 2026-09-28): Hulk x 8 on
    6 manufacturing lines plans 6 jobs; once those 6 are installed —
    every line busy — the re-plan still sees 6 free lines and lists the
    other 2 Hulks as jobs to run now, and the executed run's started
    hull count (costing.hull_cost: install_runs + installed_qty) counts
    them. This pins today's behaviour so a ruling to net this cycle's
    running jobs out of the pool flips it deliberately."""
    from magoo import costing

    first, steady, draw_of = _steady_hulk_run(conn, ref)
    run_id = first.index_run_id
    hulk = ref.type_id("Hulk")
    conn.execute("UPDATE settings SET manufacturing_slots = 6")
    conn.commit()
    pre = engine.plan_index_run(
        conn, ref, _snap(conn, ref), replace_index_run_id=run_id,
        cycle_cut=_cut(conn, run_id),
    )
    h0 = pre.items[hulk]
    assert h0.capacity_limited
    assert (h0.jobs_allocated, h0.install_runs) == (6, 6)
    six = draw_of(h0, 6, 6)
    store.save_esi_snapshot(
        conn, _installed_from(steady, six), {hulk: 6}, {}, 0.0, 0.0,
        job_starts={hulk: [[AFTER_CUT, 1]] * 6},
    )
    snap = _snap(conn, ref)
    assert snap.slots_available[config.ACTIVITY_MANUFACTURING] == 6  # R2

    post = engine.plan_index_run(
        conn, ref, snap, replace_index_run_id=run_id,
        cycle_cut=_cut(conn, run_id),
    )

    h = post.items[hulk]
    assert h.installed_qty == 6
    assert (h.deficit_qty, h.runs_allocated, h.install_runs) == (2, 2, 2)
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = "
        "datetime('now') WHERE index_run_id = ?",
        (run_id,),
    )
    conn.commit()
    pipeline_id = conn.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    hulls = costing.hull_cost(
        conn, ref, store.get_settings(conn), run_id, pipeline_id
    )
    # 6 installed + the 2 listed against lines that were busy.
    assert (hulls.hulls_per_cycle, hulls.hulls_planned) == (8, 8)


def test_a_delivered_wave_still_counts(conn, ref):
    """Contract amendment 4: a Hulk job of this cycle that finished and
    was delivered is no longer in-progress output (esi.refresh_state
    records it in job_starts only), yet it is still this cycle's wave.
    Delivered into the hangar, or delivered and already sold: either
    way the re-plan plans no more Hulks."""
    first, steady, draw_of = _steady_hulk_run(conn, ref)
    run_id = first.index_run_id
    hulk = ref.type_id("Hulk")
    hangar = _installed_from(steady, draw_of(first.items[hulk], 8, 8))
    for delivered in ({hulk: 8}, {}):
        store.save_esi_snapshot(
            conn, {**hangar, **delivered}, {}, {}, 0.0, 0.0,
            job_starts={hulk: [[AFTER_CUT, 1]] * 8},
        )
        again = engine.plan_index_run(
            conn, ref, _snap(conn, ref), replace_index_run_id=run_id,
            cycle_cut=_cut(conn, run_id),
        )
        h = again.items[hulk]
        assert (h.installed_qty, h.deficit_qty, h.runs_allocated) == (8, 0, 0)


def test_starts_before_the_cut_or_unrecorded_plan_the_full_wave(conn, ref):
    """Only starts strictly after the cut are this cycle's: the previous
    wave's jobs (started before the run opened) are bound for sale. A
    snapshot whose job_starts is unknown (NULL, or the pre-2026-09-29
    scalar {type_id: latest start} format, which store reads as None),
    or a plan with no cut, sizes the whole wave: the pre-revision-7
    plan."""
    first, _steady, _draw = _steady_hulk_run(conn, ref)
    run_id = first.index_run_id
    hulk = ref.type_id("Hulk")

    def replan(cut):
        return engine.plan_index_run(
            conn, ref, _snap(conn, ref), replace_index_run_id=run_id,
            cycle_cut=cut,
        ).items[hulk]

    store.save_esi_snapshot(
        conn, {}, {hulk: 8}, {}, 0.0, 0.0, job_starts={hulk: [[BEFORE_CUT, 8]]}
    )
    h = replan(_cut(conn, run_id))
    assert (h.installed_qty, h.deficit_qty, h.runs_allocated) == (0, 8, 8)
    # This cycle's starts but no cut: a brand-new run's first plan.
    store.save_esi_snapshot(
        conn, {}, {hulk: 8}, {}, 0.0, 0.0, job_starts={hulk: [[AFTER_CUT, 8]]}
    )
    assert replan(None).deficit_qty == 8
    assert replan(_cut(conn, run_id)).deficit_qty == 0
    # The same start in the old scalar format carries no units: unknown.
    conn.execute(
        "UPDATE esi_snapshot SET job_starts = ? WHERE snapshot_id = "
        "(SELECT MAX(snapshot_id) FROM esi_snapshot)",
        (json.dumps({str(hulk): AFTER_CUT}),),
    )
    conn.commit()
    assert _snap(conn, ref).job_starts is None
    h = replan(_cut(conn, run_id))
    assert (h.installed_qty, h.deficit_qty) == (0, 8)
    assert _items(conn, run_id)[hulk]["installed_qty"] == 0


def test_snapshot_from_state_carries_the_job_starts(conn):
    """engine.Snapshot.job_starts is the snapshot's list format, int-keyed
    as store restores it. A snapshot saved without it reads None (not
    recorded: the full wave), and so does the old scalar format; an
    empty map is a known "no jobs"."""
    store.save_esi_snapshot(
        conn, {}, {}, {}, 0.0, 0.0,
        job_starts={587: [[AFTER_CUT, 2], [BEFORE_CUT, 1]]},
    )
    assert engine.snapshot_from_state(conn).job_starts == {
        587: [[AFTER_CUT, 2], [BEFORE_CUT, 1]]
    }
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    assert engine.snapshot_from_state(conn).job_starts is None
    store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0, job_starts={})
    assert engine.snapshot_from_state(conn).job_starts == {}
    conn.execute(
        "UPDATE esi_snapshot SET job_starts = ? WHERE snapshot_id = "
        "(SELECT MAX(snapshot_id) FROM esi_snapshot)",
        (json.dumps({"587": AFTER_CUT}),),
    )
    conn.commit()
    assert engine.snapshot_from_state(conn).job_starts is None
