"""Desktop launcher: start the local server, then show Magoo in its own window.

The window is Edge WebView2 via pywebview. Two things it deliberately does
NOT do:

* It never runs EVE SSO inside the window. RFC 8252 says a native app must
  authorize in the user's own browser, and web.sso_login honours that.
* It never moves off config.DEFAULT_PORT. EVE SSO exact-matches the callback
  URL registered with the application, so an auto-porting launcher would
  produce an app that works fine until the moment someone tries to log in.

Everything here degrades to "open it in your browser instead", because a
missing or broken WebView2 runtime is a hard failure with no fallback of its
own.
"""

import argparse
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from magoo import __version__, config, logsetup

log = logging.getLogger(__name__)

# Evergreen WebView2 runtime, per Microsoft's distribution guide.
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

WINDOW_TITLE = "Magoo"
STARTUP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Talking to whoever owns the port
# ---------------------------------------------------------------------------


def probe_health(port: int, timeout: float = 1.5) -> dict | None:
    """Ask whoever owns the port whether they are Magoo.

    Returns the health payload for a Magoo instance, None for anything else
    (including nothing listening). This is what makes a second launch open
    another window instead of failing to bind, or worse, running a second
    server against the same database.
    """
    url = f"http://localhost:{port}/magoo/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.load(resp)
    except Exception:  # noqa: BLE001 - anything here just means "not Magoo"
        return None
    return payload if isinstance(payload, dict) and payload.get("app") == "magoo" else None


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_for_health(port: int, timeout: float = STARTUP_TIMEOUT) -> bool:
    """Block until the server answers, so the window never opens on a
    connection-refused page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_health(port, timeout=0.5):
            return True
        time.sleep(0.15)
    return False


# ---------------------------------------------------------------------------
# Windows plumbing
# ---------------------------------------------------------------------------


def message_box(text: str, title: str = WINDOW_TITLE) -> None:
    """Say something to a user who has no console.

    A packaged build is windowed, so a traceback goes nowhere they will ever
    look. MB_ICONWARNING | MB_OK.
    """
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)
            return
        except Exception:  # noqa: BLE001 - never let a dialog be the failure
            pass
    log.warning("%s: %s", title, text)


def webview2_version() -> str | None:
    """Installed Evergreen WebView2 version, or None.

    Windows 11 ships it and most Windows 10 machines have it, but Microsoft
    explicitly says to handle its absence. Note a registry hit is necessary,
    not sufficient — there is a documented corruption state where the key
    survives the files, which is why window creation is still guarded.
    """
    if sys.platform != "win32":
        return None
    import winreg

    for hive, path in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}",
        ),
    ):
        try:
            with winreg.OpenKey(hive, path) as key:
                found, _kind = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if found and found != "0.0.0.0":
            return found
    return None


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def start_server(port: int):
    """Run the app under waitress on a background thread.

    waitress rather than app.run(): the Werkzeug development server is not
    meant to be shipped, and create_server gives a handle we can close on
    the way out. The GUI loop needs the main thread.
    """
    from waitress.server import create_server

    from magoo.web import create_app

    # waitress narrates every request at INFO; the log is for diagnosis.
    logging.getLogger("waitress").setLevel(logging.WARNING)

    server = create_server(create_app(), host="127.0.0.1", port=port, threads=8)
    thread = threading.Thread(target=server.run, name="magoo-server", daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Showing it
# ---------------------------------------------------------------------------


def unblock_bundle() -> int:
    """Strip the Mark of the Web from the frozen build's own files.

    A zip downloaded from the internet carries a Zone.Identifier stream,
    and Explorer's Extract All copies it onto every extracted file. The
    .NET Framework then refuses to load pythonnet's Python.Runtime.dll
    from an Internet-zone file, pywebview cannot start, and the app falls
    back to the browser (v1.23 portable zip, 2026-09-03). Deleting the
    stream is exactly what the Properties → Unblock checkbox does; doing
    it here means the first launch heals itself. The installer never
    needs it (Inno Setup writes unmarked files), so this is a no-op
    there, and always a no-op outside a frozen Windows build. Returns the
    number of files unblocked.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return 0
    bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    unblocked = 0
    for path in [Path(sys.executable), *bundle.rglob("*")]:
        if not path.is_file():
            continue
        try:
            os.remove(f"{path}:Zone.Identifier")
        except FileNotFoundError:
            continue  # no mark on this file
        except OSError:
            log.warning("could not unblock %s", path, exc_info=True)
            continue
        unblocked += 1
    if unblocked:
        log.info(
            "removed the Mark of the Web from %d bundled file(s)", unblocked
        )
    return unblocked


def show_window(url: str) -> bool:
    """Open the native window. False means the caller should fall back to a
    browser — a missing runtime crashes or shows a blank window rather than
    degrading, so this is guarded rather than trusted."""
    if webview2_version() is None:
        log.warning("WebView2 runtime not detected")
        return False
    try:
        import webview
    except ImportError:
        log.warning("pywebview is not available", exc_info=True)
        return False
    try:
        webview.create_window(
            WINDOW_TITLE, url, width=1380, height=900, min_size=(900, 600)
        )
        webview.start()
    except Exception:  # noqa: BLE001 - any GUI failure means use the browser
        log.exception("could not open the Magoo window")
        return False
    return True


