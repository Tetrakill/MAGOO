"""Alchemy for-sale holds on the pages (user rulings 2026-10-31, rebuilt
after the review of the same day): the hold driven through the stubbed ESI
update — installs anchor it, deliveries open it to the settle, sales close
it — and what each page then shows: the Sell in Jita panel's Released /
Held / status cells and Mark sold, the Profit tab's Alchemy for sale table
and the "incl." totals, the Index Runs list's figures, the Ledger section,
the Stockpile annotation, and the Mark executed / Reopen / Discard guards.

Over conftest.seeded_client (Hulk x 8, slots 500/500, FairValue quotes).
The network steps of POST /esi/refresh are stubbed the way
test_runs_list.py does it, with the job record and the wallet sells the
test controls (a Scenario); every figure asserted is computed here from
the plan's own row and the fee rates, never read back from the module
under test. The hold fixtures are test_alchemy_hold's.
"""

import html as html_mod
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from magoo import config, costing, engine, esi, holds, ledger, market, store
from conftest import FairValuePrices
from test_alchemy import expensive_rare_inputs, reaction_candidates, snapshot
from test_alchemy_hold import complete_run, reaction_job
from test_ledger import A, BUYER, STATION, seed_quote, seed_tx
from test_ledger_page import _add_character
from test_runs_list import _row as _runs_row, _strip_isk, _title_isk
from test_web_lifecycle import (
    _detail_ctx, _newer_run, _render_detail, _settings, _settings_form, _state,
)

TRITANIUM = 34
YIELD = 0.55
POOL = 500  # conftest.seeded_client's reaction pool


# --- helpers -------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _u(page: str) -> str:
    """The page with Jinja's escaping undone (apostrophes in flashes and
    titles come out as &#39;)."""
    return html_mod.unescape(page)


def _whole(value: float) -> int:
    """`value` as the `isk` filter prints it (whole ISK, same rounding)."""
    return int(f"{value:.0f}")


def _expected(units: int, route, yield_=YIELD) -> int:
    """Composite units of `units` unrefined at the yield — D1's figure."""
    return math.floor(units * route.composite_qty * yield_)


def _plan(client) -> int:
    resp = client.post("/run")
    assert resp.status_code == 302, resp.status_code
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def _db_settings():
    c = _state()
    try:
        return store.get_settings(c)
    finally:
        c.close()


def _item(c, run_id, type_id):
    return c.execute(
        "SELECT * FROM index_run_item WHERE index_run_id = ? AND type_id = ?",
        (run_id, type_id),
    ).fetchone()


def _hold(ref, run_id, composite_id) -> holds.Hold:
    c = _state()
    try:
        [hold] = [
            h for h in holds.load(c, ref, store.get_settings(c), run_id=run_id)
            if h.composite_type_id == composite_id
        ]
    finally:
        c.close()
    return hold


def _releases(run_id=None) -> list[tuple]:
    """(run, source, ref_id, qty, unit_price, unit_cost) of every release."""
    c = _state()
    try:
        return [
            (x["index_run_id"], x["source"], x["ref_id"], x["qty"], x["unit_price"], x["unit_cost"])
            for x in store.release_rows(c, run_id)
        ]
    finally:
        c.close()


def _shape(c, run_id) -> dict:
    """{type_id: (jobs, install_jobs, for-sale jobs, buy)} — the plan's shape."""
    return {
        r["type_id"]: (
            r["jobs_allocated"], r["install_jobs"], r["alchemy_profit_jobs"], r["recommended_buy_qty"],
        )
        for r in c.execute("SELECT * FROM index_run_item WHERE index_run_id = ?", (run_id,))
    }


def _sale_units_of(item) -> int:
    """The unrefined units a formula row runs FOR SALE now: the row's
    for-sale jobs' units, trimmed first by an install cut (the savings
    jobs come first — alchemy_sale_split's rule, written out)."""
    if item is None:
        return 0
    per = int(item["max_runs_per_job"]) * int(item["portion_size"] or 1)
    jobs, profit_jobs = int(item["jobs_allocated"] or 0), int(item["alchemy_profit_jobs"] or 0)
    total = profit_jobs * per
    if item["install_runs"] is None:
        return total
    savings = (jobs - profit_jobs) * per
    return min(total, max(0, int(item["install_runs"]) * int(item["portion_size"] or 1) - savings))


def _net_unit(ref, settings, type_id, price, location_id=None) -> float:
    """Net proceeds per composite unit sold at `price`: fees by where it
    sold (an NPC station = the hub rates), the class rule with no place —
    the Ledger's and the Profit tab's own convention."""
    info = ref.type_info(type_id)
    return costing.net_proceeds_at_venue(
        price, info.freight_volume, settings, costing.sale_venue(location_id),
        capital=False, freight_exempt=costing.freight_out_exempt(type_id),
    )


# --- the world ------------------------------------------------------------------


class World:
    """The seeded app with Alchemy for Profit on and run 1 planned: the
    on-chain target composite (Prometium, from Unrefined Prometium — the
    brief's route) quoted at `quote`, a Count-sales character logged in,
    the run's opening dated `days_ago` days back so the record the
    scenarios write sits inside its buying window and before today."""

    def __init__(self, client, ref, run_id, route, opened, item):
        self.client = client
        self.ref = ref
        self.run_id = run_id
        self.route = route
        self.opened = opened
        self.composite = route.composite_id
        self.unrefined = route.unrefined_id
        self.name = ref.type_info(route.composite_id).name
        self.hulk = ref.type_id("Hulk")
        self.jobs = int(item["jobs_allocated"])
        self.profit_jobs = int(item["alchemy_profit_jobs"] or 0)
        self.per = int(item["max_runs_per_job"]) * int(item["portion_size"] or 1)
        self.sale_units = self.profit_jobs * self.per
        self.planned = _expected(self.sale_units, route)
        self.route_unit = item["alchemy_route_unit_cost"]
        self.start = opened + timedelta(seconds=1)  # inside the window, as install_jobs does

    @property
    def sold_action(self) -> str:
        return f"/runs/{self.run_id}/alchemy/{self.composite}/sold"

    def get(self, view=None, run_id=None) -> str:
        path = f"/runs/{run_id or self.run_id}" + (f"?view={view}" if view else "")
        resp = self.client.get(path)
        assert resp.status_code == 200, (path, resp.status_code)
        return resp.get_data(as_text=True)

    def execute(self, run_id=None) -> str:
        """Mark executed through the route; the flash page, unescaped."""
        resp = self.client.post(f"/runs/{run_id or self.run_id}/complete", follow_redirects=True)
        assert resp.status_code == 200
        return _u(resp.get_data(as_text=True))

    def sold(self, run_id=None, composite_id=None, **form) -> str:
        """POST Mark sold from `run_id`'s page; the flash page, unescaped."""
        resp = self.client.post(
            f"/runs/{run_id or self.run_id}/alchemy/{composite_id or self.composite}/sold",
            data=form, follow_redirects=True,
        )
        assert resp.status_code == 200
        return _u(resp.get_data(as_text=True))

    def net_quote(self) -> float:
        """Today's net per unit at the cached hub quote (the class rule)."""
        return _net_unit(self.ref, _db_settings(), self.composite, self._quote())

    def pin_cap(self) -> None:
        """Max Alchemy Jobs per Type = the jobs run 1 listed: once they are
        installed the cap is met (D5 counts installed jobs), so later
        re-plans list no new job and the panel's figures are the hold's
        alone."""
        c = _state()
        c.execute("UPDATE settings SET max_alchemy_jobs_per_type = ?", (self.jobs,))
        c.commit()
        c.close()

    def _quote(self) -> float:
        c = _state()
        try:
            return c.execute(
                "SELECT price FROM market_price WHERE type_id = ? AND hub = 1", (self.composite,)
            ).fetchone()[0]
        finally:
            c.close()


def _world(client, ref, *, quote=1e6, savings=False, profit=True, cap=1000, days_ago=20) -> World:
    c = _state()
    # The target before the flags: the first routed composite the chain
    # BUILDS (reaction_candidates), so the hold's bite has a row to show on.
    base = engine.plan_index_run(c, ref, snapshot(ref), persist=False)
    target = reaction_candidates(ref, base)[0]
    route = ref.alchemy_routes()[target.type_id]
    assert (ref.type_info(route.composite_id).name, ref.type_info(route.unrefined_id).name) == (
        "Prometium", "Unrefined Prometium"
    ), "the brief's route (an SDE / fixture tripwire)"
    c.execute(
        "UPDATE settings SET alchemy_enabled = 1, alchemy_for_profit = ?, "
        "max_alchemy_jobs_per_type = ?",
        (1 if profit else 0, cap),
    )
    c.commit()
    overrides = expensive_rare_inputs(ref, [target]) if savings else {}
    overrides[route.composite_id] = quote
    if savings:
        # The conn-based for_sale_world's semantics: FairValuePrices prices
        # every parent off its inputs, so the dear goo lifts the quotes of
        # everything built from it and the chain keeps BUILDING the
        # composite (re-quoting the goo alone would make the market beat
        # the build above it and drop the composite from the chain).
        settings_ = store.get_settings(c)
        prices = FairValuePrices(ref, overrides=overrides)
        now = _now().isoformat()
        c.executemany(
            "INSERT OR REPLACE INTO market_price (type_id, region_id, source, price, fetched_at, hub) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            [
                (type_id, settings_.price_region_id, settings_.price_source, prices.get(type_id), now)
                for type_id in engine.demand_type_ids(c, ref)
            ],
        )
        c.commit()
    for type_id, price in overrides.items():
        seed_quote(c, type_id, price)
    _add_character(c)  # the Count-sales owner the sales belong to
    c.close()
    run_id = _plan(client)
    opened = _now() - timedelta(days=days_ago)
    c = _state()
    c.execute(
        "UPDATE index_run SET opened_at = ?, planned_start = ? WHERE index_run_id = ?",
        (store.utc_text(opened), store.utc_text(opened), run_id),
    )
    # The hold rows the plan wrote are anchored at the plan's snapshot time
    # (now); the world moves 20 days back as a whole, anchors included, so
    # the jobs the scenario installs at `start` are this plan's.
    c.execute(
        "UPDATE alchemy_hold SET anchored_at = ? WHERE index_run_id = ?",
        (store.utc_text(opened), run_id),
    )
    c.commit()
    item = _item(c, run_id, route.unrefined_id)
    c.close()
    assert item is not None, "the plan lists the route"
    return World(client, ref, run_id, route, opened, item)


# --- the stubbed ESI update -------------------------------------------------------


class Scenario:
    """What the stubbed update reports: the job record (one row per job,
    replaced wholesale as save_industry_jobs does), the hangar, the output
    in flight, the job starts, and the wallet sells the sales pull stores."""

    def __init__(self):
        self.jobs: dict[int, dict] = {}
        self.on_hand: dict[int, int] = {}
        self.in_progress: dict[int, int] = {}
        self.job_starts: dict[int, list] = {}
        self.sales: list[dict] = []

    def install(self, type_id, start, jobs, units_per_job, *, first_job_id,
                activity=config.ACTIVITY_REACTION, status="active"):
        """`jobs` jobs of `type_id` started at `start`, 720 h long; their
        output in flight. Returns (job ids, timer end)."""
        end = start + timedelta(hours=720)
        ids = []
        for n in range(jobs):
            row = reaction_job(first_job_id + n, type_id, start, units_per_job, status, end_date=end)
            row["activity_id"] = activity
            self.jobs[row["job_id"]] = row
            ids.append(row["job_id"])
        units = jobs * units_per_job
        self.in_progress[type_id] = self.in_progress.get(type_id, 0) + units
        self.job_starts.setdefault(type_id, []).append([_iso(start), units])
        return ids, end

    def deliver(self, job_ids, completed, to_hangar=None):
        """ESI shows the jobs delivered at `completed` (completed_date):
        their output leaves the in-flight figure; `to_hangar` ({type:
        units}) is what the reprocess put in the hangar."""
        gone: dict[int, int] = {}
        for job_id in job_ids:
            row = self.jobs[job_id]
            row["status"] = "delivered"
            row["completed_date"] = completed
            gone[row["type_id"]] = gone.get(row["type_id"], 0) + int(row["units"])
        for type_id, units in gone.items():
            self.in_progress[type_id] = max(0, self.in_progress.get(type_id, 0) - units)
        for type_id, units in (to_hangar or {}).items():
            self.on_hand[type_id] = self.on_hand.get(type_id, 0) + units

    def sale(self, transaction_id, type_id, qty, price, date, *, client_id=BUYER, location_id=STATION):
        self.sales.append({
            "tid": transaction_id, "type_id": type_id, "qty": qty, "price": price,
            "date": _iso(date), "client_id": client_id, "location_id": location_id,
        })


def _refresh(client, monkeypatch, scenario: Scenario) -> str:
    """POST /esi/refresh with its network steps stubbed (the
    test_runs_list pattern): the snapshot step writes the scenario's job
    rows (the pull logged now, as refresh_state does) and a snapshot with
    its hangar / in-flight / job starts; the sales pull stores the
    scenario's wallet sells and logs itself; prices stay cached. Returns
    the flash page, unescaped."""
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (0, 0, 0))
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)
    now = _iso(_now())

    def fake_state(conn, ref_):
        store.save_industry_jobs(conn, [dict(r) for r in scenario.jobs.values()])
        starts = {t: [list(p) for p in ps] for t, ps in scenario.job_starts.items()}
        store.save_esi_snapshot(
            conn, dict(scenario.on_hand), dict(scenario.in_progress), {}, 0.0, 0.0, {},
            job_starts=starts,
        )
        return {"on_hand": dict(scenario.on_hand), "in_progress": dict(scenario.in_progress),
                "active_jobs": {}, "job_ends": {}, "character_isk": 0.0,
                "corporation_isk": 0.0, "job_starts": starts}

    def fake_pull(conn, ref_):
        new = 0
        for s in scenario.sales:
            cur = conn.execute(
                "INSERT OR IGNORE INTO sale_transaction (transaction_id, owner_kind, owner_id, "
                "division, source_feed, type_id, quantity, unit_price, date, location_id, "
                "client_id, journal_ref_id, fetched_at) VALUES (?, 'character', ?, NULL, "
                "'character', ?, ?, ?, ?, ?, ?, NULL, ?)",
                (s["tid"], A, s["type_id"], s["qty"], s["price"], s["date"], s["location_id"],
                 s["client_id"], now),
            )
            new += cur.rowcount
        conn.execute(
            "INSERT INTO sales_pull (owner_kind, owner_id, family, division, status, pulled_at) "
            "VALUES ('character', ?, 'transactions', 0, 'ok', ?) "
            "ON CONFLICT (owner_kind, owner_id, family, division) DO UPDATE SET "
            "status = 'ok', pulled_at = excluded.pulled_at",
            (A, now),
        )
        conn.commit()
        return ledger.PullSummary(new_sales=new, contracts_finished=0, open_orders=0)

    monkeypatch.setattr(esi, "refresh_state", fake_state)
    monkeypatch.setattr(ledger, "pull_sales", fake_pull)
    resp = client.post("/esi/refresh", follow_redirects=True)
    assert resp.status_code == 200
    return _u(resp.get_data(as_text=True))


def _install_run(w: World, sc: Scenario, *, first_job_id=90000, hulls=True):
    """The scenario installs the run's listed for-sale jobs (and the
    cycle's 8 Hulk jobs) one second after it opened. Returns (job ids,
    timer end)."""
    ids, end = sc.install(w.unrefined, w.start, w.jobs, w.per, first_job_id=first_job_id)
    if hulls:
        sc.install(w.hulk, w.start, 8, 1, first_job_id=80000, activity=config.ACTIVITY_MANUFACTURING)
    return ids, end


