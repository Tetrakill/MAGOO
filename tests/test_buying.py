"""Purchases from ESI (v1.29 revisions 3 and 4, user rulings 2026-09-28):
buying.assign_purchases over a temp state database.

Invariants under test: R2's buying windows (executed runs, the newest
planned run, superseded plans, the first executed run, a late pull into a
closed window, reopen / re-execute, legacy completed_at gaps and
inversions) compared as parsed UTC timestamps, never as text (review
C2); the Count buys toggle at read time (R4/C7); internal transfers and
swaps listed but not costed (C9); venue by location (R6 as revision 4
revised it: the hub station, the structure market, anything else
'other'); R7 contract
scaling — hub sell quote, adjusted fallback, region-wide rows dropped,
blueprint copies and unpriced items, the exact allocation and its freeze
on the contract (C10); idempotence (no rewrite when nothing changed); a
purchase on at most one run (C4); one transaction for the whole pass
(C5); types the plan does not buy retained (R3).

Revision 4 (contract §2 and amendments A5-A9): a costed purchase of a
compressed ore / gas the plan did not choose becomes one 'delivered' line
per raw it yields — whole batches, the asserted yields, the landed ISK
(price x converted units + the run's venue freight + the refining tax for
ore, never gas) split by landed plan value, via_type_id set — with the
units short of a batch left as an ore line; plan-chosen ores, ice, a zero
yield and ref None are untouched; an executed run's conversion is frozen.
The arithmetic is hand-worked from the SDE figures (A7), read from the
session `ref` fixture.
"""

import math
import sqlite3

import pytest

from magoo import buying, config, store

A, B = 2001, 2002
CORP = 98000001
SELLER = 91000001            # a stranger on the market
SELLER_CORP = 98000077
STATION = 60003760           # Jita 4-4 (the hub station → hub)
CJ6 = 1049588174021          # the configured structure market → structure
AMARR = 60008494             # another NPC station → other (revision 4)
SOTIYO = 1_035_466_617_946   # another Upwell structure → other (revision 4)
TRIT, PYE, MEX = 34, 35, 36
HULK = 22544
FORGE = 10000002


# --- fixtures and builders -------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    c.execute(
        "INSERT INTO pool_character (character_id, character_name, include_assets, "
        "include_job_slots, count_assets, count_sales) VALUES (?, 'main', 0, 0, 0, 1)",
        (A,),
    )
    c.execute(
        "INSERT INTO esi_corp (corporation_id, corporation_name) VALUES (?, 'Holdings')",
        (CORP,),
    )
    c.commit()
    yield c
    c.close()


def add_run(conn, run_number, status="planned", planned_start="2026-09-01 00:00:00",
            completed_at=None):
    cur = conn.execute(
        "INSERT INTO index_run (run_number, planned_start, status, completed_at) "
        "VALUES (?, ?, ?, ?)",
        (run_number, planned_start, status, completed_at),
    )
    conn.commit()
    return cur.lastrowid


def add_buy(conn, tid, date, type_id=TRIT, qty=100, price=5.0, owner=("character", A),
            location=STATION, client=SELLER):
    conn.execute(
        "INSERT INTO buy_transaction (transaction_id, owner_kind, owner_id, division, "
        "source_feed, type_id, quantity, unit_price, date, location_id, client_id, "
        "journal_ref_id, fetched_at) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, "
        "'2026-09-20T00:00:00Z')",
        (tid, owner[0], owner[1], owner[0], type_id, qty, price, date, location, client),
    )
    conn.commit()


def item(record_id, type_id=TRIT, qty=100, included=True, raw=None):
    return {"record_id": record_id, "type_id": type_id, "quantity": qty,
            "is_included": included, "raw_quantity": raw if raw is not None else qty}


def add_contract(conn, contract_id, items, price=1000.0, reward=None, date="2026-09-03T12:00:00Z",
                 owner=("character", A), issuer=SELLER, issuer_corp=SELLER_CORP,
                 status="finished", type="item_exchange", items_status="ok",
                 location=STATION, title=None, for_corp=False):
    conn.execute(
        "INSERT INTO buy_contract (contract_id, owner_kind, owner_id, issuer_id, "
        "issuer_corporation_id, for_corporation, acceptor_id, type, status, price, reward, "
        "title, date_issued, date_accepted, date_completed, start_location_id, "
        "via_character_id, first_seen_at, last_seen_at, items_fetched_at, items_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-09-01T00:00:00Z', ?, ?, ?, ?, "
        "'2026-09-20T00:00:00Z', '2026-09-20T00:00:00Z', ?, ?)",
        (contract_id, owner[0], owner[1], issuer, issuer_corp, 1 if for_corp else 0,
         owner[1], type, status, price, reward, title, date,
         date if status == "finished" else None, location, A,
         "2026-09-20T00:00:00Z" if items_status else None, items_status),
    )
    conn.executemany(
        "INSERT INTO buy_contract_item (contract_id, record_id, type_id, quantity, "
        "raw_quantity, is_included, is_singleton) VALUES (?, ?, ?, ?, ?, ?, 0)",
        [(contract_id, i["record_id"], i["type_id"], i["quantity"], i["raw_quantity"],
          1 if i["is_included"] else 0) for i in items],
    )
    conn.commit()


def quote(conn, type_id, price, hub=1, source="sell", region=FORGE):
    conn.execute(
        "INSERT OR REPLACE INTO market_price (type_id, region_id, source, price, "
        "fetched_at, hub) VALUES (?, ?, ?, ?, '2026-09-20 00:00:00', ?)",
        (type_id, region, source, price, hub),
    )
    conn.commit()


def adjusted(conn, type_id, price):
    quote(conn, type_id, price, hub=1, source="adjusted", region=0)


def assign(conn, **kwargs):
    return buying.assign_purchases(conn, None, store.get_settings(conn), **kwargs)


def lines(conn, run_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM run_purchase WHERE index_run_id = ? AND esi_kind IS NOT NULL "
        "ORDER BY purchase_id", (run_id,))]


def placement(conn):
    """{(esi_kind, esi_id, type_id): [run ids]} over every derived line."""
    out = {}
    for r in conn.execute(
        "SELECT index_run_id, esi_kind, esi_id, type_id FROM run_purchase "
        "WHERE esi_kind IS NOT NULL"
    ):
        out.setdefault((r["esi_kind"], r["esi_id"], r["type_id"]), set()).add(r["index_run_id"])
    return out


def on_run(conn, run_id):
    return sorted({(l["esi_kind"], l["esi_id"]) for l in lines(conn, run_id)})


def assert_one_run_each(conn):
    assert all(len(runs) == 1 for runs in placement(conn).values())


# --- windows (R2, C2, C3) ------------------------------------------------------


def test_executed_runs_and_the_newest_plan_split_the_timeline(conn):
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 12:00:00")
    r2 = add_run(conn, 2, "complete", "2026-09-05 13:00:00", "2026-09-10 12:00:00")
    r3 = add_run(conn, 3, "planned", "2026-09-10 13:00:00")
    add_buy(conn, 1, "2026-08-31T23:00:00Z")   # before the first run's planned_start
    add_buy(conn, 2, "2026-09-03T00:00:00Z")
    add_buy(conn, 3, "2026-09-07T00:00:00Z")
    add_buy(conn, 4, "2026-09-10T12:30:00Z")   # before run 3's planned_start: the cycle
    add_buy(conn, 5, "2026-09-12T00:00:00Z")   # is bounded by run 2's completion, not it
    summary = assign(conn)
    assert on_run(conn, r1) == [("transaction", 2)]
    assert on_run(conn, r2) == [("transaction", 3)]
    assert on_run(conn, r3) == [("transaction", 4), ("transaction", 5)]
    assert ("transaction", 1, TRIT) not in placement(conn)
    windows = {w.index_run_id: w for w in buying.buying_windows(conn)}
    assert windows[r1].executed and not windows[r3].executed and windows[r3].hi is None
    assert summary.runs == 3 and summary.runs_with_lines == 3 and summary.transactions == 4
    assert summary.run_numbers == [1, 2, 3]
    assert buying.collecting_run_id(conn) == r3


def test_same_day_boundary_compares_timestamps_not_text(conn):
    """C2: completed_at is 'YYYY-MM-DD HH:MM:SS' and ESI dates end in 'Z';
    as text a space sorts before 'T', so every same-day purchase would
    fall after the close. The bound is inclusive at completion."""
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 12:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 12:30:00")
    add_buy(conn, 1, "2026-09-05T10:00:00Z")
    add_buy(conn, 2, "2026-09-05T12:00:00Z")
    add_buy(conn, 3, "2026-09-05T12:00:01Z")
    assert "2026-09-05T10:00:00Z" > "2026-09-05 12:00:00"  # the text trap
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1), ("transaction", 2)]
    assert on_run(conn, r2) == [("transaction", 3)]


def test_first_executed_run_starts_at_its_planned_start_same_day(conn):
    r1 = add_run(conn, 1, "complete", "2026-09-02 08:00:00", "2026-09-06 00:00:00")
    add_buy(conn, 1, "2026-09-02T07:59:59Z")
    add_buy(conn, 2, "2026-09-02T08:00:01Z")
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 2)]


