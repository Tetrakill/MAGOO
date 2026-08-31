"""Industry math against the real imported SDE.

The Hulk worked example from PROJECT.md §5 is the acceptance test: ME10/TE20
in a Sotiyo in nullsec. Under the per-class build-settings model the T2 Ships
class has no ME rig fitted (the original example's Large Ship rig never
applied to a Hulk), so the expected numbers are unchanged: Construction
Blocks 150 -> 134, build time 240,000s x 0.80 x 0.70 = 37.33h.
"""

import pytest

from magoo import config, industry
from magoo.industry import BuildSetting


SOTIYO_NULL = BuildSetting(
    structure_type_id=config.STRUCTURE_TYPE_SOTIYO, security=-0.2
)


# --- Worked example: Hulk ME10/TE20, Sotiyo, nullsec, no applicable rig ----


def test_hulk_construction_blocks(ref):
    mult = industry.build_multiplier(
        ref, SOTIYO_NULL, config.ACTIVITY_MANUFACTURING, "material"
    )
    assert mult == pytest.approx(0.99)  # engineering complex ME role bonus
    assert industry.required_quantity(1, 150, 10, mult) == 134


def test_hulk_build_time(ref):
    mult = industry.build_multiplier(
        ref, SOTIYO_NULL, config.ACTIVITY_MANUFACTURING, "time"
    )
    assert mult == pytest.approx(0.70)
    seconds = industry.job_time_seconds(240_000, 1, 20, mult)
    assert seconds == pytest.approx(134_400)  # 37.33 h
    assert seconds / 3600 == pytest.approx(37.33, abs=0.01)


def test_hulk_bp_ground_truth(ref):
    """The worked example's inputs exist as expected in the imported SDE."""
    hulk = ref.type_id("Hulk")
    bp = ref.blueprint_for_product(hulk)
    assert bp.activity_id == config.ACTIVITY_MANUFACTURING
    assert bp.base_time == 240_000
    materials = dict(ref.materials(bp.blueprint_id, bp.activity_id))
    assert materials[ref.type_id("Construction Blocks")] == 150


# --- Rig math (per-class assertion x security band) ------------------------


def test_rig_multipliers_by_band():
    for security, band_mult in ((0.9, 1.0), (0.3, 1.9), (-0.2, 2.1)):
        s = BuildSetting(
            structure_type_id=config.STRUCTURE_TYPE_RAITARU,
            security=security,
            me_rig="t1",
            te_rig="t2",
        )
        assert industry.rig_multiplier(
            s, config.ACTIVITY_MANUFACTURING, "material"
        ) == pytest.approx(1 - 0.020 * band_mult)
        assert industry.rig_multiplier(
            s, config.ACTIVITY_MANUFACTURING, "time"
        ) == pytest.approx(1 - 0.240 * band_mult)


def test_reaction_rig_multipliers_use_refinery_bands():
    """Reaction (refinery) rigs carry their own security bands in the SDE:
    lowSecModifier 1.0, nullSecModifier 1.1 — NOT the engineering 1.9/2.1."""
    for security, band_mult in ((0.3, 1.0), (-0.2, 1.1)):
        s = BuildSetting(
            structure_type_id=config.STRUCTURE_TYPE_TATARA,
            security=security,
            me_rig="t1",
            te_rig="t2",
        )
        assert industry.rig_multiplier(
            s, config.ACTIVITY_REACTION, "material"
        ) == pytest.approx(1 - 0.020 * band_mult)
        assert industry.rig_multiplier(
            s, config.ACTIVITY_REACTION, "time"
        ) == pytest.approx(1 - 0.240 * band_mult)


def test_no_rig_no_bonus():
    for activity in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION):
        assert industry.rig_multiplier(SOTIYO_NULL, activity, "material") == 1.0
        assert industry.rig_multiplier(SOTIYO_NULL, activity, "time") == 1.0


def test_npc_station_no_bonuses(ref):
    assert (
        industry.build_multiplier(
            ref, industry.NPC_STATION, config.ACTIVITY_MANUFACTURING, "material"
        )
        == 1.0
    )


