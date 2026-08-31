"""Notify-and-link update check.

Deliberately NOT an auto-updater. A self-modifying binary is an antivirus
heuristic red flag and a good way to break a signed install; telling someone
a new version exists and linking them to it cannot corrupt anything.

Reads GitHub's releases.atom rather than api.github.com/releases/latest. The
REST API allows 60 unauthenticated requests an hour PER IP, so a corp running
Magoo on twenty boxes behind one gateway would throttle itself, and GitHub
documents the ETag no-charge exemption only for authorized requests. The Atom
feed is CDN-served, carries the same tag, and costs nothing from that budget.
Embedding a token to raise the limit is not an option: unlike the EVE client
id — which CCP documents as public — a GitHub token is a real secret, and
secret scanning would revoke it the moment the repository went public.

Every failure here is silent. An update banner that turns into an error
because someone is offline or behind a captive portal is worse than no
banner at all.
"""

import logging
import re
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

from magoo import __version__, config

log = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(hours=24)
TIMEOUT_SECONDS = 5.0

# The first <entry> of the Atom feed is the newest release; its title is the
# tag, with or without a leading "v".
_ENTRY_TITLE = re.compile(
    r"<entry>.*?<title>\s*v?([0-9][0-9.]*)\s*</title>", re.DOTALL
)


def parse_version(text: str) -> tuple[int, ...] | None:
    """'1.20.0' -> (1, 20, 0). None if it is not a plain dotted number."""
    if not text:
        return None
    parts = text.strip().lstrip("vV").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def is_newer(candidate: str, current: str = __version__) -> bool:
    """Compare numerically, never by string order or feed position: GitHub
    sorts releases.atom by the underlying commit date, so tagging an older
    commit can put a surprising entry first."""
    new, now = parse_version(candidate), parse_version(current)
    if new is None or now is None:
        return False
    length = max(len(new), len(now))
    return new + (0,) * (length - len(new)) > now + (0,) * (length - len(now))


def feed_url() -> str | None:
    if not config.GITHUB_REPO:
        return None
    return f"https://github.com/{config.GITHUB_REPO}/releases.atom"


def releases_url() -> str | None:
    if not config.GITHUB_REPO:
        return None
    return f"https://github.com/{config.GITHUB_REPO}/releases/latest"


def fetch_latest(etag: str | None = None) -> tuple[str | None, str | None]:
    """(version, etag). (None, etag) means unchanged or unavailable."""
    url = feed_url()
    if url is None:
        return None, etag
    request = urllib.request.Request(
        url, headers={"User-Agent": config.USER_AGENT}
    )
    if etag:
        request.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read(200_000).decode("utf-8", "replace")
            new_etag = resp.headers.get("ETag") or etag
    except Exception:  # noqa: BLE001 - offline, 304, rate limit: all the same
        return None, etag
    found = _ENTRY_TITLE.search(body)
    return (found.group(1) if found else None), new_etag


# ---------------------------------------------------------------------------
# Stored state
# ---------------------------------------------------------------------------


def _row(conn):
    return conn.execute(
        "SELECT update_check_enabled, update_checked_at, update_etag, "
        "update_latest, update_dismissed FROM settings WHERE id = 1"
    ).fetchone()


def due(conn) -> bool:
    row = _row(conn)
    if row is None or not row["update_check_enabled"]:
        return False
    if feed_url() is None:
        return False
    last = row["update_checked_at"]
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when >= CHECK_INTERVAL


def check_now(conn) -> str | None:
    """Fetch and record. Returns the newest version seen, if any."""
    row = _row(conn)
    latest, etag = fetch_latest(row["update_etag"] if row else None)
    conn.execute(
        "UPDATE settings SET update_checked_at = ?, update_etag = ?, "
        "update_latest = COALESCE(?, update_latest) WHERE id = 1",
        (datetime.now(timezone.utc).isoformat(), etag, latest),
    )
    conn.commit()
    return latest


def banner(conn) -> dict | None:
    """What the nav should show, or None. Reads only stored state — never
    the network — so rendering a page is never blocked on GitHub."""
    row = _row(conn)
    if row is None or not row["update_check_enabled"]:
        return None
    latest = row["update_latest"]
    if not latest or not is_newer(latest):
        return None
    if row["update_dismissed"] and not is_newer(latest, row["update_dismissed"]):
        return None
    return {"version": latest, "url": releases_url()}


def dismiss(conn, version: str) -> None:
    conn.execute(
        "UPDATE settings SET update_dismissed = ? WHERE id = 1", (version,)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------


def refresh_in_background(connect) -> None:
    """Check on a worker thread so launching never waits on the network.

    `connect` is a callable returning a fresh connection: sqlite3 objects
    belong to the thread that made them.
    """

    def work():
        conn = None
        try:
            conn = connect()
            if due(conn):
                found = check_now(conn)
                if found and is_newer(found):
                    log.info("update available: %s", found)
        except Exception:  # noqa: BLE001 - a failed check is a non-event
            log.debug("update check failed", exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=work, name="magoo-update-check", daemon=True).start()
