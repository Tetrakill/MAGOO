"""Lag-based per-hull costing (v1.5).

The pipeline advances one stage per executed index run, so an input at
depth k of a hull delivered at completed run N was bought — or its job
installed — at the k-th previous *completed* run. Costing therefore reads
each item's price/fee snapshot from `min(depth, available history)` completed
runs back. The clamp is exact during spin-up: with history shallower than
the chain, the oldest executed run really did buy everything deeper in one
priming pass. Planned-but-never-executed runs are invisible here.

This supersedes FIFO lot genealogy (Phase 8) as the realized-cost model:
planned prices stand in for receipts, one executed bit per run.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config, industry, store

# Sell-side fee model. Sub-capital sales list at an NPC station (the only
# venue since 2026-08-23 — the player-structure option was removed): the
# broker fee shrinks with Broker Relations and standings toward the station
# owner; sales tax depends on Accounting alone. Constants to be verified
# against a live client order dialog, like every other formula in this
# project.
BROKER_FEE_BASE = 0.03
BROKER_FEE_PER_SKILL = 0.003  # Broker Relations, per level
BROKER_FEE_PER_FACTION_STANDING = 0.0003
BROKER_FEE_PER_CORP_STANDING = 0.0002
SALES_TAX_BASE = 0.075
SALES_TAX_REDUCTION_PER_SKILL = 0.11  # Accounting, per level


# The game floors the NPC-station broker fee at 1% regardless of skills
# and standings.
BROKER_FEE_NPC_FLOOR = 0.01


def broker_fee_rate(settings) -> float:
    """NPC-station broker fee from skills and standings, floored at 1%."""
    return max(
        BROKER_FEE_NPC_FLOOR,
        BROKER_FEE_BASE
        - BROKER_FEE_PER_SKILL * settings.skill_broker_relations
        - BROKER_FEE_PER_FACTION_STANDING * settings.standing_broker_faction
        - BROKER_FEE_PER_CORP_STANDING * settings.standing_broker_corp,
    )


def sales_tax_rate(settings) -> float:
    return SALES_TAX_BASE * (
        1.0 - SALES_TAX_REDUCTION_PER_SKILL * settings.skill_accounting
    )


def is_capital_priced(ref, type_id: int) -> bool:
    """Capital-class hulls (capitals, freighters, JFs, Orca) sell on the
    structure market with their own fee pair and a fixed movement cost
    (v1.6); everything else sells on the Jita region."""
    return ref.type_info(type_id).group_id in config.CAPITAL_PRICING_GROUPS


def freight_out_exempt(type_id: int) -> bool:
    """XL Upwell hulls (Keepstar, Palatine Keepstar, Sotiyo) are not hauled
    per m³ — freight-out is waived for them (v1.9)."""
    return type_id in config.FREIGHT_OUT_EXEMPT_TYPES


# --- buy venue (v1.10) -------------------------------------------------


@dataclass(frozen=True)
class BuyQuote:
    """Where one input is bought and at what raw price. ``venue`` is
    store.BUY_VENUE_HUB / BUY_VENUE_STRUCTURE, or None when neither venue
    has an order (unpriced). ``units_cheaper`` (structure venue only): how
    many units of the structure's sell ladder still land at or below the
    hub landed price — the numerator of the "market too shallow" flag the
    run page raises against the quantity actually bought (every listed
    unit when the hub has no order at all). ``region_wide`` (v1.9
    provenance): a hub quote that came from the region-wide fallback."""

    price: float | None
    venue: str | None
    units_cheaper: int | None = None
    region_wide: bool = False


def choose_buy_venue(
    hub_price: float | None,
    ladder,
    freight_volume: float,
    hub_rate: float,
    structure_rate: float,
) -> BuyQuote:
    """Pick the cheaper LANDED venue for one input (decision 2026-08-22).

    ``ladder`` is the structure market's sell ladder, ascending
    [(price, volume_remain), ...]; ``freight_volume`` the packaged m³ per
    unit; the rates are the two flat inbound ISK/m³ legs. Landed = price +
    rate × m³. The structure wins only when strictly cheaper (a tie goes to
    the deeper hub); its quote is the BEST order's raw price — the user
    chose the best price plus a depth flag over a fill price — and
    ``units_cheaper`` counts ladder units whose own landed price still beats
    the hub (every unit when the hub has no order at all). A hub-only quote
    returns the hub; no quote anywhere returns (None, None)."""
    hub_landed = (
        None if hub_price is None else hub_price + hub_rate * freight_volume
    )
    ladder = sorted(ladder or (), key=lambda order: order[0])
    if not ladder:
        return BuyQuote(hub_price, None if hub_price is None else store.BUY_VENUE_HUB)
    best_price = ladder[0][0]
    structure_landed = best_price + structure_rate * freight_volume
    if hub_landed is not None and structure_landed >= hub_landed:
        return BuyQuote(hub_price, store.BUY_VENUE_HUB)
    units = 0
    for price, volume in ladder:
        if hub_landed is not None and price + structure_rate * freight_volume > hub_landed:
            break  # ascending ladder: nothing further beats the hub
        units += int(volume)
    return BuyQuote(best_price, store.BUY_VENUE_STRUCTURE, units)


def net_proceeds_per_hull(
    price: float,
    packaged_volume: float,
    settings,
    capital: bool = False,
    freight_exempt: bool = False,
) -> float:
    """What one sold hull actually banks: price less sales tax, broker fee,
    the SCC market surcharge, and outbound movement. No collateral term by
    design. Capital-class hulls use their own fee pair and a fixed
    ISK-per-hull movement cost in place of the per-m³ freight-out;
    freight_exempt waives the per-m³ term (XL Upwell hulls, v1.9).

    The SCC surcharge applies to ALL market sales (flat ~1.5% since April
    2023), not just capitals — decision 2026-08-20; the one user-asserted
    rate covers both branches."""
    if capital:
        return (
            price
            * (
                1.0
                - settings.capital_sales_tax
                - settings.capital_broker_rate
                - settings.capital_scc_surcharge
            )
            - settings.capital_movement_cost_isk
        )
    freight = 0.0 if freight_exempt else (
        packaged_volume * settings.freight_out_isk_per_m3
    )
    return (
        price
        * (
            1.0
            - sales_tax_rate(settings)
            - broker_fee_rate(settings)
            - settings.capital_scc_surcharge
        )
        - freight
    )


@dataclass
class CostLine:
    type_id: int
    name: str
    kind: str  # 'material' | 'install' | 'freight_in' | 'bpc'
    depth: int
    qty_per_hull: float
    unit_cost: float  # snapshot price (material) or per-unit fee (install)
    lag_runs: int  # completed runs walked back for the snapshot
    clamped: bool  # true depth exceeded available history (spin-up)
    # No price on record (costed at 0, understating the total) — happens
    # for items a pipeline change added before the next price refresh.
    missing_price: bool = False
    # v1.9: priced from a region-wide fallback order (no hub-station order
    # for this raw leaf — NPC-seeded goods), current-prices view only.
    region_price: bool = False
    # v1.10: buy venue of a material line (store.BUY_VENUE_*; every
    # material line carries one — pre-v1.10 rows read as hub buys — and
    # non-material lines carry None except the per-venue freight lines).
    venue: str | None = None

    @property
    def cost_per_hull(self) -> float:
        return self.qty_per_hull * self.unit_cost


@dataclass
class HullCost:
    pipeline_id: int
    hulls_per_cycle: int
    lines: list
    # Set for the lagged (executed-run) view; None for the current-prices
    # view, which has no run anchor.
    index_run_id: int | None = None
    run_number: int | None = None

    @property
    def total(self) -> float:
        return sum(line.cost_per_hull for line in self.lines)

    def subtotal(self, kind: str) -> float:
        return sum(
            line.cost_per_hull for line in self.lines if line.kind == kind
        )

    @property
    def spin_up(self) -> bool:
        return any(line.clamped for line in self.lines)

    @property
    def missing_prices(self) -> int:
        return sum(1 for line in self.lines if line.missing_price)

    @property
    def region_priced(self) -> int:
        return sum(1 for line in self.lines if line.region_price)

    @property
    def structure_priced(self) -> int:
        """Material lines bought from the structure market (v1.10)."""
        return sum(
            1 for line in self.lines
            if line.kind == "material" and line.venue == store.BUY_VENUE_STRUCTURE
        )

    @property
    def structure_material_cost(self) -> float:
        """ISK per hull of materials bought from the structure market."""
        return sum(
            line.cost_per_hull for line in self.lines
            if line.kind == "material" and line.venue == store.BUY_VENUE_STRUCTURE
        )

    @property
    def structure_material_share_pct(self) -> float | None:
        """Null Sec Market Share: the structure market's share of this
        hull's materials cost, in percent; None with no materials cost."""
        materials = self.subtotal("material")
        if not materials:
            return None
        return self.structure_material_cost / materials * 100


