#!/usr/bin/env python3
"""Build the deep-sky catalogue, ``src/tscheduler/catalog/dso.csv``.

Sources, pinned so a rebuild is byte-identical:

* OpenNGC (Mattia Verga et al., CC BY-SA 4.0), ``NGC.csv`` and
  ``addendum.csv`` at commit ``OPENNGC_COMMIT``.
* The Sharpless (1959) catalogue of HII regions, VizieR VII/20, with the
  J2000 positions VizieR computes on the fly.
* ``src/tscheduler/catalog/names.csv``: hand-maintained common names.
* The curated tables in this file: every place we overrule the sources is
  here, with the reason beside it.

What it does, in order:

1. SELECT the imaging-worthy rows: all Messier and Caldwell objects, two
   Hickson groups, a short allow-list, and per-type magnitude and size cuts
   (galaxies V <= 12 and >= 2', planetaries V <= 13 and >= 0.25', ...).
2. FOLD duplicates: OpenNGC's ``Dup`` rows, and a few pairs we merge on
   purpose (NGC 2238 into NGC 2237, the Rosette), become aliases.
3. DERIVE a V mean surface brightness for every object -- the number the
   exposure calculator runs on. See ``surface_brightness``.
4. ADD Sharpless regions of 5' or more that no selected NGC/IC nebula of
   comparable size already covers, and the few (``SH2_WHOLE``) where the
   nebula that seems to cover one is only a piece of it.
5. WRITE one CSV row per object, sorted by id, so a rebuild diffs cleanly.

Usage::

    uv run python scripts/build_catalog.py                  # download the sources
    uv run python scripts/build_catalog.py --openngc DIR --sharpless FILE
    uv run python scripts/build_catalog.py --check          # exit 1 if dso.csv is stale

``DIR`` is an OpenNGC clone (the directory holding ``database_files/``) and
``FILE`` the VizieR TSV saved from ``SHARPLESS_URL``.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tscheduler.catalog.normalise import designation_key

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "tscheduler" / "catalog"
OUT = PACKAGE / "dso.csv"
NAMES = PACKAGE / "names.csv"

OPENNGC_COMMIT = "da90466031b0372c896588b85be6016c617e205b"
OPENNGC_URL = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/{commit}/database_files/{file}"
SHARPLESS_URL = (
    "https://vizier.cds.unistra.fr/viz-bin/asu-tsv?-source=VII/20/catalog"
    "&-out.max=unlimited&-out.add=_RAJ2000,_DEJ2000&-oc.form=d"
    "&-out=Sh2,Diam,Form,Struct,Bright,Stars"
)

# --------------------------------------------------------------------------
# types
# --------------------------------------------------------------------------

GALAXY = ("G", "GPair", "GTrpl", "GGroup")
NEBULA = ("HII", "EmN", "Neb", "RfN", "SNR", "Cl+N")

#: OpenNGC type code -> (wire type, display label).
TYPES: dict[str, tuple[str, str]] = {
    "G": ("galaxy", "Galaxy"),
    "GPair": ("galaxy", "Galaxy pair"),
    "GTrpl": ("galaxy", "Galaxy triplet"),
    "GGroup": ("galaxy", "Galaxy group"),
    "PN": ("planetary-nebula", "Planetary nebula"),
    "HII": ("emission-nebula", "HII region"),
    "EmN": ("emission-nebula", "Emission nebula"),
    "Neb": ("emission-nebula", "Nebula"),
    "RfN": ("reflection-nebula", "Reflection nebula"),
    "SNR": ("supernova-remnant", "Supernova remnant"),
    "Cl+N": ("cluster-nebula", "Cluster with nebula"),
    "OCl": ("open-cluster", "Open cluster"),
    "GCl": ("globular-cluster", "Globular cluster"),
    "*Ass": ("other", "Stellar association"),
    "**": ("other", "Double star"),
    "Other": ("other", "Asterism"),
    "DrkN": ("other", "Dark nebula"),
}

#: Where OpenNGC's type code is wrong for our purposes.
TYPE_OVERRIDES: dict[str, str] = {
    "NGC6334": "HII",  # the Cat's Paw is an HII region; OpenNGC has SNR
    "NGC2237": "HII",  # the Rosette, after absorbing NGC 2238 (an HII row)
    # The Pleiades: imaged for the reflection nebula as much as the stars. The
    # label (below) still says it is a cluster.
    "Mel022": "RfN",
    # Classic reflection nebulae that OpenNGC files as generic "Neb" (or, for
    # the Maia Nebula, "HII"): starlight scattered by dust, not emission.
    "NGC7023": "RfN",  # the Iris, C4 (HD 200775)
    "NGC1432": "RfN",  # the Maia Nebula, in the Pleiades
    "NGC1435": "RfN",  # the Merope Nebula, in the Pleiades
    "IC4604": "RfN",  # the Rho Ophiuchi Nebula
    "IC4605": "RfN",  # around 22 Sco
    "NGC1975": "RfN",  # beside the Running Man, in Orion
    "NGC6729": "RfN",  # R CrA's nebula, C68
    # Cederblad (1946; VizieR VII/231) nebulae of continuous spectrum, in the
    # reflection-nebula catalogues too. Their SIMBAD B magnitude (quality E,
    # no reference), which OpenNGC carries, is Cederblad's photographic
    # magnitude of the illuminating star, not the nebula's light.
    "NGC6589": "RfN",  # Ced 157a, DG 149; SIMBAD lists RefNeb. OpenNGC: Neb
    "NGC2282": "RfN",  # Ced 87 (BD +1 1503, pg 10.10), vdB 85; SIMBAD: RNe. OpenNGC: HII
    "IC0447": "RfN",  # Ced 78 (its cluster, pg 7.70 = the B 7.70), DG 103. OpenNGC: HII
}

LABEL_OVERRIDES: dict[str, str] = {
    "IC4715": "Star cloud",  # M24
    "Cl399": "Asterism",  # Brocchi's Cluster, the Coathanger
    "C014": "Pair of open clusters",
    "Mel111": "Open cluster",
    "Mel022": "Open cluster with reflection nebula",
}

#: Sharpless regions that are not HII regions (SIMBAD object types).
SH2_TYPES: dict[int, str] = {
    **dict.fromkeys((68, 78, 174, 176, 188, 200, 216, 274, 290), "PN"),
    103: "SNR",  # the Cygnus Loop
    240: "SNR",  # Simeis 147
}

#: Sharpless regions that get a row of their own although an NGC/IC nebula
#: close enough and big enough "covers" them by the rule in ``build``: that
#: nebula is one piece of the region, not the region. Their Sharpless
#: brightness class is their brightest pieces' (which have rows of their own),
#: not a mean over the whole, so they are priced at their type's typical
#: surface brightness instead.
SH2_WHOLE: dict[int, str] = {
    # The Cygnus Loop. NGC 6960, the Western Veil (70'), lies inside its 210'
    # and passes the size test by a whisker (70' >= 0.3 x 210').
    103: "NGC6960",
}

# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

#: Kept whatever the cuts say. Stephan's Quintet and Seyfert's Sextet are
#: groups with no catalogue magnitude; the rest are well-known targets that a
#: size or type cut would otherwise drop.
ALLOW = {"HCG092", "HCG079", "IC0418", "NGC6572", "NGC4414", "Cl399"}

#: Absorbed row -> surviving row. The absorbed row's designations and names
#: become the survivor's. Used where OpenNGC splits one imaging target in two.
MERGE: dict[str, str] = {
    # The Rosette Nebula. OpenNGC puts C49 and the name on NGC 2238, a small
    # part of it, and calls NGC 2237 "Rosette A"; imagers and Wikipedia say
    # NGC 2237.
    "NGC2238": "NGC2237",
    # The Eagle Nebula. OpenNGC has the nebula as IC 4703 and M16 as NGC 6611;
    # both are "the Eagle" to anyone pointing a telescope at it.
    "IC4703": "NGC6611",
}

#: Canonical ids that the OpenNGC name would not give. Messier numbers win
#: over NGC without a table; these are the addendum's Caldwell-only rows.
ID_OVERRIDES: dict[str, str] = {
    "C009": "sh2-155",  # the Cave Nebula
    "C041": "mel25",  # the Hyades
    "C014": "c14",
    "C099": "c99",
}

#: Aliases no source provides.
EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    # OpenNGC follows NED in calling M102 a duplicate of M101. What imagers
    # mean by M102 is almost always the Spindle, NGC 5866.
    "NGC5866": ("m102",),
    "IC0434": ("b33",),  # the Horsehead itself is a dark nebula; plan its background
}

#: Identifiers a source row carries that name a larger whole with a row of
#: its own: key -> (the row that carries it, the row it names).
MOVED_ALIASES: dict[str, tuple[str, str]] = {
    # OpenNGC gives NGC 6960 (the Western Veil) LBN 191, the whole Cygnus Loop.
    "lbn191": ("NGC6960", "Sh2-103"),
}

#: Searchable, never recommended, refused by sessions -- and why.
NOT_PLANNABLE: dict[str, str] = {
    "M040": "a double star, not an extended object -- there is no surface to expose for",
    "NGC6994": "an asterism of four unrelated stars, not an extended object",
    "Cl399": "an asterism of bright stars more than a degree across, not an extended object",
    "C014": (
        "Caldwell 14 names two clusters; plan NGC 869 or NGC 884 (they share a one-degree field)"
    ),
    "C041": (
        "a naked-eye star cluster five degrees across -- a camera-lens target, and "
        "not a surface the exposure model can price"
    ),
    "Mel111": (
        "a naked-eye star cluster four degrees across -- a camera-lens target, and "
        "not a surface the exposure model can price"
    ),
    "C099": (
        "a dark nebula: a deficit against the background, so there is no source "
        "brightness to expose for"
    ),
}
NOT_PLANNABLE_BY_TYPE = {
    "**": NOT_PLANNABLE["M040"],
    "Other": "an asterism, not an extended object",
    "DrkN": NOT_PLANNABLE["C099"],
}

# --------------------------------------------------------------------------
# sizes and magnitudes we overrule
# --------------------------------------------------------------------------

#: (major, minor) arcmin. Mostly nebulae whose OpenNGC size is that of the
#: embedded cluster, which would make a two-degree nebula "fit" a small chip.
#: Hand-curated only: applying Sharpless diameters mechanically would make
#: IC 420 twenty degrees across (it sits inside Barnard's Loop).
SIZE_OVERRIDES: dict[str, tuple[float, float]] = {
    "IC1396": (170.0, 140.0),  # de.wikipedia (SEDS); Sh2-131 is 170'. OpenNGC: Tr 37, 14'x4'
    "NGC7000": (120.0, 100.0),  # SEDS. OpenNGC: 120'x30'
    "IC1805": (150.0, 150.0),  # en.wikipedia "Heart Nebula". OpenNGC: 60'
    "IC1848": (150.0, 75.0),  # en.wikipedia "Westerhout 5". OpenNGC: 40'x10'
    "NGC6334": (35.0, 20.0),  # en.wikipedia. OpenNGC: 8.4', the cluster
    "NGC6357": (25.0, 25.0),  # de.wikipedia. OpenNGC: 3.9', the cluster
    "IC2944": (75.0, 75.0),  # en.wikipedia (SEDS). OpenNGC: 7.2', the cluster
    "NGC6611": (70.0, 50.0),  # en.wikipedia "Eagle Nebula". OpenNGC: 120'x25'
    "NGC2467": (15.0, 15.0),  # de.wikipedia (SEDS). OpenNGC: 4.2', the cluster
    "NGC1977": (20.0, 10.0),  # en.wikipedia "Sh 2-279". OpenNGC: 10.2', the cluster
    "NGC7822": (100.0, 100.0),  # en.wikipedia (SEDS). OpenNGC: 20'x4'
    "NGC2264": (40.0, 40.0),  # en.wikipedia. OpenNGC: 11.4', the cluster
    "NGC2237": (80.0, 60.0),  # the Rosette, from the absorbed NGC 2238 row
    "Sh2-101": (16.0, 9.0),  # en.wikipedia "Sh 2-101"
    # The Western Veil (C34). OpenNGC's 210'x160' is LBN 191, the whole Cygnus
    # Loop, at the western filament's position an hour of arc from the Loop's
    # centre. SEDS: about 70'x6'.
    "NGC6960": (70.0, 6.0),
    "NGC2068": (8.0, 6.0),  # M78: SEDS and en.wikipedia. OpenNGC: 4.5', the MWSC cluster
    # The whole Cygnus Loop: Green's catalogue of Galactic SNRs, G74.0-8.5,
    # as SIMBAD gives it for "Cyg Loop". Sharpless: a 210' circle.
    "Sh2-103": (230.0, 160.0),
}

#: (RA, Dec) J2000 degrees, where the source row's position is not the target's.
POSITION_OVERRIDES: dict[str, tuple[float, float]] = {
    # The Rosette. The merged row keeps NGC 2237's position ("Rosette A", the
    # western lobe), 16' from the middle of the ring; a frame centred there
    # cuts off the east side. The ring is centred on its cluster, NGC 2244
    # (OpenNGC's NGC 2239 row).
    "NGC2237": (97.98150, 4.94294),
    # The Cygnus Loop: the centre of Green's 230'x160' ellipse (SIMBAD "Cyg
    # Loop", 20 51 00 +30 40 00), 16' from Sharpless's position for Sh2-103.
    "Sh2-103": (312.75000, 30.66667),
}

#: Integrated V where OpenNGC's is unusable. A value here is taken to be the
#: object's own light: the checks in ``borrowed_magnitude`` do not apply.
V_OVERRIDES: dict[str, float] = {
    "NGC6882": 8.1,  # C37: OpenNGC has 14.10; WEBDA/Stellarium give 8.1 for NGC 6885
    "NGC2237": 6.0,  # the Rosette: the absorbed NGC 2238 row's B 6.0 (nebular, B-V ~ 0)
    # M78: V 8.3 over 8'x6' (SEDS, en.wikipedia), one source for both.
    # OpenNGC's V 8.0 (HEASARC's Messier table) is nebular too, but sits on
    # the MWSC cluster's 4.5'.
    "NGC2068": 8.3,
}

# --------------------------------------------------------------------------
# surface brightness
# --------------------------------------------------------------------------

#: 2.5 log10(3600): arcmin^2 -> arcsec^2.
ARCMIN2_TO_ARCSEC2 = 2.5 * math.log10(3600.0)

#: B-V by Hubble class: medians of OpenNGC galaxies with both magnitudes.
#: E and E-S0 -> E; S0 and S0-a -> S0; Sa, Sab, Sb (any bar) -> Sa-Sb;
#: Sbc, Sc, Scd, Sd -> Sbc-Sd; Sm and I* -> Sm/Irr; anything else -> unknown.
BV_HUBBLE = {"E": 0.95, "S0": 0.90, "Sa-Sb": 0.75, "Sbc-Sd": 0.57, "Sm/Irr": 0.45, None: 0.63}

#: B-V by type, for rows with B and no V: OpenNGC medians where n >= 10, else 0
#: (nebular "magnitudes" carry no real colour information).
BV_TYPE = {
    "GCl": 0.72,
    "OCl": 0.37,
    "*Ass": 0.12,
    "Cl+N": 0.17,
    "PN": 0.80,
    # 0, like the other nebulae, not OpenNGC's median of 0.41: many "Neb"
    # magnitudes are a star's or a cluster's, and so is their median colour.
    # (IC 4605's B 4.70 less 0.41 came out brighter than its own star, 22 Sco.)
    "Neb": 0.0,
    "HII": 0.0,
    "EmN": 0.0,
    "RfN": 0.0,
    "SNR": 0.0,
}

#: V mean SB for an object with no usable magnitude (flagged estimated).
#: Nebulae: 22.5 sits between the big northern HII regions (NGC 7000 22.8,
#: IC 1396 23.1) and the bright ones (M42 22.0, M8 22.3); SNRs are fainter.
TYPICAL_SB = {
    "HII": 22.5,
    "EmN": 22.5,
    "Neb": 22.5,
    "Cl+N": 22.5,
    "RfN": 22.5,
    "SNR": 23.0,
    "OCl": 20.0,
    "GCl": 21.0,
    "PN": 20.0,
    "G": 22.5,
    "GPair": 22.5,
    "GTrpl": 22.5,
    "GGroup": 22.5,
    "*Ass": 22.5,
    "**": 22.5,
    "Other": 22.5,
    "DrkN": 22.5,
}

#: Sharpless brightness class (3 brightest) -> V mean SB, +-1 mag.
SH2_CLASS_SB = {3: 22.5, 2: 23.0, 1: 24.0}

#: Derived values outside these ranges are catalogue errors, not objects:
#: a nebula's "magnitude" is often that of one bright knot, or of a star. No
#: diffuse nebula whose magnitude really is nebular averages brighter than
#: about 21 over its catalogue ellipse (M17 21.1, M42 22.0, M8 22.3); a
#: supernova remnant can (the Crab, 20.8).
NEBULA_SB_RANGE = (21.0, 24.0)
SNR_SB_RANGE = (19.0, 24.0)
CLUSTER_SB_RANGE = (17.0, 23.5)
#: LEDA's mean SB may disagree with total magnitude over area by this much
#: before we distrust it (the Sculptor dwarf's is 5.8 mag too bright).
GALAXY_SB_TOLERANCE = 1.5
#: Median of LEDA SurfBr - (B + area term) over 10,262 galaxies: the light a
#: total magnitude includes from outside the D25 isophote.
GALAXY_ISOPHOTE_OFFSET = 0.41

#: Nebula rows whose catalogue magnitude is the nebula's own light, though
#: the rules in ``borrowed_magnitude`` would reject it: M42's V 4.0 is from
#: HEASARC's Messier table, not its cluster's.
NEBULAR_MAGNITUDE = {"NGC1976"}

#: An identifier that names a star. A nebula row that carries one is SIMBAD's
#: record for the star, and so is its magnitude: NGC 6888 resolves to WR 136
#: (B 7.49; OpenNGC B 7.44), IC 4605 to 22 Sco.
STELLAR_ID = re.compile(r"\b(?:HD|HIP|BD|TYC|CD|CPD|HR|SAO|WR)\s")

#: Nebula rows whose catalogue magnitude is a star's although nothing in the
#: row says so (no star identifier, no parallax), with whose it is.
STAR_MAGNITUDE: dict[str, str] = {
    # B 7.70 from SIMBAD, whose IC 1284 record also carries a star's spectral
    # type (B2); Cederblad 157d is "the nebula around BD -19 4953 = HD 167815"
    # (pg 7.56). Not retyped as reflection: no reflection catalogue lists it
    # (SIMBAD: GNe), and it is the HII region Sh2-37's position and size.
    "IC1284": "HD 167815's",
}

#: One magnitude for several rows: HyperLEDA gives B 7.00 to each of NGC 6960,
#: 6992 and 6995, whichever piece of the Cygnus Loop it is. It is no one
#: piece's light, so no piece spreads it or shows it.
SHARED_MAGNITUDE = {"NGC6960", "NGC6992", "NGC6995"}

#: OpenNGC's code for SIMBAD in its ``Sources`` column.
SOURCE_SIMBAD = "2"


def area_term(a: float, b: float) -> float:
    """Magnitudes to add to spread a total magnitude over an ellipse of full
    axes ``a`` x ``b`` arcmin, giving mag/arcsec^2."""
    return 2.5 * math.log10(math.pi / 4.0 * a * b) + ARCMIN2_TO_ARCSEC2


def hubble_class(h: str) -> str | None:
    h = h.strip().rstrip("?")
    if h in ("E", "E-S0") or re.fullmatch(r"E\d?", h):
        return "E"
    if h in ("S0", "S0-a"):
        return "S0"
    if h.startswith("I"):
        return "Sm/Irr"
    m = re.fullmatch(r"S(?:AB|B)?(a|ab|b|bc|c|cd|d|m)", h)
    if m is None:
        return None
    stage = m.group(1)
    if stage in ("a", "ab", "b"):
        return "Sa-Sb"
    if stage == "m":
        return "Sm/Irr"
    return "Sbc-Sd"


def galaxy_label(h: str, code: str) -> str:
    if code != "G":
        return TYPES[code][1]
    cls = hubble_class(h)
    return {
        "E": "Elliptical galaxy",
        "S0": "Lenticular galaxy",
        "Sa-Sb": "Spiral galaxy",
        "Sbc-Sd": "Spiral galaxy",
        "Sm/Irr": "Irregular galaxy",
    }.get(cls or "", "Galaxy")


def num(x: str | None) -> float | None:
    if x is None:
        return None
    x = x.strip()
    if not x:
        return None
    try:
        return float(x)
    except ValueError:
        return None


def v_effective(r: dict[str, str]) -> float | None:
    """The V magnitude the cuts use: galaxy V only when B-V is plausible,
    otherwise B less a typical colour."""
    t = r["Type"]
    if r["Name"] in V_OVERRIDES:
        return V_OVERRIDES[r["Name"]]
    b, v = num(r["B-Mag"]), num(r["V-Mag"])
    if t in GALAXY:
        if b is not None and v is not None and 0.3 <= b - v <= 1.2:
            return v
        if b is not None:
            return b - BV_HUBBLE[hubble_class(r["Hubble"])]
        return v
    if v is not None:
        return v
    if b is not None:
        return b - BV_TYPE.get(t, 0.0)
    return None


def sources(r: dict[str, str]) -> dict[str, str]:
    """OpenNGC's ``Sources`` column, ``"B-Mag:2|V-Mag:10"`` -> field -> source code."""
    out = {}
    for part in r.get("Sources", "").split("|"):
        field_name, _, code = part.partition(":")
        if code:
            out[field_name.strip()] = code.strip()
    return out