def test_superseded_plans_collect_nothing_and_are_cleared(conn):
    """C3/C4: only the newest non-executed run collects; a plan that
    becomes superseded loses its lines, so no purchase is on two runs.
    R2 as written: before any execution a re-plan starts at its own
    planned_start, so a buy made under the earlier plan lands nowhere."""
    r1 = add_run(conn, 1, "planned", "2026-09-01 00:00:00")
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    add_buy(conn, 2, "2026-09-04T00:00:00Z")
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1), ("transaction", 2)]
    r2 = add_run(conn, 2, "planned", "2026-09-03 00:00:00")
    summary = assign(conn)
    assert lines(conn, r1) == [] and summary.cleared_runs == 1
    assert on_run(conn, r2) == [("transaction", 2)]
    assert buying.run_window(conn, r1) is None and buying.run_purchase_records(conn, r1) == []
    assert buying.collecting_run_id(conn) == r2
    assert_one_run_each(conn)


def test_late_pull_lands_in_a_closed_window(conn):
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 01:00:00")
    add_buy(conn, 1, "2026-09-06T00:00:00Z")
    assign(conn)
    assert on_run(conn, r1) == [] and on_run(conn, r2) == [("transaction", 1)]
    add_buy(conn, 2, "2026-09-04T00:00:00Z")   # pulled after run 1 was executed
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 2)]
    assert on_run(conn, r2) == [("transaction", 1)]


def test_reopen_and_re_execute_move_the_bound(conn):
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 12:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 12:30:00")
    add_buy(conn, 1, "2026-09-05T11:00:00Z")
    add_buy(conn, 2, "2026-09-05T13:00:00Z")
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1)] and on_run(conn, r2) == [("transaction", 2)]
    # Reopen run 1: it is now a superseded plan — cleared — and run 2 has
    # no executed run before it, so it starts at its own planned_start.
    conn.execute("UPDATE index_run SET status = 'planned', completed_at = NULL WHERE index_run_id = ?", (r1,))
    conn.commit()
    assign(conn)
    assert lines(conn, r1) == [] and on_run(conn, r2) == [("transaction", 2)]
    # Re-execute it later: its window now reaches past both buys.
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = '2026-09-05 14:00:00' "
        "WHERE index_run_id = ?", (r1,))
    conn.commit()
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1), ("transaction", 2)]
    assert lines(conn, r2) == []
    assert_one_run_each(conn)


def test_executed_run_without_completed_at_collects_nothing_and_inversions_are_empty(conn):
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", None)          # pre-v1.5
    r2 = add_run(conn, 2, "complete", "2026-09-04 00:00:00", "2026-09-08 00:00:00")
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    add_buy(conn, 2, "2026-09-05T00:00:00Z")
    assign(conn)
    assert lines(conn, r1) == []
    assert on_run(conn, r2) == [("transaction", 2)]   # lo = its own planned_start
    # A legacy inversion: run 3 stamped before run 2 — an empty window.
    r3 = add_run(conn, 3, "complete", "2026-09-06 00:00:00", "2026-09-07 00:00:00")
    add_buy(conn, 3, "2026-09-06T12:00:00Z")
    assign(conn)
    assert lines(conn, r3) == [] and on_run(conn, r2) == [("transaction", 2), ("transaction", 3)]
    assert_one_run_each(conn)


def test_newest_executed_run_leaves_nothing_collecting(conn):
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    add_buy(conn, 1, "2026-09-06T00:00:00Z")
    assign(conn)
    assert lines(conn, r1) == [] and buying.collecting_run_id(conn) is None


def test_unparseable_date_is_not_matched(conn):
    r1 = add_run(conn, 1)
    add_buy(conn, 1, "not a date")
    add_buy(conn, 2, "2026-09-02T00:00:00Z")
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 2)]


def _replan_in_place(conn, run_id, planned_start):
    """What engine.plan_index_run(replace_index_run_id=…) does to the
    index_run row (contract review A2/A3): planned_start moves, opened_at
    keeps the run's first opening — both SET expressions read the OLD
    row, so a legacy run with no opened_at keeps its original start."""
    conn.execute(
        "UPDATE index_run SET opened_at = COALESCE(opened_at, planned_start), "
        "planned_start = ? WHERE index_run_id = ?",
        (planned_start, run_id),
    )
    conn.commit()


def test_a_re_plan_in_place_keeps_the_cycles_purchases(conn):
    """Revision 6 (R1, contract review A3): the open run is re-planned in
    place on every ESI update, which moves planned_start to now. The
    window opens at opened_at, so a purchase dated after the run opened
    but before the re-plan stays on the run — and after Mark executed it
    is in the FIRST executed run's window, so realized cost keeps it."""
    r1 = add_run(conn, 1, "planned", "2026-09-01 00:00:00")
    conn.execute(
        "UPDATE index_run SET opened_at = '2026-09-01 00:00:00' WHERE index_run_id = ?",
        (r1,),
    )
    conn.commit()
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    _replan_in_place(conn, r1, "2026-09-03 00:00:00")
    add_buy(conn, 2, "2026-09-04T00:00:00Z")
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1), ("transaction", 2)]
    assert buying.collecting_run_id(conn) == r1
    window = buying.run_window(conn, r1)
    assert window.lo == buying._ts("2026-09-01 00:00:00")
    # Mark executed: the first executed run's window is (opened_at, completed_at].
    conn.execute(
        "UPDATE index_run SET status = 'complete', completed_at = '2026-09-05 00:00:00' "
        "WHERE index_run_id = ?", (r1,))
    conn.commit()
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1), ("transaction", 2)]
    assert buying.run_window(conn, r1).executed


def test_a_legacy_open_run_keeps_its_original_opening(conn):
    """A run planned before opened_at existed (NULL) falls back to
    planned_start; the first in-place re-plan copies that original
    planned_start into opened_at in the same UPDATE, so the purchase
    made before the re-plan stays on the run."""
    r1 = add_run(conn, 1, "planned", "2026-09-01 00:00:00")
    assert conn.execute(
        "SELECT opened_at FROM index_run WHERE index_run_id = ?", (r1,)
    ).fetchone()[0] is None
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    assert buying.run_window(conn, r1).lo == buying._ts("2026-09-01 00:00:00")
    _replan_in_place(conn, r1, "2026-09-03 00:00:00")
    row = conn.execute(
        "SELECT opened_at, planned_start FROM index_run WHERE index_run_id = ?", (r1,)
    ).fetchone()
    assert tuple(row) == ("2026-09-01 00:00:00", "2026-09-03 00:00:00")
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1)]


def test_an_executed_runs_successor_still_opens_at_the_last_execution(conn):
    """opened_at only replaces planned_start as the FIRST window's lower
    bound; after an execution the next window opens at its completed_at
    whatever the newer run's opened_at says."""
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-07 00:00:00")
    conn.execute(
        "UPDATE index_run SET opened_at = '2026-09-07 00:00:00' WHERE index_run_id = ?",
        (r2,),
    )
    conn.commit()
    add_buy(conn, 1, "2026-09-06T00:00:00Z")
    assign(conn)
    assert on_run(conn, r2) == [("transaction", 1)] and lines(conn, r1) == []


# --- owners, exclusions, venue ----------------------------------------------------


def test_count_buys_is_honoured_at_read_time(conn):
    r1 = add_run(conn, 1)
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    add_buy(conn, 2, "2026-09-02T01:00:00Z", owner=("corporation", CORP))
    add_contract(conn, 3, [item(1)], date="2026-09-02T02:00:00Z")
    quote(conn, TRIT, 5.0)
    assign(conn)
    assert on_run(conn, r1) == [("contract", 3), ("transaction", 1), ("transaction", 2)]
    conn.execute("UPDATE pool_character SET count_buys = 0")
    conn.execute("UPDATE esi_corp SET count_buys = 0")
    conn.commit()
    assign(conn)
    assert lines(conn, r1) == []
    statuses = {r.esi_id: r.status for r in buying.run_purchase_records(conn, r1)}
    assert statuses == {1: "buys_off", 2: "buys_off", 3: "buys_off"}
    conn.execute("UPDATE pool_character SET count_buys = 1")
    conn.commit()
    assign(conn)
    assert on_run(conn, r1) == [("contract", 3), ("transaction", 1)]
    # A character that left the pool no longer counts.
    conn.execute("DELETE FROM pool_character WHERE character_id = ?", (A,))
    conn.commit()
    assign(conn)
    assert lines(conn, r1) == []


