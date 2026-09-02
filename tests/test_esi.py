"""refresh_state scoping, corp role fallback, and location resolution —
all network calls monkeypatched; real reference data for portion sizes.

Scope under test (decisions 2026-08-20, asset toggles 2026-08-25): stock =
corporation assets (per-corp opt-out) plus personal hangars only for
characters opted in; in-progress = corp AND personal jobs, tracked-system
filtered by delivery location; corp endpoints fall back per family to a
role-holding character.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from magoo import config, engine, esi, store

TRACKED = 30000142  # Jita
UNTRACKED = 30099999
STATION = 60003760  # NPC station in the tracked system
PLAYER_CORP = 98000001


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    store.ensure_schema(c)
    c.execute("INSERT INTO tracked_system VALUES (?)", (TRACKED,))
    c.execute(
        "INSERT INTO location_system (location_id, solar_system_id) "
        "VALUES (?, ?)",
        (STATION, TRACKED),
    )
    c.commit()
    yield c
    c.close()


def add_character(conn, character_id, assets=1, slots=1, personal=0):
    conn.execute(
        "INSERT INTO pool_character (character_id, character_name, "
        "include_assets, include_job_slots, count_assets) "
        "VALUES (?, ?, ?, ?, ?)",
        (character_id, f"char {character_id}", assets, slots, personal),
    )
    conn.commit()


def corp_asset(item_id, type_id, qty, location_id=STATION, *,
               location_type="station", flag="CorpSAG1", singleton=False):
    return {
        "item_id": item_id,
        "type_id": type_id,
        "quantity": qty,
        "location_id": location_id,
        "location_type": location_type,
        "location_flag": flag,
        "is_singleton": singleton,
    }


def job(job_id, installer, product, runs=5, location=STATION, end=None,
        activity=1, blueprint_type=None):
    record = {
        "job_id": job_id,
        "installer_id": installer,
        "activity_id": activity,
        "status": "active",
        "product_type_id": product,
        "runs": runs,
        "output_location_id": location,
        "end_date": end or "2099-01-01T00:00:00Z",
    }
    if blueprint_type is not None:
        record["blueprint_type_id"] = blueprint_type
    return record


def patch_pull(monkeypatch, *, personal_jobs=None, personal_assets=None,
               corp_assets=None, corp_jobs=None, corp_wallets=None,
               corp_of=None):
    """Wire refresh_state's fetchers to canned data. The corp_* values are
    either a dict keyed by character_id (per-character answers, None = 403)
    or a plain value applied to every character."""
    def per_char(table, default):
        def fetch(conn, character_id, corporation_id=None):
            if isinstance(table, dict):
                return table.get(character_id, default)
            return table if table is not None else default
        return fetch

    monkeypatch.setattr(
        esi, "fetch_industry_jobs",
        lambda conn, cid: (personal_jobs or {}).get(cid, []),
    )
    monkeypatch.setattr(
        esi, "fetch_assets",
        lambda conn, cid: (personal_assets or {}).get(cid, []),
    )
    monkeypatch.setattr(
        esi, "corporation_name", lambda corp_id: f"corp {corp_id}"
    )
    monkeypatch.setattr(esi, "fetch_character_wallet", lambda conn, cid: 100.0)
    monkeypatch.setattr(esi, "fetch_corp_assets", per_char(corp_assets, None))
    monkeypatch.setattr(esi, "fetch_corp_industry_jobs", per_char(corp_jobs, None))
    monkeypatch.setattr(esi, "fetch_corp_wallets", per_char(corp_wallets, None))
    monkeypatch.setattr(
        esi, "character_corporation_id",
        lambda cid: (corp_of or {}).get(cid, PLAYER_CORP),
    )


def test_stock_counts_corp_assets_only(conn, ref, monkeypatch):
    """Personal hangars are invisible to planning — even with
    include_assets set, only corp assets feed on_hand."""
    add_character(conn, 2001)
    tritanium = ref.type_id("Tritanium")
    patch_pull(
        monkeypatch,
        corp_assets={2001: [corp_asset(1, tritanium, 5000)]},
        corp_jobs={2001: []},
        corp_wallets={2001: 1e9},
    )
    state = esi.refresh_state(conn, ref)
    assert state["on_hand"] == {tritanium: 5000}
    assert state["character_isk"] == 100.0  # include_assets gates wallet
    assert state["corporation_isk"] == 1e9


def _fitted_scenario(ref):
    """An anchored Astrahus in the tracked system with a rig fitted and fuel
    in the bay, a packaged Astrahus and a loose rig in a station hangar."""
    astrahus = ref.type_id("Astrahus")
    rig = ref.type_id("Standup M-Set Structure Manufacturing Material Efficiency I")
    fuel = ref.type_id("Nitrogen Fuel Block")
    assets = [
        corp_asset(100, astrahus, 1, TRACKED, location_type="solar_system",
                   flag="AutoFit", singleton=True),          # anchored
        corp_asset(101, rig, 1, 100, location_type="item", flag="RigSlot0",
                   singleton=True),                           # fitted rig
        corp_asset(102, fuel, 5000, 100, location_type="item",
                   flag="StructureFuel"),                     # fuel bay
        corp_asset(103, fuel, 700, 100, location_type="item",
                   flag="CorpSAG2"),                           # corp hangar inside it
        corp_asset(104, astrahus, 1, STATION),                # packaged in station
        corp_asset(105, rig, 3, STATION),                     # loose rigs
    ]
    return astrahus, rig, fuel, assets


def test_fitted_and_deployed_excluded_from_stock_by_default(conn, ref, monkeypatch):
    add_character(conn, 2001)
    astrahus, rig, fuel, assets = _fitted_scenario(ref)
    patch_pull(monkeypatch, corp_assets={2001: assets},
               corp_jobs={2001: []}, corp_wallets={2001: 0.0})
    state = esi.refresh_state(conn, ref)
    # anchored structure and its fitted rig / fuel bay are skipped; the corp
    # hangar inside the structure, the packaged hull and loose rigs count
    assert state["on_hand"] == {astrahus: 1, rig: 3, fuel: 700}


def test_ship_fittings_and_deployed_assets_excluded(conn, ref, monkeypatch):
    """The other half of the rule: an assembled carrier in a corp hangar
    keeps its hull and cargo as stock but not its fitted module, drones or
    fighters; an anchored non-Upwell singleton (a POCO / sov hub sitting in
    space) is deployed, not stock; a singleton of an unknown type counts."""
    add_character(conn, 2001)
    thanatos = ref.type_id("Thanatos")
    module = ref.type_id("Capital Armor Repairer I")
    drone = ref.type_id("Hammerhead II")
    fighter = ref.type_id("Templar I")
    trit = ref.type_id("Tritanium")
    poco = ref.type_id("Customs Office")
    assets = [
        corp_asset(200, thanatos, 1, STATION, flag="CorpSAG1", singleton=True),
        corp_asset(201, module, 1, 200, location_type="item", flag="HiSlot0",
                   singleton=True),
        corp_asset(202, drone, 5, 200, location_type="item", flag="DroneBay"),
        corp_asset(203, fighter, 9, 200, location_type="item", flag="FighterBay"),
        corp_asset(204, trit, 1000, 200, location_type="item", flag="Cargo"),
        corp_asset(205, poco, 1, TRACKED, location_type="solar_system",
                   flag="AutoFit", singleton=True),
        corp_asset(206, 999999999, 1, STATION, singleton=True),  # unknown type
    ]
    patch_pull(monkeypatch, corp_assets={2001: assets},
               corp_jobs={2001: []}, corp_wallets={2001: 0.0})
    state = esi.refresh_state(conn, ref)
    assert state["on_hand"] == {thanatos: 1, trit: 1000, 999999999: 1}


def test_fitted_and_deployed_counted_when_setting_on(conn, ref, monkeypatch):
    add_character(conn, 2001)
    conn.execute("UPDATE settings SET count_fitted_stock = 1")
    conn.commit()
    astrahus, rig, fuel, assets = _fitted_scenario(ref)
    patch_pull(monkeypatch, corp_assets={2001: assets},
               corp_jobs={2001: []}, corp_wallets={2001: 0.0})
    state = esi.refresh_state(conn, ref)
    assert state["on_hand"] == {astrahus: 2, rig: 4, fuel: 5700}


def test_all_jobs_credit_in_progress_regardless_of_slot_flag(conn, ref, monkeypatch):
    """A slots-excluded character's in-flight output still counts as stock
    (it delivers into corp hangars); only the slot count is gated."""
    add_character(conn, 2001, slots=0)
    merlin = ref.type_id("Merlin")
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [job(1, 2001, merlin, runs=5)]},
        corp_assets={2001: []},
        corp_jobs={2001: []},
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert state["in_progress"].get(merlin, 0) == 5
    assert state["active_jobs"][config.ACTIVITY_MANUFACTURING] == 0


def test_job_output_scoped_to_tracked_systems(conn, ref, monkeypatch):
    """Output delivering into an untracked system never becomes tracked
    stock, so its credit is dropped; an unresolvable delivery location
    keeps the credit (dropping it wrongly would double-build)."""
    add_character(conn, 2001)
    merlin = ref.type_id("Merlin")
    rifter = ref.type_id("Rifter")
    condor = ref.type_id("Condor")
    untracked_station = 60000001
    unresolvable = 1_000_000_000_001
    conn.execute(
        "INSERT INTO location_system (location_id, solar_system_id) "
        "VALUES (?, ?)",
        (untracked_station, UNTRACKED),
    )
    conn.commit()
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [
            job(1, 2001, merlin, runs=5, location=STATION),
            job(2, 2001, rifter, runs=7, location=untracked_station),
            job(3, 2001, condor, runs=3, location=unresolvable),
        ]},
        corp_assets={2001: []},
        corp_jobs={2001: []},
        corp_wallets={2001: 0.0},
    )
    # The unresolvable structure answers 502 (transient) if asked.
    monkeypatch.setattr(
        esi, "_get",
        lambda *a, **k: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "502", request=httpx.Request("GET", "https://esi"),
                response=httpx.Response(502),
            )
        ),
    )
    state = esi.refresh_state(conn, ref)
    assert state["in_progress"].get(merlin, 0) == 5
    assert rifter not in state["in_progress"]
    assert state["in_progress"].get(condor, 0) == 3
    # All three jobs still occupy lines (slot counting is location-blind).
    assert state["active_jobs"][config.ACTIVITY_MANUFACTURING] == 3


def test_corp_families_fall_back_to_role_holder(conn, ref, monkeypatch):
    """CharA (enumerated first) lacks every corp role; CharB holds them.
    Each endpoint family must fall through to CharB instead of burning the
    corp on CharA's 403."""
    add_character(conn, 2001)
    add_character(conn, 2002)
    tritanium = ref.type_id("Tritanium")
    merlin = ref.type_id("Merlin")
    patch_pull(
        monkeypatch,
        corp_assets={2001: None, 2002: [corp_asset(1, tritanium, 5000)]},
        corp_jobs={2001: None, 2002: [job(9, 2002, merlin, runs=4)]},
        corp_wallets={2001: None, 2002: 2e9},
    )
    state = esi.refresh_state(conn, ref)
    assert state["on_hand"] == {tritanium: 5000}
    assert state["in_progress"].get(merlin, 0) == 4
    assert state["corporation_isk"] == 2e9


