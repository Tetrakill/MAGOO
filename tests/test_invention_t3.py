"""T3 (relic) invention + generalized multi-source picker (2026-08-31).

Worked example throughout: Tengu Offensive - Accelerated Ejection Bay
(subsystem, invented BP 45696) from the Weapon Subroutines relics
30628/30632/30633 (Intact 0.26/20 · Malfunctioning 0.21/10 · Wrecked
0.14/3), datacores Plasma Physics (20412) x3 + Offensive Subsystems
Engineering (20425) x3; and the Tengu hull (invented BP 29985) from the
Hull Section relics 30752/30753/30754. All-V skills give the chance factor
1.458333.
"""

import math
import sqlite3

import pytest

from magoo import config, costing, engine, industry, store

from test_invention import (
    ZEALOT_P_NONE,
    UniformDict,
    _state,
    add_pipeline,
    decryptor_named,
    enable_invention,
    get_pipeline,
    rich_snapshot,
    uniform_snapshot,
)

SUBSYSTEM = "Tengu Offensive - Accelerated Ejection Bay"
INTACT_WS, MALF_WS, WRECKED_WS = 30628, 30632, 30633  # Weapon Subroutines
INTACT_HULL, MALF_HULL = 30752, 30753  # Hull Sections
P_INTACT = 0.26 * (1 + 10 / 30 + 5 / 40)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


# --- refdata: source resolution ---------------------------------------------


def test_sources_ordered_intact_first(ref):
    sources = ref.invention_sources_for_product(ref.type_id("Tengu"))
    assert [s.t1_blueprint_id for s in sources] == [30752, 30753, 30754]
    assert [s.probability for s in sources] == [0.26, 0.21, 0.14]
    assert [s.runs for s in sources] == [20, 10, 3]


def test_chosen_source_resolution(ref):
    tengu = ref.type_id("Tengu")
    assert ref.invention_source_for_product(tengu) is None  # no choice
    chosen = ref.invention_source_for_product(tengu, INTACT_HULL)
    assert chosen is not None and chosen.runs == 20
    assert ref.invention_source_for_product(tengu, 999999) is None  # junk
    # Single-source finals ignore a bogus stored choice (drift healing).
    zealot = ref.type_id("Zealot")
    assert ref.invention_source_for_product(zealot, 999999) is not None


def test_multi_t1_sources_deterministic(ref):
    """The seven multi-T1-source T2 targets: equal probabilities fall back
    to blueprint_id order."""
    sources = ref.invention_sources_for_product(
        ref.type_id("ElectroPunch Ultra S")
    )
    assert len(sources) == 2
    assert [s.t1_blueprint_id for s in sources] == sorted(
        s.t1_blueprint_id for s in sources
    )
    assert all(not ref.is_relic_source(s.t1_blueprint_id) for s in sources)


def test_is_relic_source(ref):
    assert ref.is_relic_source(INTACT_WS)
    assert ref.is_relic_source(INTACT_HULL)
    assert not ref.is_relic_source(2007)  # Omen Blueprint


# --- classification ----------------------------------------------------------


def test_subsystems_classify_t2_ships(ref):
    """Category 32 joins t2_ships (CCP rig filter 8 spans it)."""
    assert (
        industry.classify_item(ref, ref.type_id(SUBSYSTEM), 1) == "t2_ships"
    )
    assert industry.classify_item(ref, ref.type_id("Tengu"), 1) == "t2_ships"


# --- costing: relic deltas ---------------------------------------------------


def test_relic_invention_cost_hand_computed(conn, ref):
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    source = ref.invention_source_for_product(
        ref.type_id(SUBSYSTEM), INTACT_WS
    )
    cost = costing.invention_cost(
        ref, settings, class_settings, source, None,
        price_of=lambda t: 100_000.0, adjusted_of=lambda t: 10.0,
    )
    assert cost.probability == pytest.approx(P_INTACT)
    assert (cost.me, cost.te, cost.runs_per_copy) == (2, 4, 20)
    # Relic fee base: 2% of the INVENTED blueprint's product manufacturing
    # EIV (decision 2026-08-31); NPC lab defaults -> x(tax .25% + SCC 4%).
    eiv = sum(
        qty * 10.0
        for _m, qty in ref.materials(
            source.product_blueprint_id, config.ACTIVITY_MANUFACTURING
        )
    )
    assert eiv > 0
    assert cost.invention_fee == pytest.approx(0.02 * eiv * 0.0425)
    assert cost.copy_fee == 0.0  # a relic is consumed — no copy job
    # The relic rides the datacores tuple as one extra consumable.
    assert cost.datacores[-1] == (INTACT_WS, 1, 100_000.0)
    assert len(cost.datacores) == 3  # 2 datacore types + the relic
    attempt = 6 * 100_000.0 + 100_000.0 + cost.invention_fee
    assert cost.attempt_cost == pytest.approx(attempt)
    assert cost.cost_per_run == pytest.approx(attempt / (P_INTACT * 20))


