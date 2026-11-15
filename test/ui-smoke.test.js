import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('example loads, date filtering stays inside the range, and errors remain explicit', async () => {
  const listeners = {};
  const app = {
    innerHTML: '',
    addEventListener(event, callback) { (listeners[event] ||= []).push(callback); },
    querySelector() { return null; },
  };
  globalThis.location = new URL('http://localhost:5173/');
  globalThis.history = { replaceState(_state, _title, url) { globalThis.location = new URL(url); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  globalThis.window = { addEventListener() {} };
  const manifest = await readFile(new URL('../public/demo/frames.json', import.meta.url), 'utf8');
  const realFovs = [];
  const realBands = [];
  globalThis.fetch = async (url, options) => {
    if (options?.body) {
      const request = JSON.parse(options.body);
      realFovs.push(request.fov_arcmin);
      realBands.push(request.band);
    }
    return String(url).includes('frames.json')
    ? new Response(manifest, { status: 200 }) : new Response('Unavailable', { status: 503 });
  };
  await import(`../src/app.js?smoke=${Date.now()}`);
  assert.match(app.innerHTML, /Pick a place in the sky/);

  const click = (action, extras = {}) => {
    const event = { target: {
      closest: () => ({ dataset: { action, ...extras }, tagName: 'BUTTON' }),
      matches: () => false,
    } };
    listeners.click.forEach(listener => listener(event));
  };
  click('try-wise');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(app.innerHTML, /SYNTHETIC DEMO — NOT SPHEREx OBSERVATIONS/);
  assert.match(app.innerHTML, /Showing 8 of 8 loaded frames/);
  assert.doesNotMatch(app.innerHTML, /1 pixel ≈/);

  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'from' }, value: '2025-11-08' } }));
  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'to' }, value: '2025-11-08' } }));
  assert.match(app.innerHTML, /Showing 1 of 8 loaded frames/);
  click('band', { band: '2' });
  assert.match(app.innerHTML, /Showing 1 of 6 loaded frames/);
  click('band', { band: '6' });
  assert.match(app.innerHTML, /No loaded frames at 4.42–5.00 µm in this date range/);
  assert.doesNotMatch(app.innerHTML, /b6_2025-11-10\.png/);
  click('clear-range');
  assert.match(app.innerHTML, /Showing 4 of 4 loaded frames/);

  click('source-real');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(realFovs, [5]);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
  assert.match(app.innerHTML, /Could not load this patch/);
  assert.match(app.innerHTML, /View saved real observations at RA 180°, Dec −30° \(5′\)/);
  assert.match(app.innerHTML, /Your search has not been replaced with simulated data/);
  assert.doesNotMatch(app.innerHTML, /SYNTHETIC DEMO — NOT SPHEREx OBSERVATIONS/);

  // Returning to the form hides the band picker; a new real search must use
  // its visible default rather than the previously selected synthetic band.
  click('home');
  click('source-demo');
  click('source-real');
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
  assert.deepEqual(realBands, ['D6', 'D4']);
  assert.equal(globalThis.location.searchParams.get('date'), null);
});

