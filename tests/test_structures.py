"""v1.9 structures scope: Upwell structures, Standup rigs/modules and
structure components as pipeline finals / chain items — planning against
the live SDE in a temp state DB, plus the Plan/Chain/Profit templates
rendered through the app's Jinja environment with synthetic context (the
web routes have no test client; this proves the new sections compile and
appear).

Anchors (Astrahus 24 items, Keepstar 33) are SDE-build tripwires like the
Hulk 78 — re-baseline on import (decision log 'SDE test tripwires')."""

import sqlite3

import pytest

from magoo import config, costing, engine, store
from magoo.engine import Snapshot

from conftest import FairValuePrices, template_app


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    # XL structures need long jobs; the default 24 h window would need
    # thousands of component jobs (documented dependency, PROJECT.md v1.9).
    c.execute("UPDATE settings SET max_run_duration_hours = 2000")
    c.commit()
    yield c
    c.close()


def add_pipeline(conn, ref, name, qty):
    conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run) VALUES (?, ?, ?)",
        (name, ref.type_id(name), qty),
    )
    conn.commit()


def snapshot(ref, slots=500):
    prices = FairValuePrices(ref)
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=prices,
        adjusted_prices=prices,
    )


def component_items(plan, ref):
    return {
        tid: item
        for tid, item in plan.items.items()
        if ref.type_info(tid).group_id in config.STRUCTURE_COMPONENT_GROUPS
    }


# --- planning --------------------------------------------------------------


def test_astrahus_pipeline_plans_its_chain(conn, ref):
    add_pipeline(conn, ref, "Astrahus", 1)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    assert len(plan.items) == 24  # final + components + minerals/PI (SDE anchor)
    final = plan.items[ref.type_id("Astrahus")]
    # finals are exact — no ship batch multiple for a structure
    assert final.total_runs_needed == 1
    assert final.recommended_build_qty == 1
    assert final.recommended_action == "build"
    comps = component_items(plan, ref)
    assert comps, "structure components missing from the chain"
    for item in comps.values():
        assert item.depth == 1
        assert item.recommended_action == "build"
        assert item.item_class == "structures"
    trit = plan.items[ref.type_id("Tritanium")]
    assert trit.recommended_action == "buy" and trit.recommended_buy_qty > 0


def test_keepstar_is_one_exact_single_run_job(conn, ref):
    add_pipeline(conn, ref, "Keepstar", 1)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    assert len(plan.items) == 33  # SDE anchor
    final = plan.items[ref.type_id("Keepstar")]
    assert final.total_runs_needed == 1  # never rounded to ship_batch_multiple (8)
    assert final.jobs_allocated == 1
    assert final.recommended_build_qty == 1
    assert final.item_class == "structures"
    # ~500 h per run at TE20 / all-V / NPC: a multi-cycle job under the
    # default 24 h window, planned as max(1, floor(window / time)) runs
    assert final.time_per_run == pytest.approx(3_500_000 * 0.8 * 0.80 * 0.85 * 0.95)


def test_shared_components_merge_across_structure_pipelines(conn, ref):
    hangar = ref.type_id("Structure Hangar Array")
    add_pipeline(conn, ref, "Astrahus", 1)
    alone = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    astrahus_need = alone.items[hangar].merged_min_qty
    add_pipeline(conn, ref, "Raitaru", 1)
    both = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    conn.execute("DELETE FROM pipeline WHERE name = 'Astrahus'")
    conn.commit()
    raitaru_only = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    assert both.items[hangar].merged_min_qty == (
        astrahus_need + raitaru_only.items[hangar].merged_min_qty
    )


def test_structures_class_setting_flows_into_planning(conn, ref):
    """The 'structures' class row governs structure components: a Sotiyo
    with T2 rigs in nullsec must cut their mineral demand versus the NPC
    default (and leave the final's own run count alone)."""
    add_pipeline(conn, ref, "Astrahus", 1)
    npc = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    conn.execute(
        "UPDATE class_setting SET structure_type_id = ?, security = -0.5, "
        "me_rig = 't2', te_rig = 't2' WHERE item_class = 'structures'",
        (config.STRUCTURE_TYPE_SOTIYO,),
    )
    conn.commit()
    rigged = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    trit = ref.type_id("Tritanium")
    assert rigged.items[trit].merged_min_qty < npc.items[trit].merged_min_qty
    assert rigged.items[ref.type_id("Astrahus")].total_runs_needed == 1


