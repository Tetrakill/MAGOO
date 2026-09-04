"""Distribution seams: where user data lives, and staying safe across upgrades.

These cover failures that only appear in a PACKAGED build, which is exactly
why they need tests — a dev checkout exercises none of them.
"""

import importlib
import sqlite3
import sys

import pytest

from magoo import config, logsetup, store, web


# ---------------------------------------------------------------------------
# Where user data lives
# ---------------------------------------------------------------------------


def test_source_checkout_keeps_data_beside_the_code():
    """The dev path must not move: conftest's session-scoped `ref` fixture
    opens the developer's real database out of PROJECT_ROOT/data, so
    relocating unconditionally would collapse the whole suite."""
    assert config._resolve_data_dir() == config.PROJECT_ROOT / "data"


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGOO_DATA_DIR", str(tmp_path / "elsewhere"))
    assert config._resolve_data_dir() == tmp_path / "elsewhere"


def test_frozen_build_uses_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Magoo.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.delenv("MAGOO_DATA_DIR", raising=False)
    assert config._resolve_data_dir() == tmp_path / "AppData" / "Magoo"


def test_portable_marker_keeps_data_beside_the_exe(monkeypatch, tmp_path):
    """The portable zip ships the marker; the installer does not."""
    (tmp_path / config.PORTABLE_MARKER).write_text("portable")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Magoo.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.delenv("MAGOO_DATA_DIR", raising=False)
    assert config._resolve_data_dir() == tmp_path / "data"


def _mark_from_internet(path) -> None:
    """Stamp a file the way Explorer does when extracting a downloaded zip."""
    with open(f"{path}:Zone.Identifier", "w") as stream:
        stream.write("[ZoneTransfer]\nZoneId=3\n")


def _is_marked(path) -> bool:
    try:
        with open(f"{path}:Zone.Identifier"):
            return True
    except FileNotFoundError:
        return False


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate streams")
def test_frozen_build_unblocks_its_own_files(monkeypatch, tmp_path):
    """The v1.23 portable zip opened in the browser: Extract All had put
    the Mark of the Web on every file, and .NET refuses to load
    pythonnet's Python.Runtime.dll from an Internet-zone file. The first
    launch strips the mark from the exe and the whole bundle, and a clean
    bundle is left alone."""
    from magoo import desktop

    exe = tmp_path / "Magoo.exe"
    exe.write_bytes(b"MZ")
    internal = tmp_path / "_internal"
    (internal / "pythonnet" / "runtime").mkdir(parents=True)
    dll = internal / "pythonnet" / "runtime" / "Python.Runtime.dll"
    dll.write_bytes(b"MZ")
    clean = internal / "clean.dll"
    clean.write_bytes(b"MZ")
    for path in (exe, dll):
        _mark_from_internet(path)
    assert _is_marked(dll)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)

    assert desktop.unblock_bundle() == 2
    assert not _is_marked(exe)
    assert not _is_marked(dll)
    assert dll.read_bytes() == b"MZ"  # the file itself is untouched
    assert not _is_marked(clean)
    assert desktop.unblock_bundle() == 0  # already clean: nothing to do


def test_unblock_is_a_no_op_in_a_source_checkout(monkeypatch, tmp_path):
    from magoo import desktop

    marked = tmp_path / "marked.dll"
    marked.write_bytes(b"MZ")
    if sys.platform == "win32":
        _mark_from_internet(marked)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert desktop.unblock_bundle() == 0
    if sys.platform == "win32":
        assert _is_marked(marked)  # not frozen: never touches anything


def test_all_three_paths_move_together(monkeypatch, tmp_path):
    """SDE_CACHE_DIR and DB_PATH derive from DATA_DIR at import time, so a
    build that relocated DATA_DIR alone would still write the 99 MB SDE zip
    and the database into a read-only install directory."""
    monkeypatch.setenv("MAGOO_DATA_DIR", str(tmp_path / "roaming"))
    try:
        importlib.reload(config)
        root = tmp_path / "roaming"
        assert config.DATA_DIR == root
        assert config.SDE_CACHE_DIR == root / "sde"
        assert config.DB_PATH == root / "magoo.sqlite"
        assert config.LOG_DIR == root / "logs"
    finally:
        monkeypatch.delenv("MAGOO_DATA_DIR", raising=False)
        importlib.reload(config)
    assert config.DATA_DIR == config.PROJECT_ROOT / "data"


