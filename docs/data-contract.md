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
  "fov_arcmin": 10,
  "band": "D4"
}
```

RA and Dec are ICRS decimal degrees; the frontend converts sexagesimal input. The backend requires both inclusive UTC dates, a positive field width of at most 60 arcminutes, and one of `D1` through `D6`. The UI currently offers widths from 3 to 20 arcminutes. When a date field is blank, the frontend uses May 1, 2025 for the start and the current UTC day for the end. The archive query receives that interval, with the end date extended to midnight on the following day. The backend processes one selected detector at a time and rejects overlapping builds with HTTP 409.

## Response

The backend returns a manifest with `status: "ready"`, `"partial"`, or `"empty"`, `request`, `grid`, `frames`, `failures`, `limitations`, candidate/attempted/skipped counts, and a `manifest_url`. Crowded searches sample up to 12 exposures across the available dates and stop after eight usable frames or roughly two minutes. Skipped candidates are disclosed in the limitations. Each `frames[]` entry contains `date_utc`, `band` (such as `"D4"`), `image_url` under `/outputs/<sequence_id>/`, `center_wavelength_um`, `coverage_fraction`, and archive `source_url`/`cutout_url`. The backend writes the same manifest under `output/<sequence_id>/manifest.json` and serves it through `/outputs/`.

The frontend's `src/data.js` converts that manifest to `{ source: "real", status: "ok" | "empty", target, dates, bands, frames, ... }` for `src/app.js`. It gives individual exposures unique timeline IDs even when several fall on the same UTC day. A `partial` sequence remains viewable with a failure note. It does not treat a failed backend call as an empty sky patch. The backend does not yet provide an observation ID, a pipeline version, or instrument quality flags; the UI labels those fields honestly.

For a frame, `<img src="/outputs/<sequence_id>/frame_000.png">` must load from the same frontend origin via the Node proxy. All frames in one backend sequence share a WCS grid and display stretch. Different bands are separate sequences, so their pixel intensities should not be directly compared as calibrated measurements.

## Demo and errors

`source=demo` explicitly loads `public/demo/frames.json` and bundled synthetic PNGs. The real endpoint never falls back to those assets. A backend HTTP 409 displays a waiting state and retries the same search every five seconds, with cancellation and a ten-minute limit. A backend HTTP 502, a proxy 503/504, malformed metadata, or an image load failure produces an explicit UI error. A successful `empty` manifest with no failed observations means no usable frames in the selected band and date range; the UI offers other bands.

The backend is still a prototype: individual downloads can be slow, crowded searches use a time-spread sample rather than every exposure, and generated outputs have no automatic cleanup. It does not apply instrument quality flags, background matching, or PSF matching. A submitted project should state those limits clearly.