def test_plan_persists_region_wide_price_provenance(conn, ref):
    """Snapshot.region_wide marks which price_snapshot values were fallback
    quotes; the engine persists it per item so the Buy list badges the
    price it shows, not today's cache (review finding, 2026-08-22)."""
    add_pipeline(conn, ref, "Astrahus", 1)
    # persist=True needs an index_run: a minimal ESI state row
    store.save_esi_snapshot(conn, {}, {}, {1: 0, 11: 0}, 0.0, 0.0, job_ends={})
    trit = ref.type_id("Tritanium")
    prices = FairValuePrices(ref)
    snap = engine.snapshot_from_state(
        conn, prices=prices, adjusted=prices, region_wide={trit}
    )
    assert snap is not None and snap.region_wide == {trit}
    plan = engine.plan_index_run(conn, ref, snap, persist=True)
    assert plan.items[trit].price_region_wide is True
    hangar = ref.type_id("Structure Hangar Array")
    assert plan.items[hangar].price_region_wide is False
    rows = {
        r["type_id"]: r["price_region_wide"]
        for r in conn.execute(
            "SELECT type_id, price_region_wide FROM index_run_item "
            "WHERE index_run_id = ?", (plan.index_run_id,)
        )
    }
    assert rows[trit] == 1 and rows[hangar] == 0


def test_rig_pipeline_plans_exact_quantity(conn, ref):
    name = "Standup M-Set Structure Manufacturing Material Efficiency I"
    add_pipeline(conn, ref, name, 10)
    plan = engine.plan_index_run(conn, ref, snapshot(ref), persist=False)
    final = plan.items[ref.type_id(name)]
    assert final.total_runs_needed == 10  # exact, no batch multiple
    assert final.item_class == "structures"


# --- costing ---------------------------------------------------------------


def test_current_hull_cost_for_astrahus_marks_region_priced_lines(conn, ref):
    add_pipeline(conn, ref, "Astrahus", 1)
    pipeline = conn.execute("SELECT * FROM pipeline").fetchone()
    prices = FairValuePrices(ref)
    trit = ref.type_id("Tritanium")
    cost = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), pipeline, prices, prices,
        region_wide=frozenset({trit}),
    )
    assert cost.total > 0 and cost.missing_prices == 0
    assert cost.region_priced == 1
    line = next(l for l in cost.lines if l.type_id == trit)
    assert line.region_price and line.kind == "material"
    # install lines exist for the final and every component stage
    installs = {l.type_id for l in cost.lines if l.kind == "install"}
    assert ref.type_id("Astrahus") in installs
    assert ref.type_id("Structure Hangar Array") in installs


# --- templates -------------------------------------------------------------


_app = template_app


# --- run-tab helpers (route logic that the render tests feed in as context) --


def test_display_category_labels(ref):
    from magoo.web import (
        STRUCTURE_COMPONENTS_LABEL, STRUCTURE_MODULES_LABEL, STRUCTURES_LABEL,
        _CATEGORY_RANK, _UNRANKED, _display_category,
    )

    def label(name):
        info = ref.type_info(ref.type_id(name))
        group = ref.conn.execute(
            "SELECT name FROM ref_group WHERE group_id = ?", (info.group_id,)
        ).fetchone()["name"]
        return _display_category(ref, info.type_id, info.group_id,
                                 info.category_id, group)

    assert label("Keepstar") == STRUCTURES_LABEL
    assert label("Metenox Moon Drill") == STRUCTURES_LABEL
    assert label("Standup M-Set Structure Manufacturing Material Efficiency I") == STRUCTURE_MODULES_LABEL
    assert label("Standup Cloning Center I") == STRUCTURE_MODULES_LABEL
    assert label("Structure Construction Parts") == STRUCTURE_COMPONENTS_LABEL
    assert label("Hulk") == "T2/T3 Subcapital Ships"
    assert label("Revelation") == "T1 Capital Ships"
    assert label("Capital Propulsion Engine") == "Capital Construction Components"
    # ranks: ships, then structures, then alphabetical EVE groups; reaction
    # groups rank among themselves; components fall to the unranked block
    assert _CATEGORY_RANK[STRUCTURES_LABEL] < _CATEGORY_RANK[STRUCTURE_MODULES_LABEL] < _UNRANKED
    assert _CATEGORY_RANK["T2/T3 Subcapital Ships"] < _CATEGORY_RANK[STRUCTURES_LABEL]
    assert STRUCTURE_COMPONENTS_LABEL not in _CATEGORY_RANK


