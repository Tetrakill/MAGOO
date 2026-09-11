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
  7.5 Sourcing pass (fill pricing, compressed substitution)
  7.6 Install check (planned jobs vs the stock there to feed them;
      finals by return, intermediates in proportion)
  8. Cost-lot bookkeeping primitives (FIFO vintage costing)
"""

import json
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linprog, milp
from scipy.sparse import coo_matrix

from . import bom, config, costing, industry, store

log = logging.getLogger(__name__)

# The synthetic hub rung's depth (review 2026-09-05, finding A2): a cached
# Jita QUOTE with no stored ladder stands in for the hub book as one
# rung deep enough to absorb any buy, so a structure-only ladder never
# routes a purchase to C-J6 at a dearer price than the hub quote.
_SYNTHETIC_HUB_DEPTH = 10**15


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
    # v1.25: the sell ladders the sourcing pass walks, per venue —
    # {store.BUY_VENUE_*: {type_id: ascending [(price, volume_remain[,
    # min_volume]), ...]}} — for every bought input and compressed
    # candidate (market.sell_ladders; the third element is ESI's
    # minimum fill since the 2026-09-05 review). Empty = single quotes,
    # no fill.
    sell_ladders: dict[str, dict[int, list]] = field(default_factory=dict)
    # Review 2026-09-05 (finding A2, contract C5): the cached Jita hub
    # quote per type — {type_id: price} — kept even where the structure
    # venue won Phase 1 (`prices` then holds the structure price). The
    # sourcing pass turns it into one synthetic unbounded hub rung when
    # no hub LADDER is stored, so the merged walk still compares a
    # structure-only ladder against Jita. Empty = no hub quotes known.
    hub_prices: dict[int, float] = field(default_factory=dict)
    # v1.26: the structure market's quote per type at ITS pricing basis
    # ({type_id: price}, market.cached_structure_quotes) — the synthetic
    # rung a 'min_sell' / 'max_buy' structure basis stands in with.
    structure_prices: dict[int, float] = field(default_factory=dict)
    # v1.27.1: the SELL reference per pipeline final for the install
    # check's ranking ({type_id: price}, ledger.final_quote — the hub
    # quote for sub-capitals, the capital structure's sell quote for
    # capital-class hulls, which Jita never quotes). A final absent here
    # falls back to its own plan-time price.
    sell_quotes: dict[int, float] = field(default_factory=dict)
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
    # One cycle's consumption AT THE JOBS' OWN ROUNDING (Phase 3.5, user
    # ruling 2026-09-09): merged_min rounds the cycle as one merged job;
    # this propagates it through the jobs Phase 5-7 will install (per-job
    # ceilings, uniform round-up, full reaction windows, whole copies),
    # so a stage stocked to it feeds its consumers' jobs. The target and
    # deficit basis; >= merged_min_qty within one pipeline (it can dip
    # below where pipelines share a consumer: merged_min sums separately
    # rounded runs, this merges first). merged_min stays the BOM figure
    # and the pipeline-share attribution basis.
    cycle_need_qty: int = 0
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
    # v1.26 fill-aware build-vs-buy: units of a buildable item the
    # market beat the build cost on (bought; the rest is built) and the
    # units dearer rungs could still supply — the only purchase
    # fallback a capacity shortfall may take (None: no ladder known,
    # the single-quote rule stands).
    market_buy_qty: int = 0
    market_fallback_qty: int | None = None
    low_stock: bool = False
    price_snapshot: float | None = None
    # v1.9: price_snapshot came from a region-wide fallback quote
    price_region_wide: bool = False
    # v1.10: the venue price_snapshot came from (store.BUY_VENUE_*; None
    # when unpriced) and, for structure buys, the units of the structure's
    # sell ladder that still beat the hub landed price — the run page shows
    # the rest as a Jita share when recommended_buy_qty exceeds it (v1.26.1).
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
    # Compressed sourcing (v1.25). On a compressed buy row (a synthetic
    # raw the pass injects): what the purchase is FOR — ((material_id,
    # units out at the yield, units used to cover demand), …) — plus the
    # ladder depth the fill walked (units on the chosen venue's ladder,
    # orders taken). On a covered raw row: the units of this cycle's
    # purchase the compressed buys cover (recommended_buy_qty is the direct
    # remainder) and the blended LANDED per-unit cost the realized costing
    # prices the raw at.
    compressed_outputs: tuple = ()
    compressed_ladder_units: int | None = None
    # Ruling R6 (2026-09-05): the whole-batch quantity the LP wanted
    # BEFORE the venue's ladder shrank the buy. Equal to the buy since the
    # v1.26.1 re-solve unless the pass cap was hit; the compressed tooltip
    # states the figure when it exceeds recommended_buy_qty.
    compressed_wanted_qty: int | None = None
    compressed_fill_orders: int | None = None
    compressed_covered_qty: int = 0
    effective_unit_cost: float | None = None
    # Fill pricing (v1.25, 2026-09-05): the direct buy walked across the
    # Jita and structure ladders — per-venue units, average raw fill
    # price and orders taken; units no stored ladder held (priced at the
    # last rung walked) and that price. Those unfilled units ride the
    # hub quantity only when Jita's stored ladder was truncated (its
    # book goes on); otherwise they are UNSOURCED — in no venue quantity
    # and no Multibuy block, still inside recommended_buy_qty and the
    # price_snapshot blend (ruling R5, 2026-09-05). hub_buy_qty None =
    # not fill-priced (no ladder anywhere).
    hub_buy_qty: int | None = None
    hub_fill_price: float | None = None
    hub_fill_orders: int | None = None
    structure_buy_qty: int | None = None
    structure_fill_price: float | None = None
    structure_fill_orders: int | None = None
    unfilled_qty: int = 0
    unfilled_price: float | None = None
    # Install check (v1.27.1, Phase 7.6): can this cycle's planned jobs
    # actually be installed from stock? On a row holding jobs: the runs
    # (and the jobs they occupy) that stock on hand, in-flight output and
    # this cycle's purchases can feed — the plan's own build output is
    # NOT available (it delivers next cycle; that lag is the pipeline);
    # the material that bound the figure (None when every planned run
    # installs); and, on a pipeline final, its install priority (1 =
    # highest return — finals take scarce stock in that order). On a
    # consumed row: the planned jobs' total draw and the units it
    # exceeds availability by. None = not a consumer / not consumed.
    install_runs: int | None = None
    install_jobs: int | None = None
    # Runs per installed job: uniform for an intermediate (install_jobs ×
    # install_per_job == install_runs — the per-job count rounded up the
    # way Phase 7 sizes it, user ruling 2026-09-09), the plan's own count
    # for an exact-quantity ship or a saturating reaction, whose last
    # job takes the remainder.
    install_per_job: int | None = None
    install_limited_by: int | None = None
    install_priority: int | None = None
    # The return on cost that ranked a final (final_return; None =
    # unpriced, ranked last) — persisted so the page restates exactly
    # what the order was built on.
    install_return: float | None = None
    install_draw_qty: int | None = None
    install_short_qty: int | None = None
    # v1.27.1 (user ruling 2026-09-11): of `alchemy_output_qty`, the
    # units produced to replace units the plan was going to BUY rather
    # than to cover a build shortfall. Phase 7 credits each against its
    # own figure, so neither is counted twice. Not persisted.
    alchemy_buy_qty: int = 0
    # v1.27.1 stock-aware backfill (Phase 6): jobs this item received
    # from slots whose allocated jobs cannot start this cycle. Not
    # persisted; the plan-time figure the tests read.
    backfilled_jobs: int = 0

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
    # v1.25: landed ISK the compressed pass saved against buying every
    # covered raw direct (None when the pass changed nothing).
    compressed_saving_isk: float | None = None

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

    # Targets and deficits are sized on cycle_need_qty — one cycle's
    # consumption at the jobs' own rounding (Phase 3.5) — not on the
    # merged BOM figure (user ruling 2026-09-09).
    for item in merged.values():
        if item.type_id in final_products:
            item.target_stock_qty = item.cycle_need_qty
        else:
            # round-before-ceil kills binary-float noise (100 * 1.1 ==
            # 110.00000000000001 would otherwise ceil to 111), mirroring
            # industry.required_quantity's guard.
            item.target_stock_qty = math.ceil(
                round(item.cycle_need_qty * buffer_mult, 9)
            )

    # Composite reaction inputs get extra runs' worth of material on top.
    for material_id, extra in _composite_extra_targets(
        ref, settings, class_settings, merged
    ).items():
        merged[material_id].target_stock_qty += extra

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
            component_share = item.cycle_need_qty - item.requested_qty
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
                + item.cycle_need_qty
                - item.on_hand_qty
                - item.in_progress_qty,
            )


def _composite_extra_targets(
    ref, settings, class_settings, merged: dict[int, PlanItem],
    consumers_with_jobs_only: bool = False,
) -> dict[int, int]:
    """The composite-reaction extra-runs adder per input material
    ({material_id: units}): each composite reaction in the plan puts
    `composite_reaction_extra_runs` runs' worth of its inputs on top of
    their targets. Phase 4 applies it for every composite (the BOM
    demand); the feedback loop (review 2026-09-05, finding A3) only for
    composites that actually HOLD jobs this cycle, so an input whose
    consumers all flipped to buy carries no adder either."""
    extra_runs = settings.composite_reaction_extra_runs
    extra: dict[int, int] = {}
    if extra_runs <= 0:
        return extra
    for item in merged.values():
        if (
            item.activity_id != config.ACTIVITY_REACTION
            or ref.type_info(item.type_id).group_id
            not in config.COMPOSITE_REACTION_GROUPS
        ):
            continue
        if consumers_with_jobs_only and item.runs_allocated <= 0:
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
                extra[material_id] = extra.get(
                    material_id, 0
                ) + industry.required_quantity(
                    extra_runs, base_qty, 0, mat_mult
                )
    return extra


def _steady_packing(
    ref, item: PlanItem, runs: int, max_runs: int, finals
) -> list[tuple[int, int]]:
    """[(jobs, runs each)] — how Phases 5-7 pack `runs` runs of an item
    into jobs at its window: an exact-quantity ship (a final; capitals,
    freighters, JFs anywhere) as Phase 7's divmod split (the last jobs
    short), a saturating reaction as full windows per job, everything
    else as uniform jobs with the per-job count rounded up (the slight
    overbuild nets off next cycle). The cycle-need pass draws each
    material through this packing so the target matches the installs."""
    if runs <= 0:
        return []
    jobs = math.ceil(runs / max_runs)
    info = ref.type_info(item.type_id)
    saturating = (
        item.activity_id == config.ACTIVITY_REACTION
        and info.group_id not in config.NON_SATURATING_REACTION_GROUPS
    )
    exact_total = item.type_id in finals or (
        info.category_id == config.CATEGORY_SHIP
        and info.group_id in config.EXACT_QTY_SHIP_GROUPS
    )
    if saturating:
        return [(jobs, max_runs)]
    if exact_total:
        base, extra = divmod(runs, jobs)
        return [
            (count, each)
            for count, each in ((jobs - extra, base), (extra, base + 1))
            if count > 0 and each > 0
        ]
    return [(jobs, math.ceil(runs / jobs))]


def _cycle_need(conn, ref, merged: dict[int, PlanItem]) -> dict[int, dict[int, int]]:
    """Phase 3.5 (user ruling 2026-09-09): one cycle's consumption of
    every item AT THE JOBS' OWN ROUNDING — stamped as cycle_need_qty, the
    target and deficit basis — plus each consumer's share of it
    ({material_id: {consumer_id: units}}), the proration basis the
    feedback loop uses (a self-consuming blueprint is not its own
    consumer).

    bom.expand's merged_min rounds an item's cycle as ONE job (ceil once
    per item). The jobs Phase 7 installs round PER JOB — six one-run
    Thanatos jobs each round their capital engines up, a saturating
    reaction runs a full window per job, an intermediate rounds its runs
    up to uniform jobs, a sub-capital final builds whole copies — so a
    stage stocked to the merged figure started every cycle a few percent
    short of what its consumers' jobs draw (the 11-run simulation of
    2026-09-09: a 1-3 % shortfall alternating every other cycle on the
    capital components and the fullerene reactions). This pass walks the
    merged chain top-down in depth order (every consumer sits shallower
    than its material, so each item's cycle is complete before it is
    packed), starting from the finals' requested output, and propagates
    each item's cycle quantity through the packing Phases 5-7 will give
    it: whole runs by _runs_for_units, jobs at its window
    (_job_windower), then _steady_packing. Within one pipeline
    cycle_need_qty >= merged_min_qty (a sum of per-job ceilings is never
    below the one ceiling); across pipelines that share a consumer it can
    be LOWER, since merged_min sums each pipeline's separately-rounded
    runs while this pass merges the demand before rounding (review
    2026-09-09). A line stocked at target installs every planned job —
    what the install check verifies — given slots for every stage: a
    stage denied slots leaves its inputs' targets prorated down (ruling
    R7), so the re-plan that grants them slots finds those inputs short."""
    window = _job_windower(conn, ref)
    class_settings = store.get_class_settings(conn)
    me_te = store.me_te_resolver(conn)
    finals = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }
    need: dict[int, int] = {t: 0 for t in merged}
    for item in merged.values():
        need[item.type_id] += item.requested_qty
    shares: dict[int, dict[int, int]] = {}
    for item in sorted(merged.values(), key=lambda i: (i.depth, i.type_id)):
        qty = need[item.type_id]
        item.cycle_need_qty = qty
        if not item.buildable or qty <= 0:
            continue
        runs = _runs_for_units(ref, item, qty)
        _time_per_run, max_runs = window(item)
        me, _te = me_te(item.blueprint_id, item.activity_id)
        mat_mult = industry.build_multiplier(
            ref,
            class_settings.get(item.item_class, industry.NPC_STATION),
            item.activity_id,
            "material",
            group_id=ref.type_info(item.type_id).group_id,
        )
        packing = _steady_packing(ref, item, runs, max_runs, finals)
        for material_id, base_qty in ref.materials(
            item.blueprint_id, item.activity_id
        ):
            if material_id not in need:
                continue  # every expanded material is in the plan
            units = sum(
                jobs * industry.required_quantity(each, base_qty, me, mat_mult)
                for jobs, each in packing
            )
            need[material_id] += units
            if material_id != item.type_id:
                shares.setdefault(material_id, {})[item.type_id] = units
        # A self-consuming blueprint counts its own draw once (bom.expand
        # does the same) without re-packing.
        item.cycle_need_qty = need[item.type_id]
    return shares


def _steady_shares(conn, ref, merged: dict[int, PlanItem]) -> dict[int, dict[int, int]]:
    """Each buildable consumer's ONE-CYCLE requirement of every material
    it consumes — {material_id: {consumer_id: units}} — at the jobs' own
    rounding (the cycle-need pass; it re-stamps cycle_need_qty, which is
    idempotent). The feedback loop prorates a stage's stockpile target
    by the share of this that comes from consumers holding jobs this
    cycle (review 2026-09-05, finding A3 / ruling R7)."""
    return _cycle_need(conn, ref, merged)


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


def _game_job_run_cap(time_per_run: float) -> int:
    """In-game per-job run ceiling, one rule for both activities
    (user-verified 2026-08-21): runs keep being added while the job's total
    MODIFIED time is under 30 days, so the last run may overhang —
    ceil(30d / tpr) — and a single run longer than 30 days installs as
    1 run. The SDE's maxProductionLimit never applies: for manufacturing
    it is a copy-runs concept, and for reactions the client accepts more
    runs than the formula's figure (user-verified 2026-09-05, ruling R2 —
    the earlier "additional ceiling where lower" arm was WRONG and is
    gone). _ceil, not math.ceil: 30d / tpr lands an ulp above an exact
    integer for many run times (review 2026-09-05, finding A5)."""
    return max(1, _ceil(config.MAX_JOB_SECONDS / time_per_run))


def _skill_levels(settings) -> industry.SkillLevels:
    # v1.22: the mapping lives on Settings so costing's invention math can
    # reuse it without importing the engine.
    return settings.skill_levels()


def _runs_for_units(ref, item: PlanItem, units: int) -> int:
    """Whole runs for `units` of an item. Sub-capital ships build whole
    blueprint copies: when runs-per-BPC is set (pasted, or materialised
    from the invention choice) it is the batch unit — never a partial
    BPC; capacity gets the final word in Phase 7. With no runs-per-BPC
    the final builds its exact quantity (the global ship batch multiple
    was removed 2026-09-05: every invention pipeline overrode it, and a
    BPO-built hull has no copy to fill). Capitals, freighters, and jump
    freighters are exempt — they build in exact quantities.

    Review 2026-09-05 (finding A8, coordinator ruling): the whole-copy
    rounding stands whatever the window. In game a copy keeps its unused
    runs, so parallel jobs on separate copies leave reusable runs for
    the next cycle rather than wasting copies; the runs per job stay
    capped by the window, the 30-day rule and the copy itself (a job
    never installs more runs than its copy holds). The T3 whole-copies
    contract (a 20-run Intact-relic hull builds in whole 20-hull batches)
    depends on this. v1.26: the fill-aware build-vs-buy split re-sizes
    the built part through the same rule."""
    runs = math.ceil(max(0, units) / item.portion_size)
    info = ref.type_info(item.type_id)
    if (
        info.category_id == config.CATEGORY_SHIP
        and info.group_id not in config.EXACT_QTY_SHIP_GROUPS
        and item.bpc_runs_limit
        and item.bpc_runs_limit > 1
    ):
        multiple = item.bpc_runs_limit
        runs = math.ceil(runs / multiple) * multiple
    return runs


def _job_windower(conn, ref):
    """window(item) -> (time_per_run, max_runs_per_job) for a buildable
    item: the modified time of one run (ME/TE, facility and skills) and
    how many runs one job may hold. One job per slot for the full cycle;
    jobs are installed simultaneously and never restarted mid-cycle, so
    the window (`max_run_duration_hours`) caps the runs per job; jobs
    longer than the window span multiple cycles (their output counts as
    in-progress stock, and snapshot_from_state nets them from the slot
    pool while they run). A job can never exceed the runs on its
    blueprint copy, and both activities share the in-game 30-day per-job
    run cap (the reaction formula's maxProductionLimit no longer caps
    anything — ruling R2, 2026-09-05). _floor, not math.floor: window /
    time_per_run lands an ulp BELOW an exact integer for many run times
    and dropped a whole run per job (review 2026-09-05, finding A5).
    Shared by _size_jobs (Phase 5) and the cycle-need pass (Phase 3.5),
    so both pack an item's cycle into the same jobs."""
    settings = store.get_settings(conn)
    class_settings = store.get_class_settings(conn)
    me_te = store.me_te_resolver(conn)
    skills = _skill_levels(settings)
    window_seconds = settings.max_run_duration_hours * 3600.0

    def window(item: PlanItem) -> tuple[float, int]:
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
        time_per_run = industry.job_time_seconds(
            blueprint.base_time, 1, te, time_mult
        ) * industry.skill_time_multiplier(
            ref, item.blueprint_id, item.activity_id, skills
        )
        if time_per_run <= 0:
            raise ValueError(
                f"non-positive job time for {item.name}: check skill "
                "levels, TE, and structure/rig settings"
            )
        game_cap = _game_job_run_cap(time_per_run)
        max_runs = min(
            max(1, _floor(window_seconds / time_per_run)), game_cap
        )
        if item.bpc_runs_limit is not None:
            max_runs = min(max_runs, item.bpc_runs_limit)
        return time_per_run, max_runs

    return window


