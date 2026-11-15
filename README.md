# Ingress · SPHEREx sky-patch viewer

Team Ingress's project for the [2026 NASA Space Apps “Planet X and SPHEREx” challenge](https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/). Enter right ascension and declination, then play through SPHEREx exposures of the same sky region. The frontend and backend live together on this branch.

## Try the live website

**[Open the Ingress viewer](https://ideal-train-jjgp96g4g46rfqgwv-5173.app.github.dev/)** · [Open a saved real SPHEREx example (RA 180°, Dec −30°)](https://ideal-train-jjgp96g4g46rfqgwv-5173.app.github.dev/?ra=180&dec=-30&band=4&fov=5&from=2025-05-01&to=2026-11-15)

The website's Codespace port 5173 is public, so teammates can use these links without signing in to Sean's GitHub account. GitHub may show a one-time development-port notice; choose **Continue** to open the site. Port 8000 stays private; the website forwards its real-data requests to the Python backend inside the Codespace.

This is a live development preview, available **only while the Codespace and both servers are running**. If the link is unavailable, ask Sean to restart the Codespace and the servers using the commands below. The example contains verified real Band 4 observations, with the original FITS links and dates, and is labeled as a fixed snapshot. A new sky position still depends on IRSA's archive search being reachable. This is not a permanent hosted deployment.

## Run the complete app

Use a stable Python 3.11–3.14 and Node 20+. On macOS, double-click `setup-python.command` once to create `.venv` and install the Python dependencies. The same setup from a terminal is:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Then start the backend in one terminal:

```sh
.venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
INGRESS_API_ORIGIN=http://127.0.0.1:8000 npm run dev
```

Open <http://localhost:5173>. The Node server serves the frontend, forwards `/api/sequence`, `/api/zoom`, and `/api/prepare` to Python, and forwards `/outputs/*` for generated PNG frames. `npm test` runs the frontend integration checks. Run `.venv/bin/python -m unittest discover -s test -p 'test_*.py'` for backend checks.

You can run `npm run dev` without Python to explore **Try synthetic WISE 0855**. Its 18 bundled frames are invented illustrations with exaggerated motion, clearly labeled as a synthetic demo. Real observations are never replaced with demo images after an archive error.

## How real data works

The backend searches the IRSA SPHEREx DR1 and QR3 wide-survey collections, downloads FITS cutouts, aligns each exposure to one north-up sky grid, and saves both floating-point aligned science arrays with no-data masks and PNG previews. Deep-field-only exposures are outside this search. On a new real-data search, `/api/prepare` attempts 20′ images for all six detector bands within a finite server budget, preparing the requested band first. A completed band can be switched into view without a new exposure search. Bands with unavailable archive data remain explicitly unavailable; the app never substitutes another detector's frames. Detector ranges follow the SPHEREx Explanatory Supplement; wavelength at the target can vary between exposures in one detector.

Blank date fields search from May 1, 2025 through the current UTC date. The archive query trims dates before the May 2025 mission start and after today while preserving the interval you entered. Once a 20′ band is prepared, 10′ and 5′ are exact central crops of the **same selected exposures**: no new archive search, date sampling, or independent contrast calculation. An older selected 5′ or 10′ sequence can be enlarged by `/api/zoom`, which requests more sky from each **same parent FITS product** and preserves every previously aligned center pixel and no-data mask. Zoom never silently changes an exposure. Widening a date range still requests more observations; narrowing a loaded range filters its existing frames locally. To keep crowded fields responsive, the backend attempts up to 12 exposures spread across available dates, fetches up to four cutouts concurrently, and stops after eight usable frames. The six-band preparation has a shared 105-second processing deadline; any band it cannot verify in time is labeled unavailable and can be retried separately.

Complete successful searches reuse intact saved PNGs **and science arrays** after a server restart. Partial searches with real frames are cached for 15 minutes and labeled **Cached partial real observations**; missing dates are not treated as empty sky. An older PNG-only saved sequence is upgraded by rereading its exact original parent FITS products before it can be zoomed; a failed recovery does not invent pixel values. **Refresh observations** bypasses saved sequence and archive listing caches for a fresh single-band search. `POST /api/prepare?refresh=1` rechecks empty and unavailable detector bands while preserving already verified real ones. A complete observation listing for an exact patch and date range can be shared across bands and saved for up to one hour. When the IRSA index fails, the backend can reuse previously verified exposure URLs for that exact center and detector, with date coverage labeled partial. The repo includes verified Band 3 and Band 4 source lists and small real frames for RA 180°, Dec −30°; these do not prove other dates or positions are empty. Generated science arrays, previews, and metadata accumulate in `output/` until manually removed.

The backend has **not applied instrument quality flags** and has not corrected background or PSF differences. New sequences exclude exposures with less than 25% valid field coverage or a blank target center; saved sequences check their science arrays and preview masks before reuse. If a wider cutout from the same parent exposure fails, the original center remains intact and the outer region is transparently masked and labeled partial. The first alignment uses bilinear WCS interpolation; later zoom crops preserve those aligned floating-point values exactly, while any new outskirts are sampled from the same parent product. A single fixed brightness stretch applies across zoom levels. PNGs are for visual comparison, not photometry or proof of a discovery. Frame details include parent product links, timestamp, detector, wavelength, image dimensions, and requested WCS footprint; valid sky can cover less than that rectangle.

The actual API and translation to the UI are documented in [docs/data-contract.md](docs/data-contract.md). The backend entry point is `api.py`; the frontend modules are `src/app.js` and `src/data.js`. The browser always requests its own origin, so deployment needs the same `/api/sequence`, `/api/zoom`, `/api/prepare`, and `/outputs/*` routing as `server.mjs`, or an equivalent same-origin service.
