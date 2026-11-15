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

RA and Dec are ICRS decimal degrees; the frontend converts sexagesimal input. The backend requires both inclusive UTC dates, a positive field width of at most 60 arcminutes, and one of `D1` through `D6`. The UI offers widths from 3 to 20 arcminutes; new real searches start at 5′ and encode their width in the URL. Existing coordinate links without `fov` retain the earlier 10′ default. When a date field is blank, the frontend uses May 1, 2025 for the start and the current UTC day for the end. The archive query clips that interval to the mission period through today, with the final available day extended to midnight on the following day. The original requested dates remain in the manifest. The backend processes one selected detector at a time and rejects overlapping builds with HTTP 409.

## Response

The backend returns a manifest with `status: "ready"`, `"partial"`, or `"empty"`, `request`, `grid`, `frames`, `failures`, `limitations`, candidate/attempted/skipped counts, and a `manifest_url`. Crowded searches sample up to 12 exposures across the available dates, fetch up to four cutouts at once, and stop after eight usable frames or roughly two minutes. Skipped candidates are disclosed in the limitations. Each `frames[]` entry contains `date_utc`, `band` (such as `"D4"`), `image_url` under `/outputs/<sequence_id>/`, `center_wavelength_um`, `coverage_fraction`, and archive `source_url`/`cutout_url`. The backend writes the same manifest under `output/<sequence_id>/manifest.json` and serves it through `/outputs/`. Successful exact requests reuse intact saved frames after a server restart, with a note that newer archive data may be available. Successful nonempty SIA observation listings are saved for one hour under `output/.archive-listings/`, keyed by exact position, search radius, collections, and dates; another detector can reuse the listing while still downloading its own cutouts. After a confirmed transient IRSA outage, new lookups fail promptly for a 15-second cooldown, then one request probes recovery. New positions without a saved listing still require the archive service.

The frontend's `src/data.js` converts that manifest to `{ source: "real", status: "ok" | "empty", target, dates, bands, frames, ... }` for `src/app.js`. It gives individual exposures unique timeline IDs even when several fall on the same UTC day. The timeline states how many candidates were sampled and how many frames loaded. A `partial` sequence remains viewable with a failure note. It does not treat a failed backend call as an empty sky patch. The backend does not yet provide an observation ID, a pipeline version, or instrument quality flags; the UI labels those fields honestly. The current archive search covers DR1 and QR3 wide-survey exposures, not deep-field-only exposures.

For a frame, `<img src="/outputs/<sequence_id>/frame_000.png">` must load from the same frontend origin via the Node proxy. All frames in one backend sequence share a WCS grid and display stretch. Different bands are separate sequences, so their pixel intensities should not be directly compared as calibrated measurements. Wavelength at the target can also differ within a detector; the viewer warns when loaded frames differ meaningfully, since wavelength can affect apparent brightness independently of time. The real detector ranges (µm) are D1 0.744–1.116, D2 1.099–1.651, D3 1.636–2.421, D4 2.423–3.822, D5 3.809–4.420, and D6 4.412–5.002, from the SPHEREx Explanatory Supplement v2.1, Table 2.

## Demo and errors

`source=demo` explicitly loads `public/demo/frames.json` and bundled synthetic PNGs. The real endpoint never falls back to those assets. A backend HTTP 409 displays a waiting state and retries the same search every five seconds, with cancellation and a 30-wait limit (about 2.5 minutes plus request time). A backend HTTP 502, a proxy 503/504, malformed metadata, or an image load failure produces an explicit UI error. A successful `empty` manifest with no failed observations means no usable frames in the selected band and date range; the UI offers other bands.

The backend is still a prototype: individual downloads can be slow, crowded searches use a time-spread sample rather than every exposure, and generated outputs have no automatic cleanup. It does not apply instrument quality flags, background matching, or PSF matching. A submitted project should state those limits clearly.