def test_internal_transfers_are_listed_but_not_costed(conn):
    """C9: the alt that buys at Jita and hands the goods to the main is
    one purchase, not two — a market buy from our own seller, and a
    contract issued by us (the issuer, or on behalf of our corporation),
    write no line."""
    conn.execute(
        "INSERT INTO pool_character (character_id, character_name, include_assets, "
        "include_job_slots, count_assets, count_sales) VALUES (?, 'alt', 0, 0, 0, 1)", (B,))
    conn.commit()
    r1 = add_run(conn, 1)
    quote(conn, TRIT, 5.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", owner=("character", B))            # the alt's Jita buy
    add_buy(conn, 2, "2026-09-02T01:00:00Z", client=B)                          # bought from the alt
    add_contract(conn, 3, [item(1)], price=0.0, issuer=B, issuer_corp=CORP)     # the hand-over
    add_contract(conn, 4, [item(1)], issuer=77, issuer_corp=CORP, for_corp=True,
                 date="2026-09-03T13:00:00Z")                                   # FOR our corp
    summary = assign(conn)
    assert on_run(conn, r1) == [("transaction", 1)]
    records = {r.esi_id: r for r in buying.run_purchase_records(conn, r1)}
    assert records[2].status == "internal" and records[3].status == "internal"
    assert records[4].status == "internal"
    assert records[3].status_label == "internal — not costed"
    assert conn.execute("SELECT excluded FROM buy_contract WHERE contract_id = 3").fetchone()[0] == "internal"
    assert summary.not_costed == 3 and summary.transactions == 1 and summary.contracts == 0


def test_a_corpmate_selling_personally_is_a_purchase(conn):
    """Review 2026-09-28 (P1): ESI fills issuer_corporation_id on EVERY
    contract, so our corporation's id there does not make it internal. A
    corpmate outside the pool selling to us personally (for_corporation
    false) is a real purchase: costed, on the run, off Remaining."""
    r1 = add_run(conn, 1)
    quote(conn, TRIT, 5.0)
    add_contract(conn, 20, [item(1, TRIT, 10_000)], price=50e6, issuer=91000077,
                 issuer_corp=CORP, for_corp=False)
    summary = assign(conn)
    assert conn.execute(
        "SELECT excluded FROM buy_contract WHERE contract_id = 20").fetchone()[0] is None
    assert on_run(conn, r1) == [("contract", 20)]
    (line,) = lines(conn, r1)
    assert line["quantity"] == 10_000 and line["unit_price"] == pytest.approx(5000.0)
    assert summary.contracts == 1 and summary.not_costed == 0
    # The same seller issuing it on the corporation's behalf is ours.
    conn.execute("UPDATE buy_contract SET for_corporation = 1 WHERE contract_id = 20")
    conn.commit()
    assign(conn)
    assert lines(conn, r1) == []
    assert buying.run_purchase_records(conn, r1)[0].status == "internal"


def test_venue_follows_where_it_was_bought(conn):
    """R6 as revision 4 revised it (user ruling 2026-09-28): only Jita 4-4
    is the hub and only the configured structure market the structure;
    another station, another structure and no location are 'other' (the
    default inbound rate). Before, every NPC station and no location
    were the hub and every structure the structure market."""
    r1 = add_run(conn, 1)
    quote(conn, TRIT, 5.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", location=STATION)
    add_buy(conn, 2, "2026-09-02T01:00:00Z", location=CJ6)
    add_contract(conn, 3, [item(1)], date="2026-09-02T02:00:00Z", location=None)
    add_contract(conn, 4, [item(1)], date="2026-09-02T03:00:00Z", location=CJ6)
    add_buy(conn, 5, "2026-09-02T04:00:00Z", location=AMARR)
    add_buy(conn, 6, "2026-09-02T05:00:00Z", location=SOTIYO)
    add_contract(conn, 7, [item(1)], date="2026-09-02T06:00:00Z", location=AMARR)
    assign(conn)
    venue = {(l["esi_kind"], l["esi_id"]): l["venue"] for l in lines(conn, r1)}
    assert venue == {
        ("transaction", 1): "hub", ("transaction", 2): "structure",
        ("contract", 3): "other", ("contract", 4): "structure",
        ("transaction", 5): "other", ("transaction", 6): "other",
        ("contract", 7): "other",
    }
    records = {(r.kind, r.esi_id): r for r in buying.run_purchase_records(conn, r1)}
    assert records[("transaction", 6)].venue == "other"
    assert records[("transaction", 6)].location_id == SOTIYO
    line = next(l for l in lines(conn, r1) if l["esi_id"] == 1)
    assert (line["source"], line["owner_kind"], line["owner_id"], line["date"]) == (
        None, "character", A, "2026-09-02T00:00:00Z")
    assert (line["quantity"], line["unit_price"], line["contract_k"]) == (100, 5.0, None)


# --- R7: contract scaling ---------------------------------------------------------


def test_contract_scales_hub_quotes_with_adjusted_fallback_and_freezes(conn):
    r1 = add_run(conn, 1)
    quote(conn, TRIT, 50.0)                 # hub sell
    quote(conn, PYE, 999.0, hub=0)          # a region-wide fallback: dropped
    adjusted(conn, PYE, 40.0)               # ... so CCP's adjusted price stands in
    adjusted(conn, TRIT, 1.0)               # the hub quote wins over adjusted
    add_contract(conn, 7, [item(1, TRIT, 10), item(2, PYE, 5)], price=1000.0, title="minerals")
    assign(conn)
    k = 1000.0 / (10 * 50.0 + 5 * 40.0)
    got = {l["type_id"]: l for l in lines(conn, r1)}
    assert got[TRIT]["unit_price"] == pytest.approx(k * 50.0)
    assert got[PYE]["unit_price"] == pytest.approx(k * 40.0)
    assert all(l["contract_k"] == pytest.approx(k) for l in got.values())
    assert sum(l["unit_price"] * l["quantity"] for l in got.values()) == pytest.approx(1000.0)
    row = conn.execute("SELECT * FROM buy_contract WHERE contract_id = 7").fetchone()
    assert row["k"] == pytest.approx(k) and row["priced_at"] is not None
    assert row["unpriced_items"] == 0 and row["excluded"] is None
    record = buying.run_purchase_records(conn, r1)[0]
    assert record.status == "costed" and record.title == "minerals" and record.isk == 1000.0
    # Frozen on the contract: a later cache move never re-prices it...
    quote(conn, TRIT, 500.0)
    adjusted(conn, PYE, 1.0)
    assign(conn)
    assert conn.execute("SELECT k FROM buy_contract WHERE contract_id = 7").fetchone()[0] == pytest.approx(k)
    assert {l["type_id"]: l["unit_price"] for l in lines(conn, r1)} == {
        TRIT: got[TRIT]["unit_price"], PYE: got[PYE]["unit_price"]}
    # ... and a reopen that moves it to another run keeps its prices.
    conn.execute("UPDATE index_run SET status = 'complete', completed_at = '2026-09-02 00:00:00' "
                 "WHERE index_run_id = ?", (r1,))
    conn.commit()
    r2 = add_run(conn, 2, "planned", "2026-09-02 01:00:00")
    assign(conn)
    assert lines(conn, r1) == []
    assert {l["type_id"]: l["unit_price"] for l in lines(conn, r2)} == {
        TRIT: got[TRIT]["unit_price"], PYE: got[PYE]["unit_price"]}


def test_unpriced_items_cost_zero_until_the_cache_fills(conn):
    """An item with neither price gets 0 and flags the contract; the
    allocation is re-derived on every pass while any item is unpriced,
    and freezes once they all are."""
    r1 = add_run(conn, 1)
    quote(conn, TRIT, 10.0)
    add_contract(conn, 8, [item(1, TRIT, 50), item(2, MEX, 20)], price=800.0)
    summary = assign(conn)
    got = {l["type_id"]: l for l in lines(conn, r1)}
    assert got[TRIT]["unit_price"] == pytest.approx(800.0 / 50)   # carries the whole price
    assert got[MEX]["unit_price"] == 0.0 and got[MEX]["quantity"] == 20
    row = conn.execute("SELECT * FROM buy_contract WHERE contract_id = 8").fetchone()
    assert row["unpriced_items"] == 1 and row["priced_at"] is None and row["excluded"] is None
    assert summary.unpriced_items == 1 and summary.contracts == 1
    assert buying.run_purchase_records(conn, r1)[0].unpriced_items == 1
    adjusted(conn, MEX, 15.0)
    assign(conn)
    k = 800.0 / (50 * 10.0 + 20 * 15.0)
    got = {l["type_id"]: l for l in lines(conn, r1)}
    assert got[TRIT]["unit_price"] == pytest.approx(k * 10.0)
    assert got[MEX]["unit_price"] == pytest.approx(k * 15.0)
    row = conn.execute("SELECT * FROM buy_contract WHERE contract_id = 8").fetchone()
    assert row["unpriced_items"] == 0 and row["priced_at"] is not None


def test_no_reference_price_and_blueprint_copies(conn):
    """C10.3/C10.4: a BPC is never priced (its adjusted price is the
    BPO's and would swallow P); with no received item priced the contract
    is 'no_price' — listed, k NULL, nothing costed (never every unit at
    0, which would lose P)."""
    r1 = add_run(conn, 1)
    adjusted(conn, HULK + 1, 5e9)   # a BPO-sized adjusted price on the blueprint type
    add_contract(conn, 9, [item(1, HULK + 1, 1, raw=-2)], price=20e6)
    add_contract(conn, 10, [item(1, HULK + 1, 1, raw=-2), item(2, TRIT, 1000)], price=20e6,
                 date="2026-09-03T13:00:00Z")
    quote(conn, TRIT, 4.0)
    summary = assign(conn)
    row = conn.execute("SELECT * FROM buy_contract WHERE contract_id = 9").fetchone()
    assert row["excluded"] == "no_price" and row["k"] is None and row["priced_at"] is None
    assert on_run(conn, r1) == [("contract", 10)]
    got = {l["type_id"]: l for l in lines(conn, r1)}
    assert got[TRIT]["unit_price"] == pytest.approx(20e6 / 1000)   # the BPC takes 0
    assert got[HULK + 1]["unit_price"] == 0.0
    statuses = {r.esi_id: r.status for r in buying.run_purchase_records(conn, r1)}
    assert statuses == {9: "no_price", 10: "costed"}
    assert summary.not_costed == 1
    # The later cache fill does not help a BPC; the contract stays unpriced.
    assign(conn)
    assert conn.execute("SELECT excluded FROM buy_contract WHERE contract_id = 9").fetchone()[0] == "no_price"
    # Contract 10's only priceable item is priced: it froze despite the
    # BPC, which stays flagged.
    row = conn.execute("SELECT * FROM buy_contract WHERE contract_id = 10").fetchone()
    assert row["priced_at"] is not None and row["unpriced_items"] == 1


def test_a_kit_with_a_blueprint_copy_freezes_on_its_first_priced_pass(conn):
    """Review 2026-09-28 (P2): a BPC can never be priced, so it must not
    hold the freeze back; otherwise a kit (BPC + minerals) re-splits P by
    the live quote ratio on every pass and an EXECUTED run's lines move
    with the market long after it closed."""
    r7 = add_run(conn, 7, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    add_run(conn, 8, "planned", "2026-09-05 01:00:00")
    quote(conn, TRIT, 4.0)
    quote(conn, PYE, 10.0)
    add_contract(conn, 30, [item(1, HULK + 1, 1, raw=-2), item(2, TRIT, 10_000),
                            item(3, PYE, 4_000)], price=100e6)
    assign(conn)
    k = 100e6 / (10_000 * 4.0 + 4_000 * 10.0)
    before = {l["type_id"]: l["unit_price"] for l in lines(conn, r7)}
    assert before == {HULK + 1: 0.0, TRIT: pytest.approx(k * 4.0), PYE: pytest.approx(k * 10.0)}
    row = conn.execute("SELECT * FROM buy_contract WHERE contract_id = 30").fetchone()
    assert row["priced_at"] is not None and row["unpriced_items"] == 1
    assert row["k"] == pytest.approx(k)
    record = buying.run_purchase_records(conn, r7)[0]
    assert record.costed and record.unpriced_items == 1
    # The market moves: the executed run's lines do not.
    quote(conn, TRIT, 8.0)
    quote(conn, PYE, 5.0)
    summary = assign(conn)
    assert {l["type_id"]: l["unit_price"] for l in lines(conn, r7)} == before
    assert summary.changed_runs == 0
    assert sum(l["unit_price"] * l["quantity"] for l in lines(conn, r7)) == pytest.approx(100e6)


def test_allocation_complete_ignores_blueprint_copies_only():
    items = [dict(record_id=1, type_id=HULK + 1, quantity=1, raw_quantity=-2, is_included=1),
             dict(record_id=2, type_id=TRIT, quantity=10, raw_quantity=10, is_included=1),
             dict(record_id=3, type_id=MEX, quantity=10, raw_quantity=10, is_included=1)]
    partial = buying.allocate_contract(100.0, items, {TRIT: 5.0})
    assert partial.unpriced == 2 and partial.blueprint_copies == 1 and not partial.complete
    full = buying.allocate_contract(100.0, items, {TRIT: 5.0, MEX: 5.0})
    assert full.unpriced == 1 and full.complete


def test_swaps_and_rewards(conn):
    r1 = add_run(conn, 1)
    quote(conn, TRIT, 5.0)
    quote(conn, PYE, 5.0)
    add_contract(conn, 11, [item(1), item(2, PYE, 10, included=False)], price=100.0)   # we gave items
    add_contract(conn, 12, [item(1)], price=100.0, reward=100.0)                       # P = 0
    add_contract(conn, 13, [item(1)], price=None)                                      # NULL price
    add_contract(conn, 14, [item(1, TRIT, 100)], price=200.0, reward=50.0)             # P = 150
    assign(conn)
    statuses = {r.esi_id: r.status for r in buying.run_purchase_records(conn, r1)}
    assert statuses == {11: "swap", 12: "swap", 13: "swap", 14: "costed"}
    (line,) = lines(conn, r1)
    assert line["esi_id"] == 14 and line["unit_price"] == pytest.approx(1.5)
    assert line["contract_k"] == pytest.approx(150.0 / 500.0)


def test_contract_needs_its_items_and_a_finish(conn):
    r1 = add_run(conn, 1)
    quote(conn, TRIT, 5.0)
    add_contract(conn, 15, [], items_status=None)
    add_contract(conn, 16, [item(1)], status="outstanding")
    add_contract(conn, 17, [item(1)], type="courier")
    assign(conn)
    assert lines(conn, r1) == []
    assert [(r.esi_id, r.status) for r in buying.run_purchase_records(conn, r1)] == [(15, "no_items")]
    conn.execute("UPDATE buy_contract SET items_status = 'ok' WHERE contract_id = 15")
    conn.execute("INSERT INTO buy_contract_item (contract_id, record_id, type_id, quantity, "
                 "raw_quantity, is_included, is_singleton) VALUES (15, 1, ?, 10, 10, 1, 0)", (TRIT,))
    conn.commit()
    assign(conn)
    assert on_run(conn, r1) == [("contract", 15)]


def test_allocation_is_exact():
    items = [
        {"record_id": 1, "type_id": TRIT, "quantity": 7, "raw_quantity": 7, "is_included": 1},
        {"record_id": 2, "type_id": PYE, "quantity": 3, "raw_quantity": 3, "is_included": 1},
        {"record_id": 3, "type_id": MEX, "quantity": 2, "raw_quantity": 2, "is_included": 1},
    ]
    alloc = buying.allocate_contract(1234.5, items, {TRIT: 3.3, PYE: 17.0})
    assert alloc.unpriced == 1 and not alloc.complete and alloc.unit_prices[3] == 0.0
    assert sum(alloc.unit_prices[i["record_id"]] * i["quantity"] for i in items) == pytest.approx(1234.5)
    assert buying.allocate_contract(10.0, items, {}).excluded == "no_price"
    assert buying.allocate_contract(10.0, items, {TRIT: math.inf}).excluded == "no_price"
    # A blueprint copy stays unpriced even when its type has a price (a
    # BPO beside it in the same contract shares the type id).
    bpo_bpc = [
        {"record_id": 1, "type_id": HULK + 1, "quantity": 1, "raw_quantity": -1, "is_included": 1},
        {"record_id": 2, "type_id": HULK + 1, "quantity": 1, "raw_quantity": -2, "is_included": 1},
    ]
    alloc = buying.allocate_contract(900.0, bpo_bpc, {HULK + 1: 300.0})
    assert alloc.unit_prices == {1: 900.0, 2: 0.0} and alloc.unpriced == 1
    assert buying.contract_isk(None, None) == 0.0 and buying.contract_isk(200, 50) == 150.0


# --- the pass --------------------------------------------------------------------


def test_types_outside_the_plan_are_retained(conn):
    """R3: a buy of something the run does not buy is still recorded —
    the Buy tab counts it — and never touches another row."""
    r1 = add_run(conn, 1)
    conn.execute("INSERT INTO index_run_item (index_run_id, type_id, recommended_buy_qty) "
                 "VALUES (?, ?, 500)", (r1, TRIT))
    conn.commit()
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=PYE)
    assign(conn)
    assert [(l["type_id"], l["quantity"]) for l in lines(conn, r1)] == [(PYE, 100)]


def test_pass_is_idempotent(conn):
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 01:00:00")
    quote(conn, TRIT, 5.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    add_buy(conn, 2, "2026-09-06T00:00:00Z", location=CJ6)
    add_contract(conn, 3, [item(1), item(2, MEX, 5)], date="2026-09-06T01:00:00Z")
    first = assign(conn)
    dump = lambda t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
    before = {t: dump(t) for t in ("run_purchase", "buy_contract", "buy_contract_item")}
    second = assign(conn)
    assert {t: dump(t) for t in before} == before   # purchase ids included: nothing rewritten
    assert first.changed_runs == 2 and second.changed_runs == 0
    assert (second.lines, second.transactions, second.contracts) == (4, 2, 1)
    assert on_run(conn, r1) == [("transaction", 1)]
    assert on_run(conn, r2) == [("contract", 3), ("transaction", 2)]


def test_hand_entered_lines_are_never_touched(conn):
    r1 = add_run(conn, 1)
    store.add_purchase(conn, r1, TRIT, "delivered", 10, 4.0)
    conn.commit()
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    assign(conn)
    conn.execute("UPDATE index_run SET status = 'complete', completed_at = '2026-09-01 12:00:00'")
    conn.commit()
    assign(conn)
    rows = conn.execute("SELECT esi_kind, venue FROM run_purchase WHERE index_run_id = ?", (r1,)).fetchall()
    assert [tuple(r) for r in rows] == [(None, "delivered")]


def test_one_transaction_for_the_whole_pass(conn, monkeypatch):
    """C5: a failure half way leaves every run as it was."""
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 01:00:00")
    add_buy(conn, 1, "2026-09-02T00:00:00Z")
    assign(conn)
    before = lines(conn, r1)
    add_buy(conn, 2, "2026-09-03T00:00:00Z")
    add_buy(conn, 3, "2026-09-06T00:00:00Z")
    real = store.replace_derived_purchases
    calls = []

    def flaky(c, run_id, new_lines):
        calls.append(run_id)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(c, run_id, new_lines)

    monkeypatch.setattr(store, "replace_derived_purchases", flaky)
    with pytest.raises(sqlite3.OperationalError):
        assign(conn)
    assert not conn.in_transaction
    assert lines(conn, r1) == before and lines(conn, r2) == []
    monkeypatch.setattr(store, "replace_derived_purchases", real)
    assign(conn)
    assert on_run(conn, r1) == [("transaction", 1), ("transaction", 2)]
    assert on_run(conn, r2) == [("transaction", 3)]


def test_costing_reads_the_derived_lines(conn):
    """The seam into §5: costing's purchase override reads run_purchase
    through store.list_purchases, the derived lines included."""
    r1 = add_run(conn, 1)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", qty=40, price=6.0)
    assign(conn)
    listed = store.list_purchases(conn, r1)
    assert [(l["quantity"], l["unit_price"], l["venue"]) for l in listed[TRIT]] == [(40, 6.0, "hub")]


def test_summary_line_wording(conn):
    assert buying.summary_line(buying.AssignSummary()) == "purchases: none in the current buying windows"
    line = buying.summary_line(buying.AssignSummary(
        transactions=5, contracts=2, run_numbers=[3, 4], unpriced_items=1, not_costed=2))
    assert line == (
        "purchases: 5 buys, 2 contracts matched (runs 3, 4) (1 contract items with no "
        "reference price; 2 not costed — internal, swap or no price)"
    )
    assert buying.summary_line(buying.AssignSummary(transactions=1, run_numbers=[7])) == (
        "purchases: 1 buys, 0 contracts matched (run 7)")


# --- revision 4: the purchase venue resolver (§2b, A4) ------------------------------


def test_purchase_venue_resolves_the_hub_station_the_structure_market_and_elsewhere(conn):
    settings = store.get_settings(conn)
    assert settings.structure_market() == config.CJ6_KEEPSTAR_STRUCTURE_ID == CJ6
    assert buying.purchase_venue(STATION, settings) == store.BUY_VENUE_HUB
    assert buying.purchase_venue(CJ6, settings) == store.BUY_VENUE_STRUCTURE
    assert buying.purchase_venue(AMARR, settings) == store.BUY_VENUE_OTHER
    assert buying.purchase_venue(SOTIYO, settings) == store.BUY_VENUE_OTHER
    assert buying.purchase_venue(None, settings) == store.BUY_VENUE_OTHER
    # A custom structure market moves the structure venue; the Keepstar is
    # then just another structure.
    conn.execute("UPDATE settings SET capital_market_mode = 'custom', "
                 "capital_structure_id = ?", (SOTIYO,))
    conn.commit()
    custom = store.get_settings(conn)
    assert buying.purchase_venue(SOTIYO, custom) == store.BUY_VENUE_STRUCTURE
    assert buying.purchase_venue(CJ6, custom) == store.BUY_VENUE_OTHER
    # A price region with no configured station keeps Jita 4-4 as the hub
    # (the hub leg is the "from Jita" courier); PRICE_STATION_FILTERS[...]
    # would raise KeyError there.
    conn.execute("UPDATE settings SET price_region_id = 10000043")   # Domain
    conn.commit()
    domain = store.get_settings(conn)
    assert 10000043 not in config.PRICE_STATION_FILTERS
    assert buying.purchase_venue(STATION, domain) == store.BUY_VENUE_HUB
    assert buying.purchase_venue(AMARR, domain) == store.BUY_VENUE_OTHER


# --- revision 4: unplanned compressed ore → the raws it yields (§2, A5-A9) ----------

SCORDITE = 62520     # Compressed Scordite: portion 100 → Trit 150 + Pye 110, 0.0015 m³
VELDSPAR = 62516     # Compressed Veldspar: portion 100 → Trit 400, 0.001 m³
C50_GAS = 62399      # Compressed Fullerite-C50: portion 1 → 1 Fullerite-C50, 0.1 m³
C50 = 30370          # Fullerite-C50 (group 711)
BLUE_ICE = 28433     # Compressed Blue Ice: Ice Products only — never converted


def add_item(conn, run_id, type_id, **cols):
    names = ", ".join(["index_run_id", "type_id", *cols])
    marks = ", ".join("?" * (2 + len(cols)))
    conn.execute(f"INSERT INTO index_run_item ({names}) VALUES ({marks})",
                 (run_id, type_id, *cols.values()))
    conn.commit()


def set_rates(conn, run_id, hub=None, structure=None, other=None):
    """The inbound rates the run was PLANNED at (index_run columns)."""
    conn.execute(
        "UPDATE index_run SET freight_in_isk_per_m3 = ?, "
        "structure_freight_in_isk_per_m3 = ?, freight_in_default_isk_per_m3 = ? "
        "WHERE index_run_id = ?", (hub, structure, other, run_id))
    conn.commit()


def set_settings(conn, **cols):
    conn.execute("UPDATE settings SET " + ", ".join(f"{k} = ?" for k in cols),
                 tuple(cols.values()))
    conn.commit()


def assign_ref(conn, ref, **kwargs):
    return buying.assign_purchases(conn, ref, store.get_settings(conn), **kwargs)


def shape(conn, run_id):
    """(type_id, venue, quantity, unit_price, via_type_id) per derived line."""
    return [(l["type_id"], l["venue"], l["quantity"], l["unit_price"], l["via_type_id"])
            for l in lines(conn, run_id)]


def plan_chose_veldspar(conn, run_id):
    """The plan bought Compressed Veldspar for Tritanium and priced both
    minerals landed through effective_unit_cost."""
    add_item(conn, run_id, VELDSPAR, recommended_buy_qty=1000,
             compressed_outputs="[[34, 3625, 3625]]")
    add_item(conn, run_id, TRIT, recommended_buy_qty=0, effective_unit_cost=4.0)
    add_item(conn, run_id, PYE, recommended_buy_qty=5000, effective_unit_cost=10.0)


def test_unplanned_ore_becomes_mineral_lines_hand_worked(conn, ref):
    """A7's reference case: 250 Compressed Scordite @ 100 ISK at Jita 4-4
    on a run planned at 1,000 ISK/m³ inbound, yield 0.9063, tax 4 %,
    p_Trit 4.0 and p_Pye 10.0 landed, while the plan chose Veldspar.

        B = 250 // 100 = 2 batches, C = 200 converted, R = 50 remain
        Trit = floor(2 × 150 × 0.9063) = floor(271.89)  = 271
        Pye  = floor(2 × 110 × 0.9063) = floor(199.386) = 199
        ISK  = 100 × 200 + 1,000 × 0.0015 × 200 + 0.04 × (271 × 4 + 199 × 10)
             = 20,000 + 300 + 122.96 = 20,422.96
        Trit = 20,422.96 × 1,084 / 3,074 = 7,201.850566 → 26.575094 / unit
        Pye  = 20,422.96 × 1,990 / 3,074 = 13,221.109434 → 66.437736 / unit
    """
    settings = store.get_settings(conn)
    assert (settings.compressed_ore_yield, settings.compressed_reprocess_tax) == (0.9063, 0.04)
    r1 = add_run(conn, 1)
    set_rates(conn, r1, hub=1000.0, structure=0.0, other=0.0)
    plan_chose_veldspar(conn, r1)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=SCORDITE, qty=250, price=100.0)
    summary = assign_ref(conn, ref)
    got = shape(conn, r1)
    assert [(t, v, q, via) for t, v, q, _u, via in got] == [
        (TRIT, "delivered", 271, SCORDITE),
        (PYE, "delivered", 199, SCORDITE),
        (SCORDITE, "hub", 50, None),          # the remainder, outside the plan
    ]
    assert got[0][3] == pytest.approx(26.575094, abs=1e-6)
    assert got[1][3] == pytest.approx(66.437736, abs=1e-6)
    assert 271 * got[0][3] == pytest.approx(7201.850566, abs=1e-6)
    assert 199 * got[1][3] == pytest.approx(13221.109434, abs=1e-6)
    assert got[2][3] == 100.0
    assert 271 * got[0][3] + 199 * got[1][3] == pytest.approx(20422.96, abs=1e-6)
    # The ESI identity rides on every line; contract_k stays NULL on a buy.
    for line in lines(conn, r1):
        assert (line["esi_kind"], line["esi_id"], line["owner_kind"], line["owner_id"],
                line["date"], line["contract_k"]) == (
            "transaction", 1, "character", A, "2026-09-02T00:00:00Z", None)
    assert summary.refined == 1 and summary.transactions == 1
    # The Purchases annotation's reader: outputs, landed ISK, remainder.
    (refined,) = buying.refined_ores(conn, r1)[("transaction", 1)]
    assert refined.ore_type_id == SCORDITE and refined.remainder == 50
    assert refined.outputs == ((TRIT, 271), (PYE, 199))
    assert refined.landed_isk == pytest.approx(20422.96, abs=1e-6)
    (record,) = buying.run_purchase_records(conn, r1)
    assert record.received_units(SCORDITE) == 250 and record.received_units(TRIT) == 0
    assert record.received_units(SCORDITE) - refined.remainder == 200


def test_compressed_gas_is_decompressed_untaxed(conn, ref):
    """1,000 Compressed Fullerite-C50 @ 500 at yield 0.95 → 950 Fullerite-
    C50 (portion 1, one for one). Gas pays no refining tax (engine
    tax_of): ISK = 500 × 1,000 + 1,000 ISK/m³ × 0.1 × 1,000 = 600,000,
    never + 0.04 × 950 × 2,000."""
    r1 = add_run(conn, 1)
    set_rates(conn, r1, hub=1000.0, structure=0.0, other=0.0)
    add_item(conn, r1, C50, recommended_buy_qty=3000, effective_unit_cost=2000.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=C50_GAS, qty=1000, price=500.0)
    assign_ref(conn, ref)
    ((t, venue, q, u, via),) = shape(conn, r1)
    assert (t, venue, q, via) == (C50, "delivered", 950, C50_GAS)
    assert u == pytest.approx(600_000 / 950)
    assert buying.refined_ores(conn, r1)[("transaction", 1)][0].remainder == 0


def test_a_contract_kit_with_two_ores_pools_each_ore_and_keeps_the_order(conn, ref):
    """Items: Scordite 60, Tritanium 1,000, Scordite 60, Veldspar 150 at
    Jita 4-4, no ore chosen by the plan, run hub rate 500 ISK/m³, no plan
    rows (the raws price from the cache, landed at the hub rate). The two
    Scordite items pool to 120 → one batch (they share k × p_i); the
    lines go out at each ore's first item: its raws by type id, then the
    remainder."""
    r1 = add_run(conn, 1)
    set_rates(conn, r1, hub=500.0, structure=0.0, other=0.0)
    quote(conn, SCORDITE, 300.0)
    quote(conn, VELDSPAR, 200.0)
    quote(conn, TRIT, 4.0)
    quote(conn, PYE, 9.0)
    add_contract(conn, 40, [item(1, SCORDITE, 60), item(2, TRIT, 1000),
                            item(3, SCORDITE, 60), item(4, VELDSPAR, 150)],
                 price=100_000.0, title="ore kit")
    summary = assign_ref(conn, ref)
    k = 100_000.0 / (120 * 300.0 + 1000 * 4.0 + 150 * 200.0)
    p_trit, p_pye = 4.0 + 500 * 0.01, 9.0 + 500 * 0.01          # landed: 9.0, 14.0
    # Scordite: 1 batch → Trit floor(135.945) = 135, Pye floor(99.693) = 99.
    scord_isk = k * 300.0 * 100 + 500 * 0.0015 * 100 + 0.04 * (135 * p_trit + 99 * p_pye)
    scord_total = 135 * p_trit + 99 * p_pye
    # Veldspar: 1 batch → Trit floor(362.52) = 362, the only output.
    veld_isk = k * 200.0 * 100 + 500 * 0.001 * 100 + 0.04 * (362 * p_trit)
    got = shape(conn, r1)
    assert [(t, v, q, via) for t, v, q, _u, via in got] == [
        (TRIT, "delivered", 135, SCORDITE),
        (PYE, "delivered", 99, SCORDITE),
        (SCORDITE, "hub", 20, None),
        (TRIT, "hub", 1000, None),
        (TRIT, "delivered", 362, VELDSPAR),
        (VELDSPAR, "hub", 50, None),
    ]
    assert got[0][3] == pytest.approx(scord_isk * 135 * p_trit / scord_total / 135)
    assert got[1][3] == pytest.approx(scord_isk * 99 * p_pye / scord_total / 99)
    assert got[2][3] == pytest.approx(k * 300.0)
    assert got[3][3] == pytest.approx(k * 4.0)
    assert got[4][3] == pytest.approx(veld_isk / 362)
    assert got[5][3] == pytest.approx(k * 200.0)
    assert all(l["contract_k"] == pytest.approx(k) and l["esi_id"] == 40
               for l in lines(conn, r1))
    assert summary.refined == 2 and summary.contracts == 1
    # What was paid is still all there: the converted ore's price moved
    # into the raws, plus freight and tax on top.
    paid = sum(q * u for _t, _v, q, u, via in got if via is None)
    refined_isk = sum(q * u for _t, _v, q, u, via in got if via is not None)
    assert paid + refined_isk == pytest.approx(
        100_000.0 + 500 * 0.0015 * 100 + 500 * 0.001 * 100
        + 0.04 * scord_total + 0.04 * 362 * p_trit)
    ores = buying.refined_ores(conn, r1)[("contract", 40)]
    assert [(o.ore_type_id, o.outputs, o.remainder) for o in ores] == [
        (SCORDITE, ((TRIT, 135), (PYE, 99)), 20), (VELDSPAR, ((TRIT, 362),), 50)]
    (record,) = buying.run_purchase_records(conn, r1)
    assert record.received_units(SCORDITE) == 120


def test_a_plan_chosen_ore_keeps_its_ore_lines(conn, ref):
    """§2.4: the plan's own ore follows the existing path — costing
    re-blends it through compressed_alloc — surplus past the plan's
    quantity included (the existing per-unit cap applies)."""
    r1 = add_run(conn, 1)
    set_rates(conn, r1, hub=1000.0, structure=0.0, other=0.0)
    plan_chose_veldspar(conn, r1)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=VELDSPAR, qty=1000, price=80.0)
    add_buy(conn, 2, "2026-09-02T01:00:00Z", type_id=VELDSPAR, qty=5000, price=80.0)
    summary = assign_ref(conn, ref)
    assert shape(conn, r1) == [(VELDSPAR, "hub", 1000, 80.0, None),
                               (VELDSPAR, "hub", 5000, 80.0, None)]
    assert summary.refined == 0 and buying.refined_ores(conn, r1) == {}


def test_what_never_converts(conn, ref):
    """A5/A6: ice (outputs only Ice Products), a kind whose yield is 0,
    a record short of one batch (the two 40-unit items of one contract
    pool to 80: still short, so both lines stay exactly as bought) and a
    pass without reference data write the ore lines as bought."""
    r1 = add_run(conn, 1)
    set_rates(conn, r1, hub=1000.0, structure=0.0, other=0.0)
    quote(conn, SCORDITE, 300.0)
    quote(conn, TRIT, 4.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=BLUE_ICE, qty=10, price=9000.0)
    add_buy(conn, 2, "2026-09-02T01:00:00Z", type_id=SCORDITE, qty=99, price=300.0)
    add_contract(conn, 3, [item(1, SCORDITE, 40), item(2, TRIT, 10), item(3, SCORDITE, 40)],
                 price=24_040.0, date="2026-09-02T02:00:00Z")
    add_buy(conn, 4, "2026-09-02T03:00:00Z", type_id=C50_GAS, qty=10, price=500.0)
    set_settings(conn, compressed_gas_yield=0.0)
    summary = assign_ref(conn, ref)
    assert [(t, v, q, via) for t, v, q, _u, via in shape(conn, r1)] == [
        (BLUE_ICE, "hub", 10, None),
        (SCORDITE, "hub", 99, None),
        (SCORDITE, "hub", 40, None), (TRIT, "hub", 10, None), (SCORDITE, "hub", 40, None),
        (C50_GAS, "hub", 10, None),
    ]
    assert summary.refined == 0
    # With a yield the gas converts; without ref nothing does.
    set_settings(conn, compressed_gas_yield=0.95)
    assign_ref(conn, ref)
    assert (C50, "delivered", 9, C50_GAS) in [(t, v, q, via) for t, v, q, _u, via in shape(conn, r1)]
    add_buy(conn, 5, "2026-09-02T04:00:00Z", type_id=SCORDITE, qty=500, price=300.0)
    assign(conn)   # ref None (every revision-3 helper): no conversion at all
    assert all(via is None for *_rest, via in shape(conn, r1))
    assert (SCORDITE, "hub", 500, 300.0, None) in shape(conn, r1)


def test_ore_freight_follows_the_purchase_venue_at_the_runs_rates(conn, ref):
    """§2b: the ore hauls at its purchase venue's leg — the structure
    market at the structure rate, anywhere else at the default rate —
    each at the rate the run was PLANNED at, the live setting only where
    the run predates the column. One output (Veldspar → Trit), so the
    whole landed ISK lands on it: 100 × price + rate × 0.001 × 100 +
    0.04 × 362 × p_Trit."""
    set_settings(conn, freight_in_default_isk_per_m3=300.0,
                 structure_freight_in_isk_per_m3=700.0)
    r1 = add_run(conn, 1)
    set_rates(conn, r1, hub=1000.0, structure=250.0, other=2000.0)
    add_item(conn, r1, TRIT, recommended_buy_qty=0, effective_unit_cost=4.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=VELDSPAR, qty=100, price=50.0,
            location=AMARR)
    add_buy(conn, 2, "2026-09-02T01:00:00Z", type_id=VELDSPAR, qty=100, price=50.0,
            location=CJ6)
    add_buy(conn, 3, "2026-09-02T02:00:00Z", type_id=VELDSPAR, qty=100, price=50.0,
            location=SOTIYO)
    assign_ref(conn, ref)
    tax = 0.04 * 362 * 4.0
    unit = {l["esi_id"]: l["unit_price"] for l in lines(conn, r1)}
    assert unit[1] == pytest.approx((5000 + 2000 * 0.1 + tax) / 362)   # other: the run's 2,000
    assert unit[2] == pytest.approx((5000 + 250 * 0.1 + tax) / 362)    # structure: the run's 250
    assert unit[3] == pytest.approx((5000 + 2000 * 0.1 + tax) / 362)   # another structure
    # A run planned before the default-rate column: the live setting.
    set_rates(conn, r1, hub=1000.0, structure=250.0, other=None)
    assign_ref(conn, ref)
    unit = {l["esi_id"]: l["unit_price"] for l in lines(conn, r1)}
    assert unit[1] == pytest.approx((5000 + 300 * 0.1 + tax) / 362)


def test_raw_prices_fall_back_landed_then_split_by_units(conn, ref):
    """A7's p_m chain, every step landed at the run's hub rate (100):
    a plan row priced by its blend (price_snapshot 3.0 at the hub → 3.0 +
    100 × 0.01 = 4.0), no row → the cached hub SELL quote (9.0 + 1.0 =
    10.0); then with no price anywhere the ISK is split by units and no
    tax is due (it is a share of zero value)."""
    r1 = add_run(conn, 1)
    set_rates(conn, r1, hub=100.0, structure=0.0, other=0.0)
    add_item(conn, r1, TRIT, recommended_buy_qty=1000, price_snapshot=3.0, buy_venue="hub")
    quote(conn, PYE, 9.0)
    quote(conn, PYE, 99.0, hub=0, region=10000043)   # another region: never read
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=SCORDITE, qty=200, price=100.0)
    assign_ref(conn, ref)
    isk = 100 * 200 + 100 * 0.0015 * 200 + 0.04 * (271 * 4.0 + 199 * 10.0)
    got = {t: u for t, _v, _q, u, _via in shape(conn, r1)}
    assert got[TRIT] == pytest.approx(isk * 271 * 4.0 / 3074 / 271)
    assert got[PYE] == pytest.approx(isk * 199 * 10.0 / 3074 / 199)
    # The adjusted price stands in for a missing sell quote, landed too.
    r2 = add_run(conn, 2, planned_start="2026-09-03 00:00:00")
    set_rates(conn, r2, hub=100.0, structure=0.0, other=0.0)
    conn.execute("DELETE FROM market_price")
    adjusted(conn, TRIT, 3.0)
    add_buy(conn, 2, "2026-09-04T00:00:00Z", type_id=SCORDITE, qty=200, price=100.0)
    assign_ref(conn, ref)
    isk = 100 * 200 + 100 * 0.0015 * 200 + 0.04 * (271 * 4.0)
    got = {t: u for t, _v, _q, u, _via in shape(conn, r2)}
    assert got[TRIT] == pytest.approx(isk / 271)      # Pye unpriced: its share is 0
    assert got[PYE] == 0.0
    # Nothing priced at all: by units, untaxed.
    conn.execute("DELETE FROM market_price")
    conn.commit()
    assign_ref(conn, ref)
    got = {t: u for t, _v, _q, u, _via in shape(conn, r2)}
    assert got[TRIT] == got[PYE] == pytest.approx((20_000 + 30.0) / (271 + 199))


