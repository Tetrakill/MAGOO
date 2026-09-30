"""Buy tab (v1.29 revision 3, user rulings 2026-09-28) at the request
level: purchases come from ESI, so the tab has no controls — the strip
(Required / On Hand / Remaining / Purchased and its badges), Multibuy All,
the family groups with Required · On Hand · Remaining · Purchased ·
Ladder · Δ (revision 6, user ruling R4 2026-09-28) and each group's
Multibuy of the plan's buy list, and the Purchases section. Revision 6
(R1): the open run is re-planned in place on every ESI update, so the
page subtracts nothing from the plan — a purchase shows in Purchased and
reaches Remaining only through the next re-plan's stock. Revision 8 (user
rulings 2026-09-29): the Purchased cell carries no badge (its sources are
title clauses), a group's Multibuy is one plain list of its Remaining,
no group renders a compressed ore (Multibuy All alone lists them), and the
groups sit collapsed under one "Input Materials" section. Plus the ESI
page's Count buys marker and every place the app
runs buying.assign_purchases (the ESI update, planning / discarding a
run, run complete / reopen, a Count buys toggle, removing a character,
and a Buy tab view whose run has no lines, only when the pass's inputs
changed — review 2026-09-28).

Runs are built by direct INSERT (like tests/test_ledger_page.py) so each
case owns exactly the rows it reasons about; purchases are written with
store.replace_derived_purchases — what the matcher writes — so these cases
do not depend on the matcher itself (tests/test_buying.py covers it). Where
a route calls the matcher, the real module's entry points are patched to
record the call (web imports magoo.buying with its other modules).
"""

import dataclasses
import html
import json
import re
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from flask import render_template

from magoo import config, costing, esi, ledger, market, store

from conftest import template_app

TRITANIUM = 34
HYDROCARBONS = 16633          # group 427 Moon Materials
FULLERITE = 30370             # group 711 Harvestable Cloud -> "Gas"
NITROGEN_FUEL = 4051          # group 1136 Fuel Block
COOLANT = 9832                # category 43 Planetary Commodities
TUNGSTEN = 16672              # group 429 "Composite" — an unranked group
COMPRESSED_VELDSPAR = 62516   # category 25; covers Tritanium

TRIT_M3 = 0.01
ORE_M3 = 0.001                # Compressed Veldspar, packaged
HUB_RATE = 100.0              # ISK/m³ -> 1.00 ISK of freight per Tritanium
STRUCTURE_RATE = 200.0        # -> 2.00 ISK per Tritanium

BUYER = 90000001
BUYER_CORP = 98000001
NPC_STATION = 60003760        # Jita 4-4: an NPC station -> the hub venue
NOW = "2026-09-21T10:00:00Z"


def _state():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _settings(c):
    return store.get_settings(c)


def _run(c, status="planned", hub_rate=HUB_RATE, structure_rate=STRUCTURE_RATE,
         run_number=1, default_rate=None):
    """One run. `default_rate` is the inbound rate it was planned at for a
    purchase at neither market (revision 4); None is a run planned
    before that column, which reads the live setting."""
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, planned_end, "
        "status, freight_in_isk_per_m3, structure_freight_in_isk_per_m3, "
        "freight_in_default_isk_per_m3) "
        "VALUES (?, '2026-09-20', '2026-09-27', ?, ?, ?, ?)",
        (run_number, status, hub_rate, structure_rate, default_rate),
    )
    c.commit()
    return c.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]


def _no_newer_update(c):
    """Drop the fixture's ESI snapshot (fetched today, after these runs'
    hand-set planned_start): with a newer update on record the live plan
    reads as STALE — that update's re-plan was skipped (review
    2026-09-28) — and the page drops the transit cue and the "already off
    Remaining" wording. Tests of the fresh live plan call this."""
    c.execute("DELETE FROM esi_snapshot")
    c.commit()


def _item(c, run_id, type_id, qty=100, price=10.0, **extra):
    """One bought row. hub_buy_qty defaults to the whole quantity, which
    is what a plan that bought it all at Jita persists; deficit_qty and
    target_stock_qty default to the row's whole demand (direct + covered)
    — a just-in-time raw with nothing on hand, which is what the engine
    persists for one (revision 6: Required reads them, web._required_of)."""
    demand = qty + int(extra.get("compressed_covered_qty") or 0)
    cols = {
        "index_run_id": run_id,
        "type_id": type_id,
        "recommended_buy_qty": qty,
        "price_snapshot": price,
        "recommended_action": "buy",
        "depth": 1,
        "buy_venue": store.BUY_VENUE_HUB,
        "hub_buy_qty": qty,
        "structure_buy_qty": 0,
        "hub_fill_price": price,
        "unfilled_qty": 0,
        "deficit_qty": demand,
        "target_stock_qty": demand,
    }
    cols.update(extra)
    names = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    c.execute(
        f"INSERT INTO index_run_item ({names}) VALUES ({marks})",
        tuple(cols.values()),
    )
    c.commit()


def _covered_pair(c, run_id, direct=100, covered=400, ore_qty=5,
                  ore_price=1000.0, alloc=1.0, outputs=None):
    """A compressed ore and the Tritanium it covers, wired the way the
    engine persists them: the ore carries the allocation and its landed
    ISK, the raw carries the covered quantity, its blended effective cost
    and the landed ISK of its direct remainder. `outputs` overrides the
    ore's [[material, out, used], ...] (default: Tritanium, out == used ==
    covered)."""
    ore_landed = ore_qty * ore_price + HUB_RATE * ORE_M3 * ore_qty
    direct_landed = direct * 10.0 + HUB_RATE * TRIT_M3 * direct
    _item(
        c, run_id, COMPRESSED_VELDSPAR, qty=ore_qty, price=ore_price,
        compressed_outputs=json.dumps(
            outputs if outputs is not None
            else [[TRITANIUM, covered, covered]]
        ),
        compressed_alloc=json.dumps({str(TRITANIUM): alloc}),
        compressed_landed_isk=ore_landed, compressed_tax_isk=0.0,
    )
    effective = (direct_landed + alloc * ore_landed) / (direct + covered)
    _item(
        c, run_id, TRITANIUM, qty=direct, price=10.0,
        hub_buy_qty=direct, hub_fill_price=10.0 if direct else None,
        compressed_covered_qty=covered, effective_unit_cost=effective,
        direct_landed_isk=direct_landed,
    )
    return {
        "ore_landed": ore_landed,
        "direct_landed": direct_landed,
        "effective": effective,
        "demand": direct + covered,
        "plan_landed": effective * (direct + covered),
    }


def _line(type_id, qty, price, venue=store.BUY_VENUE_HUB,
          kind=store.ESI_KIND_TRANSACTION, esi_id=5001, k=None, date=NOW,
          owner=("character", BUYER)):
    return {
        "type_id": type_id, "venue": venue, "quantity": qty,
        "unit_price": price, "esi_kind": kind, "esi_id": esi_id,
        "contract_k": k, "date": date, "owner_kind": owner[0],
        "owner_id": owner[1],
    }


def _buy(c, run_id, *lines):
    """Write a run's ESI-derived lines the way the matcher does."""
    store.replace_derived_purchases(c, run_id, lines)
    c.commit()


def _contract(c, contract_id, price, title=None, k=None, reward=0.0,
              unpriced_items=0, priced_at=None, issuer_id=91000001,
              location=None):
    """One bought item exchange, as the Ledger pull writes it (issued by a
    stranger unless told otherwise, handed over at Jita 4-4 unless
    `location` says where); `priced_at` freezes a k already allocated
    (R7, C10.5)."""
    location = NPC_STATION if location is None else location
    c.execute(
        "INSERT INTO buy_contract (contract_id, owner_kind, owner_id, "
        "issuer_id, acceptor_id, type, status, price, reward, title, "
        "date_issued, date_accepted, date_completed, start_location_id, "
        "first_seen_at, last_seen_at, items_status, k, unpriced_items, "
        "priced_at) "
        "VALUES (?, 'character', ?, ?, ?, 'item_exchange', "
        "'finished', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?)",
        (contract_id, BUYER, issuer_id, BUYER, price, reward, title, NOW, NOW,
         NOW, location, NOW, NOW, k, unpriced_items, priced_at),
    )
    c.commit()


# Revision 4 (user ruling 2026-09-28; contract review A4): only the
# configured structure market is the structure venue — any other Upwell
# structure, and any NPC station but Jita 4-4, is "elsewhere".
STRUCTURE_LOCATION = config.CJ6_KEEPSTAR_STRUCTURE_ID   # the structure venue
OTHER_STRUCTURE = 1_035_466_617_946      # another Upwell structure -> elsewhere
AMARR_STATION = 60008494                 # an NPC station, not Jita 4-4 -> elsewhere
AMARR_SYSTEM = 30002187


def _tx_row(c, transaction_id, type_id, qty, price, date=NOW,
            location=NPC_STATION, owner=("character", BUYER), client_id=None):
    """One stored wallet BUY, as the Ledger pull writes it."""
    c.execute(
        "INSERT INTO buy_transaction (transaction_id, owner_kind, owner_id, "
        "source_feed, type_id, quantity, unit_price, date, location_id, "
        "client_id, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (transaction_id, owner[0], owner[1], owner[0], type_id, qty, price,
         date, location, client_id, NOW),
    )
    c.commit()


def _contract_item(c, contract_id, record_id, type_id, qty, unit_price=None,
                   raw_quantity=None, included=1):
    c.execute(
        "INSERT INTO buy_contract_item (contract_id, record_id, type_id, "
        "quantity, raw_quantity, is_included, unit_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (contract_id, record_id, type_id, qty,
         qty if raw_quantity is None else raw_quantity, included, unit_price),
    )
    c.commit()


def _match(c):
    """The real matcher over the stored ESI rows (magoo.buying)."""
    from magoo import buying

    return buying.assign_purchases(c, None, store.get_settings(c))


def _buyer(c):
    c.execute(
        "INSERT INTO pool_character (character_id, character_name, "
        "include_assets, include_job_slots, count_assets) "
        "VALUES (?, 'Buyer', 0, 0, 0)",
        (BUYER,),
    )
    c.commit()


def _buy_page(client, run_id, query=""):
    resp = client.get(f"/runs/{run_id}?view=buy{query}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _row_html(page, type_id):
    row = page[page.index(f'id="t{type_id}"'):]
    return row[:row.index("</tr>")]


def _cells(page, type_id):
    """(title, text) of every <td> of one row, the text stripped of tags."""
    out = []
    for attrs, body in re.findall(
        r"<td([^>]*)>(.*?)</td>", _row_html(page, type_id), re.S
    ):
        title = re.search(r'title="([^"]*)"', attrs)
        text = re.sub(r"<[^>]+>", " ", body)
        out.append((
            title.group(1) if title else "",
            " ".join(text.split()),
        ))
    return out


def _stat(page, label):
    """(class, tooltip, rendered value) of one header strip stat."""
    match = re.search(
        r'<span class="label">' + re.escape(label)
        + r'</span>.*?<span class="value ?([^"]*)"[^>]*title="([^"]*)"[^>]*>'
        + r'([^<]*)',
        page, re.S,
    )
    assert match, f"no {label!r} stat"
    return match.group(1).strip(), match.group(2), match.group(3)


def _multibuy(page):
    """{aria-label: pasted contents} for every Multibuy textarea."""
    return {
        label: body
        for label, body in re.findall(
            r'aria-label="([^"]*)">([^<]*)</textarea>', page
        )
    }


def _purchased_td(page, type_id):
    """The raw <td> of one row's Purchased cell (the fifth column)."""
    return re.findall(
        r"<td[^>]*>.*?</td>", _row_html(page, type_id), re.S
    )[4]


def _purchased(page, type_id):
    """(title, text) of one row's Purchased cell. Revision 8 (user ruling
    R1 2026-09-29: "for the purchased column, remove the tags"): the text
    is only `<units> @ <landed unit>` — no badge inside the cell — and
    each source is one ` · <units> <label> (<detail>)` clause of the
    title. Asserts the no-badge half here, for every caller."""
    assert "badge" not in _purchased_td(page, type_id)
    title, text = _cells(page, type_id)[4]
    return html.unescape(title), text


def _group_ore_free(page):
    """Revision 8's R2b (user ruling 2026-09-29: "the only place that
    matters is the multibuy all"): the page between the Input Materials
    header and the Purchases section — every group — with the two
    tooltips a group may name an ore in cut out: a covered raw's "N via
    compressed" tag title (the ores the plan picked) and the Purchased
    title's via-compressed clause (R1: the ores the pool bought that the
    plan did not pick, web._line_sources). A group names an ore nowhere
    else — no row, no cell text, no Multibuy line.

    The clause runs from ` · N via compressed (` to the `)` that ends the
    title or precedes the next ` · ` clause — its own text closes a
    nested "(user ruling …)" first."""
    groups = page[
        page.index('data-key="buy:inputs"'):page.index('data-key="buy:purchases"')
    ]
    groups = re.sub(
        r'<span class="badge accent" title="[^"]*">[\d,]+ via compressed</span>',
        "", groups,
    )
    return re.sub(
        r' · [\d,]+ via compressed \([^"]*?\)(?= · |")', "", groups,
    )


def _fake_buying(monkeypatch, calls, fail=False):
    """Patch magoo.buying's entry points: record each assign_purchases
    call and return a summary the route can flash. web holds the module
    itself and looks the functions up at call time, so patching the
    module's attributes reaches every route."""
    from magoo import buying

    def assign_purchases(conn, ref, settings, now=None):
        calls.append("assign")
        if fail:
            raise RuntimeError("matcher exploded")
        return {"matched": 2}

    monkeypatch.setattr(buying, "assign_purchases", assign_purchases)
    monkeypatch.setattr(
        buying, "summary_line",
        lambda summary: f"purchases: {summary['matched']} matched",
    )
    return buying


# --- navigation, titles and what left the Industry Jobs page ---------------


def test_the_subnav_reads_industry_jobs_buy_stockpile_profit(seeded_client):
    """R1 (revision 1): the URLs are unchanged, the LABELS say what each
    page is."""
    c = _state()
    run_id = _run(c)
    plan = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert ">Industry Jobs</a>" in plan and ">Stockpile</a>" in plan
    assert (
        plan.index(">Industry Jobs</a>")
        < plan.index(">Buy</a>")
        < plan.index(">Stockpile</a>")
        < plan.index(">Profit</a>")
    )
    page = _buy_page(seeded_client, run_id)
    assert 'class="active">Buy</a>' in page
    assert "Nothing to buy on this run" in page
    assert "No purchases matched to this run yet" in page
    c.close()


def test_purchasing_left_the_industry_jobs_page(seeded_client):
    c = _state()
    run_id = _run(c)
    _covered_pair(c, run_id)
    _item(c, run_id, TUNGSTEN, qty=7, price=3.0)
    plan = seeded_client.get(f"/runs/{run_id}").get_data(as_text=True)
    for gone in (
        "<textarea", "still to buy", "Compressed sourcing",
        "via compressed", "Reprocess <b>",
    ):
        assert gone not in plan, gone
    assert "Buy total" in plan and 'id="deficit"' in plan
    page = _buy_page(seeded_client, run_id)
    assert re.search(r"\d+ items? to buy", page) and "via compressed" in page
    assert page.count("Reprocess <b>5 Compressed Veldspar</b>") == 1
    assert "openDeficit" in page and 'id="deficit"' in page
    c.close()


# --- no controls any more (R1, R5) ----------------------------------------


def test_the_lock_routes_and_the_freight_toggle_are_gone(seeded_client):
    """R1: no click-to-lock; R5: every figure is landed, no toggle, no
    cookie. The page carries no form at all."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM)
    for path in ("choose", "unlock"):
        resp = seeded_client.post(
            f"/runs/{run_id}/purchases/{path}",
            data={"type_id": str(TRITANIUM), "choice": "ladder"},
        )
        assert resp.status_code == 404, path
    resp = seeded_client.get(f"/runs/{run_id}?view=buy&freight=off")
    assert resp.status_code == 200
    assert "Set-Cookie" not in resp.headers
    page = resp.get_data(as_text=True)
    assert "order prices" not in page and 'name="choice"' not in page
    body = page[page.index('<div class="totals">'):page.index('id="deficit"')]
    assert "<form" not in body
    assert store.list_purchases(c, run_id) == {}
    c.close()


# --- groups and the raws-only rule ------------------------------------------


def test_groups_follow_the_contract_order(seeded_client):
    """Minerals · Moon Materials · Gas · Planetary Industry · Fuel Blocks,
    then every other EVE group name alphabetically."""
    c = _state()
    run_id = _run(c)
    for type_id in (
        TUNGSTEN, NITROGEN_FUEL, COOLANT, FULLERITE, HYDROCARBONS, TRITANIUM
    ):
        _item(c, run_id, type_id)
    page = _buy_page(seeded_client, run_id)
    order = [
        page.index(f">{label} <span")
        for label in (
            "Minerals", "Moon Materials", "Gas", "Planetary Industry",
            "Fuel Blocks", "Composite",
        )
    ]
    assert order == sorted(order)
    # Multibuy All comes before the groups, Purchases after them.
    assert page.index("Multibuy All") < order[0]
    assert page.index(">Purchases <span") > order[-1]
    c.close()


def test_a_raw_group_row_is_the_raw_at_its_full_demand(seeded_client):
    """R4 stays: the row is the END-RESULT raw — Required and Remaining
    are its whole demand, direct + compressed-covered — and its Ladder is
    the plan's blended landed cost (effective_unit_cost). The ore has no
    row in the raw table."""
    c = _state()
    run_id = _run(c)
    figures = _covered_pair(c, run_id, direct=100, covered=400)
    page = _buy_page(seeded_client, run_id)
    cells = _cells(page, TRITANIUM)
    assert cells[1][1] == "500" and cells[3][1] == "500"
    assert "500 to buy: 100 direct + 400 from compressed ore" in cells[3][0]
    assert f"{figures['effective']:,.2f} ISK landed per unit" in cells[5][0]
    assert "blend of its direct buy and the compressed ore" in cells[5][0]
    # the ore is a row nowhere (R2b): its only line is in Multibuy All
    assert 'id="t62516"' not in page
    assert "Compressed Veldspar" not in _group_ore_free(page)
    c.close()


def test_a_fully_covered_raw_is_a_row(seeded_client):
    c = _state()
    run_id = _run(c)
    _covered_pair(c, run_id, direct=0, covered=400)
    page = _buy_page(seeded_client, run_id)
    assert _cells(page, TRITANIUM)[1][1] == "400"
    assert _cells(page, TRITANIUM)[3][1] == "400"
    assert "not a row of its own" in page
    c.close()


# --- Required · On Hand · Remaining · Purchased · Ladder · Δ (revision 6) ----


def test_the_table_reads_required_on_hand_remaining_purchased(seeded_client):
    """The user's ruling (2026-09-28): "Bought" became Purchased and the
    columns read Item · Required · On Hand · Remaining · Purchased, then
    Ladder · Δ (contract review A16: a rename and reorder, not a
    removal). Need, Bought and revision 5's New stock are gone from every
    visible label; so is the strip's Ladder stat."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM)
    page = _buy_page(seeded_client, run_id)
    table = page[page.index('<table class="buy">'):]
    heads = re.findall(r"<th[^>]*>([^<]*)</th>", table[:table.index("</tr>")])
    assert heads == [
        "Item", "Required", "On Hand", "Remaining", "Purchased", "Ladder", "Δ",
    ]
    labels = re.findall(r'<span class="label">([^<]*)</span>', page)
    assert labels[:4] == ["Required", "On Hand", "Remaining", "Purchased"]
    for gone in (">Need<", ">Bought<", "New stock", ">Ladder</span>",
                 "new stock", "bought 0"):
        assert gone not in page, gone
    c.close()


def test_a_wallet_buy_fills_purchased_and_delta_not_remaining(seeded_client):
    """40 of 100 Tritanium bought at 9.00 at an NPC station: landed at the
    RUN's hub rate (+1.00), so Purchased is 400 ISK, 10.00 a unit; the
    plan's ladder is 11.00 landed, so Δ = 400 − 11 × 40 = −40. Revision 6
    (R4): Remaining is the plan's buy — 100 — until the next ESI update
    re-plans from stock that holds the 40; the page subtracts nothing.
    The live run asks whether the rest is in transit (B2)."""
    c = _state()
    _buyer(c)
    _no_newer_update(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _buy(c, run_id, _line(TRITANIUM, 40, 9.0))
    page = _buy_page(seeded_client, run_id)
    item, required, on_hand, remaining, purchased, ladder, delta = _cells(
        page, TRITANIUM
    )
    assert required[1] == "100" and on_hand[1] == "0"
    assert purchased[1] == "40 @ 10"
    title, _text = _purchased(page, TRITANIUM)
    assert (
        "40 units purchased this cycle for 400 ISK landed, 10.00 ISK per unit"
    ) in title
    assert " · 40 Jita (wallet buys at Jita 4-4 — landed at the hub rate)" in title
    assert remaining[1] == "100"
    assert "100 to buy — 1,100 ISK at the ladder" in remaining[0]
    assert ladder[1] == "11" and "11.00 ISK landed per unit" in ladder[0]
    assert "1,100 ISK for the 100 it buys" in ladder[0]
    assert delta[1] == "−40"
    assert "40 ISK less than the plan's own landed fill" in delta[0]
    assert "can move differently" in delta[0]   # C14.5: not the Profit tab's Δ
    assert "in transit?" in item[1]
    # the same figures as costing's own cell, to the ISK
    row = c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,)
    ).fetchone()
    cell = costing.bought_cell(
        row, store.list_purchases(c, run_id)[TRITANIUM], HUB_RATE,
        STRUCTURE_RATE, TRIT_M3,
    )
    assert cell.bought_landed == pytest.approx(400.0)
    assert cell.delta == pytest.approx(-40.0)
    # the strip
    assert _stat(page, "Required")[1].startswith("1,100 ISK")
    assert _stat(page, "Purchased")[1].startswith("400 ISK landed")
    cls, title, _v = _stat(page, "Remaining")
    assert cls == "warn" and title.startswith("1,100 ISK — the 100 units")
    # the group's one list and Multibuy All's Jita block list the plan's 100
    blocks = _multibuy(page)
    assert blocks["Multibuy — Minerals"] == "Tritanium 100"
    assert blocks["Multibuy — All — Jita"] == "Tritanium 100"
    c.close()


