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

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone

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
    """Pick the cheaper LANDED venue for one input — the Phase 1 SINGLE
    quote (decision 2026-08-22). Since 2026-09-05 the sourcing pass
    supersedes it for every item with a stored ladder by fill-pricing the
    buy across both venues (fill_merged); this quote stands for items with
    no ladder anywhere and seeds the plan before that pass.

    ``ladder`` is the structure market's sell ladder, ascending
    [(price, volume_remain[, min_volume]), ...]; ``freight_volume`` the
    packaged m³ per unit; the rates are the two flat inbound ISK/m³ legs.
    Landed = price + rate × m³. The structure wins only when strictly
    cheaper (a tie goes to the deeper hub); its quote is the BEST order's
    raw price and ``units_cheaper`` counts ladder units whose own landed
    price still beats the hub (every unit when the hub has no order at
    all). A hub-only quote returns the hub; no quote anywhere returns
    (None, None)."""
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
    for order in ladder:
        price, volume, _min_volume = _rung(order)
        if hub_landed is not None and price + structure_rate * freight_volume > hub_landed:
            break  # ascending ladder: nothing further beats the hub
        units += volume
    return BuyQuote(best_price, store.BUY_VENUE_STRUCTURE, units)


def _rung(order) -> tuple[float, int, int]:
    """(price, volume_remain, min_volume) from one ladder row — a
    (price, volume) pair from pre-2026-09-05 callers and tests, or the
    (price, volume, min_volume) triple the cached ladders return since
    the review added ESI's min_volume (a 2-tuple's minimum is 1)."""
    price, volume = order[0], int(order[1])
    min_volume = int(order[2]) if len(order) > 2 else 1
    return price, volume, max(1, min_volume)


def _fillable(volume: int, min_volume: int, remaining: int) -> int:
    """Units a walk takes from one rung with ``remaining`` still needed:
    min(remaining, volume), or 0 when that is below the order's minimum
    fill (review 2026-09-05: such a rung cannot be bought in that
    quantity, so the walk skips it and continues to the next rung)."""
    take = min(remaining, volume)
    if take <= 0 or take < min_volume:
        return 0
    return take


@dataclass(frozen=True)
class LadderFill:
    """What walking ONE sell ladder for a quantity costs (v1.25: the
    compressed pass re-fills a chosen type on its venue's ladder; direct
    buys walk both venues merged, see fill_merged). ``units`` is how many
    of the requested units the ladder held (short when it ran out),
    ``cost`` their RAW price sum, ``orders`` the rungs taken and
    ``marginal_price`` the last rung's price (None on an empty fill)."""

    units: int
    cost: float
    orders: int
    marginal_price: float | None

    @property
    def average(self) -> float | None:
        return self.cost / self.units if self.units else None


@dataclass(frozen=True)
class DirectFill:
    """One bought item's direct purchase walked across BOTH venues' sell
    ladders (v1.25 fill pricing, 2026-09-05): per-venue units, raw cost
    and orders taken, the units no stored ladder held (``unfilled``,
    priced at the last rung walked), and the landed total.

    ``remainder_venue`` (review ruling R5, 2026-09-05): store.BUY_VENUE_HUB
    only when the Jita ladder was TRUNCATED at config.HUB_LADDER_MAX_RUNGS
    rungs — the real book continues past the stored depth, so the
    remainder rides the hub quantity. Otherwise None: a ladder shorter
    than the cap WAS the whole book (or there is no Jita ladder at all),
    so the remainder is UNSOURCED — no market holds those units at plan
    time; the engine keeps them out of both venues' quantities and the
    run page says so. ``raw_average`` is the blended raw price over every
    unit (the remainder at the marginal price), the figure persisted as
    price_snapshot."""

    hub_units: int
    hub_cost: float
    hub_orders: int
    structure_units: int
    structure_cost: float
    structure_orders: int
    unfilled: int
    marginal_price: float | None
    remainder_venue: str | None
    landed_cost: float

    @property
    def units(self) -> int:
        return self.hub_units + self.structure_units + self.unfilled

    @property
    def raw_average(self) -> float | None:
        if not self.units or (
            self.marginal_price is None and self.unfilled == self.units
        ):
            return None  # nothing on any ladder: no fill price exists
        return (
            self.hub_cost
            + self.structure_cost
            + self.unfilled * (self.marginal_price or 0.0)
        ) / self.units


def _merged_rungs(hub_ladder, structure_ladder, hub_rate, structure_rate, m3):
    """merged_rungs with each rung's min_volume as a fifth element —
    the walk (fill_merged) needs it; the engine's LP columns read the
    public four-element shape."""
    rungs = []
    for ladder, rate, tie, venue in (
        (hub_ladder, hub_rate, 0, store.BUY_VENUE_HUB),
        (structure_ladder, structure_rate, 1, store.BUY_VENUE_STRUCTURE),
    ):
        for order in ladder or ():
            price, volume, min_volume = _rung(order)
            if volume > 0:
                rungs.append((price + rate * m3, tie, price, volume, venue, min_volume))
    rungs.sort()
    return [
        (landed, price, volume, venue, min_volume)
        for landed, _t, price, volume, venue, min_volume in rungs
    ]


def merged_rungs(hub_ladder, structure_ladder, hub_rate, structure_rate, m3):
    """Both venues' rungs as one ascending list of (landed, raw price,
    volume, venue) — a landed tie goes to the hub (the 2026-08-22 tie rule
    at rung level). Ladder rows may be (price, volume) pairs or the
    (price, volume, min_volume) triples the cache returns since the
    2026-09-05 review; the public shape stays four elements."""
    return [
        rung[:4]
        for rung in _merged_rungs(
            hub_ladder, structure_ladder, hub_rate, structure_rate, m3
        )
    ]


def fill_merged(
    hub_ladder, structure_ladder, qty: int, hub_rate: float,
    structure_rate: float, m3: float,
) -> DirectFill:
    """Fill ``qty`` units of one item from the cheapest LANDED rungs of
    both venues (the greedy walk is the optimum of the per-item covering
    problem). A rung whose min_volume exceeds what the walk would take
    from it is skipped (review 2026-09-05). Units beyond every stored
    rung are priced at the last rung walked; they are attributed to the
    hub only when the Jita ladder is TRUNCATED (exactly
    config.HUB_LADDER_MAX_RUNGS stored rungs, so the real book continues)
    and are otherwise unsourced (remainder_venue None — ruling R5). The
    remainder's landed cost adds the hub freight rate when the item has
    a Jita ladder at all, else the structure rate."""
    hub_rows = list(hub_ladder or ())
    rungs = _merged_rungs(hub_rows, structure_ladder, hub_rate, structure_rate, m3)
    remaining = max(0, int(qty))
    taken = {store.BUY_VENUE_HUB: [0, 0.0, 0], store.BUY_VENUE_STRUCTURE: [0, 0.0, 0]}
    landed_total = 0.0
    marginal = None
    for landed, price, volume, venue, min_volume in rungs:
        if remaining <= 0:
            break
        take = _fillable(volume, min_volume, remaining)
        if take <= 0:
            continue  # below the order's minimum fill: not buyable here
        bucket = taken[venue]
        bucket[0] += take
        bucket[1] += take * price
        bucket[2] += 1
        landed_total += take * landed
        marginal = price
        remaining -= take
    remainder_venue = None
    if remaining > 0:
        if marginal is None and rungs:
            marginal = rungs[-1][1]
        # R5: only a truncated Jita ladder proves the book goes on.
        if hub_rows and len(hub_rows) == config.HUB_LADDER_MAX_RUNGS:
            remainder_venue = store.BUY_VENUE_HUB
        rate = hub_rate if hub_rows else structure_rate
        landed_total += remaining * ((marginal or 0.0) + rate * m3)
    hub, structure = taken[store.BUY_VENUE_HUB], taken[store.BUY_VENUE_STRUCTURE]
    return DirectFill(
        hub_units=hub[0], hub_cost=hub[1], hub_orders=hub[2],
        structure_units=structure[0], structure_cost=structure[1],
        structure_orders=structure[2],
        unfilled=remaining, marginal_price=marginal,
        remainder_venue=remainder_venue, landed_cost=landed_total,
    )


def rungs_taken(
    hub_ladder, structure_ladder, qty: int, hub_rate: float,
    structure_rate: float, m3: float,
) -> list[tuple[float, float, int, str]]:
    """The rungs fill_merged's walk takes for ``qty`` units, cheapest
    landed first — [(landed, raw price, units taken, venue)], the same
    min_volume rule — stopping where the ladders run out (fewer units
    than asked means the rest is unsourced). The fill-aware build-vs-buy
    rule (v1.26) reads it rung by rung against the build cost."""
    rungs = _merged_rungs(
        list(hub_ladder or ()), structure_ladder, hub_rate, structure_rate, m3
    )
    remaining = max(0, int(qty))
    taken: list[tuple[float, float, int, str]] = []
    for landed, price, volume, venue, min_volume in rungs:
        if remaining <= 0:
            break
        take = _fillable(volume, min_volume, remaining)
        if take <= 0:
            continue
        taken.append((landed, price, take, venue))
        remaining -= take
    return taken


def fill_ladder(ladder, qty: int) -> LadderFill:
    """Walk ``ladder`` — [(price, volume_remain[, min_volume]), ...], any
    order — from the cheapest rung up until ``qty`` units are taken,
    skipping a rung whose minimum fill exceeds what would be taken from
    it (review 2026-09-05). Pure: raw prices only; the caller lands the
    result with its venue's freight leg."""
    remaining = max(0, int(qty))
    units = 0
    cost = 0.0
    orders = 0
    marginal = None
    for order in sorted(ladder or (), key=lambda order: order[0]):
        if remaining <= 0:
            break
        price, volume, min_volume = _rung(order)
        take = _fillable(volume, min_volume, remaining)
        if take <= 0:
            continue
        units += take
        cost += take * price
        orders += 1
        marginal = price
        remaining -= take
    return LadderFill(units, cost, orders, marginal)


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


SALE_VENUE_HUB = "hub"
SALE_VENUE_STRUCTURE = "structure"
# ESI location ids below this are NPC stations (60m ids); Upwell
# structures sit above it — the same split esi._resolve_location uses.
STRUCTURE_LOCATION_MIN = 64_000_000


def sale_venue(location_id) -> str | None:
    """Where a sale happened, from its ESI location id: an NPC station is
    the high-sec hub side, any Upwell structure the null-sec market side;
    None when ESI gave no location (a contract without one)."""
    if location_id is None:
        return None
    return SALE_VENUE_STRUCTURE if int(location_id) >= STRUCTURE_LOCATION_MIN else SALE_VENUE_HUB


def net_proceeds_at_venue(
    price: float,
    packaged_volume: float,
    settings,
    venue: str | None,
    capital: bool = False,
    freight_exempt: bool = False,
) -> float:
    """What one sold hull banks given WHERE it sold (the Ledger, v1.27.0).
    A hub sale pays the NPC-station broker fee and Accounting sales tax
    plus the SCC surcharge; a structure sale pays the null-sec market's
    fee pair plus the SCC surcharge. Movement: a capital-class hull takes
    the flat movement cost at EITHER venue (capitals, freighters and jump
    freighters fly themselves — 1.3M m³ of packaged volume is not
    freighted); a sub-cap pays freight-out per m³ to the hub or the
    structure leg per m³, waived for the freight-exempt XL hulls. No
    venue: the class rule of net_proceeds_per_hull, which the Planning tab
    also uses."""
    if venue is None:
        return net_proceeds_per_hull(
            price, packaged_volume, settings, capital=capital, freight_exempt=freight_exempt
        )
    if venue == SALE_VENUE_STRUCTURE:
        if capital:
            movement = settings.capital_movement_cost_isk
        else:
            movement = 0.0 if freight_exempt else (
                packaged_volume * settings.structure_freight_in_isk_per_m3
            )
        return (
            price
            * (
                1.0
                - settings.capital_sales_tax
                - settings.capital_broker_rate
                - settings.capital_scc_surcharge
            )
            - movement
        )
    if capital:
        movement = settings.capital_movement_cost_isk  # flown, never freighted
    else:
        movement = 0.0 if freight_exempt else (
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
        - movement
    )


