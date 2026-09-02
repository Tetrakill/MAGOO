"""T2 invention (v1.22): the pure math, the imported reference data, the
shared cost assembly, both hull-cost views, the engine's post-convergence
invention pass, and the web lifecycle.

Same fixture style as test_engine / test_costing: real reference data from
the production SDE (read-only), temp state DB per test. The Zealot is the
worked example throughout — source blueprint 2007 (Omen), product blueprint
12004, base probability 0.26, 1 base run, datacores High Energy Physics x8
+ Amarrian Starship Engineering x8. All-V skills give the chance factor
1 + (5+5)/30 + 5/40 = 1.458333.
"""

import math
import sqlite3

import pytest

from magoo import config, costing, engine, industry, store
from magoo.engine import Snapshot

from conftest import FairValuePrices
from test_web_lifecycle import _settings_form

ZEALOT_P_NONE = 0.26 * (1 + 10 / 30 + 5 / 40)  # no decryptor, all V


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    yield c
    c.close()


def add_pipeline(conn, ref, name, qty, runs_per_bpc=None, bpc_cost=None):
    cur = conn.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run, runs_per_bpc, bpc_cost_isk) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, ref.type_id(name), qty, runs_per_bpc, bpc_cost),
    )
    conn.commit()
    return cur.lastrowid


def rich_snapshot(ref, slots=500, overrides=None):
    prices = FairValuePrices(ref, overrides=overrides)
    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=prices,
        adjusted_prices=prices,
    )


def decryptor_named(ref, name):
    return next(d for d in ref.decryptors() if d.name == name)


def enable_invention(
    conn, ref, pipeline_id, product_name, decryptor=None, source_id=None
):
    """Materialize an invention choice exactly as the web route does:
    use_invention + decryptor (+ chosen source for multi-source finals)
    on the pipeline, derived runs into runs_per_bpc, derived ME/TE into
    blueprint_setting."""
    source = ref.invention_source_for_product(
        ref.type_id(product_name), source_id
    )
    me, te, runs = industry.invented_bpc(
        source.runs,
        decryptor.me_mod if decryptor else 0,
        decryptor.te_mod if decryptor else 0,
        decryptor.run_mod if decryptor else 0,
    )
    conn.execute(
        "UPDATE pipeline SET manual_runs_per_bpc = CASE "
        "WHEN use_invention THEN manual_runs_per_bpc "
        "ELSE runs_per_bpc END, "
        "use_invention = 1, decryptor_type_id = ?, "
        "invention_source_blueprint_id = ?, "
        "runs_per_bpc = ? WHERE pipeline_id = ?",
        (
            decryptor.type_id if decryptor else None,
            source_id,
            runs,
            pipeline_id,
        ),
    )
    conn.execute(
        "INSERT INTO blueprint_setting VALUES (?, ?, ?) "
        "ON CONFLICT (blueprint_id) DO UPDATE SET me_level = "
        "excluded.me_level, te_level = excluded.te_level",
        (source.product_blueprint_id, me, te),
    )
    conn.commit()
    return source


# --- Pure math (industry.py) -----------------------------------------------


def test_invention_probability_zealot_hand_check():
    assert industry.invention_probability(0.26, [5, 5], 5) == pytest.approx(
        0.26 * 1.4583333333
    )
    # Accelerant Decryptor: x1.2
    assert industry.invention_probability(
        0.26, [5, 5], 5, 1.2
    ) == pytest.approx(0.455)
    # skill terms: two sciences at /30, encryption at /40
    assert industry.invention_probability(0.3, [4, 3], 2) == pytest.approx(
        0.3 * (1 + 7 / 30 + 2 / 40)
    )


def test_invention_probability_clamps_at_certainty():
    # 0.34 x 1.4583 x 1.9 (Optimized Attainment) = 0.942 — legal…
    assert industry.invention_probability(0.34, [5, 5], 5, 1.9) < 1.0
    # …but the clamp guards fabricated over-unity combinations.
    assert industry.invention_probability(0.9, [5, 5], 5, 1.9) == 1.0


def test_invented_bpc_modifiers_and_clamps():
    assert industry.invented_bpc(1) == (2, 4, 1)
    # Accelerant: +2 ME / +10 TE / +1 run
    assert industry.invented_bpc(1, 2, 10, 1) == (4, 14, 2)
    # Attainment: −1 ME — ME 1, never negative
    assert industry.invented_bpc(1, -1, 4, 4) == (1, 8, 5)
    # Augmentation: −2 ME → ME 0
    assert industry.invented_bpc(10, -2, 2, 9) == (0, 6, 19)
    # data-drift guards only: nothing in game goes below these
    assert industry.invented_bpc(1, -5, -9, -3) == (0, 0, 1)


def test_invention_cost_per_run():
    assert industry.invention_cost_per_run(100.0, 0.5, 4) == pytest.approx(
        50.0
    )


