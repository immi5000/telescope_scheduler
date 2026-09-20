"""The deep-sky catalogue: integrity, resolution, and the numbers behind it.

The catalogue is data, so most of these tests pin facts rather than code
paths: every Messier and Caldwell number resolves, the ids the app has always
used still resolve, the Flame is NGC 2024, and the surface brightnesses land
where the build's own derivation says they should.
"""

from __future__ import annotations

import math
import time
from importlib import resources

import pytest

from tscheduler import catalog
from tscheduler.api import presets
from tscheduler.catalog import _parse, by_id, resolve
from tscheduler.catalog.normalise import designation_key, name_key

TYPES = {
    "galaxy",
    "emission-nebula",
    "reflection-nebula",
    "planetary-nebula",
    "supernova-remnant",
    "open-cluster",
    "globular-cluster",
    "cluster-nebula",
    "other",
}


def _id(query: str) -> str | None:
    o = resolve(query)
    return o.id if o else None


# --------------------------------------------------------------------------
# integrity
# --------------------------------------------------------------------------


def test_ids_are_unique_and_the_size_is_what_the_build_intends() -> None:
    objs = catalog.objects()
    ids = [o.id for o in objs]
    assert len(ids) == len(set(ids))
    assert 1700 <= len(objs) <= 2000
    assert presets.catalog_size() == len(objs)


def test_every_field_is_in_its_vocabulary() -> None:
    for o in catalog.objects():
        assert o.type in TYPES, o.id
        assert o.id == o.id.lower() and " " not in o.id, o.id
        assert 0.0 <= o.ra_deg < 360.0 and -90.0 <= o.dec_deg <= 90.0, o.id
        assert o.constellation, o.id
        assert math.isfinite(o.surface_brightness), o.id
        assert o.sb_basis in {"leda", "derived", "typical", "sharpless"}, o.id
        assert o.magnitude_estimated == (o.sb_basis in {"typical", "sharpless"}), o.id
        assert o.designation.startswith(o.designation.split(" · ")[0]), o.id
        # A target's name is cut from the front by the timeline; the
        # designation must lead it.
        assert o.target_name.startswith(o.short), o.id
        if o.major_arcmin is not None:
            assert o.major_arcmin > 0 and (o.minor_arcmin or o.major_arcmin) > 0, o.id


def test_plannable_and_why_not_agree() -> None:
    for o in catalog.objects():
        assert o.plannable == (o.why_not is None), o.id


def test_the_things_the_calculator_cannot_price_are_search_only() -> None:
    expected = {
        "m40": "double star",
        "m73": "asterism",
        "c14": "two clusters",
        "c99": "dark nebula",
        "mel25": "naked-eye",
        "mel111": "naked-eye",
        "cr399": "asterism",
    }
    for oid, words in expected.items():
        o = by_id(oid)
        assert o is not None, oid
        assert not o.plannable, oid
        assert o.why_not is not None and words in o.why_not, (oid, o.why_not)
    assert {o.id for o in catalog.objects() if not o.plannable} == set(expected)


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n", range(1, 111))
def test_every_messier_number_resolves(n: int) -> None:
    o = resolve(f"M{n}")
    assert o is not None, n
    if n == 102:
        # OpenNGC calls M102 a duplicate of M101; imagers mean the Spindle.
        assert o.id == "ngc5866"
    else:
        assert o.id == f"m{n}"
        assert o.messier == n


@pytest.mark.parametrize("n", range(1, 110))
def test_every_caldwell_number_resolves(n: int) -> None:
    o = resolve(f"Caldwell {n}")
    assert o is not None, n
    assert o.caldwell == n, (n, o.id)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("C9", "sh2-155"),
        ("C14", "c14"),
        ("C20", "ngc7000"),
        ("C37", "ngc6882"),
        ("C41", "mel25"),
        ("C49", "ngc2237"),
        ("C50", "ngc2239"),
        ("C99", "c99"),
    ],
)
def test_caldwell_numbers_land_on_the_right_object(query: str, expected: str) -> None:
    assert _id(query) == expected


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("n7000", "ngc7000"),
        ("n6946", "ngc6946"),
        ("n7331", "ngc7331"),
        ("n891", "ngc891"),
        ("ic1396", "ic1396"),
        ("m31", "m31"),
    ],
)
def test_the_ids_the_app_has_always_used_still_resolve(legacy: str, canonical: str) -> None:
    assert _id(legacy) == canonical


