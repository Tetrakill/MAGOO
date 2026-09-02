"""ME/TE/facility/rig math and job cost (PROJECT.md §5).

Bonus model (design change 2026-08-15): the user configures build settings
globally per ITEM CLASS (capital ships, T1/T2 ships, capital/advanced
components, structures — Upwell structures, Standup rigs/modules and
structure components, v1.9 — reactions, other) — structure type, system
security, ME/TE rig tier (none/T1/T2/Thukker where applicable), system cost
index, and tax rate. The rig tier is asserted
by the user per class, so no rig-applicability filtering is needed at
planning time.

Magnitudes:
- Structure bonuses are read from the structure type's dogma attributes via
  CCP's industryModifierSources data (e.g. strEngMatBonus 0.99 / strEngTimeBonus
  0.70 on a Sotiyo, strReactionTimeMultiplier 0.75 on a Tatara).
- Rig bonuses use the universal verified percentages (T1 -2.0%/-20%,
  T2 -2.4%/-24%) scaled by the rig family's own security bands:
  engineering rigs 1.0/1.9/2.1, reaction (refinery) rigs 1.0 low / 1.1
  null-WH (no highsec band — refineries cannot deploy there).
"""

import math
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class BuildSetting:
    """Per-item-class build configuration (single global row per class)."""

    structure_type_id: int | None = None  # None = NPC station, no bonuses
    security: float = 1.0
    me_rig: str = "none"  # none / t1 / t2
    te_rig: str = "none"
    system_cost_index: float = 0.0
    tax_rate: float = config.NPC_STATION_FACILITY_TAX


NPC_STATION = BuildSetting()


# ---------------------------------------------------------------------------
# Item classification
# ---------------------------------------------------------------------------


def classify_item(ref, type_id: int, activity_id: int | None = None) -> str:
    """Map an item to the item class whose build settings govern it.

    activity_id is the blueprint activity that produces the item (reactions
    classify by activity, not group); pass None for unbuildable items.
    """
    if activity_id == config.ACTIVITY_REACTION:
        return "reactions"
    info = ref.type_info(type_id)
    if info.category_id == config.CATEGORY_SHIP:
        if info.group_id in config.CAPITAL_SHIP_GROUPS:
            return "capital_ships"
        tech = ref.attribute_by_name(type_id, config.ATTR_TECH_LEVEL, 1.0)
        # >= not ==: techLevel-3 Strategic Cruisers deliberately share the
        # t2_ships facility setup (decision 2026-08-20) — do not "fix".
        return "t2_ships" if tech >= 2.0 else "t1_ships"
    if info.category_id == config.CATEGORY_SUBSYSTEM:
        # T3 subsystems deliberately share the t2_ships facility setup:
        # CCP's "Medium T2 Ships" rig filter (8) covers category 32 the
        # same way the >= above keeps techLevel-3 hulls in t2_ships
        # (decision 2026-08-31) — do not "fix".
        return "t2_ships"
    if info.group_id in config.BASIC_CAPITAL_COMPONENT_GROUPS:
        return "basic_capital_components"
    if info.group_id in config.ADVANCED_COMPONENT_GROUPS:
        return "advanced_components"
    # Upwell structures, Standup rigs/modules and structure components share
    # CCP's "Structures" rig filter; by category/group only — structures
    # carry no techLevel and rigs' T1/T2 split is not a build-setting split.
    if (
        info.category_id in config.STRUCTURE_CLASS_CATEGORIES
        or info.group_id in config.STRUCTURE_COMPONENT_GROUPS
    ):
        return "structures"
    return "other"


# ---------------------------------------------------------------------------
# Bonus multipliers
# ---------------------------------------------------------------------------


def structure_multiplier(
    ref, structure_type_id: int, activity_id: int, kind: str
) -> float:
    """Structure bonus multiplier for one kind (material/time/cost), read
    from the structure's own attributes via industryModifierSources. 1.0 for
    unknown structures or kinds the structure does not bonus."""
    mult = 1.0
    for entry_kind, attr_id, filter_id in ref.industry_modifiers(
        structure_type_id, activity_id
    ):
        if entry_kind != kind or filter_id is not None:
            continue
        value = ref.attribute_by_id(structure_type_id, attr_id)
        if value is not None:
            mult *= value
    return mult