def test_corp_pulled_once_when_first_character_has_roles(conn, ref, monkeypatch):
    """No double-counting: once a family succeeds for a corp, later pool
    characters of the same corp are not asked again."""
    add_character(conn, 2001)
    add_character(conn, 2002)
    tritanium = ref.type_id("Tritanium")
    calls = []

    def assets(conn_, cid, corp):
        calls.append(cid)
        return [corp_asset(1, tritanium, 5000)]

    patch_pull(monkeypatch, corp_jobs=[], corp_wallets=0.0)
    monkeypatch.setattr(esi, "fetch_corp_assets", assets)
    state = esi.refresh_state(conn, ref)
    assert calls == [2001]
    assert state["on_hand"] == {tritanium: 5000}


def test_personal_assets_counted_when_flag_on(conn, ref, monkeypatch):
    """count_assets opts a character's PERSONAL hangars into stock —
    tracked-system filtered like corp assets; a flag-off character's
    hangars stay invisible."""
    add_character(conn, 2001, personal=1)
    add_character(conn, 2002)  # count_assets defaults off
    tritanium = ref.type_id("Tritanium")
    merlin = ref.type_id("Merlin")
    untracked_station = 60000001
    conn.execute(
        "INSERT INTO location_system (location_id, solar_system_id) "
        "VALUES (?, ?)",
        (untracked_station, UNTRACKED),
    )
    conn.commit()
    patch_pull(
        monkeypatch,
        personal_assets={
            2001: [corp_asset(1, tritanium, 800),
                   corp_asset(2, tritanium, 50, untracked_station)],
            2002: [corp_asset(3, merlin, 2)],
        },
        corp_assets=[], corp_jobs=[], corp_wallets=0.0,
    )
    state = esi.refresh_state(conn, ref)
    assert state["on_hand"] == {tritanium: 800}


