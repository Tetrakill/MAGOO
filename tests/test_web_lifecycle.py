"""Request-level run-lifecycle coverage: POST /run planning, mark-executed /
reopen (the backbone of lag costing — completed_sequence is the timeline the
cost walk prices from), the older-run splice guard, the drive-by-POST Origin
guard, and the settings save's numeric hardening.

Uses conftest.seeded_client: a populated app (real SDE attached read-only,
seeded price cache + ESI snapshot, one Hulk x 8 pipeline) — see the fixture
docstring for the wiring.
"""

import json
import sqlite3

from magoo import config, costing, store


def _state():
    """A plain connection to the test app's temp state database
    (config.DB_PATH is monkeypatched for the duration of the test)."""
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _plan(client) -> int:
    """POST /run and return the new run's index_run_id (from the redirect
    to its detail page)."""
    resp = client.post("/run")
    assert resp.status_code == 302, resp.status_code
    location = resp.headers["Location"]
    assert "/runs/" in location, location
    return int(location.rstrip("/").rsplit("/", 1)[-1])


def _completed_ids(c) -> list[int]:
    return [r["index_run_id"] for r in costing.completed_sequence(c)]


# --- (a) planning ------------------------------------------------------------


def test_run_post_creates_planned_run_with_items(seeded_client, ref):
    run_id = _plan(seeded_client)
    c = _state()
    run = c.execute(
        "SELECT * FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()
    assert run["status"] == "planned"
    assert run["run_number"] == 1
    assert run["completed_at"] is None
    items = c.execute(
        "SELECT COUNT(*) AS n FROM index_run_item WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()["n"]
    assert items > 1  # the whole chain, not just the final
    hulk = c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, ref.type_id("Hulk")),
    ).fetchone()
    c.close()
    assert hulk is not None
    assert hulk["recommended_build_qty"] == 8  # the pipeline's request


# --- (b) complete ------------------------------------------------------------


