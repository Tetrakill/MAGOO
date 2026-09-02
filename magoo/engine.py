"""Index run planning — Phases 2 through 8 of PROJECT.md §7.

Phase 1 (snapshotting ESI assets, jobs, skills, and prices) is the province
of esi.py / market.py; the engine consumes their output as a `Snapshot` so
planning is fully testable without network access.

Phases:
  2. Expand demand per pipeline (bom.expand)
  3. Merge into unified demand across pipelines
  4. Targets (buffer %, composite extra runs) and deficits
  5. Buy vs. build sizing under the max-run-duration window
  6. Slot allocation under contention (scipy MILP on build savings)
  7. Final recommendations and flags (ship batching, reaction saturation,
     capacity_limited, low_stock)
  8. Cost-lot bookkeeping primitives (FIFO vintage costing)
"""

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from . import bom, config, costing, industry, store


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    """Phase 1 output: current world state.

    in_progress counts output of active jobs as stock, preventing duplicate
    recommendations for work already underway. slots_available is the
    user-entered pool total per activity (manufacturing / reaction), net
    only of MULTI-CYCLE jobs still running past the next index run —
    single-cycle jobs deliver before planning by design (v1.1, revised
    2026-08-20). adjusted_prices are CCP adjusted prices for EIV; market
    prices are used where an adjusted price is missing.
    """

    on_hand: dict[int, int] = field(default_factory=dict)
    in_progress: dict[int, int] = field(default_factory=dict)
    slots_available: dict[int, int] = field(default_factory=dict)
    prices: dict[int, float] = field(default_factory=dict)
    adjusted_prices: dict[int, float] = field(default_factory=dict)
    # v1.9: types whose price is a region-wide fallback quote (raw leaf
    # with no hub-station order) — persisted per item for the Buy list.
    region_wide: set = field(default_factory=set)
    # v1.10: where each price came from (store.BUY_VENUE_*; a type absent
    # here is a hub quote) and, for structure buys, how many units of the
    # structure's sell ladder still beat the hub landed price. `prices`
    # holds the CHOSEN venue's raw price, so every consumer above is
    # venue-agnostic; only the freight leg looks at the venue.
    buy_venue: dict[int, str] = field(default_factory=dict)
    structure_units_cheaper: dict[int, int] = field(default_factory=dict)
    # Informational (UI: buying power vs. the shopping list) — planning
    # itself does not budget ISK.
    character_isk: float = 0.0
    corporation_isk: float = 0.0

    def price(self, type_id: int) -> float | None:
        return self.prices.get(type_id)

    def venue(self, type_id: int) -> str:
        return self.buy_venue.get(type_id, store.BUY_VENUE_HUB)

    def adjusted(self, type_id: int) -> float | None:
        return self.adjusted_prices.get(type_id, self.prices.get(type_id))


# ---------------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------------


@dataclass
class PlanItem:
    type_id: int
    name: str
    item_class: str
    depth: int
    on_hand_qty: int = 0
    in_progress_qty: int = 0
    merged_min_qty: int = 0
    # The share of merged_min the pipelines directly requested as output
    # (nonzero only for finals). The remainder of a dual-role final's
    # demand is another pipeline's component draw, which nets against
    # stock like any other stage (ruling 2026-08-27).
    requested_qty: int = 0
    target_stock_qty: int = 0
    deficit_qty: int = 0
    recommended_action: str | None = None  # buy / build / both
    blueprint_id: int | None = None
    activity_id: int | None = None
    time_per_run: float | None = None
    portion_size: int = 0
    max_runs_per_job: int = 0
    total_runs_needed: int = 0
    jobs_needed_unconstrained: int = 0
    jobs_allocated: int = 0
    runs_allocated: int = 0
    recommended_build_qty: int = 0
    recommended_buy_qty: int = 0
    build_savings_per_unit: float | None = None
    # The vertically-integrated chain cost per unit behind that savings
    # figure (2026-08-23: persisted, so the UI never reverse-engineers it
    # from price − savings — savings is against the LANDED buy price).
    unit_chain_cost: float | None = None
    # Raw leaves in the savings chain with no price on record (they cost 0
    # in the figure, understating chain cost) — surfaced as a UI badge.
    savings_unpriced_inputs: int = 0
    capacity_limited: bool = False
    low_stock: bool = False
    price_snapshot: float | None = None
    # v1.9: price_snapshot came from a region-wide fallback quote
    price_region_wide: bool = False
    # v1.10: the venue price_snapshot came from (store.BUY_VENUE_*; None
    # when unpriced) and, for structure buys, the units of the structure's
    # sell ladder that still beat the hub landed price — the run page flags
    # the buy as shallow when recommended_buy_qty exceeds it.
    buy_venue: str | None = None
    structure_units_cheaper: int | None = None
    # Hypothetical install fee per product unit at this run's cost indices
    # and adjusted prices — snapshotted for every buildable even when no
    # jobs are planned, so lag-based costing can price any stage from the
    # run it would have been installed at (v1.5).
    unit_install_fee: float | None = None
    pipeline_share: dict[int, int] = field(default_factory=dict)
    # The item's max depth within each attributable pipeline's OWN chain —
    # the depth lag costing prices from (the merged `depth` above is the
    # cross-pipeline max, display-only since 2026-08-20).
    pipeline_depth: dict[int, int] = field(default_factory=dict)
    # Runs on the final product's BPC (min across pipelines that set it);
    # None = uncapped.
    bpc_runs_limit: int | None = None
    # Alchemy (v1.4). On an unrefined-formula row: the composite the route
    # feeds (also marks the row as an alchemy job). On a composite row: the
    # unit-cost comparison, the composite units expected from this cycle's
    # allocated alchemy jobs, and the units credited from unrefined items
    # already on hand / in flight (they count as in-progress stock).
    alchemy_for_type_id: int | None = None
    direct_unit_cost: float | None = None
    alchemy_unit_cost: float | None = None
    alchemy_output_qty: int = 0
    alchemy_credit_qty: int = 0

    @property
    def buildable(self) -> bool:
        return self.blueprint_id is not None


@dataclass
class Plan:
    index_run_id: int | None
    run_number: int
    items: dict[int, PlanItem]
    # v1.22: per-pipeline invention VINTAGE computed by _invention_pass
    # (pipeline_id -> the index_run_invention row as a dict) — the carrier
    # to the persist step; empty when no active pipeline's invention
    # choice resolves.
    invention: dict[int, dict] = field(default_factory=dict)

    def by_action(self, action: str) -> list[PlanItem]:
        return sorted(
            (i for i in self.items.values() if i.recommended_action == action),
            key=lambda i: i.depth,
        )


# ---------------------------------------------------------------------------
# Phases 2-3: expand and merge
# ---------------------------------------------------------------------------


def _blacklist_checker(conn, ref):
    """type_id -> True when the production blacklist says buy, don't build."""
    keys = store.blacklist_categories(conn)
    groups: set[int] = set()
    for key, _label, group_ids in config.BLACKLIST_CATEGORIES:
        if key in keys:
            groups |= group_ids
    t1_hulls = "t1_hulls" in keys
    items = store.blacklist_items(conn)
    if not groups and not t1_hulls and not items:
        return None

    def check(type_id: int) -> bool:
        if type_id in items:
            return True
        info = ref.type_info(type_id)
        if info.group_id in groups:
            return True
        if t1_hulls and info.category_id == config.CATEGORY_SHIP:
            tech = ref.attribute_by_name(type_id, config.ATTR_TECH_LEVEL, 1.0)
            return tech < 2.0
        return False

    return check


def _expand_and_merge(conn, ref, output_qty=None) -> dict[int, PlanItem]:
    """bom.expand each active pipeline, merge into unified demand, and record
    each pipeline's attributable share per item. output_qty (pipeline_id ->
    qty) overrides a pipeline's expansion quantity — the steady-state path's
    built-scale expansion; the real /run path never passes it."""
    class_settings = store.get_class_settings(conn)
    me_te = store.me_te_resolver(conn)
    blacklist = _blacklist_checker(conn, ref)
    pipelines = store.active_pipelines(conn)
    # The blacklist's finals exemption must span ALL active pipelines, not
    # just the one being expanded: the merge sets blueprint_id only when a
    # PlanItem is first created, so a pipeline expanded earlier that
    # consumes another pipeline's final as a blacklisted intermediate would
    # otherwise strip that final's buildability (order-dependent).
    if blacklist is not None:
        finals = {p["final_product_type_id"] for p in pipelines}
        category_check = blacklist
        blacklist = lambda t: t not in finals and category_check(t)
    merged: dict[int, PlanItem] = {}
    for pipeline in pipelines:
        quantity = pipeline["output_qty_per_run"]
        if output_qty is not None:
            quantity = output_qty.get(pipeline["pipeline_id"], quantity)
        items = bom.expand(
            ref,
            pipeline["final_product_type_id"],
            quantity,
            build_settings=class_settings,
            me_te=me_te,
            blacklist=blacklist,
        )
        for type_id, item in items.items():
            plan_item = merged.get(type_id)
            if plan_item is None:
                plan_item = merged[type_id] = PlanItem(
                    type_id=type_id,
                    name=item.name,
                    item_class=item.item_class,
                    depth=item.depth,
                    blueprint_id=item.blueprint_id,
                    activity_id=item.activity_id,
                    portion_size=item.portion_size,
                )
            plan_item.merged_min_qty += item.quantity
            plan_item.depth = max(plan_item.depth, item.depth)
            plan_item.pipeline_share[pipeline["pipeline_id"]] = (
                plan_item.pipeline_share.get(pipeline["pipeline_id"], 0)
                + item.quantity
            )
            plan_item.pipeline_depth[pipeline["pipeline_id"]] = item.depth
            if type_id == pipeline["final_product_type_id"]:
                # The (possibly steady-state-overridden) requested output —
                # the share of a final's demand that keeps the exact
                # ignore-stock rule when the final is also consumed as
                # another pipeline's intermediate.
                plan_item.requested_qty += quantity
                if pipeline["runs_per_bpc"]:
                    plan_item.bpc_runs_limit = (
                        pipeline["runs_per_bpc"]
                        if plan_item.bpc_runs_limit is None
                        else min(
                            plan_item.bpc_runs_limit, pipeline["runs_per_bpc"]
                        )
                    )
    return merged


# ---------------------------------------------------------------------------
# Phase 4: targets and deficits
# ---------------------------------------------------------------------------


