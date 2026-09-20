"""The deep-sky catalogue: ~1,850 objects worth pointing a camera at.

Built by ``scripts/build_catalog.py`` from OpenNGC and the Sharpless catalogue
into ``dso.csv`` beside this module, which ships with the package -- nothing here touches the
network, and a cold load takes a few tens of milliseconds.

Three ways in:

``objects()``     every object, in the data file's order (Messier, NGC, IC,
                  Sharpless, the rest).
``by_id(id)``     exact canonical id: ``m31``, ``ngc7000``, ``sh2-101``.
``resolve(q)``    anything a person might type -- "NGC 224", "n7000",
                  "Sh 2-101", "Caldwell 20", "Horsehead", "Bode's Galaxy" --
                  normalised, then looked up among ids, every alias and the
                  common names.

When ``resolve`` finds nothing, ``ambiguous(q)`` lists what a shared name
("Helix") or a group ("Veil Nebula") could mean, and ``refusal(q)`` says why
in words.

``surface_brightness`` is a V MEAN surface brightness in mag/arcsec^2 over the
catalogue ellipse: the number the exposure calculator runs on. How each one
was obtained is in ``sb_basis`` (see the build script); ``typical`` and
``sharpless`` mean no catalogue magnitude stood behind it.

The data is CC BY-SA 4.0; see ``NOTICE`` beside this module (``notice_text()``).
"""

from __future__ import annotations

import csv
import functools
import io
import re
from dataclasses import dataclass
from importlib import resources
from typing import Final

from tscheduler.catalog.normalise import clean, designation_key, name_key

__all__ = [
    "ATTRIBUTION",
    "LICENSE_URL",
    "NOTICE_URL",
    "Catalogue",
    "DeepSkyObject",
    "ambiguous",
    "by_id",
    "load",
    "notice_text",
    "objects",
    "refusal",
    "resolve",
]

#: One line, for wherever the catalogue is listed. CC BY-SA 4.0 asks for the
#: creator, a statement that the data was modified, and the licence; show it
#: with links to ``LICENSE_URL`` and ``NOTICE_URL``.
ATTRIBUTION: Final = (
    "Catalogue: OpenNGC by Mattia Verga et al. (CC BY-SA 4.0), modified; Sharpless 1959 via VizieR"
)
#: Where the API serves ``notice_text()``: the full credits and the list of changes.
NOTICE_URL: Final = "/api/catalog/notice"
LICENSE_URL: Final = "https://creativecommons.org/licenses/by-sa/4.0/"

#: Surface-brightness provenance that means "no catalogue magnitude behind it".
ESTIMATED_BASES: Final = frozenset({"typical", "sharpless"})


@dataclass(frozen=True, slots=True)
class DeepSkyObject:
    id: str
    """Canonical and stable: ``m31``, ``ngc7000``, ``ic1396``, ``sh2-101``."""
    designation: str
    """Every principal catalogue number, the object's own first: "M 31 · NGC 224"."""
    short: str
    """The designation a target's name starts with: ``M31``, ``NGC 7000``,
    ``Sh2-101``. The frontend's ``shortName()`` recovers it from the name."""
    names: tuple[str, ...]
    """Common names, the one to display first."""
    aliases: tuple[str, ...]
    """Every other key that resolves here: cross-listed NGC/IC numbers,
    duplicates, Caldwell, Sharpless, LBN, PGC, UGC..."""
    tags: tuple[str, ...]
    """Groups it belongs to ("Leo Triplet"): searchable, never resolved."""
    type: str
    type_label: str
    code: str
    """OpenNGC's type code, or ``HII``/``PN``/``SNR`` for a Sharpless region."""
    constellation: str
    ra_deg: float
    dec_deg: float
    major_arcmin: float | None
    minor_arcmin: float | None
    v_mag: float | None
    surface_brightness: float
    sb_basis: str
    """``leda`` | ``derived`` | ``typical`` | ``sharpless``."""
    messier: int | None
    caldwell: int | None
    plannable: bool
    why_not: str | None
    """Why an object is not plannable; None when it is."""
    search_text: str

    @property
    def common_name(self) -> str | None:
        return self.names[0] if self.names else None

    @property
    def name(self) -> str:
        """The common name, or failing that the object's own designation."""
        return self.names[0] if self.names else self.designation.split(" · ")[0]

    @property
    def target_name(self) -> str:
        """What a scheduled target is called: "M31 Andromeda Galaxy",
        "NGC 7000 North America Nebula", "NGC 7331". Designation first,
        always -- the timeline's short labels are cut from the front."""
        return f"{self.short} {self.names[0]}" if self.names else self.short

    @property
    def magnitude_estimated(self) -> bool:
        return self.sb_basis in ESTIMATED_BASES


