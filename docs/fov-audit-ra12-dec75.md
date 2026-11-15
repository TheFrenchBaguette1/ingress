# FOV audit: RA 12°, Dec 75°

These are the archived responses from the live Codespace before the zoom fix. All three requests used detector D4, a 6″ output pixel scale, and independently searched and selected exposures. The product ID below is the exact FITS basename from each frame's `source_url`; UTC timestamps and wavelengths are from the manifests. Wavelength is the value at the requested target, not a single fixed wavelength for the detector.

| FOV | Saved sequence | Frames | Output pixels | WCS pixel-edge sky footprint corners, RA/Dec in degrees | Display vmin/vmax (MJy/sr) |
| --- | --- | ---: | --- | --- | --- |
| 20′ | `00704cd9da3b400ab07250dd0f3d805c` | 4 | 200 × 200 | (12.6370086, 74.8324396); (11.3629914, 74.8324396); (11.3490099, 75.1657509); (12.6509901, 75.1657509) | 0.07927958250045776 / 4.4194103050230975 |
| 10′ | `7769e70d4a5b4e048e1cafc528b6ad2e` | 1 | 100 × 100 | (12.3202337, 74.9164419); (11.6797663, 74.9164419); (11.6762709, 75.0831058); (12.3237291, 75.0831058) | 0.0930284571647644 / 46.83575126647961 |
| 5′ | `c201453042b247e1b4546cdd329f9da0` | 7 | 50 × 50 | (12.1605515, 74.9582770); (11.8394485, 74.9582770); (11.8385747, 75.0416100); (12.1614253, 75.0416100) | 0.07867612794041634 / 1.3339082741737416 |

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

## After the zoom fix: one selected sequence

The live site restored the exact 20′ sequence `00704cd9da3b400ab07250dd0f3d805c` and prepared all six real detector bands before opening the viewer. With Band 4 selected, clicking 20′ → 10′ → 5′ displayed **4 → 4 → 4** frames. The first selected product remained `level2_2026W06_2A_0214_1D4_spx_l2b-v27-2026-222.fits`, observed 2026-02-06 12:29:04.416 UTC in detector `SPHEREx-D4` at 2.656354047935542 µm. The other three products and their exact UTC times and target wavelengths are the four 20′ entries in the table above; their order did not change. The fixed asinh limits remained `0.07927958250045776` to `4.4194103050230975` MJy/sr at every width.

| Display width | Selected sequence | Frames | Center-aligned displayed science pixels | Requested WCS sky footprint corners |
| --- | --- | ---: | --- | --- |
| 20′ | `00704cd9da3b400ab07250dd0f3d805c` | 4 | 200 × 200 | 20′ WCS row above |
| 10′ | `00704cd9da3b400ab07250dd0f3d805c` | 4 | 100 × 100 | 10′ WCS row above |
| 5′ | `00704cd9da3b400ab07250dd0f3d805c` | 4 | 50 × 50 | 5′ WCS row above |

The browser uses a decoded 200 × 200 image from the same 20′ master at every narrower width, centered and displayed with nearest-neighbor magnification. We also generated 10′ and 5′ science crops from the **same four parent products** in the live Codespace and compared each frame: the central 50 × 50 floating-point values and NaN masks were exactly equal at all three widths (4/4 products passed). This older selected sequence was originally PNG-only; the backend first recovered aligned science arrays by rereading those recorded parent FITS URLs. The equality claim applies to that recovered master and its derived crops. Its original pre-fix floating-point arrays were never stored, so those old PNG values cannot be used to prove bit-for-bit identity against the recovered master.