def _apply_targets(
    conn,
    ref,
    merged: dict[int, PlanItem],
    snapshot: Snapshot,
    alchemy: bool = True,
):
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    buffer_mult = 1.0 + settings.stockpile_buffer
    # Final ship counts are exact — the buffer protects the feeder stages,
    # not the finished output.
    final_products = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }

    for item in merged.values():
        if item.type_id in final_products:
            item.target_stock_qty = item.merged_min_qty
        else:
            # round-before-ceil kills binary-float noise (100 * 1.1 ==
            # 110.00000000000001 would otherwise ceil to 111), mirroring
            # industry.required_quantity's guard.
            item.target_stock_qty = math.ceil(
                round(item.merged_min_qty * buffer_mult, 9)
            )

    # Composite reaction inputs get extra runs' worth of material on top.
    extra_runs = settings.composite_reaction_extra_runs
    if extra_runs > 0:
        for item in merged.values():
            if (
                item.activity_id != config.ACTIVITY_REACTION
                or ref.type_info(item.type_id).group_id
                not in config.COMPOSITE_REACTION_GROUPS
            ):
                continue
            mat_mult = industry.build_multiplier(
                ref,
                class_settings.get(item.item_class, industry.NPC_STATION),
                item.activity_id,
                "material",
            )
            for material_id, base_qty in ref.materials(
                item.blueprint_id, item.activity_id
            ):
                if material_id in merged:
                    merged[material_id].target_stock_qty += (
                        industry.required_quantity(
                            extra_runs, base_qty, 0, mat_mult
                        )
                    )

    for item in merged.values():
        item.on_hand_qty = snapshot.on_hand.get(item.type_id, 0)
        item.in_progress_qty = snapshot.in_progress.get(item.type_id, 0)
        _stamp_price(item, snapshot)

    # Unrefined items on hand or in flight count as their reprocess outputs
    # (composite AND recovered inputs) at the asserted yield — as
    # in-progress, not on-hand, because a manual reprocess still stands
    # between them and usable stock. Once the user actually reprocesses,
    # the next ESI snapshot replaces the credit with real items. Skipped
    # entirely when the caller plans with alchemy off (steady state), so
    # a stocked unrefined item can never fake credits there.
    if alchemy:
        _apply_unrefined_credits(conn, ref, merged, snapshot)

    for item in merged.values():
        if item.type_id in final_products:
            # Final ships always build their requested quantities — the
            # line advances every cycle regardless of stock or in-flight
            # jobs (those are the previous wave, bound for sale). But a
            # final consumed as ANOTHER pipeline's intermediate nets that
            # component share against stock like any other stage (ruling
            # 2026-08-27); single-role finals have no component share and
            # keep the exact rule unchanged.
            component_share = item.merged_min_qty - item.requested_qty
            item.deficit_qty = item.requested_qty + max(
                0,
                component_share - item.on_hand_qty - item.in_progress_qty,
            )
        else:
            # A stage must END the cycle back at target, so the deficit
            # includes what this cycle's downstream jobs will consume
            # (merged_min = one cycle's consumption). Steady state: stock
            # at target -> build exactly one cycle's worth every cycle.
            # Without this term, a fully-stocked stage plans zero jobs,
            # gets drained by this cycle's consumers, and the whole line
            # oscillates full/empty one cycle out of phase.
            item.deficit_qty = max(
                0,
                item.target_stock_qty
                + item.merged_min_qty
                - item.on_hand_qty
                - item.in_progress_qty,
            )


def _apply_unrefined_credits(conn, ref, merged: dict[int, PlanItem], snapshot):
    settings = store.get_settings(conn)
    if not settings.alchemy_enabled or settings.alchemy_reprocess_yield <= 0:
        return
    yield_ = settings.alchemy_reprocess_yield
    for route in ref.alchemy_routes().values():
        unrefined_qty = snapshot.on_hand.get(
            route.unrefined_id, 0
        ) + snapshot.in_progress.get(route.unrefined_id, 0)
        if unrefined_qty <= 0:
            continue
        outputs = ((route.composite_id, route.composite_qty), *route.recovered)
        for type_id, base_qty in outputs:
            item = merged.get(type_id)
            if item is None:
                continue
            credit = math.floor(unrefined_qty * base_qty * yield_)
            if credit > 0:
                item.in_progress_qty += credit
                item.alchemy_credit_qty += credit


# ---------------------------------------------------------------------------
# Phase 5: buy vs. build sizing
# ---------------------------------------------------------------------------


def _game_job_run_cap(time_per_run: float, blueprint=None) -> int:
    """In-game per-job run ceiling, one rule for both activities
    (user-verified 2026-08-21): runs keep being added while the job's total
    MODIFIED time is under 30 days, so the last run may overhang —
    ceil(30d / tpr) — and a single run longer than 30 days installs as
    1 run. Pass the blueprint for REACTIONS only: the formula's own
    maxProductionLimit is kept as an additional ceiling where lower
    (still unverified in client; manufacturing maxProductionLimit is a
    copy-runs concept and never applies)."""
    cap = max(1, math.ceil(config.MAX_JOB_SECONDS / time_per_run))
    if blueprint is not None and blueprint.max_runs:
        cap = min(cap, blueprint.max_runs)
    return max(1, cap)


def _skill_levels(settings) -> industry.SkillLevels:
    # v1.22: the mapping lives on Settings so costing's invention math can
    # reuse it without importing the engine.
    return settings.skill_levels()


def _size_jobs(conn, ref, merged: dict[int, PlanItem]):
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    me_te = store.me_te_resolver(conn)
    skills = _skill_levels(settings)
    window_seconds = settings.max_run_duration_hours * 3600.0

    for item in merged.values():
        if item.deficit_qty <= 0:
            continue
        if not item.buildable:
            # Raw inputs are bought just-in-time in Phase 7, sized to the
            # consumption of the jobs actually allocated — not to a
            # stockpile target.
            continue

        setting = class_settings.get(item.item_class, industry.NPC_STATION)
        _me, te = me_te(item.blueprint_id, item.activity_id)
        time_mult = industry.build_multiplier(
            ref,
            setting,
            item.activity_id,
            "time",
            group_id=ref.type_info(item.type_id).group_id,
        )
        blueprint = ref.blueprint_for_product(item.type_id)
        item.time_per_run = industry.job_time_seconds(
            blueprint.base_time, 1, te, time_mult
        ) * industry.skill_time_multiplier(
            ref, item.blueprint_id, item.activity_id, skills
        )
        # One job per slot for the full cycle; jobs longer than the window
        # span multiple cycles (their output counts as in-progress stock,
        # and snapshot_from_state nets them from the slot pool while they
        # run). A job can never exceed the runs on its blueprint copy, and
        # reactions have a hard per-job run cap (game limit) plus the
        # formula's own maxProductionLimit.
        if item.time_per_run <= 0:
            raise ValueError(
                f"non-positive job time for {item.name}: check skill "
                "levels, TE, and structure/rig settings"
            )
        item.max_runs_per_job = max(
            1, math.floor(window_seconds / item.time_per_run)
        )
        item.max_runs_per_job = min(
            item.max_runs_per_job,
            _game_job_run_cap(
                item.time_per_run,
                blueprint
                if item.activity_id == config.ACTIVITY_REACTION
                else None,
            ),
        )
        if item.bpc_runs_limit is not None:
            item.max_runs_per_job = min(
                item.max_runs_per_job, item.bpc_runs_limit
            )
        item.total_runs_needed = math.ceil(
            item.deficit_qty / item.portion_size
        )
        # Ships build in batch multiples; capacity gets the final word in
        # Phase 7. When runs-per-BPC is set it becomes the batch unit (build
        # whole blueprint copies, never a partial BPC); otherwise the global
        # ship batch multiple applies. Capitals, freighters, and jump
        # freighters are exempt — they build in exact quantities.
        info = ref.type_info(item.type_id)
        if (
            info.category_id == config.CATEGORY_SHIP
            and info.group_id not in config.EXACT_QTY_SHIP_GROUPS
        ):
            multiple = item.bpc_runs_limit or settings.ship_batch_multiple
            if multiple > 1:
                item.total_runs_needed = (
                    math.ceil(item.total_runs_needed / multiple) * multiple
                )
        item.jobs_needed_unconstrained = math.ceil(
            item.total_runs_needed / item.max_runs_per_job
        )
        item.recommended_action = "build"


# ---------------------------------------------------------------------------
# Phase 6: slot allocation under contention
# ---------------------------------------------------------------------------


def _unit_build_cost(
    ref, blueprint_id, activity_id, portion_size, setting, me, snapshot,
    runs: int = 1,
    group_id: int | None = None,
    scc_surcharge: float | None = None,
    price_of=None,
):
    """SINGLE-STAGE (material cost + install cost) per product unit, priced
    at the scale of a `runs`-run job so once-per-job rounding amortizes the
    way it does in the jobs actually installed. Since 2026-08-21 this feeds
    only the alchemy route comparison (both routes' inputs are raw goo, so
    single-stage is internally consistent there); build savings use the
    vertically-integrated _chain_coster below. Materials are priced by
    `price_of` (the alchemy pass passes the LANDED price, 2026-08-24 —
    freight does not cancel between the routes: at 55% yield the unrefined
    route hauls ~1.8× the volume per composite unit), falling back to the
    raw snapshot price. Missing prices count as zero. EIV uses base pre-ME
    quantities against adjusted prices."""
    runs = max(1, runs)
    if price_of is None:
        price_of = snapshot.price
    mat_mult = industry.build_multiplier(
        ref, setting, activity_id, "material", group_id=group_id
    )
    material_cost = 0.0
    eiv_per_run = 0.0
    for material_id, base_qty in ref.materials(blueprint_id, activity_id):
        mat_price = price_of(material_id) or 0.0
        material_cost += (
            industry.required_quantity(runs, base_qty, me, mat_mult)
            * mat_price
        )
        eiv_per_run += base_qty * (snapshot.adjusted(material_id) or 0.0)
    cost_mult = industry.build_multiplier(ref, setting, activity_id, "cost")
    install = industry.job_install_cost(
        eiv_per_run * runs, setting, cost_mult, scc_surcharge=scc_surcharge
    )
    return (material_cost + install) / (portion_size * runs)


def _landed_price(ref, settings, snapshot, type_id: int) -> float | None:
    """Landed buy price from a Snapshot: costing.landed_price over the
    chosen venue's raw price (v1.10). None when unpriced."""
    return costing.landed_price(
        ref, settings, snapshot.price(type_id), snapshot.venue(type_id), type_id
    )


def _ceil(x: float) -> int:
    """math.ceil after rounding away float noise (review 2026-09-01): the
    invention chance and the overbuild multipliers are short decimals whose
    quotients and products land an ulp off exact integers (7 / 0.4375,
    10 × 1.1), and a raw ceil would plan a whole extra attempt or copy."""
    return math.ceil(round(x, 9))


def _floor(x: float) -> int:
    """math.floor with the same guard — 16 × 0.4375 must credit 7 copies."""
    return math.floor(round(x, 9))


