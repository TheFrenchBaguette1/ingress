import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('the home form and old demo links search real observations only', async () => {
  const listeners = {};
  const app = {
    innerHTML: '',
    addEventListener(event, callback) { (listeners[event] ||= []).push(callback); },
    querySelector() { return null; },
  };
  globalThis.location = new URL('http://localhost:5173/');
  globalThis.history = { replaceState(_state, _title, url) { globalThis.location = new URL(url); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  const windowListeners = {};
  globalThis.window = { addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); } };
  const realRequests = [];
  globalThis.fetch = async (url, options) => {
    assert.equal(String(url), '/api/prepare');
    realRequests.push(JSON.parse(options.body));
    return new Response('Unavailable', { status: 503 });
  };
  await import(`../src/app.js?smoke=${Date.now()}`);
  assert.match(app.innerHTML, /Pick a place in the sky/);
  const removedDemoControls = /Synthetic demo|try-wise|source-demo|WISE 0855 demo/i;
  assert.doesNotMatch(app.innerHTML, removedDemoControls);

  globalThis.location = new URL('http://localhost:5173/?ra=133.79512917&dec=-7.24514722&source=demo&band=6&fov=10');
  windowListeners.popstate.forEach(listener => listener());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(realRequests.length, 1);
  assert.equal(realRequests[0].band, 'D6');
  assert.equal(realRequests[0].ra_deg, 133.79512917);
  assert.equal(realRequests[0].dec_deg, -7.24514722);
  assert.equal(realRequests[0].fov_arcmin, undefined); // Preparation owns the 20′ master width.
  assert.equal(globalThis.location.searchParams.get('fov'), '10');
  assert.equal(globalThis.location.searchParams.get('source'), null);
  assert.match(app.innerHTML, /Could not load this patch/);
  assert.match(app.innerHTML, /View saved real observations at RA 180°, Dec −30° \(5′\)/);
  assert.doesNotMatch(app.innerHTML, removedDemoControls);

  listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action: 'home' }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  const OriginalFormData = globalThis.FormData;
  globalThis.FormData = class {
    get(name) { return { ra: '180', dec: '-30', from: '', to: '' }[name]; }
  };
  try {
    listeners.submit.forEach(listener => listener({ target: { id: 'home-form' }, preventDefault() {} }));
    await new Promise(resolve => setImmediate(resolve));
  } finally {
    globalThis.FormData = OriginalFormData;
  }
  assert.deepEqual(realRequests.map(request => request.band), ['D6', 'D4']);
  assert.equal(globalThis.location.searchParams.get('date'), null);
});