def _size_jobs(conn, ref, merged: dict[int, PlanItem]):
    window = _job_windower(conn, ref)

    for item in merged.values():
        if item.deficit_qty <= 0:
            continue
        if not item.buildable:
            # Raw inputs are bought just-in-time in Phase 7, sized to the
            # consumption of the jobs actually allocated — not to a
            # stockpile target.
            continue

        item.time_per_run, item.max_runs_per_job = window(item)
        item.total_runs_needed = _runs_for_units(ref, item, item.deficit_qty)
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
    COUNTED so the UI can flag the figure as understated — as are the
    invention inputs (datacores / relic / decryptor) a computed
    invention adder priced at zero (review 2026-09-05). Mirrors Phase-2
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
    # Review 2026-09-05 (finding A4): the invention inputs the figure
    # priced at zero (datacores / relic / decryptor with no quote), per
    # final — carried beside bpc_per_unit so they reach the final's
    # savings_unpriced_inputs badge like an unpriced raw leaf does.
    bpc_unpriced: dict[int, int] = {}
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
            bpc_unpriced[tid] = cost.unpriced
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
        unpriced += bpc_unpriced.get(type_id, 0)
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
    the item's chain cost and unpriced-raw-leaf count — since v1.27.1 even
    when the item itself has no buy price (savings None): the install
    check ranks a capital final, which has no Jita quote, by its return
    on THIS chain cost against its structure-market sell quote."""
    cost, unpriced = chain(item.type_id)
    item.unit_chain_cost = cost
    item.savings_unpriced_inputs = unpriced
    landed = buy_cost(item.type_id)
    if landed is None:
        return None
    return landed - cost


class _LadderLookup:
    """One item's sell ladders per venue as the engine walks them —
    shared by the fill-aware build-vs-buy rule (Phase 5, v1.26) and the
    sourcing pass (Phase 7.5). Per venue: the stored ladder when that
    market's pricing basis is 'ladder'; otherwise ('min_sell' /
    'max_buy', v1.26) ONE unbounded synthetic rung at the market's quote
    (`synthetic` says which). A 'ladder' hub with no stored ladder gets
    the synthetic rung only beside a structure ladder (finding A2, so a
    structure book alone can never fill-price a buy the Jita quote
    covers); with no ladder and no quote anywhere the item keeps its
    single Phase 1 quote untouched. A ladder with no positive-volume
    rung counts as absent (finding A11)."""

    def __init__(self, snapshot: Snapshot, settings, ref):
        self.snapshot, self.settings, self.ref = snapshot, settings, ref
        self.rates = {
            venue: settings.freight_in_rate(venue)
            for venue in (store.BUY_VENUE_HUB, store.BUY_VENUE_STRUCTURE)
        }
        self._hub = snapshot.sell_ladders.get(store.BUY_VENUE_HUB, {})
        self._structure = snapshot.sell_ladders.get(store.BUY_VENUE_STRUCTURE, {})
        self._synthetic: set[tuple[str, int]] = set()
        self._memo: dict[int, tuple] = {}

    @staticmethod
    def _stored(ladder):
        if any(int(o[1]) > 0 for o in (ladder or ())):
            return ladder
        return None

    def _hub_quote(self, type_id: int):
        price = self.snapshot.hub_prices.get(type_id)
        if price is None and self.snapshot.venue(type_id) == store.BUY_VENUE_HUB:
            price = self.snapshot.price(type_id)  # the hub won Phase 1
        return price

    def _structure_quote(self, type_id: int):
        price = self.snapshot.structure_prices.get(type_id)
        if (
            price is None
            and self.snapshot.venue(type_id) == store.BUY_VENUE_STRUCTURE
        ):
            price = self.snapshot.price(type_id)
        return price

    def ladders_of(self, type_id: int):
        if type_id in self._memo:
            return self._memo[type_id]
        settings = self.settings
        hub = structure = None
        if settings.walks_ladder(store.BUY_VENUE_HUB):
            hub = self._stored(self._hub.get(type_id))
        if settings.structure_buy_enabled and settings.walks_ladder(
            store.BUY_VENUE_STRUCTURE
        ):
            structure = self._stored(self._structure.get(type_id))
        if settings.structure_buy_enabled and not settings.walks_ladder(
            store.BUY_VENUE_STRUCTURE
        ):
            price = self._structure_quote(type_id)
            if price is not None:
                structure = [(price, _SYNTHETIC_HUB_DEPTH)]
                self._synthetic.add((store.BUY_VENUE_STRUCTURE, type_id))
        if not settings.walks_ladder(store.BUY_VENUE_HUB) or (
            hub is None and structure is not None
        ):
            price = self._hub_quote(type_id)
            if price is not None:
                hub = [(price, _SYNTHETIC_HUB_DEPTH)]
                self._synthetic.add((store.BUY_VENUE_HUB, type_id))
        self._memo[type_id] = (hub, structure)
        return hub, structure

    def venue_ladder(self, venue: str, type_id: int):
        hub, structure = self.ladders_of(type_id)
        return structure if venue == store.BUY_VENUE_STRUCTURE else hub

    def synthetic(self, venue: str, type_id: int) -> bool:
        self.ladders_of(type_id)
        return (venue, type_id) in self._synthetic

    def has_ladder(self, type_id: int) -> bool:
        hub, structure = self.ladders_of(type_id)
        return hub is not None or structure is not None

    def _m3(self, type_id: int) -> float:
        return self.ref.type_info(type_id).freight_volume

    def direct_fill(self, type_id: int, qty: int) -> costing.DirectFill:
        hub, structure = self.ladders_of(type_id)
        return costing.fill_merged(
            hub, structure, qty, self.rates[store.BUY_VENUE_HUB],
            self.rates[store.BUY_VENUE_STRUCTURE], self._m3(type_id),
        )

    def rungs_taken(self, type_id: int, qty: int):
        hub, structure = self.ladders_of(type_id)
        return costing.rungs_taken(
            hub, structure, qty, self.rates[store.BUY_VENUE_HUB],
            self.rates[store.BUY_VENUE_STRUCTURE], self._m3(type_id),
        )


def _resize_build(ref, item: PlanItem, units: int) -> None:
    """Re-size an item's jobs to `units` (the part of its cycle quantity
    the market did not beat), the same rounding _size_jobs used."""
    item.total_runs_needed = _runs_for_units(ref, item, units)
    item.jobs_needed_unconstrained = math.ceil(
        item.total_runs_needed / item.max_runs_per_job
    )


def _saturating_reaction(ref, item: PlanItem) -> bool:
    """A reaction whose every job runs the full window (composites and
    the like): its output is a whole-job multiple, not the deficit."""
    return (
        item.activity_id == config.ACTIVITY_REACTION
        and ref.type_info(item.type_id).group_id
        not in config.NON_SATURATING_REACTION_GROUPS
    )


def _market_split(ref, item: PlanItem, ladders: _LadderLookup) -> None:
    """Fill-aware build-vs-buy (v1.26, 2026-09-06). Walk the item's
    merged sell ladders for its whole cycle quantity: every unit whose
    landed rung price is at or below the vertically-integrated build
    cost is BOUGHT (market_buy_qty), the rest — dearer rungs and units
    no market holds — is BUILT, the jobs re-sized to that part. The
    dearer rungs are the only purchase fallback a capacity shortfall
    may take (market_fallback_qty); a shortfall beyond them is unmet.
    build_savings_per_unit becomes the built units' saving against
    those dearer rungs (the MILP weight), None when nothing dearer
    exists (no fallback: allocated ahead of priced contenders, like an
    unpriced item). Every unit cheaper on the market: the figure goes
    non-positive and Phase 7 buys the lot, as the 2026-08-20 rule
    always did — now judged at the fill over the quantity, not the
    best single order. No rung anywhere (no ladder, no quote): the
    single-quote figure stands untouched.

    A SATURATING reaction runs the full window per job whatever the
    deficit, so its built output is a whole-job multiple: only the units
    those jobs leave uncovered are worth buying (verifier counterexample
    2026-09-06 — Crystallite Alloy bought 200 cheap units on top of jobs
    that already overshot the need). Two documented edges: the fallback
    counts dearer rungs at the split's own take, so a rung whose
    min_volume exceeds a later capacity shortfall lands unsourced in the
    sourcing pass (badged unsourced); and units past a Jita ladder
    truncated at the pull cap count as 'no market' here, though the
    sourcing pass (ruling R5) would price a remainder there."""
    units = item.total_runs_needed * item.portion_size
    chain_cost = item.unit_chain_cost
    if units <= 0 or chain_cost is None:
        return
    taken = ladders.rungs_taken(item.type_id, units)
    if not taken:
        return
    cheaper = dearer = 0
    cheaper_cost = dearer_cost = 0.0
    for landed, _price, take, _venue in taken:
        if landed <= chain_cost:
            cheaper += take
            cheaper_cost += take * landed
        else:
            dearer += take
            dearer_cost += take * landed
    if cheaper >= units:
        item.build_savings_per_unit = cheaper_cost / units - chain_cost
        return
    if cheaper > 0 and _saturating_reaction(ref, item):
        # Whole-window jobs: buy only what the jobs sized for the rest
        # leave uncovered, never units their overshoot already makes.
        runs = _runs_for_units(ref, item, units - cheaper)
        jobs = math.ceil(runs / item.max_runs_per_job)
        output = jobs * item.max_runs_per_job * item.portion_size
        cheaper = min(cheaper, max(0, units - output))
    item.market_fallback_qty = dearer
    if cheaper > 0:
        item.market_buy_qty = cheaper
        _resize_build(ref, item, units - cheaper)
    item.build_savings_per_unit = (
        dearer_cost / dearer - chain_cost if dearer else None
    )


def _allocate_slots(conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot, backfill: bool = True):
    chain, buy_cost = _chain_coster(conn, ref, snapshot)
    ladders = _LadderLookup(snapshot, store.get_settings(conn), ref)
    finals = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }

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
            # v1.26: judge the buy side at its fill over the cycle
            # quantity, unit by unit, and split the item where the
            # market beats the build cost on part of it only.
            if item.type_id not in finals and item.build_savings_per_unit is not None:
                _market_split(ref, item, ladders)

        # Building above the LANDED market price wastes ISK regardless of
        # slot pressure (decision 2026-08-20; landed since 2026-08-23):
        # INTERMEDIATES whose savings (landed buy price − integrated chain
        # cost) are zero or negative never get slots — contended or not —
        # and Phase 7 flips their deficit to purchases instead. Unpriced
        # items (savings None) stay: they have no purchase fallback.
        # Pipeline FINALS are exempt (2026-08-21): they are built to SELL,
        # so "buy your own product" is never actionable advice; their
        # negative paper margin is surfaced as a badge, not a buy order.
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


    if not backfill:
        # The steady-state planner: a what-if has no stock to backfill
        # from and must allocate within its pool (review 2026-09-10).
        return
    # ---- stock-aware backfill (v1.27.1, user ruling 2026-09-09) --------
    # A job stock cannot feed this cycle holds no slot in practice. The
    # install-time rationing (Phase 7.6's core at ALLOCATION-time
    # availability: raws and market-beaten intermediates unlimited —
    # Phase 7 buys them — other buildables at stock + the units the
    # market already beat) says which allocated jobs can start; the
    # slots of the rest go, in savings order (finals, unpriced, then
    # priced by savings per job), to the contenders the pool starved,
    # for as many extra jobs as the LEFTOVER stock feeds. The starved
    # jobs stay in the plan — they wait for stock and keep their
    # suppliers sized — so a pool's planned jobs may exceed it; its
    # startable jobs never do (the run page shows both).
    rationing = _ration(
        conn, ref, merged, snapshot, _allocation_availability(conn, ref, merged, snapshot)
    )
    draw_of = _draw_calculator(conn, ref)
    leftover = dict(rationing.remaining)

    def left(m: int) -> int:
        return leftover[m] if m in leftover else rationing.left(m)

    def feedable(item: PlanItem, want: int):
        """(extra jobs the leftover stock feeds, their draw, their output)
        — the most of `want` jobs at the item's window (a saturating
        reaction runs full windows; anything else only the runs it still
        needs). Where the item's uncovered need is bought (a priced
        intermediate: the buy its consumers' startable jobs were counted
        on), the extra jobs replace that buy with output that lands at
        the END of the cycle — so they may only convert the part of it
        no startable consumer draws (what is left of the item itself)."""
        # The extra jobs are sized as Phase 7 will size the row once it
        # holds them (_sized_runs — review 2026-09-10: a non-exact row is
        # re-split uniform and rounded UP across ALL its jobs, so the
        # round-up of the existing jobs is part of the extra draw); the
        # draw is what those jobs add over the allocation-time model the
        # rationing already charged (min(total, jobs × window) runs, a
        # saturating reaction at full windows).
        J = item.jobs_allocated
        sat = _saturating_reaction(ref, item)
        bought = item.type_id not in finals and snapshot.price(item.type_id) is not None
        buy_now = _fallback_buy_of(ref, finals, item) if bought else 0
        charged = (
            J * item.max_runs_per_job
            if sat
            else min(item.total_runs_needed, J * item.max_runs_per_job)
        )
        before = draw_of(item, charged, J) if J > 0 and charged > 0 else {}

        def draw_k(k: int) -> dict[int, int]:
            runs = _sized_runs(ref, finals, item, J + k)
            if runs <= 0:
                return {}
            after = draw_of(item, runs, J + k)
            return {
                m: q - before.get(m, 0)
                for m, q in after.items()
                if q - before.get(m, 0) > 0
            }

        def replaced_k(k: int) -> int:
            return buy_now - _fallback_buy_of(ref, finals, item, k) if bought else 0

        def fits(k: int) -> bool:
            if replaced_k(k) > left(item.type_id):
                return False
            return all(q <= left(m) for m, q in draw_k(k).items())

        lo, hi = 0, want
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(mid):
                lo = mid
            else:
                hi = mid - 1
        return lo, draw_k(lo), replaced_k(lo)

    for activity_id in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION):
        pool = [
            i
            for i in merged.values()
            if i.activity_id == activity_id
            and i.recommended_action == "build"
            and i.alchemy_for_type_id is None
        ]
        freed = sum(
            i.jobs_allocated - rationing.startable_jobs.get(i.type_id, i.jobs_allocated)
            for i in pool
        )
        if freed <= 0:
            continue
        candidates = [
            i
            for i in pool
            if i.jobs_needed_unconstrained > i.jobs_allocated
            and _type_known(ref, i.type_id)
            and (
                i.type_id in finals
                or i.build_savings_per_unit is None
                or i.build_savings_per_unit > 0
            )
        ]
        candidates.sort(
            key=lambda i: (
                0 if i.type_id in finals else 1 if i.build_savings_per_unit is None else 2,
                -((i.build_savings_per_unit or 0.0) * i.portion_size * i.max_runs_per_job),
                i.name,
            )
        )
        for item in candidates:
            if freed <= 0:
                break
            want = min(item.jobs_needed_unconstrained - item.jobs_allocated, freed)
            k, need, replaced = feedable(item, want)
            if k <= 0:
                continue
            for m, q in need.items():
                leftover[m] = left(m) - q
            if replaced:
                leftover[item.type_id] = left(item.type_id) - replaced
            item.jobs_allocated += k
            item.backfilled_jobs += k
            freed -= k

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
    never drops below the deficit. Direct reactions are far more
    slot-efficient, so alchemy never takes a slot a STARTABLE direct job
    holds: under a contended pool (every slot allocated on paper) the
    free slots are the ones direct jobs stock cannot feed this cycle, and
    the alchemy jobs must themselves be startable (v1.27.1, user ruling
    2026-09-09; it disabled alchemy entirely before)."""
    settings = store.get_settings(conn)
    if (
        not settings.alchemy_enabled
        or settings.alchemy_reprocess_yield <= 0
        or settings.max_alchemy_jobs_per_type <= 0
    ):
        return
    slots = snapshot.slots_available.get(config.ACTIVITY_REACTION, 0)
    allocated = sum(
        i.jobs_allocated
        for i in merged.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    # With spare slots on paper the pass runs as it always has (the plan
    # is advisory; Phase 7.6 rations the alchemy jobs like any other).
    # Under a CONTENDED pool (every slot allocated on paper) it used to
    # stay out entirely; since v1.27.1 (user ruling 2026-09-09) the slots
    # that count are the ones STARTABLE direct jobs hold — a direct job
    # stock cannot feed this cycle holds no slot — so alchemy may run in
    # the rest by the original rules, ranked by savings: a swap drops
    # the composite's own unstartable direct job where it has one (it
    # held no slot, so the alchemy jobs cost their full count), else a
    # startable one (net jobs − 1, its inputs back in the pot), and the
    # alchemy jobs must themselves be startable (their fuel block is in
    # the leftover stock; the goo is bought just in time).
    contended = allocated >= slots
    rationing = _ration(
        conn, ref, merged, snapshot, _allocation_availability(conn, ref, merged, snapshot)
    )
    startable_direct = sum(
        rationing.startable_jobs.get(i.type_id, i.jobs_allocated)
        for i in merged.values()
        if i.activity_id == config.ACTIVITY_REACTION
    )
    spare = (slots - startable_direct) if contended else (slots - allocated)
    if spare <= 0:
        return
    unstartable = {
        i.type_id: i.jobs_allocated - rationing.startable_jobs.get(i.type_id, i.jobs_allocated)
        for i in merged.values()
        if i.activity_id == config.ACTIVITY_REACTION
    }
    leftover = dict(rationing.remaining)
    draw_of = _draw_calculator(conn, ref)

    def left(m: int) -> int:
        return leftover[m] if m in leftover else rationing.left(m)

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
            # Direct jobs to displace, or units the plan means to BUY
            # that the route could supply instead (user ruling
            # 2026-09-11). A market-preferred composite carries its whole
            # cycle need with no jobs (Phase 7 buys the shortfall); where
            # ladders split it, the bought part is `market_buy_qty`.
            # Neither, and there is nothing for the route to beat.
            or (
                item.jobs_allocated <= 0
                and item.total_runs_needed <= 0
                and item.market_buy_qty <= 0
            )
            or ref.type_info(composite_id).group_id
            in config.NON_SATURATING_REACTION_GROUPS
            # The route's unrefined product is already a demanded plan row
            # (a pipeline sells it, or the chain draws it). The pass
            # OVERWRITES `merged[unrefined_id]` with its own job row, which
            # would destroy that row's request, cycle need and pipeline
            # attribution — and the feedback loop then deletes it every
            # pass as an alchemy row, so the demand vanishes with no unmet
            # flag (review 2026-09-10). Leave the route alone.
            or route.unrefined_id in merged
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
        # _floor: the same ulp guard as _size_jobs (review 2026-09-05,
        # finding A5); the formula's maxProductionLimit no longer caps
        # (ruling R2).
        max_runs = min(
            max(1, _floor(window_seconds / time_per_run)),
            _game_job_run_cap(time_per_run),
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
        # What the route has to beat: the direct build where the item
        # holds jobs, else the LANDED market price it would otherwise be
        # bought at (user ruling 2026-09-11 — a bought composite has no
        # build to compare against, and comparing it to one it already
        # lost would keep the route out for ever).
        buys = item.jobs_allocated <= 0
        benchmark = (landed(composite_id) or 0.0) if buys else direct_unit
        if benchmark <= 0 or alchemy_unit >= benchmark:
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
                "buys": buys,
                "savings_per_unit": benchmark - alchemy_unit,
                # A probe row for the unrefined formula's draw (its fuel
                # block is the one input stock must hold).
                "probe": PlanItem(
                    type_id=route.unrefined_id,
                    name=ref.type_info(route.unrefined_id).name,
                    item_class="reactions",
                    depth=item.depth,
                    blueprint_id=route.formula.blueprint_id,
                    activity_id=config.ACTIVITY_REACTION,
                    portion_size=route.formula.portion_size,
                ),
            }
        )

    alchemy_items: dict[int, PlanItem] = {}
    while True:
        best = None
        for cand in candidates:
            item = cand["item"]
            buys = cand["buys"]
            if not buys and item.jobs_allocated <= 0:
                continue
            existing = alchemy_items.get(cand["route"].unrefined_id)
            jobs_so_far = existing.jobs_allocated if existing else 0
            if buys:
                # Nothing to drop: the route supplies units the plan was
                # going to buy. A market-preferred row keeps its whole
                # cycle need (Phase 7 buys the shortfall and already
                # credits `alchemy_output_qty` against it); a ladder-split
                # row carries the bought part in `market_buy_qty`.
                needed = max(
                    item.total_runs_needed * item.portion_size,
                    item.market_buy_qty,
                )
                covered_without = item.alchemy_output_qty
            else:
                out_d = item.max_runs_per_job * item.portion_size
                needed = item.total_runs_needed * item.portion_size
                covered_without = (
                    item.jobs_allocated - 1
                ) * out_d + item.alchemy_output_qty
            residual = max(0, needed - covered_without)
            # A build candidate's last direct job is mostly overshoot, so
            # residual 0 is the CHEAP first swap and must stay (v1.4). A
            # buy candidate with nothing left to replace has no swap.
            if buys and residual <= 0:
                continue
            jobs = max(1, math.ceil(residual / cand["out_per_job"]))
            # Under a contended pool: displace the composite's direct job
            # that cannot start where it has one (it held no slot, so the
            # alchemy jobs cost `jobs` slots), else a startable one (its
            # slot is reused), and only where the alchemy jobs can start.
            drops_unstartable = (
                not buys and contended and unstartable.get(item.type_id, 0) > 0
            )
            # A bought composite frees no slot of its own, so its alchemy
            # jobs cost their full count whatever the pool looks like.
            net = jobs if (buys or drops_unstartable) else jobs - 1
            if buys:
                # Its residual is the WHOLE purchase, so covering it often
                # wants more than the per-type cap or the spare slots
                # hold. Take a partial bite rather than veto the route
                # (a direct swap stays all-or-nothing: its residual is one
                # job's overshoot, and half a swap would under-cover the
                # deficit the dropped job was carrying).
                room = min(cap - jobs_so_far, spare)
                if room < 1:
                    continue
                jobs = min(jobs, room)
                net = jobs
            elif jobs_so_far + jobs > cap or net > spare:
                continue
            # Only inputs that are plan rows gate the swap (the fuel
            # block, and goo the chain already buys); an input no direct
            # formula demands is not in `merged` yet — the pass adds it
            # below and Phase 7 buys it just in time (review 2026-09-10).
            need = (
                {
                    m: q
                    for m, q in draw_of(
                        cand["probe"], jobs * cand["max_runs"], jobs
                    ).items()
                    if m in merged
                }
                if contended else {}
            )
            if any(q > left(m) for m, q in need.items()):
                continue
            # Rank by ISK saved on the units these jobs actually supply,
            # per spare slot consumed. A clamped buy replacement covers
            # only part of its residual, so crediting the whole of it
            # would out-rank honest candidates (2026-09-11); an unclamped
            # swap always covers its residual, so this is the old figure.
            covered = min(residual, jobs * cand["out_per_job"])
            score = (
                cand["savings_per_unit"] * max(covered, 1) / max(net, 1)
            )
            if best is None or score > best["score"]:
                best = {"cand": cand, "jobs": jobs, "score": score, "net": net,
                        "buys": buys, "drops_unstartable": drops_unstartable,
                        "need": need}
        if best is None:
            break
        cand = best["cand"]
        item, route = cand["item"], cand["route"]
        produced = best["jobs"] * cand["out_per_job"]
        if not best["buys"]:
            item.jobs_allocated -= 1
        else:
            # Credited against the purchase, not the build shortfall.
            item.alchemy_buy_qty += min(produced, item.market_buy_qty)
        item.alchemy_output_qty += produced
        spare -= best["net"]
        if best["drops_unstartable"]:
            unstartable[item.type_id] -= 1
        elif contended and not best["buys"]:
            # A startable direct job gave up its slot and its inputs for
            # the cycle (a job's draw, or the shorter run count the
            # rationing granted it).
            granted = rationing.startable_runs.get(item.type_id, 0)
            runs = min(item.max_runs_per_job, granted)
            if runs > 0:
                for m, q in draw_of(item, runs, 1).items():
                    leftover[m] = left(m) + q
        for m, q in best["need"].items():
            leftover[m] = left(m) - q
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
    draw_of = _draw_calculator(conn, ref)
    consumption: dict[int, int] = {}
    for item in merged.values():
        if item.runs_allocated <= 0:
            continue
        for material_id, qty in draw_of(
            item, item.runs_allocated, item.jobs_allocated
        ).items():
            consumption[material_id] = (
                consumption.get(material_id, 0) + qty
            )
    return consumption


