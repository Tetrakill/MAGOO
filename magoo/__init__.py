"""Project Magoo — EVE Online industry planner.

Only the version lives here. It is read by the ESI/SDE User-Agent
(config.USER_AGENT), the nav chip, the update check and the build scripts,
so this file must stay importable without pulling in Flask or SciPy — a
release bump is a one-line edit and nothing else.
"""

__version__ = "1.24.0"
