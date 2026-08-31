"""ESI integration: EVE SSO OAuth2, token storage/refresh, endpoint fetchers,
and Snapshot assembly (PROJECT.md §8).

Authenticates as a PUBLIC client using PKCE, with the application's client id
shipped in config.ESI_CLIENT_ID. No client secret is sent or stored: CCP
documents the client id as public, and PKCE exists precisely so a distributed
app can ship without a secret — one embedded in a binary would not be secret
anyway. The web UI drives login (web.sso_login), which sends the user to
their own browser per RFC 8252.

Tokens live in the esi_token table. The plan is advisory; ESI is the ledger.

CLI:
    python -m magoo.esi status       # authed characters + token health
    python -m magoo.esi test         # pull assets/jobs for the pool
"""

import base64
import hashlib
import os
import secrets
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from jwt import PyJWKClient

from . import config, store

SSO_AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
SSO_JWKS_URL = "https://login.eveonline.com/oauth/jwks"
ESI_BASE = "https://esi.evetech.net/latest"

USER_AGENT = config.USER_AGENT


def _int_header(headers, name: str, default: int) -> int:
    """Defensive integer header parse: Retry-After may legally be an
    HTTP-date (RFC 9110), and a fronting proxy can send junk — either used
    to crash esi_request with ValueError mid-refresh."""
    try:
        return int(headers.get(name, default) or default)
    except (TypeError, ValueError):
        return default