@dataclass
class CostLine:
    type_id: int
    name: str
    kind: str  # 'material' | 'install' | 'freight_in' | 'bpc' | 'invention'
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
    # v1.25: unit_cost is already LANDED — a raw part-sourced from
    # compressed purchases carries its blended landed cost, freight
    # included, so the per-venue freight aggregation skips the line.
    landed: bool = False
    # v1.25 fill pricing: the share of a material's units bought at the
    # hub when the buy was SPLIT across venues (venue = store.BUY_VENUE_
    # SPLIT); None for a single-venue line.
    hub_fraction: float | None = None
    # Review 2026-09-05: the per-venue average RAW fill prices persisted
    # with a split buy (index_run_item.hub_fill_price /
    # structure_fill_price), so the structure's ISK share is its own
    # units at its own price rather than the blended unit_cost. None on
    # single-venue lines and on rows priced before fill pricing.
    hub_fill_price: float | None = None
    structure_fill_price: float | None = None
    # v1.29 Buy tab: a line priced from the purchase lines the user
    # recorded on the run it lags to carries all THREE venue shares
    # explicitly, because the third one ('delivered' — someone else
    # hauled it, the price is already landed) is not what is left over
    # after the hub share. Leaving structure_fraction None on such a line
    # would book the delivered units as null-sec spend and haul them at
    # the structure rate (contract review A13).
    structure_fraction: float | None = None
    delivered_fraction: float | None = None
    delivered_fill_price: float | None = None
    # Revision 4 (user ruling 2026-09-28): the share bought ANYWHERE but
    # Jita 4-4 and the configured structure market (store.BUY_VENUE_OTHER:
    # another station, another structure, a contract that starts
    # elsewhere), hauled at the default inbound rate. Same explicit-zero
    # discipline as structure_fraction: a purchase-priced line states it
    # (0.0 when nothing was bought elsewhere); a plan-priced line leaves
    # it None — the plan never buys "elsewhere".
    other_fraction: float | None = None
    other_fill_price: float | None = None
    # This line's cost came from purchase lines, not the plan's price
    # snapshot (the UI badges it so the number is traceable).
    locked: bool = False
    # ... and the re-blend behind it leaned on a pre-v1.29 approximation
    # (allocation shares inferred, no refining-tax term) because the run
    # was planned before the Buy tab existed.
    approximate: bool = False

    @property
    def cost_per_hull(self) -> float:
        return self.qty_per_hull * self.unit_cost

    def structure_share(self) -> float:
        """Fraction of this material line bought at the structure market.

        An 'other' share (revision 4) is never counted here, even when it
        was bought at another null-sec structure: the Null Sec Market
        Share measures the CONFIGURED market's share of the materials
        spend, and a purchase elsewhere is not that market's."""
        if self.kind != "material":
            return 0.0
        if self.structure_fraction is not None:
            # v1.29: stated outright, because a delivered share makes
            # "everything that is not the hub" the wrong answer.
            return self.structure_fraction
        if self.hub_fraction is not None:
            return 1.0 - self.hub_fraction
        return 1.0 if self.venue == store.BUY_VENUE_STRUCTURE else 0.0

    def structure_cost_per_hull(self) -> float:
        """ISK per hull of this line bought at the structure market. A
        split line with its structure fill price on record prices its
        structure units at THAT price (review 2026-09-05: the blended
        average over- or under-stated the null-sec share whenever the two
        venues filled at different prices); otherwise the share of the
        blended cost."""
        share = self.structure_share()
        if not share:
            return 0.0
        if self.hub_fraction is not None and self.structure_fill_price is not None:
            return self.qty_per_hull * share * self.structure_fill_price
        return self.cost_per_hull * share


@dataclass
class HullCost:
    pipeline_id: int
    # The hulls this cycle STARTS: on an executed run, this pipeline's
    # share of what the install check let the user start (v1.27.1 — the
    # jobs the Plan tab told them to run; the plan's own count on runs
    # planned before the check) plus the units already installed this
    # cycle when the plan was made (revision 7, 2026-09-29: a plan
    # re-made after the installs sizes only the rest); on the
    # current-prices view, the request.
    hulls_per_cycle: int
    lines: list
    # Set for the lagged (executed-run) view; None for the current-prices
    # view, which has no run anchor.
    index_run_id: int | None = None
    run_number: int | None = None
    # This pipeline's share of the hulls the plan BUILDS this cycle
    # (v1.27.1): differs from hulls_per_cycle only when stock fed fewer
    # jobs than the plan sized — a pool that let the plan size fewer runs
    # lowers both (review 2026-09-10). Installed units count here too
    # (revision 7). None on the current-prices view.
    hulls_planned: int | None = None

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
        """Material lines bought (wholly or partly) from the structure
        market (v1.10; split buys count since v1.25)."""
        return sum(1 for line in self.lines if line.structure_share() > 0)

    @property
    def structure_material_cost(self) -> float:
        """ISK per hull of materials bought from the structure market —
        a split line contributes its structure units at the structure
        fill price when that is on record (CostLine.structure_cost_per_hull)."""
        return sum(line.structure_cost_per_hull() for line in self.lines)

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


@dataclass(frozen=True)
class _SnapshotRow:
    """One persisted index_run_item as the lag walk reads it. buy_venue is
    NULL on pre-v1.10 rows — those were all hub buys; effective_unit_cost
    (v1.25) is the blended LANDED cost of a raw part-sourced from
    compressed purchases, NULL otherwise; hub_fraction (v1.25 fill
    pricing) is the share of the direct buy bought at the hub, NULL on
    rows priced before fill pricing (their venue says it all); the two
    fill prices (review 2026-09-05) are the per-venue average raw prices
    of a fill-priced buy, NULL before fill pricing.

    The trailing fields (v1.29 Buy tab) are only ever set by the purchase
    override in _apply_purchases: the venue shares of what was ACTUALLY
    paid (three, plus the 'other' share revision 4 added for a purchase
    anywhere but Jita 4-4 and the structure market), whether this row was
    priced from purchase lines
    (`locked`) and whether that pricing leaned on a pre-v1.29
    approximation (`approximate`). `missing_price` is set when a partial
    lock left an unpriced remainder — a plain unpriced row still says so
    by carrying price=None."""

    price: float | None
    fee: float | None
    venue: str | None
    effective: float | None
    hub_fraction: float | None
    hub_fill_price: float | None = None
    structure_fill_price: float | None = None
    structure_fraction: float | None = None
    delivered_fraction: float | None = None
    delivered_fill_price: float | None = None
    locked: bool = False
    approximate: bool = False
    missing_price: bool = False
    other_fraction: float | None = None
    other_fill_price: float | None = None


_EMPTY_SNAPSHOT_ROW = _SnapshotRow(None, None, None, None, None)


def _run_snapshot(
    conn, index_run_id: int, ref=None, rates=None, settings=None
) -> dict[int, _SnapshotRow]:
    """{type_id: _SnapshotRow} persisted by one run.

    With ``ref`` given (the lag walk always passes it), the purchases
    recorded against that run's buying cycle WIN over the plan's price
    snapshot — the realized cost of an executed run is what was actually
    paid. Since revision 3 (user ruling 2026-09-28) those run_purchase
    lines are derived from ESI wallet buys and accepted contracts
    (buying.assign_purchases); the arithmetic reads venue / quantity /
    unit_price only, so a hand-entered line and a derived one cost alike.
    ``rates`` is that run's persisted inbound freight ({venue: ISK/m³ or
    None}, see _run_freight_rates) and ``settings`` the fallback for a
    None leg; both only matter to landed figures (the compressed re-blend
    and a covered raw's stock slice).

    With ``ref`` None — or with no purchase lines on the run — the rows
    read exactly as they did before v1.29, and the caller gets the same
    object graph it got then. That early return, not arithmetic, is what
    guarantees the feature's promise that a run nothing was bought for
    costs bit-identically to before (contract review A10: reconstructing
    the plan's own split from hub_buy_qty / structure_buy_qty understates
    an unsourced row, because price_snapshot averages in the unfilled
    units at the marginal price while the per-venue quantities exclude
    them). The two extra columns the purchase basis needs
    (cycle_need_qty, merged_min_qty — contract review C13.1) are selected
    here but never reach a _SnapshotRow, so the early return's object
    graph is unchanged.

    Note what that early return does NOT cover (B22): one purchase
    anywhere — one ESI wallet buy, even of a type the run does not hold
    (C13.2: it arms the pass and touches no row) — arms _apply_purchases
    for every row of the run. The untouched rows keep their exact numbers
    because that pass skips them, not because of this return."""
    rows = conn.execute(
        "SELECT type_id, price_snapshot, unit_install_fee, buy_venue, "
        "effective_unit_cost, hub_buy_qty, structure_buy_qty, "
        "hub_fill_price, structure_fill_price, recommended_buy_qty, "
        "unfilled_qty, compressed_covered_qty, compressed_outputs, "
        "compressed_alloc, compressed_landed_isk, compressed_tax_isk, "
        "direct_landed_isk, cycle_need_qty, merged_min_qty "
        "FROM index_run_item WHERE index_run_id = ?",
        (index_run_id,),
    ).fetchall()
    snapshot: dict[int, _SnapshotRow] = {}
    for row in rows:
        fraction = None
        if row["hub_buy_qty"] is not None:
            direct = (row["hub_buy_qty"] or 0) + (row["structure_buy_qty"] or 0)
            if direct:
                fraction = (row["hub_buy_qty"] or 0) / direct
        snapshot[row["type_id"]] = _SnapshotRow(
            price=row["price_snapshot"],
            fee=row["unit_install_fee"],
            venue=row["buy_venue"],
            effective=row["effective_unit_cost"],
            hub_fraction=fraction,
            hub_fill_price=row["hub_fill_price"],
            structure_fill_price=row["structure_fill_price"],
        )
    if ref is None:
        return snapshot
    purchases = store.list_purchases(conn, index_run_id)
    if not purchases:
        return snapshot
    run = conn.execute(
        "SELECT planned_start FROM index_run WHERE index_run_id = ?", (index_run_id,)
    ).fetchone()
    return _apply_purchases(
        snapshot, rows, purchases, ref, rates, settings,
        planned_start=run["planned_start"] if run is not None else None,
    )


# --- realized purchases (Buy tab, v1.29) -----------------------------------
#
# After a run is planned the user buys its inputs, usually beating the
# plan (a Jita buy order delivered to C-J6, a split fill, a courier's
# landed price). Each purchase is one line in store.run_purchase —
# since revision 3 (user ruling 2026-09-28) derived from ESI by
# buying.assign_purchases for the run whose buying CYCLE the purchase
# falls in, not typed in. The realized cost of the run is what those
# lines say, with whatever was not bought still priced at the plan's own
# venue split and prices. Nothing here rewrites the plan: the override
# lives in the snapshot loader, so the Profit tab, the Ledger's cost
# vintages and the Buy tab's Bought / Δ cells all read one piece of
# arithmetic.
#
# The BASIS a row's lines are blended against (revision 3 §5, contract
# review C13) is what the cycle CONSUMES, not what the plan bought:
#
#     basis = max(cycle_need_qty or merged_min_qty,
#                 recommended_buy_qty + compressed_covered_qty)
#
# so min(bought, basis) units are priced at what was paid and the rest of
# the basis — the direct units nobody bought yet and the stock-covered
# slice (basis − direct − covered), which the cycle draws from on-hand
# stock (bought in an earlier cycle, or in this window before the plan
# was made) — at the plan's price. Units beyond
# the basis are over-buy: they reach the next plan as on-hand stock (R3)
# and are priced here only through the pro-rata average of the lines.
# All the terms are item units.


@dataclass(frozen=True)
class PlanBuy:
    """The plan's side of one bought row — what the UNBOUGHT remainder of
    a partial purchase is priced at, and the venue split that remainder
    keeps. ``qty`` is what the lines are blended against: the plan's
    direct buy as plan_buy_from_row decodes it, which the realized pass
    widens to the row's purchase_basis (revision 3 §5) — the remainder
    then covers the stock-covered slice too, at the same price and
    split.

    ``price`` is index_run_item.price_snapshot, the blended raw price the
    plan paid over EVERY unit (the unsourced ones included, at the
    marginal rung). The per-venue quantities exclude those unsourced
    units, so the remainder's ISK is always ``remainder × price`` and
    only its QUANTITIES split by venue; hub_price / structure_price are
    used instead only when nothing was unsourced and both are on record,
    where the two rules agree exactly (contract review A11)."""

    qty: int
    price: float | None
    venue: str | None = None
    hub_qty: int | None = None
    structure_qty: int | None = None
    hub_price: float | None = None
    structure_price: float | None = None
    unfilled_qty: int = 0