test('the static Pages preview opens saved real frames and steps through them without an API', async () => {
  const listeners = {};
  const app = {
    innerHTML: '',
    addEventListener(type, callback) { (listeners[type] ||= []).push(callback); },
    querySelector() { return null; },
  };
  globalThis.location = new URL('https://thefrenchbaguette1.github.io/ingress/?ra=12&dec=75&band=1&fov=20');
  globalThis.history = { replaceState(_state, _title, url) { globalThis.location = new URL(url); } };
  globalThis.document = {
    querySelector(selector) {
      if (selector === '#app') return app;
      if (selector === 'meta[name="ingress-static-preview"]') {
        return { getAttribute(name) { return name === 'content' ? 'true' : null; } };
      }
      return null;
    },
    addEventListener() {}, hidden: false,
  };
  globalThis.window = { addEventListener() {} };
  const savedPath = '/ingress/public/real-example/ra180-dec-30-d4/';
  const manifest = await readFile(new URL('../public/real-example/ra180-dec-30-d4/manifest.json', import.meta.url), 'utf8');
  const fetchedPaths = [];
  globalThis.fetch = async url => {
    const path = new URL(url, globalThis.location).pathname;
    fetchedPaths.push(path);
    assert.equal(path, `${savedPath}manifest.json`);
    return new Response(manifest, { status: 200 });
  };

  await import(`../src/app.js?pages=${Date.now()}`);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(fetchedPaths, [`${savedPath}manifest.json`]);
  assert.match(app.innerHTML, /Saved real SPHEREx observations/);
  assert.match(app.innerHTML, /Frame 1 of 8/);
  assert.match(app.innerHTML, /href="https:\/\/ideal-train-jjgp96g4g46rfqgwv-5173\.app\.github\.dev\//);
  assert.doesNotMatch(app.innerHTML, /id="(?:home|header)-form"|Search the archive|data-action="(?:refresh-observations|search-date-range|retry-preparation)"/);
  assert.equal(globalThis.location.searchParams.get('ra'), '180');
  assert.equal(globalThis.location.searchParams.get('dec'), '-30');
  assert.equal(globalThis.location.searchParams.get('band'), '4');
  assert.equal(globalThis.location.searchParams.get('fov'), '5');

  const imagePath = () => new URL(app.innerHTML.match(/data-frame-image src="([^"]+)"/)[1],
    globalThis.location).pathname;
  assert.equal(imagePath(), `${savedPath}frame_000.png`);
  listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action: 'next' }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  assert.match(app.innerHTML, /Frame 2 of 8/);
  assert.equal(imagePath(), `${savedPath}frame_001.png`);
  assert.deepEqual(fetchedPaths, [`${savedPath}manifest.json`]);
});