def test_nothing_bought_reads_as_the_plan(seeded_client):
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    page = _buy_page(seeded_client, run_id)
    item, _r, _o, remaining, purchased, _l, delta = _cells(page, TRITANIUM)
    assert purchased[1] == "—" and remaining[1] == "100"
    assert delta[1] == "—" and delta[0] == "nothing purchased yet"
    assert "in transit?" not in item[1]
    assert _stat(page, "Purchased")[1].startswith("0 ISK")
    assert _stat(page, "Remaining")[1].startswith("1,100 ISK")
    c.close()


def test_a_purchase_takes_nothing_off_the_plans_venue_split(seeded_client):
    """Revision 6 (contract review A15): Multibuy All's lines are the
    plan's venue shares exactly — 700 Jita / 300 C-J6 before a purchase
    and after one; the purchase shows in Purchased, and the next ESI
    update's re-plan takes it off through stock. Revision 8 (R2): the
    group's own list is ONE line of the row's whole Remaining, 1,000 —
    not split by market."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=1000, price=9.4,
          buy_venue=store.BUY_VENUE_SPLIT, hub_buy_qty=700,
          structure_buy_qty=300, hub_fill_price=9.0,
          structure_fill_price=9.5)
    blocks = _multibuy(_buy_page(seeded_client, run_id))
    assert blocks["Multibuy — Minerals"] == "Tritanium 1000"
    assert blocks["Multibuy — All — Jita"] == "Tritanium 700"
    assert blocks["Multibuy — All — C-J6 structure market"] == "Tritanium 300"
    assert set(blocks) == {
        "Multibuy — Minerals", "Multibuy — All — Jita",
        "Multibuy — All — C-J6 structure market",
    }
    _buy(c, run_id, _line(TRITANIUM, 400, 9.0, venue=store.BUY_VENUE_STRUCTURE))
    page = _buy_page(seeded_client, run_id)
    assert _multibuy(page) == blocks
    title, text = _purchased(page, TRITANIUM)
    assert text == "400 @ 11"                                 # 9 + 2 freight
    assert " · 400 C-J6 (wallet buys at the C-J6 structure market" in title
    assert _cells(page, TRITANIUM)[3][1] == "1,000"
    c.close()


def test_the_page_carries_no_revision_5_netting():
    """Revision 6 removed revisions 3–5's purchase netting and revision
    5's New stock outright (contract review A15, A17)."""
    from magoo import web

    for gone in (
        "_reduce_shares", "_new_stock", "_new_stock_excluded", "_units_since",
    ):
        assert not hasattr(web, gone), gone


def test_unsourced_units_stay_out_of_every_block(seeded_client):
    """B35 / C14.1: units no stored sell order held are in neither
    Multibuy All block — they are in Remaining (the plan buys them) and
    its title says so. Revision 8 (R2): the group's own list is the
    row's Remaining, so it carries them."""
    c = _state()
    run_id = _run(c)
    _item(
        c, run_id, TRITANIUM, qty=1200, price=10.0, hub_buy_qty=1000,
        unfilled_qty=200, unfilled_price=12.0,
    )
    page = _buy_page(seeded_client, run_id)
    blocks = _multibuy(page)
    assert blocks["Multibuy — Minerals"] == "Tritanium 1200"
    assert blocks["Multibuy — All — Jita"] == "Tritanium 1000"
    remaining = _cells(page, TRITANIUM)[3]
    assert remaining[1] == "1,200"
    assert "200 of them had no market at plan time" in remaining[0]
    _buy(c, run_id, _line(TRITANIUM, 1100, 10.0))
    assert _multibuy(_buy_page(seeded_client, run_id)) == blocks
    c.close()


def test_a_contract_is_badged_with_its_title_and_k(seeded_client):
    """R7 end to end through the real matcher: a bought contract whose
    price is spread over what was received — Tritanium at k × 10.00 =
    9.00. The Purchased cell's title names the contract and its k (no
    badge since revision 8, R1); the Hydrocarbons
    the run does not hold are outside the plan: counted, listed, never in
    Purchased."""
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _contract(c, 7001, price=3600.0, title="Minerals lot", k=0.9, priced_at=NOW)
    _contract_item(c, 7001, 1, TRITANIUM, 100, unit_price=9.0)
    _contract_item(c, 7001, 2, HYDROCARBONS, 3, unit_price=900.0)
    _match(c)
    lines = store.list_purchases(c, run_id)
    assert [(l["quantity"], l["unit_price"], l["contract_k"]) for l in lines[TRITANIUM]] == [(100, 9.0, 0.9)]
    page = _buy_page(seeded_client, run_id)
    title, text = _purchased(page, TRITANIUM)
    assert text == "100 @ 10"
    assert " · 100 contract (item exchange “Minerals lot”, k = 0.9000" in title
    assert _cells(page, TRITANIUM)[6][1] == "−100"      # 1,000 − 11 × 100
    assert _stat(page, "Purchased")[1].startswith("1,000 ISK")
    assert ">3 units outside the plan</span>" in page
    assert "(Hydrocarbons)" in page
    # Purchases: one record, the contract's price, its k
    purchases = page[page.index(">Purchases <span"):]
    assert "1 from ESI" in purchases
    assert "3.60K" in purchases and ">0.900<" in purchases
    assert "3 Hydrocarbons" in purchases
    assert "outside the plan</span>" in purchases
    assert "Buyer" in purchases and "“Minerals lot”" in purchases
    c.close()


def test_unpriced_contract_items_are_badged(seeded_client):
    """A blueprint copy is never priced by reference (C10.3): the whole
    500 ISK lands on the 40 Tritanium at k = 500 / (40 × 10.00) = 1.25,
    and the copy is counted unpriced — in the strip and on the record."""
    c = _state()
    settings_ = _settings(c)
    _buyer(c)
    c.execute(
        "INSERT OR REPLACE INTO market_price (type_id, region_id, source, "
        "price, fetched_at, hub) VALUES (?, ?, 'sell', 10.0, ?, 1)",
        (TRITANIUM, settings_.price_region_id, NOW),
    )
    c.commit()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _contract(c, 7002, price=500.0)
    _contract_item(c, 7002, 1, TRITANIUM, 40)
    _contract_item(c, 7002, 2, TUNGSTEN, 1, raw_quantity=-2)
    _match(c)
    page = _buy_page(seeded_client, run_id)
    assert ">1 unpriced contract item</span>" in page
    assert ">1 unpriced</span>" in page       # on the Purchases record
    assert "#7002" in page                    # no title: named by its id
    assert ">copy</span>" in page
    title, text = _purchased(page, TRITANIUM)
    assert text == "40 @ 14"                                  # 12.50 + 1.00
    assert " · 40 contract (item exchange #7002" in title
    assert ">1.250<" in page
    c.close()


def test_a_built_rows_purchase_is_outside_the_plan(seeded_client):
    """§5's known limit (C14.4): realized costing prices a built row from
    its own inputs, so its purchase is recorded and shown but is not in
    Purchased — it is counted outside the plan. Revision 6 (A13, A14):
    Required is target stock + this cycle's draw (deficit 1,100 + nothing
    on hand), the plan builds 600 of it, so Remaining — the 500 it buys —
    is not Required − On Hand, and its title says why."""
    c = _state()
    run_id = _run(c)
    _item(
        c, run_id, NITROGEN_FUEL, qty=500, price=8000.0,
        blueprint_id=4312, recommended_build_qty=600,
        recommended_action="both", deficit_qty=1100, target_stock_qty=800,
    )
    _buy(c, run_id, _line(NITROGEN_FUEL, 100, 7000.0))
    page = _buy_page(seeded_client, run_id)
    assert ">built</span>" in page
    assert "it is not in the Purchased total" in page
    cells = _cells(page, NITROGEN_FUEL)
    assert cells[1] == (
        "target stock 800 + this cycle's draw 300 (the Stockpile's "
        "deficit basis)", "1,100",
    )
    assert cells[4][1].startswith("100 @")
    assert cells[3][1] == "500"
    assert (
        "Required − On Hand is 1,100: the plan builds 600 — Remaining is "
        "only the part it buys"
    ) in html.unescape(cells[3][0])
    assert _stat(page, "Purchased")[1].startswith("0 ISK")
    assert ">100 units outside the plan</span>" in page
    c.close()