def esi_request(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """All ESI traffic funnels through here for CCP-guideline compliance:
    descriptive User-Agent, error-limit awareness (back off before the 420
    window trips, honor Retry-After when it does), and retry on transient
    5xx."""
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        merged.update(headers)
    resp = None
    for attempt in range(4):
        if client is not None:
            resp = client.get(url, params=params or {}, headers=merged)
        else:
            resp = httpx.get(
                url, params=params or {}, headers=merged, timeout=60.0
            )
        remain = _int_header(resp.headers, "X-ESI-Error-Limit-Remain", 100)
        reset = _int_header(resp.headers, "X-ESI-Error-Limit-Reset", 1)
        if resp.status_code in (420, 429):
            wait = _int_header(resp.headers, "Retry-After", reset)
            time.sleep(min(wait + 1, 65))
            continue
        if remain <= 5:
            time.sleep(min(reset + 1, 65))  # nearly error-limited: cool off
        if resp.status_code in (502, 503, 504) and attempt < 3:
            time.sleep(2 * (attempt + 1))
            continue
        return resp
    return resp

# Derived, never a second literal: EVE SSO exact-matches the callback
# registered at developers.eveonline.com against what we send, so these
# must agree with the port the app actually binds.
CALLBACK_PORT = config.DEFAULT_PORT
CALLBACK_PATH = config.CALLBACK_PATH

# 2026-08-20: dropped esi-skills.read_skills.v1 and both read_blueprints
# scopes — skill levels and blueprint ME/TE are user-entered (v1.1 design);
# no code path ever read them from ESI. Existing tokens keep the old scope
# string until re-auth, which is harmless.
REQUESTED_SCOPES = (
    "esi-assets.read_assets.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-assets.read_corporation_assets.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-wallet.read_corporation_wallets.v1",
    "esi-universe.read_structures.v1",  # resolve structures -> solar systems
    # v1.6: capital hulls sell-priced from a structure market (C-J6MT
    # Keepstar or custom). Must also be enabled on the app registration at
    # developers.eveonline.com; characters need a re-auth to pick it up.
    "esi-markets.structure_markets.v1",
)

STRUCTURE_MARKETS_SCOPE = "esi-markets.structure_markets.v1"

# ESI industry jobs use activity 9 for reactions in some eras and 11 in
# others; accept both and map onto the blueprint activity id.
_REACTION_JOB_ACTIVITIES = {9, 11}
_ACTIVE_JOB_STATUSES = {"active", "paused", "ready"}

# v1.9: asset location flags that mean "fitted to / loaded in a ship or
# structure" (module, rig, subsystem, service, fuel, core, drone and
# fighter slots/bays) rather than "sitting in a hangar". Unless the user
# opts in (settings.count_fitted_stock), these — and anything deployed in
# space (an is_singleton asset whose own location is a solar system:
# anchored Upwell structures, sov hubs, POCOs, starbases, depots) — are left
# out of on-hand stock. Items STORED inside an excluded structure (corp
# hangar divisions) still count: the exclusion is per asset, never per
# subtree.
_FITTED_FLAG_PREFIXES = (
    "RigSlot",
    "HiSlot",
    "MedSlot",
    "LoSlot",
    "SubSystemSlot",
    "ServiceSlot",
    "FighterTube",
    "StructureActive",
    "StructureInactive",
    "StructureOffline",
)
_FITTED_FLAGS = frozenset({"StructureFuel", "QuantumCoreRoom", "DroneBay", "FighterBay"})


def _fitted_or_deployed(asset: dict, ref) -> bool:
    flag = asset.get("location_flag") or ""
    if flag in _FITTED_FLAGS or flag.startswith(_FITTED_FLAG_PREFIXES):
        return True
    if not asset.get("is_singleton"):
        return False
    if asset.get("location_type") == "solar_system":
        return True  # assembled and sitting in space = deployed
    if ref is not None:
        try:
            category = ref.type_info(asset["type_id"]).category_id
        except KeyError:
            return False
        return category == config.CATEGORY_STRUCTURE
    return False


# ---------------------------------------------------------------------------
# Credentials and tokens
# ---------------------------------------------------------------------------


def client_id() -> str:
    """The application's PUBLIC client ID (config.ESI_CLIENT_ID).

    Magoo ships one registered EVE application and authenticates as a public
    client using PKCE — CCP's documented shape for native apps. There is no
    client secret in this code path at all: one embedded in a distributed
    binary would not be secret, and keeping a confidential branch alive
    would mean the developer's machine ran different code from every
    player's, so the shipped flow would never actually be under test.

    MAGOO_ESI_CLIENT_ID overrides it for development against a separate
    registration.
    """
    return os.environ.get("MAGOO_ESI_CLIENT_ID") or config.ESI_CLIENT_ID


def _token_request(form: dict) -> dict:
    """Public-client token request: client_id travels in the form body and
    no Authorization header is sent. Both the authorization-code exchange
    and the refresh grant come through here."""
    form["client_id"] = client_id()
    resp = httpx.post(
        SSO_TOKEN_URL,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _decode_token(access_token: str) -> dict:
    """Validate the JWT against EVE SSO's JWKS and return its claims."""
    signing_key = PyJWKClient(SSO_JWKS_URL).get_signing_key_from_jwt(
        access_token
    )
    return jwt.decode(
        access_token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        audience="EVE Online",
        options={"verify_iss": False},  # issuer varies: host vs https URL
        leeway=120,  # tolerate local clock skew vs SSO's iat/exp
    )


def _store_tokens(conn, tokens: dict) -> tuple[int, str]:
    claims = _decode_token(tokens["access_token"])
    character_id = int(claims["sub"].split(":")[-1])
    character_name = claims.get("name", f"character {character_id}")
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=tokens.get("expires_in", 1199)
    )
    conn.execute(
        "INSERT INTO esi_token (character_id, refresh_token, access_token, "
        "expires_at, scopes) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (character_id) DO UPDATE SET refresh_token = "
        "excluded.refresh_token, access_token = excluded.access_token, "
        "expires_at = excluded.expires_at, scopes = excluded.scopes",
        (
            character_id,
            tokens["refresh_token"],
            tokens["access_token"],
            expires_at.isoformat(),
            " ".join(claims.get("scp", []))
            if isinstance(claims.get("scp"), list)
            else claims.get("scp", ""),
        ),
    )
    # Every count flag starts off. Written explicitly rather than left to the
    # column defaults: SQLite cannot alter a default, so a database created
    # before this change still declares DEFAULT 1 and would otherwise opt the
    # character in behind the user's back. count_assets already defaults to 0
    # and is omitted so this still works if its migration has not run.
    conn.execute(
        "INSERT OR IGNORE INTO pool_character "
        "(character_id, character_name, include_assets, include_job_slots) "
        "VALUES (?, ?, 0, 0)",
        (character_id, character_name),
    )
    conn.commit()
    return character_id, character_name


def access_token(conn, character_id: int) -> str:
    """Current access token, refreshing transparently when stale."""
    row = conn.execute(
        "SELECT * FROM esi_token WHERE character_id = ?", (character_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"character {character_id} is not authenticated")
    if row["expires_at"]:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires > datetime.now(timezone.utc) + timedelta(seconds=60):
            return row["access_token"]
    try:
        tokens = _token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": row["refresh_token"],
            },
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            # invalid_grant: the refresh token was revoked or expired —
            # only a fresh SSO login can recover, so say so instead of
            # surfacing a bare 400 from the token endpoint.
            raise RuntimeError(
                f"EVE SSO rejected character {character_id}'s refresh "
                "token — re-authenticate from the ESI tab"
            ) from exc
        raise
    _store_tokens(conn, tokens)
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# Login flow (PKCE; the web UI drives it and /sso/callback completes it)
# ---------------------------------------------------------------------------


def authorize_url() -> tuple[str, str, str]:
    """(auth_url, code_verifier, state) for an SSO login.

    redirect_uri is config.CALLBACK_URL — one fixed string that must match
    the callback registered at developers.eveonline.com byte for byte.
    Building it from the live request host instead is what produced the old
    "SSO state mismatch" dead-end: browsing 127.0.0.1 while localhost was
    registered sends a redirect_uri CCP rejects. EVE SSO v2's PKCE token
    exchange (complete_login) sends no redirect_uri, so nothing downstream
    needs the value.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    url = SSO_AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": config.CALLBACK_URL,
            "client_id": client_id(),
            "scope": " ".join(REQUESTED_SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return url, verifier, state


def complete_login(conn, code: str, verifier: str) -> tuple[int, str]:
    """Exchange an authorization code and store the character's tokens."""
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
        },
    )
    return _store_tokens(conn, tokens)