def _column(row, name: str):
    """``row[name]``, or None when the row does not carry that column —
    tolerant of sqlite3.Row and plain mappings alike, since the Buy tab's
    render tests hand in dict rows that predate a column (C13.1)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    return row[name] if name in keys else None


def plan_buy_from_row(row) -> PlanBuy:
    """PlanBuy from an index_run_item row (sqlite3.Row or mapping) — the
    one decoder, so the Buy tab and the realized costing agree on what
    the plan side of a partial purchase is. ``qty`` is the plan's DIRECT
    buy; the realized pass re-sizes it to purchase_basis(row)."""
    return PlanBuy(
        qty=int(_column(row, "recommended_buy_qty") or 0),
        price=_column(row, "price_snapshot"),
        venue=_column(row, "buy_venue"),
        hub_qty=_column(row, "hub_buy_qty"),
        structure_qty=_column(row, "structure_buy_qty"),
        hub_price=_column(row, "hub_fill_price"),
        structure_price=_column(row, "structure_fill_price"),
        unfilled_qty=int(_column(row, "unfilled_qty") or 0),
    )


def purchase_basis(row) -> int:
    """The units a row's purchases are priced against (revision 3 §5,
    user ruling 2026-09-28; contract review C13.1):

        max(cycle_need_qty or merged_min_qty,
            recommended_buy_qty + compressed_covered_qty)

    — what the cycle CONSUMES. Revision 2 blended against the plan's
    direct buy alone, so a row the plan half covered from stock priced
    that stock at the purchase price too, and a re-planned row that no
    longer buys anything (R2: its purchases still belong to this cycle)
    was skipped outright. The first term is the consumption: the per-job
    cycle need where the plan stamped one (v1.28.0), the merged BOM
    demand on an older row. The second keeps the basis from falling under
    what the plan still buys (a buffered target buys past the cycle
    need) — but since v1.29 revision 6 (user ruling R1 2026-09-28) the
    open run is re-planned after every ESI pull, so recommended_buy_qty
    is what is STILL to buy and drops to 0 once the purchases land
    (review 2026-09-28). The term therefore only holds until the next
    re-plan (and on an executed or legacy run, whose plan stays as it
    was); after an update has re-planned, the basis is the consumption
    alone and the purchase margin's units read as next cycle's stock —
    which is R5's reading. NULLs read as 0 — an older row, a compressed
    ore (merged_min_qty 0) and a hand-built test row all fall back to
    the plan's own buy, which is revision 2's basis exactly."""
    need = int(_column(row, "cycle_need_qty") or 0) or int(
        _column(row, "merged_min_qty") or 0
    )
    planned = int(_column(row, "recommended_buy_qty") or 0) + int(
        _column(row, "compressed_covered_qty") or 0
    )
    return max(need, planned)


@dataclass(frozen=True)
class PurchaseBlend:
    """One bought row's realized cost: the purchase lines plus, when the
    lock is partial, the plan's own remainder.

    ``unit_cost`` is the RAW weighted average over every unit — a
    delivered line's price is already landed but carries no freight leg,
    so summing it in raw is exactly right. ``landed_unit`` /
    ``landed_total`` add the hub, structure and 'other' shares' freight
    at the rates handed in; the delivered share hauls nothing.

    ``other_fraction`` / ``other_fill_price`` (revision 4, user ruling
    2026-09-28) are the share bought anywhere but Jita 4-4 and the
    configured structure market (store.BUY_VENUE_OTHER), hauled at the
    default inbound rate. They trail the other fields with defaults so
    the empty blend and every keyword constructor stay valid; a blend
    that priced anything always sets both."""

    plan_qty: int
    locked_qty: int
    remainder_qty: int
    quantity: float
    unit_cost: float
    hub_fraction: float
    structure_fraction: float
    delivered_fraction: float
    hub_fill_price: float | None
    structure_fill_price: float | None
    delivered_fill_price: float | None
    landed_unit: float
    landed_total: float
    # The plan left a remainder it had no price for: those units cost 0
    # here and understate the row, exactly as an unpriced row does today.
    missing_price: bool
    other_fraction: float = 0.0
    other_fill_price: float | None = None

    @property
    def venue(self) -> str | None:
        """store.BUY_VENUE_SPLIT when more than one venue holds units,
        otherwise the single venue that does (None on an empty blend) —
        store.BUY_VENUE_OTHER for a row bought wholly elsewhere."""
        live = [
            venue
            for venue, share in (
                (store.BUY_VENUE_HUB, self.hub_fraction),
                (store.BUY_VENUE_STRUCTURE, self.structure_fraction),
                (store.BUY_VENUE_DELIVERED, self.delivered_fraction),
                (store.BUY_VENUE_OTHER, self.other_fraction),
            )
            if share > 0
        ]
        if not live:
            return None
        return live[0] if len(live) == 1 else store.BUY_VENUE_SPLIT

    @property
    def status(self) -> str:
        """'none' | 'partial' | 'locked' | 'over' — how far the user got
        recording what they paid for this row."""
        if not self.locked_qty:
            return "none"
        if self.remainder_qty > 0:
            return "partial"
        return "over" if self.locked_qty > self.plan_qty else "locked"


_EMPTY_BLEND = PurchaseBlend(
    plan_qty=0, locked_qty=0, remainder_qty=0, quantity=0.0, unit_cost=0.0,
    hub_fraction=0.0, structure_fraction=0.0, delivered_fraction=0.0,
    hub_fill_price=None, structure_fill_price=None,
    delivered_fill_price=None, landed_unit=0.0, landed_total=0.0,
    missing_price=False, other_fraction=0.0, other_fill_price=None,
)


def blend_purchases(
    lines, plan: PlanBuy, hub_rate: float = 0.0,
    structure_rate: float = 0.0, m3: float = 0.0,
    other_rate: float = 0.0,
) -> PurchaseBlend:
    """Blend a row's purchase lines with the plan's remainder.

    ``lines`` are store.run_purchase rows (anything indexable by 'venue',
    'quantity', 'unit_price'). ``plan.qty`` is whatever the caller sizes
    the blend to — the realized pass passes the row's purchase_basis
    (revision 3 §5), a covered raw its direct buy, the Buy tab's Bought
    cell 0 (the lines alone, bought_cell). Buying past it is allowed and
    prices everything at actual: the remainder is max(0, plan − bought),
    so over-bought lines have no remainder and ``min(bought, qty)`` units
    are priced at the lines' PRO-RATA average across lines and venues —
    not FIFO; which ESI fill came first does not pick the units that
    count (contract review C13.2). Quantities become floats once the
    remainder splits across venues — they are weights here, never a
    units-to-buy figure.

    Four venue buckets (revision 4, user ruling 2026-09-28): hub,
    structure, delivered and 'other' — a purchase anywhere but Jita 4-4
    and the configured structure market, hauled at ``other_rate`` (the
    run's default inbound rate). ``other_rate`` trails ``m3`` so every
    positional caller written before it keeps its meaning. The plan's
    remainder never lands in 'other': the plan buys at the hub or the
    structure only."""
    qty = {
        store.BUY_VENUE_HUB: 0.0,
        store.BUY_VENUE_STRUCTURE: 0.0,
        store.BUY_VENUE_DELIVERED: 0.0,
        store.BUY_VENUE_OTHER: 0.0,
    }
    isk = dict.fromkeys(qty, 0.0)
    locked = 0
    for line in lines or ():
        venue = line["venue"]
        if venue not in qty:  # defensive: the CHECK constraint bars it
            venue = store.BUY_VENUE_HUB
        units = int(line["quantity"])
        qty[venue] += units
        isk[venue] += units * float(line["unit_price"])
        locked += units
    remainder = max(0, int(plan.qty) - locked)
    missing = False
    if remainder:
        price = plan.price
        if price is None:
            missing = True
            price = 0.0
        share = None
        if plan.hub_qty is not None:
            direct = (plan.hub_qty or 0) + (plan.structure_qty or 0)
            if direct:
                share = (plan.hub_qty or 0) / direct
        if share is None:
            # Legacy / single-venue row: the whole remainder at its venue
            # (a pre-v1.10 row carries none — those were all hub buys).
            venue = plan.venue if plan.venue in qty else store.BUY_VENUE_HUB
            qty[venue] += remainder
            isk[venue] += remainder * price
        else:
            # A11: the per-venue fill prices average to price_snapshot
            # only when no unit went unsourced — otherwise they understate
            # the remainder by the marginal rung the unsourced units sit at.
            exact = (
                not plan.unfilled_qty
                and plan.price is not None
                and plan.hub_price is not None
                and plan.structure_price is not None
            )
            legs = (
                (
                    store.BUY_VENUE_HUB,
                    remainder * share,
                    plan.hub_price if exact else price,
                ),
                (
                    store.BUY_VENUE_STRUCTURE,
                    remainder * (1.0 - share),
                    plan.structure_price if exact else price,
                ),
            )
            for venue, units, unit in legs:
                if units > 0:
                    qty[venue] += units
                    isk[venue] += units * unit
    total = sum(qty.values())
    if total <= 0:
        # A17: nothing to price (a row the plan does not buy and nobody
        # locked, or lines that somehow carry no units).
        return replace(
            _EMPTY_BLEND, plan_qty=int(plan.qty), locked_qty=locked,
            remainder_qty=remainder, missing_price=missing,
        )
    order = sum(isk.values())
    # The 'other' term is exactly 0.0 when nothing was bought elsewhere,
    # and x + 0.0 is x: a blend with no 'other' units lands to the bit
    # where it landed before revision 4.
    freight = (
        qty[store.BUY_VENUE_HUB] * hub_rate
        + qty[store.BUY_VENUE_STRUCTURE] * structure_rate
        + qty[store.BUY_VENUE_OTHER] * other_rate
    ) * m3
    landed = order + freight

    def fill_price(venue):
        return isk[venue] / qty[venue] if qty[venue] else None

    return PurchaseBlend(
        plan_qty=int(plan.qty),
        locked_qty=locked,
        remainder_qty=remainder,
        quantity=total,
        unit_cost=order / total,
        hub_fraction=qty[store.BUY_VENUE_HUB] / total,
        structure_fraction=qty[store.BUY_VENUE_STRUCTURE] / total,
        delivered_fraction=qty[store.BUY_VENUE_DELIVERED] / total,
        hub_fill_price=fill_price(store.BUY_VENUE_HUB),
        structure_fill_price=fill_price(store.BUY_VENUE_STRUCTURE),
        delivered_fill_price=fill_price(store.BUY_VENUE_DELIVERED),
        landed_unit=landed / total,
        landed_total=landed,
        missing_price=missing,
        other_fraction=qty[store.BUY_VENUE_OTHER] / total,
        other_fill_price=fill_price(store.BUY_VENUE_OTHER),
    )


def ladder_landed_unit(
    row, hub_rate: float = 0.0, structure_rate: float = 0.0, m3: float = 0.0,
) -> float | None:
    """The plan's LANDED price per unit of one index_run_item row — the
    Buy tab's Ladder cell (revision 3 R8, contract review C14.5).

    A compressed-covered raw's is its ``effective_unit_cost`` (freight
    and refining tax already inside). Any other row's is the plan blend's
    ``landed_unit``: price_snapshot at the plan's venue split plus that
    split's freight at the rates handed in — the RUN's persisted rates
    (_run_freight_rates), as for every landed figure of a planned run.
    The blend is sized to the plan's direct buy, which makes it the plan
    blend C14.5 names to the bit, or to one unit on a row the plan no
    longer buys: a re-planned zero-buy row (R2) still has a ladder, and
    the per-unit figure does not depend on the quantity. None when the
    plan priced nothing — and for ``row`` None (a line whose type has no
    index_run_item row in the run)."""
    effective = _column(row, "effective_unit_cost")
    if effective is not None:
        return float(effective)
    plan = plan_buy_from_row(row)
    if plan.price is None:
        return None
    return blend_purchases(
        (), replace(plan, qty=max(plan.qty, 1)),
        hub_rate=hub_rate, structure_rate=structure_rate, m3=m3,
    ).landed_unit


@dataclass(frozen=True)
class BoughtCell:
    """One Buy-tab row's Need / Bought / Remaining / Ladder / Δ cells
    (revision 3 R8, contract review C14.2 and C14.5).

    ``need`` is the plan's buy quantity — direct + compressed-covered —
    NOT the costing basis (purchase_basis adds on-hand stock, and a
    Remaining sized on it would tell the user to buy stock they hold).
    ``bought_landed`` is the lines alone landed at the run's rates
    (blend_purchases against a zero plan), ``ladder_unit`` is
    ladder_landed_unit, and ``delta`` = bought_landed − ladder_unit ×
    bought_qty: what the bought units cost against the ladder, None when
    nothing was bought or the plan priced nothing. These are R8's
    per-unit cells, not the realized cost: for a covered raw the Profit
    tab's change also carries the ore's reallocation (covered_raw_costs),
    and for any row it depends on the basis — the page must not claim
    this Δ equals the Profit tab's change.

    Pre-plan units (review 2026-09-28). A buying window can open before
    its run was planned (the newest plan's opens at the last execution; a
    superseded plan's purchases move to its successor), and the plan
    netted what was bought by then into on-hand stock: its Need already
    excludes those units. ``bought_qty`` / ``bought_landed`` / ``delta``
    still count every line — they are what the cycle paid — but
    ``remaining`` = Need − ``post_plan_qty``, the units bought AFTER the
    plan was made, or a re-plan would read "nothing left" while the plan
    still needs its whole Need. ``pre_plan_qty`` is the rest
    (``pre_plan_qty + post_plan_qty == bought_qty``). The per-venue
    ``hub_qty`` / ``structure_qty`` / ``delivered_qty`` / ``other_qty``
    are the POST-plan units per venue: they exist to feed the Multibuy
    reduction (C14.3), and pre-plan units come off no share either, so
    the four sum to ``post_plan_qty``, not ``bought_qty``.

    ``other_qty`` (revision 4, user ruling 2026-09-28) is the post-plan
    units bought anywhere but Jita 4-4 and the structure market
    (store.BUY_VENUE_OTHER). It trails the other fields with a default
    only so the dataclass stays constructible by keyword as before;
    bought_cell always sets it.

    Revision 6 (user ruling 2026-09-28, R4): the open run is re-planned
    in place on every ESI update, from stock that already holds the
    cycle's purchases, so the page subtracts nothing from the plan. Its
    columns are Required · On Hand · Remaining · Purchased · Ladder · Δ;
    this cell feeds Purchased (``bought_qty``, ``bought_unit``,
    ``bought_landed``, ``venue``), Ladder (``ladder_unit``) and Δ
    (``delta``) only. ``need``, ``remaining`` and the pre-/post-plan
    split stay — the arithmetic above is unchanged and pinned by tests —
    but the page no longer reads them: Remaining is the plan's own
    recommended_buy_qty + compressed_covered_qty. The per-venue fields
    fed the Multibuy reduction revision 6 removed; they still count
    POST-plan units only, so after an update-time re-plan (every line
    pre-plan) they read 0 — the Purchased cell's venue split comes from
    the lines themselves, not from them."""

    need: int
    bought_qty: int
    pre_plan_qty: int
    post_plan_qty: int
    bought_landed: float
    bought_unit: float | None
    venue: str | None
    hub_qty: int
    structure_qty: int
    delivered_qty: int
    ladder_unit: float | None
    delta: float | None
    remaining: int
    other_qty: int = 0


def bought_cell(
    row, lines, hub_rate: float = 0.0, structure_rate: float = 0.0,
    m3: float = 0.0, planned_start=None, other_rate: float = 0.0,
) -> BoughtCell:
    """BoughtCell for one row (an index_run_item row or mapping; None
    for a type the run does not hold) and its run_purchase ``lines``.
    ``hub_rate`` / ``structure_rate`` / ``other_rate`` are the run's
    persisted inbound freight (ISK/m³, a None leg already resolved by the
    caller; ``other_rate`` is the default rate a purchase anywhere but
    Jita 4-4 and the structure market hauls at — revision 4, keyword
    only in practice, after ``planned_start``) and ``m3`` the type's
    packaged volume — the same inputs the realized pass lands purchases
    with, so the two cannot disagree on a line's landed price.

    ``planned_start`` is the run's index_run.planned_start (review
    2026-09-28): a line dated at or before it is pre-plan — _pre_plan,
    the test realized costing's _prefill_split makes, so the tab and the
    realized pass cannot disagree on which side of the plan a line
    fell. None (the default) makes every line post-plan: a run with no
    planned_start, and the Buy tab's compressed-ore rows (their
    ``remaining`` is unused since revision 6, and ore_realized_landed
    counts every ore line the same way). A line refined from an
    unplanned ore (``via_type_id`` set, revision 4) is dated like any
    other: revision 6 (R3) counts hangar compressed ore as the minerals
    it yields, so the plan netted it — see _pre_plan."""
    need = int(_column(row, "recommended_buy_qty") or 0) + int(
        _column(row, "compressed_covered_qty") or 0
    )
    bought = blend_purchases(
        lines, PlanBuy(qty=0, price=None),
        hub_rate=hub_rate, structure_rate=structure_rate, m3=m3,
        other_rate=other_rate,
    )
    cut = _when(planned_start)
    by_venue = {
        store.BUY_VENUE_HUB: 0,
        store.BUY_VENUE_STRUCTURE: 0,
        store.BUY_VENUE_DELIVERED: 0,
        store.BUY_VENUE_OTHER: 0,
    }
    pre_plan = 0
    for line in lines or ():
        if _pre_plan(line, cut):
            pre_plan += int(line["quantity"])
            continue
        venue = line["venue"]
        # The same defensive fold blend_purchases makes.
        by_venue[venue if venue in by_venue else store.BUY_VENUE_HUB] += int(
            line["quantity"]
        )
    units = bought.locked_qty
    post_plan = units - pre_plan
    ladder = ladder_landed_unit(row, hub_rate, structure_rate, m3)
    return BoughtCell(
        need=need,
        bought_qty=units,
        pre_plan_qty=pre_plan,
        post_plan_qty=post_plan,
        bought_landed=bought.landed_total,
        bought_unit=bought.landed_unit if units else None,
        venue=bought.venue,
        hub_qty=by_venue[store.BUY_VENUE_HUB],
        structure_qty=by_venue[store.BUY_VENUE_STRUCTURE],
        delivered_qty=by_venue[store.BUY_VENUE_DELIVERED],
        other_qty=by_venue[store.BUY_VENUE_OTHER],
        ladder_unit=ladder,
        delta=(
            bought.landed_total - ladder * units
            if units and ladder is not None
            else None
        ),
        remaining=max(0, need - post_plan),
    )


def _compressed_outputs(value) -> dict[int, int]:
    """{material_id: units USED} from an index_run_item.compressed_outputs
    JSON blob — [[material_id, units_out, units_used], …] (A16); the
    leftovers (used = 0) are dropped, they carry no cost share."""
    if not value:
        return {}
    try:
        outputs = json.loads(value) if isinstance(value, str) else list(value)
        return {
            int(m): int(used) for m, _out, used in outputs if int(used) > 0
        }
    except (TypeError, ValueError):
        return {}


@dataclass(frozen=True)
class CoveredRawCost:
    """One compressed-covered raw priced over its FULL demand (§R7).

    ``plan_landed`` is the plan's own figure for the covered raw's
    demand — ``effective_unit_cost × demand``, freight and refining tax
    already inside — and ``realized_landed`` is those same units priced
    by what was bought: ``blend_purchases`` alone is sized to the DIRECT
    remainder and understates a row whose quantity is the whole demand
    (contract review B33). Revision 2's Buy tab showed these two numbers
    as the row's cells; revision 3's R8 cells are per bought unit
    instead (bought_cell, contract review C14.5), because these re-price
    the whole demand, the ore's reallocation included.

    ``locked`` says whether anything feeding the row moved — the raw
    itself, an ore covering it, or a sibling raw that left one of those
    ores' routes (the field keeps its v1.29 name; since revision 3 it
    means "bought", from ESI). When nothing did, ``realized_landed`` IS
    ``plan_landed``, the same float, so an untouched row shows a zero
    delta rather than a reconstruction's rounding dust.

    Revision 3 §5 (contract review C13.3) prices the raw over its
    purchase ``basis``, which can exceed the demand: the cycle also
    draws ``stock_qty = basis − demand`` units from on-hand stock bought
    in an earlier cycle, or in this window before the plan (below). That
    slice is priced at ``effective_unit_cost``
    — LANDED, as hull_cost carries a covered raw's line with no freight
    of its own; price_snapshot would drop the slice's freight — and is
    displaced last (C13.4: direct → covered → stock): bought units past
    the whole demand, up to the basis, are priced at the lines' landed
    average instead (``stock_displaced_qty`` of them). ``plan_landed``
    and ``realized_landed`` stay the DEMAND's figures, so every
    revision-2 reader of them is unchanged; the ``*_total`` properties
    add the slice. The defaults describe a row with no slice.

    ``prefilled_qty`` (review 2026-09-28) is how many of the slice's
    units were filled by lines dated at or before the run's
    planned_start: stock the plan already SAW on hand and netted out of
    direct + covered, so they never displace direct or covered units
    (see _purchase_math). They are counted in ``stock_displaced_qty``
    too."""

    type_id: int
    direct_qty: int
    covered_qty: int
    demand: int
    locked_qty: int
    displaced_qty: float
    displaced_fraction: float
    plan_landed: float
    realized_landed: float
    locked: bool
    approximate: bool
    missing_price: bool
    basis: int = 0
    stock_qty: int = 0
    stock_displaced_qty: int = 0
    plan_stock_landed: float = 0.0
    realized_stock_landed: float = 0.0
    prefilled_qty: int = 0

    @property
    def plan_total(self) -> float:
        """The plan's landed ISK over the whole basis."""
        return self.plan_landed + self.plan_stock_landed

    @property
    def realized_total(self) -> float:
        """The realized landed ISK over the whole basis."""
        return self.realized_landed + self.realized_stock_landed

    @property
    def unit_cost(self) -> float:
        """Realized landed ISK per unit of the basis — what the
        snapshot's ``effective`` becomes, and what hull_cost carries.
        With no stock slice the basis is the demand and this is
        revision 2's ``realized_landed / demand`` to the bit (x + 0.0 is
        x)."""
        basis = self.basis or self.demand
        return self.realized_total / basis if basis else 0.0

    @property
    def delta(self) -> float:
        """Realized − plan over the basis; negative is a saving against
        the ladder. An untouched slice contributes the same float to both
        sides."""
        return self.realized_total - self.plan_total


