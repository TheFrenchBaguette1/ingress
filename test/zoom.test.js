import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchZoomedRealPatch, normalizeSequencePatch, parseCoordinates, restoreRealSequence } from '../src/data.js';

const id = 'd'.repeat(32);
const originalScale = { vmin: -0.1, vmax: 3.7, stretch: 'asinh', stretch_a: 0.1, missing_pixels: 'transparent' };
const sourceUrls = ['https://irsa.ipac.caltech.edu/p0.fits', 'https://irsa.ipac.caltech.edu/p1.fits'];
const jobId = 'f'.repeat(32);
function manifest(fov, { zoom = false } = {}) {
  return {
    sequence_id: id, status: 'ready',
    request: { ra_deg: 12, dec_deg: 75, band: 'D4', fov_arcmin: fov,
      start_date: '2025-05-01', end_date: '2025-11-30' },
    grid: { width_pixels: fov * 10, height_pixels: fov * 10,
      pixel_scale_arcsec: 6, orientation: 'north up, east left', wcs_header: 'RA---TAN / DEC--TAN' },
    display: { ...originalScale },
    candidate_count: 9, attempted_count: 2, skipped_count: 7, failures: [], limitations: [],
    frames: sourceUrls.map((url, index) => ({
      index, band: 'D4', detector: 'SPHEREx-D4', source_url: url,
      product_id: `p${index}.fits`,
      date_utc: index ? '2025-11-08 21:05:00.000' : '2025-05-07 03:12:00.000',
      center_wavelength_um: 3.05 + index * 0.01, coverage_fraction: 1,
      image_url: `/outputs/${id}/${zoom ? `zoom_${String(fov).padStart(3, '0')}/` : ''}frame_${String(index).padStart(3, '0')}.png`,
    })),
  };
}
function preparation(band4 = manifest(20)) {
  return { job_id: jobId, status: 'complete', requested_band: 'D4',
    canonical_fov_arcmin: 20, completed_count: 6, total_count: 6,
    bands: Object.fromEntries(Array.from({ length: 6 }, (_, i) => [
      `D${i + 1}`, i === 3 ? { status: 'ready', manifest: band4 } : { status: 'empty' },
    ])) };
}

test('same-parent zoom keeps exposure order, wavelength, and brightness and rejects substitutions', async () => {
  const base = normalizeSequencePatch(manifest(5), parseCoordinates('12', '75'), 4,
    { from: '2025-05-01', to: '2025-11-30' }, 5);
  let zoomCalls = 0;
  const fetchImpl = async (url, options) => {
    zoomCalls++;
    assert.equal(url, '/api/zoom');
    assert.deepEqual(JSON.parse(options.body), { sequence_id: id, fov_arcmin: 20 });
    return new Response(JSON.stringify(manifest(20, { zoom: true })), { status: 200 });
  };
  const wide = await fetchZoomedRealPatch({ patch: base, fov: 20, fetchImpl });
  assert.equal(zoomCalls, 1);
  assert.deepEqual(wide.frames.map(f => f.dateId), base.frames.map(f => f.dateId));
  assert.deepEqual(wide.frames.map(f => f.sourceUrl), sourceUrls);
  assert.deepEqual(wide.frames.map(f => f.productId), ['p0.fits', 'p1.fits']);
  assert.deepEqual(wide.frames.map(f => f.wavelengthUm), base.frames.map(f => f.wavelengthUm));
  assert.deepEqual(wide.display, base.display);
  assert.equal(wide.baseFovArcmin, 20);
  assert.equal(wide.grid.widthPixels, 200);

  const altered = manifest(20, { zoom: true });
  altered.frames[1].source_url = 'https://irsa.ipac.caltech.edu/replacement.fits';
  await assert.rejects(fetchZoomedRealPatch({ patch: base, fov: 20,
    fetchImpl: async () => new Response(JSON.stringify(altered)) }),
  error => error.code === 'zoom_changed_selection');
  altered.frames[1].source_url = sourceUrls[1];
  altered.display.vmax = 100;
  await assert.rejects(fetchZoomedRealPatch({ patch: base, fov: 20,
    fetchImpl: async () => new Response(JSON.stringify(altered)) }),
  error => error.code === 'zoom_changed_selection');
});

