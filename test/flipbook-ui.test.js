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
  const click = (action, extra = {}, form = null) => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action, ...extra }, tagName: 'BUTTON', closest: () => form }),
    matches: () => false,
  } }));
  return { app, click };
}

test('a known target opens an archive search with the entered dates in one click', async () => {
  const expected = { ra_deg: 331.096, dec_deg: -56.801, start_date: '2025-06-01', end_date: '2025-07-01', band: 'all' };
  const calls = [];
  const { app, click } = await mount('http://localhost:5173/', (path, options) => {
    assert.equal(path, '/api/flipbook');
    calls.push(JSON.parse(options.body));
    const observation = manifest('D4', sequence4, ['2025-06-04 06:30:00.000']);
    observation.request = { ...expected, band: 'D4', fov_arcmin: 20 };
    return Promise.resolve(payload('complete', 1, { D4: { status: 'ready', manifest: observation } }));
  });
  assert.equal([...app.innerHTML.matchAll(/data-action="target-preset"/g)].length, 5);
  assert.deepEqual(calls, []);
  const originalFormData = globalThis.FormData;
  const form = { id: 'home-form' };
  globalThis.FormData = class {
    constructor(value) { assert.equal(value, form); }
    get(name) { return { ra: '12', dec: '75', from: expected.start_date, to: expected.end_date }[name]; }
  };
  try {
    click('target-preset', { target: 'epsilon-indi-b' }, form);
    await eventually(() => /Frame 1 of 1/.test(app.innerHTML));
    assert.deepEqual(calls, [expected]);
    assert.match(app.innerHTML, /Epsilon Indi B · <span[^>]*>RA 331\.096 · Dec -56\.801/);
  } finally {
    globalThis.FormData = originalFormData;
    click('home');
  }
});

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

function queueFull() {
  return new Response(JSON.stringify({
    detail: { code: 'flipbook_busy', message: 'The archive queue is full.' },
  }), { status: 503, headers: { 'Retry-After': '1' } });
}

test('opening a flipbook retries admission, shows the accepted queue, then opens the first real frame', async () => {
  let admissions = 0;
  let releasePoll;
  const { app, click } = await mount(base, path => {
    if (path === '/api/flipbook') {
      if (++admissions === 1) return Promise.resolve(queueFull());
      return Promise.resolve(payload('running', 1, Object.fromEntries(
        [1, 2, 3, 4, 5, 6].map(n => [`D${n}`, { status: 'queued' }]))));
    }
    assert.equal(path, `/api/flipbook/${jobId}`);
    return new Promise(resolve => { releasePoll = resolve; });
  });
  try {
    await eventually(() => /Waiting to start the archive search/.test(app.innerHTML));
    assert.match(app.innerHTML, /This search will retry automatically/);
    await eventually(() => /Archive search queued/.test(app.innerHTML));
    assert.match(app.innerHTML, /start when a worker is free/);
    assert.doesNotMatch(app.innerHTML, /Finding available SPHEREx exposures/);
    await eventually(() => typeof releasePoll === 'function');
    releasePoll(payload('complete', 2, {
      D4: { status: 'ready', manifest: manifest('D4', sequence4, ['2025-06-04 06:30:00.000']) },
    }));
    await eventually(() => /Frame 1 of 1/.test(app.innerHTML));
    assert.equal(admissions, 2);
  } finally {
    click('home');
  }
});

test('cancel leaves the queue wait and stops automatic admission retries', async () => {
  const signals = [];
  const { app, click } = await mount(base, (path, { signal }) => {
    assert.equal(path, '/api/flipbook');
    signals.push(signal);
    return Promise.resolve(queueFull());
  });
  await eventually(() => /Waiting to start the archive search/.test(app.innerHTML));
  click('cancel');
  assert.match(app.innerHTML, /Show this patch/);
  assert.ok(signals[0].aborted);
  await delay(1050);
  assert.equal(signals.length, 1);
});