def _seed_contract(c, contract_id, items, price, issued, completed, *, acceptor=BUYER):
    """A finished item-exchange contract we issued, `items` = [(type_id,
    qty)] included, dated by hand (test_ledger.seed_contract hard-codes
    its dates)."""
    now = _iso(_now())
    c.execute(
        "INSERT INTO sale_contract (contract_id, owner_kind, owner_id, issuer_id, "
        "issuer_corporation_id, for_corporation, acceptor_id, type, status, price, date_issued, "
        "date_completed, date_expired, start_location_id, first_seen_at, last_seen_at, "
        "items_fetched_at, items_status) VALUES (?, 'character', ?, ?, 98000001, 0, ?, "
        "'item_exchange', 'finished', ?, ?, ?, ?, ?, ?, ?, ?, 'ok')",
        (contract_id, A, A, acceptor, price, _iso(issued), _iso(completed),
         _iso(completed + timedelta(days=14)), STATION, now, now, now),
    )
    c.executemany(
        "INSERT INTO sale_contract_item (contract_id, record_id, type_id, quantity, raw_quantity, "
        "is_included, is_singleton) VALUES (?, ?, ?, ?, ?, 1, 0)",
        [(contract_id, n + 1, t, q, q) for n, (t, q) in enumerate(items)],
    )
    c.commit()


# --- page parsers ----------------------------------------------------------------

_TD = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)


def _tds(tr: str) -> list[tuple[str, str]]:
    """Each <td> of a row as (attributes, inner html)."""
    return _TD.findall(tr)