def test_rigged_t1_ship_line(ref):
    """T1 ships in a rigged Sotiyo in nullsec: struct 0.99 x rig T1."""
    s = BuildSetting(
        structure_type_id=config.STRUCTURE_TYPE_SOTIYO,
        security=-0.2,
        me_rig="t1",
    )
    mult = industry.build_multiplier(
        ref, s, config.ACTIVITY_MANUFACTURING, "material"
    )
    assert mult == pytest.approx(0.99 * (1 - 0.020 * 2.1))


# --- Structure multipliers come from data, not the hardcoded table ---------


@pytest.mark.parametrize(
    "structure,activity,kind,expected",
    [
        (config.STRUCTURE_TYPE_RAITARU, config.ACTIVITY_MANUFACTURING, "time", 0.85),
        (config.STRUCTURE_TYPE_AZBEL, config.ACTIVITY_MANUFACTURING, "time", 0.80),
        (config.STRUCTURE_TYPE_SOTIYO, config.ACTIVITY_MANUFACTURING, "time", 0.70),
        (config.STRUCTURE_TYPE_RAITARU, config.ACTIVITY_MANUFACTURING, "material", 0.99),
        (config.STRUCTURE_TYPE_TATARA, config.ACTIVITY_REACTION, "time", 0.75),
        (config.STRUCTURE_TYPE_ATHANOR, config.ACTIVITY_REACTION, "time", 1.0),
        # Cost bonus scales the system-cost-index term of every install fee
        # — it had zero coverage (a regression to 1.0 passed the suite).
        (config.STRUCTURE_TYPE_RAITARU, config.ACTIVITY_MANUFACTURING, "cost", 0.97),
        (config.STRUCTURE_TYPE_AZBEL, config.ACTIVITY_MANUFACTURING, "cost", 0.96),
        (config.STRUCTURE_TYPE_SOTIYO, config.ACTIVITY_MANUFACTURING, "cost", 0.95),
    ],
)
def test_structure_multipliers_from_data(ref, structure, activity, kind, expected):
    assert industry.structure_multiplier(
        ref, structure, activity, kind
    ) == pytest.approx(expected)


# --- Rounding rules --------------------------------------------------------


def test_rounding_once_per_job():
    # 10 runs x 2 x 0.90 = 18 -> 18, NOT per-run ceil(1.8) x 10 = 20
    assert industry.required_quantity(10, 2, 10) == 18


def test_job_needs_at_least_runs_units():
    # 1000 runs x 1 x 0.90 = 900, but a job always needs >= runs units
    assert industry.required_quantity(1000, 1, 10) == 1000


def test_round_then_ceil():
    # 1 x 150 x 0.90 x 0.99 = 133.65 -> round 133.65 -> ceil 134
    assert industry.required_quantity(1, 150, 10, 0.99) == 134


# --- Classification --------------------------------------------------------


@pytest.mark.parametrize(
    "type_name,expected_class",
    [
        ("Hulk", "t2_ships"),
        ("Rifter", "t1_ships"),
        ("Revelation", "capital_ships"),
        ("Charon", "t1_ships"),  # freighters are Large T1 per CCP
        ("Capital Propulsion Engine", "basic_capital_components"),
        ("Ion Thruster", "advanced_components"),
        ("Tritanium", "other"),
        # v1.9 structures class: Upwell structures (65), Standup rigs and
        # service modules (66), structure components (group 536).
        ("Keepstar", "structures"),
        ("Athanor", "structures"),
        ("Metenox Moon Drill", "structures"),
        ("Standup M-Set Structure Manufacturing Material Efficiency I", "structures"),
        ("Standup Cloning Center I", "structures"),
        ("Structure Hangar Array", "structures"),
        ("Nitrogen Fuel Block", "other"),  # shares CCP filter 12, stays 'other'
    ],
)
def test_classify(ref, type_name, expected_class):
    type_id = ref.type_id(type_name)
    bp = ref.blueprint_for_product(type_id)
    assert (
        industry.classify_item(ref, type_id, bp.activity_id if bp else None)
        == expected_class
    )


def test_type_id_is_case_insensitive(ref):
    """'astrahus' / 'HULK' resolve like the canonical names; an exact-case
    match still wins first, then published, then lowest id."""
    assert ref.type_id("astrahus") == ref.type_id("Astrahus")
    assert ref.type_id("HULK") == ref.type_id("Hulk")
    assert ref.type_id("azbel") == 35826
    with pytest.raises(KeyError):
        ref.type_id("citadels")