# ---------------------------------------------------------------------------
# Surviving an upgrade
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.sqlite")
    c = sqlite3.connect(tmp_path / "state.sqlite")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_schema_version_is_stamped(db):
    store.ensure_schema(db)
    assert store._user_version(db) == store.SCHEMA_VERSION


def test_database_from_a_newer_build_is_refused(db):
    store.ensure_schema(db)
    db.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 5}")
    db.commit()
    with pytest.raises(RuntimeError, match="newer version of Magoo"):
        store.ensure_schema(db)


def test_fresh_database_is_not_backed_up(db, tmp_path):
    store.ensure_schema(db)
    assert not (tmp_path / "backups").exists()


def test_existing_database_is_backed_up_before_migrating(db, tmp_path):
    """A pre-v1.21 database carries user_version 0; its first launch under
    the new build takes a snapshot before touching anything."""
    store.ensure_schema(db)
    db.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run) VALUES ('canary', 42, 1)"
    )
    db.execute("PRAGMA user_version = 0")  # pretend it predates the stamp
    db.commit()

    store.ensure_schema(db)

    backup = tmp_path / "backups" / f"magoo-pre-{store.__version__}.sqlite"
    assert backup.exists()
    restored = sqlite3.connect(backup)
    try:
        names = [r[0] for r in restored.execute("SELECT name FROM pipeline")]
    finally:
        restored.close()
    assert names == ["canary"], "backup must hold the user's real data"


def test_backup_captures_wal_resident_writes(db, tmp_path):
    """WAL means a just-committed row can live only in the -wal sidecar, so
    the snapshot has to go through SQLite rather than copying the file."""
    db.execute("PRAGMA journal_mode = WAL")
    store.ensure_schema(db)
    db.execute(
        "INSERT INTO pipeline (name, final_product_type_id, "
        "output_qty_per_run) VALUES ('wal-only', 7, 1)"
    )
    db.commit()
    db.execute("PRAGMA user_version = 0")
    db.commit()

    store.ensure_schema(db)

    backup = tmp_path / "backups" / f"magoo-pre-{store.__version__}.sqlite"
    restored = sqlite3.connect(backup)
    try:
        names = [r[0] for r in restored.execute("SELECT name FROM pipeline")]
    finally:
        restored.close()
    assert "wal-only" in names


def test_interrupted_thukker_rebuild_is_recovered(db):
    """Simulate a pre-v1.21 crash: settings stranded in class_setting_old
    while class_setting sits empty. Without recovery the next launch
    silently reseeds the user's facilities to defaults."""
    store.ensure_schema(db)
    db.execute("UPDATE class_setting SET me_rig = 't2', security = 0.5")
    db.commit()
    db.execute("ALTER TABLE class_setting RENAME TO class_setting_old")
    db.execute(
        "CREATE TABLE class_setting AS SELECT * FROM class_setting_old WHERE 0"
    )
    db.commit()

    store._recover_orphaned_class_setting(db)

    rows = db.execute("SELECT me_rig, security FROM class_setting").fetchall()
    assert rows, "the stranded rows must come back"
    assert all(r["me_rig"] == "t2" for r in rows)
    leftover = db.execute(
        "SELECT name FROM sqlite_master WHERE name = 'class_setting_old'"
    ).fetchone()
    assert leftover is None


# ---------------------------------------------------------------------------
# A windowed build has no console, and may have no writable data directory
# ---------------------------------------------------------------------------


def test_print_survives_a_missing_stdout(monkeypatch):
    """PyInstaller --windowed leaves sys.stdout as None; a bare print() in
    the SDE import worker would otherwise raise AttributeError and take the
    game-data download down with it."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    logsetup.ensure_std_streams()
    print("this must not raise")
    assert not logsetup.is_tty()


def test_unwritable_data_dir_does_not_kill_startup(monkeypatch, tmp_path):
    """_persistent_secret runs inside create_app(), before any route. If it
    raised, a windowed build would die during construction with no console
    to show why — the user would just see a window that never opens."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "read-only")
    monkeypatch.delenv("MAGOO_SECRET", raising=False)

    def refuse(*args, **kwargs):
        raise PermissionError("access is denied")

    monkeypatch.setattr("pathlib.Path.mkdir", refuse)
    monkeypatch.setattr("pathlib.Path.write_text", refuse)

    secret = web._persistent_secret()
    assert secret and len(secret) == 64