def borrowed_magnitude(r: dict[str, str], code: str) -> str | None:
    """Why a nebula row's catalogue magnitude is some other object's light,
    or None if it may be the nebula's own. Spreading a star's or a cluster's
    magnitude over a nebula's ellipse gives a surface brightness one to three
    magnitudes too bright, and the exposure estimate with it."""
    name = r["Name"]
    if name in V_OVERRIDES or name in NEBULAR_MAGNITUDE:
        return None
    if name in SHARED_MAGNITUDE:
        return "one magnitude shared by several rows"
    if name in STAR_MAGNITUDE:
        return STAR_MAGNITUDE[name]
    if code == "Cl+N":
        # OpenNGC's README: a Cl+N row's size is the cluster's (HEASARC mwsc)
        # and its magnitude SIMBAD's, for the cluster.
        return "the embedded cluster's"
    if code == "RfN":
        return "the illuminating star's"  # a reflection nebula is its star's light
    if STELLAR_ID.search(r["Identifiers"]):
        return "a star's (the row carries a star's identifiers)"
    src = sources(r)
    used = "V-Mag" if num(r["V-Mag"]) is not None else "B-Mag"
    if r["Pax"].strip() and src.get(used) == SOURCE_SIMBAD:
        # A parallax means SIMBAD's record for this name is a star or a
        # cluster, and the magnitude came with it: M16's V 6.0 is NGC 6611's.
        return "SIMBAD's star or cluster record"
    return None


