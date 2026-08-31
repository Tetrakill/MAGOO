"""Multi-stage BOM expansion (PROJECT.md §7 Phase 2).

Expands a final product into every buildable intermediate and raw material,
applying ME and per-class build-setting bonuses at every level so quantities
reflect what jobs actually consume. Demand for an item is MERGED across all
of its consumers before its run count is derived (ceil once per item, not
once per demand edge — revised 2026-08-20), matching the single merged job
set the engine actually installs. Cycle-safe: an item that transitively
requires itself is treated as raw (self-consuming legacy blueprints keep
their self-demand counted once, without re-expansion).

The build chain is never stored — it is re-derived on every planning pass.
"""

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping

from . import industry
from .industry import BuildSetting


@dataclass
class BomItem:
    """Aggregate requirement for one item across the whole expansion."""

    type_id: int
    name: str
    quantity: int = 0
    depth: int = 0  # max depth at which the item appears (final product = 0)
    item_class: str = "other"
    # Buildable items only (None/0 for raw materials):
    blueprint_id: int | None = None
    activity_id: int | None = None
    portion_size: int = 0
    base_time: int = 0

    @property
    def buildable(self) -> bool:
        return self.blueprint_id is not None


def _cycle_members(
    edges: dict[int, tuple[tuple[int, int], ...]], nodes: set[int]
) -> set[int]:
    """Members of strongly connected components of size > 1 within
    `nodes` — the items that transitively require themselves. Self-edges
    are excluded (they are handled inline by the demand propagation).
    Iterative Tarjan, keeping the module recursion-free."""
    graph = {
        t: [m for m, _q in edges.get(t, ()) if m != t and m in nodes]
        for t in nodes
    }
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    members: set[int] = set()
    counter = 0
    for root in graph:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            else:
                # Returning from the child pushed at position i - 1.
                low[node] = min(low[node], low[graph[node][i - 1]])
            descended = False
            for j in range(i, len(graph[node])):
                succ = graph[node][j]
                if succ not in index:
                    work.append((node, j + 1))
                    work.append((succ, 0))
                    descended = True
                    break
                if succ in on_stack:
                    low[node] = min(low[node], index[succ])
            if descended:
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    top = stack.pop()
                    on_stack.discard(top)
                    component.append(top)
                    if top == node:
                        break
                if len(component) > 1:
                    members.update(component)
    return members


def _default_me_te(blueprint_id: int, activity_id: int) -> tuple[int, int]:
    """ME/TE assumption when no blueprint_setting exists. Reactions have no
    ME/TE research; manufacturing defaults to unresearched."""
    return (0, 0)


def expand(
    ref,
    product_type_id: int,
    quantity: int,
    build_settings: Mapping[str, BuildSetting] | None = None,
    me_te: Callable[[int, int], tuple[int, int]] = _default_me_te,
    blacklist: Callable[[int], bool] | None = None,
) -> dict[int, BomItem]:
    """Expand `quantity` units of a final product into aggregate per-item
    requirements, keyed by type_id (the final product included, depth 0).

    build_settings maps item class -> BuildSetting (missing classes get NPC
    defaults, i.e. no bonuses). me_te(blueprint_id, activity_id) supplies
    ME/TE assumptions per blueprint. blacklist(type_id) marks buildable
    items that should be bought instead of built — they are treated as raw
    and their sub-chain is not expanded (never applied to the final
    product itself).
    """
    build_settings = build_settings or {}

    # ---- Discover the reachable graph (structure only, no quantities) ----
    blueprints: dict[int, object] = {}
    edges: dict[int, tuple[tuple[int, int], ...]] = {}
    seen: set[int] = set()
    stack = [product_type_id]
    while stack:
        type_id = stack.pop()
        if type_id in seen:
            continue
        seen.add(type_id)
        blueprint = ref.blueprint_for_product(type_id)
        # Production blacklist: buy instead of build (finals exempt).
        if (
            blueprint is not None
            and type_id != product_type_id
            and blacklist
            and blacklist(type_id)
        ):
            blueprint = None
        blueprints[type_id] = blueprint
        if blueprint is None:
            continue
        materials = tuple(
            ref.materials(blueprint.blueprint_id, blueprint.activity_id)
        )
        edges[type_id] = materials
        stack.extend(m for m, _qty in materials)

    # ---- Cycle safety ----
    # Self-edges (legacy starbase blueprints that consume their own product)
    # are excluded from the ordering and handled inline below. Nodes on any
    # longer cycle are demoted to raw — none exist in the current SDE.
    def kahn(active: dict[int, tuple]) -> list[int]:
        pending = {t: 0 for t in seen}
        for type_id, materials in active.items():
            for m, _qty in materials:
                if m != type_id:
                    pending[m] += 1
        queue = deque(t for t, c in pending.items() if c == 0)
        order: list[int] = []
        while queue:
            type_id = queue.popleft()
            order.append(type_id)
            for m, _qty in active.get(type_id, ()):
                if m == type_id:
                    continue
                pending[m] -= 1
                if pending[m] == 0:
                    queue.append(m)
        return order

    order = kahn(edges)
    if len(order) < len(seen):
        # Demote only true cycle members (SCCs of size > 1 among the
        # unordered nodes). The first Kahn pass leaves the whole subtree
        # UNDER a cycle unordered too, but an acyclic descendant does not
        # transitively require itself — it stays buildable and orders
        # normally once the cycle members are raw.
        for type_id in _cycle_members(edges, seen - set(order)):
            blueprints[type_id] = None
        edges = {
            t: mats for t, mats in edges.items() if blueprints[t] is not None
        }
        order = kahn(edges)

    # ---- Propagate merged demand in topological order ----
    items: dict[int, BomItem] = {}
    for type_id in seen:
        blueprint = blueprints[type_id]
        item = items[type_id] = BomItem(
            type_id=type_id,
            name=ref.type_info(type_id).name,
            item_class=industry.classify_item(
                ref, type_id, blueprint.activity_id if blueprint else None
            ),
        )
        if blueprint is not None:
            item.blueprint_id = blueprint.blueprint_id
            item.activity_id = blueprint.activity_id
            item.portion_size = blueprint.portion_size
            item.base_time = blueprint.base_time
    items[product_type_id].quantity = quantity

    for type_id in order:
        item = items[type_id]
        blueprint = blueprints[type_id]
        if blueprint is None or item.quantity <= 0:
            continue
        runs = math.ceil(item.quantity / blueprint.portion_size)
        me_level, _te = me_te(blueprint.blueprint_id, blueprint.activity_id)
        mat_mult = industry.build_multiplier(
            ref,
            build_settings.get(item.item_class, industry.NPC_STATION),
            blueprint.activity_id,
            "material",
            group_id=ref.type_info(type_id).group_id,
        )
        for material_id, base_qty in edges[type_id]:
            required = industry.required_quantity(
                runs, base_qty, me_level, mat_mult
            )
            child = items[material_id]
            child.quantity += required
            child.depth = max(child.depth, item.depth + 1)

    # Nodes discovered under a branch that ended up raw (blacklist, cycle)
    # receive no demand — drop them; the final product always stays.
    return {
        t: item
        for t, item in items.items()
        if item.quantity > 0 or t == product_type_id
    }