test('shared zoom URL restores its original sequence without a new archive search', async () => {
  const restored = await restoreRealSequence({ sequenceId: id, ra: '12', dec: '75', band: 4, fov: 10,
    fetchImpl: async (url, options) => {
      assert.equal(url, '/api/zoom');
      assert.equal(JSON.parse(options.body).fov_arcmin, 10);
      return new Response(JSON.stringify(manifest(10, { zoom: true })));
    } });
  assert.equal(restored.sequenceId, id);
  assert.equal(restored.baseFovArcmin, 10);
  assert.equal(restored.frames.length, 2);
});

test('unavailable outer parent pixels remain masked and selected frames stay in the timeline', async () => {
  const base = normalizeSequencePatch(manifest(5), parseCoordinates('12', '75'), 4,
    { from: '2025-05-01', to: '2025-11-30' }, 5);
  const wider = manifest(20, { zoom: true });
  wider.status = 'partial';
  wider.zoom_missing_outer_count = 1;
  wider.frames[0].coverage_fraction = 0.0625;
  wider.frames[0].zoom_outer_region_status = 'unavailable';
  wider.frames[0].zoom_note = 'Outer pixels unavailable from the selected parent exposure.';
  const result = await fetchZoomedRealPatch({ patch: base, fov: 20,
    fetchImpl: async () => new Response(JSON.stringify(wider)) });
  assert.equal(result.frames.length, base.frames.length);
  assert.equal(result.zoomMissingOuterCount, 1);
  assert.ok(result.frames[0].flags.some(flag => flag.code === 'zoom_outer_unavailable' && flag.label.includes('Outer pixels unavailable')));
  assert.ok(result.warnings.some(warning => warning.includes('Outer sky pixels')));
  assert.ok(result.frames.every(frame => !frame.flags.some(flag => flag.code === 'partial_sequence')));
});