def _stamp_price(item: PlanItem, snapshot: Snapshot) -> None:
    """The item's plan-time price and its provenance, stamped together so
    the four fields can never diverge: price_snapshot, the v1.9
    region-wide bit, and (v1.10) the buy venue plus, for structure buys,
    the structure's depth figure. An unpriced item carries no venue."""
    item.price_snapshot = snapshot.price(item.type_id)
    item.price_region_wide = item.type_id in snapshot.region_wide
    if item.price_snapshot is None:
        item.buy_venue = None
        item.structure_units_cheaper = None
        return
    item.buy_venue = snapshot.venue(item.type_id)
    item.structure_units_cheaper = (
        snapshot.structure_units_cheaper.get(item.type_id)
        if item.buy_venue == store.BUY_VENUE_STRUCTURE
        else None
    )


def _invention_configs(conn, ref) -> dict[int, tuple]:
    """pipeline_id -> (pipeline row, InventionSource, Decryptor | None) for
    active use_invention pipelines whose choice still resolves
    (costing.resolve_invention — the one stale-config rule since the
    2026-09-01 review; v1.22, multi-source/T3 since 2026-08-31). A stale
    pipeline — source gone after SDE drift, a multi-source final with a
    missing/invalid choice, or a decryptor id that no longer resolves —
    silently falls back to the manual bpc_cost_isk path; the Pipelines
    page shows it its Off control."""
    configs: dict[int, tuple] = {}
    for pipeline in store.active_pipelines(conn):
        resolved = costing.resolve_invention(ref, pipeline)
        if resolved is None:
            continue
        source, decryptor = resolved
        configs[pipeline["pipeline_id"]] = (pipeline, source, decryptor)
    return configs


def materialize_invention(
    conn, ref, pipeline_id: int, source, decryptor, chosen_id
) -> tuple[int, int, int]:
    """Write a pipeline's invention choice (v1.22 materialise-at-config-
    time): use_invention on, the decryptor and source choice, runs_per_bpc
    = the invented copy's runs — the user's own value stashed in
    manual_runs_per_bpc on the OFF -> ON transition only (SET right-hand
    sides read the OLD row, so one statement suffices) — and the invented
    ME/TE pinned on the T2 blueprint. Returns (me, te, runs). Shared by
    the Pipelines-page save and rematerialize_invention."""
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
        "runs_per_bpc = ?, modified_at = datetime('now') "
        "WHERE pipeline_id = ?",
        (decryptor.type_id if decryptor else None, chosen_id, runs, pipeline_id),
    )
    store.set_blueprint_setting(conn, source.product_blueprint_id, me, te)
    return me, te, runs


def rematerialize_invention(conn, ref) -> int:
    """After an SDE import: re-derive every invention pipeline's
    materialised runs_per_bpc and ME/TE from the NEW reference data
    (review 2026-09-01: they were written once at config time while every
    cost path recomputed from ref data, so a rebalanced decryptor or relic
    tier desynchronised the batch size the run plans from the copies the
    vintage and the Invention tab assume). Stale configs — source or
    decryptor gone — are left for the Pipelines page's Off control.
    Returns the number rewritten."""
    rewritten = 0
    for pipeline in conn.execute(
        "SELECT * FROM pipeline WHERE use_invention = 1"
    ).fetchall():
        resolved = costing.resolve_invention(ref, pipeline)
        if resolved is None:
            continue
        source, decryptor = resolved
        materialize_invention(
            conn, ref, pipeline["pipeline_id"], source, decryptor,
            pipeline["invention_source_blueprint_id"],
        )
        rewritten += 1
    conn.commit()
    return rewritten


def _chain_coster(conn, ref, snapshot: Snapshot):
    """Vertically-integrated chain cost per unit (decision 2026-08-21).

    Returns (chain, buy_cost): chain(type_id) -> (cost_per_unit,
    unpriced_raw_leaves), and buy_cost(type_id) -> the LANDED buy price
    (venue raw price + that venue's courier rate × packaged m³, None when
    unpriced) — the same leg the chain uses for bought inputs, exposed so
    the savings figure compares like with like (2026-08-23).

    Each buildable stage costs its install fee plus its inputs, where every
    buildable input is priced at min(buy it at market + inbound freight,
    build it from ITS chain) — the figure is self-consistent with the buy
    decisions the negative-savings rule takes. Bought units (raw leaves,
    blacklisted stages, and stages the market undercuts) carry inbound
    freight on packaged volume; finals add their pipeline's per-hull BPC
    amortization. Raw leaves with no price on record cost zero and are
    COUNTED so the UI can flag the figure as understated. Mirrors Phase-2
    semantics: blacklist never applies to finals, cycles are raw; unlike
    the Profit page's what-if walk it may BUY a mid-chain stage, which is
    exactly what makes it match real economics."""
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    me_te = store.me_te_resolver(conn)
    blacklist = _blacklist_checker(conn, ref)
    pipelines = store.active_pipelines(conn)
    finals = {p["final_product_type_id"] for p in pipelines}
    bpc_per_unit: dict[int, float] = {}
    invention_configs = _invention_configs(conn, ref)
    for p in pipelines:
        tid = p["final_product_type_id"]
        if tid in bpc_per_unit:
            continue
        cfg = invention_configs.get(p["pipeline_id"])
        if cfg is not None:
            # v1.22: computed expected invention cost per licensed run
            # replaces the hand-entered bpc figure — same constant per-unit
            # adder on finals, so the MILP objective shape is unchanged.
            _pipeline, source, decryptor = cfg
            cost = costing.invention_cost(
                ref, settings, class_settings, source, decryptor,
                price_of=lambda t: _landed_price(ref, settings, snapshot, t),
                adjusted_of=snapshot.adjusted,
            )
            blueprint = ref.blueprint_for_product(tid)
            bpc_per_unit[tid] = cost.cost_per_run / (
                blueprint.portion_size if blueprint else 1
            )
        elif p["bpc_cost_isk"] and costing.bpc_divisor(p):
            # bpc_divisor: the stashed manual runs while a (stale)
            # invention flag holds the materialized value in runs_per_bpc.
            bpc_per_unit[tid] = p["bpc_cost_isk"] / costing.bpc_divisor(p)
    memo: dict[int, tuple[float, int]] = {}

    def buy_cost(type_id: int) -> float | None:
        return _landed_price(ref, settings, snapshot, type_id)

    def chain(type_id: int, visiting: frozenset = frozenset()):
        if type_id in memo:
            return memo[type_id]
        blueprint = ref.blueprint_for_product(type_id)
        buildable = (
            blueprint is not None
            and type_id not in visiting
            and (
                type_id in finals
                or not (blacklist and blacklist(type_id))
            )
        )
        if not buildable:
            bought = buy_cost(type_id)
            return (bought or 0.0, 0 if bought is not None else 1)
        item_class = industry.classify_item(
            ref, type_id, blueprint.activity_id
        )
        setting = class_settings.get(item_class, industry.NPC_STATION)
        me, _te = me_te(blueprint.blueprint_id, blueprint.activity_id)
        mat_mult = industry.build_multiplier(
            ref,
            setting,
            blueprint.activity_id,
            "material",
            group_id=ref.type_info(type_id).group_id,
        )
        cost_mult = industry.build_multiplier(
            ref, setting, blueprint.activity_id, "cost"
        )
        total = 0.0
        unpriced = 0
        eiv = 0.0
        for material_id, base_qty in ref.materials(
            blueprint.blueprint_id, blueprint.activity_id
        ):
            eiv += base_qty * (snapshot.adjusted(material_id) or 0.0)
            per_unit = industry.unit_quantity(
                base_qty, me, mat_mult, blueprint.portion_size
            )
            built_cost, built_unpriced = chain(
                material_id, visiting | {type_id}
            )
            bought = buy_cost(material_id)
            sub = ref.blueprint_for_product(material_id)
            if sub is not None and material_id not in visiting:
                # Buildable input: the rational chain takes the cheaper leg.
                if bought is not None and bought <= built_cost:
                    unit_cost, leg_unpriced = bought, 0
                else:
                    unit_cost, leg_unpriced = built_cost, built_unpriced
            else:
                unit_cost, leg_unpriced = built_cost, built_unpriced
            total += per_unit * unit_cost
            unpriced += leg_unpriced
        total += (
            industry.job_install_cost(
                eiv, setting, cost_mult,
                scc_surcharge=settings.industry_scc_surcharge,
            )
            / blueprint.portion_size
        )
        total += bpc_per_unit.get(type_id, 0.0)
        memo[type_id] = (total, unpriced)
        return memo[type_id]

    return chain, buy_cost


def _build_savings_per_unit(ref, item: PlanItem, chain, buy_cost, snapshot):
    """LANDED buy price − the vertically-integrated chain cost per unit
    (gross of sell-side fees: for build-vs-buy you are avoiding a
    purchase, not making a sale). Landed = the chosen venue's raw price +
    that venue's courier rate × packaged m³ (2026-08-23: the item's own
    inbound freight was missing — the inputs were landed, the item was
    not, which biased every bulky intermediate toward "buy"). Also stamps
    the item's chain cost and unpriced-raw-leaf count."""
    landed = buy_cost(item.type_id)
    if landed is None:
        return None
    cost, unpriced = chain(item.type_id)
    item.unit_chain_cost = cost
    item.savings_unpriced_inputs = unpriced
    return landed - cost


