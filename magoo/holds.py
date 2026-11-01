"""The alchemy for-sale hold (schema 14, user rulings 2026-10-31).

Alchemy for Profit plans unrefined reactions whose composite is meant to be
SOLD in Jita. ESI cannot tell that pile from the chain's own units of the
same composite, so a hold remembers it: planning hides the pile from the
composite's stock, settlement matches the pile's sales to it, and the Ledger
and Profit tab book what it earned.

The hold row (store.alchemy_hold) stores ANCHORS only — the plan's proposal
(`planned_qty`), the route cost, and three figures that tie it to the job
record: `baseline_units` (unrefined units of the formula already installed
in the run's buying window when the row was first written — the chain's),
`chain_units` (that baseline plus the plan's savings jobs' units: the FIRST
chain_units of the window's installed units are the chain's, the rest the
hold's for-sale jobs — alchemy_sale_split's "savings first") and `yield_`.
Everything else is DERIVED here, at read time, from `industry_job`,
`index_run` and the release rows, so no stored figure can go stale:

* what ESI shows installed, delivered, still in flight (a cancelled job
  simply leaves the hold);
* `for_sale_qty`, the composite units ESI vouches for — an open run's hold
  is 0 until its jobs are installed, so it never hides a proposal and never
  feeds back into its own re-plan;
* life (ruling b): live while the for-sale jobs are in flight, then until
  the SECOND Mark executed after they deliver (one full run after delivery,
  as a count over index_run — delivery between runs needs no special case,
  Reopen of the ending run revives the hold);
* the settlement window: a sale counts against a hold from its first
  for-sale delivery (ESI's completed_date) to the hold's expiry.

The only writer is the plan's persist path (`write_plan_holds`): a row is
updated while the window shows no job started after its anchor, frozen once one
does (the plan the user installed from is the last one written before the
installs appeared), and never deleted by a plan.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import buying, config, store

log = logging.getLogger(__name__)


@dataclass
class Hold:
    """One (run, composite) hold with the figures derived from the record."""

    index_run_id: int
    run_number: int
    run_status: str
    opened_at: str | None
    completed_at: str | None
    composite_type_id: int
    unrefined_type_id: int | None
    composite_qty: int
    planned_qty: int
    route_unit_cost: float | None
    baseline_units: int | None
    chain_units: int | None
    yield_: float
    anchored_at: str | None = None  # the row's first write; NULL on legacy rows
    cost_from_fills: bool = False  # route_unit_cost came from a fill-priced plan
    # --- derived from the job record -------------------------------------
    covered: bool = False
    plan_figure: bool = False  # for_sale_qty is planned_qty (uncovered executed window / legacy row)
    superseded: bool = False
    installed_units: int = 0  # the window's installed unrefined units, chain's included
    pre_anchor_units: int = 0  # of those, started at or before the anchor (the chain's)
    frozen: bool = False  # an installed job started after the anchor: the row is ESI's
    for_sale_unrefined: int = 0
    for_sale_qty: int = 0
    in_flight: bool = False  # a for-sale job still active / paused / ready
    delivered_units: int = 0  # for-sale unrefined units delivered
    delivered_qty: int = 0  # ... in composite units
    first_delivered: datetime | None = None
    delivered_at: datetime | None = None  # only once no for-sale job is in flight
    jobs_end: datetime | None = None  # latest timer end among in-flight for-sale jobs
    # --- releases ----------------------------------------------------------
    observed_qty: int = 0
    manual_priced_qty: int = 0
    manual_unpriced_qty: int = 0
    releases: list = field(default_factory=list)
    # --- life ---------------------------------------------------------------
    live: bool = False
    expires_after: int | None = None  # the run whose Mark executed ends it
    expired_at: datetime | None = None

    @property
    def released_qty(self) -> int:
        return self.observed_qty + self.manual_priced_qty + self.manual_unpriced_qty

    @property
    def remaining(self) -> int:
        return max(0, self.for_sale_qty - self.released_qty)

    @property
    def installed(self) -> bool:
        """Whether the window shows for-sale jobs."""
        return self.for_sale_unrefined > 0

    @property
    def room_observed(self) -> int:
        """Units an observed sale may still release: the delivered for-sale
        pile less what observed sales already took (by-hand marks never
        block an observed sale — settlement replaces them)."""
        return max(0, self.delivered_qty - self.observed_qty)

    @property
    def status(self) -> str:
        """returned to stock > sold > plan figure > delivered > in flight >
        not installed."""
        if self.for_sale_qty <= 0:
            return "not installed"
        if not self.live and self.remaining > 0:
            return "returned to stock"
        if self.remaining <= 0:
            return "sold"
        if self.plan_figure:
            return "plan figure"  # an executed run the record does not cover yet
        if self.delivered_at is not None:
            return "delivered"
        if self.in_flight or self.installed:
            return "in flight"
        return "not installed"


@dataclass(frozen=True)
class Bite:
    """What planning hides of one composite (engine._apply_targets)."""

    remaining: int  # Σ remaining over the live holds
    listed: int  # listed units attributed to those live holds
    unrefined_type_id: int
    composite_qty: int

    @property
    def hide(self) -> int:
        return max(0, self.remaining - self.listed)


_RUNNING = store.JOB_STATUSES_RUNNING
_INSTALLED = store.JOB_STATUSES_INSTALLED
_DELIVERED = "delivered"


def _dt(value) -> datetime | None:
    return store._utc_datetime(value)


def _runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT index_run_id, run_number, status, completed_at FROM index_run "
        "ORDER BY run_number, index_run_id"
    ).fetchall()


def load(conn: sqlite3.Connection, ref, settings=None, *, run_id: int | None = None) -> list[Hold]:
    """Every hold (one run's when `run_id` is given), oldest run first,
    with the figures derived from the job record. `settings` supplies the
    yield for a row written without one (a database stamped 14 before the
    column; the migrations backfill it, this is the belt)."""
    rows = store.hold_rows(conn, run_id)
    if not rows:
        return []
    routes = ref.alchemy_routes()
    windows = {w.index_run_id: w for w in buying.buying_windows(conn)}
    last_pull = _dt(store.last_job_pull(conn))
    runs = _runs(conn)
    executed = sorted(
        (
            (_dt(r["completed_at"]), r["run_number"])
            for r in runs
            if r["status"] == buying.RUN_EXECUTED and _dt(r["completed_at"]) is not None
        ),
        key=lambda t: t[0],
    )
    max_executed = max((n for _, n in executed), default=0)
    open_run_number = next(
        (w.run_number for w in windows.values() if w.hi is None), None
    )
    next_run_number = max((r["run_number"] for r in runs), default=0) + 1
    releases: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for x in store.release_rows(conn, run_id):
        releases.setdefault((x["index_run_id"], x["composite_type_id"]), []).append(x)
    default_yield = (
        settings.alchemy_reprocess_yield if settings is not None else 0.55
    )
    covered_cache: dict[tuple, bool] = {}  # one jobs_covered walk per window

    holds: list[Hold] = []
    for row in rows:
        route = routes.get(row["composite_type_id"])
        yield_ = row["yield_"] if row["yield_"] is not None else default_yield
        hold = Hold(
            index_run_id=row["index_run_id"],
            run_number=row["run_number"],
            run_status=row["run_status"],
            opened_at=row["opened_at"],
            completed_at=row["completed_at"],
            composite_type_id=row["composite_type_id"],
            unrefined_type_id=route.unrefined_id if route else None,
            composite_qty=route.composite_qty if route else 0,
            planned_qty=int(row["planned_qty"] or 0),
            route_unit_cost=row["route_unit_cost"],
            baseline_units=row["baseline_units"],
            chain_units=row["chain_units"],
            yield_=float(yield_),
            anchored_at=row["anchored_at"],
            cost_from_fills=bool(row["cost_from_fills"]),
        )
        rel = releases.get((hold.index_run_id, hold.composite_type_id), [])
        hold.releases = rel
        hold.observed_qty = sum(int(x["qty"]) for x in rel if x["source"] != "manual")
        hold.manual_priced_qty = sum(
            int(x["qty"]) for x in rel if x["source"] == "manual" and x["unit_price"] is not None
        )
        hold.manual_unpriced_qty = sum(
            int(x["qty"]) for x in rel if x["source"] == "manual" and x["unit_price"] is None
        )
        window = windows.get(hold.index_run_id)
        executed_run = hold.run_status == buying.RUN_EXECUTED
        if window is None:
            # No buying window: a superseded plan (not executed, not the
            # newest) or an executed run without a completed_at. Neither
            # can be read from the record; the second is a plan figure.
            hold.superseded = not executed_run
            if executed_run and hold.chain_units is not None and hold.planned_qty > 0:
                hold.plan_figure = True
                hold.for_sale_qty = hold.planned_qty
        elif route is not None:
            _derive(conn, hold, route, window, last_pull, executed_run, covered_cache)
        _life(hold, executed, max_executed, open_run_number, next_run_number)
        holds.append(hold)
    return holds


def _derive(
    conn, hold: Hold, route, window, last_pull, executed_run: bool, covered_cache=None
) -> None:
    hi = window.hi if window.hi is not None else last_pull
    cache = covered_cache if covered_cache is not None else {}

    def covered(lo, hi_) -> bool:
        key = (store.utc_text(lo), store.utc_text(hi_))
        if key not in cache:
            cache[key] = store.jobs_covered(conn, lo, hi_)
        return cache[key]

    hold.covered = False if hi is None else covered(window.lo, hi)
    if hold.chain_units is None:
        # A legacy row written without anchors: nothing to split the
        # installs on. An executed run falls back to the plan's figure
        # (as costing.hull_cost does for hulls) where there is one; an
        # open run holds nothing until a plan anchors it.
        if executed_run and hold.planned_qty > 0:
            hold.plan_figure = True
            hold.for_sale_qty = hold.planned_qty
        return
    jobs = store.jobs_in_window(conn, route.unrefined_id, window.lo, hi) if hi is not None else []
    anchor = _dt(hold.anchored_at)
    if anchor is not None:
        # An anchored row: a job started at or before the anchor is the
        # chain's whatever the record shows later (a late install of the
        # previous run); the plan's savings share applies after it.
        chain_left = max(0, int(hold.chain_units) - int(hold.baseline_units or 0))
    else:
        chain_left = int(hold.chain_units)  # a legacy row: the stored total
    sale_jobs: list[tuple[int, sqlite3.Row]] = []  # (for-sale units, job)
    for job in jobs:
        if job["status"] not in _INSTALLED:
            continue  # cancelled / reverted: no pile will come
        units = int(job["units"] or 0)
        hold.installed_units += units
        if anchor is not None:
            started = _dt(job["start_date"]) or anchor
            if started <= anchor:
                hold.pre_anchor_units += units
                continue
            hold.frozen = True
        take = min(units, chain_left)
        chain_left -= take
        if units - take > 0:
            sale_jobs.append((units - take, job))
    if anchor is None and hold.baseline_units is not None:
        hold.frozen = hold.installed_units > int(hold.baseline_units)
    hold.for_sale_unrefined = sum(u for u, _ in sale_jobs)
    hold.for_sale_qty = math.floor(hold.for_sale_unrefined * route.composite_qty * hold.yield_)
    delivered_dates: list[datetime] = []
    ends: list[datetime] = []
    slack = timedelta(hours=1)
    for units, job in sale_jobs:
        status = job["status"]
        end = _dt(job["end_date"])
        if status in _RUNNING:
            # A running job the latest pull no longer reports, past its
            # timer end, has delivered out of sight (its installer left
            # the pool, or its system the tracked set): dated at the
            # timer end so the hold can end under ruling (b) instead of
            # hiding stock for ever (review 2026-10-31).
            seen = _dt(job["seen_at"]) if "seen_at" in job.keys() else None
            stale = (
                last_pull is not None and seen is not None and end is not None
                and seen < last_pull - slack and end < last_pull
            )
            if not stale:
                hold.in_flight = True
                if end is not None:
                    ends.append(end)
                continue
            status = _DELIVERED
        if status == _DELIVERED:
            hold.delivered_units += units
            when = _dt(job["completed_date"]) or end or _dt(job["start_date"])
            if when is not None:
                delivered_dates.append(when)
    hold.delivered_qty = math.floor(hold.delivered_units * route.composite_qty * hold.yield_)
    if delivered_dates:
        hold.first_delivered = min(delivered_dates)
        if not hold.in_flight:
            hold.delivered_at = max(delivered_dates)
    if ends:
        hold.jobs_end = max(ends)
    if executed_run and not hold.covered and hold.for_sale_qty <= 0 and hold.planned_qty > 0:
        # The record shows no for-sale job and no pull reaches past
        # completed_at. Where a pull inside the window already covers it
        # up to the latest pull, the record speaks (nothing was installed
        # before it) and only the tail since is unknown — read it, as
        # the next update would. Only a window the record cannot speak
        # for at all falls back to the plan's figure, badged.
        lo_dt, hi_dt = _dt(window.lo), _dt(hi)
        partial = (
            last_pull is not None and lo_dt is not None and last_pull > lo_dt
            and covered(window.lo, min(hi_dt, last_pull) if hi_dt is not None else last_pull)
        )
        if not partial:
            hold.plan_figure = True
            hold.for_sale_qty = hold.planned_qty


def _life(hold: Hold, executed, max_executed: int, open_run_number, next_run_number) -> None:
    """Ruling (b) as a count: live while in flight, then while at most ONE
    executed run completed at or after the delivery."""
    if hold.superseded or hold.for_sale_qty <= 0:
        hold.live = False
        return
    if hold.in_flight:
        # Ruling (b): live while any for-sale job runs, whatever runs are
        # marked executed meanwhile; its end is dated once it delivers.
        hold.live = True
        return
    if hold.delivered_at is None:
        # A plan figure the record cannot date: live while its own run
        # is the newest executed or newer.
        hold.live = hold.run_number >= max_executed
        if hold.plan_figure:
            hold.expires_after = hold.run_number + 1
        return
    after = [(when, number) for when, number in executed if when >= hold.delivered_at]
    hold.live = len(after) <= 1
    if after:
        hold.expires_after = after[0][1] + 1
    elif open_run_number is not None:
        hold.expires_after = open_run_number + 1
    else:
        hold.expires_after = next_run_number + 1
    if len(after) >= 2:
        hold.expired_at = after[1][0]


def live(conn: sqlite3.Connection, ref, settings=None) -> list[Hold]:
    """The holds planning honours, oldest run first."""
    return [h for h in load(conn, ref, settings) if h.live]


def bites(conn: sqlite3.Connection, ref, settings) -> dict[int, Bite]:
    """What planning hides per composite (engine._apply_targets): the live
    holds' remaining units, less the units already listed on the market or
    sitting in outstanding contracts — those have left ESI assets. Each
    listed lot (ledger.listed_lots) is attributed by its ISSUE date to the
    delivered holds whose life contains it — first delivery to expiry,
    oldest run first, each up to its delivered, unreleased units — so an
    expired pile still on the market keeps its own listing and a live pile
    listed after an older hold ended is read as that live pile, not as the
    older remainder (review 2026-10-31); a lot issued outside every life
    is chain surplus and hides nothing."""
    from . import ledger

    holds = load(conn, ref, settings)
    by_composite: dict[int, list[Hold]] = {}
    for hold in holds:
        if hold.unrefined_type_id is None:
            continue
        by_composite.setdefault(hold.composite_type_id, []).append(hold)
    wanted = [c for c, hs in by_composite.items() if any(h.live and h.remaining > 0 for h in hs)]
    if not wanted:
        return {}
    lots = ledger.listed_lots(conn, wanted)
    out: dict[int, Bite] = {}
    for composite in wanted:
        hs = by_composite[composite]
        room = {id(h): max(0, h.delivered_qty - h.released_qty) for h in hs}
        listed_live = 0
        for issued_text, units in sorted(lots.get(composite, []), key=lambda lot: lot[0] or ""):
            issued = _dt(issued_text)
            left = int(units)
            for hold in hs:  # oldest run first
                if left <= 0:
                    break
                if hold.first_delivered is None or room[id(hold)] <= 0:
                    continue
                if issued is None:
                    if not hold.live:
                        continue  # undated: read as the live pile's
                elif issued < hold.first_delivered or (
                    hold.expired_at is not None and issued > hold.expired_at
                ):
                    continue
                take = min(left, room[id(hold)])
                room[id(hold)] -= take
                if hold.live:
                    listed_live += take
                left -= take
        remaining = sum(h.remaining for h in hs if h.live)
        if remaining <= 0:
            continue
        sample = hs[0]
        out[composite] = Bite(
            remaining=remaining,
            listed=listed_live,
            unrefined_type_id=sample.unrefined_type_id,
            composite_qty=sample.composite_qty,
        )
    return out


def write_plan_holds(conn: sqlite3.Connection, ref, settings, index_run_id: int, merged) -> None:
    """The plan's persist step: write this run's holds from the plan's
    unrefined rows. A row is ANCHORED at its first write: a formula job the
    record shows started at or before the anchor is the chain's (a late
    install of the previous run, savings jobs already running), one started
    after it belongs to this plan — and FREEZES the row, since the plan the
    user installed from is the last one written before those installs
    appeared (which holds inside one ESI update too: the pull writes the
    jobs, then the re-plan runs). Until then the row follows the plan
    (proposal, savings share, yield; the route cost per ruling (f) — a plan
    that priced an input at a fill stamps it, a later plan that buys
    nothing of the route keeps it). Rows are never deleted: a composite the
    plan dropped keeps its row with planned_qty 0. No commit."""
    from .engine import alchemy_sale_split  # local: engine imports this module

    routes = ref.alchemy_routes()
    window = buying.run_window(conn, index_run_id)
    last_pull = store.last_job_pull(conn)
    hi = (window.hi if window.hi is not None else _dt(last_pull)) if window else None
    # The anchor: this plan's snapshot time (planned_start, the cut
    # _replace_run_row stamps from the snapshot's fetched_at) — a job
    # the snapshot could have seen is the chain's, one started after it
    # was installed from a plan; ESI's clock on both sides.
    run_row = conn.execute(
        "SELECT planned_start FROM index_run WHERE index_run_id = ?", (int(index_run_id),)
    ).fetchone()
    anchor_candidates = [
        t for t in (_dt(run_row["planned_start"]) if run_row else None, _dt(last_pull))
        if t is not None
    ]
    # ... and never earlier than the latest job pull: a job that pull
    # already showed is the chain's, whatever snapshot the plan read.
    now_text = store.utc_text(
        max(anchor_candidates) if anchor_candidates else datetime.now(timezone.utc)
    )
    existing = {
        row["composite_type_id"]: row for row in store.hold_rows(conn, index_run_id)
    }
    yield_ = float(settings.alchemy_reprocess_yield)
    by_composite: dict[int, object] = {}
    for item in merged.values():
        composite = getattr(item, "alchemy_for_type_id", None)
        if composite is None:
            continue
        if item.alchemy_profit_jobs > 0 or composite in existing:
            by_composite[composite] = item
    for composite in existing:
        by_composite.setdefault(composite, None)

    for composite, item in by_composite.items():
        route = routes.get(composite)
        if route is None:
            continue
        installed_jobs: list[sqlite3.Row] = []
        if window is not None and hi is not None:
            installed_jobs = [
                j for j in store.jobs_in_window(conn, route.unrefined_id, window.lo, hi)
                if j["status"] in _INSTALLED
            ]
        installed = sum(int(j["units"] or 0) for j in installed_jobs)
        if item is None:
            savings, proposal, cost, from_fills = 0, 0, None, False
        else:
            savings = (
                max(0, int(item.jobs_allocated) - int(item.alchemy_profit_jobs))
                * int(item.max_runs_per_job or 0) * int(item.portion_size or 1)
            )
            _chain, sale_units = alchemy_sale_split(
                item.jobs_allocated, item.alchemy_profit_jobs,
                item.max_runs_per_job, item.portion_size, item.install_runs,
            )
            proposal = int(sale_units * route.composite_qty * yield_)
            cost = item.alchemy_route_unit_cost
            from_fills = bool(getattr(item, "alchemy_cost_from_fills", False))
        row = existing.get(composite)
        if row is None:
            # Anchored now: everything the record shows in the window is
            # the chain's (0 for a run planned this moment); what it shows
            # later, started after now, was installed from a plan.
            store.upsert_hold(
                conn, index_run_id, composite,
                planned_qty=proposal, route_unit_cost=cost,
                baseline_units=installed, chain_units=installed + savings, yield_=yield_,
                anchored_at=now_text, cost_from_fills=int(from_fills and cost is not None),
            )
            continue
        anchor = _dt(row["anchored_at"])
        if anchor is not None:
            pre = 0
            frozen = False
            for j in installed_jobs:
                started = _dt(j["start_date"]) or anchor
                if started <= anchor:
                    pre += int(j["units"] or 0)
                else:
                    frozen = True
        else:
            baseline = row["baseline_units"]
            if baseline is None:
                baseline = installed  # a row from before the column: anchor it now
            pre = int(baseline)
            frozen = installed > int(baseline)
        if frozen:
            continue  # ESI shows a job of this plan: the row is its record's
        fields = dict(
            planned_qty=proposal, baseline_units=pre,
            chain_units=pre + savings, yield_=yield_,
        )
        if cost is not None and (from_fills or not row["cost_from_fills"]):
            fields["route_unit_cost"] = cost
            fields["cost_from_fills"] = int(from_fills)
        if anchor is None:
            fields["anchored_at"] = now_text  # a legacy row: anchored from here on
        store.upsert_hold(conn, index_run_id, composite, **fields)


def holds_blocking_delete(conn: sqlite3.Connection, ref, settings, index_run_id: int) -> list[Hold]:
    """The run's holds that make a Discard refuse: a job installed after
    the row's anchor (the row is ESI's record — installs beyond the
    baseline on a legacy row), or any release (the sale history would
    cascade away and the installs would land in the next run's baseline as
    chain stock)."""
    return [
        h for h in load(conn, ref, settings, run_id=index_run_id)
        if h.frozen or h.installed or h.releases
    ]


def latest_ending(live_holds: list[Hold]) -> dict[int, Hold]:
    """Per composite, the live hold that ends LAST — the one the
    Stockpile note's "until" names when several holds hide one
    composite's stock (review 2026-10-31): an in-flight hold outlasts
    every delivered one (its end is not dated yet), delivered holds end
    at the later `expires_after`, and only then does the newer run win."""
    best: dict[int, Hold] = {}

    def key(h: Hold):
        return (h.in_flight, h.expires_after or 0, h.run_number)

    for h in live_holds:
        cur = best.get(h.composite_type_id)
        if cur is None or key(h) > key(cur):
            best[h.composite_type_id] = h
    return best


def expiry_words(hold: Hold) -> str:
    """When the hold ends, in the three states a hold passes through —
    or, once it has, when it did."""
    if hold.superseded:
        return "superseded — the run was reopened and re-planned"
    if not hold.live and hold.expired_at is not None and hold.expires_after is not None:
        return (
            f"ended when run {hold.expires_after} was marked executed "
            f"({hold.expired_at:%Y-%m-%d %H:%M})"
        )
    if not hold.live and hold.plan_figure:
        return "ended — a newer run is executed"
    if hold.delivered_at is not None and hold.expires_after is not None:
        return f"until run {hold.expires_after} is marked executed"
    if hold.installed or hold.in_flight:
        end = f" — jobs end {hold.jobs_end:%Y-%m-%d %H:%M}" if hold.jobs_end else ""
        return (
            "until the run after the one its jobs deliver in is marked executed" + end
        )
    if hold.plan_figure and hold.expires_after is not None:
        return f"until run {hold.expires_after} is marked executed (plan figure)"
    return "not installed yet"


def changed_words(
    ref, before: list[Hold], after: list[Hold], *, reopened: int | None = None
) -> list[str]:
    """Flash clauses for the holds a status change ended or revived (Mark
    executed / Reopen): "run 7's for-sale hold ended: 17,615 Hyperflurite
    unsold now count as stock". A hold standing at the plan's figure was
    never a pile, so its end is worded apart ("plan-figure hold ended" /
    "withdrawn" for the reopened run's own stand-in, `reopened` = its
    index_run_id): nothing of it was installed, delivered or unsold."""
    key = lambda h: (h.index_run_id, h.composite_type_id)  # noqa: E731
    was = {key(h): h for h in before if h.live}
    now = {key(h): h for h in after if h.live}
    words: list[str] = []
    for k, hold in sorted(was.items()):
        if k not in now and hold.remaining > 0:
            name = ref.type_info(hold.composite_type_id).name
            if hold.plan_figure and hold.index_run_id == reopened:
                # The reopened run's own stand-in: an open run holds only
                # what an ESI update shows installed.
                words.append(
                    f"run {hold.run_number}'s plan-figure hold withdrawn: "
                    f"{hold.remaining:,} {name} count as stock again — the reopened "
                    f"run holds only what an ESI update shows installed"
                )
            elif hold.plan_figure:
                words.append(
                    f"run {hold.run_number}'s plan-figure hold ended: "
                    f"{hold.remaining:,} {name} count as stock again"
                )
            else:
                words.append(
                    f"run {hold.run_number}'s for-sale hold ended: {hold.remaining:,} "
                    f"{name} unsold now count as stock"
                )
    for k, hold in sorted(now.items()):
        if k not in was and hold.remaining > 0:
            name = ref.type_info(hold.composite_type_id).name
            if hold.plan_figure:
                # Mark executed before a pull covers the run: the plan's
                # figure stands in until the next ESI update, as it does
                # for hulls — not a revival.
                words.append(
                    f"run {hold.run_number}'s for-sale hold stands at the plan's figure "
                    f"until the next ESI update: {hold.remaining:,} {name} hidden from stock"
                )
            else:
                words.append(
                    f"run {hold.run_number}'s for-sale hold is live again: {hold.remaining:,} "
                    f"{name} hidden from stock"
                )
    return words