def surface_brightness(
    r: dict[str, str], code: str, a: float | None, b: float | None
) -> tuple[float, str, bool]:
    """``(V mean SB mag/arcsec^2, basis, magnitude usable)``.

    Basis is ``leda`` (galaxies: HyperLEDA's mean B SB inside D25, less B-V),
    ``derived`` (magnitude spread over the catalogue ellipse), or ``typical``
    (no usable magnitude; a value for the type stands in, and the wire flags
    it estimated). The flag is False when a guard REJECTED the catalogue
    magnitude as describing something else -- a nebula's brightest knot, a
    star or the cluster inside it (``borrowed_magnitude``) -- so the data
    file does not display it either."""
    typical = TYPICAL_SB.get(code, 22.5)
    bmag, vmag, sbr = num(r["B-Mag"]), num(r["V-Mag"]), num(r["SurfBr"])
    if r["Name"] in V_OVERRIDES:
        bmag, vmag, sbr = None, V_OVERRIDES[r["Name"]], None

    if code in GALAXY:
        bv_own = bmag - vmag if bmag is not None and vmag is not None else None
        bv = (
            bv_own
            if bv_own is not None and 0.3 <= bv_own <= 1.2
            else BV_HUBBLE[hubble_class(r["Hubble"])]
        )
        naive_b = (
            bmag + area_term(a, b or a) + GALAXY_ISOPHOTE_OFFSET if bmag is not None and a else None
        )
        if sbr is not None:
            if naive_b is not None and abs(sbr - naive_b) > GALAXY_SB_TOLERANCE:
                return naive_b - bv, "derived", True
            return sbr - bv, "leda", True
        if naive_b is not None:
            return naive_b - bv, "derived", True
        if vmag is not None and a:
            return vmag + area_term(a, b or a) + GALAXY_ISOPHOTE_OFFSET, "derived", True
        return typical, "typical", True

    v = vmag if vmag is not None else (bmag - BV_TYPE.get(code, 0.0) if bmag is not None else None)
    if v is None:
        return typical, "typical", True
    # Before the size test: a star's magnitude is hidden even on a row with
    # no size to spread it over.
    if code in NEBULA and borrowed_magnitude(r, code) is not None:
        return typical, "typical", False
    if not a:
        return typical, "typical", True
    sb = v + area_term(a, b or a)
    lo, hi = SNR_SB_RANGE if code == "SNR" else NEBULA_SB_RANGE
    if code in NEBULA and not lo <= sb <= hi:
        return typical, "typical", False
    if code in ("OCl", "GCl") and not CLUSTER_SB_RANGE[0] <= sb <= CLUSTER_SB_RANGE[1]:
        return typical, "typical", False
    return sb, "derived", True