def test_the_conversion_is_idempotent(conn, ref):
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 01:00:00")
    for run in (r1, r2):
        set_rates(conn, run, hub=1000.0, structure=0.0, other=0.0)
        plan_chose_veldspar(conn, run)
    quote(conn, SCORDITE, 100.0)
    quote(conn, TRIT, 4.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=SCORDITE, qty=250, price=100.0)
    add_buy(conn, 2, "2026-09-06T00:00:00Z", type_id=SCORDITE, qty=250, price=100.0)
    add_contract(conn, 3, [item(1, SCORDITE, 330), item(2, TRIT, 10)],
                 date="2026-09-06T01:00:00Z", price=40_000.0)
    first = assign_ref(conn, ref)
    dump = lambda: [tuple(r) for r in conn.execute("SELECT * FROM run_purchase ORDER BY 1")]
    before = dump()
    second = assign_ref(conn, ref)
    assert dump() == before           # purchase ids included: nothing rewritten
    assert first.changed_runs == 2 and second.changed_runs == 0
    assert first.refined == second.refined == 3
    # The no-change check sees via_type_id: a line that lost it is rewritten.
    conn.execute("UPDATE run_purchase SET via_type_id = NULL WHERE index_run_id = ? "
                 "AND via_type_id IS NOT NULL AND type_id = ?", (r2, TRIT))
    conn.commit()
    third = assign_ref(conn, ref)
    assert third.changed_runs == 1
    assert [via for t, v, _q, _u, via in shape(conn, r2) if t == TRIT and v == "delivered"] == [
        SCORDITE, SCORDITE]
    assert assign_ref(conn, ref).changed_runs == 0