@pytest.mark.parametrize(
    ("query", "key"),
    [
        ("NGC 7000", "ngc7000"),
        ("ngc7000", "ngc7000"),
        ("n7000", "ngc7000"),
        ("N 7000", "ngc7000"),
        ("NGC7000", "ngc7000"),
        ("M031", "m31"),
        ("Messier 31", "m31"),
        ("m 31", "m31"),
        ("IC 1396A", "ic1396a"),
        ("Caldwell 20", "c20"),
        ("C20", "c20"),
        ("C 020", "c20"),
        ("Sh2-101", "sh2-101"),
        ("Sh 2-101", "sh2-101"),
        ("sh2 101", "sh2-101"),
        ("Sharpless 101", "sh2-101"),
        ("Sh2\u2013155", "sh2-155"),
        # The Sharpless "2" is the catalogue only when a separator follows it.
        ("Sharpless 240", "sh2-240"),
        ("Sh2-240", "sh2-240"),
        ("sh2 240", "sh2-240"),
        ("Sharpless 22", "sh2-22"),
        ("sh 240", "sh2-240"),
        ("Sharpless 2-240", "sh2-240"),
        ("Sharpless 201", "sh2-201"),
        ("Mel 22", "mel22"),
        ("Collinder 399", "cr399"),
        ("B33", "b33"),
        ("Barnard 33", "b33"),
        ("HCG 92", "hcg92"),
        ("PGC 143", "pgc143"),
        ("ESO 056-115", "eso56-115"),
        ("NGC 4656 NED01", "ngc4656-ned01"),
        ("M 111", None),
        ("c110", None),
        ("Sh2-314", None),
        ("Andromeda", None),
    ],
)
def test_designations_normalise(query: str, key: str | None) -> None:
    assert designation_key(query) == key


def test_common_names_normalise() -> None:
    assert name_key("The Running Man Nebula") == "running man"
    assert name_key("Bode's Galaxy") == "bodes"
    assert name_key("Herschel's Jewel Box") == "herschels jewel box"
    assert name_key("Butterfly Nebula", generic=True) == "butterfly nebula"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Dark nebulae are planned through the emission behind them.
        ("Horsehead", "ic434"),
        ("Horsehead Nebula", "ic434"),
        ("B33", "ic434"),
        ("Cone Nebula", "ngc2264"),
        # OpenNGC names IC 434 the Flame; the Flame is NGC 2024.
        ("Flame Nebula", "ngc2024"),
        ("Rosette Nebula", "ngc2237"),
        ("NGC 2238", "ngc2237"),
        ("NGC 2244", "ngc2239"),
        ("IC 2118", "ngc1909"),
        ("NGC 6885", "ngc6882"),
        ("IC 4703", "m16"),
        ("Sh2-117", "ngc7000"),
        ("Sh2-131", "ic1396"),
        ("Sh2-275", "ngc2237"),
        ("Sh2-277", "ngc2024"),
        # Huge regions are not swallowed by a small nebula inside them.
        ("Sh2-276", "sh2-276"),
        ("Barnard's Loop", "sh2-276"),
        ("Sh2-109", "sh2-109"),
        ("Spindle Galaxy", "ngc5866"),
        ("Bode's Galaxy", "m81"),
        ("bodes", "m81"),
        ("The Running Man Nebula", "ngc1977"),
        ("Hockey Stick", "ngc4656"),
        ("NGC 4656", "ngc4656"),
        ("Double Cluster", "c14"),
        ("Pleiades", "m45"),
        ("Tulip", "sh2-101"),
        ("PGC 2557", "m31"),
        ("LBN 373", "ngc7000"),
        # Ambiguous without the kind word; exact with it.
        ("Owl", None),
        ("Owl Nebula", "m97"),
        ("Butterfly Nebula", "ngc6302"),
        ("Butterfly Cluster", "m6"),
        # Groups are searchable but name no single object.
        ("Leo Triplet", None),
        ("unknown-thing", None),
        ("", None),
    ],
)
def test_resolve(query: str, expected: str | None) -> None:
    assert _id(query) == expected


