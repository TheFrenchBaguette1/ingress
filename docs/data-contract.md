# Ingress frontend ↔ SPHEREx backend contract

The viewer is a static frontend. It requests a sky patch from the team's backend; the backend handles archive discovery, FITS cutouts, WCS alignment, and conversion to browser-viewable image files. The bundled WISE 0855 example is **synthetic artwork** and never served through the real endpoint.

## Request

`GET /api/patch?ra=133.79512917&dec=-7.24514722&fov=10&from=2025-05-01&to=2026-06-01`

- `ra`, `dec`: required decimal degrees, ICRS/J2000 coordinates. RA in `[0,360)` and Dec in `[-90,90]`. The frontend also accepts user input such as `08h55m10.831s` and `-07d14m42.53s`, then sends decimal degrees.
- `fov`: width of square patch in arcminutes, 3 through 20 inclusive in this UI. The backend may accept more in future.
- `from`, `to`: optional inclusive UTC calendar dates, `YYYY-MM-DD`. The frontend orders reversed endpoints before sending them. Omit both to request all available dates. If the backend applies these filters, changing the range may require another request; do not silently display excluded frames.
- Return `Content-Type: application/json`; do not return a partial success for an archive outage. A controlled `503` or `{ "status": "unavailable" }` is appropriate.

## Success with images

```json
{
  "status": "ok",
  "source": "real",
  "target": { "raDeg": 133.79512917, "decDeg": -7.24514722, "name": "WISE 0855" },
  "releaseDate": "2026-11-01",
  "sizePx": 192,
  "pixelScaleArcsec": 6.15,
  "fovMaxArcmin": 20,
  "orientation": "North up, East left",
  "bands": [{ "number": 4, "rangeUm": [2.42, 3.82] }],
  "dates": [
    { "id": "2025-05-07", "observedAt": "2025-05-07T03:12:00Z", "visit": "May 2025" },
    { "id": "2025-11-08", "observedAt": "2025-11-08T21:05:00Z", "visit": "Nov 2025" }
  ],
  "frames": [
    {
      "dateId": "2025-05-07",
      "band": 4,
      "wavelengthUm": 3.23,
      "imageUrl": "/api/images/2025-05-07-band4.png",
      "flags": [],
      "observationId": "archive-exposure-id",
      "pipelineVersion": "pipeline-version",
      "archiveUrl": "https://irsa.ipac.caltech.edu/..."
    },
    {
      "dateId": "2025-11-08",
      "band": 4,
      "wavelengthUm": 3.05,
      "imageUrl": "/api/images/2025-11-08-band4.png",
      "flags": [{ "code": "contamination", "label": "Possible contamination near target" }],
      "observationId": "another-exposure-id",
      "pipelineVersion": "pipeline-version",
      "archiveUrl": "https://irsa.ipac.caltech.edu/..."
    }
  ]
}
```

`dates` is the ordered union of time slots for this patch across available bands. Each `dateId` in `frames` refers to a `dates[].id`; use a unique ID beginning with `YYYY-MM-DD` if multiple observations occur on one day. Each `(dateId, band)` should identify one display cutout. The backend chooses a representative exposure or makes more time slots when multiple exposures overlap. A missing `(dateId, band)` makes a hollow timeline point for that band. `observedAt` is a UTC ISO 8601 timestamp. `wavelengthUm` is the wavelength **at the target for that frame**, not a single fixed wavelength per detector; leave null if unknown. `rangeUm` is an approximate detector range. Avoid brightness comparisons across unequal wavelengths without calibration.

Image URLs must be **same-origin paths** such as `/api/images/...` or `./images/...`, so the browser can load them without archive CORS issues. They must display the same center, field size, pixel scale and orientation to make motion meaningful. The backend should preserve the scientific image values during processing, choose consistent display scaling across frames of a band, and record any limitations in flags. Provide an observation ID, processing version and IRSA archive URL for provenance when available. Never return `source: "real"` with synthetic pixels or placeholder metadata.

## Empty and unavailable states

An archive query that completed but found zero usable cutouts returns HTTP 200:

```json
{ "status": "empty", "source": "real", "target": { "raDeg": 133.79512917, "decDeg": -7.24514722 }, "dates": [], "frames": [] }
```

An outage returns HTTP 503, or a JSON response with `status: "unavailable"` and a plain-text `message`. Neither case should be relabeled as synthetic automatically. The UI shows an explicit retry and a separate opt-in synthetic example button.

## Frontend module

`src/data.js` exports:

- `parseRA(text)` / `parseDec(text)` → `{ degrees }` or `{ error }`.
- `parseCoordinates(raText, decText)` → `{ raDeg, decDeg, raError, decError, isValid, isWiseExample }`.
- `fetchRealPatch({ ra, dec, fov, from, to, signal })` → normalized patch, or `PatchDataError` with `code` `invalid_request`, `unavailable`, or `invalid_response`. `AbortError` remains an abort.
- `loadDemoPatch({ ra, dec, signal })` → synthetic WISE 0855 patch from local `public/demo/frames.json`, or `PatchDataError` with `code` `demo_target_unavailable`/`demo_unavailable`.
- `WISE_0855`, `PatchDataError`, `normalizeRealPatch`.

Both loaders return `{ source, status, target, dates, bands, frames, sizePx, pixelScaleArcsec, fovMaxArcmin, orientation, releaseDate }`. Each frame is `{ dateId, band, wavelengthUm, imageUrl, flags, observationId, pipelineVersion, archiveUrl }`; demo frames also have `moverPct` and `variablePct` for simulated overlay labels. The UI must only use those overlay labels on `source === "synthetic"` and must show the synthetic warning and watermark there. The adapter never calls `/api/patch` while loading demo assets.
