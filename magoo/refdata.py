"""Read layer over imported reference data.

industry.py, bom.py, and engine.py depend only on this module — never on SDE
file formats or the ref_* table shapes (PROJECT.md §3). Lookups are cached;
reference data only changes on SDE reimport, so open a fresh Refdata after
running sdeimport.
"""

import sqlite3
from dataclasses import dataclass

from . import config, store


@dataclass(frozen=True)
class TypeInfo:
    type_id: int
    name: str
    group_id: int
    category_id: int
    volume: float | None
    # Repackaged volume (ships shrink dramatically); None for most types.
    packaged_volume: float | None = None

    @property
    def freight_volume(self) -> float:
        """m³ as hauled: packaged when the SDE gives one, else volume."""
        return self.packaged_volume or self.volume or 0.0


@dataclass(frozen=True)
class Blueprint:
    blueprint_id: int
    activity_id: int
    product_id: int
    portion_size: int
    base_time: int
    max_runs: int | None


@dataclass(frozen=True)
class AlchemyRoute:
    """An alternate supply route for a composite: run the unrefined reaction
    formula, then reprocess its product into the composite plus recovered
    inputs. Quantities are BASE reprocessing outputs per unrefined unit —
    the caller applies the (user-asserted, scrapmetal-capped) yield."""

    composite_id: int
    formula: Blueprint  # the unrefined reaction formula
    unrefined_id: int  # the formula's product
    composite_qty: int  # base composite units per unrefined unit
    recovered: tuple[tuple[int, int], ...]  # other reprocess outputs


