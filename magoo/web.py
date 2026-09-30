"""Flask routes (PROJECT.md §3). Server-rendered Jinja2, no JS frameworks.

Pages: dashboard, pipelines, planning (steady-state cycle analysis, never
persisted), settings (globals + tracked systems + per-class build settings),
blueprints (ME/TE overrides), characters (pool + in-app SSO login), index
runs (list + detail with buy/build lists and multibuy export).
"""

import json
import logging
import os
import secrets as pysecrets
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import escape

import httpx

from magoo import __version__

from . import (
    bom,
    buying,
    config,
    costing,
    engine,
    esi,
    ledger,
    market,
    sdeimport,
    store,
    update,
)
from .refdata import Refdata

log = logging.getLogger(__name__)

STRUCTURE_CHOICES = (
    (None, "NPC Station"),
    (config.STRUCTURE_TYPE_RAITARU, "Raitaru"),
    (config.STRUCTURE_TYPE_AZBEL, "Azbel"),
    (config.STRUCTURE_TYPE_SOTIYO, "Sotiyo"),
    (config.STRUCTURE_TYPE_ATHANOR, "Athanor"),
    (config.STRUCTURE_TYPE_TATARA, "Tatara"),
)

def _sde_message(status: dict) -> str:
    """One human-readable line for an sdeimport.ImportJob status dict —
    the same wording serves the checklist's server-rendered initial state
    and the /sde/status poll."""
    state, stage = status.get("state"), status.get("stage")
    build = status.get("build", "?")
    if state == "error":
        return f"failed — {status.get('error', 'unknown error')}"
    if state == "done":
        if status.get("changed"):
            return f"build {build} imported"
        return f"build {build} — already up to date"
    if state != "running":
        return ""
    if stage == "download":
        done, total = status.get("done", 0), status.get("total", 0)
        if total:
            return f"downloading — {done / 1e6:,.0f} / {total / 1e6:,.0f} MB"
        return f"downloading — {done / 1e6:,.0f} MB"
    if stage == "import":
        return (
            f"importing — {status.get('dataset', '…')} "
            f"({status.get('step', 0)}/{status.get('steps', 8)})"
        )
    if stage == "finalize":
        return "finishing — recording the new build"
    if stage == "resolved":
        if status.get("outdated"):
            return (
                f"build {build} — game data predates this version, "
                "importing it again"
            )
        return f"build {build} — starting download"
    if stage == "current":
        return f"build {build} — already up to date"
    return "checking for a new build"


def _split_structure_components(builds_mfg_all, buys):
    """(struct_builds, struct_buys) — v1.9 gave structure components their
    own Plan/Chain section; since 2026-09-01 the built rows render as a
    "Structure Components" category group INSIDE Manufacturing (via
    _display_category) and only the bought rows keep a sub-table there
    (they stay in the Buy list / Multibuy too), so the split feeds the
    Mfg-slots sub-line and that sub-table. Slot totals must keep summing
    over builds_mfg_all: the split is display-only."""
    is_comp = lambda i: i["group_id"] in config.STRUCTURE_COMPONENT_GROUPS
    return (
        [i for i in builds_mfg_all if is_comp(i)],
        [i for i in buys if is_comp(i)],
    )


STRUCTURES_LABEL = "Upwell Structures"
STRUCTURE_MODULES_LABEL = "Structure Rigs & Modules"
STRUCTURE_COMPONENTS_LABEL = "Structure Components"
_UNRANKED = 20

# Buy tab group headings (v1.29). These are UI labels, NOT SDE group names
# (contract review A23: group 18 is "Mineral", 711 "Harvestable Cloud",
# 1136 "Fuel Block", and category 43 is "Planetary Commodities" spread
# over four tier groups) — the tab files a row under the family the user
# shops for, in the order they shop. Anything else keeps its EVE group
# name via _display_category and sorts alphabetically after these five.
BUY_GROUP_MINERALS = "Minerals"
BUY_GROUP_MOON = "Moon Materials"
BUY_GROUP_GAS = "Gas"
BUY_GROUP_PLANETARY = "Planetary Industry"
BUY_GROUP_FUEL = "Fuel Blocks"
# Planetary Commodities. config.py has no constant for it and no agent
# owns that file (A24), so the id lives here.
_CATEGORY_PLANETARY = 43
_BUY_GROUP_BY_GROUP = {
    config.COMPRESSED_MINERALS_GROUP: BUY_GROUP_MINERALS,
    config.COMPRESSED_MOON_GROUP: BUY_GROUP_MOON,
    config.COMPRESSED_GAS_SOURCE_GROUP: BUY_GROUP_GAS,
    **{g: BUY_GROUP_FUEL for g in config.FUEL_BLOCK_GROUPS},
}
# Buy-tab specific: _CATEGORY_RANK ranks the Plan tab's job sections and
# must not learn these.
_BUY_GROUP_RANK = {
    BUY_GROUP_MINERALS: 0,
    BUY_GROUP_MOON: 1,
    BUY_GROUP_GAS: 2,
    BUY_GROUP_PLANETARY: 3,
    BUY_GROUP_FUEL: 4,
}

_CATEGORY_RANK = {
    # Manufacturing section ordering
    "T1 Capital Ships": 0,
    "T2 Capital Ships": 1,
    "T1 Subcapital Ships": 2,
    "T2/T3 Subcapital Ships": 3,
    STRUCTURES_LABEL: 4,
    STRUCTURE_MODULES_LABEL: 5,
    # Reaction section ordering
    "Intermediate Materials": 10,
    "Composite": 11,
    "Hybrid Polymers": 12,
    "Molecular-Forged Materials": 13,
}


def _display_category(ref, type_id, group_id, category_id, group_name) -> str:
    """Ships split by tech level and scale (freighters and jump freighters
    count under the capital umbrella); Upwell structures and their
    rigs/modules collapse to one heading each (v1.9 — category 66 alone
    spans 100+ EVE groups); everything else shows its EVE group."""
    if category_id == config.CATEGORY_SHIP:
        advanced = (
            ref.attribute_by_name(type_id, config.ATTR_TECH_LEVEL, 1.0) >= 2.0
        )
        if group_id in config.EXACT_QTY_SHIP_GROUPS:
            return "T2 Capital Ships" if advanced else "T1 Capital Ships"
        return "T2/T3 Subcapital Ships" if advanced else "T1 Subcapital Ships"
    if category_id == config.CATEGORY_STRUCTURE:
        return STRUCTURES_LABEL
    if category_id == config.CATEGORY_STRUCTURE_MODULE:
        return STRUCTURE_MODULES_LABEL
    if group_id in config.STRUCTURE_COMPONENT_GROUPS:
        return STRUCTURE_COMPONENTS_LABEL
    return group_name


def _chain_status(x) -> str:
    """The Chain tab's status badge — the plan's DECISION for the row, not
    its shape (2026-08-23; it used to read build/react for anything
    buildable with a deficit, contradicting the Plan tab for intermediates
    the savings rule, blacklist or capacity had flipped to buy):
    alchemy route rows → 'alchemy'; a deficit met by jobs → 'build' /
    'react' (a partly-bought capacity loser keeps its build status and is
    badged '+buy'; a partly-built one with NO purchase fallback — a starved
    final or an unpriced intermediate — keeps it too and is badged
    '+unmet', see _chain_short); a deficit met by purchase → 'buy'; a
    composite whose deficit the alchemy route covers → 'alchemy'; a
    deficit nothing covers → 'unmet'; no deficit → 'covered'."""
    if x["alchemy_route"]:
        return "alchemy"
    if not (x["deficit"] and x["deficit"] > 0):
        return "covered"
    if x["build_qty"] > 0:
        return "react" if x["activity_id"] == 11 else "build"
    if x["buy_qty"] > 0 or x.get("compressed_covered", 0) > 0:
        # v1.25: a raw fully covered by compressed purchases is still a
        # purchase, not unmet demand.
        return "buy"
    if x["alchemy_out"] > 0:
        return "alchemy"
    return "unmet"


def _unmet_row(i) -> bool:
    """The Plan tab's Unmet list — the same rule as the Chain tab's
    _chain_short, over a persisted row: capacity-limited, and the jobs,
    the purchase and the alchemy route together still fall short of the
    deficit (v1.26: a capacity shortfall buys only from rungs the market
    holds, so a partly-bought row can be short too)."""
    deficit = i["deficit_qty"] or 0
    if not (i["capacity_limited"] and deficit > 0):
        return False
    return _unmet_qty(i) > 0


def _unmet_qty(i) -> int:
    """Units of a row's deficit nothing covers (0 when covered)."""
    covered = (
        (i["recommended_build_qty"] or 0)
        + (i["recommended_buy_qty"] or 0)
        + ((i["alchemy_output_qty"] or 0) if "alchemy_output_qty" in i.keys() else 0)
    )
    return max(0, (i["deficit_qty"] or 0) - covered)


def _run_pool(run, column: str, fallback: int) -> int:
    """The slot pool a persisted run was planned against (v1.27.1,
    schema 11), or the settings' pool for a run planned before it was
    recorded."""
    if column in run.keys() and run[column] is not None:
        return run[column]
    return fallback


def _jobs_to_run(i) -> int:
    """Jobs a row says to RUN NOW (v1.27.1, user ruling 2026-09-09): the
    install check's figure where the row carries one, else the plan's —
    the job tables, their section stats and the totals strip all read
    this, so the page adds up to what the user will actually install."""
    if "install_jobs" in i.keys() and i["install_jobs"] is not None:
        return int(i["install_jobs"])
    return int(i["jobs_allocated"] or 0)


def _build_qty_to_run(i) -> int:
    """Units the jobs to run now will build (_jobs_to_run's twin)."""
    if "install_runs" in i.keys() and i["install_runs"] is not None:
        return int(i["install_runs"]) * int(i["portion_size"] or 0)
    return int(i["recommended_build_qty"] or 0)


def _install_context(ref, settings_, items) -> dict:
    """run_detail's install-check derivations (v1.27.1, engine Phase 7.6)
    over the persisted rows: the materials whose planned draw exceeds
    what stock on hand, in-flight jobs and this cycle's buys hold (each
    with the cut consumers that eat it), the jobs installable now against
    the jobs planned, the finals in install-priority order with the
    persisted return on cost that ranked them, and a type-id → name
    lookup for the binding-material tooltips. A run planned before the
    check existed carries NULLs throughout and renders none of it."""

    def col(i, key):
        return i[key] if key in i.keys() else None

    names = {i["type_id"]: i["name"] for i in items}

    def name_of(type_id) -> str:
        if type_id in names:
            return names[type_id]
        if type_id is not None and type_id < 0:
            return "the slot pool — no free slot this cycle"
        try:
            return ref.type_info(type_id).name
        except KeyError:
            return f"type {type_id}"

    consumers = [i for i in items if col(i, "install_runs") is not None]
    # Every short input a cut consumer eats holds it back — not only the
    # tightest one its own tooltip names — so a short input lists all
    # the cut consumers that draw on it.
    limits: dict[int, list[str]] = {}
    for i in consumers:
        if (i["install_runs"] or 0) >= (i["runs_allocated"] or 0):
            continue
        if (i["install_limited_by"] or 0) < 0:
            continue  # stopped by the slot pool, not by an input
        for m, _q in ref.materials(i["blueprint_id"], i["activity_id"]):
            limits.setdefault(m, []).append(i["name"])
    short = []
    for i in items:
        units = col(i, "install_short_qty")
        if not units:
            continue
        # Units no stored sell order held are not bought (the unsourced
        # badge) — the engine leaves them out of availability.
        unsourced = col(i, "unfilled_qty") or 0
        bought = (i["recommended_buy_qty"] or 0) - unsourced
        covered = col(i, "compressed_covered_qty") or 0
        short.append(
            {
                "row": i,
                "name": i["name"],
                "draw": i["install_draw_qty"] or 0,
                "on_hand": i["on_hand_qty"] or 0,
                "in_jobs": i["in_progress_qty"] or 0,
                "bought": bought,
                "unsourced": unsourced,
                "covered": covered,
                "available": (i["on_hand_qty"] or 0)
                + (i["in_progress_qty"] or 0)
                + bought
                + covered,
                "short": units,
                "limits": limits.get(i["type_id"], []),
            }
        )
    finals = [
        {
            "row": i,
            "name": i["name"],
            "priority": i["install_priority"],
            # The figure the engine ranked on, persisted with the row —
            # never recomputed here (a capital final's sell reference is
            # the structure quote the run route handed the snapshot).
            "return": col(i, "install_return"),
            # Inputs the chain cost priced at 0 (the 'N unpriced' badge):
            # the return reads high, and the engine ranks such a final
            # after every fully priced one.
            "unpriced": col(i, "savings_unpriced_inputs") or 0,
            "bound": (
                name_of(i["install_limited_by"])
                if i["install_limited_by"] is not None
                else None
            ),
        }
        for i in sorted(
            (i for i in consumers if col(i, "install_priority")),
            key=lambda i: i["install_priority"],
        )
    ]
    return dict(
        install_checked=bool(consumers),
        install_short=short,
        install_jobs_now=sum(i["install_jobs"] or 0 for i in consumers),
        install_jobs_planned=sum(i["jobs_allocated"] or 0 for i in consumers),
        install_finals=finals,
        type_names=names,
        name_of=name_of,
    )


def _chain_short(x) -> bool:
    """Unmet demand on a row that still reads build/react: capacity-limited
    and the jobs plus whatever the market could supply fall short of
    the deficit — the Plan tab's "Unmet" list, so the two tabs count the
    same items. v1.26: a capacity shortfall buys only from rungs the
    market holds, so a partly-bought row can still be short."""
    if not (x["capacity_limited"] and x["deficit"] and x["deficit"] > 0):
        return False
    covered = x["build_qty"] + x["buy_qty"] + (x.get("alchemy_out") or 0)
    if covered <= 0:
        return False  # nothing planned at all: the status 'unmet' covers it
    return covered < x["deficit"]


def _installed_qty(i) -> int:
    """Units of the row's type installed this cycle (index_run_item.
    installed_qty, v1.29 revision 7); 0 on a row planned before the column
    (NULL) and on a _steady_rows dict."""
    if "installed_qty" not in i.keys():
        return 0
    return int(i["installed_qty"] or 0)


def _job_rows(items, activity_id, final_ids) -> list:
    """The Industry Jobs table rows for one activity (1 manufacturing, 11
    reactions; alchemy rows have their own section): every row that builds,
    plus — revision 7, contract review amendment 9 — a final whose wave is
    already installed this cycle (installed_qty > 0) and so builds nothing
    more. It stays listed with no job to run and its "installed N/M" badge;
    it adds nothing to the slot counts or the section's build value.
    Keeps the rows' own order (depth, then name)."""
    return [
        i
        for i in items
        if i["activity_id"] == activity_id
        and not i["alchemy_for_type_id"]
        and (
            (i["recommended_build_qty"] or 0) > 0
            or (i["type_id"] in final_ids and _installed_qty(i) > 0)
        )
    ]


def _group_by_category(ref, rows) -> list:
    """[(display category, rows)] ranked by _CATEGORY_RANK — the grouping
    the Plan-view job tables share (run_detail and the Planning tab)."""
    grouped: dict[str, list] = {}
    for row in rows:
        label = _display_category(
            ref,
            row["type_id"],
            row["group_id"],
            row["category_id"],
            row["category"],
        )
        grouped.setdefault(label, []).append(row)
    return sorted(
        grouped.items(),
        key=lambda entry: (_CATEGORY_RANK.get(entry[0], _UNRANKED), entry[0]),
    )


def _steady_rows(ref, plan) -> list[dict]:
    """A live Plan's items as the dict rows the plan templates read (the
    persisted-column shape run_detail gets from its SQL join) — the
    Planning tab renders a plan that was never written, so the ref
    name/group join happens here instead. Ordered like the join:
    depth, then name."""
    group_ids = {
        ref.type_info(item.type_id).group_id for item in plan.items.values()
    }
    group_names = {}
    if group_ids:
        marks = ",".join("?" * len(group_ids))
        group_names = {
            row["group_id"]: row["name"]
            for row in ref.conn.execute(
                "SELECT group_id, name FROM ref_group "
                f"WHERE group_id IN ({marks})",
                tuple(group_ids),
            )
        }
    rows = []
    for item in sorted(plan.items.values(), key=lambda i: (i.depth, i.name)):
        info = ref.type_info(item.type_id)
        rows.append(
            {
                "type_id": item.type_id,
                "name": item.name,
                "group_id": info.group_id,
                "category_id": info.category_id,
                "category": group_names.get(info.group_id, ""),
                "depth": item.depth,
                "activity_id": item.activity_id,
                "blueprint_id": item.blueprint_id,
                "portion_size": item.portion_size,
                "merged_min_qty": item.merged_min_qty,
                "cycle_need_qty": item.cycle_need_qty,
                "target_stock_qty": item.target_stock_qty,
                "deficit_qty": item.deficit_qty,
                "recommended_build_qty": item.recommended_build_qty,
                "recommended_buy_qty": item.recommended_buy_qty,
                "jobs_allocated": item.jobs_allocated,
                "jobs_needed_unconstrained": item.jobs_needed_unconstrained,
                "total_runs_needed": item.total_runs_needed,
                "runs_allocated": item.runs_allocated,
                "max_runs_per_job": item.max_runs_per_job,
                "time_per_run": item.time_per_run,
                "capacity_limited": item.capacity_limited,
                "market_buy_qty": item.market_buy_qty,
                "market_fallback_qty": item.market_fallback_qty,
                "savings_unpriced_inputs": item.savings_unpriced_inputs,
                "price_snapshot": item.price_snapshot,
                "price_region_wide": item.price_region_wide,
                "buy_venue": item.buy_venue,
                "structure_units_cheaper": item.structure_units_cheaper,
                "alchemy_for_type_id": item.alchemy_for_type_id,
                "direct_unit_cost": item.direct_unit_cost,
                "alchemy_unit_cost": item.alchemy_unit_cost,
                # v1.25: the steady-state path never runs the compressed
                # pass; the keys exist so _buy_context sees one shape.
                "compressed_outputs": None,
                "compressed_ladder_units": None,
                "compressed_fill_orders": None,
                "compressed_wanted_qty": None,
                "compressed_covered_qty": 0,
                "effective_unit_cost": None,
                "hub_buy_qty": None,
                "hub_fill_price": None,
                "hub_fill_orders": None,
                "structure_buy_qty": None,
                "structure_fill_price": None,
                "structure_fill_orders": None,
                "unfilled_qty": 0,
                "unfilled_price": None,
                # v1.27.1: the install check runs on the steady plan too
                # (its stocked draft rations the saturating reactions,
                # whose inputs sit at one cycle's target), but the Slot
                # Planner is a what-if with no stock of its own and shows
                # the PLAN — so the figures are dropped here, not carried:
                # _jobs_to_run and the section stats then read the plan's
                # jobs. The keys exist so the rows keep one shape.
                "install_runs": None,
                "install_jobs": None,
                "install_per_job": None,
                "install_limited_by": None,
                "install_priority": None,
                "install_return": None,
                "install_draw_qty": None,
                "install_short_qty": None,
                # v1.29 revision 7: the Slot Planner's steady plan passes
                # no cycle cut, so nothing counts as installed this cycle
                # (contract review amendment 9: both row shapes match).
                "installed_qty": 0,
                # Revision 7 fix pass: the persisted wave columns, for the
                # same one-shape reason (nothing installed: the wave is
                # the request).
                "requested_qty": item.requested_qty,
                "wave_qty": item.wave_qty,
            }
        )
    return rows


def _steady_demand(rows, activity_id) -> int:
    """Uncapped slot demand for one activity: jobs the plan builds (or
    would build but for the slot pool — capacity-limited rows count at
    their unconstrained size). Deliberate buys (savings rule, blacklist)
    are not demand. Steady plans carry no alchemy rows (alchemy is
    assumed off for planning); the filter below guards it anyway."""
    return sum(
        i["jobs_needed_unconstrained"]
        for i in rows
        if i["activity_id"] == activity_id
        and not i["alchemy_for_type_id"]
        and (i["recommended_build_qty"] > 0 or i["capacity_limited"])
    )


def _buy_context(rows, ref=None) -> dict:
    """The buy-side derivations run_detail and _planning_context share:
    the buy list and its total, plan-time venue provenance (structure vs
    hub, thin structure ladders shown as a Jita share, region-wide
    fallbacks), the structure-component
    split, and the per-venue Multibuy blocks. Rows are either sqlite rows
    (NULL-able columns) or _steady_rows dicts (never None) — the `or 0`
    guards cover both shapes identically."""
    buys = [i for i in rows if (i["recommended_buy_qty"] or 0) > 0]
    builds_mfg_all = [
        i
        for i in rows
        if (i["recommended_build_qty"] or 0) > 0 and i["activity_id"] == 1
    ]
    struct_builds, struct_buys = _split_structure_components(
        builds_mfg_all, buys
    )
    # v1.10: plan-time buy venue per row (NULL on pre-v1.10 rows = hub).
    # v1.25 fill pricing: a row walked on its ladders carries per-venue
    # quantities and may be SPLIT; a legacy row (hub_buy_qty NULL) puts
    # its whole quantity on its buy_venue. One Multibuy block per venue.
    def venue_split(i) -> tuple[int, int]:
        if "hub_buy_qty" in i.keys() and i["hub_buy_qty"] is not None:
            return int(i["hub_buy_qty"] or 0), int(i["structure_buy_qty"] or 0)
        qty = int(i["recommended_buy_qty"] or 0)
        if i["buy_venue"] == store.BUY_VENUE_STRUCTURE:
            # v1.26.1: a single-quote structure row whose ladder held
            # fewer units beating Jita than the plan buys sends the rest
            # to Jita — the venue cell and Multibuy say so (it used to be
            # a 'shallow' / 'thin book' badge with the rest left implicit).
            cheaper = i["structure_units_cheaper"]
            if cheaper is not None and 0 < cheaper < qty:
                return qty - int(cheaper), int(cheaper)
            return 0, qty
        return qty, 0

    venue_qty = {i["type_id"]: venue_split(i) for i in buys}
    structure_buys = {t for t, (_h, s) in venue_qty.items() if s > 0}
    split_buys = {t for t, (h, s) in venue_qty.items() if h > 0 and s > 0}
    # 'unsourced' (badge text since v1.26.1; 'shallow' before): a
    # fill-priced row whose stored ladders ran out — the rest
    # (unfilled_qty) has no market: priced at the last rung walked, in
    # no venue quantity and no Multibuy block (review 2026-09-05, R5; the
    # engine folds a remainder into the Jita quantity only when Jita's
    # stored book was truncated at HUB_LADDER_MAX_RUNGS, so its real book
    # continues) — invariant hub_buy_qty + structure_buy_qty +
    # unfilled_qty == recommended_buy_qty. The single-quote case (a
    # structure row whose ladder held fewer units beating Jita than the
    # plan buys) is no badge at all: venue_split above sends the rest to
    # Jita, so the venue cell and Multibuy show it as a split.
    unsourced = {
        i["type_id"]
        for i in buys
        if "hub_buy_qty" in i.keys()
        and i["hub_buy_qty"] is not None
        and (i["unfilled_qty"] or 0) > 0
    }
    # v1.25 compressed sourcing: the compressed buy rows (what each is
    # for, decoded), the raws they part-cover, and compressed rows whose
    # fill outran their venue's ladder at plan time.
    compressed = {}
    for i in buys:
        raw = i["compressed_outputs"] if "compressed_outputs" in i.keys() else None
        if raw:
            outputs = json.loads(raw) if isinstance(raw, str) else list(raw)
            # (material_id, name, units out, units used) — names resolved
            # here so the badge and the reprocess checklist share them.
            compressed[i["type_id"]] = [
                (
                    int(m),
                    ref.type_info(int(m)).name if ref is not None else str(m),
                    int(out),
                    int(used),
                )
                for m, out, used in outputs
            ]
    compressed_covered = {
        i["type_id"]: i["compressed_covered_qty"]
        for i in rows
        if "compressed_covered_qty" in i.keys()
        and (i["compressed_covered_qty"] or 0) > 0
    }
    # compressed_wanted_qty (ruling R6, 2026-09-05) once drove a badge for
    # a compressed buy shrunk below what the LP wanted; since v1.26.1 the
    # pass re-solves after pinning a type to its market's depth, so the
    # two are equal on new rows. Older rows show the figure in the
    # compressed badge's tooltip (_macros.compressed_badge) — no badge.
    return dict(
        buys=buys,
        compressed=compressed,
        compressed_covered=compressed_covered,
        buy_total=sum(
            (i["recommended_buy_qty"] or 0) * (i["price_snapshot"] or 0)
            for i in buys
        ),
        # Unpriced buy rows contribute 0 above — the total understates and
        # must say so (mirrors the profit pages' "N unpriced" badge).
        buys_unpriced=sum(1 for i in buys if i["price_snapshot"] is None),
        structure_buys=structure_buys,
        split_buys=split_buys,
        venue_qty=venue_qty,
        unsourced=unsourced,
        # Plan-time provenance of price_snapshot: which bought inputs were
        # priced from a region-wide fallback.
        region_wide={i["type_id"] for i in buys if i["price_region_wide"]},
        multibuy_hub="\n".join(
            f"{i['name']} {venue_qty[i['type_id']][0]}"
            for i in buys if venue_qty[i["type_id"]][0] > 0
        ),
        multibuy_structure="\n".join(
            f"{i['name']} {venue_qty[i['type_id']][1]}"
            for i in buys if venue_qty[i["type_id"]][1] > 0
        ),
        struct_builds=struct_builds,
        struct_buys=struct_buys,
        struct_slots=sum(_jobs_to_run(i) for i in struct_builds),
        builds_mfg_all=builds_mfg_all,
    )