@dataclass(frozen=True, slots=True)
class Catalogue:
    objects: tuple[DeepSkyObject, ...]
    by_id: dict[str, DeepSkyObject]
    by_alias: dict[str, DeepSkyObject]
    by_name: dict[str, DeepSkyObject]
    """Exact common name, kind words kept ("butterfly nebula")."""
    by_loose_name: dict[str, DeepSkyObject]
    """Common name with the kind words dropped ("butterfly"), only where that
    still points at exactly one object."""
    ambiguous_names: dict[str, tuple[DeepSkyObject, ...]]
    """The loose names that point at more than one object ("helix": the Helix
    Nebula and the Helix Galaxy), better-known first."""
    groups: dict[str, tuple[DeepSkyObject, ...]]
    """Group tags, by both name keys ("leo triplet", "veil nebula", "veil"),
    to their members, better-known first."""


def _float(x: str) -> float | None:
    return float(x) if x else None


def _int(x: str) -> int | None:
    return int(x) if x else None


def _split(x: str) -> tuple[str, ...]:
    return tuple(p for p in x.split("|") if p)


_KEY_PARTS: Final = re.compile(r"^([a-z]+?)(2-|)(\d.*)$")

_LONG_FORMS: Final = {"m": "messier", "c": "caldwell", "sh": "sharpless"}


def _key_forms(key: str) -> list[str]:
    """``ngc7000`` -> ``ngc7000 ngc 7000``; ``sh2-101`` -> ``sh2-101 sh2 101``;
    ``m31`` -> ``m31 m 31 messier 31``. The spellings a person types."""
    m = _KEY_PARTS.match(key)
    if m is None or "-ned" in key:
        return [key]
    cat, two, number = m.groups()
    forms = [key]
    if two:  # Sharpless
        forms += [f"sh2 {number}", f"sh 2-{number}", f"sharpless {number}"]
    else:
        forms.append(f"{cat} {number}")
        if cat in _LONG_FORMS:
            forms.append(f"{_LONG_FORMS[cat]} {number}")
    return forms


def _search_text(
    keys: list[str], names: tuple[str, ...], tags: tuple[str, ...], extra: list[str]
) -> str:
    parts: list[str] = []
    for k in keys:
        parts += _key_forms(k)
    for n in (*names, *tags):
        low = clean(n)
        parts.append(low)
        bare = re.sub("['\u2019.]", "", low)
        if bare != low:
            parts.append(bare)
    text = " ".join(dict.fromkeys(parts))
    # Type, label and constellation, each only if it adds something: a
    # "galaxy" is already in "spiral galaxy".
    for x in extra:
        low = clean(x)
        if low and low not in text:
            text = f"{text} {low}"
    return text


def _parse(text: str) -> Catalogue:
    objs: list[DeepSkyObject] = []
    for r in csv.DictReader(io.StringIO(text)):
        names, aliases, tags = _split(r["names"]), _split(r["aliases"]), _split(r["tags"])
        keys = [r["id"], *aliases]
        messier, caldwell = _int(r["messier"]), _int(r["caldwell"])
        if caldwell is not None and f"c{caldwell}" not in keys:
            keys.append(f"c{caldwell}")
        objs.append(
            DeepSkyObject(
                id=r["id"],
                designation=r["designation"],
                short=r["short"],
                names=names,
                aliases=aliases,
                tags=tags,
                type=r["type"],
                type_label=r["type_label"],
                code=r["code"],
                constellation=r["constellation"],
                ra_deg=float(r["ra_deg"]),
                dec_deg=float(r["dec_deg"]),
                major_arcmin=_float(r["major_arcmin"]),
                minor_arcmin=_float(r["minor_arcmin"]),
                v_mag=_float(r["v_mag"]),
                surface_brightness=float(r["sb"]),
                sb_basis=r["sb_basis"],
                messier=messier,
                caldwell=caldwell,
                plannable=r["plannable"] == "1",
                why_not=r["why_not"] or None,
                search_text=_search_text(
                    keys,
                    names,
                    tags,
                    [r["type_label"], r["type"].replace("-", " "), r["constellation"]],
                ),
            )
        )

    by_id = {o.id: o for o in objs}
    if len(by_id) != len(objs):
        raise ValueError("catalogue ids are not unique")

    # Aliases never shadow an id; the first object to claim one keeps it.
    by_alias: dict[str, DeepSkyObject] = {}
    for o in objs:
        for a in o.aliases:
            if a not in by_id:
                by_alias.setdefault(a, o)
        if o.caldwell is not None:
            by_alias.setdefault(f"c{o.caldwell}", o)

    # A name shared by two objects goes to the better-known one: Messier
    # (including the M102 alias) over Caldwell over the rest, then file order.
    # So "Spindle Galaxy" is NGC 5866 and "Eastern Veil" is NGC 6992 (C33).
    def fame(o: DeepSkyObject) -> int:
        if o.messier is not None or any(re.fullmatch(r"m\d+", a) for a in o.aliases):
            return 0
        return 1 if o.caldwell is not None else 2

    def add(table: dict[str, list[DeepSkyObject]], key: str, o: DeepSkyObject) -> None:
        if not key:
            return
        members = table.setdefault(key, [])
        if all(m is not o for m in members):
            members.append(o)

    by_name: dict[str, DeepSkyObject] = {}
    loose: dict[str, list[DeepSkyObject]] = {}
    groups: dict[str, list[DeepSkyObject]] = {}
    for o in sorted(objs, key=fame):
        for n in o.names:
            by_name.setdefault(name_key(n, generic=True), o)
            add(loose, name_key(n), o)
        for t in o.tags:
            add(groups, name_key(t, generic=True), o)
            add(groups, name_key(t), o)

    return Catalogue(
        objects=tuple(objs),
        by_id=by_id,
        by_alias=by_alias,
        by_name=by_name,
        by_loose_name={k: v[0] for k, v in loose.items() if len(v) == 1},
        ambiguous_names={k: tuple(v) for k, v in loose.items() if len(v) > 1},
        groups={k: tuple(v) for k, v in groups.items()},
    )


