"""Schema 14 (2026-10-30, the hold anchors 2026-10-31), the store and web
sides: the alchemy blacklist table and settings flag, the hold tables and
their indexes, the 13 → 14 upgrade, the Settings page's grid and save,
and the run page's for-sale rows, "Sell in Jita" panel and For sale stat.
The one literal pin of the schema number lives here."""

import re
import sqlite3
from datetime import datetime, timezone

import pytest

from magoo import holds, store
from test_onboarding import fresh_app, fresh_client  # noqa: F401 — fixtures
from test_web_lifecycle import (
    _detail_ctx,
    _render_detail,
    _settings,
    _settings_form,
    _state,
)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    yield c
    c.close()


# --- store --------------------------------------------------------------------


def _columns(conn, table) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn, table) -> dict:
    return {r["name"]: r for r in conn.execute(f"PRAGMA index_list({table})")}


def test_schema_14_tables_columns_and_defaults(conn):
    """The one place the schema number is pinned: 14 is the unreleased
    stamp the blacklist (2026-10-30) and the hold anchors (2026-10-31)
    ship under; every other test compares against store.SCHEMA_VERSION."""
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 14
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"alchemy_blacklist", "alchemy_hold", "alchemy_hold_release"} <= tables
    assert {
        "alchemy_profit_jobs", "alchemy_sale_net_unit", "alchemy_route_unit_cost",
        "held_for_sale_qty", "held_from_credit_qty",
    } <= _columns(conn, "index_run_item")
    assert {"baseline_units", "chain_units", "yield_"} <= _columns(conn, "alchemy_hold")
    assert "unit_cost" in _columns(conn, "alchemy_hold_release")
    assert {"end_date", "completed_date"} <= _columns(conn, "industry_job")
    indexes = _indexes(conn, "alchemy_hold_release")
    assert {"alchemy_hold_release_ref", "alchemy_hold_release_hold", "alchemy_hold_release_sale"} <= set(indexes)
    assert indexes["alchemy_hold_release_sale"]["unique"] == 1
    s = store.get_settings(conn)
    assert s.alchemy_for_profit is False
    assert store.alchemy_blacklist(conn) == set()


def test_alchemy_for_profit_round_trips_as_a_bool(conn):
    conn.execute("UPDATE settings SET alchemy_for_profit = 1 WHERE id = 1")
    conn.commit()
    assert store.get_settings(conn).alchemy_for_profit is True


def test_alchemy_blacklist_replace_and_read(conn):
    store.set_alchemy_blacklist(conn, {16671, 16673})
    conn.commit()
    assert store.alchemy_blacklist(conn) == {16671, 16673}
    store.set_alchemy_blacklist(conn, {16673})
    conn.commit()
    assert store.alchemy_blacklist(conn) == {16673}
    store.set_alchemy_blacklist(conn, set())
    conn.commit()
    assert store.alchemy_blacklist(conn) == set()