def test_encryption_skill_routes_by_name_family():
    skills = industry.SkillLevels(encryption=3, science=1)
    assert (
        industry._per_bp_skill_level("Amarr Encryption Methods", skills) == 3
    )
    assert industry._per_bp_skill_level("High Energy Physics", skills) == 1


# --- Reference data (sdeimport + refdata) ----------------------------------


def test_zealot_invention_source(ref):
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    assert source is not None
    assert source.t1_blueprint_id == 2007  # Omen Blueprint
    assert source.product_blueprint_id == 12004  # Zealot Blueprint
    assert source.probability == pytest.approx(0.26)
    assert source.runs == 1
    mats = ref.materials(source.t1_blueprint_id, config.ACTIVITY_INVENTION)
    assert len(mats) == 2
    for material_id, qty in mats:
        assert ref.type_info(material_id).group_id == config.DATACORE_GROUP
        assert qty == 8
    skills = ref.blueprint_skills(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    )
    names = {ref.type_info(s).name for s, _lvl in skills}
    assert "Amarr Encryption Methods" in names
    assert len(names) == 3


def test_uninvented_and_multi_source_products_not_capable(ref):
    # Raw material: no blueprint at all.
    assert ref.invention_source_for_product(ref.type_id("Tritanium")) is None
    # T1 ship: buildable but never an invention product.
    assert ref.invention_source_for_product(ref.type_id("Omen")) is None
    # T3 (relic-invented): several sources — deferred, not capable.
    multi = ref.conn.execute(
        "SELECT product_blueprint_id FROM ref_invention "
        "GROUP BY product_blueprint_id HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()[0]
    assert len(ref.invention_sources(multi)) > 1
    t3_product = ref.conn.execute(
        "SELECT product_id FROM ref_blueprint WHERE blueprint_id = ? "
        "AND activity_id = 1",
        (multi,),
    ).fetchone()[0]
    assert ref.invention_source_for_product(t3_product) is None


def test_decryptors(ref):
    decryptors = ref.decryptors()
    assert len(decryptors) == 8
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    assert (
        accelerant.prob_mult,
        accelerant.me_mod,
        accelerant.te_mod,
        accelerant.run_mod,
    ) == (1.2, 2, 10, 1)
    assert ref.decryptor(accelerant.type_id) == accelerant
    assert ref.decryptor(999999999) is None


def test_no_invention_rows_leaked_into_ref_blueprint(ref):
    rows = ref.conn.execute(
        "SELECT DISTINCT activity_id FROM ref_blueprint"
    ).fetchall()
    assert {r[0] for r in rows} == {
        config.ACTIVITY_MANUFACTURING,
        config.ACTIVITY_REACTION,
    }


# --- Schema / settings ------------------------------------------------------


def test_schema_migrations(conn):
    pipeline_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(pipeline)")
    }
    assert {"use_invention", "decryptor_type_id"} <= pipeline_cols
    settings = store.get_settings(conn)
    assert settings.skill_encryption == 5
    assert settings.skill_levels().encryption == 5
    classes = store.get_class_settings(conn)
    assert "invention" in classes
    assert "copying" in classes
    assert conn.execute(
        "SELECT COUNT(*) FROM index_run_invention"
    ).fetchone()[0] == 0


def test_copying_class_seeds_from_invention_row(conn):
    """Split 2026-08-31: on an existing database the new copying row must
    inherit the invention lab it was silently sharing, not 'other' — but
    NEVER its rig tiers (a manufacturing-rig assertion is not a lab-rig
    assertion, and the lab tier is a live job-cost bonus)."""
    conn.execute(
        "UPDATE class_setting SET structure_type_id = 35825, "
        "system_cost_index = 0.05, tax_rate = 0.01, me_rig = 't2' "
        "WHERE item_class = 'invention'"
    )
    conn.execute("DELETE FROM class_setting WHERE item_class = 'copying'")
    conn.commit()
    store.ensure_schema(conn)  # idempotent; reseeds the missing row
    copying = store.get_class_settings(conn)["copying"]
    assert copying.structure_type_id == 35825
    assert copying.system_cost_index == pytest.approx(0.05)
    assert copying.tax_rate == pytest.approx(0.01)
    assert copying.me_rig == "none"
    assert copying.te_rig == "none"


# --- costing.invention_cost -------------------------------------------------


