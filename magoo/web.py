"""Flask routes (PROJECT.md §3). Server-rendered Jinja2, no JS frameworks.

Pages: dashboard, pipelines, planning (steady-state cycle analysis, never
persisted), settings (globals + tracked systems + per-class build settings),
blueprints (ME/TE overrides), characters (pool + in-app SSO login), index
runs (list + detail with buy/build lists and multibuy export).
"""

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

import httpx

from magoo import __version__

from . import (
    config,
    costing,
    engine,
    esi,
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
        return f"build {build} — starting download"
    if stage == "current":
        return f"build {build} — already up to date"
    return "checking for a new build"


def _split_structure_components(builds_mfg_all, buys):
    """(struct_builds, struct_buys, other_mfg_builds) — v1.9: structure
    components get their own Plan/Chain section, built AND bought rows
    (bought ones stay in the Buy list / Multibuy too). Slot totals must
    keep summing over builds_mfg_all: the split is display-only."""
    is_comp = lambda i: i["group_id"] in config.STRUCTURE_COMPONENT_GROUPS
    return (
        [i for i in builds_mfg_all if is_comp(i)],
        [i for i in buys if is_comp(i)],
        [i for i in builds_mfg_all if not is_comp(i)],
    )


STRUCTURES_LABEL = "Upwell Structures"
STRUCTURE_MODULES_LABEL = "Structure Rigs & Modules"
STRUCTURE_COMPONENTS_LABEL = "Structure Components"
_UNRANKED = 20

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
    if x["buy_qty"] > 0:
        return "buy"
    if x["alchemy_out"] > 0:
        return "alchemy"
    return "unmet"


def _chain_short(x) -> bool:
    """Unmet demand on a row that still reads build/react: capacity-limited
    with no purchase fallback — the Plan tab's "Unmet" list (capacity_limited,
    nothing bought, deficit > 0), so the two tabs count the same items."""
    return bool(
        x["capacity_limited"]
        and x["buy_qty"] == 0
        and x["deficit"] and x["deficit"] > 0
        and x["build_qty"] > 0
    )


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
                "savings_unpriced_inputs": item.savings_unpriced_inputs,
                "price_snapshot": item.price_snapshot,
                "price_region_wide": item.price_region_wide,
                "buy_venue": item.buy_venue,
                "structure_units_cheaper": item.structure_units_cheaper,
                "alchemy_for_type_id": item.alchemy_for_type_id,
                "direct_unit_cost": item.direct_unit_cost,
                "alchemy_unit_cost": item.alchemy_unit_cost,
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


def _buy_context(rows) -> dict:
    """The buy-side derivations run_detail and _planning_context share:
    the buy list and its total, plan-time venue provenance (structure vs
    hub, shallow ladders, region-wide fallbacks), the structure-component
    split, and the per-venue Multibuy blocks. Rows are either sqlite rows
    (NULL-able columns) or _steady_rows dicts (never None) — the `or 0`
    guards cover both shapes identically."""
    buys = [i for i in rows if (i["recommended_buy_qty"] or 0) > 0]
    builds_mfg_all = [
        i
        for i in rows
        if (i["recommended_build_qty"] or 0) > 0 and i["activity_id"] == 1
    ]
    struct_builds, struct_buys, builds_mfg = _split_structure_components(
        builds_mfg_all, buys
    )
    # v1.10: plan-time buy venue per row (NULL on pre-v1.10 rows = hub).
    # One Multibuy block per venue so each pastes into the right market
    # window; 'shallow' = the structure's ladder had fewer units beating
    # the Jita landed price than the plan buys there (the engine NULLs
    # structure_units_cheaper on every non-structure row).
    structure_buys = {
        i["type_id"] for i in buys
        if i["buy_venue"] == store.BUY_VENUE_STRUCTURE
    }
    return dict(
        buys=buys,
        buy_total=sum(
            (i["recommended_buy_qty"] or 0) * (i["price_snapshot"] or 0)
            for i in buys
        ),
        # Unpriced buy rows contribute 0 above — the total understates and
        # must say so (mirrors the profit pages' "N unpriced" badge).
        buys_unpriced=sum(1 for i in buys if i["price_snapshot"] is None),
        structure_buys=structure_buys,
        shallow={
            i["type_id"]
            for i in buys
            if i["structure_units_cheaper"] is not None
            and i["recommended_buy_qty"] > i["structure_units_cheaper"]
        },
        # Plan-time provenance of price_snapshot: which bought inputs were
        # priced from a region-wide fallback.
        region_wide={i["type_id"] for i in buys if i["price_region_wide"]},
        multibuy_hub="\n".join(
            f"{i['name']} {i['recommended_buy_qty']}"
            for i in buys if i["type_id"] not in structure_buys
        ),
        multibuy_structure="\n".join(
            f"{i['name']} {i['recommended_buy_qty']}"
            for i in buys if i["type_id"] in structure_buys
        ),
        struct_builds=struct_builds,
        struct_buys=struct_buys,
        struct_slots=sum(i["jobs_allocated"] or 0 for i in struct_builds),
        builds_mfg=builds_mfg,
        builds_mfg_all=builds_mfg_all,
    )


def _planning_context(ref, plan, settings_) -> dict:
    """Template context for the Slot Planner view, from a live
    (never-persisted) steady-state Plan. Shares the buy-side derivations
    with run_detail via _buy_context; everything stock- or
    wallet-dependent (low stock, wallets, multibuy) is absent here, and
    alchemy too — plan_steady_state plans direct reactions only."""
    rows = _steady_rows(ref, plan)
    bc = _buy_context(rows)
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
        shallow=bc["shallow"],
        region_wide=bc["region_wide"],
        builds_grouped=_group_by_category(ref, bc["builds_mfg"]),
        reactions_grouped=_group_by_category(ref, builds_reaction),
        struct_builds=bc["struct_builds"],
        struct_buys=bc["struct_buys"],
        struct_slots=bc["struct_slots"],
        capacity_rows=[i for i in rows if i["capacity_limited"]],
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
        qty = i["recommended_build_qty"]
        yield_ = settings_.alchemy_reprocess_yield
        alchemy.append(
            {
                "item": i,
                "composite_name": ref.type_info(composite_id).name,
                # Prefer the engine's persisted per-job-floored figure
                # (what the Chain tab shows); the one-shot recomputation
                # here floors once over the total and can disagree.
                "expected_qty": (
                    composite["alchemy_output_qty"]
                    if composite is not None
                    and composite["alchemy_output_qty"]
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
                "direct_unit": (
                    composite["direct_unit_cost"] if composite else None
                ),
                "alchemy_unit": (
                    composite["alchemy_unit_cost"] if composite else None
                ),
            }
        )
    return alchemy


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
            "cycle_need": i["merged_min_qty"],
            "target": i["target_stock_qty"],
            "on_hand": i["on_hand_qty"],
            "in_jobs": i["in_progress_qty"],
            "deficit": i["deficit_qty"],
            "buildable": i["blueprint_id"] is not None,
            "activity_id": i["activity_id"],
            "alchemy_credit": i["alchemy_credit_qty"] or 0,
            "alchemy_route": bool(i["alchemy_for_type_id"]),
            "build_qty": i["recommended_build_qty"] or 0,
            "buy_qty": i["recommended_buy_qty"] or 0,
            "alchemy_out": i["alchemy_output_qty"] or 0,
            "capacity_limited": bool(i["capacity_limited"]),
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
    return dict(
        chain_rows=chain_rows,
        chain_raws=[x for x in chain_rows if not x["buildable"]],
        chain_struct=[
            x for x in chain_rows
            if x["group_id"] in config.STRUCTURE_COMPONENT_GROUPS
        ],
        chain_mfg=group_chain(
            [
                x for x in chain_rows
                if x["buildable"] and x["activity_id"] == 1
                and x["group_id"] not in config.STRUCTURE_COMPONENT_GROUPS
            ]
        ),
        chain_reactions=group_chain(
            [
                x for x in chain_rows
                if x["buildable"] and x["activity_id"] == 11
            ]
        ),
        chain_counts=chain_counts,
    )


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
    final_ids = {i["type_id"] for i in items if i["depth"] == 0}
    final_ids |= {
        row["final_product_type_id"]
        for row in c.execute(
            "SELECT final_product_type_id FROM pipeline WHERE is_active = 1"
        )
    }
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
    c.execute(
        "UPDATE settings SET input_purchase_margin = ?, "
        "stockpile_buffer = ?, "
        "max_run_duration_hours = ?, ship_batch_multiple = ?, "
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
        "structure_freight_in_isk_per_m3 = ?, structure_buy_enabled = ? "
        "WHERE id = 1",
        (
            margin,
            buffer,
            max(1.0, _form_number(form, "duration")),
            max(1, int_field("batch")),
            max(0, int_field("extra_runs")),
            int_field("region"),
            form["source"],
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
                        store.ensure_schema(g.conn)
                        schema_ready.add(str(config.DB_PATH))
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

    sde_job = sdeimport.ImportJob()
    app.extensions["sde_import"] = sde_job

    def sde_job_view() -> dict:
        status = sde_job.status()
        status["message"] = _sde_message(status)
        return status

    @app.post("/sde/import")
    def sde_import_start():
        # No sde_ready() guard on purpose: with a build already imported
        # this is the "check for updates" path (run_import no-ops when
        # the build is unchanged).
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
        columns may be omitted (runs/BPC -> uncapped, ME/TE -> None, which
        the caller resolves: 0 for ships, the intermediate defaults for
        other products — v1.9). Comma- and space-separated rows work too
        (name may contain spaces)."""
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
        fields = [f for f in fields if f != ""]
        if len(fields) < 2 or len(fields) > 5:
            raise ValueError(line)
        name, *numbers = fields
        if not name or not all(n.isdigit() for n in numbers):
            raise ValueError(line)
        qty = int(numbers[0])
        runs_bpc = int(numbers[1]) if len(numbers) > 1 else None
        me = int(numbers[2]) if len(numbers) > 2 else None
        te = int(numbers[3]) if len(numbers) > 3 else None
        if (
            qty < 1
            or (runs_bpc is not None and runs_bpc < 1)
            or (me is not None and me > 10)
            or (te is not None and te > 20)
        ):
            raise ValueError(line)
        return name, qty, runs_bpc, me, te

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
                row["final_product_type_id"]
                for row in c.execute(
                    "SELECT final_product_type_id FROM pipeline"
                )
            }
            added, updated, errors = [], [], []
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
                # Omitted ME/TE: ships default to 0/0 (the paste contract);
                # any other product — structures, rigs, components, which
                # often double as intermediates of other chains — takes the
                # intermediate defaults so an ME0 pin never leaks into
                # every chain that consumes it (v1.9).
                is_ship = (
                    ref().type_info(type_id).category_id == config.CATEGORY_SHIP
                )
                if me is None:
                    me = 0 if is_ship else settings_.default_intermediate_me
                if te is None:
                    te = 0 if is_ship else settings_.default_intermediate_te
                if type_id in existing:
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
                    existing.add(type_id)
                    added.append(name)
                c.execute(
                    "INSERT INTO blueprint_setting VALUES (?, ?, ?) "
                    "ON CONFLICT (blueprint_id) DO UPDATE SET me_level = "
                    "excluded.me_level, te_level = excluded.te_level",
                    (blueprint.blueprint_id, me, te),
                )
            c.commit()
            if added:
                flash(f"added {len(added)} pipeline(s): {', '.join(added)}")
            if updated:
                flash(f"updated {len(updated)}: {', '.join(updated)}")
            for error in errors:
                flash(error)
            return redirect(url_for("pipelines"))
        return render_template(
            "pipelines.html",
            sde_ready=sde_ready(),
            pipelines=(
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
            ),
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
                return _settings_save(c, request.form)
            except ValueError as exc:
                # One bad field ("1,5b", an emptied autofill) must not
                # 500 and discard the whole ~40-field save — flash which
                # input was bad, like the pipeline-paste path does.
                flash(f"invalid number in {exc} — nothing was saved")
                return redirect(url_for("settings"))
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
            item_classes=config.ITEM_CLASSES,
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
        return render_template(
            "characters.html",
            characters=c.execute(
                "SELECT p.*, t.expires_at FROM pool_character p "
                "LEFT JOIN esi_token t USING (character_id)"
            ).fetchall(),
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
        if flag not in ("include_assets", "include_job_slots", "count_assets"):
            abort(400)
        c = conn()
        c.execute(
            f"UPDATE pool_character SET {flag} = 1 - {flag} "
            "WHERE character_id = ?",
            (character_id,),
        )
        c.commit()
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
        c.commit()

        message = f"removed {row['character_name']}"
        if stranded:
            message += (
                " — it was pulling corporation data for "
                + ", ".join(stranded)
                + "; another character with the same corp roles must be "
                "logged in for that to keep updating"
            )
        flash(message)
        return redirect(url_for("characters"))

    @app.post("/corps/<int:corporation_id>/toggle/<flag>")
    def corp_toggle(corporation_id, flag):
        if flag not in ("count_assets", "count_wallet", "count_jobs"):
            abort(400)
        c = conn()
        c.execute(
            f"UPDATE esi_corp SET {flag} = 1 - {flag} "
            "WHERE corporation_id = ?",
            (corporation_id,),
        )
        c.commit()
        return redirect(url_for("characters"))

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
        """The slow pull (assets, jobs, wallets), decoupled from planning."""
        import time as _time

        if not sde_ready():
            flash(
                "download the game data first (dashboard checklist) — the "
                "refresh classifies assets and jobs against its item data"
            )
            return redirect(url_for("dashboard"))
        t0 = _time.monotonic()
        try:
            state = esi.refresh_state(conn(), ref())
        except httpx.HTTPError as exc:
            flash(
                f"ESI refresh failed ({exc}) — the previous snapshot is "
                "unchanged; try again once ESI recovers"
            )
            return redirect(url_for("dashboard"))
        except RuntimeError as exc:
            # A dead refresh token: retrying cannot help, so surface the
            # re-auth guidance alone without the "once ESI recovers" tail.
            flash(f"{exc} — the previous snapshot is unchanged")
            return redirect(url_for("dashboard"))
        flash(
            f"ESI refreshed in {_time.monotonic() - t0:.0f}s: "
            f"{len(state['on_hand'])} types on hand, "
            f"{sum(state['active_jobs'].values())} active jobs"
        )
        return redirect(url_for("dashboard"))

    @app.post("/prices/refresh")
    def prices_refresh():
        """The slow price pull (parallel, ESI-guideline compliant),
        decoupled from planning like the ESI snapshot refresh."""
        import time as _time

        if not sde_ready():
            flash(
            "download the game data first — the dashboard checklist "
            "has the button"
        )
            return redirect(url_for("dashboard"))
        c = conn()
        r = ref()
        type_ids = engine.demand_type_ids(c, r)
        if not type_ids:
            flash("no active pipelines — add one first")
            return redirect(url_for("pipelines"))
        settings_ = store.get_settings(c)
        t0 = _time.monotonic()
        # v1.9: raw leaves (no blueprint) with no hub-station order fall
        # back to the region-wide best order — in the price region itself
        # (one region setting since 2026-08-23; same pull, no extra call).
        raw_leaves = {t for t in type_ids if r.blueprint_for_product(t) is None}
        try:
            fetched, skipped, fresh = market.refresh_prices(
                c,
                settings_.price_region_id,
                type_ids,
                settings_.price_source,
                fallback_type_ids=raw_leaves,
                fallback_region_id=settings_.price_region_id,
            )
        except httpx.HTTPError as exc:
            flash(
                f"price refresh failed ({exc}) — cached prices are "
                "unchanged; try again once ESI recovers"
            )
            return redirect(url_for("dashboard"))
        try:
            n_adjusted = market.store_adjusted_prices(
                c, type_ids, market.fetch_adjusted_prices()
            )
        except httpx.HTTPError as exc:
            flash(
                f"regional prices refreshed ({fetched} fetched, {skipped} "
                f"skipped) but the adjusted-price pull failed ({exc}) — "
                "install-fee bases keep their previous values"
            )
            return redirect(url_for("dashboard"))
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

        # One structure-market pull (the whole book comes down regardless)
        # serves two consumers: v1.6 capital-class finals' SELL quotes, and
        # v1.10 buy quotes + sell ladders for every input the plan may buy
        # (the Jita-vs-structure landed comparison at plan time).
        finals = {
            p["final_product_type_id"] for p in store.active_pipelines(c)
        }
        capital_finals = [
            t for t in finals if costing.is_capital_priced(r, t)
        ]
        # Inputs are wanted whenever the book is pulled at all — even with
        # the comparison switched off — so turning it on later works from
        # the existing cache instead of silently comparing against nothing.
        inputs = [t for t in type_ids if t not in finals]
        wanted = capital_finals + inputs  # disjoint by construction
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
        flash(message)
        # The Planning → Profit view's refresh button lands back on the
        # numbers it just refreshed ("profit" kept for old form values).
        if request.form.get("next") in ("planning", "profit"):
            return redirect(url_for("planning"))
        return redirect(url_for("dashboard"))

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
            prices, venues, _units, region_wide = market.quote_maps(
                market.buy_quotes(c, r, settings_, type_ids)
            )
            region_wide = frozenset(region_wide)
            adjusted = market.cached_adjusted_prices(c, type_ids)
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
        type_ids = engine.demand_type_ids(c, r)
        if not type_ids:
            return render_template(
                "planning_slots.html",
                rows=None,
                settings=settings_,
                reason="no active pipelines — add one and this page shows "
                "its steady-state cycle",
            )
        prices, buy_venue, structure_units_cheaper, region_wide = (
            market.quote_maps(market.buy_quotes(c, r, settings_, type_ids))
        )
        if not prices:
            return render_template(
                "planning_slots.html",
                rows=None,
                settings=settings_,
                reason="no price data yet — run a price refresh first",
            )
        adjusted = market.cached_adjusted_prices(c, type_ids)
        # Raw settings pools, not snapshot_from_state's overhang-netted
        # ones: steady state assumes the pools are free each cycle.
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

    @app.post("/run")
    def run_plan():
        """Plan from the last stored ESI snapshot and the price cache —
        fast, no network at all."""
        c = conn()
        if not sde_ready():
            flash(
            "download the game data first — the dashboard checklist "
            "has the button"
        )
            return redirect(url_for("dashboard"))
        r = ref()
        type_ids = engine.demand_type_ids(c, r)
        if not type_ids:
            flash("no active pipelines — add one first")
            return redirect(url_for("pipelines"))
        settings_ = store.get_settings(c)
        # v1.10: per type, the cheaper LANDED of the hub quote and the
        # structure market's sell ladder (finals always keep the hub quote).
        prices, buy_venue, structure_units_cheaper, region_wide = (
            market.quote_maps(market.buy_quotes(c, r, settings_, type_ids))
        )
        if not prices:
            flash("no price data yet — run a price refresh first")
            return redirect(url_for("dashboard"))
        adjusted = market.cached_adjusted_prices(c, type_ids)
        snapshot = engine.snapshot_from_state(
            c,
            prices=prices,
            adjusted=adjusted,
            region_wide=region_wide,
            buy_venue=buy_venue,
            structure_units_cheaper=structure_units_cheaper,
        )
        if snapshot is None:
            flash("no ESI data yet — run an ESI update first")
            return redirect(url_for("dashboard"))
        plan = engine.plan_index_run(c, r, snapshot)
        _n, latest = market.price_cache_state(
            c, settings_.price_region_id, settings_.price_source
        )
        # Both caches drove this plan's buy quotes: say how old each is.
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
        flash(
            f"index run {plan.run_number} planned"
            + (f" ({'; '.join(stamps)})" if stamps else "")
        )
        return redirect(url_for("run_detail", index_run_id=plan.index_run_id))

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
            "DELETE FROM index_run WHERE index_run_id = ?", (index_run_id,)
        )
        c.commit()
        flash(f"run {run['run_number']} discarded")
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
        bc = _buy_context(items)
        unmet = [
            i
            for i in items
            if i["capacity_limited"]
            and (i["recommended_buy_qty"] or 0) == 0
            and (i["deficit_qty"] or 0) > 0
        ]
        builds_reaction = [
            i
            for i in items
            if (i["recommended_build_qty"] or 0) > 0
            and i["activity_id"] == 11
            and not i["alchemy_for_type_id"]
        ]
        alchemy = _alchemy_section(ref(), settings_, items)
        final_ids, final_net_margin = _final_margin_badges(
            c, ref(), settings_, items
        )
        template = (
            "run_chain.html" if request.args.get("view") == "chain"
            else "run_detail.html"
        )
        return render_template(
            template,
            run=run,
            superseded=superseded,
            final_ids=final_ids,
            region_wide=bc["region_wide"],
            items=items,
            final_net_margin=final_net_margin,
            buys=bc["buys"],
            builds=bc["builds_mfg"],
            reactions=builds_reaction,
            builds_grouped=_group_by_category(ref(), bc["builds_mfg"]),
            reactions_grouped=_group_by_category(ref(), builds_reaction),
            struct_builds=bc["struct_builds"],
            struct_buys=bc["struct_buys"],
            struct_slots=bc["struct_slots"],
            alchemy=alchemy,
            alchemy_yield=settings_.alchemy_reprocess_yield,
            **_chain_context(ref(), items),
            unmet=unmet,
            low_stock=[i for i in items if i["low_stock"]],
            buy_total=bc["buy_total"],
            buys_unpriced=bc["buys_unpriced"],
            multibuy_hub=bc["multibuy_hub"],
            multibuy_structure=bc["multibuy_structure"],
            structure_buys=bc["structure_buys"],
            shallow=bc["shallow"],
            settings=settings_,
            mfg_slots_used=sum(
                i["jobs_allocated"] or 0 for i in bc["builds_mfg_all"]
            ),
            reaction_slots_used=sum(
                i["jobs_allocated"] or 0 for i in builds_reaction
            ),
            alchemy_slots_used=sum(
                a["item"]["jobs_allocated"] or 0 for a in alchemy
            ),
        )

    def sell_quote(c, settings_, type_id):
        """(sell price, net proceeds/hull, is_capital) for one final —
        capital-class hulls quote from the structure market cache with
        their own fees and movement cost, everything else from the Jita
        region cache (v1.6)."""
        capital = costing.is_capital_priced(ref(), type_id)
        if capital:
            price = market.cached_prices(
                c,
                settings_.capital_structure(),
                [type_id],
                market.STRUCTURE_SOURCE,
            ).get(type_id)
        else:
            price = market.cached_prices(
                c, settings_.price_region_id, [type_id], settings_.price_source
            ).get(type_id)
        net = (
            costing.net_proceeds_per_hull(
                price,
                ref().type_info(type_id).freight_volume,
                settings_,
                capital=capital,
                freight_exempt=costing.freight_out_exempt(type_id),
            )
            if price is not None
            else None
        )
        return price, net, capital

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
        flash("run marked executed — it now feeds cost history")
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
        flash("run reopened — excluded from cost history")
        return redirect(url_for("run_detail", index_run_id=index_run_id))

    return app