test('real viewer prepares all detectors and keeps date filtering and band switching local', async () => {
  const listeners = {};
  const app = {
    innerHTML: '',
    addEventListener(event, callback) { (listeners[event] ||= []).push(callback); },
    querySelector() { return null; },
  };
  const base = 'http://localhost:5173/?ra=133.79512917&dec=-7.24514722&from=2025-05-07&to=2025-05-08&source=demo';
  globalThis.location = new URL(base);
  globalThis.history = { replaceState(_state, _title, url) { globalThis.location = new URL(url); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  const windowListeners = {};
  globalThis.window = { addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); } };
  const requestedBands = [];
  const requestedUrls = [];
  globalThis.fetch = async (url, options) => {
    requestedUrls.push(String(url));
    assert.match(String(url), /^\/api\/prepare(?:\?refresh=1)?$/);
    const request = JSON.parse(options.body);
    requestedBands.push(request.band);
    assert.equal(request.fov_arcmin, undefined);
    const manifest = (band, id, count) => ({
      status: 'ready', sequence_id: id,
      request: { ...request, band, fov_arcmin: 20 },
      grid: { width_pixels: 200, height_pixels: 200, pixel_scale_arcsec: 6,
        orientation: 'north up, east left' },
      candidate_count: band === 'D4' ? 10 : 1, attempted_count: count,
      skipped_count: band === 'D4' ? 8 : 0,
      failures: [], limitations: ['Instrument quality flags have not yet been applied.'],
      frames: Array.from({ length: count }, (_, index) => ({
        index, date_utc: index ? '2025-05-08 20:15:00.000' : '2025-05-07 03:12:00.000', band,
        image_url: `/outputs/${id}/frame_${String(index).padStart(3, '0')}.png`,
        source_url: `https://irsa.ipac.caltech.edu/${band}-${index}.fits`,
        center_wavelength_um: band === 'D4' ? 3.2 + index * 0.4 : 1.9,
        coverage_fraction: 1,
      })),
    });
    const bands = Object.fromEntries(Array.from({ length: 6 }, (_, index) =>
      [`D${index + 1}`, { status: 'empty' }]));
    bands.D4 = { status: 'ready', manifest: manifest('D4', 'b'.repeat(32), 2) };
    bands.D3 = { status: 'ready', manifest: manifest('D3', 'c'.repeat(32), 1) };
    return new Response(JSON.stringify({ job_id: 'a'.repeat(32), status: 'complete',
      requested_band: request.band, canonical_fov_arcmin: 20, bands,
      completed_count: 6, total_count: 6 }), { status: 200 });
  };
  await import(`../src/app.js?real=${Date.now()}`);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  assert.deepEqual(requestedBands, ['D4']);
  assert.deepEqual(requestedUrls, ['/api/prepare']);
  assert.equal(globalThis.location.searchParams.get('fov'), '10');
  assert.equal(globalThis.location.searchParams.get('source'), null);
  assert.match(app.innerHTML, /10′ wide/);
  assert.match(app.innerHTML, /Real SPHEREx observation|Loading real image/);
  assert.match(app.innerHTML, /\/outputs\/b{32}\/frame_000\.png/);
  assert.match(app.innerHTML, /Instrument quality flags have not yet been applied/);
  assert.match(app.innerHTML, /sampled 2 of 10 candidate exposures/);
  assert.match(app.innerHTML, /Brightness changes may reflect different wavelengths/);
  assert.match(app.innerHTML, /Verified bands were prepared with 20′ master images/);
  assert.match(app.innerHTML, /2\.423–3\.822 µm/);
  assert.doesNotMatch(app.innerHTML, /Synthetic demo|try-wise|source-demo/i);
  assert.match(app.innerHTML, /min="2025-05-01"/);
  assert.match(app.innerHTML, /data-band="2"[^>]*disabled/);

  const clickAction = (action, extras = {}) => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action, ...extras }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  clickAction('band', { band: '4' });
  clickAction('fov', { fov: '10' });
  await flush();
  assert.deepEqual(requestedBands, ['D4']); // Re-selecting unchanged settings is local.

  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'to' }, value: '2025-05-07' } }));
  assert.match(app.innerHTML, /Showing 1 of 2 loaded frames/);
  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'to' }, value: '2025-05-08' } }));
  assert.match(app.innerHTML, /Showing 2 of 2 loaded frames/);
  assert.deepEqual(requestedBands, ['D4']);

  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'from' }, value: '2025-05-01' } }));
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4']);

  const click = (band) => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action: 'band', band: String(band) }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  click(2);
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4']);
  assert.equal(globalThis.location.searchParams.get('band'), '4');
  assert.match(app.innerHTML, /data-band="2"[^>]*disabled/);
  click(3);
  assert.deepEqual(requestedBands, ['D4', 'D4']);
  assert.match(app.innerHTML, /\/outputs\/c{32}\/frame_000\.png/);
  assert.equal(globalThis.location.searchParams.get('band'), '3');
  click(4);
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4']);
  assert.match(app.innerHTML, /\/outputs\/b{32}\/frame_000\.png/);

  listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action: 'retry-preparation' }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4', 'D4']);
  assert.equal(requestedUrls.at(-1), '/api/prepare?refresh=1');

  const reentry = new URL(globalThis.location.href);
  reentry.searchParams.delete('sequence');
  reentry.searchParams.set('fov', '5');
  globalThis.location = reentry;
  windowListeners.popstate.forEach(listener => listener());
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4', 'D4']);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
  assert.match(app.innerHTML, /5′ wide/);
});

