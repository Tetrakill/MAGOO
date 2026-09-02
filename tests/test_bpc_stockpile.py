"""v1.23 BPC stockpile overbuild: the two settings, the live Invention
tab's target/netting math (engine.invention_stockpile), and the amortized
realized-cost invariance.

Zealot worked example (all-V, no decryptor): P = 0.3792, 1-run copies.
Cycle qty 2 -> 2 copies/cycle; 400%% overbuild -> target 8.
"""

import json
import math
import sqlite3

import pytest

from magoo import config, costing, engine, store

from test_invention import (
    ZEALOT_P_NONE,
    add_pipeline,
    decryptor_named,
    enable_invention,
    rich_snapshot,
    uniform_snapshot,
    _state,
)
from test_web_lifecycle import _settings_form

ZEALOT_BP = 12004  # the invented blueprint type
OMEN_BP = 2007  # the T1 source blueprint type
CELESTIS_BP = 978  # shared T1 source of Arazu and Lachesis
INTACT_HULL = 30752


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


def test_settings_defaults(conn):
    settings = store.get_settings(conn)
    assert settings.t1_bpc_overbuild == pytest.approx(4.0)
    assert settings.t2_bpc_overbuild == pytest.approx(4.0)


def section(data, name):
    return next(s for s in data["sections"] if s["product_name"] == name)


def buy(data, type_id):
    return next(b for b in data["buys"] if b["type_id"] == type_id)


def with_margin(conn, need):
    """The tab's gross need: attempts-scaled need plus the Raw Material
    Buffer, like every other bought input (review 2026-09-01)."""
    margin = store.get_settings(conn).input_purchase_margin
    return math.ceil(round(need * (1.0 + margin), 9))


def test_stockpile_targets_and_netting(conn, ref):
    """Target 8 (2/cycle x 400%), 5 stocked, 3 in-flight attempts -> 1
    expected copy -> invent 2 -> 6 attempts; T1 stack-minus-one plus
    copy-job in-flight credit."""
    pid = add_pipeline(conn, ref, "Zealot", 2)
    enable_invention(conn, ref, pid, "Zealot")
    snap = rich_snapshot(ref)
    snap.on_hand[ZEALOT_BP] = 5
    snap.in_progress[ZEALOT_BP] = 3  # invention attempts in flight
    snap.on_hand[OMEN_BP] = 5  # 1 BPO + 4 copies (stack minus one)
    snap.in_progress[OMEN_BP] = 3  # copy jobs in flight
    data = engine.invention_stockpile(conn, ref, snap)

    s = section(data, "Zealot")
    assert s["cycle_copies"] == 2
    assert s["target"] == 8
    assert s["from_stock"] == 5
    assert s["from_flight"] == 1  # floor(3 x 0.3792)
    assert s["to_invent"] == 2
    assert s["attempts"] == math.ceil(2 / ZEALOT_P_NONE)  # 6
    assert not s["covered"]

    # T1 side is in licensed RUNS: copies hold the blueprint's max runs
    # (stocked and planned alike), one run per attempt.
    t1 = s["t1"]
    max_runs = ref.max_runs(OMEN_BP)
    assert t1["max_runs"] == max_runs >= 1
    assert t1["target"] == 24  # 6 attempts x 400%
    assert t1["from_stock"] == min(24, 4 * max_runs)  # 4 copies × max
    remaining = 24 - t1["from_stock"]
    assert t1["from_flight"] == min(remaining, 3)  # 3 runs copying
    assert t1["runs_to_make"] == remaining - t1["from_flight"]
    assert t1["to_make"] == math.ceil(t1["runs_to_make"] / max_runs)
    assert any(c["pipeline_id"] == pid for c in data["copy_jobs"]) == (
        t1["to_make"] > 0
    )
    # Invention jobs group like copies: one attempt per run, up to the
    # T1 copy's max runs per job.
    assert s["max_runs"] == max_runs
    assert s["jobs"] == math.ceil(s["attempts"] / max_runs)

    # Datacore demand scales with attempts (8 each x 6) plus the purchase
    # margin, netted once; hub buys are never shallow.
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    for material_id, qty in ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    ):
        assert buy(data, material_id)["gross"] == with_margin(conn, qty * 6)
        assert buy(data, material_id)["to_buy"] == with_margin(conn, qty * 6)
        assert buy(data, material_id)["shallow"] is False
    assert data["buys_shallow"] == 0
    assert data["buy_total"] > 0
    assert "Datacore" in data["multibuy_hub"]


def test_stock_covered_pipeline(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2)
    enable_invention(conn, ref, pid, "Zealot")
    snap = rich_snapshot(ref)
    snap.on_hand[ZEALOT_BP] = 8
    data = engine.invention_stockpile(conn, ref, snap)
    s = section(data, "Zealot")
    assert s["covered"] and s["attempts"] == 0 and s["to_invent"] == 0
    assert s["t1"]["to_make"] == 0
    assert data["buys"] == []
    assert data["copy_jobs"] == []
    assert data["total_outlay"] == 0