def covered_raw_costs(
    rows, purchases, ref, rates=None, settings=None, planned_start=None
) -> dict[int, CoveredRawCost]:
    """{type_id: CoveredRawCost} for every raw of ONE run that the
    compressed pass part- or fully sourced.

    The public seam §R7 needs (contract review B32): the Buy tab's
    covered-raw totals and the realized cost the Profit tab reads are
    one piece of arithmetic, so they cannot drift. It takes the whole
    run rather than one row because the re-normalisation of an ore's
    shares needs every raw that ore covers — B32's sketched per-row
    ``covered_raw_landed(row, ores, …)`` cannot be written.

    ``rows`` are index_run_item rows of one run (anything with the
    columns _run_snapshot selects; web.py's ``SELECT i.*`` join is
    fine), ``purchases`` is store.list_purchases for that run, ``rates``
    its persisted freight ({venue: ISK/m³ or None}), ``settings`` the
    fallback for a None leg and ``planned_start`` the run's
    index_run.planned_start (None: every line follows the displacement
    order, as a hand-entered line always does)."""
    return _purchase_math(rows, purchases, ref, rates, settings, planned_start)[1]


def _when(text) -> datetime | None:
    """A run_purchase date (ESI '...Z') or index_run.planned_start
    (SQLite 'YYYY-MM-DD HH:MM:SS') as an aware UTC datetime — compared
    parsed, never as text (on the same day a space sorts before 'T'); None
    for NULL or text that does not parse."""
    if not text:
        return None
    value = str(text).strip().replace(" ", "T", 1)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _line_date(line):
    try:
        return line["date"]
    except (KeyError, IndexError):  # a hand-built line with no date column
        return None


def _pre_plan(line, cut: datetime | None) -> bool:
    """Was this purchase line bought at or before the run's planned_start
    (``cut``, already _when-parsed)? Units bought then were on hand when
    the plan was made, and the plan netted them as stock (review
    2026-09-28). The ONE test realized costing (_prefill_split) and the
    Buy tab's cells (bought_cell) share, so a line can never be stock to
    one and a fresh purchase to the other — the exact second of
    planned_start included, on the pre-plan side. A line with no date (a
    hand-entered one) and a run with no planned_start are never
    pre-plan.

    A pure date test (revision 6, user ruling 2026-09-28, R3/R4).
    Revision 4 §3 exempted lines refined from an unplanned compressed ore
    (``via_type_id`` set) because the plan then ignored hangar compressed
    stock; revision 6 counts hangar compressed ore / moon ore / gas as
    the raws it reprocesses into (never ice), so ore bought before the
    plan WAS netted into the raws' buys, and its mineral lines fill the
    stock slice like any other pre-plan line. planned_start moves on
    every ESI update now (the open run is re-planned in place), so after
    an update nearly every line of the cycle is pre-plan — which is
    true: the re-plan saw all of them as stock."""
    if cut is None:
        return False
    when = _when(_line_date(line))
    return when is not None and when <= cut


def _prefill_split(lines, stock_qty: int, cut: datetime | None):
    """(prefill, rest) for one covered raw's lines (review 2026-09-28).

    Lines dated at or before the run's planned_start bought units the
    plan already counted as on-hand stock (the plan buys raws just in
    time, net of stock, so they sit in basis − direct − covered). They
    fill the stock slice first, oldest first, capped at ``stock_qty`` —
    a line straddling the cap is split into two lines of whole units —
    and never displace direct or covered units — a line refined from an
    unplanned ore included since revision 6 (hangar compressed ore is
    stock at its yield, R3; _pre_plan). Everything else (lines after the
    plan, a pre-plan surplus past the slice, a line with no date) is
    ``rest`` and follows direct → covered → stock (C13.4)."""
    lines = list(lines or ())
    if cut is None or stock_qty <= 0:
        return [], lines
    pre = []
    rest = []
    for index, line in enumerate(lines):
        if _pre_plan(line, cut):
            pre.append((_when(_line_date(line)), index, line))
        else:
            rest.append(line)
    prefill = []
    room = stock_qty
    for _when_, _index, line in sorted(pre, key=lambda p: (p[0], p[1])):
        units = int(line["quantity"])
        take = min(room, units)
        if take == units:
            prefill.append(line)
        else:
            if take > 0:
                prefill.append(dict(line, quantity=take))
            rest.append(dict(line, quantity=units - take))
        room -= take
    return prefill, rest