@dataclass
class CycleTotals:
    """Whole-cycle roll-up of the profit cards: every hull of every
    pipeline this cycle, priced cards only. ``unpriced`` counts the cards
    left out because their final has no sell quote (their cost is not
    folded in either, so the margin stays an apples-to-apples figure)."""

    cost: float = 0.0
    proceeds: float = 0.0
    profit: float = 0.0
    hulls: int = 0
    priced: int = 0
    unpriced: int = 0
    # v1.10: materials ISK per cycle, and the part bought from the
    # structure market (Null Sec Market Share = structure ÷ materials).
    materials: float = 0.0
    structure_materials: float = 0.0

    @property
    def margin_pct(self) -> float | None:
        return self.profit / self.cost * 100 if self.cost else None

    @property
    def structure_share_pct(self) -> float | None:
        """Null Sec Market Share of the cycle's materials cost, percent;
        None when nothing priced."""
        if not self.materials:
            return None
        return self.structure_materials / self.materials * 100


def cycle_totals(cards) -> CycleTotals:
    """Sum the per-hull profit cards (dicts with ``cost``, ``net``,
    ``margin``) across the cycle: per-hull figures × hulls per cycle."""
    totals = CycleTotals()
    for card in cards:
        cost = card["cost"]
        if card.get("net") is None or card.get("margin") is None:
            totals.unpriced += 1
            continue
        hulls = cost.hulls_per_cycle
        totals.cost += cost.total * hulls
        totals.proceeds += card["net"] * hulls
        totals.profit += card["margin"] * hulls
        totals.materials += cost.subtotal("material") * hulls
        totals.structure_materials += cost.structure_material_cost * hulls
        totals.hulls += hulls
        totals.priced += 1
    return totals