def test_search_text_carries_every_spelling_a_client_needs() -> None:
    m31 = by_id("m31")
    assert m31 is not None
    for part in ("m31", "m 31", "ngc224", "ngc 224", "andromeda galaxy", "spiral galaxy"):
        assert part in m31.search_text, part
    n7000 = by_id("ngc7000")
    assert n7000 is not None
    for part in ("ngc7000", "ngc 7000", "c20", "caldwell 20", "sh2-117", "sh2 117"):
        assert part in n7000.search_text, part
    for part in ("north america nebula", "emission nebula", "cygnus"):
        assert part in n7000.search_text, part
    for o in (by_id("m65"), by_id("m66"), by_id("ngc3628")):
        assert o is not None and "leo triplet" in o.search_text
    for o in catalog.objects():
        assert o.search_text == o.search_text.lower(), o.id
        assert o.id in o.search_text, o.id


# --------------------------------------------------------------------------
# the numbers
# --------------------------------------------------------------------------

#: The eleven legacy targets the derivation was checked against by hand (V
#: mean SB over the catalogue ellipse); IC 1396 has no catalogue magnitude.
LEGACY_SB = {
    "m31": 22.78,
    "m27": 20.16,
    "m57": 17.95,
    "m13": 20.52,
    "m33": 23.05,
    "m81": 21.91,
    "m101": 23.51,
    "ngc7000": 22.83,
    "ngc6946": 23.02,
    "ngc7331": 22.29,
    "ngc891": 23.34,
    "ic1396": 22.5,
}


@pytest.mark.parametrize(("oid", "sb"), LEGACY_SB.items())
def test_legacy_targets_keep_their_derived_surface_brightness(oid: str, sb: float) -> None:
    o = by_id(oid)
    assert o is not None
    assert abs(o.surface_brightness - sb) <= 0.3, (oid, o.surface_brightness)


def test_ic1396_is_the_nebula_not_the_cluster() -> None:
    o = by_id("ic1396")
    assert o is not None
    assert o.major_arcmin == 170.0
    assert o.magnitude_estimated


#: V mean mag/arcsec^2 by type. Wide on purpose: they catch a unit error or a
#: sign error (a 5-mag slip), not a disagreement over a tenth. The diffuse
#: nebulae start at 21: brighter than that, the "magnitude" behind the number
#: was a star's or a cluster's (the Iris came out at 20.2, the Pleiades 20.7).
SB_RANGE = {
    "galaxy": (18.0, 28.5),
    "planetary-nebula": (12.0, 24.5),
    "globular-cluster": (17.0, 23.5),
    "open-cluster": (16.5, 23.5),
    "emission-nebula": (21.0, 24.5),
    "reflection-nebula": (21.0, 24.5),
    "supernova-remnant": (19.0, 24.5),
    "cluster-nebula": (21.0, 24.5),
    "other": (19.0, 24.5),
}


def test_surface_brightness_is_plausible_for_each_type() -> None:
    for o in catalog.objects():
        lo, hi = SB_RANGE[o.type]
        assert lo <= o.surface_brightness <= hi, (o.id, o.type, o.surface_brightness)


@pytest.mark.parametrize(
    ("oid", "whose"),
    [
        ("ngc7023", "the illuminating star's (HD 200775), a reflection nebula"),
        ("ic5146", "the Cocoon's cluster's (Cl+N)"),
        ("m45", "the Pleiades stars' (V 1.2)"),
        ("ngc6888", "WR 136's: SIMBAD resolves NGC 6888 to the star"),
        ("ic4605", "22 Sco's (HD 148605)"),
        ("m16", "the NGC 6611 cluster's (SIMBAD, with its parallax)"),
        ("ngc6960", "the whole Cygnus Loop's, shared by three rows"),
        ("ngc6992", "the whole Cygnus Loop's, shared by three rows"),
        ("ngc6995", "the whole Cygnus Loop's, shared by three rows"),
        # SIMBAD's B for these is Cederblad's (1946) photographic magnitude of
        # the illuminating star, and nothing in the OpenNGC row says so.
        ("ngc6589", "a star's (Cederblad 157a), a reflection nebula"),
        ("ngc2282", "BD +1 1503's (Cederblad 87, pg 10.10), a reflection nebula"),
        ("ic447", "its cluster's (Cederblad 78, pg 7.70), a reflection nebula"),
        ("ic1284", "HD 167815's (Cederblad 157d; SIMBAD's record has its B2 spectrum)"),
    ],
)
def test_a_nebula_is_not_priced_with_another_objects_magnitude(oid: str, whose: str) -> None:
    """The catalogue magnitude is ``whose``: spread over the nebula's ellipse
    it made the nebula one to three magnitudes too bright. It is neither used
    nor shown."""
    o = by_id(oid)
    assert o is not None
    assert o.magnitude_estimated, (oid, whose, o.sb_basis, o.surface_brightness)
    assert o.v_mag is None, (oid, whose, o.v_mag)


