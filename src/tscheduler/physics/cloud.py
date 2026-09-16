"""Cloud.

**A forecast "40% cloud cover" is a sky-COVERAGE fraction, not an opacity.** It
says how much of the dome is covered, not how much light gets through. Treating
it as a grey attenuation of 2.5*f magnitudes is physics theatre.

The two-state model instead says: the line of sight is clear with probability
(1-f), or covered by cloud of optical depth ``a_thick`` magnitudes. The
consequence is worth stating loudly, because it changes how the scheduler should
think:

    With a_thick = 3.0 mag, transmission when covered is 0.063, and SNR^2
    accumulates as T^2 -> 0.004, i.e. effectively zero. So forecast cloud acts
    almost purely as a DUTY CYCLE factor (1-f) on effective exposure, NOT as a
    grey extinction. A 50%-cloud slot does not mean "expose twice as long"; it
    means "expect half your frames to be junk and sigma-clip them out".

Clouds also brighten the sky where there is light pollution to reflect, and
DARKEN a genuinely pristine sky by occulting airglow and starlight (Kyba et al.
2011 PLoS ONE 6 e17307 measured x10.1 in urban Berlin and x2.8 rural; Jechow et
al. 2018 found darkening at pristine sites).

Every constant here is a documented, tunable heuristic, not physics. Ship the
logging schema from night one and fit them from the site's own data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

#: Skyglow amplification when the line of sight is covered, by site character.
AMPLIFICATION: Final[dict[str, float]] = {
    "urban": 10.1,
    "suburban": 4.0,
    "rural": 2.8,
    "pristine": 0.3,
}


@dataclass(frozen=True, slots=True)
class TwoStateCloud:
    """Coverage-fraction cloud model. HEURISTIC -- see the module docstring."""

    a_thick_mag: float = 3.0
    amplification: float = 4.0
    natural_blocking: float = 0.3
    """Fraction of airglow/starlight still transmitted through a cloud deck."""
    risk_exponent: float = 1.0

    def duty_cycle(self, cloud_fraction: NDArray[np.float64] | float) -> NDArray[np.float64]:
        """Expected fraction of frames that are usable. The dominant effect."""
        f = np.clip(np.asarray(cloud_fraction, dtype=np.float64), 0.0, 1.0)
        return np.asarray(1.0 - f, dtype=np.float64)

    def expected_transmission(
        self, cloud_fraction: NDArray[np.float64] | float
    ) -> NDArray[np.float64]:
        f = np.clip(np.asarray(cloud_fraction, dtype=np.float64), 0.0, 1.0)
        return np.asarray((1.0 - f) + f * 10.0 ** (-0.4 * self.a_thick_mag), dtype=np.float64)

    def reliability(self, cloud_fraction: NDArray[np.float64] | float) -> NDArray[np.float64]:
        """Preference-side factor. Punishes VARIANCE, which the duty cycle (an
        unbiased expectation) deliberately does not."""
        f = np.clip(np.asarray(cloud_fraction, dtype=np.float64), 0.0, 1.0)
        return np.asarray((1.0 - f) ** self.risk_exponent, dtype=np.float64)

    def sky_delta_nl(
        self,
        cloud_fraction: NDArray[np.float64] | float,
        artificial_nl: NDArray[np.float64] | float,
        moon_nl: NDArray[np.float64] | float,
        natural_nl: NDArray[np.float64] | float,
    ) -> NDArray[np.float64]:
        """Net change in sky brightness from cloud, in nL.

        Legitimately NEGATIVE at a dark site, where cloud occults more airglow
        than it reflects. Do not clamp this to zero.
        """
        f = np.clip(np.asarray(cloud_fraction, dtype=np.float64), 0.0, 1.0)
        art = np.asarray(artificial_nl, dtype=np.float64)
        moon = np.asarray(moon_nl, dtype=np.float64)
        nat = np.asarray(natural_nl, dtype=np.float64)
        covered = self.amplification * (art + moon) + self.natural_blocking * nat
        clear = art + moon + nat
        return np.asarray(f * (covered - clear), dtype=np.float64)
