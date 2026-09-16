"""Canonical hashing, for fingerprints that must be stable across processes.

Fingerprints are load-bearing twice over: they key layer memoization, and they
are the assertion in the no-lookahead property test (``plan_full.fingerprint()
== plan_trunc.fingerprint()``). So they must be deterministic, cheap, and
insensitive to dict ordering.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

_DIGEST_SIZE = 16


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace, ASCII-escaped."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def hash_json(obj: Any) -> str:
    return hashlib.blake2b(canonical_json(obj).encode(), digest_size=_DIGEST_SIZE).hexdigest()


def hash_arrays(named: dict[str, np.ndarray]) -> str:
    """Hash numpy arrays by raw bytes, in sorted field order.

    ``np.ascontiguousarray`` matters: a transposed view and its copy compare
    equal but have different strides, and ``tobytes()`` would otherwise differ.
    """
    h = hashlib.blake2b(digest_size=_DIGEST_SIZE)
    for name in sorted(named):
        arr = named[name]
        h.update(name.encode())
        h.update(str(arr.dtype).encode())
        h.update(str(arr.shape).encode())
        h.update(np.ascontiguousarray(arr).tobytes())
    return h.hexdigest()