def test_an_executed_runs_conversion_is_frozen(conn, ref):
    """A9: after execution a settings change (yield, tax, rates) or a
    price move never reprices the executed run's refined lines — only
    the newest planned run recomputes. A zero yield later does not undo
    them either."""
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 01:00:00")
    for run in (r1, r2):
        set_rates(conn, run, hub=1000.0, structure=0.0, other=0.0)
        plan_chose_veldspar(conn, run)
    quote(conn, SCORDITE, 100.0)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=SCORDITE, qty=250, price=100.0)
    add_contract(conn, 2, [item(1, SCORDITE, 200)], date="2026-09-03T00:00:00Z",
                 price=30_000.0)                                    # frozen on priced_at
    add_buy(conn, 3, "2026-09-06T00:00:00Z", type_id=SCORDITE, qty=250, price=100.0)
    assign_ref(conn, ref)
    executed = shape(conn, r1)
    assert [(t, q) for t, _v, q, _u, _via in executed] == [
        (TRIT, 271), (PYE, 199), (SCORDITE, 50), (TRIT, 271), (PYE, 199)]
    # Everything the conversion reads moves.
    set_settings(conn, compressed_ore_yield=0.5, compressed_reprocess_tax=0.10)
    conn.execute("UPDATE index_run_item SET effective_unit_cost = effective_unit_cost * 3")
    conn.execute("UPDATE index_run SET freight_in_isk_per_m3 = 5.0")
    conn.commit()
    summary = assign_ref(conn, ref)
    assert shape(conn, r1) == executed
    assert summary.changed_runs == 1                  # the newest planned run only
    newest = shape(conn, r2)
    assert [(t, q) for t, _v, q, _u, _via in newest] == [
        (TRIT, 150), (PYE, 110), (SCORDITE, 50)]      # floor(2 × 150 × 0.5) etc.
    isk = 100 * 200 + 5.0 * 0.0015 * 200 + 0.10 * (150 * 12.0 + 110 * 30.0)
    assert newest[0][3] == pytest.approx(isk * 150 * 12.0 / (150 * 12.0 + 110 * 30.0) / 150)
    # A zero yield stops new conversions, never the frozen ones.
    set_settings(conn, compressed_ore_yield=0.0)
    assign_ref(conn, ref)
    assert shape(conn, r1) == executed
    assert shape(conn, r2) == [(SCORDITE, "hub", 250, 100.0, None)]
    # A late pull into the executed window converts fresh, at today's
    # settings, beside the frozen records.
    set_settings(conn, compressed_ore_yield=0.9063, compressed_reprocess_tax=0.04)
    add_buy(conn, 4, "2026-09-04T00:00:00Z", type_id=SCORDITE, qty=100, price=100.0)
    assign_ref(conn, ref)
    assert shape(conn, r1)[:5] == executed
    assert [(t, q, via) for t, _v, q, _u, via in shape(conn, r1)[5:]] == [
        (TRIT, 135, SCORDITE), (PYE, 99, SCORDITE)]