def rig_multiplier(
    setting: BuildSetting,
    activity_id: int,
    kind: str,
    group_id: int | None = None,
) -> float:
    """Rig bonus multiplier from the class's asserted rig tier x the rig
    family's security band multiplier (engineering, reaction, and Thukker
    rigs each carry different bands).

    group_id is the PRODUCT's group: the Thukker tier's ME magnitude splits
    by covered group (-3.7% for capital-component groups, the standard
    -2.0% for plain T2/T3 components) — see config's Thukker constants.

    Cost rigs exist only in the lab families (v1.22): the invention and
    copying classes' single asserted tier (stored in me_rig) is their
    Standup Invention/Copy/Laboratory Optimization rig, -10%/-12% job cost
    on the engineering security bands. Manufacturing/reaction rig families
    carry a zero cost bonus, so every other activity's cost multiplier
    stays 1.0 here."""
    if kind == "cost":
        if activity_id not in (
            config.ACTIVITY_INVENTION,
            config.ACTIVITY_COPYING,
        ):
            return 1.0
        pct = config.LAB_RIG_COST_PERCENT.get(setting.me_rig, 0.0)
        if not pct:
            return 1.0
        return 1.0 + (
            pct * config.rig_security_multiplier(setting.security, activity_id)
        ) / 100.0
    tier = (
        setting.me_rig
        if kind == "material"
        else setting.te_rig
        if kind == "time"
        else None
    )
    if tier is None:
        return 1.0
    if tier == "thukker":
        if kind == "material":
            pct = (
                config.THUKKER_RIG_ME_PERCENT_CAPITAL
                if group_id in config.THUKKER_FULL_BONUS_GROUPS
                else config.THUKKER_RIG_ME_PERCENT_STANDARD
            )
        else:
            pct = config.THUKKER_RIG_TE_PERCENT
        return 1.0 + (
            pct * config.thukker_security_multiplier(setting.security)
        ) / 100.0
    pct = (
        config.RIG_ME_PERCENT[tier]
        if kind == "material"
        else config.RIG_TE_PERCENT[tier]
    )
    if not pct:
        return 1.0
    return 1.0 + (
        pct * config.rig_security_multiplier(setting.security, activity_id)
    ) / 100.0


def build_multiplier(
    ref,
    setting: BuildSetting,
    activity_id: int,
    kind: str,
    group_id: int | None = None,
) -> float:
    """Combined structure x rig multiplier for one bonus kind. NPC stations
    have no structure bonus and cannot fit rigs. group_id is the product's
    group, needed only for the Thukker tier's split ME magnitude."""
    if setting.structure_type_id is None:
        return 1.0
    return structure_multiplier(
        ref, setting.structure_type_id, activity_id, kind
    ) * rig_multiplier(setting, activity_id, kind, group_id)


# ---------------------------------------------------------------------------
# Skill time multipliers (user-entered levels — PROJECT.md v1.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillLevels:
    """The user's industry skill levels, entered in settings (not ESI)."""

    industry: int = 5
    advanced_industry: int = 5
    reactions: int = 5
    adv_ship_construction: int = 5
    starship_engineering: int = 5
    science: int = 5
    # v1.9 (appended last: tests construct SkillLevels positionally)
    outpost_construction: int = 5
    # v1.22 (appended last, same rule): racial "* Encryption Methods" level
    # for the invention chance — datacore sciences ride the two levels above.
    encryption: int = 5


def _per_bp_skill_level(skill_name: str, skills: SkillLevels) -> int:
    """Which user-entered level covers a required science/construction
    skill, by name family. Outpost Construction (Upwell structures, Standup
    modules, structure fighters) has its own level since v1.9 — before
    that it silently rode on the science level; the racial Encryption
    Methods skills (invention chance, v1.22) likewise get their own."""
    if skill_name == config.SKILL_NAME_OUTPOST_CONSTRUCTION:
        return skills.outpost_construction
    if skill_name.endswith("Ship Construction"):
        return skills.adv_ship_construction
    if skill_name.endswith("Starship Engineering"):
        return skills.starship_engineering
    if skill_name.endswith(config.SKILL_SUFFIX_ENCRYPTION):
        return skills.encryption
    return skills.science


def skill_time_multiplier(
    ref, blueprint_id: int, activity_id: int, skills: SkillLevels
) -> float:
    """Combined job-time multiplier from skills.

    Reactions: Reactions -4%/level. Manufacturing: Industry -4%/level,
    Advanced Industry -3%/level, plus each required science/construction
    skill's manufactureTimePerLevel (-1%/level) at the user's entered level.
    """
    if activity_id == config.ACTIVITY_REACTION:
        return 1.0 + (config.SKILL_TIME_REACTIONS_PCT * skills.reactions) / 100.0
    mult = (
        1.0 + (config.SKILL_TIME_INDUSTRY_PCT * skills.industry) / 100.0
    ) * (
        1.0
        + (config.SKILL_TIME_ADV_INDUSTRY_PCT * skills.advanced_industry)
        / 100.0
    )
    for skill_type_id, _required in ref.blueprint_skills(
        blueprint_id, activity_id
    ):
        per_level = ref.attribute_by_name(
            skill_type_id, config.ATTR_MANUFACTURE_TIME_PER_LEVEL
        )
        if per_level is None:
            continue  # Industry itself, prerequisites with no time bonus
        level = _per_bp_skill_level(
            ref.type_info(skill_type_id).name, skills
        )
        mult *= 1.0 + (per_level * level) / 100.0
    return mult


