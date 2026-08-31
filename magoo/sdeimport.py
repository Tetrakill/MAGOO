"""CCP JSONL SDE download and import into reference tables.

Implements the auto-pull protocol from PROJECT.md §4:

1. Fetch latest.jsonl; the record keyed "sde" holds the current build number
   ("_meta" carries lastBuildNumber for change detection).
2. Compare against ref_sde_build; skip if unchanged (unless --force).
3. Download the versioned zip into data/sde/ (cached).
4. Extract and import; record the build number.

This module is the ONLY place that knows SDE file shapes. Everything else
reads through refdata.py. Reference tables are rebuilt wholesale on import;
state tables are never touched.

JSONL encoding note: JSON keys must be strings, so integer-keyed records are
encoded with `_key` (and sometimes `_value`) fields.

CLI:
    python -m magoo.sdeimport            # pull + import if build changed
    python -m magoo.sdeimport --force    # reimport even if build unchanged
    python -m magoo.sdeimport --probe    # dump first record of each dataset
"""

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx

from . import config, logsetup, store

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tolerant field access
#
# The JSONL export is documented but young; field-name fallbacks below let a
# first run against real data fail loudly in one place instead of importing
# garbage. --probe exists to inspect the real shapes.
# ---------------------------------------------------------------------------


def _localized(value):
    """SDE names may be plain strings or locale dicts ({"en": ...})."""
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), None)
    return value


def _first(record: dict, *keys, default=None):
    for key in keys:
        if key in record:
            return record[key]
    return default


def _record_id(record: dict, *fallback_keys) -> int:
    """Integer identity of a record: `_key`, else a named ID field."""
    value = _first(record, "_key", *fallback_keys)
    if value is None:
        raise KeyError(f"no record id in {list(record)[:8]}")
    return int(value)


def _iter_jsonl(lines):
    """Yield parsed records from an iterable of JSONL lines (bytes or str)."""
    for line in lines:
        line = line.strip()
        if line:
            yield json.loads(line)


# ---------------------------------------------------------------------------
# Progress reporting
#
# run_import takes an optional callback so the web UI can watch a background
# import; the CLI passes none and keeps its prints. Events are flat dicts
# keyed by "stage": check -> resolved -> download (repeating) -> import
# (per dataset group) -> finalize; or "current" when the build is already
# imported. The callback runs on the importing thread — keep it cheap.
# ---------------------------------------------------------------------------


def _report(progress, **event) -> None:
    if progress is not None:
        progress(event)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def fetch_latest_build(client: httpx.Client) -> int:
    """Current SDE build number from latest.jsonl."""
    resp = client.get(config.SDE_LATEST_URL)
    resp.raise_for_status()
    meta_build = None
    for record in _iter_jsonl(resp.content.splitlines()):
        key = record.get("_key")
        if key == "sde":
            build = _first(record, "buildNumber", "build", "_value")
            if isinstance(build, dict):
                build = _first(build, "buildNumber", "build")
            if build is not None:
                return int(build)
        if key == "_meta":
            meta_build = _first(record, "lastBuildNumber")
    if meta_build is not None:
        return int(meta_build)
    raise RuntimeError(
        "could not find a build number in latest.jsonl — run --probe and "
        "inspect the format"
    )


def stored_build(conn: sqlite3.Connection):
    row = conn.execute(
        "SELECT build_number FROM ref_sde_build ORDER BY imported_at DESC LIMIT 1"
    ).fetchone()
    return row["build_number"] if row else None


def download_sde_zip(client: httpx.Client, build: int, progress=None) -> Path:
    """Download the versioned zip into the cache; reuse if already present."""
    config.SDE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.SDE_CACHE_DIR / f"eve-online-static-data-{build}-jsonl.zip"
    if dest.exists() and zipfile.is_zipfile(dest):
        log.info("using cached %s", dest.name)
        return dest
    url = config.SDE_ZIP_URL_TEMPLATE.format(build=build)
    log.info("downloading %s", url)
    # Per-process temp name: the CLI importer and the dashboard button can
    # legitimately race on the same build, and on Windows a shared .part
    # means one writer truncates the other mid-stream.
    tmp = dest.with_suffix(f".{os.getpid()}.part")
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        done = 0
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
                done += len(chunk)
                _report(progress, stage="download", done=done, total=total)
                if total and logsetup.is_tty():
                    print(
                        f"\r  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB",
                        end="",
                        flush=True,
                    )
        if logsetup.is_tty():
            print()
    try:
        tmp.replace(dest)
    except OSError:
        # Lost the rename race to a concurrent importer holding dest open
        # (Windows sharing violation). If the winner's zip is whole, use
        # it; anything else is a real error.
        if not (dest.exists() and zipfile.is_zipfile(dest)):
            raise
        tmp.unlink(missing_ok=True)
    return dest