def test_100_percent_is_plain_cycle_need(conn, ref):
    conn.execute(
        "UPDATE settings SET t1_bpc_overbuild = 1.0, t2_bpc_overbuild = 1.0"
    )
    conn.commit()
    pid = add_pipeline(conn, ref, "Zealot", 2)
    enable_invention(conn, ref, pid, "Zealot")
    data = engine.invention_stockpile(conn, ref, rich_snapshot(ref))
    s = section(data, "Zealot")
    assert s["target"] == s["cycle_copies"] == 2
    assert s["t1"]["target"] == s["attempts"]


def test_shared_t1_source_pool_never_double_credited(conn, ref):
    """Arazu and Lachesis both invent from the Celestis Blueprint: the
    copy stack is one shared pool, drawn in pipeline order."""
    pid_a = add_pipeline(conn, ref, "Arazu", 2)
    pid_l = add_pipeline(conn, ref, "Lachesis", 2)
    enable_invention(conn, ref, pid_a, "Arazu")
    enable_invention(conn, ref, pid_l, "Lachesis")
    snap = rich_snapshot(ref)
    snap.on_hand[CELESTIS_BP] = 5  # BPO + 4 copies
    data = engine.invention_stockpile(conn, ref, snap)
    arazu = section(data, "Arazu")
    lachesis = section(data, "Lachesis")
    combined = arazu["t1"]["from_stock"] + lachesis["t1"]["from_stock"]
    # never more than (stack minus one) copies × max runs each
    assert combined <= 4 * ref.max_runs(CELESTIS_BP)
    assert arazu["t1"]["shared"] == ["Lachesis"]
    assert lachesis["t1"]["shared"] == ["Arazu"]
    # Shared datacore demand nets once: gross is the SUM of both
    # pipelines' attempts-scaled need.
    source = ref.invention_source_for_product(ref.type_id("Arazu"))
    material_id, qty = ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    )[0]
    expected = with_margin(conn, qty * (arazu["attempts"] + lachesis["attempts"]))
    assert buy(data, material_id)["gross"] == expected


def test_relic_pipeline_on_tab(conn, ref):
    pid = add_pipeline(conn, ref, "Tengu", 2)
    enable_invention(conn, ref, pid, "Tengu", source_id=INTACT_HULL)
    snap = rich_snapshot(ref)
    snap.on_hand[INTACT_HULL] = 10**9  # wormhole loot
    data = engine.invention_stockpile(conn, ref, snap)
    s = section(data, "Tengu")
    assert s["is_relic"] and s["t1"] is None
    assert s["attempts"] > 0
    assert s["jobs"] is None  # relic job grouping is not modeled
    relic = buy(data, INTACT_HULL)
    assert relic["gross"] == with_margin(conn, s["attempts"])
    assert relic["to_buy"] == 0  # loot covers it
    assert data["copy_jobs"] == []  # relics need no copy jobs


def test_hull_cost_invariant_to_attempts(conn, ref):
    """The realized per-hull invention cost is the amortized expectation
    regardless of how many attempts the cycle actually installed —
    a 4x stockpile run and a stock-covered run cost the same per hull."""
    pid = add_pipeline(conn, ref, "Zealot", 2)
    enable_invention(conn, ref, pid, "Zealot")
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(10.0))
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    settings = store.get_settings(conn)

    def subtotal():
        cost = costing.hull_cost(conn, ref, settings, plan.index_run_id, pid)
        return cost.subtotal("invention")

    baseline = subtotal()
    row = conn.execute("SELECT * FROM index_run_invention").fetchone()
    assert baseline == pytest.approx(row["cost_per_run"])
    # Nothing about the cycle's sizing is even persisted (schema 5): the
    # replay can only ever read the vintage's probability and runs.
    assert "attempts" not in row.keys() and "copies_needed" not in row.keys()
    assert subtotal() == pytest.approx(
        1.0 / (row["probability"] * row["runs_per_copy"]) * (
            row["invention_fee_per_attempt"] + row["copy_fee_per_attempt"]
            + sum(q * (p or 0.0) for _t, q, p in json.loads(row["datacores"]))
        )
    )


# --- web ---------------------------------------------------------------------


def test_web_settings_clamp_overbuild(seeded_client):
    resp = seeded_client.post(
        "/settings",
        data=_settings_form(t1_overbuild_pct="2000", t2_overbuild_pct="50"),
    )
    assert resp.status_code == 302
    c = _state()
    settings = store.get_settings(c)
    assert settings.t1_bpc_overbuild == pytest.approx(10.0)
    assert settings.t2_bpc_overbuild == pytest.approx(1.0)
    c.close()


def test_web_invention_tab_renders(seeded_client, ref):
    # Empty state: no invention-enabled pipelines.
    page = seeded_client.get("/invention").get_data(as_text=True)
    assert "no invention-enabled pipelines" in page
    # Enable on the seeded Hulk (capable: exhumers invent from barges).
    c = _state()
    pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Hulk'"
    ).fetchone()["pipeline_id"]
    seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "none"}
    )
    page = seeded_client.get("/invention").get_data(as_text=True)
    assert "Hulk" in page
    assert "stockpile" in page
    assert "Datacore" in page
    assert "copy job" in page
    c.close()
