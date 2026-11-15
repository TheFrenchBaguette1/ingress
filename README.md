# Ingress · SPHEREx sky-patch viewer

Team Ingress's project for the [2026 NASA Space Apps “Planet X and SPHEREx” challenge](https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/). Enter right ascension and declination, then play through SPHEREx exposures of the same sky region. The frontend and backend live together on this branch.

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

The backend searches the IRSA SPHEREx DR1 and QR3 collections, downloads FITS cutouts, aligns each exposure to one north-up sky grid, and exports PNGs with a consistent display stretch for a requested detector band. The frontend searches one of six detector bands at a time, so switching bands may trigger another build. It reuses successful results when you revisit a band during the session.

Blank date fields search from May 1, 2025 through the current UTC date. The archive query trims dates before the May 2025 mission start and after today while preserving the interval you entered; a narrower range reduces work. Widening a range requests more observations; narrowing a loaded range filters its frames locally. The Python backend currently handles one build at a time. When it returns HTTP 409, the viewer waits and retries the same search automatically for up to ten minutes; you can cancel from the loading screen. To keep crowded fields responsive, the backend attempts up to 12 exposures spread across the available dates, fetches up to four cutouts concurrently, stops after eight usable frames, and caps a build at about two minutes. Successful exact searches are reused from memory and from their saved real images after a server restart. The viewer labels reused images as cached because newer archive data may exist. The frame details disclose how many candidates were skipped. Generated files accumulate in `output/` until manually removed.

The backend has **not applied instrument quality flags** and has not corrected background or PSF differences. Partial coverage and failed exposures are shown as data notes. PNGs are for visual comparison, not photometry or proof of a discovery. Source cutout links and wavelengths appear in the frame details when the backend provides them.

The actual API and translation to the UI are documented in [docs/data-contract.md](docs/data-contract.md). The backend entry point is `api.py`; the frontend modules are `src/app.js` and `src/data.js`. The browser always requests its own origin, so deployment needs the same `/api/sequence` and `/outputs/*` routing as `server.mjs`, or an equivalent same-origin service.
