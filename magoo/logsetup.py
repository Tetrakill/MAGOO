"""Logging, and the guards that keep a windowed build from dying on print().

A PyInstaller --windowed build has no console: Windows leaves sys.stdout and
sys.stderr as None, so the FIRST bare print() raises AttributeError. That
would have killed the dashboard's game-data download, whose worker thread
prints progress — the failure surfaces through sdeimport's except-all as a
cryptic UI message rather than anything a user could act on.

So: ensure_std_streams() makes stray prints harmless anywhere, and
configure() gives the app a real log file to diagnose from, since a packaged
user has no terminal to read a traceback in.
"""

import logging
import logging.handlers
import sys
import threading

from magoo import config

LOG_NAME = "magoo"
_MAX_BYTES = 1 << 20  # 1 MB per file
_BACKUPS = 3

_configured = False


class _LogWriter:
    """Minimal write-only stream that forwards whole lines to the log.

    Stands in for a missing stdout/stderr so print() keeps working; partial
    writes are buffered because print() emits its text and its newline as
    separate write() calls.
    """

    def __init__(self, level: int):
        self._level = level
        self._buf = ""
        self._guard = threading.local()

    def write(self, text: str) -> int:
        # Second guard, independent of has_console(): if ANY handler writes
        # to stdout/stderr while we are logging — including logging's own
        # handleError path when a handler raises — the write lands back
        # here and recurses. Drop it instead. Per-thread, so one thread's
        # logging never silences another's.
        if getattr(self._guard, "busy", False):
            return len(text)
        self._guard.busy = True
        try:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if line:
                    logging.getLogger(LOG_NAME).log(self._level, line)
        finally:
            self._guard.busy = False
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def has_console() -> bool:
    """True only when stdout and stderr are REAL streams.

    The _LogWriter substitutes installed by ensure_std_streams do not count.
    Treating one as a console attaches a StreamHandler that writes into the
    very object that forwards writes back to the logger — logging feeding
    itself until the stack runs out. That is not hypothetical: it is what
    crashed the first packaged build on launch, and it only reproduces with
    no console attached, i.e. exactly when a user double-clicks the exe.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or isinstance(stream, _LogWriter):
            return False
    return True


def is_tty() -> bool:
    """True only for a real interactive console. This — not has_console() —
    is the test for whether a \r progress animation makes sense: output
    redirected to a file, or standing in for a missing stream, should get
    plain lines instead."""
    try:
        return bool(sys.stdout is not None and sys.stdout.isatty())
    except (AttributeError, ValueError):  # closed or exotic stream
        return False


def ensure_std_streams() -> None:
    """Give sys.stdout/stderr something safe to write to.

    Called by the packaged entry point before anything else runs. Cheap and
    idempotent, so it is also safe to call from a worker thread's entry.
    """
    if sys.stdout is None:
        sys.stdout = _LogWriter(logging.INFO)
    if sys.stderr is None:
        sys.stderr = _LogWriter(logging.ERROR)


def configure(console: bool | None = None) -> logging.Logger:
    """Set up the 'magoo' logger. Idempotent; safe to call from any entry
    point. A log directory we cannot write to must never stop the app from
    starting, so file-handler failures degrade to console-only."""
    global _configured
    log = logging.getLogger(LOG_NAME)
    if _configured:
        return log

    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            config.LOG_DIR / "magoo.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        log.addHandler(handler)
    except OSError:
        # Read-only or unwritable data dir. The console handler below (or
        # nothing at all) is better than refusing to start.
        pass

    if console is None:
        console = has_console()
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        log.addHandler(stream)

    _configured = True
    return log
