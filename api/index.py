"""Vercel's entrypoint: the same app, on a host that forgets.

Vercel's Python runtime looks for a module-level ASGI ``app`` here and serves
it. Everything else in this file exists to reconcile two assumptions the app
makes with a platform that does not share them.

**The package is not installed.** The repository is a ``src`` layout and
Vercel installs ``requirements.txt``, not the project itself, so ``src`` goes
on the path by hand rather than through an editable install that never
happened.

**The filesystem is read-only.** ``CelesTrakProvider`` caches orbital elements
under the configured cache directory, whose default (``data/cache``) is inside
the deployment and therefore unwritable. ``/tmp`` is the one writable path,
and it is per-instance and short-lived -- which suits a cache exactly: a miss
costs one download, and nothing is lost when the instance goes away.

``setdefault`` rather than assignment, so a real ``TSCHED_CACHE_DIR`` set in
the project's environment still wins.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("TSCHED_CACHE_DIR", "/tmp/tsched-cache")

from tscheduler.api.app import create_app  # noqa: E402

app = create_app()