def test_invention_cost_hand_computed(conn, ref):
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))

    prices = {
        material_id: 100_000.0
        for material_id, _q in ref.materials(
            source.t1_blueprint_id, config.ACTIVITY_INVENTION
        )
    }
    cost = costing.invention_cost(
        ref, settings, class_settings, source, None,
        price_of=prices.get, adjusted_of=lambda t: 10.0,
    )
    assert cost.probability == pytest.approx(ZEALOT_P_NONE)
    assert (cost.me, cost.te, cost.runs_per_copy) == (2, 4, 1)
    # Default lab: NPC (no structure bonus), index 0, tax 0.25%, SCC 4% —
    # each fee = 0.02 x EIV_T1 x 0.0425, invention and copy identical.
    eiv = sum(
        qty * 10.0
        for _m, qty in ref.materials(
            source.t1_blueprint_id, config.ACTIVITY_MANUFACTURING
        )
    )
    expected_fee = 0.02 * eiv * (0.0025 + 0.04)
    assert cost.invention_fee == pytest.approx(expected_fee)
    assert cost.copy_fee == pytest.approx(expected_fee)
    assert cost.unpriced == 0
    expected_attempt = 16 * 100_000.0 + 2 * expected_fee
    assert cost.attempt_cost == pytest.approx(expected_attempt)
    assert cost.cost_per_run == pytest.approx(
        expected_attempt / ZEALOT_P_NONE
    )

    # Accelerant: x1.2 chance, 2-run ME4/TE14 copies, decryptor priced in.
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    prices[accelerant.type_id] = 1_000_000.0
    with_d = costing.invention_cost(
        ref, settings, class_settings, source, accelerant,
        price_of=prices.get, adjusted_of=lambda t: 10.0,
    )
    assert with_d.probability == pytest.approx(0.455)
    assert (with_d.me, with_d.te, with_d.runs_per_copy) == (4, 14, 2)
    assert with_d.attempt_cost == pytest.approx(
        expected_attempt + 1_000_000.0
    )
    assert with_d.cost_per_run == pytest.approx(
        (expected_attempt + 1_000_000.0) / (0.455 * 2)
    )


def test_copy_fee_reads_the_copying_class_row(conn, ref):
    """Split 2026-08-31: the copy fee prices off the 'copying' row, the
    invention fee off the 'invention' row — a copying-only index moves
    only the copy fee."""
    conn.execute(
        "UPDATE class_setting SET system_cost_index = 0.10 "
        "WHERE item_class = 'copying'"
    )
    conn.commit()
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    cost = costing.invention_cost(
        ref, settings, class_settings, source, None,
        price_of=lambda t: None, adjusted_of=lambda t: 10.0,
    )
    eiv = sum(
        qty * 10.0
        for _m, qty in ref.materials(
            source.t1_blueprint_id, config.ACTIVITY_MANUFACTURING
        )
    )
    # invention row untouched: index 0, tax 0.25%, SCC 4%
    assert cost.invention_fee == pytest.approx(0.02 * eiv * 0.0425)
    # copying row: + the 10% index (NPC structure -> cost mult 1.0)
    assert cost.copy_fee == pytest.approx(0.02 * eiv * (0.10 + 0.0425))


def test_lab_cost_rig_multiplier():
    """Lab cost rigs (2026-08-31): universal −10% (T1) / −12% (T2) on the
    engineering security bands, only ever for the lab activities — the
    manufacturing/reaction rig families carry a zero cost bonus."""
    t1_null = industry.BuildSetting(
        structure_type_id=config.STRUCTURE_TYPE_RAITARU,
        security=-0.5,
        me_rig="t1",
    )
    assert industry.rig_multiplier(
        t1_null, config.ACTIVITY_INVENTION, "cost"
    ) == pytest.approx(1 - 0.10 * 2.1)
    assert industry.rig_multiplier(
        t1_null, config.ACTIVITY_COPYING, "cost"
    ) == pytest.approx(1 - 0.10 * 2.1)
    t2_low = industry.BuildSetting(
        structure_type_id=config.STRUCTURE_TYPE_RAITARU,
        security=0.25,
        me_rig="t2",
    )
    assert industry.rig_multiplier(
        t2_low, config.ACTIVITY_INVENTION, "cost"
    ) == pytest.approx(1 - 0.12 * 1.9)
    # A manufacturing class's ME rig never bleeds into its cost mult.
    assert (
        industry.rig_multiplier(t2_low, config.ACTIVITY_MANUFACTURING, "cost")
        == 1.0
    )
    no_rig = industry.BuildSetting(
        structure_type_id=config.STRUCTURE_TYPE_RAITARU, security=-0.5
    )
    assert (
        industry.rig_multiplier(no_rig, config.ACTIVITY_INVENTION, "cost")
        == 1.0
    )


def test_invention_fee_applies_lab_cost_rig(conn, ref):
    """The fee's cost multiplier composes structure bonus × lab cost rig
    (per lab row) — the copying row stays independent."""
    conn.execute(
        "UPDATE class_setting SET structure_type_id = 35825, "
        "security = -0.5, me_rig = 't2', system_cost_index = 0.10 "
        "WHERE item_class = 'invention'"
    )
    conn.commit()
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    cost = costing.invention_cost(
        ref, settings, class_settings, source, None,
        price_of=lambda t: None, adjusted_of=lambda t: 10.0,
    )
    eiv = sum(
        qty * 10.0
        for _m, qty in ref.materials(
            source.t1_blueprint_id, config.ACTIVITY_MANUFACTURING
        )
    )
    # Raitaru invention cost 0.97 × T2 lab rig in nullsec (−12% × 2.1).
    mult = 0.97 * (1 - 0.12 * 2.1)
    assert cost.invention_fee == pytest.approx(
        0.02 * eiv * (0.10 * mult + 0.0025 + 0.04)
    )
    # Copying row untouched: NPC defaults, no index, no rig.
    assert cost.copy_fee == pytest.approx(0.02 * eiv * 0.0425)