def test_a_contract_still_repricing_rederives_its_minerals_on_an_executed_run(conn, ref):
    """A9: a contract's refined lines freeze only while its stored
    contract_k equals the current k. One with an unpriced item re-derives
    k on every pass (priced_at NULL); when the cache fills k moves, and
    the executed run's minerals follow it."""
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    add_run(conn, 2, "planned", "2026-09-05 01:00:00")
    set_rates(conn, r1, hub=0.0, structure=0.0, other=0.0)
    add_item(conn, r1, TRIT, effective_unit_cost=4.0)
    add_item(conn, r1, PYE, effective_unit_cost=10.0)
    quote(conn, SCORDITE, 100.0)
    add_contract(conn, 5, [item(1, SCORDITE, 200), item(2, MEX, 100)],
                 date="2026-09-03T00:00:00Z", price=30_000.0)
    assign_ref(conn, ref)
    first = shape(conn, r1)
    assert conn.execute("SELECT priced_at FROM buy_contract").fetchone()[0] is None
    # k = 30,000 / (200 × 100): Scordite carries everything, 150 / unit.
    assert sum(q * u for t, _v, q, u, _via in first if t in (TRIT, PYE)) == pytest.approx(
        150.0 * 200 + 0.04 * (271 * 4.0 + 199 * 10.0))
    adjusted(conn, MEX, 200.0)                          # the cache fills: k = 0.75
    assign_ref(conn, ref)
    second = shape(conn, r1)
    assert sum(q * u for t, _v, q, u, _via in second if t in (TRIT, PYE)) == pytest.approx(
        75.0 * 200 + 0.04 * (271 * 4.0 + 199 * 10.0))
    assert conn.execute("SELECT priced_at FROM buy_contract").fetchone()[0] is not None
    # Frozen now: a price move no longer touches it.
    adjusted(conn, MEX, 1.0)
    conn.execute("UPDATE index_run_item SET effective_unit_cost = 1.0")
    conn.commit()
    assign_ref(conn, ref)
    assert shape(conn, r1) == second


