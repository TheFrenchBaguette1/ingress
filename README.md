# Ingress · SPHEREx sky-patch viewer

Frontend for Team Ingress's [2026 NASA Space Apps “Planet X and SPHEREx” challenge](https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/). Enter RA/Dec, then step through the available observation times and infrared detector bands.

## Run it

With Node 20 or later:

```sh
npm run dev
```

Open `http://localhost:5173`. No package installation or API key is needed. **Try WISE 0855** loads 18 bundled *synthetic* frames so the controls can be tested while the backend is built. They are invented illustrations with deliberately exaggerated motion, not SPHEREx observations. The real-data option displays an honest unavailable state until the backend is connected.

If Henry's backend runs at `http://localhost:8000`, start the frontend with:

```sh
INGRESS_API_ORIGIN=http://localhost:8000 npm run dev
```

The dev server forwards `/api/*` to that origin, so the browser requests images and JSON from its own origin. For deployment, serve these files with an equivalent `/api` reverse proxy or serve the frontend from the backend directly.

## Integration

The precise request and response shape is in [docs/data-contract.md](docs/data-contract.md). The browser sends `GET /api/patch?ra=<decimal>&dec=<decimal>&fov=<arcmin>`. It needs browser-readable aligned image cutouts and metadata; raw FITS cannot be shown by an `<img>` element. The frontend normalizes responses in `src/data.js`. The date picker currently filters a complete returned manifest locally so “All dates” and other bands stay available. The backend can support optional `from`/`to` parameters later for large results.

The interface supports decimal or sexagesimal coordinates, inclusive UTC date filters, irregular observation times, per-frame wavelengths and flags, 3–20′ field widths, responsive mobile settings, and URLs with `ra`, `dec`, `source`, `band`, `date`, `fov`, `from`, and `to`. Only the explicit `source=demo` path uses bundled illustrations. Real errors never turn into synthetic data automatically.

## Files

- `index.html`, `src/app.js`, `src/styles.css`: dependency-free interface.
- `src/data.js`: coordinate parsing, synthetic manifest, backend response boundary.
- `public/demo/`: Claude's synthetic frames and metadata, never used as real data.
- `public/art/hero.svg`, `public/fonts/`: design handoff illustration and bundled fonts.
- `server.mjs`: optional local static server and `/api` development proxy.

Run `npm test` for the data adapter checks. The implementation follows the latest design handoff; its old comparison controls and hardcoded archive error were intentionally excluded.