def test_invention_cost_missing_prices_count_zero(conn, ref):
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    cost = costing.invention_cost(
        ref, settings, class_settings, source, accelerant,
        price_of=lambda t: None, adjusted_of=lambda t: None,
    )
    assert cost.unpriced == 3  # two datacores + the decryptor
    assert cost.attempt_cost == 0.0  # missing adjusted -> zero fees too
    assert cost.cost_per_run == 0.0


def test_invention_chance_matches_cost(conn, ref):
    settings = store.get_settings(conn)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    assert costing.invention_chance(
        ref, settings, source, None
    ) == pytest.approx(ZEALOT_P_NONE)
    conn.execute("UPDATE settings SET skill_encryption = 0")
    conn.commit()
    assert costing.invention_chance(
        ref, store.get_settings(conn), source, None
    ) == pytest.approx(0.26 * (1 + 10 / 30))


# --- Engine: chain coster, invention pass, persistence ----------------------


def test_chain_cost_swaps_bpc_for_computed_invention(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2, runs_per_bpc=1, bpc_cost=1e9)
    zealot = ref.type_id("Zealot")
    # Pin the T2 blueprint at the invented ME/TE first, so the only delta
    # the assertion sees is the per-unit adder itself.
    conn.execute(
        "INSERT INTO blueprint_setting VALUES (12004, 2, 4)"
    )
    conn.commit()
    snap = rich_snapshot(ref)
    base = engine._chain_coster(conn, ref, snap)[0](zealot)[0]
    assert base > 1e9  # manual bpc: 1e9 / 1 run dominates

    conn.execute(
        "UPDATE pipeline SET use_invention = 1 WHERE pipeline_id = ?", (pid,)
    )
    conn.commit()
    settings = store.get_settings(conn)
    source = ref.invention_source_for_product(zealot)
    expected = costing.invention_cost(
        ref, settings, store.get_class_settings(conn), source, None,
        price_of=lambda t: engine._landed_price(ref, settings, snap, t),
        adjusted_of=snap.adjusted,
    )
    with_inv = engine._chain_coster(conn, ref, snap)[0](zealot)[0]
    # bpc_cost_isk (1e9/run) is ignored; the computed cost/run replaces it.
    assert with_inv == pytest.approx(
        base - 1e9 + expected.cost_per_run, rel=1e-9
    )


def test_invention_pass_persists_vintage_only(conn, ref):
    """v1.23: the run persists the invention VINTAGE but injects NO buy
    rows — sizing and purchasing live on the Invention tab."""
    pid = add_pipeline(conn, ref, "Zealot", 2)
    source = enable_invention(conn, ref, pid, "Zealot")
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=True)

    zealot = plan.items[ref.type_id("Zealot")]
    assert zealot.runs_allocated == 2  # bpc cap 1 run/job, 2 jobs
    row = plan.invention[pid]
    assert row["probability"] == pytest.approx(ZEALOT_P_NONE)
    assert (row["invented_me"], row["invented_te"]) == (2, 4)
    assert row["runs_per_copy"] == 1
    # Sizing figures are the Invention tab's business — none are persisted
    # (schema 5 dropped the informational copies_needed/attempts columns).
    assert "attempts" not in row and "copies_needed" not in row

    # No datacore/decryptor buy rows in the plan (moved to the tab).
    for material_id, _qty in ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    ):
        assert material_id not in plan.items

    persisted = conn.execute(
        "SELECT * FROM index_run_invention WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchall()
    assert len(persisted) == 1
    assert persisted[0]["probability"] == pytest.approx(row["probability"])
    assert persisted[0]["cost_per_run"] > 0
    assert "attempts" not in persisted[0].keys()


def test_invention_pass_decryptor_math(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2)
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    enable_invention(conn, ref, pid, "Zealot", accelerant)
    plan = engine.plan_index_run(conn, ref, rich_snapshot(ref), persist=False)
    row = plan.invention[pid]
    # Accelerant: +1 run, ME +2 -> 2-run ME4 copies at 0.455.
    assert row["runs_per_copy"] == 2
    assert row["invented_me"] == 4
    assert row["decryptor_type_id"] == accelerant.type_id
    assert row["probability"] == pytest.approx(0.455)
    assert accelerant.type_id not in plan.items  # no buy rows in the run


def test_invention_pass_persists_vintage_even_when_starved(conn, ref):
    """Review 2026-09-01: a final the slot pool starved to zero runs still
    gets its vintage row — the executed run's profit view replays the
    invention expectation instead of falling back to the manual bpc line
    (the very figure invention ignores)."""
    pid = add_pipeline(conn, ref, "Zealot", 2, bpc_cost=1e9)
    source = enable_invention(conn, ref, pid, "Zealot")
    plan = engine.plan_index_run(
        conn, ref, rich_snapshot(ref, slots=0), persist=True
    )
    assert plan.items[ref.type_id("Zealot")].runs_allocated == 0
    assert pid in plan.invention
    datacore_id = ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    )[0][0]
    assert datacore_id not in plan.items
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    cost = costing.hull_cost(
        conn, ref, store.get_settings(conn), plan.index_run_id, pid
    )
    assert cost is not None
    assert not [l for l in cost.lines if l.kind == "bpc"]
    assert cost.subtotal("invention") == pytest.approx(
        plan.invention[pid]["cost_per_run"]
    )