def test_a_replanned_row_the_plan_no_longer_buys_keeps_its_purchase(
    seeded_client,
):
    """R2 + C14.4: purchases belong to the cycle, so a re-planned row with
    nothing left to buy still shows (Remaining 0) — realized costing
    prices it (§5 dropped the zero-buy skip) and the tab shows every line
    costing uses. Its ladder is the plan price at one unit. The re-plan
    saw the 50 on hand (Required 50 = On Hand), so nothing is in
    transit."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=0, price=10.0, hub_buy_qty=0)
    assert 'id="t34"' not in _buy_page(seeded_client, run_id)
    c.execute(
        "UPDATE index_run_item SET on_hand_qty = 50, target_stock_qty = 50 "
        "WHERE index_run_id = ?", (run_id,),
    )
    c.commit()
    _buy(c, run_id, _line(TRITANIUM, 50, 9.0))
    page = _buy_page(seeded_client, run_id)
    item, required, on_hand, remaining, bought, ladder, delta = _cells(
        page, TRITANIUM
    )
    assert (required[1], on_hand[1], remaining[1]) == ("50", "50", "0")
    assert "On Hand covers Required" in remaining[0]
    assert ">not on the buy list</span>" in page
    assert "in transit?" not in item[1]
    assert bought[1] == "50 @ 10" and ladder[1] == "11"
    assert " · 50 Jita (" in _purchased(page, TRITANIUM)[0]
    assert delta[1] == "−50"
    assert _stat(page, "Purchased")[1].startswith("500 ISK")
    assert "outside the plan" not in page
    c.close()


# --- the compressed ore under the ore rule (R8, C14.3) --------------------


def test_every_plan_ore_is_listed_at_its_plan_quantity(seeded_client):
    """Revision 6 (contract review A15): the plan's ore is ALWAYS a line
    of Multibuy All at its plan quantity — buying the raw's direct share,
    or the ore itself, takes nothing off (the next ESI update's re-plan
    does, from stock); the reprocess checklist stays with it. Revision 8
    (R2): the group's own list names the raw itself at its whole
    Remaining, direct + covered (100 + 400), and no ore line."""
    c = _state()
    run_id = _run(c)
    figures = _covered_pair(c, run_id, direct=100, covered=400)
    page = _buy_page(seeded_client, run_id)
    blocks = _multibuy(page)
    assert blocks == {
        "Multibuy — Minerals": "Tritanium 500",
        "Multibuy — All — Jita": "Tritanium 100\nCompressed Veldspar 5",
    }
    assert page.count("Reprocess <b>5 Compressed Veldspar</b>") == 1   # All only
    _buy(
        c, run_id, _line(TRITANIUM, 100, 10.0),
        _line(COMPRESSED_VELDSPAR, 5, 900.0, esi_id=5002),
    )
    page = _buy_page(seeded_client, run_id)
    assert _multibuy(page) == blocks
    assert _cells(page, TRITANIUM)[3][1] == "500"
    assert page.count("Reprocess <b>5 Compressed Veldspar</b>") == 1
    assert f"{figures['plan_landed']:,.0f} ISK" in _stat(page, "Required")[1]
    assert _stat(page, "Remaining")[1].startswith(f"{figures['plan_landed']:,.0f} ISK")
    c.close()


def test_no_group_renders_a_compressed_ore_even_one_with_purchases(
    seeded_client,
):
    """Revision 8's R2b (user ruling 2026-09-29: "the only place that
    matters is the multibuy all"): the per-group ore table is gone — no
    ore row, block or badge row in any group, even for a plan-chosen ore
    with purchases. The purchase is still listed in the Purchases section
    and still in the strip's and the group's Purchased ISK, exactly as
    before the removal — hand-derived: the raw's 100 at 10.00 + 1.00 hub
    freight = 1,100, the ore's 5 at 900.00 + 100 ISK/m³ × 0.001 m³ = 0.10
    freight = 4,500.50; 5,600.50 in all (5,600 as whole ISK). The
    covered raw's "400 via
    compressed" title still names the ore; nothing else in a group does."""
    c = _state()
    _buyer(c)
    _no_newer_update(c)
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    _tx_row(c, 5001, TRITANIUM, 100, 10.0)
    _tx_row(c, 5002, COMPRESSED_VELDSPAR, 5, 900.0)
    _match(c)
    assert set(store.list_purchases(c, run_id)) == {
        TRITANIUM, COMPRESSED_VELDSPAR,
    }
    page = _buy_page(seeded_client, run_id)
    assert 'id="t62516"' not in page
    groups = _group_ore_free(page)
    assert "Compressed Veldspar" not in groups
    assert "Compressed ore" not in groups and ">compressed</span>" not in groups
    # ... and the tag's title is where the group names it
    assert "From Compressed Veldspar — each is a line in Multibuy All above" in (
        html.unescape(_row_html(page, TRITANIUM))
    )
    # the removal changes no total
    bought = 100 * 11.0 + 5 * (900.0 + HUB_RATE * ORE_M3)
    assert bought == pytest.approx(5600.5)
    assert _stat(page, "Purchased")[1].startswith(f"{bought:,.0f} ISK landed")
    title, _shown = _group_header(page, "Minerals")["purchased"]
    assert title.startswith(f"{bought:,.0f} ISK of purchases matched to this group")
    # the raw's own cell is its own 100 only
    assert _purchased(page, TRITANIUM)[1] == "100 @ 11"
    # the Purchases section still lists the ore purchase
    purchases = page[page.index(">Purchases <span"):]
    assert "2 from ESI" in purchases
    assert "5 Compressed Veldspar" in purchases
    # Multibuy All keeps it, with its reprocess step
    assert "Compressed Veldspar 5" in _multibuy(page)["Multibuy — All — Jita"]
    assert page.count("Reprocess <b>5 Compressed Veldspar</b>") == 1
    c.close()


def _two_group_ore(c, run_id):
    """One ore reprocessing into a mineral AND a moon material; the
    mineral side is far the larger, so the ore files under Minerals."""
    ore_qty, ore_price = 5, 1000.0
    ore_landed = ore_qty * ore_price + HUB_RATE * ORE_M3 * ore_qty
    trit_share, hydro_share = 0.9, 0.1
    _item(
        c, run_id, COMPRESSED_VELDSPAR, qty=ore_qty, price=ore_price,
        compressed_outputs=json.dumps(
            [[TRITANIUM, 6000, 6000], [HYDROCARBONS, 65, 65]]
        ),
        compressed_alloc=json.dumps(
            {str(TRITANIUM): trit_share, str(HYDROCARBONS): hydro_share}
        ),
        compressed_landed_isk=ore_landed, compressed_tax_isk=0.0,
    )
    for type_id, covered, share in (
        (TRITANIUM, 6000, trit_share), (HYDROCARBONS, 65, hydro_share),
    ):
        direct_landed = 100 * 10.0 + HUB_RATE * TRIT_M3 * 100
        _item(
            c, run_id, type_id, qty=100, price=10.0, hub_buy_qty=100,
            compressed_covered_qty=covered,
            direct_landed_isk=direct_landed,
            effective_unit_cost=(
                (direct_landed + share * ore_landed) / (100 + covered)
            ),
        )
    return ore_qty


def test_an_ore_covering_two_groups_is_pasted_and_named_once(seeded_client):
    """B36: filed in ONE group, so Multibuy All pastes it once. Revision 8
    (R2, R2b): no group's list carries an ore line — each names its raw at
    direct + covered — and both covered raws name the ore only in their
    tag's title, pointing at Multibuy All. A purchase takes nothing off
    (revision 6)."""
    c = _state()
    run_id = _run(c)
    ore_qty = _two_group_ore(c, run_id)
    page = _buy_page(seeded_client, run_id)
    blocks = _multibuy(page)
    assert blocks["Multibuy — Minerals"] == "Tritanium 6100"
    assert blocks["Multibuy — Moon Materials"] == "Hydrocarbons 165"
    all_jita = blocks["Multibuy — All — Jita"]
    assert all_jita.count("Compressed Veldspar") == 1
    assert f"Compressed Veldspar {ore_qty}" in all_jita
    for type_id in (TRITANIUM, HYDROCARBONS):
        assert "From Compressed Veldspar — each is a line in Multibuy All above" in (
            html.unescape(_row_html(page, type_id))
        )
    assert "under Minerals" not in page
    assert "Compressed Veldspar" not in _group_ore_free(page)
    _buy(c, run_id, _line(TRITANIUM, 6100, 9.0))
    assert _multibuy(_buy_page(seeded_client, run_id)) == blocks
    c.close()


def test_the_ore_line_carries_the_compressed_badges_tooltip(seeded_client):
    c = _state()
    run_id = _run(c)
    _item(
        c, run_id, COMPRESSED_VELDSPAR, qty=500, price=21.0,
        compressed_outputs=json.dumps([[TRITANIUM, 1500, 1200]]),
        compressed_wanted_qty=600,
    )
    _item(
        c, run_id, TRITANIUM, qty=10, price=10.0,
        compressed_covered_qty=1200, effective_unit_cost=7.0,
    )
    page = _buy_page(seeded_client, run_id)
    assert ">compressed</span>" in page
    assert "covers 1,200 Tritanium; leftover 300 Tritanium" in page
    assert (
        "the plan wanted 600 but the market could fill only 500 in whole "
        "batches"
    ) in page
    assert "~1,200 Tritanium" in page and "leftover +300 Tritanium" in page
    assert "90.63% for ore" in page and "95% for gas" in page
    assert "4% reprocessing tax on every refined-ore output" in page
    c.close()


def test_an_ore_the_plan_allocated_nothing_to_is_named_in_the_strip(
    seeded_client,
):
    """B14: the degenerate pick's landed ISK sits in no row or total."""
    c = _state()
    run_id = _run(c)
    figures = _covered_pair(c, run_id, direct=100, covered=400, alloc=0.0)
    page = _buy_page(seeded_client, run_id)
    assert f"{figures['ore_landed']:,.0f} ISK of compressed buys" in page
    c.close()
    c = _state()
    run_id = _run(c, run_number=2)
    _covered_pair(c, run_id, direct=100, covered=400)
    assert "unallocated" not in _buy_page(seeded_client, run_id)
    c.close()


# --- the strip --------------------------------------------------------------


def test_the_header_carries_the_plan_badges(seeded_client):
    c = _state()
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    _item(c, run_id, HYDROCARBONS, qty=12, price=None, hub_fill_price=None)
    _item(
        c, run_id, FULLERITE, qty=50, price=30.0,
        buy_venue=store.BUY_VENUE_SPLIT, hub_buy_qty=30,
        structure_buy_qty=20, hub_fill_price=29.0,
        structure_fill_price=31.0,
    )
    page = _buy_page(seeded_client, run_id)
    for label in ("Required", "On Hand", "Remaining", "Purchased"):
        assert f">{label}</span>" in page
    for gone in ("Locked", "Saving vs ladder", "Rows locked", "Units unlocked",
                 "Freight", "Ladder", "Bought"):
        assert f">{gone}</span>" not in page
    assert ">1 unpriced</span>" in page
    assert ">1 split</span>" in page
    assert ">1 via compressed</span>" in page
    assert "outside the plan" not in page
    # 500 + 12 + 50 units to buy
    assert "the 562 units the plan buys" in _stat(page, "Remaining")[1]
    c.close()


def test_a_completed_run_says_a_late_purchase_reprices_later_runs(
    seeded_client,
):
    c = _state()
    run_id = _run(c, status="complete")
    _item(c, run_id, TRITANIUM)
    page = _buy_page(seeded_client, run_id)
    assert "in cost history" in page and "reprices" in page
    c.close()


def test_a_superseded_run_says_which_run_its_purchases_went_to(seeded_client):
    """C4: a superseded plan collects nothing; its tab points at the run
    that does — or at the next plan when the newest run is executed."""
    c = _state()
    old = _run(c)
    _item(c, old, TRITANIUM)
    new = _run(c, run_number=2)
    page = _buy_page(seeded_client, old)
    assert "this plan collects no purchases" in page
    assert f"/runs/{new}?view=buy" in page and "run 2</a>, the newest plan" in page
    assert "a superseded plan collects no purchases" in page
    c.execute("UPDATE index_run SET status = 'complete' WHERE index_run_id = ?", (new,))
    c.commit()
    page = _buy_page(seeded_client, old)
    assert "the next run you plan" in page
    assert "this plan collects no purchases" not in _buy_page(seeded_client, new)
    c.close()


def test_a_bought_row_keeps_its_why_this_quantity_dialog(seeded_client):
    c = _state()
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    page = _buy_page(seeded_client, run_id)
    assert 'data-covered="400"' in page and 'data-buy="100"' in page
    assert 'id="deficit"' in page and "openDeficit" in page
    c.close()


def test_a_bought_structure_component_is_a_row_under_its_own_group(
    seeded_client,
):
    c = _state()
    run_id = _run(c)
    parts = 21947   # Structure Construction Parts (group 536)
    _item(c, run_id, parts, qty=12, price=1000.0)
    page = _buy_page(seeded_client, run_id)
    assert 'id="t21947"' in page and "Structure Components" in page
    c.close()


# --- the Purchases section ------------------------------------------------


def test_the_purchases_section_lists_what_esi_delivered(seeded_client):
    """Wallet buys matched by the real matcher, newest first, venue by
    where they were bought: Jita 4-4 is Jita, the configured structure
    market (the C-J6 Keepstar) the structure market (R6 as revised by
    the user's ruling of 2026-09-28 — any other location is elsewhere)."""
    c = _state()
    _buyer(c)
    c.execute(
        "INSERT INTO esi_corp (corporation_id, corporation_name) VALUES (?, 'Buy Corp')",
        (BUYER_CORP,),
    )
    c.commit()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _tx_row(c, 5001, TRITANIUM, 40, 9.0, date="2026-09-21T10:00:00Z")
    _tx_row(c, 5002, TRITANIUM, 10, 9.5, date="2026-09-22T08:30:00Z",
            location=STRUCTURE_LOCATION, owner=("corporation", BUYER_CORP))
    _match(c)
    page = _buy_page(seeded_client, run_id)
    title, text = _purchased(page, TRITANIUM)
    assert text == "50 @ 10"                              # (400 + 115) / 50
    assert " · 40 Jita (" in title and " · 10 C-J6 (" in title
    section = page[page.index(">Purchases <span"):]
    assert "2 from ESI" in section
    rows = re.findall(r"<tr>(.*?)</tr>", section, re.S)
    texts = [" ".join(re.sub(r"<[^>]+>", " ", r).split()) for r in rows]
    assert texts[0] == "Date Owner Venue Items Price k"
    texts = texts[1:]
    # newest first
    assert texts[0].startswith("2026-09-22 08:30 Buy Corp (corp) C-J6 10 Tritanium")
    assert texts[1].startswith("2026-09-21 10:00 Buyer Jita 40 Tritanium")
    assert texts[1].endswith("360 —")     # 40 × 9.00, no k
    c.close()


def test_a_purchase_that_is_not_costed_is_listed_with_its_reason(
    seeded_client,
):
    """C9: a buy from one of our own characters is a transfer — listed
    "internal — not costed", no line, not in Purchased."""
    c = _state()
    _buyer(c)
    c.execute(
        "INSERT INTO pool_character (character_id, character_name, "
        "include_assets, include_job_slots, count_assets) "
        "VALUES (90000002, 'Alt', 0, 0, 0)"
    )
    c.commit()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _tx_row(c, 5003, TRITANIUM, 40, 9.0, client_id=90000002)
    _match(c)
    assert store.list_purchases(c, run_id) == {}
    page = _buy_page(seeded_client, run_id)
    assert "1 from ESI, 1 not costed" in page
    assert ">internal — not costed</span>" in page
    assert _cells(page, TRITANIUM)[4][1] == "—"
    assert _stat(page, "Purchased")[1].startswith("0 ISK")
    c.close()


def test_when_the_reader_fails_the_section_lists_the_runs_lines(
    seeded_client, monkeypatch,
):
    """The page never depends on the matcher's reader succeeding: when
    buying.run_purchase_records raises, Purchases is rebuilt from the
    run's derived lines."""
    from magoo import buying

    def broken_reader(conn, index_run_id):
        raise RuntimeError("reader exploded")

    monkeypatch.setattr(buying, "run_purchase_records", broken_reader)
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _buy(c, run_id, _line(TRITANIUM, 40, 9.0))
    page = _buy_page(seeded_client, run_id)
    section = page[page.index(">Purchases <span"):]
    assert "1 from ESI" in section and "40 Tritanium" in section
    assert "Buyer" in section
    c.close()


# --- the ESI page: Count buys (R4) ------------------------------------------


def test_the_esi_page_has_a_count_buys_marker_per_owner(seeded_client):
    c = _state()
    _buyer(c)
    c.execute(
        "INSERT INTO esi_corp (corporation_id, corporation_name) VALUES (?, 'Buy Corp')",
        (BUYER_CORP,),
    )
    c.commit()
    html = seeded_client.get("/characters").get_data(as_text=True)
    assert html.count("<th>Count buys</th>") == 2
    assert html.count("<th>Count sales</th>") == 2
    assert f"/characters/{BUYER}/toggle/count_buys" in html
    assert f"/corps/{BUYER_CORP}/toggle/count_buys" in html
    assert "Count buys (v1.29)" in html
    assert seeded_client.post(f"/characters/{BUYER}/toggle/count_buys").status_code == 302
    assert seeded_client.post(f"/corps/{BUYER_CORP}/toggle/count_buys").status_code == 302
    s = _state()
    assert s.execute(
        "SELECT count_buys FROM pool_character WHERE character_id = ?", (BUYER,)
    ).fetchone()[0] == 0
    assert s.execute(
        "SELECT count_buys, count_sales FROM esi_corp WHERE corporation_id = ?",
        (BUYER_CORP,),
    ).fetchone()[:] == (0, 1)
    assert store.buys_enabled_owners(s) == set()
    s.close()
    assert seeded_client.post(f"/characters/{BUYER}/toggle/bogus").status_code == 400
    c.close()