def completed_sequence(conn) -> list:
    """Executed runs, oldest first — the timeline the lag walks."""
    return conn.execute(
        "SELECT index_run_id, run_number FROM index_run "
        "WHERE status = 'complete' ORDER BY run_number"
    ).fetchall()


def _run_snapshot(conn, index_run_id: int) -> dict[int, tuple]:
    """{type_id: (price, install fee, buy venue)} persisted by one run.
    buy_venue is NULL on pre-v1.10 rows — those were all hub buys."""
    return {
        row["type_id"]: (
            row["price_snapshot"],
            row["unit_install_fee"],
            row["buy_venue"],
        )
        for row in conn.execute(
            "SELECT type_id, price_snapshot, unit_install_fee, buy_venue "
            "FROM index_run_item WHERE index_run_id = ?",
            (index_run_id,),
        )
    }


def _freight_in_lines(settings, ref, lines) -> list:
    """Inbound freight (v1.10): one aggregate line per buy venue, derived
    from the material lines — packaged m³ per hull summed by each line's
    venue × that venue's flat rate. A venue with nothing hauled or a zero
    rate emits no line (pre-v1.10 runs therefore still show one Jita
    line). The structure leg is named after the configured market."""
    m3_by_venue: dict[str, float] = {}
    for line in lines:
        if line.kind != "material":
            continue
        venue = line.venue or store.BUY_VENUE_HUB
        m3_by_venue[venue] = (
            m3_by_venue.get(venue, 0.0)
            + line.qty_per_hull * ref.type_info(line.type_id).freight_volume
        )
    names = (
        (store.BUY_VENUE_HUB, "Inbound freight (Jita)"),
        (
            store.BUY_VENUE_STRUCTURE,
            f"Inbound freight ({settings.structure_market_label()})",
        ),
    )
    freight = []
    for venue, name in names:
        m3 = m3_by_venue.get(venue, 0.0)
        rate = settings.freight_in_rate(venue)
        if rate and m3:
            freight.append(
                CostLine(
                    type_id=0,
                    name=name,
                    kind="freight_in",
                    depth=0,
                    qty_per_hull=m3,  # m³ hauled per hull
                    unit_cost=rate,
                    lag_runs=0,
                    clamped=False,
                    venue=venue,
                )
            )
    return freight


