# Ingress frontend ↔ SPHEREx backend contract

The browser calls the same-origin Node development proxy. The proxy forwards the request to `api.py` without changing its JSON body and serves generated PNGs from the backend's `/outputs/` mount. The Python backend performs the IRSA archive work; the browser does not read FITS directly.

## Request

`POST /api/sequence` (proxied to backend `POST /sequence`), `Content-Type: application/json`:

```json
{
  "ra_deg": 133.79512917,
  "dec_deg": -7.24514722,
  "start_date": "2025-05-01",
  "end_date": "2026-05-31",
  "fov_arcmin": 5,
  "band": "D4"
}
```

RA and Dec are ICRS decimal degrees; the frontend converts sexagesimal input. The backend requires both inclusive UTC dates, a positive field width of at most 60 arcminutes, and one of `D1` through `D6`. The UI offers widths from 3 to 20 arcminutes and encodes the currently displayed width in the URL. Existing coordinate links without `fov` retain the earlier 10′ default. When a date field is blank, the frontend uses May 1, 2025 for the start and the current UTC day for the end. The archive query clips that interval to the mission period through today, with the final available day extended to midnight on the following day. The original requested dates remain in the manifest. The direct `/sequence` endpoint still builds one selected detector at a time and rejects overlapping direct builds with HTTP 409.

`POST /api/sequence?refresh=1` forwards to backend `/sequence?refresh=1` with the same JSON body. It bypasses an exact saved sequence and the saved archive listing to probe IRSA again. The viewer uses the preparation route for a new search and **Refresh observations**, and the direct route for **Search this date range** when a loaded range is filtered to no frames. Changing to a narrower date filter otherwise filters loaded frames locally; a range extending outside the loaded query starts a new initial preparation. An empty local filter does not prove that IRSA has no observations in that interval.

## Prepare bands before opening the viewer

The main real-data flow calls `POST /api/prepare` with `ra_deg`, `dec_deg`, inclusive `start_date`/`end_date`, and the requested initial `band` (no FOV parameter). It receives a 32-character `job_id`, `status: "running" | "complete"`, `canonical_fov_arcmin: 20`, `completed_count`, `total_count: 6`, and six entries in `bands` (`D1`…`D6`). The browser polls `GET /api/prepare/<job_id>` while the initial preparation screen is visible. Each entry progresses through `queued`, `running`, then `ready`, `partial`, `empty`, or `unavailable`. Ready/partial entries contain their own genuine `/sequence` manifest. The requested band runs first. A single short unfiltered SIA lookup may be shared across bands only when complete; each detector's source URLs and cutouts remain separately verified.

The job has a 105-second shared processing deadline; the browser has a 130-second safety limit, then preloads and decodes all ready frame PNGs into local Blob URLs before opening the viewer. Later band changes and 20′→10′→5′ zooms use these already loaded images without another archive or image request. A band with no verified image, an incomplete upstream lookup, a failed preview download, or an exhausted preparation budget stays disabled with its actual reason. `POST /api/prepare?refresh=1` rechecks partial, empty, and unavailable bands while retaining verified ready manifests; if the refresh fails, previously verified partial frames remain available and are clearly labeled. An explicit refresh, a new sky position, or a widened date range begins another preparation. A shared `sequence=<id>` URL restores that precise product set at up to 20′ before other bands are prepared; another batch result cannot replace its selected sequence.

## Response

The backend returns a manifest with `status: "ready"`, `"partial"`, or `"empty"`, `request`, `grid`, `frames`, `failures`, `limitations`, candidate/attempted/skipped counts, and a `manifest_url`. Crowded searches sample up to 12 exposures across the available dates, fetch up to four cutouts at once, and stop after eight usable frames or roughly two minutes. Skipped candidates are disclosed in the limitations. Each `frames[]` entry contains `date_utc`, `band` (such as `"D4"`), `image_url` under `/outputs/<sequence_id>/`, `center_wavelength_um`, `coverage_fraction`, and archive `source_url`/`cutout_url`. Exposures below 25% valid field coverage or with a blank target center are excluded. The backend writes the same manifest under `output/<sequence_id>/manifest.json` and serves it through `/outputs/`.

Exact `ready` requests reuse verified PNG frames in memory or from disk after a restart; `partial` requests with frames reuse them for at most 15 minutes. Disk reuse and partial memory reuse set `cached_result`, `cached_partial`, and `cache_age_seconds`; partial reuse also sets `cache_ttl_seconds: 900` and adds a limitation note. The cache verifies each saved frame's PNG, coverage, target center and aligned science array before reuse. Empty responses and failures without frames are not cached as sequences. Successful nonempty SIA observation listings are saved in memory and under `output/.archive-listings/` for one hour, keyed by exact position, search radius, collections, dates, and detector; an older unfiltered listing may also be reused by another detector. Refresh bypasses both caches, subject to the archive outage cooldown. After a confirmed transient IRSA outage, new lookups fail promptly for a 15-second cooldown, then one request probes recovery. New positions without a saved listing still require the archive service.