def _planning_context(ref, plan, settings_) -> dict:
    """Template context for the Slot Planner view, from a live
    (never-persisted) steady-state Plan. Shares the buy-side derivations
    with run_detail via _buy_context; everything stock- or
    wallet-dependent (low stock, wallets, multibuy) is absent here, and
    alchemy too — plan_steady_state plans direct reactions only."""
    rows = _steady_rows(ref, plan)
    bc = _buy_context(rows, ref)
    builds_reaction = [
        i
        for i in rows
        if i["recommended_build_qty"] > 0 and i["activity_id"] == 11
    ]
    return dict(
        rows=rows,
        reason=None,
        buys=bc["buys"],
        buy_total=bc["buy_total"],
        buys_unpriced=bc["buys_unpriced"],
        structure_buys=bc["structure_buys"],
        split_buys=bc["split_buys"],
        venue_qty=bc["venue_qty"],
        unsourced=bc["unsourced"],
        region_wide=bc["region_wide"],
        compressed=bc["compressed"],
        compressed_covered=bc["compressed_covered"],
        builds=bc["builds_mfg_all"],
        reactions=builds_reaction,
        builds_grouped=_group_by_category(ref, bc["builds_mfg_all"]),
        reactions_grouped=_group_by_category(ref, builds_reaction),
        struct_builds=bc["struct_builds"],
        struct_buys=bc["struct_buys"],
        struct_slots=bc["struct_slots"],
        capacity_rows=[i for i in rows if i["capacity_limited"]],
        unmet_qty=unmet_qty if "unmet_qty" in dir() else {},
        mfg_demand=_steady_demand(rows, config.ACTIVITY_MANUFACTURING),
        reaction_demand=_steady_demand(rows, config.ACTIVITY_REACTION),
        settings=settings_,
    )


def _alchemy_section(ref, settings_, items) -> list[dict]:
    """run_detail's Alchemy section rows: the reaction install plus the
    manual reprocess step and the cost comparison that justified it."""
    routes = ref.alchemy_routes()
    by_type = {i["type_id"]: i for i in items}
    alchemy = []
    for i in items:
        composite_id = i["alchemy_for_type_id"]
        if not composite_id or (i["recommended_build_qty"] or 0) <= 0:
            continue
        route = routes.get(composite_id)
        composite = by_type.get(composite_id)
        # v1.27.1: the section describes the jobs to RUN NOW — a row the
        # install check cut reprocesses only what those jobs deliver, so
        # the checklist, the expected composite and the savings follow
        # the install quantity, not the plan's.
        qty = _build_qty_to_run(i)
        cut = qty < (i["recommended_build_qty"] or 0)
        yield_ = settings_.alchemy_reprocess_yield
        # What the route's units replace (2026-09-12): a swap replaces the
        # direct reaction, a buy move the composite's landed purchase. The
        # plan does not persist the split, so a composite bought outright
        # is measured against its landed price and any other against the
        # cheaper of the two — the Savings figure never overstates.
        direct = composite["direct_unit_cost"] if composite else None
        bought = (
            costing.landed_price(
                ref, settings_, composite["price_snapshot"],
                composite["buy_venue"], composite_id,
            )
            if composite is not None and composite["price_snapshot"] is not None
            else None
        )
        outright = composite is not None and (composite["jobs_allocated"] or 0) <= 0
        if outright and bought is not None:
            benchmark, benchmark_label = bought, "landed buy"
        elif direct is not None and bought is not None and bought < direct:
            benchmark, benchmark_label = bought, "landed buy (cheaper than the direct reaction)"
        else:
            benchmark, benchmark_label = direct, "direct"
        alchemy.append(
            {
                "item": i,
                "build_qty": qty,
                "composite_name": ref.type_info(composite_id).name,
                # Prefer the engine's persisted per-job-floored figure
                # (what the Chain tab shows); the one-shot recomputation
                # here floors once over the total and can disagree. A
                # cut row has no persisted figure for its install
                # quantity, so it takes the recomputation.
                "expected_qty": (
                    composite["alchemy_output_qty"]
                    if composite is not None
                    and composite["alchemy_output_qty"]
                    and not cut
                    else (
                        int(qty * route.composite_qty * yield_)
                        if route
                        else 0
                    )
                ),
                "recovered": [
                    (ref.type_info(m).name, int(qty * q * yield_))
                    for m, q in (route.recovered if route else ())
                ],
                "benchmark_unit": benchmark,
                "benchmark_label": benchmark_label,
                "alchemy_unit": (
                    composite["alchemy_unit_cost"] if composite else None
                ),
            }
        )
    return alchemy


# --- Buy tab (v1.29, revision 3) -------------------------------------------
#
# What this cycle's inputs cost to BUY, what the plan counted on hand and
# what the pool purchased — Required · On Hand · Remaining · Purchased ·
# Ladder · Δ since revision 6 (user ruling R4 2026-09-28; the open run is
# re-planned in place on every ESI update, so nothing is subtracted from
# the plan here). Nothing is entered (user rulings 2026-09-28, R1): the
# ESI update
# stores the pool's wallet buys and the item exchanges it accepted, and
# buying.assign_purchases files them as run_purchase lines under the buying
# cycle they fall in (R2). The plan's own columns are never rewritten; the
# lines win inside the realized-costing snapshot loader, so a run with no
# purchases costs exactly what it did before v1.29.
#
# Every per-row Purchased / Ladder / Δ figure is costing.bought_cell — the
# blend_purchases arithmetic and the run's persisted freight rates realized
# costing lands a purchase with — so the two cannot disagree on what a line
# cost landed (contract review C14.5). They are R8's per-unit cells, not the
# realized change: that also depends on the costing basis and, for a covered
# raw, on the ore's reallocation, and the page never claims otherwise.


def _type_name(ref, type_id) -> str:
    """A type's name, or "type N" for one the local game data does not
    know (review 2026-09-28, P0). ESI purchases are stored whatever their
    type, and CCP adds items faster than the SDE import catches up: an
    unguarded type_info on the Buy tab turned one such purchase into a
    500 on every view of the run until a re-import. ledger._type_name is
    the Ledger's twin."""
    try:
        return ref.type_info(type_id).name
    except KeyError:
        return f"type {type_id}"


