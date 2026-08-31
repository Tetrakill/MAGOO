"""The dashboard's game-data download (v1.19): ImportJob threading,
run_import progress events against a tiny offline SDE zip, and the
/sde/import + /sde/status routes. No network, no production data."""

import json
import sqlite3
import threading
import zipfile

import pytest

from magoo import config, sdeimport, store, web


# ---------------------------------------------------------------------------
# ImportJob (no Flask, no network)
# ---------------------------------------------------------------------------


def test_job_runs_reports_progress_and_done():
    def runner(force=False, progress=None):
        progress({"stage": "import", "dataset": "types", "step": 3, "steps": 8})
        return True

    job = sdeimport.ImportJob(runner)
    assert job.start()
    assert job.wait(5)
    s = job.status()
    assert s["state"] == "done"
    assert s["changed"] is True
    assert (s["dataset"], s["step"]) == ("types", 3)  # events merged in


def test_job_reports_error_instead_of_raising():
    def runner(force=False, progress=None):
        raise RuntimeError("CCP is down")

    job = sdeimport.ImportJob(runner)
    assert job.start()
    assert job.wait(5)
    s = job.status()
    assert s["state"] == "error"
    assert s["error"] == "CCP is down"


def test_job_refuses_concurrent_start_but_allows_rerun():
    release = threading.Event()
    started = threading.Event()

    def runner(force=False, progress=None):
        started.set()
        release.wait(5)
        return False

    job = sdeimport.ImportJob(runner)
    assert job.start()
    assert started.wait(5)
    assert job.status()["state"] == "running"
    assert not job.start()  # one at a time
    release.set()
    assert job.wait(5)
    assert job.status()["state"] == "done"
    assert job.status()["changed"] is False
    started.clear()
    assert job.start()  # a finished job can run again
    assert job.wait(5)


def test_job_status_is_a_snapshot():
    job = sdeimport.ImportJob(lambda force=False, progress=None: True)
    job.status()["state"] = "mangled"
    assert job.status()["state"] == "idle"


def test_job_forwards_force():
    seen = []

    def runner(force=False, progress=None):
        seen.append(force)
        return True

    job = sdeimport.ImportJob(runner)
    assert job.start(force=True)
    assert job.wait(5)
    assert seen == [True]


def test_job_spawn_failure_reports_error_not_wedged_running(monkeypatch):
    def no_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(sdeimport.threading.Thread, "start", no_start)
    job = sdeimport.ImportJob(lambda force=False, progress=None: True)
    assert job.start()  # the attempt happened; its outcome is in status
    s = job.status()
    assert s["state"] == "error"
    assert "thread" in s["error"]
    monkeypatch.undo()
    assert job.start()  # and the job is not wedged
    assert job.wait(5)
    assert job.status()["state"] == "done"


# ---------------------------------------------------------------------------
# run_import progress events, fully offline via a minimal SDE zip
# ---------------------------------------------------------------------------

_DATASET_RECORDS = {
    "categories": [{"_key": 6, "name": {"en": "Ship"}}],
    "groups": [{"_key": 25, "categoryID": 6, "name": {"en": "Frigate"}}],
    "types": [
        {"_key": 587, "groupID": 25, "name": {"en": "Rifter"}, "published": True},
        {"_key": 687, "groupID": 25, "name": {"en": "Rifter Blueprint"}, "published": True},
    ],
    "blueprints": [
        {
            "_key": 687,
            "maxProductionLimit": 30,
            "activities": {
                "manufacturing": {
                    "time": 6000,
                    "products": [{"typeID": 587, "quantity": 1}],
                    "materials": [{"typeID": 34, "quantity": 100}],
                }
            },
        }
    ],
    "dogmaAttributes": [{"_key": 4, "name": "mass", "defaultValue": 0.0}],
    "typeDogma": [
        {"_key": 587, "dogmaAttributes": [{"attributeID": 4, "value": 1000.0}]}
    ],
    "mapSolarSystems": [
        {
            "_key": 30000142,
            "name": {"en": "Jita"},
            "securityStatus": 0.945,
            "regionID": 10000002,
        }
    ],
    "industryModifierSources": [
        {
            "_key": 35825,
            "manufacturing": {
                "material": [{"dogmaAttributeID": 2600, "filterID": 4}]
            },
        }
    ],
    "industryTargetFilters": [
        {"_key": 4, "name": {"en": "All"}, "categoryIDs": [6], "groupIDs": []}
    ],
    "typeMaterials": [
        {"_key": 587, "materials": [{"materialTypeID": 34, "quantity": 10}]}
    ],
}


