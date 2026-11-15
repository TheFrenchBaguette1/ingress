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

Open <http://localhost:5173>. The Node server serves the frontend, forwards `POST /api/sequence` to the Python backend's `/sequence`, and forwards `/outputs/*` for generated PNG frames. `npm test` runs the frontend integration checks.

You can run `npm run dev` without Python to explore **Try synthetic WISE 0855**. Its 18 bundled frames are invented illustrations with exaggerated motion, clearly labeled as a synthetic demo. Real observations are never replaced with demo images after an archive error.

## How real data works

The backend searches the IRSA SPHEREx DR1 and QR3 wide-survey collections, downloads FITS cutouts, aligns each exposure to one north-up sky grid, and exports PNGs with a consistent display stretch for a requested detector band. Deep-field-only exposures are outside this search. The frontend searches one of six detector bands at a time, so switching bands may trigger another build. It reuses successful results when you revisit a band during the session. Detector ranges shown for real observations follow the SPHEREx Explanatory Supplement; the actual wavelength at the target can vary between exposures in one detector.

Blank date fields search from May 1, 2025 through the current UTC date. New real searches start at a 5′ field; earlier shared coordinate links without `fov` retain their original 10′ field, and new links include their width. The archive query trims dates before the May 2025 mission start and after today while preserving the interval you entered. Widening a date range requests more observations; narrowing a loaded range filters its frames locally. If a narrowed range has no loaded frames, **Search this date range** queries the archive for that interval. The Python backend handles one build at a time. On HTTP 409, the viewer retries every five seconds for up to 30 waits (about 2.5 minutes, plus request time); you can cancel from the loading screen. To keep crowded fields responsive, the backend attempts up to 12 exposures spread across the available dates, fetches up to four cutouts concurrently, stops after eight usable frames, and caps a build at about two minutes.

Complete successful searches reuse intact saved images in memory and after a server restart. Partial searches with real frames reuse verified PNGs for 15 minutes and are labeled **Cached partial real observations**; their missing dates are not treated as empty sky. **Refresh observations** bypasses the saved sequence and archive listing to request a fresh search; an active archive outage cooldown can still delay the probe. A successful observation listing for an exact patch, date range, and detector stays in memory and on disk for up to one hour. When the IRSA index fails, the backend can reuse previously verified exposure URLs for that exact center and detector, request new cutouts at the selected width, and label the date coverage as partial. The repo includes verified Band 3 and Band 4 source lists and small real frames for RA 180°, Dec −30°; the saved Band 4 example can be opened from the error screen. These known exposures do not establish that other dates or sky positions are empty. A first-time sky position still needs a working archive index. The timeline shows sampled and loaded exposure counts. Generated files accumulate in `output/` until manually removed.

The backend has **not applied instrument quality flags** and has not corrected background or PSF differences. New sequences exclude exposures with less than 25% valid field coverage or a blank target center; saved sequences are also checked for adequate coverage and opaque pixels at the target center before reuse. Partial coverage and failed exposures are shown as data notes. When frames in one band differ meaningfully in wavelength, the viewer cautions that brightness may differ because of wavelength rather than time. PNGs are for visual comparison, not photometry or proof of a discovery. Source cutout links and wavelengths appear in the frame details when the backend provides them.

The actual API and translation to the UI are documented in [docs/data-contract.md](docs/data-contract.md). The backend entry point is `api.py`; the frontend modules are `src/app.js` and `src/data.js`. The browser always requests its own origin, so deployment needs the same `/api/sequence` and `/outputs/*` routing as `server.mjs`, or an equivalent same-origin service.