# --------------------------------------------------------------------------
# constellations
# --------------------------------------------------------------------------

CONSTELLATIONS = {
    "And": "Andromeda", "Ant": "Antlia", "Aps": "Apus", "Aqr": "Aquarius",
    "Aql": "Aquila", "Ara": "Ara", "Ari": "Aries", "Aur": "Auriga",
    "Boo": "Boötes", "Cae": "Caelum", "Cam": "Camelopardalis", "Cnc": "Cancer",
    "CVn": "Canes Venatici", "CMa": "Canis Major", "CMi": "Canis Minor",
    "Cap": "Capricornus", "Car": "Carina", "Cas": "Cassiopeia", "Cen": "Centaurus",
    "Cep": "Cepheus", "Cet": "Cetus", "Cha": "Chamaeleon", "Cir": "Circinus",
    "Col": "Columba", "Com": "Coma Berenices", "CrA": "Corona Australis",
    "CrB": "Corona Borealis", "Crv": "Corvus", "Crt": "Crater", "Cru": "Crux",
    "Cyg": "Cygnus", "Del": "Delphinus", "Dor": "Dorado", "Dra": "Draco",
    "Equ": "Equuleus", "Eri": "Eridanus", "For": "Fornax", "Gem": "Gemini",
    "Gru": "Grus", "Her": "Hercules", "Hor": "Horologium", "Hya": "Hydra",
    "Hyi": "Hydrus", "Ind": "Indus", "Lac": "Lacerta", "Leo": "Leo",
    "LMi": "Leo Minor", "Lep": "Lepus", "Lib": "Libra", "Lup": "Lupus",
    "Lyn": "Lynx", "Lyr": "Lyra", "Men": "Mensa", "Mic": "Microscopium",
    "Mon": "Monoceros", "Mus": "Musca", "Nor": "Norma", "Oct": "Octans",
    "Oph": "Ophiuchus", "Ori": "Orion", "Pav": "Pavo", "Peg": "Pegasus",
    "Per": "Perseus", "Phe": "Phoenix", "Pic": "Pictor", "Psc": "Pisces",
    "PsA": "Piscis Austrinus", "Pup": "Puppis", "Pyx": "Pyxis", "Ret": "Reticulum",
    "Sge": "Sagitta", "Sgr": "Sagittarius", "Sco": "Scorpius", "Scl": "Sculptor",
    "Sct": "Scutum", "Ser": "Serpens", "Se1": "Serpens", "Se2": "Serpens",
    "Sex": "Sextans", "Tau": "Taurus", "Tel": "Telescopium", "Tri": "Triangulum",
    "TrA": "Triangulum Australe", "Tuc": "Tucana", "UMa": "Ursa Major",
    "UMi": "Ursa Minor", "Vel": "Vela", "Vir": "Virgo", "Vol": "Volans",
    "Vul": "Vulpecula",
}  # fmt: skip