def test_a_count_buys_toggle_rematches_the_runs_purchase_lines(seeded_client):
    """Count buys is honoured at matching time (C7), so the toggle runs
    the matcher: off takes the owner's buys off the run (and its realized
    cost), on puts them back — no ESI update needed in between."""
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _tx_row(c, 5101, TRITANIUM, 40, 9.0, date="2026-09-21T10:00:00Z")
    _match(c)

    def lines():
        s = _state()
        out = [
            (line["esi_id"], line["quantity"])
            for rows in store.list_purchases(s, run_id).values()
            for line in rows
        ]
        s.close()
        return out

    assert lines() == [(5101, 40)]
    assert seeded_client.post(f"/characters/{BUYER}/toggle/count_buys").status_code == 302
    assert lines() == []
    assert seeded_client.post(f"/characters/{BUYER}/toggle/count_buys").status_code == 302
    assert lines() == [(5101, 40)]
    # A toggle that is not Count buys leaves matching alone.
    c.execute("DELETE FROM run_purchase")
    c.commit()
    assert seeded_client.post(f"/characters/{BUYER}/toggle/count_sales").status_code == 302
    assert lines() == []
    c.close()


def test_a_corp_count_buys_toggle_rematches_too(seeded_client, monkeypatch):
    calls = []
    _fake_buying(monkeypatch, calls)
    c = _state()
    c.execute(
        "INSERT INTO esi_corp (corporation_id, corporation_name) VALUES (?, 'Buy Corp')",
        (BUYER_CORP,),
    )
    c.commit()
    seeded_client.post(f"/corps/{BUYER_CORP}/toggle/count_sales")
    assert calls == []
    seeded_client.post(f"/corps/{BUYER_CORP}/toggle/count_buys")
    assert calls == ["assign"]
    _fake_buying(monkeypatch, calls, fail=True)
    html = seeded_client.post(
        f"/corps/{BUYER_CORP}/toggle/count_buys", follow_redirects=True
    ).get_data(as_text=True)
    assert "purchase matching failed (matcher exploded)" in html
    assert c.execute(
        "SELECT count_buys FROM esi_corp WHERE corporation_id = ?", (BUYER_CORP,)
    ).fetchone()[0] == 1                     # the toggle itself committed
    c.close()


def test_deleting_a_character_clears_it_from_bought_contracts(seeded_client):
    c = _state()
    _buyer(c)
    _contract(c, 7003, price=100.0)
    c.execute("UPDATE buy_contract SET via_character_id = ?", (BUYER,))
    c.commit()
    assert seeded_client.post(f"/characters/{BUYER}/delete").status_code == 302
    s = _state()
    assert s.execute(
        "SELECT via_character_id FROM buy_contract WHERE contract_id = 7003"
    ).fetchone()[0] is None
    s.close()
    c.close()


def test_the_ledger_help_says_purchases_are_not_counted_there(seeded_client):
    html = seeded_client.get("/ledger").get_data(as_text=True)
    assert "also with Count buys on, for the Buy tab" in html


# --- where purchase matching runs (C1) ------------------------------------


def _refresh_stubs(monkeypatch, calls):
    monkeypatch.setattr(
        market, "refresh_prices",
        lambda *a, **k: (calls.append("prices"), (0, 0, 0))[1],
    )
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)

    def fake_state(conn, ref):
        calls.append("snapshot")
        store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0, {})
        return {"on_hand": {}, "in_progress": {}, "active_jobs": {},
                "job_ends": {}, "character_isk": 0.0, "corporation_isk": 0.0}

    def fake_pull(conn, ref):
        calls.append("sales")
        return ledger.PullSummary(new_sales=0, contracts_finished=0, open_orders=0)

    monkeypatch.setattr(esi, "refresh_state", fake_state)
    monkeypatch.setattr(ledger, "pull_sales", fake_pull)


def test_the_esi_update_matches_purchases_after_the_prices(
    seeded_client, monkeypatch,
):
    calls = []
    _refresh_stubs(monkeypatch, calls)
    _fake_buying(monkeypatch, calls)
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    assert resp.status_code == 200
    assert calls == ["snapshot", "sales", "prices", "assign"]
    assert "purchases: 2 matched" in resp.get_data(as_text=True)


def test_matching_runs_even_when_esi_is_down(seeded_client, monkeypatch):
    """It reads local rows only (C1)."""
    calls = []
    _refresh_stubs(monkeypatch, calls)
    _fake_buying(monkeypatch, calls)
    monkeypatch.setattr(
        esi, "refresh_state",
        lambda conn, ref: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    html = seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True)
    assert calls == ["assign"]
    assert "sales pull skipped" in html and "purchases: 2 matched" in html


def test_a_matching_failure_never_costs_the_refresh(seeded_client, monkeypatch):
    calls = []
    _refresh_stubs(monkeypatch, calls)
    _fake_buying(monkeypatch, calls, fail=True)
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert calls == ["snapshot", "sales", "prices", "assign"]
    assert "ESI refreshed" in html
    assert "purchase matching failed (matcher exploded)" in html


def test_web_imports_the_matcher_with_its_other_modules():
    """Integration: the matcher is a hard dependency of web, not an
    optional lazy import — a broken buying module fails at start-up
    instead of silently skipping every purchase-matching step."""
    import magoo.buying
    import magoo.web

    assert magoo.web.buying is magoo.buying


def test_executing_and_reopening_a_run_rematch(seeded_client, monkeypatch):
    """Both move a buying-cycle bound (R2), so both re-file (C1)."""
    calls = []
    _fake_buying(monkeypatch, calls)
    c = _state()
    run_id = _run(c)
    seeded_client.post(f"/runs/{run_id}/complete")
    assert calls == ["assign"]
    seeded_client.post(f"/runs/{run_id}/reopen")
    assert calls == ["assign", "assign"]
    _fake_buying(monkeypatch, calls, fail=True)
    html = seeded_client.post(
        f"/runs/{run_id}/complete", follow_redirects=True
    ).get_data(as_text=True)
    assert "run marked executed" in html and "purchase matching failed" in html
    assert c.execute(
        "SELECT status FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] == "complete"
    c.close()


def test_a_buy_tab_matches_on_first_view_only_when_there_is_anything(
    seeded_client, monkeypatch,
):
    """§4: a run planned after the last ESI update has no derived lines;
    its first view matches — but only when ESI delivered a purchase at
    all, never for a superseded plan, and never again once it has lines."""
    calls = []
    _fake_buying(monkeypatch, calls)
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM)
    _buy_page(seeded_client, run_id)
    assert calls == []                       # nothing stored: no write on a view
    c.execute(
        "INSERT INTO buy_transaction (transaction_id, owner_kind, owner_id, "
        "source_feed, type_id, quantity, unit_price, date, location_id, "
        "fetched_at) VALUES (1, 'character', ?, 'character', ?, 10, 9.0, ?, ?, ?)",
        (BUYER, TRITANIUM, NOW, NPC_STATION, NOW),
    )
    c.commit()
    _buy_page(seeded_client, run_id)
    assert calls == ["assign"]
    _buy(c, run_id, _line(TRITANIUM, 10, 9.0))
    _buy_page(seeded_client, run_id)
    assert calls == ["assign"]               # it has lines now
    newer = _run(c, run_number=2)
    _buy_page(seeded_client, run_id)         # superseded now
    assert calls == ["assign"]
    _buy_page(seeded_client, newer)
    assert calls == ["assign", "assign"]
    c.close()


def test_the_price_refresh_stores_adjusted_prices_for_contract_items(
    seeded_client, monkeypatch,
):
    """C10.1: R7 falls back to CCP's adjusted price, which used to be
    stored for the demand set only — a contract item outside the plan
    would have been unpriced and the whole contract's ISK would land on
    the plan's items."""
    stored = []
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (0, 0, 0))
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {TUNGSTEN: 42.0})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)
    real = market.store_adjusted_prices

    def spy(conn, type_ids, adjusted):
        stored.append(set(type_ids))
        return real(conn, type_ids, adjusted)

    monkeypatch.setattr(market, "store_adjusted_prices", spy)
    c = _state()
    _contract(c, 7004, price=100.0)
    c.execute(
        "INSERT INTO buy_contract_item (contract_id, record_id, type_id, "
        "quantity, raw_quantity, is_included) VALUES (7004, 1, ?, 5, 5, 1)",
        (TUNGSTEN,),
    )
    c.commit()
    assert seeded_client.post("/prices/refresh").status_code == 302
    assert stored and TUNGSTEN in stored[-1]
    assert market.cached_adjusted_prices(_state(), [TUNGSTEN]) == {TUNGSTEN: 42.0}
    c.close()


# --- the Profit tab's badges for a bought line ------------------------------


def _cost_stub(lines):
    return SimpleNamespace(
        pipeline_id=1, lines=lines, total=100.0, hulls_per_cycle=1,
        hulls_planned=1, spin_up=False, missing_prices=0, region_priced=0,
        structure_material_share_pct=None, structure_material_cost=0.0,
        subtotal=lambda kind: 0.0,
    )


def _render_profit(lines):
    app = template_app()
    card = {
        "name": "Hulk", "cost": _cost_stub(lines), "price": 200.0,
        "net": 190.0, "capital": False, "margin": 90.0,
    }
    with app.test_request_context("/runs/1?view=profit"):
        return render_template(
            "run_profit.html",
            run={"index_run_id": 1, "run_number": 1, "status": "complete",
                 "completed_at": "2026-09-20T00:00:00Z"},
            superseded=False, cards=[card],
            totals=costing.cycle_totals([card]),
            completed_history=1, broker_rate=0.02, sales_tax=0.03,
            settings=store.Settings(
                stockpile_buffer=1.0, max_run_duration_hours=168.0,
                composite_reaction_extra_runs=0, price_region_id=10000002,
                price_source="sell",
            ),
        )


def test_the_profit_breakdown_badges_a_bought_line(seeded_client):
    """C14.7: the copy says bought, from ESI — nothing is locked any more."""
    line = costing.CostLine(
        type_id=TRITANIUM, name="Tritanium", kind="material", depth=1,
        qty_per_hull=100.0, unit_cost=8.0, lag_runs=1, clamped=False,
        venue=store.BUY_VENUE_SPLIT,
        hub_fraction=0.5, structure_fraction=0.3, delivered_fraction=0.2,
        delivered_fill_price=9.0, locked=True,
    )
    html = _render_profit([line])
    assert ">bought</span>" in html and ">locked</span>" not in html
    assert "priced from the ESI purchases matched to the run it lags to" in html
    assert "recorded on the Buy tab" not in html
    assert "50% of the units from Jita, 30% from C-J6, 20% delivered" in html
    html = _render_profit([
        costing.CostLine(
            type_id=TRITANIUM, name="Tritanium", kind="material", depth=1,
            qty_per_hull=100.0, unit_cost=8.0, lag_runs=1, clamped=False,
            venue=store.BUY_VENUE_DELIVERED, hub_fraction=0.0,
            structure_fraction=0.0, delivered_fraction=1.0,
            delivered_fill_price=8.0, locked=True, approximate=True,
        )
    ])
    assert ">delivered</span>" in html and ">approx.</span>" in html


def test_the_profit_breakdown_does_not_claim_plan_time_cheapness_when_bought(
    seeded_client,
):
    line = costing.CostLine(
        type_id=TRITANIUM, name="Tritanium", kind="material", depth=1,
        qty_per_hull=100.0, unit_cost=7.0, lag_runs=1, clamped=False,
        venue=store.BUY_VENUE_STRUCTURE, hub_fraction=0.0,
        structure_fraction=1.0, delivered_fraction=0.0, locked=True,
    )
    html = _render_profit([line])
    assert "every unit bought at the structure market, from the ESI purchases" in html
    assert "cheaper landed than Jita at that run's plan time" not in html
    html = _render_profit([dataclasses.replace(line, locked=False)])
    assert "cheaper landed than Jita at that run's plan time" in html


# --- review 2026-09-28 -------------------------------------------------------


def test_a_purchase_of_a_type_the_game_data_does_not_know_renders(seeded_client):
    """P0: ESI purchases are stored whatever their type, and CCP adds items
    faster than the SDE import catches up. One such buy used to turn every
    view of the run's Buy tab into a 500 (KeyError in type_info) until a
    re-import; it now reads "type N" in the outside-the-plan count and in
    Purchases, like the Ledger's own names."""
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM)
    _tx_row(c, 6001, 987654321, 5, 100.0)
    _match(c)
    assert c.execute(
        "SELECT COUNT(*) FROM run_purchase WHERE type_id = 987654321"
    ).fetchone()[0] == 1
    page = _buy_page(seeded_client, run_id)
    assert ">5 units outside the plan</span>" in page
    assert "type 987654321" in page
    # The fallback listing (matcher's reader down) resolves names the same way.
    from magoo import web

    assert web._type_name(SimpleNamespace(type_info=lambda t: (_ for _ in ()).throw(
        KeyError(t))), 42) == "type 42"
    c.close()


def test_a_superseded_run_reads_none_of_its_lines(seeded_client):
    """P1: a newer run leaves the old run's derived lines in place until a
    pass clears them; its tab says it collects no purchases, so it must
    not show Purchased from them in the meantime — nor ask whether they
    are in transit."""
    c = _state()
    old = _run(c)
    _item(c, old, TRITANIUM)
    _buy(c, old, _line(TRITANIUM, 40, 9.0))
    assert _stat(_buy_page(seeded_client, old), "Purchased")[2].strip() != "0"
    _run(c, run_number=2)                      # superseded, no pass yet
    page = _buy_page(seeded_client, old)
    assert "this plan collects no purchases" in page
    assert _stat(page, "Purchased")[1].startswith("0 ISK")
    assert _cells(page, TRITANIUM)[3][1] == "100"     # Remaining = the plan
    assert "40 @" not in _row_html(page, TRITANIUM)
    assert "in transit?" not in _row_html(page, TRITANIUM)
    c.close()


def test_planning_replans_the_open_run_in_place_and_keeps_its_purchase(
    seeded_client,
):
    """Revision 6 (R1, contract review A18): while a run is open the Plan
    button re-plans it IN PLACE — the same run id, the purchase stays on
    it (run_plan still runs the matcher). Only after Mark executed does
    it create a new run, and the next cycle's purchase lands there."""
    from datetime import datetime, timedelta, timezone

    c = _state()
    _buyer(c)
    assert seeded_client.post("/run").status_code == 302
    first = c.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    _tx_row(c, 6101, TRITANIUM, 1, 1.0, date=soon)
    _match(c)
    placed = "SELECT DISTINCT index_run_id FROM run_purchase WHERE esi_kind IS NOT NULL"
    assert [r[0] for r in c.execute(placed)] == [first]
    resp = seeded_client.post("/run", follow_redirects=True)
    assert "index run 1 re-planned in place" in resp.get_data(as_text=True)
    assert c.execute("SELECT COUNT(*) FROM index_run").fetchone()[0] == 1
    assert [r[0] for r in c.execute(placed)] == [first]
    assert seeded_client.post(f"/runs/{first}/complete").status_code == 302
    assert seeded_client.post("/run").status_code == 302
    second = c.execute("SELECT MAX(index_run_id) FROM index_run").fetchone()[0]
    assert second != first
    assert c.execute(
        "SELECT run_number FROM index_run WHERE index_run_id = ?", (second,)
    ).fetchone()[0] == 2
    c.close()


def test_planning_flashes_a_matching_failure(seeded_client, monkeypatch):
    calls = []
    _fake_buying(monkeypatch, calls, fail=True)
    resp = seeded_client.post("/run", follow_redirects=True)
    assert calls == ["assign"]
    assert "purchase matching failed (matcher exploded)" in resp.get_data(as_text=True)


def test_discarding_a_run_and_removing_a_character_rematch(seeded_client, monkeypatch):
    """P2: both change what the matcher would produce (the previous run
    becomes the newest again; a character's buys stop counting), so both
    re-file the purchases at once, like a Count buys toggle."""
    calls = []
    _fake_buying(monkeypatch, calls)
    c = _state()
    _buyer(c)
    run_id = _run(c)
    assert seeded_client.post(f"/runs/{run_id}/delete").status_code == 302
    assert calls == ["assign"]
    assert seeded_client.post(f"/characters/{BUYER}/delete").status_code == 302
    assert calls == ["assign", "assign"]
    c.close()