async function mount(fov, fetchImpl, { sequenceId = '' } = {}) {
  const listeners = {};
  const app = { innerHTML: '', addEventListener(type, cb) { (listeners[type] ||= []).push(cb); },
    querySelector() { return null; } };
  globalThis.location = new URL(`http://localhost:5173/?ra=12&dec=75&band=4&fov=${fov}&from=2025-05-01&to=2025-11-30${sequenceId ? `&sequence=${sequenceId}` : ''}`);
  globalThis.history = { replaceState(_s, _t, url) { globalThis.location = new URL(url); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  globalThis.window = { addEventListener() {} };
  globalThis.fetch = fetchImpl;
  await import(`../src/app.js?zoom=${Math.random()}`);
  const click = (action, extra = {}) => listeners.click.forEach(cb => cb({ target: {
    closest: () => ({ dataset: { action, ...extra }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  return { app, click };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('20′ to 10′ to 5′ zoom crops one image set locally, with fixed frame count, selected product and stretch', async () => {
  const calls = [];
  const originalLog = console.info;
  const logs = [];
  console.info = (...args) => logs.push(args);
  try {
    const { app, click } = await mount(20, async (url, options) => {
      calls.push(String(url));
      assert.equal(url, '/api/prepare');
      assert.deepEqual(JSON.parse(options.body), { ra_deg: 12, dec_deg: 75,
        start_date: '2025-05-01', end_date: '2025-11-30', band: 'D4' });
      return new Response(JSON.stringify(preparation()));
    });
    await tick();
    assert.equal(calls.length, 1);
    assert.equal(globalThis.location.searchParams.get('sequence'), id);
    click('next');
    const selected = globalThis.location.searchParams.get('date');
    assert.match(app.innerHTML, /p1\.fits/);
    click('fov', { fov: '10' });
    assert.equal(calls.length, 1);
    assert.match(app.innerHTML, /Showing 2 of 2 loaded frames/);
    assert.match(app.innerHTML, /--zoom:2/);
    assert.match(app.innerHTML, /10′ wide/);
    assert.equal(globalThis.location.searchParams.get('date'), selected);
    click('fov', { fov: '5' });
    assert.equal(calls.length, 1);
    assert.match(app.innerHTML, /--zoom:4/);
    assert.match(app.innerHTML, /\/outputs\/d{32}\/frame_001\.png/);
    assert.match(app.innerHTML, /Showing 2 of 2 loaded frames/);
    assert.equal(globalThis.location.searchParams.get('date'), selected);
    assert.equal(logs.length, 2);
    const [first, second] = logs.map(item => item[1]);
    assert.equal(first.before.product_id, 'p1.fits');
    assert.equal(first.after.product_id, 'p1.fits');
    assert.deepEqual(first.before.displayed_science_pixel_dimensions.width, 200);
    assert.deepEqual(second.after.displayed_science_pixel_dimensions.width, 50);
    assert.deepEqual(first.after.brightness_scale, second.after.brightness_scale);
    assert.equal(second.selected_product_unchanged, true);
  } finally { console.info = originalLog; }
});

test('initial 5′ view preloads the 20′ master, then zooms out and in without another network request', async () => {
  const originalLog = console.info;
  console.info = () => {};
  try {
  const calls = [];
  const { app, click } = await mount(5, async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    assert.equal(url, '/api/prepare');
    return new Response(JSON.stringify(preparation()));
  });
  await tick();
  assert.equal(calls.length, 1);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
  assert.match(app.innerHTML, /--zoom:4/);
  click('fov', { fov: '20' });
  assert.deepEqual(calls.map(c => c.url), ['/api/prepare']);
  assert.match(app.innerHTML, /--zoom:1/);
  click('next');
  const selected = globalThis.location.searchParams.get('date');
  assert.equal(globalThis.location.searchParams.get('date'), selected);
  assert.equal(globalThis.location.searchParams.get('fov'), '20');
  assert.equal(globalThis.location.searchParams.get('sequence'), id);
  assert.match(app.innerHTML, /Showing 2 of 2 loaded frames/);
  assert.match(app.innerHTML, /\/outputs\/d{32}\/frame_001\.png/);
  click('fov', { fov: '10' });
  assert.equal(calls.length, 1);
  assert.match(app.innerHTML, /--zoom:2/);
  assert.match(app.innerHTML, /p1\.fits/);
  assert.equal(globalThis.location.searchParams.get('date'), selected);
  } finally { console.info = originalLog; }
});

test('rapid 5′ → 20′ → 10′ clicks reuse the prepared 20′ master with no re-query', async () => {
  const originalLog = console.info;
  console.info = () => {};
  try {
    const calls = [];
    const { app, click } = await mount(5, async url => {
      calls.push(url);
      assert.equal(url, '/api/prepare');
      return new Response(JSON.stringify(preparation()));
    });
    await tick();
    click('fov', { fov: '20' });
    click('fov', { fov: '10' });
    assert.deepEqual(calls, ['/api/prepare']);
    assert.equal(globalThis.location.searchParams.get('fov'), '10');
    assert.match(app.innerHTML, /--zoom:2/);
    assert.match(app.innerHTML, /Showing 2 of 2 loaded frames/);
    assert.equal(calls.length, 1);
  } finally { console.info = originalLog; }
});

test('a linked sequence stays selected when six-band preparation finds a different product', async () => {
  const originalLog = console.info;
  console.info = () => {};
  try {
    const replacement = manifest(20);
    replacement.sequence_id = 'e'.repeat(32);
    replacement.frames.forEach((frame, i) => {
      frame.source_url = `https://irsa.ipac.caltech.edu/replacement-${i}.fits`;
      frame.product_id = `replacement-${i}.fits`;
      frame.image_url = `/outputs/${replacement.sequence_id}/frame_${String(i).padStart(3, '0')}.png`;
    });
    const calls = [];
    const { app, click } = await mount(5, async (url, options) => {
      calls.push(url);
      if (url === '/api/zoom') {
        assert.deepEqual(JSON.parse(options.body), { sequence_id: id, fov_arcmin: 20 });
        return new Response(JSON.stringify(manifest(20, { zoom: true })));
      }
      assert.equal(url, '/api/prepare');
      return new Response(JSON.stringify(preparation(replacement)));
    }, { sequenceId: id });
    await tick(); await tick();
    assert.deepEqual(calls, ['/api/zoom', '/api/prepare']);
    assert.equal(globalThis.location.searchParams.get('sequence'), id);
    assert.match(app.innerHTML, /p0\.fits/);
    assert.doesNotMatch(app.innerHTML, /replacement-0\.fits/);
    assert.match(app.innerHTML, /--zoom:4/);
    click('fov', { fov: '10' });
    assert.equal(calls.length, 2);
    assert.equal(globalThis.location.searchParams.get('sequence'), id);
    assert.match(app.innerHTML, /\/outputs\/d{32}\/zoom_020\/frame_000\.png/);
  } finally { console.info = originalLog; }
});