def test_corp_asset_optout_skips_pull(conn, ref, monkeypatch):
    """esi_corp.count_assets = 0 excludes the corp's hangars from stock and
    skips the (expensive paginated) assets pull entirely; jobs and wallet
    still pull."""
    add_character(conn, 2001)
    conn.execute(
        "INSERT INTO esi_corp (corporation_id, count_assets) VALUES (?, 0)",
        (PLAYER_CORP,),
    )
    conn.commit()
    tritanium = ref.type_id("Tritanium")
    merlin = ref.type_id("Merlin")
    calls = []

    def assets(conn_, cid, corp):
        calls.append(cid)
        return [corp_asset(1, tritanium, 5000)]

    patch_pull(
        monkeypatch,
        corp_jobs={2001: [job(9, 2001, merlin, runs=4)]},
        corp_wallets={2001: 2e9},
    )
    monkeypatch.setattr(esi, "fetch_corp_assets", assets)
    state = esi.refresh_state(conn, ref)
    assert calls == []
    assert state["on_hand"] == {}
    assert state["in_progress"].get(merlin, 0) == 4
    assert state["corporation_isk"] == 2e9


def test_corp_wallet_and_jobs_optout_skip_pulls(conn, ref, monkeypatch):
    """count_wallet / count_jobs off skip those corp pulls: no buying-power
    ISK, no in-progress credit from corp-feed jobs. Assets still pull, and
    the character's OWN jobs still count via the character feed."""
    add_character(conn, 2001)
    conn.execute(
        "INSERT INTO esi_corp (corporation_id, count_wallet, count_jobs) "
        "VALUES (?, 0, 0)",
        (PLAYER_CORP,),
    )
    conn.commit()
    tritanium = ref.type_id("Tritanium")
    merlin = ref.type_id("Merlin")
    rifter = ref.type_id("Rifter")
    jobs_calls, wallet_calls = [], []

    def jobs_fetch(conn_, cid, corp):
        jobs_calls.append(cid)
        return [job(9, 5555, rifter, runs=4)]  # non-pool installer

    def wallet_fetch(conn_, cid, corp):
        wallet_calls.append(cid)
        return 2e9

    patch_pull(
        monkeypatch,
        personal_jobs={2001: [job(1, 2001, merlin, runs=2)]},
        corp_assets={2001: [corp_asset(1, tritanium, 10)]},
    )
    monkeypatch.setattr(esi, "fetch_corp_industry_jobs", jobs_fetch)
    monkeypatch.setattr(esi, "fetch_corp_wallets", wallet_fetch)
    state = esi.refresh_state(conn, ref)
    assert jobs_calls == [] and wallet_calls == []
    assert state["corporation_isk"] == 0.0
    assert rifter not in state["in_progress"]
    assert state["in_progress"].get(merlin, 0) == 2
    assert state["on_hand"] == {tritanium: 10}