def _decryptor_line_checks(inv, accelerant, probability, runs_per_copy):
    # 2 datacores + the decryptor + invention fee + copy fee.
    assert len(inv) == 5
    line = next(l for l in inv if l.type_id == accelerant.type_id)
    assert line.unit_cost == 10.0 and not line.missing_price
    assert line.qty_per_hull == pytest.approx(1 / (probability * runs_per_copy))


def test_hull_cost_replays_decryptor_line(conn, ref):
    """Review 2026-09-01: the decryptor CostLine branch had no test."""
    pid = add_pipeline(conn, ref, "Zealot", 2)
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    enable_invention(conn, ref, pid, "Zealot", accelerant)
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
    row = plan.invention[pid]
    assert row["decryptor_unit_price"] == 10.0
    _decryptor_line_checks(
        [l for l in cost.lines if l.kind == "invention"],
        accelerant, row["probability"], row["runs_per_copy"],
    )
    assert cost.subtotal("invention") == pytest.approx(row["cost_per_run"])


def test_current_hull_cost_prices_decryptor_line(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2)
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    source = enable_invention(conn, ref, pid, "Zealot", accelerant)
    settings = store.get_settings(conn)
    cost = costing.current_hull_cost(
        conn, ref, settings, get_pipeline(conn, pid),
        UniformDict(10.0), UniformDict(10.0),
    )
    expected = costing.invention_cost(
        ref, settings, store.get_class_settings(conn), source, accelerant,
        price_of=lambda t: 10.0, adjusted_of=lambda t: 10.0,
    )
    _decryptor_line_checks(
        [l for l in cost.lines if l.kind == "invention"],
        accelerant, expected.probability, expected.runs_per_copy,
    )
    assert cost.subtotal("invention") == pytest.approx(
        expected.cost_per_run, rel=1e-9
    )


def test_stale_invention_config_falls_back_to_bpc(conn, ref):
    # A final with no invention source (a T1 ship — note the Hulk would NOT
    # do here: exhumers are T2, invented from mining barges) stuck at
    # use_invention=1 after "SDE drift": the config resolves to nothing and
    # the manual bpc figure still applies — divided by the STASHED manual
    # runs, never the materialized value left in runs_per_bpc.
    pid = add_pipeline(conn, ref, "Omen", 1, runs_per_bpc=10, bpc_cost=1e9)
    conn.execute(
        "UPDATE pipeline SET use_invention = 1, "
        "manual_runs_per_bpc = runs_per_bpc, runs_per_bpc = 3 "
        "WHERE pipeline_id = ?",
        (pid,),
    )
    conn.commit()
    assert engine._invention_configs(conn, ref) == {}
    omen = ref.type_id("Omen")
    snap = rich_snapshot(ref)
    base = engine._chain_coster(conn, ref, snap)[0](omen)[0]
    conn.execute("UPDATE pipeline SET bpc_cost_isk = NULL")
    conn.commit()
    without = engine._chain_coster(conn, ref, rich_snapshot(ref))[0](omen)[0]
    assert base == pytest.approx(without + 1e8)  # 1e9 / manual 10, not / 3


def test_demand_type_ids_include_invention_inputs(conn, ref):
    add_pipeline(conn, ref, "Zealot", 2)  # capable is enough — chosen or not
    ids = engine.demand_type_ids(conn, ref)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    for material_id, _qty in ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    ):
        assert material_id in ids
    for d in ref.decryptors():
        assert d.type_id in ids
    for material_id, _qty in ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_MANUFACTURING
    ):
        assert material_id in ids


# --- Costing: both hull-cost views ------------------------------------------


def uniform_snapshot(price, slots=500):
    class UniformPrices(dict):
        def get(self, key, default=None):
            return price

    return Snapshot(
        slots_available={
            config.ACTIVITY_MANUFACTURING: slots,
            config.ACTIVITY_REACTION: slots,
        },
        prices=UniformPrices(),
        adjusted_prices=UniformPrices(),
    )


class UniformDict(dict):
    def __init__(self, value):
        self.value = value

    def get(self, key, default=None):
        return self.value


def get_pipeline(conn, pid):
    return conn.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()