def test_a_schema_13_database_upgrades_to_the_current_schema(tmp_path, monkeypatch):
    """The migration adds the blacklist table, the settings flag, the
    for-sale and held columns on index_run_item, the two hold tables with
    their indexes (the partial UNIQUE one included) and the job record's
    delivery columns to a database written before them (the v1.30.0
    shape), keeps every row, stamps the current version and lands the
    pre-upgrade backup — named for the build AND the schema it found —
    in THIS temp dir."""
    from magoo import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.sqlite")
    c = sqlite3.connect(tmp_path / "old.sqlite")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    c.execute("INSERT INTO pipeline (name, final_product_type_id, output_qty_per_run) VALUES ('Hulk', 22544, 8)")
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, opened_at, status) "
        "VALUES (1, '2026-09-01 12:00:00', '2026-09-01 12:00:00', 'planned')"
    )
    # Back to the schema-13 shape.
    c.execute("DROP TABLE alchemy_blacklist")
    for index in ("alchemy_hold_release_ref", "alchemy_hold_release_hold", "alchemy_hold_release_sale"):
        c.execute(f"DROP INDEX IF EXISTS {index}")
    c.execute("DROP TABLE alchemy_hold_release")
    c.execute("DROP TABLE alchemy_hold")
    c.execute("ALTER TABLE settings DROP COLUMN alchemy_for_profit")
    for column in (
        "alchemy_profit_jobs", "alchemy_sale_net_unit", "alchemy_route_unit_cost",
        "held_for_sale_qty", "held_from_credit_qty",
    ):
        c.execute(f"ALTER TABLE index_run_item DROP COLUMN {column}")
    for column in ("end_date", "completed_date"):
        c.execute(f"ALTER TABLE industry_job DROP COLUMN {column}")
    c.execute("PRAGMA user_version = 13")
    c.commit()
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not tables & {"alchemy_blacklist", "alchemy_hold", "alchemy_hold_release"}

    store.ensure_schema(c)

    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert (tmp_path / "backups" / f"magoo-pre-{store.__version__}-s13.sqlite").exists()
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"alchemy_blacklist", "alchemy_hold", "alchemy_hold_release"} <= tables
    indexes = _indexes(c, "alchemy_hold_release")
    assert {"alchemy_hold_release_ref", "alchemy_hold_release_hold", "alchemy_hold_release_sale"} <= set(indexes)
    assert indexes["alchemy_hold_release_sale"]["unique"] == 1
    assert {
        "alchemy_profit_jobs", "alchemy_sale_net_unit", "alchemy_route_unit_cost",
        "held_for_sale_qty", "held_from_credit_qty",
    } <= _columns(c, "index_run_item")
    assert {"baseline_units", "chain_units", "yield_"} <= _columns(c, "alchemy_hold")
    assert "unit_cost" in _columns(c, "alchemy_hold_release")
    assert {"end_date", "completed_date"} <= _columns(c, "industry_job")
    assert c.execute("SELECT COUNT(*) FROM pipeline").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 1
    assert store.get_settings(c).alchemy_for_profit is False
    assert store.alchemy_blacklist(c) == set()
    # The rebuilt tables take rows, and the UNIQUE index folds a sale
    # applied twice to one hold into one row.
    store.upsert_hold(c, 1, 17960, planned_qty=500, route_unit_cost=12.5, baseline_units=0, chain_units=0, yield_=0.55)
    store.add_alchemy_release(c, 1, 17960, 40, "market", ref_id=9001, unit_price=30000.0, unit_cost=12.5)
    store.add_alchemy_release(c, 1, 17960, 10, "market", ref_id=9001, unit_price=30000.0, unit_cost=12.5)
    c.commit()
    [release] = store.release_rows(c, 1)
    assert (release["qty"], release["unit_cost"]) == (50, 12.5)
    c.close()


# --- the Settings page --------------------------------------------------------


def _a_route(ref):
    composite_id = sorted(ref.alchemy_routes())[0]
    route = ref.alchemy_routes()[composite_id]
    return composite_id, ref.type_info(composite_id).name, ref.type_info(route.formula.blueprint_id).name


def test_settings_page_lists_every_route_under_never_run(seeded_client, ref):
    page = seeded_client.get("/settings").get_data(as_text=True)
    assert "Alchemy for Profit" in page
    assert 'name="alchemy_for_profit"' in page
    assert "Never run" in page
    assert 'name="alchemy_blacklist_present"' in page
    for composite_id, route in ref.alchemy_routes().items():
        assert f'name="alchemy_block_{composite_id}"' in page
        # Labelled by the unrefined product (user wording 2026-10-30),
        # with no formula sub-label.
        assert ref.type_info(route.unrefined_id).name in page
        assert ref.type_info(route.formula.blueprint_id).name not in page
    assert len(ref.alchemy_routes()) == 17  # the SDE tripwire, as test_alchemy pins it
    # The cap's help says what the code does (review 2026-10-31): the
    # savings moves count the jobs this PLAN places, the profit tail alone
    # adds the formula's jobs installed this cycle — never "per cycle" alone.
    assert "per plan, savings and for-profit jobs together" in page
    assert "the for-profit tail also counts the formula's jobs installed this cycle" in page
    assert "per cycle, savings and for-profit" not in page