def test_removing_a_character_drops_its_lines_at_once(seeded_client):
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM)
    _tx_row(c, 6201, TRITANIUM, 40, 9.0)
    _match(c)
    assert store.list_purchases(c, run_id)
    assert seeded_client.post(f"/characters/{BUYER}/delete").status_code == 302
    assert store.list_purchases(c, run_id) == {}
    c.close()


def test_the_view_trigger_does_not_rerun_a_pass_on_unchanged_inputs(
    seeded_client, monkeypatch,
):
    """P2: a run whose window holds no costed purchase keeps no derived
    lines; its Buy tab must not open a write pass on every view."""
    calls = []
    _fake_buying(monkeypatch, calls)
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM)
    _tx_row(c, 6301, TRITANIUM, 10, 9.0)
    _buy_page(seeded_client, run_id)
    assert calls == ["assign"]
    _buy_page(seeded_client, run_id)
    _buy_page(seeded_client, run_id)
    assert calls == ["assign"]                 # nothing changed since that pass
    _tx_row(c, 6302, TRITANIUM, 10, 9.0)
    _buy_page(seeded_client, run_id)
    assert calls == ["assign", "assign"]       # a new purchase: match again
    c.close()


def test_a_buildable_row_the_plan_buys_outright_is_outside_the_plan(seeded_client):
    """Pinned (review 2026-09-28): "outside the plan" is any BUILDABLE row
    (blueprint_id set) — a build-vs-buy 'buy' verdict with no build at all
    included — because hull_cost prices every such row from its own
    inputs. The Multibuy lists the plan's buy (revision 6: nothing
    taken off)."""
    c = _state()
    run_id = _run(c)
    _item(
        c, run_id, NITROGEN_FUEL, qty=500, price=8000.0,
        blueprint_id=4312, recommended_build_qty=0, recommended_action="buy",
    )
    _buy(c, run_id, _line(NITROGEN_FUEL, 100, 7000.0))
    page = _buy_page(seeded_client, run_id)
    assert ">built</span>" in page and "a row the plan can build" in page
    assert _cells(page, NITROGEN_FUEL)[3][1] == "500"
    assert _stat(page, "Purchased")[1].startswith("0 ISK")
    assert ">100 units outside the plan</span>" in page
    assert any("Nitrogen Fuel Block 500" in body for body in _multibuy(page).values())
    c.close()


# --- revision 6 (R4): purchases the re-plan saw are stock it netted --------
#
# Every ESI update re-plans the open run from stock that holds what was
# bought, so the page subtracts nothing: Purchased counts every unit the
# cycle paid for, Required / On Hand / Remaining are the plan's own
# figures, and every Multibuy block lists the plan's buy. Revisions 3–4's
# pre-plan / post-plan split no longer reaches the page.

PLANNED_B = "2026-09-21 12:00:00"      # SQLite datetime('now') text
BEFORE_B = "2026-09-21T10:00:00Z"      # same day: sorts AFTER it as text
AFTER_B = "2026-09-21T13:00:00Z"


def _set_planned_start(c, run_id, planned_start=PLANNED_B):
    c.execute(
        "UPDATE index_run SET planned_start = ? WHERE index_run_id = ?",
        (planned_start, run_id),
    )
    c.commit()


def test_a_replan_after_buying_reads_required_on_hand_remaining(seeded_client):
    """The review's worked case under revision 6. Run 1 executed; run 2
    needs 1000 Tritanium; the pool buys 600 at C-J6 (8.00 + 2.00
    structure freight = 10.00 landed); the next ESI update re-plans run 2
    in place from stock holding the 600: Required 1,000, On Hand 600,
    Remaining 400 — all at Jita (10.00 + 1.00 hub freight = 11.00 ladder)
    — and Purchased 600. The group's list and Multibuy All's Jita block
    list the 400; the Purchased title says nothing of "before this plan"
    any more."""
    c = _state()
    _buyer(c)
    c.execute(
        "INSERT INTO index_run (run_number, planned_start, completed_at, "
        "status, freight_in_isk_per_m3, structure_freight_in_isk_per_m3) "
        "VALUES (1, '2026-09-19 08:00:00', '2026-09-20 08:00:00', "
        "'complete', ?, ?)",
        (HUB_RATE, STRUCTURE_RATE),
    )
    c.commit()
    run_id = _run(c, run_number=2)
    _set_planned_start(c, run_id)
    _no_newer_update(c)
    _item(c, run_id, TRITANIUM, qty=400, price=10.0, on_hand_qty=600,
          target_stock_qty=1000, deficit_qty=400)
    _tx_row(c, 7001, TRITANIUM, 600, 8.0, date=BEFORE_B,
            location=STRUCTURE_LOCATION)
    _match(c)
    [line] = store.list_purchases(c, run_id)[TRITANIUM]
    assert (line["quantity"], line["venue"]) == (600, store.BUY_VENUE_STRUCTURE)

    page = _buy_page(seeded_client, run_id)
    item, required, on_hand, remaining, bought, ladder, delta = _cells(
        page, TRITANIUM
    )
    assert (required[1], on_hand[1], remaining[1]) == ("1,000", "600", "400")
    assert bought[1] == "600 @ 10"
    assert " · 600 C-J6 (" in _purchased(page, TRITANIUM)[0]
    assert "before this plan" not in bought[0]
    assert "400 to buy — 4,400 ISK at the ladder" in remaining[0]
    assert "Required − On Hand" not in remaining[0]       # the identity holds
    assert ladder[1] == "11"
    assert delta[1] == "−600"                  # 6,000 − 11 × 600
    blocks = _multibuy(page)
    assert blocks["Multibuy — Minerals"] == "Tritanium 400"
    assert blocks["Multibuy — All — Jita"] == "Tritanium 400"
    assert "Multibuy — All — C-J6 structure market" not in blocks
    assert _stat(page, "Purchased")[1].startswith("6,000 ISK")
    assert _stat(page, "Required")[1].startswith("11,000 ISK")
    assert _stat(page, "On Hand")[1].startswith("6,600 ISK")
    cls, title, _v = _stat(page, "Remaining")
    assert cls == "warn" and title.startswith("4,400 ISK — the 400 units")
    assert 'title="4,400 ISK — what the plan buys, at the ladder"' in page
    # bought 600, still buying 400: the live run asks about transit (B2)
    assert "in transit?" in item[1]
    c.close()


def test_purchases_never_come_off_the_plans_split(seeded_client):
    """Tritanium 1000 split 700 Jita / 300 C-J6 (ladder 0.7 × 10.00 +
    0.3 × 11.50 = 10.45 landed); 800 purchased, before and after
    planned_start. Purchased counts all 800 and Δ prices them; Remaining,
    the group's one list (revision 8, R2) and Multibuy All's two blocks
    are the plan's own 1,000 / 1,000 / 700 / 300."""
    c = _state()
    run_id = _run(c)
    _set_planned_start(c, run_id)
    _item(c, run_id, TRITANIUM, qty=1000, price=9.15,
          buy_venue=store.BUY_VENUE_SPLIT, hub_buy_qty=700,
          structure_buy_qty=300, hub_fill_price=9.0,
          structure_fill_price=9.5)
    _buy(
        c, run_id,
        _line(TRITANIUM, 500, 8.0, venue=store.BUY_VENUE_STRUCTURE,
              esi_id=7101, date=BEFORE_B),
        _line(TRITANIUM, 200, 8.0, venue=store.BUY_VENUE_STRUCTURE,
              esi_id=7102, date=AFTER_B),
        _line(TRITANIUM, 100, 9.0, esi_id=7103, date=AFTER_B),
    )
    page = _buy_page(seeded_client, run_id)
    _i, required, _o, remaining, bought, _ladder, delta = _cells(page, TRITANIUM)
    assert required[1] == "1,000" and remaining[1] == "1,000"
    assert bought[1].startswith("800 @ 10")    # 700 × 10.00 + 100 × 10.00
    assert "before this plan" not in bought[0]
    assert delta[1] == "−360"                  # 8,000 − 10.45 × 800
    blocks = _multibuy(page)
    assert blocks["Multibuy — Minerals"] == "Tritanium 1000"
    assert blocks["Multibuy — All — Jita"] == "Tritanium 700"
    assert blocks["Multibuy — All — C-J6 structure market"] == "Tritanium 300"
    assert _stat(page, "Purchased")[1].startswith("8,000 ISK")
    assert _stat(page, "Remaining")[1].startswith("10,450 ISK — the 1,000 units")
    c.close()


# --- revision 4 (user rulings 2026-09-28): unplanned compressed ore, and ----
# --- the default inbound rate for a purchase made elsewhere ----------------

COMPRESSED_SCORDITE = 62520   # portion 100 -> 150 Tritanium + 110 Pyerite a batch
PYERITE = 35
RIFTER, MERLIN, REAPER = 587, 603, 588   # nothing the Hulk chain buys


def _unescaped(page):
    return html.unescape(page)


def _short(value):
    """isk_short for a figure under 1,000 ISK (the Bought cell's unit)."""
    return f"{value:,.0f}"


def _match_with_ref(c, ref):
    """The real matcher WITH the game data — the unplanned-ore conversion
    needs it (with no ref nothing converts, contract review A6)."""
    from magoo import buying

    return buying.assign_purchases(c, ref, store.get_settings(c))


def test_an_unplanned_ore_purchase_covers_the_minerals_it_yields(
    seeded_client, ref,
):
    """§6 through the real matcher. The plan covers Tritanium with
    Compressed Veldspar (direct 100 + covered 400); the pool buys 250
    Compressed Scordite at Jita 4-4, an ore the plan did not pick. Two
    whole batches refine — at 90.63% into 271 Tritanium and 199 Pyerite —
    and the 50 short of a batch stay ore. So: Tritanium's Purchased is the
    271 via the ore (revision 6: Remaining is the plan's 500 until the
    next ESI update re-plans from stock — hangar ore counts as its yield,
    R3); the outside-the-plan badge counts the 50 ore and the 199 Pyerite
    the run does not hold — not the 200 refined — and names the ore
    remainder; the Purchases record reads "→ refined into …" with the
    landed ISK summed from the stored lines."""
    c = _state()
    c.execute(
        "UPDATE settings SET compressed_ore_yield = 0.9063, "
        "compressed_reprocess_tax = 0.04 WHERE id = 1"
    )
    c.commit()
    _buyer(c)
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    _tx_row(c, 8001, COMPRESSED_SCORDITE, 250, 100.0)
    _match_with_ref(c, ref)

    lines = store.list_purchases(c, run_id)
    [trit] = lines[TRITANIUM]
    [pye] = lines[PYERITE]
    [ore] = lines[COMPRESSED_SCORDITE]
    assert (trit["quantity"], trit["venue"], trit["via_type_id"]) == (
        271, store.BUY_VENUE_DELIVERED, COMPRESSED_SCORDITE,
    )
    assert (pye["quantity"], pye["via_type_id"]) == (199, COMPRESSED_SCORDITE)
    assert (ore["quantity"], ore["unit_price"], ore["via_type_id"]) == (
        50, 100.0, None,
    )
    landed = 271 * trit["unit_price"] + 199 * pye["unit_price"]
    # 200 × 100 ISK + 200 × 0.0015 m³ × the run's hub rate + the tax
    assert landed > 20_000 + 200 * 0.0015 * HUB_RATE

    page = _buy_page(seeded_client, run_id)
    _item_cell, required, _on_hand, remaining, bought, _ladder, _delta = _cells(
        page, TRITANIUM
    )
    assert required[1] == "500"
    assert bought[1] == f"271 @ {_short(trit['unit_price'])}"
    assert remaining[1] == "500"                          # the plan's buy
    title, _text = _purchased(page, TRITANIUM)
    assert (
        f" · 271 via compressed (271 units refined out of Compressed "
        f"Scordite — {271 * trit['unit_price']:,.0f} ISK landed"
    ) in title
    # R2b: the Purchased title's via clause is, with the covered raw's
    # tag title, the only place a group names an ore — an unplanned one
    # included; outside those two tooltips no compressed type is named
    groups = page[
        page.index('data-key="buy:inputs"'):page.index('data-key="buy:purchases"')
    ]
    assert "Compressed Scordite" in html.unescape(_purchased_td(page, TRITANIUM))
    assert "Compressed Scordite" in groups
    assert "Compressed" not in _group_ore_free(page)
    blocks = _multibuy(page)
    assert blocks["Multibuy — Minerals"] == "Tritanium 500"
    assert blocks["Multibuy — All — Jita"] == (
        "Tritanium 100\nCompressed Veldspar 5"
    )
    assert ">249 units outside the plan</span>" in page    # 50 ore + 199 Pyerite
    text = _unescaped(page)
    assert (
        "Of these, 50 Compressed Scordite is compressed ore left short of a "
        "whole reprocessing batch"
    ) in text
    purchases = text[text.index(">Purchases <span"):]
    assert (
        f"200 Compressed Scordite → refined into 271 Tritanium, 199 Pyerite "
        f"(landed {landed:,.0f} ISK incl. freight and refining tax); 199 "
        "Pyerite outside the plan; 50 short of a batch stay outside the plan"
    ) in purchases
    record = re.findall(r"<tr>(.*?)</tr>", purchases, re.S)[1]
    assert ">refined</span>" in record
    assert "outside the plan</span>" not in record.split("footnote")[0]
    # the refined Tritanium is plan purchase ISK: in Purchased, the Pyerite not
    bought_isk = float(_stat(page, "Purchased")[1].split(" ISK")[0].replace(",", ""))
    assert bought_isk == pytest.approx(round(271 * trit["unit_price"]))
    c.close()


def test_a_plan_chosen_ore_purchase_keeps_todays_wording(seeded_client, ref):
    """The ore the plan picked is not converted (§2.4): its line stays an
    ore line — not in the covered raw's Purchased cell — and the Purchases
    record carries no annotation. Revision 8's R2b: the ore has no cell of
    its own any more; the Purchases section lists it and the strip's
    Purchased holds its 5 × (900.00 + 0.10 freight) = 4,500.50 landed."""
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    _tx_row(c, 8002, COMPRESSED_VELDSPAR, 5, 900.0)
    _match_with_ref(c, ref)
    lines = store.list_purchases(c, run_id)
    assert set(lines) == {COMPRESSED_VELDSPAR}
    page = _buy_page(seeded_client, run_id)
    assert _cells(page, TRITANIUM)[4][1] == "—"
    assert 'id="t62516"' not in page
    purchases = page[page.index(">Purchases <span"):]
    assert "5 Compressed Veldspar" in purchases
    assert _stat(page, "Purchased")[1].startswith(
        f"{5 * (900.0 + HUB_RATE * ORE_M3):,.0f} ISK landed"
    )
    assert "refined into" not in page and "via Compressed" not in page
    c.close()


def test_the_fallback_purchases_list_labels_refined_lines(
    seeded_client, monkeypatch,
):
    """When the reader fails, the section is rebuilt from the run's lines:
    a via line sits under its ore's record, labelled as refined from it,
    and the record's venue comes from its own lines, never from a via
    line's 'delivered'."""
    from magoo import buying

    def broken_reader(conn, index_run_id):
        raise RuntimeError("reader exploded")

    monkeypatch.setattr(buying, "run_purchase_records", broken_reader)
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=1000, price=10.0)
    via = dict(
        _line(TRITANIUM, 271, 26.5, venue=store.BUY_VENUE_DELIVERED,
              esi_id=8003),
        via_type_id=COMPRESSED_SCORDITE,
    )
    _buy(c, run_id, via, _line(COMPRESSED_SCORDITE, 50, 100.0, esi_id=8003))
    page = _unescaped(_buy_page(seeded_client, run_id))
    section = page[page.index(">Purchases <span"):]
    assert "1 from ESI" in section
    record = re.findall(r"<tr>(.*?)</tr>", section, re.S)[1]
    text = " ".join(re.sub(r"<[^>]+>", " ", record).split())
    assert "Jita" in text and "delivered" not in text.split("271")[0]
    assert "271 Tritanium" in text and "refined from Compressed Scordite" in text
    # Review 2026-09-28: the price sums the via line's LANDED ISK (the
    # ore's own order price is stored nowhere), so its title says so
    # instead of "order price, before freight".
    price_title = re.findall(r'<td class="num" title="([^"]*)"', record)[0]
    assert price_title.startswith(f"{271 * 26.5 + 50 * 100.0:,.0f} ISK")
    assert "LANDED price" in price_title and "before freight" not in price_title
    c.close()