def test_corp_feed_jobs_count_slots_via_corp_auth(conn, ref, monkeypatch):
    """Slot occupancy and multi-cycle end dates come from the corp feed
    regardless of the installer's character-level flag: corp ESI carries
    installer and end date, so corp auth alone covers corp-hangar jobs
    (2026-08-25). The character flag governs only PERSONAL jobs — here
    it is off, so the personal job credits stock but occupies no slot."""
    add_character(conn, 2001, slots=0)
    merlin = ref.type_id("Merlin")
    rifter = ref.type_id("Rifter")
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [job(1, 2001, merlin, runs=2)]},
        corp_assets={2001: []},
        corp_jobs={2001: [job(2, 2001, rifter, runs=4),
                          job(3, 5555, rifter, runs=1)]},  # non-pool member
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert state["active_jobs"][config.ACTIVITY_MANUFACTURING] == 2
    assert state["in_progress"].get(merlin, 0) == 2
    assert state["in_progress"].get(rifter, 0) == 5


def test_refresh_records_corp_pull_provenance(conn, ref, monkeypatch):
    """Each refresh upserts who answered each corp endpoint family and how
    many rows came back; the user's count_assets toggle survives the
    upsert."""
    add_character(conn, 2001)
    add_character(conn, 2002)
    tritanium = ref.type_id("Tritanium")
    merlin = ref.type_id("Merlin")
    canned = dict(
        corp_assets={2001: None, 2002: [corp_asset(1, tritanium, 5000),
                                        corp_asset(2, tritanium, 100)]},
        corp_jobs={2001: [job(9, 2001, merlin, runs=4)]},
        corp_wallets={2001: None, 2002: None},
    )
    patch_pull(monkeypatch, **canned)
    esi.refresh_state(conn, ref)
    row = conn.execute("SELECT * FROM esi_corp").fetchone()
    assert row["corporation_id"] == PLAYER_CORP
    assert row["corporation_name"] == f"corp {PLAYER_CORP}"
    assert row["assets_via"] == 2002 and row["asset_rows"] == 2
    assert row["jobs_via"] == 2001 and row["job_rows"] == 1
    assert row["wallet_via"] is None
    assert row["count_assets"] == 1

    # The user opts the corp out; the next refresh preserves the toggle
    # and records the skipped pull as no answer.
    conn.execute("UPDATE esi_corp SET count_assets = 0")
    conn.commit()
    patch_pull(monkeypatch, **canned)
    state = esi.refresh_state(conn, ref)
    row = conn.execute("SELECT * FROM esi_corp").fetchone()
    assert row["count_assets"] == 0
    assert row["assets_via"] is None
    assert state["on_hand"] == {}


