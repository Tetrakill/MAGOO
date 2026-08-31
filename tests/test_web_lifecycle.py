"""Request-level run-lifecycle coverage: POST /run planning, mark-executed /
reopen (the backbone of lag costing — completed_sequence is the timeline the
cost walk prices from), the older-run splice guard, the drive-by-POST Origin
guard, and the settings save's numeric hardening.

Uses conftest.seeded_client: a populated app (real SDE attached read-only,
seeded price cache + ESI snapshot, one Hulk x 8 pipeline) — see the fixture
docstring for the wiring.
"""

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
        "batch": "8",
        "extra_runs": "1",
        "region": "10000002",
        "source": "sell",
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