def test_settings_save_stores_the_flag_and_the_blacklist(seeded_client, ref):
    composite_id, composite_name, _formula = _a_route(ref)
    resp = seeded_client.post(
        "/settings",
        data=_settings_form(**{
            "alchemy_for_profit": "on",
            "alchemy_blacklist_present": "1",
            f"alchemy_block_{composite_id}": "on",
            "alchemy_block_999999": "on",  # not a route: ignored
        }),
    )
    assert resp.status_code == 302
    c = _state()
    assert store.get_settings(c).alchemy_for_profit is True
    assert store.alchemy_blacklist(c) == {composite_id}
    c.close()
    # The grid renders the stored list back: the blocked route's box is
    # checked, an unblocked one's is not (review 2026-10-31: strict).
    page = seeded_client.get("/settings").get_data(as_text=True)
    assert re.search(rf'<input type="checkbox" name="alchemy_block_{composite_id}"\s+checked>', page)
    other = next(t for t in sorted(ref.alchemy_routes()) if t != composite_id)
    assert re.search(rf'<input type="checkbox" name="alchemy_block_{other}"\s*>', page)
    assert not re.search(rf'name="alchemy_block_{other}"\s+checked', page)
    assert len(re.findall(r'name="alchemy_block_\d+"\s+checked>', page)) == 1


def test_the_never_run_grid_waits_for_the_game_data(fresh_client):
    """Before the SDE import the Settings page shows the Never run
    heading with its pointer at the Dashboard and no route box — and no
    marker, so a save from that page cannot clear a stored list."""
    page = fresh_client.get("/settings").get_data(as_text=True)
    assert "Never run" in page
    assert 'name="alchemy_blacklist_present"' not in page
    assert "alchemy_block_" not in page
    assert re.search(r"Download the game data from the Dashboard to list the\s+routes\.", page)


def test_settings_save_without_the_grid_keeps_the_blacklist(seeded_client, ref):
    """A form that never rendered the grid (no game data at the time)
    carries no marker and must not clear the list; one that did, with
    nothing ticked, clears it."""
    composite_id, _name, _formula = _a_route(ref)
    c = _state()
    store.set_alchemy_blacklist(c, {composite_id})
    c.commit()
    c.close()
    assert seeded_client.post("/settings", data=_settings_form()).status_code == 302
    c = _state()
    assert store.alchemy_blacklist(c) == {composite_id}
    assert store.get_settings(c).alchemy_for_profit is False  # the unticked box
    c.close()
    assert seeded_client.post(
        "/settings", data=_settings_form(alchemy_blacklist_present="1")
    ).status_code == 302
    c = _state()
    assert store.alchemy_blacklist(c) == set()
    c.close()


# --- the run page -------------------------------------------------------------


def _unrefined_row(ref, jobs, profit_jobs, runs_per_job=272, **extra):
    composite_id = sorted(ref.alchemy_routes())[0]
    route = ref.alchemy_routes()[composite_id]
    runs = jobs * runs_per_job
    row = {
        "name": ref.type_info(route.unrefined_id).name, "type_id": route.unrefined_id,
        "group_id": 428, "category": "Material", "category_id": 4, "depth": 3,
        "blueprint_id": route.formula.blueprint_id, "activity_id": 11,
        "portion_size": 1, "max_runs_per_job": runs_per_job,
        "jobs_allocated": jobs, "runs_allocated": runs, "recommended_build_qty": runs,
        "recommended_buy_qty": 0, "time_per_run": 21600.0, "capacity_limited": 0,
        "low_stock": 0, "price_snapshot": None, "deficit_qty": 0,
        "target_stock_qty": 0, "merged_min_qty": 0, "on_hand_qty": 0,
        "in_progress_qty": 0, "alchemy_for_type_id": composite_id,
        "alchemy_output_qty": 0, "alchemy_credit_qty": 0,
        "direct_unit_cost": None, "alchemy_unit_cost": None,
        "alchemy_profit_jobs": profit_jobs,
        "alchemy_sale_net_unit": 900.0 if profit_jobs else None,
        "alchemy_route_unit_cost": 400.0 if profit_jobs else None,
        "install_runs": None, "install_jobs": None, "install_per_job": None,
        "install_limited_by": None, "savings_unpriced_inputs": 0,
        "buy_venue": None, "structure_units_cheaper": None, "price_region_wide": 0,
    }
    row.update(extra)
    return row, route