# ---------------------------------------------------------------------------
# Invention (v1.22 — pure math; the caller assembles prices and settings)
# ---------------------------------------------------------------------------


def invention_probability(
    base: float,
    science_levels,
    encryption_level: int,
    prob_mult: float = 1.0,
) -> float:
    """Invention success chance: base x (1 + Σ science/30 + encryption/40)
    x decryptor multiplier, capped at certainty. science_levels carries one
    entry per required datacore science skill (two in practice), each
    resolved through _per_bp_skill_level's name families.

    Rounded to 12 places (review 2026-09-01): the float product lands one
    ulp below exact fractions such as 7/16 (base 0.30, all-V, no
    decryptor), and the engine's ceil/floor sizing then misrounds by a
    whole attempt at every multiple of the fraction."""
    return min(
        1.0,
        round(
            base
            * (1.0 + sum(science_levels) / 30.0 + encryption_level / 40.0)
            * prob_mult,
            12,
        ),
    )


def invented_bpc(
    base_runs: int, me_mod: int = 0, te_mod: int = 0, run_mod: int = 0
) -> tuple[int, int, int]:
    """(me, te, runs) of an invented copy: ME 2 / TE 4 plus the decryptor's
    modifiers; runs = the invention product quantity plus the decryptor's
    run modifier. The clamps guard against data drift only — no in-game
    combination lands below ME 0 / TE 0 / 1 run today."""
    return (
        max(0, config.INVENTED_BASE_ME + me_mod),
        max(0, config.INVENTED_BASE_TE + te_mod),
        max(1, base_runs + run_mod),
    )


def invention_cost_per_run(
    attempt_cost: float, probability: float, runs_per_copy: int
) -> float:
    """Expected invention ISK per licensed run: each attempt succeeds with
    chance `probability` and a success licenses runs_per_copy runs. The
    caller guarantees probability > 0 (base probabilities in data are all
    positive and the skill factor only raises them)."""
    return attempt_cost / (probability * runs_per_copy)


# ---------------------------------------------------------------------------
# Job math
# ---------------------------------------------------------------------------


def required_quantity(
    runs: int, base_qty: int, me_level: int, build_mult: float = 1.0
) -> int:
    """Materials one job consumes. Rounding is applied once per job, not per
    run, and a job always needs at least `runs` units of each material."""
    me_mult = 1.0 - me_level / 100.0
    return max(
        runs, math.ceil(round(runs * base_qty * me_mult * build_mult, 2))
    )


def unit_quantity(
    base_qty: int, me_level: int, build_mult: float, portion_size: int
) -> float:
    """Continuous per-product-unit input quantity (no per-job rounding) —
    the costing walks' amortized figure. Floored at one unit per run:
    ME/rig bonuses can never take a material below 1/run in-game, so a
    base-qty-1 material consumes exactly 1.0 per run at every job scale
    (mirrors required_quantity's max(runs, ...) clamp)."""
    return (
        max(1.0, base_qty * (1.0 - me_level / 100.0) * build_mult)
        / portion_size
    )


def job_time_seconds(
    base_time: int, runs: int, te_level: int, build_mult: float = 1.0
) -> float:
    """Duration of one job of `runs` runs."""
    return base_time * runs * (1.0 - te_level / 100.0) * build_mult


def job_install_cost(
    eiv: float,
    setting: BuildSetting,
    cost_mult: float = 1.0,
    scc_surcharge: float | None = None,
) -> float:
    """Installation cost. EIV uses base (pre-ME) quantities against CCP
    adjusted prices; computing EIV is the caller's job (market layer).
    scc_surcharge overrides the default 4% job-cost SCC (a settings value
    since 2026-08-21 — the rate lives only in CCP's server config, not the
    SDE, so it is user-adjustable like the other fee constants)."""
    if scc_surcharge is None:
        scc_surcharge = config.SCC_SURCHARGE
    return eiv * (
        setting.system_cost_index * cost_mult
        + setting.tax_rate
        + scc_surcharge
    )