def test_a_nebula_is_never_shown_brighter_than_its_own_star() -> None:
    # IC 4605 once showed "V 4.3": OpenNGC's B 4.70 (22 Sco, V 4.79) less a
    # typical nebular B-V.
    o = by_id("ic4605")
    assert o is not None
    assert o.v_mag is None


@pytest.mark.parametrize(
    ("oid", "sb"),
    [
        # Magnitudes that really are the nebula's keep their derived value.
        ("m42", 21.96),
        ("m8", 22.25),
        ("m17", 21.13),
        ("ngc7000", 22.83),
        ("ngc2237", 23.83),
        ("m1", 20.79),  # a supernova remnant may be brighter than 21
        ("m78", 21.13),  # V 8.3 over 8'x6' (SEDS), not over the cluster's 4.5'
    ],
)
def test_nebulae_with_their_own_magnitude_keep_a_derived_surface_brightness(
    oid: str, sb: float
) -> None:
    o = by_id(oid)
    assert o is not None
    assert o.sb_basis == "derived", (oid, o.sb_basis)
    assert o.surface_brightness == pytest.approx(sb, abs=0.01)


# --------------------------------------------------------------------------
# corrections to the sources
# --------------------------------------------------------------------------

#: The Cygnus Loop and its named pieces, all tagged "Veil Nebula".
VEIL = {"sh2-103", "ngc6960", "ngc6992", "ngc6995", "ngc6979"}


def test_the_western_veil_is_the_filament_not_the_whole_cygnus_loop() -> None:
    o = by_id("ngc6960")
    assert o is not None
    assert o.caldwell == 34
    assert o.names[0] == "Western Veil"
    assert "Veil Nebula" not in o.names and "Veil Nebula" in o.tags
    # SEDS, about 70'x6': one frame on an 80 mm refractor, not a mosaic.
    assert (o.major_arcmin, o.minor_arcmin) == (70.0, 6.0)
    assert _id("Western Veil") == "ngc6960"
    assert _id("Witch's Broom") == "ngc6960"
    # The whole Loop's designations are the Loop's row's, not the filament's.
    assert not {"sh2-103", "lbn191"} & set(o.aliases), o.aliases
    assert o.designation == "NGC 6960 · C 34"


def test_the_cygnus_loop_is_a_row_of_its_own() -> None:
    o = by_id("sh2-103")
    assert o is not None
    assert (o.type, o.code, o.type_label) == ("supernova-remnant", "SNR", "Supernova remnant")
    # Green's catalogue, G74.0-8.5 (SIMBAD "Cyg Loop", 20 51 00 +30 40 00).
    assert (o.major_arcmin, o.minor_arcmin) == (230.0, 160.0)
    assert (o.ra_deg, o.dec_deg) == pytest.approx((312.75, 30.66667), abs=1e-5)
    assert o.constellation == "Cygnus"
    assert o.names == ("Cygnus Loop",) and o.tags == ("Veil Nebula",)
    assert o.target_name == "Sh2-103 Cygnus Loop"
    # No magnitude of its own: the typical supernova remnant, flagged estimated.
    assert (o.surface_brightness, o.sb_basis, o.v_mag) == (23.0, "typical", None)
    assert o.plannable
    for query in ("Cygnus Loop", "Sh2-103", "Sharpless 103", "LBN 191"):
        assert _id(query) == "sh2-103", query
    # NGC 6979 is Pickering's Triangle, a piece with a row of its own.
    assert "ngc6979" not in o.aliases
    assert _id("NGC 6979") == _id("Pickering's Triangle") == "ngc6979"