# --- review 2026-09-28: the freeze outlives a record that stops counting, ---
# --- and an executed run keeps the structure market it was planned against -


def _refine_rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT * FROM run_purchase_refine ORDER BY index_run_id, esi_kind, esi_id, "
        "via_type_id, type_id")]


def _frozen_scordite_run(conn, ref):
    """r1 executed holding 250 Compressed Scordite bought at Jita 4-4 (two
    batches refined at yield 0.9063, tax 4 %), r2 the newest planned run."""
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r2 = add_run(conn, 2, "planned", "2026-09-05 01:00:00")
    for run in (r1, r2):
        set_rates(conn, run, hub=1000.0, structure=0.0, other=0.0)
        plan_chose_veldspar(conn, run)
    add_buy(conn, 1, "2026-09-02T00:00:00Z", type_id=SCORDITE, qty=250, price=100.0)
    add_buy(conn, 2, "2026-09-02T01:00:00Z", type_id=TRIT, qty=1000, price=5.0)
    assign_ref(conn, ref)
    return r1, r2


@pytest.mark.parametrize("round_trip", ["count_buys", "character"])
def test_an_executed_runs_conversion_survives_a_record_that_stops_counting(
    conn, ref, round_trip,
):
    """Review 2026-09-28 (P2): the freeze read only the run's stored via
    lines, so a pass that stopped costing the record (Count buys off, or
    the character removed) dropped them, and switching it back on
    re-converted the executed run at the CURRENT yield, tax and prices
    (Trit 150 @ 48.16 / Pye 110 @ 120.41 instead of 271 @ 26.575 / 199 @
    66.438). run_purchase_refine keeps the conversion apart from the
    lines, so the round trip restores it byte for byte."""
    r1, _r2 = _frozen_scordite_run(conn, ref)
    executed = shape(conn, r1)
    assert [(t, q, via) for t, _v, q, _u, via in executed] == [
        (TRIT, 271, SCORDITE), (PYE, 199, SCORDITE), (SCORDITE, 50, None),
        (TRIT, 1000, None)]
    assert executed[0][3] == pytest.approx(26.575094, abs=1e-6)
    frozen = _refine_rows(conn)
    assert [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in frozen] == [
        (r1, "transaction", 1, SCORDITE, TRIT, 271),
        (r1, "transaction", 1, SCORDITE, PYE, 199)]
    # Everything a fresh conversion would read moves ...
    set_settings(conn, compressed_ore_yield=0.5, compressed_reprocess_tax=0.10)
    # ... and the record stops counting for one pass.
    if round_trip == "count_buys":
        conn.execute("UPDATE pool_character SET count_buys = 0")
    else:
        conn.execute("DELETE FROM pool_character WHERE character_id = ?", (A,))
    conn.commit()
    assign_ref(conn, ref)
    assert lines(conn, r1) == []
    assert _refine_rows(conn) == frozen            # kept while not costed
    if round_trip == "count_buys":
        conn.execute("UPDATE pool_character SET count_buys = 1")
    else:
        conn.execute(
            "INSERT INTO pool_character (character_id, character_name, include_assets, "
            "include_job_slots, count_assets, count_sales) VALUES (?, 'main', 0, 0, 0, 1)",
            (A,),
        )
    conn.commit()
    assign_ref(conn, ref)
    assert shape(conn, r1) == executed
    assert _refine_rows(conn) == frozen
    # And a repeat pass writes nothing at all.
    before = conn.total_changes
    assert assign_ref(conn, ref).changed_runs == 0
    assert conn.total_changes == before