# ---------------------------------------------------------------------------
# Logging in from a native window: two browsers, one flow
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MAGOO_SECRET", "test-secret")
    application = web.create_app()
    application.config["TESTING"] = True
    return application


def _state_from(auth_url):
    import urllib.parse

    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth_url).query))[
        "state"
    ]


def test_login_started_in_one_browser_completes_in_another(app, monkeypatch):
    """THE regression this design exists for.

    The window shows Magoo; the login happens in the system browser. Those
    are separate cookie jars, so while the PKCE verifier lived in the Flask
    session the callback could never match its state and aborted with "SSO
    state mismatch". Two independent test clients reproduce exactly that.
    """
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(
        web.esi, "complete_login", lambda conn, code, verifier: (99, "Pilot")
    )

    window = app.test_client()   # the pywebview window
    browser = app.test_client()  # the user's real browser

    window.get("/sso/login")
    state = _state_from(opened[0])

    resp = browser.get(f"/sso/callback?code=abc&state={state}")

    assert resp.status_code == 200
    assert "Signed in as Pilot" in resp.get_data(as_text=True)


def test_window_learns_about_the_login_by_polling(app, monkeypatch):
    """The window cannot be redirected by a callback that landed in another
    browser, so it polls /sso/status instead."""
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(
        web.esi, "complete_login", lambda conn, code, verifier: (99, "Pilot")
    )
    window = app.test_client()
    browser = app.test_client()

    window.get("/sso/login")
    before = window.get("/sso/status").get_json()
    assert before["waiting"] is True
    assert before["character"] is None

    browser.get(f"/sso/callback?code=abc&state={_state_from(opened[0])}")

    after = window.get("/sso/status").get_json()
    assert after["completed"] == before["completed"] + 1
    assert after["character"] == "Pilot"
    assert after["waiting"] is False


def test_callback_state_is_single_use(app, monkeypatch):
    """A replayed callback URL must not mint a second login."""
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(
        web.esi, "complete_login", lambda conn, code, verifier: (99, "Pilot")
    )
    client = app.test_client()
    client.get("/sso/login")
    url = f"/sso/callback?code=abc&state={_state_from(opened[0])}"

    assert client.get(url).status_code == 200
    replay = client.get(url)
    assert replay.status_code == 400
    assert "expired or was already used" in replay.get_data(as_text=True)


def test_unknown_state_is_rejected(app):
    resp = app.test_client().get("/sso/callback?code=abc&state=forged")
    assert resp.status_code == 400


def test_sso_error_from_ccp_is_shown_not_crashed(app):
    resp = app.test_client().get(
        "/sso/callback?error=access_denied"
        "&error_description=The+user+denied+access"
    )
    assert resp.status_code == 400
    assert "denied access" in resp.get_data(as_text=True)


def test_authorize_url_is_pkce_public_client_at_the_fixed_callback():
    """No client secret anywhere, and a redirect_uri that matches the
    registration byte for byte."""
    import urllib.parse

    from magoo import esi

    url, verifier, state = esi.authorize_url()
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["redirect_uri"] == config.CALLBACK_URL == (
        f"http://localhost:{config.DEFAULT_PORT}/sso/callback"
    )
    assert q["client_id"] == config.ESI_CLIENT_ID
    assert q["code_challenge_method"] == "S256"
    assert len(q["scope"].split()) == len(esi.REQUESTED_SCOPES)
    assert verifier and state


