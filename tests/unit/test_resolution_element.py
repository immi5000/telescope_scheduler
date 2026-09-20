"""The patch an extended target's SNR is quoted over must change smoothly with
the rig. It used to reuse the point-source aperture's ``n_pix >= 4`` floor,
which in a surface's SIGNAL area made time-to-goal drop ~2.8x the moment the
pixel scale crossed the star FWHM -- a 0.25% change of pixel size.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tscheduler.domain.equipment import Camera, Optics
from tscheduler.physics.quality import REFERENCE_SEEING_ARCSEC, resolution_element

RASA8 = Optics(aperture_mm=203.0, focal_length_mm=400.0, central_obstruction_mm=93.0)


def _camera(pixel_um: float) -> Camera:
    return Camera(pixel_size_um=pixel_um, sensor_width_px=4000, sensor_height_px=3000)


def _crossing_pixel_um(optics: Optics) -> float:
    """The pixel size whose scale equals the star FWHM on ``optics``."""
    fwhm = optics.psf_fwhm_arcsec(REFERENCE_SEEING_ARCSEC)
    return fwhm * optics.focal_length_mm * 1e-3 / 206_264.806 * 1e6


def test_the_element_is_continuous_where_pixels_outgrow_the_star() -> None:
    p = _crossing_pixel_um(RASA8)
    below = resolution_element(RASA8, _camera(p * 0.9975)).solid_angle_arcsec2
    above = resolution_element(RASA8, _camera(p * 1.0025)).solid_angle_arcsec2
    assert above / below == pytest.approx(1.0, abs=0.01)


def test_the_element_grows_with_the_pixel_once_undersampled() -> None:
    p = _crossing_pixel_um(RASA8)
    sizes = [resolution_element(RASA8, _camera(p * k)).solid_angle_arcsec2 for k in (1, 2, 3)]
    assert sizes[0] < sizes[1] < sizes[2]


def test_a_well_sampled_rig_barely_notices_the_pixel() -> None:
    c8 = Optics(aperture_mm=203.2, focal_length_mm=2032.0, central_obstruction_mm=64.0)
    fine = resolution_element(c8, _camera(2.0)).solid_angle_arcsec2
    coarse = resolution_element(c8, _camera(3.76)).solid_angle_arcsec2
    fwhm = c8.psf_fwhm_arcsec(REFERENCE_SEEING_ARCSEC)
    assert fine == pytest.approx(math.pi * (0.67 * fwhm) ** 2, rel=0.02)
    assert coarse / fine == pytest.approx(1.0, abs=0.03)


# --- point source vs extended source -----------------------------------------
# The distinction `Target.is_point_source` exists to carry. Confusing the two is
# the error `extended_snr2_rate`'s docstring documents -- reading a surface
# brightness as a total magnitude, or the reverse -- and it is worth a factor of
# the resolution element's solid angle in exposure time, in the OPTIMISTIC
# direction, which is the direction that silently ruins a night.
def test_a_point_source_needs_longer_than_the_same_number_read_as_a_surface() -> None:
    from tscheduler.api import equipment
    from tscheduler.physics.quality import efficiency, resolution_element

    eq = equipment.resolve("sct8-2600mm", None)
    el = resolution_element(eq.optics, eq.camera)
    kw = {
        "airmass": np.array([1.2]),
        "sky_mag_arcsec2": np.array([21.0]),
        "seeing_fwhm_arcsec": np.array([2.5]),
        "cloud_fraction": np.array([0.0]),
        "optics": eq.optics,
        "camera": eq.camera,
        "t_sub_s": 90.0,
    }
    for mag in (6.0, 10.0, 14.0):
        _, rho_ext = efficiency(target_magnitude=mag, point_source=False, **kw)
        _, rho_pt = efficiency(target_magnitude=mag, point_source=True, **kw)
        assert rho_pt < rho_ext, f"mag {mag}: a point source cannot accumulate faster"
        # The gap is the solid angle the extended reading multiplies in. Not
        # exactly it -- the point path also pays the encircled-energy fraction
        # and the two enter the noise differently -- so this is an order check.
        ratio = rho_ext / rho_pt
        assert 2.0 < ratio < 200.0, f"mag {mag}: ratio {ratio:.1f} is not a solid-angle factor"
    assert el.solid_angle_arcsec2 > 1.0


def test_the_default_is_extended_so_nothing_already_planned_changes_meaning() -> None:
    """`point_source` defaults to False, and `Target.is_point_source` to False,
    so all 1,850 catalogue objects and every planet keep the reading they had."""
    from tscheduler.api import equipment
    from tscheduler.domain.targets import Target
    from tscheduler.physics.quality import efficiency, extended_snr2_rate

    assert (
        Target(id="x", name="x", ra_deg=0.0, dec_deg=0.0, magnitude=22.0).is_point_source is False
    )

    eq = equipment.resolve("sct8-2600mm", None)
    a, sky = np.array([1.2]), np.array([21.0])
    _, rho = efficiency(
        target_magnitude=22.0,
        airmass=a,
        sky_mag_arcsec2=sky,
        seeing_fwhm_arcsec=np.array([2.5]),
        cloud_fraction=np.array([0.0]),
        optics=eq.optics,
        camera=eq.camera,
        t_sub_s=90.0,
    )
    direct = extended_snr2_rate(
        surface_brightness=22.0,
        airmass=1.0,
        sky_mag_arcsec2=21.5,
        optics=eq.optics,
        camera=eq.camera,
        t_sub_s=90.0,
    )
    assert rho == pytest.approx(float(direct), rel=1e-9)