def constellations_of(ra_deg: list[float], dec_deg: list[float]) -> list[str]:
    """IAU constellation of each J2000 position (Delporte boundaries, via
    astropy -- the one non-stdlib step, and only for Sharpless regions)."""
    if not ra_deg:
        return []
    from astropy import units as u
    from astropy.coordinates import SkyCoord, get_constellation

    codes = get_constellation(SkyCoord(ra_deg * u.deg, dec_deg * u.deg), short_name=True)
    return [CONSTELLATIONS[str(c)] for c in codes]


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------

#: OpenNGC names rewritten for display: Bayer letters spelled out, casing.
NAME_FIXES = {
    "omi Per Cloud": "Omicron Persei Cloud",
    "omi Vel Cluster": "Omicron Velorum Cluster",
    "tet Car Cluster": "Theta Carinae Cluster",
    "lam Cen Nebula": "Lambda Centauri Nebula",
    "rho Oph Nebula": "Rho Ophiuchi Nebula",
    "eta Car Nebula": "Eta Carinae Nebula",
    "47 Tuc Cluster": "47 Tucanae",
    "S Nor Cluster": "S Normae Cluster",
    "Small Sgr Star Cloud": "Small Sagittarius Star Cloud",
    "chi Persei Cluster": "Chi Persei Cluster",
    "kappa Crucis Cluster": "Kappa Crucis Cluster",
    "omega Nebula": "Omega Nebula",
    "Bow-Tie nebula": "Bow-Tie Nebula",
    "h & chi Persei": "h and chi Persei",
    "Beehive": "Beehive Cluster",
    "Amas de l'Ecu de Sobieski": "",  # French for the Wild Duck's constellation; not a name
}


def clean_name(n: str) -> str:
    n = n.strip()
    n = NAME_FIXES.get(n, n)
    return re.sub(r"^the\s+", "", n, flags=re.IGNORECASE).strip()


@dataclass
class NameEdits:
    primary: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    alt: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    remove: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    tag: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def targets(self) -> set[str]:
        return set(self.primary) | set(self.alt) | set(self.remove) | set(self.tag)


def read_names(path: Path) -> NameEdits:
    edits = NameEdits()
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
    for row in csv.DictReader(lines):
        target, name, action = row["target"].strip(), row["name"].strip(), row["action"].strip()
        if action == "primary":
            edits.primary[target].append(name)
        elif action == "alt":
            edits.alt[target].append(name)
        elif action == "remove":
            edits.remove[target].add(name.casefold())
        elif action == "tag":
            edits.tag[target].append(name)
        else:
            raise SystemExit(f"names.csv: unknown action {action!r} for {target}")
    return edits


def apply_names(key: str, base: Iterable[str], edits: NameEdits) -> list[str]:
    names = [clean_name(n) for n in base]
    names = [n for n in names if n and n.casefold() not in edits.remove.get(key, set())]
    for n in edits.alt.get(key, []):
        names.append(n)
    for n in reversed(edits.primary.get(key, [])):
        names = [x for x in names if x.casefold() != n.casefold()]
        names.insert(0, n)
    out: list[str] = []
    for n in names:
        if n.casefold() not in {x.casefold() for x in out}:
            out.append(n)
    return out


# --------------------------------------------------------------------------
# designations
# --------------------------------------------------------------------------