# ---------------------------------------------------------------------------
# Authenticated requests
# ---------------------------------------------------------------------------


def _get(conn, character_id: int, path: str, params: dict | None = None):
    """Authenticated GET returning (json, headers). Retries once on 401."""
    for attempt in (1, 2):
        token = access_token(conn, character_id)
        resp = esi_request(
            f"{ESI_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401 and attempt == 1:
            conn.execute(
                "UPDATE esi_token SET expires_at = NULL WHERE character_id = ?",
                (character_id,),
            )
            continue
        resp.raise_for_status()
        return resp.json(), resp.headers
    raise RuntimeError("unreachable")


def _get_paginated(conn, character_id: int, path: str) -> list:
    data, headers = _get(conn, character_id, path, {"page": 1})
    pages = _int_header(headers, "X-Pages", 1)
    for page in range(2, pages + 1):
        more, _ = _get(conn, character_id, path, {"page": page})
        data.extend(more)
    return data


def character_with_scope(conn, scope: str) -> int | None:
    """First authenticated character whose token carries the scope."""
    for row in conn.execute("SELECT character_id, scopes FROM esi_token"):
        if scope in (row["scopes"] or "").split():
            return row["character_id"]
    return None


def fetch_structure_orders(conn, character_id: int, structure_id: int) -> list:
    """Every market order in one structure (requires docking access and the
    structure-markets scope; the endpoint has no per-type filter)."""
    return _get_paginated(
        conn, character_id, f"/markets/structures/{structure_id}/"
    )


def fetch_assets(conn, character_id: int) -> list:
    return _get_paginated(conn, character_id, f"/characters/{character_id}/assets/")


def fetch_industry_jobs(conn, character_id: int) -> list:
    data, _ = _get(conn, character_id, f"/characters/{character_id}/industry/jobs/")
    return data


def fetch_character_wallet(conn, character_id: int) -> float:
    data, _ = _get(conn, character_id, f"/characters/{character_id}/wallet/")
    return float(data)


def character_corporation_id(character_id: int) -> int:
    """Public endpoint — no auth needed."""
    resp = esi_request(f"{ESI_BASE}/characters/{character_id}/")
    resp.raise_for_status()
    return resp.json()["corporation_id"]


def corporation_name(corporation_id: int) -> str | None:
    """Public endpoint — no auth needed."""
    resp = esi_request(f"{ESI_BASE}/corporations/{corporation_id}/")
    resp.raise_for_status()
    return resp.json().get("name")


# Corp endpoints need in-game roles (assets: Director; industry jobs:
# Factory Manager; wallets: Accountant). A 403 just means this character
# lacks the role — it returns None (distinguishable from empty data), so
# refresh_state can try the next pool character for that endpoint family.


def fetch_corp_assets(
    conn, character_id: int, corporation_id: int
) -> list | None:
    try:
        return _get_paginated(
            conn, character_id, f"/corporations/{corporation_id}/assets/"
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return None
        raise


def fetch_corp_industry_jobs(
    conn, character_id: int, corporation_id: int
) -> list | None:
    try:
        # Paginated endpoint (unlike the character equivalent) — a single
        # _get silently dropped corp jobs beyond page 1.
        return _get_paginated(
            conn, character_id, f"/corporations/{corporation_id}/industry/jobs/"
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return None
        raise


def fetch_corp_wallets(
    conn, character_id: int, corporation_id: int
) -> float | None:
    """Total ISK across all corp wallet divisions (None without the role)."""
    try:
        data, _ = _get(
            conn, character_id, f"/corporations/{corporation_id}/wallets/"
        )
        return sum(d.get("balance", 0.0) for d in data)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return None
        raise


# ---------------------------------------------------------------------------
# Asset -> solar system resolution
# ---------------------------------------------------------------------------

# A cached NULL resolution (403/404) is honored only this long: a docking
# 403 is ACL state that changes, and one probe per dead location per day
# is cheap — a permanent NULL silently dropped that structure's assets
# from stock forever.
_LOCATION_MISS_TTL = timedelta(hours=24)


def _cached_miss_fresh(fetched_at: str | None) -> bool:
    """False for a legacy (pre-TTL, unstamped) or expired NULL row — both
    re-probe; junk timestamps count as expired."""
    if not fetched_at:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return False
    return age < _LOCATION_MISS_TTL


def _resolve_location(
    conn,
    character_id: int,
    location_id: int,
    memo: dict[int, int | None] | None = None,
) -> int | None:
    """Solar system for a station/structure id, cached in location_system.
    Returns None when unresolvable.

    Only DEFINITIVE answers are cached: a resolved system (permanent), or
    a 403/404 (no docking access / gone) cached as NULL with a fetched_at
    stamp and re-probed after _LOCATION_MISS_TTL — access can be granted
    and structures reanchor. A transient failure (5xx surviving retries,
    throttling) returns None WITHOUT caching, so the next refresh retries
    — a cached NULL used to silently drop that location's assets forever.
    `memo` (per-refresh) bounds repeat lookups within one pull."""
    if memo is not None and location_id in memo:
        return memo[location_id]
    row = conn.execute(
        "SELECT solar_system_id, fetched_at FROM location_system "
        "WHERE location_id = ?",
        (location_id,),
    ).fetchone()
    if row is not None:
        cached = row["solar_system_id"]
        if cached is not None or _cached_miss_fresh(row["fetched_at"]):
            if memo is not None:
                memo[location_id] = cached
            return cached
        # stale/legacy NULL row: fall through and re-probe (heals in place)
    system_id = None
    definitive = False
    try:
        if location_id < 64_000_000:  # NPC station range (also covers 60m ids)
            resp = esi_request(f"{ESI_BASE}/universe/stations/{location_id}/")
            if resp.status_code == 200:
                system_id = resp.json().get("system_id")
                definitive = True
            elif resp.status_code in (403, 404):
                definitive = True
        else:  # player structure
            data, _ = _get(
                conn, character_id, f"/universe/structures/{location_id}/"
            )
            system_id = data.get("solar_system_id")
            definitive = True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            definitive = True
    if definitive:
        conn.execute(
            "INSERT OR REPLACE INTO location_system "
            "(location_id, solar_system_id, fetched_at) VALUES (?, ?, ?)",
            (location_id, system_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    if memo is not None:
        memo[location_id] = system_id
    return system_id


def _aggregate_by_system(
    conn,
    character_id: int,
    assets: list,
    memo: dict | None = None,
    *,
    count_fitted: bool = True,
    ref=None,
) -> dict[int, dict[int, int]]:
    """{solar_system_id: {type_id: quantity}} for an asset list (character
    or corporation). Assets inside containers count — their location chain
    is walked to the top before resolving — except anything inside an
    Asset Safety Wrap, which is locked and skipped. With count_fitted False,
    fitted/loaded items and assets deployed in space are skipped (v1.9,
    settings.count_fitted_stock; see _fitted_or_deployed)."""
    by_item_id = {a["item_id"]: a for a in assets}
    result: dict[int, dict[int, int]] = {}
    for asset in assets:
        if not count_fitted and _fitted_or_deployed(asset, ref):
            continue
        top = asset
        seen = set()
        # Any AssetSafety link in the containment chain (the wrap itself,
        # or anything packed inside it) means the item sits in EVE's asset
        # safety, locked 5-20 days — not usable stock.
        in_asset_safety = top.get("location_flag") == "AssetSafety"
        while (
            not in_asset_safety
            and top["location_id"] in by_item_id
            and top["item_id"] not in seen
        ):
            seen.add(top["item_id"])
            top = by_item_id[top["location_id"]]
            in_asset_safety = top.get("location_flag") == "AssetSafety"
        if in_asset_safety:
            continue
        if top["location_type"] == "solar_system":
            system_id = top["location_id"]
        else:
            system_id = _resolve_location(
                conn, character_id, top["location_id"], memo
            )
        if system_id is None:
            continue
        bucket = result.setdefault(system_id, {})
        bucket[asset["type_id"]] = (
            bucket.get(asset["type_id"], 0) + asset["quantity"]
        )
    return result


def assets_by_system(conn, character_id: int) -> dict[int, dict[int, int]]:
    return _aggregate_by_system(conn, character_id, fetch_assets(conn, character_id))


# ---------------------------------------------------------------------------
# State refresh (Phase 1) — persisted; planning reads the stored snapshot
# ---------------------------------------------------------------------------


def refresh_state(conn, ref) -> dict:
    """Pull assets, jobs, and wallets from ESI for the whole pool and persist
    them as an esi_snapshot row.

    Scope (decisions 2026-08-20, per-corp/-character toggles 2026-08-25):
    - Stock on hand counts CORPORATION assets (per-corp opt-out via
      esi_corp.count_assets — an opted-out corp skips the assets pull
      entirely), plus PERSONAL hangars only for characters whose
      count_assets flag is on (default off). Both filtered to tracked
      systems, both under the fitted-stock rule.
    - esi_corp.count_jobs / count_wallet likewise skip that corp's jobs /
      wallets pull (defaults on). Corp jobs installed by pool characters
      still arrive via their CHARACTER feed, so opting a corp's jobs out
      only drops jobs installed by non-pool members.
    - In-progress output counts from corp AND personal jobs (personal job
      output is delivered into corp hangars), filtered by delivery
      location to tracked systems. An unresolvable delivery location KEEPS
      the credit — wrongly dropping it would double-build.
    - Slot capacity is user-entered. The corp feed runs FIRST and claims
      corp jobs in the job_id dedup: every corp-feed job counts toward the
      active-job counts and the multi-cycle end dates planning nets from
      the pool (corp ESI carries installer and end date, so corp auth
      alone covers corp-hangar jobs — 2026-08-25). include_job_slots
      gates only the character's remaining PERSONAL jobs; include_assets
      gates the character's wallet contribution to buying power.
    - Corp endpoints are tried per endpoint FAMILY across pool characters
      until one holds the role (roles differ per endpoint: Director /
      Factory Manager / Accountant), instead of burning the corp on the
      first character enumerated.
    """
    tracked = store.tracked_systems(conn)
    pool = store.pool_characters(conn)
    count_fitted = store.get_settings(conn).count_fitted_stock
    slot_ids = {
        c["character_id"] for c in pool if c["include_job_slots"]
    }
    on_hand: dict[int, int] = {}
    in_progress: dict[int, int] = {}
    active_jobs = {config.ACTIVITY_MANUFACTURING: 0, config.ACTIVITY_REACTION: 0}
    job_ends: dict[int, list] = {
        config.ACTIVITY_MANUFACTURING: [],
        config.ACTIVITY_REACTION: [],
    }
    character_isk = 0.0
    corporation_isk = 0.0
    seen_job_ids: set[int] = set()
    location_memo: dict[int, int | None] = {}

    def add_stock(stock_by_system: dict[int, dict[int, int]]) -> None:
        for system_id, stock in stock_by_system.items():
            # Empty tracked list = no filter (count everything).
            if tracked and system_id not in tracked:
                continue
            for type_id, qty in stock.items():
                on_hand[type_id] = on_hand.get(type_id, 0) + qty

    def add_jobs(
        jobs: list, resolver_id: int, count_all_slots: bool = False
    ) -> None:
        for job in jobs:
            if job.get("status") not in _ACTIVE_JOB_STATUSES:
                continue
            if job.get("job_id") in seen_job_ids:
                continue  # same job visible via character AND corp endpoints
            seen_job_ids.add(job.get("job_id"))
            activity = (
                config.ACTIVITY_REACTION
                if job["activity_id"] in _REACTION_JOB_ACTIVITIES
                else config.ACTIVITY_MANUFACTURING
                if job["activity_id"] == 1
                else None
            )
            if activity is None:
                continue  # research/copying/invention: not our pools
            # Slot counting is location-blind: the line is busy wherever
            # the job runs. The corp feed (processed first, so it claims
            # corp jobs in the dedup) counts every job it pulled — corp
            # ESI carries installer and end date, so corp auth alone
            # suffices (2026-08-25). Character feeds count only their
            # slot-flagged installer's remaining (personal) jobs.
            if count_all_slots or job.get("installer_id") in slot_ids:
                active_jobs[activity] += 1
                if job.get("end_date"):
                    job_ends[activity].append(job["end_date"])
            product_id = job.get("product_type_id")
            if not product_id:
                continue
            # Output delivering into an untracked system will never appear
            # in tracked stock — crediting it would suppress builds forever.
            if tracked:
                delivery = (
                    job.get("output_location_id")
                    or job.get("facility_id")
                    or job.get("location_id")
                    or job.get("station_id")
                )
                if delivery:
                    system_id = _resolve_location(
                        conn, resolver_id, delivery, location_memo
                    )
                    if system_id is not None and system_id not in tracked:
                        continue
            blueprint = ref.blueprint_for_product(product_id)
            portion = blueprint.portion_size if blueprint else 1
            in_progress[product_id] = (
                in_progress.get(product_id, 0)
                + job.get("runs", 0) * portion
            )

    # Corporation data FIRST: per distinct corp, each endpoint family
    # retried across the pool until a character with the role answers
    # (None = 403). Who answered (and how many rows) is recorded per corp
    # for the ESI tab. Running before the character feeds lets the corp
    # feed claim corp jobs in the job_id dedup, so slot counting for them
    # follows the CORP toggle, not the installer's character flag.
    corp_prefs = store.corp_settings(conn)
    corp_records: dict[int, dict] = {}
    assets_done: set[int] = set()
    jobs_done: set[int] = set()
    wallets_done: set[int] = set()
    for character in pool:
        character_id = character["character_id"]
        corporation_id = character_corporation_id(character_id)
        if corporation_id < 2_000_000:
            continue  # NPC corp
        record = corp_records.setdefault(
            corporation_id,
            {
                "corporation_id": corporation_id,
                "corporation_name": corporation_name(corporation_id),
                "assets_via": None,
                "jobs_via": None,
                "wallet_via": None,
                "asset_rows": None,
                "job_rows": None,
            },
        )
        pref = corp_prefs.get(corporation_id)
        counts = {
            "assets": pref is None or pref["count_assets"],
            "jobs": pref is None or pref["count_jobs"],
            "wallet": pref is None or pref["count_wallet"],
        }
        if corporation_id not in assets_done and counts["assets"]:
            corp_assets = fetch_corp_assets(conn, character_id, corporation_id)
            if corp_assets is not None:
                assets_done.add(corporation_id)
                record["assets_via"] = character_id
                record["asset_rows"] = len(corp_assets)
                add_stock(
                    _aggregate_by_system(
                        conn,
                        character_id,
                        corp_assets,
                        location_memo,
                        count_fitted=count_fitted,
                        ref=ref,
                    )
                )
        if corporation_id not in jobs_done and counts["jobs"]:
            corp_jobs = fetch_corp_industry_jobs(
                conn, character_id, corporation_id
            )
            if corp_jobs is not None:
                jobs_done.add(corporation_id)
                record["jobs_via"] = character_id
                record["job_rows"] = len(corp_jobs)
                add_jobs(corp_jobs, character_id, count_all_slots=True)
        if corporation_id not in wallets_done and counts["wallet"]:
            corp_isk = fetch_corp_wallets(conn, character_id, corporation_id)
            if corp_isk is not None:
                wallets_done.add(corporation_id)
                record["wallet_via"] = character_id
                corporation_isk += corp_isk

    store.upsert_esi_corps(
        conn, list(corp_records.values()), set(corp_records)
    )

    # Character feeds after the corp feed: whatever the dedup left them is
    # personal work (plus corp jobs of corps opted out of the jobs pull).
    for character in pool:
        character_id = character["character_id"]
        if character["include_assets"]:
            character_isk += fetch_character_wallet(conn, character_id)
        if character["count_assets"]:
            add_stock(
                _aggregate_by_system(
                    conn,
                    character_id,
                    fetch_assets(conn, character_id),
                    location_memo,
                    count_fitted=count_fitted,
                    ref=ref,
                )
            )
        add_jobs(fetch_industry_jobs(conn, character_id), character_id)

    store.save_esi_snapshot(
        conn,
        on_hand,
        in_progress,
        active_jobs,
        character_isk,
        corporation_isk,
        job_ends=job_ends,
    )
    return {
        "on_hand": on_hand,
        "in_progress": in_progress,
        "active_jobs": active_jobs,
        "character_isk": character_isk,
        "corporation_isk": corporation_isk,
        "job_ends": job_ends,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    conn = store.connect()
    store.ensure_schema(conn)
    command = argv[0] if argv else "status"
    if command == "status":
        rows = conn.execute(
            "SELECT p.character_id, p.character_name, p.include_assets, "
            "p.include_job_slots, t.expires_at FROM pool_character p "
            "LEFT JOIN esi_token t USING (character_id)"
        ).fetchall()
        if not rows:
            print("no characters in the pool — log in from the Magoo window")
        for row in rows:
            print(
                f"{row['character_name']} ({row['character_id']}) "
                f"assets={'y' if row['include_assets'] else 'n'} "
                f"slots={'y' if row['include_job_slots'] else 'n'} "
                f"token_expires={row['expires_at']}"
            )
    elif command == "test":
        from .refdata import Refdata

        ref = Refdata()
        t0 = time.monotonic()
        state = refresh_state(conn, ref)
        print(f"state refreshed in {time.monotonic() - t0:.1f}s:")
        print(f"  distinct types on hand: {len(state['on_hand'])}")
        print(f"  in-progress products:   {len(state['in_progress'])}")
        print(f"  active jobs:            {state['active_jobs']}")
        print(f"  character ISK:          {state['character_isk']:,.0f}")
        print(f"  corporation ISK:        {state['corporation_isk']:,.0f}")
    else:
        print(f"unknown command {command!r}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
