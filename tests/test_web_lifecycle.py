"""Request-level run-lifecycle coverage: POST /run planning, mark-executed /
reopen (the backbone of lag costing — completed_sequence is the timeline the
cost walk prices from), the older-run splice guard, the drive-by-POST Origin
guard, and the settings save's numeric hardening. Since v1.29 revision 6
(user ruling R1 2026-09-28: one run per buying cycle, kept live) also the
in-place re-plan: the Plan button and every ESI update re-plan the open
run instead of creating one — since revision 7 (user ruling 2026-09-29)
with no stop rule, counting final jobs started inside the current buying
cycle as this cycle's wave.

Uses conftest.seeded_client: a populated app (real SDE attached read-only,
seeded price cache + ESI snapshot, one Hulk x 8 pipeline) — see the fixture
docstring for the wiring.
"""

import html as html_mod
import json
import re
import sqlite3
from datetime import datetime, timezone

import pytest

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


def _newer_run(c, run_number: int, status: str = "planned") -> int:
    """A newer run built by SQL — since revision 6 a second POST /run
    re-plans the open run in place, so an older / superseded run can no
    longer be made by planning twice (contract review A18)."""
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, status) "
        "VALUES (?, datetime('now'), ?)",
        (run_number, status),
    )
    c.commit()
    return c.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]


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
    c = _state()
    run2 = _newer_run(c, 2)
    c.close()
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
    c = _state()
    run2 = _newer_run(c, 2)
    c.close()

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
        "freight_in_default": "0",
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


@pytest.mark.parametrize(
    "posted, stored",
    [("250", 250.0), ("1,5", 1.5), ("", 0.0), ("-40", 0.0)],
)
def test_settings_default_inbound_freight_round_trips(
    seeded_client, posted, stored,
):
    """Revision 4 (user ruling 2026-09-28): the rate a purchase at neither
    Jita 4-4 nor the structure market hauls at — parsed like its
    structure sibling: blank reads 0, a negative clamps to 0 — and the
    page shows it back beside the other two inbound rates."""
    resp = seeded_client.post(
        "/settings", data=_settings_form(freight_in_default=posted)
    )
    assert resp.status_code == 302
    c = _state()
    saved = store.get_settings(c)
    c.close()
    assert saved.freight_in_default_isk_per_m3 == stored
    assert saved.freight_in_rate(store.BUY_VENUE_OTHER) == stored
    page = seeded_client.get("/settings").get_data(as_text=True)
    assert 'name="freight_in_default"' in page
    assert f'value="{stored}"' in page[page.index('id="freight_in_default"'):][:200]
    assert page.index('id="freight_in"') < page.index('id="freight_in_default"')
    assert "At 0 those purchases carry no freight" in page


def test_settings_bad_default_inbound_freight_saves_nothing(seeded_client):
    c = _state()
    before = store.get_settings(c)
    c.close()
    resp = seeded_client.post(
        "/settings", data=_settings_form(freight_in_default="12x"),
        follow_redirects=True,
    )
    assert "invalid number in freight_in_default" in resp.get_data(as_text=True)
    c = _state()
    assert store.get_settings(c) == before
    c.close()


def test_a_settings_save_reruns_the_purchase_matching(seeded_client, monkeypatch):
    """Revision 4 (contract review A12): the matcher reads settings (the
    venues, the unplanned-ore conversion, the live inbound rates), so a
    save re-derives the runs' purchase lines at once; a matching failure
    is flashed and the save still stands."""
    from magoo import buying

    calls = []

    def assign(conn, ref, settings, now=None):
        calls.append(settings.freight_in_default_isk_per_m3)
        raise RuntimeError("matcher exploded")

    monkeypatch.setattr(buying, "assign_purchases", assign)
    resp = seeded_client.post(
        "/settings", data=_settings_form(freight_in_default="75"),
        follow_redirects=True,
    )
    assert calls == [75.0]
    page = resp.get_data(as_text=True)
    assert "settings saved" in page
    assert "purchase matching failed (matcher exploded)" in page
    c = _state()
    assert store.get_settings(c).freight_in_default_isk_per_m3 == 75.0
    c.close()


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

    # v1.29 (ruling R2, 2026-09-24): the badges, the venue cell and the
    # Multibuy blocks moved to the Buy tab, which is where the unsourced
    # signal is now asserted (tests/test_buy_tab.py). The Industry Jobs
    # page must show none of it.
    html = _render_detail(_detail_ctx([trit, pyer], bc, settings=_settings()))
    assert "unsourced" not in html
    assert "no stored sell ladder held any of this buy" not in html
    assert "<textarea" not in html


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
    # v1.29: the two badges are on the Buy tab's header now (ruling R9);
    # the rule they encode is this _buy_context split, unchanged.
    html = _render_detail(_detail_ctx([split, struct], bc, settings=_settings()))
    assert "via C-J6" not in html and "split" not in html


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
    # v1.29 (R2/R8): m.compressed_badge carries that tooltip, and it now
    # renders beside the ore in the Buy tab's per-group Multibuy — not
    # here. This page shows nothing about compressed buying at all.
    html = _render_detail(
        _detail_ctx([shrunk, whole, legacy, absent], bc, settings=_settings())
    )
    assert "the plan wanted" not in html
    assert ">compressed</span>" not in html
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
    # v1.29: a bought-only row's item button moved to the Buy tab with the
    # rest of purchasing (ruling R2) — the dialog and its script are a
    # shared partial now, so the covered leg is asserted there
    # (tests/test_buy_tab.py); the JS that reads it is still on this page.
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


