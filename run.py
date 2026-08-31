"""Entry point: launch the Magoo web UI.

Serves on localhost at config.DEFAULT_PORT. The port is fixed rather than
floating because EVE SSO exact-matches the callback URL registered at
developers.eveonline.com against the redirect_uri we send — see
config.CALLBACK_URL. PORT still overrides it for development.
"""

import os
import sys

from magoo import config, logsetup

if __name__ == "__main__":
    # Before Flask is imported: a windowed build leaves sys.stdout as None,
    # and any stray print() on a worker thread would raise AttributeError.
    logsetup.ensure_std_streams()
    logsetup.configure()

    from magoo.web import create_app

    app = create_app()
    # Werkzeug's interactive debugger executes code in the browser — dev
    # sets MAGOO_DEBUG=1; distributed users get plain 500s. It is forced off
    # when frozen regardless: the reloader re-execs sys.executable with the
    # original argv, which a packaged build cannot survive.
    debug = (
        os.environ.get("MAGOO_DEBUG") == "1"
        and not getattr(sys, "frozen", False)
    )
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", config.DEFAULT_PORT)),
        debug=debug,
    )