def test_hull_cost_reads_persisted_invention_and_drops_bpc(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2, bpc_cost=1e9)
    source = enable_invention(conn, ref, pid, "Zealot")
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(10.0))
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    settings = store.get_settings(conn)
    cost = costing.hull_cost(conn, ref, settings, plan.index_run_id, pid)
    assert cost is not None
    assert not [l for l in cost.lines if l.kind == "bpc"]  # 1e9 ignored
    inv = [l for l in cost.lines if l.kind == "invention"]
    # 2 datacore lines + invention fee + copy fee (no decryptor)
    assert len(inv) == 4
    # v1.23: the replay prices the vintage's CONTINUOUS expected
    # consumption (1/(P × runs × portion)) — production volume is the
    # Invention tab's business, so attempts never scale the lines.
    for material_id, qty in ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    ):
        line = next(l for l in inv if l.type_id == material_id)
        assert line.qty_per_hull == pytest.approx(qty / ZEALOT_P_NONE)
        assert line.unit_cost == 10.0  # uniform, zero freight
        assert line.lag_runs == 0 and not line.clamped
    # The per-hull invention subtotal IS the amortized cost per run.
    row = conn.execute(
        "SELECT * FROM index_run_invention WHERE index_run_id = ?",
        (plan.index_run_id,),
    ).fetchone()
    attempt_cost = (
        row["invention_fee_per_attempt"]
        + row["copy_fee_per_attempt"]
        + 16 * 10.0
    )
    assert cost.subtotal("invention") == pytest.approx(
        attempt_cost / ZEALOT_P_NONE
    )
    assert cost.subtotal("invention") == pytest.approx(row["cost_per_run"])


def test_hull_cost_without_invention_row_keeps_bpc(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2, runs_per_bpc=4, bpc_cost=40e6)
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
    bpc = [l for l in cost.lines if l.kind == "bpc"]
    assert len(bpc) == 1 and bpc[0].cost_per_hull == pytest.approx(10e6)
    assert not [l for l in cost.lines if l.kind == "invention"]


def test_current_hull_cost_live_invention_expectation(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2, bpc_cost=1e9)
    source = enable_invention(conn, ref, pid, "Zealot")
    settings = store.get_settings(conn)
    cost = costing.current_hull_cost(
        conn, ref, settings, get_pipeline(conn, pid),
        UniformDict(10.0), UniformDict(10.0),
    )
    assert not [l for l in cost.lines if l.kind == "bpc"]
    inv = [l for l in cost.lines if l.kind == "invention"]
    assert len(inv) == 4
    # Continuous expectation: qty/attempt ÷ (P x runs_per_copy x portion).
    datacore_id, qty = ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    )[0]
    line = next(l for l in inv if l.type_id == datacore_id)
    assert line.qty_per_hull == pytest.approx(qty / ZEALOT_P_NONE)
    # Consistency: the invention subtotal equals attempt cost x E[attempts].
    expected = costing.invention_cost(
        ref, settings, store.get_class_settings(conn), source, None,
        price_of=lambda t: 10.0, adjusted_of=lambda t: 10.0,
    )
    assert cost.subtotal("invention") == pytest.approx(
        expected.cost_per_run, rel=1e-9
    )


def test_current_hull_cost_stale_config_falls_back_to_bpc(conn, ref):
    pid = add_pipeline(conn, ref, "Omen", 8, runs_per_bpc=4, bpc_cost=40e6)
    conn.execute(
        "UPDATE pipeline SET use_invention = 1, "
        "manual_runs_per_bpc = runs_per_bpc, runs_per_bpc = 2 "
        "WHERE pipeline_id = ?",
        (pid,),
    )
    conn.commit()
    cost = costing.current_hull_cost(
        conn, ref, store.get_settings(conn), get_pipeline(conn, pid),
        UniformDict(10.0), UniformDict(10.0),
    )
    bpc = [l for l in cost.lines if l.kind == "bpc"]
    assert len(bpc) == 1 and bpc[0].cost_per_hull == pytest.approx(10e6)
    assert not [l for l in cost.lines if l.kind == "invention"]


def test_toggle_never_reprices_realized_history(conn, ref):
    """v1.22 review fix: enabling invention materializes runs_per_bpc, but
    the manual-BPC fallback for PRE-invention executed runs keeps dividing
    by the STASHED manual value — in both toggle directions."""
    pid = add_pipeline(conn, ref, "Zealot", 2, runs_per_bpc=10, bpc_cost=40e6)
    plan = engine.plan_index_run(conn, ref, uniform_snapshot(10.0))
    conn.execute(
        "UPDATE index_run SET status = 'complete', "
        "completed_at = datetime('now') WHERE index_run_id = ?",
        (plan.index_run_id,),
    )
    conn.commit()
    settings = store.get_settings(conn)

    def bpc_per_hull():
        cost = costing.hull_cost(conn, ref, settings, plan.index_run_id, pid)
        return next(l for l in cost.lines if l.kind == "bpc").cost_per_hull

    assert bpc_per_hull() == pytest.approx(4e6)  # 40M / 10-run copies
    enable_invention(conn, ref, pid, "Zealot")  # materializes runs -> 1
    row = get_pipeline(conn, pid)
    assert row["runs_per_bpc"] == 1
    assert row["manual_runs_per_bpc"] == 10
    assert bpc_per_hull() == pytest.approx(4e6)  # history untouched
    # Changing decryptors keeps the original stash.
    enable_invention(
        conn, ref, pid, "Zealot", decryptor_named(ref, "Accelerant Decryptor")
    )
    assert get_pipeline(conn, pid)["manual_runs_per_bpc"] == 10
    # Off restores the manual value (the web route's SQL).
    conn.execute(
        "UPDATE pipeline SET runs_per_bpc = manual_runs_per_bpc, "
        "manual_runs_per_bpc = NULL, use_invention = 0, "
        "decryptor_type_id = NULL WHERE pipeline_id = ?",
        (pid,),
    )
    conn.commit()
    row = get_pipeline(conn, pid)
    assert row["runs_per_bpc"] == 10
    assert row["manual_runs_per_bpc"] is None
    assert bpc_per_hull() == pytest.approx(4e6)


