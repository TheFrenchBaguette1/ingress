import test from 'node:test';
import assert from 'node:assert/strict';

const flush = () => new Promise(resolve => setImmediate(resolve));

test('all-band preparation disables missing Band 1, retry prepares it, then band clicks stay local', async () => {
  const listeners = {};
  const app = {
    innerHTML: '',
    addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
    querySelector() { return null; },
  };
  globalThis.location = new URL('http://localhost:5173/?ra=12&dec=75&band=4&fov=10');
  globalThis.history = { replaceState(_state, _title, href) { globalThis.location = new URL(href); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  globalThis.window = { addEventListener() {} };
  const calls = [];
  function manifest(body, band) {
    const id = band === 'D4' ? '4'.repeat(32) : '1'.repeat(32);
    return {
      status: 'ready', sequence_id: id,
      request: { ...body, band, fov_arcmin: 20 },
      grid: { width_pixels: 200, height_pixels: 200, pixel_scale_arcsec: 6,
        orientation: 'north up, east left' },
      display: { vmin: 0, vmax: 2, stretch: 'asinh', stretch_a: 0.1,
        brightness_unit: 'MJy / sr', missing_pixels: 'transparent' },
      frame_count: 1, candidate_count: 1, attempted_count: 1, skipped_count: 0,
      failures: [], limitations: [],
      frames: [{ index: 0, date_utc: '2026-02-06 12:29:04.416', band,
        detector: `SPHEREx-${band}`, center_wavelength_um: band === 'D4' ? 2.65 : 0.91,
        coverage_fraction: 0.86,
        image_url: `/outputs/${id}/frame_000.png`,
        source_url: `https://irsa.ipac.caltech.edu/${band}-actual.fits` }],
    };
  }
  globalThis.fetch = async (url, options) => {
    calls.push(String(url));
    assert.match(String(url), /^\/api\/prepare(?:\?refresh=1)?$/);
    const body = JSON.parse(options.body);
    assert.equal(body.band, 'D4');
    assert.equal(body.fov_arcmin, undefined); // The backend owns the canonical 20′ grid.
    const retry = String(url).includes('refresh=1');
    const bands = Object.fromEntries(Array.from({ length: 6 }, (_, index) =>
      [`D${index + 1}`, { status: 'empty' }]));
    bands.D4 = { status: 'ready', manifest: manifest(body, 'D4') };
    bands.D1 = retry ? { status: 'ready', manifest: manifest(body, 'D1') } :
      { status: 'unavailable', error: 'IRSA D1 index timed out.' };
    return new Response(JSON.stringify({ job_id: 'a'.repeat(32), status: 'complete',
      requested_band: 'D4', canonical_fov_arcmin: 20, bands,
      completed_count: 6, total_count: 6 }), { status: 200 });
  };
  await import(`../src/app.js?band-switch=${Date.now()}`);
  await flush();
  assert.deepEqual(calls, ['/api/prepare']);
  assert.match(app.innerHTML, /SPHEREx-D4/);
  assert.match(app.innerHTML, /D4-actual\.fits/);
  assert.match(app.innerHTML, /data-band="1"[^>]*disabled/);
  assert.match(app.innerHTML, /Unavailable/);

  const click = (action, extras = {}) => listeners.click.forEach(handler => handler({ target: {
    closest: () => ({ dataset: { action, ...extras }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  click('band', { band: '1' });
  assert.deepEqual(calls, ['/api/prepare']);
  assert.equal(globalThis.location.searchParams.get('band'), '4');
  assert.doesNotMatch(app.innerHTML, /D1-actual\.fits/);

  click('retry-preparation');
  await flush();
  assert.deepEqual(calls, ['/api/prepare', '/api/prepare?refresh=1']);
  assert.match(app.innerHTML, /D4-actual\.fits/);
  click('band', { band: '1' });
  assert.deepEqual(calls, ['/api/prepare', '/api/prepare?refresh=1']);
  assert.equal(globalThis.location.searchParams.get('band'), '1');
  assert.match(app.innerHTML, /SPHEREx-D1/);
  assert.match(app.innerHTML, /D1-actual\.fits/);
  assert.doesNotMatch(app.innerHTML, /Band 1 is unavailable/);
});