The frontend's `src/data.js` converts that manifest to `{ source: "real", status: "ok" | "empty", target, dates, bands, frames, ... }` for `src/app.js`. It gives individual exposures unique timeline IDs even when several fall on the same UTC day. The timeline states how many candidates were sampled and how many frames loaded. A `partial` sequence remains viewable with a note and a refresh control; cached partial data is labeled. It does not treat a failed backend call or a local date filter with no loaded frames as an empty sky patch. New manifests expose `product_id` as the exact FITS basename from `source_url`, plus per-frame `date_utc`, `detector`, `center_wavelength_um`, `pixel_dimensions` (`width`, `height`) and `sky_footprint`. There is no separate catalog observation ID, pipeline version field, or instrument quality flag; the UI labels those fields honestly. The current archive search covers DR1 and QR3 wide-survey exposures, not deep-field-only exposures.

For a frame, `<img src="/outputs/<sequence_id>/frame_000.png">` must load from the same frontend origin via the Node proxy. All frames in one backend sequence share a WCS grid and display stretch. Different bands are separate sequences, so their pixel intensities should not be directly compared as calibrated measurements. Wavelength at the target can also differ within a detector; the viewer warns when loaded frames differ meaningfully, since wavelength can affect apparent brightness independently of time. The real detector ranges (µm) are D1 0.744–1.116, D2 1.099–1.651, D3 1.636–2.421, D4 2.423–3.822, D5 3.809–4.420, and D6 4.412–5.002, from the SPHEREx Explanatory Supplement v2.1, Table 2.

## Zoom an existing exposure sequence

`POST /api/zoom` (proxied to backend `POST /zoom`), `Content-Type: application/json`:

```json
{"sequence_id":"00704cd9da3b400ab07250dd0f3d805c","fov_arcmin":5}
```

`sequence_id` identifies an already selected, saved sequence; `fov_arcmin` is an integer field width from 1 to 60. Zoom returns a sequence-shaped manifest with the **same** `sequence_id`, number and order of frames, FITS `source_url`/`product_id`, observation timestamp, detector band, and wavelength at the target. It keeps `display` unchanged (`vmin`, `vmax`, asinh stretch and units), and changes `request.fov_arcmin`, output `grid`, per-frame `pixel_dimensions`, `sky_footprint` and `image_url` when the width differs. Derived previews live under `/outputs/<sequence_id>/zoom_<width padded to three digits>/frame_000.png`. A derived zoom reports `zoom_source_sequence_id`, `zoom_source_fov_arcmin` and `zoom_policy`; requesting the original width returns the original manifest with a `zoom_policy` note. Per-frame `zoom_outer_region_status` is `cropped`, `loaded`, or `unavailable` in derived views; `zoom_cutout_url` and `zoom_note` describe same-product expansion when applicable.

The original build stores each aligned `float64` science image in `science_000.npy` alongside its preview and marks the manifest `science_format: "aligned-npy-v1"`. Its NaNs mark no-data pixels. Zooming in takes a centered crop from the widest saved parent grid. Zooming out may fetch a wider cutout from **the same FITS parent product**; it copies every prior science pixel and no-data mask back into the aligned center. If that parent download is unavailable, the center stays intact and the unseen outer sky is transparent, with `zoom_outer_region_status: "unavailable"` and a limitation. Zoom does not run a new SIA observation search, sample candidates, switch exposures, or recompute display brightness limits. Repeated widths reuse saved zoom grids. The frontend checks identity and display invariants before accepting a zoom response and leaves the current image selected if they fail.

Older PNG-only saved sequences have no recoverable floating-point array. The backend can upgrade one by rereading the **recorded FITS URLs only** and rebuilding the aligned arrays and previews; it fails explicitly if those sources are unavailable. It cannot prove that pixels reconstructed from a legacy sequence exactly match the old PNG preview because the original float values were never saved. New sequences retain arrays from their first build and support exact pixel comparisons across zoom widths.

`grid.sky_footprint` and `frames[].sky_footprint` list four RA/Dec corners of the **requested rectangular WCS grid**, in order around its edges; they do not claim that every point was observed. Per-frame `coverage_fraction` and transparent PNG pixels describe the actual valid mask. The saved science pixels are the output of the initial **bilinear WCS reprojection** from the archive cutout; this format does not retain the original native FITS pixel values. Subsequent crops preserve the saved aligned floats exactly. The same fixed display scale makes intensities visually comparable across zoom widths of one sequence. An unrelated new `/sequence` request, even at the same coordinates but a different FOV, can select different products and calculate an independent scale.

## Demo and errors

`source=demo` explicitly loads `public/demo/frames.json` and bundled synthetic PNGs. The real endpoint never falls back to those assets. A backend HTTP 409 displays a waiting state and retries the same search every five seconds, with cancellation and a 30-wait limit (about 2.5 minutes plus request time). A backend HTTP 502, a proxy 503/504, malformed metadata, or an image load failure produces an explicit UI error. A successful `empty` manifest with no failed observations means no usable frames in the selected band and date range; the UI offers other bands.

The backend is still a prototype: individual downloads can be slow, crowded searches use a time-spread sample rather than every exposure, and generated outputs have no automatic cleanup. It does not apply instrument quality flags, background matching, or PSF matching. A submitted project should state those limits clearly.