def _allocate_slots(conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot):
    chain, buy_cost = _chain_coster(conn, ref, snapshot)

    for activity_id in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION):
        contenders = [
            i
            for i in merged.values()
            if i.recommended_action == "build" and i.activity_id == activity_id
        ]
        if not contenders:
            continue
        slots = snapshot.slots_available.get(activity_id, 0)

        for item in contenders:
            item.build_savings_per_unit = _build_savings_per_unit(
                ref, item, chain, buy_cost, snapshot
            )

        # Building above the LANDED market price wastes ISK regardless of
        # slot pressure (decision 2026-08-20; landed since 2026-08-23):
        # INTERMEDIATES whose savings (landed buy price − integrated chain
        # cost) are zero or negative never get slots — contended or not —
        # and Phase 7 flips their deficit to purchases instead. Unpriced
        # items (savings None) stay: they have no purchase fallback.
        # Pipeline FINALS are exempt (2026-08-21): they are built to SELL,
        # so "buy your own product" is never actionable advice; their
        # negative paper margin is surfaced as a badge, not a buy order.
        finals = {
            p["final_product_type_id"] for p in store.active_pipelines(conn)
        }
        contenders = [
            i
            for i in contenders
            if i.type_id in finals
            or i.build_savings_per_unit is None
            or i.build_savings_per_unit > 0
        ]
        if not contenders:
            continue
        demand = sum(i.jobs_needed_unconstrained for i in contenders)

        if demand <= slots:
            for item in contenders:
                item.jobs_allocated = item.jobs_needed_unconstrained
            continue

        # Pipeline FINALS take their slots FIRST (decision 2026-08-21:
        # finals never flip to buy — they are the point of the pipeline,
        # so a savings-maximizing MILP must not starve them into a
        # purchase). Unpriced contenders go next: they have no purchase
        # fallback either (Phase 7 cannot flip them to buy). Both in
        # merged-plan order, deterministic.
        free = slots
        rest = []
        for item in contenders:
            if item.type_id in finals:
                take = min(item.jobs_needed_unconstrained, free)
                item.jobs_allocated = take
                free -= take
            else:
                rest.append(item)
        priced = []
        for item in rest:
            if item.build_savings_per_unit is None:
                take = min(item.jobs_needed_unconstrained, free)
                item.jobs_allocated = take
                free -= take
            else:
                priced.append(item)
        if free <= 0 or not priced:
            continue

        # Contention: maximize total build savings via MILP. A
        # non-saturating item's LAST job only realizes the residual runs
        # Phase 7 will grant it, so that job is a separate variable at the
        # residual weight — weighting it at a full window inverted
        # allocations toward nearly-empty jobs. Saturating reactions really
        # do run the full window every job, so they keep one full-weight
        # variable.
        cols: list[tuple[PlanItem, float, int]] = []
        for i in priced:
            per_unit = i.build_savings_per_unit
            full_weight = per_unit * i.portion_size * i.max_runs_per_job
            saturating = (
                activity_id == config.ACTIVITY_REACTION
                and ref.type_info(i.type_id).group_id
                not in config.NON_SATURATING_REACTION_GROUPS
            )
            if saturating:
                cols.append((i, full_weight, i.jobs_needed_unconstrained))
                continue
            last_runs = (
                i.total_runs_needed
                - (i.jobs_needed_unconstrained - 1) * i.max_runs_per_job
            )
            if i.jobs_needed_unconstrained > 1:
                cols.append((i, full_weight, i.jobs_needed_unconstrained - 1))
            cols.append((i, per_unit * i.portion_size * last_runs, 1))
        n = len(cols)
        weights = np.array([w for _item, w, _u in cols])
        upper = np.array([u for _item, _w, u in cols], dtype=float)
        result = milp(
            c=-weights,
            constraints=LinearConstraint(np.ones((1, n)), 0, free),
            integrality=np.ones(n),
            bounds=Bounds(np.zeros(n), upper),
            # The model is a tiny knapsack HiGHS solves in milliseconds; the
            # limit only exists so a pathological solve can never hang the
            # request — a limit hit returns success=False and fails loudly
            # below instead.
            options={"time_limit": 60},
        )
        if not result.success:
            # A silent zero-allocation here would flip the whole pool to
            # market buys — fail loudly instead (the model is always
            # feasible at x=0, so this only fires on solver breakdown).
            raise RuntimeError(
                f"slot allocation MILP failed: {result.message}"
            )
        allocation = np.round(result.x).astype(int)
        for (item, _w, _u), jobs in zip(cols, allocation):
            item.jobs_allocated += int(jobs)


# ---------------------------------------------------------------------------
# Phase 6.5: alchemy substitution into spare reaction slots (v1.4)
# ---------------------------------------------------------------------------