def test_the_two_pieces_of_the_eastern_veil_are_told_apart() -> None:
    east, south = by_id("ngc6992"), by_id("ngc6995")
    assert east is not None and south is not None
    assert east.names[0] == "Eastern Veil" and south.names[0] != east.names[0]
    assert _id("Eastern Veil") == "ngc6992"  # C33
    # One whole-Loop magnitude, not spread over each piece: the same estimate.
    assert east.surface_brightness == south.surface_brightness


def test_the_veil_nebula_is_found_by_search_and_names_its_pieces() -> None:
    # The picker's search is a substring test on search_text.
    found = {o.id for o in catalog.objects() if "veil" in o.search_text}
    assert found >= VEIL
    assert {o.id for o in catalog.objects() if "veil nebula" in o.search_text} >= VEIL
    # SIMBAD gives the name to NGC 6960, Wikipedia to the whole Loop, and
    # imagers to either: resolving it to one row would be a guess.
    assert resolve("Veil Nebula") is None
    for query in ("Veil Nebula", "Veil"):
        assert {o.id for o in catalog.ambiguous(query)} == VEIL, query
        msg = catalog.refusal(query)
        assert msg is not None and "Western Veil (NGC 6960)" in msg, msg
        assert "Cygnus Loop (Sh2-103)" in msg, msg


def test_the_rosette_is_centred_on_its_ring() -> None:
    rosette, cluster = by_id("ngc2237"), by_id("ngc2239")  # NGC 2244 is the ring's cluster
    assert rosette is not None and cluster is not None
    assert (rosette.ra_deg, rosette.dec_deg) == pytest.approx((97.98150, 4.94294), abs=1e-5)
    assert (rosette.ra_deg, rosette.dec_deg) == (cluster.ra_deg, cluster.dec_deg)
    assert rosette.constellation == "Monoceros"


@pytest.mark.parametrize(
    "oid",
    [
        "ngc7023",
        "ngc1432",
        "ngc1435",
        "ic4604",
        "ic4605",
        "ngc1975",
        "ngc6729",
        "m45",
        "ngc6589",
        "ngc2282",
        "ic447",
    ],
)
def test_reflection_nebulae_are_typed_as_reflection(oid: str) -> None:
    o = by_id(oid)
    assert o is not None
    assert (o.type, o.code) == ("reflection-nebula", "RfN"), (oid, o.type, o.code)


def test_ic1284_loses_its_stars_magnitude_but_keeps_its_type() -> None:
    """No reflection catalogue lists IC 1284, and it carries Sh2-37: still a
    nebula, but its B 7.70 is HD 167815's, so it is priced as typical."""
    o = by_id("ic1284")
    assert o is not None
    assert (o.type, o.code) == ("emission-nebula", "Neb")
    assert "sh2-37" in o.aliases
    assert (o.surface_brightness, o.sb_basis, o.v_mag) == (22.5, "typical", None)


def test_the_pleiades_keep_their_label() -> None:
    o = by_id("m45")
    assert o is not None
    assert o.type_label == "Open cluster with reflection nebula"
    assert "open cluster with reflection nebula" in o.search_text


def test_bright_showpieces_are_brighter_than_faint_ones() -> None:
    def sb(oid: str) -> float:
        o = by_id(oid)
        assert o is not None
        return o.surface_brightness

    # A compact planetary is far brighter per arcsec^2 than a big galaxy disk.
    assert sb("m57") < sb("m27") < sb("m31") < sb("m101")


# --------------------------------------------------------------------------
# presets on top of it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "tid", "name"),
    [
        ("m31", "m31", "M31 Andromeda Galaxy"),
        ("n7000", "ngc7000", "NGC 7000 North America Nebula"),
        ("NGC 7000", "ngc7000", "NGC 7000 North America Nebula"),
        ("sh2-101", "sh2-101", "Sh2-101 Tulip Nebula"),
        ("ngc7331", "ngc7331", "NGC 7331"),
        ("Horsehead", "ic434", "IC 434 Horsehead Nebula"),
        ("hcg92", "hcg92", "HCG92 Stephan's Quintet"),
    ],
)
def test_catalog_target_builds_a_schedulable_target(query: str, tid: str, name: str) -> None:
    t = presets.catalog_target(query, priority=2.0, snr_goal=30.0)
    assert t is not None
    assert (t.id, t.name) == (tid, name)
    assert t.priority == 2.0 and t.snr_goal == 30.0
    o = by_id(tid)
    assert o is not None
    assert t.magnitude == o.surface_brightness
    assert (t.ra_deg, t.dec_deg) == (o.ra_deg, o.dec_deg)