def _row_value(row, name):
    """One column of a sqlite3.Row / mapping, None when it has no such
    key (render tests hand these helpers bare dicts)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    return row[name] if name in keys else None


def _run_buy_rates(run, settings_) -> dict[str, float]:
    """The inbound ISK/m³ rates this run's buys are landed at, per venue:
    the rates the run was PLANNED at, falling back to the live setting
    where a run persisted before those columns carries NULL — the same
    resolution costing._run_freight_rates + Settings.freight_in_rate
    make. A delivered price is already landed and hauls nothing (A14).

    Revision 4 (user ruling 2026-09-28): a purchase anywhere but Jita 4-4
    and the configured structure market — another station or structure,
    a contract handed over elsewhere — hauls at the default inbound rate
    (store.BUY_VENUE_OTHER), persisted per run the same way."""

    def rate(column, venue):
        value = _row_value(run, column)
        return value if value is not None else settings_.freight_in_rate(venue)

    return {
        store.BUY_VENUE_HUB: rate("freight_in_isk_per_m3", store.BUY_VENUE_HUB),
        store.BUY_VENUE_STRUCTURE: rate(
            "structure_freight_in_isk_per_m3", store.BUY_VENUE_STRUCTURE
        ),
        store.BUY_VENUE_OTHER: rate(
            "freight_in_default_isk_per_m3", store.BUY_VENUE_OTHER
        ),
        store.BUY_VENUE_DELIVERED: 0.0,
    }


def _buy_group(ref, row, covers) -> str:
    """The Buy tab heading one bought row files under. A compressed ore /
    gas row joins the group of the RAWS it covers (contract review A25:
    config.COMPRESSED_* are the raw groups — a compressed ore's own group
    is its ore group under category 25), the largest covered quantity
    winning, lowest group id breaking a tie."""
    tally: dict[int, int] = {}
    for material_id, _name, _out, used in covers or ():
        if used > 0:
            group = ref.type_info(material_id).group_id
            tally[group] = tally.get(group, 0) + used
    if tally:
        group = max(tally.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        label = _BUY_GROUP_BY_GROUP.get(group)
        if label:
            return label
    label = _BUY_GROUP_BY_GROUP.get(row["group_id"])
    if label:
        return label
    if row["category_id"] == _CATEGORY_PLANETARY:
        return BUY_GROUP_PLANETARY
    return _display_category(
        ref,
        row["type_id"],
        row["group_id"],
        row["category_id"],
        row["category"],
    )


def _ore_shares_any(ore) -> bool:
    """Does this compressed row allocate any of its landed cost to a raw?

    True for a pre-v1.29 row (no compressed_alloc at all — the covered
    raws' effective_unit_cost still carries every ISK of it). False only
    for the engine's degenerate pick, whose outputs were worth nothing at
    plan time: it stores share 0.0 for every raw, so its landed ISK
    belongs to no row of this tab and the strip has to say so (B14)."""
    blob = _row_value(ore, "compressed_alloc")
    if not blob:
        return True
    try:
        alloc = json.loads(blob) if isinstance(blob, str) else blob
        return any(float(v) > 0 for v in alloc.values())
    except (TypeError, ValueError, AttributeError):
        return True


def _multibuy_text(entries, venue_key) -> str:
    """One Multibuy paste block: `name qty` per line, a SPACE, exactly as
    _buy_context builds the plan's own blocks (B34). `venue_key` names the
    quantity: a market's share for Multibuy All, `remaining` for a group's
    plain list (revision 8)."""
    return "\n".join(
        f"{e['name']} {e[venue_key]}" for e in entries if e[venue_key] > 0
    )


def _group_multibuy(group) -> dict:
    """One group's Multibuy: ONE plain list of what its rows still need.

    v1.29 revision 8 (user ruling R2 2026-09-29: "the straight no
    compression, just the item list with no Jita vs C-J6 differing
    lists"): one line per row whose Remaining > 0, quantity = Remaining,
    in the table's order, in _multibuy_text's `name qty` format. It is
    the raw item itself — a compressed-covered raw lists its direct AND
    covered units, as if bought straight, and no compressed ore is a line
    — and it is not split by market. Unsourced units are part of
    Remaining, so they are in it too (the row's `unsourced` badge says
    they had no market at plan time).

    Multibuy All keeps the plan's sell-ladder buy — the two per-market
    blocks with the ore lines (revision 6, contract review A15) — so it
    still reads `multibuy_ores`: every compressed ore the plan picked, in
    the ONE group _buy_group files it under (B36), so it is pasted once
    and in the groups' order. That is the ores' only use: revision 8's
    R2b (user ruling 2026-09-29: "the only place that matters is the
    multibuy all") removed the group's own ore table, so a group renders
    no ore as a row, a cell or a Multibuy line — what was paid for one
    stays in the Purchased totals and the Purchases section. A group
    names an ore only in tooltips: the covered raw's "N via compressed"
    tag (the ores the plan picked) and the Purchased title's via-compressed
    clause (an ore the pool bought that the plan did not pick,
    _line_sources)."""
    listed = [r for r in group["rows"] if r["remaining"] > 0]
    return {
        "multibuy": _multibuy_text(listed, "remaining"),
        "multibuy_items": len(listed),
        "multibuy_ores": list(group["ores"]),
    }


def _owner_names(conn) -> dict[tuple[str, int], str]:
    """{(owner_kind, owner_id): name} for the Purchases section — the pool's
    characters and the corporations the ESI refresh recorded."""
    names: dict[tuple[str, int], str] = {}
    for r in conn.execute(
        "SELECT character_id, character_name FROM pool_character"
    ):
        names[("character", r["character_id"])] = r["character_name"]
    for r in conn.execute(
        "SELECT corporation_id, corporation_name FROM esi_corp"
    ):
        if r["corporation_name"]:
            names[("corporation", r["corporation_id"])] = r["corporation_name"]
    return names


def _bought_contracts(conn, contract_ids) -> dict[int, sqlite3.Row]:
    """buy_contract rows by id, for the rows' `contract` badges (title,
    k)."""
    ids = sorted({int(i) for i in contract_ids})
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    return {
        r["contract_id"]: r
        for r in conn.execute(
            f"SELECT * FROM buy_contract WHERE contract_id IN ({marks})", ids
        )
    }


def _contract_label(contract, contract_id) -> str:
    """How a bought contract is named on the page: its title, else its id."""
    title = _row_value(contract, "title") if contract is not None else None
    return f"“{title}”" if title else f"#{contract_id}"


def _location_names(conn, ref, location_ids) -> dict[int, str]:
    """{location_id: how the page names it} for purchase locations (revision
    4, user ruling 2026-09-28: a purchase made elsewhere names where).

    The Ledger resolves a station or structure to its SOLAR SYSTEM only —
    location_system has no name column and the SDE import keeps no
    station table (contract review A12) — so a location is named by its
    system, or "location <id>" while the ESI pull has not resolved it
    (resolution is best-effort: a structure without docking rights never
    resolves). ledger._system_name is the Ledger's twin."""
    ids = sorted({int(i) for i in location_ids if i is not None})
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    systems = {
        r["location_id"]: r["solar_system_id"]
        for r in conn.execute(
            "SELECT location_id, solar_system_id FROM location_system "
            f"WHERE location_id IN ({marks})",
            ids,
        )
    }
    out: dict[int, str] = {}
    for location_id in ids:
        name = None
        system_id = systems.get(location_id)
        if system_id:
            row = ref.solar_system(int(system_id))
            name = row["name"] if row is not None else None
        out[location_id] = name or f"location {location_id}"
    return out


def _line_locations(conn, ref, lines, contracts) -> dict[tuple, str]:
    """{(esi_kind, esi_id): location name} for a run's wallet-buy lines
    bought at neither market (venue 'other') and for every bought contract
    — the badge titles name where those were handed over. run_purchase
    stores no location, so it is read back by esi_id: buy_transaction's
    location_id, or the contract's start_location_id on the row
    _bought_contracts loaded (contract review A12)."""
    tx_ids = sorted({
        int(_row_value(line, "esi_id"))
        for line in lines
        if _row_value(line, "esi_kind") == store.ESI_KIND_TRANSACTION
        and line["venue"] == store.BUY_VENUE_OTHER
    })
    where: dict[tuple, int | None] = {}
    if tx_ids:
        marks = ", ".join("?" * len(tx_ids))
        for r in conn.execute(
            "SELECT transaction_id, location_id FROM buy_transaction "
            f"WHERE transaction_id IN ({marks})",
            tx_ids,
        ):
            where[(store.ESI_KIND_TRANSACTION, r["transaction_id"])] = r["location_id"]
    for contract_id, contract in contracts.items():
        where[(store.ESI_KIND_CONTRACT, contract_id)] = _row_value(
            contract, "start_location_id"
        )
    names = _location_names(conn, ref, where.values())
    return {
        key: names[int(location_id)]
        for key, location_id in where.items()
        if location_id is not None
    }


def _line_sources(lines, contracts, structure_label, where=None,
                  type_name=None) -> list[dict]:
    """The sources of one row's Purchased cell (R8): Jita / the structure
    market / `elsewhere` for wallet buys, `contract` for an accepted item
    exchange — with the contract's title (or id), its k and where it was
    handed over — and `via compressed` for the units the unplanned
    compressed ores refine into. One entry per distinct source, units
    summed.

    Revision 8 (user ruling R1 2026-09-29: "for the purchased column,
    remove the tags"): the cell renders no badge any more — each entry is
    one clause of the cell's title (units, label, title). The entries,
    and the revision 4/5 rules below, are unchanged.

    Revision 4 (user ruling 2026-09-28):
    - a line carrying via_type_id is tested FIRST — the matcher copies the
      ore purchase's esi_kind onto it, so a converted contract item would
      otherwise fold into that contract's badge (contract review A12). Its
      title states the units and the landed ISK from that ore, summed
      from the lines themselves: they are stored landed, so the refining
      tax is inside and not separable (A13);
    - venue 'other' is its own `elsewhere` badge, titled with where the
      purchases were made (`where`: _line_locations) — before revision 4
      it folded into Jita and claimed the hub rate.

    Revision 5 (user ruling 2026-09-28: the tables were stretched): every
    via line folds into ONE `via compressed` badge, however many ores fed
    it; its title names each ore with its units and landed ISK. The venue
    badges (Jita / the structure market / elsewhere / contract) keep one
    each.

    `type_name` names the ore (the tab's _type_name over its ref)."""
    where = where or {}
    out: dict[tuple, dict] = {}
    for line in lines:
        kind = _row_value(line, "esi_kind")
        esi_id = _row_value(line, "esi_id")
        units = int(line["quantity"])
        via = _row_value(line, "via_type_id")
        if via is not None:
            entry = out.setdefault(("via",), {
                "label": "via compressed", "tone": "accent", "units": 0,
                "title": "", "_ores": {},
            })
            ore = entry["_ores"].get(int(via))
            if ore is None:
                ore = entry["_ores"][int(via)] = {
                    "name": (
                        type_name(int(via)) if type_name else f"type {via}"
                    ),
                    "units": 0, "isk": 0.0, "records": set(),
                }
            entry["units"] += units
            ore["units"] += units
            ore["isk"] += units * float(line["unit_price"])
            ore["records"].add((kind, esi_id))
            continue
        venue = line["venue"]
        if kind == store.ESI_KIND_CONTRACT:
            key = ("contract", esi_id)
            if key not in out:
                contract = contracts.get(esi_id)
                k = _row_value(line, "contract_k")
                if venue == store.BUY_VENUE_STRUCTURE:
                    handed = (
                        f"handed over at the {structure_label} structure "
                        "market — landed at the structure rate"
                    )
                elif venue == store.BUY_VENUE_OTHER:
                    handed = (
                        "handed over in "
                        + where.get((kind, esi_id), "an unknown location")
                        + ", at neither Jita 4-4 nor the structure market — "
                        "landed at the default inbound rate"
                    )
                else:
                    handed = "handed over at Jita 4-4 — landed at the hub rate"
                out[key] = {
                    "label": "contract",
                    "tone": "accent",
                    "units": 0,
                    "title": (
                        f"item exchange {_contract_label(contract, esi_id)}"
                        + (
                            f", k = {k:.4f} — its price spread over the items "
                            "received at their Jita reference prices (R7)"
                            if k is not None else ""
                        )
                        + f"; {handed}"
                    ),
                }
            out[key]["units"] += units
            continue
        if venue == store.BUY_VENUE_STRUCTURE:
            key, label = ("venue", venue), structure_label
            title = (
                f"wallet buys at the {structure_label} structure market — "
                "landed at the structure rate"
            )
        elif venue == store.BUY_VENUE_DELIVERED:
            key, label = ("venue", venue), "delivered"
            title = "bought delivered — the price is already landed"
        elif venue == store.BUY_VENUE_OTHER:
            key, label = ("venue", venue), "elsewhere"
            title = ""        # set below, once every location is known
        else:
            key, label = ("venue", store.BUY_VENUE_HUB), "Jita"
            title = "wallet buys at Jita 4-4 — landed at the hub rate"
        entry = out.setdefault(key, {
            "label": label, "tone": "", "units": 0,
            "title": (
                "recorded by hand before v1.29 revision 3 read purchases "
                "from ESI" if kind is None else title
            ),
            "_where": set(),
        })
        entry["units"] += units
        if venue == store.BUY_VENUE_OTHER:
            entry["_where"].add(where.get((kind, esi_id), "an unknown location"))
    for entry in out.values():
        if "_ores" in entry:
            parts = []
            for ore in sorted(entry["_ores"].values(),
                              key=lambda o: o["name"]):
                n = len(ore["records"])
                parts.append(
                    f"{ore['units']:,} units refined out of {ore['name']} "
                    + (f"from {n} purchases " if n > 1 else "")
                    + f"— {ore['isk']:,.0f} ISK landed"
                )
            entry["title"] = (
                "; ".join(parts)
                + ". Landed is what the ore cost for the whole batches "
                "refined, its inbound freight and the refining tax, spread "
                "over what it yields by value at this run's plan prices. The "
                "plan did not pick "
                + ("this ore" if len(parts) == 1 else "these ores")
                + ", so the purchase counts as what it refines into (user "
                "ruling 2026-09-28)"
            )
        elif entry.get("_where") and not entry["title"]:
            entry["title"] = (
                "wallet buys in " + ", ".join(sorted(entry["_where"]))
                + " — at neither Jita 4-4 nor the structure market, so "
                "landed at the default inbound rate (Settings)"
            )
    return [
        {k: v for k, v in entry.items() if not k.startswith("_")}
        for entry in out.values()
    ]


def _venue_label(venue, structure_label) -> str:
    return (
        structure_label if venue == store.BUY_VENUE_STRUCTURE
        else "delivered" if venue == store.BUY_VENUE_DELIVERED
        else "elsewhere" if venue == store.BUY_VENUE_OTHER
        else "Jita"
    )


# Why a listed purchase writes no line, keyed by buying's record status:
# (badge tone, tooltip). Internal transfers and a switched-off owner are
# bookkeeping, not trouble — neutral; the rest want the user's eye.
_RECORD_STATUS_NOTE = {
    "internal": (
        "", "a transfer from one of your own characters or corporations — "
        "not a purchase, so it writes no line: the units were costed where "
        "they were first bought (contract review C9)",
    ),
    "buys_off": (
        "", "this owner's Count buys is off (ESI tab) — stored, not counted",
    ),
    "swap": (
        "warn", "items went both ways, or it cost nothing — not a purchase, "
        "not costed",
    ),
    "no_price": (
        "warn", "none of the items received has a Jita sell or CCP adjusted "
        "price on record, so its price cannot be spread over them — not "
        "costed; the next ESI update tries again",
    ),
    "no_items": (
        "warn", "its item list has not been read from ESI yet — the next "
        "update reads it",
    ),
    "pending": (
        "warn", "not priced yet — the next ESI update prices it",
    ),
}


def _refined_notes(record, ores, ref, in_plan=frozenset()) -> list[dict]:
    """The Purchases annotation of one record's refined ores (revision 4,
    user ruling 2026-09-28; contract review A13): per ore, the units
    refined of those received, what they refined into and the landed ISK,
    all summed from the run's STORED via lines (buying.RefinedOre) — never
    recomputed, which would drift from an executed run's frozen lines.
    The refining tax is inside the landed figure and not stored apart, so
    it is not stated on its own. A remainder short of one reprocessing
    batch stayed an ore line: outside the plan.

    `outside` names the outputs of a type the run does not buy (`in_plan`,
    the Buy tab's own test): the matcher writes them too (contract review
    A7), and the strip counts them outside the plan, so the annotation
    says so rather than reading as if every raw fed the plan (review
    2026-09-28)."""
    notes = []
    for ore in ores:
        received = record.received_units(ore.ore_type_id)
        notes.append({
            "ore": _type_name(ref, ore.ore_type_id),
            "refined": max(0, received - ore.remainder),
            "received": received,
            "outputs": ", ".join(
                f"{units:,} {_type_name(ref, type_id)}"
                for type_id, units in ore.outputs
            ),
            "outside": ", ".join(
                f"{units:,} {_type_name(ref, type_id)}"
                for type_id, units in ore.outputs
                if type_id not in in_plan
            ),
            "landed_isk": float(ore.landed_isk),
            "remainder": int(ore.remainder),
        })
    return notes


def _records_from_esi(records, names, ref, in_plan, structure_label,
                      refined=None, locations=None) -> list[dict]:
    """The Purchases section (R8) from buying.run_purchase_records: every
    wallet buy and bought contract dated inside this run's buying window —
    costed or not, each non-costed one with its reason (C9) — newest
    first. `in_plan` is the set of types whose lines costing uses; a costed
    item outside it is marked (R3/R7: it is stock, recorded all the
    same).

    Revision 4 (user ruling 2026-09-28): `refined` is
    buying.refined_ores for the run — an unplanned compressed ore the
    matcher refined is not itself outside the plan (its refined units are
    the raws' lines now; contract review A12), and the record carries the
    "→ refined into …" annotation (_refined_notes), which names the raws
    the run does not buy — those ARE outside the plan, as the strip counts
    them (review 2026-09-28) — and any remainder that stayed outside. `locations` ({location_id: name}, _location_names) names
    where a purchase made at neither market was handed over."""
    refined = refined or {}
    locations = locations or {}
    out = []
    for r in records:
        costed = bool(r.costed)
        ores = refined.get((r.kind, r.esi_id), ()) if costed else ()
        ore_ids = {o.ore_type_id for o in ores}
        if r.kind == store.ESI_KIND_CONTRACT:
            items = [
                {
                    "name": _type_name(ref, i.type_id),
                    "qty": int(i.quantity),
                    "unit_price": i.unit_price,
                    "outside": (
                        costed and i.received and i.type_id not in in_plan
                        and i.type_id not in ore_ids
                    ),
                    "refined": bool(i.received and i.type_id in ore_ids),
                    "given": not i.received,
                    "bpc": bool(i.blueprint_copy),
                }
                for i in r.items
            ]
            label = f"“{r.title}”" if r.title else f"#{r.esi_id}"
        else:
            items = [{
                "name": _type_name(ref, r.type_id),
                "qty": int(r.quantity or 0),
                "unit_price": r.unit_price,
                "outside": (
                    costed and r.type_id not in in_plan
                    and r.type_id not in ore_ids
                ),
                "refined": r.type_id in ore_ids,
                "given": False,
                "bpc": False,
            }]
            label = None
        tone, note = _RECORD_STATUS_NOTE.get(r.status, ("warn", r.status_label))
        location_id = getattr(r, "location_id", None)
        out.append({
            "kind": r.kind,
            "esi_id": r.esi_id,
            "date": r.date or "",
            "owner": names.get((r.owner_kind, r.owner_id), str(r.owner_id)),
            "owner_kind": r.owner_kind,
            "venue": r.venue,
            "venue_label": _venue_label(r.venue, structure_label),
            "location": (
                locations.get(int(location_id), f"location {location_id}")
                if location_id is not None else None
            ),
            "label": label,
            "k": r.k,
            "price": float(r.isk or 0.0),
            "items": items,
            "refined": _refined_notes(r, ores, ref, in_plan),
            "landed_in_price": False,
            "unpriced_items": int(r.unpriced_items or 0) if costed else 0,
            "costed": costed,
            "status_label": None if costed else r.status_label,
            "status_tone": tone,
            "status_note": note,
        })
    return sorted(out, key=lambda rec: rec["date"], reverse=True)


def _records_from_lines(purchases, names, ref, in_plan, structure_label) -> list[dict]:
    """The Purchases section rebuilt from the run's own derived lines —
    the fallback when the matcher's reader fails. Costed records
    only (a non-costed purchase writes no line); a line recorded by hand
    before revision 3 is listed on its own.

    Revision 4 (contract review A12): the raws an unplanned compressed ore
    refined into (via_type_id set) are listed under their ore's record,
    labelled as refined from it — the ore line itself is gone for the
    batches refined, and their landed unit price (freight and refining
    tax inside) is what the lines hold. The record's venue is read from
    its own purchase lines, never from a via line's 'delivered'."""
    records: dict[tuple, dict] = {}
    for lines in purchases.values():
        for line in lines:
            kind = _row_value(line, "esi_kind")
            esi_id = _row_value(line, "esi_id")
            via = _row_value(line, "via_type_id")
            key = (kind, esi_id) if kind is not None else ("hand", line["purchase_id"])
            rec = records.get(key)
            if rec is None:
                owner_kind = _row_value(line, "owner_kind")
                owner_id = _row_value(line, "owner_id")
                rec = records[key] = {
                    "kind": kind or "hand",
                    "esi_id": esi_id,
                    "date": _row_value(line, "date") or "",
                    "owner": (
                        names.get((owner_kind, owner_id), str(owner_id))
                        if owner_id is not None else None
                    ),
                    "owner_kind": owner_kind,
                    "venue": None,
                    "venue_label": None,
                    "location": None,
                    "label": (
                        f"#{esi_id}" if kind == store.ESI_KIND_CONTRACT else None
                    ),
                    "k": _row_value(line, "contract_k"),
                    "price": 0.0,
                    "items": [],
                    "refined": [],
                    "landed_in_price": False,
                    "unpriced_items": 0,
                    "costed": True,
                    "status_label": None,
                    "status_tone": "",
                    "status_note": None,
                }
            if via is None and rec["venue"] is None:
                rec["venue"] = line["venue"]
                rec["venue_label"] = _venue_label(line["venue"], structure_label)
            units = int(line["quantity"])
            rec["items"].append({
                "name": _type_name(ref, line["type_id"]),
                "qty": units,
                "unit_price": float(line["unit_price"]),
                "outside": line["type_id"] not in in_plan,
                "via": _type_name(ref, via) if via is not None else None,
                "refined": False,
                "given": False,
                "bpc": False,
            })
            # For a contract, Σ k × p_i × q_i is the price paid (R7). A
            # refined raw holds only its LANDED price (the ore's price,
            # freight and refining tax spread by value) — the ore's order
            # price for the refined units is stored nowhere — so it adds
            # that, and the Price cell's title says so rather than "before
            # freight" (review 2026-09-28).
            rec["price"] += units * float(line["unit_price"])
            if via is not None:
                rec["landed_in_price"] = True
    return sorted(records.values(), key=lambda rec: rec["date"], reverse=True)


def _cycle_cut(conn) -> datetime | None:
    """The moment the current buying cycle opened — the cut every plan
    passes to engine.plan_index_run (v1.29 revision 7, user ruling
    2026-09-29: final jobs started inside the current buying cycle are
    this cycle's wave; the plan sizes the rest; every ESI update re-plans
    the open run). One source: buying.buying_windows, never re-derived
    here (contract review amendment 6).

    With an open run it is that run's window's lower bound — the greatest
    completed_at among executed runs, else the run's own opened_at
    (planned_start on runs planned before the column), which an in-place
    re-plan never moves. With no open run (▶ Plan is about to create one)
    it is the greatest executed window's upper bound, i.e. the last Mark
    executed; with no executed run at all, None — the first cycle plans
    its full wave. An aware UTC datetime; the engine parses the ESI job
    starts and compares them strictly after it (a job started in the
    cut's own second is the previous wave's, like the windows' (lo, hi]).

    Known limits (amendment 7), both on the under-planning side now:
    finals of the executed cycle installed after Mark executed — or
    within the PC clock's skew from CCP's, since completed_at is the PC's
    datetime('now') and start_date the server's — count as the NEW
    cycle's wave, so the new run plans that many fewer — and the Profit
    tab counts those hulls on BOTH executed runs (the old run's last plan
    still holds them as runs; the new run's installed_qty holds them
    too); and on the first cycle, finals installed before the first
    ▶ Plan are ignored and planned again. The Industry Jobs badge
    (installed N/M) makes either visible."""
    windows = buying.buying_windows(conn)
    open_window = [w for w in windows if w.hi is None]
    if open_window:
        return open_window[0].lo
    executed = [w.hi for w in windows if w.executed and w.hi is not None]
    return max(executed) if executed else None


def _cut_label(cut: datetime | None) -> str | None:
    """A cycle cut as the page prints it — 'YYYY-MM-DD HH:MM' UTC."""
    if cut is None:
        return None
    return cut.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _plan_snapshot_cut(conn, planned_start) -> tuple[str | None, bool]:
    """(when, exact): the ESI update the plan read — the On Hand title's
    "as of" (v1.29 revision 6; the helper dates from revision 5's review
    of 2026-09-28). Planning never pulls ESI: it nets the newest snapshot
    persisted by planned_start (engine.snapshot_from_state), so its
    on_hand_qty / in_progress_qty are the hangar and jobs at THAT
    snapshot's fetched_at. After an update-time re-plan (R1 — the re-plan
    follows the pull in the same request) that is the pull just made;
    after a ▶ Plan it can be hours older. Snapshots are pruned to the last
    five (store.save_esi_snapshot) and index_run stores no snapshot time,
    so when none that old is left the answer falls back to planned_start
    and `exact` is False — the title then says "when the plan was made"."""
    plan_at = costing._when(planned_start)
    if plan_at is None:
        return planned_start, True
    fetched = [
        (at, text)
        for (text,) in conn.execute("SELECT fetched_at FROM esi_snapshot")
        for at in (costing._when(text),)
        if at is not None and at <= plan_at
    ]
    if not fetched:
        return planned_start, False
    return max(fetched)[1], True


def _required_of(row) -> int:
    """A Buy-tab row's Required (v1.29 revision 6, user ruling R4
    2026-09-28; contract review A13): what this cycle's plan needs of the
    item in total —

        deficit_qty + on_hand_qty + in_progress_qty   when deficit_qty > 0
        target_stock_qty                              otherwise

    — so Required − On Hand is exactly the deficit the plan sized its buy
    on. For a just-in-time raw that is target_stock_qty itself (the
    allocated jobs' consumption plus the purchase margin, engine
    _finalize — not cycle_need_qty, which would break Required − On Hand
    = Remaining on nearly every raw); for a buildable it is target stock
    plus this cycle's draw, the identity the Stockpile's deficit dialog
    inverts. The Stockpile computes no such figure in Python, so this is
    the one place it is derived. Every buildable row the tab lists has a
    positive deficit (a buildable buys only out of one), so the fallback
    reaches raws with nothing to buy and the alchemy-route edge only.
    NULLs read as 0 (a hand-built row)."""
    deficit = int(_row_value(row, "deficit_qty") or 0)
    if deficit > 0:
        return (
            deficit
            + int(_row_value(row, "on_hand_qty") or 0)
            + int(_row_value(row, "in_progress_qty") or 0)
        )
    return int(_row_value(row, "target_stock_qty") or 0)


def _buy_tab_context(
    run, rows, ref, settings_, conn, bc, esi_records=None, superseded=False
) -> dict:
    """The Buy tab's whole context: the strip, Multibuy All, the family
    groups with Required · On Hand · Remaining · Purchased · Ladder · Δ
    and each group's Multibuy, and the Purchases ESI delivered for this
    cycle.

    Revision 6 (user rulings 2026-09-28). R1: one run per buying cycle,
    kept live — every ⟳ Update from ESI re-plans the open run IN PLACE
    from the stock, jobs and prices it just pulled, so the plan's buy
    list already nets everything bought that reached a tracked system.
    R4: the page therefore subtracts NOTHING from the plan —

    - Required (_required_of) — what the cycle needs of the item in
      total: target stock plus this cycle's draw, the Stockpile's deficit
      basis; for a just-in-time raw its planned consumption plus the
      purchase margin. In the strip's and each group's Required ISK a
      row the plan builds counts only what it holds plus what it buys —
      the built units are no purchase (review 2026-09-28).
    - On Hand = on_hand_qty + in_progress_qty — the stock the fresh plan
      counted (contract review A12): the hangar, what the hangar's
      compressed ore reprocesses into (on_hand_from_ore_qty, ruling R3 —
      inside on_hand_qty), and job output in progress (alchemy's expected
      credit inside it).
    - Remaining = recommended_buy_qty + compressed_covered_qty — the
      plan's buy, the covered part sourced as ore lines in Multibuy All
      (the group's own list names the raw itself, revision 8). For a raw with a deficit Required − On Hand = Remaining
      exactly (the sourcing pass splits the deficit into direct +
      covered); it diverges where stock exceeds Required (Remaining 0),
      on a buildable row (the plan builds part of the deficit, expects
      alchemy output or leaves a capacity shortfall no market holds —
      Remaining is only the bought part), and the unsourced units are in
      Remaining but in neither Multibuy All block (B35; the group's own
      list, revision 8, carries them as part of Remaining) — each title
      says which (contract review A14).
    - Purchased — the cycle's purchase lines (costing.bought_cell's
      bought_qty / bought_unit / bought_landed; _line_sources' sources
      are clauses of its title, no badge — revision 8, R1),
      with the units past costing.purchase_basis named as stock for the
      next cycle (R5: realized costing leaves them unpriced).
    - Ladder and Δ — costing.bought_cell's ladder_unit and delta, as
      before.

    Multibuy All's blocks (contract review A15) list the plan's venue
    shares exactly (`bc["venue_qty"]`), unsourced units excluded, and
    every compressed ore the plan picked at its plan quantity, in ONE
    group (_buy_group, B36). Revision 8 (user ruling R2 2026-09-29): a
    group's own Multibuy is ONE plain list — each row's Remaining, the
    raw itself, no market split and no ore line (_group_multibuy) — and
    R2b removed the group's ore table: a plan-chosen ore's purchases stay
    in the Purchased totals and the Purchases section, and a group names
    an ore only in tooltips — the covered raw's "via compressed" tag and,
    for an ore the plan did not pick, the Purchased title's via clause.
    Revisions
    3–5's post-plan purchase netting, the via-ore pre-plan exemption and
    revision 5's New stock column are gone.

    Known limit (contract review B2): a purchase still in transit — a
    Jita buy on its way to the structure — is in no tracked system's
    assets, so the re-plan cannot net it and it still reads as Remaining;
    a row with Purchased > 0 and Remaining > 0 carries an "in transit?"
    cue, and the page caption says so. A live plan older than the newest
    ESI update (`stale_since`: that update's re-plan was skipped — nothing
    to plan from — or failed; review 2026-09-28) nets nothing since it
    was made: the captions say so instead and the transit cue is off.

    Rows (C14.4) are revision 2's — in Minerals, Moon Materials and Gas
    the END-RESULT raws at their demand (direct + compressed-covered),
    with no ore row (R4); elsewhere every row the plan buys — plus every
    non-built row of the run that carries purchase lines, which includes
    a re-planned row that no longer buys anything, so the tab shows every
    line costing uses. "Outside the plan" is exactly two kinds of line: a
    type with no index_run_item row in the run, and a BUILDABLE row
    (blueprint_id set, or an alchemy route) — stock, never costed into
    the Purchased total (§5's known limit: hull_cost prices such a row
    from its own inputs). Revision 4: a compressed ore the plan did NOT
    pick is written by the matcher as the raws it refines into (lines
    carrying via_type_id), so it lands in those raws' Purchased, named
    `via compressed` (in the cell's title since revision 8); a purchase at
    neither market is `elsewhere` and landed at the run's default inbound
    rate.

    `bc` is run_detail's _buy_context dict (venue_split is a closure
    inside it, A26). `esi_records` is buying.run_purchase_records for the
    run; None rebuilds the Purchases section from the run's derived
    lines. A `superseded` run collects no purchases (R2) and reads none
    of its lines, even ones a matching pass has not cleared yet."""
    index_run_id = run["index_run_id"]
    planned_start = _row_value(run, "planned_start")
    rates = _run_buy_rates(run, settings_)
    hub_rate = rates[store.BUY_VENUE_HUB]
    structure_rate = rates[store.BUY_VENUE_STRUCTURE]
    other_rate = rates[store.BUY_VENUE_OTHER]
    ore_ids = set(bc["compressed"])
    raw_groups = {
        config.COMPRESSED_MINERALS_GROUP,
        config.COMPRESSED_MOON_GROUP,
        config.COMPRESSED_GAS_SOURCE_GROUP,
    }
    by_type = {i["type_id"]: i for i in rows}
    purchases = {} if superseded else store.list_purchases(conn, index_run_id)
    structure_label = settings_.structure_market_label()
    contracts = _bought_contracts(
        conn,
        (
            _row_value(line, "esi_id")
            for lines in purchases.values() for line in lines
            if _row_value(line, "esi_kind") == store.ESI_KIND_CONTRACT
        ),
    )
    # Revision 4: where a purchase made at neither market, or a contract,
    # was handed over — the badge titles name it (contract review A12).
    where = _line_locations(
        conn, ref,
        [line for lines in purchases.values() for line in lines],
        contracts,
    )
    # The On Hand title's "as of": the ESI update this plan read.
    stock_at, stock_exact = _plan_snapshot_cut(conn, planned_start)
    # The open run is the live one (R1): only its plan follows the ESI
    # updates, so only there can a purchase read as "still in transit".
    live = not superseded and run["status"] != buying.RUN_EXECUTED
    # Review 2026-09-28: a live plan is STALE when an ESI update landed
    # after it was planned — that update's re-plan was skipped (nothing to
    # plan from: no active pipeline or prices) or failed and rolled back.
    # Revision 7 (user ruling 2026-09-29) removed the final-installs stop
    # rule, and a snapshot that did not refresh saves no newer row
    # (esi.refresh_state saves it last), so neither can make a plan stale
    # (contract review amendment 11). Its buy list then nets none of
    # the stock or purchases since, so the page must not claim "what you
    # bought is off Remaining already", nor blame transit for a purchase
    # already in the hangar. `stale_since` is that newer update's time.
    stale_since = None
    if live:
        newest = conn.execute("SELECT MAX(fetched_at) FROM esi_snapshot").fetchone()[0]
        newest_at = costing._when(newest)
        plan_at = costing._when(planned_start)
        if newest_at is not None and plan_at is not None and newest_at > plan_at:
            stale_since = newest

    def sources_of(lines):
        return _line_sources(
            lines, contracts, structure_label, where,
            lambda type_id: _type_name(ref, type_id),
        )

    def demand_of(item):
        return (
            int(item["recommended_buy_qty"] or 0)
            + int(_row_value(item, "compressed_covered_qty") or 0)
        )

    def built_of(item):
        # blueprint_id is the plan's own "this row can be built" marker;
        # an alchemy route is a reaction row, so it carries one too.
        return (
            _row_value(item, "blueprint_id") is not None
            or bool(_row_value(item, "alchemy_for_type_id"))
        )

    def shares_of(type_id) -> tuple[int, int]:
        # The plan's (Jita, structure) units — the Multibuy lines as they
        # are, nothing taken off (contract review A15).
        hub, structure = bc["venue_qty"].get(type_id, (0, 0))
        return int(hub), int(structure)

    # B13: the row set is BUILT, not filtered — bc["buys"] excludes a raw
    # the compressed pass covered in full (its recommended_buy_qty is 0).
    table_rows = [
        i for i in rows
        if i["type_id"] not in ore_ids
        and (
            (
                demand_of(i) > 0 if i["group_id"] in raw_groups
                else (i["recommended_buy_qty"] or 0) > 0
            )
            or (i["type_id"] in purchases and not built_of(i))
        )
    ]
    # The lines costing uses: every table row but a built one, and the
    # ores (C14.3: an ore's lines are plan purchases).
    in_plan = {
        i["type_id"] for i in table_rows if not built_of(i)
    } | {o for o in ore_ids if o in by_type}

    # An ore is filed in ONE group — the one covering the most of its
    # output (_buy_group's tally and tie rule, B36) — but it can cover
    # raws in two. The filing only orders Multibuy All's ore lines (the
    # ore is pasted once) and says whose Purchased ISK its purchases join;
    # since revision 8's R2b no group renders the ore, so a covered raw
    # names its ores without pointing at a group.
    ore_group = {
        ore_id: _buy_group(ref, by_type[ore_id], covers)
        for ore_id, covers in bc["compressed"].items()
        if ore_id in by_type
    }
    covers_raw: dict[int, list[dict]] = {}
    for ore_id, covers in bc["compressed"].items():
        for material_id, _name, _out, used in covers:
            if used > 0 and ore_id in by_type:
                covers_raw.setdefault(material_id, []).append(
                    {
                        "name": by_type[ore_id]["name"],
                        "type_id": ore_id,
                        "used": int(used),
                    }
                )

    groups: dict[str, dict] = {}

    def group_for(label):
        return groups.setdefault(
            label,
            {
                "label": label, "rows": [], "ores": [],
                "required_total": 0.0, "on_hand_total": 0.0,
                "remaining_total": 0.0, "purchased_total": 0.0,
            },
        )

    totals = {
        "required_total": 0.0, "on_hand_total": 0.0,
        "remaining_total": 0.0, "purchased_total": 0.0,
    }
    on_hand_beyond = 0          # units on hand past each row's Required
    for item in table_rows:
        type_id = item["type_id"]
        direct_qty = int(item["recommended_buy_qty"] or 0)
        covered_qty = int(_row_value(item, "compressed_covered_qty") or 0)
        m3 = ref.type_info(type_id).freight_volume
        lines = purchases.get(type_id, [])
        cell = costing.bought_cell(
            item, lines, hub_rate, structure_rate, m3, other_rate=other_rate,
        )
        built = built_of(item)
        remaining = direct_qty + covered_qty
        required = _required_of(item)
        deficit = int(_row_value(item, "deficit_qty") or 0)
        target = int(_row_value(item, "target_stock_qty") or 0)
        hangar_and_ore = int(_row_value(item, "on_hand_qty") or 0)
        from_ore = int(_row_value(item, "on_hand_from_ore_qty") or 0)
        in_jobs = int(_row_value(item, "in_progress_qty") or 0)
        on_hand = hangar_and_ore + in_jobs
        hub_qty, structure_qty = shares_of(type_id)
        unsourced_qty = (
            int(_row_value(item, "unfilled_qty") or 0)
            if type_id in bc["unsourced"] else 0
        )
        build_qty = int(_row_value(item, "recommended_build_qty") or 0)
        alchemy_out = int(_row_value(item, "alchemy_output_qty") or 0)
        ladder_unit = cell.ladder_unit
        price = ladder_unit or 0.0
        group_label = _buy_group(ref, item, None)
        ore_list = covers_raw.get(type_id, [])
        row = {
            "item": item,
            "type_id": type_id,
            "name": item["name"],
            "built": built,
            "direct_qty": direct_qty,
            "covered_qty": covered_qty,
            "cell": cell,
            "lines": lines,
            "sources": sources_of(lines),
            # Required and what it is made of (the title names the basis).
            "required": required,
            "target": target,
            "draw": required - target if deficit > 0 else 0,
            "cycle_need": int(
                _row_value(item, "cycle_need_qty")
                or _row_value(item, "merged_min_qty") or 0
            ),
            # On Hand: the stock the plan netted (A12).
            "on_hand": on_hand,
            "hangar": hangar_and_ore - from_ore,
            "from_ore": from_ore,
            "in_jobs": in_jobs,
            "alchemy_credit": int(_row_value(item, "alchemy_credit_qty") or 0),
            # Remaining: the plan's buy (R4) and why it can differ from
            # Required − On Hand (A14).
            "remaining": remaining,
            "gap": max(0, required - on_hand),
            "build_qty": build_qty,
            "alchemy_out": alchemy_out,
            "unmet_qty": max(
                0, deficit - build_qty - direct_qty - alchemy_out
            ) if built else 0,
            "unsourced_qty": unsourced_qty,
            "hub_qty": hub_qty,
            "structure_qty": structure_qty,
            # Purchased: the units past what realized costing prices this
            # cycle (costing.purchase_basis, R5) are next cycle's stock.
            "over_bought": (
                0 if built
                else max(0, cell.bought_qty - costing.purchase_basis(item))
            ),
            # B2: bought, yet the live plan still lists units — some may
            # be on their way (ESI shows no stock in transit). An executed
            # run's plan predates its purchases, so it never asks.
            # A stale plan (review 2026-09-28) netted nothing since it
            # was made, so transit is not the likely reason: no cue.
            "in_transit": (
                live and stale_since is None
                and cell.bought_qty > 0 and remaining > 0
            ),
            "ladder_unit": ladder_unit,
            # A row the plan BUILDS counts in the strip's and the group's
            # Required ISK only as what it holds plus what it buys (review
            # 2026-09-28): the units it builds are no purchase, and at the
            # ladder they would inflate Required by the market value of
            # the build, so Required − On Hand ≈ Remaining held for bought
            # rows only (contract review A16). The column keeps the full
            # Required; `built_share` is what the ISK leaves out.
            "required_isk": price * (
                min(required, min(on_hand, required) + remaining)
                if built else required
            ),
            "built_share": (
                max(0, required - min(on_hand, required) - remaining)
                if built else 0
            ),
            "on_hand_isk": price * min(on_hand, required),
            "remaining_isk": price * remaining,
            # The ores behind the "N via compressed" tag, named in its
            # title — since revision 8 (user rulings R2/R2b 2026-09-29) a
            # plan-chosen ore is a line in Multibuy All alone, and this
            # tag (with the Purchased title's via clause for an ore the
            # plan did not pick) is the only place a group names one.
            "ores": [o["name"] for o in ore_list],
            "unsourced": type_id in bc["unsourced"],
            "unfilled_qty": int(_row_value(item, "unfilled_qty") or 0),
            "unfilled_price": _row_value(item, "unfilled_price"),
            "effective_unit_cost": _row_value(item, "effective_unit_cost"),
            "missing_price": ladder_unit is None and (required or remaining) > 0,
            "plan_hub": hub_qty,
            "plan_structure": structure_qty,
        }
        on_hand_beyond += max(0, on_hand - required)
        group = group_for(group_label)
        group["rows"].append(row)
        for key, value in (
            ("required_total", row["required_isk"]),
            ("on_hand_total", row["on_hand_isk"]),
            ("remaining_total", row["remaining_isk"]),
        ):
            group[key] += value
            totals[key] += value
        if not built:
            group["purchased_total"] += cell.bought_landed
            totals["purchased_total"] += cell.bought_landed

    # -- the ores: every plan-chosen ore at its plan quantity (A15) ------
    # Revision 8's R2b (user ruling 2026-09-29): an ore is a line in
    # Multibuy All and a step of its reprocess checklist — nothing else on
    # the page. The entry carries only what those two read; no group
    # renders an ore, so it keeps no Purchased cell or sources.
    for ore_id in sorted(ore_ids):
        ore = by_type.get(ore_id)
        if ore is None:
            continue
        hub_qty, structure_qty = shares_of(ore_id)
        entry = {
            "item": ore,
            "name": ore["name"],
            "qty": int(ore["recommended_buy_qty"] or 0),
            "hub_qty": hub_qty,
            "structure_qty": structure_qty,
            "outputs": [
                {
                    "name": name, "out": out, "used": used,
                    "leftover": max(0, out - used),
                }
                for _m, name, out, used in bc["compressed"][ore_id]
            ],
        }
        group = group_for(ore_group[ore_id])
        group["ores"].append(entry)
        # The ore's ISK is plan purchase ISK (C14.3) — in Purchased, the
        # strip's and its group's, exactly as before R2b removed its table
        # (the Purchases section still lists the purchase). Its plan
        # figure is NOT added to Required / Remaining: it already sits
        # inside the covered raws' effective cost, which is why it has no
        # row. No planned_start: bought_cell's pre-/post-plan split is
        # unused here (only the landed ISK is read).
        bought_landed = costing.bought_cell(
            ore, purchases.get(ore_id, []), hub_rate, structure_rate,
            ref.type_info(ore_id).freight_volume, other_rate=other_rate,
        ).bought_landed
        group["purchased_total"] += bought_landed
        totals["purchased_total"] += bought_landed

    ordered = sorted(
        groups.values(),
        key=lambda g: (_BUY_GROUP_RANK.get(g["label"], _UNRANKED), g["label"]),
    )
    for group in ordered:
        group["rows"].sort(key=lambda r: r["name"])
        group["ores"].sort(key=lambda o: o["name"])
        group.update(_group_multibuy(group))

    # Multibuy All (R8): every group's rows and filed ores, in the groups'
    # order, one block per venue — the plan's sell-ladder buy. Revision 8
    # (user ruling R2 2026-09-29) left it unchanged; the group lists are
    # the same rows' Remaining, unsplit and with covered units as the raw.
    all_entries = [
        e for g in ordered
        for e in list(g["rows"]) + g["multibuy_ores"]
    ]
    all_ores = [o for g in ordered for o in g["multibuy_ores"]]

    # "Outside the plan" (C14.4): a type the run does not hold, and a row
    # the plan builds — recorded, and stock for the next plan (R3).
    # Revision 4 (user ruling 2026-09-28): the batches of an unplanned
    # compressed ore the matcher refined are no ore line any more — they
    # are the raws' via lines, so they drop out of this count by type
    # (a raw the run does not hold is still outside: stock, A7). What an
    # ore keeps as its own line is the remainder short of one whole
    # reprocessing batch; the badge's title names it.
    refined_ores = {
        int(_row_value(line, "via_type_id"))
        for lines in purchases.values() for line in lines
        if _row_value(line, "via_type_id") is not None
    }
    outside_units = 0
    outside_names: list[str] = []
    outside_remainders: list[str] = []
    for type_id, lines in purchases.items():
        if type_id in in_plan:
            continue
        units = sum(int(line["quantity"]) for line in lines)
        outside_units += units
        outside_names.append(_type_name(ref, type_id))
        if type_id in refined_ores:
            outside_remainders.append(f"{units:,} {_type_name(ref, type_id)}")

    # B14: an ore whose outputs were worth nothing at plan time allocates
    # share 0.0 to every raw it covers, so its landed ISK sits in NO row
    # here. Name the amount rather than let it vanish from the total.
    orphan_ore_isk = sum(
        float(by_type[ore_id]["compressed_landed_isk"] or 0.0)
        for ore_id in ore_ids
        if by_type.get(ore_id) is not None
        and _row_value(by_type[ore_id], "compressed_landed_isk") is not None
        and not _ore_shares_any(by_type[ore_id])
    )
    rendered = [r for g in ordered for r in g["rows"]]
    names = _owner_names(conn)
    if esi_records is not None:
        # The refined-ore annotation reads the run's stored via lines — a
        # superseded run reads none (its figures are the plan alone).
        try:
            refined = (
                {} if superseded else buying.refined_ores(conn, index_run_id)
            )
        except Exception:  # noqa: BLE001 — the annotation is optional
            log.exception("reading the run's refined ores failed")
            refined = {}
        records = _records_from_esi(
            esi_records, names, ref, in_plan, structure_label,
            refined=refined,
            locations=_location_names(
                conn, ref,
                (getattr(r, "location_id", None) for r in esi_records),
            ),
        )
    else:
        records = _records_from_lines(
            purchases, names, ref, in_plan, structure_label
        )
    return dict(
        buy_groups=ordered,
        buy_summary={
            **totals,
            "rows": len(rendered),
            "rows_purchased": sum(1 for r in rendered if r["cell"].bought_qty),
            "units_required": sum(r["required"] for r in rendered),
            # Units of built rows' Required left out of the Required ISK.
            "units_built": sum(r["built_share"] for r in rendered),
            "rows_built": sum(1 for r in rendered if r["built_share"]),
            "units_on_hand": sum(r["on_hand"] for r in rendered),
            "units_remaining": sum(r["remaining"] for r in rendered),
            "on_hand_beyond": on_hand_beyond,
            "in_transit": sum(1 for r in rendered if r["in_transit"]),
            # An unpriced row's units count as 0 ISK in Required, On Hand
            # and Remaining, exactly as the Industry Jobs tab's buy total
            # treats them — the strip says so rather than reading low.
            "unpriced": sum(1 for r in rendered if r["missing_price"]),
            # B39: these count the rows this tab RENDERS, not the plan's
            # whole buy list — the ore rows are not in it.
            "split": sum(
                1 for r in rendered
                if r["plan_hub"] > 0 and r["plan_structure"] > 0
            ),
            "structure_only": sum(
                1 for r in rendered
                if r["plan_structure"] > 0 and r["plan_hub"] == 0
            ),
            "unsourced": sum(1 for r in rendered if r["unsourced"]),
            "compressed": len(ore_ids),
            "compressed_covered": len(bc["compressed_covered"]),
            "outside_units": outside_units,
            "outside_names": sorted(outside_names),
            "outside_remainders": sorted(outside_remainders),
            "unpriced_contract_items": sum(
                r["unpriced_items"] for r in records
            ),
            "not_costed": sum(1 for r in records if not r["costed"]),
            "orphan_ore_isk": orphan_ore_isk,
        },
        # The ESI update this plan read (the On Hand titles); `exact` is
        # False when it has been pruned and the plan's start stands in.
        stock_at=stock_at,
        stock_exact=stock_exact,
        multibuy_all={
            "hub": _multibuy_text(all_entries, "hub_qty"),
            "structure": _multibuy_text(all_entries, "structure_qty"),
            "items": sum(
                1 for e in all_entries
                if e["hub_qty"] > 0 or e["structure_qty"] > 0
            ),
            "ores": all_ores,
        },
        purchase_records=records,
        # A late ESI pull that adds a purchase to an executed run reprices
        # later runs' lagged inputs on the next read — the lag walk
        # computes on read.
        history_note=run["status"] == "complete",
        structure_label=structure_label,
        live=live,
        stale_since=stale_since,
        # The margin the Settings read NOW: it is not persisted on the run,
        # so the Required title names it as today's setting, never as the
        # one this plan used (review 2026-09-28).
        purchase_margin=settings_.input_purchase_margin,
        # m.item_button opens the shared deficit dialog from this tab too
        # (review B5: a bought-only row has no other "why this quantity"
        # anywhere in the app).
        final_ids=_final_ids(conn, rows),
        compressed=bc["compressed"],
        compressed_covered=bc["compressed_covered"],
        compressed_saving=(
            run["compressed_saving_isk"]
            if "compressed_saving_isk" in run.keys() else None
        ),
    )


def _chain_context(ref, items) -> dict:
    """run_detail's Chain-tab derivations: the per-item status rows, their
    raw/manufactured/reacted/structure groupings, and the status counts."""

    def display_category(item) -> str:
        return _display_category(
            ref,
            item["type_id"],
            item["group_id"],
            item["category_id"],
            item["category"],
        )

    chain_rows = [
        {
            "type_id": i["type_id"],
            "name": i["name"],
            "category": display_category(i),
            "group": i["category"],  # EVE group, e.g. "Jump Freighter"
            "group_id": i["group_id"],
            "depth": i["depth"],
            # One cycle's consumption at the jobs' own rounding (the
            # target basis since v1.27.1); the merged BOM figure on runs
            # planned before the column existed.
            "cycle_need": (
                i["cycle_need_qty"]
                if "cycle_need_qty" in i.keys() and i["cycle_need_qty"] is not None
                else i["merged_min_qty"]
            ),
            "target": i["target_stock_qty"],
            "on_hand": i["on_hand_qty"],
            # v1.29 revision 6 (ruling R3): the part of on_hand that is
            # the hangar's compressed ore reprocessed at the asserted
            # yields (inside on_hand — contract review A12); NULL on runs
            # planned before, and absent on a hand-built row.
            "on_hand_from_ore": (
                (i["on_hand_from_ore_qty"] or 0)
                if "on_hand_from_ore_qty" in i.keys()
                else 0
            ),
            "in_jobs": i["in_progress_qty"],
            "deficit": i["deficit_qty"],
            "buildable": i["blueprint_id"] is not None,
            "activity_id": i["activity_id"],
            "alchemy_credit": i["alchemy_credit_qty"] or 0,
            "alchemy_route": bool(i["alchemy_for_type_id"]),
            "build_qty": i["recommended_build_qty"] or 0,
            "buy_qty": i["recommended_buy_qty"] or 0,
            "alchemy_out": i["alchemy_output_qty"] or 0,
            # v1.25: units covered by reprocessing compressed purchases
            # (pre-v1.25 rows have no column).
            "compressed_covered": (
                (i["compressed_covered_qty"] or 0)
                if "compressed_covered_qty" in i.keys()
                else 0
            ),
            "capacity_limited": bool(i["capacity_limited"]),
            # v1.26: units bought because the market beat the build cost
            # (pre-v1.26 rows have no column).
            "market_buy": (
                (i["market_buy_qty"] or 0)
                if "market_buy_qty" in i.keys()
                else 0
            ),
            # 2026-09-01: the section-header stats (slots, build/buy value)
            # read the same keys the Plan tab's rows carry.
            "jobs_allocated": i["jobs_allocated"] or 0,
            "recommended_build_qty": i["recommended_build_qty"] or 0,
            "recommended_buy_qty": i["recommended_buy_qty"] or 0,
            "price_snapshot": i["price_snapshot"],
        }
        for i in items
    ]

    def group_chain(rows) -> list:
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["category"], []).append(row)
        return sorted(
            grouped.items(),
            key=lambda e: (_CATEGORY_RANK.get(e[0], _UNRANKED), e[0]),
        )

    for x in chain_rows:
        x["status"] = _chain_status(x)
        x["short"] = _chain_short(x)
    chain_counts = {
        k: sum(1 for x in chain_rows if x["status"] == k)
        for k in ("covered", "buy", "build", "react", "alchemy")
    }
    # Unmet = the Plan tab's definition: deficit with no purchase
    # fallback, whether no jobs at all or only part of them.
    chain_counts["unmet"] = sum(
        1 for x in chain_rows if x["status"] == "unmet" or x["short"]
    )
    chain_mfg_rows = [
        x for x in chain_rows if x["buildable"] and x["activity_id"] == 1
    ]
    chain_reaction_rows = [
        x for x in chain_rows if x["buildable"] and x["activity_id"] == 11
    ]
    return dict(
        chain_rows=chain_rows,
        chain_raws=[x for x in chain_rows if not x["buildable"]],
        # Totals-strip stat only: the rows themselves sit in chain_mfg's
        # "Structure Components" group (2026-09-01).
        chain_struct=[
            x for x in chain_rows
            if x["group_id"] in config.STRUCTURE_COMPONENT_GROUPS
        ],
        chain_mfg=group_chain(chain_mfg_rows),
        chain_mfg_rows=chain_mfg_rows,
        chain_reactions=group_chain(chain_reaction_rows),
        chain_reaction_rows=chain_reaction_rows,
        chain_counts=chain_counts,
    )


def _final_ids(c, items) -> set:
    """The type ids the run pages treat as pipeline finals — the run's own
    depth-0 rows (a single-role final is never consumed, so its merged
    depth stays 0; deactivating the pipeline later must not demote its
    history) unioned with the live ACTIVE finals, which covers a dual-role
    final whose merged depth is >= 1 because another chain consumes it.
    m.item_button reads it for the deficit dialog's wording, so the Buy
    tab needs it as much as the Industry Jobs tab does (v1.29)."""
    return {i["type_id"] for i in items if i["depth"] == 0} | {
        row["final_product_type_id"]
        for row in c.execute(
            "SELECT final_product_type_id FROM pipeline WHERE is_active = 1"
        )
    }


def _final_margin_badges(c, ref, settings_, items) -> tuple[set, dict]:
    """(final_ids, final_net_margin) for run_detail. Finals badge
    (decision 2026-08-21): net proceeds after sell-side fees minus the
    integrated chain cost at plan-time prices. The chain cost is
    persisted per row since 2026-08-23 (savings is against the LANDED
    buy price); older rows recover it from the raw-price savings they
    were planned with (chain = price − savings), so history renders
    without replanning."""
    # Plan-time finals: the run's own depth-0 rows (a single-role final
    # is never consumed, so its merged depth stays 0 — deactivating the
    # pipeline later must not demote its history), unioned with the live
    # ACTIVE finals (covers dual-role finals, whose merged depth is >= 1
    # because another chain consumes them; matching the engine's rule
    # for current runs).
    final_ids = _final_ids(c, items)
    final_net_margin = {}
    for i in items:
        if (
            i["type_id"] in final_ids
            and i["price_snapshot"] is not None
            and i["build_savings_per_unit"] is not None
        ):
            chain_cost = (
                i["unit_chain_cost"]
                if i["unit_chain_cost"] is not None
                else i["price_snapshot"] - i["build_savings_per_unit"]
            )
            info = ref.type_info(i["type_id"])
            final_net_margin[i["type_id"]] = (
                costing.net_proceeds_per_hull(
                    i["price_snapshot"],
                    info.freight_volume,
                    settings_,
                    capital=costing.is_capital_priced(ref, i["type_id"]),
                    freight_exempt=costing.freight_out_exempt(i["type_id"]),
                )
                - chain_cost
            )
    return final_ids, final_net_margin


CLASS_LABELS = {
    "capital_ships": "Capital Ships",
    "t2_ships": "T2 Ships",
    "t1_ships": "T1 Ships",
    "basic_capital_components": "Basic Capital Components",
    "advanced_components": "Advanced Components",
    "structures": "Structures, Rigs & Components",
    "reactions": "Reactions",
    "other": "Everything Else",
    "invention": "Invention Lab",
    "copying": "Copy Lab",
}


def _form_number(form, key, cast=float):
    """Locale-tolerant numeric form field; raises ValueError with the field
    name so the route can flash which input was bad instead of 500ing."""
    raw = (form.get(key) or "").strip().replace(",", ".")
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(key)


def _settings_save(c, form):
    """The settings POST body: parse ~40 numeric fields — a ValueError
    (carrying the field name) aborts before commit, so the caller can
    flash which input was bad and nothing is saved."""

    def pct_field(key: str) -> float:
        """Percentage input -> stored fraction (the form shows
        5 for a stored 0.05)."""
        return round(_form_number(form, key) / 100.0, 8)

    def int_field(key: str) -> int:
        # int("1.5") raises, matching the pre-helper strictness for
        # integer fields; the ValueError still names the field.
        return _form_number(form, key, int)

    buffer = min(0.1, max(0.001, pct_field("buffer_pct")))
    margin = min(0.5, max(0.0, pct_field("purchase_margin_pct")))

    def basis_field(key: str) -> str:
        # v1.26: one pricing basis per market; an unknown value (an old
        # form, a hand-edited POST) keeps the ladder walk.
        value = form.get(key) or store.PRICE_BASIS_LADDER
        return value if value in store.PRICE_BASES else store.PRICE_BASIS_LADDER

    hub_basis = basis_field("hub_price_basis")
    structure_basis = basis_field("structure_price_basis")
    c.execute(
        "UPDATE settings SET input_purchase_margin = ?, "
        "stockpile_buffer = ?, "
        "max_run_duration_hours = ?, "
        "composite_reaction_extra_runs = ?, price_region_id = ?, "
        "price_source = ?, manufacturing_slots = ?, "
        "reaction_slots = ?, skill_industry = ?, "
        "skill_advanced_industry = ?, skill_reactions = ?, "
        "skill_adv_ship_construction = ?, "
        "skill_starship_engineering = ?, skill_science = ?, "
        "default_intermediate_me = ?, default_intermediate_te = ?, "
        "alchemy_enabled = ?, alchemy_reprocess_yield = ?, "
        "max_alchemy_jobs_per_type = ?, "
        "skill_accounting = ?, skill_broker_relations = ?, "
        "standing_broker_faction = ?, standing_broker_corp = ?, "
        "freight_in_isk_per_m3 = ?, freight_out_isk_per_m3 = ?, "
        "capital_market_mode = ?, capital_structure_id = ?, "
        "capital_sales_tax = ?, capital_broker_rate = ?, "
        "capital_movement_cost_isk = ?, capital_scc_surcharge = ?, "
        "industry_scc_surcharge = ?, "
        "skill_outpost_construction = ?, "
        "count_fitted_stock = ?, "
        "structure_freight_in_isk_per_m3 = ?, structure_buy_enabled = ?, "
        "freight_in_default_isk_per_m3 = ?, "
        "skill_encryption = ?, "
        "t1_bpc_overbuild = ?, t2_bpc_overbuild = ?, "
        "compressed_minerals_enabled = ?, compressed_moon_enabled = ?, "
        "compressed_gas_enabled = ?, compressed_ore_yield = ?, "
        "compressed_gas_yield = ?, compressed_reprocess_tax = ?, "
        "hub_price_basis = ?, structure_price_basis = ? "
        "WHERE id = 1",
        (
            margin,
            buffer,
            max(1.0, _form_number(form, "duration")),
            max(0, int_field("extra_runs")),
            int_field("region"),
            # v1.26: the ESI side the hub refresh pulls follows the hub
            # basis (buy orders only under Max Buy Order).
            "buy" if hub_basis == store.PRICE_BASIS_MAX_BUY else "sell",
            max(0, int_field("mfg_slots")),
            max(0, int_field("reaction_slots")),
            min(5, max(0, int_field("skill_industry"))),
            min(5, max(0, int_field("skill_advanced_industry"))),
            min(5, max(0, int_field("skill_reactions"))),
            min(5, max(0, int_field("skill_adv_ship_construction"))),
            min(5, max(0, int_field("skill_starship_engineering"))),
            min(5, max(0, int_field("skill_science"))),
            min(10, max(0, int_field("intermediate_me"))),
            min(20, max(0, int_field("intermediate_te"))),
            1 if form.get("alchemy_enabled") else 0,
            min(1.0, max(0.0, pct_field("alchemy_yield_pct"))),
            max(0, int_field("max_alchemy_jobs")),
            min(5, max(0, int_field("skill_accounting"))),
            min(5, max(0, int_field("skill_broker_relations"))),
            min(10.0, max(-10.0, _form_number(form, "standing_faction"))),
            min(10.0, max(-10.0, _form_number(form, "standing_corp"))),
            max(0.0, _form_number(form, "freight_in")),
            max(0.0, _form_number(form, "freight_out")),
            (
                form["capital_market_mode"]
                if form["capital_market_mode"] in ("cj6", "custom")
                else "cj6"
            ),
            (
                int_field("capital_structure_id")
                if (form.get("capital_structure_id") or "").strip()
                else None
            ),
            min(0.2, max(0.0, pct_field("capital_sales_tax_pct"))),
            min(0.2, max(0.0, pct_field("capital_broker_pct"))),
            max(0.0, _form_number(form, "capital_movement_cost")),
            min(0.2, max(0.0, pct_field("capital_scc_pct"))),
            min(0.2, max(0.0, pct_field("industry_scc_pct"))),
            min(5, max(0, int_field("skill_outpost_construction"))),
            1 if form.get("count_fitted_stock") else 0,
            (
                max(0.0, _form_number(form, "structure_freight_in"))
                if (form.get("structure_freight_in") or "").strip()
                else 0.0
            ),
            1 if form.get("structure_buy_enabled") else 0,
            # Revision 4 (user ruling 2026-09-28): the inbound rate of a
            # purchase at neither Jita 4-4 nor the structure market —
            # parsed like its structure sibling (blank → 0, never
            # negative).
            (
                max(0.0, _form_number(form, "freight_in_default"))
                if (form.get("freight_in_default") or "").strip()
                else 0.0
            ),
            min(5, max(0, int_field("skill_encryption"))),
            min(10.0, max(1.0, pct_field("t1_overbuild_pct"))),
            min(10.0, max(1.0, pct_field("t2_overbuild_pct"))),
            1 if form.get("compressed_minerals_enabled") else 0,
            1 if form.get("compressed_moon_enabled") else 0,
            1 if form.get("compressed_gas_enabled") else 0,
            min(1.0, max(0.0, pct_field("compressed_ore_yield_pct"))),
            min(1.0, max(0.0, pct_field("compressed_gas_yield_pct"))),
            min(0.5, max(0.0, pct_field("compressed_tax_pct"))),
            hub_basis,
            structure_basis,
        ),
    )
    # Security is chosen as a band (high/low/null) and stored as a
    # canonical status inside that band — free-form statuses proved
    # error-prone (a stored 2.1 silently read as highsec).
    band_status = {"high": 1.0, "low": 0.25, "null": -0.5}
    for cls in config.ITEM_CLASSES:
        structure = form.get(f"{cls}_structure") or None
        band = form.get(f"{cls}_security_band")
        if band not in band_status or (
            cls == "reactions" and band == "high"
        ):
            band = "low" if cls == "reactions" else "high"
        # Thukker tier: component classes + structures (XL Thukker
        # structure rig, standard leg) — config.THUKKER_CLASSES.
        tiers = (
            ("none", "t1", "t2", "thukker")
            if cls in config.THUKKER_CLASSES
            else ("none", "t1", "t2")
        )
        me_rig = form.get(f"{cls}_me_rig")
        te_rig = form.get(f"{cls}_te_rig")
        c.execute(
            "UPDATE class_setting SET structure_type_id = ?, "
            "security = ?, me_rig = ?, te_rig = ?, "
            "system_cost_index = ?, tax_rate = ? WHERE item_class = ?",
            (
                int(structure) if structure else None,
                band_status[band],
                me_rig if me_rig in tiers else "none",
                te_rig if te_rig in tiers else "none",
                max(0.0, pct_field(f"{cls}_index_pct")),
                max(0.0, pct_field(f"{cls}_tax_pct")),
                cls,
            ),
        )
    c.commit()
    flash("settings saved")
    return redirect(url_for("settings"))


class LoginBroker:
    """PKCE verifiers held SERVER-SIDE, keyed by the SSO state value.

    They used to live in the Flask session cookie, which cannot work once
    the app runs in its own desktop window: RFC 8252 says a native app must
    send the user to their real browser for authorization, and the window
    and the browser are separate cookie jars. The callback would arrive
    carrying a state the browser's (empty) session could not match, and
    abort with "SSO state mismatch".

    Server-side, it no longer matters which browser finishes the flow. The
    app is a single local process serving one person, so a dict is the
    whole implementation; entries expire so an abandoned login does not
    linger.
    """

    TTL_SECONDS = 600

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, float]] = {}
        self._completed = 0
        self._character: str | None = None
        self._error: str | None = None

    def begin(self, state: str, verifier: str) -> None:
        with self._lock:
            self._expire()
            self._error = None
            self._pending[state] = (verifier, time.monotonic())

    def take(self, state: str) -> str | None:
        """One-shot: a state is redeemable exactly once."""
        with self._lock:
            self._expire()
            entry = self._pending.pop(state, None)
        return entry[0] if entry else None

    def succeeded(self, character: str) -> None:
        with self._lock:
            self._completed += 1
            self._character = character
            self._error = None

    def failed(self, message: str) -> None:
        with self._lock:
            self._error = message

    def status(self) -> dict:
        with self._lock:
            self._expire()
            return {
                "waiting": bool(self._pending),
                "completed": self._completed,
                "character": self._character,
                "error": self._error,
            }

    def _expire(self) -> None:
        cutoff = time.monotonic() - self.TTL_SECONDS
        for state, (_verifier, started) in list(self._pending.items()):
            if started < cutoff:
                del self._pending[state]


