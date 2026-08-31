"""Shared fixtures.

Reference data comes from the live imported SDE in data/magoo.sqlite — the
user's PRODUCTION database — so the suite opens it strictly read-only. Tests
that need state build it in a temp database of their own.
"""

import sqlite3

import pytest

from magoo import config
from magoo.refdata import Refdata


class FairValuePrices(dict):
    """Price model where every buildable sells for `margin` x its input
    cost (raws at `base`), so build savings stay positive and plans build
    — matching a production line the user would actually run. Since the
    2026-08-20 decision, zero/negative-savings items are BOUGHT even with
    idle slots, which made flat uniform prices (where every deep build is
    a loss) a degenerate fixture. Tests override individual prices via
    `overrides` to steer specific decisions."""

    def __init__(self, ref, overrides=None, base=10.0, margin=2.0):
        super().__init__()
        self._ref = ref
        self._base = base
        self._margin = margin
        self._overrides = dict(overrides or {})
        self._memo: dict[int, float] = {}

    def get(self, key, default=None):
        # An override of None means "no price on record" to callers, but
        # inside the recursion it falls back to base so parents still get
        # a fair value.
        if key in self._overrides and self._overrides[key] is None:
            return None
        return self._price(key, frozenset())

    def _price(self, type_id, visiting):
        if type_id in self._overrides:
            value = self._overrides[type_id]
            return self._base if value is None else value
        if type_id in self._memo:
            return self._memo[type_id]
        blueprint = self._ref.blueprint_for_product(type_id)
        if blueprint is None or type_id in visiting:
            return self._base
        total = sum(
            qty * self._price(m, visiting | {type_id})
            for m, qty in self._ref.materials(
                blueprint.blueprint_id, blueprint.activity_id
            )
        )
        price = self._margin * total / blueprint.portion_size + self._base
        self._memo[type_id] = price
        return price


def template_app():
    """The Flask app with base.html's context processor stubbed, so
    rendering a template never opens the production database
    (nav_status() would otherwise run ensure_schema against
    config.DB_PATH). The web routes have no test client; render tests
    call render_template inside app.test_request_context()."""
    from magoo import web

    app = web.create_app()
    app.config["TESTING"] = True
    stub = {
        "esi_at": None, "esi_stale": True, "prices_at": None,
        "prices_stale": True, "corp_isk": None, "sde_build": 1,
    }
    # This REPLACES the real context processors, so it must mirror
    # everything base.html reads — a missing key is an UndefinedError in
    # every template test at once, not just the one that added it.
    app.template_context_processors[None] = [
        lambda: {
            "CLASS_LABELS": web.CLASS_LABELS,
            "nav_status": lambda: stub,
            "magoo_version": web.__version__,
            # No database here, so the banner is simply never shown.
            "update_banner": lambda: None,
        }
    ]
    return app


@pytest.fixture(scope="session")
def ref():
    conn = sqlite3.connect(
        f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True
    )
    conn.row_factory = sqlite3.Row
    r = Refdata(conn)
    assert r.sde_build() is not None, "run `python -m magoo.sdeimport` first"
    yield r
    r.close()


@pytest.fixture()
def seeded_client(tmp_path, monkeypatch, ref):
    """Request-level Flask client over a POPULATED app — test_onboarding's
    fresh_app reaches only the empty-database guidance paths; this one
    reaches planning, the run lifecycle, and the settings save.

    Wiring, seam by seam (least-invasive choices, per the fixture contract):

    * State: config.DB_PATH / DATA_DIR point at a temp database (the
      fresh_app pattern), so nothing here can touch production state.
    * Reference data: store.connect is monkeypatched to ATTACH the
      production SDE database READ-ONLY (URI mode=ro) to every app
      connection. SQLite resolves unqualified table names main-first, so
      the state tables ensure_schema creates in `main` shadow production's
      state, while the ref_* tables (absent from main) resolve to the
      attached schema — web.py's sde_ready(), its Refdata, and every
      state-vs-ref SQL join work unchanged, and a write to the attached
      file fails with 'attempt to write a readonly database' (verified).
    * Planning inputs are seeded STATE, not monkeypatches: one empty ESI
      snapshot row (store.save_esi_snapshot) and a cached market_price
      quote per demanded type at FairValuePrices levels, so POST /run
      exercises the real market.buy_quotes -> engine.snapshot_from_state
      -> engine.plan_index_run path end to end. Both paths are cache-only;
      no network is ever touched.
    * Seeded world: one pipeline (Hulk x 8), ample slot pools (500/500).
    """
    from datetime import datetime, timezone

    from magoo import engine, store, web

    prod = config.DB_PATH.as_posix()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MAGOO_SECRET", "test-secret")

    def connect_with_ref():
        # Mirrors store.connect but opens with uri=True so the ATTACH
        # below gets URI processing (mode=ro is what keeps production
        # read-only; a plain-path attach would mount it writable).
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            f"file:{config.DB_PATH.as_posix()}?mode=rwc",
            uri=True,
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(
            "ATTACH DATABASE ? AS refdb", (f"file:{prod}?mode=ro",)
        )
        return conn

    monkeypatch.setattr(store, "connect", connect_with_ref)

    c = connect_with_ref()
    store.ensure_schema(c)
    c.execute(
        "UPDATE settings SET manufacturing_slots = 500, reaction_slots = 500"
    )
    c.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run) VALUES ('Hulk', ?, 8)",
        (ref.type_id("Hulk"),),
    )
    c.commit()
    store.save_esi_snapshot(c, {}, {}, {}, 0.0, 0.0)
    prices = FairValuePrices(ref)
    now = datetime.now(timezone.utc).isoformat()
    settings_ = store.get_settings(c)
    c.executemany(
        "INSERT OR REPLACE INTO market_price "
        "(type_id, region_id, source, price, fetched_at, hub) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        [
            (
                type_id,
                settings_.price_region_id,
                settings_.price_source,
                prices.get(type_id),
                now,
            )
            for type_id in engine.demand_type_ids(c, ref)
        ],
    )
    c.commit()
    c.close()

    app = web.create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client
