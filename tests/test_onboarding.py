"""Fresh-install walk: a brand-new database (no SDE import, no state) must
render every page — the setup checklist, not a traceback, is the first thing
the next industrialist sees. Uses a temp database via monkeypatched
config.DB_PATH; production data is never touched."""

import sqlite3

import pytest

from magoo import config, web


@pytest.fixture()
def fresh_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MAGOO_SECRET", "test-secret")
    app = web.create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def fresh_client(fresh_app):
    with fresh_app.test_client() as client:
        yield client


PAGES = [
    "/",
    "/pipelines",
    "/planning",
    "/planning?view=slots",
    "/runs",
    "/characters",
    "/settings",
]


@pytest.mark.parametrize("path", PAGES)
def test_fresh_install_page_renders(fresh_client, path):
    resp = fresh_client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"


def test_fresh_dashboard_shows_setup_checklist(fresh_client):
    html = fresh_client.get("/").get_data(as_text=True)
    assert ">First-run setup</h2>" in html
    # Step 1 is a button now, not a CLI instruction (v1.19).
    assert "Download game data" in html
    assert 'action="/sde/import"' in html
    assert "python -m magoo.sdeimport" not in html
    # v1.21 ships the client id, so there is no developer-app step and
    # nothing anywhere tells a packaged user to open a terminal.
    assert "developers.eveonline.com" not in html
    assert "python -m magoo.esi" not in html
    assert "Log in with EVE" in html
    # Nothing is done yet; the SDE download is the next actionable step.
    assert 'class="done"' not in html
    assert html.count('class="next"') == 1
    assert 'aria-current="step"' in html
    # The panel is dismissable, with the reopen path named in its title.
    assert "dismissSetup()" in html


def test_fresh_pipelines_page_points_at_sde_import(fresh_client):
    html = fresh_client.get("/pipelines").get_data(as_text=True)
    assert "Download the game data first" in html
    # The paste form is withheld until blueprint data exists.
    assert "Add / update pipelines" not in html


def test_fresh_pipeline_paste_redirects_with_guidance(fresh_client):
    resp = fresh_client.post("/pipelines", data={"products": "Ishtar\t40"})
    assert resp.status_code == 302  # back to /pipelines, no traceback


def test_disabled_actions_explain_their_prerequisites(fresh_client):
    html = fresh_client.get("/").get_data(as_text=True)
    # ESI refresh gates on the game data existing first.
    assert "download the game data first" in html
    assert "add a pipeline first" in html
    assert "needs pipelines and an ESI update first" in html


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("POST", "/esi/refresh", {}),  # no SDE to classify against
        ("POST", "/run", {}),
        ("POST", "/prices/refresh", {}),
        ("POST", "/settings/systems", {"system": "Jita"}),
        ("POST", "/settings/blacklist/items", {"item": "Tritanium"}),
    ],
)
def test_fresh_actions_redirect_with_guidance(fresh_client, method, path, data):
    """Every action reachable pre-SDE/pre-SSO answers with a redirect and a
    flash, never a traceback."""
    if method == "GET":
        resp = fresh_client.get(path)
    else:
        resp = fresh_client.post(path, data=data)
    assert resp.status_code == 302, f"{method} {path} -> {resp.status_code}"


def test_fresh_sso_login_works_without_any_setup(fresh_client, monkeypatch):
    """v1.21 ships the client id, so login is actionable on a brand-new
    install — it no longer bounces with 'no ESI client id configured'.
    The browser is stubbed: this must not open a real one."""
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))

    html = fresh_client.get("/sso/login").get_data(as_text=True)

    assert len(opened) == 1
    assert opened[0].startswith("https://login.eveonline.com/")
    assert "code_challenge_method=S256" in opened[0]
    # RFC 8252: authorization happens in the user's own browser, and the
    # window waits rather than embedding the login.
    assert "Waiting for EVE SSO" in html


def test_fresh_planning_survives_alchemy_enabled(fresh_client):
    """A legal settings toggle (alchemy on) must not crash planning before
    the SDE exists — demand_type_ids reads alchemy routes from ref tables."""
    fresh_client.get("/")  # create the schema
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE settings SET alchemy_enabled = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    assert fresh_client.get("/planning").status_code == 200
    assert fresh_client.get("/planning?view=slots").status_code == 200


def test_checklist_disappears_once_a_run_exists(fresh_client):
    # Prime the temp DB (route request created the schema), then add a run.
    fresh_client.get("/")
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO index_run (run_number, planned_start, status) "
        "VALUES (1, '2026-01-01', 'planned')"
    )
    conn.commit()
    conn.close()
    html = fresh_client.get("/").get_data(as_text=True)
    assert ">First-run setup</h2>" not in html
    # ...but the Settings page can bring it back on demand.
    html = fresh_client.get("/?setup=1").get_data(as_text=True)
    assert ">First-run setup</h2>" in html
    html = fresh_client.get("/settings").get_data(as_text=True)
    assert "/?setup=1" in html