def test_demand_type_ids_cover_inactive_capable_pipelines(conn, ref):
    """v1.22 review fix: the comparison renders for inactive pipelines too,
    so the price refresh must fetch their invention inputs."""
    add_pipeline(conn, ref, "Zealot", 2)
    conn.execute("UPDATE pipeline SET is_active = 0")
    conn.commit()
    ids = engine.demand_type_ids(conn, ref)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    for material_id, _qty in ref.materials(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    ):
        assert material_id in ids


# --- Web lifecycle -----------------------------------------------------------


def _state():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def test_web_invention_lifecycle(seeded_client, ref):
    # Paste a T2 pipeline (10-run bought copies). Zealot is capable.
    resp = seeded_client.post(
        "/pipelines", data={"products": "Zealot\t2\t10"}
    )
    assert resp.status_code == 302
    c = _state()
    pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Zealot'"
    ).fetchone()["pipeline_id"]

    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert "Accelerant Decryptor" in page  # the select renders
    # The nine-option comparison was removed 2026-08-31 (user request).
    assert "Compare decryptors" not in page

    # Enable with the Accelerant: runs 2, ME 4 / TE 14 materialized.
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    resp = seeded_client.post(
        f"/pipelines/{pid}/invention",
        data={"decryptor": str(accelerant.type_id)},
    )
    assert resp.status_code == 302
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["use_invention"] == 1
    assert row["decryptor_type_id"] == accelerant.type_id
    assert row["runs_per_bpc"] == 2
    assert row["manual_runs_per_bpc"] == 10  # the pasted value, stashed
    bs = c.execute(
        "SELECT * FROM blueprint_setting WHERE blueprint_id = 12004"
    ).fetchone()
    assert (bs["me_level"], bs["te_level"]) == (4, 14)

    # Re-paste: qty updates, the materialized overrides stay untouched.
    seeded_client.post(
        "/pipelines", data={"products": "Zealot\t3\t10\t5\t5"}
    )
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["output_qty_per_run"] == 3
    assert row["runs_per_bpc"] == 2
    bs = c.execute(
        "SELECT * FROM blueprint_setting WHERE blueprint_id = 12004"
    ).fetchone()
    assert (bs["me_level"], bs["te_level"]) == (4, 14)

    # Plan a run: the vintage row persists; the run pages show NO
    # invention information (user ruling 2026-09-01) — the vintage only
    # feeds the Profit tab's cost line; production lives on the Invention
    # tab.
    resp = seeded_client.post("/run")
    assert resp.status_code == 302
    run_id = c.execute(
        "SELECT MAX(index_run_id) AS id FROM index_run"
    ).fetchone()["id"]
    inv_row = c.execute(
        "SELECT * FROM index_run_invention WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()
    assert inv_row is not None and inv_row["pipeline_id"] == pid
    # Accelerant: 2-run copies at P=0.455 (the vintage, no sizing figures)
    assert inv_row["runs_per_copy"] == 2
    assert inv_row["probability"] == pytest.approx(0.455)
    detail = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "amortized cost" not in detail
    assert "Omen Blueprint" not in detail
    assert 'data-key="invention"' not in detail
    assert "T1 BPC" not in detail  # production checklist moved to the tab
    chain = seeded_client.get(
        f"/runs/{run_id}?view=chain"
    ).get_data(as_text=True)
    assert "Datacore - High Energy Physics" not in chain  # no buy rows
    # No datacore buy rows persisted with the run.
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    datacore_ids = [
        m
        for m, _q in ref.materials(
            source.t1_blueprint_id, config.ACTIVITY_INVENTION
        )
    ]
    placeholders = ",".join("?" * len(datacore_ids))
    assert c.execute(
        f"SELECT COUNT(*) FROM index_run_item WHERE index_run_id = ? "
        f"AND type_id IN ({placeholders})",
        (run_id, *datacore_ids),
    ).fetchone()[0] == 0
    # The live Invention tab renders the workbench instead.
    tab = seeded_client.get("/invention").get_data(as_text=True)
    assert "Datacore - High Energy Physics" in tab
    assert "copy job" in tab

    # Deleting the run clears its invention snapshot.
    seeded_client.post(f"/runs/{run_id}/delete")
    assert c.execute(
        "SELECT COUNT(*) FROM index_run_invention WHERE index_run_id = ?",
        (run_id,),
    ).fetchone()[0] == 0

    # Off: the stashed runs come back; ME/TE reset to paste defaults
    # (ship -> 0/0).
    resp = seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "off"}
    )
    assert resp.status_code == 302
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["use_invention"] == 0
    assert row["decryptor_type_id"] is None
    assert row["runs_per_bpc"] == 10  # restored from the stash
    assert row["manual_runs_per_bpc"] is None
    bs = c.execute(
        "SELECT * FROM blueprint_setting WHERE blueprint_id = 12004"
    ).fetchone()
    assert (bs["me_level"], bs["te_level"]) == (0, 0)
    c.close()