def test_transient_location_failure_is_not_cached(conn, monkeypatch):
    """A 502 must not poison the location cache; a later resolve succeeds.
    Definitive misses (403/404) are cached as stamped NULL rows, honored
    within the 24h TTL and re-probed after it (docking ACLs change)."""
    structure = 1_000_000_000_002

    def failing_get(conn_, cid, path, params=None):
        raise httpx.HTTPStatusError(
            "502", request=httpx.Request("GET", "https://esi"),
            response=httpx.Response(502),
        )

    monkeypatch.setattr(esi, "_get", failing_get)
    assert esi._resolve_location(conn, 2001, structure) is None
    row = conn.execute(
        "SELECT * FROM location_system WHERE location_id = ?", (structure,)
    ).fetchone()
    assert row is None  # not cached — will retry next refresh

    monkeypatch.setattr(
        esi, "_get", lambda *a, **k: ({"solar_system_id": TRACKED}, {})
    )
    assert esi._resolve_location(conn, 2001, structure) == TRACKED

    denied = 1_000_000_000_003
    probes = []

    def forbidden_get(conn_, cid, path, params=None):
        probes.append(path)
        raise httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", "https://esi"),
            response=httpx.Response(403),
        )

    monkeypatch.setattr(esi, "_get", forbidden_get)
    assert esi._resolve_location(conn, 2001, denied) is None
    row = conn.execute(
        "SELECT solar_system_id, fetched_at FROM location_system "
        "WHERE location_id = ?",
        (denied,),
    ).fetchone()
    assert row is not None and row["solar_system_id"] is None  # cached miss
    assert row["fetched_at"] is not None  # stamped for the TTL

    # within the TTL the cached miss answers without a network probe
    assert esi._resolve_location(conn, 2001, denied) is None
    assert len(probes) == 1

    # past the TTL the row is a MISS again — the next lookup re-probes
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    conn.execute(
        "UPDATE location_system SET fetched_at = ? WHERE location_id = ?",
        (stale.isoformat(), denied),
    )
    conn.commit()
    assert esi._resolve_location(conn, 2001, denied) is None
    assert len(probes) == 2