@functools.cache
def load() -> Catalogue:
    """The catalogue, parsed once per process."""
    text = resources.files("tscheduler.catalog").joinpath("dso.csv").read_text("utf-8")
    return _parse(text)


def objects() -> tuple[DeepSkyObject, ...]:
    return load().objects


def by_id(object_id: str) -> DeepSkyObject | None:
    return load().by_id.get(object_id)


def resolve(query: str) -> DeepSkyObject | None:
    """The object ``query`` means, or None.

    Order: an exact id; a catalogue designation in any spelling (then its
    aliases); an exact common name; a common name with its kind word left
    out, if that is unambiguous. A query that parses as a designation but
    names nothing here is None -- it is never re-read as a common name.
    """
    cat = load()
    q = clean(query)
    if not q:
        return None
    if q in cat.by_id:
        return cat.by_id[q]
    if q in cat.by_alias:
        return cat.by_alias[q]
    key = designation_key(q)
    if key is not None:
        return cat.by_id.get(key) or cat.by_alias.get(key)
    found = cat.by_name.get(name_key(q, generic=True))
    if found is not None:
        return found
    return cat.by_loose_name.get(name_key(q))


def ambiguous(query: str) -> list[DeepSkyObject]:
    """The objects ``query`` could mean when it names no single one, better
    known first; empty when it resolves, or names nothing here.

    Two cases: a common name that several objects share once the kind word
    is left out ("Helix": the Helix Nebula and the Helix Galaxy; "Owl"), and
    a group ("Leo Triplet", "Veil Nebula", "Veil"), which is searchable but
    never resolves to one of its members.
    """
    if resolve(query) is not None:
        return []
    q = clean(query)
    if not q or designation_key(q) is not None:
        return []
    cat = load()
    exact, loose = name_key(q, generic=True), name_key(q)
    found = cat.groups.get(exact) or cat.ambiguous_names.get(loose) or cat.groups.get(loose)
    return list(found or ())


def _choice(query: str, obj: DeepSkyObject) -> str:
    """One option in an ambiguity message, "Helix Nebula (NGC 7293)": the
    name ``query`` matched (else the display name), then the designation."""
    key = name_key(query)
    name = next((n for n in obj.names if name_key(n) == key), obj.common_name)
    return f"{name} ({obj.short})" if name else obj.short


def refusal(query: str) -> str | None:
    """Why ``query`` cannot be scheduled from the catalogue, in words, or
    None if it can. For turning a refused target into a useful 422."""
    obj = resolve(query)
    if obj is None:
        options = [_choice(query, o) for o in ambiguous(query)]
        if not options:
            return f"{query!r} is not in the catalogue"
        if len(options) == 1:
            # Only a group can have one member here ("Deer Lick Group"): a
            # shared name has two objects by definition. Not "ambiguous".
            return f"{query!r} is a group, not one object: did you mean {options[0]}?"
        return f"{query!r} is ambiguous: did you mean {', '.join(options[:-1])} or {options[-1]}?"
    if not obj.plannable:
        label = f"{obj.common_name} ({obj.designation})" if obj.common_name else obj.designation
        return f"{label} cannot be planned: {obj.why_not}"
    return None


@functools.cache
def notice_text() -> str:
    """The catalogue's licence notice (``NOTICE`` beside this module): the
    sources, their licences, and what was changed. Served at ``NOTICE_URL``."""
    return resources.files("tscheduler.catalog").joinpath("NOTICE").read_text("utf-8")
