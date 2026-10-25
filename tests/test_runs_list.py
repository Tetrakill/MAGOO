"""The Index Runs list (user ruling 2026-10-24): the Buy total is what the
run's OPENING plan asked the user to buy — frozen on the run row by the
engine's INSERT, untouched by the in-place re-plans that shrink the live
sum as bought stock lands — and every row carries revenue and profit per
cycle: an executed run's are its Profit tab's cycle totals, the open run's
the today's-prices estimate Planning → Profit shows (badged est.), a
superseded plan's none.

Engine side with test_costing's fixtures (real reference data, a temp
state DB, uniform prices); page side over conftest's seeded_client.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from magoo import config, engine, store

from conftest import FairValuePrices
from magoo.engine import Snapshot
from test_costing import add_pipeline, uniform_snapshot

TRITANIUM = 34


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


def _figures(conn, run_id):
    return conn.execute(
        "SELECT opening_buy_total_isk, opening_buys_unpriced, "
        "(SELECT SUM(recommended_buy_qty * price_snapshot) FROM index_run_item "
        " WHERE index_run_id = r.index_run_id) AS live_total, "
        "(SELECT SUM(CASE WHEN recommended_buy_qty > 0 AND price_snapshot IS NULL "
        " THEN 1 ELSE 0 END) FROM index_run_item WHERE index_run_id = r.index_run_id) "
        " AS live_unpriced "
        "FROM index_run r WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()


# --- engine: the opening figure --------------------------------------------


def test_a_new_run_freezes_its_opening_buy_total(conn, ref):
    """The INSERT writes the same sum the list computed live until now:
    recommended_buy_qty × price_snapshot over every persisted row, an
    unpriced row counting 0 and counted."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(10.0))
    row = _figures(conn, plan.index_run_id)
    assert row["live_total"] > 0
    assert row["opening_buy_total_isk"] == pytest.approx(row["live_total"])
    assert (row["opening_buys_unpriced"], row["live_unpriced"]) == (0, 0)


def test_the_opening_figure_counts_unpriced_buys(conn, ref):
    add_pipeline(conn, ref, "Hulk", 8)
    prices = FairValuePrices(ref, overrides={TRITANIUM: None})
    snap = Snapshot(
        slots_available={config.ACTIVITY_MANUFACTURING: 500, config.ACTIVITY_REACTION: 500},
        prices=prices, adjusted_prices=prices,
    )
    plan = engine.plan_index_run(conn, ref, snap)
    row = _figures(conn, plan.index_run_id)
    assert row["live_unpriced"] >= 1
    assert row["opening_buys_unpriced"] == row["live_unpriced"]
    assert row["opening_buy_total_isk"] == pytest.approx(row["live_total"])


def test_an_in_place_replan_keeps_the_opening_figure_while_the_live_sum_falls(conn, ref):
    """The cycle's purchases land as stock; the re-plan nets them and the
    live Buy total shrinks — what the run's own pages show. The list's
    figure is the opening one, so it does not move."""
    add_pipeline(conn, ref, "Hulk", 8)
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(10.0))
    before = _figures(conn, plan.index_run_id)
    stocked = uniform_snapshot(10.0)
    stocked.on_hand = {
        t: i.recommended_buy_qty * 10
        for t, i in plan.items.items()
        if i.recommended_buy_qty > 0
    }
    again = engine.plan_index_run(
        conn, ref, stocked, replace_index_run_id=plan.index_run_id
    )
    assert again.index_run_id == plan.index_run_id
    after = _figures(conn, plan.index_run_id)
    assert after["live_total"] < before["live_total"]
    assert after["opening_buy_total_isk"] == before["opening_buy_total_isk"]
    assert after["opening_buys_unpriced"] == before["opening_buys_unpriced"]


def test_the_columns_are_migrations_under_the_unreleased_schema_13(conn):
    assert store.SCHEMA_VERSION == 13
    for col in ("opening_buy_total_isk", "opening_buys_unpriced"):
        assert col not in store.STATE_SCHEMA
        assert any(col in m for m in store._MIGRATIONS)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(index_run)")}
    assert {"opening_buy_total_isk", "opening_buys_unpriced"} <= cols


# --- the page ---------------------------------------------------------------


def _row(html: str, run_number: int) -> str:
    """The <tr> of one run on the list page."""
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.S):
        if re.search(rf'<td class="num">{run_number}</td>', tr):
            return tr
    raise AssertionError(f"run {run_number} not on the page")