def test_relic_with_attainment_decryptor(conn, ref):
    source = ref.invention_source_for_product(
        ref.type_id(SUBSYSTEM), INTACT_WS
    )
    cost = costing.invention_cost(
        ref,
        store.get_settings(conn),
        store.get_class_settings(conn),
        source,
        decryptor_named(ref, "Attainment Decryptor"),
        price_of=lambda t: None,
        adjusted_of=lambda t: None,
    )
    assert cost.probability == pytest.approx(P_INTACT * 1.8)
    assert (cost.me, cost.te, cost.runs_per_copy) == (1, 8, 24)
    # 2 datacores + relic + decryptor all unpriced, fees 0 (no adjusted).
    assert cost.unpriced == 4
    assert cost.attempt_cost == 0.0


# --- engine ------------------------------------------------------------------


def test_relic_pipeline_persists_vintage(conn, ref):
    """v1.23: the run persists the relic vintage (copy fee 0, relic in
    the datacores JSON) but injects NO buy rows — the Invention tab owns
    purchasing."""
    pid = add_pipeline(conn, ref, SUBSYSTEM, 5)
    enable_invention(conn, ref, pid, SUBSYSTEM, source_id=INTACT_WS)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=True)

    final = plan.items[ref.type_id(SUBSYSTEM)]
    assert final.runs_allocated == 5  # subsystems never batch-round
    row = plan.invention[pid]
    assert row["t1_blueprint_id"] == INTACT_WS
    assert row["runs_per_copy"] == 20
    assert row["probability"] == pytest.approx(P_INTACT)
    assert row["copy_fee_per_attempt"] == 0.0

    assert INTACT_WS not in plan.items  # no relic buy row in the run
    persisted = conn.execute(
        "SELECT datacores FROM index_run_invention WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchone()[0]
    assert f"[{INTACT_WS}, 1, " in persisted


def test_tengu_hull_builds_whole_copies(conn, ref):
    """runs_per_bpc is the ship batch unit: an Intact Tengu pipeline
    builds in 20-hull batches (settled build-whole-copies contract)."""
    pid = add_pipeline(conn, ref, "Tengu", 2)
    enable_invention(conn, ref, pid, "Tengu", source_id=INTACT_HULL)
    assert get_pipeline(conn, pid)["runs_per_bpc"] == 20
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    tengu = plan.items[ref.type_id("Tengu")]
    assert tengu.total_runs_needed == 20
    assert tengu.runs_allocated == 20
    row = plan.invention[pid]
    assert row["runs_per_copy"] == 20
    assert row["t1_blueprint_id"] == INTACT_HULL


def test_stale_source_choice_falls_back(conn, ref):
    pid = add_pipeline(conn, ref, "Tengu", 2, runs_per_bpc=4, bpc_cost=40e6)
    enable_invention(conn, ref, pid, "Tengu", source_id=INTACT_HULL)
    conn.execute(
        "UPDATE pipeline SET invention_source_blueprint_id = 999999 "
        "WHERE pipeline_id = ?",
        (pid,),
    )
    conn.commit()
    assert engine._invention_configs(conn, ref) == {}
    cost = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), get_pipeline(conn, pid),
        UniformDict(10.0), UniformDict(10.0),
    )
    bpc = [l for l in cost.lines if l.kind == "bpc"]
    # bpc fallback divides by the STASHED manual runs (4), not the
    # materialized 20.
    assert len(bpc) == 1 and bpc[0].cost_per_hull == pytest.approx(10e6)
    assert not [l for l in cost.lines if l.kind == "invention"]


def test_demand_ids_cover_all_relic_tiers(conn, ref):
    add_pipeline(conn, ref, "Tengu", 2)  # capable, not enabled
    conn.execute("UPDATE pipeline SET is_active = 0")
    conn.commit()
    ids = engine.demand_type_ids(conn, ref)
    assert {30752, 30753, 30754} <= ids  # every relic tier
    assert {20412, 20424} <= ids  # Plasma Physics + Mechanical Engineering
    assert {d.type_id for d in ref.decryptors()} <= ids
    # Relic fee base: the invented blueprint's own manufacturing mats.
    source = ref.invention_source_for_product(
        ref.type_id("Tengu"), INTACT_HULL
    )
    product_mats = {
        m
        for m, _q in ref.materials(
            source.product_blueprint_id, config.ACTIVITY_MANUFACTURING
        )
    }
    assert product_mats <= ids


# --- costing: both hull-cost views -------------------------------------------