test('partial real frames can be refreshed and an empty local date filter can search the archive', async () => {
  const listeners = {};
  const app = { innerHTML: '', addEventListener(type, callback) { (listeners[type] ||= []).push(callback); },
    querySelector() { return null; } };
  globalThis.location = new URL('http://localhost:5173/?ra=240&dec=0&band=4&fov=5&from=2025-05-01&to=2025-05-31');
  globalThis.history = { replaceState(_state, _title, url) { globalThis.location = new URL(url); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  globalThis.window = { addEventListener() {} };
  const requests = [];
  globalThis.fetch = async (url, options) => {
    assert.match(String(url), /^\/api\/prepare(?:\?refresh=1)?$/);
    const body = JSON.parse(options.body);
    requests.push({ url: String(url), body });
    const searchedDay = body.start_date === '2025-05-08' ? '2025-05-08' : '2025-05-07';
    const partial = body.start_date !== '2025-05-08';
    const manifest = {
      status: partial ? 'partial' : 'ready', cached_partial: partial && requests.length === 1,
      request: { ...body, fov_arcmin: 20 }, sequence_id: 'c'.repeat(32),
      grid: { width_pixels: 200, height_pixels: 200, pixel_scale_arcsec: 6, orientation: 'north up, east left' },
      candidate_count: 4, attempted_count: 2, skipped_count: 2,
      limitations: ['Time-spread sample: some dates were skipped.'],
      failures: partial ? [{ error: 'No valid center' }] : [],
      frames: [{ index: 0, band: 'D4', date_utc: `${searchedDay} 12:00:00.000`,
        image_url: `/outputs/${'c'.repeat(32)}/frame_000.png`,
        source_url: 'https://irsa.ipac.caltech.edu/real.fits', center_wavelength_um: 3.1,
        coverage_fraction: 0.9 }],
    };
    const bands = Object.fromEntries(Array.from({ length: 6 }, (_, index) =>
      [`D${index + 1}`, { status: 'empty' }]));
    bands.D4 = { status: manifest.status, manifest };
    return new Response(JSON.stringify({ job_id: 'a'.repeat(32), status: 'complete',
      requested_band: body.band, canonical_fov_arcmin: 20, bands,
      completed_count: 6, total_count: 6 }), { status: 200 });
  };
  await import(`../src/app.js?date-search=${Date.now()}`);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  assert.equal(requests.length, 1);
  assert.match(app.innerHTML, /Cached partial real observations/);
  const click = (action, extras = {}) => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action, ...extras }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  click('refresh-observations');
  await flush();
  assert.equal(requests[1].url, '/api/prepare?refresh=1');
  assert.equal(requests[1].body.ra_deg, 240);

  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'from' }, value: '2025-05-08' } }));
  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'to' }, value: '2025-05-08' } }));
  assert.equal(requests.length, 2); // Narrowing only filters loaded frames locally.
  assert.match(app.innerHTML, /No loaded frames in this range/);
  assert.match(app.innerHTML, /Search this date range/);
  assert.match(app.innerHTML, /Real SPHEREx observations/);
  assert.doesNotMatch(app.innerHTML, /Loading real image/);
  click('search-date-range');
  await flush();
  assert.equal(requests[2].url, '/api/prepare?refresh=1');
  assert.deepEqual([requests[2].body.start_date, requests[2].body.end_date],
    ['2025-05-08', '2025-05-08']);
  assert.match(app.innerHTML, /8 May 2025|May 8, 2025/);

  const OriginalFormData = globalThis.FormData;
  globalThis.FormData = class { get(name) { return { ra: '240', dec: '0' }[name]; } };
  try {
    listeners.submit.forEach(listener => listener({ target: { id: 'header-form' }, preventDefault() {} }));
    await flush();
  } finally { globalThis.FormData = OriginalFormData; }
  assert.equal(globalThis.location.searchParams.get('from'), '2025-05-08');
  assert.equal(globalThis.location.searchParams.get('to'), '2025-05-08');

  globalThis.FormData = class { get(name) { return { ra: '241', dec: '0' }[name]; } };
  try {
    listeners.submit.forEach(listener => listener({ target: { id: 'header-form' }, preventDefault() {} }));
    await flush();
  } finally { globalThis.FormData = OriginalFormData; }
  assert.equal(requests.at(-1).body.ra_deg, 241);
  assert.equal(requests.at(-1).body.start_date, '2025-05-01');
  assert.equal(requests.at(-1).body.end_date, new Date().toISOString().slice(0, 10));
  assert.equal(globalThis.location.searchParams.get('from'), null);
  assert.equal(globalThis.location.searchParams.get('to'), null);
});