def test_a_fallback_record_without_refined_lines_keeps_before_freight(
    seeded_client, monkeypatch,
):
    from magoo import buying

    def broken_reader(conn, index_run_id):
        raise RuntimeError("reader exploded")

    monkeypatch.setattr(buying, "run_purchase_records", broken_reader)
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=1000, price=10.0)
    _buy(c, run_id, _line(TRITANIUM, 100, 9.0, esi_id=8004))
    page = _unescaped(_buy_page(seeded_client, run_id))
    section = page[page.index(">Purchases <span"):]
    record = re.findall(r"<tr>(.*?)</tr>", section, re.S)[1]
    price_title = re.findall(r'<td class="num" title="([^"]*)"', record)[0]
    assert "order price, before freight" in price_title
    assert "LANDED" not in price_title
    c.close()


def test_a_refined_output_the_run_does_not_buy_is_marked_outside(
    seeded_client, ref,
):
    """Review 2026-09-28: the run buys only Tungsten, so both raws 200
    Compressed Scordite refine into are outside the plan — the strip
    counts them, and the Purchases annotation now says so too instead of
    reading as if they fed the plan. The ore item itself stays badged
    refined (A12)."""
    c = _state()
    c.execute(
        "UPDATE settings SET compressed_ore_yield = 0.9063, "
        "compressed_reprocess_tax = 0.04 WHERE id = 1"
    )
    c.commit()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TUNGSTEN, qty=100, price=10.0)
    _tx_row(c, 8005, COMPRESSED_SCORDITE, 200, 100.0)
    _match_with_ref(c, ref)
    page = _unescaped(_buy_page(seeded_client, run_id))
    assert ">470 units outside the plan</span>" in page
    purchases = page[page.index(">Purchases <span"):]
    record = re.findall(r"<tr>(.*?)</tr>", purchases, re.S)[1]
    assert (
        "ISK incl. freight and refining tax); 271 Tritanium, 199 Pyerite "
        "outside the plan"
    ) in record
    assert "not on this run's buy list" in record
    head = record.split("footnote")[0]
    assert ">refined</span>" in head and "outside the plan</span>" not in head
    c.close()


def test_a_purchase_elsewhere_hauls_at_the_default_rate(seeded_client):
    """§2b: a wallet buy at an NPC station that is not Jita 4-4, or at a
    structure that is not the configured market, is `elsewhere` — landed
    at the RUN's default inbound rate (300 ISK/m³ → 3.00 per Tritanium,
    so 9.00 lands at 12, where the hub rate would have made it 10), its
    title naming where (the solar system; an unresolved structure by
    id), and the Purchases row naming the location. The Multibuy is the
    plan's (revision 6). Since revision 8 (R1) `elsewhere` is a clause of
    the Purchased cell's title, not a badge."""
    c = _state()
    _buyer(c)
    c.execute(
        "INSERT INTO location_system (location_id, solar_system_id, fetched_at) "
        "VALUES (?, ?, ?)",
        (AMARR_STATION, AMARR_SYSTEM, NOW),
    )
    c.commit()
    run_id = _run(c, default_rate=300.0)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _tx_row(c, 8101, TRITANIUM, 40, 9.0, date="2026-09-21T10:00:00Z",
            location=AMARR_STATION)
    _tx_row(c, 8102, TRITANIUM, 10, 9.0, date="2026-09-22T10:00:00Z",
            location=OTHER_STRUCTURE)
    _match(c)
    lines = store.list_purchases(c, run_id)[TRITANIUM]
    assert {line["venue"] for line in lines} == {store.BUY_VENUE_OTHER}
    page = _buy_page(seeded_client, run_id)
    title, text = _purchased(page, TRITANIUM)
    assert text == "50 @ 12"
    assert "50 units purchased this cycle for 600 ISK landed" in title
    assert (
        " · 50 elsewhere (wallet buys in Amarr, location 1035466617946 — at "
        "neither Jita 4-4 nor the structure market, so landed at the default "
        "inbound rate"
    ) in title
    text = _unescaped(page)
    assert _cells(page, TRITANIUM)[3][1] == "100"
    assert _multibuy(page)["Multibuy — Minerals"] == "Tritanium 100"
    section = text[text.index(">Purchases <span"):]
    rows = [
        " ".join(re.sub(r"<[^>]+>", " ", r).split())
        for r in re.findall(r"<tr>(.*?)</tr>", section, re.S)
    ][1:]
    assert rows[0].startswith(
        "2026-09-22 10:00 Buyer elsewhere location 1035466617946 10 Tritanium"
    )
    assert rows[1].startswith("2026-09-21 10:00 Buyer elsewhere Amarr 40 Tritanium")
    assert 'title="bought in Amarr — neither Jita 4-4 nor the C-J6' in text
    c.close()


def test_a_run_planned_before_the_default_rate_reads_the_live_setting(
    seeded_client,
):
    """Vintage rule (A1): NULL on the run → the live setting, like the
    other two rates. 500 ISK/m³ → 5.00 freight, 9.00 lands at 14."""
    c = _state()
    _buyer(c)
    c.execute("UPDATE settings SET freight_in_default_isk_per_m3 = 500 WHERE id = 1")
    c.commit()
    run_id = _run(c)                         # default_rate None
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _tx_row(c, 8103, TRITANIUM, 40, 9.0, location=AMARR_STATION)
    _match(c)
    page = _buy_page(seeded_client, run_id)
    title, text = _purchased(page, TRITANIUM)
    assert text == "40 @ 14"
    assert " · 40 elsewhere (" in title
    assert "location 60008494" in title              # unresolved station
    c.close()


def test_the_run_buy_rates_carry_the_default_rate():
    from magoo.web import _run_buy_rates

    settings_ = store.Settings(
        stockpile_buffer=1.0, max_run_duration_hours=168.0,
        composite_reaction_extra_runs=0, price_region_id=10000002,
        price_source="sell", freight_in_default_isk_per_m3=40.0,
    )
    run = {"freight_in_isk_per_m3": 1.0, "structure_freight_in_isk_per_m3": 2.0,
           "freight_in_default_isk_per_m3": 3.0}
    assert _run_buy_rates(run, settings_)[store.BUY_VENUE_OTHER] == 3.0
    run["freight_in_default_isk_per_m3"] = None
    assert _run_buy_rates(run, settings_)[store.BUY_VENUE_OTHER] == 40.0
    run.pop("freight_in_default_isk_per_m3")
    assert _run_buy_rates(run, settings_)[store.BUY_VENUE_OTHER] == 40.0
    assert _run_buy_rates(run, settings_)[store.BUY_VENUE_DELIVERED] == 0.0


def test_a_contract_handed_over_elsewhere_says_where(seeded_client):
    """A contract whose start location is neither market hauls at the
    default rate; its clause of the Purchased title (revision 8, R1) says
    where it was handed over."""
    c = _state()
    _buyer(c)
    c.execute(
        "INSERT INTO location_system (location_id, solar_system_id, fetched_at) "
        "VALUES (?, ?, ?)",
        (AMARR_STATION, AMARR_SYSTEM, NOW),
    )
    c.commit()
    run_id = _run(c, default_rate=300.0)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _contract(c, 7301, price=900.0, title="Amarr lot", k=0.9, priced_at=NOW,
              location=AMARR_STATION)
    _contract_item(c, 7301, 1, TRITANIUM, 100, unit_price=9.0)
    _match(c)
    [line] = store.list_purchases(c, run_id)[TRITANIUM]
    assert line["venue"] == store.BUY_VENUE_OTHER
    title, text = _purchased(_buy_page(seeded_client, run_id), TRITANIUM)
    assert text == "100 @ 12"
    assert (
        " · 100 contract (item exchange “Amarr lot”, k = 0.9000 — its price "
        "spread over the items received at their Jita reference prices; "
        "handed over in Amarr, at neither Jita 4-4 nor the structure market — "
        "landed at the default inbound rate)"
    ) in title
    c.close()


def test_the_price_refresh_pulls_order_books_for_unpriced_contract_items(
    seeded_client, monkeypatch,
):
    """§5 / A14: the items of a contract still to be priced are added to
    the ORDER-BOOK pull — not to the region-wide fallback, not to the
    stored ladders — so k uses a real Jita sell quote; a contract already
    priced (frozen) and a blueprint copy add nothing."""
    calls = []

    def fake_refresh(conn, region_id, type_ids, source="sell", **kw):
        calls.append((set(type_ids), set(kw.get("fallback_type_ids", ())),
                      set(kw.get("ladder_type_ids", ()))))
        return 0, 0, 0

    monkeypatch.setattr(market, "refresh_prices", fake_refresh)
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)
    c = _state()
    _contract(c, 7401, price=100.0)                     # not priced yet
    _contract_item(c, 7401, 1, RIFTER, 1)
    _contract_item(c, 7401, 2, MERLIN, 1, raw_quantity=-2)          # a BPC
    _contract(c, 7402, price=100.0, k=1.0, priced_at=NOW)           # frozen
    _contract_item(c, 7402, 1, REAPER, 5)
    assert seeded_client.post("/prices/refresh").status_code == 302
    [(type_ids, fallback, ladder)] = calls
    assert RIFTER in type_ids
    assert RIFTER not in fallback and RIFTER not in ladder
    assert MERLIN not in type_ids and REAPER not in type_ids
    c.close()


def _other_line(**over):
    base = dict(
        type_id=TRITANIUM, name="Tritanium", kind="material", depth=1,
        qty_per_hull=100.0, unit_cost=8.0, lag_runs=1, clamped=False,
        locked=True,
    )
    base.update(over)
    return costing.CostLine(**base)


def test_the_profit_breakdown_badges_a_line_bought_elsewhere(seeded_client):
    """A12: a line bought wholly elsewhere gets its own badge, and a split
    with an elsewhere share states it instead of "the rest from C-J6"."""
    html_ = _render_profit([_other_line(
        venue=store.BUY_VENUE_OTHER, hub_fraction=0.0, structure_fraction=0.0,
        delivered_fraction=0.0, other_fraction=1.0,
    )])
    assert ">elsewhere</span>" in html_
    assert "landed at that run" in html_
    html_ = _unescaped(_render_profit([_other_line(
        venue=store.BUY_VENUE_SPLIT, hub_fraction=0.6, structure_fraction=0.0,
        delivered_fraction=0.0, other_fraction=0.4,
    )]))
    badge = _split_badge(html_)
    assert (
        "60% of the units from Jita, 40% elsewhere (at neither "
        "market — landed at the default inbound rate)"
    ) in badge
    assert "the rest from" not in badge
    assert "C-J6" not in badge and not re.search(r"(?<![0-9])0%", badge)
    assert "bought at several venues" in badge
    assert badge.endswith(">Jita + elsewhere</span>")


def _split_badge(page: str) -> str:
    """The Profit card's one split badge (unescaped), title and label."""
    [badge] = re.findall(
        r'<span class="badge accent" title="bought at [^"]*">[^<]*</span>', page
    )
    return badge


@pytest.mark.parametrize("fractions, label, shares", [
    # Review 2026-09-28: a purchase-priced line is 'split' whenever any
    # two of the four buckets hold units, so the badge names only the
    # venues that supplied units — never a market at 0%.
    (dict(hub_fraction=0.6, structure_fraction=0.0, delivered_fraction=0.4,
          other_fraction=0.0),
     "Jita + delivered",
     "60% of the units from Jita, 40% delivered (already landed — it hauls "
     "nothing)"),
    (dict(hub_fraction=0.0, structure_fraction=0.0, delivered_fraction=0.6,
          other_fraction=0.4),
     "delivered + elsewhere",
     "60% of the units delivered (already landed — it hauls nothing), 40% "
     "elsewhere (at neither market — landed at the default inbound rate)"),
    (dict(hub_fraction=0.0, structure_fraction=0.7, delivered_fraction=0.0,
          other_fraction=0.3),
     "C-J6 + elsewhere",
     "70% of the units from C-J6, 30% elsewhere"),
    (dict(hub_fraction=0.5, structure_fraction=0.3, delivered_fraction=0.2,
          other_fraction=0.0),
     "Jita + C-J6 + delivered",
     "50% of the units from Jita, 30% from C-J6, 20% delivered"),
])
def test_the_split_badge_names_only_the_venues_that_supplied_units(
    seeded_client, fractions, label, shares,
):
    badge = _split_badge(_unescaped(_render_profit([
        _other_line(venue=store.BUY_VENUE_SPLIT, **fractions)
    ])))
    assert badge.endswith(f">{label}</span>")
    assert shares in badge
    assert "bought at several venues" in badge
    assert "both markets" not in badge
    assert not re.search(r"(?<![0-9])0%", badge)


def test_a_two_market_split_keeps_its_wording(seeded_client):
    """Jita and the structure market alone: "both markets … the rest from
    C-J6", locked or plan-priced, exactly as before revision 4."""
    for locked in (True, False):
        badge = _split_badge(_unescaped(_render_profit([_other_line(
            venue=store.BUY_VENUE_SPLIT, hub_fraction=0.6,
            structure_fraction=0.4, locked=locked,
        )])))
        assert "bought at both markets" in badge
        assert "60% of the units from Jita, the rest from C-J6" in badge
        assert badge.endswith(">Jita + C-J6</span>")


def test_a_refined_contract_item_is_badged_via_its_ore_not_the_contract(
    seeded_client,
):
    """A12: a via line carries the contract's esi_kind (the matcher copies
    the purchase's identity), so via_type_id is tested FIRST — otherwise
    the refined Tritanium would fold into the contract's clause. A direct
    Tritanium item of the same contract keeps its contract clause (title
    clauses since revision 8, R1)."""
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=1000, price=10.0)
    _contract(c, 7501, price=9000.0, title="Ore kit", k=0.9, priced_at=NOW)
    via = dict(
        _line(TRITANIUM, 271, 26.0, venue=store.BUY_VENUE_DELIVERED,
              kind=store.ESI_KIND_CONTRACT, esi_id=7501, k=0.9),
        via_type_id=COMPRESSED_SCORDITE,
    )
    direct = _line(TRITANIUM, 100, 9.0, kind=store.ESI_KIND_CONTRACT,
                   esi_id=7501, k=0.9)
    _buy(c, run_id, via, direct)
    page = _buy_page(seeded_client, run_id)
    title, text = _purchased(page, TRITANIUM)
    assert text == "371 @ 22"
    assert "· 100 contract (item exchange “Ore kit”" in title
    assert "· 271 via compressed (271 units refined out of " \
        "Compressed Scordite — 7,046 ISK landed" in title
    c.close()


def test_a_plan_chosen_ore_bought_elsewhere_lands_at_the_default_rate(
    seeded_client, ref,
):
    """A11/A12: the plan's own ore bought at neither market keeps its ore
    line and lands at the run's default rate: 900 + 1,000 ISK/m³ × 0.001
    m³ = 901 a unit (the hub rate would give 900.10), 4,505 for the 5.
    Revision 8's R2b: the ore has no cell — the figure is the strip's
    Purchased, and the Purchases section lists it elsewhere."""
    c = _state()
    _buyer(c)
    run_id = _run(c, default_rate=1000.0)
    _covered_pair(c, run_id, direct=100, covered=400)
    _tx_row(c, 8201, COMPRESSED_VELDSPAR, 5, 900.0, location=AMARR_STATION)
    _match_with_ref(c, ref)
    [line] = store.list_purchases(c, run_id)[COMPRESSED_VELDSPAR]
    assert (line["venue"], line["via_type_id"]) == (store.BUY_VENUE_OTHER, None)
    page = _buy_page(seeded_client, run_id)
    assert 'id="t62516"' not in page
    assert _stat(page, "Purchased")[1].startswith("4,505 ISK landed")
    purchases = _unescaped(page[page.index(">Purchases <span"):])
    [record] = re.findall(r"<tr>(.*?)</tr>", purchases, re.S)[1:]
    text = " ".join(re.sub(r"<[^>]+>", " ", record).split())
    assert "elsewhere" in text and "5 Compressed Veldspar" in text
    c.close()