#: Order in which designations are listed, most familiar first.
CATALOGUE_RANK = {"m": 0, "ngc": 1, "ic": 2, "c": 3, "sh2": 4, "mel": 5, "cr": 6}
#: Shown in ``designation`` (as is an object's own catalogue, whatever it
#: is); the rest -- PGC, UGC, LBN, MWSC, ... -- are search-only.
DISPLAYED = {"m", "ngc", "ic", "c", "sh2", "mel", "cr", "hcg", "h"}
#: Catalogues the frontend's shortName() recognises as a prefix before a
#: number; others are written without a space in a target's name.
SPACED_PREFIX = {"ngc", "ic", "sh2", "mel", "cr"}


def catalogue_of(key: str) -> str:
    m = re.match(r"[a-z]+(?:2(?=-))?", key)
    return m.group(0) if m else key


def display(key: str) -> str:
    """``ngc7000`` -> ``NGC 7000``; ``sh2-101`` -> ``Sh2-101``; ``m31`` -> ``M 31``."""
    cat = catalogue_of(key)
    rest = key[len(cat) :]
    if cat == "sh2":
        return f"Sh2{rest}"
    if cat in ("ngc", "ic") and "-ned" in rest:
        base, _, comp = rest.partition("-ned")
        return f"{cat.upper()} {base} NED{comp}"
    label = {
        "m": "M",
        "ngc": "NGC",
        "ic": "IC",
        "c": "C",
        "mel": "Mel",
        "cr": "Cr",
        "hcg": "HCG",
        "h": "Harvard",
        "eso": "ESO",
        "pgc": "PGC",
        "ugc": "UGC",
        "ugca": "UGCA",
        "mwsc": "MWSC",
        "lbn": "LBN",
        "ldn": "LDN",
        "b": "B",
    }.get(cat, cat.upper())
    return f"{label} {rest.upper()}"


def short(key: str) -> str:
    """The designation a target's NAME starts with: ``M31``, ``NGC 7000``,
    ``Sh2-101``, ``HCG92``."""
    cat = catalogue_of(key)
    d = display(key)
    if cat in SPACED_PREFIX:
        return d
    return d.replace(" ", "")


def key_rank(key: str) -> tuple[int, str, int, str]:
    cat = catalogue_of(key)
    m = re.search(r"(\d+)(.*)$", key[len(cat) :])
    n = int(m.group(1)) if m else 0
    return (CATALOGUE_RANK.get(cat, 9), cat, n, m.group(2) if m else "")


def id_rank(key: str) -> tuple[int, str, int, str]:
    """Row order in the data file: Messier, NGC, IC, Sharpless, the rest."""
    r = key_rank(key)
    order = {"m": 0, "ngc": 1, "ic": 2, "sh2": 3}.get(r[1], 4)
    return (order, r[1], r[2], r[3])


def openngc_key(name: str) -> str:
    key = designation_key(name)
    if key is None:
        raise SystemExit(f"cannot normalise OpenNGC name {name!r}")
    return key


def pointer_names(r: dict[str, str]) -> list[str]:
    out = []
    for col, prefix in (("NGC", "NGC"), ("IC", "IC")):
        for part in r[col].split(","):
            part = part.strip()
            if part:
                out.append(f"{prefix}{part.zfill(4) if part.isdigit() else part}")
    return out


IDENTIFIER_CATALOGUES = {
    "c", "sh2", "lbn", "ldn", "pgc", "ugc", "ugca", "mel", "cr", "vdb", "arp",
    "abell", "hcg", "eso", "ced", "b", "mwsc",
}  # fmt: skip


def pointer_keys(r: dict[str, str]) -> list[str]:
    """Keys for a row's NGC/IC cross-references. A few carry position
    suffixes ("1487NW") that name no catalogue entry; those are skipped."""
    keys = (designation_key(p) for p in pointer_names(r))
    return [k for k in keys if k is not None]


def identifier_keys(r: dict[str, str]) -> list[str]:
    out = []
    for token in r["Identifiers"].split(","):
        token = token.strip()
        if not token:
            continue
        key = designation_key(token)
        if key and catalogue_of(key) in IDENTIFIER_CATALOGUES:
            out.append(key)
    return out


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------


