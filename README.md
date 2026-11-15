# Ingress · SPHEREx sky-patch viewer

Team Ingress's project for the [2026 NASA Space Apps “Planet X and SPHEREx” challenge](https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/). Enter right ascension and declination, then play through SPHEREx exposures of the same sky region. The frontend and backend live together on this branch.

## Try the live website

**[Open Ingress](https://ingress-ro5t.onrender.com/)**

Anyone can open the viewer and search for real SPHEREx observations. After inactivity, the first visit wakes the free hosting service and can take about a minute.

## Run the complete app

Use Python 3.11–3.14. On macOS, double-click `setup-python.command` once to install dependencies, or run these commands from the repository root.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Start the viewer and API together.

```sh
.venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in your browser. Run `npm test` with Node 20+ for frontend checks and `.venv/bin/python -m unittest discover -s test -p 'test_*.py'` for backend checks.

## How real data works

The backend searches the IRSA SPHEREx DR1 and QR3 wide and deep collections independently. For each matching exposure, it fetches the required science and flag rows from NASA’s public SPHEREx FITS bucket. The IRSA cutout service can supply the same parent exposure when the bucket read fails. The server aligns each exposure to one north-up sky grid and saves floating-point science arrays and PNG previews. The flipbook starts in All wavelengths and adds real observations as their previews finish. Select a detector to focus the timeline on that band, or play at 1–20 frames per second. Each frame keeps its archive timestamp.

Blank date fields search from May 1, 2025 through the current UTC date. The archive query trims dates before the May 2025 mission start and after today while preserving the interval you entered. The flipbook checks up to 160 archive candidates per requested detector within a 210-second budget. Its first batch prioritizes coverage across the selected dates. The timeline reports how many candidates were checked and how many yielded usable images. Frame count depends on actual observations, valid sky coverage, and archive availability.

The 10′ and 5′ views crop the same 20′ aligned exposures, with a fixed display scale and the original no-data mask. Existing shared sequences still support same-parent cutouts through `/api/zoom`. Narrowing the date range filters loaded frames locally. Widening it can request more observations. A complete archive listing can be shared across detector jobs. If IRSA is unavailable, the backend can use previously verified exposure URLs and labels incomplete coverage. Generated science arrays and previews are stored in `output/`.

Bucket images mask the selected instrument quality flags and preserve calibrated brightness. Earlier saved frames and cutout fallbacks can lack those flags, as indicated in frame details. Background and PSF differences can affect visual comparisons. New sequences exclude exposures with less than 25% valid field coverage or a blank target center. The first alignment uses bilinear WCS interpolation. Later zoom crops preserve those aligned floating-point values and masks exactly, with a fixed brightness stretch. Frame details show parent product links, timestamp, detector, wavelength, image dimensions, and requested WCS footprint. PNGs are for visual comparison.

The per-collection search and FITS range reads are based on [Henry Lin’s Ingress backend](https://github.com/henrylin1039/Ingress/tree/ab96fafea21f57dd54f91c741615a1b33eb787c6/backend). Ingress retains the team viewer’s progressive API, anchored zoom, and floating-point data handling.

See [the data contract](docs/data-contract.md) for the flipbook API, legacy routes, and browser data model.
