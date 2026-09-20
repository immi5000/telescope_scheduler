"""``python -m tscheduler.api`` -- start the server.

Exists so the server has one obvious entry point that does not require anyone
to remember the import path of the app object.
"""

from __future__ import annotations

import argparse

import uvicorn

from tscheduler.config import load_settings


def main() -> None:
    cfg = load_settings()
    ap = argparse.ArgumentParser(description="Run the Traveling Telescope API.")
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--reload", action="store_true", help="restart on source changes")
    args = ap.parse_args()

    uvicorn.run(
        "tscheduler.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