def fetch(url: str) -> str:
    print(f"fetching {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "tscheduler-build-catalog"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data: bytes = resp.read()
    return data.decode("utf-8")


def read_openngc(clone: Path | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for file in ("NGC.csv", "addendum.csv"):
        text = (
            (clone / "database_files" / file).read_text(encoding="utf-8")
            if clone
            else fetch(OPENNGC_URL.format(commit=OPENNGC_COMMIT, file=file))
        )
        rows.extend(csv.DictReader(io.StringIO(text), delimiter=";"))
    return rows


@dataclass(frozen=True)
class Sharpless:
    n: int
    ra: float
    dec: float
    diam: float
    bright: int


def read_sharpless(path: Path | None) -> list[Sharpless]:
    text = path.read_text(encoding="utf-8") if path else fetch(SHARPLESS_URL)
    out = []
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 7 or line.startswith("#") or not p[2].strip().isdigit():
            continue
        out.append(
            Sharpless(
                n=int(p[2]), ra=float(p[0]), dec=float(p[1]), diam=float(p[3]), bright=int(p[6])
            )
        )
    if len(out) != 313:
        raise SystemExit(f"expected 313 Sharpless regions, read {len(out)}")
    return out


def ra_deg(s: str) -> float:
    h, m, sec = s.split(":")
    return (int(h) + int(m) / 60.0 + float(sec) / 3600.0) * 15.0


def dec_deg(s: str) -> float:
    sign = -1.0 if s.startswith("-") else 1.0
    d, m, sec = s.lstrip("+-").split(":")
    return sign * (int(d) + int(m) / 60.0 + float(sec) / 3600.0)


def separation_arcmin(ra1: float, d1: float, ra2: float, d2: float) -> float:
    r = math.radians
    c = math.sin(r(d1)) * math.sin(r(d2)) + math.cos(r(d1)) * math.cos(r(d2)) * math.cos(
        r(ra1 - ra2)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 60.0


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def caldwell_numbers(r: dict[str, str]) -> list[int]:
    out = []
    m = re.fullmatch(r"C(\d{3})", r["Name"])
    if m:
        out.append(int(m.group(1)))
    for token in r["Identifiers"].split(","):
        m = re.fullmatch(r"C (\d{3})", token.strip())
        if m:
            out.append(int(m.group(1)))
    return out


def admitted_by(r: dict[str, str]) -> str | None:
    """Why a row is in the catalogue, or None. First rule that matches wins."""
    t, name = r["Type"], r["Name"]
    if t in ("Dup", "NonEx"):
        return None
    if r["M"].strip():
        return "messier"
    if caldwell_numbers(r):
        return "caldwell"
    if name in ALLOW or name in MERGE:
        return "curated"
    if t in ("*", "**", "Other", "Nova", "DrkN", "*Ass"):
        return None
    v, a = v_effective(r), num(r["MajAx"])
    if t in GALAXY:
        return "galaxy" if v is not None and v <= 12.0 and a is not None and a >= 2.0 else None
    if t == "PN":
        return "pn" if v is not None and v <= 13.0 and a is not None and a >= 0.25 else None
    if t == "GCl":
        return "gcl" if v is not None and v <= 11.0 else None
    if t == "OCl":
        bright = v is not None and v <= 9.0 and a is not None and a >= 3.0
        big = v is None and a is not None and a >= 15.0
        return "ocl" if bright or big else None
    if t in NEBULA:
        return "nebula" if (a is not None and a >= 3.0) or (v is not None and v <= 11.0) else None
    return None


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------


@dataclass
class Entry:
    source: str  # OpenNGC row name or "Sh2-nnn"
    id: str
    code: str
    ra: float
    dec: float
    constellation: str
    major: float | None
    minor: float | None
    v_mag: float | None
    sb: float
    sb_basis: str
    hubble: str = ""
    messier: int | None = None
    caldwell: int | None = None
    keys: list[str] = field(default_factory=list)  # every designation key, id first
    names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    why_not: str = ""
    label: str = ""


def build(
    openngc: list[dict[str, str]], sharpless: list[Sharpless], edits: NameEdits
) -> list[Entry]:
    by_name = {r["Name"]: r for r in openngc}
    selected = [r for r in openngc if admitted_by(r)]

    # ---- canonical ids, including OpenNGC's NED component rows -----------
    ids: dict[str, str] = {}
    for r in selected:
        name = r["Name"]
        if name in MERGE:
            continue
        if name in ID_OVERRIDES:
            ids[name] = ID_OVERRIDES[name]
        elif r["M"].strip():
            ids[name] = f"m{int(r['M'])}"
        else:
            ids[name] = openngc_key(name)
    # "NGC 4656 NED01" is the Hockey Stick: give it the plain id when nothing
    # else holds it, so ngc4656 means what everyone means by it.
    taken = set(ids.values())
    for name in sorted(ids):
        key = ids[name]
        m = re.fullmatch(r"(ngc|ic)(\d+)-ned\d\d", key)
        if m and f"{m.group(1)}{m.group(2)}" not in taken:
            ids[name] = f"{m.group(1)}{m.group(2)}"
            taken.add(ids[name])
    dupes = [k for k, v in Counter(ids.values()).items() if v > 1]
    if dupes:
        raise SystemExit(f"duplicate canonical ids: {dupes}")

    def survivor(name: str) -> str:
        return MERGE.get(name, name)

    # ---- entries ------------------------------------------------------------
    entries: dict[str, Entry] = {}
    for r in selected:
        name = r["Name"]
        if name in MERGE:
            continue
        code = TYPE_OVERRIDES.get(name, r["Type"])
        a, b = num(r["MajAx"]), num(r["MinAx"])
        if name in SIZE_OVERRIDES:
            a, b = SIZE_OVERRIDES[name]
        sb, basis, magnitude_ok = surface_brightness(r, code, a, b)
        v = v_effective(r) if magnitude_ok else None
        messier = r["M"].strip()
        cal = caldwell_numbers(r)
        ra, dec = POSITION_OVERRIDES.get(name, (ra_deg(r["RA"]), dec_deg(r["Dec"])))
        e = Entry(
            source=name,
            id=ids[name],
            code=code,
            ra=ra,
            dec=dec,
            constellation=CONSTELLATIONS[r["Const"]],
            major=a,
            minor=b if b is not None else None,
            v_mag=v,
            sb=sb,
            sb_basis=basis,
            hubble=r["Hubble"].strip(),
            messier=int(messier) if messier else None,
            caldwell=cal[0] if cal else None,
        )
        e.keys = [e.id, openngc_key(name)]
        e.keys += pointer_keys(r)
        e.keys += identifier_keys(r)
        e.keys += list(EXTRA_ALIASES.get(name, ()))
        e.names = [n for n in r["Common names"].split(",") if n.strip()]
        entries[name] = e

    # ---- merged rows ------------------------------------------------------
    for absorbed, kept in MERGE.items():
        r, e = by_name[absorbed], entries[kept]
        e.keys += [openngc_key(absorbed), *pointer_keys(r)]
        e.keys += identifier_keys(r)
        e.names += [n for n in r["Common names"].split(",") if n.strip()]
        if e.messier is None and r["M"].strip():
            e.messier = int(r["M"])
        cal = caldwell_numbers(r)
        if e.caldwell is None and cal:
            e.caldwell = cal[0]

    # ---- Dup rows -> aliases of their master -------------------------------
    for r in openngc:
        if r["Type"] != "Dup":
            continue
        masters = [p for p in pointer_names(r) if p in by_name and by_name[p]["Type"] != "Dup"]
        master = survivor(masters[0]) if masters else None
        if master in entries:
            entries[master].keys.append(openngc_key(r["Name"]))

    # ---- Sharpless ------------------------------------------------------------
    nebulae = [e for e in entries.values() if e.code in NEBULA]
    covered: dict[int, str] = {}
    for s in sharpless:
        if s.n in SH2_PAIRS:
            covered[s.n] = survivor(SH2_PAIRS[s.n])
            continue
        radius = max(0.35 * s.diam, 3.0)
        hits = sorted(
            (separation_arcmin(s.ra, s.dec, e.ra, e.dec), e.source)
            for e in nebulae
            if (e.major or 0.0) >= COMPARABLE * s.diam
            and separation_arcmin(s.ra, s.dec, e.ra, e.dec) <= radius
        )
        if s.n in SH2_WHOLE:
            # A row of its own; the check keeps the reason in SH2_WHOLE true.
            if not hits or hits[0][1] != SH2_WHOLE[s.n]:
                raise SystemExit(f"Sh2-{s.n} is no longer covered by {SH2_WHOLE[s.n]}")
            continue
        if hits:
            covered[s.n] = hits[0][1]
    for n, name in covered.items():
        entries[name].keys.append(f"sh2-{n}")

    new = [s for s in sharpless if s.n not in covered and s.diam >= 5.0]
    where = [POSITION_OVERRIDES.get(f"Sh2-{s.n}", (s.ra, s.dec)) for s in new]
    consts = constellations_of([p[0] for p in where], [p[1] for p in where])
    for s, (ra, dec), const in zip(new, where, consts, strict=True):
        src = f"Sh2-{s.n}"
        code = SH2_TYPES.get(s.n, "HII")
        a, b = SIZE_OVERRIDES.get(src, (s.diam, s.diam))
        whole = s.n in SH2_WHOLE
        entries[src] = Entry(
            source=src,
            id=f"sh2-{s.n}",
            code=code,
            ra=ra,
            dec=dec,
            constellation=const,
            major=a,
            minor=b,
            v_mag=None,
            sb=TYPICAL_SB[code] if whole else SH2_CLASS_SB[s.bright],
            sb_basis="typical" if whole else "sharpless",
            keys=[f"sh2-{s.n}"],
        )

    # ---- identifiers that name a larger whole -------------------------------
    for key, (holder, owner) in MOVED_ALIASES.items():
        if key not in entries[holder].keys:
            raise SystemExit(f"{holder} no longer carries {key}: revisit MOVED_ALIASES")
        entries[holder].keys = [k for k in entries[holder].keys if k != key]
        entries[owner].keys.append(key)

    # ---- names, tags, plannability, labels ----------------------------------
    unknown = sorted(t for t in edits.targets() if t not in entries)
    if unknown:
        raise SystemExit(f"names.csv names objects not in the catalogue: {unknown}")
    for src, e in entries.items():
        e.names = apply_names(src, e.names, edits)
        e.tags = list(dict.fromkeys(edits.tag.get(src, [])))
        e.why_not = NOT_PLANNABLE.get(src) or NOT_PLANNABLE_BY_TYPE.get(e.code, "")
        e.label = LABEL_OVERRIDES.get(src) or (
            galaxy_label(e.hubble, e.code) if e.code in GALAXY else TYPES[e.code][1]
        )
        if src.startswith("Sh2-") and e.code == "HII":
            e.label = "HII region"

    # ---- keys: unique per object, never another object's id ----------------
    canonical = {e.id for e in entries.values()}
    for e in entries.values():
        seen: list[str] = []
        for k in e.keys:
            if k not in seen and (k == e.id or k not in canonical):
                seen.append(k)
        e.keys = seen

    return sorted(entries.values(), key=lambda e: id_rank(e.id))


#: An Sh2 region counts as covered by an NGC/IC nebula only if the nebula is
#: at least this fraction of the region's diameter -- otherwise Barnard's Loop
#: (1200') would be swallowed by IC 420, a 6' knot inside it.
COMPARABLE = 0.3

#: Sharpless region -> the NGC/IC entry it IS, where position and size alone
#: pick wrongly (SIMBAD identifications).
SH2_PAIRS: dict[int, str] = {
    8: "NGC6334",  # Cat's Paw
    11: "NGC6357",  # War and Peace
    25: "NGC6523",  # M8, not the NGC 6530 cluster inside it
    45: "NGC6618",  # M17
    49: "NGC6611",  # M16
    131: "IC1396",
    273: "NGC2264",  # the Cone / Christmas Tree region
    275: "NGC2237",  # the Rosette Nebula, not the NGC 2239 cluster
    277: "NGC2024",  # the Flame region, not IC 434
    281: "NGC1976",  # M42
    311: "NGC2467",
}


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

COLUMNS = (
    "id",
    "designation",
    "short",
    "names",
    "aliases",
    "tags",
    "type",
    "type_label",
    "code",
    "constellation",
    "ra_deg",
    "dec_deg",
    "major_arcmin",
    "minor_arcmin",
    "v_mag",
    "sb",
    "sb_basis",
    "messier",
    "caldwell",
    "plannable",
    "why_not",
    "source",
)


def fmt(x: float | None, nd: int) -> str:
    return "" if x is None else f"{x:.{nd}f}"


def row_of(e: Entry) -> dict[str, str]:
    hidden = set(EXTRA_ALIASES.get(e.source, ()))
    shown = [
        k
        for k in e.keys
        if k != e.id and k not in hidden and catalogue_of(k) in DISPLAYED and "-ned" not in k
    ]
    if e.caldwell is not None:
        shown.append(f"c{e.caldwell}")
    # The object's own designation first, then the rest most familiar first.
    shown = [e.id, *sorted(set(shown) - {e.id}, key=key_rank)]
    designation = " · ".join(display(k) for k in shown)
    return {
        "id": e.id,
        "designation": designation,
        "short": short(e.id if e.messier is None else f"m{e.messier}"),
        "names": "|".join(e.names),
        "aliases": "|".join(k for k in e.keys if k != e.id),
        "tags": "|".join(e.tags),
        "type": TYPES[e.code][0],
        "type_label": e.label,
        "code": e.code,
        "constellation": e.constellation,
        "ra_deg": fmt(e.ra, 5),
        "dec_deg": fmt(e.dec, 5),
        "major_arcmin": fmt(e.major, 2),
        "minor_arcmin": fmt(e.minor, 2),
        "v_mag": fmt(e.v_mag, 2),
        "sb": fmt(e.sb, 2),
        "sb_basis": e.sb_basis,
        "messier": "" if e.messier is None else str(e.messier),
        "caldwell": "" if e.caldwell is None else str(e.caldwell),
        "plannable": "0" if e.why_not else "1",
        "why_not": e.why_not,
        "source": e.source,
    }


def render(entries: list[Entry]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for e in entries:
        w.writerow(row_of(e))
    return buf.getvalue()


def summary(entries: list[Entry]) -> str:
    by_type = Counter(TYPES[e.code][0] for e in entries)
    basis = Counter(e.sb_basis for e in entries)
    plannable = sum(1 for e in entries if not e.why_not)
    lines = [
        f"{len(entries)} objects ({plannable} plannable, {len(entries) - plannable} search-only)",
        "by type: " + ", ".join(f"{k} {v}" for k, v in by_type.most_common()),
        "surface brightness: " + ", ".join(f"{k} {v}" for k, v in basis.most_common()),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--openngc", type=Path, help="OpenNGC clone (holding database_files/)")
    ap.add_argument("--sharpless", type=Path, help="VizieR VII/20 TSV with _RAJ2000/_DEJ2000")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--check", action="store_true", help="exit 1 if --out is not up to date")
    args = ap.parse_args(argv)

    entries = build(read_openngc(args.openngc), read_sharpless(args.sharpless), read_names(NAMES))
    text = render(entries)
    print(summary(entries), file=sys.stderr)
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is out of date; rebuild it", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date", file=sys.stderr)
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