def test_expired_null_location_heals_to_real_system(conn, monkeypatch):
    """An expired NULL row (and a legacy unstamped one) is re-probed; when
    the API answers 200 the row heals in place — a docking-ACL 403 no
    longer drops that structure's assets forever."""
    expired = 1_000_000_000_004
    legacy = 1_000_000_000_005
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    conn.execute(
        "INSERT INTO location_system (location_id, solar_system_id, "
        "fetched_at) VALUES (?, NULL, ?)",
        (expired, stale.isoformat()),
    )
    conn.execute(
        "INSERT INTO location_system (location_id, solar_system_id) "
        "VALUES (?, NULL)",  # pre-TTL row: no stamp
        (legacy,),
    )
    conn.commit()
    monkeypatch.setattr(
        esi, "_get", lambda *a, **k: ({"solar_system_id": TRACKED}, {})
    )
    assert esi._resolve_location(conn, 2001, expired) == TRACKED
    assert esi._resolve_location(conn, 2001, legacy) == TRACKED
    for location_id in (expired, legacy):
        row = conn.execute(
            "SELECT solar_system_id, fetched_at FROM location_system "
            "WHERE location_id = ?",
            (location_id,),
        ).fetchone()
        assert row["solar_system_id"] == TRACKED
        assert row["fetched_at"] is not None


def test_asset_safety_wrap_contents_excluded_from_stock(conn, ref, monkeypatch):
    """Anything whose containment chain passes through an Asset Safety
    Wrap is locked 5-20 days — not usable stock, even when only an outer
    link (not the asset's own flag) carries AssetSafety. A sibling outside
    the wrap still counts."""
    add_character(conn, 2001)
    tritanium = ref.type_id("Tritanium")
    assets = [
        corp_asset(300, 60, 1, TRACKED, location_type="solar_system",
                   flag="AssetSafety", singleton=True),      # the wrap
        corp_asset(301, 999999998, 1, 300, location_type="item",
                   flag="AssetSafety", singleton=True),      # container inside
        corp_asset(302, tritanium, 4000, 301, location_type="item",
                   flag="Unlocked"),                         # chain check only
        corp_asset(303, tritanium, 250, STATION),            # normal hangar
    ]
    patch_pull(monkeypatch, corp_assets={2001: assets},
               corp_jobs={2001: []}, corp_wallets={2001: 0.0})
    state = esi.refresh_state(conn, ref)
    assert state["on_hand"] == {tritanium: 250}


def test_corp_jobs_paginated(conn, monkeypatch):
    pages = {1: [{"job_id": 1}], 2: [{"job_id": 2}]}

    def fake_get(conn_, cid, path, params=None):
        page = (params or {}).get("page", 1)
        return pages[page], {"X-Pages": "2"}

    monkeypatch.setattr(esi, "_get", fake_get)
    jobs = esi.fetch_corp_industry_jobs(conn, 2001, PLAYER_CORP)
    assert [j["job_id"] for j in jobs] == [1, 2]


def test_retry_after_http_date_does_not_crash(monkeypatch):
    """RFC 9110 allows Retry-After as an HTTP-date; int() on it used to
    ValueError out of the whole refresh."""
    responses = iter([
        httpx.Response(
            429,
            headers={
                "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT",
                "X-ESI-Error-Limit-Remain": "junk",
                "X-ESI-Error-Limit-Reset": "1",
            },
        ),
        httpx.Response(200, json={"ok": True}),
    ])
    monkeypatch.setattr(esi.httpx, "get", lambda *a, **k: next(responses))
    monkeypatch.setattr(esi.time, "sleep", lambda s: None)
    resp = esi.esi_request("https://esi.example/latest/x/")
    assert resp.status_code == 200


# --- OAuth token layer (refresh grant) ---------------------------------------


def seed_token(conn, character_id=2001, expires_in=-100):
    """An esi_token row whose access token is already stale (expires_in
    seconds from now), so the next use must refresh. No credentials to seed:
    the client id is shipped in config and there is no secret at all."""
    expires = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    conn.execute(
        "INSERT INTO esi_token (character_id, refresh_token, access_token, "
        "expires_at, scopes) VALUES (?, ?, ?, ?, ?)",
        (character_id, "rt-old", "at-old", expires.isoformat(), ""),
    )
    conn.commit()