@pytest.mark.parametrize("query", ["m40", "m73", "C14", "Double Cluster", "c99", "Hyades", "nope"])
def test_catalog_target_refuses_what_cannot_be_planned(query: str) -> None:
    assert presets.catalog_target(query) is None


def test_refusal_says_why() -> None:
    assert catalog.refusal("m31") is None
    msg = catalog.refusal("C14")
    assert msg is not None and "NGC 869" in msg and "NGC 884" in msg
    assert catalog.refusal("nope") == "'nope' is not in the catalogue"


def test_a_name_two_objects_share_is_refused_as_ambiguous_not_absent() -> None:
    assert catalog.refusal("Helix") == (
        "'Helix' is ambiguous: did you mean Helix Nebula (NGC 7293) or Helix Galaxy (NGC 2685)?"
    )
    assert [o.id for o in catalog.ambiguous("Helix")] == ["ngc7293", "ngc2685"]
    assert {o.id for o in catalog.ambiguous("Owl")} == {"m97", "ngc457"}
    msg = catalog.refusal("Owl")
    assert msg is not None and "Owl Nebula (M97)" in msg and "Owl Cluster (NGC 457)" in msg
    # With the kind word it is one object, and nothing is ambiguous.
    assert _id("Helix Nebula") == "ngc7293"
    assert catalog.ambiguous("Helix Nebula") == []
    # Nothing to choose from: absent, not ambiguous.
    for query in ("nope", "M 111", "Sh2-314", ""):
        assert catalog.ambiguous(query) == [], query


def test_a_group_is_refused_with_its_members() -> None:
    assert [o.id for o in catalog.ambiguous("Leo Triplet")] == ["m65", "m66", "ngc3628"]
    msg = catalog.refusal("Leo Triplet")
    assert msg is not None and msg.startswith("'Leo Triplet' is ambiguous: did you mean M65")
    # A group with one member here is not "ambiguous", but still not one object.
    assert [o.id for o in catalog.ambiguous("Deer Lick Group")] == ["ngc7331"]
    assert catalog.refusal("Deer Lick Group") == (
        "'Deer Lick Group' is a group, not one object: did you mean NGC 7331?"
    )


def test_default_targets_are_plannable_under_their_own_ids() -> None:
    assert len(presets.DEFAULT_TARGET_IDS) == 7
    for tid in presets.DEFAULT_TARGET_IDS:
        t = presets.catalog_target(tid)
        assert t is not None and t.id == tid, tid


def test_attribution_names_both_sources_and_the_licence() -> None:
    a = presets.CATALOG_ATTRIBUTION
    assert a == catalog.ATTRIBUTION
    # CC BY-SA 4.0 s3(a)(1): the creator, the licence, and that it was modified.
    for words in ("OpenNGC", "Mattia Verga", "CC BY-SA 4.0", "modified", "Sharpless"):
        assert words in a, words
    assert catalog.LICENSE_URL == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert catalog.NOTICE_URL == "/api/catalog/notice"
    notice = catalog.notice_text()
    assert notice == resources.files("tscheduler.catalog").joinpath("NOTICE").read_text("utf-8")
    for words in ("Mattia Verga", "creativecommons.org/licenses/by-sa/4.0", "MODIFIED", "VizieR"):
        assert words in notice, words


def test_a_cold_load_is_fast() -> None:
    text = resources.files("tscheduler.catalog").joinpath("dso.csv").read_text("utf-8")
    t0 = time.perf_counter()
    cat = _parse(text)
    elapsed = time.perf_counter() - t0
    assert len(cat.objects) == len(catalog.objects())
    # ~20 ms on a laptop; the bound leaves room for a loaded CI runner.
    assert elapsed < 0.25, f"parsing the catalogue took {elapsed * 1000:.0f} ms"


@pytest.mark.network
@pytest.mark.slow
def test_the_data_file_is_what_the_build_script_makes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebuild from the pinned upstream sources and compare byte for byte."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "build_catalog.py"
    spec = importlib.util.spec_from_file_location("build_catalog", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "build_catalog", mod)  # dataclasses look it up
    spec.loader.exec_module(mod)
    assert mod.main(["--check"]) == 0
