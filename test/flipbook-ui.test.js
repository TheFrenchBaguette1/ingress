import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';

const jobId = 'a'.repeat(32);
const sequence4 = 'b'.repeat(32);
const sequence1 = 'c'.repeat(32);
const base = 'http://localhost:5173/?ra=25&dec=-15&from=2025-05-01&to=2026-11-07';
const request = { ra_deg: 25, dec_deg: -15, start_date: '2025-05-01', end_date: '2026-11-07' };

function manifest(band, sequenceId, dates, status = 'ready') {
  return {
    sequence_id: sequenceId, status, request: { ...request, band, fov_arcmin: 20 },
    grid: { width_pixels: 200, height_pixels: 200, pixel_scale_arcsec: 6 },
    display: { vmin: 0, vmax: band === 'D4' ? 2 : 1, stretch: 'asinh' },
    candidate_count: band === 'D4' ? 59 : 4, attempted_count: dates.length,
    frames: dates.map((date, index) => ({
      band, date_utc: date, index, coverage_fraction: 0.8,
      detector: `SPHEREx-${band}`, center_wavelength_um: band === 'D4' ? 2.64 : 0.9,
      product_id: `${band}-${index}.fits`, source_url: `https://irsa.ipac.caltech.edu/${band}-${index}.fits`,
      image_url: `/outputs/${sequenceId}/frame_${String(index).padStart(3, '0')}.png`,
    })),
  };
}

function payload(status, revision, values) {
  return new Response(JSON.stringify({
    job_id: jobId, status, revision, canonical_fov_arcmin: 20,
    bands: Object.fromEntries([1, 2, 3, 4, 5, 6].map(n => [`D${n}`, values[`D${n}`] ?? { status: 'idle' }])),
  }));
}

async function eventually(check) {
  const deadline = Date.now() + 2500;
  while (!check()) {
    assert.ok(Date.now() < deadline, 'The expected flipbook state did not appear');
    await delay(15);
  }
}

async function mount(url, fetchImpl) {
  const listeners = {};
  const app = {
    innerHTML: '',
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
    querySelector() { return null; },
  };
  globalThis.location = new URL(url);
  globalThis.history = { replaceState(_state, _title, next) { globalThis.location = new URL(next); } };
  globalThis.document = {
    querySelector(selector) {
      if (selector === '#app') return app;
      if (selector === 'meta[name="ingress-live-flipbook"]') return { getAttribute: () => 'true' };
      return null;
    },
    addEventListener() {}, hidden: false,
  };
  globalThis.window = { addEventListener() {} };
  globalThis.fetch = fetchImpl;
  await import(`../src/app.js?flipbook=${Math.random()}`);
  const click = (action, extra = {}) => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action, ...extra }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  return { app, click };
}

test('All wavelengths opens progressively, adds earlier frames without changing the selection, and switches locally', async () => {
  const firstDate = '2025-06-04 06:30:00.000';
  const calls = [];
  let releasePoll;
  const { app, click } = await mount(base, (path, options) => {
    calls.push(path);
    if (path === '/api/flipbook') {
      assert.equal(JSON.parse(options.body).band, 'all');
      return Promise.resolve(payload('running', 1, {
        D1: { status: 'queued', candidate_count: 4 },
        D4: { status: 'running', candidate_count: 59, attempted_count: 1,
          manifest: manifest('D4', sequence4, [firstDate], 'partial') },
      }));
    }
    assert.equal(path, `/api/flipbook/${jobId}`);
    return new Promise(resolve => { releasePoll = resolve; });
  });
  await eventually(() => /Frame 1 of 1/.test(app.innerHTML));
  assert.match(app.innerHTML, /All wavelengths/);
  assert.match(app.innerHTML, /Loading exposures across all wavelengths/);
  const firstSelected = globalThis.location.searchParams.get('date');
  assert.equal(globalThis.location.searchParams.get('band'), 'all');
  await eventually(() => typeof releasePoll === 'function');
  releasePoll(payload('complete', 2, {
    D1: { status: 'ready', candidate_count: 4, attempted_count: 1,
      manifest: manifest('D1', sequence1, ['2025-06-03 06:30:00.000']) },
    D4: { status: 'ready', candidate_count: 59, attempted_count: 2,
      manifest: manifest('D4', sequence4, [firstDate, '2025-06-05 06:30:00.000']) },
  }));
  await eventually(() => /Frame 2 of 3/.test(app.innerHTML));
  assert.match(app.innerHTML, /data-frame-image src="\/outputs\/b{32}\/frame_000\.png"/);
  assert.equal(globalThis.location.searchParams.get('date'), firstSelected);
  assert.match(app.innerHTML, /each detector has its own fixed brightness scale/i);
  click('band', { band: '1' });
  assert.match(app.innerHTML, /Frame 1 of 1/);
  assert.match(app.innerHTML, /data-frame-image src="\/outputs\/c{32}\/frame_000\.png"/);
  click('band', { band: 'all' });
  assert.match(app.innerHTML, /Frame 1 of 3/);
  assert.equal(calls.length, 2);
  click('home');
});

test('Old linked exposure sets stay intact until explicitly expanded', async () => {
  const date = '2025-06-04 06:30:00.000';
  const calls = [];
  const { app, click } = await mount(`${base}&band=4&sequence=${sequence4}`, (path, options) => {
    calls.push(path);
    if (path === '/api/zoom') {
      assert.equal(JSON.parse(options.body).sequence_id, sequence4);
      return Promise.resolve(new Response(JSON.stringify(manifest('D4', sequence4, [date]))));
    }
    assert.equal(path, '/api/flipbook');
    assert.equal(JSON.parse(options.body).band, 'D4');
    return Promise.resolve(payload('complete', 1, {
      D4: { status: 'ready', candidate_count: 59, attempted_count: 2,
        manifest: manifest('D4', sequence1, [date, '2025-06-05 06:30:00.000']) },
    }));
  });
  await eventually(() => /Frame 1 of 1/.test(app.innerHTML));
  assert.match(app.innerHTML, /Load more observations/);
  assert.deepEqual(calls, ['/api/zoom']);
  click('expand-flipbook');
  await eventually(() => /Frame 1 of 2/.test(app.innerHTML));
  assert.equal(globalThis.location.searchParams.get('sequence'), sequence1);
  assert.deepEqual(calls, ['/api/zoom', '/api/flipbook']);
  click('home');
});