def _write_tiny_sde_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        for dataset, records in _DATASET_RECORDS.items():
            zf.writestr(
                f"{dataset}.jsonl",
                "\n".join(json.dumps(r) for r in records) + "\n",
            )
    return path


@pytest.fixture()
def offline_sde(tmp_path, monkeypatch):
    """run_import against the tiny zip: no network, temp DB."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SDE_CACHE_DIR", tmp_path / "sde")
    archive = _write_tiny_sde_zip(tmp_path / "sde-999.zip")
    monkeypatch.setattr(sdeimport, "fetch_latest_build", lambda client: 999)
    monkeypatch.setattr(
        sdeimport,
        "download_sde_zip",
        lambda client, build, progress=None: archive,
    )
    return archive


def test_run_import_emits_ordered_progress_events(offline_sde):
    events = []
    assert sdeimport.run_import(progress=events.append) is True
    stages = [e["stage"] for e in events]
    assert stages[0] == "check"
    assert {"stage": "resolved", "build": 999, "had": None} in events
    imports = [e for e in events if e["stage"] == "import"]
    assert [e["step"] for e in imports] == list(range(1, 9))
    assert all(e["steps"] == 8 for e in imports)
    assert imports[0]["dataset"] == "categories"
    assert imports[-1]["dataset"] == "solar systems"
    assert stages[-1] == "finalize"
    # And the import really landed.
    conn = sqlite3.connect(config.DB_PATH)
    assert conn.execute("SELECT build_number FROM ref_sde_build").fetchone() == (
        999,
    )
    conn.close()


def test_run_import_reports_current_when_unchanged(offline_sde):
    assert sdeimport.run_import() is True
    events = []
    assert sdeimport.run_import(progress=events.append) is False
    assert events == [{"stage": "check"}, {"stage": "current", "build": 999}]
    # ...but --force reimports the same build.
    assert sdeimport.run_import(force=True) is True


def test_download_sde_zip_streams_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SDE_CACHE_DIR", tmp_path / "sde")
    chunk = b"x" * (1 << 20)

    class FakeStream:
        headers = {"content-length": str(3 * len(chunk))}

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            yield from (chunk, chunk, chunk)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def stream(self, method, url):
            return FakeStream()

    events = []
    dest = sdeimport.download_sde_zip(FakeClient(), 999, events.append)
    assert dest.stat().st_size == 3 * len(chunk)
    assert [e["done"] for e in events] == [1 << 20, 2 << 20, 3 << 20]
    assert all(
        e["stage"] == "download" and e["total"] == 3 * len(chunk)
        for e in events
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


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


def test_status_starts_idle_and_json(fresh_client):
    resp = fresh_client.get("/sde/status")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/json")
    assert resp.get_json() == {"state": "idle", "message": ""}


def test_status_never_touches_the_database(fresh_client, monkeypatch):
    """The poll must stay DB-free: during an import the write lock is held
    for minutes and any sqlite touch would stall it into _db_busy's 302."""

    def boom():
        raise AssertionError("/sde/status opened a DB connection")

    monkeypatch.setattr(store, "connect", boom)
    assert fresh_client.get("/sde/status").status_code == 200


def test_ensure_schema_runs_once_per_app(tmp_path, monkeypatch):
    calls = []
    real = store.ensure_schema
    monkeypatch.setattr(
        store, "ensure_schema", lambda c: (calls.append(1), real(c))[1]
    )
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MAGOO_SECRET", "test-secret")
    app = web.create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        client.get("/")
        client.get("/")
        client.get("/pipelines")
    assert len(calls) == 1


