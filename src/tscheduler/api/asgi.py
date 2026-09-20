"""The deployed process: the API, with the built UI behind it.

Running as a container rather than a function changes one thing and fixes
another. The fix is size -- a function bundle has to fit in 225 MB and
ortools' own shared libraries are 79 MB of that before numpy, astropy and
pandas are counted, so CP-SAT simply does not fit; a container image may be
15 GB. The change is that there is no CDN in front of the static files any
more, because everything now arrives at one process.

So this module mounts the built SPA under the API. Order is the whole trick:
Starlette matches routes in the order they were added, and a mount at ``/``
matches everything. Registering it after ``create_app`` has added every
``/api/...`` route means the API wins its own paths and the mount picks up
what is left -- which is exactly the static file, or the app shell.

The mount is conditional because ``static/`` is a build artefact. Running
this module from a source checkout without one is a normal thing to do (it is
what the API tests do), and it should not fail on a missing directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from tscheduler.api.app import create_app

#: Where the Dockerfile leaves the Vite build. Overridable so the layout is
#: stated in one place rather than assumed in two.
STATIC_DIR = Path(os.environ.get("TSCHED_STATIC_DIR", "static"))

app = create_app()

if STATIC_DIR.is_dir():
    # html=True serves index.html for a directory request, which is what makes
    # "/" the app rather than a 404.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")