def test_type_id_prefers_published_type(ref):
    """'Azbel' names both the Engineering Complex (35826, published) and an
    unpublished celestial (58735); by-name lookup must return the former
    deterministically (v1.9 — structure pastes rely on it)."""
    assert ref.type_id("Azbel") == 35826


def test_classify_reaction(ref):
    fernite = ref.type_id("Fernite Carbide")
    bp = ref.blueprint_for_product(fernite)
    assert bp.activity_id == config.ACTIVITY_REACTION
    assert industry.classify_item(ref, fernite, bp.activity_id) == "reactions"


# --- Skill time multipliers (user-entered levels) --------------------------


def test_skill_time_multiplier_manufacturing(ref):
    """Hulk BP requires Laser Physics + Gallente Starship Engineering; at
    all-V: Industry 0.80 x Adv Industry 0.85 x 0.95 x 0.95."""
    bp = ref.blueprint_for_product(ref.type_id("Hulk"))
    mult = industry.skill_time_multiplier(
        ref, bp.blueprint_id, bp.activity_id, industry.SkillLevels()
    )
    assert mult == pytest.approx(0.8 * 0.85 * 0.95 * 0.95)


def test_skill_time_multiplier_reaction(ref):
    bp = ref.blueprint_for_product(ref.type_id("Fernite Carbide"))
    mult = industry.skill_time_multiplier(
        ref, bp.blueprint_id, bp.activity_id, industry.SkillLevels()
    )
    assert mult == pytest.approx(0.80)  # Reactions V only


def test_skill_time_multiplier_zero_skills(ref):
    bp = ref.blueprint_for_product(ref.type_id("Hulk"))
    zero = industry.SkillLevels(0, 0, 0, 0, 0, 0, 0)
    assert industry.skill_time_multiplier(
        ref, bp.blueprint_id, bp.activity_id, zero
    ) == pytest.approx(1.0)


def test_industry_skill_not_double_counted(ref):
    """Industry appears in BP skill requirements but carries no
    manufactureTimePerLevel — only the global -4%/level applies."""
    bp = ref.blueprint_for_product(ref.type_id("Hulk"))
    only_industry = industry.SkillLevels(5, 0, 0, 0, 0, 0, 0)
    assert industry.skill_time_multiplier(
        ref, bp.blueprint_id, bp.activity_id, only_industry
    ) == pytest.approx(0.80)


def test_outpost_construction_has_its_own_level(ref):
    """Astrahus BP requires Outpost Construction (manufactureTimePerLevel
    -1%): all-V = 0.80 x 0.85 x 0.95; zeroing Outpost Construction drops the
    leg while zeroing Science does not (v1.9 — it no longer rides on the
    science level)."""
    bp = ref.blueprint_for_product(ref.type_id("Astrahus"))
    mult = lambda skills: industry.skill_time_multiplier(
        ref, bp.blueprint_id, bp.activity_id, skills
    )
    assert mult(industry.SkillLevels()) == pytest.approx(0.80 * 0.85 * 0.95)
    assert mult(industry.SkillLevels(outpost_construction=0)) == pytest.approx(
        0.80 * 0.85
    )
    assert mult(industry.SkillLevels(science=0)) == pytest.approx(
        0.80 * 0.85 * 0.95
    )


@pytest.mark.parametrize(
    "type_name",
    [
        "Standup M-Set Structure Manufacturing Material Efficiency I",
        "Structure Construction Parts",
    ],
)
def test_structure_rigs_and_components_need_only_industry(ref, type_name):
    bp = ref.blueprint_for_product(ref.type_id(type_name))
    assert industry.skill_time_multiplier(
        ref, bp.blueprint_id, bp.activity_id, industry.SkillLevels()
    ) == pytest.approx(0.80 * 0.85)


def test_per_bp_skill_level_routing():
    skills = industry.SkillLevels(
        adv_ship_construction=1, starship_engineering=2, science=3,
        outpost_construction=4,
    )
    assert industry._per_bp_skill_level("Outpost Construction", skills) == 4
    assert industry._per_bp_skill_level("Capital Ship Construction", skills) == 1
    assert industry._per_bp_skill_level("Advanced Medium Ship Construction", skills) == 1
    assert industry._per_bp_skill_level("Gallente Starship Engineering", skills) == 2
    assert industry._per_bp_skill_level("Plasma Physics", skills) == 3