test('band switching keeps the current exposure visible during a retry and after queue admission', async () => {
  let admissions = 0;
  let releasePoll;
  const readyBand4 = { status: 'ready', manifest: manifest('D4', sequence4, ['2025-06-04 06:30:00.000']) };
  const { app, click } = await mount(`${base}&band=4`, path => {
    if (path === '/api/flipbook') return Promise.resolve(payload('complete', 1, { D4: readyBand4 }));
    if (path === `/api/flipbook/${jobId}/band/D1`) {
      if (++admissions === 1) return Promise.resolve(queueFull());
      return Promise.resolve(payload('running', 2, { D4: readyBand4, D1: { status: 'queued' } }));
    }
    assert.equal(path, `/api/flipbook/${jobId}`);
    return new Promise(resolve => { releasePoll = resolve; });
  });
  try {
    await eventually(() => /Frame 1 of 1/.test(app.innerHTML));
    click('band', { band: '1' });
    await eventually(() => /Waiting to start Band 1/.test(app.innerHTML));
    assert.match(app.innerHTML, /Band 4 is still selected/);
    assert.match(app.innerHTML, /data-frame-image src="\/outputs\/b{32}\/frame_000\.png"/);
    await eventually(() => /Band 1 queued and waiting for a worker/.test(app.innerHTML));
    assert.match(app.innerHTML, /data-frame-image src="\/outputs\/b{32}\/frame_000\.png"/);
    await eventually(() => typeof releasePoll === 'function');
    releasePoll(payload('complete', 3, {
      D4: readyBand4,
      D1: { status: 'ready', manifest: manifest('D1', sequence1, ['2025-06-05 06:30:00.000']) },
    }));
    await eventually(() => /data-frame-image src="\/outputs\/c{32}\/frame_000\.png"/.test(app.innerHTML));
    assert.equal(admissions, 2);
  } finally {
    click('home');
  }
});

test('preview retries and band switches reuse the prepared observations', async () => {
  const oldImage = globalThis.Image;
  const oldCreate = URL.createObjectURL;
  const oldRevoke = URL.revokeObjectURL;
  let attempts = 0;
  const calls = [];
  globalThis.Image = class {
    decode() {
      attempts++;
      return attempts <= 2 ? Promise.reject(new Error(`transient decode error ${attempts}`)) : Promise.resolve();
    }
  };
  URL.createObjectURL = () => `blob:test-${attempts}`;
  URL.revokeObjectURL = () => {};
  let view;
  try {
    view = await mount(base, (path) => {
      calls.push(path);
      if (path === '/api/flipbook') return Promise.resolve(payload('complete', 1, {
        D1: { status: 'ready', candidate_count: 1, attempted_count: 1,
          manifest: manifest('D1', sequence1, ['2025-06-03 06:30:00.000']) },
        D2: { status: 'empty' }, D3: { status: 'empty' },
        D4: { status: 'empty' }, D5: { status: 'empty' }, D6: { status: 'empty' },
      }));
      assert.match(path, new RegExp(`^/outputs/${sequence1}/frame_000\\.png$`));
      return Promise.resolve(new Response(new Blob(['test'], { type: 'image/png' })));
    });
    await eventually(() => /All wavelengths is unavailable/.test(view.app.innerHTML));
    assert.match(view.app.innerHTML, /transient decode error 1/);
    view.click('retry-preparation');
    await eventually(() => /transient decode error 2/.test(view.app.innerHTML));
    view.click('band', { band: '1' });
    await eventually(() => /Frame 1 of 1/.test(view.app.innerHTML));
    assert.equal(attempts, 3);
    assert.equal(calls.filter(path => path === '/api/flipbook').length, 1);
    assert.equal(calls.filter(path => path.startsWith('/outputs/')).length, 3);
  } finally {
    view?.click('home');
    if (oldImage === undefined) delete globalThis.Image;
    else globalThis.Image = oldImage;
    URL.createObjectURL = oldCreate;
    URL.revokeObjectURL = oldRevoke;
  }
});

test('a shared archive timeout shows its real error and avoids six blind band retries', async () => {
  const archiveError = "IRSA's SPHEREx image search did not respond in time for this patch.";
  const unavailable = Object.fromEntries([1, 2, 3, 4, 5, 6].map(number =>
    [`D${number}`, { status: 'unavailable', error: number === 1 ? archiveError :
      "IRSA's SPHEREx image search is temporarily unavailable." }]));
  const { app, click } = await mount(base, (path) => {
    assert.equal(path, '/api/flipbook');
    return Promise.resolve(payload('complete', 1, unavailable));
  });
  await eventually(() => /Archive search unavailable/.test(app.innerHTML));
  assert.match(app.innerHTML, /IRSA&#39;s SPHEREx image search did not respond in time/);
  assert.match(app.innerHTML, /Retry archive search/);
  assert.doesNotMatch(app.innerHTML, /Image previews could not load/);
  assert.doesNotMatch(app.innerHTML, /Try Band [1-6]/);
  click('home');
});

test('mixed detector failures still offer a different band', async () => {
  const { app, click } = await mount(base, (path) => {
    assert.equal(path, '/api/flipbook');
    return Promise.resolve(payload('complete', 1, {
      D1: { status: 'unavailable', error: 'Band 1 archive timed out.' },
      D2: { status: 'empty' }, D3: { status: 'empty' },
      D4: { status: 'empty' }, D5: { status: 'empty' }, D6: { status: 'empty' },
    }));
  });
  await eventually(() => /All wavelengths is unavailable/.test(app.innerHTML));
  assert.match(app.innerHTML, /Band 1 archive timed out/);
  assert.match(app.innerHTML, /Try Band 2/);
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