def _text(fragment: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _attr(attrs: str, name: str):
    m = re.search(rf'{name}="([^"]*)"', attrs)
    return html_mod.unescape(m.group(1)) if m else None


def _num(fragment: str):
    text = _text(fragment)
    return None if text in ("—", "") else int(text.replace(",", ""))


def _isk_of(title):
    """The leading 'N ISK' of a title as an int; None when there is none."""
    m = re.match(r"(-?[\d,]+) ISK", title or "")
    return int(m.group(1).replace(",", "")) if m else None


def _panel(page: str) -> str:
    start = page.index('id="sell-in-jita"')
    return page[start:page.index("</table>", start)]


def _panel_rows(page: str) -> dict[str, dict]:
    """The Sell in Jita rows by composite: Sell qty / Released / Held
    (None for a dash), the Expected profit cell and title, the status
    badge's text, the flags and the actions cell."""
    rows = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", _panel(page), flags=re.S):
        tds = _tds(tr)
        if len(tds) != 7:
            continue
        badge = re.search(r'<span class="badge[^"]*"[^>]*>([^<]*)</span>', tds[5][1])
        rows[_text(tds[0][1])] = {
            "sell_qty": _num(tds[1][1]), "sell_title": _attr(tds[1][0], "title"),
            "released": _num(tds[2][1]), "released_title": _attr(tds[2][0], "title"),
            "held": _num(tds[3][1]), "held_title": _attr(tds[3][0], "title"),
            "profit": _text(tds[4][1]), "profit_title": _attr(tds[4][0], "title"),
            "status": badge.group(1) if badge else None,
            "flags": tds[5][1], "actions": tds[6][1],
        }
    return rows


def _panel_total(page: str) -> tuple[int, str]:
    """(Total row's Sell qty, its Expected profit title)."""
    m = re.search(r'<tr class="total">(.*?)</tr>', _panel(page), flags=re.S)
    assert m, "the Sell in Jita Total row"
    tds = _tds(m.group(1))
    return _num(tds[1][1]), _attr(tds[4][0], "title")


def _stat(page: str, label: str):
    """(value text, title) of a totals-strip stat; None when absent."""
    m = re.search(
        rf'<span class="label">{re.escape(label)}</span>\s*'
        r'<span class="value[^"]*"(?:\s+title="([^"]*)")?>\s*([^<]*)<',
        page,
    )
    return None if m is None else (m.group(2).strip(), html_mod.unescape(m.group(1) or ""))


def _profit_row(page: str, name: str) -> dict:
    """The Profit tab's Alchemy for sale row of `name`: Planned / Sold /
    Still held from data-sort, the money cells' titles (leading 'N ISK'),
    the Sold cell's text and the flags cell."""
    start = page.index('data-key="alchemy-sale"')
    section = page[start:page.index("</table>", start)]
    for tr in re.findall(r"<tr>(.*?)</tr>", section, flags=re.S):
        tds = _tds(tr)
        if len(tds) != 9 or _text(tds[0][1]) != name:
            continue
        sort = lambda attrs: int(re.search(r'data-sort="(-?\d+)"', attrs).group(1))  # noqa: E731
        return {
            "planned": sort(tds[1][0]), "planned_title": _attr(tds[1][0], "title"),
            "sold": sort(tds[2][0]), "sold_text": _text(tds[2][1]), "sold_title": _attr(tds[2][0], "title"),
            "realized": _isk_of(_attr(tds[3][0], "title")), "realized_title": _attr(tds[3][0], "title"),
            "held": sort(tds[4][0]), "held_title": _attr(tds[4][0], "title"),
            "est": _isk_of(_attr(tds[5][0], "title")), "est_title": _attr(tds[5][0], "title"),
            "est_text": _text(tds[5][1]),
            "cost": _isk_of(_attr(tds[6][0], "title")), "cost_title": _attr(tds[6][0], "title"),
            "profit": _isk_of(_attr(tds[7][0], "title")), "profit_title": _attr(tds[7][0], "title"),
            "flags": tds[8][1],
        }
    raise AssertionError(f"{name} is not on the Profit tab")


def _ledger_section(page: str) -> str:
    # The section stands on its own after the history (review 2026-10-31,
    # out of the pipelines gate): its own <details> block.
    start = page.index('data-key="alchemy"')
    end = page.index("</details>", start)
    return page[start:end + len("</details>")]


def _ledger_rows(page: str, cells: int) -> list[list[tuple[str, str]]]:
    """The alchemy section's table rows with `cells` cells: 10 for a sale
    (Date, Composite, Qty, Unit price, Net, Cost / unit, Net income,
    Where, Run, Source), 8 for a Still held row (Composite, Run, Status,
    Planned, Released, Held, Expected, Until)."""
    return [
        tds for tds in (_tds(tr) for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", _ledger_section(page), flags=re.S))
        if len(tds) == cells
    ]


def _held_rows(page: str) -> dict[int, tuple[str, list[tuple[str, str]]]]:
    """The Ledger's Still held rows by run number: (the <tr>'s attributes
    — its class says whether the hold is live — and its 8 cells)."""
    rows = {}
    for attrs, tr in re.findall(r"<tr([^>]*)>(.*?)</tr>", _ledger_section(page), flags=re.S):
        tds = _tds(tr)
        if len(tds) == 8:
            rows[int(_text(tds[1][1]))] = (attrs, tds)
    return rows


def _badge_title(fragment: str):
    """The title of the first badge inside a cell's html."""
    m = re.search(r'<span class="badge[^"]*" title="([^"]*)"', fragment)
    return html_mod.unescape(m.group(1)) if m else None


def _flashes(page: str) -> str:
    """The page's flash messages joined with ' | ' (base.html renders each
    in its own <div class="flash">), so a 'no such clause' assertion reads
    the flashes alone and not a tooltip elsewhere on the page."""
    return " | ".join(
        _text(m) for m in re.findall(r'<div class="flash">(.*?)</div>', page, flags=re.S)
    )


def _execute_confirm(page: str) -> str:
    """The Mark executed form's data-confirm, unescaped."""
    m = re.search(r'action="/runs/\d+/complete"\s+data-confirm="([^"]*)"', page)
    assert m, "the Mark executed form"
    return html_mod.unescape(m.group(1))


def _squash(fragment: str) -> str:
    """`fragment` with every whitespace run one space — the templates wrap
    their prose over several lines."""
    return re.sub(r"\s+", " ", fragment)


def _for_sale_figures(page: str, w: World) -> dict:
    """The for-sale figures of `w`'s route on an Industry Jobs page: the
    Alchemy section row's 'for sale' badge title, its reprocess checklist
    line (tags stripped, whitespace squashed), the Sell in Jita row's Sell
    qty and Held, and the For sale stat's value (None when absent)."""
    unrefined = w.ref.type_info(w.unrefined).name
    [alchemy_row] = [
        tr for tr in re.findall(r"<tr>(.*?)</tr>", page, flags=re.S)
        if f"<td>{unrefined}</td>" in tr and 'class="badge accent"' in tr
    ]
    badge = re.search(r'<span class="badge accent" title="([^"]*)">for sale</span>', alchemy_row)
    li = re.search(rf"<li>Reprocess <b>[\d,]+ {re.escape(unrefined)}</b>.*?</li>", page, flags=re.S)
    row = _panel_rows(page)[w.name]
    stat = _stat(page, "For sale")
    return {
        "badge": html_mod.unescape(badge.group(1)) if badge else None,
        "checklist": _squash(_text(li.group(0))) if li else None,
        "sell_qty": row["sell_qty"],
        "held": row["held"],
        "stat": stat[0] if stat else None,
    }


_REFRESH_PRICES_FLASH = (
    "settings saved — refresh prices before the next plan: Alchemy for Profit prices every "
    "route it may run, and a route whose inputs or composite have no cached price is skipped "
    "until then"
)


def _save_settings(client, **over) -> str:
    """POST the Settings form — test_web_lifecycle's complete body with
    `over` applied (a checkbox posts only when ticked, so a key left out
    un-ticks it) — and return the flashes of the page it redirects to."""
    resp = client.post("/settings", data=_settings_form(**over), follow_redirects=True)
    assert resp.status_code == 200
    return _flashes(_u(resp.get_data(as_text=True)))


# --- installs through the update -------------------------------------------------


def test_installs_seen_by_the_update_anchor_the_hold_and_the_re_plan_lists_no_phantom_job(
    seeded_client, monkeypatch, ref,
):
    """Plan → the user installs the listed for-sale jobs → ⟳ Update: the
    hold is what ESI shows installed beyond the chain's share, the formula
    lists no job again (the pool and the cap count the installed ones),
    the Sell in Jita row, the For sale stat and the Total read the
    installed units, the Stockpile In jobs cell carries the credit share
    with the hold's end, nothing is delivered so Mark sold is not offered,
    and a second identical update changes nothing (the open run's hold
    never feeds back into its own re-plan). Then the cycle's Hulk wave
    installs too: the lines its finished chain frees take NEW for-sale
    jobs — the installed ones are never listed again, the hold is frozen
    and the Sell qty is the hold plus the new jobs' share."""
    w = _world(seeded_client, ref)
    assert w.profit_jobs == w.jobs > 0, "a pure for-sale row"
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.planned_qty == w.planned > 0
    assert (hold.baseline_units, hold.chain_units) == (0, 0)
    assert hold.for_sale_qty == 0 and not hold.live and hold.status == "not installed"
    page = w.get()
    row = _panel_rows(page)[w.name]
    assert (row["sell_qty"], row["released"], row["held"], row["status"]) == (w.planned, 0, 0, "not installed")
    assert f"{w.jobs} jobs to run now for profit" in row["sell_title"]
    assert w.sold_action not in page

    sc = Scenario()
    ids, end = _install_run(w, sc, hulls=False)
    flash = _refresh(seeded_client, monkeypatch, sc)
    assert "run 1 re-planned: " in flash and "not re-planned" not in flash
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.installed_units == w.jobs * w.per
    assert hold.for_sale_unrefined == w.sale_units
    assert hold.for_sale_qty == w.planned == hold.planned_qty
    assert hold.in_flight and hold.live and hold.delivered_at is None
    assert hold.status == "in flight" and hold.jobs_end == end

    def reactions_now(c) -> int:
        return c.execute(
            "SELECT COALESCE(SUM(install_jobs), 0) FROM index_run_item "
            "WHERE index_run_id = ? AND activity_id = ?",
            (w.run_id, config.ACTIVITY_REACTION),
        ).fetchone()[0]

    c = _state()
    item = _item(c, w.run_id, w.unrefined)
    assert item is None or (item["alchemy_profit_jobs"] or 0) == 0, "installed jobs are not listed again"
    assert reactions_now(c) + w.jobs <= POOL
    composite = _item(c, w.run_id, w.composite)
    assert composite["held_for_sale_qty"] == w.planned == composite["held_from_credit_qty"]
    shape = _shape(c, w.run_id)
    c.close()

    page = w.get()
    row = _panel_rows(page)[w.name]
    assert (row["sell_qty"], row["released"], row["held"], row["status"]) == (
        w.planned, 0, w.planned, "in flight"
    )
    assert f"{w.planned:,} from the for-sale jobs ESI shows installed" in row["sell_title"]
    until = (
        "until the run after the one its jobs deliver in is marked executed"
        f" — jobs end {end:%Y-%m-%d %H:%M}"
    )
    assert f"hidden from the plan's stock {until}" == row["held_title"]
    assert f"for-sale jobs are running — held {until}" in _u(row["flags"])
    assert _stat(page, "For sale")[0] == f"{w.planned:,}"
    assert _panel_total(page)[0] == w.planned
    assert w.sold_action not in page and "Mark sold" not in row["actions"]
    chain = _u(seeded_client.get(f"/runs/{w.run_id}?view=chain").get_data(as_text=True))
    assert (
        f"{w.planned:,} more units expected from the formula's unrefined stock or jobs held for sale "
        "(alchemy run for profit) are left out of On hand / In jobs and so stay in the "
        f"deficit, {until}"
    ) in chain
    assert "more units in the hangar held for sale" not in chain  # nothing came off the hangar

    _refresh(seeded_client, monkeypatch, sc)
    c = _state()
    assert _shape(c, w.run_id) == shape
    c.close()
    again = _hold(ref, w.run_id, w.composite)
    assert (again.for_sale_qty, again.chain_units, again.planned_qty, again.route_unit_cost) == (
        hold.for_sale_qty, hold.chain_units, hold.planned_qty, hold.route_unit_cost
    )

    # The Hulk wave installs: its chain builds back to target only, the
    # lines that frees take NEW for-sale jobs — never the installed ones.
    sc.install(w.hulk, w.start, 8, 1, first_job_id=80000, activity=config.ACTIVITY_MANUFACTURING)
    _refresh(seeded_client, monkeypatch, sc)
    c = _state()
    hulk = _item(c, w.run_id, w.hulk)
    assert hulk["installed_qty"] == 8 and hulk["runs_allocated"] == 0
    item = _item(c, w.run_id, w.unrefined)
    new_units = _sale_units_of(item)
    assert new_units > 0, "the installed wave frees lines for new for-sale jobs"
    assert reactions_now(c) + w.jobs <= POOL  # the installed jobs still hold their lines
    c.close()
    frozen = _hold(ref, w.run_id, w.composite)
    assert (frozen.for_sale_qty, frozen.chain_units, frozen.planned_qty, frozen.route_unit_cost) == (
        hold.for_sale_qty, hold.chain_units, hold.planned_qty, hold.route_unit_cost
    )
    new_qty = _expected(new_units, w.route)
    row = _panel_rows(w.get())[w.name]
    assert row["sell_qty"] == w.planned + new_qty and row["status"] == "in flight"
    assert (
        f"{w.planned:,} from the for-sale jobs ESI shows installed + {new_qty:,} from "
        f"{math.ceil(new_units / w.per)} job"
    ) in row["sell_title"]


def test_the_open_runs_list_row_names_the_alchemy_share_of_its_estimate(seeded_client, monkeypatch, ref):
    """The Index Runs list's open row: Planning → Profit's hull what-if
    plus the run's own hold at today's quote — 'incl. X ISK from N units'
    where N is what ESI shows installed, X its net at today's quote (less
    the route's cost on the Profit side)."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    net = w.net_quote()
    revenue = w.planned * net
    profit = w.planned * (net - w.route_unit)
    tr = _u(_runs_row(seeded_client.get("/runs").get_data(as_text=True), 1))
    assert ">est.</span>" in tr
    assert f"incl. {revenue:,.0f} ISK from {w.planned:,} units of alchemy run for sale" in tr
    assert f"incl. {profit:,.0f} ISK from alchemy run for sale, net of its route cost" in tr
    planning = seeded_client.get("/planning").get_data(as_text=True)
    hull_revenue = int(_strip_isk(planning, "Net proceeds / cycle").replace(",", ""))
    hull_profit = int(_strip_isk(planning, "Profit / cycle").replace(",", ""))
    assert abs(int(_title_isk(tr, 5).replace(",", "")) - hull_revenue - _whole(revenue)) <= 1
    assert abs(int(_title_isk(tr, 6).replace(",", "")) - hull_profit - _whole(profit)) <= 1


# --- delivery, the settle and the money figures ----------------------------------


def test_a_delivered_pile_sold_through_the_update_lands_on_every_page_to_the_isk(
    seeded_client, monkeypatch, ref,
):
    """Installs → the jobs deliver and a wallet sell of half the pile
    arrives in the same update → the settle matches it to run 1's hold.
    After Mark executed the Profit tab's row reads Planned = Sold + Still
    held, Realized net at the venue's fees, Est. net at today's quote,
    Cost = every unit × the frozen route cost and Profit = Realized + Est
    − Cost; the cycle totals fold the line in beside the 8 hulls ESI shows
    installed and name it; the Index Runs row equals the strip and names
    the share; the Ledger's row nets the same release to the same ISK."""
    w = _world(seeded_client, ref)
    s = _db_settings()
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    delivered = w.opened + timedelta(days=2)
    sc.deliver(ids, delivered, to_hangar={w.composite: w.planned})
    half, price = w.planned // 2, 900_000.0
    sc.sale(7001, w.composite, half, price, w.opened + timedelta(days=3))
    flash = _refresh(seeded_client, monkeypatch, sc)
    assert f"alchemy: {half:,} units sold matched against the for-sale holds" in flash
    assert _releases() == [(w.run_id, "market", 7001, half, price, w.route_unit)]
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.delivered_at == delivered and hold.status == "delivered" and hold.live
    assert (hold.observed_qty, hold.remaining) == (half, w.planned - half)

    flash = w.execute()
    assert "for-sale hold ended" not in flash  # it lives through the next run
    _refresh(seeded_client, monkeypatch, sc)  # the pull after Mark executed covers the window

    realized = _net_unit(ref, s, w.composite, price, STATION) * half
    est = (w.planned - half) * w.net_quote()
    cost = w.planned * w.route_unit
    profit = (realized + est) - cost
    tab = w.get("profit")
    row = _profit_row(tab, w.name)
    assert (row["planned"], row["sold"], row["held"]) == (w.planned, half, w.planned - half)
    assert "by hand" not in row["sold_text"]
    assert row["realized"] == _whole(realized)
    assert row["est"] == _whole(est) and f"{w.planned - half:,} still held × " in row["est_title"]
    assert row["cost"] == _whole(cost) and f"{w.planned:,} × " in row["cost_title"]
    assert row["profit"] == _whole(profit)
    assert "hidden from the plan's stock until run 2 is marked executed" == row["held_title"]
    assert "badge" not in row["flags"]
    strip_profit, strip_title = _stat(tab, "Profit / cycle")
    assert f"incl. {profit:,.0f} ISK from {w.planned:,} units of alchemy run for sale" in strip_title
    assert "of them returned to stock" not in strip_title
    assert _stat(tab, "Units / cycle")[0] == "8"
    assert _stat(tab, "Net proceeds / cycle")[1].startswith(f"{_strip_isk(tab, 'Net proceeds / cycle')} ISK — incl. {realized + est:,.0f} ISK")
    assert "the hulls whose jobs ESI shows started inside this run" in tab

    tr = _u(_runs_row(seeded_client.get("/runs").get_data(as_text=True), 1))
    assert _title_isk(tr, 5) == _strip_isk(tab, "Net proceeds / cycle")
    assert _title_isk(tr, 6) == _strip_isk(tab, "Profit / cycle")
    assert f"incl. {realized + est:,.0f} ISK from {w.planned:,} units of alchemy run for sale" in tr
    assert f"incl. {profit:,.0f} ISK from alchemy run for sale, net of its route cost" in tr
    assert "the hulls ESI shows started inside this cycle" in tr

    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    assert f"Alchemy sales <span class=\"muted\">— {half:,} units sold in this window · {w.planned - half:,} units still held" in page
    [sale] = _ledger_rows(page, 10)
    assert _text(sale[1][1]) == w.name and _num(sale[2][1]) == half
    assert _isk_of(_attr(sale[4][0], "title")) == _whole(realized) == row["realized"]
    assert _attr(sale[5][0], "title").startswith(f"{w.route_unit:,.2f} ISK — run 1's route cost per unit")
    assert _text(sale[8][1]) == "1" and "market transaction 7001" in sale[9][1]
    [held] = _ledger_rows(page, 8)
    assert (_num(held[3][1]), _num(held[4][1]), _num(held[5][1])) == (w.planned, half, w.planned - half)
    assert _text(held[7][1]) == "until run 2 is marked executed"


def test_two_runs_holding_one_composite_each_show_their_own_releases(seeded_client, monkeypatch, ref):
    """Run 1's pile sells whole; run 2 (opened after run 1's Mark
    executed) installs and delivers its own, and the next sale lands on
    run 2 because run 1 has no room left (oldest first, room = delivered
    less observed). Each Profit tab shows its own releases; the Ledger
    keys each sale to its run."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids1, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids1, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    sc.sale(7001, w.composite, w.planned, 900_000.0, w.opened + timedelta(days=3))
    assert f"alchemy: {w.planned:,} units sold matched" in _refresh(seeded_client, monkeypatch, sc)
    c1 = w.opened + timedelta(days=5)
    c = _state()
    complete_run(c, w.run_id, c1)  # by hand: the timeline needs a past completion
    c.close()

    run2 = _plan(seeded_client)
    c = _state()
    c.execute(  # run 2's world opened at c1 (in the past): its anchors move there too
        "UPDATE alchemy_hold SET anchored_at = ? WHERE index_run_id = ?", (store.utc_text(c1), run2),
    )
    c.commit()
    item2 = _item(c, run2, w.unrefined)
    c.close()
    assert item2 is not None and item2["alchemy_profit_jobs"] > 0, "run 2 lists for-sale jobs again"
    per2 = int(item2["max_runs_per_job"]) * int(item2["portion_size"] or 1)
    planned2 = _expected(int(item2["alchemy_profit_jobs"]) * per2, w.route)
    ids2, _end2 = sc.install(w.unrefined, c1 + timedelta(seconds=1), int(item2["jobs_allocated"]), per2,
                             first_job_id=92000)
    _refresh(seeded_client, monkeypatch, sc)
    hold1, hold2 = (_hold(ref, r, w.composite) for r in (w.run_id, run2))
    assert hold1.status == "sold" and hold1.remaining == 0
    assert hold2.for_sale_qty == planned2 and hold2.in_flight and hold2.live
    sc.deliver(ids2, c1 + timedelta(days=2), to_hangar={w.composite: planned2})
    third = planned2 // 3
    sc.sale(7002, w.composite, third, 950_000.0, c1 + timedelta(days=3))
    assert f"alchemy: {third:,} units sold matched" in _refresh(seeded_client, monkeypatch, sc)
    assert sorted(_releases()) == sorted([
        (w.run_id, "market", 7001, w.planned, 900_000.0, w.route_unit),
        (run2, "market", 7002, third, 950_000.0, item2["alchemy_route_unit_cost"]),
    ])

    flash = w.execute(run2)
    assert "for-sale hold ended" not in flash  # run 1's pile is sold: nothing returns to stock
    one = _profit_row(w.get("profit"), w.name)
    assert (one["planned"], one["sold"], one["held"]) == (w.planned, w.planned, 0)
    assert "returned to stock" not in one["flags"] and "nothing left to estimate" in one["est_title"]
    two = _profit_row(w.get("profit", run2), w.name)
    assert (two["planned"], two["sold"], two["held"]) == (planned2, third, planned2 - third)
    assert two["held_title"] == "hidden from the plan's stock until run 3 is marked executed"
    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    by_ref = {
        re.search(r"transaction (\d+)", tds[9][1]).group(1): _text(tds[8][1]) for tds in _ledger_rows(page, 10)
    }
    assert by_ref == {"7001": "1", "7002": "2"}


def test_a_pipeline_deleted_after_execution_leaves_the_alchemy_line_standing(seeded_client, monkeypatch, ref):
    """With the pipeline gone there is no hull to cost: the Index Runs
    list and the Profit tab still render, their figures the alchemy line
    alone."""
    w = _world(seeded_client, ref)
    s = _db_settings()
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    half, price = w.planned // 2, 900_000.0
    sc.sale(7001, w.composite, half, price, w.opened + timedelta(days=3))
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    c = _state()
    pipeline_id = c.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    c.close()
    assert seeded_client.post(f"/pipelines/{pipeline_id}/delete").status_code == 302

    proceeds = _net_unit(ref, s, w.composite, price, STATION) * half + (w.planned - half) * w.net_quote()
    profit = proceeds - w.planned * w.route_unit
    resp = seeded_client.get("/runs")
    assert resp.status_code == 200
    tr = _u(_runs_row(resp.get_data(as_text=True), 1))
    assert _title_isk(tr, 5) == f"{proceeds:,.0f}" and _title_isk(tr, 6) == f"{profit:,.0f}"
    assert "no hulls (nothing attributable)" in tr
    tab = w.get("profit")
    assert "No active pipeline is attributable to this run." in tab
    assert _profit_row(tab, w.name)["profit"] == _whole(profit)
    assert _stat(tab, "Units / cycle")[0] == "0"
    assert _strip_isk(tab, "Profit / cycle") == f"{profit:,.0f}"


# --- the hold's end --------------------------------------------------------------


def test_the_second_mark_executed_after_delivery_returns_the_remainder_to_stock(
    seeded_client, monkeypatch, ref,
):
    """Ruling (b) and (c) on the pages: run 1's pile delivers and half
    sells; Mark executed of run 1 keeps the hold (the confirm on run 2's
    page and run 2's Mark executed flash name what ends); after run 2 is
    executed the remainder is 'returned to stock' — estimated at nothing,
    its route cost kept, the strip saying how many — and Reopen of run 2
    revives it, said in its flash."""
    w = _world(seeded_client, ref)
    s = _db_settings()
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    half, price = w.planned // 2, 900_000.0
    sc.sale(7001, w.composite, half, price, w.opened + timedelta(days=3))
    _refresh(seeded_client, monkeypatch, sc)
    remaining = w.planned - half
    assert "for-sale hold ended" not in w.execute()
    run2 = _plan(seeded_client)
    page2 = _u(w.get(run_id=run2))
    assert (
        f"It also ends run 1's for-sale hold — {remaining:,} {w.name} unsold now count as stock."
    ) in page2
    flash = w.execute(run2)
    assert f"run 1's for-sale hold ended: {remaining:,} {w.name} unsold now count as stock" in flash

    realized = _net_unit(ref, s, w.composite, price, STATION) * half
    cost = w.planned * w.route_unit
    tab = w.get("profit")
    row = _profit_row(tab, w.name)
    assert ">returned to stock</span>" in row["flags"] and "no quote" not in row["flags"]
    assert row["est"] == 0 and f"nothing for the {remaining:,} returned to stock" in row["est_title"]
    assert row["held"] == remaining
    assert "the hold ended — these units count as ordinary stock again" in row["held_title"]
    assert row["cost"] == _whole(cost) and row["profit"] == _whole(realized - cost)
    assert f"; {remaining:,} of them returned to stock" in _stat(tab, "Profit / cycle")[1]
    tr = _u(_runs_row(seeded_client.get("/runs").get_data(as_text=True), 1))
    assert "alchemy unpriced" not in tr, tr
    assert _title_isk(tr, 6) == _strip_isk(tab, "Profit / cycle")
    assert f"incl. {realized - cost:,.0f} ISK from alchemy run for sale, net of its route cost" in tr
    panel = _panel_rows(w.get())[w.name]
    assert panel["status"] == "returned to stock" and panel["profit"] == "—"
    assert "this pile is returned to stock — what it earned is on the Profit tab" == panel["profit_title"]

    flash = _u(seeded_client.post(f"/runs/{run2}/reopen", follow_redirects=True).get_data(as_text=True))
    assert f"run 1's for-sale hold is live again: {remaining:,} {w.name} hidden from stock" in flash
    row = _profit_row(w.get("profit"), w.name)
    assert "returned to stock" not in row["flags"]
    assert row["est"] == _whole(remaining * w.net_quote())
    assert row["profit"] == _whole(realized + remaining * w.net_quote() - cost)


def test_mark_executed_before_an_update_says_the_runs_own_hold_stands_at_the_plans_figure(
    seeded_client, monkeypatch, ref,
):
    """Run 2 planned and executed with no ESI update between: the record
    cannot cover its window, so its proposal-only hold becomes a plan
    figure (D1) — the flash says so rather than claiming a revival."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    run2 = _plan(seeded_client)
    planned2 = _hold(ref, run2, w.composite).planned_qty
    assert planned2 > 0
    flash = w.execute(run2)
    assert f"run 1's for-sale hold ended: {w.planned:,} {w.name} unsold now count as stock" in flash
    assert "is live again" not in flash
    assert (
        f"run 2's for-sale hold stands at the plan's figure until the next ESI update: "
        f"{planned2:,} {w.name} hidden from stock"
    ) in flash
    hold2 = _hold(ref, run2, w.composite)
    assert hold2.plan_figure and hold2.live and hold2.for_sale_qty == planned2
    assert ">plan figure</span>" in _profit_row(w.get("profit", run2), w.name)["flags"]


def test_reopening_a_run_executed_before_an_update_withdraws_its_plan_figure_quietly(
    seeded_client, monkeypatch, ref,
):
    """The mirror of the Mark executed wording: reopening run 2 revives
    run 1's hold (said) and drops run 2's plan-figure stand-in, which was
    never a pile — no 'ended ... unsold now count as stock' clause for it."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    run2 = _plan(seeded_client)
    w.execute(run2)
    hold2 = _hold(ref, run2, w.composite)
    assert hold2.plan_figure
    flash = _u(seeded_client.post(f"/runs/{run2}/reopen", follow_redirects=True).get_data(as_text=True))
    assert f"run 1's for-sale hold is live again: {w.planned:,} {w.name} hidden from stock" in flash
    assert not _hold(ref, run2, w.composite).live
    assert "run 2's for-sale hold ended" not in flash
    assert (
        f"run 2's plan-figure hold withdrawn: {hold2.remaining:,} {w.name} count as stock "
        "again — the reopened run holds only what an ESI update shows installed"
    ) in flash


def test_the_mark_executed_confirm_and_flash_word_a_plan_figure_hold_apart(
    seeded_client, monkeypatch, ref,
):
    """Run 1 executed before any update stands at the plan's figure (D1)
    and ends with run 2. Run 2's Mark executed confirm names it as a
    plan-figure hold whose units 'count as stock again' — never 'unsold':
    nothing of it was a pile — at the plan's figure through the qty
    filter, and the flash says 'plan-figure hold ended' the same way,
    while run 2's own stand-in is announced as before."""
    w = _world(seeded_client, ref)
    w.execute()
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.plan_figure and hold.live and hold.expires_after == 2
    assert hold.remaining == w.planned  # nothing installed, released or sold: the whole figure
    run2 = _plan(seeded_client)
    planned2 = _hold(ref, run2, w.composite).planned_qty
    assert planned2 > 0
    confirm = _execute_confirm(w.get(run_id=run2))
    assert confirm.startswith("Mark run 2 executed? ")
    assert confirm.endswith(
        f" It also ends run 1's plan-figure hold — {w.planned:,} {w.name} count as stock again."
    )
    assert "unsold" not in confirm and "for-sale hold" not in confirm
    flash = _flashes(w.execute(run2))
    assert f"run 1's plan-figure hold ended: {w.planned:,} {w.name} count as stock again" in flash
    assert "unsold" not in flash and "for-sale hold ended" not in flash and "live again" not in flash
    assert (
        f"run 2's for-sale hold stands at the plan's figure until the next ESI update: "
        f"{planned2:,} {w.name} hidden from stock"
    ) in flash
    ended = _hold(ref, w.run_id, w.composite)
    assert not ended.live and ended.plan_figure and ended.remaining == w.planned
    assert holds.expiry_words(ended) == "ended — a newer run is executed"


def test_a_hold_in_flight_outlives_the_next_mark_executed_until_its_jobs_deliver(
    seeded_client, monkeypatch, ref,
):
    """Ruling (b) through the pages: run 1's for-sale jobs are still
    running when run 1 and then run 2 are marked executed — neither
    confirm nor flash ends run 1's hold, its panel row stays 'in flight',
    the Ledger's Still held row is live with the in-flight wording and the
    running jobs' end. The executed window the pull log does not cover is
    read from the record where that shows the jobs (PROJECT.md's
    refinement of D1): no plan figure. Once the record shows the delivery
    the count rule takes over — dated before both Mark executeds, it ends
    the hold at run 2's, as the panel then says."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    flash = _flashes(w.execute())
    assert "hold" not in flash, flash  # nothing ended, nothing revived, nothing standing in
    # Pin the timeline against the clock's seconds: run 1 completed a minute
    # ago and every pull so far two minutes ago, so run 2's window (c1, c2]
    # holds no pull until the update below, whose pull then covers it.
    c = _state()
    c.execute(
        "UPDATE index_run SET completed_at = datetime(completed_at, '-1 minute') WHERE index_run_id = ?",
        (w.run_id,),
    )
    c.execute("UPDATE industry_job_pull SET pulled_at = datetime(pulled_at, '-2 minutes')")
    c.commit()
    c.close()
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.run_status == "complete" and hold.in_flight and hold.live
    assert not hold.plan_figure and hold.for_sale_qty == w.planned
    assert (hold.expires_after, hold.expired_at, hold.delivered_at) == (None, None, None)
    run2 = _plan(seeded_client)
    assert "It also ends" not in _execute_confirm(w.get(run_id=run2))
    flash = _flashes(w.execute(run2))
    assert "for-sale hold ended" not in flash and "run 1's" not in flash, flash
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.in_flight and hold.live and hold.expires_after is None
    assert hold.remaining == w.planned and hold.jobs_end == end
    until = (
        "until the run after the one its jobs deliver in is marked executed"
        f" — jobs end {end:%Y-%m-%d %H:%M}"
    )
    assert holds.expiry_words(hold) == until
    row = _panel_rows(w.get())[w.name]
    assert (row["status"], row["released"], row["held"]) == ("in flight", 0, w.planned)
    assert row["held_title"] == f"hidden from the plan's stock {until}"
    assert w.sold_action not in w.get()
    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    attrs, held = _held_rows(page)[1]
    assert "superseded" not in attrs
    assert _text(held[2][1]) == "in flight" and "plan figure" not in _text(held[2][1])
    assert _badge_title(held[2][1]) == f"for-sale jobs are running — jobs end {end:%Y-%m-%d %H:%M}"
    assert (_num(held[3][1]), _num(held[4][1]), _num(held[5][1])) == (w.planned, 0, w.planned)
    assert _attr(held[5][0], "title") == "hidden from the plan's stock"
    assert _isk_of(_attr(held[6][0], "title")) == _whole((w.net_quote() - w.route_unit) * w.planned)
    assert _text(held[7][1]) == until
    assert _attr(held[7][0], "title") == (
        "a hold lives while its for-sale jobs run and for one full run after they deliver — "
        "the second Mark executed after delivery ends it"
    )
    # The stat sums the live holds: run 1's pile and run 2's own stand-in.
    standing = _hold(ref, run2, w.composite)
    assert standing.live and standing.plan_figure
    assert _stat(page, "Still held")[0] == f"{w.planned + standing.remaining:,}"

    # The next update shows the delivery, dated before both Mark executeds:
    # two executed runs completed at or after it, so the hold has had its
    # full run and ends at run 2's — the count rule, from the record's date.
    delivered = w.opened + timedelta(days=2)
    sc.deliver(ids, delivered, to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    c = _state()
    c2 = store._utc_datetime(
        c.execute("SELECT completed_at FROM index_run WHERE index_run_id = ?", (run2,)).fetchone()[0]
    )
    c.close()
    hold = _hold(ref, w.run_id, w.composite)
    assert (hold.in_flight, hold.delivered_at, hold.live) == (False, delivered, False)
    assert (hold.expires_after, hold.expired_at) == (2, c2)
    row = _panel_rows(w.get())[w.name]
    assert row["status"] == "returned to stock" and row["held"] == w.planned
    assert (
        f"the hold ended when run 2 was marked executed — the {w.planned:,} unsold count as "
        "ordinary stock; the Profit tab values them at nothing and keeps their route cost"
    ) == _badge_title(row["flags"])
    # Run 1's pile is stock again, and the update's pull covered run 2's
    # window showing nothing installed, so its stand-in is withdrawn too
    # (the record speaks): no live hold of the composite is left.
    assert not _hold(ref, run2, w.composite).live
    assert f"no live for-sale hold of {w.name} — nothing changed" in _flashes(w.sold(qty="1"))
    assert _releases() == []


def test_the_ledger_keeps_an_ended_holds_row_and_its_releases_as_history(
    seeded_client, monkeypatch, ref,
):
    """Ruling (c) in the Ledger's Still held table: run 1's pile delivers,
    half sells, run 1 and then run 2 are marked executed — run 2's
    confirm naming the real hold with its unsold units (the wording a
    plan figure never gets). The ended row is drawn superseded: Expected
    a dash whose title says the remainder carries no estimate, Until
    'ended when run 2 was marked executed (<run 2's completed_at>)' with
    the history title — and the matched sale stays in the sales table
    under run 1, out of the Still held stat, which counts live holds
    only (run 2's stand-in)."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    half = w.planned // 2
    sc.sale(7001, w.composite, half, 900_000.0, w.opened + timedelta(days=3))
    _refresh(seeded_client, monkeypatch, sc)
    remaining = w.planned - half
    w.execute()
    run2 = _plan(seeded_client)
    confirm = _execute_confirm(w.get(run_id=run2))
    assert confirm.endswith(
        f" It also ends run 1's for-sale hold — {remaining:,} {w.name} unsold now count as stock."
    )
    assert "plan-figure" not in confirm
    flash = _flashes(w.execute(run2))
    assert f"run 1's for-sale hold ended: {remaining:,} {w.name} unsold now count as stock" in flash
    c = _state()
    c2 = store._utc_datetime(
        c.execute("SELECT completed_at FROM index_run WHERE index_run_id = ?", (run2,)).fetchone()[0]
    )
    c.close()
    ended = _hold(ref, w.run_id, w.composite)
    assert (ended.live, ended.expires_after, ended.expired_at, ended.remaining) == (False, 2, c2, remaining)
    standing = _hold(ref, run2, w.composite)
    assert standing.live and standing.plan_figure and standing.remaining > 0

    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    rows = _held_rows(page)
    attrs, held = rows[1]
    assert 'class="superseded"' in attrs
    assert _text(held[2][1]) == "returned to stock"
    assert _badge_title(held[2][1]) == "the hold ended — the unsold units count as ordinary stock"
    assert (_num(held[3][1]), _num(held[4][1]), _num(held[5][1])) == (w.planned, half, remaining)
    assert _attr(held[5][0], "title") == "the hold ended — counts as ordinary stock"
    assert _text(held[6][1]) == "—"
    assert _attr(held[6][0], "title") == (
        "the hold ended — its unsold units count as ordinary stock and carry no estimate (ruling c)"
    )
    assert _text(held[7][1]) == f"ended when run 2 was marked executed ({c2:%Y-%m-%d %H:%M})"
    assert _attr(held[7][0], "title") == "this hold has ended — its releases stay as history"
    # Run 2's stand-in beside it: live, worded as a plan figure until run 3.
    attrs2, held2 = rows[2]
    assert "superseded" not in attrs2 and _text(held2[2][1]) == "plan figure"
    assert _text(held2[7][1]) == "until run 3 is marked executed (plan figure)"
    assert _stat(page, "Still held")[0] == f"{standing.remaining:,}"
    assert f"{half:,} units sold in this window · {standing.remaining:,} units still held" in page
    [sale] = _ledger_rows(page, 10)
    assert (_text(sale[1][1]), _num(sale[2][1]), _text(sale[8][1])) == (w.name, half, "1")
    assert "market transaction 7001" in sale[9][1]
    assert _releases() == [(w.run_id, "market", 7001, half, 900_000.0, w.route_unit)]


def test_an_unpriced_contract_release_is_valued_at_todays_quote_and_badged(seeded_client, monkeypatch, ref):
    """The whole pile goes in a bundle contract (the composite with
    Tritanium): the settle releases it without a price; the Profit tab
    values those units at today's quote inside Est. net and badges the
    row, the strip and the Index Runs row 'N alchemy unpriced'; Profit is
    the same before the sale, after it, and after the hold expires."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    net = w.net_quote()
    before = _profit_row(w.get("profit"), w.name)
    assert before["est"] == _whole(w.planned * net) and "unpriced" not in before["flags"]
    expected_profit = _whole(w.planned * net - w.planned * w.route_unit)
    assert before["profit"] == expected_profit

    c = _state()
    _seed_contract(
        c, 5001, [(w.composite, w.planned), (TRITANIUM, 100)], 1e9,
        issued=w.opened + timedelta(days=3), completed=w.opened + timedelta(days=4),
    )
    c.close()
    flash = _refresh(seeded_client, monkeypatch, sc)
    assert f"alchemy: {w.planned:,} units sold matched" in flash
    assert _releases() == [(w.run_id, "contract", 5001, w.planned, None, w.route_unit)]
    tab = w.get("profit")
    row = _profit_row(tab, w.name)
    assert (row["sold"], row["held"], row["realized"]) == (w.planned, 0, 0)
    assert f">{w.planned:,} unpriced</span>" in row["flags"]
    assert (
        f"{w.planned:,} units sold without an attributable price × the same quote = "
        f"{w.planned * net:,.0f} ISK"
    ) in row["est_title"]
    assert row["est"] == _whole(w.planned * net) and row["profit"] == expected_profit
    assert ">1 alchemy unpriced</span>" in tab
    tr = _u(_runs_row(seeded_client.get("/runs").get_data(as_text=True), 1))
    assert ">1 alchemy unpriced</span>" in tr
    assert _title_isk(tr, 6) == _strip_isk(tab, "Profit / cycle")
    assert f"incl. {expected_profit:,} ISK from alchemy run for sale, net of its route cost" in tr
    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    [sale] = _ledger_rows(page, 10)
    assert _text(sale[3][1]) == "—" and "not attributable (bundled or swap contract)" in _attr(sale[3][0], "title")
    assert "item-exchange contract 5001" in sale[9][1]
    assert f">1 unpriced · {w.planned:,} units</span>" in page

    run2 = _plan(seeded_client)
    w.execute(run2)  # the second Mark executed after delivery: the hold expires
    assert not _hold(ref, w.run_id, w.composite).live
    after = _profit_row(w.get("profit"), w.name)
    assert after["profit"] == expected_profit and after["est"] == _whole(w.planned * net)
    assert "returned to stock" not in after["flags"] and f">{w.planned:,} unpriced</span>" in after["flags"]


def test_a_live_hold_without_a_quote_leaves_its_unsold_cost_out_and_badges(seeded_client, monkeypatch, ref):
    """No cached Jita quote for the composite (review #17): the unsold
    units are left out of Est. net AND their route cost out of Cost, so a
    missing quote never reads as a loss; the row, the strip and the list
    badge it."""
    w = _world(seeded_client, ref)
    s = _db_settings()
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    half, price = w.planned // 2, 900_000.0
    sc.sale(7001, w.composite, half, price, w.opened + timedelta(days=3))
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    c = _state()
    c.execute("DELETE FROM market_price WHERE type_id = ?", (w.composite,))
    c.commit()
    c.close()
    realized = _net_unit(ref, s, w.composite, price, STATION) * half
    tab = w.get("profit")
    row = _profit_row(tab, w.name)
    assert ">no quote</span>" in row["flags"]
    assert row["est_text"] == "—" and row["est"] is None
    assert f"the {w.planned - half:,} still held are left out of Est. net and their route cost out of Cost" in row["est_title"]
    assert row["cost"] == _whole(half * w.route_unit)
    assert f"{w.planned - half:,} still held without a quote left out" in row["cost_title"]
    assert row["profit"] == _whole(realized - half * w.route_unit)
    assert ">1 alchemy unpriced</span>" in tab
    assert ">1 alchemy unpriced</span>" in _runs_row(seeded_client.get("/runs").get_data(as_text=True), 1)
    # The Ledger's Still held row of a LIVE hold without a quote keeps its
    # own dash wording (an ended hold's is ruling c's), and the stat
    # counts the composite at 0.
    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    attrs, held = _held_rows(page)[1]
    assert "superseded" not in attrs and _text(held[2][1]) == "delivered"
    assert (_num(held[3][1]), _num(held[4][1]), _num(held[5][1])) == (w.planned, half, w.planned - half)
    assert _text(held[6][1]) == "—" and _attr(held[6][0], "title") == "no Jita quote cached"
    assert _text(held[7][1]) == "until run 2 is marked executed"
    value, title = _stat(page, "Still held")
    assert value == f"{w.planned - half:,}"
    assert title.startswith("0 ISK expected: units of live holds not yet sold")
    assert title.endswith("(1 composite(s) without a quote count 0)")


def test_by_hand_marks_with_and_without_a_price_book_as_ruled(seeded_client, monkeypatch, ref):
    """Ruling (d): a priced mark is revenue as entered (no fee or freight
    term) on the Profit tab and in the Ledger alike; an unpriced one frees
    the stock and books neither revenue nor cost. Planned = Sold + By hand
    + Still held."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    flash = w.sold(qty="10", price="950000")
    assert (
        f"{w.name}: 10 marked sold — run 1's hold (booked as revenue at 950,000.00 ISK per unit, "
        "as entered — no fees deducted)"
    ) in flash
    flash = w.sold(qty="7")
    assert f"{w.name}: 7 marked sold — run 1's hold (no price: these units book neither revenue nor cost)" in flash
    assert _releases() == [
        (w.run_id, "manual", None, 10, 950_000.0, w.route_unit),
        (w.run_id, "manual", None, 7, None, w.route_unit),
    ]
    net = w.net_quote()
    tab = w.get("profit")
    row = _profit_row(tab, w.name)
    assert row["sold"] == 0 and "+ 17 by hand" in row["sold_text"]
    assert "10 with a price (revenue as entered), 7 without (neither revenue nor cost)" in row["sold_title"]
    assert row["realized"] == 9_500_000
    assert "plus 10 units marked sold by hand at the price entered (no fees deducted)" in row["realized_title"]
    assert row["held"] == w.planned - 17 and row["planned"] == row["sold"] + 17 + row["held"]
    assert row["est"] == _whole((w.planned - 17) * net)
    assert row["cost"] == _whole((w.planned - 7) * w.route_unit)
    assert "7 marked sold without a price left out" in row["cost_title"]
    assert row["profit"] == _whole(9_500_000 + (w.planned - 17) * net - (w.planned - 7) * w.route_unit)

    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    assert (
        "— 10 units sold in this window (10 of them marked by hand at a price) "
        "(+ 7 marked by hand without a price)"
    ) in page
    rows = {_num(tds[2][1]): tds for tds in _ledger_rows(page, 10)}
    priced, unpriced = rows[10], rows[7]
    assert _attr(priced[4][0], "title") == "9,500,000 ISK — the price entered × qty, no fees deducted"
    assert "as entered — ISK received per unit, no fees deducted" in _attr(priced[3][0], "title")
    assert _text(priced[7][1]) == "—" and "a by-hand mark has no place" in priced[7][1]
    assert ">by hand</span>" in priced[9][1]
    assert _text(unpriced[4][1]) == "—" and _text(unpriced[6][1]) == "—"
    assert "no price entered — neither revenue nor cost" in _attr(unpriced[6][0], "title")
    assert re.search(r'<tr class="superseded">\s*<td[^>]*>[^<]*</td>\s*<td>' + re.escape(w.name), page)
    # The run page lists both marks with an undo each.
    run_page = _u(w.get())
    assert f"10 {w.name} on run 1's hold" in run_page and "950,000.00 ISK per unit as entered" in run_page
    assert f"7 {w.name} on run 1's hold" in run_page and "no price (neither revenue nor cost)" in run_page
    assert run_page.count("/alchemy/release/") == 2


def test_the_ledger_heading_and_its_units_sold_stat_count_each_kind_of_release_apart(
    seeded_client, monkeypatch, ref,
):
    """ledger.alchemy_sales_view's totals as ledger.html words them
    (2026-10-31): with observed sales alone the heading reads '— N units
    sold in this window · H units still held' and the Units sold stat's
    title is the bare one — neither parenthesis. Then a bundle contract
    without an attributable price joins the units sold (and the title's
    unpriced clause) but neither parenthesis; a by-hand mark WITH a price
    joins the units sold, names itself in the first parenthesis and in the
    stat's title; one WITHOUT a price is only the second parenthesis and
    the stat's sub-line — not a unit sold, not an unpriced sale."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    sc.sale(7001, w.composite, 100, 900_000.0, w.opened + timedelta(days=3))
    assert "alchemy: 100 units sold matched" in _refresh(seeded_client, monkeypatch, sc)
    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    section = _ledger_section(page)
    assert f"— 100 units sold in this window · {w.planned - 100:,} units still held</span>" in section
    assert _stat(section, "Units sold") == ("100", "market and contract sales matched to the holds")
    assert "marked by hand" not in section and "by hand, no price" not in section
    assert "unpriced ·" not in section

    c = _state()
    _seed_contract(
        c, 5001, [(w.composite, 50), (TRITANIUM, 100)], 1e9,
        issued=w.opened + timedelta(days=4), completed=w.opened + timedelta(days=5),
    )
    c.close()
    assert "alchemy: 50 units sold matched" in _refresh(seeded_client, monkeypatch, sc)
    assert f"{w.name}: 10 marked sold — run 1's hold (booked as revenue" in w.sold(qty="10", price="950000")
    assert f"{w.name}: 7 marked sold — run 1's hold (no price" in w.sold(qty="7")
    assert _releases() == [
        (w.run_id, "market", 7001, 100, 900_000.0, w.route_unit),
        (w.run_id, "contract", 5001, 50, None, w.route_unit),
        (w.run_id, "manual", None, 10, 950_000.0, w.route_unit),
        (w.run_id, "manual", None, 7, None, w.route_unit),
    ]
    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    section = _ledger_section(page)
    assert (
        "— 160 units sold in this window (10 of them marked by hand at a price) "
        f"(+ 7 marked by hand without a price) · {w.planned - 167:,} units still held</span>"
    ) in section
    assert _stat(section, "Units sold") == (
        "160",
        "market and contract sales matched to the holds, plus 10 marked by hand at a price "
        "(the Profit tab counts those apart from Sold); 50 of them sold by contract without "
        "an attributable price",
    )
    assert '<span class="sub">+ 7 by hand, no price</span>' in section
    assert ">1 unpriced · 50 units</span>" in section


def test_an_executed_window_the_record_does_not_cover_shows_the_plans_figure(seeded_client, monkeypatch, ref):
    """Mark executed before any ESI update covered the window: the plan's
    proposal stands as the hold's figure, badged 'plan figure' on the
    Profit tab and the Sell in Jita panel (as hull_cost does for hulls),
    until a pull covers the window and shows nothing installed."""
    w = _world(seeded_client, ref)
    w.execute()
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.plan_figure and hold.for_sale_qty == w.planned and hold.live
    tab = w.get("profit")
    row = _profit_row(tab, w.name)
    assert row["planned"] == w.planned and ">plan figure</span>" in row["flags"]
    assert row["planned_title"].startswith("the plan's figure: the ESI job record does not cover")
    assert "Units are the plan's started figure" in tab
    panel = _panel_rows(w.get())[w.name]
    assert panel["sell_qty"] == w.planned and ">plan figure</span>" in panel["flags"]
    assert "the plan's figure — the job record does not cover this run's buying window" in panel["sell_title"]
    tr = _runs_row(seeded_client.get("/runs").get_data(as_text=True), 1)
    assert "the hulls the plan started (the ESI job record does not cover this cycle)" in tr
    assert f"from {w.planned:,} units of alchemy run for sale" in _u(tr)
    # The next update covers the window: nothing of the proposal was installed.
    _refresh(seeded_client, monkeypatch, Scenario())
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.covered and not hold.plan_figure and hold.for_sale_qty == 0 and not hold.live
    assert 'data-key="alchemy-sale"' not in w.get("profit")


# --- Mark sold --------------------------------------------------------------------


def test_mark_sold_spreads_over_the_oldest_delivered_hold_first_and_stores_the_price(
    seeded_client, monkeypatch, ref,
):
    """Two delivered live holds (run 1 executed, run 2 open): a Mark sold
    posted from run 2's page comes off run 1 first, the rest off run 2 —
    one 'manual' row per hold with the entered price and each hold's
    route cost; the form's max is the total room and its sub-text names
    run 1; undo removes a mark, never an observed sale."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids1, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids1, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    sc.sale(7001, w.composite, 100, 900_000.0, w.opened + timedelta(days=3))
    _refresh(seeded_client, monkeypatch, sc)
    c1 = w.opened + timedelta(days=5)
    c = _state()
    complete_run(c, w.run_id, c1)
    c.close()
    run2 = _plan(seeded_client)
    c = _state()
    c.execute(  # run 2's world opened at c1 (in the past): its anchors move there too
        "UPDATE alchemy_hold SET anchored_at = ? WHERE index_run_id = ?", (store.utc_text(c1), run2),
    )
    c.commit()
    item2 = _item(c, run2, w.unrefined)
    c.close()
    assert item2 is not None and int(item2["alchemy_profit_jobs"]) > 0, "run 2's cycle starts fresh under the cap"
    per2 = int(item2["max_runs_per_job"]) * int(item2["portion_size"] or 1)
    planned2 = _expected(int(item2["alchemy_profit_jobs"]) * per2, w.route)
    ids2, _end2 = sc.install(w.unrefined, c1 + timedelta(seconds=1), int(item2["jobs_allocated"]), per2,
                             first_job_id=92000)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids2, c1 + timedelta(days=2), to_hangar={w.composite: planned2})
    _refresh(seeded_client, monkeypatch, sc)
    r1 = w.planned - 100
    page = w.get(run_id=run2)
    row = _panel_rows(page)[w.name]
    assert f'max="{r1 + planned2}"' in row["actions"]
    assert "marks run 1's delivered units" in _u(row["actions"])
    assert (
        f"Mark {w.name} sold by hand? The units come off run 1 then run 2's delivered hold, "
        "oldest first, and the next plan counts them as stock. A blank price books neither "
        "revenue nor cost; a price books revenue at that ISK per unit, no fees deducted."
    ) in _u(row["actions"])

    flash = w.sold(run_id=run2, qty=str(r1 + 5), price="1234.5")
    assert f"{w.name}: {r1:,} marked sold — run 1's hold; 5 marked sold — run 2's hold" in flash
    assert "booked as revenue at 1,234.50 ISK per unit" in flash
    cost2 = item2["alchemy_route_unit_cost"]
    assert _releases() == [
        (w.run_id, "market", 7001, 100, 900_000.0, w.route_unit),
        (w.run_id, "manual", None, r1, 1234.5, w.route_unit),
        (run2, "manual", None, 5, 1234.5, cost2),
    ]
    assert _hold(ref, w.run_id, w.composite).remaining == 0
    assert _hold(ref, run2, w.composite).remaining == planned2 - 5

    c = _state()
    market_id, mark_id = [
        r["release_id"] for r in c.execute(
            "SELECT release_id FROM alchemy_hold_release WHERE index_run_id = ? ORDER BY release_id",
            (w.run_id,),
        )
    ]
    c.close()
    flash = _u(seeded_client.post(f"/runs/{run2}/alchemy/release/{market_id}/undo", follow_redirects=True).get_data(as_text=True))
    assert "only a by-hand mark can be undone — an observed sale stays matched" in flash
    flash = _u(seeded_client.post(f"/runs/{run2}/alchemy/release/{mark_id}/undo", follow_redirects=True).get_data(as_text=True))
    assert f"{w.name}: the by-hand mark of {r1:,} on run 1's hold is undone — {r1:,} held for sale again" in flash
    assert [x for x in _releases() if x[0] == w.run_id] == [(w.run_id, "market", 7001, 100, 900_000.0, w.route_unit)]
    flash = _u(seeded_client.post(f"/runs/{run2}/alchemy/release/{mark_id}/undo", follow_redirects=True).get_data(as_text=True))
    assert "that by-hand mark is already gone — nothing changed" in flash


def test_mark_sold_refusals_follow_what_the_record_shows(seeded_client, monkeypatch, ref):
    """Nothing installed → no live hold; in flight → nothing delivered yet
    (and no form on the page); delivered → a missing or bad number, a
    negative price, a quantity beyond the room, then nothing left; an
    unknown composite redirects with a flash; an unknown run is a 404."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    assert f"no live for-sale hold of {w.name} — nothing changed" in w.sold(qty="5")
    sc = Scenario()
    ids, end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    assert f"{w.name}: nothing delivered to mark sold yet — run 1's jobs end {end:%Y-%m-%d %H:%M}" in w.sold(qty="5")
    assert w.sold_action not in w.get()
    assert _releases() == []

    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    assert w.sold_action in w.get()
    assert f"{w.name}: enter at least 1 — {w.planned:,} still held" in w.sold()
    assert f"{w.name}: enter at least 1 — {w.planned:,} still held" in w.sold(qty="0")
    assert f"{w.name}: invalid number in qty or price — nothing changed" in w.sold(qty="x")
    assert f"{w.name}: invalid number in qty or price — nothing changed" in w.sold(qty="5", price="lots")
    assert f"{w.name}: the price is ISK received per unit, 0 or more — nothing changed" in w.sold(qty="5", price="-5")
    assert _releases() == []
    flash = w.sold(qty="10000000")
    assert f"{w.name}: {w.planned:,} marked sold — run 1's hold; {10_000_000 - w.planned:,} not applied: nothing else delivered" in flash
    assert _hold(ref, w.run_id, w.composite).remaining == 0
    assert f"{w.name}: nothing left to mark sold — every delivered unit is accounted for" in w.sold(qty="1")
    resp = seeded_client.post(f"/runs/{w.run_id}/alchemy/999999/sold", data={"qty": "1"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/runs/{w.run_id}")
    assert "no live for-sale hold of type 999999 — nothing changed" in w.sold(composite_id=999999, qty="1")
    assert seeded_client.post(f"/runs/999/alchemy/{w.composite}/sold", data={"qty": "1"}).status_code == 404


def test_mark_sold_refuses_an_expired_hold(seeded_client, monkeypatch, ref):
    """An expired hold's units are stock again: no mark, no form, and its
    earlier marks cannot be undone."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    assert f"{w.name}: 10 marked sold — run 1's hold" in w.sold(qty="10")
    # The tail switched off before run 2: it proposes nothing, so run 1's
    # hold is the only one and its expiry is what the refusal reads.
    c = _state()
    c.execute("UPDATE settings SET alchemy_for_profit = 0")
    c.commit()
    c.close()
    run2 = _plan(seeded_client)
    c = _state()
    assert store.hold_rows(c, run2) == []
    c.close()
    w.execute(run2)
    assert not _hold(ref, w.run_id, w.composite).live
    assert f"no live for-sale hold of {w.name} — nothing changed" in w.sold(qty="1")
    assert w.sold_action not in w.get()
    [(_run, _source, _ref, _qty, _price, _cost)] = _releases()
    c = _state()
    mark_id = c.execute("SELECT release_id FROM alchemy_hold_release").fetchone()[0]
    c.close()
    flash = _u(seeded_client.post(f"/runs/{w.run_id}/alchemy/release/{mark_id}/undo", follow_redirects=True).get_data(as_text=True))
    assert "that hold is no longer live — its by-hand mark stays as history; nothing changed" in flash
    assert len(_releases()) == 1


def test_mark_sold_refuses_a_superseded_runs_hold(seeded_client, monkeypatch, ref):
    """A superseded plan (a bare newer run exists) has no buying window:
    its hold is read as nothing, so Mark sold is refused and its page
    offers no form — even with its for-sale jobs delivered."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    assert _hold(ref, w.run_id, w.composite).live and w.sold_action in w.get()
    c = _state()
    _newer_run(c, 2)
    c.close()
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.superseded and not hold.live and hold.for_sale_qty == 0
    assert f"no live for-sale hold of {w.name} — nothing changed" in w.sold(qty="1")
    assert _releases() == []
    page = w.get()
    assert ">superseded</span>" in page and w.sold_action not in page


def test_mark_sold_refuses_a_hold_standing_at_the_plans_figure(seeded_client, monkeypatch, ref):
    """D4's refusal for a stand-in: run 1's pile delivered and its hold
    ended by run 2's Mark executed, run 2 executed before any update
    covered its window — the composite's one live hold is run 2's plan
    figure, nothing of it installed. No page offers a Mark sold form and a
    POST from either run's page is refused with the 'stands at the plan's
    figure' wording — not 'no live hold' (one is live), not 'nothing
    delivered' (no job is running)."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    run2 = _plan(seeded_client)
    w.execute(run2)
    hold1, hold2 = (_hold(ref, r, w.composite) for r in (w.run_id, run2))
    assert not hold1.live and hold1.delivered_at is not None and hold1.remaining == w.planned
    assert hold2.live and hold2.plan_figure and not hold2.installed and hold2.remaining > 0
    refusal = (
        f"{w.name}: run 2's hold stands at the plan's figure — nothing to mark sold until an "
        "ESI update shows its jobs delivered"
    )
    assert refusal in _flashes(w.sold(run_id=run2, qty="5"))
    assert refusal in _flashes(w.sold(qty="5"))  # whichever run's page posted it
    assert _releases() == []
    for run_id in (w.run_id, run2):
        page = w.get(run_id=run_id)
        assert f"/runs/{run_id}/alchemy/{w.composite}/sold" not in page
        assert "Mark sold" not in _panel_rows(page)[w.name]["actions"]
    assert _panel_rows(w.get(run_id=run2))[w.name]["status"] == "plan figure"


def test_mark_sold_waits_for_the_last_of_staggered_batches(seeded_client, monkeypatch, ref):
    """D1: delivered_at is set only once no for-sale job is in flight. The
    update shows one of run 1's for-sale jobs delivered while the rest
    run: the hold is 'in flight' (its delivered part is the settle's
    business, not Mark sold's), the page offers no form and a POST is
    refused with the 'still delivering' wording and the running jobs'
    end; once the last job lands Mark sold applies as usual, the price
    kept."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    assert w.jobs >= 2, "staggered batches need two for-sale jobs"
    sc = Scenario()
    ids, end = _install_run(w, sc, hulls=False)
    first, rest = ids[:1], ids[1:]
    first_landed = w.opened + timedelta(days=2)
    per_job = _expected(w.per, w.route)
    sc.deliver(first, first_landed, to_hangar={w.composite: per_job})
    _refresh(seeded_client, monkeypatch, sc)
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.for_sale_qty == w.planned and hold.in_flight and hold.live
    assert (hold.delivered_units, hold.delivered_qty) == (w.per, per_job)
    assert (hold.first_delivered, hold.delivered_at, hold.jobs_end) == (first_landed, None, end)
    assert hold.status == "in flight" and hold.expires_after is None
    page = w.get()
    row = _panel_rows(page)[w.name]
    assert (row["status"], row["released"], row["held"]) == ("in flight", 0, w.planned)
    assert w.sold_action not in page
    assert (
        f"{w.name}: run 1's for-sale jobs are still delivering — mark sold once the last one "
        f"lands (run 1's jobs end {end:%Y-%m-%d %H:%M})"
    ) in _flashes(w.sold(qty="5"))
    assert _releases() == []

    last_landed = w.opened + timedelta(days=3)
    sc.deliver(rest, last_landed, to_hangar={w.composite: w.planned - per_job})
    _refresh(seeded_client, monkeypatch, sc)
    hold = _hold(ref, w.run_id, w.composite)
    assert (hold.in_flight, hold.first_delivered, hold.delivered_at) == (False, first_landed, last_landed)
    assert hold.status == "delivered" and hold.delivered_qty == w.planned and hold.expires_after == 2
    assert w.sold_action in w.get()
    assert (
        f"{w.name}: 10 marked sold — run 1's hold (booked as revenue at 950,000.00 ISK per unit, "
        "as entered — no fees deducted)"
    ) in _flashes(w.sold(qty="10", price="950000"))
    assert _releases() == [(w.run_id, "manual", None, 10, 950_000.0, w.route_unit)]
    assert _hold(ref, w.run_id, w.composite).remaining == w.planned - 10
    assert _panel_rows(w.get())[w.name]["released"] == 10


# --- the Sell in Jita panel through the states ------------------------------------


def test_the_sell_in_jita_panel_reads_released_held_and_status_from_the_record(
    seeded_client, monkeypatch, ref,
):
    """Delivered with half sold: Released / Held / status 'delivered', the
    Total the whole pile's Sell qty while the For sale stat and the expected
    profit are the unsold remainder's (the active quantity, review
    2026-10-31); every unit marked sold: status 'sold', Expected profit a
    dash pointing at the Ledger (the Profit tab activates at Mark executed
    — this run is open), the stat gone and the Total 0."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    delivered = w.opened + timedelta(days=2)
    sc.deliver(ids, delivered, to_hangar={w.composite: w.planned})
    half = w.planned // 2
    sc.sale(7001, w.composite, half, 900_000.0, w.opened + timedelta(days=3))
    _refresh(seeded_client, monkeypatch, sc)
    page = w.get()
    row = _panel_rows(page)[w.name]
    assert (row["sell_qty"], row["released"], row["held"], row["status"]) == (
        w.planned, half, w.planned - half, "delivered"
    )
    assert f"{half:,} matched from market sales and item-exchange contracts" == row["released_title"]
    assert row["held_title"] == "hidden from the plan's stock until run 2 is marked executed"
    assert f"every for-sale job has delivered ({delivered:%Y-%m-%d %H:%M}) — held until run 2 is marked executed" in _u(row["flags"])
    profit = (w.net_quote() - w.route_unit) * (w.planned - half)  # the unsold remainder
    assert _isk_of(row["profit_title"]) == _whole(profit) and "at today's quote" in row["profit_title"]
    assert f"× {w.planned - half:,} units" in row["profit_title"]
    assert _stat(page, "For sale")[0] == f"{w.planned - half:,}"
    assert _panel_total(page) == (w.planned, f"{profit:,.0f} ISK")

    w.sold(qty=str(w.planned - half))
    page = w.get()
    row = _panel_rows(page)[w.name]
    assert (row["released"], row["held"], row["status"], row["profit"]) == (w.planned, 0, "sold", "—")
    assert f"{half:,} matched from market sales and item-exchange contracts + {w.planned - half:,} marked sold by hand (0 with a price, {w.planned - half:,} without)" == row["released_title"]
    assert row["profit_title"] == (
        "this pile is sold — what it earned is in the Ledger's Alchemy sales "
        "(the Profit tab activates at Mark executed)"
    )
    assert _stat(page, "For sale") is None
    assert _panel_total(page) == (0, "0 ISK")


def test_a_mixed_row_renders_its_savings_and_its_profit_together(seeded_client, ref):
    """The target's rare goo priced at a fortune and its composite quoted
    higher still: the route wins savings swaps AND for-sale jobs on one
    formula row — the Savings / profit cell's title ends in the total,
    the badge counts the for-sale jobs out of the row's, the hold's chain
    share is the savings jobs' units and the Sell in Jita row the rest."""
    w = _world(seeded_client, ref, savings=True, quote=1e7)
    assert 0 < w.profit_jobs < w.jobs, "a mixed row"
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.chain_units == (w.jobs - w.profit_jobs) * w.per and hold.baseline_units == 0
    c = _state()
    item = _item(c, w.run_id, w.unrefined)
    c.close()
    savings_units = (w.jobs - w.profit_jobs) * w.per
    install_runs = item["install_runs"]
    sale_units = (
        w.sale_units if install_runs is None
        else min(w.sale_units, max(0, int(install_runs) * int(item["portion_size"] or 1) - savings_units))
    )
    page = w.get()
    assert "Savings / profit" in page
    unrefined = ref.type_info(w.unrefined).name
    start = page.index(f"<td>{unrefined}</td>")
    alchemy_row = _u(page[page.rindex("<tr", 0, start):page.index("</tr>", start)])
    badge = re.search(r"(\d+) of (\d+) jobs? run for profit", alchemy_row)
    assert badge, alchemy_row
    for_sale, of = int(badge.group(1)), int(badge.group(2))
    assert 1 <= for_sale < of and for_sale <= w.profit_jobs and of <= w.jobs
    assert f"~{_expected(sale_units, w.route):,} {w.name} to sell in Jita after the reprocess" in alchemy_row
    assert re.search(r"= [\d,]+ profit = [\d,]+ ISK in all", alchemy_row)
    assert "plus " in alchemy_row and " units still to sell: " in alchemy_row
    row = _panel_rows(page)[w.name]
    assert row["sell_qty"] == _expected(sale_units, w.route) and row["status"] == "not installed"
    assert "the row's other jobs still are" in _u(page)


# --- the plan-figure row, the frozen yield and the wording ---------------------------


def test_a_live_plan_figure_row_is_this_cycles_in_the_for_sale_stat_and_the_total(
    seeded_client, ref,
):
    """web._FOR_SALE_COUNTED includes 'plan figure' (2026-10-31): run 1
    executed before any update stands at the plan's figure and is live,
    so its Sell in Jita row is this cycle's — the For sale stat reads the
    hold's units and the Total row sums them (both read from the page,
    the units computed from the plan's row); its Expected profit is the
    plan's figure — the formula row carries its plan-time net, so the
    title never says 'at today's quote' and a quote change afterwards
    moves nothing; the status badge's title says the record does not
    cover the window yet and how long the stand-in lives."""
    w = _world(seeded_client, ref)
    c = _state()
    plan_net = _item(c, w.run_id, w.unrefined)["alchemy_sale_net_unit"]
    c.close()
    assert plan_net == pytest.approx(w.net_quote())  # the plan's net is the class rule at the quote
    profit = (plan_net - w.route_unit) * w.planned
    w.execute()
    hold = _hold(ref, w.run_id, w.composite)
    assert hold.plan_figure and hold.live and hold.status == "plan figure"
    assert hold.remaining == w.planned

    page = w.get()
    row = _panel_rows(page)[w.name]
    assert (row["sell_qty"], row["released"], row["held"], row["status"]) == (
        w.planned, 0, w.planned, "plan figure"
    )
    assert row["held_title"] == "hidden from the plan's stock until run 2 is marked executed (plan figure)"
    assert _badge_title(row["flags"]) == (
        "the ESI job record does not cover this run's buying window yet — the plan's figure "
        "stands in until the next ESI update; held until run 2 is marked executed (plan figure)"
    )
    assert row["profit"] != "—" and _isk_of(row["profit_title"]) == _whole(profit)
    assert "at today's quote" not in row["profit_title"]
    assert row["profit_title"].endswith(f"ISK per {w.name}, × {w.planned:,} units still to sell")
    value, title = _stat(page, "For sale")
    assert value == f"{w.planned:,}"
    assert title.startswith(f"{w.planned:,} composite units still to sell: ")
    assert f"sell in Jita for ~{profit:,.0f} ISK over the route's cost" in title
    assert _panel_total(page) == (w.planned, f"{profit:,.0f} ISK")

    # The quote moves after the fact: the row's plan-time net stands.
    c = _state()
    seed_quote(c, w.composite, 1.3e6)
    c.close()
    assert abs(w.net_quote() - plan_net) > 1
    page = w.get()
    assert _isk_of(_panel_rows(page)[w.name]["profit_title"]) == _whole(profit)
    assert _stat(page, "For sale")[0] == f"{w.planned:,}"
    assert _panel_total(page) == (w.planned, f"{profit:,.0f} ISK")


def test_a_yield_change_after_mark_executed_moves_nothing_on_the_executed_runs_page(
    seeded_client, monkeypatch, ref,
):
    """Review #13 / #76 through Settings: the hold freezes the yield it
    was anchored at (D1) and web._alchemy_section reads that yield for a
    row whose composite has a hold. Run 1 installs, delivers and is marked
    executed, then Reprocess Yield drops from 55% to 40% in Settings: run
    1's Industry Jobs page keeps every for-sale figure — the for-sale
    badge's units (the row's listed jobs at the frozen yield beside the
    hold), the reprocess checklist's expected composite and recovered goo,
    Sell qty and the For sale stat (an executed run's: the hold alone) and
    Held — exactly as before, while run 2, planned after the change,
    figures its for-sale jobs at 40%."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    w.execute()
    c = _state()
    item = _item(c, w.run_id, w.unrefined)
    c.close()
    assert item is not None and item["jobs_allocated"] == item["alchemy_profit_jobs"], "a pure for-sale row"
    new_units = _sale_units_of(item)
    assert new_units > 0, "the executed plan lists new for-sale jobs beside the installed ones"
    new_qty = _expected(new_units, w.route)
    unrefined = ref.type_info(w.unrefined).name

    before = _for_sale_figures(w.get(), w)
    # An executed run sells its hold alone: the row's listed jobs never
    # ran, so Sell qty and the stat are the hold's units, while the badge
    # and the checklist still describe the row's jobs — at the frozen yield.
    assert (before["sell_qty"], before["held"], before["stat"]) == (w.planned, w.planned, f"{w.planned:,}")
    # An executed run's badge describes the hold alone — the row's listed
    # jobs are history (review 2026-10-31); the checklist keeps them.
    assert f"{w.planned:,} {w.name} from the for-sale jobs ESI shows installed (the run's hold)" in before["badge"]
    assert f"~{new_qty:,}" not in before["badge"]
    assert before["checklist"].startswith(f"Reprocess {new_units:,} {unrefined} → ~{new_qty:,} {w.name}")
    assert before["checklist"].endswith("— all for sale")

    # Reprocess Yield 55% → 40% through the Settings form, every other
    # field at the world's value.
    assert _save_settings(
        seeded_client, alchemy_enabled="on", alchemy_for_profit="on", alchemy_yield_pct="40",
        max_alchemy_jobs="1000", duration="24",
    ) == "settings saved"
    assert _db_settings().alchemy_reprocess_yield == pytest.approx(0.40)
    assert _expected(new_units, w.route, 0.40) != new_qty, "the change would show, were the yield read live"
    assert _for_sale_figures(w.get(), w) == before
    assert _hold(ref, w.run_id, w.composite).for_sale_qty == w.planned

    # A NEW plan figures its for-sale jobs at the new yield; run 1's page
    # keeps its figures beside it.
    run2 = _plan(seeded_client)
    c = _state()
    item2 = _item(c, run2, w.unrefined)
    c.close()
    assert item2 is not None and int(item2["alchemy_profit_jobs"] or 0) > 0, "run 2 lists for-sale jobs"
    units2 = _sale_units_of(item2)
    planned2 = _expected(units2, w.route, 0.40)
    assert planned2 != _expected(units2, w.route)
    page2 = _for_sale_figures(w.get(run_id=run2), w)
    assert (page2["sell_qty"], page2["held"], page2["stat"]) == (planned2, 0, f"{planned2:,}")
    assert f"~{planned2:,} {w.name} to sell in Jita after the reprocess" in page2["badge"]
    assert page2["checklist"].startswith(f"Reprocess {units2:,} {unrefined} → ~{planned2:,} {w.name}")
    assert _for_sale_figures(w.get(), w) == before


def test_the_panel_intro_and_the_mark_sold_button_name_the_sales_the_update_cannot_see(
    seeded_client, monkeypatch, ref,
):
    """Wording (2026-10-31): the Sell in Jita intro says what the ESI
    update sees sold and what Mark sold is for — on the open run with the
    'plus what the jobs listed above would add' clause, on an executed run
    without it — and the Mark sold button's title opens with the same
    list of the sales the update cannot see."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    unseen = "(a trade, a hand-over, a want-to-buy contract you accepted, the pile sold unrefined)"
    intro = (
        "These units are <b>held for sale</b>: the plan does not count them as stock while the "
        "jobs run and for one full run after they deliver, unless the ESI update sees them sold "
        f"(a market sale or an item-exchange contract you issued) or you mark them sold here {unseen}."
    )
    page = _squash(w.get())
    assert intro in page
    assert (
        "Sell qty follows the for-sale jobs ESI shows installed plus what the jobs listed above "
        "would add; expected profit is the Jita quote"
    ) in page
    assert "a sale the ESI update cannot see" not in page  # nothing delivered: no Mark sold

    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    button = (
        f'title="a sale the ESI update cannot see {unseen}: marks run 1\'s delivered units sold, '
        "oldest run first; with a price the units book revenue as entered, without one neither "
        'revenue nor cost">Mark sold</button>'
    )
    assert button in _u(_panel_rows(w.get())[w.name]["actions"])
    w.execute()
    page = w.get()
    assert intro in _squash(page)
    assert (
        "Sell qty follows the for-sale jobs ESI shows installed; expected profit is the Jita quote"
    ) in _squash(page)
    assert button in _u(_panel_rows(page)[w.name]["actions"])


# --- GET pages never settle --------------------------------------------------------


def test_get_pages_write_nothing_the_update_settles(seeded_client, monkeypatch, ref):
    """Review #95: a matchable sale on record is matched by the next
    ⟳ Update, never by viewing the Index Runs list, the run page, its
    Profit tab or the Ledger."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    c = _state()
    seed_tx(c, 7009, type_id=w.composite, qty=25, price=900_000.0, date=_iso(w.opened + timedelta(days=3)))
    c.close()
    for path in ("/runs", f"/runs/{w.run_id}", f"/runs/{w.run_id}?view=profit", f"/runs/{w.run_id}?view=chain",
                 "/ledger?window=all"):
        assert seeded_client.get(path).status_code == 200, path
    assert _releases() == []
    assert _panel_rows(w.get())[w.name]["released"] == 0
    flash = _refresh(seeded_client, monkeypatch, sc)
    assert "alchemy: 25 units sold matched" in flash
    assert _releases() == [(w.run_id, "market", 7009, 25, 900_000.0, w.route_unit)]


# --- the lifecycle guards ------------------------------------------------------------


def test_reopen_refuses_while_a_newer_planned_run_exists(seeded_client, ref):
    """The plan's forced consequence: a reopened run that is not the
    newest has no buying window, so its hold could neither be read from
    ESI nor expire — discard the newer plan first."""
    w = _world(seeded_client, ref)
    w.execute()
    run2 = _plan(seeded_client)
    flash = _u(seeded_client.post(f"/runs/{w.run_id}/reopen", follow_redirects=True).get_data(as_text=True))
    assert (
        "discard run 2 first — a reopened run that is not the newest has no buying window, "
        "so its for-sale hold could neither be read from ESI nor expire"
    ) in flash
    c = _state()
    assert c.execute("SELECT status FROM index_run WHERE index_run_id = ?", (w.run_id,)).fetchone()[0] == "complete"
    c.close()
    assert "run 2 discarded" in _u(seeded_client.post(f"/runs/{run2}/delete", follow_redirects=True).get_data(as_text=True))
    flash = _u(seeded_client.post(f"/runs/{w.run_id}/reopen", follow_redirects=True).get_data(as_text=True))
    assert "run reopened" in flash and "discard run" not in flash


def test_discard_refuses_a_run_whose_hold_shows_installs(seeded_client, monkeypatch, ref):
    """D2 rule 4: a proposal-only run discards; once ESI shows its
    for-sale jobs installed the discard is refused (the sale history
    would cascade away and the installs would land in the next run's
    baseline), and the list's confirm says so beforehand."""
    w = _world(seeded_client, ref)
    listing = seeded_client.get("/runs").get_data(as_text=True)
    assert "the discard will be refused" not in listing
    assert "run 1 discarded" in _u(seeded_client.post(f"/runs/{w.run_id}/delete", follow_redirects=True).get_data(as_text=True))
    # The flags and quotes stay: plan run 1 again, this time with installs.
    run_id = _plan(seeded_client)
    c = _state()
    c.execute(
        "UPDATE index_run SET opened_at = ?, planned_start = ? WHERE index_run_id = ?",
        (store.utc_text(w.opened), store.utc_text(w.opened), run_id),
    )
    c.execute(
        "UPDATE alchemy_hold SET anchored_at = ? WHERE index_run_id = ?",
        (store.utc_text(w.opened), run_id),
    )
    c.commit()
    item = _item(c, run_id, w.unrefined)
    c.close()
    sc = Scenario()
    sc.install(w.unrefined, w.start, int(item["jobs_allocated"]),
               int(item["max_runs_per_job"]) * int(item["portion_size"] or 1), first_job_id=90000)
    _refresh(seeded_client, monkeypatch, sc)
    assert _hold(ref, run_id, w.composite).installed
    listing = _u(seeded_client.get("/runs").get_data(as_text=True))
    assert "Its for-sale hold shows installed jobs or recorded sales, so the discard will be refused — re-plan it instead." in listing
    flash = _u(seeded_client.post(f"/runs/{run_id}/delete", follow_redirects=True).get_data(as_text=True))
    assert (
        f"run 1 not discarded — its for-sale hold ({w.name}) shows installed jobs or recorded "
        "sales; re-plan it instead"
    ) in flash
    c = _state()
    assert c.execute("SELECT COUNT(*) FROM index_run WHERE index_run_id = ?", (run_id,)).fetchone()[0] == 1
    c.close()


def test_mark_executed_refuses_a_second_post_on_an_executed_run(seeded_client, monkeypatch, ref):
    """web.run_complete (review 2026-10-31): a second POST on an executed
    run — a stale page, a second window — would move completed_at (the
    next run's buying window opens there) and with it every hold the
    window anchors. It is refused with the exact flash, and completed_at
    and every hold row stay as they were."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    assert "run marked executed" in w.execute()
    c1 = w.opened + timedelta(days=5)
    c = _state()
    # A completion dated in the past: a rewrite to datetime('now') would show.
    c.execute("UPDATE index_run SET completed_at = ? WHERE index_run_id = ?", (store.utc_text(c1), w.run_id))
    c.commit()
    c.close()

    def snapshot() -> tuple:
        c = _state()
        try:
            run = c.execute(
                "SELECT status, completed_at FROM index_run WHERE index_run_id = ?", (w.run_id,)
            ).fetchone()
            rows = [
                tuple(r) for r in c.execute(
                    "SELECT * FROM alchemy_hold ORDER BY index_run_id, composite_type_id"
                )
            ]
            return (run["status"], run["completed_at"]), rows
        finally:
            c.close()

    before = snapshot()
    assert before[0] == ("complete", store.utc_text(c1)) and before[1], "an executed run with hold rows"
    hold_before = _hold(ref, w.run_id, w.composite)
    flash = _flashes(w.execute())
    assert "run 1 is already executed — nothing changed" in flash
    assert "run marked executed" not in flash and "hold" not in flash
    assert snapshot() == before
    hold = _hold(ref, w.run_id, w.composite)
    assert (hold.for_sale_qty, hold.remaining, hold.live, hold.status) == (
        hold_before.for_sale_qty, hold_before.remaining, hold_before.live, hold_before.status
    )


def test_mark_executed_refuses_a_run_that_is_not_the_newest(seeded_client, ref):
    """web.run_complete (review 2026-10-31): run 1 open and run 2 planned
    over it (a bare newer run by SQL — a second POST /run re-plans the
    open run in place) — run 1 is a superseded plan with no buying window
    of its own, so its Mark executed is refused with the exact flash
    naming run 2, and both runs stay open."""
    w = _world(seeded_client, ref)
    c = _state()
    run2 = _newer_run(c, 2)
    c.close()
    flash = _flashes(w.execute())
    assert (
        "run 1 is not the newest run — only the newest run can be marked executed "
        "(discard or execute run 2 first)"
    ) in flash
    assert "run marked executed" not in flash
    c = _state()
    rows = c.execute(
        "SELECT index_run_id, status, completed_at FROM index_run ORDER BY index_run_id"
    ).fetchall()
    c.close()
    assert [r["index_run_id"] for r in rows] == [w.run_id, run2]
    assert all(r["status"] != "complete" and r["completed_at"] is None for r in rows)


def test_a_sold_pile_beside_new_jobs_keeps_the_open_runs_row_counted_for_the_new_units(
    seeded_client, monkeypatch, ref,
):
    """Review 2026-10-31: a row counts while it has an ACTIVE quantity —
    the hold's unsold remainder while it lives plus the open run's new
    jobs' share. Run 1 open: its for-sale jobs deliver and the whole pile
    sells through the update (status 'sold', nothing held), while the
    re-plan lists NEW for-sale jobs on the lines the delivered chain
    freed. The Sell in Jita row stays counted: its Expected profit is the
    new jobs' units alone (the plan's own net, not today's quote), the
    For sale stat shows the new units with no 'still held' part, the
    Total row's Sell qty is pile + new and its profit the new units', and
    the Alchemy-table badge names the new units to sell and the sold hold
    apart."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    ids, _end = _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    sc.sale(7001, w.composite, w.planned, 900_000.0, w.opened + timedelta(days=3))
    flash = _refresh(seeded_client, monkeypatch, sc)
    assert f"alchemy: {w.planned:,} units sold matched" in flash
    hold = _hold(ref, w.run_id, w.composite)
    assert (hold.status, hold.remaining, hold.live, hold.for_sale_qty) == ("sold", 0, True, w.planned)
    c = _state()
    item = _item(c, w.run_id, w.unrefined)
    c.close()
    new_units = _sale_units_of(item)
    assert new_units > 0, "the delivered chain frees lines for NEW for-sale jobs"
    per_now = int(item["max_runs_per_job"]) * int(item["portion_size"] or 1)
    sale_jobs = math.ceil(new_units / per_now)
    jobs_word = f"{sale_jobs} job{'s' if sale_jobs != 1 else ''}"
    new_qty = _expected(new_units, w.route)
    sale_net, route_unit = item["alchemy_sale_net_unit"], item["alchemy_route_unit_cost"]
    assert sale_net is not None and route_unit is not None
    profit = (sale_net - route_unit) * new_qty

    page = w.get()
    row = _panel_rows(page)[w.name]
    assert (row["sell_qty"], row["released"], row["held"], row["status"]) == (
        w.planned + new_qty, w.planned, 0, "sold"
    )
    assert row["sell_title"] == (
        f"{w.planned:,} from the for-sale jobs ESI shows installed + {new_qty:,} from {jobs_word} to run now"
    )
    assert row["profit"] != "—" and _isk_of(row["profit_title"]) == _whole(profit)
    assert row["profit_title"].endswith(f"× {new_qty:,} units still to sell")
    assert "at today's quote" not in row["profit_title"]  # the plan's own net prices the new jobs
    value, title = _stat(page, "For sale")
    assert value == f"{new_qty:,}"
    assert title.startswith(
        f"{new_qty:,} composite units still to sell: ~{new_qty:,} from {sale_jobs} alchemy "
        f"job{'s' if sale_jobs != 1 else ''} to run now for profit"
    )
    assert "still held" not in title and "no quote" not in title
    assert _panel_total(page) == (w.planned + new_qty, f"{profit:,.0f} ISK")
    badge = _for_sale_figures(page, w)["badge"]
    assert badge.endswith(
        f": ~{new_qty:,} {w.name} to sell in Jita after the reprocess; {w.planned:,} {w.name} "
        "from the for-sale jobs ESI shows installed (the run's hold — sold)"
    )
    assert w.sold_action not in page  # nothing left delivered to mark


def _second_run_delivered(client, monkeypatch, ref, w: World, sc: Scenario, *, sold_first=100):
    """The two-run world of test_mark_sold_spreads...: run 1's for-sale
    jobs (no hulls, the cap pinned) deliver and `sold_first` units sell on
    transaction 7001; run 1 is completed by hand at c1 (5 days in); run 2
    plans, its anchors moved to c1, installs its own for-sale jobs a
    second after c1 and delivers them two days later. Returns (run 2's id,
    its planned composite units, its formula row, c1)."""
    w.pin_cap()
    ids1, _end = _install_run(w, sc, hulls=False)
    _refresh(client, monkeypatch, sc)
    sc.deliver(ids1, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    if sold_first:
        sc.sale(7001, w.composite, sold_first, 900_000.0, w.opened + timedelta(days=3))
    _refresh(client, monkeypatch, sc)
    c1 = w.opened + timedelta(days=5)
    c = _state()
    complete_run(c, w.run_id, c1)
    c.close()
    run2 = _plan(client)
    c = _state()
    c.execute("UPDATE alchemy_hold SET anchored_at = ? WHERE index_run_id = ?", (store.utc_text(c1), run2))
    c.commit()
    item2 = _item(c, run2, w.unrefined)
    c.close()
    assert item2 is not None and int(item2["alchemy_profit_jobs"]) > 0, "run 2 lists for-sale jobs again"
    per2 = int(item2["max_runs_per_job"]) * int(item2["portion_size"] or 1)
    planned2 = _expected(int(item2["alchemy_profit_jobs"]) * per2, w.route)
    ids2, _end2 = sc.install(
        w.unrefined, c1 + timedelta(seconds=1), int(item2["jobs_allocated"]), per2, first_job_id=92000,
    )
    _refresh(client, monkeypatch, sc)
    sc.deliver(ids2, c1 + timedelta(days=2), to_hangar={w.composite: planned2})
    _refresh(client, monkeypatch, sc)
    return run2, planned2, item2, c1


def test_a_mark_that_emptied_an_older_hold_keeps_its_undo_on_the_page_it_was_posted_from(
    seeded_client, monkeypatch, ref,
):
    """web._alchemy_sales's undo list reaches every LIVE hold of the
    composite (review 2026-10-31): a Mark sold posted from run 2's page
    comes off run 1 first (D4) and empties run 1's hold, which is then no
    Mark sold target any more — run 2's page still lists that mark, with
    its undo, and the undo from there puts run 1's units back on hold."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    run2, planned2, _item2, _c1 = _second_run_delivered(seeded_client, monkeypatch, ref, w, sc)
    r1 = w.planned - 100
    flash = w.sold(run_id=run2, qty=str(r1), price="1000")
    assert (
        f"{w.name}: {r1:,} marked sold — run 1's hold (booked as revenue at 1,000.00 ISK per unit, "
        "as entered — no fees deducted)"
    ) in flash
    hold1 = _hold(ref, w.run_id, w.composite)
    assert (hold1.remaining, hold1.live, hold1.status) == (0, True, "sold")
    assert _hold(ref, run2, w.composite).remaining == planned2
    c = _state()
    [mark_id] = [r[0] for r in c.execute("SELECT release_id FROM alchemy_hold_release WHERE source = 'manual'")]
    c.close()

    page2 = _u(w.get(run_id=run2))
    row = _panel_rows(page2)[w.name]
    assert "marks run 1's delivered units" not in row["actions"], "run 1 is empty: run 2 is the only target"
    assert f"{r1:,} {w.name} on run 1's hold" in page2 and "1,000.00 ISK per unit as entered" in page2
    undo = f'action="/runs/{run2}/alchemy/release/{mark_id}/undo"'
    assert undo in page2
    assert f"Undo the by-hand mark of {r1:,} {w.name} on run 1's hold? The units are held for sale again." in page2
    flash = _u(seeded_client.post(f"/runs/{run2}/alchemy/release/{mark_id}/undo", follow_redirects=True).get_data(as_text=True))
    assert f"{w.name}: the by-hand mark of {r1:,} on run 1's hold is undone — {r1:,} held for sale again" in flash
    assert _releases() == [(w.run_id, "market", 7001, 100, 900_000.0, w.route_unit)]
    assert _hold(ref, w.run_id, w.composite).remaining == r1
    assert "/alchemy/release/" not in w.get(run_id=run2)


def test_the_ledgers_alchemy_section_renders_without_a_pipeline_and_counts_a_split_contract_once(
    seeded_client, monkeypatch, ref,
):
    """Review 2026-10-31: the Ledger's Alchemy sales section stands on
    holds and releases, outside the finals' 'No pipelines' gate; its
    'N unpriced' badge counts distinct sales. One bundle contract (the
    composite with Tritanium: no attributable price) larger than run 1's
    room is split over run 1's and run 2's holds — two release rows, ONE
    unpriced sale — and with every pipeline deleted the section, its
    sales table and its Still held table still render."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    run2, planned2, item2, c1 = _second_run_delivered(seeded_client, monkeypatch, ref, w, sc)
    r1 = w.planned - 100
    bundle = r1 + 10
    c = _state()
    _seed_contract(
        c, 5001, [(w.composite, bundle), (TRITANIUM, 100)], 1e9,
        issued=c1 + timedelta(days=3), completed=c1 + timedelta(days=4),
    )
    c.close()
    assert f"alchemy: {bundle:,} units sold matched" in _refresh(seeded_client, monkeypatch, sc)
    assert _releases() == [
        (w.run_id, "market", 7001, 100, 900_000.0, w.route_unit),
        (w.run_id, "contract", 5001, r1, None, w.route_unit),
        (run2, "contract", 5001, 10, None, item2["alchemy_route_unit_cost"]),
    ]
    c = _state()
    pipeline_id = c.execute("SELECT pipeline_id FROM pipeline").fetchone()[0]
    c.close()
    assert seeded_client.post(f"/pipelines/{pipeline_id}/delete").status_code == 302
    c = _state()
    assert c.execute("SELECT COUNT(*) FROM pipeline").fetchone()[0] == 0
    c.close()

    resp = seeded_client.get("/ledger?window=all")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "No pipelines — the Ledger tracks pipeline finals only" in page
    section = _ledger_section(page)
    assert (
        f"— {w.planned + 10:,} units sold in this window · {planned2 - 10:,} units still held</span>"
    ) in section
    assert f">1 unpriced · {bundle:,} units</span>" in section
    assert ">2 unpriced" not in section
    sales = _ledger_rows(page, 10)
    by_run = sorted((_text(tds[8][1]), _num(tds[2][1]), "contract 5001" in tds[9][1]) for tds in sales)
    assert by_run == [("1", 100, False), ("1", r1, True), ("2", 10, True)]
    held = _held_rows(page)
    assert set(held) == {1, 2}
    assert (_num(held[1][1][3][1]), _num(held[1][1][4][1]), _num(held[1][1][5][1])) == (w.planned, w.planned, 0)
    assert (_num(held[2][1][3][1]), _num(held[2][1][4][1]), _num(held[2][1][5][1])) == (planned2, 10, planned2 - 10)
    assert _stat(section, "Still held")[0] == f"{planned2 - 10:,}"


def test_the_index_runs_list_badges_a_failed_alchemy_estimate_and_keeps_the_hull_figures(
    seeded_client, monkeypatch, ref,
):
    """Review #99 on the list: the open run's alchemy estimate is computed
    in its own try — when it raises, the row keeps Planning → Profit's
    hull figures, drops the 'incl.' clause and shows the 'alchemy failed'
    badge with its title. web._alchemy_profit is a closure of create_app,
    so the failure is injected where it reads: holds.load for this run."""
    w = _world(seeded_client, ref)
    sc = Scenario()
    _install_run(w, sc)
    _refresh(seeded_client, monkeypatch, sc)
    assert "incl." in _u(_runs_row(seeded_client.get("/runs").get_data(as_text=True), 1))
    real_load = holds.load

    def boom(conn, ref_, settings=None, *, run_id=None):
        if run_id == w.run_id:
            raise RuntimeError("the hold could not be valued")
        return real_load(conn, ref_, settings, run_id=run_id)

    monkeypatch.setattr(holds, "load", boom)
    resp = seeded_client.get("/runs")
    assert resp.status_code == 200
    tr = _u(_runs_row(resp.get_data(as_text=True), 1))
    assert (
        '<span class="badge warn" title="the open run\'s for-sale hold could not be valued '
        '(see the log) — these figures leave it out">alchemy failed</span>'
    ) in tr
    assert ">est.</span>" in tr and "incl." not in tr
    planning = seeded_client.get("/planning").get_data(as_text=True)
    assert _title_isk(tr, 5) == _strip_isk(planning, "Net proceeds / cycle")
    assert _title_isk(tr, 6) == _strip_isk(planning, "Profit / cycle")


def test_the_settle_matches_a_contract_by_its_issue_date_before_a_later_market_sale(
    seeded_client, monkeypatch, ref,
):
    """ledger.settle_alchemy_holds (review 2026-10-31): the sale events
    are taken in the order the units LEFT the hangar — a contract by its
    date_issued, a wallet sell by its date. A contract issued on day 3
    and completed on day 6 beside a market sale on day 4, with room on
    run 1's hold for only one of them: the contract lands on run 1 (its
    release row dated by the completion) and the market sale stays
    unassigned, a second update matching nothing new."""
    w = _world(seeded_client, ref)
    w.pin_cap()
    sc = Scenario()
    ids, _end = _install_run(w, sc, hulls=False)
    _refresh(seeded_client, monkeypatch, sc)
    sc.deliver(ids, w.opened + timedelta(days=2), to_hangar={w.composite: w.planned})
    _refresh(seeded_client, monkeypatch, sc)
    assert _hold(ref, w.run_id, w.composite).status == "delivered"
    completed = w.opened + timedelta(days=6)
    c = _state()
    _seed_contract(
        c, 5001, [(w.composite, w.planned)], w.planned * 950_000.0,
        issued=w.opened + timedelta(days=3), completed=completed,
    )
    c.close()
    sc.sale(7001, w.composite, w.planned, 900_000.0, w.opened + timedelta(days=4))
    flash = _refresh(seeded_client, monkeypatch, sc)
    assert f"alchemy: {w.planned:,} units sold matched" in flash
    [(run, source, ref_id, qty, price, cost)] = _releases()
    assert (run, source, ref_id, qty, cost) == (w.run_id, "contract", 5001, w.planned, w.route_unit)
    assert price == pytest.approx(950_000.0)  # a one-type contract: price / units, exact
    c = _state()
    released_at = c.execute("SELECT released_at FROM alchemy_hold_release").fetchone()[0]
    c.close()
    assert released_at.startswith(completed.strftime("%Y-%m-%d"))
    hold = _hold(ref, w.run_id, w.composite)
    assert (hold.observed_qty, hold.remaining, hold.status) == (w.planned, 0, "sold")
    assert "alchemy:" not in _refresh(seeded_client, monkeypatch, sc)
    assert len(_releases()) == 1
    page = seeded_client.get("/ledger?window=all").get_data(as_text=True)
    [sale] = _ledger_rows(page, 10)
    assert "item-exchange contract 5001" in sale[9][1] and _num(sale[2][1]) == w.planned
    assert "transaction 7001" not in page


# --- the Settings save -----------------------------------------------------------------


def test_the_settings_flash_says_to_refresh_prices_when_a_route_may_be_newly_run(seeded_client, ref):
    """Review #127 (web._settings_save): a save that switches Alchemy for
    Profit on under Enable Alchemy, or un-ticks a route from Never run
    while it is on, adds routes nobody has priced yet and flashes the
    long 'refresh prices' message; the same save again, ticking a route,
    switching the tail off, or switching it on with Enable Alchemy off
    flashes plain 'settings saved'. Each flash is the page's only one."""
    composite_id = sorted(ref.alchemy_routes())[0]
    on = dict(alchemy_enabled="on", alchemy_for_profit="on", alchemy_blacklist_present="1")
    assert _db_settings().alchemy_for_profit is False
    # The tail ticked on with Enable Alchemy off runs nothing: plain.
    assert _save_settings(seeded_client, alchemy_for_profit="on", alchemy_blacklist_present="1") == "settings saved"
    s = _db_settings()
    assert s.alchemy_for_profit is True and s.alchemy_enabled is False
    # Off again (plain), then on under Enable Alchemy: the long flash.
    assert _save_settings(seeded_client, alchemy_enabled="on", alchemy_blacklist_present="1") == "settings saved"
    assert _db_settings().alchemy_for_profit is False
    assert _save_settings(seeded_client, **on) == _REFRESH_PRICES_FLASH
    s = _db_settings()
    assert s.alchemy_for_profit is True and s.alchemy_enabled is True
    # The same save again: nothing newly run.
    assert _save_settings(seeded_client, **on) == "settings saved"
    # Ticking a route under Never run takes a route away: plain.
    assert _save_settings(seeded_client, **on, **{f"alchemy_block_{composite_id}": "on"}) == "settings saved"
    c = _state()
    assert store.alchemy_blacklist(c) == {composite_id}
    c.close()
    # Un-ticking it frees a route nobody has priced: the long flash.
    assert _save_settings(seeded_client, **on) == _REFRESH_PRICES_FLASH
    c = _state()
    assert store.alchemy_blacklist(c) == set()
    c.close()
    # Un-ticking with the tail off says nothing about prices.
    assert _save_settings(
        seeded_client, alchemy_enabled="on", alchemy_blacklist_present="1",
        **{f"alchemy_block_{composite_id}": "on"},
    ) == "settings saved"
    assert _save_settings(seeded_client, alchemy_enabled="on", alchemy_blacklist_present="1") == "settings saved"


# --- pieces ------------------------------------------------------------------------


def test_cycle_totals_fold_the_alchemy_line_and_keep_it_apart():
    totals = costing.cycle_totals(
        [],
        [
            {"cost": 100.0, "proceeds": 160.0, "profit": 60.0, "units": 7, "unpriced_units": 2},
            {"cost": 50.0, "proceeds": 0.0, "profit": -50.0, "units": 3, "no_quote": True},
            {"cost": 10.0, "proceeds": 0.0, "profit": -10.0, "units": 4, "returned_units": 4},
        ],
    )
    assert (totals.cost, totals.proceeds, totals.profit) == (160.0, 160.0, 0.0)
    assert (totals.alchemy_cost, totals.alchemy_proceeds, totals.alchemy_profit) == (160.0, 160.0, 0.0)
    assert totals.alchemy_units == 14 and totals.hulls == 0 and totals.priced == 0
    # D7: the badge counts composites short of a price or a quote, apart
    # from the hull count; returned units are summed.
    assert totals.alchemy_unpriced == 2 and totals.unpriced == 0
    assert totals.alchemy_returned_units == 4
    empty = costing.cycle_totals([])
    assert (empty.alchemy_units, empty.alchemy_unpriced, empty.alchemy_returned_units) == (0, 0, 0)


def test_the_stockpile_tab_annotates_each_cell_with_its_share_of_the_hold(ref):
    """Review #117: the hidden units are split between the cells they came
    off — the unrefined credit in flight on In jobs, the rest on On hand
    — each note naming the deficit and the hold's end."""
    from magoo.web import _chain_context

    row = {
        "name": "Hyperflurite", "type_id": 16679, "group_id": 429, "category": "Material",
        "category_id": 4, "depth": 3, "blueprint_id": 46166, "activity_id": 11,
        "recommended_buy_qty": 0, "price_snapshot": 10.0, "capacity_limited": 0,
        "runs_allocated": 0, "jobs_allocated": 0, "max_runs_per_job": 1,
        "recommended_build_qty": 0, "time_per_run": 0.0, "low_stock": 0,
        "savings_unpriced_inputs": 0, "deficit_qty": 0, "target_stock_qty": 100,
        "merged_min_qty": 100, "cycle_need_qty": 100, "on_hand_qty": 0,
        "in_progress_qty": 0, "alchemy_credit_qty": 0, "alchemy_for_type_id": None,
        "alchemy_output_qty": 0, "held_for_sale_qty": 185653, "held_from_credit_qty": 20790,
        "compressed_covered_qty": 0, "market_buy_qty": 0,
    }
    until = "until run 8 is marked executed"
    ctx = _chain_context(ref, [row], {16679: until})
    x = next(r for r in ctx["chain_rows"] if r["type_id"] == 16679)
    assert (x["held_for_sale"], x["held_from_credit"], x["held_until"]) == (185653, 20790, until)
    html = _u(_render_detail(
        dict(_detail_ctx([row], {"buys": [], "buy_total": 0, "buys_unpriced": 0,
                                 "multibuy_hub": "", "multibuy_structure": "",
                                 "structure_buys": [], "split_buys": [], "venue_qty": {},
                                 "unsourced": {}, "region_wide": set(), "compressed": {},
                                 "compressed_covered": {}},
                         settings=_settings()), **ctx),
        "run_chain.html", "/runs/1?view=chain",
    ))
    note = (
        " held for sale (alchemy run for profit) are left out of On hand / In jobs and so "
        f"stay in the deficit, {until}"
    )
    assert f"164,863 more units in the hangar{note}" in html
    assert f"20,790 more units expected from the formula's unrefined stock or jobs{note}" in html
    # A row without the credit column (planned before it) puts it all on the hangar.
    legacy = {k: v for k, v in row.items() if k != "held_from_credit_qty"}
    y = next(r for r in _chain_context(ref, [legacy])["chain_rows"] if r["type_id"] == 16679)
    assert (y["held_for_sale"], y["held_from_credit"], y["held_until"]) == (185653, 0, "")


def test_the_esi_update_settles_holds_after_the_sales_pull_and_before_the_prices(seeded_client, monkeypatch):
    """The update's order (D4): snapshot, sales pull, settle, prices,
    re-plan — the settle reads the job rows the snapshot step wrote and
    the sales the pull stored; its clause rides in the one flash."""
    c = _state()
    _add_character(c)
    c.close()
    calls = []
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (calls.append("prices"), (0, 0, 0))[1])
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)

    def fake_state(conn, ref):
        calls.append("snapshot")
        store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0, {})
        return {"on_hand": {}, "in_progress": {}, "active_jobs": {}, "job_ends": {},
                "character_isk": 0.0, "corporation_isk": 0.0}

    monkeypatch.setattr(esi, "refresh_state", fake_state)
    monkeypatch.setattr(
        ledger, "pull_sales",
        lambda conn, ref: (calls.append("sales"), ledger.PullSummary(new_sales=0, contracts_finished=0, open_orders=0))[1],
    )
    monkeypatch.setattr(
        ledger, "settle_alchemy_holds", lambda conn, ref, settings: (calls.append("settle"), 3)[1]
    )
    resp = seeded_client.post("/esi/refresh", follow_redirects=True)
    assert calls[:4] == ["snapshot", "sales", "settle", "prices"]
    assert "alchemy: 3 units sold matched against the for-sale holds" in resp.get_data(as_text=True)