def _draw_calculator(conn, ref):
    """draw_of(item, runs, jobs) -> {material_id: units} — what `runs`
    runs of the item's blueprint consume when installed as `jobs` jobs
    (0 jobs = one job), with the game's once-per-job rounding: the runs
    split divmod-style, the extra jobs running one more run. The one
    material walk _planned_consumption and the install check share, so
    the two can never disagree on a job's draw."""
    class_settings = store.get_class_settings(conn)
    me_te = store.me_te_resolver(conn)
    per_item: dict[int, tuple] = {}

    def draw_of(item: PlanItem, runs: int, jobs: int) -> dict[int, int]:
        if runs <= 0:
            return {}
        cached = per_item.get(item.type_id)
        if cached is None:
            setting = class_settings.get(item.item_class, industry.NPC_STATION)
            me, _te = me_te(item.blueprint_id, item.activity_id)
            mat_mult = industry.build_multiplier(
                ref,
                setting,
                item.activity_id,
                "material",
                group_id=ref.type_info(item.type_id).group_id,
            )
            cached = per_item[item.type_id] = (
                me,
                mat_mult,
                ref.materials(item.blueprint_id, item.activity_id),
            )
        me, mat_mult, materials = cached
        jobs = jobs if jobs > 0 else 1
        base_runs, extra = divmod(runs, jobs)
        out: dict[int, int] = {}
        for material_id, base_qty in materials:
            qty = (jobs - extra) * industry.required_quantity(
                base_runs, base_qty, me, mat_mult
            )
            if extra:
                qty += extra * industry.required_quantity(
                    base_runs + 1, base_qty, me, mat_mult
                )
            out[material_id] = qty
        return out

    return draw_of


