"""Turning what a person types into a catalogue key.

Two kinds of query, two functions:

``designation_key``  "NGC 7000", "n7000", "M031", "Sh 2-101", "Caldwell 20"
                     -> ``ngc7000``, ``m31``, ``sh2-101``, ``c20``. Anchored
                     patterns, so "M 111" and "C 110" are not designations
                     at all rather than being coerced into one.
``name_key``         "The Running Man Nebula", "Bode's Galaxy" -> ``running
                     man``, ``bodes``. Case, punctuation, a leading "the" and
                     the generic words (nebula, galaxy, cluster) do not count.

The build script and the runtime share this module, so an alias written into
the data file and a query typed into the search box are normalised by the
same code.

One trap is worth naming. The Sharpless catalogue is "Sh 2", and a pattern
that treats that "2" as optional will read "Sharpless 240" as Sh2-40. Here the
catalogue's "2" only counts when a separator follows it ("Sh2-240",
"Sh 2 240", "Sharpless 2-240"); a bare number after "Sh" or "Sharpless" is the
region number itself.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Final

__all__ = ["clean", "designation_key", "name_key"]

_DASHES: Final = re.compile("[\u2010-\u2015\u2212]")

_Rule = tuple[re.Pattern[str], Callable[[re.Match[str]], str | None]]


def _in_range(n: int, lo: int, hi: int, key: str) -> str | None:
    return key if lo <= n <= hi else None


def _suffix(m: re.Match[str], group: int) -> str:
    s = m.group(group)
    return s if s else ""


_RULES: Final[tuple[_Rule, ...]] = (
    (
        re.compile(r"^(?:m|messier)\s*-?\s*0*(\d{1,3})$"),
        lambda m: _in_range(int(m.group(1)), 1, 110, f"m{int(m.group(1))}"),
    ),
    # OpenNGC's NED component suffix: "NGC 4656 NED01" is the Hockey Stick.
    (
        re.compile(r"^(ngc|ic)\s*-?\s*0*(\d{1,4})\s*-?\s*ned\s*0*(\d{1,2})$"),
        lambda m: f"{m.group(1)}{int(m.group(2))}-ned{int(m.group(3)):02d}",
    ),
    (
        re.compile(r"^(?:ngc|n)\s*-?\s*0*(\d{1,4})\s*([a-z])?$"),
        lambda m: f"ngc{int(m.group(1))}{_suffix(m, 2)}",
    ),
    # No bare "i" prefix: "i 10" is far more likely a typo than IC 10.
    (
        re.compile(r"^ic\s*-?\s*0*(\d{1,4})\s*([a-z])?$"),
        lambda m: f"ic{int(m.group(1))}{_suffix(m, 2)}",
    ),
    (
        re.compile(r"^(?:c|cald|caldwell)\s*-?\s*0*(\d{1,3})$"),
        lambda m: _in_range(int(m.group(1)), 1, 109, f"c{int(m.group(1))}"),
    ),
    # Sharpless, catalogue "2" followed by a separator: Sh2-240, Sh 2 240.
    (
        re.compile(r"^(?:sh|sharpless)\s*-?\s*2\s*[-\s]\s*0*(\d{1,3})$"),
        lambda m: _in_range(int(m.group(1)), 1, 313, f"sh2-{int(m.group(1))}"),
    ),
    # "Sh2101": the glued form, unambiguous because "sh2" is spelled out.
    (
        re.compile(r"^sh2\s*0*(\d{1,3})$"),
        lambda m: _in_range(int(m.group(1)), 1, 313, f"sh2-{int(m.group(1))}"),
    ),
    # "Sharpless 240", "Sh 240": the number IS the region number.
    (
        re.compile(r"^(?:sh|sharpless)\s+0*(\d{1,3})$"),
        lambda m: _in_range(int(m.group(1)), 1, 313, f"sh2-{int(m.group(1))}"),
    ),
    (
        re.compile(r"^(?:mel|melotte)\s*-?\s*0*(\d{1,3})$"),
        lambda m: f"mel{int(m.group(1))}",
    ),
    (
        re.compile(r"^(?:cr|cl|collinder)\s*-?\s*0*(\d{1,3})$"),
        lambda m: f"cr{int(m.group(1))}",
    ),
    (
        re.compile(r"^(?:b|barnard)\s*-?\s*0*(\d{1,3})$"),
        lambda m: f"b{int(m.group(1))}",
    ),
    (
        re.compile(r"^(?:h|harvard)\s*-?\s*0*(\d{1,2})$"),
        lambda m: f"h{int(m.group(1))}",
    ),
    (
        re.compile(r"^eso\s*-?\s*0*(\d{1,3})\s*-\s*0*(\d{1,3})$"),
        lambda m: f"eso{int(m.group(1))}-{int(m.group(2))}",
    ),
    (
        re.compile(r"^(ldn|lbn|vdb|hcg|pgc|ugca|ugc|arp|abell|ced|mwsc)\s*-?\s*0*(\d{1,7})$"),
        lambda m: f"{m.group(1)}{int(m.group(2))}",
    ),
)

#: Words that describe a KIND of object rather than naming one. "Orion" and
#: "Orion Nebula" are the same query; "Nebula" alone is no query at all.
_GENERIC: Final = re.compile(
    r"\b(?:nebula|nebulae|galaxy|galaxies|cluster|star cluster|globular|planetary|asterism)\b"
)


def clean(s: str) -> str:
    """Case-fold, unify dashes, turn ``_`` and ``.`` into spaces, collapse runs
    of whitespace."""
    s = unicodedata.normalize("NFKC", s).casefold().strip()
    s = _DASHES.sub("-", s)
    s = re.sub(r"[_.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def designation_key(query: str) -> str | None:
    """The canonical key for a catalogue designation, or None if ``query`` is
    not one. Caldwell numbers come back as ``c<n>``: which object that is, is
    the catalogue's business, not the parser's."""
    s = clean(query)
    for pattern, build in _RULES:
        m = pattern.match(s)
        if m:
            return build(m)
    return None


def name_key(name: str, *, generic: bool = False) -> str:
    """A common name reduced to what distinguishes it.

    ``generic=True`` keeps the words that say what KIND of object it is. The
    catalogue matches on that stricter key first: "Butterfly Nebula" and
    "Butterfly Cluster" are different objects, and only when a query leaves
    the kind out ("Butterfly") does the looser key -- and then only if it
    points at exactly one object -- get a say.
    """
    s = clean(name)
    s = re.sub(r"^the ", "", s)
    s = s.replace("'s", "s").replace("\u2019s", "s")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    if not generic:
        s = _GENERIC.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()