def refresh_post(posts, tokens=None):
    """An esi.httpx.post stand-in for the SSO token endpoint recording
    each form it was sent."""
    body = tokens or {
        "access_token": "at-new",
        "refresh_token": "rt-new",  # SSO rotates the refresh token
        "expires_in": 1199,
    }

    def fake_post(url, data=None, headers=None, timeout=None):
        posts.append(dict(data or {}))
        return httpx.Response(
            200, json=body, request=httpx.Request("POST", url)
        )

    return fake_post


def fake_claims(monkeypatch, character_id=2001):
    """JWT validation hits SSO's JWKS over the network — stub the decode,
    keeping the claim shape _store_tokens reads."""
    monkeypatch.setattr(
        esi, "_decode_token",
        lambda token: {
            "sub": f"CHARACTER:EVE:{character_id}",
            "name": f"char {character_id}",
            "scp": ["esi-assets.read_assets.v1"],
        },
    )


def test_expired_token_refreshes_once_and_request_succeeds(conn, monkeypatch):
    """A stale access token triggers exactly one refresh POST; the original
    GET then goes out with the new bearer and succeeds."""
    seed_token(conn)
    fake_claims(monkeypatch)
    posts = []
    monkeypatch.setattr(esi.httpx, "post", refresh_post(posts))
    bearers = []

    def fake_get(url, params=None, headers=None, timeout=None):
        bearers.append(headers["Authorization"])
        return httpx.Response(
            200, json=[{"item_id": 1}], request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(esi.httpx, "get", fake_get)
    data, _headers = esi._get(conn, 2001, "/characters/2001/assets/")
    assert data == [{"item_id": 1}]
    assert len(posts) == 1
    assert posts[0]["grant_type"] == "refresh_token"
    assert posts[0]["refresh_token"] == "rt-old"
    assert bearers == ["Bearer at-new"]


def test_rotated_refresh_token_is_persisted(conn, monkeypatch):
    """SSO rotates refresh tokens on use — losing the new one would strand
    the character at the next refresh."""
    seed_token(conn)
    fake_claims(monkeypatch)
    posts = []
    monkeypatch.setattr(esi.httpx, "post", refresh_post(posts))
    assert esi.access_token(conn, 2001) == "at-new"
    row = conn.execute(
        "SELECT refresh_token, access_token, expires_at FROM esi_token "
        "WHERE character_id = 2001"
    ).fetchone()
    assert row["refresh_token"] == "rt-new"
    assert row["access_token"] == "at-new"
    # and the stored expiry is fresh: the next call needs no second POST
    assert esi.access_token(conn, 2001) == "at-new"
    assert len(posts) == 1


def test_invalid_grant_surfaces_clear_reauth_error(conn, monkeypatch):
    """A 400 (invalid_grant: token revoked/expired) from the refresh must
    name the character and say to re-authenticate — not surface a bare
    HTTPStatusError from the token endpoint, and never retry-loop."""
    seed_token(conn)
    posts = []

    def rejecting_post(url, data=None, headers=None, timeout=None):
        posts.append(dict(data or {}))
        return httpx.Response(
            400, json={"error": "invalid_grant"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(esi.httpx, "post", rejecting_post)
    with pytest.raises(RuntimeError, match=r"2001.*re-authenticate"):
        esi.access_token(conn, 2001)
    assert len(posts) == 1  # no retry loop against a dead token


def test_snapshot_pruned_to_recent_five(conn):
    for i in range(8):
        store.save_esi_snapshot(conn, {}, {}, {}, 0.0, 0.0)
    count = conn.execute("SELECT COUNT(*) AS n FROM esi_snapshot").fetchone()
    assert count["n"] == 5


def test_multi_cycle_jobs_net_from_slot_pool(conn):
    """A job still running past the next index run occupies a real line;
    jobs ending inside the window do not reduce the pool (v1.1)."""
    conn.execute(
        "UPDATE settings SET manufacturing_slots = 10, reaction_slots = 4"
    )
    conn.commit()
    store.save_esi_snapshot(
        conn, {}, {}, {config.ACTIVITY_MANUFACTURING: 3}, 0.0, 0.0,
        job_ends={
            config.ACTIVITY_MANUFACTURING: [
                "2099-01-01T00:00:00Z",  # far beyond any window
                "2099-01-02T00:00:00Z",
                "2001-01-01T00:00:00Z",  # long since ended
                "not a date",  # ignored
            ],
            config.ACTIVITY_REACTION: [],
        },
    )
    snap = engine.snapshot_from_state(conn)
    assert snap.slots_available[config.ACTIVITY_MANUFACTURING] == 8
    assert snap.slots_available[config.ACTIVITY_REACTION] == 4


# --- Lab job crediting (v1.23) ----------------------------------------------


def test_invention_jobs_credit_attempts_no_slots(conn, ref, monkeypatch):
    """Activity-8 jobs credit RAW attempts under the invented blueprint
    type (the engine converts to expected copies) and never touch the
    slot pools."""
    add_character(conn, 2001)
    zealot_bp = 12004  # Zealot Blueprint (the invented type)
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [job(1, 2001, zealot_bp, runs=6, activity=8)]},
        corp_assets={2001: []},
        corp_jobs={2001: []},
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert state["in_progress"].get(zealot_bp, 0) == 6  # attempts, portion 1
    assert state["active_jobs"][config.ACTIVITY_MANUFACTURING] == 0
    assert state["active_jobs"][config.ACTIVITY_REACTION] == 0
    assert state["job_ends"][config.ACTIVITY_MANUFACTURING] == []


def test_copy_jobs_credit_licensed_runs_by_blueprint_type(conn, ref, monkeypatch):
    """Activity-5 jobs key on blueprint_type_id (product_type_id is
    optional in ESI) and credit copies × licensed runs — one run per copy
    when the record carries no licensed_runs."""
    add_character(conn, 2001)
    omen_bp = 2007  # Omen Blueprint
    with_runs = job(1, 2001, None, runs=4, activity=5, blueprint_type=omen_bp)
    del with_runs["product_type_id"]
    with_runs["licensed_runs"] = 10
    bare = job(2, 2001, None, runs=3, activity=5, blueprint_type=omen_bp)
    del bare["product_type_id"]
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [with_runs, bare]},
        corp_assets={2001: []},
        corp_jobs={2001: []},
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert state["in_progress"].get(omen_bp, 0) == 4 * 10 + 3
    assert state["active_jobs"][config.ACTIVITY_MANUFACTURING] == 0


def test_copy_job_on_invented_blueprint_is_not_an_attempt(conn, ref, monkeypatch):
    """Review 2026-09-01: a copy job on a T2 blueprint ORIGINAL keys the
    same in_progress slot as invention attempts on that type, which the
    engine converts at the invention chance — so it is not credited at
    all (the copies count as stock once delivered)."""
    add_character(conn, 2001)
    zealot_bp = 12004  # an invented (T2) blueprint type
    copy = job(1, 2001, None, runs=10, activity=5, blueprint_type=zealot_bp)
    del copy["product_type_id"]
    copy["licensed_runs"] = 10
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [copy]},
        corp_assets={2001: []},
        corp_jobs={2001: []},
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert zealot_bp not in state["in_progress"]


def test_research_jobs_still_dropped(conn, ref, monkeypatch):
    add_character(conn, 2001)
    omen_bp = 2007
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [
            job(1, 2001, omen_bp, runs=3, activity=3),
            job(2, 2001, omen_bp, runs=3, activity=4),
        ]},
        corp_assets={2001: []},
        corp_jobs={2001: []},
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert omen_bp not in state["in_progress"]


def test_lab_job_delivery_filter_applies(conn, ref, monkeypatch):
    """A lab job delivering into an untracked system contributes nothing
    (its output BPC will never appear in tracked stock)."""
    add_character(conn, 2001)
    zealot_bp = 12004
    conn.execute(
        "INSERT INTO location_system (location_id, solar_system_id) "
        "VALUES (?, ?)",
        (60099999, UNTRACKED),
    )
    conn.commit()
    patch_pull(
        monkeypatch,
        personal_jobs={2001: [
            job(1, 2001, zealot_bp, runs=6, activity=8, location=60099999)
        ]},
        corp_assets={2001: []},
        corp_jobs={2001: []},
        corp_wallets={2001: 0.0},
    )
    state = esi.refresh_state(conn, ref)
    assert zealot_bp not in state["in_progress"]
