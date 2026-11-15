# FOV audit: RA 12°, Dec 75°

These are the archived responses from the live Codespace before the zoom fix. All three requests used detector D4, a 6″ output pixel scale, and independently searched and selected exposures. The product ID below is the exact FITS basename from each frame's `source_url`; UTC timestamps and wavelengths are from the manifests. Wavelength is the value at the requested target, not a single fixed wavelength for the detector.

| FOV | Saved sequence | Frames | Output pixels | Requested sky footprint corners, RA/Dec in degrees | Display vmin/vmax (MJy/sr) |
| --- | --- | ---: | --- | --- | --- |
| 20′ | `00704cd9da3b400ab07250dd0f3d805c` | 4 | 200 × 200 | (12.6338578, 74.8332818); (12.6476998, 75.1649268); (11.3523002, 75.1649268); (11.3661422, 74.8332818) | 0.07927958250045776 / 4.4194103050230975 |
| 10′ | `7769e70d4a5b4e048e1cafc528b6ad2e` | 1 | 100 × 100 | (12.3170485, 74.9172797); (12.3204744, 75.082277); (11.6795256, 75.082277); (11.6829515, 74.9172797) | 0.0930284571647644 / 46.83575126647961 |
| 5′ | `c201453042b247e1b4546cdd329f9da0` | 7 | 50 × 50 | (12.1573490, 74.9591125); (12.1581882, 75.0407789); (11.8418118, 75.0407789); (11.8426510, 74.9591125) | 0.07867612794041634 / 1.3339082741737416 |

All frames below have detector `SPHEREx-D4`, band `D4`, the output dimensions and rectangular WCS sky footprint in their sequence row above, and an original no-data mask. Coverage is the fraction of unmasked output pixels. The rectangular WCS footprint is **not** a claim that every pixel inside it was observed. These values are a snapshot of particular saved searches, and a fresh archive search could produce another selection.

| FOV | Product ID (FITS basename) | Observation UTC | Wavelength at target (µm) | Valid coverage |
| --- | --- | --- | ---: | ---: |
| 20′ | `level2_2026W06_2A_0214_1D4_spx_l2b-v27-2026-222.fits` | 2026-02-06 12:29:04.416 | 2.656354047935542 | 0.836725 |
| 20′ | `level2_2026W32_1A_0387_3D4_spx_l2b-v27-2026-223.fits` | 2026-08-04 20:55:04.166 | 2.407114230722482 | 0.443700 |
| 20′ | `level2_2026W32_2A_0552_1D4_spx_l2b-v27-2026-223.fits` | 2026-08-08 19:23:14.525 | 2.8330770013439994 | 0.827100 |
| 20′ | `level2_2026W38_1A_0113_4D4_spx_l2b-v27-2026-261.fits` | 2026-09-14 14:28:32.506 | 2.4516338714155292 | 0.832050 |
| 10′ | `level2_2026W32_1A_0387_3D4_spx_l2b-v27-2026-223.fits` | 2026-08-04 20:55:04.166 | 2.407114230722482 | 0.468300 |
| 5′ | `level2_2026W05_2A_0363_1D4_spx_l2b-v27-2026-220.fits` | 2026-01-31 06:38:53.146 | 3.3314018332317015 | 0.838800 |
| 5′ | `level2_2026W06_2A_0214_1D4_spx_l2b-v27-2026-222.fits` | 2026-02-06 12:29:04.416 | 2.656354047935542 | 0.841200 |
| 5′ | `level2_2026W07_1A_0542_1D4_spx_l2b-v27-2026-225.fits` | 2026-02-11 08:15:37.843 | 3.4221126297591082 | 0.840000 |
| 5′ | `level2_2026W32_1A_0387_3D4_spx_l2b-v27-2026-223.fits` | 2026-08-04 20:55:04.166 | 2.407114230722482 | 0.530800 |
| 5′ | `level2_2026W32_2A_0552_1D4_spx_l2b-v27-2026-223.fits` | 2026-08-08 19:23:14.525 | 2.8330770013439994 | 0.830800 |
| 5′ | `level2_2026W36_1A_0507_1D4_spx_l2b-v27-2026-252.fits` | 2026-09-02 05:47:20.285 | 2.8328744945660977 | 0.829600 |
| 5′ | `level2_2026W38_1A_0113_4D4_spx_l2b-v27-2026-261.fits` | 2026-09-14 14:28:32.506 | 2.4516338714155292 | 0.835600 |

The three independent stretches used the same `asinh` curve with `stretch_a = 0.1`, but different vmin/vmax. In particular, 10′ vmax was 46.84 while 5′ vmax was 1.33 MJy/sr. Identical input values would therefore render at different intensities even when the same FITS product appeared in two searches.
