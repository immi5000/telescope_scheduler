# Vendored sky data

Everything in this directory was fetched by `scripts/build-sky-data.mjs` and is
redistributed under the terms below. Both notices must travel with the files.

## stars.bin, starnames.json, constellations.bin, constellations.json

41,411 stars to visual magnitude 8.0 from the Hipparcos catalogue, their
proper names and Bayer/Flamsteed designations, plus the 89 IAU constellation
line figures and their names, as distributed in the `stars.8.json`,
`starnames.json`, `constellations.lines.json` and `constellations.json`
data files of **d3-celestial** by Olaf Frohn.

Source: https://github.com/ofrohn/d3-celestial

    Copyright (c) 2015-2022, Olaf Frohn
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the name of the copyright holder nor the names of its
      contributors may be used to endorse or promote products derived from this
      software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
    AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
    ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
    LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
    CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.

## milkyway.webp -- diffuse all-sky background

"Deep Star Maps 2020", the Milky Way layer, equirectangular in equatorial
(ICRS) coordinates, downscaled from the 32768x16384 master.

Source: https://svs.gsfc.nasa.gov/4851/
Obtained via Wikimedia Commons, because svs.gsfc.nasa.gov serves
`Access-Control-Allow-Origin` for a single unrelated origin.

    NASA/Goddard Space Flight Center Scientific Visualization Studio
    (Ernie Wright, Laurence Schuler, Ian Jones).
    Gaia DR2: ESA/Gaia/DPAC.

This file ships with the built app at `/sky/LICENSES.md`, which is how the
notice travels with the data it describes. The sky view itself no longer
prints a credit line over the sky; the selected-object card carries the
provenance that changes (which orbital elements, published when).
