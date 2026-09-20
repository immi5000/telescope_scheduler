"""The single place the process reads its environment.

Everything else takes what it needs as an argument. That is not ceremony: a
credential read from ``os.environ`` deep inside a provider is invisible at the
call site, impossible to override in a test, and the usual route by which a
secret ends up in a log line. Here, the values are read once, and
``__repr__`` cannot print them because they are never stored on a repr-able
field without masking.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "TSCHED_"

#: Vite's dev server. A module constant rather than a class attribute because
#: ``slots=True`` makes ``Settings.cors_origins`` a descriptor, not the default.
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader. Does NOT overwrite real environment variables --
    an exported value must always win over a file on disk."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass(frozen=True, slots=True)
class Settings:
    cache_dir: Path = Path("data/cache")
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    host: str = "127.0.0.1"
    port: int = 8000
    solve_seconds: float = 4.0
    live_refresh_minutes: float = 15.0
    """How often a night that is still happening asks its weather source for
    anything new. Zero turns the watch off (a manual refresh still works).
    Not lower than ~15: Open-Meteo is free, rate-limited, and publishes a new
    model run a few times a day, so a faster poll finds nothing and risks 429s."""

    #: Credentials. Masked in every representation; see ``describe()``.
    spacetrack_user: str = field(default="", repr=False)
    spacetrack_pass: str = field(default="", repr=False)

    @property
    def has_spacetrack(self) -> bool:
        return bool(self.spacetrack_user and self.spacetrack_pass)

    def describe(self) -> dict[str, str | bool | int | float]:
        """Safe to log and safe to serve. Credentials appear only as a boolean."""
        return {
            "cache_dir": str(self.cache_dir),
            "host": self.host,
            "port": self.port,
            "solve_seconds": self.solve_seconds,
            "live_refresh_minutes": self.live_refresh_minutes,
            "spacetrack_configured": self.has_spacetrack,
        }


def load_settings(root: Path | None = None) -> Settings:
    base = root or Path.cwd()
    env = {**_load_dotenv(base / ".env"), **os.environ}

    def get(name: str, default: str = "") -> str:
        return env.get(ENV_PREFIX + name, env.get(name, default))

    origins = get("CORS_ORIGINS")
    return Settings(
        cache_dir=Path(get("CACHE_DIR", "data/cache")),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip())
        or DEFAULT_CORS_ORIGINS,
        host=get("HOST", "127.0.0.1"),
        port=int(get("PORT", "8000")),
        solve_seconds=float(get("SOLVE_SECONDS", "4.0")),
        live_refresh_minutes=float(get("LIVE_REFRESH_MINUTES", "15")),
        spacetrack_user=get("SPACETRACK_USER"),
        spacetrack_pass=get("SPACETRACK_PASS"),
    )