def test_no_client_secret_is_ever_sent(monkeypatch):
    """The confidential branch is gone: the token request carries client_id
    in the body and no Authorization header."""
    import httpx

    from magoo import esi

    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["data"] = dict(data or {})
        seen["headers"] = dict(headers or {})
        return httpx.Response(
            200,
            json={"access_token": "x", "refresh_token": "y", "expires_in": 1},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(esi.httpx, "post", fake_post)
    esi._token_request({"grant_type": "refresh_token"})

    assert seen["data"]["client_id"] == esi.client_id()
    assert "client_secret" not in seen["data"]
    assert "Authorization" not in seen["headers"]


def test_stored_client_secret_is_wiped_on_upgrade(db):
    """A pre-v1.21 database may hold a secret the user pasted in via the
    retired CLI. It is a live credential in a file that gets synced."""
    store.ensure_schema(db)
    db.execute("UPDATE settings SET esi_client_secret = 'leftover-secret'")
    db.commit()

    store.ensure_schema(db)

    row = db.execute(
        "SELECT esi_client_secret FROM settings WHERE id = 1"
    ).fetchone()
    assert row["esi_client_secret"] is None


def test_health_endpoint_identifies_magoo(app):
    """The launcher probes this to tell 'Magoo is already running here' from
    'something else owns the port'."""
    import os

    body = app.test_client().get("/magoo/health").get_json()
    assert body["app"] == "magoo"
    assert body["version"]
    # pid, not port: --port can move the server, and a health payload that
    # reported the compiled-in default would be wrong exactly when it matters.
    assert body["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes</title>
  <entry><title>v{newest}</title><link href="https://example/{newest}"/></entry>
  <entry><title>v1.19</title><link href="https://example/1.19"/></entry>
</feed>"""


def test_versions_compare_numerically_not_as_strings():
    """1.9 < 1.20 is the whole point; string ordering gets this backwards
    and would nag every user forever."""
    from magoo import update

    assert update.is_newer("1.21", current="1.20.0")
    assert update.is_newer("1.20.1", current="1.20.0")
    assert update.is_newer("2.0", current="1.20.0")
    assert not update.is_newer("1.9", current="1.20.0")
    assert not update.is_newer("1.20.0", current="1.20.0")
    assert not update.is_newer("1.19.9", current="1.20.0")
    assert not update.is_newer("not-a-version", current="1.20.0")


def test_no_repo_configured_means_no_network_and_no_banner(db, monkeypatch):
    from magoo import update

    monkeypatch.setattr(config, "GITHUB_REPO", None)
    store.ensure_schema(db)
    assert update.feed_url() is None
    assert update.due(db) is False
    assert update.banner(db) is None


def test_a_newer_release_produces_a_banner(db, monkeypatch):
    from magoo import update

    monkeypatch.setattr(config, "GITHUB_REPO", "someone/magoo")
    monkeypatch.setattr(
        update, "fetch_latest", lambda etag=None: ("99.0.0", "etag-1")
    )
    store.ensure_schema(db)

    assert update.due(db) is True
    assert update.check_now(db) == "99.0.0"

    shown = update.banner(db)
    assert shown["version"] == "99.0.0"
    assert shown["url"] == "https://github.com/someone/magoo/releases/latest"
    assert update.due(db) is False  # not again for 24h


def test_dismissing_a_version_silences_only_that_version(db, monkeypatch):
    from magoo import update

    monkeypatch.setattr(config, "GITHUB_REPO", "someone/magoo")
    store.ensure_schema(db)
    db.execute("UPDATE settings SET update_latest = '99.0.0'")
    db.commit()

    update.dismiss(db, "99.0.0")
    assert update.banner(db) is None

    db.execute("UPDATE settings SET update_latest = '99.1.0'")
    db.commit()
    assert update.banner(db)["version"] == "99.1.0"


def test_update_check_can_be_switched_off(db, monkeypatch):
    from magoo import update

    monkeypatch.setattr(config, "GITHUB_REPO", "someone/magoo")
    store.ensure_schema(db)
    db.execute(
        "UPDATE settings SET update_latest = '99.0.0', "
        "update_check_enabled = 0"
    )
    db.commit()
    assert update.banner(db) is None
    assert update.due(db) is False


def test_a_failing_check_is_silent(db, monkeypatch):
    """Offline, captive portal, rate limited — all the same non-event."""
    from magoo import update

    monkeypatch.setattr(config, "GITHUB_REPO", "someone/magoo")

    def explode(request, timeout=None):
        raise OSError("no route to host")

    monkeypatch.setattr(update.urllib.request, "urlopen", explode)
    store.ensure_schema(db)

    assert update.fetch_latest() == (None, None)
    assert update.check_now(db) is None
    assert update.banner(db) is None


def test_feed_parsing_takes_the_first_entry(monkeypatch):
    from magoo import update

    monkeypatch.setattr(config, "GITHUB_REPO", "someone/magoo")

    class Resp:
        headers = {"ETag": "W/abc"}

        def read(self, _n=None):
            return ATOM.format(newest="1.21.0").encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        update.urllib.request, "urlopen", lambda req, timeout=None: Resp()
    )
    assert update.fetch_latest() == ("1.21.0", "W/abc")


# ---------------------------------------------------------------------------
# A newly authed character opts into nothing
# ---------------------------------------------------------------------------


def _add_character(conn, monkeypatch, character_id=4242, name="Newbie"):
    """Drive esi.complete_login with the network and JWT stubbed out."""
    from magoo import esi

    monkeypatch.setattr(
        esi, "_token_request", lambda form: {
            "access_token": "at", "refresh_token": "rt", "expires_in": 1200
        }
    )
    monkeypatch.setattr(
        esi, "_decode_token",
        lambda token: {"sub": f"CHARACTER:EVE:{character_id}", "name": name,
                       "scp": list(esi.REQUESTED_SCOPES)},
    )
    return esi.complete_login(conn, "code", "verifier")


def test_new_character_counts_nothing_by_default(db, monkeypatch):
    """Most industrialists run everything from corporation hangars, so
    counting a character's own assets, wallet and slots by default silently
    inflated the plan."""
    store.ensure_schema(db)
    _add_character(db, monkeypatch)

    row = db.execute("SELECT * FROM pool_character").fetchone()
    assert row["character_name"] == "Newbie"
    # UI labels: count_assets -> "Count assets",
    # include_assets -> "Count wallet", include_job_slots -> "Count job slots"
    assert row["count_assets"] == 0
    assert row["include_assets"] == 0
    assert row["include_job_slots"] == 0


def test_defaults_hold_on_a_database_that_predates_the_change(db, monkeypatch):
    """SQLite cannot alter a column default, so a database created before
    this change still declares DEFAULT 1. The INSERT has to write the zeros
    itself or those users keep getting opted in."""
    store.ensure_schema(db)
    db.execute("DROP TABLE pool_character")
    db.execute(
        "CREATE TABLE pool_character ("
        " character_id INTEGER PRIMARY KEY,"
        " character_name TEXT NOT NULL,"
        " include_assets INTEGER NOT NULL DEFAULT 1,"
        " include_job_slots INTEGER NOT NULL DEFAULT 1,"
        " count_assets INTEGER NOT NULL DEFAULT 0)"
    )
    db.commit()

    _add_character(db, monkeypatch, character_id=99, name="Legacy")

    row = db.execute("SELECT * FROM pool_character").fetchone()
    assert (row["include_assets"], row["include_job_slots"]) == (0, 0)


def test_existing_choices_are_never_overwritten(db, monkeypatch):
    """Re-authenticating a character must not silently reset toggles the
    user turned on — INSERT OR IGNORE, not REPLACE."""
    store.ensure_schema(db)
    _add_character(db, monkeypatch, character_id=7, name="Veteran")
    db.execute(
        "UPDATE pool_character SET include_assets = 1, include_job_slots = 1"
    )
    db.commit()

    _add_character(db, monkeypatch, character_id=7, name="Veteran")

    row = db.execute("SELECT * FROM pool_character").fetchone()
    assert (row["include_assets"], row["include_job_slots"]) == (1, 1)


# ---------------------------------------------------------------------------
# Removing a character
# ---------------------------------------------------------------------------


def _seed_character(app, character_id=5150, name="Doomed", corp_via=False):
    """Create the schema through a request, then seed directly."""
    client = app.test_client()
    client.get("/")
    conn = store.connect()
    conn.execute(
        "INSERT OR REPLACE INTO pool_character "
        "(character_id, character_name, include_assets, include_job_slots) "
        "VALUES (?, ?, 1, 1)",
        (character_id, name),
    )
    conn.execute(
        "INSERT OR REPLACE INTO esi_token (character_id, refresh_token, "
        "access_token, expires_at, scopes) VALUES (?, 'rt', 'at', 'x', '')",
        (character_id,),
    )
    if corp_via:
        conn.execute(
            "INSERT OR REPLACE INTO esi_corp (corporation_id, "
            "corporation_name, assets_via, jobs_via, wallet_via) "
            "VALUES (98, 'Test Holdings', ?, ?, ?)",
            (character_id, character_id, character_id),
        )
    conn.commit()
    conn.close()
    return client


def test_remove_character_deletes_row_and_token(app):
    client = _seed_character(app)

    resp = client.post("/characters/5150/delete")
    assert resp.status_code == 302

    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM pool_character"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM esi_token"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_remove_character_clears_stale_corp_references(app):
    """esi_corp records which token pulled each feed. A deleted id left
    behind renders as a blank 'via' until the next refresh re-derives it."""
    client = _seed_character(app, corp_via=True)

    client.post("/characters/5150/delete")

    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM esi_corp").fetchone()
    finally:
        conn.close()
    assert row["corporation_name"] == "Test Holdings", "the corp must survive"
    assert row["assets_via"] is None
    assert row["jobs_via"] is None
    assert row["wallet_via"] is None


def test_remove_character_warns_when_corp_data_is_stranded(app):
    """Losing the only character with corp roles silently stops corp data,
    so say so rather than letting the numbers quietly go stale."""
    client = _seed_character(app, corp_via=True)

    html = client.post(
        "/characters/5150/delete", follow_redirects=True
    ).get_data(as_text=True)

    assert "removed Doomed" in html
    assert "Test Holdings" in html


def test_remove_character_leaves_others_alone(app):
    client = _seed_character(app, character_id=1, name="Keeper")
    _seed_character(app, character_id=2, name="Doomed")

    client.post("/characters/2/delete")

    conn = store.connect()
    try:
        names = [r["character_name"] for r in conn.execute(
            "SELECT character_name FROM pool_character")]
        tokens = [r["character_id"] for r in conn.execute(
            "SELECT character_id FROM esi_token")]
    finally:
        conn.close()
    assert names == ["Keeper"]
    assert tokens == [1]


def test_remove_unknown_character_is_404(app):
    client = _seed_character(app)
    assert client.post("/characters/999999/delete").status_code == 404


# ---------------------------------------------------------------------------
# Logging with no console attached (the double-click case)
# ---------------------------------------------------------------------------


def _no_console(monkeypatch, tmp_path):
    """Put logsetup in the state a double-clicked windowed exe starts in."""
    import logging

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(logsetup, "_configured", False)
    log = logging.getLogger(logsetup.LOG_NAME)
    monkeypatch.setattr(log, "handlers", [])
    return log


def test_logging_does_not_recurse_without_a_console(monkeypatch, tmp_path):
    """THE crash that shipped in the first packaged build.

    With no console, ensure_std_streams substitutes sys.stdout with a
    _LogWriter that forwards writes to the logger. If configure() then
    treats that substitute as a console and attaches a StreamHandler to it,
    logging writes into the object that writes back into logging, and the
    app dies with RecursionError before its window ever opens.

    It never reproduces from a terminal, which is exactly why every test
    and every manual launch missed it.
    """
    log = _no_console(monkeypatch, tmp_path)

    logsetup.ensure_std_streams()
    assert logsetup.has_console() is False, "a substitute is not a console"

    logsetup.configure()

    for handler in log.handlers:
        stream = getattr(handler, "stream", None)
        assert not isinstance(stream, logsetup._LogWriter), (
            "a handler writing into the stdout substitute recurses"
        )

    log.info("must not recurse")          # would blow the stack before
    print("a stray print must not recurse either")


def test_log_writer_refuses_to_re_enter(monkeypatch, tmp_path):
    """Second, independent guard: even if something does attach a handler
    that writes to stdout, the substitute drops the re-entrant write rather
    than recursing. logging's own handleError path does exactly this."""
    import logging

    log = _no_console(monkeypatch, tmp_path)
    logsetup.ensure_std_streams()
    logsetup.configure()

    hostile = logging.StreamHandler(sys.stdout)  # the substitute itself
    hostile.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(hostile)
    try:
        log.warning("re-entrant write")   # must terminate, not recurse
    finally:
        log.removeHandler(hostile)


def test_a_real_console_still_gets_a_stream_handler(monkeypatch, tmp_path):
    """The guard must not cost developers their console output."""
    import io
    import logging

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(logsetup, "_configured", False)
    log = logging.getLogger(logsetup.LOG_NAME)
    monkeypatch.setattr(log, "handlers", [])

    assert logsetup.has_console() is True
    logsetup.configure()
    assert any(
        type(h) is logging.StreamHandler for h in log.handlers
    ), "a real console should still receive log output"