def _purchase_math(rows, purchases, ref, rates=None, settings=None, planned_start=None):
    """``(blend, covered)`` — realized purchase costing, built once over
    one run's item rows.

    ``blend(type_id)`` is §4.2's PurchaseBlend for a row priced from its
    snapshot price, sized to the row's purchase_basis (revision 3 §5):
    min(bought, basis) units at the lines' pro-rata average, the rest of
    the basis at the plan's price and venue split. ``covered`` is
    {type_id: CoveredRawCost} for every raw the compressed pass sourced.

    §R7 (user ruling 2026-09-24) is the rule for a covered raw: units
    the user locked displace the DIRECT remainder first and only then
    the units the compressed pass covered, so

        f_r = clamp((locked_r − direct_r) / covered_r, 0, 1)

    is the share of r's covered units bought direct instead. Ore c's
    share of r falls to ``share × (1 − f_r)``, and c's shares are then
    re-normalised over its covered raws back to the total they carried
    before — the ore is still BOUGHT WHOLE, so what r stops taking from
    it lands on the raws that stayed on its route.

    The asymmetry a reader trips on: with one raw left at f = 0.99 the
    ore's cost is still paid in full (by that raw, which is also paying
    for the units it bought direct — it bought twice), while when every
    covered raw reaches f = 1 the ore is not bought at all and
    contributes nothing anywhere (``Z_c = 0``). That is a ruling about
    the buy list, not arithmetic: a plan whose covered demand is
    entirely bought direct drops the ore from the list, and one that
    still needs a single covered unit does not.

    When no raw of an ore is displaced the persisted shares are used
    verbatim and no division happens at all (contract review B28: a
    degenerate ore whose shares are every one 0.0 — the engine's
    ``total_value <= 0`` case — and a share sum that floating point
    leaves an ulp short of 1.0 must not be re-normalised into something
    else). That, with the untouched-row short circuit in
    ``covered_cost``, is what keeps a run nobody locked bit-identical.

    Revision 3 §5 leaves all of that sized to the DEMAND (direct +
    covered) and adds the stock slice on top (contract review C13.3/4):
    a covered raw's basis can exceed its demand by the units the cycle
    draws from on-hand stock. That slice is priced at the plan's landed
    ``effective_unit_cost`` and is displaced LAST — only bought units
    beyond the whole demand reach it, at the lines' landed average,
    capped at the basis. Whenever bought <= direct + covered the slice
    sits at the plan's price and every revision-2 figure stands.

    Pre-plan lines (review 2026-09-28, P1). A buying window can open
    before the run was planned (the newest plan's opens at the last
    execution; a superseded plan's purchases move to its successor), so
    a covered raw can carry lines dated at or before ``planned_start``.
    Those units were on hand when the plan was made — it netted them out
    of direct + covered into the stock slice — so counting them toward
    f_r would push the raw off an ore the plan still buys whole and
    re-normalise the ore onto its sibling raws while the slice kept
    charging its own share: the ore counted twice. They therefore fill
    the slice first (_prefill_split) and only the rest follow
    direct → covered → stock. A line with no date (hand-entered) keeps
    that order, and so does every line of a run with no planned_start."""
    by_type = {row["type_id"]: row for row in rows}
    cut = _when(planned_start)

    def leg_rate(venue):
        # A14: never ask Settings for a 'delivered' rate — freight_in_rate
        # hands back the HUB leg for anything that is not 'structure' or
        # 'other', which would charge Jita freight on an already-landed
        # price.
        if venue == store.BUY_VENUE_DELIVERED:
            return 0.0
        value = (rates or {}).get(venue)
        if value is None and settings is not None:
            value = settings.freight_in_rate(venue)
        return value or 0.0

    hub_rate = leg_rate(store.BUY_VENUE_HUB)
    structure_rate = leg_rate(store.BUY_VENUE_STRUCTURE)
    # Revision 4 (user ruling 2026-09-28): a purchase anywhere but Jita
    # 4-4 and the structure market hauls at the run's default rate —
    # index_run.freight_in_default_isk_per_m3, the live setting on a run
    # planned before that column (the same vintage rule as its siblings).
    other_rate = leg_rate(store.BUY_VENUE_OTHER)

    def venue_rate(venue):
        """One purchase line's inbound ISK/m³ by its venue. Covers the
        ore re-blend's per-line freight (ore_realized_landed: a
        PLAN-CHOSEN ore bought elsewhere lands at the default rate) and a
        covered raw's pre-plan stock fill. Anything unrecognised folds
        to the hub leg, as blend_purchases folds it."""
        if venue == store.BUY_VENUE_DELIVERED:
            return 0.0
        if venue == store.BUY_VENUE_STRUCTURE:
            return structure_rate
        if venue == store.BUY_VENUE_OTHER:
            return other_rate
        return hub_rate

    def m3(type_id):
        return ref.type_info(type_id).freight_volume

    def direct_blend(type_id):
        """The lines blended against the plan's DIRECT buy — a covered
        raw's direct term (§R7's arithmetic, unchanged by revision 3)."""
        row = by_type[type_id]
        return blend_purchases(
            own_lines(type_id),
            plan_buy_from_row(row),
            hub_rate=hub_rate,
            structure_rate=structure_rate,
            m3=m3(type_id),
            other_rate=other_rate,
        )

    def blend(type_id):
        """The lines blended against the row's purchase_basis (revision 3
        §5) — a snapshot-priced row's realized price."""
        row = by_type[type_id]
        return blend_purchases(
            purchases.get(type_id, ()),
            replace(plan_buy_from_row(row), qty=purchase_basis(row)),
            hub_rate=hub_rate,
            structure_rate=structure_rate,
            m3=m3(type_id),
            other_rate=other_rate,
        )

    def plan_blend(type_id):
        """The same blend with NO lines — what the plan alone said this
        row's direct buy lands at. Only a pre-v1.29 row needs it (review
        2026-09-24): `direct_landed_isk` is the engine's own figure
        everywhere else."""
        row = by_type[type_id]
        return blend_purchases(
            (),
            plan_buy_from_row(row),
            hub_rate=hub_rate,
            structure_rate=structure_rate,
            m3=m3(type_id),
            other_rate=other_rate,
        )

    # Covered raws only: the pre-plan lines that fill the stock slice,
    # and the lines that follow the displacement order (everything, when
    # there is no slice or no planned_start).
    prefill_by_raw: dict[int, list] = {}
    rest_by_raw: dict[int, list] = {}
    for row in rows:
        type_id = row["type_id"]
        if row["effective_unit_cost"] is None or type_id not in purchases:
            continue
        demand = int(row["recommended_buy_qty"] or 0) + int(
            row["compressed_covered_qty"] or 0
        )
        if demand <= 0:
            continue
        stock_qty = max(purchase_basis(row), demand) - demand
        prefill_by_raw[type_id], rest_by_raw[type_id] = _prefill_split(
            purchases[type_id], stock_qty, cut
        )

    def own_lines(type_id):
        """The lines that displace direct, then covered, then stock."""
        if type_id in rest_by_raw:
            return rest_by_raw[type_id]
        return purchases.get(type_id, ())

    def locked_units(type_id):
        return sum(int(line["quantity"]) for line in own_lines(type_id))

    # Which ore rows of THIS run cover which raws (a raw is re-blended
    # when it, an ore covering it, or a sibling raw of that ore moved).
    used_by_ore = {}
    coverage: dict[int, list[int]] = {}
    for row in rows:
        used = _compressed_outputs(row["compressed_outputs"])
        if not used:
            continue
        used_by_ore[row["type_id"]] = used
        for raw_id in used:
            coverage.setdefault(raw_id, []).append(row["type_id"])

    def ore_shares(ore_id):
        """({raw: share of this ore's landed cost}, approximate?).

        compressed_alloc holds the shares the engine ACTUALLY used, zeros
        included (A5) — never re-normalised. Without it (a run planned
        before v1.29) the shares are inferred from units used × the raw's
        own price, which is not the engine's displaced-value weighting:
        close, and flagged."""
        row = by_type[ore_id]
        blob = row["compressed_alloc"]
        if blob:
            try:
                alloc = json.loads(blob) if isinstance(blob, str) else blob
                return {int(k): float(v) for k, v in alloc.items()}, False
            except (TypeError, ValueError, AttributeError):
                pass
        weights = {
            raw_id: units * float(
                (by_type[raw_id]["price_snapshot"] if raw_id in by_type else None)
                or 1.0
            )
            for raw_id, units in used_by_ore[ore_id].items()
        }
        total = sum(weights.values())
        if total <= 0:
            return {raw_id: 0.0 for raw_id in weights}, True
        return {raw_id: w / total for raw_id, w in weights.items()}, True

    def ore_plan_landed(ore_id):
        """(landed ISK at plan, its refining-tax term, approximate?) —
        order ISK + freight + reprocess tax, as the engine priced the
        pick. Pre-v1.29 rows have to be rebuilt from price_snapshot and
        know no tax term, so they understate an ore by roughly the
        configured refining tax (A16)."""
        row = by_type[ore_id]
        landed = row["compressed_landed_isk"]
        if landed is not None:
            return float(landed), float(row["compressed_tax_isk"] or 0.0), False
        qty = int(row["recommended_buy_qty"] or 0)
        price = float(row["price_snapshot"] or 0.0)
        return (
            qty * (price + venue_rate(row["buy_venue"]) * m3(ore_id)),
            0.0,
            True,
        )

    def ore_realized_landed(ore_id):
        """(landed ISK actually paid for the PLAN's quantity, approximate?).

        The refining tax stays the plan's: it is a function of the
        outputs the ore reprocesses into, not of what the ore cost.

        Over-locking is priced per unit, exactly as a direct buy is
        (review 2026-09-23): a user who bought a round lot bigger than
        the plan asked for is charged this cycle for the plan's units at
        the price they paid, not for the surplus — whose ore reaches the
        next plan as on-hand stock and would otherwise be counted twice.
        Without the cap an ore over-bought AT the plan's own price
        silently raised the covered raws' realized cost, while the same
        over-lock on a direct buy was a no-op."""
        plan_landed, tax, approximate = ore_plan_landed(ore_id)
        lines = purchases.get(ore_id)
        if not lines:
            return plan_landed, approximate
        row = by_type[ore_id]
        volume = m3(ore_id)
        actual = 0.0
        locked = 0
        for line in lines:
            units = int(line["quantity"])
            locked += units
            actual += units * (
                float(line["unit_price"]) + venue_rate(line["venue"]) * volume
            )
        plan_qty = int(row["recommended_buy_qty"] or 0)
        remainder = max(0, plan_qty - locked)
        if remainder and plan_qty > 0:  # A17: plan_qty guards the division
            actual += (plan_landed - tax) * remainder / plan_qty
        elif locked > plan_qty > 0:
            # Over-locked: the same ISK per unit, for the plan's units.
            actual *= plan_qty / locked
        return actual + tax, approximate

    displaced_by_raw: dict[int, float] = {}

    def displaced_fraction(raw_id):
        """§R7's f_r — the share of raw r's COVERED units its own
        purchase lines bought direct instead, after those lines have
        displaced the whole direct remainder. Over-locking clamps to 1:
        the surplus reaches the next plan as stock, it does not buy the
        ore twice over.

        B29: ``compressed_covered_qty`` of 0 or NULL on a row that
        somehow carries an effective cost divides by zero — the engine
        always writes the pair together, so this only guards an older or
        hand-edited row. Nothing covered, nothing to displace."""
        if raw_id in displaced_by_raw:
            return displaced_by_raw[raw_id]
        row = by_type.get(raw_id)
        covered_qty = int((row["compressed_covered_qty"] or 0) if row else 0)
        if row is None or covered_qty <= 0:
            value = 0.0
        else:
            direct_qty = int(row["recommended_buy_qty"] or 0)
            spill = locked_units(raw_id) - direct_qty
            value = min(1.0, max(0.0, spill / covered_qty))
        displaced_by_raw[raw_id] = value
        return value

    def adjusted_shares(ore_id):
        """({raw: share after §R7's displacement}, approximate?) — the
        ore is still bought whole, so its shares re-normalise to the
        total they carried before, onto the raws that stayed."""
        shares, approximate = ore_shares(ore_id)
        if not any(displaced_fraction(raw_id) > 0 for raw_id in shares):
            return shares, approximate  # B28: no division at all
        before = sum(shares.values())
        scaled = {
            raw_id: share * (1.0 - displaced_fraction(raw_id))
            for raw_id, share in shares.items()
        }
        total = sum(scaled.values())
        if total <= 0:
            # Z_c = 0: every covered raw left the route, so the ore is
            # not bought and costs nobody anything.
            return dict.fromkeys(scaled, 0.0), approximate
        factor = before / total
        return {raw_id: v * factor for raw_id, v in scaled.items()}, approximate

    locked_types = set(purchases)

    def covered_cost(row):
        """One CoveredRawCost, or None for a row that is not a covered
        raw (no effective cost, or no demand to price)."""
        type_id = row["type_id"]
        effective = row["effective_unit_cost"]
        direct_qty = int(row["recommended_buy_qty"] or 0)
        covered_qty = int(row["compressed_covered_qty"] or 0)
        demand = direct_qty + covered_qty
        if effective is None or demand <= 0:
            return None
        ores = [c for c in coverage.get(type_id, ()) if c in by_type]
        plan_landed = float(effective) * demand
        # Revision 3 §5: the units the cycle draws from on-hand stock on
        # top of the demand the plan bought or covered (C13.3). The max()
        # only guards a hand-built row: purchase_basis is never below
        # direct + covered.
        basis = max(purchase_basis(row), demand)
        stock_qty = basis - demand
        plan_stock = float(effective) * stock_qty
        locked_qty = sum(int(line["quantity"]) for line in purchases.get(type_id, ()))
        fraction = displaced_fraction(type_id)
        moved = (
            type_id in locked_types
            or any(ore_id in locked_types for ore_id in ores)
            or any(
                displaced_fraction(raw_id) > 0
                for ore_id in ores
                for raw_id in used_by_ore[ore_id]
            )
        )
        row_cost = CoveredRawCost(
            type_id=type_id,
            direct_qty=direct_qty,
            covered_qty=covered_qty,
            demand=demand,
            locked_qty=locked_qty,
            displaced_qty=fraction * covered_qty,
            displaced_fraction=fraction,
            plan_landed=plan_landed,
            realized_landed=plan_landed,
            locked=False,
            approximate=False,
            missing_price=False,
            basis=basis,
            stock_qty=stock_qty,
            plan_stock_landed=plan_stock,
            realized_stock_landed=plan_stock,
        )
        if not moved:
            # Nothing feeding this raw moved: the plan value stands, the
            # same float rather than a reconstruction of it (A10).
            return row_cost
        approximate = False
        plan_covered = 0.0
        realized_covered = 0.0
        for ore_id in ores:
            plan_share = float(ore_shares(ore_id)[0].get(type_id, 0.0))
            share_map, approximate_shares = adjusted_shares(ore_id)
            share = float(share_map.get(type_id, 0.0))
            ore_plan = ore_plan_landed(ore_id)[0]
            realized, approximate_landed = ore_realized_landed(ore_id)
            plan_covered += plan_share * ore_plan
            realized_covered += share * realized
            approximate = approximate or approximate_shares or approximate_landed
        if row["direct_landed_isk"] is not None:
            plan_direct = float(row["direct_landed_isk"])
        elif direct_qty > 0:
            # Pre-v1.29: price the direct remainder from the row's OWN plan
            # side, never by backing it out of the identity (review
            # 2026-09-24). The identity's slack is the ore's refining tax
            # the reconstruction cannot know plus the share weighting it can
            # only approximate; backing the remainder out buries that slack
            # in the one term a raw lock throws away below, so locking a
            # covered raw to the plan's own fill — a documented no-op —
            # moved the row by the tax. It belongs in `residual`, which no
            # lock touches.
            plan_direct = plan_blend(type_id).landed_total
        else:
            plan_direct = 0.0
        # Whatever the identity does not explain — nothing at all on a v1.29
        # plan, and on an older one the refining tax the reconstruction cannot
        # know plus the share weighting it can only approximate. Carrying it
        # through means the re-blend moves the plan's cost by exactly what
        # the locks changed and by nothing else, so an approximation can
        # never shift the part of the cost nobody touched (A16, B27). A
        # pre-v1.29 raw driven to f = 1 therefore reads as what its lines
        # paid PLUS that unexplained slack: the doctrine applied evenly,
        # and invisible on a v1.29 row, where the residual is 0.
        residual = plan_landed - plan_direct - plan_covered
        direct = plan_direct
        spill = 0.0
        missing = False
        stock_displaced = 0
        realized_stock = plan_stock
        prefill = prefill_by_raw.get(type_id, ())
        prefilled = sum(int(line["quantity"]) for line in prefill)
        if prefilled:
            # Pre-plan stock (review 2026-09-28): those slice units at
            # what they cost landed; the rest of the slice as below.
            volume = m3(type_id)
            stock_displaced = prefilled
            realized_stock = sum(
                int(line["quantity"])
                * (float(line["unit_price"]) + venue_rate(line["venue"]) * volume)
                for line in prefill
            ) + float(effective) * (stock_qty - prefilled)
        rest_qty = locked_units(type_id)
        if rest_qty:
            own = direct_blend(type_id)
            # §R7's direct part: the §4.2 blend over the first
            # min(locked, direct) locked units plus the unlocked direct
            # remainder at the plan's price — and, once the lines run
            # past the direct remainder, the spill part: the covered
            # units they displaced, at the lines' own landed price.
            # direct_blend() is sized to the plan's direct quantity, so
            # its landed_unit already IS that price when locked > direct
            # (and the pro rata cap on an over-lock comes with it).
            #
            # Priced as a DELTA against this pass's own no-line blend
            # (pre-release review 2026-09-30, P1): the engine lands a
            # direct remainder's unsourced units at the hub rate whenever
            # the item has a Jita ladder (fill_merged — "remainder at
            # marginal"), while blend_purchases splits the whole
            # remainder by the filled units' venue share. On a v1.29 row
            # with unfilled units, a structure share and hub rate ≠
            # structure rate the two figures differ, so swapping
            # plan_direct for the blend outright moved the row by that
            # whole difference on the first purchased unit. The
            # difference now stays where no line touches it (like
            # `residual`), and one unit moves the row by what that unit
            # changed — and, like the residual, it stands even once every
            # direct unit is bought (a known, bounded over-statement: the
            # unsourced units × m³ × the hub rate's excess over the
            # share-weighted rate). A pre-v1.29 row's plan_direct IS the no-line
            # blend, so it keeps the plain expression (bit for bit).
            direct = own.landed_unit * direct_qty
            if row["direct_landed_isk"] is not None:
                direct += plan_direct - plan_blend(type_id).landed_total
            spill = own.landed_unit * fraction * covered_qty
            missing = own.missing_price
            # C13.4: the stock slice is displaced last — only by units
            # past the whole demand, at that same landed average, capped
            # at the basis (beyond it is next cycle's stock, R3).
            # A pre-plan fill has already taken its part of the slice.
            room = stock_qty - prefilled
            late = min(room, max(0, rest_qty - demand))
            if late and prefilled:
                realized_stock += (own.landed_unit - float(effective)) * late
                stock_displaced += late
            elif late:
                realized_stock = (
                    own.landed_unit * late + float(effective) * (stock_qty - late)
                )
                stock_displaced = late
        return replace(
            row_cost,
            realized_landed=direct + spill + realized_covered + residual,
            locked=True,
            approximate=approximate,
            missing_price=missing,
            stock_displaced_qty=stock_displaced,
            realized_stock_landed=realized_stock,
            prefilled_qty=prefilled,
        )

    covered: dict[int, CoveredRawCost] = {}
    for row in rows:
        result = covered_cost(row)
        if result is not None:
            covered[row["type_id"]] = result
    return blend, covered


