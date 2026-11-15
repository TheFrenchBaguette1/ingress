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
  globalThis.fetch = async url => String(url).includes('frames.json')
    ? new Response(manifest, { status: 200 }) : new Response('Unavailable', { status: 503 });
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
  assert.match(app.innerHTML, /Showing 8 of 8 frames/);

  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'from' }, value: '2025-11-08' } }));
  listeners.change.forEach(listener => listener({ target: { dataset: { range: 'to' }, value: '2025-11-08' } }));
  assert.match(app.innerHTML, /Showing 1 of 8 frames/);
  click('band', { band: '2' });
  assert.match(app.innerHTML, /Showing 1 of 6 frames/);
  click('band', { band: '6' });
  assert.match(app.innerHTML, /No frames at 4.42–5.00 µm in this date range/);
  assert.doesNotMatch(app.innerHTML, /b6_2025-11-10\.png/);
  click('clear-range');
  assert.match(app.innerHTML, /Showing 4 of 4 frames/);

  click('source-real');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(app.innerHTML, /Could not load this patch/);
  assert.match(app.innerHTML, /Your search has not been replaced with simulated data/);
  assert.doesNotMatch(app.innerHTML, /SYNTHETIC DEMO — NOT SPHEREx OBSERVATIONS/);
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
  globalThis.window = { addEventListener() {} };
  const requestedBands = [];
  globalThis.fetch = async (_url, options) => {
    const request = JSON.parse(options.body);
    requestedBands.push(request.band);
    const empty = request.band === 'D2';
    const id = 'b'.repeat(32);
    const manifest = {
      status: empty ? 'empty' : 'ready',
      request,
      grid: { width_pixels: 100, pixel_scale_arcsec: 6, orientation: 'north up, east left' },
      failures: [], limitations: ['Instrument quality flags have not yet been applied.'],
      frames: empty ? [] : [{
        index: 0, date_utc: '2025-05-07 03:12:00.000', band: request.band,
        image_url: `/outputs/${id}/frame_000.png`, source_url: 'https://irsa.ipac.caltech.edu/example',
        center_wavelength_um: 3.2, coverage_fraction: 1,
      }],
    };
    return new Response(JSON.stringify(manifest), { status: 200 });
  };
  await import(`../src/app.js?real=${Date.now()}`);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  assert.deepEqual(requestedBands, ['D4']);
  assert.match(app.innerHTML, /Real SPHEREx observation|Loading real image/);
  assert.match(app.innerHTML, /\/outputs\/b{32}\/frame_000\.png/);
  assert.match(app.innerHTML, /Instrument quality flags have not yet been applied/);
  assert.doesNotMatch(app.innerHTML, /SYNTHETIC DEMO — NOT SPHEREx OBSERVATIONS/);
  assert.match(app.innerHTML, /min="2025-05-01"/);

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
});