# --- v1.29 revision 6 (user ruling R1 2026-09-28): one live run per cycle ----


def _refresh_world(monkeypatch, calls, on_hand=None, in_progress=None,
                   fail_snapshot=None, job_starts=None):
    """Stub the ESI update's network steps: the snapshot pull saves
    `on_hand` / `in_progress` (and `job_starts`, esi.refresh_state's
    revision-7 format {type_id: [[ESI start text, units], ...]}; None
    stores NULL, "not recorded" — the full wave) as the new snapshot (or
    raises `fail_snapshot`), the sales pull and the price pull record
    themselves.
    The price cache seeded_client wrote stays, so the re-plan prices
    exactly like the Plan button."""
    import httpx  # noqa: F401 — the fail_snapshot exceptions come from it

    from magoo import esi, ledger, market

    monkeypatch.setattr(
        market, "refresh_prices",
        lambda *a, **k: (calls.append("prices"), (0, 0, 0))[1],
    )
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)

    def fake_state(conn, ref):
        if fail_snapshot is not None:
            raise fail_snapshot
        calls.append("snapshot")
        store.save_esi_snapshot(
            conn, dict(on_hand or {}), dict(in_progress or {}), {}, 0.0, 0.0,
            {}, job_starts=None if job_starts is None else dict(job_starts),
        )
        return {"on_hand": dict(on_hand or {}), "in_progress": dict(in_progress or {}),
                "active_jobs": {}, "job_ends": {}, "character_isk": 0.0,
                "corporation_isk": 0.0}

    def fake_pull(conn, ref):
        calls.append("sales")
        return ledger.PullSummary(new_sales=0, contracts_finished=0, open_orders=0)

    monkeypatch.setattr(esi, "refresh_state", fake_state)
    monkeypatch.setattr(ledger, "pull_sales", fake_pull)


def _spy_plans(monkeypatch, calls):
    """Record every engine.plan_index_run call (the real one still runs)
    with the run it replaced."""
    from magoo import engine

    real = engine.plan_index_run

    def spy(conn, ref, snapshot, **kw):
        calls.append(("replan", kw.get("replace_index_run_id")))
        return real(conn, ref, snapshot, **kw)

    monkeypatch.setattr(engine, "plan_index_run", spy)


def _row(c, run_id, type_id):
    return c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, type_id),
    ).fetchone()