def test_web_invention_pipeline_delete_clears_snapshot(seeded_client, ref):
    seeded_client.post("/pipelines", data={"products": "Zealot\t2"})
    c = _state()
    pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Zealot'"
    ).fetchone()["pipeline_id"]
    seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "none"}
    )
    seeded_client.post("/run")
    assert c.execute(
        "SELECT COUNT(*) FROM index_run_invention WHERE pipeline_id = ?",
        (pid,),
    ).fetchone()[0] == 1
    seeded_client.post(f"/pipelines/{pid}/delete")
    assert c.execute(
        "SELECT COUNT(*) FROM index_run_invention WHERE pipeline_id = ?",
        (pid,),
    ).fetchone()[0] == 0
    c.close()


def test_web_invention_rejects_incapable_and_unknown(seeded_client, ref):
    # A T1 final is not invention-capable (the seeded Hulk IS — exhumers
    # are invented from mining barges — so paste an Omen to test the
    # rejection).
    seeded_client.post("/pipelines", data={"products": "Omen\t2"})
    c = _state()
    omen_pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Omen'"
    ).fetchone()["pipeline_id"]
    seeded_client.post(
        f"/pipelines/{omen_pid}/invention", data={"decryptor": "none"}
    )
    row = c.execute(
        "SELECT use_invention FROM pipeline WHERE pipeline_id = ?",
        (omen_pid,),
    ).fetchone()
    assert row["use_invention"] == 0
    # An unknown decryptor id on a CAPABLE pipeline saves nothing either.
    hulk_pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Hulk'"
    ).fetchone()["pipeline_id"]
    seeded_client.post(
        f"/pipelines/{hulk_pid}/invention", data={"decryptor": "999999999"}
    )
    row = c.execute(
        "SELECT use_invention FROM pipeline WHERE pipeline_id = ?",
        (hulk_pid,),
    ).fetchone()
    assert row["use_invention"] == 0
    c.close()


def test_web_vanished_decryptor_is_stale(seeded_client, ref):
    """Review 2026-09-01: a stored decryptor id that no longer resolves
    makes the config STALE everywhere (costing.resolve_invention) — the
    engine drops it to the bpc fallback and the Pipelines page shows the
    Off control — instead of silently costing it as no-decryptor while
    the materialised runs/ME/TE keep the vanished decryptor's modifiers."""
    c = _state()
    pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Hulk'"
    ).fetchone()["pipeline_id"]
    seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "none"}
    )
    c.execute(
        "UPDATE pipeline SET decryptor_type_id = 999999 WHERE pipeline_id = ?",
        (pid,),
    )
    c.commit()
    pipeline = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert costing.resolve_invention(ref, pipeline) is None
    assert pid not in engine._invention_configs(c, ref)
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert "stale — source or decryptor gone" in page
    c.close()


def test_web_stale_invention_keeps_off_control(seeded_client, ref):
    """v1.22 review fix: a use_invention pipeline whose final no longer
    resolves to a single source still renders an Off control, and Off
    restores the stashed manual runs."""
    # An Omen (T1, never capable) hand-flagged as a materialized-then-stale
    # invention pipeline.
    seeded_client.post("/pipelines", data={"products": "Omen\t2\t10"})
    c = _state()
    pid = c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = 'Omen'"
    ).fetchone()["pipeline_id"]
    c.execute(
        "UPDATE pipeline SET use_invention = 1, "
        "manual_runs_per_bpc = runs_per_bpc, runs_per_bpc = 2 "
        "WHERE pipeline_id = ?",
        (pid,),
    )
    c.commit()
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert "stale — source or decryptor gone" in page
    assert f"/pipelines/{pid}/invention" in page
    resp = seeded_client.post(
        f"/pipelines/{pid}/invention", data={"decryptor": "off"}
    )
    assert resp.status_code == 302
    row = c.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pid,)
    ).fetchone()
    assert row["use_invention"] == 0
    assert row["runs_per_bpc"] == 10
    assert row["manual_runs_per_bpc"] is None
    c.close()


def test_web_settings_saves_encryption_level(seeded_client):
    resp = seeded_client.post(
        "/settings", data=_settings_form(skill_encryption="3")
    )
    assert resp.status_code == 302
    c = _state()
    assert store.get_settings(c).skill_encryption == 3
    c.close()