def _apply_purchases(snapshot, rows, purchases, ref, rates, settings, planned_start=None):
    """Let a run's purchase lines override its price snapshot.

    Direct buys (§4.2 of the v1.29 contract) become a blended raw price
    plus every venue share — hub, structure, delivered and, since
    revision 4, 'other' (bought anywhere but Jita 4-4 and the structure
    market, hauled at the run's default rate) — so freight and the Null
    Sec Market Share follow the units that were really bought. A raw the
    compressed pass part-sourced is re-blended instead (§4.3, amended by §R7): its landed
    effective cost is rebuilt from the realized cost of the ores covering
    it and of its own direct buys, each ore's allocation share taken from
    what the engine actually used (compressed_alloc) and then adjusted
    for the covered units the user bought direct instead. Runs planned
    before v1.29 carry neither the shares nor the landed totals, so those
    are approximated and the row is flagged. What the approximation
    cannot touch: an ore nobody locked contributes exactly its plan cost,
    and the part of a raw's cost the persisted identity explains is
    carried through untouched (the residual term) — only the SPLIT of the
    delta a lock makes is approximate, which is what the *approx.* badge
    means.

    What §R7 deliberately gives up is conservation ACROSS an ore's raws:
    a raw whose own purchases displace its covered units leaves that
    ore's route, and the ore — still bought whole for the raws that
    stayed — pushes its cost onto them (see _purchase_math).

    Revision 3 §5 (user ruling 2026-09-28) sizes every snapshot-priced
    row's blend to its purchase_basis — what the cycle consumes — so a
    row the plan part-covered from on-hand stock keeps that stock at the
    plan's price instead of pricing it at what this cycle paid, and
    revision 2's skip of a row the plan does not buy
    (recommended_buy_qty <= 0) is gone: purchases belong to a buying
    CYCLE (R2), so a run re-planned after the user bought can carry lines
    on a row it no longer buys, and those units are consumed and priced
    like any other. Only an empty basis (nothing consumed, nothing
    bought: an inert row) keeps its plan price — min(bought, 0) units
    are priced at actual. A compressed ore has no pipeline attribution
    at all (merged_min_qty 0, so its basis is its own buy), so its own
    lines reach a hull only through the re-blend (contract review A15).

    Known limit, documented not fixed (revision 3 §5, C13.5): a purchase
    of an item the plan BUILDS is recorded and repriced here, but
    hull_cost's install branch wins over the material branch, so
    realized costing still expands the build — the purchase moves no
    hull. Two more shapes accept lines that can never move a hull's
    cost, for reasons that predate v1.29: hull_cost skips alchemy-route
    rows outright, and a buildable partly flipped to buy is still costed
    as BUILT. The Buy tab may show their delta; its copy must not claim
    they reprice a hull. Lines on a type the run does not hold at all
    arm this pass and touch no row (C13.2).

    B22: one purchase anywhere on the run arms this pass for every row
    of it — _run_snapshot's early return only covers a run with NO lines
    at all. The promise that untouched rows keep their exact numbers
    rests on the two skips below (a row with no lines, a covered raw
    whose cluster nothing moved), not on that early return."""
    by_type = {row["type_id"]: row for row in rows}
    blend, covered = _purchase_math(
        rows, purchases, ref, rates, settings, planned_start
    )
    locked_types = set(purchases)
    for type_id, snap in list(snapshot.items()):
        row = by_type[type_id]
        if snap.effective is not None:
            realized = covered.get(type_id)
            if realized is None or not realized.locked:
                continue  # nothing feeding this raw moved: plan value stands
            snapshot[type_id] = replace(
                snap,
                effective=realized.unit_cost,
                locked=True,
                approximate=realized.approximate,
                missing_price=snap.missing_price or realized.missing_price,
            )
            continue
        if type_id not in locked_types:
            continue
        if purchase_basis(row) <= 0:
            continue  # nothing consumed: no unit to price at actual
        realized = blend(type_id)
        snapshot[type_id] = replace(
            snap,
            price=realized.unit_cost,
            venue=realized.venue,
            hub_fraction=realized.hub_fraction,
            structure_fraction=realized.structure_fraction,
            delivered_fraction=realized.delivered_fraction,
            hub_fill_price=realized.hub_fill_price,
            structure_fill_price=realized.structure_fill_price,
            delivered_fill_price=realized.delivered_fill_price,
            other_fraction=realized.other_fraction,
            other_fill_price=realized.other_fill_price,
            locked=True,
            missing_price=realized.missing_price,
        )
    return snapshot