def test_alchemy_section_splits_the_for_sale_share(ref):
    """Of a row's jobs, the savings jobs come first: the for-sale units
    are the rest, and an install cut trims them first."""
    from magoo.web import _alchemy_sales, _alchemy_section

    settings = _settings()
    row, route = _unrefined_row(ref, jobs=5, profit_jobs=2)
    (a,) = _alchemy_section(ref, settings, [row])
    out = lambda runs: int(runs * route.composite_qty * 0.55)
    assert a["profit_jobs"] == 2
    assert a["sale_qty"] == out(2 * 272)
    assert a["expected_qty"] == out(3 * 272)
    assert a["profit_isk"] == pytest.approx((900.0 - 400.0) * out(2 * 272))
    (s,) = _alchemy_sales([a])
    assert s["sell_qty"] == out(2 * 272) and s["jobs"] == 2
    assert s["net_unit"] == 900.0 and s["route_unit"] == 400.0
    # The install check cut it to 4 jobs: one for-sale job is gone.
    cut, _route = _unrefined_row(
        ref, jobs=5, profit_jobs=2, install_runs=4 * 272, install_jobs=4,
        install_per_job=272, install_limited_by=-1,
    )
    (a,) = _alchemy_section(ref, settings, [cut])
    assert a["sale_qty"] == out(272) and a["expected_qty"] == out(3 * 272)
    # A pure savings row has no sale share and no panel row.
    plain, _route = _unrefined_row(ref, jobs=3, profit_jobs=0)
    (a,) = _alchemy_section(ref, settings, [plain])
    assert a["sale_qty"] == 0 and a["profit_isk"] is None
    assert _alchemy_sales([a]) == []


def _hold_row(route, **over) -> holds.Hold:
    """A derived hold as the loader hands it to the panel (holds.Hold with
    the D1 figures set by hand)."""
    hold = holds.Hold(
        index_run_id=1, run_number=1, run_status="complete", opened_at=None, completed_at=None,
        composite_type_id=route.composite_id, unrefined_type_id=route.unrefined_id,
        composite_qty=route.composite_qty, planned_qty=1000, route_unit_cost=400.0,
        baseline_units=0, chain_units=0, yield_=0.55,
    )
    for name, value in over.items():
        setattr(hold, name, value)
    return hold