# ---------------------------------------------------------------------------
# Phase 7.6: install check — can the planned jobs be installed from stock?
# ---------------------------------------------------------------------------


def final_return(
    ref, settings, type_id: int, price, chain_cost
) -> float | None:
    """A final's return on cost — net proceeds per unit (after sell-side
    fees, the Profit views' figure) minus the vertically-integrated chain
    cost, over that chain cost — from its plan-time sell quote and chain
    cost. None when the final is unpriced or its chain cost is unknown /
    zero: such a final ranks after every priced one. Primitives, not a
    PlanItem, so the run page can restate the figure from a persisted
    row (install priority is explained by it)."""
    if price is None or chain_cost is None or chain_cost <= 0:
        return None
    info = ref.type_info(type_id)
    net = costing.net_proceeds_per_hull(
        price,
        info.freight_volume,
        settings,
        capital=costing.is_capital_priced(ref, type_id),
        freight_exempt=costing.freight_out_exempt(type_id),
    )
    return (net - chain_cost) / chain_cost


def _final_return(ref, settings, snapshot: Snapshot, item: PlanItem):
    """The final's sell reference is the snapshot's sell quote where one
    was supplied (the web route passes ledger.final_quote's figure: the
    hub quote for sub-capitals, the capital structure's SELL quote for
    capital-class hulls, which Jita never quotes), else the row's own
    plan-time price."""
    price = snapshot.sell_quotes.get(item.type_id, item.price_snapshot)
    return final_return(
        ref, settings, item.type_id, price, item.unit_chain_cost
    )


def _packed_draw(draw_of, item: PlanItem, runs: int, per_job: int) -> dict[int, int]:
    """What `runs` runs draw when installed the way a short item IS
    installed (user ruling 2026-09-09): full jobs of `per_job` runs and
    one last job with the remainder — a short last job overrules the
    plan's whole-copy / uniform-job rounding, so nothing installable
    waits for a full batch."""
    if runs <= 0:
        return {}
    full, remainder = divmod(runs, per_job)
    out = dict(draw_of(item, full * per_job, full)) if full else {}
    if remainder:
        for m, qty in draw_of(item, remainder, 1).items():
            out[m] = out.get(m, 0) + qty
    return out