test('real viewer searches selected detector, offers another band after empty result, and reuses loaded frames', async () => {
  const listeners = {};
  const app = {
    innerHTML: '',
    addEventListener(event, callback) { (listeners[event] ||= []).push(callback); },
    querySelector() { return null; },
  };
  const base = 'http://localhost:5173/?ra=133.79512917&dec=-7.24514722&from=2025-05-07&to=2025-05-07';
  globalThis.location = new URL(base);
  globalThis.history = { replaceState(_state, _title, url) { globalThis.location = new URL(url); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  const windowListeners = {};
  globalThis.window = { addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); } };
  const requestedBands = [];
  const requestedFovs = [];
  const requestedUrls = [];
  globalThis.fetch = async (url, options) => {
    requestedUrls.push(String(url));
    const request = JSON.parse(options.body);
    requestedBands.push(request.band);
    requestedFovs.push(request.fov_arcmin);
    const empty = request.band === 'D2';
    const id = 'b'.repeat(32);
    const manifest = {
      status: empty ? 'empty' : 'ready',
      request,
      grid: { width_pixels: 100, pixel_scale_arcsec: 6, orientation: 'north up, east left' },
      candidate_count: empty ? 0 : 10, attempted_count: empty ? 0 : 2, skipped_count: empty ? 0 : 8,
      failures: [], limitations: ['Instrument quality flags have not yet been applied.'],
      frames: empty ? [] : [{
        index: 0, date_utc: '2025-05-07 03:12:00.000', band: request.band,
        image_url: `/outputs/${id}/frame_000.png`, source_url: 'https://irsa.ipac.caltech.edu/example',
        center_wavelength_um: 3.2, coverage_fraction: 1,
      }, {
        index: 1, date_utc: '2025-05-07 20:15:00.000', band: request.band,
        image_url: `/outputs/${id}/frame_001.png`, source_url: 'https://irsa.ipac.caltech.edu/example-two',
        center_wavelength_um: 3.6, coverage_fraction: 1,
      }],
    };
    return new Response(JSON.stringify(manifest), { status: 200 });
  };
  await import(`../src/app.js?real=${Date.now()}`);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  assert.deepEqual(requestedBands, ['D4']);
  assert.deepEqual(requestedFovs, [10]); // Shared coordinate URLs without fov retain their old width.
  assert.equal(globalThis.location.searchParams.get('fov'), '10');
  assert.match(app.innerHTML, /10′ wide/);
  assert.match(app.innerHTML, /Real SPHEREx observation|Loading real image/);
  assert.match(app.innerHTML, /\/outputs\/b{32}\/frame_000\.png/);
  assert.match(app.innerHTML, /Instrument quality flags have not yet been applied/);
  assert.match(app.innerHTML, /sampled 2 of 10 candidate exposures/);
  assert.match(app.innerHTML, /Brightness changes may reflect different wavelengths/);
  assert.match(app.innerHTML, /deep-field-only exposures are not searched/);
  assert.match(app.innerHTML, /2\.423–3\.822 µm/);
  assert.doesNotMatch(app.innerHTML, /SYNTHETIC DEMO — NOT SPHEREx OBSERVATIONS/);
  assert.match(app.innerHTML, /min="2025-05-01"/);

  const clickAction = (action, extras = {}) => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action, ...extras }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  clickAction('band', { band: '4' });
  clickAction('fov', { fov: '10' });
  await flush();
  assert.deepEqual(requestedBands, ['D4']); // Re-selecting unchanged settings is local.

  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'from' }, value: '2025-05-01' } }));
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4']);

  const click = (band) => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action: 'band', band: String(band) }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  click(2);
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4', 'D2']);
  assert.match(app.innerHTML, /No usable images for this search/);
  assert.match(app.innerHTML, /Search Band 4/);
  click(4);
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4', 'D2']);
  assert.match(app.innerHTML, /\/outputs\/b{32}\/frame_000\.png/);

  listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action: 'retry' }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  await flush();
  assert.deepEqual(requestedBands, ['D4', 'D4', 'D2', 'D4']);
  assert.equal(requestedUrls.at(-1), '/api/sequence?refresh=1');

  globalThis.location = new URL(`${base}&fov=5`);
  windowListeners.popstate.forEach(listener => listener());
  await flush();
  assert.equal(requestedFovs.at(-1), 5);
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
    const body = JSON.parse(options.body);
    requests.push({ url: String(url), body });
    const searchedDay = body.start_date === '2025-05-08' ? '2025-05-08' : '2025-05-07';
    const partial = body.start_date !== '2025-05-08';
    return new Response(JSON.stringify({
      status: partial ? 'partial' : 'ready', cached_partial: partial && requests.length === 1,
      request: body, sequence_id: 'c'.repeat(32),
      grid: { width_pixels: 50, pixel_scale_arcsec: 6, orientation: 'north up, east left' },
      candidate_count: 4, attempted_count: 2, skipped_count: 2,
      limitations: ['Time-spread sample: some dates were skipped.'],
      failures: partial ? [{ error: 'No valid center' }] : [],
      frames: [{ index: 0, band: 'D4', date_utc: `${searchedDay} 12:00:00.000`,
        image_url: `/outputs/${'c'.repeat(32)}/frame_000.png`,
        source_url: 'https://irsa.ipac.caltech.edu/real.fits', center_wavelength_um: 3.1,
        coverage_fraction: 0.9 }],
    }), { status: 200 });
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
  assert.equal(requests[1].url, '/api/sequence?refresh=1');
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
  assert.equal(requests[2].url, '/api/sequence?refresh=1');
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