# --- Job cost --------------------------------------------------------------


def test_job_install_cost():
    s = BuildSetting(system_cost_index=0.05, tax_rate=0.01)
    # EIV x (index x cost_mult + tax + SCC)
    assert industry.job_install_cost(1_000_000, s) == pytest.approx(
        1_000_000 * (0.05 + 0.01 + 0.04)
    )
    # The structure cost bonus discounts the index term only.
    assert industry.job_install_cost(1_000_000, s, 0.95) == pytest.approx(
        1_000_000 * (0.05 * 0.95 + 0.01 + 0.04)
    )
    # The SCC term is a setting since 2026-08-21 (default 4%).
    assert industry.job_install_cost(
        1_000_000, s, scc_surcharge=0.025
    ) == pytest.approx(1_000_000 * (0.05 + 0.01 + 0.025))


# --- Thukker component rigs (2026-08-21) -----------------------------------


def test_thukker_rig_bands_and_split_magnitude():
    """Thukker rigs: ME -3.7% on capital-component groups (873/913), the
    standard -2.0% on plain components, TE -20% — all on the Thukker bands
    (x1.9 lowsec, x0.1 high AND null; verified live SDE 2026-08-21)."""
    lowsec = BuildSetting(
        structure_type_id=config.STRUCTURE_TYPE_AZBEL,
        security=0.25,
        me_rig="thukker",
        te_rig="thukker",
    )
    mfg = config.ACTIVITY_MANUFACTURING
    # Capital component (group 873): -3.7 x 1.9
    assert industry.rig_multiplier(
        lowsec, mfg, "material", group_id=873
    ) == pytest.approx(1 - 0.037 * 1.9)
    # Advanced capital component (913): same full bonus
    assert industry.rig_multiplier(
        lowsec, mfg, "material", group_id=913
    ) == pytest.approx(1 - 0.037 * 1.9)
    # Plain T2 component (334): the standard -2.0 leg on Thukker bands
    assert industry.rig_multiplier(
        lowsec, mfg, "material", group_id=334
    ) == pytest.approx(1 - 0.020 * 1.9)
    # Time: -20 x 1.9
    assert industry.rig_multiplier(
        lowsec, mfg, "time", group_id=873
    ) == pytest.approx(1 - 0.200 * 1.9)
    # High and null are both nearly dead (x0.1)
    for security in (0.9, -0.2):
        s = BuildSetting(
            structure_type_id=config.STRUCTURE_TYPE_AZBEL,
            security=security,
            me_rig="thukker",
        )
        assert industry.rig_multiplier(
            s, mfg, "material", group_id=873
        ) == pytest.approx(1 - 0.037 * 0.1)


def test_thukker_combines_with_structure_bonus(ref):
    """End to end through build_multiplier: Azbel 0.99 structure ME x the
    Thukker lowsec capital-component leg."""
    s = BuildSetting(
        structure_type_id=config.STRUCTURE_TYPE_AZBEL,
        security=0.25,
        me_rig="thukker",
    )
    assert industry.build_multiplier(
        ref, s, config.ACTIVITY_MANUFACTURING, "material", group_id=873
    ) == pytest.approx(0.99 * (1 - 0.037 * 1.9))


def test_unit_quantity_floors_at_one_per_run():
    """The costing walks' continuous per-unit figure can never go below
    1/run: a base-qty-1 material consumes exactly 1.0 per run at every ME
    and rig bonus (matches required_quantity's max(runs, ...) clamp)."""
    assert industry.unit_quantity(1, 10, 0.9484, 1) == 1.0
    assert industry.unit_quantity(1, 0, 1.0, 1) == 1.0
    # Above the floor the continuous figure is untouched
    assert industry.unit_quantity(100, 10, 1.0, 1) == pytest.approx(90.0)
    # Portion size divides after the floor (per PRODUCT unit)
    assert industry.unit_quantity(1, 10, 1.0, 200) == pytest.approx(1 / 200)