def hull_cost(conn, ref, settings, index_run_id: int, pipeline_id: int):
    """Per-hull cost of one pipeline's product as of one completed run,
    built from lagged snapshots. Returns None if the run isn't in the
    completed sequence or produced no attributable hulls.

    Simplifications, deliberate: composites planned via alchemy are costed
    at the direct-route install fee (route rows are skipped); a
    capacity-limited item partially flipped to buy is still costed as
    built."""
    seq = completed_sequence(conn)
    positions = {row["index_run_id"]: i for i, row in enumerate(seq)}
    if index_run_id not in positions:
        return None
    pos = positions[index_run_id]

    pipeline = conn.execute(
        "SELECT * FROM pipeline WHERE pipeline_id = ?", (pipeline_id,)
    ).fetchone()
    if pipeline is None:
        return None

    items = conn.execute(
        # COALESCE: rows persisted before the 2026-08-20 per-pipeline depth
        # column fall back to the merged cross-pipeline max.
        "SELECT i.*, a.qty_attributable, "
        "COALESCE(a.depth, i.depth) AS pipeline_depth "
        "FROM index_run_item i "
        "JOIN index_run_item_pipeline a "
        "  ON a.index_run_item_id = i.index_run_item_id "
        "WHERE i.index_run_id = ? AND a.pipeline_id = ?",
        (index_run_id, pipeline_id),
    ).fetchall()
    hulls = next(
        (
            i["qty_attributable"]
            for i in items
            if i["type_id"] == pipeline["final_product_type_id"]
        ),
        0,
    )
    if not hulls:
        return None

    snapshots: dict[int, dict] = {}

    def lagged(type_id: int, depth: int) -> tuple:
        """(price, fee, venue, lag_runs, clamped) from the deepest snapshot
        the history allows, walking forward on a missing item (chain
        changed between runs) — the costed run itself always has it."""
        want = pos - depth
        clamped = want < 0
        for p in range(max(0, want), pos + 1):
            run_id = seq[p]["index_run_id"]
            if run_id not in snapshots:
                snapshots[run_id] = _run_snapshot(conn, run_id)
            found = snapshots[run_id].get(type_id)
            if found is not None:
                return (
                    found[0], found[1], found[2],
                    pos - p, clamped or p != max(0, want),
                )
        return None, None, None, 0, True

    lines: list[CostLine] = []
    for item in items:
        if item["alchemy_for_type_id"]:
            continue
        depth = item["pipeline_depth"] or 0
        qty_per_hull = item["qty_attributable"] / hulls
        price, fee, venue, lag, clamped = lagged(item["type_id"], depth)
        info = ref.type_info(item["type_id"])
        if item["blueprint_id"] is not None:
            lines.append(
                CostLine(
                    type_id=item["type_id"],
                    name=info.name,
                    kind="install",
                    depth=depth,
                    qty_per_hull=qty_per_hull,
                    unit_cost=fee or 0.0,
                    lag_runs=lag,
                    clamped=clamped,
                )
            )
        else:
            lines.append(
                CostLine(
                    type_id=item["type_id"],
                    name=info.name,
                    kind="material",
                    depth=depth,
                    qty_per_hull=qty_per_hull,
                    unit_cost=price or 0.0,
                    lag_runs=lag,
                    clamped=clamped,
                    missing_price=price is None,
                    # Pre-v1.10 rows carry no venue: they were hub buys.
                    venue=venue or store.BUY_VENUE_HUB,
                )
            )

    lines.extend(_freight_in_lines(settings, ref, lines))

    bpc_cost = pipeline["bpc_cost_isk"] or 0.0
    if bpc_cost:
        lines.append(
            CostLine(
                type_id=pipeline["final_product_type_id"],
                name="BPC amortization",
                kind="bpc",
                depth=0,
                qty_per_hull=1.0,
                unit_cost=bpc_cost / (pipeline["runs_per_bpc"] or 1),
                lag_runs=0,
                clamped=False,
            )
        )

    run_number = next(
        row["run_number"] for row in seq if row["index_run_id"] == index_run_id
    )
    return HullCost(
        pipeline_id=pipeline_id,
        index_run_id=index_run_id,
        run_number=run_number,
        hulls_per_cycle=hulls,
        lines=lines,
    )


