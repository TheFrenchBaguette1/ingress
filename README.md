# Ingress · SPHEREx sky-patch viewer

Team Ingress's project for the [2026 NASA Space Apps “Planet X and SPHEREx” challenge](https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/). Enter right ascension and declination, then play through SPHEREx exposures of the same sky region. The frontend and backend live together on this branch.

## Try the live website

**[Open the live Ingress viewer](https://ideal-train-jjgp96g4g46rfqgwv-5173.app.github.dev/)** · **[Play the saved SPHEREx observations on GitHub Pages](https://thefrenchbaguette1.github.io/ingress/)**

The website's Codespace port 5173 is public, so teammates can use these links without signing in to Sean's GitHub account. GitHub may show a one-time development-port notice; choose **Continue** to open the site. Port 8000 stays private; the website forwards its real-data requests to the Python backend inside the Codespace.

Use the GitHub Pages link if the Codespace link fails. It plays eight real Band 4 observations at RA 180°, Dec −30° with dates and original FITS links. It works independently of the Codespace. Searching new coordinates requires the live viewer and access to the IRSA archive.

The live viewer is available **only while the Codespace and both servers are running**. If the link is unavailable, use the saved sequence or restart the Codespace and servers using the commands below.

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

Open <http://localhost:5173>. The Node server serves the frontend, forwards `/api/*` to Python, and serves generated PNG frames through `/outputs/*`. `npm test` runs the frontend integration checks. Run `.venv/bin/python -m unittest discover -s test -p 'test_*.py'` for backend checks.

You can run `npm run dev` without Python to open the bundled real SPHEREx example. Searching for other observations requires the Python backend.

## How real data works

The backend searches the IRSA SPHEREx DR1 and QR3 wide-survey collections, downloads FITS cutouts, aligns each exposure to one north-up sky grid, and saves both floating-point aligned science arrays with no-data masks and PNG previews. The flipbook starts in All wavelengths and adds real observations as their previews finish. Select a detector to focus the timeline on that band. Play at 1–20 frames per second or scrub to a specific exposure. The speed changes how quickly the viewer steps through observations. Observation timestamps remain the archive timestamps.

Blank date fields search from May 1, 2025 through the current UTC date. The archive query trims dates before the May 2025 mission start and after today while preserving the interval you entered. The flipbook checks up to 128 archive candidates per requested detector within a 210-second budget. Its first batch prioritizes coverage across the selected dates. The timeline reports how many candidates were checked and how many yielded usable images. Frame count depends on actual observations, valid sky coverage, and archive availability.

The 10′ and 5′ views crop the same 20′ aligned exposures, with a fixed display scale and the original no-data mask. Existing shared sequences still support same-parent cutouts through `/api/zoom`. Narrowing the date range filters loaded frames locally. Widening it can request more observations. A complete archive listing can be shared across detector jobs. If IRSA is unavailable, the backend can use previously verified exposure URLs and labels incomplete coverage. Generated science arrays and previews are stored in `output/`.

The backend has **not applied instrument quality flags** and has not corrected background or PSF differences. New sequences exclude exposures with less than 25% valid field coverage or a blank target center; saved sequences check their science arrays and preview masks before reuse. If a wider cutout from the same parent exposure fails, the original center remains intact and the outer region is transparently masked and labeled partial. The first alignment uses bilinear WCS interpolation; later zoom crops preserve those aligned floating-point values exactly, while any new outskirts are sampled from the same parent product. A single fixed brightness stretch applies across zoom levels. PNGs are for visual comparison, not photometry or proof of a discovery. Frame details include parent product links, timestamp, detector, wavelength, image dimensions, and requested WCS footprint; valid sky can cover less than that rectangle.

See [the data contract](docs/data-contract.md) for the flipbook API, legacy routes, and browser data model.