def _persistent_secret() -> str:
    """Stable session secret across restarts (a per-process random one
    invalidated every session — and broke in-flight SSO logins whenever
    the debug reloader fired mid-login).

    This runs from create_app(), before any route exists, so it must never
    raise: an unwritable data directory would otherwise kill a packaged
    build during construction — and a windowed build has no console to
    show the traceback in, so the user just sees a window that never
    opens. Falling back to a process-random secret costs only that
    sessions reset when Magoo restarts."""
    path = config.DATA_DIR / "secret_key"
    try:
        secret = path.read_text().strip()
        if secret:
            return secret
    except OSError:
        pass
    secret = pysecrets.token_hex(32)
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(secret)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "cannot persist the session secret to %s (%s) — logins will not "
            "survive a restart", path, exc
        )
    return secret


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("MAGOO_SECRET") or _persistent_secret()

    # The app binds to loopback but has no CSRF tokens: any web page could
    # otherwise drive-by POST to http://127.0.0.1:5000 (cross-site POST —
    # e.g. /pipelines/clear), and DNS rebinding defeats the loopback bind
    # because the dev server accepts any Host. Browsers always send Origin
    # on cross-site POSTs; same-origin fetch/form POSTs carry a loopback
    # Origin and pass. /sso/callback is a GET and unaffected.
    _LOCAL = ("127.0.0.1", "localhost", "::1")

    @app.before_request
    def _local_only():
        host = urlsplit("//" + request.host).hostname  # strips [::1] brackets and port
        if host not in _LOCAL:
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).hostname not in _LOCAL:
                abort(403)

    @app.errorhandler(sqlite3.OperationalError)
    def _db_busy(exc):
        # A write during a long SDE import would otherwise stall out the
        # 30s busy timeout and raw-500 with "database is locked".
        if "locked" not in str(exc):
            raise exc
        flash(
            "database is busy — an SDE import may be running; try again "
            "in a minute (nothing was saved)"
        )
        return redirect(request.referrer or url_for("dashboard"))

    @app.errorhandler(RuntimeError)
    def _runtime_error(exc):
        # Review 2026-09-05: store.ensure_schema refuses a database written
        # by a NEWER build with a RuntimeError whose message tells the user
        # exactly what to do (install the latest release) — but it fired
        # inside conn(), before any template could render, so a packaged
        # user saw a bare "Internal Server Error". Render the message on a
        # plain page instead. Deliberately no template: base.html's
        # context processor opens the database, which would raise again.
        # Still a 500 — the request genuinely failed and nothing was saved.
        log.error("request failed: %s", exc)
        message = escape(str(exc))
        return (
            "<!doctype html><title>Magoo — cannot continue</title>"
            "<body style=\"font-family:system-ui,sans-serif;max-width:40rem;"
            "margin:3rem auto;padding:0 1rem;line-height:1.5\">"
            "<h1 style=\"font-size:1.3rem\">Magoo cannot continue</h1>"
            f"<p>{message}</p>"
            "<p style=\"color:#666\">Nothing was changed. Close this window "
            "after reading the message above.</p></body>",
            500,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    # -- per-request database handles -----------------------------------

    # ensure_schema is not read-only (INSERT OR IGNORE seeds), so during a
    # background SDE import — whose single transaction holds the write
    # lock for minutes — running it per request would stall every page
    # ~30s into the "database is busy" flash. Ensure once per app per DB
    # path instead; reads then keep flowing off the old WAL snapshot.
    # The lock serializes the first-ever requests: two at once on a
    # brand-new DB used to race their schema writes into an instant
    # "database is locked" flash on the very first page a user sees.
    schema_ready: set[str] = set()
    schema_lock = threading.Lock()

    def conn():
        if "conn" not in g:
            g.conn = store.connect()
            if str(config.DB_PATH) not in schema_ready:
                with schema_lock:
                    if str(config.DB_PATH) not in schema_ready:
                        store.ensure_schema(
                            g.conn, profile=store.FIRST_RUN_PROFILE
                        )
                        schema_ready.add(str(config.DB_PATH))
                        # An installed build's reference tables can
                        # predate this version (v1.25 reads
                        # ref_compressible / portion_size, which a
                        # v1.24 import never wrote): re-import the
                        # game data now — from the cached archive
                        # when there is one — rather than leave the
                        # feature silently inert behind an "already
                        # up to date" button.
                        if sdeimport.ref_schema_outdated(g.conn):
                            log.info(
                                "reference tables predate this version "
                                "— re-importing the game data"
                            )
                            sde_job.start()
        return g.conn

    def ref():
        if "ref" not in g:
            g.ref = Refdata(conn())
        return g.ref

    def sde_ready() -> bool:
        """False on a fresh install: the ref_* tables exist only after the
        first SDE import, so pages must not join against them before then."""
        return ref().sde_build() is not None

    # -- background SDE import (the dashboard's Download button) ---------

    logins = LoginBroker()
    app.extensions["sso_logins"] = logins

    @app.get("/magoo/health")
    def magoo_health():
        """Identity probe for the launcher: if Magoo already owns the
        port, open a window onto the running instance instead of
        starting a second server. Deliberately DB-free, so it still
        answers while a long SDE import holds the write lock."""
        # pid, not port: the caller already knows which port it probed, and
        # reporting config.DEFAULT_PORT here would lie whenever --port moved
        # the server. pid is what actually helps when something is wedged.
        return jsonify(app="magoo", version=__version__, pid=os.getpid())

    def _after_sde_import():
        """Worker-thread hook (its own connection): re-derive the invention
        pipelines' materialised runs/ME/TE from the new build (review
        2026-09-01)."""
        c = store.connect()
        try:
            n = engine.rematerialize_invention(c, Refdata(c))
            if n:
                log.info(
                    "re-materialised %d invention pipeline(s) for the new "
                    "SDE build",
                    n,
                )
        except Exception:  # never let a hook wedge the import status
            log.exception("post-import invention re-materialisation failed")
        finally:
            c.close()

    sde_job = sdeimport.ImportJob(on_imported=_after_sde_import)
    app.extensions["sde_import"] = sde_job

    def sde_job_view() -> dict:
        status = sde_job.status()
        status["message"] = _sde_message(status)
        return status

    @app.post("/sde/import")
    def sde_import_start():
        # No sde_ready() guard on purpose: with a build already imported
        # this is the "check for updates" path (run_import no-ops when
        # the build is unchanged and its tables are current; a build
        # that predates this version is imported again).
        if sde_job.start():
            flash(
                "game data download started — progress shows on the "
                "dashboard checklist"
            )
        else:
            flash("a game data download is already running")
        return redirect(url_for("dashboard"))

    @app.get("/sde/status")
    def sde_status():
        # Deliberately DB-free: while the import transaction holds the
        # write lock, touching sqlite would stall this poll ~30s (see
        # conn() above), and _db_busy would turn it into an HTML 302.
        return jsonify(sde_job_view())

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("conn", None)
        g.pop("ref", None)
        if db is not None:
            try:
                # Keeps the -wal empty at rest so OneDrive's non-atomic
                # sync can't pair a mismatched sqlite/-wal. The short
                # timeout keeps teardown snappy when a long request (a
                # plan, an import) holds the write lock — the WAL drains
                # on the next idle teardown instead.
                db.execute("PRAGMA busy_timeout = 100")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
            db.close()

    @app.template_filter("isk")
    def isk(value):
        return f"{value:,.0f}" if value is not None else "—"

    @app.template_filter("qty")
    def qty(value):
        return f"{value:,}" if value is not None else "—"

    @app.template_filter("isk_unit")
    def isk_unit(value):
        """A UNIT price at full precision (v1.29 Buy tab): `isk` rounds to
        whole ISK, which renders a mineral at 4.87 and one at 5.02 alike —
        and telling those apart is what that tab is for. Tooltips only:
        the visible cell stays abbreviated (the Full-Figure Rule)."""
        return f"{value:,.2f}" if value is not None else "—"

    @app.template_filter("isk_short")
    def isk_short(value):
        """Abbreviated ISK for dense tables: 2.81B / 741.3M / 12.5K.
        Full figure belongs in a title= next to it."""
        if value is None:
            return "—"
        sign = "−" if value < 0 else ""
        v = abs(value)
        for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if v >= div:
                n = v / div
                return f"{sign}{n:,.2f}{unit}" if n < 100 else f"{sign}{n:,.1f}{unit}"
        return f"{sign}{v:,.0f}"

    # Section-header stats (2026-09-01): every job-table h2/h3 on Plan,
    # Chain and the Slot Planner reads "N items, S slots, X ISK" via the
    # _macros job_stats / buy_stats macros; these are their reducers.
    @app.template_filter("slots")
    def slots(rows):
        """Job slots a section's rows occupy — the jobs to run now
        (v1.27.1: the install check's figure where a row carries one)."""
        return sum(_jobs_to_run(r) for r in rows)

    @app.template_filter("build_value")
    def build_value(rows):
        """Σ build qty × unit price over a job-table section — the same
        price snapshot the Buy list prices from, so the figures reconcile
        across sections; the build qty is what the jobs to run now build
        (v1.27.1). Unpriced rows contribute 0 (the macro says so)."""
        return sum(
            _build_qty_to_run(r) * (r["price_snapshot"] or 0) for r in rows
        )

    @app.template_filter("buy_value")
    def buy_value(rows):
        """Σ buy qty × unit price — build_value's buy-side twin."""
        return sum(
            (r["recommended_buy_qty"] or 0) * (r["price_snapshot"] or 0)
            for r in rows
        )

    @app.template_filter("unpriced")
    def unpriced(rows):
        return sum(1 for r in rows if r["price_snapshot"] is None)

    @app.template_filter("pct")
    def pct(fraction):
        """Stored fraction -> percentage text for a form value: 0.05 -> 5,
        0.0025 -> 0.25, 0.01186 -> 1.186. Trailing zeros trimmed."""
        if fraction is None:
            return ""
        text = f"{fraction * 100:.4f}".rstrip("0").rstrip(".")
        return text or "0"

    @app.template_filter("age")
    def age(timestamp):
        """ISO timestamp -> '41m' / '2h 14m' / '3d 5h'; '—' for None.
        Naive timestamps are UTC (ESI snapshots store them that way)."""
        seconds = _age_seconds(timestamp)
        if seconds is None:
            return "—"
        m = int(seconds // 60)
        if m < 60:
            return f"{m}m"
        h, m = divmod(m, 60)
        if h < 48:
            return f"{h}h {m:02d}m"
        d, h = divmod(h, 24)
        return f"{d}d {h}h"

    def _age_seconds(timestamp):
        if not timestamp:
            return None
        try:
            t = datetime.fromisoformat(str(timestamp))
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())

    @app.template_filter("hours")
    def hours(seconds):
        return f"{seconds / 3600:.1f}h" if seconds else "—"

    STALE_SECONDS = 24 * 3600

    @app.template_filter("stale")
    def stale(timestamp):
        """The one freshness threshold, shared by the nav cluster and the
        dashboard pills: missing or older than STALE_SECONDS."""
        seconds = _age_seconds(timestamp)
        return seconds is None or seconds > STALE_SECONDS

    def nav_status():
        """Cheap freshness cluster for the nav bar: ESI snapshot age, price
        cache age, SDE build, corp wallet. Two small queries + one lookup;
        never loads the snapshot JSON."""
        c = conn()
        snap = c.execute(
            "SELECT fetched_at, corporation_isk FROM esi_snapshot "
            "ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        settings_ = store.get_settings(c)
        _n, prices_at = market.price_cache_state(
            c, settings_.price_region_id, settings_.price_source
        )
        esi_age = _age_seconds(snap["fetched_at"]) if snap else None
        px_age = _age_seconds(prices_at)
        return {
            "esi_at": snap["fetched_at"] if snap else None,
            "esi_stale": esi_age is None or esi_age > STALE_SECONDS,
            "prices_at": prices_at,
            "prices_stale": px_age is None or px_age > STALE_SECONDS,
            "corp_isk": snap["corporation_isk"] if snap else None,
            "sde_build": ref().sde_build(),
        }

    @app.context_processor
    def globals_():
        # magoo_version is a plain constant, deliberately not folded into
        # nav_status() — that runs two DB queries on every render and the
        # version costs nothing.
        return {
            "CLASS_LABELS": CLASS_LABELS,
            "nav_status": nav_status,
            "magoo_version": __version__,
            "update_banner": _update_banner,
        }

    def _update_banner():
        """Stored state only — never the network, so no page render waits on
        GitHub. The refresh happens on a worker thread."""
        try:
            return update.banner(conn())
        except sqlite3.Error:
            return None

    @app.post("/update/dismiss")
    def update_dismiss():
        version = request.form.get("version", "")
        if version:
            update.dismiss(conn(), version)
        return redirect(request.referrer or url_for("dashboard"))

    update_checked = threading.Event()

    @app.before_request
    def _kick_update_check():
        """Once per process, after the app is actually serving. Never blocks
        the request: the work happens on its own thread with its own
        connection."""
        if update_checked.is_set() or app.config.get("TESTING"):
            return
        update_checked.set()
        update.refresh_in_background(store.connect)


    # -- dashboard ------------------------------------------------------

    @app.route("/")
    def dashboard():
        c = conn()
        runs = _mark_superseded(
            c.execute(
                "SELECT * FROM index_run ORDER BY index_run_id DESC LIMIT 10"
            ).fetchall()
        )
        state = store.latest_esi_snapshot(c)
        settings_ = store.get_settings(c)
        return render_template(
            "dashboard.html",
            price_state=market.price_cache_state(
                c, settings_.price_region_id, settings_.price_source
            ),
            structure_price_state=market.structure_cache_state(
                c, settings_.structure_market()
            ),
            structure_label=settings_.structure_market_label(),
            sde_build=ref().sde_build(),
            sde_outdated=sdeimport.ref_schema_outdated(c),
            sde_job=sde_job_view(),
            pipelines=(
                c.execute(
                    "SELECT p.*, t.name AS product_name FROM pipeline p "
                    "JOIN ref_type t ON t.type_id = p.final_product_type_id "
                    "ORDER BY p.pipeline_id"
                ).fetchall()
                if sde_ready()
                else []
            ),
            characters=store.pool_characters(c),
            corps=c.execute(
                "SELECT * FROM esi_corp ORDER BY corporation_name"
            ).fetchall(),
            runs=runs,
            esi_state=state,
            # ?setup=1 (the Settings page links here) shows the first-run
            # checklist even after runs exist — a setup health check.
            show_setup=request.args.get("setup") == "1",
        )

    # -- pipelines ------------------------------------------------------

    def _parse_pipeline_line(
        line: str,
    ) -> tuple[str, int, int | None, int, int] | None:
        """One pasted row -> (name, qty, runs_per_bpc, me, te). Excel pastes
        tab-separated columns: product, quantity, runs/BPC, ME, TE. Trailing
        columns may be omitted, and an interior tab/comma column may be
        left blank (runs/BPC -> uncapped, ME/TE -> None, which the caller
        resolves: 0 for ships, the intermediate defaults for other
        products — v1.9). Comma- and space-separated rows work too (name
        may contain spaces)."""
        line = line.strip()
        if not line:
            return None
        if "\t" in line:
            fields = [f.strip() for f in line.split("\t")]
        elif "," in line:
            fields = [f.strip() for f in line.split(",")]
        else:
            tokens = line.split()
            numbers = []
            while tokens and tokens[-1].isdigit() and len(numbers) < 4:
                numbers.insert(0, tokens.pop())
            fields = [" ".join(tokens)] + numbers
        # Review 2026-09-05: only TRAILING empties are dropped. An empty
        # INTERIOR column is an omitted value in its own position — an
        # Excel row with a blank runs/BPC cell pastes as
        # "Ishtar\t40\t\t4\t8", and collapsing the blank used to shift ME
        # 4 into runs/BPC and TE 8 into ME. (The space-separated branch
        # never produces empties: it takes trailing digits only.)
        while fields and fields[-1] == "":
            fields.pop()
        if len(fields) < 2 or len(fields) > 5:
            raise ValueError(line)
        name, *numbers = fields
        if (
            not name
            or numbers[0] == ""
            or not all(n == "" or n.isdigit() for n in numbers)
        ):
            raise ValueError(line)

        def column(k: int) -> int | None:
            return (
                int(numbers[k])
                if len(numbers) > k and numbers[k] != ""
                else None
            )

        qty = int(numbers[0])
        runs_bpc = column(1)
        me = column(2)
        te = column(3)
        if (
            qty < 1
            or (runs_bpc is not None and runs_bpc < 1)
            or (me is not None and me > 10)
            or (te is not None and te > 20)
        ):
            raise ValueError(line)
        return name, qty, runs_bpc, me, te

    def _paste_default_me_te(final_id, settings_) -> tuple[int, int]:
        """The paste-contract ME/TE for a final with no explicit pin: ships
        0/0; any other product — structures, rigs, components, which often
        double as intermediates of other chains — the intermediate
        defaults, so an ME0 pin never leaks into every chain that consumes
        it (v1.9). The one home for the rule (review 2026-09-01: the
        paste, invention-off and the inline ME/TE edit each restated it)."""
        if ref().type_info(final_id).category_id == config.CATEGORY_SHIP:
            return 0, 0
        return (
            settings_.default_intermediate_me,
            settings_.default_intermediate_te,
        )

    def _pipeline_invention_context(c, rows) -> dict[int, dict]:
        """Per-pipeline invention context for the Pipelines page (v1.22):
        present for every invention-capable final (any source count since
        2026-08-31 — multi-source finals get a source select for the
        relic tier or T1 BPO). Carries just what the selects need — the
        option lists, the current choices, and the source name for the
        tooltip. (The nine-option economics comparison that used to
        render below each row was removed 2026-08-31 — user request; since
        2026-09-05 the row's compare button fetches the full source ×
        decryptor comparison into a dialog on demand — pipeline_compare.)"""
        if not rows or not sde_ready():
            return {}
        options = [
            {"value": str(d.type_id), "label": d.name}
            for d in ref().decryptors()
        ]
        options.insert(0, {"value": "none", "label": "No decryptor"})
        ctx: dict[int, dict] = {}
        for p in rows:
            sources = ref().invention_sources_for_product(
                p["final_product_type_id"]
            )
            chosen = ref().invention_source_for_product(
                p["final_product_type_id"],
                p["invention_source_blueprint_id"],
            )
            stale = bool(p["use_invention"]) and (
                costing.resolve_invention(ref(), p) is None
            )
            if not sources or stale:
                if p["use_invention"]:
                    # Stale config (SDE drift removed the sources, the
                    # stored source choice or the decryptor no longer
                    # resolves — costing.resolve_invention, the one rule):
                    # planning/costing already fall back to bpc_cost_isk,
                    # but the Off control must stay reachable or the
                    # materialized runs/ME/TE are stuck (v1.22 review).
                    ctx[p["pipeline_id"]] = {
                        "stale": True,
                        "options": [],
                        "sources": [],
                        "current": (
                            str(p["decryptor_type_id"])
                            if p["decryptor_type_id"]
                            else "none"
                        ),
                        "t1_name": None,
                    }
                continue
            # Shown source: the stored choice, else the highest-probability
            # one (Intact first) — the select's default, so a plain
            # decryptor change always posts a valid source.
            shown = chosen or sources[0]
            ctx[p["pipeline_id"]] = {
                "options": options,
                "sources": (
                    [
                        {
                            "value": str(s.t1_blueprint_id),
                            "label": ref().type_info(s.t1_blueprint_id).name,
                        }
                        for s in sources
                    ]
                    if len(sources) > 1
                    else []
                ),
                "current_source": str(shown.t1_blueprint_id),
                "current": (
                    (
                        str(p["decryptor_type_id"])
                        if p["decryptor_type_id"]
                        else "none"
                    )
                    if p["use_invention"]
                    else "off"
                ),
                "t1_name": ref().type_info(shown.t1_blueprint_id).name,
            }
        return ctx

    @app.route("/pipelines", methods=["GET", "POST"])
    def pipelines():
        c = conn()
        if request.method == "POST":
            if not sde_ready():
                flash(
                    "download the game data first (dashboard checklist) — "
                    "the parser needs blueprint data to recognize products"
                )
                return redirect(url_for("pipelines"))
            settings_ = store.get_settings(c)
            existing = {
                row["final_product_type_id"]: row["use_invention"]
                for row in c.execute(
                    "SELECT final_product_type_id, use_invention FROM pipeline"
                )
            }
            added, updated, errors = [], [], []
            invention_kept = []
            for line in request.form["products"].splitlines():
                try:
                    parsed = _parse_pipeline_line(line)
                except ValueError:
                    errors.append(f"can't parse: {line.strip()!r}")
                    continue
                if parsed is None:
                    continue
                name, qty, runs_bpc, me, te = parsed
                try:
                    type_id = ref().type_id(name)
                except KeyError:
                    errors.append(f"unknown item: {name!r}")
                    continue
                # Lookup is case-insensitive; store and report the
                # canonical name so 'astrahus' becomes the Astrahus row.
                name = ref().type_info(type_id).name
                blueprint = ref().blueprint_for_product(type_id)
                if blueprint is None:
                    errors.append(f"{name}: no blueprint — not buildable")
                    continue
                # Omitted ME/TE take the paste-contract defaults.
                default_me, default_te = _paste_default_me_te(type_id, settings_)
                if me is None:
                    me = default_me
                if te is None:
                    te = default_te
                if type_id in existing:
                    if existing[type_id]:
                        # v1.22: an invention pipeline's runs/ME/TE are
                        # materialized from the invention math — the paste
                        # updates the quantity only, never the overrides.
                        c.execute(
                            "UPDATE pipeline SET output_qty_per_run = ?, "
                            "modified_at = datetime('now') "
                            "WHERE final_product_type_id = ?",
                            (qty, type_id),
                        )
                        updated.append(name)
                        invention_kept.append(name)
                        continue
                    # Re-pasting the sheet updates the row in place.
                    c.execute(
                        "UPDATE pipeline SET output_qty_per_run = ?, "
                        "runs_per_bpc = ?, modified_at = datetime('now') "
                        "WHERE final_product_type_id = ?",
                        (qty, runs_bpc, type_id),
                    )
                    updated.append(name)
                else:
                    c.execute(
                        "INSERT INTO pipeline (name, final_product_type_id, "
                        "output_qty_per_run, runs_per_bpc) VALUES (?, ?, ?, ?)",
                        (name, type_id, qty, runs_bpc),
                    )
                    existing[type_id] = 0
                    added.append(name)
                store.set_blueprint_setting(c, blueprint.blueprint_id, me, te)
            c.commit()
            if added:
                flash(f"added {len(added)} pipeline(s): {', '.join(added)}")
            if updated:
                flash(f"updated {len(updated)}: {', '.join(updated)}")
            if invention_kept:
                flash(
                    f"{len(invention_kept)} invention pipeline(s) kept their "
                    f"computed runs/ME/TE (pasted values ignored): "
                    f"{', '.join(invention_kept)}"
                )
            for error in errors:
                flash(error)
            return redirect(url_for("pipelines"))
        rows = (
            c.execute(
                "SELECT p.*, t.name AS product_name, "
                "bs.me_level, bs.te_level FROM pipeline p "
                "JOIN ref_type t ON t.type_id = p.final_product_type_id "
                "LEFT JOIN ref_blueprint b ON b.product_id = "
                "  p.final_product_type_id AND b.activity_id = 1 "
                "LEFT JOIN blueprint_setting bs ON bs.blueprint_id = "
                "  b.blueprint_id "
                "ORDER BY p.pipeline_id"
            ).fetchall()
            if sde_ready()
            else []
        )
        return render_template(
            "pipelines.html",
            sde_ready=sde_ready(),
            pipelines=rows,
            invention=_pipeline_invention_context(c, rows),
        )

    @app.post("/pipelines/<int:pipeline_id>/toggle")
    def pipeline_toggle(pipeline_id):
        c = conn()
        c.execute(
            "UPDATE pipeline SET is_active = 1 - is_active, "
            "modified_at = datetime('now') WHERE pipeline_id = ?",
            (pipeline_id,),
        )
        c.commit()
        return redirect(url_for("pipelines"))

    def _delete_pipeline_rows(c, pipeline_id):
        # The final's ME/TE pin was written by the paste for THIS pipeline;
        # drop it with the pipeline so a stale pin cannot keep governing the
        # blueprint wherever it appears as an intermediate (v1.9).
        row = c.execute(
            "SELECT final_product_type_id FROM pipeline WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()
        if row is not None:
            blueprint = ref().blueprint_for_product(row["final_product_type_id"])
            if blueprint is not None:
                c.execute(
                    "DELETE FROM blueprint_setting WHERE blueprint_id = ?",
                    (blueprint.blueprint_id,),
                )
        c.execute(
            "DELETE FROM index_run_item_pipeline WHERE pipeline_id = ?",
            (pipeline_id,),
        )
        c.execute(
            "DELETE FROM index_run_invention WHERE pipeline_id = ?",
            (pipeline_id,),
        )
        c.execute(
            "DELETE FROM finished_batch WHERE pipeline_id = ?", (pipeline_id,)
        )
        c.execute("DELETE FROM pipeline WHERE pipeline_id = ?", (pipeline_id,))

    @app.post("/pipelines/<int:pipeline_id>/delete")
    def pipeline_delete(pipeline_id):
        c = conn()
        _delete_pipeline_rows(c, pipeline_id)
        c.commit()
        flash("pipeline deleted")
        return redirect(url_for("pipelines"))

    @app.post("/pipelines/clear")
    def pipelines_clear():
        c = conn()
        ids = [row["pipeline_id"] for row in c.execute("SELECT pipeline_id FROM pipeline")]
        for pipeline_id in ids:
            _delete_pipeline_rows(c, pipeline_id)
        c.commit()
        flash(f"cleared {len(ids)} pipeline(s)")
        return redirect(url_for("pipelines"))

    @app.post("/pipelines/<int:pipeline_id>/bpc_cost")
    def pipeline_bpc_cost(pipeline_id):
        c = conn()
        try:
            bpc_cost = max(0.0, _form_number(request.form, "bpc_cost"))
        except ValueError as exc:
            # Inline fetch() save: a flash would be consumed invisibly by
            # the redirected response while the tick shows "saved" — a
            # 422 makes inlineSave surface the error instead.
            return (f"invalid number in {exc} — nothing was saved", 422)
        c.execute(
            "UPDATE pipeline SET bpc_cost_isk = ?, "
            "modified_at = datetime('now') WHERE pipeline_id = ?",
            (bpc_cost, pipeline_id),
        )
        c.commit()
        return redirect(url_for("pipelines"))

    @app.post("/pipelines/<int:pipeline_id>/invention")
    def pipeline_invention(pipeline_id):
        """v1.22: set (or clear) a pipeline's invention choice. Enabling
        MATERIALIZES the derived values — runs_per_bpc on the pipeline and
        the invented ME/TE on the T2 manufacturing blueprint's
        blueprint_setting — so the planning path is untouched; disabling
        restores the paste-contract defaults (a custom researched ME/TE
        needs a re-paste — leftovers like ME 4 / TE 14 would silently
        misprice a bought-BPC pipeline)."""
        c = conn()
        pipeline = c.execute(
            "SELECT * FROM pipeline WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
        if pipeline is None:
            abort(404)
        choice = (request.form.get("decryptor") or "off").strip()
        final_id = pipeline["final_product_type_id"]
        if choice == "off":
            if not pipeline["use_invention"]:
                # Reachable since the source select exists (changing it
                # posts decryptor=off): running the restore UPDATE here
                # would NULL the user's manual runs_per_bpc (the stash is
                # empty while off).
                flash(
                    "invention is already off — pick a decryptor option "
                    "to enable it"
                )
                return redirect(url_for("pipelines"))
            # SET right-hand sides read the OLD row: runs_per_bpc gets the
            # stashed manual value back in the same statement.
            c.execute(
                "UPDATE pipeline SET runs_per_bpc = manual_runs_per_bpc, "
                "manual_runs_per_bpc = NULL, use_invention = 0, "
                "decryptor_type_id = NULL, "
                "invention_source_blueprint_id = NULL, "
                "modified_at = datetime('now') WHERE pipeline_id = ?",
                (pipeline_id,),
            )
            blueprint = ref().blueprint_for_product(final_id)
            if blueprint is not None:
                me, te = _paste_default_me_te(final_id, store.get_settings(c))
                store.set_blueprint_setting(c, blueprint.blueprint_id, me, te)
            c.commit()
            flash(
                "invention off — runs/BPC restored; ME/TE reset to paste "
                "defaults (re-paste the row to restore custom values)"
            )
            return redirect(url_for("pipelines"))
        sources = ref().invention_sources_for_product(final_id)
        if not sources:
            flash(
                f"{pipeline['name']} is not invention-capable — "
                "nothing was saved"
            )
            return redirect(url_for("pipelines"))
        if len(sources) == 1:
            source, chosen_id = sources[0], None  # column stays NULL = auto
        else:
            # Multi-source (T3 relic tiers, or several T1 BPOs): the form
            # must name a valid source. The select defaults to the
            # highest-probability one, so a plain decryptor change always
            # posts one.
            try:
                chosen_id = int((request.form.get("source") or "").strip())
            except ValueError:
                chosen_id = None
            source = next(
                (s for s in sources if s.t1_blueprint_id == chosen_id), None
            )
            if source is None:
                flash("pick an invention source — nothing was saved")
                return redirect(url_for("pipelines"))
        decryptor = None
        if choice != "none":
            try:
                decryptor = ref().decryptor(int(choice))
            except ValueError:
                decryptor = None
            if decryptor is None:
                flash("unknown decryptor — nothing was saved")
                return redirect(url_for("pipelines"))
        # Materialise the choice (runs/BPC, ME/TE pin, the runs stash) —
        # the same write the post-import re-derivation uses.
        me, te, runs = engine.materialize_invention(
            c, ref(), pipeline_id, source, decryptor, chosen_id
        )
        c.commit()
        chance = costing.invention_chance(
            ref(), store.get_settings(c), source, decryptor
        )
        info = ref().type_info(final_id)
        # Batch wording only where _size_jobs batches: exact-quantity ship
        # groups (capitals, freighters, jump freighters) never round up to
        # the copy's runs (review 2026-09-01).
        batches = (
            info.category_id == config.CATEGORY_SHIP
            and info.group_id not in config.EXACT_QTY_SHIP_GROUPS
        )
        flash(
            f"{pipeline['name']}: "
            + (
                f"{ref().type_info(source.t1_blueprint_id).name}, "
                if len(sources) > 1
                else ""
            )
            + f"{decryptor.name if decryptor else 'no decryptor'} — "
            f"{chance:.1%} chance, {runs}-run ME{me}/TE{te} copies"
            + (
                f" (a ship final builds in whole {runs}-hull batches)"
                if batches and runs > 1
                else ""
            )
        )
        return redirect(url_for("pipelines"))

    @app.get("/pipelines/<int:pipeline_id>/compare")
    def pipeline_compare(pipeline_id):
        """The invention comparison window (2026-09-05): every source ×
        decryptor option of one capable pipeline, costed at today's
        prices — the whole chain re-walked at each option's invented ME
        (a decryptor's ME modifier moves the materials bill, not just the
        invention line), that option's invention lines, and the final's
        net proceeds — rendered as an HTML fragment the Pipelines page
        fetches into its shared dialog on demand. The inline nine-option
        table was struck as clutter on 2026-08-31; fetched on demand the
        comparison costs the page GET nothing. Invention on, off or stale
        alike — the window is how the choice gets made. 404 for a final
        with no invention source."""
        c = conn()
        pipeline = c.execute(
            "SELECT * FROM pipeline WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
        if pipeline is None or not sde_ready():
            abort(404)
        r = ref()
        final_id = pipeline["final_product_type_id"]
        sources = r.invention_sources_for_product(final_id)
        if not sources:
            abort(404)
        settings_ = store.get_settings(c)
        class_settings = store.get_class_settings(c)
        # Price ids: THIS pipeline's chain — it may be inactive, and the
        # active-demand set would then miss its materials (structure
        # only; quantities and blacklisting do not change which types
        # can appear) — plus the invention inputs and EIV bases of every
        # capable pipeline, which demand_type_ids carries active or not.
        type_ids = set(bom.expand(r, final_id, 1)) | engine.demand_type_ids(
            c, r
        )
        prices, venues, _units, region_wide, adjusted = _price_maps(
            c, r, settings_, type_ids
        )
        region_wide = frozenset(region_wide)
        price, net, capital = sell_quote(c, settings_, final_id)
        resolved = costing.resolve_invention(r, pipeline)
        current = (
            (
                resolved[0].t1_blueprint_id,
                resolved[1].type_id if resolved[1] else None,
            )
            if resolved is not None
            else None
        )

        def landed(type_id):
            return costing.landed_price(
                r, settings_, prices.get(type_id), venues.get(type_id), type_id
            )

        rows = []
        for source in sources:
            for decryptor in (None, *r.decryptors()):
                choice = (source, decryptor)
                # invention_cost for the option's own figures (chance,
                # invented stats, cost per licensed run); the chain walk
                # for what a hull then costs end to end.
                inv = costing.invention_cost(
                    r, settings_, class_settings, source, decryptor,
                    price_of=landed, adjusted_of=adjusted.get,
                )
                cost = costing.current_hull_cost(
                    c, r, settings_, pipeline, prices, adjusted,
                    region_wide=region_wide, venues=venues, invention=choice,
                )
                margin = net - cost.total if net is not None else None
                rows.append(
                    {
                        "source_name": r.type_info(source.t1_blueprint_id).name,
                        "decryptor_name": (
                            decryptor.name if decryptor else "No decryptor"
                        ),
                        "chance": inv.probability,
                        "runs": inv.runs_per_copy,
                        "me": inv.me,
                        "te": inv.te,
                        "cost_per_run": inv.cost_per_run,
                        "invention_per_unit": cost.subtotal("invention"),
                        "cost": cost,
                        "margin": margin,
                        "current": current
                        == (
                            source.t1_blueprint_id,
                            decryptor.type_id if decryptor else None,
                        ),
                        "unpriced": cost.missing_prices,
                    }
                )
        # Best margin first (unpriced-quote rows keep their source /
        # decryptor order at the end); the top row is the recommendation.
        rows.sort(
            key=lambda row: row["margin"]
            if row["margin"] is not None
            else float("-inf"),
            reverse=True,
        )
        if rows and rows[0]["margin"] is not None:
            rows[0]["best"] = True
        info = r.type_info(final_id)
        _count, prices_at = market.price_cache_state(
            c, settings_.price_region_id, settings_.price_source
        )
        return render_template(
            "_invention_compare.html",
            name=info.name,
            rows=rows,
            multi=len(sources) > 1,
            price=price,
            net=net,
            capital=capital,
            qty=pipeline["output_qty_per_run"],
            prices_at=prices_at,
            current=current,
            # Batch wording only where _size_jobs batches (the save flash
            # applies the same rule): a ship final outside the
            # exact-quantity groups builds in whole runs-per-copy batches.
            batches=(
                info.category_id == config.CATEGORY_SHIP
                and info.group_id not in config.EXACT_QTY_SHIP_GROUPS
            ),
        )

    def _pipeline_or_422(c, pipeline_id):
        """The pipeline row for an inline edit of a value invention
        materializes (runs/BPC, ME, TE): 404 when missing, a 422 message
        while invention is on — those cells are derived from the choice
        and editing them would be silently overwritten."""
        pipeline = c.execute(
            "SELECT * FROM pipeline WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
        if pipeline is None:
            abort(404)
        if pipeline["use_invention"]:
            return pipeline, (
                "controlled by the invention choice — turn invention off "
                "to edit this",
                422,
            )
        return pipeline, None

    @app.post("/pipelines/<int:pipeline_id>/runs_per_bpc")
    def pipeline_runs_per_bpc(pipeline_id):
        """Inline edit (2026-09-01): runs on the final's blueprint copy.
        Blank = uncapped (NULL), else >= 1 — the paste column's contract."""
        c = conn()
        _pipeline, refusal = _pipeline_or_422(c, pipeline_id)
        if refusal:
            return refusal
        raw = (request.form.get("runs_per_bpc") or "").strip()
        try:
            runs = None if not raw else _form_number(request.form, "runs_per_bpc", int)
        except ValueError as exc:
            return (f"invalid number in {exc} — nothing was saved", 422)
        if runs is not None and runs < 1:
            return ("runs per BPC must be at least 1 — nothing was saved", 422)
        c.execute(
            "UPDATE pipeline SET runs_per_bpc = ?, "
            "modified_at = datetime('now') WHERE pipeline_id = ?",
            (runs, pipeline_id),
        )
        c.commit()
        return redirect(url_for("pipelines"))

    @app.post("/pipelines/<int:pipeline_id>/me_te")
    def pipeline_me_te(pipeline_id):
        """Inline edit (2026-09-01): the final's blueprint ME and/or TE
        (each input posts alone). Writes the same blueprint_setting pin the
        paste does, keeping whichever level was not posted; a missing pin
        starts from the paste-contract defaults (ships 0/0, else the
        intermediate defaults). Same clamps as the paste: ME 0-10, TE 0-20."""
        c = conn()
        pipeline, refusal = _pipeline_or_422(c, pipeline_id)
        if refusal:
            return refusal
        final_id = pipeline["final_product_type_id"]
        blueprint = ref().blueprint_for_product(final_id)
        if blueprint is None:
            return ("no blueprint for this product — nothing was saved", 422)
        default_me, default_te = _paste_default_me_te(
            final_id, store.get_settings(c)
        )
        current = c.execute(
            "SELECT me_level, te_level FROM blueprint_setting "
            "WHERE blueprint_id = ?",
            (blueprint.blueprint_id,),
        ).fetchone()
        me = current["me_level"] if current else default_me
        te = current["te_level"] if current else default_te
        try:
            if (request.form.get("me") or "").strip():
                me = _form_number(request.form, "me", int)
                if not 0 <= me <= 10:
                    return ("ME must be 0-10 — nothing was saved", 422)
            elif (request.form.get("te") or "").strip():
                te = _form_number(request.form, "te", int)
                if not 0 <= te <= 20:
                    return ("TE must be 0-20 — nothing was saved", 422)
            else:
                return ("empty value — nothing was saved", 422)
        except ValueError as exc:
            return (f"invalid number in {exc} — nothing was saved", 422)
        store.set_blueprint_setting(c, blueprint.blueprint_id, me, te)
        c.commit()
        return redirect(url_for("pipelines"))

    @app.post("/pipelines/<int:pipeline_id>/qty")
    def pipeline_qty(pipeline_id):
        c = conn()
        # The inline fetch() save bypasses the HTML min="1": clamp so a
        # zero/negative qty can't make the pipeline silently vanish from
        # plans. An emptied or junk field is refused with a 422 (NOT
        # silently overwritten) so inlineSave shows the error instead of
        # a false "saved" tick.
        try:
            if not (request.form.get("qty") or "").strip():
                raise ValueError("qty")
            qty_ = max(1, _form_number(request.form, "qty", int))
        except ValueError as exc:
            return (f"invalid number in {exc} — nothing was saved", 422)
        c.execute(
            "UPDATE pipeline SET output_qty_per_run = ?, "
            "modified_at = datetime('now') WHERE pipeline_id = ?",
            (qty_, pipeline_id),
        )
        c.commit()
        return redirect(url_for("pipelines"))

    # -- settings -------------------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        c = conn()
        if request.method == "POST":
            try:
                response = _settings_save(c, request.form)
            except ValueError as exc:
                # One bad field ("1,5b", an emptied autofill) must not
                # 500 and discard the whole ~40-field save — flash which
                # input was bad, like the pipeline-paste path does.
                flash(f"invalid number in {exc} — nothing was saved")
                return redirect(url_for("settings"))
            # Revision 4 (contract review A12): the matcher's lines read
            # settings — the purchase venues (price region, structure
            # market), the unplanned-ore conversion (yields, refining tax)
            # and, on runs planned before their columns, the live inbound
            # rates — so re-derive them now rather than leave the Buy tab
            # stale until the next ESI update. Executed runs keep their
            # frozen conversions (buying's A9 freeze). A failure is
            # flashed; the save itself has already committed.
            message, ok = _assign_purchases(c)
            if not ok and message:
                flash(message)
            return response
        settings_obj = store.get_settings(c)
        return render_template(
            "settings.html",
            settings=c.execute("SELECT * FROM settings WHERE id = 1").fetchone(),
            broker_rate=costing.broker_fee_rate(settings_obj),
            sales_tax=costing.sales_tax_rate(settings_obj),
            class_settings={
                row["item_class"]: row
                for row in c.execute("SELECT * FROM class_setting")
            },
            # Display order: alphabetical by label (user request
            # 2026-08-31). config.ITEM_CLASSES itself stays append-only —
            # seeding and the save loop key by name, not position.
            item_classes=sorted(config.ITEM_CLASSES, key=CLASS_LABELS.get),
            thukker_classes=config.THUKKER_CLASSES,
            structures=STRUCTURE_CHOICES,
            tracked=(
                c.execute(
                    "SELECT t.solar_system_id, s.name FROM tracked_system t "
                    "LEFT JOIN ref_solar_system s "
                    "  ON s.system_id = t.solar_system_id "
                    "ORDER BY s.name"
                ).fetchall()
                if sde_ready()
                else []
            ),
            blacklist_categories=config.BLACKLIST_CATEGORIES,
            blacklist_checked=store.blacklist_categories(c),
            blacklist_items=(
                c.execute(
                    "SELECT b.type_id, t.name FROM blacklist_item b "
                    "JOIN ref_type t USING (type_id) ORDER BY t.name"
                ).fetchall()
                if sde_ready()
                else []
            ),
        )

    @app.post("/settings/blacklist/categories")
    def blacklist_categories_save():
        c = conn()
        keys = {
            key
            for key, _label, _groups in config.BLACKLIST_CATEGORIES
            if request.form.get(f"bl_{key}")
        }
        store.set_blacklist_categories(c, keys)
        flash(f"blacklist: {len(keys)} categor{'y' if len(keys) == 1 else 'ies'} checked")
        return redirect(url_for("settings"))

    @app.post("/settings/blacklist/items")
    def blacklist_item_add():
        c = conn()
        if not sde_ready():
            flash(
            "download the game data first — the dashboard checklist "
            "has the button"
        )
            return redirect(url_for("settings"))
        name = request.form["item"].strip()
        try:
            type_id = ref().type_id(name)
        except KeyError:
            flash(f"unknown item: {name!r}")
            return redirect(url_for("settings"))
        if ref().blueprint_for_product(type_id) is None:
            flash(f"{name} isn't buildable — nothing to blacklist")
            return redirect(url_for("settings"))
        c.execute("INSERT OR IGNORE INTO blacklist_item VALUES (?)", (type_id,))
        c.commit()
        flash(f"blacklisted: {name}")
        return redirect(url_for("settings"))

    @app.post("/settings/blacklist/items/<int:type_id>/delete")
    def blacklist_item_delete(type_id):
        c = conn()
        c.execute("DELETE FROM blacklist_item WHERE type_id = ?", (type_id,))
        c.commit()
        return redirect(url_for("settings"))

    @app.post("/settings/systems")
    def tracked_add():
        c = conn()
        if not sde_ready():
            flash(
            "download the game data first — the dashboard checklist "
            "has the button"
        )
            return redirect(url_for("settings"))
        name = request.form["system"].strip()
        row = ref().solar_system_by_name(name)
        if row is None:
            flash(f"unknown solar system: {name!r}")
        else:
            c.execute(
                "INSERT OR IGNORE INTO tracked_system VALUES (?)",
                (row["system_id"],),
            )
            c.commit()
            flash(f"tracking {row['name']}")
        return redirect(url_for("settings"))

    @app.post("/settings/systems/<int:system_id>/delete")
    def tracked_delete(system_id):
        c = conn()
        c.execute(
            "DELETE FROM tracked_system WHERE solar_system_id = ?", (system_id,)
        )
        c.commit()
        return redirect(url_for("settings"))

    # -- characters and SSO ---------------------------------------------

    @app.route("/characters")
    def characters():
        c = conn()
        characters_ = c.execute(
            "SELECT p.*, t.expires_at, t.scopes FROM pool_character p "
            "LEFT JOIN esi_token t USING (character_id)"
        ).fetchall()
        return render_template(
            "characters.html",
            characters=characters_,
            # v1.27.0: which scopes each stored login still lacks (a token
            # records the JWT's scp claim and a refresh never widens it).
            missing_scopes={
                r["character_id"]: esi.missing_scopes(r["scopes"]) for r in characters_
            },
            sales_prov=store.sales_pull_state(c),
            character_names={r["character_id"]: r["character_name"] for r in characters_},
            corps=c.execute(
                "SELECT ec.*, pa.character_name AS assets_via_name, "
                "pj.character_name AS jobs_via_name, "
                "pw.character_name AS wallet_via_name "
                "FROM esi_corp ec "
                "LEFT JOIN pool_character pa ON pa.character_id = ec.assets_via "
                "LEFT JOIN pool_character pj ON pj.character_id = ec.jobs_via "
                "LEFT JOIN pool_character pw ON pw.character_id = ec.wallet_via "
                "ORDER BY ec.corporation_name"
            ).fetchall(),
        )

    @app.post("/characters/<int:character_id>/toggle/<flag>")
    def character_toggle(character_id, flag):
        if flag not in (
            "include_assets", "include_job_slots", "count_assets",
            "count_sales", "count_buys",
        ):
            abort(400)
        c = conn()
        c.execute(
            f"UPDATE pool_character SET {flag} = 1 - {flag} "
            "WHERE character_id = ?",
            (character_id,),
        )
        c.commit()
        if flag == "count_buys":
            _rematch_after_buys_toggle(c)
        return redirect(url_for("characters"))

    @app.post("/characters/<int:character_id>/delete")
    def character_delete(character_id):
        """Remove a character from the pool: its token and its row.

        esi_corp records which character's token last pulled each corp feed,
        so references to this one are cleared too — a deleted id would
        otherwise render as a blank "via" until the next refresh re-derives
        it.

        This does NOT revoke the authorisation at CCP's end. That lives in
        the user's EVE account settings, and a network call that can hang or
        fail has no business standing between someone and deleting their own
        local data.
        """
        c = conn()
        row = c.execute(
            "SELECT character_name FROM pool_character WHERE character_id = ?",
            (character_id,),
        ).fetchone()
        if row is None:
            abort(404)

        # Warn before the update, while the references still exist: losing
        # the only character with corp roles silently stops corp data.
        stranded = [
            r["corporation_name"]
            for r in c.execute(
                "SELECT corporation_name FROM esi_corp "
                "WHERE ? IN (assets_via, jobs_via, wallet_via)",
                (character_id,),
            )
        ]

        sales_stranded = [
            r["corporation_name"]
            for r in c.execute(
                "SELECT DISTINCT ec.corporation_name FROM sales_pull sp "
                "JOIN esi_corp ec ON ec.corporation_id = sp.owner_id "
                "WHERE sp.owner_kind = 'corporation' AND sp.via_character_id = ? "
                "AND sp.status = 'ok'",
                (character_id,),
            )
            if r["corporation_name"] and r["corporation_name"] not in stranded
        ]

        c.execute("DELETE FROM esi_token WHERE character_id = ?", (character_id,))
        c.execute(
            "DELETE FROM pool_character WHERE character_id = ?", (character_id,)
        )
        c.execute(
            "UPDATE esi_corp SET "
            "assets_via = CASE WHEN assets_via = ? THEN NULL ELSE assets_via END, "
            "jobs_via   = CASE WHEN jobs_via   = ? THEN NULL ELSE jobs_via   END, "
            "wallet_via = CASE WHEN wallet_via = ? THEN NULL ELSE wallet_via END",
            (character_id, character_id, character_id),
        )
        # v1.27.0: the Ledger's provenance columns are informational; the
        # rows and the sales they cover stay (item fetches pick a token at
        # fetch time, so nothing depended on this character).
        c.execute(
            "UPDATE sales_pull SET via_character_id = NULL WHERE via_character_id = ?",
            (character_id,),
        )
        c.execute(
            "UPDATE sale_contract SET via_character_id = NULL WHERE via_character_id = ?",
            (character_id,),
        )
        # v1.29 revision 3 (C9): the bought side keeps the same column.
        c.execute(
            "UPDATE buy_contract SET via_character_id = NULL WHERE via_character_id = ?",
            (character_id,),
        )
        c.commit()
        # A character that left the pool no longer counts its buys
        # (store.buys_enabled_owners) and no longer makes a contract
        # internal, exactly like a Count buys toggle: re-match now, or its
        # lines keep costing runs until the next ESI update (review
        # 2026-09-28).
        _rematch_after_buys_toggle(c)

        message = f"removed {row['character_name']}"
        if stranded:
            message += (
                " — it was pulling corporation data for "
                + ", ".join(stranded)
                + "; another character with the same corp roles must be "
                "logged in for that to keep updating"
            )
        if sales_stranded:
            message += (
                " — it was also reading the sales feeds of "
                + ", ".join(sales_stranded)
                + "; another member with the same roles keeps the Ledger current"
            )
        flash(message)
        return redirect(url_for("characters"))

    @app.post("/corps/<int:corporation_id>/toggle/<flag>")
    def corp_toggle(corporation_id, flag):
        if flag not in (
            "count_assets", "count_wallet", "count_jobs", "count_sales",
            "count_buys",
        ):
            abort(400)
        c = conn()
        c.execute(
            f"UPDATE esi_corp SET {flag} = 1 - {flag} "
            "WHERE corporation_id = ?",
            (corporation_id,),
        )
        c.commit()
        if flag == "count_buys":
            _rematch_after_buys_toggle(c)
        return redirect(url_for("characters"))

    def _rematch_after_buys_toggle(c) -> None:
        """Count buys is honoured when purchases are matched (contract
        review C7: buys are always stored), so the runs' purchase lines —
        and the realized cost they feed — only follow the toggle on a
        matching pass. Matching reads local rows only, so run it now rather
        than leave the Buy tab stale until the next ESI update. A failure
        is flashed; the toggle itself has already committed."""
        message, ok = _assign_purchases(c)
        if not ok and message:
            flash(message)

    @app.route("/sso/login")
    def sso_login():
        """Start an EVE login in the user's OWN browser.

        RFC 8252 is explicit that a native app must not run authorization
        in an embedded view: the host app can read the credentials and the
        DOM. Sending people to their real browser also means they can see
        they are on login.eveonline.com — which EVE players are rightly
        trained to check — and their password manager works.
        """
        url, verifier, state = esi.authorize_url()
        logins.begin(state, verifier)
        try:
            opened = bool(webbrowser.open(url))
        except OSError:
            opened = False
        if not opened:
            log.warning("could not open a browser for EVE SSO")
        return render_template(
            "sso_waiting.html",
            auth_url=url,
            opened=opened,
            baseline=logins.status()["completed"],
        )

    @app.get("/sso/status")
    def sso_status():
        """Polled by the waiting page. The login completes in a different
        browser from the one showing that page, so this is how the window
        finds out it succeeded."""
        return jsonify(logins.status())

    @app.route("/sso/callback")
    def sso_callback():
        """Lands in the SYSTEM browser, not the Magoo window — so it must
        stand on its own rather than redirect into the app."""
        error = request.args.get("error_description") or request.args.get(
            "error"
        )
        if error:
            logins.failed(error)
            return render_template("sso_done.html", error=error), 400
        verifier = logins.take(request.args.get("state", ""))
        if not verifier:
            message = (
                "This login has expired or was already used. Start it again "
                "from Magoo."
            )
            logins.failed(message)
            return render_template("sso_done.html", error=message), 400
        try:
            _id, name = esi.complete_login(
                conn(), request.args.get("code", ""), verifier
            )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.exception("SSO token exchange failed")
            logins.failed(str(exc))
            return render_template("sso_done.html", error=str(exc)), 400
        logins.succeeded(name)
        return render_template("sso_done.html", character=name)

    # -- index runs -----------------------------------------------------

    @app.post("/esi/refresh")
    def esi_refresh():
        """Five independently guarded steps: the slow snapshot pull
        (assets, jobs, wallets), the Ledger's sales pull (v1.27.0 — since
        v1.29 revision 3 it also stores the pool's wallet BUYS and the item
        exchanges it accepted), the price refresh (user request 2026-09-08
        — one button keeps the Ledger's quotes, undercut verdicts and
        contract splits current), the re-plan of the open run in place
        (v1.29 revision 6, user ruling R1 2026-09-28 — _replan_open_run:
        one run per buying cycle, kept live; skipped with a note when no
        run is open, the snapshot did not refresh or there is nothing to
        plan from — since revision 7 (user ruling 2026-09-29) never
        because the cycle's final jobs are installing: the plan counts
        them as this cycle's wave and sizes the rest), then purchase
        matching
        (buying.assign_purchases, contract review C1): it runs AFTER the
        prices so a bought contract's reference prices (R7) come from this
        refresh, and even when ESI is down, since it reads local rows only.
        The snapshot has committed before the sales step starts, the sales
        step never raises for scope, role, token, rate-limit or network
        trouble (each owner × family records its own status), a price
        failure only leaves the cache unchanged, a re-plan failure rolls
        back and leaves the run's previous plan, and a matching failure
        leaves every run's purchases as they were, so no step can lose
        another's data. One flash carries all five outcomes;
        `next=ledger` returns to the Ledger tab instead of the dashboard."""
        import time as _time

        if request.form.get("next") == "ledger":
            target = url_for("ledger", window=request.form.get("window") or None)
        else:
            target = url_for("dashboard")
        if not sde_ready():
            flash(
                "download the game data first (dashboard checklist) — the "
                "refresh classifies assets and jobs against its item data"
            )
            return redirect(target)
        parts = []
        esi_down = False
        snapshot_fresh = False
        t0 = _time.monotonic()
        try:
            state = esi.refresh_state(conn(), ref())
            snapshot_fresh = True
            parts.append(
                f"ESI refreshed in {_time.monotonic() - t0:.0f}s: "
                f"{len(state['on_hand'])} types on hand, "
                f"{sum(state['active_jobs'].values())} active jobs"
            )
        except httpx.TransportError as exc:
            # ESI unreachable: the sales pull would only stack failures.
            parts.append(
                f"ESI refresh failed ({exc}) — the previous snapshot is "
                "unchanged; try again once ESI recovers"
            )
            esi_down = True
        except httpx.HTTPError as exc:
            parts.append(
                f"ESI refresh failed ({exc}) — the previous snapshot is "
                "unchanged; try again once ESI recovers"
            )
        except RuntimeError as exc:
            # A dead refresh token: retrying cannot help, so surface the
            # re-auth guidance alone without the "once ESI recovers" tail.
            # Other characters' sales feeds may still answer, so step 2 runs.
            parts.append(f"{exc} — the previous snapshot is unchanged")
        if esi_down:
            ledger.mark_skipped(conn(), "ESI unavailable — sales pull skipped")
            parts.append("sales pull skipped — ESI unavailable")
        else:
            try:
                parts.append(ledger.summary_line(ledger.pull_sales(conn(), ref())))
            except Exception as exc:  # noqa: BLE001 — the _after_sde_import precedent
                # A bug in the sales step must not 500 a request whose
                # snapshot already saved; the stored ledger is untouched.
                log.exception("sales pull failed")
                parts.append(
                    f"sales pull failed ({exc}) — the stored ledger is unchanged"
                )
        if esi_down:
            parts.append("prices not refreshed — ESI unavailable")
        else:
            try:
                price_message, _ok = _refresh_prices_now()
            except Exception as exc:  # noqa: BLE001 — same guard as the sales step
                log.exception("price refresh failed")
                price_message = f"price refresh failed ({exc}) — cached prices are unchanged"
            parts.append(price_message)
        # v1.29 revision 6 (ruling R1): the open run follows the update —
        # re-planned in place from what was just pulled, BEFORE matching,
        # so contract k and the unplanned-ore conversion see the fresh
        # plan. Guarded inside; its clause says when and why it skipped.
        parts.append(_replan_open_run(conn(), snapshot_fresh))
        match_message, _ok = _assign_purchases(conn())
        if match_message:
            parts.append(match_message)
        flash(" — ".join(parts))
        return redirect(target)

    def _refresh_prices_now():
        """The price pull (parallel, ESI-guideline compliant), shared by
        the ⟳ Refresh prices button and — since v1.27.0 (user request) —
        the third step of ⟳ Update from ESI. Returns (message, ok): ok is
        False when nothing was refreshed and the message says why. Callers
        check sde_ready() first."""
        import time as _time

        c = conn()
        r = ref()
        # Two id sets (review 2026-09-01): the per-type order-book pull
        # takes the MARKET set; the adjusted-price store also takes the
        # invention EIV bases, which only CCP's bulk adjusted feed prices.
        # Capable INACTIVE pipelines contribute invention ids by design, so
        # an empty market set means there is nothing at all to price.
        type_ids = engine.demand_type_ids(c, r)
        market_ids = engine.market_type_ids(c, r)
        # v1.29 revision 3 (contract review C10.1): a bought contract's
        # items are priced by reference (R7) — Jita's best sell, else CCP's
        # adjusted price. Only the demand set has sell rows, so every item
        # outside the plan leans on the adjusted fallback; without its
        # adjusted price it would be unpriced and the contract's whole ISK
        # would land on the plan's items. CCP's bulk feed already carries
        # every type: widening the stored set costs no extra call.
        adjusted_ids = set(type_ids) | {
            row[0] for row in c.execute(
                "SELECT DISTINCT type_id FROM buy_contract_item"
            )
        }
        if not market_ids:
            return "nothing to price — add or activate a pipeline first", False
        settings_ = store.get_settings(c)
        t0 = _time.monotonic()
        # v1.9: raw leaves (no blueprint) with no hub-station order fall
        # back to the region-wide best order — in the price region itself
        # (one region setting since 2026-08-23; same pull, no extra call).
        raw_leaves = {
            t for t in market_ids if r.blueprint_for_product(t) is None
        }
        # v1.25 fill pricing: every input the plan may buy (compressed
        # candidates included) also persists its hub SELL ladder — the
        # depth the sourcing pass walks. Same paged pull, rows only.
        ladder_ids = set(market_ids)
        # v1.29 revision 4 (contract §5, review A14): the contract items
        # still waiting to be priced get a Jita order-book pull too, so k
        # spreads a contract of ore the plan never considered over a real
        # Jita sell quote rather than CCP's adjusted price. Order books
        # only: NOT raw_leaves (R7 drops region-wide rows anyway, C10.2)
        # and NOT ladder_ids (a kit's items need no stored ladder). Bounded
        # to unfrozen contracts' received non-BPC items — each id is one
        # more pull, and a priced contract never re-prices. The ESI update
        # pulls the ledger, then prices, then matches, so a contract
        # pulled in an update is quoted before it is priced and frozen.
        # Known limit: under the Max Buy Order hub basis the pull is the
        # 'buy' side only, so there is no sell row and R7 keeps the
        # adjusted fallback.
        contract_ids = {
            row[0] for row in c.execute(
                "SELECT DISTINCT i.type_id FROM buy_contract_item i "
                "JOIN buy_contract bc USING (contract_id) "
                "WHERE bc.priced_at IS NULL AND bc.items_status = 'ok' "
                "AND i.is_included = 1 "
                "AND COALESCE(i.raw_quantity, 0) != -2"
            )
        }
        try:
            fetched, skipped, fresh = market.refresh_prices(
                c,
                settings_.price_region_id,
                sorted(set(market_ids) | contract_ids),
                settings_.price_source,
                fallback_type_ids=raw_leaves,
                fallback_region_id=settings_.price_region_id,
                ladder_type_ids=ladder_ids,
            )
        except httpx.HTTPError as exc:
            return (
                f"price refresh failed ({exc}) — cached prices are "
                "unchanged; try again once ESI recovers"
            ), False
        try:
            n_adjusted = market.store_adjusted_prices(
                c, adjusted_ids, market.fetch_adjusted_prices()
            )
        except httpx.HTTPError as exc:
            return (
                f"regional prices refreshed ({fetched} fetched, {skipped} "
                f"skipped) but the adjusted-price pull failed ({exc}) — "
                "install-fee bases keep their previous values"
            ), False
        message = (
            f"prices refreshed in {_time.monotonic() - t0:.0f}s: "
            f"{fetched} fetched, {fresh} already current, "
            f"{n_adjusted} adjusted prices"
        )
        if skipped:
            message += f" — {skipped} skipped (throttled), refresh again later"
        region_priced = market.region_wide_types(
            c, settings_.price_region_id, raw_leaves, settings_.price_source
        )
        if region_priced:
            message += (
                f" — {len(region_priced)} raw input(s) priced region-wide "
                "(no hub-station order)"
            )
        if ladder_ids:
            n_ladders = len(
                market.cached_hub_ladders(
                    c, settings_.price_region_id, ladder_ids
                )
            )
            message += (
                f" — Jita sell ladders for {n_ladders}/{len(ladder_ids)} "
                "inputs"
            )

        # One structure-market pull (the whole book comes down regardless)
        # serves three consumers: v1.6 capital-class finals' SELL quotes,
        # v1.10 buy quotes + sell ladders for every input the plan may buy
        # (the Jita-vs-structure landed comparison at plan time), and since
        # v1.27.0 the SELL quote of every other pipeline final — the Ledger
        # judges an open order against the book it sits in, and most
        # sub-capital orders sit at the structure market too (Planning
        # still prices sub-caps at Jita; this only keeps rows the pull
        # already downloaded). Finals of every pipeline, active or not: the
        # Ledger tracks them all.
        finals = {
            p["final_product_type_id"] for p in store.active_pipelines(c)
        }
        all_finals = {
            r_["final_product_type_id"]
            for r_ in c.execute("SELECT final_product_type_id FROM pipeline")
        }
        capital_finals = [
            t for t in finals if costing.is_capital_priced(r, t)
        ]
        subcap_finals = [
            t for t in all_finals if t not in capital_finals
        ]
        # Inputs are wanted whenever the book is pulled at all — even with
        # the comparison switched off — so turning it on later works from
        # the existing cache instead of silently comparing against nothing.
        inputs = [t for t in type_ids if t not in all_finals]
        wanted = capital_finals + subcap_finals + inputs  # disjoint by construction
        if settings_.structure_buy_enabled or capital_finals:
            structure_id = settings_.structure_market()
            character_id = esi.character_with_scope(
                c, esi.STRUCTURE_MARKETS_SCOPE
            )
            if character_id is None:
                message += (
                    " — structure market skipped: no character has the "
                    "structure-markets scope (log one in again via Characters)"
                )
            else:
                try:
                    market.refresh_structure_prices(
                        c, structure_id, wanted, character_id
                    )
                except Exception as exc:
                    # 403 = no docking access; keep the regional refresh.
                    message += f" — structure market failed: {exc}"
                else:
                    if capital_finals:
                        n_caps = len(market.cached_prices(
                            c, structure_id, capital_finals,
                            market.STRUCTURE_SOURCE,
                        ))
                        message += (
                            f", {n_caps}/{len(capital_finals)} capital hulls "
                            "quoted from the structure market"
                        )
                    if inputs and settings_.structure_buy_enabled:
                        n_inputs = len(market.cached_prices(
                            c, structure_id, inputs, market.STRUCTURE_SOURCE
                        ))
                        message += (
                            f", {n_inputs}/{len(inputs)} inputs quoted at "
                            "the structure market"
                        )
                    if subcap_finals:
                        n_sub = len(market.cached_prices(
                            c, structure_id, subcap_finals, market.STRUCTURE_SOURCE
                        ))
                        message += (
                            f", {n_sub}/{len(subcap_finals)} sub-capital hulls "
                            "quoted there for the Ledger"
                        )
        return message, True

    @app.post("/prices/refresh")
    def prices_refresh():
        """The slow price pull, decoupled from planning like the ESI
        snapshot refresh (and folded into it since v1.27.0)."""
        if not sde_ready():
            flash(
                "download the game data first — the dashboard checklist "
                "has the button"
            )
            return redirect(url_for("dashboard"))
        message, ok = _refresh_prices_now()
        flash(message)
        if not ok and message.startswith("nothing to price"):
            return redirect(url_for("pipelines"))
        # The Planning → Profit view's refresh button lands back on the
        # numbers it just refreshed ("profit" kept for old form values).
        if request.form.get("next") in ("planning", "profit"):
            return redirect(url_for("planning"))
        return redirect(url_for("dashboard"))

    def _price_maps(c, r, settings_, type_ids):
        """The cached price maps for one id set — (prices, buy_venue,
        structure_units_cheaper, region_wide, adjusted): the block /run,
        Planning and Invention share (review 2026-09-01)."""
        prices, buy_venue, structure_units_cheaper, region_wide = (
            market.quote_maps(market.buy_quotes(c, r, settings_, type_ids))
        )
        adjusted = market.cached_adjusted_prices(c, type_ids)
        return prices, buy_venue, structure_units_cheaper, region_wide, adjusted

    @app.route("/invention")
    def invention():
        """Invention — the live BPC stockpile workbench (v1.23): what to
        invent, copy and buy, from CURRENT stock, in-flight lab jobs and
        today's cached prices. Never persisted; index runs keep only the
        amortized invention cost vintage."""
        c = conn()
        settings_ = store.get_settings(c) if sde_ready() else None

        def empty(reason):
            return render_template(
                "invention.html", data=None, settings=settings_, reason=reason
            )

        if not sde_ready():
            return empty(
                "Download the game data first — the Dashboard checklist "
                "has the button"
            )
        r = ref()
        # Empty states first — no pricing for a tab with nothing to show,
        # and "enabled but inactive/stale" named as such (review 2026-09-01).
        if not engine._invention_configs(c, r):
            enabled = c.execute(
                "SELECT SUM(is_active = 0) AS inactive, "
                "SUM(is_active = 1) AS active FROM pipeline "
                "WHERE use_invention = 1"
            ).fetchone()
            if enabled["inactive"]:
                return empty(
                    f"invention is enabled on {enabled['inactive']} inactive "
                    "pipeline(s) — activate them on the Pipelines page"
                )
            if enabled["active"]:
                return empty(
                    f"{enabled['active']} invention pipeline(s) no longer "
                    "resolve (source or decryptor gone since the SDE "
                    "changed) — pick the choice again or turn invention "
                    "Off on the Pipelines page"
                )
            return empty(
                "no invention-enabled pipelines — enable invention on "
                "the Pipelines page"
            )
        # Only the invention inputs and their EIV bases need pricing here.
        type_ids = engine.invention_type_ids(c, r)
        prices, buy_venue, structure_units_cheaper, region_wide, adjusted = (
            _price_maps(c, r, settings_, type_ids)
        )
        if not prices:
            return empty("no price data yet — run a price refresh first")
        snapshot = engine.snapshot_from_state(
            c,
            prices=prices,
            adjusted=adjusted,
            region_wide=region_wide,
            buy_venue=buy_venue,
            structure_units_cheaper=structure_units_cheaper,
        )
        if snapshot is None:
            return empty("no ESI data yet — run an ESI update first")
        return render_template(
            "invention.html",
            data=engine.invention_stockpile(c, r, snapshot),
            settings=settings_,
            reason=None,
        )

    @app.route("/planning")
    def planning():
        """Planning — today's estimates, nothing persisted. Two views:
        Profit (default; what-if margins at TODAY'S cached prices, one
        row per active pipeline — moved here from the old top-level
        Profit page) and Slot Planner (?view=slots; the steady-state
        cycle: slot demand vs pools and the replacement materials, with
        alchemy assumed off). Both recompute on every load; realized
        costing lives on each executed run's Profit tab."""
        c = conn()
        if not sde_ready():
            return render_template(
                "planning_slots.html",
                rows=None,
                reason=(
                    "Download the game data first (the Dashboard "
                    "checklist has the button), then add pipelines"
                ),
            )
        r = ref()
        settings_ = store.get_settings(c)
        if request.args.get("view") != "slots":
            type_ids = engine.demand_type_ids(c, r)
            # v1.10: inputs at the cheaper landed venue (finals keep the
            # hub quote — their sell side is sell_quote's business).
            prices, venues, _units, region_wide, adjusted = _price_maps(
                c, r, settings_, type_ids
            )
            region_wide = frozenset(region_wide)
            cards = []
            for p in store.active_pipelines(c):
                cost = costing.current_hull_cost(
                    c, r, settings_, p, prices, adjusted,
                    region_wide=region_wide, venues=venues,
                )
                info = r.type_info(p["final_product_type_id"])
                price, net, capital = sell_quote(
                    c, settings_, p["final_product_type_id"]
                )
                cards.append(
                    {
                        "pipeline": p,
                        "name": info.name,
                        "cost": cost,
                        "price": price,
                        "net": net,
                        "capital": capital,
                        "margin": (
                            net - cost.total if net is not None else None
                        ),
                    }
                )
            cards.sort(
                key=lambda card: card["margin"]
                if card["margin"] is not None
                else float("-inf"),
                reverse=True,
            )
            _count, prices_at = market.price_cache_state(
                c, settings_.price_region_id, settings_.price_source
            )
            _count, structure_prices_at = market.structure_cache_state(
                c, settings_.structure_market()
            )
            return render_template(
                "planning_profit.html",
                cards=cards,
                totals=costing.cycle_totals(cards),
                prices_at=prices_at,
                structure_prices_at=structure_prices_at,
                broker_rate=costing.broker_fee_rate(settings_),
                sales_tax=costing.sales_tax_rate(settings_),
                settings=settings_,
            )
        # Explicit active check: demand_type_ids is no longer a proxy for
        # it — a capable INACTIVE pipeline contributes invention price ids.
        type_ids = engine.demand_type_ids(c, r)
        if not store.active_pipelines(c):
            return render_template(
                "planning_slots.html",
                rows=None,
                settings=settings_,
                reason="no active pipelines — add one and this page shows "
                "its steady-state cycle",
            )
        prices, buy_venue, structure_units_cheaper, region_wide, adjusted = (
            _price_maps(c, r, settings_, type_ids)
        )
        if not prices:
            return render_template(
                "planning_slots.html",
                rows=None,
                settings=settings_,
                reason="no price data yet — run a price refresh first",
            )
        # The settings' pools — what snapshot_from_state also hands the
        # index run since v1.29 revision 6 (ruling R2): steady state
        # assumes the pools are free each cycle.
        snapshot = engine.Snapshot(
            slots_available={
                config.ACTIVITY_MANUFACTURING: settings_.manufacturing_slots,
                config.ACTIVITY_REACTION: settings_.reaction_slots,
            },
            prices=prices,
            adjusted_prices=adjusted,
            region_wide=region_wide,
            buy_venue=buy_venue,
            structure_units_cheaper=structure_units_cheaper,
        )
        plan = engine.plan_steady_state(c, r, snapshot)
        return render_template(
            "planning_slots.html", **_planning_context(r, plan, settings_)
        )

    def _plan_inputs(c, r, settings_):
        """(snapshot, skip, notes): everything plan_index_run reads, built
        ONE way for the ▶ Plan button and the ESI update's re-plan (v1.29
        revision 6, contract review A4), so the two can never plan
        differently. `skip` is None, or (kind, message) — kind
        'pipelines' / 'prices' / 'esi' — when there is nothing to plan
        from; `notes` are warnings to flash beside the plan (the Max Buy
        structure basis with no cached buy orders). Local rows only: no
        network."""
        # Explicit active check: demand_type_ids is no longer a proxy for
        # it — a capable INACTIVE pipeline contributes invention price ids,
        # and planning a run against zero active pipelines would persist an
        # empty run.
        type_ids = engine.demand_type_ids(c, r)
        active = store.active_pipelines(c)
        if not active:
            return None, ("pipelines", "no active pipelines — add one first"), []
        # v1.10: per type, the cheaper LANDED of the hub quote and the
        # structure market's sell ladder (finals always keep the hub quote).
        prices, buy_venue, structure_units_cheaper, region_wide, adjusted = (
            _price_maps(c, r, settings_, type_ids)
        )
        if not prices:
            return None, (
                "prices", "no price data yet — run a price refresh first"
            ), []
        notes = []
        # v1.25: every demanded type's sell ladders, per venue, for the
        # sourcing pass (fill pricing + compressed substitution).
        sell_ladders = market.sell_ladders(c, settings_, type_ids)
        # Review 2026-09-05 (C5): the cached Jita quote per type, kept even
        # where the structure venue won Phase 1 — `prices` then holds the
        # structure's quote, and the sourcing pass's synthetic hub rung
        # (an item with a hub price but no stored hub ladder) needs the
        # hub figure, not the winner's.
        hub_prices = {
            t: price
            for t, (price, _region_wide) in market.cached_hub_quotes(
                c, settings_.price_region_id, type_ids, settings_.price_source
            ).items()
        }
        # v1.26: the structure market's quote per type at ITS basis —
        # the synthetic rung a 'min_sell' / 'max_buy' structure basis
        # stands in with (empty when the structure comparison is off).
        structure_prices = (
            market.cached_structure_quotes(
                c, settings_.structure_market(), type_ids,
                settings_.structure_price_basis,
            )
            if settings_.structure_buy_enabled
            else {}
        )
        if (
            settings_.structure_buy_enabled
            and settings_.structure_price_basis == store.PRICE_BASIS_MAX_BUY
            and not structure_prices
        ):
            # The buy side of the structure book is cached only by a
            # structure refresh made since the basis existed: say so
            # rather than let the venue vanish from the plan silently.
            notes.append(
                f"{settings_.structure_market_label()} has no cached buy "
                "orders yet — refresh the structure market for the Max Buy "
                "Order basis; this plan priced nothing there"
            )
        snapshot = engine.snapshot_from_state(
            c,
            prices=prices,
            adjusted=adjusted,
            region_wide=region_wide,
            buy_venue=buy_venue,
            structure_units_cheaper=structure_units_cheaper,
            sell_ladders=sell_ladders,
            hub_prices=hub_prices,
            structure_prices=structure_prices,
            # v1.27.1: each active final's SELL reference for the install
            # check's ranking — the Planning / Ledger quote (the hub for
            # sub-capitals, the capital structure's sell quote for
            # capital-class hulls, which `prices` never carries).
            sell_quotes={
                t: price
                for t in {p["final_product_type_id"] for p in active}
                for price in (ledger.final_quote(c, r, settings_, t)[0],)
                if price is not None
            },
        )
        if snapshot is None:
            return None, ("esi", "no ESI data yet — run an ESI update first"), notes
        return snapshot, None, notes

    def _plan_stamps(c, settings_) -> list[str]:
        """How old each cache that drove a plan's buy quotes is — the
        prices, and the structure market when the comparison is on."""
        _n, latest = market.price_cache_state(
            c, settings_.price_region_id, settings_.price_source
        )
        stamps = []
        if latest:
            stamps.append(f"prices as of {latest[:16].replace('T', ' ')} UTC")
        if settings_.structure_buy_enabled:
            _n, latest_structure = market.structure_cache_state(
                c, settings_.structure_market()
            )
            label = settings_.structure_market_label()
            stamps.append(
                f"{label} as of {latest_structure[:16].replace('T', ' ')} UTC"
                if latest_structure
                else f"{label} market never pulled"
            )
        return stamps

    def _plan_summary(plan) -> str:
        """"N buys, M jobs" — the re-plan's flash clause."""
        items = plan.items.values()
        buys = sum(
            1 for i in items
            if (i.recommended_buy_qty or 0) > 0
            or (getattr(i, "compressed_covered_qty", 0) or 0) > 0
        )
        jobs = sum(int(i.jobs_allocated or 0) for i in items)
        return (
            f"{buys} buy{'s' if buys != 1 else ''}, "
            f"{jobs} job{'s' if jobs != 1 else ''}"
        )

    def _replan_open_run(c, snapshot_fresh: bool) -> str:
        """The ESI update's re-plan step (v1.29 revision 6, user ruling R1
        2026-09-28): re-plan the open run IN PLACE from the stock, jobs and
        prices this update just pulled, so every tab reads the cycle as it
        stands now. Returns the flash clause; never raises.

        The open run is buying.collecting_run_id — the newest run while it
        is not executed (contract review A1), the same run the matcher
        files purchases under and a superseded run's Buy tab points at.
        Skipped, with a clause saying why (A4): no open run (after Mark
        executed — ▶ Plan opens the next cycle); the snapshot step did not
        succeed in THIS request (re-planning from the previous snapshot
        would move planned_start past purchases the plan never saw);
        nothing to plan from (no active pipelines, prices or snapshot).
        A failure rolls back — the engine already rolled its own write
        back — and leaves the run's previous plan in place; a ValueError
        (the run was executed or a newer one made meanwhile) is a skip.

        Revision 7 (user ruling 2026-09-29, "the real fix"): no stop rule
        any more. Revision 6 skipped this re-plan once the cycle's final
        jobs were installing (review B1), because a final was planned at
        its full requested quantity whatever its own jobs in flight. Now
        the plan passes _cycle_cut: final jobs started after it are THIS
        cycle's wave (engine Phase 4 sizes only the rest, and the
        intermediates below keep their targets), so a re-plan after the
        installs plans the remainder, not the wave again."""
        try:
            open_id = buying.collecting_run_id(c)
        except Exception as exc:  # noqa: BLE001 — the sales-step precedent
            log.exception("finding the open run failed")
            return f"re-plan skipped ({exc})"
        if open_id is None:
            if c.execute("SELECT 1 FROM index_run LIMIT 1").fetchone() is None:
                return (
                    "no open run to re-plan — ▶ Plan index run plans the "
                    "first cycle"
                )
            return (
                "no open run to re-plan — the newest run is executed; "
                "▶ Plan index run opens the next cycle"
            )
        number = c.execute(
            "SELECT run_number FROM index_run WHERE index_run_id = ?",
            (open_id,),
        ).fetchone()[0]
        if not snapshot_fresh:
            return (
                f"run {number} not re-planned — the ESI snapshot did not "
                "refresh, so it keeps its previous plan"
            )
        try:
            r = ref()
            settings_ = store.get_settings(c)
            snapshot, skip, notes = _plan_inputs(c, r, settings_)
            if skip is not None:
                return f"run {number} not re-planned — {skip[1]}"
            plan = engine.plan_index_run(
                c, r, snapshot, replace_index_run_id=open_id,
                cycle_cut=_cycle_cut(c),
            )
        except ValueError as exc:
            return f"run {number} not re-planned — {exc}"
        except Exception as exc:  # noqa: BLE001 — the sales-step precedent
            log.exception("re-plan failed")
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            return (
                f"re-plan failed ({exc}) — run {number} keeps its previous "
                "plan"
            )
        message = f"run {plan.run_number} re-planned: {_plan_summary(plan)}"
        if notes:
            message += " — " + "; ".join(notes)
        return message

    @app.post("/run")
    def run_plan():
        """▶ Plan index run — from the last stored ESI snapshot and the
        price cache: fast, no network at all.

        v1.29 revision 6 (user ruling R1 2026-09-28: one run per buying
        cycle, kept live): while a run is open (buying.collecting_run_id —
        the newest run, not executed) the button re-plans it IN PLACE
        (engine.plan_index_run's replace_index_run_id: same id and run
        number, planned_start moved to the stored snapshot's fetched_at —
        costing's pre-plan cut, so purchases made since that snapshot stay
        post-plan (review 2026-09-28) — purchases kept); a NEW run is
        created only when none is open, i.e. after Mark executed.

        Revision 7 (user ruling 2026-09-29): it passes the same
        _cycle_cut as the ESI update's re-plan, so final jobs started
        inside the current buying cycle count as this cycle's wave and
        the plan sizes only the rest."""
        c = conn()
        if not sde_ready():
            flash(
            "download the game data first — the dashboard checklist "
            "has the button"
        )
            return redirect(url_for("dashboard"))
        r = ref()
        settings_ = store.get_settings(c)
        snapshot, skip, notes = _plan_inputs(c, r, settings_)
        for note in notes:
            flash(note)
        if skip is not None:
            kind, message = skip
            flash(message)
            return redirect(
                url_for("pipelines") if kind == "pipelines"
                else url_for("dashboard")
            )
        open_id = buying.collecting_run_id(c)
        try:
            plan = engine.plan_index_run(
                c, r, snapshot, replace_index_run_id=open_id,
                cycle_cut=_cycle_cut(c),
            )
        except ValueError as exc:
            # The open run was executed (or a newer one made) while this
            # plan ran: nothing changed — say so and show the runs.
            flash(f"run not re-planned — {exc}; nothing changed")
            return redirect(url_for("runs"))
        # A plan moves the buying windows (a new run opens its own, R2; a
        # re-plan moves planned_start, which the windows no longer read
        # but the pre-plan cut does): re-file the purchases now, before
        # either tab is viewed (review 2026-09-28).
        match_message, match_ok = _assign_purchases(c, settings_)
        # Both caches drove this plan's buy quotes: say how old each is.
        stamps = _plan_stamps(c, settings_)
        flash(
            f"index run {plan.run_number} "
            + ("re-planned in place" if open_id is not None else "planned")
            + (f" ({'; '.join(stamps)})" if stamps else "")
            + (f" — {match_message}" if not match_ok and match_message else "")
        )
        return redirect(url_for("run_detail", index_run_id=plan.index_run_id))

    # -- purchases from ESI (v1.29 revision 3, user rulings 2026-09-28) ------
    #
    # buying.assign_purchases derives every run's run_purchase lines from
    # the stored wallet buys and accepted item exchanges (R1/R2). It runs
    # from web.py only — never from ledger.pull_sales, which has no
    # settings and which buying imports (contract review C1):
    #   * as the fourth step of the ESI update;
    #   * after every call that moves a cycle window or changes who counts
    #     — planning a run, discarding one, run_complete / run_reopen, a
    #     Count buys toggle, removing a character (review 2026-09-28);
    #   * on a Buy tab view of a run with no derived lines, but only when
    #     the pass's inputs changed since the last successful pass (the
    #     stamp below) — a safety net for a pass that failed or a process
    #     restart, never a write on every view.
    # Every call is guarded: matching reads local rows only, and a fault in
    # it must never cost the refresh, the completion or the page that
    # triggered it. buying is imported with the other modules: an import
    # error is a packaging bug that must fail at start-up, not hide as a
    # silently skipped step.
    purchase_pass = {"stamp": None}

    def _purchase_inputs_stamp(c) -> tuple:
        """Everything a matching pass reads, bar what the pass itself
        writes (a contract's k / priced_at / excluded, the run_purchase
        lines): the ingest tables' extent, the runs and their windows,
        who counts, who is internal and the price cache's age. Equal
        stamps mean a new pass would change nothing.

        Revision 4 (contract review A12): plus the settings the pass reads
        — the price region and source and the structure market (purchase
        venues, reference prices), the compressed yields and refining tax
        (the unplanned-ore conversion) and the three live inbound rates
        (runs planned before their columns fall back to them)."""
        return (
            tuple(c.execute(
                "SELECT price_region_id, price_source, capital_market_mode, "
                "capital_structure_id, compressed_ore_yield, "
                "compressed_gas_yield, compressed_reprocess_tax, "
                "freight_in_isk_per_m3, structure_freight_in_isk_per_m3, "
                "freight_in_default_isk_per_m3 FROM settings WHERE id = 1"
            ).fetchone() or ()),
            tuple(c.execute(
                "SELECT COUNT(*), MAX(transaction_id), MAX(fetched_at) "
                "FROM buy_transaction").fetchone()),
            tuple(c.execute(
                "SELECT COUNT(*), MAX(last_seen_at), MAX(items_fetched_at), "
                "SUM(items_status = 'ok') FROM buy_contract").fetchone()),
            tuple(c.execute("SELECT COUNT(*) FROM buy_contract_item").fetchone()),
            tuple(
                tuple(r) for r in c.execute(
                    "SELECT index_run_id, run_number, status, planned_start, "
                    "completed_at FROM index_run ORDER BY index_run_id")
            ),
            tuple(sorted(store.buys_enabled_owners(c))),
            tuple(sorted(ledger.internal_ids(c))),
            tuple(c.execute("SELECT MAX(fetched_at) FROM market_price").fetchone()),
        )

    def _assign_purchases(c, settings_=None) -> tuple[str | None, bool]:
        """Run buying.assign_purchases once; (flash text, ok). The text is
        None when there is nothing worth saying (no game data yet to
        resolve venues against)."""
        if not sde_ready():
            return None, True
        try:
            summary = buying.assign_purchases(
                c, ref(), settings_ or store.get_settings(c)
            )
        except Exception as exc:  # noqa: BLE001 — the sales-step precedent
            log.exception("purchase matching failed")
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            return (
                f"purchase matching failed ({exc}) — each run keeps the "
                "purchases it had"
            ), False
        try:
            purchase_pass["stamp"] = _purchase_inputs_stamp(c)
        except sqlite3.Error:  # the stamp only saves a later view a pass
            purchase_pass["stamp"] = None
        return buying.summary_line(summary), True

    def _assign_on_first_view(c, index_run_id, settings_) -> None:
        """§4: match purchases when a Buy tab opens on a run that has no
        derived lines yet, provided ESI has delivered any purchase at all
        — so an empty pool never writes on a page view — and provided the
        pass's inputs changed since the last successful pass (review
        2026-09-28). Without that last test every view of a run whose
        window simply holds no costed purchase (an executed run older than
        ESI's wallet history, the newest plan before anything is bought)
        re-ran a BEGIN IMMEDIATE pass over every run."""
        if c.execute(
            "SELECT 1 FROM run_purchase WHERE index_run_id = ? "
            "AND esi_kind IS NOT NULL LIMIT 1",
            (index_run_id,),
        ).fetchone() is not None:
            return
        if (
            c.execute("SELECT 1 FROM buy_transaction LIMIT 1").fetchone() is None
            and c.execute("SELECT 1 FROM buy_contract LIMIT 1").fetchone() is None
        ):
            return
        if (
            purchase_pass["stamp"] is not None
            and purchase_pass["stamp"] == _purchase_inputs_stamp(c)
        ):
            return
        _assign_purchases(c, settings_)

    def _run_purchase_records(c, index_run_id):
        """buying.run_purchase_records for the Buy tab's Purchases section,
        or None when the reader fails — the tab then lists the run's
        derived lines instead of failing."""
        try:
            return buying.run_purchase_records(c, index_run_id)
        except Exception:  # noqa: BLE001 — the page still renders
            log.exception("reading the run's purchase records failed")
            return None

    def _purchases_owner(c, index_run_id):
        """The run a SUPERSEDED run's buying cycle went to (C4): the newest
        run while it is still a plan, else None — purchases after the last
        executed run wait for the next plan. The run comes from
        buying.collecting_run_id, so the pointer and the matcher can never
        disagree about where the purchases went."""
        owner_id = buying.collecting_run_id(c)
        if owner_id is None or owner_id == index_run_id:
            return None
        return c.execute(
            "SELECT index_run_id, run_number, status FROM index_run "
            "WHERE index_run_id = ?",
            (owner_id,),
        ).fetchone()

    def _mark_superseded(rows):
        """Derived, never stored: a non-complete run is superseded when any
        newer run exists — only the newest plan is actionable. Rows must be
        ordered index_run_id DESC (newest first)."""
        newest_id = rows[0]["index_run_id"] if rows else None
        out = []
        for r in rows:
            r = dict(r)
            r["superseded"] = (
                r["status"] != "complete"
                and r["index_run_id"] != newest_id
            )
            out.append(r)
        return out

    @app.route("/runs")
    def runs():
        c = conn()
        all_runs = _mark_superseded(
            c.execute(
                "SELECT r.*, COUNT(i.index_run_item_id) AS items, "
                "SUM(i.recommended_buy_qty * i.price_snapshot) AS buy_total, "
                "SUM(CASE WHEN i.recommended_buy_qty > 0 "
                "  AND i.price_snapshot IS NULL THEN 1 ELSE 0 END) "
                "  AS buys_unpriced "
                "FROM index_run r LEFT JOIN index_run_item i USING (index_run_id) "
                "GROUP BY r.index_run_id ORDER BY r.index_run_id DESC"
            ).fetchall()
        )
        show_all = request.args.get("all") == "1"
        return render_template(
            "runs.html",
            runs=(
                all_runs if show_all
                else [r for r in all_runs if not r["superseded"]]
            ),
            superseded_count=sum(1 for r in all_runs if r["superseded"]),
            show_all=show_all,
        )

    @app.post("/runs/<int:index_run_id>/delete")
    def run_delete(index_run_id):
        c = conn()
        run = c.execute(
            "SELECT * FROM index_run WHERE index_run_id = ?", (index_run_id,)
        ).fetchone()
        if run is None:
            abort(404)
        # Executed runs are cost history — never deletable, from any path.
        if run["status"] == "complete":
            abort(400)
        # v1.29: the run's purchase lines go first — connections open with
        # PRAGMA foreign_keys = ON, and run_purchase references index_run
        # (A29; only planned/active runs ever reach here).
        store.delete_run_purchases(c, index_run_id)
        c.execute(
            "DELETE FROM index_run_item_pipeline WHERE index_run_item_id IN "
            "(SELECT index_run_item_id FROM index_run_item "
            " WHERE index_run_id = ?)",
            (index_run_id,),
        )
        c.execute(
            "DELETE FROM index_run_item WHERE index_run_id = ?",
            (index_run_id,),
        )
        c.execute(
            "DELETE FROM index_run_invention WHERE index_run_id = ?",
            (index_run_id,),
        )
        c.execute(
            "DELETE FROM index_run WHERE index_run_id = ?", (index_run_id,)
        )
        c.commit()
        # Discarding the newest plan makes the previous run the newest
        # again, which moves its window (R2): re-file now (review
        # 2026-09-28), as run_complete / run_reopen do.
        message = f"run {run['run_number']} discarded"
        match_message, ok = _assign_purchases(c)
        if not ok and match_message:
            message += f" — {match_message}"
        flash(message)
        return redirect(url_for("runs"))

    def _run_profit_context(run, index_run_id) -> dict:
        """run_detail's Profit-view context: realized cost cards for an
        executed run (planned runs show an empty list)."""
        c = conn()
        settings_ = store.get_settings(c)
        cards = []
        if run["status"] == "complete":
            # Pipelines ATTRIBUTABLE to this run, not currently-active
            # ones: deactivating a pipeline later must not hide the
            # intact cost history of its past executed runs.
            attributable = c.execute(
                "SELECT DISTINCT p.* FROM pipeline p "
                "JOIN index_run_item_pipeline a "
                "  ON a.pipeline_id = p.pipeline_id "
                "JOIN index_run_item i "
                "  ON i.index_run_item_id = a.index_run_item_id "
                "WHERE i.index_run_id = ?",
                (index_run_id,),
            ).fetchall()
            for p in attributable:
                cost = costing.hull_cost(
                    c, ref(), settings_, index_run_id, p["pipeline_id"]
                )
                if cost is None:
                    continue
                info = ref().type_info(p["final_product_type_id"])
                price, net, capital = sell_quote(
                    c, settings_, p["final_product_type_id"]
                )
                cards.append(
                    {
                        "name": info.name,
                        "cost": cost,
                        "price": price,
                        "net": net,
                        "capital": capital,
                        "margin": (
                            net - cost.total if net is not None else None
                        ),
                    }
                )
            cards.sort(
                key=lambda card: card["margin"]
                if card["margin"] is not None
                else float("-inf"),
                reverse=True,
            )
        return dict(
            cards=cards,
            totals=costing.cycle_totals(cards),
            completed_history=len(costing.completed_sequence(c)),
            broker_rate=costing.broker_fee_rate(settings_),
            sales_tax=costing.sales_tax_rate(settings_),
            settings=settings_,
        )

    @app.route("/runs/<int:index_run_id>")
    def run_detail(index_run_id):
        c = conn()
        run = c.execute(
            "SELECT * FROM index_run WHERE index_run_id = ?", (index_run_id,)
        ).fetchone()
        if run is None:
            abort(404)
        superseded = (
            run["status"] != "complete"
            and c.execute(
                "SELECT 1 FROM index_run WHERE index_run_id > ? LIMIT 1",
                (index_run_id,),
            ).fetchone()
            is not None
        )
        if request.args.get("view") == "profit":
            return render_template(
                "run_profit.html",
                run=run,
                superseded=superseded,
                **_run_profit_context(run, index_run_id),
            )
        items = c.execute(
            "SELECT i.*, t.name, t.group_id, t.category_id, "
            "g.name AS category FROM index_run_item i "
            "JOIN ref_type t ON t.type_id = i.type_id "
            "JOIN ref_group g ON g.group_id = t.group_id "
            "WHERE i.index_run_id = ? ORDER BY i.depth, t.name",
            (index_run_id,),
        ).fetchall()
        settings_ = store.get_settings(c)
        bc = _buy_context(items, ref())
        if request.args.get("view") == "buy":
            # §4 / C1: a run with no derived lines is matched on view —
            # only when there is anything to match and the inputs moved
            # since the last pass (_assign_on_first_view).
            if not superseded:
                _assign_on_first_view(c, index_run_id, settings_)
            return render_template(
                "run_buy.html",
                run=run,
                superseded=superseded,
                purchases_owner=(
                    _purchases_owner(c, index_run_id) if superseded else None
                ),
                settings=settings_,
                **_buy_tab_context(
                    run, items, ref(), settings_, c, bc,
                    esi_records=_run_purchase_records(c, index_run_id),
                    superseded=superseded,
                ),
            )
        unmet = [i for i in items if _unmet_row(i)]
        unmet_qty = {i["type_id"]: _unmet_qty(i) for i in unmet}
        alchemy = _alchemy_section(ref(), settings_, items)
        final_ids, final_net_margin = _final_margin_badges(
            c, ref(), settings_, items
        )
        # Revision 7 (contract review amendment 9): a final whose whole
        # wave is installed this cycle builds nothing, so it would drop
        # out of both job tables — exactly when its "installed N/M" cue
        # matters. _job_rows keeps it, with no job to run.
        builds_mfg = _job_rows(items, 1, final_ids)
        builds_reaction = _job_rows(items, 11, final_ids)
        window = buying.run_window(c, index_run_id)
        template = (
            "run_chain.html" if request.args.get("view") == "chain"
            else "run_detail.html"
        )
        return render_template(
            template,
            run=run,
            superseded=superseded,
            final_ids=final_ids,
            items=items,
            final_net_margin=final_net_margin,
            # v1.29 (ruling 2026-09-24): purchasing lives on the Buy tab.
            # `buys` survives here for the wallet stat alone (its count and
            # the "exceeds wallets" badge); every other buy-side key —
            # venue_qty, the Multibuy blocks, the compressed section, the
            # unpriced / split / unsourced / via-compressed counts — is
            # passed to run_buy.html instead and nothing on this page or
            # the Stockpile tab reads it any more.
            buys=bc["buys"],
            builds=builds_mfg,
            reactions=builds_reaction,
            builds_grouped=_group_by_category(ref(), builds_mfg),
            reactions_grouped=_group_by_category(ref(), builds_reaction),
            cycle_cut_label=_cut_label(window.lo) if window else None,
            struct_builds=bc["struct_builds"],
            struct_buys=bc["struct_buys"],
            struct_slots=bc["struct_slots"],
            alchemy=alchemy,
            alchemy_yield=settings_.alchemy_reprocess_yield,
            **_chain_context(ref(), items),
            **_install_context(ref(), settings_, items),
            unmet=unmet,
            low_stock=[i for i in items if i["low_stock"]],
            buy_total=bc["buy_total"],
            settings=settings_,
            # v1.27.1: the strip counts the jobs to RUN NOW, the plan's
            # allocation beside it where stock feeds fewer — against the
            # pool the run was planned against (schema 11; the settings'
            # figure on runs planned before it was persisted).
            mfg_pool=_run_pool(run, "manufacturing_slots_available", settings_.manufacturing_slots),
            reaction_pool=_run_pool(run, "reaction_slots_available", settings_.reaction_slots),
            mfg_slots_used=sum(_jobs_to_run(i) for i in builds_mfg),
            mfg_slots_planned=sum(
                i["jobs_allocated"] or 0 for i in builds_mfg
            ),
            reaction_slots_used=sum(_jobs_to_run(i) for i in builds_reaction),
            reaction_slots_planned=sum(
                i["jobs_allocated"] or 0 for i in builds_reaction
            ),
            alchemy_slots_used=sum(_jobs_to_run(a["item"]) for a in alchemy),
            alchemy_slots_planned=sum(
                a["item"]["jobs_allocated"] or 0 for a in alchemy
            ),
        )

    def sell_quote(c, settings_, type_id):
        """(sell price, net proceeds/hull, is_capital) for one final —
        capital-class hulls quote from the structure market cache with
        their own fees and movement cost, everything else from the Jita
        region cache (v1.6)."""
        # v1.27.0: hoisted to ledger.final_quote so the Ledger prices its
        # finals the same way; this closure is the profit pages' name for it.
        return ledger.final_quote(c, ref(), settings_, type_id)

    # -- ledger ------------------------------------------------------------

    @app.route("/ledger", endpoint="ledger")
    def ledger_tab():
        """Sales of pipeline finals (v1.27.0): wallet transactions, orders
        and contracts persisted by the ESI refresh, each costed against
        the latest priced run executed on or before it (v1.27.1,
        ledger.CostVintages) — recomputed on every load, nothing saved.
        Named ledger_tab: a nested `def ledger` would make `ledger` a local
        of create_app and shadow the module for every closure here."""
        c = conn()
        window = request.args.get("window") or str(ledger.DEFAULT_WINDOW)
        pool_size = c.execute("SELECT COUNT(*) FROM pool_character").fetchone()[0]
        if not sde_ready():
            return render_template(
                "ledger.html", view=None, window=window, sde_build=None,
                pool_size=pool_size, settings=None,
            )
        settings_ = store.get_settings(c)
        view = ledger.build_view(
            c, ref(), settings_, window,
            fmt_isk=app.jinja_env.filters["isk_short"],
            fmt_qty=app.jinja_env.filters["qty"],
            show_all_rows=request.args.get("rows") == "all",
        )
        return render_template(
            "ledger.html",
            view=view,
            window=view["window"].key,
            settings=settings_,
            broker_rate=costing.broker_fee_rate(settings_),
            sales_tax=costing.sales_tax_rate(settings_),
            sde_build=ref().sde_build(),
            pool_size=pool_size,
        )

    @app.route("/profit")
    def profit():
        """The today's-prices what-if moved to Planning → Profit
        (2026-08-24); this endpoint stays as a redirect for bookmarks."""
        return redirect(url_for("planning"))

    @app.post("/runs/<int:index_run_id>/complete")
    def run_complete(index_run_id):
        c = conn()
        run = c.execute(
            "SELECT * FROM index_run WHERE index_run_id = ?", (index_run_id,)
        ).fetchone()
        if run is None:
            abort(404)
        # costing.completed_sequence orders by run_number: completing a
        # run OLDER than an already-executed one would splice it mid-
        # cost-history and silently reprice every later completed run's
        # lagged inputs. (Reopen + re-complete of the LATEST completed
        # run stays fine — only strictly-older run_numbers are blocked.)
        newer = c.execute(
            "SELECT 1 FROM index_run WHERE status = 'complete' "
            "AND run_number > ? LIMIT 1",
            (run["run_number"],),
        ).fetchone()
        if newer is not None:
            flash(
                "a newer run is already executed — completing this one "
                "would rewrite cost history (reopen the newer executed "
                "runs first if that is really the intent)"
            )
            return redirect(url_for("run_detail", index_run_id=index_run_id))
        c.execute(
            "UPDATE index_run SET status = 'complete', "
            "completed_at = datetime('now'), "
            "actual_start = COALESCE(actual_start, datetime('now')) "
            "WHERE index_run_id = ?",
            (index_run_id,),
        )
        c.commit()
        # Executing closes this run's buying cycle at completed_at (R2):
        # re-file the purchases now, not at the next ESI update (C1).
        message = (
            "run marked executed — it now feeds cost history; "
            "▶ Plan index run opens the next cycle"
        )
        match_message, ok = _assign_purchases(c)
        if not ok:
            message += f" — {match_message}"
        flash(message)
        return redirect(url_for("run_detail", index_run_id=index_run_id))

    @app.post("/runs/<int:index_run_id>/reopen")
    def run_reopen(index_run_id):
        c = conn()
        run = c.execute(
            "SELECT run_number, status FROM index_run "
            "WHERE index_run_id = ?",
            (index_run_id,),
        ).fetchone()
        if run is None:
            abort(404)
        # Symmetric with run_complete's guard: reopening a MID-history
        # executed run is the splice that actually reprices every later
        # run's lagged inputs — reopen newest-first instead.
        if run["status"] == "complete":
            newer = c.execute(
                "SELECT 1 FROM index_run WHERE status = 'complete' "
                "AND run_number > ? LIMIT 1",
                (run["run_number"],),
            ).fetchone()
            if newer is not None:
                flash(
                    "a newer run is still executed — reopen the newest "
                    "executed run first (reopening this one mid-history "
                    "would silently reprice every later run's costs)"
                )
                return redirect(
                    url_for("run_detail", index_run_id=index_run_id)
                )
        c.execute(
            "UPDATE index_run SET status = 'planned', completed_at = NULL "
            "WHERE index_run_id = ?",
            (index_run_id,),
        )
        c.commit()
        # Reopening moves the window bound back (R2): re-file now (C1).
        message = "run reopened — excluded from cost history"
        # v1.29 revision 6 (contract review A6): reopening the newest run
        # makes it the OPEN run again, and the next ESI update re-plans it
        # in place — its executed plan is not kept. Say so.
        if buying.collecting_run_id(c) == index_run_id:
            message += (
                "; it is the open run again — the next ESI update re-plans "
                "it from current stock"
            )
        match_message, ok = _assign_purchases(c)
        if not ok:
            message += f" — {match_message}"
        flash(message)
        return redirect(url_for("run_detail", index_run_id=index_run_id))

    return app