def test_a_failed_settle_is_said_and_rolled_back(seeded_client, monkeypatch):
    """Reviews #34 / #96: a settle that raises leaves no open transaction
    and no partial row; the update's flash names the failure and the
    prices and re-plan still run."""
    c = _state()
    _add_character(c)
    c.close()
    calls = []
    monkeypatch.setattr(market, "refresh_prices", lambda *a, **k: (calls.append("prices"), (0, 0, 0))[1])
    monkeypatch.setattr(market, "fetch_adjusted_prices", lambda: {})
    monkeypatch.setattr(esi, "character_with_scope", lambda conn, scope: None)

    def fake_state(conn, ref):
        store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0, {})
        return {"on_hand": {}, "in_progress": {}, "active_jobs": {}, "job_ends": {},
                "character_isk": 0.0, "corporation_isk": 0.0}

    def boom(conn, ref, settings):
        conn.execute("BEGIN IMMEDIATE")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(esi, "refresh_state", fake_state)
    monkeypatch.setattr(ledger, "pull_sales", lambda conn, ref: ledger.PullSummary())
    monkeypatch.setattr(ledger, "settle_alchemy_holds", boom)
    page = _u(seeded_client.post("/esi/refresh", follow_redirects=True).get_data(as_text=True))
    assert "alchemy holds not settled (database is locked) — hold figures lag until the next update" in page
    assert calls == ["prices"] and "re-plan" in page
