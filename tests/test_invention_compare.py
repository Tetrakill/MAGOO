"""The Pipelines-page invention comparison window (2026-09-05): every
source × decryptor option of one capable pipeline costed end to end at
today's prices — costing.current_hull_cost's what-if `invention` pair,
the GET /pipelines/<id>/compare fragment, and the row's compare button.

Same fixtures as test_invention: real reference data from the production
SDE (read-only), temp state per test. Zealot (T2, one source, 9 options)
and the Tengu hull (T3, three Hull Section relic tiers, 27 options) are
the worked examples.
"""

from datetime import datetime, timezone

from magoo import config, costing, engine, store

from conftest import FairValuePrices
from test_invention import (
    _state,
    add_pipeline,
    conn,  # noqa: F401 — the fixture
    decryptor_named,
    enable_invention,
    get_pipeline,
)

INTACT_HULL, MALF_HULL, WRECKED_HULL = 30752, 30753, 30754


def _rows(fragment: str) -> list[str]:
    """The data rows of the comparison table (the header row excluded)."""
    return fragment.split("<tr")[2:]


def _seed_prices(ref):
    """Cache a FairValuePrices quote for everything the pipelines now
    demand (finals included — their sell quote), the way the
    seeded_client fixture does for its Hulk."""
    c = store.connect()
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


def _pid(c, name):
    return c.execute(
        "SELECT pipeline_id FROM pipeline WHERE name = ?", (name,)
    ).fetchone()["pipeline_id"]


# --- costing: the what-if pair ---------------------------------------------


def test_what_if_invention_replaces_the_stored_choice(conn, ref):
    """With invention OFF the stored path amortizes the manual BPC cost;
    the what-if pair prices that option's invention lines instead and
    walks the chain at ITS invented ME — a decryptor's ME modifier moves
    the materials bill, not only the invention line."""
    pid = add_pipeline(conn, ref, "Zealot", 2, runs_per_bpc=10, bpc_cost=1e9)
    pipeline = get_pipeline(conn, pid)
    settings = store.get_settings(conn)
    prices = FairValuePrices(ref)
    source = ref.invention_source_for_product(ref.type_id("Zealot"))
    accelerant = decryptor_named(ref, "Accelerant Decryptor")

    stored = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices
    )
    assert {line.kind for line in stored.lines} >= {"bpc", "material"}
    assert not [line for line in stored.lines if line.kind == "invention"]

    plain = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices, invention=(source, None)
    )
    boosted = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices,
        invention=(source, accelerant),
    )
    for cost in (plain, boosted):
        assert not [line for line in cost.lines if line.kind == "bpc"]
        assert [line for line in cost.lines if line.kind == "invention"]
    names = {line.name for line in boosted.lines if line.kind == "invention"}
    assert "Accelerant Decryptor" in names
    assert "Accelerant Decryptor" not in {
        line.name for line in plain.lines if line.kind == "invention"
    }
    # ME 2 (no decryptor) vs ME 4 (Accelerant): every direct material of
    # the Zealot that takes more than one unit per hull shrinks by the
    # same 0.96/0.98 factor.
    by_name = lambda cost: {
        line.name: line.qty_per_hull for line in cost.lines
        if line.kind == "material" and line.depth == 1
    }
    plain_qty, boosted_qty = by_name(plain), by_name(boosted)
    shrunk = [
        name for name, qty in plain_qty.items()
        if qty > 1 and boosted_qty[name] < qty
    ]
    assert shrunk, "no ME-sensitive depth-1 material shrank"
    for name in shrunk:
        assert boosted_qty[name] / plain_qty[name] == 0.96 / 0.98
    assert boosted.subtotal("material") < plain.subtotal("material")
    # The invention line reflects the option's chance: the Accelerant
    # (prob ×1.2 vs ×1.0, 2 runs vs 1) amortizes cheaper per hull.
    assert boosted.subtotal("invention") < plain.subtotal("invention")


def test_what_if_pair_beats_a_stored_decryptor(conn, ref):
    pid = add_pipeline(conn, ref, "Zealot", 2)
    attainment = decryptor_named(ref, "Attainment Decryptor")
    source = enable_invention(conn, ref, pid, "Zealot", decryptor=attainment)
    pipeline = get_pipeline(conn, pid)
    settings = store.get_settings(conn)
    prices = FairValuePrices(ref)
    accelerant = decryptor_named(ref, "Accelerant Decryptor")

    stored = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices
    )
    what_if = costing.current_hull_cost(
        conn, ref, settings, pipeline, prices, prices,
        invention=(source, accelerant),
    )
    inv_names = lambda cost: {
        line.name for line in cost.lines if line.kind == "invention"
    }
    assert "Attainment Decryptor" in inv_names(stored)
    assert inv_names(what_if) & {"Accelerant Decryptor"}
    assert "Attainment Decryptor" not in inv_names(what_if)


# --- the fragment -----------------------------------------------------------