def _read_dataset(zf: zipfile.ZipFile, dataset: str):
    """Locate a dataset member by exact basename; return a record stream.

    Exact match matters: the archive also carries shipTreeGroups.jsonl,
    marketGroups.jsonl, metaGroups.jsonl, ... which a suffix match on
    "groups.jsonl" would wrongly hit.

    The member is decompressed and parsed line-by-line (never materialized
    in RAM — types.jsonl alone is ~150MB decompressed). Reading through
    zipfile still verifies the member's CRC: ZipExtFile checks it as the
    stream is consumed. The member is opened lazily on first iteration and
    closed when the stream is exhausted, so streams held in pairs (dogma,
    industry modifiers) that are consumed one after the other never hold
    two members open at once.
    """
    wanted = f"{dataset}.jsonl".lower()
    for name in zf.namelist():
        basename = name.rsplit("/", 1)[-1].lower()
        if basename == wanted:
            return _stream_member(zf, name)
    raise FileNotFoundError(f"{dataset}.jsonl not found in archive")


def _stream_member(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        yield from _iter_jsonl(io.TextIOWrapper(fh, encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema (reference tables only — rebuilt wholesale)
# ---------------------------------------------------------------------------

REF_SCHEMA = """
CREATE TABLE ref_category (
    category_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);
CREATE TABLE ref_group (
    group_id    INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL,
    name        TEXT NOT NULL
);
CREATE TABLE ref_type (
    type_id          INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    group_id         INTEGER NOT NULL,
    category_id      INTEGER NOT NULL,
    volume           REAL,
    packaged_volume  REAL,
    published        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE ref_blueprint (
    blueprint_id INTEGER NOT NULL,
    activity_id  INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    portion_size INTEGER NOT NULL,
    base_time    INTEGER NOT NULL,
    max_runs     INTEGER,
    PRIMARY KEY (blueprint_id, activity_id)
);
CREATE INDEX idx_ref_blueprint_product ON ref_blueprint (product_id);
CREATE TABLE ref_blueprint_material (
    blueprint_id INTEGER NOT NULL,
    activity_id  INTEGER NOT NULL,
    material_id  INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (blueprint_id, activity_id, material_id)
);
-- Skill REQUIREMENTS (prerequisites, never demand). Needed for job-time
-- math: science/construction skills reduce time only on blueprints that
-- require them.
CREATE TABLE ref_blueprint_skill (
    blueprint_id  INTEGER NOT NULL,
    activity_id   INTEGER NOT NULL,
    skill_type_id INTEGER NOT NULL,
    level         INTEGER NOT NULL,
    PRIMARY KEY (blueprint_id, activity_id, skill_type_id)
);
CREATE TABLE ref_type_attribute (
    type_id        INTEGER NOT NULL,
    attribute_id   INTEGER NOT NULL,
    attribute_name TEXT NOT NULL,
    value          REAL NOT NULL,
    PRIMARY KEY (type_id, attribute_id)
);
-- Attribute definitions (default_value kept for reference only —
-- industry.py consumes only multiplier-style structure attributes via
-- industryModifierSources).
CREATE TABLE ref_dogma_attribute (
    attribute_id  INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    default_value REAL NOT NULL DEFAULT 0.0
);
-- Industry bonus applicability, straight from CCP data (no heuristics):
-- which dogma attribute carries a source type's bonus, per activity and
-- bonus kind, and which target filter limits what it applies to.
CREATE TABLE ref_industry_modifier (
    source_type_id     INTEGER NOT NULL,
    activity_id        INTEGER NOT NULL,
    kind               TEXT NOT NULL,      -- material / time / cost
    dogma_attribute_id INTEGER NOT NULL,
    filter_id          INTEGER,            -- NULL = applies to everything
    PRIMARY KEY (source_type_id, activity_id, kind, dogma_attribute_id)
);
CREATE TABLE ref_industry_target_filter (
    filter_id INTEGER NOT NULL,
    name      TEXT,
    kind      TEXT NOT NULL,               -- category / group
    ref_id    INTEGER NOT NULL,            -- category_id or group_id
    PRIMARY KEY (filter_id, kind, ref_id)
);
-- Reprocessing outputs (typeMaterials). Used for alchemy: an Unrefined
-- reaction product reprocesses into its composite plus recovered inputs.
-- Records with randomizedMaterials (mineral alchemy min/max ranges) are
-- skipped — only fixed-quantity outputs are planable.
CREATE TABLE ref_type_material (
    type_id     INTEGER NOT NULL,
    material_id INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    PRIMARY KEY (type_id, material_id)
);
CREATE TABLE ref_solar_system (
    system_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    security  REAL NOT NULL,
    region_id INTEGER
);
CREATE TABLE ref_sde_build (
    build_number INTEGER NOT NULL,
    imported_at  TEXT NOT NULL
);
"""

REF_TABLES = (
    "ref_category",
    "ref_group",
    "ref_type",
    "ref_blueprint",
    "ref_blueprint_material",
    "ref_blueprint_skill",
    "ref_type_attribute",
    "ref_dogma_attribute",
    "ref_industry_modifier",
    "ref_industry_target_filter",
    "ref_type_material",
    "ref_solar_system",
    "ref_sde_build",
)


def _rebuild_ref_schema(conn: sqlite3.Connection) -> None:
    """Drop and recreate every ref table INSIDE the caller's transaction.

    Statement-by-statement on purpose: executescript() force-COMMITs any
    open transaction before running, which used to durably commit the
    drops the moment this returned — a crash anywhere in the minutes of
    inserts that follow then left every ref table empty and the previous
    working build destroyed. SQLite DDL is fully transactional, so under
    one explicit transaction a failed import rolls back to the old build.
    """
    for table in REF_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for statement in REF_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


# ---------------------------------------------------------------------------
# Dataset importers
# ---------------------------------------------------------------------------


def _import_categories(conn, records) -> int:
    rows = [
        (_record_id(r, "categoryID"), _localized(_first(r, "name", "categoryName")))
        for r in records
    ]
    conn.executemany("INSERT INTO ref_category VALUES (?, ?)", rows)
    return len(rows)


def _import_groups(conn, records) -> int:
    rows = [
        (
            _record_id(r, "groupID"),
            int(_first(r, "categoryID", "category_id")),
            _localized(_first(r, "name", "groupName")),
        )
        for r in records
    ]
    conn.executemany("INSERT INTO ref_group VALUES (?, ?, ?)", rows)
    return len(rows)


def _import_types(conn, records) -> int:
    group_to_category = dict(
        conn.execute("SELECT group_id, category_id FROM ref_group").fetchall()
    )
    rows = []
    for r in records:
        type_id = _record_id(r, "typeID")
        group_id = int(_first(r, "groupID", "group_id", default=0))
        rows.append(
            (
                type_id,
                _localized(_first(r, "name", "typeName")) or f"type {type_id}",
                group_id,
                group_to_category.get(group_id, 0),
                _first(r, "volume"),
                _first(r, "packagedVolume"),
                1 if _first(r, "published", default=False) else 0,
            )
        )
    conn.executemany(
        "INSERT INTO ref_type VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    return len(rows)


def _import_blueprints(conn, records) -> tuple[int, int]:
    """Import manufacturing and reaction activities only (PROJECT.md §4).

    Skips: activities other than manufacturing/reaction, activities with no
    products, materials in the skill category (16) — skills are
    prerequisites, not demand — and blueprints whose blueprint TYPE is
    unpublished (the SDE carries internal junk like "Test Reaction
    Blueprint" producing real products; picking it silently corrupts BOM
    quantities). Non-consumed materials are imported with consumed=0 so BOM
    expansion can exclude them from deficits while still knowing they must
    be on hand.
    """
    skill_type_ids = {
        row["type_id"]
        for row in conn.execute(
            "SELECT type_id FROM ref_type WHERE category_id = ?",
            (config.CATEGORY_SKILL,),
        )
    }
    unpublished = {
        row["type_id"]
        for row in conn.execute(
            "SELECT type_id FROM ref_type WHERE published = 0"
        )
    }
    bp_rows, mat_rows, skill_rows = [], [], []
    for r in records:
        bp_id = _record_id(r, "blueprintTypeID")
        if bp_id in unpublished:
            continue
        max_runs = _first(r, "maxProductionLimit")
        activities = _first(r, "activities", default={}) or {}
        for act_name, act_id in config.SDE_ACTIVITY_IDS.items():
            activity = activities.get(act_name)
            if not activity:
                continue
            products = activity.get("products") or []
            if not products:
                continue
            product = products[0]
            for skill in activity.get("skills") or []:
                skill_rows.append(
                    (
                        bp_id,
                        act_id,
                        int(_first(skill, "typeID", "typeId", "type_id")),
                        int(_first(skill, "level", default=1)),
                    )
                )
            bp_rows.append(
                (
                    bp_id,
                    act_id,
                    int(_first(product, "typeID", "typeId", "type_id")),
                    int(_first(product, "quantity", default=1)),
                    int(_first(activity, "time", default=0)),
                    max_runs,
                )
            )
            for material in activity.get("materials") or []:
                mat_id = int(_first(material, "typeID", "typeId", "type_id"))
                if mat_id in skill_type_ids:
                    continue
                consumed = _first(material, "consumed", default=True)
                mat_rows.append(
                    (
                        bp_id,
                        act_id,
                        mat_id,
                        int(_first(material, "quantity", default=0)),
                        1 if consumed else 0,
                    )
                )
    conn.executemany(
        "INSERT INTO ref_blueprint VALUES (?, ?, ?, ?, ?, ?)", bp_rows
    )
    conn.executemany(
        "INSERT OR REPLACE INTO ref_blueprint_material VALUES (?, ?, ?, ?, ?)",
        mat_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO ref_blueprint_skill VALUES (?, ?, ?, ?)",
        skill_rows,
    )
    return len(bp_rows), len(mat_rows)


def _import_type_attributes(conn, dogma_records, type_dogma_records) -> int:
    # dogma_records is fully consumed (building attr_names) before
    # type_dogma_records is first touched, so only one zip member stream
    # is open at a time.
    attr_names = {}
    def_rows = []
    for r in dogma_records:
        attr_id = _record_id(r, "attributeID")
        name = _first(r, "name", "attributeName")
        attr_names[attr_id] = name
        def_rows.append(
            (attr_id, name, float(_first(r, "defaultValue", default=0.0)))
        )
    conn.executemany(
        "INSERT OR REPLACE INTO ref_dogma_attribute VALUES (?, ?, ?)", def_rows
    )
    rows = []
    for r in type_dogma_records:
        type_id = _record_id(r, "typeID")
        attributes = _first(r, "dogmaAttributes", "attributes", default=[]) or []
        for attr in attributes:
            attr_id = int(_first(attr, "attributeID", "attributeId"))
            rows.append(
                (
                    type_id,
                    attr_id,
                    attr_names.get(attr_id, f"attr{attr_id}"),
                    float(_first(attr, "value", default=0.0)),
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO ref_type_attribute VALUES (?, ?, ?, ?)", rows
    )
    return len(rows)


def _import_industry_modifiers(conn, sources_records, filters_records):
    """CCP's data-driven industry bonus system.

    industryModifierSources: per source type (structure or rig), per activity,
    per bonus kind, the dogma attribute holding the bonus value and optionally
    a target filter restricting which products it applies to.

    industryTargetFilters: filter_id -> category IDs and/or group IDs.

    Only manufacturing and reaction activities are kept.
    """
    mod_rows = []
    for r in sources_records:
        source_type_id = _record_id(r)
        for act_name, act_id in config.SDE_ACTIVITY_IDS.items():
            activity = r.get(act_name)
            if not activity:
                continue
            for kind in ("material", "time", "cost"):
                for entry in activity.get(kind) or []:
                    mod_rows.append(
                        (
                            source_type_id,
                            act_id,
                            kind,
                            int(entry["dogmaAttributeID"]),
                            entry.get("filterID"),
                        )
                    )
    conn.executemany(
        "INSERT OR REPLACE INTO ref_industry_modifier VALUES (?, ?, ?, ?, ?)",
        mod_rows,
    )
    filter_rows = []
    for r in filters_records:
        filter_id = _record_id(r)
        name = _localized(r.get("name"))
        for category_id in r.get("categoryIDs") or []:
            filter_rows.append((filter_id, name, "category", int(category_id)))
        for group_id in r.get("groupIDs") or []:
            filter_rows.append((filter_id, name, "group", int(group_id)))
    conn.executemany(
        "INSERT OR REPLACE INTO ref_industry_target_filter VALUES (?, ?, ?, ?)",
        filter_rows,
    )
    return len(mod_rows), len(filter_rows)


def _import_type_materials(conn, records) -> int:
    """Reprocessing outputs. Records carrying only randomizedMaterials
    (mineral alchemy, min/max ranges) have no fixed yield and are skipped."""
    rows = []
    for r in records:
        type_id = _record_id(r, "typeID")
        for material in r.get("materials") or []:
            rows.append(
                (
                    type_id,
                    int(_first(material, "materialTypeID", "typeID")),
                    int(_first(material, "quantity", default=0)),
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO ref_type_material VALUES (?, ?, ?)", rows
    )
    return len(rows)


def _import_solar_systems(conn, records) -> int:
    rows = []
    for r in records:
        system_id = _record_id(r, "solarSystemID")
        name = _localized(_first(r, "name", "solarSystemName"))
        security = _first(r, "securityStatus", "security", default=0.0)
        rows.append(
            (
                system_id,
                name or f"system {system_id}",
                float(security),
                _first(r, "regionID", "region_id"),
            )
        )
    conn.executemany("INSERT INTO ref_solar_system VALUES (?, ?, ?, ?)", rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_import(force: bool = False, progress=None) -> bool:
    """Pull-and-import if the SDE build changed. Returns True if imported."""
    conn = store.connect()
    try:
        _report(progress, stage="check")
        with httpx.Client(
            follow_redirects=True,
            timeout=60.0,
            headers={"User-Agent": config.USER_AGENT},
        ) as client:
            build = fetch_latest_build(client)
            current = None
            try:
                current = stored_build(conn)
            except sqlite3.OperationalError:
                pass  # first run, no ref tables yet
            if current == build and not force:
                log.info("SDE build %s already imported; nothing to do", build)
                _report(progress, stage="current", build=build)
                return False
            log.info("importing SDE build %s (had: %s)", build, current)
            _report(progress, stage="resolved", build=build, had=current)
            archive = download_sde_zip(client, build, progress)

        with zipfile.ZipFile(archive) as zf:
            t0 = time.monotonic()

            def step(i: int, dataset: str):
                _report(progress, stage="import", dataset=dataset, step=i, steps=8)

            # One transaction spans the whole rebuild + import: a failure
            # anywhere (CCP schema drift, power loss) rolls back to the
            # previous working build instead of leaving committed-empty
            # ref tables. BEGIN IMMEDIATE takes the write lock up front —
            # a concurrently running app keeps reading the old build via
            # WAL until the single commit below.
            conn.execute("BEGIN IMMEDIATE")
            _rebuild_ref_schema(conn)
            step(1, "categories")
            n = _import_categories(conn, _read_dataset(zf, "categories"))
            log.info(f"  ref_category            {n:>9,}")
            step(2, "groups")
            n = _import_groups(conn, _read_dataset(zf, "groups"))
            log.info(f"  ref_group               {n:>9,}")
            step(3, "types")
            n = _import_types(conn, _read_dataset(zf, "types"))
            log.info(f"  ref_type                {n:>9,}")
            step(4, "blueprints")
            n_bp, n_mat = _import_blueprints(conn, _read_dataset(zf, "blueprints"))
            log.info(f"  ref_blueprint           {n_bp:>9,}")
            log.info(f"  ref_blueprint_material  {n_mat:>9,}")
            step(5, "dogma attributes")
            n = _import_type_attributes(
                conn,
                _read_dataset(zf, "dogmaAttributes"),
                _read_dataset(zf, "typeDogma"),
            )
            log.info(f"  ref_type_attribute      {n:>9,}")
            step(6, "industry modifiers")
            n_mod, n_filt = _import_industry_modifiers(
                conn,
                _read_dataset(zf, "industryModifierSources"),
                _read_dataset(zf, "industryTargetFilters"),
            )
            log.info(f"  ref_industry_modifier   {n_mod:>9,}")
            log.info(f"  ref_industry_target_f.  {n_filt:>9,}")
            step(7, "reprocessing yields")
            n = _import_type_materials(conn, _read_dataset(zf, "typeMaterials"))
            log.info(f"  ref_type_material       {n:>9,}")
            step(8, "solar systems")
            n = _import_solar_systems(conn, _read_dataset(zf, "mapSolarSystems"))
            log.info(f"  ref_solar_system        {n:>9,}")
            _report(progress, stage="finalize", build=build)
            conn.execute(
                "INSERT INTO ref_sde_build VALUES (?, datetime('now'))", (build,)
            )
            conn.commit()
            log.info("done in %.1fs", time.monotonic() - t0)
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Background import (web UI)
# ---------------------------------------------------------------------------


class ImportJob:
    """One in-process background SDE import, polled by the dashboard.

    The worker thread owns its own DB connection (run_import opens one), so
    no sqlite objects cross threads. daemon=True: the import is one
    transaction, so a process exit mid-run rolls back to the previous
    build. status() returns a snapshot dict — "state" is idle/running/
    done/error; while running the run_import progress events above are
    merged in; "done" carries "changed", "error" carries "error"."""

    def __init__(self, runner=None):
        # None = late-bind to this module's run_import at start time, so
        # tests that monkeypatch sdeimport.run_import are honored.
        self._runner = runner
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status: dict = {"state": "idle"}

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def start(self, force: bool = False) -> bool:
        """Spawn the worker; False (and no effect) if one is running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._status = {"state": "running", "stage": "check"}
            self._thread = threading.Thread(
                target=self._run, args=(force,), daemon=True
            )
            try:
                self._thread.start()
            except RuntimeError as exc:
                # Spawn failure (thread exhaustion) must not wedge the
                # status at "running" — nothing would ever clear it and
                # the UI keeps its button disabled forever.
                self._thread = None
                self._status = {
                    "state": "error",
                    "error": str(exc) or "could not start the import thread",
                }
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Join the worker (tests, mainly); True when none is running."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _emit(self, event: dict) -> None:
        with self._lock:
            self._status.update(event)

    def _run(self, force: bool) -> None:
        runner = self._runner or run_import
        try:
            changed = runner(force=force, progress=self._emit)
            self._emit({"state": "done", "changed": bool(changed)})
        except Exception as exc:  # shown in the UI, never a raw 500
            self._emit(
                {"state": "error", "error": str(exc) or type(exc).__name__}
            )


def probe() -> None:
    """Dump latest.jsonl and the first record of each dataset, for inspecting
    the real field shapes before trusting the importer."""
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        resp = client.get(config.SDE_LATEST_URL)
        resp.raise_for_status()
        print("--- latest.jsonl ---")
        print(resp.text.strip())
        build = fetch_latest_build(client)
        print(f"\nresolved build: {build}")
        archive = download_sde_zip(client, build)
    with zipfile.ZipFile(archive) as zf:
        for dataset in config.SDE_DATASETS:
            records = _read_dataset(zf, dataset)
            first = next(records)
            records.close()  # release the member stream mid-file
            print(f"\n--- {dataset} (first record) ---")
            print(json.dumps(first, indent=2)[:2000])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="reimport same build")
    parser.add_argument("--probe", action="store_true", help="dump raw shapes")
    args = parser.parse_args(argv)
    if args.probe:
        probe()
        return 0
    run_import(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