class Refdata:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or store.connect()
        self._types: dict[int, TypeInfo] = {}
        self._type_ids: dict[str, int] = {}
        self._blueprints: dict[int, Blueprint | None] = {}
        self._materials: dict[tuple[int, int, bool], tuple] = {}
        self._attrs: dict[int, dict] = {}
        self._modifiers: dict[tuple[int, int], tuple] = {}
        self._reprocess: dict[int, tuple] = {}
        self._alchemy_routes: dict[int, AlchemyRoute] | None = None

    def close(self) -> None:
        self.conn.close()

    # -- build ----------------------------------------------------------

    def sde_build(self) -> int | None:
        try:
            row = self.conn.execute(
                "SELECT build_number FROM ref_sde_build "
                "ORDER BY imported_at DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            # Fresh database: no import has ever run, so the ref tables
            # themselves don't exist yet.
            return None
        return row["build_number"] if row else None

    # -- types ----------------------------------------------------------

    def type_info(self, type_id: int) -> TypeInfo:
        info = self._types.get(type_id)
        if info is None:
            row = self.conn.execute(
                "SELECT * FROM ref_type WHERE type_id = ?", (type_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown type_id {type_id}")
            info = TypeInfo(
                row["type_id"],
                row["name"],
                row["group_id"],
                row["category_id"],
                row["volume"],
                row["packaged_volume"],
            )
            self._types[type_id] = info
        return info

    def type_id(self, name: str) -> int:
        type_id = self._type_ids.get(name)
        if type_id is None:
            # Case-insensitive (2026-08-22: 'astrahus' is the Astrahus), but
            # an exact-case match wins first. Names are not unique in the
            # SDE (e.g. 'Azbel' is both the Engineering Complex and an
            # unpublished celestial): then prefer the published type, then
            # the lowest id, deterministically.
            row = self.conn.execute(
                "SELECT type_id FROM ref_type WHERE name = ? COLLATE NOCASE "
                "ORDER BY (name = ?) DESC, published DESC, type_id LIMIT 1",
                (name, name),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown type name {name!r}")
            type_id = self._type_ids[name] = row["type_id"]
        return type_id

    def is_skill(self, type_id: int) -> bool:
        return self.type_info(type_id).category_id == config.CATEGORY_SKILL

    # -- blueprints ------------------------------------------------------

    def blueprint_for_product(self, product_id: int) -> Blueprint | None:
        """The blueprint producing this type, or None if not buildable.

        Manufacturing is preferred if a product somehow has both activities
        (open item in PROJECT.md §11).
        """
        if product_id not in self._blueprints:
            row = self.conn.execute(
                "SELECT * FROM ref_blueprint WHERE product_id = ? "
                "ORDER BY activity_id LIMIT 1",
                (product_id,),
            ).fetchone()
            self._blueprints[product_id] = (
                Blueprint(
                    row["blueprint_id"],
                    row["activity_id"],
                    row["product_id"],
                    row["portion_size"],
                    row["base_time"],
                    row["max_runs"],
                )
                if row
                else None
            )
        return self._blueprints[product_id]

    def materials(
        self, blueprint_id: int, activity_id: int, consumed_only: bool = True
    ) -> tuple[tuple[int, int], ...]:
        """(material_id, base_quantity) pairs for one activity of a blueprint.

        consumed_only excludes materials that survive the job (they must be on
        hand but are never demand — PROJECT.md §4).
        """
        key = (blueprint_id, activity_id, consumed_only)
        if key not in self._materials:
            sql = (
                "SELECT material_id, quantity FROM ref_blueprint_material "
                "WHERE blueprint_id = ? AND activity_id = ?"
            )
            if consumed_only:
                sql += " AND consumed = 1"
            self._materials[key] = tuple(
                (row["material_id"], row["quantity"])
                for row in self.conn.execute(sql, (blueprint_id, activity_id))
            )
        return self._materials[key]

    def blueprint_skills(
        self, blueprint_id: int, activity_id: int
    ) -> tuple[tuple[int, int], ...]:
        """(skill_type_id, required_level) prerequisites for one activity."""
        key = ("skills", blueprint_id, activity_id)
        if key not in self._materials:
            self._materials[key] = tuple(
                (row["skill_type_id"], row["level"])
                for row in self.conn.execute(
                    "SELECT skill_type_id, level FROM ref_blueprint_skill "
                    "WHERE blueprint_id = ? AND activity_id = ?",
                    (blueprint_id, activity_id),
                )
            )
        return self._materials[key]

    # -- reprocessing / alchemy ------------------------------------------

    def reprocess_outputs(self, type_id: int) -> tuple[tuple[int, int], ...]:
        """(material_id, base_quantity) reprocessing outputs of one item.
        Empty for items that do not reprocess (or reprocess randomly)."""
        if type_id not in self._reprocess:
            self._reprocess[type_id] = tuple(
                (row["material_id"], row["quantity"])
                for row in self.conn.execute(
                    "SELECT material_id, quantity FROM ref_type_material "
                    "WHERE type_id = ?",
                    (type_id,),
                )
            )
        return self._reprocess[type_id]

    def alchemy_routes(self) -> dict[int, AlchemyRoute]:
        """composite type_id -> its alchemy route, derived entirely from
        data: a reaction formula whose product's reprocess outputs contain
        the product of ANOTHER reaction formula is an alchemy route for that
        composite (e.g. Unrefined Ferrofluid Reaction Formula -> Unrefined
        Ferrofluid -> reprocess -> Ferrofluid + recovered Hafnium). Mineral
        alchemy never qualifies: its reprocess outputs are randomized and
        excluded at import, and minerals have no direct reaction to compare
        against anyway."""
        if self._alchemy_routes is None:
            reaction_products = {
                row["product_id"]: row["blueprint_id"]
                for row in self.conn.execute(
                    "SELECT product_id, blueprint_id FROM ref_blueprint "
                    "WHERE activity_id = ?",
                    (config.ACTIVITY_REACTION,),
                )
            }
            routes: dict[int, AlchemyRoute] = {}
            for row in self.conn.execute(
                "SELECT * FROM ref_blueprint WHERE activity_id = ?",
                (config.ACTIVITY_REACTION,),
            ):
                unrefined_id = row["product_id"]
                outputs = self.reprocess_outputs(unrefined_id)
                composites = [
                    (m, qty)
                    for m, qty in outputs
                    if reaction_products.get(m) not in (None, row["blueprint_id"])
                ]
                if len(composites) != 1:
                    continue  # not an alchemy formula
                composite_id, composite_qty = composites[0]
                routes[composite_id] = AlchemyRoute(
                    composite_id=composite_id,
                    formula=Blueprint(
                        row["blueprint_id"],
                        row["activity_id"],
                        row["product_id"],
                        row["portion_size"],
                        row["base_time"],
                        row["max_runs"],
                    ),
                    unrefined_id=unrefined_id,
                    composite_qty=composite_qty,
                    recovered=tuple(
                        (m, qty) for m, qty in outputs if m != composite_id
                    ),
                )
            self._alchemy_routes = routes
        return self._alchemy_routes

    # -- dogma attributes ------------------------------------------------

    def _type_attrs(self, type_id: int) -> dict:
        attrs = self._attrs.get(type_id)
        if attrs is None:
            by_id, by_name = {}, {}
            for row in self.conn.execute(
                "SELECT attribute_id, attribute_name, value "
                "FROM ref_type_attribute WHERE type_id = ?",
                (type_id,),
            ):
                by_id[row["attribute_id"]] = row["value"]
                by_name[row["attribute_name"]] = row["value"]
            attrs = self._attrs[type_id] = {"id": by_id, "name": by_name}
        return attrs

    def attribute_by_id(self, type_id: int, attribute_id: int, default=None):
        return self._type_attrs(type_id)["id"].get(attribute_id, default)

    def attribute_by_name(self, type_id: int, name: str, default=None):
        return self._type_attrs(type_id)["name"].get(name, default)

    # -- industry modifiers ----------------------------------------------

    def industry_modifiers(
        self, source_type_id: int, activity_id: int
    ) -> tuple[tuple[str, int, int | None], ...]:
        """(kind, dogma_attribute_id, filter_id) entries for a structure or
        rig type, for one activity. Empty if the type modifies nothing."""
        key = (source_type_id, activity_id)
        if key not in self._modifiers:
            self._modifiers[key] = tuple(
                (row["kind"], row["dogma_attribute_id"], row["filter_id"])
                for row in self.conn.execute(
                    "SELECT kind, dogma_attribute_id, filter_id "
                    "FROM ref_industry_modifier "
                    "WHERE source_type_id = ? AND activity_id = ?",
                    key,
                )
            )
        return self._modifiers[key]

    # -- solar systems ---------------------------------------------------

    def solar_system(self, system_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ref_solar_system WHERE system_id = ?", (system_id,)
        ).fetchone()

    def solar_system_by_name(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ref_solar_system WHERE name = ?", (name,)
        ).fetchone()
