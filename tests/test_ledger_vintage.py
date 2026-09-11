"""Per-sale-date cost vintages (v1.27.1, user ruling 2026-09-09): every
Ledger sale is costed at the latest run executed on or before it, so
executing a new run never re-costs an earlier sale; a sale before the
first executed run takes that run (pre-history); the unsold listings and
the products table's cost-per-unit column read the latest executed run.

Builders and fixtures are test_ledger's (imported, so pytest sees the
`conn` fixture); hull_cost is faked per run so vintages are visible.
"""

import pytest

from magoo import ledger, store
from test_ledger import (  # noqa: F401 — conn is a fixture
    A, BUYER, HULK, NOW_DT, FakeCost, add_owner, add_pipeline, conn, seed_contract,
    seed_run, seed_tx,
)


def _fake_costs_by_run(monkeypatch, costs: dict):
    asked = []

    def fake(conn, ref, settings, index_run_id, pipeline_id):
        asked.append(index_run_id)
        return FakeCost(costs[index_run_id])

    monkeypatch.setattr(ledger.costing, "hull_cost", fake)
    return asked


def _view(conn, ref, now=NOW_DT):
    return ledger.build_view(conn, ref, store.get_settings(conn), "all", now=now)


def test_each_sale_is_costed_at_the_latest_run_executed_before_it(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r2 = seed_run(conn, 2, pid)  # executed 2026-09-02
    r5 = seed_run(conn, 5, pid)  # executed 2026-09-05
    asked = _fake_costs_by_run(monkeypatch, {r2: 100e6, r5: 130e6})
    seed_tx(conn, 1, date="2026-09-01T10:00:00Z")  # before run 2: pre-history, costed at run 2
    seed_tx(conn, 2, date="2026-09-03T10:00:00Z")  # run 2
    seed_tx(conn, 3, date="2026-09-06T10:00:00Z")  # run 5
    view = _view(conn, ref)
    by_id = {s.ref_id: s for s in view["sales"]}
    assert (by_id[1].basis_run, by_id[1].pre_history, by_id[1].unit_cost) == (2, True, 100e6)
    assert (by_id[2].basis_run, by_id[2].pre_history, by_id[2].unit_cost) == (2, False, 100e6)
    assert (by_id[3].basis_run, by_id[3].pre_history, by_id[3].unit_cost) == (5, False, 130e6)
    for s in by_id.values():
        assert s.profit == pytest.approx(s.net - s.quantity * s.unit_cost)
    p = next(p for p in view["products"] if p.type_id == HULK)
    # The products table: cost per unit NOW is the latest run's; cost of
    # goods sold is the sum of each sale's own vintage.
    assert p.basis.run_number == 5 and p.unit_cost == 130e6
    assert p.cost_units == 3 and p.cogs == pytest.approx(330e6)
    assert p.cost_of_units == pytest.approx(330e6)
    assert p.avg_unit_cost == pytest.approx(110e6)
    assert p.runs_used == {2, 5} and p.runs_label == "runs 2, 5"
    assert p.pre_history_units == 1
    assert any(b[0] == "pre-history" for b in p.badges)
    assert p.profit == pytest.approx(p.net - 330e6)
    assert view["totals"].cost == pytest.approx(330e6)
    assert view["totals"].profit == pytest.approx(view["totals"].net_basis - 330e6)
    # Only the two runs the window's sales (and the current basis) needed
    # were costed, each once.
    assert sorted(set(asked)) == [r2, r5] and len(asked) == 2


def test_executing_a_new_run_does_not_recost_earlier_sales(conn, ref, monkeypatch):
    """The user's question of 2026-09-09: a sale keeps the basis it had on
    the day; a later run changes only what is still unsold and the
    cost-per-unit column."""
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r2 = seed_run(conn, 2, pid)
    r5 = seed_run(conn, 5, pid)
    costs = {r2: 100e6, r5: 130e6}
    _fake_costs_by_run(monkeypatch, costs)
    seed_tx(conn, 2, date="2026-09-03T10:00:00Z")
    seed_tx(conn, 3, date="2026-09-06T10:00:00Z")
    before = {s.ref_id: (s.profit, s.basis_run) for s in _view(conn, ref)["sales"]}
    r7 = seed_run(conn, 7, pid)  # executed 2026-09-07T00:00Z, after both sales
    costs[r7] = 200e6
    view = _view(conn, ref)
    after = {s.ref_id: (s.profit, s.basis_run) for s in view["sales"]}
    assert after == before
    p = next(p for p in view["products"] if p.type_id == HULK)
    assert p.basis.run_number == 7 and p.unit_cost == 200e6  # the current basis moved
    assert p.cogs == pytest.approx(230e6) and p.runs_used == {2, 5}  # the sales did not
    # A sale after run 7 takes run 7.
    seed_tx(conn, 4, date="2026-09-07T06:00:00Z")
    s4 = next(s for s in _view(conn, ref)["sales"] if s.ref_id == 4)
    assert (s4.basis_run, s4.unit_cost) == (7, 200e6)


def test_contract_sales_take_their_completion_date_inclusive(conn, ref, monkeypatch):
    """A contract completed at the very moment a run was executed takes
    that run ('on or before'); one completed earlier takes the run
    before."""
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r2 = seed_run(conn, 2, pid)
    r5 = seed_run(conn, 5, pid)
    _fake_costs_by_run(monkeypatch, {r2: 100e6, r5: 130e6})
    hull = {"record_id": 1, "type_id": HULK, "quantity": 1, "raw_quantity": 1,
            "is_included": True, "is_singleton": False}
    seed_contract(conn, 501, [dict(hull)], date_completed="2026-09-05T00:00:00Z")  # == run 5's stamp
    seed_contract(conn, 502, [dict(hull, record_id=2)], date_completed="2026-09-04T23:59:59Z")
    view = _view(conn, ref)
    by_id = {s.ref_id: s for s in view["sales"]}
    assert by_id[501].basis_run == 5 and by_id[501].unit_cost == 130e6
    assert by_id[502].basis_run == 2 and by_id[502].unit_cost == 100e6


def test_no_executed_run_means_no_basis_for_any_sale(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid = add_pipeline(conn)
    seed_run(conn, 3, pid, status="planned")  # never executed
    _fake_costs_by_run(monkeypatch, {})
    seed_tx(conn, 1, date="2026-09-04T10:00:00Z")
    view = _view(conn, ref)
    s = view["sales"][0]
    assert s.basis_run is None and s.unit_cost is None and s.profit is None
    p = next(p for p in view["products"] if p.type_id == HULK)
    assert p.basis is None and p.cost_of_units is None and p.profit is None
    assert view["totals"].no_basis == 1
    assert any(b[0] == "no executed run" for b in p.badges)


def test_latest_matches_cost_bases_and_sales_before_history_take_the_earliest(conn, ref, monkeypatch):
    """CostVintages.latest is cost_bases' rule (newest run number, tie →
    lowest pipeline id); at() before every executed run picks the
    earliest by execution time."""
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r3 = seed_run(conn, 3, pid)
    r4 = seed_run(conn, 4, pid)
    _fake_costs_by_run(monkeypatch, {r3: 90e6, r4: 95e6})
    settings = store.get_settings(conn)
    finals_map = ledger.finals(conn)
    vintages = ledger.CostVintages(conn, ref, settings, finals_map)
    assert vintages.latest(HULK).index_run_id == ledger.cost_bases(conn, ref, settings, finals_map)[HULK].index_run_id == r4
    basis, pre = vintages.at(HULK, "2026-09-01T00:00:00Z")
    assert basis.index_run_id == r3 and pre is True
    basis, pre = vintages.at(HULK, "2026-09-03T00:00:00Z")
    assert basis.index_run_id == r3 and pre is False
    basis, pre = vintages.at(HULK, "2026-09-09T00:00:00Z")
    assert basis.index_run_id == r4 and pre is False
    assert vintages.at(999999, "2026-09-09T00:00:00Z") == (None, False)


def test_an_unpriced_run_is_passed_over_for_the_priced_one_before_it(conn, ref, monkeypatch):
    """Review 2026-09-09: a run planned before any price pull (every
    cost line unpriced, total 0) sets no basis — a sale after it is
    costed at the priced run before it, a sale before every priced run
    at the earliest priced one; COGS stays complete, never partial."""
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r2 = seed_run(conn, 2, pid)  # priced
    r4 = seed_run(conn, 4, pid)  # unpriced (total 0)
    r6 = seed_run(conn, 6, pid)  # unpriced, the latest
    _fake_costs_by_run(monkeypatch, {r2: 100e6, r4: 0.0, r6: 0.0})
    seed_tx(conn, 1, date="2026-09-01T00:00:00Z")  # before run 2 -> run 2, pre-history
    seed_tx(conn, 2, date="2026-09-05T00:00:00Z")  # after run 4 -> run 2 (run 4 sets no basis)
    seed_tx(conn, 3, date="2026-09-06T12:00:00Z")  # after run 6 -> run 2
    view = _view(conn, ref)
    for s in view["sales"]:
        assert s.basis_run == 2 and s.unit_cost == 100e6 and s.profit is not None
    p = next(p for p in view["products"] if p.type_id == HULK)
    assert p.cost_units == p.units_priced == 3
    assert p.cost_of_units == pytest.approx(300e6)
    assert p.profit == pytest.approx(p.net - 300e6)
    # The current basis (run 6) prices nothing: the row says so, and the
    # totals keep the product's net income (it has a basis for every sale).
    assert p.basis.run_number == 6 and p.unit_cost is None
    assert any(b[0] == "latest unpriced" for b in p.badges)
    assert view["totals"].cost == pytest.approx(300e6) and view["totals"].no_basis == 0


def test_vintages_read_the_sqlite_completed_at_shape_and_order_by_it(conn, ref, monkeypatch):
    """The app stamps completed_at as sqlite datetime('now') —
    'YYYY-MM-DD HH:MM:SS', naive, UTC — not the ...Z the builders use;
    and the vintage is the run executed latest by that stamp, not by
    run number, so a legacy out-of-order execution still costs a sale at
    the run that really was the latest on the day."""
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r3 = seed_run(conn, 3, pid)
    r4 = seed_run(conn, 4, pid)
    conn.execute("UPDATE index_run SET completed_at = '2026-09-04 12:00:00' WHERE index_run_id = ?", (r3,))
    conn.execute("UPDATE index_run SET completed_at = '2026-09-02 08:30:00' WHERE index_run_id = ?", (r4,))  # run 4 executed BEFORE run 3
    conn.commit()
    _fake_costs_by_run(monkeypatch, {r3: 100e6, r4: 110e6})
    seed_tx(conn, 1, date="2026-09-03T00:00:00Z")  # only run 4 is executed by then
    seed_tx(conn, 2, date="2026-09-04T12:00:00Z")  # exactly run 3's stamp -> run 3
    seed_tx(conn, 3, date="2026-09-04T11:59:59Z")  # a second before -> run 4
    by_id = {s.ref_id: s for s in _view(conn, ref)["sales"]}
    assert by_id[1].basis_run == 4 and by_id[1].unit_cost == 110e6
    assert by_id[2].basis_run == 3 and by_id[2].unit_cost == 100e6
    assert by_id[3].basis_run == 4
    # latest() keeps the run-number rule the products table and the
    # unsold listings have always used.
    settings = store.get_settings(conn)
    assert ledger.CostVintages(conn, ref, settings, ledger.finals(conn)).latest(HULK).run_number == 4


def test_pre_history_wording_names_the_run_the_sale_was_costed_at(conn, ref, monkeypatch):
    """Review 2026-09-10: the pre-history badge named the lowest run
    NUMBER while the sale was costed at the earliest run by EXECUTION
    time; and a sale after an unpriced run but before the first priced
    one is not 'before your first executed run' — the texts say what
    happened: no priced run had been executed by the sale, so it takes
    the earliest priced run."""
    add_owner(conn, A)
    pid = add_pipeline(conn)
    r2 = seed_run(conn, 2, pid)  # unpriced, executed 2026-09-02
    r3 = seed_run(conn, 3, pid)
    r4 = seed_run(conn, 4, pid)
    conn.execute("UPDATE index_run SET completed_at = '2026-09-06 12:00:00' WHERE index_run_id = ?", (r3,))
    conn.execute("UPDATE index_run SET completed_at = '2026-09-04 08:00:00' WHERE index_run_id = ?", (r4,))  # the earliest PRICED run
    conn.commit()
    _fake_costs_by_run(monkeypatch, {r2: 0.0, r3: 100e6, r4: 110e6})
    seed_tx(conn, 1, date="2026-09-01T00:00:00Z")  # before every run
    seed_tx(conn, 2, date="2026-09-03T00:00:00Z")  # after run 2 (unpriced), before run 4
    view = _view(conn, ref)
    by_id = {s.ref_id: s for s in view["sales"]}
    for s in (by_id[1], by_id[2]):
        assert (s.basis_run, s.pre_history, s.unit_cost) == (4, True, 110e6)
    p = next(p for p in view["products"] if p.type_id == HULK)
    assert p.pre_history_units == 2 and p.pre_history_runs == {4}
    badge = next(b for b in p.badges if b[0] == "pre-history")
    assert "run 4" in badge[2] and "run 3" not in badge[2]
    assert "before any priced run" in badge[2]


def test_pre_history_wording_on_the_page(seeded_client, monkeypatch):
    """The rendered page: a sale after an unpriced run and before the
    first priced one is costed at that priced run and worded as such —
    never as 'predating your first executed run'."""
    import sqlite3

    from magoo import config

    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    add_owner(c, A)
    pid = c.execute("SELECT pipeline_id FROM pipeline ORDER BY pipeline_id").fetchone()[0]
    r2 = seed_run(c, 2, pid)  # unpriced
    r5 = seed_run(c, 5, pid)

    class PageCost(FakeCost):  # the page's cost tooltip reads subtotals too
        def subtotal(self, kind):
            return {"material": self.total * 0.8, "install": self.total * 0.1}.get(kind, 0.0)

    costs = {r2: 0.0, r5: 130e6}
    monkeypatch.setattr(ledger.costing, "hull_cost", lambda conn, ref, s, r, p: PageCost(costs[r]))
    seed_tx(c, 1, date="2026-09-03T00:00:00Z")  # after run 2, before run 5
    for family in ("orders", "transactions", "contracts"):  # the page shows sales only once pulled
        c.execute(
            "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, via_character_id, rows, "
            "pulled_at) VALUES ('character', ?, ?, 0, 'ok', ?, 1, '2026-09-09T00:00:00Z')", (A, family, A),
        )
    c.commit()
    c.close()
    html = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    assert "Hulk" in html
    assert "the cost basis of run 5, the earliest priced run — no priced run had been executed by this sale" in html
    assert "before any priced run had been executed" in html  # products tooltip and badge
    # Whitespace-insensitive: the explainer wraps mid-sentence, so a plain
    # substring check missed the stale wording living there (review
    # 2026-09-10).
    flat = " ".join(html.split())
    for stale in ("predates", "before your first executed run",
                  "sold before your first executed run"):
        assert stale not in flat, stale


def test_pre_history_tie_prefers_the_lowest_pipeline_id(conn, ref, monkeypatch):
    add_owner(conn, A)
    pid_a = add_pipeline(conn, name="alpha")
    pid_b = add_pipeline(conn, name="beta")
    run_id = seed_run(conn, 3, pid_a)
    item_id = conn.execute("SELECT index_run_item_id FROM index_run_item WHERE index_run_id = ?", (run_id,)).fetchone()[0]
    conn.execute("INSERT INTO index_run_item_pipeline (index_run_item_id, pipeline_id, qty_attributable, depth) VALUES (?, ?, 4, 0)", (item_id, pid_b))
    conn.commit()
    costs = {pid_a: 80e6, pid_b: 90e6}
    monkeypatch.setattr(ledger.costing, "hull_cost", lambda conn, ref, s, r, p: FakeCost(costs[p]))
    settings = store.get_settings(conn)
    vintages = ledger.CostVintages(conn, ref, settings, ledger.finals(conn))
    after, pre = vintages.at(HULK, "2026-09-04T00:00:00Z")
    before, pre_before = vintages.at(HULK, "2026-09-01T00:00:00Z")
    assert (after.pipeline_id, pre) == (pid_a, False)
    assert (before.pipeline_id, pre_before) == (pid_a, True)
