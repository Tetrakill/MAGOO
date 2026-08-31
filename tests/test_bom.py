"""BOM expansion against the real imported SDE."""

import pytest

from magoo import bom, config
from magoo.industry import BuildSetting


@pytest.fixture(scope="module")
def hulk_bom(ref):
    return bom.expand(ref, ref.type_id("Hulk"), 1)


def test_final_product_at_depth_zero(ref, hulk_bom):
    hulk = hulk_bom[ref.type_id("Hulk")]
    assert hulk.depth == 0
    assert hulk.quantity == 1
    assert hulk.buildable


def test_expands_to_raw_minerals(ref, hulk_bom):
    tritanium = hulk_bom.get(ref.type_id("Tritanium"))
    assert tritanium is not None
    assert not tritanium.buildable
    assert tritanium.quantity > 0


def test_multi_stage_depth(hulk_bom):
    """A T2 ship chain runs hull -> components -> reactions -> raw: the
    prior build's sanity anchor was 78 items across 5 depths."""
    assert max(i.depth for i in hulk_bom.values()) >= 4
    assert 60 <= len(hulk_bom) <= 100


def test_includes_reaction_stages(hulk_bom):
    assert any(
        i.activity_id == config.ACTIVITY_REACTION for i in hulk_bom.values()
    )


def test_no_skills_in_bom(ref, hulk_bom):
    assert not any(ref.is_skill(t) for t in hulk_bom)


def test_all_quantities_positive(hulk_bom):
    assert all(i.quantity >= 1 for i in hulk_bom.values())


def test_me_reduces_quantities(ref):
    """ME10 everywhere must not increase any requirement vs ME0."""
    base = bom.expand(ref, ref.type_id("Hulk"), 1)
    researched = bom.expand(
        ref, ref.type_id("Hulk"), 1, me_te=lambda bp, act: (10, 20)
    )
    worse = [
        t
        for t in researched
        if t in base and researched[t].quantity > base[t].quantity
    ]
    assert not worse


def test_build_settings_reduce_quantities(ref):
    """Rigged engineering complexes must not increase any requirement."""
    base = bom.expand(ref, ref.type_id("Hulk"), 1)
    settings = {
        cls: BuildSetting(
            structure_type_id=config.STRUCTURE_TYPE_SOTIYO,
            security=-0.2,
            me_rig="t2",
        )
        for cls in config.ITEM_CLASSES
    }
    bonused = bom.expand(ref, ref.type_id("Hulk"), 1, build_settings=settings)
    worse = [
        t
        for t in bonused
        if t in base and bonused[t].quantity > base[t].quantity
    ]
    assert not worse


def test_shared_intermediates_aggregate(ref):
    """Expanding 2 Hulks doubles runs, so every requirement is >= the
    single-Hulk requirement and <= double it."""
    one = bom.expand(ref, ref.type_id("Hulk"), 1)
    two = bom.expand(ref, ref.type_id("Hulk"), 2)
    for t, item in one.items():
        assert t in two
        assert item.quantity <= two[t].quantity <= 2 * item.quantity


# --- Known-good quantity anchors (hand-checkable against the SDE; deep
# --- values re-baseline on SDE import, like the 78-item count) -------------


def test_depth1_quantities_match_the_blueprint(ref):
    one = bom.expand(ref, ref.type_id("Hulk"), 1)

    def qty(name):
        return one[ref.type_id(name)].quantity

    assert qty("Construction Blocks") == 150
    assert qty("Covetor") == 1
    assert qty("Ion Thruster") == 60
    assert qty("R.A.M.- Starship Tech") == 15
    assert one[ref.type_id("Tritanium")].quantity == 1_600_556  # deep total


def test_shared_demand_merges_before_run_rounding(ref):
    """A shared intermediate's children size to ceil-of-sum runs, not
    sum-of-ceils (per-edge rounding inflated Ferrofluid to 700 and the
    isotopes by 58% before the 2026-08-20 rework)."""
    eight = bom.expand(ref, ref.type_id("Hulk"), 8)

    def qty(name):
        return eight[ref.type_id(name)].quantity

    assert qty("Ferrofluid") == 600
    assert qty("Hyperflurite") == 600
    assert qty("Prometium") == 1000
    assert qty("Nitrogen Isotopes") == 7650
    assert qty("Helium Fuel Block") == 2595


# --- Cycle containment (audit 2026-08-27) -----------------------------------


class _CycleRef:
    """Synthetic graph: P(1) -> A(2) and D(4); A <-> B(3) is a 2-cycle;
    B -> D; D -> raw R(5). D's own build path is acyclic, so only the
    cycle members A and B may be demoted to raw."""

    _EDGES = {
        1: ((2, 1), (4, 2)),
        2: ((3, 1),),
        3: ((2, 1), (4, 1)),
        4: ((5, 1),),
    }

    class _Blueprint:
        def __init__(self, product):
            self.blueprint_id = product * 100
            self.activity_id = config.ACTIVITY_MANUFACTURING
            self.portion_size = 1
            self.base_time = 100

    class _Info:
        def __init__(self, type_id):
            self.name = f"T{type_id}"
            self.group_id = 0
            self.category_id = 0

    def blueprint_for_product(self, type_id):
        return self._Blueprint(type_id) if type_id in self._EDGES else None

    def materials(self, blueprint_id, activity_id):
        return self._EDGES[blueprint_id // 100]

    def type_info(self, type_id):
        return self._Info(type_id)


def test_cycle_demotes_only_cycle_members():
    """A 2-cycle above a shared component demotes the cycle members alone;
    the acyclic descendant keeps its blueprint and expands normally (the
    old demotion flattened the whole subtree under the cycle)."""
    items = bom.expand(_CycleRef(), 1, 10)
    assert items[2].quantity == 10 and not items[2].buildable  # cycle: raw
    assert 3 not in items  # only the demoted cycle head demanded it
    d = items[4]
    assert d.buildable  # acyclic descendant survives
    assert d.quantity == 20  # demand from P only; B went raw
    assert items[5].quantity == 20  # D's own chain still expands