def test_chain_status_follows_the_plans_decision():
    """The Chain tab badge is the plan's decision, not the row's shape: a
    buildable intermediate the savings rule flipped to buy reads 'buy'."""
    from magoo.web import _chain_status

    base = {"alchemy_route": False, "deficit": 10, "activity_id": 1,
            "build_qty": 0, "buy_qty": 0, "alchemy_out": 0,
            "capacity_limited": False}
    assert _chain_status({**base, "buy_qty": 10}) == "buy"  # negative savings
    assert _chain_status({**base, "build_qty": 10}) == "build"
    assert _chain_status({**base, "build_qty": 10, "activity_id": 11}) == "react"
    # capacity loser: built in part, the rest bought — stays build
    assert _chain_status({**base, "build_qty": 6, "buy_qty": 4}) == "build"
    assert _chain_status({**base}) == "unmet"  # deficit, no jobs, no market
    # a composite whose deficit the unrefined (alchemy) route covers
    assert _chain_status({**base, "alchemy_out": 10}) == "alchemy"
    assert _chain_status({**base, "deficit": 0}) == "covered"
    assert _chain_status({**base, "alchemy_route": True}) == "alchemy"
    # +unmet: capacity-limited, partly built, nothing bought (starved final /
    # unpriced intermediate) — the Plan tab's Unmet definition
    from magoo.web import _chain_short
    assert _chain_short({**base, "build_qty": 5, "capacity_limited": True}) is True
    assert _chain_short({**base, "build_qty": 5, "buy_qty": 5, "capacity_limited": True}) is False
    assert _chain_short({**base, "build_qty": 5}) is False
    assert _chain_short({**base, "capacity_limited": True}) is False  # status 'unmet' covers it


def test_split_structure_components_partition():
    from magoo.web import _split_structure_components

    comp_built = {"group_id": 536, "name": "Structure Hangar Array", "jobs_allocated": 2}
    comp_bought = {"group_id": 536, "name": "Structure Construction Parts"}
    hull = {"group_id": 1657, "name": "Astrahus", "jobs_allocated": 1}
    mineral = {"group_id": 18, "name": "Tritanium"}
    built, bought, other = _split_structure_components(
        [hull, comp_built], [mineral, comp_bought]
    )
    assert built == [comp_built] and bought == [comp_bought] and other == [hull]
    # slot totals must sum over the unsplit list (the split is display-only)
    assert sum(i["jobs_allocated"] for i in [hull, comp_built]) == 3


def _item(name, type_id, group_id, **over):
    base = {
        "name": name,
        "type_id": type_id,
        "group_id": group_id,
        "category": "Structure Components",
        "recommended_buy_qty": 0,
        "price_snapshot": 1000.0,
        "capacity_limited": 0,
        "runs_allocated": 4,
        "jobs_allocated": 2,
        "max_runs_per_job": 2,
        "recommended_build_qty": 4,
        "time_per_run": 3600.0,
        "low_stock": 0,
        "savings_unpriced_inputs": 0,
        "deficit_qty": 4,
        # v1.10 venue provenance (hub / unpriced rows carry no depth figure)
        "buy_venue": "hub",
        "structure_units_cheaper": None,
    }
    base.update(over)
    return base


def test_run_detail_template_renders_structure_components_section(ref):
    from flask import render_template

    app = _app()
    hangar = _item("Structure Hangar Array", ref.type_id("Structure Hangar Array"), 536)
    parts = _item(
        "Structure Construction Parts", ref.type_id("Structure Construction Parts"),
        536, recommended_buy_qty=12, jobs_allocated=0, runs_allocated=0,
        recommended_build_qty=0,
    )
    trit = _item("Tritanium", ref.type_id("Tritanium"), 18,
                 category="Mineral", recommended_buy_qty=5000,
                 jobs_allocated=0, runs_allocated=0, recommended_build_qty=0)
    astrahus = _item("Astrahus", ref.type_id("Astrahus"), 1657,
                     category="Citadel", runs_allocated=1, jobs_allocated=1,
                     max_runs_per_job=1, recommended_build_qty=1)
    run = {"run_number": 99, "status": "planned", "planned_start": "2026-08-22",
           "index_run_id": 1, "wallet_character_isk": 1e9,
           "wallet_corporation_isk": 2e9, "completed_at": None}
    ctx = dict(
        run=run, items=[hangar, parts, trit, astrahus], final_net_margin={},
        buys=[trit, parts], builds=[astrahus], reactions=[],
        builds_grouped=[("Upwell Structures", [astrahus])], reactions_grouped=[],
        struct_builds=[hangar], struct_buys=[parts], struct_slots=2,
        chain_struct=[], alchemy=[], alchemy_yield=0.55, chain_rows=[],
        chain_raws=[], chain_mfg=[], chain_reactions=[],
        chain_counts={"covered": 0, "buy": 0, "build": 0, "react": 0, "alchemy": 0},
        unmet=[], low_stock=[], buy_total=5000 * 1000.0 + 12 * 1000.0,
        buys_unpriced=0,
        multibuy_hub="Tritanium 5000\nStructure Construction Parts 12",
        multibuy_structure="", structure_buys=set(), shallow=set(),
        settings=store.Settings(0.05, 24.0, 8, 1, 10000002, "sell",
                                manufacturing_slots=50, reaction_slots=50),
        mfg_slots_used=3, reaction_slots_used=0, alchemy_slots_used=0,
        region_wide={trit["type_id"]},
    )
    with app.test_request_context("/runs/1"):
        html = render_template("run_detail.html", **ctx)
    assert "Structure components" in html
    assert "1 built" in html and "1 bought" in html
    assert "Bought this cycle" in html
    assert "Upwell Structures" in html
    assert "region price" in html  # Tritanium priced region-wide at plan time
    assert "Structure comps" in html  # totals strip stat
    assert "cheaper to buy than" in html