def test_complete_marks_executed_and_feeds_cost_history(seeded_client):
    run_id = _plan(seeded_client)
    c = _state()
    assert _completed_ids(c) == []
    c.close()

    resp = seeded_client.post(f"/runs/{run_id}/complete")
    assert resp.status_code == 302

    c = _state()
    run = c.execute(
        "SELECT * FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()
    assert run["status"] == "complete"
    assert run["completed_at"] is not None
    # Marking executed also stamps the actual start (COALESCEd, so a
    # future explicit start would survive).
    assert run["actual_start"] is not None
    assert _completed_ids(c) == [run_id]
    c.close()


# --- (c) reopen --------------------------------------------------------------


def test_reopen_reverts_and_leaves_cost_history(seeded_client):
    run_id = _plan(seeded_client)
    assert seeded_client.post(f"/runs/{run_id}/complete").status_code == 302
    resp = seeded_client.post(f"/runs/{run_id}/reopen")
    assert resp.status_code == 302

    c = _state()
    run = c.execute(
        "SELECT * FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()
    assert run["status"] == "planned"
    assert run["completed_at"] is None
    assert _completed_ids(c) == []
    c.close()


# --- (d) unknown ids ---------------------------------------------------------


def test_unknown_run_ids_404(seeded_client):
    assert seeded_client.post("/runs/999/complete").status_code == 404
    assert seeded_client.post("/runs/999/reopen").status_code == 404


# --- (e) the mid-history splice guard ---------------------------------------


def test_completing_an_older_run_is_refused(seeded_client):
    """With a newer run already executed, completing an OLDER run would
    splice it mid-cost-history and silently reprice every later completed
    run's lagged inputs — the route must refuse and write nothing."""
    run1 = _plan(seeded_client)
    run2 = _plan(seeded_client)
    assert seeded_client.post(f"/runs/{run2}/complete").status_code == 302
    c = _state()
    assert _completed_ids(c) == [run2]
    c.close()

    resp = seeded_client.post(
        f"/runs/{run1}/complete", follow_redirects=True
    )
    assert resp.status_code == 200  # redirected back to the run page
    assert "rewrite cost history" in resp.get_data(as_text=True)

    c = _state()
    run = c.execute(
        "SELECT * FROM index_run WHERE index_run_id = ?", (run1,)
    ).fetchone()
    assert run["status"] == "planned"
    assert run["completed_at"] is None
    assert _completed_ids(c) == [run2]  # timeline unchanged
    c.close()

    # Reopen + re-complete of the LATEST completed run stays allowed.
    assert seeded_client.post(f"/runs/{run2}/reopen").status_code == 302
    assert seeded_client.post(f"/runs/{run2}/complete").status_code == 302
    c = _state()
    assert _completed_ids(c) == [run2]
    c.close()


# --- superseding (derived, display-only — as far as the routes expose it) ---


def test_newer_plan_supersedes_older_in_the_ui(seeded_client):
    run1 = _plan(seeded_client)
    run2 = _plan(seeded_client)

    old_html = seeded_client.get(f"/runs/{run1}").get_data(as_text=True)
    new_html = seeded_client.get(f"/runs/{run2}").get_data(as_text=True)
    assert ">superseded</span>" in old_html
    assert ">superseded</span>" not in new_html

    # The list hides superseded plans by default and offers the toggle.
    listing = seeded_client.get("/runs").get_data(as_text=True)
    assert f'"/runs/{run2}"' in listing
    assert f'"/runs/{run1}"' not in listing
    listing_all = seeded_client.get("/runs?all=1").get_data(as_text=True)
    assert f'"/runs/{run1}"' in listing_all

    # Completing the NEWEST run never marks it superseded, and the older
    # planned run stays superseded (its plan is still out of date).
    assert seeded_client.post(f"/runs/{run2}/complete").status_code == 302
    new_html = seeded_client.get(f"/runs/{run2}").get_data(as_text=True)
    assert ">superseded</span>" not in new_html
    old_html = seeded_client.get(f"/runs/{run1}").get_data(as_text=True)
    assert ">superseded</span>" in old_html


# --- (f) the drive-by cross-site POST guard ----------------------------------


def test_cross_site_origin_is_blocked_loopback_passes(seeded_client):
    # A cross-site Origin is rejected before any routing or state change.
    resp = seeded_client.post(
        "/pipelines/clear", headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    c = _state()
    n = c.execute("SELECT COUNT(*) AS n FROM pipeline").fetchone()["n"]
    c.close()
    assert n == 1  # the seeded pipeline survived

    # The guard precedes routing: even an unknown id answers 403, not 404.
    resp = seeded_client.post(
        "/runs/999/complete", headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403

    # A loopback Origin passes the guard — the 404 now comes from the route.
    resp = seeded_client.post(
        "/runs/999/complete", headers={"Origin": "http://localhost"}
    )
    assert resp.status_code == 404


# --- (g) settings save hardening ---------------------------------------------


def _settings_form(**over) -> dict:
    """A complete, valid settings POST body at (mostly) default values —
    duration is 48 so a successful save is distinguishable from the
    seeded default of 24."""
    form = {
        "buffer_pct": "5",
        "purchase_margin_pct": "5",
        "duration": "48",
        "extra_runs": "1",
        "region": "10000002",
        "hub_price_basis": "ladder",
        "structure_price_basis": "ladder",
        "mfg_slots": "500",
        "reaction_slots": "500",
        "skill_industry": "5",
        "skill_advanced_industry": "5",
        "skill_reactions": "5",
        "skill_adv_ship_construction": "5",
        "skill_starship_engineering": "5",
        "skill_science": "5",
        "intermediate_me": "10",
        "intermediate_te": "20",
        "alchemy_yield_pct": "55",
        "max_alchemy_jobs": "4",
        "skill_accounting": "5",
        "skill_broker_relations": "5",
        "standing_faction": "0",
        "standing_corp": "0",
        "freight_in": "0",
        "freight_out": "0",
        "capital_market_mode": "cj6",
        "capital_structure_id": "",
        "capital_sales_tax_pct": "3.37",
        "capital_broker_pct": "1",
        "capital_movement_cost": "0",
        "capital_scc_pct": "1.5",
        "industry_scc_pct": "4",
        "skill_outpost_construction": "5",
        "structure_freight_in": "",
        "structure_buy_enabled": "1",
        "skill_encryption": "5",
        "t1_overbuild_pct": "400",
        "t2_overbuild_pct": "400",
        "compressed_ore_yield_pct": "75",
        "compressed_gas_yield_pct": "60",
        "compressed_tax_pct": "0",
    }
    for cls in config.ITEM_CLASSES:
        form[f"{cls}_structure"] = ""
        form[f"{cls}_security_band"] = "low" if cls == "reactions" else "high"
        form[f"{cls}_me_rig"] = "none"
        form[f"{cls}_te_rig"] = "none"
        form[f"{cls}_index_pct"] = "0"
        form[f"{cls}_tax_pct"] = "0.25"
    form.update(over)
    return form


def test_settings_post_bad_number_saves_nothing(seeded_client):
    c = _state()
    before = store.get_settings(c)
    c.close()
    resp = seeded_client.post(
        "/settings",
        data=_settings_form(freight_in="1,5b"),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "invalid number in freight_in" in resp.get_data(as_text=True)
    c = _state()
    after = store.get_settings(c)
    c.close()
    assert after == before  # the entire ~40-field save was discarded
    assert after.max_run_duration_hours == 24.0  # the form's 48 included


def test_settings_post_locale_comma_decimal_saves(seeded_client):
    resp = seeded_client.post(
        "/settings", data=_settings_form(freight_in="1,5")
    )
    assert resp.status_code == 302
    c = _state()
    saved = store.get_settings(c)
    c.close()
    assert saved.freight_in_isk_per_m3 == 1.5  # "1,5" read as 1.5
    assert saved.max_run_duration_hours == 48.0  # the rest saved too


# --- review 2026-09-05: buy-side badges, the unsourced remainder, the ----------
# --- compressed shallow flag, the newer-database page, hub_prices -------------


def _fill_row(name, type_id, qty, **extra):
    """A Buy-list row as _buy_context / run_detail see it (the sqlite row
    shape, every v1.25 column present)."""
    row = {
        "name": name, "type_id": type_id, "group_id": 18, "category": "Mineral",
        "category_id": 4, "depth": 5, "blueprint_id": None, "activity_id": None,
        "recommended_buy_qty": qty, "price_snapshot": 10.0, "capacity_limited": 0,
        "runs_allocated": 0, "jobs_allocated": 0, "max_runs_per_job": 1,
        "recommended_build_qty": 0, "time_per_run": 0.0, "low_stock": 0,
        "savings_unpriced_inputs": 0, "deficit_qty": qty,
        "target_stock_qty": qty, "merged_min_qty": qty, "on_hand_qty": 0,
        "in_progress_qty": 0, "buy_venue": store.BUY_VENUE_HUB,
        "structure_units_cheaper": None, "price_region_wide": 0,
        "compressed_outputs": None, "compressed_ladder_units": None,
        "compressed_fill_orders": None, "compressed_wanted_qty": None,
        "compressed_covered_qty": 0, "effective_unit_cost": None,
        "hub_buy_qty": qty, "hub_fill_price": 10.0, "hub_fill_orders": 1,
        "structure_buy_qty": 0, "structure_fill_price": None,
        "structure_fill_orders": 0, "unfilled_qty": 0, "unfilled_price": None,
    }
    row.update(extra)
    return row


def _detail_ctx(rows, bc, **over):
    """run_detail.html context over synthetic rows + a _buy_context."""
    run = {"run_number": 7, "status": "planned", "planned_start": "2026-09-05",
           "index_run_id": 1, "wallet_character_isk": 1e9,
           "wallet_corporation_isk": 2e9, "completed_at": None,
           "compressed_saving_isk": None}
    ctx = dict(
        run=run, items=rows, final_ids=set(), final_net_margin={},
        buys=bc["buys"], builds=[], reactions=[], builds_grouped=[],
        reactions_grouped=[], struct_builds=[], struct_buys=[], struct_slots=0,
        chain_struct=[], alchemy=[], alchemy_yield=0.55, chain_rows=[],
        chain_raws=[], chain_mfg=[], chain_reactions=[],
        chain_counts={"covered": 0, "buy": 0, "build": 0, "react": 0, "alchemy": 0},
        unmet=[], low_stock=[], buy_total=bc["buy_total"],
        buys_unpriced=bc["buys_unpriced"], multibuy_hub=bc["multibuy_hub"],
        multibuy_structure=bc["multibuy_structure"],
        structure_buys=bc["structure_buys"], split_buys=bc["split_buys"],
        venue_qty=bc["venue_qty"], unsourced=bc["unsourced"],
        region_wide=bc["region_wide"], compressed=bc["compressed"],
        compressed_covered=bc["compressed_covered"],
        compressed_section=[],
        compressed_saving=None, mfg_slots_used=0, reaction_slots_used=0,
        alchemy_slots_used=0,
    )
    ctx.update(over)
    return ctx


def _render_detail(ctx, template="run_detail.html", path="/runs/1"):
    from flask import render_template

    from conftest import template_app

    app = template_app()
    with app.test_request_context(path):
        return render_template(template, **ctx)


def _settings():
    from test_buy_venue import settings_with

    return settings_with(manufacturing_slots=50, reaction_slots=50)


def test_unsourced_remainder_is_in_no_venue_and_no_multibuy(ref):
    """R5: a fill-priced row's remainder beyond an EXHAUSTED book stays in
    the row's quantity but belongs to no venue — hub + structure + unfilled
    == recommended_buy_qty, and Multibuy lists the filled parts only. A row
    nothing filled at all sits in neither block."""
    from magoo.web import _buy_context

    trit = _fill_row(
        "Tritanium", 34, 1200, hub_buy_qty=1000, unfilled_qty=200,
        unfilled_price=12.0,
    )
    pyer = _fill_row(
        "Pyerite", 35, 50, hub_buy_qty=0, hub_fill_price=None,
        hub_fill_orders=0, unfilled_qty=50, unfilled_price=20.0,
    )
    bc = _buy_context([trit, pyer], ref)
    for row in (trit, pyer):
        assert (
            row["hub_buy_qty"] + row["structure_buy_qty"] + row["unfilled_qty"]
            == row["recommended_buy_qty"]
        )
    assert bc["venue_qty"] == {34: (1000, 0), 35: (0, 0)}
    assert bc["unsourced"] == {34, 35}
    assert bc["structure_buys"] == set() and bc["split_buys"] == set()
    assert bc["multibuy_hub"] == "Tritanium 1000"  # never the 200 unsourced
    assert bc["multibuy_structure"] == ""
    # the totals still carry the whole quantity at the blended price
    assert bc["buy_total"] == 1200 * 10.0 + 50 * 10.0

    html = _render_detail(_detail_ctx([trit, pyer], bc, settings=_settings()))
    # B2: a hub-only unsourced run keeps its strip badge
    assert "2 unsourced" in html
    assert "the remainder has no market at plan time" in html
    # B9: the row badge names the unsourced units and their price
    assert "only 1,000 of 1,200 units were on the stored Jita / C-J6 sell orders" in html
    assert ("the remaining 200 have no market to buy from — they are priced at "
            "the last order walked, 12 ISK") in html
    assert "listed under Jita" not in html
    # the fully unsourced row names no venue
    assert "no stored sell ladder held any of this buy" in html
    assert ">Tritanium 1000</textarea>" in html
    assert "Pyerite 50" not in html.split("<textarea", 1)[1]


def test_via_structure_count_excludes_split_rows(ref):
    """B3: "N via C-J6" counts rows bought ONLY at the structure — a split
    row has its own badge and was counted twice."""
    from magoo.web import _buy_context

    split = _fill_row(
        "Tritanium", 34, 1500, buy_venue=store.BUY_VENUE_SPLIT,
        hub_buy_qty=1000, structure_buy_qty=500, structure_fill_price=8.0,
        structure_fill_orders=1,
    )
    struct = _fill_row(
        "Pyerite", 35, 40, buy_venue=store.BUY_VENUE_STRUCTURE,
        hub_buy_qty=0, hub_fill_price=None, hub_fill_orders=0,
        structure_buy_qty=40, structure_fill_price=9.0, structure_fill_orders=1,
    )
    bc = _buy_context([split, struct], ref)
    assert bc["structure_buys"] == {34, 35} and bc["split_buys"] == {34}
    html = _render_detail(_detail_ctx([split, struct], bc, settings=_settings()))
    assert "1 via C-J6" in html and "2 via C-J6" not in html
    assert "1 split" in html
    # and a run whose only structure rows are split shows no "via" badge
    bc = _buy_context([split], ref)
    html = _render_detail(_detail_ctx([split], bc, settings=_settings()))
    assert "via C-J6" not in html and "1 split" in html


def test_compressed_wanted_figure_shows_only_in_the_tooltip(ref):
    """R6 recorded what the LP wanted beside a shrunk compressed buy. Since
    v1.26.1 no badge fires for it (the re-solve keeps new rows within the
    market's depth); a row planned before, whose wanted figure exceeds
    the buy, says so in the compressed badge's tooltip. A row without
    the figure (pre-column, NULL, or the key absent) says nothing."""
    from magoo.web import _buy_context

    veld = ref.type_id("Compressed Veldspar")
    outputs = json.dumps([[34, 1200, 1000]])
    shrunk = _fill_row(
        "Compressed Veldspar", veld, 400, compressed_outputs=outputs,
        compressed_ladder_units=400, compressed_fill_orders=2,
        compressed_wanted_qty=500,
    )
    whole = _fill_row(
        "Compressed Scordite", ref.type_id("Compressed Scordite"), 300,
        compressed_outputs=outputs, compressed_ladder_units=300,
        compressed_fill_orders=1, compressed_wanted_qty=300,
    )
    legacy = _fill_row(
        "Compressed Plagioclase", ref.type_id("Compressed Plagioclase"), 200,
        compressed_outputs=outputs, compressed_ladder_units=150,
        compressed_fill_orders=1, compressed_wanted_qty=None,
    )
    absent = _fill_row(
        "Compressed Pyroxeres", ref.type_id("Compressed Pyroxeres"), 200,
        compressed_outputs=outputs, compressed_ladder_units=150,
        compressed_fill_orders=1,
    )
    del absent["compressed_wanted_qty"]
    bc = _buy_context([shrunk, whole, legacy, absent], ref)
    assert "cut_short" not in bc
    html = _render_detail(
        _detail_ctx([shrunk, whole, legacy, absent], bc, settings=_settings())
    )
    assert "the plan wanted 500 but the market could fill only 400 in whole batches" in html
    assert html.count("the plan wanted") == 1
    assert "cut short" not in html


def test_deficit_dialog_and_chain_tooltip_carry_the_compressed_covered_leg(ref):
    """B5/B6: a raw part-covered by compressed purchases states both legs —
    the Plan tab's deficit dialog reads the covered units off the row
    (data-covered) and the Chain tab's tooltip no longer says "bought
    just-in-time: 0" for a fully covered raw."""
    from magoo.web import _buy_context, _chain_context

    trit = _fill_row(
        "Tritanium", 34, 100, deficit_qty=1300, compressed_covered_qty=1200,
        effective_unit_cost=7.0,
    )
    covered = _fill_row(
        "Pyerite", 35, 0, deficit_qty=500, compressed_covered_qty=500,
        effective_unit_cost=7.0,
    )
    bc = _buy_context([trit, covered], ref)
    html = _render_detail(_detail_ctx([trit, covered], bc, settings=_settings()))
    assert 'data-covered="1200"' in html
    assert "come from reprocessing compressed purchases" in html  # openDeficit
    # the chain tab: buy_qty + compressed_covered, never "just-in-time: 0"
    rows = [
        {**i, "alchemy_credit_qty": 0, "alchemy_for_type_id": None,
         "alchemy_output_qty": 0}
        for i in (trit, covered)
    ]
    chain = _chain_context(ref, rows)
    by_name = {r["name"]: r for r in chain["chain_rows"]}
    assert by_name["Pyerite"]["status"] == "buy"
    chain_ctx = dict(
        run={"run_number": 7, "status": "planned", "planned_start": "2026-09-05",
             "index_run_id": 1},
        settings=_settings(), **chain,
    )
    html = _render_detail(chain_ctx, "run_chain.html", "/runs/1?view=chain")
    assert "bought just-in-time: 0" not in html
    assert ("500 just-in-time: 0 bought direct + 500 from reprocessing "
            "compressed purchases") in html
    assert ("1,300 just-in-time: 100 bought direct + 1,200 from reprocessing "
            "compressed purchases") in html


def test_newer_database_message_reaches_the_user(seeded_client, monkeypatch):
    """B7: store.ensure_schema refuses a database written by a newer build
    with a RuntimeError; the message must reach the user on a page, not
    vanish into a bare 500."""

    def refuse(*_a, **_k):
        raise RuntimeError("This database was written by a newer version of Magoo")

    monkeypatch.setattr(store, "ensure_schema", refuse)
    resp = seeded_client.get("/")
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "written by a newer version of Magoo" in body
    assert "Magoo cannot continue" in body
    # the DB-free health probe is unaffected
    assert seeded_client.get("/magoo/health").status_code == 200


def test_run_post_passes_the_cached_hub_quotes_to_the_snapshot(
    seeded_client, monkeypatch, ref
):
    """C5: /run hands snapshot_from_state hub_prices — the cached Jita quote
    per demanded type (kept even where the structure venue wins Phase 1) —
    so the sourcing pass can raise its synthetic hub rung from the hub
    figure, not the winning venue's."""
    from magoo import engine, market

    captured = {}

    def capture(conn, **kwargs):
        captured.update(kwargs)
        return None  # -> "no ESI data yet" redirect; the plan never runs

    monkeypatch.setattr(engine, "snapshot_from_state", capture)
    resp = seeded_client.post("/run")
    assert resp.status_code == 302
    assert "hub_prices" in captured
    c = _state()
    settings_ = store.get_settings(c)
    expected = {
        t: price
        for t, (price, _rw) in market.cached_hub_quotes(
            c, settings_.price_region_id, engine.demand_type_ids(c, ref),
            settings_.price_source,
        ).items()
    }
    c.close()
    assert expected and captured["hub_prices"] == expected
    assert captured["hub_prices"][ref.type_id("Tritanium")] > 0