def _alchemy_pass(conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot):
    """Substitute alchemy for direct composite reactions in spare slots.

    Direct planning is untouched; alchemy only ever converts existing
    coverage. When reaction slots are left over and a composite's alchemy
    route (unrefined reaction + reprocess at the asserted yield, recovered
    inputs credited at their landed buy price) is cheaper per unit than the
    direct reaction, direct jobs are displaced one at a time: each swap drops one
    direct job and adds however many alchemy jobs cover the RESIDUAL
    deficit that job was needed for — the last direct job of an item is
    mostly overshoot, so the first swap is cheap; wholesale replacement
    (~10 alchemy slots per direct slot at 55% yield) only happens when the
    spare capacity and the per-type cap genuinely allow it. Total coverage
    never drops below the deficit, and a contended pool (no spare slots)
    disables alchemy entirely — direct reactions are far more
    slot-efficient."""
    settings = store.get_settings(conn)
    if (
        not settings.alchemy_enabled
        or settings.alchemy_reprocess_yield <= 0
        or settings.max_alchemy_jobs_per_type <= 0
    ):
        return
    slots = snapshot.slots_available.get(config.ACTIVITY_REACTION, 0)
    spare = slots - sum(
        i.jobs_allocated
        for i in merged.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    if spare <= 0:
        return

    yield_ = settings.alchemy_reprocess_yield
    cap = settings.max_alchemy_jobs_per_type
    class_settings = store.get_class_settings(conn)
    setting = class_settings.get("reactions", industry.NPC_STATION)
    skills = _skill_levels(settings)
    window_seconds = settings.max_run_duration_hours * 3600.0

    def landed(type_id: int) -> float | None:
        # Both routes' materials and the recovered credit price LANDED
        # (2026-08-24) — the same leg every other buy decision uses.
        # Freight does not cancel between the routes: at 55% yield the
        # unrefined route hauls ~1.8× the volume per composite unit, and
        # recovered inputs offset next-cycle purchases that would have
        # carried freight too.
        return _landed_price(ref, settings, snapshot, type_id)

    candidates = []
    for composite_id, route in ref.alchemy_routes().items():
        item = merged.get(composite_id)
        if (
            item is None
            or item.activity_id != config.ACTIVITY_REACTION
            or item.jobs_allocated <= 0
            or ref.type_info(composite_id).group_id
            in config.NON_SATURATING_REACTION_GROUPS
        ):
            continue
        # Reactions have no ME; the reactions class setting governs both
        # routes, so structure/rig bonuses cancel where equal.
        time_per_run = industry.job_time_seconds(
            route.formula.base_time,
            1,
            0,
            industry.build_multiplier(
                ref, setting, config.ACTIVITY_REACTION, "time"
            ),
        ) * industry.skill_time_multiplier(
            ref, route.formula.blueprint_id, config.ACTIVITY_REACTION, skills
        )
        max_runs = min(
            max(1, math.floor(window_seconds / time_per_run)),
            _game_job_run_cap(time_per_run, route.formula),
        )
        # Each route is costed at its own job scale so once-per-job
        # rounding amortizes as it will in the installed jobs.
        direct_unit = _unit_build_cost(
            ref,
            item.blueprint_id,
            item.activity_id,
            item.portion_size,
            setting,
            0,
            snapshot,
            runs=item.max_runs_per_job,
            scc_surcharge=settings.industry_scc_surcharge,
            price_of=landed,
        )
        unrefined_cost = _unit_build_cost(
            ref,
            route.formula.blueprint_id,
            route.formula.activity_id,
            route.formula.portion_size,
            setting,
            0,
            snapshot,
            runs=max_runs,
            scc_surcharge=settings.industry_scc_surcharge,
            price_of=landed,
        )
        recovered_credit = sum(
            qty * yield_ * (landed(m) or 0.0)
            for m, qty in route.recovered
        )
        alchemy_unit = (unrefined_cost - recovered_credit) / (
            route.composite_qty * yield_
        )
        item.direct_unit_cost = direct_unit
        item.alchemy_unit_cost = alchemy_unit
        if alchemy_unit >= direct_unit:
            continue
        out_per_job = math.floor(max_runs * route.composite_qty * yield_)
        if out_per_job <= 0:
            continue
        candidates.append(
            {
                "item": item,
                "route": route,
                "time_per_run": time_per_run,
                "max_runs": max_runs,
                "out_per_job": out_per_job,
                "savings_per_unit": direct_unit - alchemy_unit,
            }
        )

    alchemy_items: dict[int, PlanItem] = {}
    while True:
        best = None
        for cand in candidates:
            item = cand["item"]
            if item.jobs_allocated <= 0:
                continue
            existing = alchemy_items.get(cand["route"].unrefined_id)
            jobs_so_far = existing.jobs_allocated if existing else 0
            out_d = item.max_runs_per_job * item.portion_size
            needed = item.total_runs_needed * item.portion_size
            covered_without = (
                item.jobs_allocated - 1
            ) * out_d + item.alchemy_output_qty
            residual = max(0, needed - covered_without)
            jobs = max(1, math.ceil(residual / cand["out_per_job"]))
            if jobs_so_far + jobs > cap or jobs - 1 > spare:
                continue
            # Rank by ISK saved on the needed units per spare slot consumed.
            score = (
                cand["savings_per_unit"] * max(residual, 1) / max(jobs - 1, 1)
            )
            if best is None or score > best["score"]:
                best = {"cand": cand, "jobs": jobs, "score": score}
        if best is None:
            break
        cand = best["cand"]
        item, route = cand["item"], cand["route"]
        item.jobs_allocated -= 1
        item.alchemy_output_qty += best["jobs"] * cand["out_per_job"]
        spare -= best["jobs"] - 1
        alch = alchemy_items.get(route.unrefined_id)
        if alch is None:
            alch = alchemy_items[route.unrefined_id] = PlanItem(
                type_id=route.unrefined_id,
                name=ref.type_info(route.unrefined_id).name,
                item_class="reactions",
                depth=item.depth,
                blueprint_id=route.formula.blueprint_id,
                activity_id=config.ACTIVITY_REACTION,
                portion_size=route.formula.portion_size,
                alchemy_for_type_id=route.composite_id,
                time_per_run=cand["time_per_run"],
                max_runs_per_job=cand["max_runs"],
                on_hand_qty=snapshot.on_hand.get(route.unrefined_id, 0),
                in_progress_qty=snapshot.in_progress.get(route.unrefined_id, 0),
                recommended_action="build",
            )
            _stamp_price(alch, snapshot)
        alch.jobs_allocated += best["jobs"]
        alch.total_runs_needed = alch.jobs_allocated * alch.max_runs_per_job
        alch.jobs_needed_unconstrained = alch.jobs_allocated

    # Alchemy inputs the chain doesn't otherwise demand (e.g. Cadmium when
    # only Dysprosium chains are active) must exist as plan items so the
    # just-in-time purchase pass in Phase 7 can buy them.
    for alch in alchemy_items.values():
        merged[alch.type_id] = alch
        for material_id, _qty in ref.materials(
            alch.blueprint_id, alch.activity_id
        ):
            if material_id in merged:
                continue
            merged[material_id] = PlanItem(
                type_id=material_id,
                name=ref.type_info(material_id).name,
                item_class=industry.classify_item(ref, material_id, None),
                depth=alch.depth + 1,
                on_hand_qty=snapshot.on_hand.get(material_id, 0),
                in_progress_qty=snapshot.in_progress.get(material_id, 0),
            )
            _stamp_price(merged[material_id], snapshot)


# ---------------------------------------------------------------------------
# Phase 7: finalize recommendations and flags
# ---------------------------------------------------------------------------


def _planned_consumption(conn, ref, merged: dict[int, PlanItem]) -> dict[int, int]:
    """What the plan's ALLOCATED jobs will actually consume, per material.
    Valid only after runs_allocated is resolved (Phase 7). The game rounds
    materials once per JOB. Intermediates install jobs_allocated uniform
    jobs; finals and exact-quantity ships skip Phase 7's round-up ("their
    last job runs short"), so their runs are split divmod-style — extra
    jobs run one extra run — and every run is charged (the old floor
    division silently dropped runs_allocated % jobs whole runs of demand,
    audit 2026-08-27)."""
    class_settings = store.get_class_settings(conn)
    me_te = store.me_te_resolver(conn)
    consumption: dict[int, int] = {}
    for item in merged.values():
        if item.runs_allocated <= 0:
            continue
        setting = class_settings.get(item.item_class, industry.NPC_STATION)
        me, _te = me_te(item.blueprint_id, item.activity_id)
        mat_mult = industry.build_multiplier(
            ref,
            setting,
            item.activity_id,
            "material",
            group_id=ref.type_info(item.type_id).group_id,
        )
        if item.jobs_allocated > 0:
            jobs = item.jobs_allocated
            base_runs, extra = divmod(item.runs_allocated, jobs)
        else:
            jobs, base_runs, extra = 1, item.runs_allocated, 0
        for material_id, base_qty in ref.materials(
            item.blueprint_id, item.activity_id
        ):
            qty = (jobs - extra) * industry.required_quantity(
                base_runs, base_qty, me, mat_mult
            )
            if extra:
                qty += extra * industry.required_quantity(
                    base_runs + 1, base_qty, me, mat_mult
                )
            consumption[material_id] = (
                consumption.get(material_id, 0) + qty
            )
    return consumption


def _finalize(conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot):
    me_te = store.me_te_resolver(conn)
    class_settings = store.get_class_settings(conn)
    final_products = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }

    for item in merged.values():
        if item.recommended_action != "build":
            continue
        saturating_reaction = (
            item.activity_id == config.ACTIVITY_REACTION
            and ref.type_info(item.type_id).group_id
            not in config.NON_SATURATING_REACTION_GROUPS
        )
        if saturating_reaction:
            # A slot allocated to a reaction always runs the full cycle
            # window, even if that overshoots the deficit. Hybrid polymers
            # and molecular-forged materials are exempt — they size to the
            # deficit like manufactured items.
            item.runs_allocated = item.jobs_allocated * item.max_runs_per_job
        else:
            # Every job of an INTERMEDIATE runs the SAME count: round the
            # per-job runs up so jobs are uniform (slight overbuild nets
            # off next cycle; ceil(runs/jobs) never exceeds
            # max_runs_per_job). Finals and exact-quantity ships never
            # overbuild — their extra hulls would never net off (finals
            # ignore stock by design), and the round-up broke the
            # batch/BPC-rounded total — so their last job runs short
            # (decision 2026-08-20).
            item.runs_allocated = min(
                item.total_runs_needed,
                item.jobs_allocated * item.max_runs_per_job,
            )
            exact_total = item.type_id in final_products or (
                ref.type_info(item.type_id).category_id == config.CATEGORY_SHIP
                and ref.type_info(item.type_id).group_id
                in config.EXACT_QTY_SHIP_GROUPS
            )
            if item.jobs_allocated > 0 and not exact_total:
                per_job = -(-item.runs_allocated // item.jobs_allocated)
                item.runs_allocated = per_job * item.jobs_allocated
        item.recommended_build_qty = item.runs_allocated * item.portion_size

        # Composite output expected from this cycle's alchemy jobs counts
        # toward coverage — a direct job displaced by the alchemy pass is
        # not a capacity shortfall.
        shortfall_qty = (
            (item.total_runs_needed - item.runs_allocated) * item.portion_size
            - item.alchemy_output_qty
        )
        if shortfall_qty > 0:
            # An intermediate denied slots because the market undercuts its
            # build cost is a deliberate buy, not a capacity shortage.
            # Finals are never market-preferred (they are built to sell);
            # any shortfall of theirs is a real capacity loss.
            market_preferred = (
                item.build_savings_per_unit is not None
                and item.build_savings_per_unit <= 0
                and item.type_id not in final_products
            )
            item.capacity_limited = not market_preferred
            # Flip the loser's shortfall to purchase — but only where a
            # market actually exists, and NEVER for a pipeline final
            # (decision 2026-08-21: buying your own product is not an
            # action; a starved final stays flagged unmet).
            if (
                snapshot.price(item.type_id) is not None
                and item.type_id not in final_products
            ):
                item.recommended_buy_qty = shortfall_qty
                item.recommended_action = (
                    "both" if item.recommended_build_qty > 0 else "buy"
                )

    # What this cycle's ALLOCATED jobs will actually consume, per material.
    consumption = _planned_consumption(conn, ref, merged)

    # Raw inputs buy just-in-time: what the allocated jobs consume, plus the
    # purchase margin, net of stock. No jobs consuming it -> nothing bought.
    settings = store.get_settings(conn)
    margin_mult = 1.0 + settings.input_purchase_margin
    for item in merged.values():
        if item.buildable:
            continue
        item.target_stock_qty = math.ceil(
            consumption.get(item.type_id, 0) * margin_mult
        )
        item.deficit_qty = max(
            0,
            item.target_stock_qty - item.on_hand_qty - item.in_progress_qty,
        )
        item.recommended_buy_qty = item.deficit_qty
        item.recommended_action = "buy" if item.deficit_qty > 0 else None

    # Low stock: project stock past this run and test against the next run's
    # expected minimum. Buildables only — raw inputs are just-in-time by
    # design now, so a near-empty raw stockpile is intended, not a warning.
    # Suppressed until a pipeline has at least one EXECUTED run behind it
    # (every stage legitimately reads low while priming). v1.5 made run
    # execution the truth signal; the finished_batch table this used to
    # read is dormant Phase-8 machinery nothing writes any more.
    primed = {
        row["pipeline_id"]
        for row in conn.execute(
            "SELECT DISTINCT a.pipeline_id FROM index_run_item_pipeline a "
            "JOIN index_run_item i USING (index_run_item_id) "
            "JOIN index_run r USING (index_run_id) "
            "WHERE r.status = 'complete'"
        )
    }
    for item in merged.values():
        if not item.buildable:
            continue
        if not any(p in primed for p in item.pipeline_share):
            continue
        projected = (
            item.on_hand_qty
            + item.in_progress_qty
            + item.recommended_build_qty
            + item.recommended_buy_qty
            + item.alchemy_output_qty
            - consumption.get(item.type_id, 0)
        )
        if projected < item.merged_min_qty:
            item.low_stock = True

    # v1.5: snapshot the per-unit install fee for every buildable, planned
    # jobs or not (a stock-covered item still needs a fee on record for
    # runs that cost against this one).
    for item in merged.values():
        if not item.buildable:
            continue
        setting = class_settings.get(item.item_class, industry.NPC_STATION)
        eiv = sum(
            base_qty * (snapshot.adjusted(material_id) or 0.0)
            for material_id, base_qty in ref.materials(
                item.blueprint_id, item.activity_id
            )
        )
        cost_mult = industry.build_multiplier(
            ref, setting, item.activity_id, "cost"
        )
        item.unit_install_fee = (
            industry.job_install_cost(
                eiv, setting, cost_mult,
                scc_surcharge=settings.industry_scc_surcharge,
            )
            / item.portion_size
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _invention_pass(
    conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot
) -> dict[int, dict]:
    """v1.22, run ONCE after the convergence loop: persist each
    invention-enabled pipeline's invention VINTAGE — the skill-applied
    probability, invented stats, per-attempt input prices and fees, and
    cost_per_run — for lag costing and the run's profit view.

    Since v1.23 this pass adds NOTHING to the plan items: sizing,
    purchasing and copy jobs live on the live Invention tab
    (invention_stockpile), which targets the BPC stockpile from CURRENT
    stock rather than any index run. Every resolving pipeline gets a row
    — a final the slot pool starved to zero runs included (review
    2026-09-01: without it the executed run's profit view fell back to
    the manual bpc line, the very figure invention ignores)."""
    configs = _invention_configs(conn, ref)
    if not configs:
        return {}
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    rows: dict[int, dict] = {}
    for pipeline_id, (pipeline, source, decryptor) in configs.items():
        if pipeline["final_product_type_id"] not in merged:
            continue
        cost = costing.invention_cost(
            ref, settings, class_settings, source, decryptor,
            price_of=lambda t: _landed_price(ref, settings, snapshot, t),
            adjusted_of=snapshot.adjusted,
        )
        rows[pipeline_id] = {
            "pipeline_id": pipeline_id,
            "t1_blueprint_id": source.t1_blueprint_id,
            "decryptor_type_id": decryptor.type_id if decryptor else None,
            "probability": cost.probability,
            "invented_me": cost.me,
            "invented_te": cost.te,
            "runs_per_copy": cost.runs_per_copy,
            "datacores": json.dumps(
                [[t, q, p] for t, q, p in cost.datacores]
            ),
            "decryptor_unit_price": cost.decryptor_price,
            "invention_fee_per_attempt": cost.invention_fee,
            "copy_fee_per_attempt": cost.copy_fee,
            "cost_per_run": cost.cost_per_run,
        }
    return rows


def invention_stockpile(conn, ref, snapshot: Snapshot) -> dict:
    """The live Invention tab (v1.23): per invention-enabled pipeline, a
    TARGET-based BPC stockpile netted against current stock and in-flight
    lab jobs — never persisted, recomputed on every GET.

    T2: target = ceil(cycle copies × t2_bpc_overbuild), where cycle
    copies = ceil(ceil(output_qty_per_run / portion) / runs_per_copy)
    (invariant to whole-copy batch rounding; a dual-role final consumed
    by another pipeline can be allocated more in a real run — the tab
    deliberately sizes from configured output only). Stock draws from a
    SHARED per-blueprint pool of on_hand[invented blueprint type] (each
    unit ≈ one copy at the CONFIGURED runs_per_copy), then from the
    in-flight pool of activity-8 attempts converted at the CONFIGURED
    chance (floor(attempts × P) — decryptor drift between install and
    now shifts the estimate). attempts = ceil(remainder / P).

    T1 (non-relic sources): consumed = attempts; target = ceil(attempts
    × t1_bpc_overbuild); stock = stack-minus-one (the BPO assumed to sit
    with its copy stack — and unseen entirely if stored outside the
    tracked systems); in-flight = activity-5 copy runs. Pipelines
    sharing a source (Arazu AND Lachesis from Celestis) draw from ONE
    pool in pipeline_id order — never double-credited.

    Datacores/decryptors/relics: gross = attempts-scaled, netted ONCE
    across all pipelines against on_hand + in_progress (the v1.22 rule);
    buy rows carry venue RAW prices like every buy list."""
    configs = _invention_configs(conn, ref)
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    t2_pool: dict[int, int] = {}
    t2_flight_pool: dict[int, int] = {}
    t1_pool: dict[int, int] = {}
    t1_flight_pool: dict[int, int] = {}
    t1_owners: dict[int, list[str]] = {}
    sections: list[dict] = []
    demand: dict[int, int] = {}
    for pipeline_id, (pipeline, source, decryptor) in sorted(
        configs.items()
    ):
        cost = costing.invention_cost(
            ref, settings, class_settings, source, decryptor,
            price_of=lambda t: _landed_price(ref, settings, snapshot, t),
            adjusted_of=snapshot.adjusted,
        )
        final_id = pipeline["final_product_type_id"]
        final_bp = ref.blueprint_for_product(final_id)
        portion = final_bp.portion_size if final_bp else 1
        cycle_runs = math.ceil(pipeline["output_qty_per_run"] / portion)
        cycle_copies = math.ceil(cycle_runs / cost.runs_per_copy)
        target = _ceil(cycle_copies * settings.t2_bpc_overbuild)

        bp_id = source.product_blueprint_id
        t2_pool.setdefault(bp_id, snapshot.on_hand.get(bp_id, 0))
        t2_flight_pool.setdefault(bp_id, snapshot.in_progress.get(bp_id, 0))
        from_stock = min(target, t2_pool[bp_id])
        t2_pool[bp_id] -= from_stock
        flight_attempts = t2_flight_pool[bp_id]
        flight_copies = _floor(flight_attempts * cost.probability)
        from_flight = min(target - from_stock, flight_copies)
        if from_flight:
            # Decrement in ATTEMPTS so a second pipeline sharing the
            # blueprint cannot re-credit the same in-flight jobs.
            used = min(_ceil(from_flight / cost.probability), flight_attempts)
            t2_flight_pool[bp_id] -= used
        to_invent = max(0, target - from_stock - from_flight)
        attempts = _ceil(to_invent / cost.probability) if to_invent else 0
        if attempts:
            for type_id, qty, _price in cost.datacores:
                demand[type_id] = demand.get(type_id, 0) + qty * attempts
            if decryptor is not None:
                demand[decryptor.type_id] = (
                    demand.get(decryptor.type_id, 0) + attempts
                )

        t1 = None
        if not ref.is_relic_source(source.t1_blueprint_id):
            # Everything here is in licensed RUNS: an attempt consumes one
            # run of a T1 copy, copies are made at the blueprint's max
            # runs (user decision 2026-09-01), stocked copies are assumed
            # to hold max runs each, and in-flight copy jobs credit
            # copies × licensed runs.
            t1_id = source.t1_blueprint_id
            max_runs = ref.max_runs(t1_id)
            t1_owners.setdefault(t1_id, []).append(
                (pipeline_id, pipeline["name"])
            )
            # Stack minus one: the BPO assumed among its copies.
            t1_pool.setdefault(
                t1_id,
                max(0, snapshot.on_hand.get(t1_id, 0) - 1) * max_runs,
            )
            t1_flight_pool.setdefault(
                t1_id, snapshot.in_progress.get(t1_id, 0)
            )
            t1_target = _ceil(attempts * settings.t1_bpc_overbuild)
            t1_from_stock = min(t1_target, t1_pool[t1_id])
            t1_pool[t1_id] -= t1_from_stock
            t1_from_flight = min(
                t1_target - t1_from_stock, t1_flight_pool[t1_id]
            )
            t1_flight_pool[t1_id] -= t1_from_flight
            runs_to_make = t1_target - t1_from_stock - t1_from_flight
            t1 = {
                "blueprint_id": t1_id,
                "max_runs": max_runs,
                "target": t1_target,
                "from_stock": t1_from_stock,
                "from_flight": t1_from_flight,
                "runs_to_make": runs_to_make,
                # copy JOBS to install, each at max runs
                "to_make": math.ceil(runs_to_make / max_runs),
            }

        # Invention jobs group like copy jobs (user decision 2026-09-01):
        # a job on a T1 copy runs up to the copy's max runs, one attempt
        # per run. Relics have no T1 blueprint, so their job grouping is
        # not modeled — attempts stand alone.
        jobs = (
            math.ceil(attempts / t1["max_runs"]) if t1 and attempts else
            (0 if t1 else None)
        )
        sections.append(
            {
                "pipeline_id": pipeline_id,
                "product_name": ref.type_info(final_id).name,
                "t1_name": ref.type_info(source.t1_blueprint_id).name,
                "is_relic": ref.is_relic_source(source.t1_blueprint_id),
                "decryptor_name": decryptor.name if decryptor else None,
                "probability": cost.probability,
                "runs_per_copy": cost.runs_per_copy,
                "invented_me": cost.me,
                "invented_te": cost.te,
                "cycle_copies": cycle_copies,
                "target": target,
                "from_stock": from_stock,
                "from_flight": from_flight,
                "flight_attempts_seen": flight_attempts,
                "to_invent": to_invent,
                "attempts": attempts,
                "max_runs": t1["max_runs"] if t1 else None,
                "jobs": jobs,
                "covered": to_invent == 0,
                "t1": t1,
                "unpriced": cost.unpriced,
                "attempt_cost": cost.attempt_cost,
                "copy_fee": cost.copy_fee,
                "cost_per_run": cost.cost_per_run,
                "outlay": cost.attempt_cost * attempts,
            }
        )
    for section in sections:
        t1 = section["t1"]
        if t1 and len(t1_owners.get(t1["blueprint_id"], [])) > 1:
            # Keyed by pipeline_id, not name (a renamed SDE type would
            # list the section itself).
            t1["shared"] = [
                name
                for owner_id, name in t1_owners[t1["blueprint_id"]]
                if owner_id != section["pipeline_id"]
            ]
        elif t1:
            t1["shared"] = []

    # One netting pass across ALL pipelines' input demand, so shared
    # datacore/relic stock is never credited twice (the v1.22 rule). Like
    # every other bought input (review 2026-09-01): the Raw Material
    # Buffer inflates the need before netting, and a structure-venue buy
    # whose cheap ladder is shorter than the buy is flagged shallow.
    margin = settings.input_purchase_margin
    buys: list[dict] = []
    for type_id, raw_need in sorted(demand.items()):
        gross = _ceil(raw_need * (1.0 + margin))
        on_hand = snapshot.on_hand.get(type_id, 0)
        in_flight = snapshot.in_progress.get(type_id, 0)
        to_buy = max(0, gross - on_hand - in_flight)
        venue = snapshot.venue(type_id)
        units_cheaper = (
            snapshot.structure_units_cheaper.get(type_id)
            if venue == store.BUY_VENUE_STRUCTURE
            else None
        )
        buys.append(
            {
                "type_id": type_id,
                "name": ref.type_info(type_id).name,
                "gross": gross,
                "margin": margin,
                "on_hand": on_hand,
                "in_progress": in_flight,
                "to_buy": to_buy,
                "price": snapshot.price(type_id),
                "venue": venue,
                "region_wide": type_id in snapshot.region_wide,
                "structure_units_cheaper": units_cheaper,
                "shallow": units_cheaper is not None and to_buy > units_cheaper,
            }
        )
    multibuy: dict[str, list[str]] = {}
    for buy in buys:
        if buy["to_buy"] <= 0:
            continue
        multibuy.setdefault(buy["venue"], []).append(
            f"{buy['name']} {buy['to_buy']}"
        )
    return {
        "sections": sections,
        "buys": buys,
        "buy_total": sum(
            b["to_buy"] * (b["price"] or 0.0) for b in buys
        ),
        "buys_unpriced": sum(
            1 for b in buys if b["to_buy"] > 0 and b["price"] is None
        ),
        "buys_shallow": sum(1 for b in buys if b["shallow"]),
        "multibuy_hub": "\n".join(
            multibuy.get(store.BUY_VENUE_HUB, [])
        ),
        "multibuy_structure": "\n".join(
            multibuy.get(store.BUY_VENUE_STRUCTURE, [])
        ),
        "copy_jobs": [
            s
            for s in sections
            if s["t1"] and s["t1"]["to_make"] > 0
        ],
        "total_outlay": sum(s["outlay"] for s in sections),
    }


def _invention_price_ids(conn, ref) -> tuple[set[int], set[int]]:
    """(market ids, adjusted-only ids) that every CAPABLE pipeline's
    invention choices can need — chosen or not, active or not, so
    switching or enabling any choice prices immediately off the ordinary
    cache (v1.22; multi-source since 2026-08-31). Market: ALL sources'
    datacores, every relic source type itself, all decryptors.
    Adjusted-only: each source's fee EIV base (the T1 blueprint's
    manufacturing materials, or for a relic the INVENTED blueprint's —
    spelled out because a capable INACTIVE pipeline's finals never
    expand), which invention_cost prices through CCP adjusted prices
    alone: the per-type order-book pull never needs them (review
    2026-09-01 — an inactive capable pipeline's EIV materials cost ~7
    wasted order-book requests per refresh)."""
    market: set[int] = set()
    adjusted: set[int] = set()
    for pipeline in conn.execute(
        "SELECT final_product_type_id FROM pipeline"
    ):
        sources = ref.invention_sources_for_product(
            pipeline["final_product_type_id"]
        )
        if not sources:
            continue
        market.update(d.type_id for d in ref.decryptors())
        for source in sources:
            market.update(
                m
                for m, _qty in ref.materials(
                    source.t1_blueprint_id, config.ACTIVITY_INVENTION
                )
            )
            if ref.is_relic_source(source.t1_blueprint_id):
                market.add(source.t1_blueprint_id)
                eiv_blueprint = source.product_blueprint_id
            else:
                eiv_blueprint = source.t1_blueprint_id
            adjusted.update(
                m
                for m, _qty in ref.materials(
                    eiv_blueprint, config.ACTIVITY_MANUFACTURING
                )
            )
    return market, adjusted


def _add_alchemy_ids(conn, ref, ids: set[int]) -> None:
    """With alchemy enabled, routes feeding a demanded composite add their
    formula inputs, unrefined product, and recovered outputs."""
    if store.get_settings(conn).alchemy_enabled:
        for route in ref.alchemy_routes().values():
            if route.composite_id not in ids:
                continue
            ids.add(route.unrefined_id)
            ids.update(m for m, _qty in route.recovered)
            ids.update(
                m
                for m, _qty in ref.materials(
                    route.formula.blueprint_id, route.formula.activity_id
                )
            )


def market_type_ids(conn, ref) -> set[int]:
    """What the per-type ORDER-BOOK refresh must pull: the active
    pipelines' demand (alchemy included) plus the invention market
    inputs — never the adjusted-only EIV bases."""
    ids = set(_expand_and_merge(conn, ref))
    ids |= _invention_price_ids(conn, ref)[0]
    _add_alchemy_ids(conn, ref, ids)
    return ids


def demand_type_ids(conn, ref) -> set[int]:
    """Every type planning may price — market_type_ids plus the
    adjusted-only invention EIV bases: the cache-READ list for /run,
    Planning and the price refresh's adjusted-price store."""
    market, adjusted = _invention_price_ids(conn, ref)
    ids = set(_expand_and_merge(conn, ref)) | market
    _add_alchemy_ids(conn, ref, ids)
    return ids | adjusted


def invention_type_ids(conn, ref) -> set[int]:
    """The Invention tab's price set — the invention inputs and their
    EIV bases, a few dozen ids rather than the whole demand set (review
    2026-09-01)."""
    market, adjusted = _invention_price_ids(conn, ref)
    return market | adjusted


def _multi_cycle_overhang(job_ends: list, horizon: datetime) -> int:
    """Count active jobs whose end date lies beyond the next index run —
    they occupy a real production line across the cycle boundary."""
    overhang = 0
    for end in job_ends:
        try:
            ends_at = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        if ends_at > horizon:
            overhang += 1
    return overhang


def snapshot_from_state(
    conn, prices=None, adjusted=None, region_wide=None,
    buy_venue=None, structure_units_cheaper=None,
) -> Snapshot | None:
    """Build a Snapshot from the last persisted ESI pull plus the manual
    slot settings. Returns None if ESI has never been refreshed.
    buy_venue / structure_units_cheaper (v1.10): per-type venue provenance
    of `prices` (see Snapshot), typically from market.buy_quotes.

    Slot pools are the user-entered totals net of MULTI-CYCLE jobs (still
    running past the next index run, e.g. a weeks-long capital hull —
    decision 2026-08-20). Single-cycle jobs do NOT reduce capacity: an
    index run is planned for a moment when the previous cycle's jobs have
    all delivered, and their output already counts as in-progress stock.
    """
    state = store.latest_esi_snapshot(conn)
    if state is None:
        return None
    settings = store.get_settings(conn)
    horizon = datetime.now(timezone.utc) + timedelta(
        hours=settings.max_run_duration_hours
    )
    job_ends = state.get("job_ends", {})
    return Snapshot(
        on_hand=state["on_hand"],
        in_progress=state["in_progress"],
        slots_available={
            activity: max(
                0,
                total
                - _multi_cycle_overhang(job_ends.get(activity, []), horizon),
            )
            for activity, total in (
                (config.ACTIVITY_MANUFACTURING, settings.manufacturing_slots),
                (config.ACTIVITY_REACTION, settings.reaction_slots),
            )
        },
        # `is not None`, not truthiness: a price *model* may be an empty
        # mapping subclass whose .get() computes prices (test fixtures).
        prices=prices if prices is not None else {},
        adjusted_prices=adjusted if adjusted is not None else {},
        region_wide=set(region_wide or ()),
        buy_venue=dict(buy_venue or {}),
        structure_units_cheaper=dict(structure_units_cheaper or {}),
        character_isk=state["character_isk"],
        corporation_isk=state["corporation_isk"],
    )


def plan_index_run(
    conn,
    ref,
    snapshot: Snapshot,
    persist: bool = True,
    output_qty=None,
    alchemy: bool = True,
) -> Plan:
    """Run planning phases 2-7 (with the consumption feedback loop) and
    (optionally) persist the index run. output_qty (built-scale expansion
    override, see _expand_and_merge) and alchemy=False (skip the
    substitution pass regardless of the setting) are the steady-state
    path's seams; the real /run path never passes either."""
    merged = _expand_and_merge(conn, ref, output_qty)
    _apply_targets(conn, ref, merged, snapshot, alchemy)
    _size_jobs(conn, ref, merged)
    _allocate_slots(conn, ref, merged, snapshot)
    if alchemy:
        _alchemy_pass(conn, ref, merged, snapshot)
    _finalize(conn, ref, merged, snapshot)

    # Sizing feedback loop (decision 2026-08-21; iterated to convergence
    # 2026-08-28): Phase 4 estimated each stage's cycle draw as the
    # steady-state merged_min, but the allocation's ACTUAL draw differs —
    # catch-up consumers build more than one cycle's worth, saturating
    # reactions overshoot, and the game rounds materials per job. Re-size
    # supplier deficits against the allocation's real draw and re-run
    # Phases 5-7 until the deficits stop moving. A correction propagates
    # one BOM tier per pass (bom.expand layers depth strictly: every
    # consumer of a material sits shallower than it), so max buildable
    # depth passes reach the deepest supplier and one more verifies —
    # that bound is also the guard for the case with no fixed point,
    # where tiers trade the last contended slots back and forth forever:
    # the final allocation stands and low_stock / capacity_limited
    # (evaluated from it) tell the truth about any residue. Finals keep
    # their exact-requested rule; steady state converges after one
    # correction (the single pass this loop replaces).
    final_products = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }
    max_passes = 1 + max(
        (i.depth for i in merged.values() if i.buildable), default=0
    )
    for _ in range(max_passes):
        draft_draw = _planned_consumption(conn, ref, merged)
        revised = False
        for item in merged.values():
            if (
                not item.buildable
                or item.type_id in final_products
                or item.alchemy_for_type_id is not None
            ):
                continue
            corrected = max(
                0,
                item.target_stock_qty
                + draft_draw.get(item.type_id, 0)
                - item.on_hand_qty
                - item.in_progress_qty,
            )
            if corrected != item.deficit_qty:
                item.deficit_qty = corrected
                revised = True
        if not revised:
            break
        for type_id in [
            t
            for t, item in merged.items()
            if item.alchemy_for_type_id is not None
            # ... and the raw formula inputs the alchemy pass added for
            # its routes: they carry no BOM demand (every expanded item
            # accumulates merged_min > 0), and left behind they persist
            # as inert target-0 rows when the re-run drops a route.
            or (not item.buildable and item.merged_min_qty == 0)
        ]:
            del merged[type_id]  # the alchemy pass re-derives its rows
        for item in merged.values():
            item.jobs_allocated = 0
            item.runs_allocated = 0
            item.recommended_build_qty = 0
            item.recommended_buy_qty = 0
            item.recommended_action = None
            item.capacity_limited = False
            item.low_stock = False
            item.alchemy_output_qty = 0
            item.build_savings_per_unit = None
            item.unit_chain_cost = None
            item.savings_unpriced_inputs = 0
        _size_jobs(conn, ref, merged)
        _allocate_slots(conn, ref, merged, snapshot)
        if alchemy:
            _alchemy_pass(conn, ref, merged, snapshot)
        _finalize(conn, ref, merged, snapshot)

    # v1.22/v1.23: the invention VINTAGE is taken once the allocation has
    # converged (the pass adds nothing to the plan items).
    invention = _invention_pass(conn, ref, merged, snapshot)

    index_run_id = None
    if persist:
        # The run number is assigned inside the INSERT itself: a separate
        # MAX+1 read raced concurrent /run requests into duplicate numbers
        # (the UNIQUE index on run_number is the backstop).
        cur = conn.execute(
            "INSERT INTO index_run (run_number, planned_start, status, "
            "wallet_character_isk, wallet_corporation_isk) "
            "SELECT COALESCE(MAX(run_number), 0) + 1, datetime('now'), "
            "'planned', ?, ? FROM index_run",
            (snapshot.character_isk, snapshot.corporation_isk),
        )
        index_run_id = cur.lastrowid
        run_number = conn.execute(
            "SELECT run_number FROM index_run WHERE index_run_id = ?",
            (index_run_id,),
        ).fetchone()["run_number"]
    else:
        run_number = store.next_run_number(conn)
    if persist:
        for item in merged.values():
            cur = conn.execute(
                """
                INSERT INTO index_run_item (
                    index_run_id, type_id, on_hand_qty, in_progress_qty,
                    target_stock_qty, deficit_qty, recommended_action,
                    blueprint_id, activity_id, time_per_run, portion_size,
                    max_runs_per_job, total_runs_needed,
                    jobs_needed_unconstrained, jobs_allocated, runs_allocated,
                    recommended_build_qty, recommended_buy_qty,
                    build_savings_per_unit, capacity_limited, low_stock,
                    price_snapshot, depth, item_class, merged_min_qty,
                    alchemy_for_type_id, direct_unit_cost, alchemy_unit_cost,
                    alchemy_output_qty, alchemy_credit_qty, unit_install_fee,
                    savings_unpriced_inputs, price_region_wide,
                    buy_venue, structure_units_cheaper, unit_chain_cost
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                          ?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    index_run_id,
                    item.type_id,
                    item.on_hand_qty,
                    item.in_progress_qty,
                    item.target_stock_qty,
                    item.deficit_qty,
                    item.recommended_action,
                    item.blueprint_id,
                    item.activity_id,
                    item.time_per_run,
                    item.portion_size,
                    item.max_runs_per_job,
                    item.total_runs_needed,
                    item.jobs_needed_unconstrained,
                    item.jobs_allocated,
                    item.runs_allocated,
                    item.recommended_build_qty,
                    item.recommended_buy_qty,
                    item.build_savings_per_unit,
                    int(item.capacity_limited),
                    int(item.low_stock),
                    item.price_snapshot,
                    item.depth,
                    item.item_class,
                    item.merged_min_qty,
                    item.alchemy_for_type_id,
                    item.direct_unit_cost,
                    item.alchemy_unit_cost,
                    item.alchemy_output_qty,
                    item.alchemy_credit_qty,
                    item.unit_install_fee,
                    item.savings_unpriced_inputs,
                    int(item.price_region_wide),
                    item.buy_venue,
                    item.structure_units_cheaper,
                    item.unit_chain_cost,
                ),
            )
            item_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO index_run_item_pipeline "
                "(index_run_item_id, pipeline_id, qty_attributable, depth) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        item_id,
                        pipeline_id,
                        qty,
                        item.pipeline_depth.get(pipeline_id, item.depth),
                    )
                    for pipeline_id, qty in item.pipeline_share.items()
                ],
            )
        conn.executemany(
            "INSERT INTO index_run_invention (index_run_id, pipeline_id, "
            "t1_blueprint_id, decryptor_type_id, probability, invented_me, "
            "invented_te, runs_per_copy, datacores, "
            "decryptor_unit_price, invention_fee_per_attempt, "
            "copy_fee_per_attempt, cost_per_run) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    index_run_id,
                    row["pipeline_id"],
                    row["t1_blueprint_id"],
                    row["decryptor_type_id"],
                    row["probability"],
                    row["invented_me"],
                    row["invented_te"],
                    row["runs_per_copy"],
                    row["datacores"],
                    row["decryptor_unit_price"],
                    row["invention_fee_per_attempt"],
                    row["copy_fee_per_attempt"],
                    row["cost_per_run"],
                )
                for row in invention.values()
            ],
        )
        conn.commit()
    return Plan(
        index_run_id=index_run_id,
        run_number=run_number,
        items=merged,
        invention=invention,
    )


def _steady_output_qty(conn, plan: Plan, current: dict | None) -> dict | None:
    """Expansion overrides (pipeline_id -> qty) scaling the steady chain to
    what the line actually PRODUCES: ship batch multiples and BPC run caps
    round a final's build above its request, and every stage below must
    replace the built amount. Per final: next request = current request +
    (built − merged_min); the merged_min excess over the request is other
    pipelines' consumption of this final, which re-adds itself on
    expansion. Rounding is idempotent, so the caller's replan loop reaches
    a fixpoint (next == current) in one extra pass. A shared final's bump
    lands on its first pipeline — attribution only; steady plans are never
    persisted. None means the built scale IS the requested scale."""
    by_final: dict[int, list] = {}
    for pipeline in store.active_pipelines(conn):
        by_final.setdefault(pipeline["final_product_type_id"], []).append(
            pipeline
        )
    override: dict[int, int] = {}
    for type_id, pipelines in by_final.items():
        item = plan.items.get(type_id)
        for pipeline in pipelines:
            override[pipeline["pipeline_id"]] = (current or {}).get(
                pipeline["pipeline_id"], pipeline["output_qty_per_run"]
            )
        if item is None or not item.buildable:
            continue
        bump = item.total_runs_needed * item.portion_size - item.merged_min_qty
        if bump > 0:
            override[pipelines[0]["pipeline_id"]] += bump
    unchanged = all(
        override[pipeline["pipeline_id"]] == pipeline["output_qty_per_run"]
        for pipelines in by_final.values()
        for pipeline in pipelines
    )
    return None if unchanged else override


def plan_steady_state(conn, ref, snapshot: Snapshot) -> Plan:
    """The hypothetical index run at steady state: every stockpile sits at
    its target, so each stage installs exactly one cycle's replacement (the
    Phase 4 deficit algebra collapses to one cycle's draw, and the buffer
    and composite-extra-runs terms cancel out) and the buy list is one
    cycle's true purchases. ESI reality is deliberately ignored — the
    caller's snapshot supplies prices and slot pools only; its stock, jobs
    and wallets are discarded. Alchemy is assumed OFF for planning
    (decision 2026-08-24): the steady cycle runs direct reactions only,
    whatever the global setting says — substitution is an execution-time
    opportunity, not part of the line's baseline shape. Never persists.

    Steady state is defined on what the line PRODUCES, not what was
    requested: batching rounds finals up, so the chain is re-expanded at
    the built scale (_steady_output_qty, replanned to a fixpoint).
    Targets are a plan output, not an input: plan from empty to learn
    them, restock every buildable at target, and replan. Raw materials
    are then stocked at the input-purchase-margin excess a perpetual
    cycle carries (bought once, not re-bought every cycle) — raw stock is
    invisible to job sizing and allocation, so the draft's consumption is
    exact and one last pass yields the true buy list."""
    base = replace(
        snapshot,
        on_hand={},
        in_progress={},
        character_isk=0.0,
        corporation_isk=0.0,
    )
    first = plan_index_run(conn, ref, base, persist=False, alchemy=False)
    output_qty = None
    for _ in range(4):
        revised = _steady_output_qty(conn, first, output_qty)
        if revised == output_qty:
            break
        output_qty = revised
        first = plan_index_run(
            conn, ref, base, persist=False, output_qty=output_qty,
            alchemy=False,
        )

    # Finals are seeded at ZERO stock: from empty their dual-role netting
    # (ruling 2026-08-27) is a no-op (deficit = requested + component
    # share = merged_min), so the steady draft still replaces the full
    # component share every cycle. Seeding them at target would cancel
    # the component share and understate the whole subtree's steady draw.
    finals = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }
    stocked = {
        item.type_id: item.target_stock_qty
        for item in first.items.values()
        if item.buildable and item.type_id not in finals
    }
    draft = plan_index_run(
        conn,
        ref,
        replace(base, on_hand=stocked),
        persist=False,
        output_qty=output_qty,
        alchemy=False,
    )

    consumption = _planned_consumption(conn, ref, draft.items)
    raw_stock: dict[int, int] = {}
    for item in draft.items.values():
        # Phase 7 targets raws at consumption × (1 + margin); the excess
        # persists as stock in a perpetual cycle.
        if item.buildable:
            continue
        excess = item.target_stock_qty - consumption.get(item.type_id, 0)
        if excess > 0:
            raw_stock[item.type_id] = excess
    if not raw_stock:
        return draft
    return plan_index_run(
        conn,
        ref,
        replace(base, on_hand={**stocked, **raw_stock}),
        persist=False,
        output_qty=output_qty,
        alchemy=False,
    )


# ---------------------------------------------------------------------------
# Phase 8: cost-lot bookkeeping (FIFO vintage costing)
# ---------------------------------------------------------------------------


def record_purchase(
    conn, index_run_id: int | None, type_id: int, qty: int, unit_cost: float
) -> int:
    """Material entering the pipeline: new lot at snapshot price."""
    cur = conn.execute(
        "INSERT INTO cost_lot (type_id, created_index_run_id, "
        "quantity_original, quantity_remaining, unit_cost, source_type) "
        "VALUES (?, ?, ?, ?, ?, 'purchased')",
        (type_id, index_run_id, qty, qty, unit_cost),
    )
    return cur.lastrowid


def _fifo_consume(conn, output_lot_id: int, type_id: int, qty: int) -> float:
    """Draw qty of type_id from the oldest open lots, recording genealogy
    edges. Returns the total cost of what was drawn. Units beyond tracked
    lots (untracked pre-existing stock) are consumed at zero cost."""
    remaining = qty
    total_cost = 0.0
    for row in conn.execute(
        "SELECT lot_id, quantity_remaining, unit_cost FROM cost_lot "
        "WHERE type_id = ? AND quantity_remaining > 0 ORDER BY lot_id",
        (type_id,),
    ).fetchall():
        if remaining <= 0:
            break
        take = min(remaining, row["quantity_remaining"])
        conn.execute(
            "UPDATE cost_lot SET quantity_remaining = quantity_remaining - ? "
            "WHERE lot_id = ?",
            (take, row["lot_id"]),
        )
        conn.execute(
            "INSERT INTO lot_consumption VALUES (?, ?, ?) "
            "ON CONFLICT (output_lot_id, input_lot_id) "
            "DO UPDATE SET qty_consumed = qty_consumed + excluded.qty_consumed",
            (output_lot_id, row["lot_id"], take),
        )
        total_cost += take * row["unit_cost"]
        remaining -= take
    return total_cost


def complete_job(
    conn,
    index_run_id: int | None,
    product_type_id: int,
    qty_produced: int,
    materials: list[tuple[int, int]],
    install_cost: float,
) -> int:
    """A finished job becomes a manufactured lot whose unit cost blends the
    FIFO cost of consumed input lots plus the job installation fee. Each
    manufactured lot's unit_cost therefore already embeds its full upstream
    genealogy — walking back happens by construction."""
    cur = conn.execute(
        "INSERT INTO cost_lot (type_id, created_index_run_id, "
        "quantity_original, quantity_remaining, unit_cost, source_type) "
        "VALUES (?, ?, ?, ?, 0, 'manufactured')",
        (product_type_id, index_run_id, qty_produced, qty_produced),
    )
    lot_id = cur.lastrowid
    total = install_cost
    for material_id, qty in materials:
        total += _fifo_consume(conn, lot_id, material_id, qty)
    conn.execute(
        "UPDATE cost_lot SET unit_cost = ? WHERE lot_id = ?",
        (total / qty_produced if qty_produced else 0.0, lot_id),
    )
    return lot_id


def reprocess_unrefined(
    conn,
    index_run_id: int | None,
    unrefined_type_id: int,
    qty_consumed: int,
    composite_type_id: int,
    composite_qty: int,
    recovered: list[tuple[int, int, float]] = (),
) -> int:
    """The manual alchemy reprocess: unrefined lots are consumed FIFO and
    become a composite lot plus recovered-input lots. Recovered outputs
    enter at the given unit price (their market credit), scaled down when
    the credit exceeds the ISK actually consumed; the composite lot
    carries the residual cost, so total cost is conserved through the
    genealogy in both directions. Quantities are the REAL reprocess results, not the planning
    estimate — the plan is advisory, this records what happened.

    recovered: (type_id, quantity, unit_price) per recovered output.
    Returns the composite lot id."""
    cur = conn.execute(
        "INSERT INTO cost_lot (type_id, created_index_run_id, "
        "quantity_original, quantity_remaining, unit_cost, source_type) "
        "VALUES (?, ?, ?, ?, 0, 'manufactured')",
        (composite_type_id, index_run_id, composite_qty, composite_qty),
    )
    lot_id = cur.lastrowid
    total = _fifo_consume(conn, lot_id, unrefined_type_id, qty_consumed)
    # The market credit can exceed the ISK actually drawn from the
    # unrefined lots (cheap unrefined, dear recovered goo). Scale the
    # recovered lots' cost basis down so the genealogy never holds more
    # ISK than entered it — conservation, not valuation, is the invariant.
    credit = sum(qty * unit_price for _t, qty, unit_price in recovered)
    scale = min(1.0, total / credit) if credit > 0 else 0.0
    for type_id, qty, unit_price in recovered:
        conn.execute(
            "INSERT INTO cost_lot (type_id, created_index_run_id, "
            "quantity_original, quantity_remaining, unit_cost, source_type) "
            "VALUES (?, ?, ?, ?, ?, 'manufactured')",
            (type_id, index_run_id, qty, qty, unit_price * scale),
        )
    # max() guards float rounding: credit * (total / credit) can exceed
    # total by an ulp, and a sub-microISK negative unit cost is noise.
    residual = max(0.0, total - credit * scale)
    conn.execute(
        "UPDATE cost_lot SET unit_cost = ? WHERE lot_id = ?",
        (residual / composite_qty if composite_qty else 0.0, lot_id),
    )
    return lot_id


def record_finished_batch(
    conn,
    pipeline_id: int,
    index_run_id: int | None,
    output_lot_id: int,
    quantity: int,
    market_value_per_unit: float | None,
) -> int:
    """Finished product: cost basis is the lot's blended unit cost (which
    embeds the full FIFO genealogy back to the original purchase runs)."""
    row = conn.execute(
        "SELECT unit_cost FROM cost_lot WHERE lot_id = ?", (output_lot_id,)
    ).fetchone()
    cost_basis = quantity * row["unit_cost"]
    market_value = (
        quantity * market_value_per_unit
        if market_value_per_unit is not None
        else None
    )
    cur = conn.execute(
        "INSERT INTO finished_batch (pipeline_id, index_run_id, output_lot_id, "
        "quantity, total_cost_basis, market_value_at_completion, profit) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            pipeline_id,
            index_run_id,
            output_lot_id,
            quantity,
            cost_basis,
            market_value,
            (market_value - cost_basis) if market_value is not None else None,
        ),
    )
    conn.commit()
    return cur.lastrowid