def test_run_chain_template_renders_structure_components_section(ref):
    from flask import render_template

    app = _app()
    row = lambda name, gid, status, buildable=True, activity=1: {
        "name": name, "group": "x", "group_id": gid, "cycle_need": 4,
        "target": 4, "on_hand": 0, "in_jobs": 0, "deficit": 4,
        "status": status, "alchemy_credit": 0, "buildable": buildable,
        "activity_id": activity, "category": "c", "depth": 1,
        "build_qty": 4 if status == "build" else 0,
        "buy_qty": 4 if status == "buy" else 0, "alchemy_out": 0,
        "capacity_limited": False, "short": False,
    }
    hangar = row("Structure Hangar Array", 536, "build")
    trit = row("Tritanium", 18, "buy", buildable=False, activity=None)
    # a capacity loser partly bought, a starved final partly built, an
    # unmet row — the new Chain-tab markup branches
    partial = {**row("Capital Armor Plates", 873, "build"), "build_qty": 6, "buy_qty": 4}
    starved = {**row("Astrahus", 1657, "build"), "build_qty": 5, "deficit": 8,
               "capacity_limited": True, "short": True}
    unmet = {**row("Structure Laboratory", 536, "unmet"), "build_qty": 0}
    run = {"run_number": 99, "status": "planned", "planned_start": "2026-08-22",
           "index_run_id": 1}
    ctx = dict(
        run=run, chain_rows=[hangar, trit, partial, starved, unmet], chain_raws=[trit],
        chain_mfg=[("Upwell Structures", [starved, unmet]),
                   ("Advanced Capital Construction Components", [partial])],
        chain_reactions=[], chain_struct=[hangar],
        chain_counts={"covered": 0, "buy": 1, "build": 3, "react": 0, "alchemy": 0,
                      "unmet": 2},
    )
    with app.test_request_context("/runs/1?view=chain"):
        html = render_template("run_chain.html", **ctx)
    assert "Structure components" in html
    assert "Structure comps" in html
    assert "Structure Hangar Array" in html
    assert "raw input — bought just-in-time: 4" in html  # Tritanium tooltip
    assert ">+buy</span>" in html and "4 of the deficit is bought" in html
    assert ">+unmet</span>" in html and "only 5 of the 8 deficit is planned" in html
    assert ">unmet</span>" in html
    assert "no purchase fallback (pipeline finals are never bought" in html
    assert "Unmet" in html and ">2</span>" in html  # header stat


def test_profit_template_renders_region_priced_badge_and_unit_wording(conn, ref):
    from flask import render_template

    add_pipeline(conn, ref, "Astrahus", 1)
    pipeline = conn.execute("SELECT * FROM pipeline").fetchone()
    prices = FairValuePrices(ref)
    trit = ref.type_id("Tritanium")
    settings = store.get_settings(conn)
    cost = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices,
        region_wide=frozenset({trit}),
    )
    card = {"pipeline": pipeline, "name": "Astrahus", "cost": cost,
            "price": cost.total * 1.3, "net": cost.total * 1.2, "capital": False,
            "margin": cost.total * 0.2}
    app = _app()
    with app.test_request_context("/profit"):
        html = render_template(
            "planning_profit.html", cards=[card], totals=costing.cycle_totals([card]),
            prices_at=None, broker_rate=0.01, sales_tax=0.03, settings=settings,
        )
    assert "1 region-priced" in html
    assert "<th>Product</th>" in html and "Cost / unit" in html
    assert "Units / cycle" in html
    assert "region price" in html  # breakdown line badge
