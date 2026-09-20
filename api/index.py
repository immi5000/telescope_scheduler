"""Vercel's entrypoint: the API, with the built UI mounted behind it.

Everything is served by this one function, static files included. That is not
the arrangement you would choose -- a CDN should serve the SPA -- but it is
the one that builds: setting `outputDirectory` puts Vercel on a code path
where the Python bundle measures 526 MB against a 500 MB ceiling, while this
shape measures 291 MB and passes. The difference is Vercel's, not ours, and
this is the side of it that deploys.

Two environment facts the app does not assume on its own:

**The package may not be installed.** uv installs the project from
pyproject.toml, so `tscheduler` is usually importable already; `src` goes on
the path anyway, because a bundle that skipped that step should fail at
import here rather than somewhere less obvious.

**The filesystem is read-only.** `CelesTrakProvider` caches orbital elements
under the configured cache directory, whose default sits inside the
deployment and cannot be written. `/tmp` is the one writable path, and being
per-instance and short-lived suits a cache exactly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("TSCHED_CACHE_DIR", "/tmp/tsched-cache")
# The Vite build, uploaded rather than built here. asgi.py mounts it if it
# exists and serves the API alone if it does not.
os.environ.setdefault("TSCHED_STATIC_DIR", str(_ROOT / "frontend" / "dist"))

from tscheduler.api.asgi import app  # noqa: E402

__all__ = ["app"]