def test_compare_fragment_lists_every_option_best_first(seeded_client, ref):
    seeded_client.post("/pipelines", data={"products": "Zealot\t2\t10"})
    _seed_prices(ref)
    c = _state()
    pid = _pid(c, "Zealot")

    resp = seeded_client.get(f"/pipelines/{pid}/compare")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Zealot — invention comparison" in html
    rows = _rows(html)
    assert len(rows) == 1 + len(ref.decryptors())
    assert "No decryptor" in html
    for d in ref.decryptors():
        assert d.name in html
    # Single-source: no Source column; invention off: no current badge.
    assert "<th>Source</th>" not in html
    assert ">current<" not in html
    assert "invention is off" in html
    # Priced end to end: a sell quote, margins, one best row on top.
    assert "no sell quote" not in html
    assert html.count(">best<") == 1
    assert ">best<" in rows[0]
    assert 'class="top"' in rows[0]
    assert "unpriced" not in html

    # Enable the Accelerant: its row (and only its row) is badged current.
    accelerant = decryptor_named(ref, "Accelerant Decryptor")
    seeded_client.post(
        f"/pipelines/{pid}/invention",
        data={"decryptor": str(accelerant.type_id)},
    )
    html = seeded_client.get(f"/pipelines/{pid}/compare").get_data(as_text=True)
    assert html.count(">current<") == 1
    current = next(row for row in _rows(html) if ">current<" in row)
    assert "Accelerant Decryptor" in current
    assert "current choice badged" in html
    c.close()


def test_compare_fragment_without_prices_still_compares(seeded_client, ref):
    """No cached quotes at all: costs count 0 and are badged unpriced,
    margins read as unavailable — the window never errors on an
    unrefreshed cache."""
    seeded_client.post("/pipelines", data={"products": "Zealot\t2"})
    c = _state()
    pid = _pid(c, "Zealot")
    html = seeded_client.get(f"/pipelines/{pid}/compare").get_data(as_text=True)
    assert "no sell quote cached for Zealot" in html
    assert len(_rows(html)) == 1 + len(ref.decryptors())
    assert ">best<" not in html
    assert "unpriced" in html
    c.close()


def test_compare_fragment_relic_tiers(seeded_client, ref):
    """A T3 hull compares every Hull Section tier × decryptor — 27 rows
    with a Source column — and the Intact tier's 20-run copies carry the
    ship-batch note."""
    seeded_client.post("/pipelines", data={"products": "Tengu\t2"})
    _seed_prices(ref)
    c = _state()
    pid = _pid(c, "Tengu")
    html = seeded_client.get(f"/pipelines/{pid}/compare").get_data(as_text=True)
    assert "<th>Source</th>" in html
    assert len(_rows(html)) == 3 * (1 + len(ref.decryptors()))
    for relic in (INTACT_HULL, MALF_HULL, WRECKED_HULL):
        assert ref.type_info(relic).name in html
    assert "builds in whole 20-hull batches" in html
    assert html.count(">best<") == 1
    c.close()


def test_compare_fragment_404s_for_uncapable_and_unknown(seeded_client, ref):
    seeded_client.post("/pipelines", data={"products": "Omen\t2"})
    c = _state()
    pid = _pid(c, "Omen")
    assert seeded_client.get(f"/pipelines/{pid}/compare").status_code == 404
    assert seeded_client.get("/pipelines/999999/compare").status_code == 404
    c.close()


def test_compare_fragment_ignores_a_stale_decryptor(seeded_client, ref):
    """A stale config (decryptor id gone) still compares — the window is
    how the user picks a fresh choice — with no row badged current."""
    c = _state()
    pid = _pid(c, "Hulk")
    c.execute(
        "UPDATE pipeline SET use_invention = 1, decryptor_type_id = 1, "
        "manual_runs_per_bpc = runs_per_bpc, runs_per_bpc = 2 "
        "WHERE pipeline_id = ?",
        (pid,),
    )
    c.commit()
    html = seeded_client.get(f"/pipelines/{pid}/compare").get_data(as_text=True)
    assert len(_rows(html)) == 1 + len(ref.decryptors())
    assert ">current<" not in html
    c.close()


# --- the button ---------------------------------------------------------------


def test_pipelines_page_offers_compare_only_where_it_applies(seeded_client, ref):
    seeded_client.post("/pipelines", data={"products": "Omen\t2"})
    c = _state()
    hulk, omen = _pid(c, "Hulk"), _pid(c, "Omen")
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    # One button — the capable Hulk's — pointing at its own fragment; the
    # comparison itself is never inlined.
    assert page.count(">compare</button>") == 1
    assert f'data-url="/pipelines/{hulk}/compare"' in page
    assert f'data-url="/pipelines/{omen}/compare"' not in page
    assert 'class="sortable profit compare"' not in page
    assert 'id="cmp"' in page
    # A stale row loses the button along with its selects.
    c.execute(
        "UPDATE pipeline SET use_invention = 1, decryptor_type_id = 1, "
        "manual_runs_per_bpc = runs_per_bpc, runs_per_bpc = 2 "
        "WHERE pipeline_id = ?",
        (hulk,),
    )
    c.commit()
    page = seeded_client.get("/pipelines").get_data(as_text=True)
    assert "stale — source or decryptor gone" in page
    assert ">compare</button>" not in page
    c.close()