def test_the_sell_in_jita_panel_takes_the_runs_holds(ref):
    """_alchemy_sales over a LIST of holds.Hold (user rulings 2026-10-31):
    a hold-only row (an executed run, the formula listing no job now)
    sells the hold's installed units and prices the unsold remainder at
    today's quote (review 2026-10-31: the active quantity), splits Released
    into observed and by-hand marks, reads its status and end from the
    hold, offers Mark sold against the delivered live holds and lists the
    by-hand marks with their undo; a proposal nothing was installed for
    is 'not installed' and a dropped proposal is no row at all."""
    from magoo.web import _alchemy_sales

    composite_id, name, _formula = _a_route(ref)
    route = ref.alchemy_routes()[composite_id]
    delivered = datetime(2026, 9, 10, tzinfo=timezone.utc)
    hold = _hold_row(
        route, for_sale_unrefined=100, for_sale_qty=1000, delivered_units=100, delivered_qty=1000,
        first_delivered=delivered, delivered_at=delivered, live=True, expires_after=2,
        observed_qty=300, manual_priced_qty=50, manual_unpriced_qty=20,
        releases=[
            {"release_id": 1, "source": "market", "qty": 300, "unit_price": 900.0,
             "released_at": "2026-09-11T00:00:00Z"},
            {"release_id": 2, "source": "manual", "qty": 50, "unit_price": 950.0,
             "released_at": "2026-09-12T00:00:00Z"},
            {"release_id": 3, "source": "manual", "qty": 20, "unit_price": None,
             "released_at": "2026-09-13T00:00:00Z"},
        ],
    )
    (row,) = _alchemy_sales(
        [], [hold], targets={composite_id: [hold]}, quotes={composite_id: 900.0}, names={composite_id: name},
    )
    assert (row["composite_name"], row["composite_id"]) == (name, composite_id)
    assert (row["sell_qty"], row["hold_qty"], row["jobs"]) == (1000, 1000, 0)
    assert (row["sold"], row["by_hand"], row["by_hand_priced"], row["by_hand_unpriced"]) == (300, 70, 50, 20)
    assert (row["released"], row["held"]) == (370, 630)
    assert row["status"] == "delivered" and row["live"] and row["counted"]
    assert row["expiry_words"] == "until run 2 is marked executed" and row["expires_after"] == 2
    assert row["profit_isk"] == pytest.approx((900.0 - 400.0) * 630) and row["profit_today"]
    assert (row["active_qty"], row["held_active"]) == (630, 630)
    assert (row["net_unit"], row["route_unit"]) == (900.0, 400.0)
    assert row["can_mark_sold"] and (row["mark_sold_max"], row["mark_sold_run"], row["mark_sold_runs"]) == (630, 1, [1])
    assert [(m["release_id"], m["qty"], m["unit_price"], m["live"]) for m in row["by_hand_rows"]] == [
        (2, 50, 950.0, True), (3, 20, None, True),
    ]
    # No quote: no expected profit, everything else the same.
    (unquoted,) = _alchemy_sales([], [hold], names={composite_id: name})
    assert unquoted["profit_isk"] is None and unquoted["sell_qty"] == 1000 and not unquoted["can_mark_sold"]
    # A proposal nothing was installed for (an open run): nothing hidden, not sold.
    idle = _hold_row(route, run_status="planned")
    (proposal,) = _alchemy_sales([], [idle], names={composite_id: name})
    assert (proposal["sell_qty"], proposal["held"], proposal["status"], proposal["hold_planned"]) == (
        0, 0, "not installed", 1000
    )
    assert proposal["counted"] and proposal["profit_isk"] is None
    # A dropped proposal (planned 0, nothing installed, no release): no row.
    assert _alchemy_sales([], [_hold_row(route, planned_qty=0)]) == []


def test_run_page_shows_the_for_sale_rows_panel_and_stat(ref):
    from magoo.web import _alchemy_sales, _alchemy_section, _buy_context

    settings = _settings()
    row, route = _unrefined_row(ref, jobs=4, profit_jobs=4)
    alchemy = _alchemy_section(ref, settings, [row])
    sales = _alchemy_sales(alchemy)
    bc = _buy_context([], ref)
    html = _render_detail(_detail_ctx(
        [row], bc, settings=settings, alchemy=alchemy, alchemy_sales=sales,
        for_sale_qty=sum(s["sell_qty"] for s in sales),
        for_sale_profit=sum(s["profit_isk"] for s in sales),
        for_sale_jobs=sum(s["jobs"] for s in sales),
    ))
    composite_name = ref.type_info(route.composite_id).name
    assert "Sell in Jita" in html
    assert ">for sale<" in html
    assert "For sale" in html  # the stat
    assert "Savings / profit" in html
    assert composite_name in html
    assert "all for sale" in html  # the reprocess checklist
    assert "4 of 4 jobs run for profit" in html
    # Without for-sale jobs the panel, badge and stat are absent.
    plain, _route = _unrefined_row(ref, jobs=3, profit_jobs=0)
    alchemy = _alchemy_section(ref, settings, [plain])
    html = _render_detail(_detail_ctx(
        [plain], bc, settings=settings, alchemy=alchemy, alchemy_sales=[],
        for_sale_qty=0, for_sale_profit=0.0, for_sale_jobs=0,
    ))
    assert "Sell in Jita" not in html
    assert ">for sale<" not in html
    assert '<span class="label">For sale</span>' not in html
