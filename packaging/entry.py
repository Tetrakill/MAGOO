"""Frozen entry point. Kept trivial on purpose.

PyInstaller needs a script, not a module, and anything that runs before
magoo.desktop.main() runs before logging exists — so it does nothing that
could fail.
"""

import sys

from magoo.desktop import main

if __name__ == "__main__":
    sys.exit(main())