def test_start_polls_and_finishes(fresh_app, fresh_client, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def fake_run(force=False, progress=None):
        progress({"stage": "resolved", "build": 999, "had": None})
        progress({"stage": "download", "done": 5_000_000, "total": 10_000_000})
        started.set()
        release.wait(5)
        progress({"stage": "import", "dataset": "types", "step": 3, "steps": 8})
        return True

    monkeypatch.setattr(sdeimport, "run_import", fake_run)
    try:
        assert fresh_client.post("/sde/import").status_code == 302
        assert started.wait(5)

        s = fresh_client.get("/sde/status").get_json()
        assert s["state"] == "running"
        assert s["stage"] == "download"
        assert s["message"] == "downloading — 5 / 10 MB"

        # While running: the dashboard disables the button, marks the
        # progress row live, and a second POST refuses politely.
        html = fresh_client.get("/").get_data(as_text=True)
        assert 'data-state="running"' in html
        assert "a game data download is already running" in html
        html = fresh_client.post(
            "/sde/import", follow_redirects=True
        ).get_data(as_text=True)
        # The FLASH, specifically — the disabled button's title carries
        # the same words, so match the flash markup.
        assert (
            '<div class="flash">a game data download is already running'
            "</div>" in html
        )
    finally:
        release.set()
    assert fresh_app.extensions["sde_import"].wait(5)

    s = fresh_client.get("/sde/status").get_json()
    assert s["state"] == "done"
    assert s["changed"] is True
    assert s["message"] == "build 999 imported"


def test_error_is_reported_not_raised(fresh_app, fresh_client, monkeypatch):
    def fake_run(force=False, progress=None):
        raise RuntimeError("latest.jsonl unreachable")

    monkeypatch.setattr(sdeimport, "run_import", fake_run)
    assert fresh_client.post("/sde/import").status_code == 302
    assert fresh_app.extensions["sde_import"].wait(5)
    s = fresh_client.get("/sde/status").get_json()
    assert s["state"] == "error"
    assert s["message"] == "failed — latest.jsonl unreachable"
    # The dashboard renders the failure inline, no traceback.
    html = fresh_client.get("/").get_data(as_text=True)
    assert "failed — latest.jsonl unreachable" in html
    assert 'data-state="error"' in html


def test_full_button_flow_flips_sde_ready(
    tmp_path, fresh_app, fresh_client, monkeypatch
):
    """POST the button, let the real run_import chew the tiny zip in the
    worker thread, and watch every SDE gate open."""
    monkeypatch.setattr(config, "SDE_CACHE_DIR", tmp_path / "sde")
    archive = _write_tiny_sde_zip(tmp_path / "sde-999.zip")
    monkeypatch.setattr(sdeimport, "fetch_latest_build", lambda client: 999)
    monkeypatch.setattr(
        sdeimport,
        "download_sde_zip",
        lambda client, build, progress=None: archive,
    )
    # Pre-SDE: pipelines page withholds the paste form.
    assert "Add / update pipelines" not in fresh_client.get(
        "/pipelines"
    ).get_data(as_text=True)

    assert fresh_client.post("/sde/import").status_code == 302
    assert fresh_app.extensions["sde_import"].wait(10)

    s = fresh_client.get("/sde/status").get_json()
    assert (s["state"], s["changed"]) == ("done", True)

    html = fresh_client.get("/").get_data(as_text=True)
    assert "SDE <b>999</b>" in html
    assert "build 999 imported" in html
    assert "⟳ Check for updates" in html  # step done: button relabels
    html = fresh_client.get("/pipelines").get_data(as_text=True)
    assert "Add / update pipelines" in html


def test_steady_state_dashboard_after_restart(tmp_path, monkeypatch):
    """Build imported, job idle — every dashboard load after an app
    restart: idle progress row stays hidden, button offers the update
    check, and nothing polls."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fresh.sqlite")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SDE_CACHE_DIR", tmp_path / "sde")
    monkeypatch.setenv("MAGOO_SECRET", "test-secret")
    archive = _write_tiny_sde_zip(tmp_path / "sde-999.zip")
    monkeypatch.setattr(sdeimport, "fetch_latest_build", lambda client: 999)
    monkeypatch.setattr(
        sdeimport,
        "download_sde_zip",
        lambda client, build, progress=None: archive,
    )
    assert sdeimport.run_import() is True  # e.g. an earlier session

    app = web.create_app()  # fresh process: ImportJob starts idle
    app.config["TESTING"] = True
    with app.test_client() as client:
        html = client.get("/?setup=1").get_data(as_text=True)
    assert "⟳ Check for updates" in html
    assert 'data-state="idle"' in html
    assert "hidden" in html.split('id="sde-progress"')[1][:120]