def test_the_plan_button_replans_the_open_run_and_creates_after_execution(
    seeded_client,
):
    """R1 / contract review A5: while the newest run is not executed the
    button re-plans it IN PLACE — same id and run number, planned_start
    moved, its purchases kept — and a NEW run is created only after Mark
    executed. The dashboard button says which."""
    run_id = _plan(seeded_client)
    c = _state()
    c.execute(
        "UPDATE index_run SET planned_start = '2026-01-01 00:00:00' "
        "WHERE index_run_id = ?", (run_id,),
    )
    c.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
        "unit_price) VALUES (?, 34, 'hub', 5, 4.0)", (run_id,),
    )
    c.commit()
    dash = seeded_client.get("/").get_data(as_text=True)
    assert "▶ Re-plan run 1" in dash and "▶ Plan index run</button>" not in dash
    resp = seeded_client.post("/run", follow_redirects=True)
    assert "index run 1 re-planned in place" in resp.get_data(as_text=True)
    assert _plan(seeded_client) == run_id
    run = c.execute("SELECT * FROM index_run").fetchall()
    assert len(run) == 1 and run[0]["run_number"] == 1
    assert run[0]["planned_start"] != "2026-01-01 00:00:00"
    assert run[0]["status"] == "planned"
    assert c.execute(
        "SELECT COUNT(*) FROM run_purchase WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] == 1
    assert seeded_client.post(f"/runs/{run_id}/complete").status_code == 302
    dash = seeded_client.get("/").get_data(as_text=True)
    assert "▶ Plan index run" in dash and "Re-plan run" not in dash
    resp = seeded_client.post("/run", follow_redirects=True)
    assert "index run 2 planned" in resp.get_data(as_text=True)
    assert c.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 2
    c.close()


def test_the_esi_update_replans_the_open_run_in_place(
    seeded_client, monkeypatch, ref,
):
    """R1 / §3 / A4: the update re-plans the open run AFTER the prices and
    BEFORE matching — same id and run number, planned_start moved, its
    purchases kept — from the stock it just pulled: 5,000 Tritanium on
    hand now lowers the Tritanium buy, and the Buy tab's Remaining is the
    re-plan's own recommended_buy_qty + compressed_covered_qty (nothing
    subtracted for purchases, contract review A16)."""
    from magoo import buying

    trit = ref.type_id("Tritanium")
    run_id = _plan(seeded_client)
    c = _state()
    c.execute(
        "UPDATE index_run SET planned_start = '2026-01-01 00:00:00' "
        "WHERE index_run_id = ?", (run_id,),
    )
    c.execute(
        "INSERT INTO run_purchase (index_run_id, type_id, venue, quantity, "
        "unit_price) VALUES (?, ?, 'hub', 5, 4.0)", (run_id, trit),
    )
    c.commit()
    before = _row(c, run_id, trit)
    assert before["on_hand_qty"] == 0
    calls = []
    _refresh_world(monkeypatch, calls, on_hand={trit: 5000})
    _spy_plans(monkeypatch, calls)
    monkeypatch.setattr(
        buying, "assign_purchases",
        lambda conn, ref, settings, now=None: (calls.append("assign"), {})[1],
    )
    monkeypatch.setattr(buying, "summary_line", lambda summary: "purchases: ok")
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert calls == ["snapshot", "sales", "prices", ("replan", run_id), "assign"]
    assert "run 1 re-planned: " in html and " jobs" in html
    runs = c.execute("SELECT * FROM index_run").fetchall()
    assert [r["index_run_id"] for r in runs] == [run_id]
    assert runs[0]["run_number"] == 1 and runs[0]["status"] == "planned"
    assert runs[0]["planned_start"] != "2026-01-01 00:00:00"
    assert c.execute(
        "SELECT COUNT(*) FROM run_purchase WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] == 1
    after = _row(c, run_id, trit)
    assert after["on_hand_qty"] == 5000
    demand = lambda row: row["recommended_buy_qty"] + (row["compressed_covered_qty"] or 0)
    assert demand(after) == max(0, demand(before) - 5000)
    page = seeded_client.get(f"/runs/{run_id}?view=buy").get_data(as_text=True)
    row = page[page.index(f'id="t{trit}"'):]
    row = row[:row.index("</tr>")]
    import re

    cells = [" ".join(re.sub(r"<[^>]+>", " ", body).split())
             for body in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
    assert cells[2] == "5,000"                          # On Hand
    assert cells[3] == f"{demand(after):,}"             # Remaining
    c.close()


def test_the_esi_update_skips_the_replan_with_a_note(seeded_client, monkeypatch):
    """A4: no open run (after Mark executed), or a snapshot step that did
    not succeed in this request, skips the re-plan with a clause — and the
    later steps still run."""
    import httpx

    calls = []
    _refresh_world(monkeypatch, calls)
    _spy_plans(monkeypatch, calls)
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert "no open run to re-plan — ▶ Plan index run plans the first cycle" in html
    assert calls == ["snapshot", "sales", "prices"]
    run_id = _plan(seeded_client)
    calls.clear()
    c = _state()
    c.execute(
        "UPDATE index_run SET planned_start = '2026-01-01 00:00:00' "
        "WHERE index_run_id = ?", (run_id,),
    )
    c.commit()
    _refresh_world(
        monkeypatch, calls,
        fail_snapshot=httpx.HTTPStatusError(
            "boom", request=httpx.Request("GET", "https://esi.example/"),
            response=httpx.Response(500),
        ),
    )
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert (
        "run 1 not re-planned — the ESI snapshot did not refresh, so it "
        "keeps its previous plan"
    ) in html
    assert ("replan", run_id) not in calls and "prices" in calls
    assert c.execute("SELECT planned_start FROM index_run").fetchone()[0] == (
        "2026-01-01 00:00:00"
    )
    assert seeded_client.post(f"/runs/{run_id}/complete").status_code == 302
    calls.clear()
    _refresh_world(monkeypatch, calls)
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert (
        "no open run to re-plan — the newest run is executed; ▶ Plan index "
        "run opens the next cycle"
    ) in html
    assert all(not (isinstance(x, tuple) and x[0] == "replan") for x in calls)
    c.close()


def _date_run(c, run_id, when):
    """Set the run's opened_at AND planned_start (a run first and last
    planned at `when`), so job start dates can sit before or after it."""
    c.execute(
        "UPDATE index_run SET opened_at = ?, planned_start = ? "
        "WHERE index_run_id = ?", (when, when, run_id),
    )
    c.commit()


# --- v1.29 revision 7 (user ruling 2026-09-29): this cycle's installed finals


def _spy_cuts(monkeypatch, cuts):
    """Record the cycle_cut every engine.plan_index_run call receives (the
    real one still runs)."""
    from magoo import engine

    real = engine.plan_index_run

    def spy(conn, ref, snapshot, **kw):
        cuts.append(kw.get("cycle_cut"))
        return real(conn, ref, snapshot, **kw)

    monkeypatch.setattr(engine, "plan_index_run", spy)


def _plan_rows(c, run_id):
    """{type_id: (target, deficit, runs, jobs, buy)} of a run's rows."""
    return {
        r["type_id"]: (
            r["target_stock_qty"], r["deficit_qty"], r["runs_allocated"],
            r["jobs_allocated"], r["recommended_buy_qty"],
        )
        for r in c.execute(
            "SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,)
        )
    }


def _job_row(page, name):
    """The Industry Jobs table row (<tr>…</tr> body) whose item button is
    `name`."""
    start = page.index(f'data-name="{name}"')
    return page[page.rindex("<tr", 0, start):page.index("</tr>", start)]


def _installed_badge(row):
    """A job row's 'installed N/M' badge as (title, text), or None."""
    found = re.search(
        r'<span class="badge[^"]*"\s+title="([^"]*)">(installed [^<]*)</span>',
        row,
    )
    if found is None:
        return None
    return html_mod.unescape(found.group(1)), found.group(2)


def test_the_esi_update_replans_while_the_final_jobs_install(
    seeded_client, monkeypatch, ref,
):
    """The stop rule is gone (contract §4, user ruling 2026-09-29): an ESI
    update re-plans the open run even with the cycle's Hulk jobs
    installing, passing the run's buying-window cut. Jobs started after
    it are this cycle's wave: with 4 of the 8 installed the Hulk plans the
    other 4 (installed_qty 4 persisted); with all 8 it plans 0 runs. Every
    stage below the Hulk keeps its target and still builds — it is the
    next wave's stock (contract review amendment 3: the R7 proration once
    read an installed wave as "no consumer holds jobs" and zeroed the
    whole chain) — and only this cycle's draw shrinks: once the whole
    wave is installed, the Hulk's own components are built back to their
    target and no more. Nothing is planned twice, nothing collapses.
    (tests/test_live_run.py checks the same figures against a hangar the
    installs drew down.)"""
    hulk = ref.type_id("Hulk")
    run_id = _plan(seeded_client)
    c = _state()
    _date_run(c, run_id, "2026-01-01 00:00:00")
    cuts = []
    _spy_cuts(monkeypatch, cuts)
    calls = []
    _refresh_world(monkeypatch, calls, job_starts={})
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert "run 1 re-planned: " in html
    assert cuts == [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    before = _plan_rows(c, run_id)
    assert before[hulk][2] == 8 and _row(c, run_id, hulk)["installed_qty"] == 0
    below = [t for t, row in before.items() if t != hulk and row[2]]
    assert below, "the Hulk chain plans intermediates"

    calls.clear()
    _refresh_world(
        monkeypatch, calls, in_progress={hulk: 4},
        job_starts={hulk: [["2026-02-01T00:00:00Z", 2], ["2026-02-01T01:00:00Z", 2]]},
    )
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert "run 1 re-planned: " in html and "not re-planned" not in html
    hulk_row = _row(c, run_id, hulk)
    assert hulk_row["installed_qty"] == 4
    assert hulk_row["runs_allocated"] == 4 and hulk_row["deficit_qty"] == 4
    half = _plan_rows(c, run_id)
    for t in below:
        name = ref.type_info(t).name
        assert half[t][0] == before[t][0], name        # target
        assert half[t][2] > 0, name                    # still builds

    calls.clear()
    _refresh_world(
        monkeypatch, calls, in_progress={hulk: 8},
        job_starts={hulk: [["2026-02-01T00:00:00Z", 4], ["2026-02-02T00:00:00Z", 4]]},
    )
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert "run 1 re-planned: " in html
    hulk_row = _row(c, run_id, hulk)
    assert hulk_row["installed_qty"] == 8
    assert hulk_row["runs_allocated"] == 0 and hulk_row["deficit_qty"] == 0
    full = _plan_rows(c, run_id)
    for t in below:
        name = ref.type_info(t).name
        assert full[t][0] == before[t][0], name
        assert full[t][2] > 0, name
    components = {
        m for m, _q in ref.materials(hulk_row["blueprint_id"], hulk_row["activity_id"])
    }
    built = [t for t in below if t in components]
    assert built, "the Hulk draws built components"
    for t in built:
        assert full[t][1] == full[t][0], ref.type_info(t).name  # deficit = target
        assert before[t][1] > before[t][0], ref.type_info(t).name
    assert c.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 1
    c.close()


def test_final_jobs_started_before_the_cut_are_the_previous_wave(
    seeded_client, monkeypatch, ref,
):
    """Jobs started at or before the cut belong to the previous wave: the
    Hulk plans its full 8. The cut is compared PARSED (amendment 6): a job
    started 09:30 UTC on the day the run opened at 10:00 — ESI text that
    sorts AFTER '2026-09-25 10:00:00' — is before it; one in the cut's own
    second is the previous wave's (strict >); of 3 started at 10:30 and 5
    the day before, only the 3 count. A snapshot with no job record (NULL)
    plans the full wave too — and still re-plans, where revision 6's stop
    rule fell back to quantities and skipped."""
    hulk = ref.type_id("Hulk")
    run_id = _plan(seeded_client)
    c = _state()
    _date_run(c, run_id, "2026-09-25 10:00:00")
    calls = []
    for starts, installed in (
        ([["2026-09-25T09:30:00Z", 8]], 0),
        ([["2026-09-25T10:00:00Z", 8]], 0),
        ([["2026-09-25T10:30:00Z", 3], ["2026-09-24T12:00:00Z", 5]], 3),
        (None, 0),
    ):
        calls.clear()
        _refresh_world(monkeypatch, calls, in_progress={hulk: 8},
                       job_starts=None if starts is None else {hulk: starts})
        html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
        assert "run 1 re-planned: " in html, starts
        row = _row(c, run_id, hulk)
        assert row["installed_qty"] == installed, starts
        assert row["runs_allocated"] == 8 - installed, starts
    c.close()


def test_the_cycle_cut_comes_from_the_buying_windows(
    seeded_client, monkeypatch, ref,
):
    """One source for the cut (amendment 6): no run → None (the first
    cycle plans its full wave); an open first run → its opened_at, which
    an in-place re-plan never moves; after Mark executed → that run's
    completed_at, both for ▶ Plan creating the next run and for that
    run's own re-plans. The Plan button and the ESI update both pass it."""
    from magoo import web

    c = _state()
    assert web._cycle_cut(c) is None
    cuts = []
    _spy_cuts(monkeypatch, cuts)
    run_1 = _plan(seeded_client)
    assert cuts == [None]
    _date_run(c, run_1, "2026-03-01 08:00:00")
    opened = datetime(2026, 3, 1, 8, tzinfo=timezone.utc)
    assert web._cycle_cut(c) == opened
    assert _plan(seeded_client) == run_1
    assert cuts[-1] == opened
    assert c.execute(
        "SELECT opened_at FROM index_run WHERE index_run_id = ?", (run_1,)
    ).fetchone()[0] == "2026-03-01 08:00:00"
    assert seeded_client.post(f"/runs/{run_1}/complete").status_code == 302
    c.execute(
        "UPDATE index_run SET completed_at = '2026-03-02 09:15:00' "
        "WHERE index_run_id = ?", (run_1,),
    )
    c.commit()
    done = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
    assert web._cycle_cut(c) == done
    run_2 = _plan(seeded_client)
    assert run_2 != run_1 and cuts[-1] == done
    assert web._cycle_cut(c) == done
    calls = []
    _refresh_world(monkeypatch, calls)
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert "run 2 re-planned: " in html and cuts[-1] == done
    c.close()


def test_the_plan_button_counts_this_cycles_installed_finals(
    seeded_client, ref,
):
    """▶ Plan re-plans from the STORED snapshot with the same cut: the
    Hulk jobs an ESI update recorded after it are this cycle's wave, so
    the re-plan sizes only the rest (revision 6's "▶ Plan re-plans it
    anyway" planned the whole wave again)."""
    hulk = ref.type_id("Hulk")
    run_id = _plan(seeded_client)
    c = _state()
    _date_run(c, run_id, "2026-01-01 00:00:00")
    store.save_esi_snapshot(
        c, {}, {hulk: 6}, {}, 0.0, 0.0, {},
        job_starts={hulk: [["2026-02-01T00:00:00Z", 6]]},
    )
    resp = seeded_client.post("/run", follow_redirects=True)
    assert "index run 1 re-planned in place" in resp.get_data(as_text=True)
    row = _row(c, run_id, hulk)
    assert row["installed_qty"] == 6 and row["runs_allocated"] == 2
    c.close()


def test_industry_jobs_says_how_much_of_the_wave_is_installed(
    seeded_client, monkeypatch, ref,
):
    """Contract §4 / amendment 9: a final with jobs started this cycle
    carries an "installed N/M" badge (M = the cycle quantity) whose
    tooltip reads "N of M already installed this cycle" and names the
    cut; its jobs to run are the rest of the wave. With the whole wave
    installed the row stays in the manufacturing table — 0 jobs, "—"
    cells — rather than vanishing. The deficit dialog gets the installed
    units (data-installed) and its "already installed" row; the "previous
    wave, bound for sale" copy is gone."""
    hulk = ref.type_id("Hulk")
    run_id = _plan(seeded_client)
    c = _state()
    _date_run(c, run_id, "2026-01-01 00:00:00")
    page = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert _installed_badge(_job_row(page, "Hulk")) is None
    assert 'data-installed="0"' in _job_row(page, "Hulk")
    assert "bound for sale" not in page and "previous wave" not in page
    assert "− already installed this cycle" in page

    calls = []
    _refresh_world(monkeypatch, calls, in_progress={hulk: 3},
                   job_starts={hulk: [["2026-02-01T00:00:00Z", 3]]})
    seeded_client.post("/esi/refresh")
    page = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    row = _job_row(page, "Hulk")
    title, text = _installed_badge(row)
    assert text == "installed 3/8"
    assert title == (
        "3 of this cycle's wave of 8 already installed — jobs started "
        "since 2026-01-01 00:00 UTC are this cycle's wave; this row builds "
        "the rest (5)"
    )
    assert 'data-installed="3"' in row and 'data-deficit="5"' in row
    assert 'data-requested="8"' in row and 'data-wave="8"' in row

    calls.clear()
    _refresh_world(monkeypatch, calls, in_progress={hulk: 8},
                   job_starts={hulk: [["2026-02-01T00:00:00Z", 8]]})
    seeded_client.post("/esi/refresh")
    page = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    mfg = page[page.index('data-key="mfg"'):page.index('data-key="reactions"')]
    row = _job_row(mfg, "Hulk")
    title, text = _installed_badge(row)
    assert text == "installed 8/8"
    assert title.startswith("8 of this cycle's wave of 8 already installed —")
    assert title.endswith("this row builds the rest, which is nothing")
    cells = [
        " ".join(re.sub(r"<[^>]+>", " ", body).split())
        for body in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    ]
    # Item, Runs/job, Jobs, Build qty, Time/job
    assert cells[1:5] == ["—", "0", "—", "—"], cells
    assert "this cycle&#39;s wave is already installed" in row or (
        "this cycle's wave is already installed" in row
    )
    # No job, so no per-job ceiling to quote (review 2026-09-29: it read
    # "max 0/job").
    runs_title = re.search(r'<td class="num"[^>]*title="([^"]*)"', row).group(1)
    assert "/job" not in runs_title, runs_title
    c.close()


def test_a_whole_copy_finals_badge_counts_the_copies_it_was_planned_at(
    seeded_client, monkeypatch, ref,
):
    """Review of revision 7 (2026-09-29): a sub-capital final built in
    whole copies counts its wave in those copies once installs exist
    (contract amendment 8) — Hulk x 8 on 10-run copies is a wave of 10.
    The badge's M is that wave (index_run_item.wave_qty), never the
    request, so N never exceeds M: 5 installed read "installed 5/10"
    with the rest (5) adding up, the whole copy "installed 10/10", and
    a manual extra job "10/10" with the surplus named as stock. The
    deficit dialog gets the request and the wave (data-requested,
    data-wave) so its rows sum."""
    hulk = ref.type_id("Hulk")
    c = _state()
    c.execute("UPDATE pipeline SET runs_per_bpc = 10")
    c.commit()
    run_id = _plan(seeded_client)
    _date_run(c, run_id, "2026-01-01 00:00:00")
    before = c.execute(
        "SELECT recommended_build_qty, requested_qty, wave_qty "
        "FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, hulk),
    ).fetchone()
    assert tuple(before) == (10, 8, 8)  # one whole copy; nothing installed

    def installed(n):
        calls = []
        _refresh_world(monkeypatch, calls, in_progress={hulk: n},
                       job_starts={hulk: [["2026-02-01T00:00:00Z", n]]})
        seeded_client.post("/esi/refresh")
        page = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
        mfg = page[page.index('data-key="mfg"'):page.index('data-key="reactions"')]
        return _job_row(mfg, "Hulk")

    row = installed(5)
    title, text = _installed_badge(row)
    assert text == "installed 5/10"
    assert title == (
        "5 of this cycle's wave of 10 already installed (the 8 requested, "
        "counted in the whole blueprint copies they were planned at) — "
        "jobs started since 2026-01-01 00:00 UTC are this cycle's wave; "
        "this row builds the rest (5)"
    )
    for attr in ('data-installed="5"', 'data-requested="8"',
                 'data-wave="10"', 'data-deficit="5"'):
        assert attr in row, attr

    title, text = _installed_badge(installed(10))
    assert text == "installed 10/10"
    assert title.endswith("this row builds the rest, which is nothing")

    title, text = _installed_badge(installed(12))
    assert text == "installed 10/10"
    assert title.endswith(
        "which is nothing; the other 2 installed beyond the wave count as stock"
    )
    c.close()


def test_the_deficit_dialog_names_the_cause_of_a_finals_quantity():
    """The dialog's final branch, run under node when it is on the PATH
    (else only its source is checked). A whole-copy remainder on a
    single-role final is never blamed on other pipelines (review
    2026-09-29: the answer compared the deficit with the target and read
    5 != 2 as a dual-role share), a dual-role draw is, and a run planned
    before the wave columns keeps its target-based rows."""
    import json as json_mod
    import pathlib
    import shutil
    import subprocess

    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "magoo" / "templates" / "_deficit_dialog.html"
    ).read_text(encoding="utf-8")
    script = source[source.index("<script>") + len("<script>"):source.index("</script>")]
    assert "+ rest of the whole copies it was planned at" in script
    assert "− already installed this cycle" in script
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on the PATH")
    script = script.replace(
        "{{ url_for('run_detail', index_run_id=run.index_run_id, view='chain') }}",
        "/chain",
    )
    harness = script + r"""
const els = {};
function node_(tag) {
  return {tag, kids: [], _t: "", className: "", href: "",
    set textContent(v) { this._t = v; this.kids = []; },
    get textContent() { return this._t; },
    appendChild(k) { this.kids.push(k); },
    append(...ks) { this.kids.push(...ks); },
    showModal() {},
    text() { return this._t + this.kids.map(k => k.text()).join(""); }};
}
globalThis.document = {
  getElementById: id => els[id] || (els[id] = node_(id)),
  createElement: tag => node_(tag),
};
const cases = JSON.parse(process.argv[1]);
const out = cases.map(data => {
  openDeficit({dataset: data});
  return {
    rows: document.getElementById("deficit-math").kids.map(
      tr => tr.kids.map(td => td.text())),
    answer: document.getElementById("deficit-answer").textContent,
  };
});
console.log(JSON.stringify(out));
"""
    base = {"kind": "final", "name": "Hulk", "type": "1", "onhand": "0",
            "injobs": "5", "buy": "0", "build": "5", "covered": "0",
            "alchemy": "0", "need": "8"}
    cases = [
        # 8 requested on 10-run copies, 5 installed: builds the rest (5).
        {**base, "target": "8", "deficit": "5", "installed": "5",
         "requested": "8", "wave": "10"},
        # Dual-role: 8 requested + another pipeline drawing 4 from empty
        # stock, all 8 installed: builds the draw (4).
        {**base, "target": "12", "deficit": "4", "installed": "8",
         "requested": "8", "wave": "8", "injobs": "8"},
        # A run planned before the wave columns.
        {**base, "target": "8", "deficit": "5", "installed": "3",
         "requested": "", "wave": ""},
    ]
    done = subprocess.run(
        [node, "-e", harness, json_mod.dumps(cases)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert done.returncode == 0, done.stderr
    whole, dual, legacy = json_mod.loads(done.stdout)
    assert whole["rows"] == [
        ["requested this cycle", "8"],
        ["+ rest of the whole copies it was planned at", "2"],
        ["− already installed this cycle", "5"],
        ["= to build", "5"],
    ]
    assert "whole blueprint copies" in whole["answer"]
    assert "pipelines" not in whole["answer"]
    assert dual["rows"] == [
        ["requested this cycle", "8"],
        ["− already installed this cycle", "8"],
        ["+ other pipelines' draw beyond free stock", "4"],
        ["= to build", "4"],
    ]
    assert "Other pipelines' draw" in dual["answer"]
    assert "whole blueprint copies" not in dual["answer"]
    assert legacy["rows"] == [
        ["cycle quantity", "8"],
        ["− already installed this cycle", "3"],
        ["= to build", "5"],
    ]


def test_the_esi_update_skips_when_there_is_nothing_to_plan_from(
    seeded_client, monkeypatch,
):
    """A4: with no active pipeline the update's re-plan is skipped with
    _plan_inputs' reason — never a plan against a None snapshot — the
    run keeps its plan, and matching still runs."""
    from magoo import buying

    run_id = _plan(seeded_client)
    c = _state()
    _date_run(c, run_id, "2026-01-01 00:00:00")
    c.execute("UPDATE pipeline SET is_active = 0")
    c.commit()
    calls = []
    _refresh_world(monkeypatch, calls)
    _spy_plans(monkeypatch, calls)
    monkeypatch.setattr(
        buying, "assign_purchases",
        lambda conn, ref, settings, now=None: (calls.append("assign"), {})[1],
    )
    monkeypatch.setattr(buying, "summary_line", lambda summary: "purchases: ok")
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert "run 1 not re-planned — no active pipelines — add one first" in html
    assert all(not (isinstance(x, tuple) and x[0] == "replan") for x in calls)
    assert calls[-1] == "assign"
    assert c.execute("SELECT planned_start FROM index_run").fetchone()[0] == (
        "2026-01-01 00:00:00"
    )
    c.close()


def test_the_plan_button_dates_a_replan_at_the_stored_snapshot(seeded_client):
    """Review 2026-09-28: ▶ Plan re-plans from the STORED snapshot, so the
    run's planned_start — costing's pre-plan cut — is that snapshot's
    fetched_at, not now: a purchase made after the snapshot stays
    post-plan (it displaces the direct buy instead of filling the stock
    slice). opened_at does not move."""
    run_id = _plan(seeded_client)
    c = _state()
    opened = c.execute("SELECT opened_at FROM index_run").fetchone()[0]
    c.execute("UPDATE esi_snapshot SET fetched_at = '2026-03-01 09:00:00'")
    c.commit()
    resp = seeded_client.post("/run", follow_redirects=True)
    assert "index run 1 re-planned in place" in resp.get_data(as_text=True)
    row = c.execute(
        "SELECT planned_start, opened_at FROM index_run WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()
    assert tuple(row) == ("2026-03-01 09:00:00", opened)
    line = {"date": "2026-03-01T10:00:00Z"}
    assert not costing._pre_plan(line, costing._when(row["planned_start"]))
    c.close()


def test_a_replan_failure_keeps_the_previous_plan_and_still_matches(
    seeded_client, monkeypatch,
):
    """A4: the re-plan step is guarded like the others — an exception is
    logged and rolled back, the flash names it and the run keeps its plan;
    a ValueError (the run executed or superseded meanwhile) is a skip
    note. Matching runs either way."""
    from magoo import buying, engine

    run_id = _plan(seeded_client)
    c = _state()
    items_before = c.execute(
        "SELECT COUNT(*) FROM index_run_item WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0]
    calls = []
    _refresh_world(monkeypatch, calls)
    monkeypatch.setattr(
        buying, "assign_purchases",
        lambda conn, ref, settings, now=None: (calls.append("assign"), {})[1],
    )
    monkeypatch.setattr(buying, "summary_line", lambda summary: "purchases: ok")

    def boom(conn, ref, snapshot, **kw):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(engine, "plan_index_run", boom)
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert "re-plan failed (engine exploded) — run 1 keeps its previous plan" in html
    assert calls[-1] == "assign" and "purchases: ok" in html
    assert c.execute(
        "SELECT COUNT(*) FROM index_run_item WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] == items_before

    def refused(conn, ref, snapshot, **kw):
        raise ValueError(f"run {kw['replace_index_run_id']} is executed")

    monkeypatch.setattr(engine, "plan_index_run", refused)
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert f"run 1 not re-planned — run {run_id} is executed" in html
    resp = seeded_client.post("/run", follow_redirects=True)
    assert "run not re-planned" in resp.get_data(as_text=True)
    c.close()


def test_reopening_the_newest_run_says_it_is_the_open_run_again(seeded_client):
    """Contract review A6: a reopened newest run is the open run again, so
    the next ESI update re-plans it in place — the flash says so. Marking
    executed says the next Plan opens the next cycle."""
    run_id = _plan(seeded_client)
    html = seeded_client.post(
        f"/runs/{run_id}/complete", follow_redirects=True
    ).get_data(as_text=True)
    assert "▶ Plan index run opens the next cycle" in html
    html = seeded_client.post(
        f"/runs/{run_id}/reopen", follow_redirects=True
    ).get_data(as_text=True)
    assert (
        "it is the open run again — the next ESI update re-plans it from "
        "current stock"
    ) in html


def test_the_slot_copy_no_longer_subtracts_running_jobs(seeded_client):
    """Ruling R2 (contract review A7): Settings no longer says running
    jobs are subtracted; the ESI tab's per-character toggle is a count of
    active jobs only."""
    settings_html = seeded_client.get("/settings").get_data(as_text=True)
    assert "Jobs still running from earlier cycles are subtracted" not in settings_html
    assert "Every plan sizes jobs against the whole pool" in settings_html
    c = _state()
    c.execute(
        "INSERT INTO pool_character (character_id, character_name, "
        "include_assets, include_job_slots, count_assets) "
        "VALUES (90000001, 'Buyer', 0, 0, 0)"
    )
    c.commit()
    c.close()
    chars = seeded_client.get("/characters").get_data(as_text=True)
    assert "Count job slots" not in chars
    assert "Count active jobs</th>" in chars
    assert "planning always sizes jobs against the Settings slot pools" in chars