def _cells(tr: str) -> list:
    return [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]


def _title_isk(tr: str, index: int) -> str:
    """The leading 'N ISK' of the index-th <td>'s title — the full figure
    the cell abbreviates."""
    tds = re.findall(r"<td([^>]*)>", tr)
    m = re.search(r'title="(-?[\d,]+) ISK', tds[index])
    assert m, tds[index]
    return m.group(1)


def _strip_isk(html: str, label: str) -> str:
    """A totals-strip stat's full ISK (_cycle_totals.html: the label, then
    the value with title="N ISK")."""
    m = re.search(
        rf'<span class="label">{re.escape(label)}</span>\s*'
        r'<span class="value[^"]*"\s*title="(-?[\d,]+) ISK',
        html,
    )
    assert m, label
    return m.group(1)


def _plan(client) -> int:
    resp = client.post("/run")
    assert resp.status_code == 302, resp.status_code
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def test_the_list_shows_the_estimate_on_the_open_run_and_realized_figures_once_executed(seeded_client):
    run_id = _plan(seeded_client)
    html = seeded_client.get("/runs").get_data(as_text=True)
    assert "<th" in html and ">Revenue</th>" in html and ">Profit</th>" in html
    tr = _row(html, 1)
    assert ">est.</span>" in tr
    assert ">latest</span>" not in tr  # the opening figure is on file
    assert "what the plan asked you to buy when this run was first planned" in tr
    cells = _cells(tr)
    # #, Planned, Status, Items, Buy total, Revenue, Profit, actions
    assert cells[2] == "planned" and cells[5] != "—" and cells[6] != "—"
    assert "estimate: net proceeds at today's cached prices" in tr

    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT opening_buy_total_isk FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()
    c.close()
    assert row["opening_buy_total_isk"] and row["opening_buy_total_isk"] > 0

    assert seeded_client.post(f"/runs/{run_id}/complete").status_code == 302
    html = seeded_client.get("/runs").get_data(as_text=True)
    tr = _row(html, 1)
    assert ">est.</span>" not in tr
    assert "the Profit tab's Net proceeds / cycle" in tr
    # No ESI job record covers the cycle: the tooltip says the units are
    # the plan's started figure, as the Profit tab's own note does.
    assert "the hulls the plan started (the ESI job record does not cover this cycle)" in tr
    cells = _cells(tr)
    assert cells[2] == "complete" and cells[5] != "—" and cells[6] != "—"


def test_the_list_falls_back_to_the_live_sum_on_a_run_opened_before_the_columns(seeded_client):
    run_id = _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    c.execute(
        "UPDATE index_run SET opening_buy_total_isk = NULL, opening_buys_unpriced = NULL "
        "WHERE index_run_id = ?",
        (run_id,),
    )
    c.commit()
    live = c.execute(
        "SELECT SUM(recommended_buy_qty * price_snapshot) FROM index_run_item "
        "WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()[0]
    c.close()
    html = seeded_client.get("/runs").get_data(as_text=True)
    tr = _row(html, 1)
    assert ">latest</span>" in tr
    assert "the buy total of this run's last plan" in tr
    assert f"{live:,.0f}" in tr or f"{live:,.2f}" in tr


def test_a_superseded_plan_shows_no_figures(seeded_client):
    first = _plan(seeded_client)
    assert seeded_client.post(f"/runs/{first}/complete").status_code == 302
    second = _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    # A bare newer plan makes run 2 superseded (derived, never stored).
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, status) "
        "VALUES (3, datetime('now'), 'planned')"
    )
    c.commit()
    c.close()
    html = seeded_client.get("/runs?all=1").get_data(as_text=True)
    tr = _row(html, 2)
    assert ">superseded</span>" in tr
    cells = _cells(tr)
    assert cells[5] == "—" and cells[6] == "—"
    assert "a superseded plan has no figures" in tr
    # Run 1 keeps its realized figures beside it; the bare run 3 (no
    # items, no pipelines costed) still renders.
    assert "the Profit tab's Net proceeds / cycle" in _row(html, 1)
    _row(html, 3)
    assert second != first