def test_hull_cost_replays_relic_line(conn, ref):
    pid = add_pipeline(conn, ref, SUBSYSTEM, 5)
    enable_invention(conn, ref, pid, SUBSYSTEM, source_id=INTACT_WS)
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(10.0))
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    cost = costing.hull_cost(
        conn, ref, store.get_settings(conn), plan.index_run_id, pid
    )
    inv = [l for l in cost.lines if l.kind == "invention"]
    # 2 datacores + relic + invention fee — NO copy-fee line (fee is 0).
    assert len(inv) == 4
    assert not [l for l in inv if l.name == "T1 copy fee"]
    # v1.23 replay: continuous expected consumption from the vintage.
    relic_line = next(l for l in inv if l.type_id == INTACT_WS)
    assert relic_line.qty_per_hull == pytest.approx(1 / (P_INTACT * 20))
    assert relic_line.unit_cost == 10.0


def test_current_hull_cost_live_relic_expectation(conn, ref):
    pid = add_pipeline(conn, ref, SUBSYSTEM, 5)
    enable_invention(conn, ref, pid, SUBSYSTEM, source_id=INTACT_WS)
    cost = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), get_pipeline(conn, pid),
        UniformDict(10.0), UniformDict(10.0),
    )
    inv = [l for l in cost.lines if l.kind == "invention"]
    relic_line = next(l for l in inv if l.type_id == INTACT_WS)
    assert relic_line.qty_per_hull == pytest.approx(1 / (P_INTACT * 20))
    assert not [l for l in cost.lines if l.kind == "bpc"]


# --- web lifecycle -----------------------------------------------------------


def test_web_t3_lifecycle(seeded_client, ref):
    seeded_client.post("/pipelines", data={"products": "Tengu\t2\t10"})
    c = _state()
    pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Tengu'"
    ).fetchone()["pipeline_id"]

    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert "Intact Hull Section" in page  # the source select renders
    assert "Wrecked Hull Section" in page

    # Enable: Intact + Attainment -> 24-run ME1/TE8 copies, source stored.
    attainment = decryptor_named(ref, "Attainment Decryptor")
    resp = seeded_client.post(
        f"/pipelines/{pid}/invention",
        data={"source": str(INTACT_HULL), "decryptor": str(attainment.type_id)},
    )
    assert resp.status_code == 302
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["use_invention"] == 1
    assert row["invention_source_blueprint_id"] == INTACT_HULL
    assert row["runs_per_bpc"] == 24
    assert row["manual_runs_per_bpc"] == 10
    bs = c.execute(
        "SELECT * FROM blueprint_setting WHERE blueprint_id = 29985"
    ).fetchone()
    assert (bs["me_level"], bs["te_level"]) == (1, 8)

    # Change the tier: re-materializes, stash kept.
    seeded_client.post(
        f"/pipelines/{pid}/invention",
        data={"source": str(MALF_HULL), "decryptor": str(attainment.type_id)},
    )
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["invention_source_blueprint_id"] == MALF_HULL
    assert row["runs_per_bpc"] == 14  # 10 + Attainment's +4
    assert row["manual_runs_per_bpc"] == 10

    # Invalid source on a multi-source final: nothing saved.
    seeded_client.post(
        f"/pipelines/{pid}/invention",
        data={"source": "12345", "decryptor": str(attainment.type_id)},
    )
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["invention_source_blueprint_id"] == MALF_HULL

    # Off: stash restored, source cleared.
    seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "off"}
    )
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["use_invention"] == 0
    assert row["runs_per_bpc"] == 10
    assert row["invention_source_blueprint_id"] is None

    # Off again while already off (source-select posts decryptor=off):
    # must NOT wipe the manual runs.
    seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "off"}
    )
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["runs_per_bpc"] == 10
    c.close()


def test_web_t3_relic_wording_on_tab_and_run(seeded_client, ref):
    seeded_client.post("/pipelines", data={"products": "Tengu\t1"})
    c = _state()
    pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Tengu'"
    ).fetchone()["pipeline_id"]
    seeded_client.post(
        f"/pipelines/{pid}/invention",
        data={"source": str(INTACT_HULL), "decryptor": "none"},
    )
    # The live Invention tab carries the relic workbench wording.
    tab = seeded_client.get("/invention").get_data(as_text=True)
    assert "Intact Hull Section" in tab
    assert "no copy job" in tab
    # The run pages show no invention information at all (user ruling
    # 2026-09-01); the relic is not a buy row either.
    seeded_client.post("/run")
    run_id = c.execute(
        "SELECT MAX(index_run_id) AS id FROM index_run"
    ).fetchone()["id"]
    detail = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "amortized cost" not in detail
    assert "Intact Hull Section" not in detail
    assert "T1 copy fee" not in detail
    assert "Consume" not in detail  # production checklist lives on the tab
    c.close()