def _run_freight_rates(conn, index_run_id: int) -> dict[str, float | None]:
    """The inbound ISK/m³ rates a run was PLANNED at, keyed by buy venue:
    index_run.freight_in_isk_per_m3 / structure_freight_in_isk_per_m3
    (persisted by the engine since the 2026-09-05 review) and, since
    revision 4 (user ruling 2026-09-28), freight_in_default_isk_per_m3 —
    the rate a purchase anywhere but Jita 4-4 and the structure market
    hauls at (store.BUY_VENUE_OTHER). Any of them is None on a run
    persisted before its column existed — the caller falls back to the
    live setting for that leg (the same vintage rule for all three)."""
    row = conn.execute(
        "SELECT freight_in_isk_per_m3, structure_freight_in_isk_per_m3, "
        "freight_in_default_isk_per_m3 "
        "FROM index_run WHERE index_run_id = ?",
        (index_run_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        store.BUY_VENUE_HUB: row["freight_in_isk_per_m3"],
        store.BUY_VENUE_STRUCTURE: row["structure_freight_in_isk_per_m3"],
        store.BUY_VENUE_OTHER: row["freight_in_default_isk_per_m3"],
    }


# The inbound freight leg of the 'other' venue (revision 4, user ruling
# 2026-09-28): purchases anywhere but Jita 4-4 and the configured
# structure market, hauled at settings.freight_in_default_isk_per_m3.
OTHER_FREIGHT_LINE_NAME = "Inbound freight (other locations)"


def _freight_in_lines(settings, ref, lines, rates=None) -> list:
    """Inbound freight (v1.10): one aggregate line per buy venue, derived
    from the material lines — packaged m³ per hull summed by each line's
    venue × that venue's flat rate. A venue with nothing hauled or a zero
    rate emits no line (pre-v1.10 runs therefore still show one Jita
    line). The structure leg is named after the configured market.

    A third leg, "Inbound freight (other locations)" (revision 4, user
    ruling 2026-09-28), hauls the 'other' share of purchase-priced lines
    — bought anywhere but Jita 4-4 and the structure market — at the
    default rate. Only a line priced from purchase lines can carry that
    share (the plan never buys elsewhere), so a run nothing was bought
    for, or bought only at the two markets, emits exactly the legs it
    emitted before. While the default rate is 0 (its default) the leg is
    not emitted at all: those units haul for free.

    ``rates`` (review 2026-09-05): {venue: ISK/m³ or None} the run was
    planned at — the realized view keeps its vintage's freight rates
    instead of repricing history whenever the setting changes. A None
    rate (a run persisted before the columns) and the current-prices
    view take the live setting for that leg."""
    m3_by_venue: dict[str, float] = {}
    for line in lines:
        if line.kind != "material" or line.landed:
            continue
        m3 = line.qty_per_hull * ref.type_info(line.type_id).freight_volume
        if line.hub_fraction is not None:
            # v1.25: a buy split across venues hauls each share at its
            # own rate. v1.29: a line priced from purchase lines states
            # its structure share outright, because the delivered share
            # (already landed) hauls nothing — the shares then sum to
            # less than 1 and the difference is deliberately not hauled.
            # Revision 4: its 'other' share hauls at the default rate; a
            # plan-priced split carries None there, which hauls nothing.
            shares = (
                (store.BUY_VENUE_HUB, line.hub_fraction),
                (
                    store.BUY_VENUE_STRUCTURE,
                    line.structure_fraction
                    if line.structure_fraction is not None
                    else 1.0 - line.hub_fraction,
                ),
                (store.BUY_VENUE_OTHER, line.other_fraction or 0.0),
            )
        else:
            shares = ((line.venue or store.BUY_VENUE_HUB, 1.0),)
        for venue, share in shares:
            if share > 0:
                m3_by_venue[venue] = m3_by_venue.get(venue, 0.0) + m3 * share
    names = (
        (store.BUY_VENUE_HUB, "Inbound freight (Jita)"),
        (
            store.BUY_VENUE_STRUCTURE,
            f"Inbound freight ({settings.structure_market_label()})",
        ),
        (store.BUY_VENUE_OTHER, OTHER_FREIGHT_LINE_NAME),
    )
    freight = []
    rates = rates or {}
    for venue, name in names:
        m3 = m3_by_venue.get(venue, 0.0)
        rate = rates.get(venue)
        if rate is None:
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


# --- invention (v1.22) --------------------------------------------------


@dataclass(frozen=True)
class InventionCost:
    """The computed economics of one pipeline's invention choice — the ONE
    shared assembly (engine's chain coster and vintage pass, the live
    profit view and the Invention tab all read this).

    Datacore/decryptor prices are LANDED (venue price + that venue's flat
    ISK/m³ × packaged m³): invention lines carry their own freight, and
    _freight_in_lines only aggregates 'material' lines, so nothing
    double-counts. A missing price counts 0 toward the attempt cost and
    increments `unpriced` (badged in the UI, like build savings).

    Relic sources (T3, 2026-08-31): the relic rides the `datacores` tuple
    as one extra per-attempt consumable triple, and `copy_fee` is 0.0 —
    a relic is consumed outright, there is no T1 copy job."""

    source: object  # refdata.InventionSource
    decryptor: object | None  # refdata.Decryptor, None = no decryptor
    probability: float  # clamped, skills applied
    me: int
    te: int
    runs_per_copy: int
    # ((type_id, qty_per_attempt, landed_price|None), ...) — the relic
    # consumable included for relic sources.
    datacores: tuple
    decryptor_price: float | None  # landed; None = unpriced or no decryptor
    invention_fee: float  # per attempt
    copy_fee: float  # per attempt: one 1-run T1 copy (0.0 for relics)
    unpriced: int  # datacore/relic/decryptor prices missing (counted as 0)

    @property
    def attempt_cost(self) -> float:
        cost = self.invention_fee + self.copy_fee
        for _type_id, qty, price in self.datacores:
            cost += qty * (price or 0.0)
        if self.decryptor is not None:
            cost += self.decryptor_price or 0.0
        return cost

    @property
    def cost_per_run(self) -> float:
        """Expected invention ISK per licensed run of the invented copy."""
        return industry.invention_cost_per_run(
            self.attempt_cost, self.probability, self.runs_per_copy
        )


def landed_price(ref, settings, price, venue, type_id: int) -> float | None:
    """Landed buy price: the venue's raw price plus THAT venue's flat
    inbound courier rate on packaged m³ (v1.10). None when unpriced. The
    one formula behind engine._landed_price and the live profit view
    (review 2026-09-01: current_hull_cost carried its own copy)."""
    if price is None:
        return None
    return (
        price
        + settings.freight_in_rate(venue) * ref.type_info(type_id).freight_volume
    )


def resolve_invention(ref, pipeline):
    """(InventionSource, Decryptor | None) for a pipeline whose invention
    choice still resolves; None when invention is off OR the config is
    STALE — the final no longer has its chosen (or single) source, or the
    stored decryptor id no longer resolves. The one stale-config rule
    (review 2026-09-01: engine, costing and the Pipelines page each
    decided this on their own, and a vanished decryptor was silently
    costed as no-decryptor while the materialised runs/ME/TE kept its
    modifiers). Stale = the manual bpc_cost_isk fallback everywhere and
    the Pipelines page's Off control."""
    if not pipeline["use_invention"]:
        return None
    source = ref.invention_source_for_product(
        pipeline["final_product_type_id"],
        pipeline["invention_source_blueprint_id"],
    )
    if source is None:
        return None
    decryptor = None
    if pipeline["decryptor_type_id"]:
        decryptor = ref.decryptor(pipeline["decryptor_type_id"])
        if decryptor is None:
            return None
    return source, decryptor


def invention_chance(ref, settings, source, decryptor) -> float:
    """Success chance of one invention choice — the prices-free piece,
    shared by invention_cost and the Pipelines-page save flash.
    Each required activity-8 skill resolves through the same name-family
    router the time math uses: the '…Encryption Methods' skill supplies
    the /40 term, the (two) datacore sciences the /30 terms. Only SCIENCE-
    group skills (config.SKILL_GROUP_SCIENCE) count: a Production-group
    gate skill on the invention activity (Capital Ship Construction,
    Outpost Construction) does not move the chance — review 2026-09-05
    (P0), it was being summed as a third /30 science term."""
    skills = settings.skill_levels()
    science_levels = []
    encryption_level = 0
    for skill_type_id, _required in ref.blueprint_skills(
        source.t1_blueprint_id, config.ACTIVITY_INVENTION
    ):
        info = ref.type_info(skill_type_id)
        if info.group_id != config.SKILL_GROUP_SCIENCE:
            continue
        name = info.name
        level = industry._per_bp_skill_level(name, skills)
        if name.endswith(config.SKILL_SUFFIX_ENCRYPTION):
            encryption_level = level
        else:
            science_levels.append(level)
    return industry.invention_probability(
        source.probability,
        science_levels,
        encryption_level,
        decryptor.prob_mult if decryptor else 1.0,
    )


def invention_cost(
    ref, settings, class_settings, source, decryptor, price_of, adjusted_of
) -> InventionCost:
    """Assemble one invention choice's economics.

    price_of(type_id) -> LANDED unit price or None; adjusted_of(type_id) ->
    CCP adjusted price or None (missing adjusted counts 0, matching every
    other fee base). Each fee reads its own lab row — 'invention' and
    'copying' (split 2026-08-31; copying falls back to the invention row
    on a not-yet-reseeded database) — and its own activity's structure
    cost bonus: fee = job_install_cost(2% × EIV_T1, lab, cost mult, scc),
    where EIV_T1 spans the T1 blueprint's MANUFACTURING materials
    (PROJECT.md §5)."""
    probability = invention_chance(ref, settings, source, decryptor)
    me, te, runs_per_copy = industry.invented_bpc(
        source.runs,
        decryptor.me_mod if decryptor else 0,
        decryptor.te_mod if decryptor else 0,
        decryptor.run_mod if decryptor else 0,
    )

    invention_lab = class_settings.get("invention", industry.NPC_STATION)
    copy_lab = class_settings.get("copying", invention_lab)
    relic = ref.is_relic_source(source.t1_blueprint_id)
    eiv_base = sum(
        base_qty * (adjusted_of(material_id) or 0.0)
        for material_id, base_qty in ref.materials(
            # A relic has no manufacturing activity: its attempt's fee
            # base is 2% of the INVENTED blueprint's product
            # manufacturing EIV (user decision 2026-08-31 — pending
            # in-client verification, like JOB_FEE_EIV_FRACTION itself).
            source.product_blueprint_id if relic else source.t1_blueprint_id,
            config.ACTIVITY_MANUFACTURING,
        )
    )
    fee_base = config.JOB_FEE_EIV_FRACTION * eiv_base
    invention_fee = industry.job_install_cost(
        fee_base,
        invention_lab,
        industry.build_multiplier(
            ref, invention_lab, config.ACTIVITY_INVENTION, "cost"
        ),
        scc_surcharge=settings.industry_scc_surcharge,
    )
    # A relic is consumed outright — there is no T1 copy job to charge.
    copy_fee = (
        0.0
        if relic
        else industry.job_install_cost(
            fee_base,
            copy_lab,
            industry.build_multiplier(
                ref, copy_lab, config.ACTIVITY_COPYING, "cost"
            ),
            scc_surcharge=settings.industry_scc_surcharge,
        )
    )

    datacores = tuple(
        (material_id, qty, price_of(material_id))
        for material_id, qty in ref.materials(
            source.t1_blueprint_id, config.ACTIVITY_INVENTION
        )
    )
    if relic:
        # The relic itself is a per-attempt consumable: folding it into
        # the datacores tuple makes every downstream surface work
        # untouched — attempt_cost, the buy-row demand loop, the
        # persisted JSON, the hull-cost replay, the run page's Input
        # table.
        datacores += (
            (source.t1_blueprint_id, 1, price_of(source.t1_blueprint_id)),
        )
    decryptor_price = price_of(decryptor.type_id) if decryptor else None
    unpriced = sum(1 for _t, _q, price in datacores if price is None)
    if decryptor is not None and decryptor_price is None:
        unpriced += 1
    return InventionCost(
        source=source,
        decryptor=decryptor,
        probability=probability,
        me=me,
        te=te,
        runs_per_copy=runs_per_copy,
        datacores=datacores,
        decryptor_price=decryptor_price,
        invention_fee=invention_fee,
        copy_fee=copy_fee,
        unpriced=unpriced,
    )


def bpc_divisor(pipeline):
    """runs_per_bpc as the manual-BPC amortization divisor. While
    use_invention is on (a stale config included), runs_per_bpc holds the
    MATERIALIZED invented run count — dividing by it would let the toggle
    reprice pre-invention realized history — so the stashed
    manual_runs_per_bpc applies instead (v1.22 review). May be None
    (uncapped / never set); callers keep their existing None semantics.
    Both columns are schema-guaranteed (SCHEMA_VERSION >= 4)."""
    if pipeline["use_invention"]:
        return pipeline["manual_runs_per_bpc"]
    return pipeline["runs_per_bpc"]


def _bpc_line(pipeline):
    """The manual-BPC amortization line, or None when the pipeline carries
    no bpc_cost_isk OR no divisor (runs_per_bpc unset) — the fallback both
    cost views share (review 2026-09-01: each had its own copy). Review
    2026-09-05: a NULL divisor used to charge the WHOLE bpc cost per hull
    here while engine._chain_coster charged nothing; the engine's rule
    wins, so the two costings agree."""
    bpc_cost = pipeline["bpc_cost_isk"] or 0.0
    divisor = bpc_divisor(pipeline)
    if not bpc_cost or not divisor:
        return None
    return CostLine(
        type_id=pipeline["final_product_type_id"],
        name="BPC amortization",
        kind="bpc",
        depth=0,
        qty_per_hull=1.0,
        unit_cost=bpc_cost / divisor,
        lag_runs=0,
        clamped=False,
    )


def _invention_cost_lines(
    ref,
    probability: float,
    runs_per_copy: int,
    portion: int,
    datacores,
    decryptor_type_id: int | None,
    decryptor_price: float | None,
    invention_fee: float,
    copy_fee: float,
) -> list:
    """kind='invention' CostLines for one pipeline, scaled by the
    CONTINUOUS expected attempts per hull, 1/(P × runs × portion) — since
    v1.23 BOTH views price this way, the realized one from the persisted
    row's vintage figures (the factor lives here, once — review
    2026-09-01). All lag 0 by design: the invention spend happens AT the
    run that licenses the copies — there is no deeper vintage to walk
    back to."""
    attempts_per_hull = 1.0 / (probability * runs_per_copy * (portion or 1))
    lines = []
    for type_id, qty, price in datacores:
        lines.append(
            CostLine(
                type_id=type_id,
                name=ref.type_info(type_id).name,
                kind="invention",
                depth=0,
                qty_per_hull=qty * attempts_per_hull,
                unit_cost=price or 0.0,
                lag_runs=0,
                clamped=False,
                missing_price=price is None,
            )
        )
    if decryptor_type_id:
        lines.append(
            CostLine(
                type_id=decryptor_type_id,
                name=ref.type_info(decryptor_type_id).name,
                kind="invention",
                depth=0,
                qty_per_hull=attempts_per_hull,
                unit_cost=decryptor_price or 0.0,
                lag_runs=0,
                clamped=False,
                missing_price=decryptor_price is None,
            )
        )
    for name, fee in (
        ("Invention job fee", invention_fee),
        ("T1 copy fee", copy_fee),
    ):
        if fee:
            lines.append(
                CostLine(
                    type_id=0,
                    name=name,
                    kind="invention",
                    depth=0,
                    qty_per_hull=attempts_per_hull,
                    unit_cost=fee,
                    lag_runs=0,
                    clamped=False,
                )
            )
    return lines


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
    final = next(
        (
            i
            for i in items
            if i["type_id"] == pipeline["final_product_type_id"]
        ),
        None,
    )
    # This pipeline's share of the cycle's hull DEMAND — the divisor of
    # every per-hull line (the material shares are attributed per
    # demanded hull) and the ceiling of both counts below.
    attributed = final["qty_attributable"] if final is not None else 0
    if not attributed:
        return None
    portion = int(final["portion_size"] or 1)
    # The merged demand that attribution is a share OF.
    whole = (
        final["cycle_need_qty"]
        if "cycle_need_qty" in final.keys() and final["cycle_need_qty"]
        else final["merged_min_qty"]
    )

    def share(total: int) -> int:
        """This pipeline's share of a merged BUILD quantity: pro rata to
        the cycle demand where another pipeline consumes the final too,
        rounded UP so a started hull never reads as none (review
        2026-09-09 — banker's rounding turned 1 of 2 into 0), capped at
        the hulls this pipeline was attributed (a batch overbuild nets
        off next cycle rather than counting here)."""
        if total <= 0:
            return 0
        if not whole or whole <= attributed:
            return min(attributed, total)
        return min(attributed, -(-total * attributed // whole))

    # v1.27.1: the hulls the cycle PLANS and the hulls it actually
    # STARTS. Both are measured on what the plan BUILDS, not on the
    # demand attribution (review 2026-09-10: `qty_attributable` is the
    # DEMAND, so using it as the plan's count badged every slot-limited
    # run `short` although nothing was short, and a final the allocator
    # gave no job at all read as a whole cycle started). `install_runs`
    # is NULL both on a run planned before the check AND on any row
    # holding no jobs, so the build is the fallback for both: a final
    # with no jobs plans 0 and starts 0. cycle_totals and the Units
    # column count the started figure; the per-hull cost stands whatever
    # the count, and stays the Ledger's cost basis even when the cycle
    # started none (user ruling 2026-09-09).
    #
    # v1.29 revision 7 (user ruling 2026-09-29; contract amendment 5):
    # the open run is re-planned on every ESI update, so the last plan
    # before Mark executed normally follows the installs and sizes only
    # the part of the wave NOT yet installed — `runs_allocated` 0 and
    # `install_runs` NULL once the whole wave is in. The units already
    # installed this cycle (`installed_qty`, 0 when NULL or on a row
    # planned before the column) count as both planned and started, or
    # the Profit tab's Units and cycle_totals would count 0 hulls for a
    # cycle that built them all. share() still caps both at the hulls
    # this pipeline was attributed.
    #
    # Two known limits of the started count (review of revision 7,
    # 2026-09-29), both awaiting the user's ruling rather than guessed
    # around here:
    # - The remainder's install_runs is the install check's, rationed
    #   against the CONFIGURED pools (ruling R2, 2026-09-28: running jobs
    #   are never subtracted). When the installs already fill every line
    #   (a slot-limited final: 6 slots, 8 requested, 6 installed) the
    #   re-plan still lists the rest (2) as jobs to run now, and started
    #   counts them — it assumes the listed remainder was installable.
    # - Finals installed after Mark executed (the cut's known limit,
    #   engine._installed_this_cycle) count in the NEXT run's
    #   installed_qty while this run's last plan still holds them as
    #   runs, so both executed runs count the same hulls.
    installed = (
        int(final["installed_qty"] or 0)
        if "installed_qty" in final.keys()
        else 0
    )
    runs_built = int(final["runs_allocated"] or 0) * portion
    built = runs_built + installed
    started = (
        int(final["install_runs"]) * portion
        if "install_runs" in final.keys() and final["install_runs"] is not None
        else runs_built
    ) + installed
    planned_hulls = share(built)
    hulls = min(planned_hulls, share(started))

    snapshots: dict[int, dict] = {}

    def lagged(type_id: int, depth: int) -> tuple[_SnapshotRow, int, bool]:
        """(snapshot row, lag_runs, clamped) from the deepest snapshot the
        history allows, walking forward on a missing item (chain changed
        between runs) — the costed run itself always has it."""
        want = pos - depth
        clamped = want < 0
        for p in range(max(0, want), pos + 1):
            run_id = seq[p]["index_run_id"]
            if run_id not in snapshots:
                # v1.29: each lagged run's own purchase lines (and its own
                # freight vintage) decide what its inputs really cost.
                snapshots[run_id] = _run_snapshot(
                    conn,
                    run_id,
                    ref=ref,
                    rates=_run_freight_rates(conn, run_id),
                    settings=settings,
                )
            found = snapshots[run_id].get(type_id)
            if found is not None:
                return found, pos - p, clamped or p != max(0, want)
        return _EMPTY_SNAPSHOT_ROW, 0, True

    lines: list[CostLine] = []
    for item in items:
        if item["alchemy_for_type_id"]:
            continue
        depth = item["pipeline_depth"] or 0
        qty_per_hull = item["qty_attributable"] / attributed
        snap, lag, clamped = lagged(item["type_id"], depth)
        info = ref.type_info(item["type_id"])
        if item["blueprint_id"] is not None:
            lines.append(
                CostLine(
                    type_id=item["type_id"],
                    name=info.name,
                    kind="install",
                    depth=depth,
                    qty_per_hull=qty_per_hull,
                    unit_cost=snap.fee or 0.0,
                    lag_runs=lag,
                    clamped=clamped,
                    # Review 2026-09-05: a NULL persisted fee (no adjusted
                    # price on record at plan time) costs 0 here and
                    # understates the total — badge it like a missing
                    # material price instead of hiding it.
                    missing_price=snap.fee is None,
                )
            )
        elif snap.effective is not None:
            # v1.25: part-sourced from compressed purchases at that
            # run — the blended LANDED cost, freight already inside.
            # v1.29: re-blended from the purchase lines when the ore (or
            # the raw's own direct remainder) was locked.
            lines.append(
                CostLine(
                    type_id=item["type_id"],
                    name=info.name,
                    kind="material",
                    depth=depth,
                    qty_per_hull=qty_per_hull,
                    unit_cost=snap.effective,
                    lag_runs=lag,
                    clamped=clamped,
                    missing_price=snap.missing_price,
                    venue=None,
                    landed=True,
                    locked=snap.locked,
                    approximate=snap.approximate,
                )
            )
        elif snap.locked:
            # v1.29: what was paid for this input in the buying cycle of
            # the run it lags to (ESI-derived since revision 3), blended
            # over the cycle's purchase basis. Every venue share is
            # stated, so freight hauls the hub, structure and 'other'
            # units each at its own leg and the Null Sec Market Share
            # counts the structure units alone (contract review A12/A13:
            # a delivered share is NOT the `landed` compressed marker and
            # is not "the rest"; revision 4's 'other' share is neither).
            lines.append(
                CostLine(
                    type_id=item["type_id"],
                    name=info.name,
                    kind="material",
                    depth=depth,
                    qty_per_hull=qty_per_hull,
                    unit_cost=snap.price or 0.0,
                    lag_runs=lag,
                    clamped=clamped,
                    missing_price=snap.missing_price,
                    venue=snap.venue,
                    hub_fraction=snap.hub_fraction,
                    structure_fraction=snap.structure_fraction,
                    delivered_fraction=snap.delivered_fraction,
                    hub_fill_price=snap.hub_fill_price,
                    structure_fill_price=snap.structure_fill_price,
                    delivered_fill_price=snap.delivered_fill_price,
                    other_fraction=snap.other_fraction,
                    other_fill_price=snap.other_fill_price,
                    locked=True,
                )
            )
        else:
            # v1.25 fill pricing: a buy split across venues carries its
            # hub share so freight and the null-sec share split with it.
            fraction = snap.hub_fraction
            venue = snap.venue
            split = fraction is not None and 0.0 < fraction < 1.0
            if fraction is not None and not split:
                venue = (
                    store.BUY_VENUE_HUB if fraction >= 1.0
                    else store.BUY_VENUE_STRUCTURE
                )
            lines.append(
                CostLine(
                    type_id=item["type_id"],
                    name=info.name,
                    kind="material",
                    depth=depth,
                    qty_per_hull=qty_per_hull,
                    unit_cost=snap.price or 0.0,
                    lag_runs=lag,
                    clamped=clamped,
                    missing_price=snap.price is None,
                    # Pre-v1.10 rows carry no venue: they were hub buys.
                    venue=(
                        store.BUY_VENUE_SPLIT if split
                        else (venue or store.BUY_VENUE_HUB)
                    ),
                    hub_fraction=fraction if split else None,
                    hub_fill_price=snap.hub_fill_price if split else None,
                    structure_fill_price=(
                        snap.structure_fill_price if split else None
                    ),
                )
            )

    # Review 2026-09-05: freight at the rates the run was planned at
    # (live rates for runs persisted before the columns existed).
    lines.extend(
        _freight_in_lines(
            settings, ref, lines, rates=_run_freight_rates(conn, index_run_id)
        )
    )

    # v1.22: a persisted invention snapshot supersedes the bpc line — the
    # realized view reads THAT run's economics, never today's config.
    invention = conn.execute(
        "SELECT * FROM index_run_invention "
        "WHERE index_run_id = ? AND pipeline_id = ?",
        (index_run_id, pipeline_id),
    ).fetchone()
    if invention is not None:
        # v1.23: the realized replay prices invention at the vintage's
        # CONTINUOUS expected consumption — qty/attempt ÷ (P ×
        # runs_per_copy × portion) — so a stockpile-overbuild cycle
        # doesn't spike the per-hull cost and a stock-covered (or
        # slot-starved) cycle doesn't dip it to zero. probability/runs
        # come from THIS row (the vintage), never live config.
        portion = next(
            (
                item["portion_size"]
                for item in items
                if item["type_id"] == pipeline["final_product_type_id"]
            ),
            1,
        ) or 1
        lines.extend(
            _invention_cost_lines(
                ref,
                invention["probability"],
                invention["runs_per_copy"],
                portion,
                json.loads(invention["datacores"]),
                invention["decryptor_type_id"],
                invention["decryptor_unit_price"],
                invention["invention_fee_per_attempt"],
                invention["copy_fee_per_attempt"],
            )
        )
    else:
        bpc = _bpc_line(pipeline)
        if bpc is not None:
            lines.append(bpc)

    run_number = next(
        row["run_number"] for row in seq if row["index_run_id"] == index_run_id
    )
    return HullCost(
        pipeline_id=pipeline_id,
        index_run_id=index_run_id,
        run_number=run_number,
        hulls_per_cycle=hulls,
        hulls_planned=planned_hulls,
        lines=lines,
    )


def current_hull_cost(
    conn, ref, settings, pipeline, prices, adjusted, region_wide=frozenset(),
    venues=None, invention=None,
):
    """Cost per hull at TODAY'S prices — what building one more hull costs
    if every input were bought and every job installed right now. The
    what-if companion to hull_cost's what-happened. region_wide: type ids
    whose cached price is a region-wide fallback (badged on the line).
    venues (v1.10): {type_id: store.BUY_VENUE_*} for the prices given —
    a type absent from it is a hub buy; each venue's m³ is hauled at its
    own flat rate.

    invention (the Pipelines compare window, 2026-09-05): an
    (InventionSource, Decryptor | None) pair to cost INSTEAD of the
    pipeline's stored choice — the final's blueprint takes that copy's
    invented ME/TE for the walk (a decryptor's ME modifier moves the
    whole materials bill, not just the invention line) and the invention
    lines price that choice, whether the pipeline has invention on, off
    or stale. None = the stored choice, as before.

    Walks the BOM with continuous per-unit quantities (no per-job rounding
    — that's a planning concern; the executed view carries the real
    rounding — but floored at one unit per run, which per-job rounding can
    never go below) and direct reaction routes only (no alchemy).
    Blacklisted sub-chains are bought at market, finals (of every active
    pipeline) exempt, mirroring Phase 2."""
    from .engine import _blacklist_checker  # deferred: engine imports store

    me_te = store.me_te_resolver(conn)
    if invention is not None:
        what_if_source, what_if_decryptor = invention
        invented_me, invented_te, _runs = industry.invented_bpc(
            what_if_source.runs,
            what_if_decryptor.me_mod if what_if_decryptor else 0,
            what_if_decryptor.te_mod if what_if_decryptor else 0,
            what_if_decryptor.run_mod if what_if_decryptor else 0,
        )
        stored_me_te = me_te

        def me_te(blueprint_id: int, activity_id: int) -> tuple[int, int]:
            # The invented copy IS the final's manufacturing blueprint
            # (InventionSource.product_blueprint_id) — override only it.
            if (
                blueprint_id == what_if_source.product_blueprint_id
                and activity_id == config.ACTIVITY_MANUFACTURING
            ):
                return invented_me, invented_te
            return stored_me_te(blueprint_id, activity_id)

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

    # v1.22: an invention-enabled pipeline gets live invention lines at
    # continuous expectation (1/(P × runs × portion) attempts per unit)
    # instead of the bpc line. A stale config (resolve_invention: source
    # or decryptor no longer resolves) falls back to bpc_cost_isk,
    # matching the engine. A what-if `invention` pair replaces the stored
    # choice outright (compare window).
    resolved = (
        invention if invention is not None else resolve_invention(ref, pipeline)
    )
    if resolved is not None:
        source, decryptor = resolved
        cost = invention_cost(
            ref, settings, class_settings, source, decryptor,
            price_of=lambda t: landed_price(
                ref, settings, prices.get(t), venues.get(t), t
            ),
            adjusted_of=adjusted.get,
        )
        final_bp = ref.blueprint_for_product(pipeline["final_product_type_id"])
        lines.extend(
            _invention_cost_lines(
                ref,
                cost.probability,
                cost.runs_per_copy,
                final_bp.portion_size if final_bp else 1,
                cost.datacores,
                cost.decryptor.type_id if cost.decryptor else None,
                cost.decryptor_price,
                cost.invention_fee,
                cost.copy_fee,
            )
        )
    else:
        bpc = _bpc_line(pipeline)
        if bpc is not None:
            lines.append(bpc)
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