def test_a_reopen_drops_the_frozen_conversion_and_a_run_delete_clears_it(conn, ref):
    """Only an executed run keeps frozen conversions: reopening r1 behind
    the newer r2 makes it superseded — its lines and its frozen rows go —
    so re-executing it converts fresh at the settings of that day (A9's
    "a reopen that moved the record"). delete_run_purchases clears a run's
    rows, so run_delete's FK order still works."""
    r1, r2 = _frozen_scordite_run(conn, ref)
    conn.execute("UPDATE index_run SET status = 'planned' WHERE index_run_id = ?", (r1,))
    conn.commit()
    assign_ref(conn, ref)
    assert _refine_rows(conn) == []
    set_settings(conn, compressed_ore_yield=0.5, compressed_reprocess_tax=0.10)
    conn.execute("UPDATE index_run SET status = 'complete' WHERE index_run_id = ?", (r1,))
    conn.commit()
    assign_ref(conn, ref)
    assert [(t, q) for t, _v, q, _u, via in shape(conn, r1) if via] == [
        (TRIT, 150), (PYE, 110)]
    assert {r[0] for r in _refine_rows(conn)} == {r1}
    # A moved bound takes the record off r1 (onto the open r2): r1's
    # frozen group goes with it, so moving it back converts fresh.
    conn.execute("UPDATE index_run SET completed_at = '2026-09-01 12:00:00' "
                 "WHERE index_run_id = ?", (r1,))
    conn.commit()
    assign_ref(conn, ref)
    assert on_run(conn, r2) == [("transaction", 1), ("transaction", 2)]
    assert _refine_rows(conn) == []
    conn.execute("UPDATE index_run SET completed_at = '2026-09-05 00:00:00' "
                 "WHERE index_run_id = ?", (r1,))
    conn.commit()
    assign_ref(conn, ref)
    assert {r[0] for r in _refine_rows(conn)} == {r1}
    store.delete_run_purchases(conn, r1)
    conn.execute("DELETE FROM index_run_item WHERE index_run_id = ?", (r1,))
    conn.execute("DELETE FROM index_run WHERE index_run_id = ?", (r1,))  # FK ON
    conn.commit()
    assert _refine_rows(conn) == []
    assert r2


def test_an_executed_run_keeps_the_structure_market_it_was_planned_against(conn):
    """Review 2026-09-28 (P2): pointing Settings at another structure used
    to reclass every executed run's buys at the old one to 'other' (the
    default rate, out of the Null Sec Market Share). An executed run
    classes its buys against index_run.structure_market_id; the open run
    follows the live setting; a run planned before the column falls back
    to the live setting too."""
    r1 = add_run(conn, 1, "complete", "2026-09-01 00:00:00", "2026-09-05 00:00:00")
    r0 = add_run(conn, 3, "complete", "2026-09-05 00:30:00", "2026-09-05 00:40:00")
    r2 = add_run(conn, 4, "planned", "2026-09-05 01:00:00")
    conn.execute("UPDATE index_run SET structure_market_id = ? WHERE index_run_id = ?",
                 (CJ6, r1))
    conn.commit()
    add_buy(conn, 1, "2026-09-02T00:00:00Z", location=CJ6)
    add_buy(conn, 2, "2026-09-05T00:35:00Z", location=CJ6)          # r0: no vintage
    add_buy(conn, 3, "2026-09-06T00:00:00Z", location=CJ6)
    add_contract(conn, 4, [item(1)], date="2026-09-03T00:00:00Z", location=CJ6)
    quote(conn, TRIT, 5.0)
    assign(conn)
    assert {l["venue"] for run in (r1, r0, r2) for l in lines(conn, run)} == {"structure"}
    set_settings(conn, capital_market_mode="custom", capital_structure_id=SOTIYO)
    summary = assign(conn)
    assert [l["venue"] for l in lines(conn, r1)] == ["structure", "structure"]
    assert [l["venue"] for l in lines(conn, r0)] == ["other"]      # live fallback
    assert [l["venue"] for l in lines(conn, r2)] == ["other"]      # the open run
    assert summary.changed_runs == 2
    venues = {r.esi_id: r.venue for r in buying.run_purchase_records(conn, r1)}
    assert venues == {1: "structure", 4: "structure"}
    # A buy at the NEW structure is 'other' on the executed run, which was
    # planned against C-J6, and 'structure' on the open one.
    assert buying.purchase_venue(SOTIYO, store.get_settings(conn), CJ6) == "other"
    assert buying.purchase_venue(SOTIYO, store.get_settings(conn)) == "structure"