def test_an_unplanned_ore_contract_item_is_refined_not_outside(
    seeded_client, ref,
):
    """A contract kit through the real matcher: its unplanned ore item is
    refined (badged so, never "outside the plan"), its annotation states
    what it became; a plan item beside it is costed as before."""
    c = _state()
    _buyer(c)
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    _contract(c, 7601, price=25_000.0, title="Ore kit", k=1.0, priced_at=NOW)
    _contract_item(c, 7601, 1, COMPRESSED_SCORDITE, 200, unit_price=100.0)
    _contract_item(c, 7601, 2, TRITANIUM, 500, unit_price=10.0)
    _match_with_ref(c, ref)
    lines = store.list_purchases(c, run_id)
    assert COMPRESSED_SCORDITE not in lines          # two whole batches
    assert {l["via_type_id"] for l in lines[TRITANIUM]} == {
        None, COMPRESSED_SCORDITE,
    }
    page = _unescaped(_buy_page(seeded_client, run_id))
    purchases = page[page.index(">Purchases <span"):]
    record = re.findall(r"<tr>(.*?)</tr>", purchases, re.S)[1]
    head = record.split("footnote")[0]
    assert ">refined</span>" in head
    assert "outside the plan</span>" not in head
    assert "200 Compressed Scordite → refined into" in record
    assert "short of a batch" not in record
    c.close()


# --- revision 5 (user rulings 2026-09-28): one compressed tag per row -------
#     (its New stock column was abandoned; revision 6 removed it)


def _plan_snapshot(c, fetched_at):
    """An older ESI snapshot, fetched at `fetched_at` — the pull a plan
    made after it read (web._plan_snapshot_cut: the newest retained
    snapshot at or before planned_start)."""
    c.execute(
        "INSERT INTO esi_snapshot (fetched_at, on_hand, in_progress, "
        "active_jobs, character_isk, corporation_isk, job_ends) "
        "VALUES (?, '{}', '{}', '{}', 0, 0, '{}')",
        (fetched_at,),
    )
    c.commit()


def _item_cell_html(page, type_id):
    row = _row_html(page, type_id)
    return row[:row.index("</td>")]


def test_a_covered_raw_carries_one_compressed_tag_naming_its_ores(
    seeded_client,
):
    """R1: the "N via compressed" badge is the Item cell's ONE compressed
    tag — the ores it is reprocessed out of are in its title, and the
    "from … — not a row of its own" footnote is gone. Revision 8 (R2,
    R2b): the ore is a line in Multibuy All alone; the group's own list
    names the raw itself."""
    c = _state()
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    page = _buy_page(seeded_client, run_id)
    cell = _unescaped(_item_cell_html(page, TRITANIUM))
    assert cell.count("via compressed</span>") == 1
    assert ">400 via compressed</span>" in cell
    assert "footnote" not in _row_html(page, TRITANIUM)
    assert "— not a row of its own</div>" not in page
    [title] = re.findall(r'title="([^"]*)">400 via compressed<', cell)
    assert "400 of the 500 units this cycle come from reprocessing" in title
    assert "the other 100 are bought direct" in title
    assert (
        "From Compressed Veldspar — each is a line in Multibuy All above, "
        "not a row of its own; 400 units reprocessed out of them. This "
        "group's own Multibuy lists the raw itself"
    ) in title
    # The strip's count badge is unchanged.
    assert ">1 via compressed</span>" in page
    c.close()


def test_two_unplanned_ores_fold_into_one_bought_clause(seeded_client):
    """Revision 5's R1: the via lines of two ores the plan did not pick
    fold into ONE "via compressed" source, listing each ore with its
    units and landed ISK; the Jita and C-J6 buys beside them keep their
    own. Revision 8 (R1 2026-09-29): each source is a clause of the
    Purchased cell's title — the cell itself carries no badge."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=2000, price=10.0)
    _buy(
        c, run_id,
        dict(_line(TRITANIUM, 271, 20.0, venue=store.BUY_VENUE_DELIVERED,
                   esi_id=9101), via_type_id=COMPRESSED_SCORDITE),
        dict(_line(TRITANIUM, 400, 30.0, venue=store.BUY_VENUE_DELIVERED,
                   esi_id=9102), via_type_id=COMPRESSED_VELDSPAR),
        _line(TRITANIUM, 100, 9.0, esi_id=9103),
        _line(TRITANIUM, 50, 8.0, venue=store.BUY_VENUE_STRUCTURE,
              esi_id=9104),
    )
    page = _buy_page(seeded_client, run_id)
    title, text = _purchased(page, TRITANIUM)
    assert text == "821 @ 23"
    assert title.count("via compressed (") == 1
    assert (
        "· 671 via compressed (271 units refined out of Compressed Scordite "
        "— 5,420 ISK landed; 400 units refined out of Compressed Veldspar — "
        "12,000 ISK landed. "
    ) in title
    assert "The plan did not pick these ores" in title
    assert "· 100 Jita (" in title and "· 50 C-J6 (" in title
    c.close()


# --- revision 6 (user ruling R4 2026-09-28): Required · On Hand ·
#     Remaining · Purchased — each figure the plan's own --------------------


def test_required_is_the_deficit_basis_or_the_target():
    """Contract review A13: Required = deficit + on hand + in jobs while
    the plan sized a deficit (target stock + this cycle's draw for a
    buildable; target stock itself for a just-in-time raw), else target
    stock. NULLs read 0."""
    from magoo.web import _required_of

    assert _required_of({"deficit_qty": 300, "on_hand_qty": 500,
                         "in_progress_qty": 200, "target_stock_qty": 800}) == 1000
    assert _required_of({"deficit_qty": 0, "on_hand_qty": 900,
                         "in_progress_qty": 0, "target_stock_qty": 100}) == 100
    assert _required_of({"deficit_qty": None, "target_stock_qty": None}) == 0
    assert _required_of({}) == 0


def test_on_hand_is_what_the_plan_counted_and_its_title_breaks_it_out(
    seeded_client,
):
    """A12: On Hand = on_hand_qty + in_progress_qty — the hangar, what the
    hangar's compressed ore reprocesses into (ruling R3, inside
    on_hand_qty) and job output in progress (alchemy's expected credit
    inside it), as of the ESI update the plan read. Required 5,000 − On
    Hand 3,700 = Remaining 1,300: the identity holds, so the Remaining
    title names no divergence. Once that update is pruned the title says
    so (web._plan_snapshot_cut)."""
    c = _state()
    c.execute("DELETE FROM esi_snapshot")
    c.commit()
    run_id = _run(c)
    _set_planned_start(c, run_id)
    _item(c, run_id, TRITANIUM, qty=1300, price=10.0, on_hand_qty=3500,
          on_hand_from_ore_qty=3000, in_progress_qty=200,
          alchemy_credit_qty=50, target_stock_qty=5000, deficit_qty=1300,
          cycle_need_qty=4700)
    _plan_snapshot(c, "2026-09-21 11:30:00")
    page = _buy_page(seeded_client, run_id)
    _i, required, on_hand, remaining, _p, _l, _d = _cells(page, TRITANIUM)
    assert (required[1], on_hand[1], remaining[1]) == ("5,000", "3,700", "1,300")
    assert html.unescape(required[0]) == (
        "this cycle's planned consumption plus the purchase margin "
        "(Settings now reads 5%) — steady cycle need 4,700"
    )
    assert on_hand[0] == (
        "3,700 counted by the plan: 500 in the hangar + 3,000 from hangar "
        "compressed ore (what it reprocesses into at your asserted yields) "
        "+ 200 in jobs (incl. 50 expected from unrefined stock at the "
        "reprocess yield) — as of the ESI update of 2026-09-21 11:30:00 "
        "UTC, in your tracked systems"
    )
    assert "Required − On Hand" not in remaining[0]
    # the strip: On Hand at the ladder, Required and Remaining with it
    assert _stat(page, "On Hand")[1].startswith(f"{3700 * 11:,} ISK at the ladder")
    assert _stat(page, "Required")[1].startswith(f"{5000 * 11:,} ISK")
    assert _stat(page, "Remaining")[1].startswith(f"{1300 * 11:,} ISK")
    c.execute("DELETE FROM esi_snapshot")
    c.commit()
    on_hand = _cells(_buy_page(seeded_client, run_id), TRITANIUM)[2]
    assert on_hand[0].endswith(
        "as of the ESI update this plan read (no longer kept — planned "
        "2026-09-21 12:00:00 UTC), in your tracked systems"
    )
    c.close()


def test_the_strip_caps_on_hand_at_required(seeded_client):
    """A16: a row's stock past its Required (a big Pyerite hangar the plan
    no longer buys from) counts at most Required in the strip's On Hand,
    whose title names the rest — so Required − On Hand ≈ Remaining reads
    true."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, PYERITE, qty=0, price=10.0, hub_buy_qty=0,
          on_hand_qty=900, target_stock_qty=100, deficit_qty=0)
    _buy(c, run_id, _line(PYERITE, 30, 9.0))
    page = _buy_page(seeded_client, run_id)
    _i, required, on_hand, remaining, _p, _l, _d = _cells(page, PYERITE)
    assert (required[1], on_hand[1], remaining[1]) == ("100", "900", "0")
    assert on_hand[0].endswith("; 800 beyond Required")
    assert "On Hand covers Required" in remaining[0]
    title = _stat(page, "On Hand")[1]
    assert title.startswith("1,100 ISK at the ladder")
    assert "capped at each row's Required; 800 more units on hand beyond it" in (
        html.unescape(title)
    )
    c.close()


def test_purchased_names_the_units_past_what_the_cycle_consumes(seeded_client):
    """R5 / A16: realized costing prices the units the cycle consumes
    (costing.purchase_basis); the 30 bought past the plan's 100 carry no
    cost this cycle — the Purchased title says they are next cycle's
    stock."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _buy(c, run_id, _line(TRITANIUM, 130, 9.0))
    purchased = _cells(_buy_page(seeded_client, run_id), TRITANIUM)[4]
    assert purchased[1].startswith("130 @ 10")
    assert (
        " · 30 beyond what the cycle consumes — stock for the next cycle"
    ) in purchased[0]
    _buy(c, run_id, _line(TRITANIUM, 100, 9.0))
    assert "beyond what the cycle consumes" not in _cells(
        _buy_page(seeded_client, run_id), TRITANIUM
    )[4][0]
    c.close()


def test_only_the_live_run_asks_whether_a_purchase_is_in_transit(seeded_client):
    """B2: a purchase ESI shows in no tracked system (a courier from Jita)
    is not stock the re-plan could net — the live run's row with
    Purchased > 0 and Remaining > 0 carries an "in transit?" cue titled
    with both figures, and the caption says so. An executed run's plan
    predates its purchases: no cue, and the caption reads the plan as it
    was."""
    c = _state()
    _no_newer_update(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _buy(c, run_id, _line(TRITANIUM, 40, 9.0))
    page = _buy_page(seeded_client, run_id)
    [badge] = re.findall(r'title="([^"]*)">in transit\?</span>', page)
    assert badge.startswith("40 purchased this cycle, and the plan still buys 100")
    flat = " ".join(html.unescape(page).split())
    assert "units bought but still in transit (a courier from Jita) are not on hand yet" in flat
    assert "Every ⟳ Update from ESI re-plans this run in place" in flat
    c.execute(
        "UPDATE index_run SET status = 'complete', completed_at = "
        "'2026-09-22 00:00:00' WHERE index_run_id = ?", (run_id,),
    )
    c.commit()
    page = _buy_page(seeded_client, run_id)
    assert "in transit?" not in page
    assert "re-plans this run in place" not in page
    c.close()


def test_the_stockpile_on_hand_names_the_hangar_ore_part(seeded_client):
    """Ruling R3 on the Stockpile tab: the On hand cell of a raw credited
    with hangar compressed ore says how much of it that is."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=500, price=10.0, on_hand_qty=3500,
          on_hand_from_ore_qty=3000)
    _item(c, run_id, PYERITE, qty=500, price=10.0, on_hand_qty=100)
    page = seeded_client.get(f"/runs/{run_id}?view=chain").get_data(as_text=True)
    trit = page[page.index(f'id="item-{TRITANIUM}"'):]
    trit = trit[:trit.index("</tr>")]
    assert (
        'title="incl. 3,000 from hangar compressed ore — what it reprocesses '
        'into at your asserted yields; 500 in the hangar itself"'
    ) in trit
    pye = page[page.index(f'id="item-{PYERITE}"'):]
    assert "hangar compressed ore" not in pye[:pye.index("</tr>")]
    c.close()


# --- review 2026-09-28: a stale live plan, built rows' ISK, group figures --


def _group_header(page, label):
    """{figure: (title, rendered)} of one group's header subtotals — an
    h3.subhead summary nested under Input Materials since revision 8."""
    head = re.search(
        r'<summary><h3 class="subhead">' + re.escape(label)
        + r" <span class=\"muted\">(.*?)</h3>",
        page, re.S,
    )
    assert head, f"no {label!r} group"
    out = {}
    for title, text in re.findall(
        r'<span title="([^"]*)">([a-z ]+ [^<]*)</span>', head.group(1)
    ):
        parts = text.split()
        out[" ".join(parts[:-1])] = (title, parts[-1])
    return out


def test_a_live_plan_older_than_the_last_esi_update_says_nothing_is_netted(
    seeded_client,
):
    """Review 2026-09-28 (P1): when an ESI update landed after the live
    plan — its re-plan was skipped (nothing to plan from) or failed — the
    plan nets none of the stock or purchases since. The page must not say
    what was bought "is off Remaining already", nor ask whether a
    purchase already in the hangar is in transit: the caption, the
    Remaining title and the Multibuy text name the newer update and
    ▶ Re-plan instead. With no newer update the live wording returns.

    v1.29 revision 7 (user ruling 2026-09-29; contract review amendment
    11): the final-installs stop rule is gone and a snapshot that did not
    refresh saves no newer row, so the caption names only the two causes
    left — never the cycle's final jobs installing."""
    c = _state()
    run_id = _run(c)          # planned 2026-09-20; the fixture pulled later
    # A run a v1.29 build planned (opened_at set): its newer update's
    # re-plan really was skipped. The legacy wording is the next test's.
    c.execute(
        "UPDATE index_run SET opened_at = '2026-09-20' WHERE index_run_id = ?",
        (run_id,),
    )
    c.commit()
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _buy(c, run_id, _line(TRITANIUM, 40, 9.0))
    newest = c.execute("SELECT MAX(fetched_at) FROM esi_snapshot").fetchone()[0]
    page = _buy_page(seeded_client, run_id)
    flat = " ".join(html.unescape(page).split())
    assert f"The ESI update of {newest} UTC did not re-plan it" in flat
    assert "UTC (its re-plan was skipped)" in html.unescape(_stat(page, "Remaining")[1])
    assert f"It predates the ESI update of {newest} UTC, which did not re-plan it" in flat
    assert (
        "its flash said why: nothing to plan from — no active pipeline or "
        "prices — or the re-plan failed"
    ) in flat
    cap = page[page.index('<span class="cap">'):]
    cap = " ".join(html.unescape(cap[:cap.index("</span>")]).split())
    assert "final jobs" not in cap and "being installed" not in cap
    assert "did not refresh" not in cap
    assert "stock and purchases since then are not netted" in flat
    assert "off Remaining already" not in flat
    assert "off it already" not in flat
    assert "in transit?" not in page
    title = html.unescape(_stat(page, "Remaining")[1])
    assert f"the plan predates the ESI update of {newest} UTC" in title
    assert "already off it" not in title
    assert f"It predates the ESI update of {newest} UTC" in flat
    # an executed run is never live: the plan-as-was caption, no stale note
    c.execute(
        "UPDATE index_run SET status = 'complete', completed_at = "
        "'2026-09-22 00:00:00' WHERE index_run_id = ?", (run_id,),
    )
    c.commit()
    assert "did not re-plan it" not in _buy_page(seeded_client, run_id)
    c.execute(
        "UPDATE index_run SET status = 'planned', completed_at = NULL "
        "WHERE index_run_id = ?", (run_id,),
    )
    c.commit()
    _no_newer_update(c)
    page = _buy_page(seeded_client, run_id)
    flat = " ".join(html.unescape(page).split())
    assert "did not re-plan it" not in flat
    assert "so it is off Remaining already" in flat
    assert "in transit?" in page
    c.close()