def show_in_browser(url: str, own_the_server: bool) -> None:
    """Fallback and --browser mode.

    When we started the server, the process has to stay alive to serve it —
    and a windowless process the user cannot quit is worse than no app, so a
    modal dialog stands in for the window and dismissing it shuts Magoo down.
    """
    webbrowser.open(url)
    if not own_the_server:
        return
    message_box(
        f"Magoo is running at {url}\n\n"
        "It has been opened in your browser.\n\n"
        "Click OK to shut Magoo down.",
        WINDOW_TITLE,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def selftest() -> int:
    """Prove the FROZEN build actually works. Exit code 0 means it does.

    A packaged app can start perfectly and still be useless: SciPy reaches
    its HiGHS backend through a dynamic import PyInstaller cannot see, so a
    missing hidden import surfaces the first time a user plans a run, not at
    build time. Templates have the same shape of failure — Jinja loads them
    off disk, and PyInstaller does not collect .html on its own.

    So this checks the things that break silently, with a known answer:

        maximise 3x + 2y   subject to   x + y <= 4,  x + 3y <= 6,
                                        x, y >= 0 and integral
        -> x = 4, y = 0, objective 12
    """
    failures = []

    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp

        result = milp(
            c=-np.array([3.0, 2.0]),  # milp minimises
            constraints=LinearConstraint(
                np.array([[1.0, 1.0], [1.0, 3.0]]), -np.inf, [4.0, 6.0]
            ),
            integrality=np.ones(2),
            bounds=Bounds(0, np.inf),
        )
        if not result.success:
            failures.append(f"MILP did not solve: {result.message}")
        elif round(-result.fun, 6) != 12.0:
            failures.append(f"MILP wrong answer: {-result.fun} != 12")
        else:
            log.info("MILP solver OK (HiGHS reachable, objective 12)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"SciPy MILP unavailable: {exc!r}")

    try:
        from magoo.web import create_app

        app = create_app()
        names = sorted(app.jinja_env.list_templates())
        if "base.html" not in names:
            failures.append(f"templates missing (found {len(names)})")
        else:
            app.jinja_env.get_template("sso_done.html")
            log.info("templates OK (%d bundled)", len(names))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"templates unavailable: {exc!r}")

    try:
        from magoo import store

        conn = store.connect()
        store.ensure_schema(conn)
        conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
        conn.close()
        log.info("database OK at %s", config.DB_PATH)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"database unusable: {exc!r}")

    try:
        import jwt
        from jwt import PyJWKClient

        # Both matter for SSO: PyJWKClient fetches CCP's signing keys, and
        # RSAAlgorithm only exists when cryptography made it into the
        # bundle. Without them login fails at the token exchange.
        if PyJWKClient is None or not hasattr(jwt.algorithms, "RSAAlgorithm"):
            failures.append("JWT verification unavailable (no cryptography?)")
        else:
            log.info("JWT/cryptography OK")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"JWT verification unavailable: {exc!r}")

    log.info("WebView2 runtime: %s", webview2_version() or "NOT INSTALLED")

    for problem in failures:
        log.error("SELFTEST: %s", problem)
    log.info("selftest %s", "FAILED" if failures else "passed")
    return 1 if failures else 0


def main(argv=None) -> int:
    logsetup.ensure_std_streams()
    logsetup.configure()

    parser = argparse.ArgumentParser(
        prog="Magoo", description="EVE Online industry planner"
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="open in your default browser instead of the Magoo window",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.DEFAULT_PORT,
        help=f"port to serve on (default {config.DEFAULT_PORT}); changing it "
        "breaks EVE SSO unless the callback registration matches",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="verify the packaged build (solver, templates, database) and "
        "exit; 0 means healthy",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    port = args.port
    url = f"http://localhost:{port}/"
    log.info("Magoo %s starting; data in %s", __version__, config.DATA_DIR)

    # Before anything imports pythonnet: a portable copy extracted from a
    # downloaded zip cannot open its window until its files are unblocked.
    unblock_bundle()

    running = probe_health(port)
    if running:
        # Already ours: show another window onto it rather than fighting for
        # the port or running a second server against the same database.
        log.info("Magoo %s is already running on port %s", running.get("version"), port)
        if args.browser or not show_window(url):
            show_in_browser(url, own_the_server=False)
        return 0

    if not port_is_free(port):
        message_box(
            f"Something else is already using port {port}, so Magoo cannot "
            "start.\n\n"
            "Magoo needs this exact port: EVE Online's login checks the "
            "address it sends you back to, character for character.\n\n"
            "Close the other program and try again."
        )
        return 1

    try:
        server = start_server(port)
    except Exception:  # noqa: BLE001 - must not die silently in a window
        log.exception("failed to start the server")
        message_box(
            "Magoo could not start.\n\n"
            f"The details are in:\n{config.LOG_DIR / 'magoo.log'}"
        )
        return 1

    if not wait_for_health(port):
        log.error("server did not answer within %.0fs", STARTUP_TIMEOUT)
        message_box(
            "Magoo started but its server did not respond.\n\n"
            f"The details are in:\n{config.LOG_DIR / 'magoo.log'}"
        )
        return 1

    try:
        if args.browser or not show_window(url):
            show_in_browser(url, own_the_server=True)
    finally:
        log.info("shutting down")
        try:
            server.close()
        except Exception:  # noqa: BLE001 - we are exiting anyway
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