def test_the_lists_figures_agree_with_planning_and_the_profit_tab_to_the_isk(seeded_client, ref):
    """The decision-log claim: the list, the Profit tab and Planning →
    Profit agree to the ISK. The open run against Planning's strip; the
    executed run — with 8 Hulk jobs on the ESI record inside its cycle,
    so the figures are real, not 0 / 0 — against its Profit tab's strip,
    the tooltip naming the ESI record as the units' source."""
    run_id = _plan(seeded_client)
    tr = _row(seeded_client.get("/runs").get_data(as_text=True), 1)
    planning = seeded_client.get("/planning").get_data(as_text=True)
    assert _title_isk(tr, 5) == _strip_isk(planning, "Net proceeds / cycle") != "0"
    assert _title_isk(tr, 6) == _strip_isk(planning, "Profit / cycle")

    c = sqlite3.connect(config.DB_PATH)
    c.execute(
        "UPDATE index_run SET opened_at = datetime('now', '-1 day'), "
        "planned_start = datetime('now', '-1 day') WHERE index_run_id = ?",
        (run_id,),
    )
    c.commit()
    c.close()
    assert seeded_client.post(f"/runs/{run_id}/complete").status_code == 302
    c = sqlite3.connect(config.DB_PATH)
    started = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.save_industry_jobs(c, [
        {"job_id": 900 + i, "type_id": ref.type_id("Hulk"),
         "activity_id": config.ACTIVITY_MANUFACTURING, "start_date": started,
         "runs": 1, "units": 1, "status": "active"}
        for i in range(8)
    ])
    c.close()
    tr = _row(seeded_client.get("/runs").get_data(as_text=True), 1)
    profit_tab = seeded_client.get(f"/runs/{run_id}?view=profit").get_data(as_text=True)
    assert "the hulls ESI shows started inside this cycle" in tr
    assert _title_isk(tr, 5) == _strip_isk(profit_tab, "Net proceeds / cycle") != "0"
    assert _title_isk(tr, 6) == _strip_isk(profit_tab, "Profit / cycle")


def test_the_open_run_shows_dashes_without_an_active_pipeline(seeded_client):
    first = _plan(seeded_client)
    assert seeded_client.post(f"/runs/{first}/complete").status_code == 302
    _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    c.execute("UPDATE pipeline SET is_active = 0")
    c.commit()
    c.close()
    resp = seeded_client.get("/runs")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    tr = _row(html, 2)
    assert _cells(tr)[5:7] == ["—", "—"]
    assert "no estimate yet" in tr and "at least one active pipeline" in tr
    assert ">est.</span>" not in tr
    # The executed run's figures come from its ATTRIBUTION rows, not the
    # active list, so deactivating the pipeline leaves them in place.
    assert "the Profit tab's Net proceeds / cycle" in _row(html, 1)


def test_a_final_without_a_sell_quote_is_badged_unquoted(seeded_client, ref):
    run_id = _plan(seeded_client)
    c = sqlite3.connect(config.DB_PATH)
    c.execute("DELETE FROM market_price WHERE type_id = ?", (ref.type_id("Hulk"),))
    c.commit()
    c.close()
    assert ">1 unquoted</span>" in _row(seeded_client.get("/runs").get_data(as_text=True), 1)
    assert seeded_client.post(f"/runs/{run_id}/complete").status_code == 302
    assert ">1 unquoted</span>" in _row(seeded_client.get("/runs").get_data(as_text=True), 1)


def test_a_costing_failure_leaves_a_dash_and_the_page_up(seeded_client, monkeypatch):
    """One uncostable run (a final whose type left the SDE, say) must not
    take the list down — the page ▶ Plan and discard redirect to."""
    from magoo import costing

    run_id = _plan(seeded_client)
    assert seeded_client.post(f"/runs/{run_id}/complete").status_code == 302

    def boom(*args, **kwargs):
        raise KeyError("unknown type_id 1")

    monkeypatch.setattr(costing, "hull_cost", boom)
    resp = seeded_client.get("/runs")
    assert resp.status_code == 200
    tr = _row(resp.get_data(as_text=True), 1)
    assert _cells(tr)[5:7] == ["—", "—"]
    assert "could not be costed" in tr


def test_an_executed_run_with_nothing_attributable_shows_a_dash(seeded_client):
    """A bare executed run (no items, so no attribution rows) has nothing
    to cost: a dash, as its Profit tab shows no totals — never 0 / 0."""
    c = sqlite3.connect(config.DB_PATH)
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, status, completed_at) "
        "VALUES (1, datetime('now'), 'complete', datetime('now'))"
    )
    c.commit()
    c.close()
    resp = seeded_client.get("/runs")
    assert resp.status_code == 200
    tr = _row(resp.get_data(as_text=True), 1)
    assert _cells(tr)[5:7] == ["—", "—"]
    assert "nothing to cost" in tr