def current_hull_cost(
    conn, ref, settings, pipeline, prices, adjusted, region_wide=frozenset(),
    venues=None,
):
    """Cost per hull at TODAY'S prices — what building one more hull costs
    if every input were bought and every job installed right now. The
    what-if companion to hull_cost's what-happened. region_wide: type ids
    whose cached price is a region-wide fallback (badged on the line).
    venues (v1.10): {type_id: store.BUY_VENUE_*} for the prices given —
    a type absent from it is a hub buy; each venue's m³ is hauled at its
    own flat rate.

    Walks the BOM with continuous per-unit quantities (no per-job rounding
    — that's a planning concern; the executed view carries the real
    rounding — but floored at one unit per run, which per-job rounding can
    never go below) and direct reaction routes only (no alchemy).
    Blacklisted sub-chains are bought at market, finals (of every active
    pipeline) exempt, mirroring Phase 2."""
    from .engine import _blacklist_checker  # deferred: engine imports store

    me_te = store.me_te_resolver(conn)
    class_settings = store.get_class_settings(conn)
    blacklist = _blacklist_checker(conn, ref)
    # The finals exemption spans ALL active pipelines (matching Phase 2's
    # settled rule and engine._chain_coster) — another pipeline's final
    # consumed here as an intermediate stays built, never market-bought.
    finals = {
        p["final_product_type_id"] for p in store.active_pipelines(conn)
    }

    materials: dict[int, float] = {}  # bought-leaf qty per hull
    built_qty: dict[int, float] = {}  # buildable-stage qty per hull
    fee_per_unit: dict[int, float] = {}  # per-unit install fee, per stage
    depths: dict[int, int] = {}

    def walk(
        type_id: int,
        qty_per_hull: float,
        depth: int,
        visiting: frozenset = frozenset(),
    ) -> None:
        bp = ref.blueprint_for_product(type_id)
        # Cycle safety, mirroring bom.expand: an item that transitively
        # requires itself is raw (40 self-consuming legacy blueprints exist
        # in the SDE; one as a pipeline final used to RecursionError the
        # whole Profit page).
        buildable = (
            bp is not None
            and type_id not in visiting
            and not (
                depth > 0
                and type_id not in finals
                and blacklist
                and blacklist(type_id)
            )
        )
        depths[type_id] = max(depths.get(type_id, 0), depth)
        if not buildable:
            materials[type_id] = materials.get(type_id, 0.0) + qty_per_hull
            return
        setting = class_settings.get(
            industry.classify_item(ref, type_id, bp.activity_id),
            industry.NPC_STATION,
        )
        me, _te = me_te(bp.blueprint_id, bp.activity_id)
        mat_mult = industry.build_multiplier(
            ref,
            setting,
            bp.activity_id,
            "material",
            group_id=ref.type_info(type_id).group_id,
        )
        cost_mult = industry.build_multiplier(
            ref, setting, bp.activity_id, "cost"
        )
        eiv = 0.0
        for mat_id, base_qty in ref.materials(bp.blueprint_id, bp.activity_id):
            eiv += base_qty * (adjusted.get(mat_id) or 0.0)
            per_unit = industry.unit_quantity(
                base_qty, me, mat_mult, bp.portion_size
            )
            walk(
                mat_id,
                qty_per_hull * per_unit,
                depth + 1,
                visiting | {type_id},
            )
        built_qty[type_id] = built_qty.get(type_id, 0.0) + qty_per_hull
        fee_per_unit[type_id] = (
            industry.job_install_cost(
                eiv, setting, cost_mult,
                scc_surcharge=settings.industry_scc_surcharge,
            )
            / bp.portion_size
        )

    walk(pipeline["final_product_type_id"], 1.0, 0)

    venues = venues or {}
    lines: list[CostLine] = []
    for type_id, qty in materials.items():
        info = ref.type_info(type_id)
        price = prices.get(type_id)
        venue = venues.get(type_id) or store.BUY_VENUE_HUB
        lines.append(
            CostLine(
                type_id=type_id,
                name=info.name,
                kind="material",
                depth=depths[type_id],
                qty_per_hull=qty,
                unit_cost=price or 0.0,
                lag_runs=0,
                clamped=False,
                missing_price=price is None,
                region_price=type_id in region_wide,
                venue=venue,
            )
        )
    for type_id, qty in built_qty.items():
        lines.append(
            CostLine(
                type_id=type_id,
                name=ref.type_info(type_id).name,
                kind="install",
                depth=depths[type_id],
                qty_per_hull=qty,
                unit_cost=fee_per_unit[type_id],
                lag_runs=0,
                clamped=False,
            )
        )
    lines.extend(_freight_in_lines(settings, ref, lines))
    bpc_cost = pipeline["bpc_cost_isk"] or 0.0
    if bpc_cost:
        lines.append(
            CostLine(
                type_id=pipeline["final_product_type_id"],
                name="BPC amortization",
                kind="bpc",
                depth=0,
                qty_per_hull=1.0,
                unit_cost=bpc_cost / (pipeline["runs_per_bpc"] or 1),
                lag_runs=0,
                clamped=False,
            )
        )
    return HullCost(
        pipeline_id=pipeline["pipeline_id"],
        # REQUESTED scale by ruling 2026-08-27: Units and Margin/cycle count
        # the configured output qty. The Slot Planner deliberately expands
        # at the batch-rounded BUILT scale (v1.13), so its materials bill
        # can cover more hulls than the Units column shows — per-unit
        # figures agree between the two views.
        hulls_per_cycle=pipeline["output_qty_per_run"],
        lines=lines,
    )