def test_a_stale_plan_from_before_v129_blames_no_skipped_replan(seeded_client):
    """Pre-release review 2026-09-30: every open run carried over from
    v1.28 (opened_at NULL — no v1.29 build planned it) is older than the
    ESI updates v1.28 made, which never re-planned anything. The first
    Buy tab view after the upgrade must still say nothing since the plan
    is netted and point at ▶ Re-plan, but name no skipped or failed
    re-plan and no flash that never existed."""
    c = _state()
    run_id = _run(c)          # opened_at NULL, planned 2026-09-20
    assert c.execute(
        "SELECT opened_at FROM index_run WHERE index_run_id = ?", (run_id,)
    ).fetchone()[0] is None
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _buy(c, run_id, _line(TRITANIUM, 40, 9.0))
    newest = c.execute("SELECT MAX(fetched_at) FROM esi_snapshot").fetchone()[0]
    page = _buy_page(seeded_client, run_id)
    flat = " ".join(html.unescape(page).split())
    cap = page[page.index('<span class="cap">'):]
    cap = " ".join(html.unescape(cap[:cap.index("</span>")]).split())
    assert (
        "This plan was made before Magoo re-planned the open run on every "
        f"ESI update (or that update could not re-plan it), so it predates "
        f"the ESI update of {newest} UTC"
    ) in cap
    assert "stock and purchases since then are not netted" in cap
    assert "▶ Re-plan run 1 on the dashboard, or ⟳ Update from ESI" in cap
    assert "its flash said why" not in flat
    assert "did not re-plan it" not in flat
    title = html.unescape(_stat(page, "Remaining")[1])
    assert f"predates the ESI update of {newest} UTC (it was not re-planned then)" in title
    assert "skipped" not in title
    assert (
        f"It predates the ESI update of {newest} UTC and has not been "
        "re-planned since"
    ) in flat
    assert "in transit?" not in page
    # Once a v1.29 build plans it (opened_at set) the ordinary wording
    # returns while it is still stale.
    c.execute(
        "UPDATE index_run SET opened_at = planned_start WHERE index_run_id = ?",
        (run_id,),
    )
    c.commit()
    flat = " ".join(html.unescape(_buy_page(seeded_client, run_id)).split())
    assert f"The ESI update of {newest} UTC did not re-plan it" in flat
    assert "This plan was made before Magoo" not in flat
    c.close()


def test_a_built_row_counts_only_what_it_holds_and_buys_in_required_isk(
    seeded_client,
):
    """Review 2026-09-28 (P2): a Nitrogen Fuel Block row with target 2,000,
    draw 1,000 and 500 on hand (deficit 2,500) that builds 2,400 and buys
    100 shows Required 3,000 in its column, but the strip's and the
    group's Required ISK count it only as its 500 on hand + 100 bought —
    the 2,400 built units are no purchase. Required − On Hand = Remaining
    in ISK; the strip title names the built units left out."""
    c = _state()
    _no_newer_update(c)
    run_id = _run(c)
    _item(
        c, run_id, NITROGEN_FUEL, qty=100, price=8000.0,
        blueprint_id=4312, recommended_build_qty=2400,
        recommended_action="both", deficit_qty=2500, target_stock_qty=2000,
        on_hand_qty=500,
    )
    page = _buy_page(seeded_client, run_id)
    cells = _cells(page, NITROGEN_FUEL)
    assert (cells[1][1], cells[2][1], cells[3][1]) == ("3,000", "500", "100")
    ladder = float(cells[5][0].split(" ISK landed per unit")[0].replace(",", ""))
    required_isk = _stat(page, "Required")[1]
    on_hand_isk = _stat(page, "On Hand")[1]
    remaining_isk = _stat(page, "Remaining")[1]
    assert required_isk.startswith(f"{600 * ladder:,.0f} ISK")
    assert on_hand_isk.startswith(f"{500 * ladder:,.0f} ISK")
    assert remaining_isk.startswith(f"{100 * ladder:,.0f} ISK")
    assert (
        "The 2,400 units the plan builds on 1 row are left out — a built "
        "row counts only what it holds and buys"
    ) in html.unescape(required_isk)
    head = _group_header(page, "Fuel Blocks")
    assert head["required"][0].startswith(f"{600 * ladder:,.0f} ISK")
    c.close()


def test_group_headers_carry_the_capped_on_hand_and_the_costed_purchases(
    seeded_client,
):
    """Review 2026-09-28 (P2): each group header's four subtotals, by hand.
    Minerals: Pyerite Required 100, On Hand 900 at an 11.00 ladder →
    required 1.10K and on hand 1.10K (capped at Required, never 9.9K);
    purchased = Pyerite's 30 at 10.00 landed + the plan-chosen Compressed
    Veldspar's 5 at 900.10 landed (an ore's purchase lands in the group
    it is filed under). Fuel Blocks: a built row's purchase is outside
    the plan, so purchased reads 0."""
    c = _state()
    _no_newer_update(c)
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    _item(c, run_id, PYERITE, qty=0, price=10.0, hub_buy_qty=0,
          on_hand_qty=900, target_stock_qty=100, deficit_qty=0)
    _item(
        c, run_id, NITROGEN_FUEL, qty=500, price=8000.0,
        blueprint_id=4312, recommended_build_qty=600,
        recommended_action="both", deficit_qty=1100, target_stock_qty=800,
    )
    _buy(
        c, run_id, _line(PYERITE, 30, 9.0),
        _line(COMPRESSED_VELDSPAR, 5, 900.0, esi_id=5002),
        _line(NITROGEN_FUEL, 100, 7000.0, esi_id=5003),
    )
    page = _buy_page(seeded_client, run_id)
    minerals = _group_header(page, "Minerals")
    ore_landed = 5 * (900.0 + HUB_RATE * ORE_M3)
    assert minerals["purchased"][0].startswith(f"{300 + ore_landed:,.0f} ISK")
    # Pyerite's capped share inside the group's On Hand: 100 × 11, never
    # 900 × 11 (Tritanium holds nothing)
    assert minerals["on hand"][0].startswith("1,100 ISK")
    fuel = _group_header(page, "Fuel Blocks")
    assert fuel["purchased"][0].startswith("0 ISK")
    assert ">100 units outside the plan</span>" in page
    c.close()


def test_the_margin_units_turn_into_next_cycles_stock_after_the_replan(
    seeded_client,
):
    """Review 2026-09-28 (P2): purchase_basis = max(cycle need, what the
    plan still buys). Tritanium: cycle need 1,000, target 1,050 (the 5%
    margin). Bought 1,050 before the next update: the plan still buys
    1,050, so nothing reads as beyond the cycle. After the update's
    re-plan the hangar holds the 1,050 and the plan buys 0: Required
    reads 1,050 (its target) and the Purchased title names the 50 margin
    units as next cycle's stock — realized costing prices 1,000."""
    c = _state()
    _no_newer_update(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=1050, price=10.0, cycle_need_qty=1000,
          target_stock_qty=1050, deficit_qty=1050)
    _buy(c, run_id, _line(TRITANIUM, 1050, 9.0))
    purchased = _cells(_buy_page(seeded_client, run_id), TRITANIUM)[4]
    assert "beyond what the cycle consumes" not in purchased[0]
    c.execute(
        "UPDATE index_run_item SET recommended_buy_qty = 0, hub_buy_qty = 0, "
        "deficit_qty = 0, on_hand_qty = 1050 WHERE index_run_id = ?",
        (run_id,),
    )
    c.commit()
    cells = _cells(_buy_page(seeded_client, run_id), TRITANIUM)
    assert (cells[1][1], cells[2][1], cells[3][1]) == ("1,050", "1,050", "0")
    assert (
        " · 50 beyond what the cycle consumes — stock for the next cycle"
    ) in cells[4][0]
    row = c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,)
    ).fetchone()
    assert costing.purchase_basis(row) == 1000
    c.close()


def test_multibuy_all_names_an_unsourced_only_remainder(seeded_client):
    """Review 2026-09-28 (P2): when every unit left is unsourced, Multibuy
    All has no line — its empty state must not claim stock covers the plan
    while the strip's Remaining reads warn."""
    c = _state()
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0, hub_buy_qty=0,
          unfilled_qty=100, unfilled_price=10.0)
    page = _buy_page(seeded_client, run_id)
    flat = " ".join(html.unescape(page).split())
    assert (
        "Nothing to buy — stock on hand covers the plan, or what is left "
        "had no market at plan time."
    ) in flat


# --- revision 8 (user rulings 2026-09-29): plain group lists, collapsed ----
# --- groups under one "Input Materials" section --------------------------


def test_each_group_multibuy_is_one_plain_list_of_remaining(seeded_client):
    """R2: a group's Multibuy is exactly ONE textarea — one line per row
    with Remaining > 0, quantity = Remaining, in the table's order, not
    split by market: Tritanium (100 direct + 400 covered) is 500 as the
    raw itself with no ore line; Hydrocarbons split 700 Jita / 300 C-J6 is
    one line of 1,000; the gas row's 20 unsourced units are part of its
    50. Pyerite (bought, nothing remaining) is no line; a group with
    nothing remaining says so. Multibuy All is unchanged: its two
    per-market blocks with the ore line, the unsourced units in
    neither."""
    c = _state()
    run_id = _run(c)
    _covered_pair(c, run_id, direct=100, covered=400)
    _item(c, run_id, PYERITE, qty=0, price=10.0, hub_buy_qty=0,
          on_hand_qty=100, target_stock_qty=100, deficit_qty=0)
    _item(c, run_id, HYDROCARBONS, qty=1000, price=9.4,
          buy_venue=store.BUY_VENUE_SPLIT, hub_buy_qty=700,
          structure_buy_qty=300, hub_fill_price=9.0,
          structure_fill_price=9.5)
    _item(c, run_id, FULLERITE, qty=50, price=30.0, hub_buy_qty=30,
          unfilled_qty=20, unfilled_price=31.0)
    _item(c, run_id, NITROGEN_FUEL, qty=0, price=8000.0, hub_buy_qty=0,
          on_hand_qty=10, target_stock_qty=10, deficit_qty=0)
    _buy(c, run_id, _line(PYERITE, 10, 9.0),
         _line(NITROGEN_FUEL, 10, 7000.0, esi_id=5002))
    page = _buy_page(seeded_client, run_id)
    blocks = _multibuy(page)
    gas = re.fullmatch(r"(.+) 50", blocks["Multibuy — Gas"]).group(1)
    assert blocks == {
        "Multibuy — All — Jita": (
            f"Tritanium 100\nCompressed Veldspar 5\nHydrocarbons 700\n{gas} 30"
        ),
        "Multibuy — All — C-J6 structure market": "Hydrocarbons 300",
        "Multibuy — Minerals": "Tritanium 500",
        "Multibuy — Moon Materials": "Hydrocarbons 1000",
        "Multibuy — Gas": f"{gas} 50",
    }
    assert page.count("<textarea") == 5         # Fuel Blocks has none
    flat = " ".join(html.unescape(page).split())
    assert flat.count("Multibuy — 1 item to buy") == 3
    assert "Multibuy — 0 items to buy" in flat
    assert "Nothing to buy in this group — stock on hand covers it." in flat
    # inside the groups: no market split, no ore, no reprocess checklist
    groups = _group_ore_free(page)
    assert "Compressed" not in groups
    assert "Jita</p>" not in groups and "structure market</p>" not in groups
    assert "Reprocess <b>" not in groups
    c.close()


def test_input_materials_wraps_the_groups_collapsed(seeded_client):
    """R3: ONE top-level section, open, keyed buy:inputs, whose h2 names
    the group count and what remains at the ladder, wraps every group;
    each group is a nested h3.subhead section that renders WITHOUT `open`
    (data-default="closed") and keeps its row count and four subtotals in
    the summary. Multibuy All before it and Purchases after it still
    render open."""
    c = _state()
    _no_newer_update(c)
    run_id = _run(c)
    _item(c, run_id, TRITANIUM, qty=100, price=10.0)
    _item(c, run_id, NITROGEN_FUEL, qty=2, price=8000.0)
    page = _buy_page(seeded_client, run_id)
    assert page.count('<details class="section" open data-key="buy:inputs">') == 1
    wrapper = page.index('data-key="buy:inputs"')
    purchases = page.index('data-key="buy:purchases"')
    head = " ".join(
        html.unescape(page[wrapper:page.index("</h2>", wrapper)]).split()
    )
    total = _stat(page, "Remaining")[1].split(" ISK")[0]
    assert head.startswith(
        'data-key="buy:inputs"> <summary><h2>Input Materials '
        '<span class="muted">— 2 groups · '
        f'<span title="{total} ISK — what the plan buys across every group, '
        'at the ladder\'s landed price">remaining '
    )
    groups = re.findall(
        r'<details ([^>]*)>\s*<summary><h3 class="subhead">([^<]*)', page
    )
    assert [(attrs, label.strip()) for attrs, label in groups] == [
        ('class="section" data-default="closed" data-key="buy:inputs:minerals"',
         "Minerals"),
        ('class="section" data-default="closed" '
         'data-key="buy:inputs:fuel-blocks"', "Fuel Blocks"),
    ]
    for attrs, label in groups:
        assert wrapper < page.index(attrs) < purchases, label
        assert "open" not in attrs.split(), label
    minerals = _group_header(page, "Minerals")
    assert set(minerals) == {"required", "on hand", "remaining", "purchased"}
    assert minerals["remaining"][1] == "1.10K"         # 100 × 11.00 landed
    summary = page[page.index('data-key="buy:inputs:minerals"'):]
    summary = " ".join(summary[:summary.index("</h3>")].split())
    assert "Minerals <span class=\"muted\">— 1 item," in summary
    assert '<details class="section" open data-key="buy:all">' in page
    assert '<details class="section" open data-key="buy:purchases">' in page
    assert page.index('data-key="buy:all"') < wrapper < purchases
    c.close()


def test_the_section_state_script_remembers_both_defaults():
    """R3: base.html's section-state script, run under node when it is on
    the PATH (else only its source is checked). A default-open section
    closes on a stored "closed" and stores "closed" when the user closes
    it — as before; a data-default="closed" section opens on a stored
    "open", stays closed without one (or on "closed"), and stores "open"
    when the user opens it."""
    import pathlib
    import shutil
    import subprocess

    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "magoo" / "templates" / "base.html"
    ).read_text(encoding="utf-8").replace("\r\n", "\n")
    start = source.index(
        'document.addEventListener("DOMContentLoaded", () => {\n'
        "  const view = new URLSearchParams"
    )
    end = source.index("\n});\n", start) + len("\n});\n")
    script = source[start:end]
    assert 'd.dataset.default === "closed"' in script
    assert 'stored === "open"' in script
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on the PATH")
    harness = r"""
const store = {
  "section:/runs/#?view=buy:buy:all": "closed",
  "section:/runs/#?view=buy:buy:inputs:minerals": "open",
  "section:/runs/#?view=buy:buy:inputs:gas": "closed",
};
globalThis.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = v; },
};
globalThis.location = {pathname: "/runs/63", search: "?view=buy"};
function section(key, open, dflt) {
  return {open, dataset: dflt ? {key, default: dflt} : {key}, listeners: [],
    addEventListener(ev, fn) { this.listeners.push(fn); },
    toggle() { this.open = !this.open; this.listeners.forEach(f => f()); }};
}
const all = section("buy:all", true);
const purchases = section("buy:purchases", true);
const minerals = section("buy:inputs:minerals", false, "closed");
const gas = section("buy:inputs:gas", false, "closed");
const fuel = section("buy:inputs:fuel-blocks", false, "closed");
let ready;
globalThis.document = {
  addEventListener: (ev, fn) => { ready = fn; },
  querySelectorAll: () => [all, purchases, minerals, gas, fuel],
};
""" + script + r"""
ready();
const loaded = [all, purchases, minerals, gas, fuel].map(d => d.open);
fuel.toggle(); purchases.toggle(); minerals.toggle();
console.log(JSON.stringify({loaded, store}));
"""
    done = subprocess.run(
        [node, "-e", harness], capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    # all: stored closed; purchases: default open; minerals: stored open;
    # gas: stored closed stays closed; fuel: nothing stored stays closed
    assert out["loaded"] == [False, True, True, False, False]
    key = "section:/runs/#?view=buy:"
    assert out["store"][key + "buy:inputs:fuel-blocks"] == "open"
    assert out["store"][key + "buy:purchases"] == "closed"
    assert out["store"][key + "buy:inputs:minerals"] == "closed"