def _uniform_jobs(runs: int, per_job: int) -> tuple[int, int]:
    """(jobs, runs per job) for an INTERMEDIATE installing `runs` runs the
    way Phase 7 sizes it (user ruling 2026-09-09): the jobs its plan's
    runs-per-job count needs, every job the same length, the per-job
    count rounded UP — so jobs × per-job may exceed `runs` by up to
    jobs − 1 (the overbuild nets off next cycle, like the plan's own).
    The install check judges the draw at THAT total, so where the
    round-up does not fit the stock it settles on the largest uniform
    count that does."""
    if runs <= 0:
        return 0, 0
    jobs = -(-runs // per_job)
    return jobs, -(-runs // jobs)


def install_draw_of(draw_of, item: PlanItem) -> dict[int, int]:
    """The draw of a row's INSTALL figures at the packing they describe:
    the plan's own split when every planned run installs, uniform jobs
    when install_per_job × install_jobs == install_runs, otherwise full
    jobs plus a remainder job. What the check guarantees fits — tests
    and audits recompute it from the persisted row."""
    runs, jobs = item.install_runs or 0, item.install_jobs or 0
    if runs <= 0:
        return {}
    if runs >= item.runs_allocated:
        return draw_of(item, item.runs_allocated, item.jobs_allocated)
    per_job = item.install_per_job or -(
        -item.runs_allocated // max(1, item.jobs_allocated)
    )
    if per_job * jobs == runs:
        return draw_of(item, runs, jobs)
    return _packed_draw(draw_of, item, runs, per_job)


_UNLIMITED = 10**18
# install_limited_by sentinel: the slot pool, not an input, stopped the job
# (the startable jobs of a pool can never exceed it).
_LIMITED_BY_SLOTS = -1


def _type_known(ref, type_id: int) -> bool:
    """False for a type the reference data does not hold (the MILP tests'
    synthetic contenders): the rationing has nothing to say about what
    it consumes, so it neither cuts it nor backfills it."""
    try:
        ref.type_info(type_id)
    except KeyError:
        return False
    return True


@dataclass
class _Rationing:
    """What one rationing pass decided (the install check's core, shared
    with the stock-aware slot backfill and the alchemy pass): per
    consumer the startable runs / jobs / runs-per-job and the binding
    input, per final its priority and return, per consumed material the
    planned draw, the availability and what is LEFT after the startable
    jobs draw. ``left(m)`` answers for materials no planned job consumes
    too (their availability, untouched)."""

    startable_runs: dict = field(default_factory=dict)
    startable_jobs: dict = field(default_factory=dict)
    per_job: dict = field(default_factory=dict)
    limited_by: dict = field(default_factory=dict)
    priority: dict = field(default_factory=dict)
    returns: dict = field(default_factory=dict)
    draw: dict = field(default_factory=dict)
    available: dict = field(default_factory=dict)
    remaining: dict = field(default_factory=dict)
    available_of: object = None

    def left(self, m: int) -> int:
        if m in self.remaining:
            return self.remaining[m]
        return self.available_of(m)


def _allocation_availability(conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot):
    """available_of(type_id) at ALLOCATION time (Phases 6–6.5), before
    the buys are sized: a raw input is bought just-in-time for whatever
    is allocated (unlimited), so is an intermediate the market beats
    outright (Phase 7 buys its whole deficit); any other buildable is
    what is on hand and in flight plus the units the market already
    beat (market_buy_qty). Finals are never bought."""
    finals = {p["final_product_type_id"] for p in store.active_pipelines(conn)}

    def available_of(m: int) -> int:
        row = merged.get(m)
        if row is None:
            return snapshot.on_hand.get(m, 0) + snapshot.in_progress.get(m, 0)
        if not row.buildable:
            return _UNLIMITED
        if (
            m not in finals
            and row.build_savings_per_unit is not None
            and row.build_savings_per_unit <= 0
        ):
            return _UNLIMITED
        available = row.on_hand_qty + row.in_progress_qty + row.market_buy_qty
        # What its jobs (and its alchemy) will not cover, Phase 7 buys
        # where a market exists — a capacity loser's fallback, capped at
        # the rungs the market holds (the same sizing as _finalize) —
        # and those bought units are there for its consumers too.
        if m not in finals and snapshot.price(m) is not None:
            available += _fallback_buy_of(ref, finals, row)
        return available

    return available_of


def _exact_total(ref, finals: set, item: PlanItem) -> bool:
    """Phase 7 never overbuilds a final or an exact-quantity ship: their
    last job runs short instead of the uniform round-up."""
    if item.type_id in finals:
        return True
    try:
        info = ref.type_info(item.type_id)
    except KeyError:
        return False
    return (
        info.category_id == config.CATEGORY_SHIP
        and info.group_id in config.EXACT_QTY_SHIP_GROUPS
    )


def _sized_runs(ref, finals: set, item: PlanItem, jobs: int) -> int:
    """The runs Phase 7 will allocate to `jobs` jobs of the item (its
    sizing rule, mirrored — review 2026-09-10): full windows for a
    saturating reaction; the plan's total capped at the windows for a
    final or an exact-quantity ship; for everything else that figure
    rounded UP to a uniform per-job count across ALL the jobs."""
    if jobs <= 0 or item.max_runs_per_job <= 0:
        return 0
    try:
        saturating = _saturating_reaction(ref, item)
    except KeyError:
        saturating = False
    if saturating:
        return jobs * item.max_runs_per_job
    runs = min(item.total_runs_needed, jobs * item.max_runs_per_job)
    if runs > 0 and not _exact_total(ref, finals, item):
        runs = -(-runs // jobs) * jobs
    return runs


def _fallback_buy_of(ref, finals: set, row: PlanItem, extra_jobs: int = 0) -> int:
    """The units Phase 7 will buy for a priced, non-final buildable's
    uncovered need (its allocated jobs — plus `extra_jobs` more — sized
    as Phase 7 sizes them, and its alchemy output, cover the rest),
    capped at the rungs the market holds."""
    covered = min(
        row.total_runs_needed,
        _sized_runs(ref, finals, row, row.jobs_allocated + extra_jobs),
    ) * row.portion_size
    uncovered = (
        row.total_runs_needed * row.portion_size - covered - row.alchemy_output_qty
    )
    if uncovered <= 0:
        return 0
    if row.market_fallback_qty is None:
        return uncovered
    return min(uncovered, row.market_fallback_qty)


def _final_availability(merged: dict[int, PlanItem], snapshot: Snapshot):
    """available_of(type_id) once every buy is sized (Phase 7.6): on hand
    + in-flight output + this cycle's purchases (compressed-covered share
    included) less the units no stored sell order held."""

    def available_of(m: int) -> int:
        row = merged.get(m)
        if row is None:
            return snapshot.on_hand.get(m, 0) + snapshot.in_progress.get(m, 0)
        return (
            row.on_hand_qty
            + row.in_progress_qty
            + row.recommended_buy_qty
            - row.unfilled_qty
            + row.compressed_covered_qty
        )

    return available_of


def _ration(conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot, available_of) -> _Rationing:
    """The install check's core (Phase 7.6, v1.27.1), also run at
    allocation time: which of the plan's jobs can be installed from what
    is there, and how.

    Every row holding runs — or, before Phase 7 has sized runs, jobs —
    is a CONSUMER; each of its materials is available at
    ``available_of(m)``. A material whose planned draw exceeds that is
    SHORT and the consumers are rationed:

    * Pipeline FINALS first, in order of return on cost (unpriced last,
      finals whose chain cost is understated by unpriced inputs after
      every fully priced one): each in turn takes the most runs its
      remaining inputs can feed (binary search — a job's draw is
      monotonic in its runs), leaving the rest to the next.
    * INTERMEDIATES (alchemy installs included) then share what remains
      in PROPORTION: progressive filling raises one fraction of planned
      runs for every consumer together until a material runs out, freezes
      that material's consumers at that fraction and carries on with the
      others — so the consumers of a scarce material all install the same
      share of their plan, a consumer bound tighter by another material
      leaves its unused share to its siblings, and nothing is cut for a
      material it never uses. The fractions become whole runs (floored),
      the exact per-job rounding is re-checked and a run trimmed from the
      largest drawer wherever it still overshoots, then runs are handed
      back while they fit.

    Packing (user rulings 2026-09-09): an INTERMEDIATE installs uniform
    jobs with the per-job count rounded up when the inputs allow, else
    the largest uniform count that fits (_uniform_jobs); an
    exact-quantity ship or a saturating reaction installs full jobs at
    the plan's runs per job plus ONE last job with the remainder
    (_packed_draw); every feasibility test judges the draw at that
    packing, and the full plan draws as Phase 7 split it."""
    settings = store.get_settings(conn)
    draw_of = _draw_calculator(conn, ref)
    finals = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }
    out = _Rationing(available_of=available_of)

    def saturating(item: PlanItem) -> bool:
        return (
            item.activity_id == config.ACTIVITY_REACTION
            and ref.type_info(item.type_id).group_id
            not in config.NON_SATURATING_REACTION_GROUPS
        )

    # Planned runs / jobs per consumer: Phase 7's figures once it has
    # run; before it (allocation time) the runs its jobs will carry.
    R: dict[int, int] = {}
    J: dict[int, int] = {}
    for item in merged.values():
        if not item.buildable or not _type_known(ref, item.type_id):
            continue
        if item.runs_allocated > 0:
            runs, jobs = item.runs_allocated, item.jobs_allocated
        elif item.jobs_allocated > 0 and item.max_runs_per_job > 0:
            jobs = item.jobs_allocated
            runs = (
                jobs * item.max_runs_per_job
                if saturating(item)
                else min(item.total_runs_needed, jobs * item.max_runs_per_job)
            )
        else:
            continue
        if runs > 0:
            R[item.type_id], J[item.type_id] = runs, max(1, jobs)
    consumers = [merged[t] for t in R]
    if not consumers:
        return out

    def per_job(item: PlanItem) -> int:
        # The plan's own runs per job (Phase 7: intermediates uniform,
        # finals' last job short) — the job count a short item's runs
        # need, and the length of a ship's / reaction's full jobs.
        return -(-R[item.type_id] // J[item.type_id])

    def jobs_for(item: PlanItem, runs: int) -> int:
        if runs <= 0:
            return 0
        if runs >= R[item.type_id]:
            return J[item.type_id]
        return -(-runs // per_job(item))

    def uniform(item: PlanItem) -> bool:
        # Phase 7's own sizing rule: intermediates run uniform jobs with
        # the per-job count rounded up; exact-quantity ships (finals,
        # capitals, freighters, jump freighters) and saturating reactions
        # do not — their last job runs short.
        info = ref.type_info(item.type_id)
        exact_total = item.type_id in finals or (
            info.category_id == config.CATEGORY_SHIP
            and info.group_id in config.EXACT_QTY_SHIP_GROUPS
        )
        return not saturating(item) and not exact_total

    def draw(item: PlanItem, runs: int) -> dict[int, int]:
        # The full plan draws as Phase 7 split it (_planned_consumption's
        # figure). Anything less installs the way Phase 7 sizes that
        # item: an intermediate as uniform jobs with the per-job count
        # rounded up — so `runs` may install as a few more, the
        # feasibility tests judge THAT draw, and where the round-up does
        # not fit they settle on the largest uniform count that does; an
        # exact-quantity ship or a saturating reaction as full jobs plus
        # a short last job.
        full = R[item.type_id]
        if runs >= full:
            return draw_of(item, full, J[item.type_id])
        if uniform(item):
            jobs, each = _uniform_jobs(runs, per_job(item))
            return draw_of(item, min(full, jobs * each), jobs)
        return _packed_draw(draw_of, item, runs, per_job(item))

    def stamp(item: PlanItem, runs: int) -> None:
        # The install figures at the packing draw() judged.
        full = R[item.type_id]
        t = item.type_id
        if runs >= full:
            out.startable_runs[t] = full
            out.startable_jobs[t] = J[t]
            out.per_job[t] = per_job(item)
        elif uniform(item):
            jobs, each = _uniform_jobs(runs, per_job(item))
            out.startable_runs[t] = min(full, jobs * each)
            out.startable_jobs[t] = jobs
            out.per_job[t] = each if jobs else per_job(item)
        else:
            out.startable_runs[t] = runs
            out.startable_jobs[t] = jobs_for(item, runs)
            out.per_job[t] = per_job(item)

    planned = {c.type_id: draw(c, R[c.type_id]) for c in consumers}
    materials = set()
    for d in planned.values():
        materials.update(d)
    for m in materials:
        out.available[m] = available_of(m)
        out.draw[m] = sum(planned[c.type_id].get(m, 0) for c in consumers)
    remaining = dict(out.available)

    # --- finals: highest return first ---------------------------------
    for c in consumers:
        if c.type_id in finals:
            out.returns[c.type_id] = _final_return(ref, settings, snapshot, c)
    # Unpriced finals last; before them, finals whose chain cost is
    # UNDERSTATED by unpriced inputs (raw leaves, datacores, relics or
    # decryptors priced at 0 — the row's 'N unpriced' badge): their
    # return reads too high, so they rank after every fully priced final
    # rather than take scarce stock on a figure the badge disowns
    # (review 2026-09-09).
    ranked_finals = sorted(
        (c for c in consumers if c.type_id in finals),
        key=lambda c: (
            out.returns[c.type_id] is None,
            c.savings_unpriced_inputs > 0,
            -(out.returns[c.type_id] or 0.0),
            c.depth,
            c.name,
        ),
    )
    for rank, item in enumerate(ranked_finals, 1):
        out.priority[item.type_id] = rank

    def fits(item: PlanItem, runs: int) -> bool:
        return all(
            qty <= remaining.get(m, 0) for m, qty in draw(item, runs).items()
        )

    def binding(item: PlanItem, runs: int) -> int | None:
        # The material that stops the next run — the one most over.
        over = [
            (qty - remaining.get(m, 0), m)
            for m, qty in draw(item, runs + 1).items()
            if qty > remaining.get(m, 0)
        ]
        return max(over)[1] if over else None

    for item in ranked_finals:
        full = R[item.type_id]
        if fits(item, full):
            runs = full
        else:
            lo, hi = 0, full - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if fits(item, mid):
                    lo = mid
                else:
                    hi = mid - 1
            runs = lo
        stamp(item, runs)
        out.limited_by[item.type_id] = None if runs == full else binding(item, runs)
        for m, qty in draw(item, runs).items():
            remaining[m] -= qty

    # --- intermediates: the same share of their plan, per material ----
    inter = [c for c in consumers if c.type_id not in finals]
    frac: dict[int, float] = {}
    limited_by: dict[int, int | None] = {}
    active = {c.type_id: c for c in inter}
    while active:
        # Per material: what the frozen consumers already took, and the
        # planned draw of the still-active ones. The material with the
        # least room per unit of active draw binds first.
        best_f, best_m = 1.0, None
        for m in materials:
            weight = sum(planned[t].get(m, 0) for t in active)
            if weight <= 0:
                continue
            taken = sum(
                planned[t].get(m, 0) * f
                for t, f in frac.items()
            )
            room = remaining.get(m, 0) - taken
            f = max(0.0, room / weight)
            if f < best_f:
                best_f, best_m = f, m
        if best_m is None:
            for t in list(active):
                frac[t], limited_by[t] = 1.0, None
            break
        for t in [t for t in active if planned[t].get(best_m, 0) > 0]:
            frac[t], limited_by[t] = best_f, best_m
            del active[t]
    runs_of = {
        c.type_id: min(R[c.type_id], _floor(frac[c.type_id] * R[c.type_id]))
        for c in inter
    }
    # Whole runs and per-job rounding: trim until every material fits.
    while True:
        exact: dict[int, int] = {}
        for c in inter:
            for m, qty in draw(c, runs_of[c.type_id]).items():
                exact[m] = exact.get(m, 0) + qty
        over = [
            (exact[m] - remaining.get(m, 0), m)
            for m in exact
            if exact[m] > remaining.get(m, 0)
        ]
        if not over:
            break
        _excess, m = max(over)
        drawers = [
            c for c in inter
            if runs_of[c.type_id] > 0 and draw(c, runs_of[c.type_id]).get(m, 0) > 0
        ]
        if not drawers:
            break  # cannot happen: a positive draw needs positive runs
        victim = max(
            drawers, key=lambda c: draw(c, runs_of[c.type_id]).get(m, 0)
        )
        runs_of[victim.type_id] -= 1
        limited_by[victim.type_id] = m
    # Flooring left slack: hand whole runs back, the most-cut consumer
    # first, while they fit — so the figure is maximal, not just safe.
    while True:
        added = False
        for c in sorted(
            inter,
            key=lambda c: (runs_of[c.type_id] / R[c.type_id], c.name),
        ):
            runs = runs_of[c.type_id]
            if runs >= R[c.type_id]:
                continue
            before = draw(c, runs)
            after = draw(c, runs + 1)
            if all(
                exact.get(m, 0) - before.get(m, 0) + qty <= remaining.get(m, 0)
                for m, qty in after.items()
            ):
                for m, qty in after.items():
                    exact[m] = exact.get(m, 0) - before.get(m, 0) + qty
                runs_of[c.type_id] = runs + 1
                added = True
        if not added:
            break
    for c in inter:
        runs = runs_of[c.type_id]
        stamp(c, runs)
        out.limited_by[c.type_id] = (
            None if runs >= R[c.type_id] else limited_by.get(c.type_id)
        )
        for m, qty in draw(c, runs).items():
            remaining[m] -= qty
    out.remaining = remaining
    return out


def _install_check(conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot):
    """Phase 7.6 (v1.27.1): verify the plan's jobs against what is
    actually there to feed them once every buy is sized, and stamp what
    to install now — _ration over _final_availability: on hand +
    in-flight output + this cycle's purchases (compressed-covered share
    included) less the units no stored sell order held. This cycle's own
    build output is never available (a stage's jobs feed NEXT cycle's
    consumers — that one-cycle lag is the pipeline) nor is the alchemy
    route's composite (it needs a reprocess after the job).

    install_runs / install_jobs / install_per_job, install_limited_by
    (the binding material; None when every planned run installs),
    install_priority / install_return on finals, and install_draw_qty /
    install_short_qty on consumed rows are the result. The run page's
    job tables, section stats and slot stats show THESE as the jobs to
    run; the plan's own sizing and buys stand underneath (tooltips and
    the Chain tab). Since the stock-aware backfill (Phase 6), the plan's
    job count may exceed a pool — the startable jobs never do."""
    for item in merged.values():
        item.install_runs = None
        item.install_jobs = None
        item.install_per_job = None
        item.install_limited_by = None
        item.install_priority = None
        item.install_return = None
        item.install_draw_qty = None
        item.install_short_qty = None
    r = _ration(conn, ref, merged, snapshot, _final_availability(merged, snapshot))
    for t, runs in r.startable_runs.items():
        item = merged[t]
        item.install_runs = runs
        item.install_jobs = r.startable_jobs[t]
        item.install_per_job = r.per_job[t]
        item.install_limited_by = r.limited_by.get(t)
    # The pool is a hard limit on what can START, whatever the plan
    # lists: the stock-aware backfill (Phase 6) hands slots of jobs
    # that could not start at allocation time to others, and a job can
    # become startable later (a loser's fallback buy sized in Phase 7
    # differs a little from the allocation-time estimate). Where a
    # pool's startable jobs exceed it, trim backfilled jobs first, then
    # the lowest-savings intermediates, finals last — one job at a
    # time, the last (shortest) job of the item first — and name the
    # pool as what stopped them, unless an input already did.
    for activity_id in (config.ACTIVITY_MANUFACTURING, config.ACTIVITY_REACTION):
        pool = snapshot.slots_available.get(activity_id, 0)
        rows = [
            merged[t]
            for t in r.startable_runs
            if merged[t].activity_id == activity_id
        ]
        over = sum(i.install_jobs or 0 for i in rows) - pool
        if over <= 0:
            continue
        finals = {
            p["final_product_type_id"] for p in store.active_pipelines(conn)
        }
        order = sorted(
            rows,
            key=lambda i: (
                2 if i.type_id in finals else 0 if i.backfilled_jobs else 1,
                (i.build_savings_per_unit or 0.0) * i.portion_size * i.max_runs_per_job,
                i.name,
            ),
        )
        for item in order:
            while over > 0 and (item.install_jobs or 0) > 0:
                last = item.install_runs - (item.install_jobs - 1) * item.install_per_job
                item.install_runs -= max(1, last)
                item.install_jobs -= 1
                # Only claim the pool where nothing else already bound the
                # row: overwriting a short input's type id lost the one
                # thing the user could act on (buy it), and blamed the
                # pool for a row stock had already cut (review
                # 2026-09-10). A row cut by both keeps the input.
                if item.install_limited_by is None:
                    item.install_limited_by = _LIMITED_BY_SLOTS
                over -= 1
            if over <= 0:
                break
    for t, rank in r.priority.items():
        merged[t].install_priority = rank
        merged[t].install_return = r.returns.get(t)
    for m, total in r.draw.items():
        row = merged.get(m)
        if row is None:
            continue
        row.install_draw_qty = total
        row.install_short_qty = max(0, total - r.available[m])


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
                buyable = shortfall_qty
                if not market_preferred and item.market_fallback_qty is not None:
                    # v1.26: a capacity shortfall can only be bought
                    # from rungs the market actually holds (the ones
                    # dearer than building); beyond them it is unmet.
                    buyable = min(shortfall_qty, item.market_fallback_qty)
                if buyable > 0:
                    item.recommended_buy_qty = buyable
        if item.market_buy_qty > 0:
            # v1.26: the units the market beat the build cost on, less
            # whatever the alchemy route now supplies for them (user
            # ruling 2026-09-11). `alchemy_buy_qty` counts only the
            # output aimed at bought units, so the build shortfall above
            # never credits the same output twice.
            item.recommended_buy_qty += max(
                0, item.market_buy_qty - item.alchemy_buy_qty
            )
        if item.recommended_buy_qty > 0:
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
        # _ceil, not math.ceil: 100 × 1.05 is 105.00000000000001 in
        # binary, and a raw ceil bought one unit too many (review
        # 2026-09-05, the finding A5 guard applied here too).
        item.target_stock_qty = _ceil(
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
        # Review 2026-09-05 (finding A3 residual): the loop prorates a
        # stage's target by its ACTIVE consumers, so a stage nobody builds
        # for this cycle (target 0) must not read as low stock; a partly
        # drawn stage is judged against its prorated target, never above
        # one cycle's need.
        if projected < min(item.cycle_need_qty, item.target_stock_qty):
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


def _sourcing_pass(
    conn, ref, merged: dict[int, PlanItem], snapshot: Snapshot
) -> float | None:
    """Phase 7.5 (v1.25): price every purchase by walking its sell ladders
    and, for the raw minerals / moon materials / gas, consider COMPRESSED
    ore / moon ore / gas instead (reprocessed at the asserted yields, less
    the reprocessing tax).

    Fill pricing (2026-09-05, superseding the 2026-08-22 "best price +
    flag, never a fill price" and "no order splitting" rules): each bought
    item's direct quantity is filled from the cheapest LANDED rungs of the
    Jita and structure ladders together (costing.fill_merged — the greedy
    walk is the per-item optimum), so a buy may split across venues. Units
    beyond every stored rung are priced at the last rung walked; they are
    attributed to the hub ONLY when its stored ladder was truncated at
    config.HUB_LADDER_MAX_RUNGS (the book continues past what was stored),
    otherwise they are UNSOURCED — no venue, no Multibuy line, counted in
    unfilled_qty for the 'N unsourced' badge (ruling R5 / contract C4,
    2026-09-05). An item whose only ladder is the structure's still
    competes against the cached Jita quote, standing in as one unbounded
    synthetic hub rung (finding A2); an item with no ladder and no hub
    quote keeps its single quote untouched, and a ladder with no
    positive-volume rung counts as absent (finding A11). With
    price_source != 'sell' no sell ladder was ever pulled for the buy
    side: the pass leaves every Phase 1 quote alone (contract C6 — market
    returns empty ladders; guarded here too).

    Compressed candidates enter one LP (HiGHS) together with the raws'
    own rungs: a column per rung (bounded by its remaining volume, costed
    landed; ore and moon ore rungs carry the reprocessing tax on every
    output's value, gas rungs none — `tax_of`) and a
    remainder column at the raw's marginal landed price. Surplus outputs
    are worth nothing to the LP (only demanded raws have rows), so it can
    never buy ore for minerals nobody needs. Chosen compressed types round
    UP to whole reprocessing batches, are re-filled on their venue's
    ladder, and survive only while their landed cost stays strictly below
    the landed direct cost of the units they displace — the MOST expensive
    direct units, valued as direct_cost(D) − direct_cost(D − q) on the
    merged walk (a tie goes to the raw). Covered raws keep their demand
    figures, drop their direct buy to the remainder, and carry a blended
    landed `effective_unit_cost` the realized costing prices them at.
    Hangar compressed stock is ignored. Returns the landed ISK the
    compressed buys saved, None when none stood."""
    settings = store.get_settings(conn)
    # v1.26: per-venue ladders through the shared lookup — a 'min_sell'
    # / 'max_buy' venue is one unbounded synthetic rung at its quote, so
    # the walk below prices it at the best order for any quantity (the
    # 2026-09-05 C6 guard is subsumed: a buy-side hub has no ladder,
    # only its quote, and a structure ladder can never undercut it
    # unless it really is cheaper landed).
    ladders = _LadderLookup(snapshot, settings, ref)
    rates = ladders.rates
    finals = {p["final_product_type_id"] for p in store.active_pipelines(conn)}

    ladders_of = ladders.ladders_of
    has_ladder = ladders.has_ladder
    direct_fill = ladders.direct_fill

    bought = {
        item.type_id: item.recommended_buy_qty
        for item in merged.values()
        if item.recommended_buy_qty > 0
        and item.alchemy_for_type_id is None
        and item.type_id not in finals
        and not item.compressed_outputs
    }

    # --- the compressed candidates (three raw groups) -------------------
    yields = {
        "ore": settings.compressed_ore_yield,
        "gas": settings.compressed_gas_yield,
    }
    tax = max(0.0, settings.compressed_reprocess_tax)

    def tax_of(source) -> float:
        # Refining ore and moon ore pays the facility's reprocessing
        # tax; decompressing gas is untaxed in the client (user,
        # 2026-09-06), so a gas candidate carries no tax term.
        return tax if source.kind == "ore" else 0.0
    demand: dict[int, int] = {}
    landed_raw: dict[int, float] = {}  # single-quote landed price
    # The toggles pick which compressed TYPES are candidates (those
    # yielding a raw of an enabled group); every demanded raw of the three
    # groups still counts and gets covered by whatever the candidates
    # yield (user ruling 2026-09-05: moon ore on / minerals off still
    # covers the Pyerite a moon ore yields).
    groups = settings.compressed_groups()
    if groups and any(y > 0 for y in yields.values()):
        for item in merged.values():
            if (
                item.buildable
                or item.type_id not in bought
                or ref.type_info(item.type_id).group_id
                not in config.COMPRESSED_SOURCE_GROUPS
            ):
                continue
            price = _landed_price(ref, settings, snapshot, item.type_id)
            if price is None:
                continue
            demand[item.type_id] = item.recommended_buy_qty
            landed_raw[item.type_id] = price

    def direct_cost(type_id: int, qty: int) -> float:
        """Landed cost of buying `qty` units of a raw direct: the merged
        walk when it has a ladder, its single quote otherwise."""
        if qty <= 0:
            return 0.0
        if has_ladder(type_id):
            return direct_fill(type_id, qty).landed_cost
        return qty * landed_raw[type_id]

    def displaced_value(type_id: int, qty: int, already: int = 0) -> float:
        """What covering `qty` more units of a raw saves: the cost of its
        most expensive direct units, `already` units being covered by
        other compressed buys."""
        total = demand[type_id]
        return direct_cost(type_id, total - already) - direct_cost(
            type_id, total - already - qty
        )

    def marginal_landed(type_id: int) -> float:
        """The raw's remainder price: its deepest rung landed (≥ every
        rung), or the single quote when it has no ladder."""
        hub, structure = ladders_of(type_id)
        rungs = costing.merged_rungs(
            hub, structure, rates[store.BUY_VENUE_HUB],
            rates[store.BUY_VENUE_STRUCTURE], ref.type_info(type_id).freight_volume,
        )
        return rungs[-1][0] if rungs else landed_raw[type_id]

    def output_price(m: int) -> float:
        # Demanded raws at their landed quote; any other output at its
        # cached landed price, 0 when unpriced (the tax basis).
        if m in landed_raw:
            return landed_raw[m]
        return _landed_price(ref, settings, snapshot, m) or 0.0

    sources = {
        c: s
        for c, s in ref.compressed_sources_for(demand, groups).items()
        if yields.get(s.kind, 0.0) > 0 and c not in merged
    } if demand else {}

    chosen: dict[int, dict] = {}
    covered: dict[int, int] = {r: 0 for r in demand}
    used: dict[int, dict[int, int]] = {}
    if sources:
        raw_ids = list(demand)
        raw_index = {r: i for i, r in enumerate(raw_ids)}
        # Each raw's own direct columns: its merged rungs (capped at the
        # demand) plus one remainder column at its marginal landed price.
        direct_cols: list[tuple[int, float, float | None]] = []  # raw, cost, bound
        for r in raw_ids:
            hub, structure = ladders_of(r)
            taken = 0
            for landed, _price, volume, _venue in costing.merged_rungs(
                hub, structure, rates[store.BUY_VENUE_HUB],
                rates[store.BUY_VENUE_STRUCTURE], ref.type_info(r).freight_volume,
            ):
                if taken >= demand[r]:
                    break
                volume = min(volume, demand[r] - taken)
                direct_cols.append((r, landed, float(volume)))
                taken += volume
            direct_cols.append((r, marginal_landed(r), None))
        coeff: dict[int, dict[int, float]] = {}
        value_bounds: dict[int, float] = {}
        per_unit_tax: dict[int, float] = {}
        caps: dict[int, int] = {}
        for c, source in sources.items():
            y = yields[source.kind]
            coeff[c] = {
                r: source.per_unit(r, y) for r in raw_ids if source.per_unit(r, y) > 0
            }
            if not coeff[c]:
                continue
            # The most a unit of this type can be worth: every useful
            # output at its raw's marginal (remainder) landed price.
            value_bounds[c] = sum(a * marginal_landed(r) for r, a in coeff[c].items())
            per_unit_tax[c] = tax_of(source) * sum(
                source.per_unit(m, y) * output_price(m) for m, _q in source.outputs
            )
            caps[c] = max(_ceil(demand[r] / a) for r, a in coeff[c].items())

        # v1.26.1: "one market per compressed row" (ruling 2026-09-05) is
        # part of the solve now. A chosen type the LP spread over both
        # markets, or asked more of than its market holds in whole
        # batches, is PINNED to its heavier market with that market's
        # whole-batch depth as its cap, and the LP runs again so another
        # ore (or the direct raw) covers what the pin gave up — before, the
        # shortfall fell straight to the raw's direct buy and the row was
        # badged "cut short". A pin never loosens (the venue is fixed once,
        # the cap only falls), so the loop settles in at most a pass per
        # chosen type; config.COMPRESSED_LP_PASSES is the safety cap, after
        # which the last solution stands and any residue goes direct.
        pins: dict[int, tuple[str, int]] = {}  # c -> (venue, whole-batch units)

        def build_rungs():
            rungs: list[tuple[int, str, float, int, float]] = []  # c, venue, price, vol, landed
            for c in coeff:
                if not coeff[c]:
                    continue
                m3 = ref.type_info(c).freight_volume
                pin = pins.get(c)
                cap = caps[c] if pin is None else min(caps[c], pin[1])
                if cap <= 0:
                    continue
                for venue, rate in rates.items():
                    if pin is not None and venue != pin[0]:
                        continue
                    ladder = ladders.venue_ladder(venue, c)
                    if not ladder:
                        continue
                    taken = 0
                    # Rows are (price, volume) or (price, volume, min_volume)
                    # since the min_volume column (contract C3, 2026-09-05).
                    for price, volume, *rest in sorted(ladder, key=lambda o: o[0]):
                        landed = price + rate * m3 + per_unit_tax[c]
                        if landed >= value_bounds[c] or taken >= cap:
                            break  # ascending: nothing further can beat direct
                        volume = min(int(volume), cap - taken)
                        if volume <= 0:
                            continue
                        if rest and int(rest[0] or 1) > volume:
                            # The order's minimum fill exceeds what this
                            # candidate could ever take from it: not buyable
                            # here (the re-fill below enforces the same rule
                            # through costing.fill_ladder).
                            continue
                        rungs.append((c, venue, price, volume, landed))
                        taken += volume
            return rungs

        def solve(rungs):
            # min Σ cost·x  s.t.  Σ direct_r + Σ_k a_{c(k),r} x_k ≥ D_r.
            n_direct, n_rung = len(direct_cols), len(rungs)
            cost = np.array(
                [col[1] for col in direct_cols] + [rung[4] for rung in rungs]
            )
            rows_i, cols_i, vals = [], [], []
            for j, (r, _cost, _bound) in enumerate(direct_cols):
                rows_i.append(raw_index[r]); cols_i.append(j); vals.append(-1.0)
            for k, (c, _venue, _price, _vol, _landed) in enumerate(rungs):
                for r, a in coeff[c].items():
                    rows_i.append(raw_index[r]); cols_i.append(n_direct + k); vals.append(-a)
            a_ub = coo_matrix(
                (vals, (rows_i, cols_i)), shape=(len(raw_ids), n_direct + n_rung)
            ).tocsr()
            b_ub = -np.array([float(demand[r]) for r in raw_ids])
            bounds = [(0, col[2]) for col in direct_cols] + [
                (0, float(rung[3])) for rung in rungs
            ]
            result = linprog(
                cost, A_ub=a_ub, b_ub=b_ub, bounds=bounds, method="highs"
            )
            if not result.success:
                # Review 2026-09-05 (finding A10): the compressed
                # substitution is an optimisation on top of a plan that
                # is already complete — a solver breakdown must not abort
                # the run. Log it, choose nothing, and let every raw
                # fill-price direct below (unlike the slot MILP, whose
                # failure would silently flip the whole pool to buys).
                log.warning(
                    "compressed sourcing LP failed (%s): planning the run "
                    "with direct purchases only", result.message,
                )
                return None
            return result.x[n_direct:]

        def whole_batches(ladder, portion: int, units: int) -> int:
            """Units a fill of `units` can take off `ladder`, floored to
            whole batches (the min_volume rule applies, as in the re-fill)."""
            return (costing.fill_ladder(ladder, units).units // portion) * portion

        takes: dict[int, dict[str, float]] = {}
        rungs = build_rungs()
        # One pin per candidate type is the settling bound when every
        # market fills what it holds; a min-volume order can lower a
        # pinned cap more than once, so the loop runs to the larger of
        # config.COMPRESSED_LP_PASSES and one pass per candidate plus one.
        max_passes = max(config.COMPRESSED_LP_PASSES, len(coeff) + 1)
        for _pass in range(max_passes):
            takes = {}
            if not rungs:
                break
            x = solve(rungs)
            if x is None:
                break
            for k, (c, venue, _price, _vol, _landed) in enumerate(rungs):
                if x[k] > 1e-9:
                    takes.setdefault(c, {})
                    takes[c][venue] = takes[c].get(venue, 0.0) + float(x[k])
            changed = False
            for c, by_venue in takes.items():
                total = sum(by_venue.values())
                if total < 0.5:
                    continue
                portion = sources[c].portion_size
                venue = max(by_venue, key=by_venue.get)
                ladder = ladders.venue_ladder(venue, c)
                wanted = _ceil(total / portion) * portion
                fillable = whole_batches(ladder, portion, wanted)
                if len(by_venue) > 1 or fillable < wanted:
                    # The cap is what the market fills AT the wanted
                    # quantity, not its whole-ladder depth: an order whose
                    # minimum volume exceeds a fill's remainder is
                    # unfillable there (contract C3), so a deeper book is
                    # no promise the wanted quantity can be taken (refute
                    # lane, 2026-09-07). A type the market fills in full
                    # keeps the whole depth so the re-solve may grow it.
                    cap = (
                        whole_batches(ladder, portion, 10**15)
                        if fillable >= wanted else fillable
                    )
                    pin = (venue, min(cap, pins[c][1]) if c in pins else cap)
                    if pins.get(c) != pin:
                        pins[c] = pin
                        changed = True
            if not changed:
                break
            rungs = build_rungs()
        else:
            log.debug(
                "compressed sourcing: %d passes without settling — keeping "
                "the last solution", max_passes,
            )

        if takes:
            # What the LP's continuous takes cover of each raw, for the
            # partly-wanted-batch test below (other types' fractions are
            # themselves rounded later — an approximation, like the LP).
            covered_lp = {
                r: sum(
                    coeff[c2][r] * sum(by.values())
                    for c2, by in takes.items() if r in coeff[c2]
                )
                for r in raw_ids
            }
            # Round each chosen type up to whole batches on its (now single)
            # market and re-fill it there for the real cost.
            for c, by_venue in takes.items():
                total = sum(by_venue.values())
                if total < 0.5:
                    continue
                source = sources[c]
                venue = max(by_venue, key=by_venue.get)
                ladder = ladders.venue_ladder(venue, c)
                portion = source.portion_size
                m3 = ref.type_info(c).freight_volume

                def settle(batches: int):
                    """Re-fill `batches` whole batches, shrinking until
                    the market fills the whole quantity — a smaller fill
                    can run short again on a min-volume order (refute
                    lane, 2026-09-07). (0, None) when nothing fills."""
                    while batches > 0:
                        fill = costing.fill_ladder(ladder, batches * portion)
                        if fill.units >= batches * portion:
                            return batches, fill
                        batches = fill.units // portion
                    return 0, None

                batches = _ceil(total / portion)
                # What the LP wanted in whole batches, persisted as
                # compressed_wanted_qty. Since the re-solve (v1.26.1) the
                # pins keep it within what the market fills, so it equals
                # the buy unless the pass cap was hit; rows planned before
                # may exceed the buy (the compressed tooltip states it).
                wanted_qty = batches * portion
                batches, fill = settle(batches)
                if batches <= 0:
                    continue
                if batches * portion > total + 1e-9:
                    # The last batch is only partly wanted: the LP asked for
                    # `frac` units of it and the rest is surplus, worth
                    # nothing (decision 2026-09-05). Buy that batch only
                    # when it costs less than the direct units the wanted
                    # part displaces — the dearest units of each raw's
                    # remaining direct buy, priced by the merged fill
                    # (the survivor loop's displaced_value, at the LP's
                    # coverage). Before (v1.25–v1.26.0) every chosen type
                    # rounded up unconditionally; the re-solve made it
                    # visible: a 0.22-unit gap another ore was asked to
                    # cover bought a 300,000 ISK batch to displace 780 ISK
                    # of direct Tritanium (refute lane, 2026-09-07).
                    frac = total - (batches - 1) * portion
                    value = 0.0
                    for r, a in coeff[c].items():
                        need = _ceil(demand[r] - (covered_lp[r] - a * frac))
                        if need <= 0:
                            continue  # over-covered without this fraction
                        delta = min(_ceil(a * frac), need)
                        value += direct_cost(r, need) - direct_cost(r, need - delta)
                    prev_cost = (
                        costing.fill_ladder(ladder, (batches - 1) * portion).cost
                        if batches > 1 else 0.0
                    )
                    batch_cost = (fill.cost - prev_cost) + portion * (
                        rates[venue] * m3 + per_unit_tax[c]
                    )
                    log.debug(
                        "compressed sourcing: %s last batch %s — %.2f of %d "
                        "units wanted, batch %.0f ISK vs %.0f ISK displaced",
                        ref.type_info(c).name,
                        "dropped" if batch_cost > value else "kept",
                        frac, portion, batch_cost, value,
                    )
                    if batch_cost > value:
                        batches, fill = settle(batches - 1)
                        if batches <= 0:
                            continue
                        # A deliberate round-down: the plan wants exactly
                        # this many, so the record says so.
                        wanted_qty = batches * portion
                qty = batches * portion
                y = yields[source.kind]
                chosen[c] = {
                    "source": source,
                    "venue": venue,
                    "qty": qty,
                    "wanted_qty": wanted_qty,
                    "fill": fill,
                    "landed_cost": fill.cost
                    + rates[venue] * m3 * qty
                    + tax_of(source)
                    * sum(
                        source.batch_output(batches, m, y) * output_price(m)
                        for m, _q in source.outputs
                    ),
                    "outputs": {
                        m: source.batch_output(batches, m, y)
                        for m, _q in source.outputs
                    },
                    # The true ladder depth (rows may carry a third
                    # min_volume element since contract C3).
                    # None for a synthetic rung (v1.26 'min_sell' /
                    # 'max_buy' basis: the book's depth is not known).
                    "ladder_units": (
                        None if ladders.synthetic(venue, c)
                        else sum(int(o[1]) for o in ladder)
                    ),
                }

            # Allocate coverage cheapest-first; drop the worst offender of
            # the strict-cheaper rule and redo until every survivor earns
            # its place. A candidate's value is what its units displace on
            # top of every other survivor's coverage (each judged last in).
            def allocate() -> tuple[dict, dict, dict]:
                cov = {r: 0 for r in raw_ids}
                use: dict[int, dict[int, int]] = {}
                order = sorted(
                    chosen,
                    key=lambda c: chosen[c]["landed_cost"]
                    / max(
                        1e-9,
                        sum(
                            out * marginal_landed(m)
                            for m, out in chosen[c]["outputs"].items()
                            if m in demand
                        ),
                    ),
                )
                for c in order:
                    use[c] = {}
                    for m, out in chosen[c]["outputs"].items():
                        if m not in demand:
                            continue
                        take = min(out, demand[m] - cov[m])
                        if take > 0:
                            use[c][m] = take
                            cov[m] += take
                value = {
                    c: sum(
                        displaced_value(m, q, cov[m] - q) for m, q in use[c].items()
                    )
                    for c in chosen
                }
                return cov, use, value

            while chosen:
                covered, used, value = allocate()
                worst, worst_ratio = None, 1.0
                for c in chosen:
                    ratio = (
                        chosen[c]["landed_cost"] / value[c]
                        if value[c] > 0 else float("inf")
                    )
                    if ratio >= worst_ratio:
                        worst, worst_ratio = c, ratio
                if worst is None:
                    break
                del chosen[worst]
            if not chosen:
                covered = {r: 0 for r in raw_ids}
                used = {}

    # --- apply the compressed coverage ------------------------------------
    saving = None
    if chosen:
        saving = 0.0
        alloc: dict[int, float] = {}
        for c, pick in chosen.items():
            # Each compressed buy's landed cost is split across the demand
            # it covers pro rata by displaced value; leftovers carry none.
            per_m = {
                m: displaced_value(m, q, covered[m] - q) for m, q in used[c].items()
            }
            total_value = sum(per_m.values())
            for m, v in per_m.items():
                alloc[m] = alloc.get(m, 0.0) + pick["landed_cost"] * (
                    v / total_value if total_value > 0 else 0.0
                )
            saving -= pick["landed_cost"]
        for r in demand:
            item = merged[r]
            if covered[r] <= 0:
                continue
            direct = demand[r] - covered[r]
            saving += direct_cost(r, demand[r]) - direct_cost(r, direct)
            item.effective_unit_cost = (
                direct_cost(r, direct) + alloc.get(r, 0.0)
            ) / demand[r]
            item.compressed_covered_qty = covered[r]
            item.recommended_buy_qty = direct
            item.recommended_action = "buy" if direct > 0 else None
            bought[r] = direct
        for c, pick in chosen.items():
            source = pick["source"]
            merged[c] = PlanItem(
                type_id=c,
                name=ref.type_info(c).name,
                item_class=industry.classify_item(ref, c, None),
                depth=max((merged[m].depth for m in used[c]), default=0),
                merged_min_qty=0,
                target_stock_qty=pick["qty"],
                deficit_qty=pick["qty"],
                recommended_action="buy",
                recommended_buy_qty=pick["qty"],
                price_snapshot=pick["fill"].average,
                buy_venue=pick["venue"],
                compressed_outputs=tuple(
                    (m, out, used[c].get(m, 0))
                    for m, out in pick["outputs"].items()
                ),
                compressed_ladder_units=pick["ladder_units"],
                compressed_wanted_qty=pick["wanted_qty"],
                compressed_fill_orders=(
                    None if ladders.synthetic(pick["venue"], c)
                    else pick["fill"].orders
                ),
            )

    # --- fill-price every direct buy ---------------------------------------
    for type_id, qty in bought.items():
        if qty <= 0 or not has_ladder(type_id):
            continue
        item = merged[type_id]
        fill = direct_fill(type_id, qty)
        hub_qty, hub_cost = fill.hub_units, fill.hub_cost
        structure_qty, structure_cost = fill.structure_units, fill.structure_cost
        unfilled = fill.unfilled
        if unfilled and fill.remainder_venue is not None:
            # Contract C4 / ruling R5 (2026-09-05): units beyond the stored
            # ladders fold into a venue ONLY when the fill names one (a
            # Jita ladder truncated at the pull cap — the real book goes
            # on); they stay at the last rung walked. Otherwise they are
            # unsourced: no market held them at plan time, so they stay
            # in the row's quantity and price blend but in no venue's
            # Multibuy line, and unfilled_qty badges the row 'N unsourced'.
            extra = unfilled * (fill.marginal_price or 0.0)
            if fill.remainder_venue == store.BUY_VENUE_STRUCTURE:
                structure_qty += unfilled
                structure_cost += extra
            else:
                hub_qty += unfilled
                hub_cost += extra
            unfilled = 0
        item.hub_buy_qty = hub_qty
        item.hub_fill_price = hub_cost / hub_qty if hub_qty else None
        # v1.26: a synthetic rung is the market's best order for any
        # quantity, not a count of orders walked.
        item.hub_fill_orders = (
            None if ladders.synthetic(store.BUY_VENUE_HUB, type_id)
            else fill.hub_orders
        )
        item.structure_buy_qty = structure_qty
        item.structure_fill_price = (
            structure_cost / structure_qty if structure_qty else None
        )
        item.structure_fill_orders = (
            None if ladders.synthetic(store.BUY_VENUE_STRUCTURE, type_id)
            else fill.structure_orders
        )
        item.unfilled_qty = unfilled
        item.unfilled_price = fill.marginal_price if unfilled else None
        if fill.raw_average is not None:
            # Never null a quote the fill could not improve on (finding
            # A11: nothing walkable on any ladder keeps the Phase 1 price).
            item.price_snapshot = fill.raw_average
        # The venue follows the FILLED units alone; an item nothing filled
        # (every unit unsourced) keeps its Phase 1 venue.
        if hub_qty and structure_qty:
            venue = store.BUY_VENUE_SPLIT
        elif structure_qty:
            venue = store.BUY_VENUE_STRUCTURE
        elif hub_qty:
            venue = store.BUY_VENUE_HUB
        else:
            venue = item.buy_venue
        if venue in (store.BUY_VENUE_STRUCTURE, store.BUY_VENUE_SPLIT):
            # Finding A16: the region-wide provenance belonged to the hub
            # quote the fill just moved off (in whole or in part).
            item.price_region_wide = False
        item.buy_venue = venue
        # The Phase 1 depth figure described the single-quote choice; the
        # fill supersedes it.
        item.structure_units_cheaper = None
    return saving


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
    # whose cheap ladder is shorter than the buy shows the rest as a Jita
    # share (v1.26.1).
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
                # The structure's ladder holds fewer units beating the Jita
                # price than the tab buys there: the venue cell shows the
                # rest as Jita and Multibuy splits the same way (v1.26.1,
                # mirroring web._buy_context.venue_split; a 'shallow' badge
                # before).
                "thin_book": (
                    units_cheaper is not None and 0 < units_cheaper < to_buy
                ),
            }
        )
    multibuy: dict[str, list[str]] = {}
    for buy in buys:
        if buy["to_buy"] <= 0:
            continue
        if buy["thin_book"]:
            cheaper = int(buy["structure_units_cheaper"])
            multibuy.setdefault(store.BUY_VENUE_STRUCTURE, []).append(
                f"{buy['name']} {cheaper}"
            )
            multibuy.setdefault(store.BUY_VENUE_HUB, []).append(
                f"{buy['name']} {buy['to_buy'] - cheaper}"
            )
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


def _add_compressed_ids(conn, ref, ids: set[int]) -> set[int]:
    """With compressed sourcing enabled (v1.25), every compressed ore /
    moon ore / gas that yields a demanded mineral, moon material or gas
    joins the price set (the candidates the pass may buy). Returns the
    candidates added."""
    groups = store.get_settings(conn).compressed_groups()
    if not groups:
        return set()
    # Candidates: types yielding a demanded raw of an ENABLED group.
    candidates = set(ref.compressed_sources_for(ids, groups))
    ids |= candidates
    return candidates


def compressed_candidate_ids(conn, ref) -> set[int]:
    """The compressed sourcing candidates for the active pipelines'
    demand — the types whose hub sell LADDER the price refresh persists
    and the /run path reads (empty with the toggle off)."""
    ids = set(_expand_and_merge(conn, ref))
    _add_alchemy_ids(conn, ref, ids)
    return _add_compressed_ids(conn, ref, ids)


def market_type_ids(conn, ref) -> set[int]:
    """What the per-type ORDER-BOOK refresh must pull: the active
    pipelines' demand (alchemy included) plus the invention market
    inputs and the compressed sourcing candidates — never the
    adjusted-only EIV bases."""
    ids = set(_expand_and_merge(conn, ref))
    ids |= _invention_price_ids(conn, ref)[0]
    _add_alchemy_ids(conn, ref, ids)
    _add_compressed_ids(conn, ref, ids)
    return ids


def demand_type_ids(conn, ref) -> set[int]:
    """Every type planning may price — market_type_ids plus the
    adjusted-only invention EIV bases: the cache-READ list for /run,
    Planning and the price refresh's adjusted-price store."""
    market, adjusted = _invention_price_ids(conn, ref)
    ids = set(_expand_and_merge(conn, ref)) | market
    _add_alchemy_ids(conn, ref, ids)
    _add_compressed_ids(conn, ref, ids)
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
    buy_venue=None, structure_units_cheaper=None, sell_ladders=None,
    hub_prices=None, structure_prices=None, sell_quotes=None,
) -> Snapshot | None:
    """Build a Snapshot from the last persisted ESI pull plus the manual
    slot settings. Returns None if ESI has never been refreshed.
    buy_venue / structure_units_cheaper (v1.10): per-type venue provenance
    of `prices` (see Snapshot), typically from market.buy_quotes.
    sell_ladders (v1.25): the compressed candidates' per-venue ladders
    from market.sell_ladders. hub_prices (contract C5, 2026-09-05): the
    cached Jita quote per type, {type_id: price}, from
    market.cached_hub_quotes — kept beside `prices` so the sourcing pass
    can weigh a structure-only ladder against Jita.

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
        sell_ladders=dict(sell_ladders or {}),
        hub_prices=dict(hub_prices or {}),
        # v1.26: the structure market's quote per type at its basis.
        structure_prices=dict(structure_prices or {}),
        # v1.27.1: {final type_id: sell price} for the install check's
        # ranking (ledger.final_quote per active final — a capital hull
        # is quoted at the structure market, which `prices` never holds).
        sell_quotes=dict(sell_quotes or {}),
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
    sourcing: bool = True,
    backfill: bool = True,
) -> Plan:
    """Run planning phases 2-7 (with the consumption feedback loop) and
    (optionally) persist the index run. output_qty (built-scale expansion
    override, see _expand_and_merge), alchemy=False (skip the
    substitution pass regardless of the setting), sourcing=False
    (skip the v1.25 sourcing pass — fill pricing and compressed
    substitution — likewise) and backfill=False (skip the v1.27.1
    stock-aware slot backfill) are the steady-state path's seams; the
    real /run path never passes any."""
    merged = _expand_and_merge(conn, ref, output_qty)
    # Phase 3.5 (user ruling 2026-09-09): one cycle's consumption at the
    # jobs' own rounding — the target and deficit basis — and the shares
    # the feedback loop prorates by.
    steady_shares = _cycle_need(conn, ref, merged)
    _apply_targets(conn, ref, merged, snapshot, alchemy)
    _size_jobs(conn, ref, merged)
    _allocate_slots(conn, ref, merged, snapshot, backfill=backfill)
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
    #
    # Review 2026-09-05: (A1) a dual-role final's COMPONENT share joins the
    # loop — corrected = requested + max(0, draw − stock − in flight),
    # which for a single-role final (no draw) still resolves to exactly
    # the request. (A3, ruling R7) an intermediate's target follows the
    # consumers that actually HOLD jobs: the Phase 4 stockpile figure
    # (one steady cycle × (1 + buffer)) prorated to the share of its
    # steady draw that comes from consumers with runs this cycle, plus
    # the composite extra-runs adder only where the consuming composite
    # holds jobs — so a stage whose consumers all flipped to buy (or were
    # starved of slots) is neither bought nor built to a BOM target
    # nobody draws on (no floor kept). The review's literal proposal,
    # target = ceil(realized draw × (1 + buffer)), was tried and
    # rejected: a catch-up consumer's draw is several cycles' worth, and
    # a target scaled to it COMPOUNDS down the chain (deficit ≈ 2.05 ×
    # draw per tier — from empty, a four-deep chain primed ~17 cycles of
    # fuel blocks instead of ~5) while a partly-stocked consumer's short
    # draw ended its suppliers below one cycle's consumption, breaking
    # the pipelined invariant. The stockpile is one steady cycle for the
    # consumers that build; the DEFICIT still adds the realized draw. The
    # target is stamped alongside the deficit so the persisted row keeps
    # the identity deficit = target + draw − stock − in flight the run
    # page's deficit dialog inverts.
    final_products = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }
    settings_ = store.get_settings(conn)
    class_settings_ = store.get_class_settings(conn)
    buffer_mult = 1.0 + settings_.stockpile_buffer
    max_passes = 1 + max(
        (i.depth for i in merged.values() if i.buildable), default=0
    )
    for _ in range(max_passes):
        draft_draw = _planned_consumption(conn, ref, merged)
        composite_extra = _composite_extra_targets(
            ref, settings_, class_settings_, merged,
            consumers_with_jobs_only=True,
        )
        revised = False
        for item in merged.values():
            if not item.buildable or item.alchemy_for_type_id is not None:
                continue
            draw = draft_draw.get(item.type_id, 0)
            if item.type_id in final_products:
                # Target stays the Phase 4 figure (the cycle's output).
                corrected = item.requested_qty + max(
                    0, draw - item.on_hand_qty - item.in_progress_qty
                )
            else:
                shares = steady_shares.get(item.type_id, {})
                total_share = sum(shares.values())
                active_share = sum(
                    units
                    for consumer_id, units in shares.items()
                    if merged[consumer_id].runs_allocated > 0
                )
                fraction = (
                    active_share / total_share if total_share else 0.0
                )
                # Phase 4's own arithmetic at fraction 1, so a fully
                # drawn stage keeps the exact target it was given.
                item.target_stock_qty = _ceil(
                    _ceil(item.cycle_need_qty * buffer_mult) * fraction
                ) + composite_extra.get(item.type_id, 0)
                corrected = max(
                    0,
                    item.target_stock_qty
                    + draw
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
            item.backfilled_jobs = 0
            item.runs_allocated = 0
            item.recommended_build_qty = 0
            item.recommended_buy_qty = 0
            item.recommended_action = None
            item.capacity_limited = False
            item.market_buy_qty = 0
            item.market_fallback_qty = None
            item.low_stock = False
            item.alchemy_output_qty = 0
            item.alchemy_buy_qty = 0
            # The alchemy comparison is re-derived each pass too (a route
            # dropped later must not leave last pass's figures behind).
            item.direct_unit_cost = None
            item.alchemy_unit_cost = None
            item.build_savings_per_unit = None
            item.unit_chain_cost = None
            item.savings_unpriced_inputs = 0
            # Review 2026-09-05 (finding A7): _size_jobs skips rows whose
            # corrected deficit is zero, so their sizing figures from the
            # previous pass would otherwise persist as if jobs were still
            # planned.
            item.total_runs_needed = 0
            item.jobs_needed_unconstrained = 0
            item.max_runs_per_job = 0
            item.time_per_run = None
        _size_jobs(conn, ref, merged)
        _allocate_slots(conn, ref, merged, snapshot, backfill=backfill)
        if alchemy:
            _alchemy_pass(conn, ref, merged, snapshot)
        _finalize(conn, ref, merged, snapshot)

    # v1.25, Phase 7.5: with the allocation converged and every buy
    # sized, fill-price every purchase on its sell ladders and re-source
    # the minerals / moon materials / gas from compressed purchases where
    # that is cheaper. Buy sizing never feeds job sizing, so this runs
    # ONCE, outside the loop (whose passes would only churn the rows).
    compressed_saving = (
        _sourcing_pass(conn, ref, merged, snapshot) if sourcing else None
    )

    # v1.27.1, Phase 7.6: with the jobs and every buy final, check what
    # stock on hand, in-flight output and this cycle's buys can actually
    # feed, and ration the installs (finals by return, intermediates in
    # proportion). Reads the buys, so it follows the sourcing pass;
    # changes nothing the earlier phases decided.
    _install_check(conn, ref, merged, snapshot)

    # v1.22/v1.23: the invention VINTAGE is taken once the allocation has
    # converged (the pass adds nothing to the plan items).
    invention = _invention_pass(conn, ref, merged, snapshot)

    index_run_id = None
    if persist:
        # The run number is assigned inside the INSERT itself: a separate
        # MAX+1 read raced concurrent /run requests into duplicate numbers
        # (the UNIQUE index on run_number is the backstop).
        # Contract C2 (2026-09-05): the plan-time courier rates ride the
        # run, so the realized costing lands this run's freight at the
        # rates it was planned under rather than whatever the settings
        # say when the run is costed.
        settings_ = store.get_settings(conn)
        cur = conn.execute(
            "INSERT INTO index_run (run_number, planned_start, status, "
            "wallet_character_isk, wallet_corporation_isk, "
            "compressed_saving_isk, freight_in_isk_per_m3, "
            "structure_freight_in_isk_per_m3, hub_price_basis, "
            "structure_price_basis, manufacturing_slots_available, "
            "reaction_slots_available) "
            "SELECT COALESCE(MAX(run_number), 0) + 1, datetime('now'), "
            "'planned', ?, ?, ?, ?, ?, ?, ?, ?, ? FROM index_run",
            (
                snapshot.character_isk,
                snapshot.corporation_isk,
                compressed_saving,
                settings_.freight_in_isk_per_m3,
                settings_.structure_freight_in_isk_per_m3,
                settings_.hub_price_basis,
                settings_.structure_price_basis,
                # v1.27.1 (schema 11): the pools the plan was built and
                # capped against — the settings' pools less multi-cycle
                # overhang — so the run page measures it against them.
                snapshot.slots_available.get(config.ACTIVITY_MANUFACTURING),
                snapshot.slots_available.get(config.ACTIVITY_REACTION),
            ),
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
                    buy_venue, structure_units_cheaper, unit_chain_cost,
                    compressed_outputs, compressed_ladder_units,
                    compressed_fill_orders, compressed_covered_qty,
                    effective_unit_cost, hub_buy_qty, hub_fill_price,
                    hub_fill_orders, structure_buy_qty, structure_fill_price,
                    structure_fill_orders, unfilled_qty, unfilled_price,
                    compressed_wanted_qty, market_buy_qty, market_fallback_qty,
                    install_runs, install_jobs, install_limited_by,
                    install_priority, install_draw_qty, install_short_qty,
                    install_return, install_per_job, cycle_need_qty
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
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
                    (
                        json.dumps([list(o) for o in item.compressed_outputs])
                        if item.compressed_outputs
                        else None
                    ),
                    item.compressed_ladder_units,
                    item.compressed_fill_orders,
                    item.compressed_covered_qty,
                    item.effective_unit_cost,
                    item.hub_buy_qty,
                    item.hub_fill_price,
                    item.hub_fill_orders,
                    item.structure_buy_qty,
                    item.structure_fill_price,
                    item.structure_fill_orders,
                    item.unfilled_qty,
                    item.unfilled_price,
                    item.compressed_wanted_qty,
                    item.market_buy_qty,
                    item.market_fallback_qty,
                    item.install_runs,
                    item.install_jobs,
                    item.install_limited_by,
                    item.install_priority,
                    item.install_draw_qty,
                    item.install_short_qty,
                    item.install_return,
                    item.install_per_job,
                    item.cycle_need_qty,
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
        compressed_saving_isk=compressed_saving,
    )


def _steady_output_qty(conn, plan: Plan, current: dict | None) -> dict | None:
    """Expansion overrides (pipeline_id -> qty) scaling the steady chain to
    what the line actually PRODUCES: BPC run caps (pasted or invented)
    round a final's build above its request, and every stage below must
    replace the built amount. Per final: next request = current request +
    (built − deficit) — the deficit is what job sizing rounded up, so the
    difference is the batch / whole-copy excess alone. Rounding is
    idempotent, so the caller's replan loop reaches a fixpoint
    (next == current) in one extra pass. A shared final's bump lands on
    its first pipeline — attribution only; steady plans are never
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
        # Against the DEFICIT — what the jobs were actually sized from —
        # so the bump is the batch / whole-copy rounding and nothing else.
        # Measured against the cycle need (or the merged BOM figure before
        # it) the term also carried the consumers' one-time stockpile
        # fill, which is not a scale change: it scales with the request,
        # so each pass re-raised it and the loop never reached a fixpoint,
        # exiting on its pass cap with an inflated cycle (review
        # 2026-09-10 — a final consumed by another pipeline's intermediate
        # climbed 1,000 -> 1,809 -> 2,609 -> ... every pass).
        bump = item.total_runs_needed * item.portion_size - item.deficit_qty
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
    first = plan_index_run(
        conn, ref, base, persist=False, alchemy=False, sourcing=False,
        backfill=False,
    )
    output_qty = None
    for _ in range(4):
        revised = _steady_output_qty(conn, first, output_qty)
        if revised == output_qty:
            break
        output_qty = revised
        first = plan_index_run(
            conn, ref, base, persist=False, output_qty=output_qty,
            alchemy=False,
            sourcing=False,
            backfill=False,
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
        sourcing=False,
        backfill=False,
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
        sourcing=False,
        backfill=False,
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
