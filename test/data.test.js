import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseCoordinates, loadBundledRealPatch, fetchRealPatch, normalizeSequencePatch, PatchDataError,
  retryWhileBusy, startFlipbook, requestFlipbookBand } from '../src/data.js';

const WISE_0855 = {
  ra: '08h55m10.831s', dec: '-07d14m42.53s',
  raDeg: 133.79512917, decDeg: -7.24514722,
};

test('sexagesimal and decimal coordinates agree, with bad values rejected', () => {
  const sexagesimal = parseCoordinates(WISE_0855.ra, WISE_0855.dec);
  const decimal = parseCoordinates('133.79512917', '-7.24514722');
  assert.equal(sexagesimal.isValid, true);
  assert.ok(Math.abs(sexagesimal.raDeg - decimal.raDeg) < 1e-6);
  assert.ok(Math.abs(sexagesimal.decDeg - decimal.decDeg) < 1e-6);
  assert.equal(parseCoordinates('25h00m00s', '0').isValid, false);
  assert.equal(parseCoordinates('08h55m10s', '-91').isValid, false);
});

test('saved real example preserves fixed sky, band, dates, and actual source links', async () => {
  const manifest = JSON.parse(await readFile(new URL('../public/real-example/ra180-dec-30-d4/manifest.json', import.meta.url), 'utf8'));
  const fetchImpl = async url => {
    assert.equal(url, './public/real-example/ra180-dec-30-d4/manifest.json');
    return new Response(JSON.stringify(manifest), { status: 200 });
  };
  const patch = await loadBundledRealPatch({ fetchImpl });
  assert.equal(patch.source, 'real');
  assert.equal(patch.bundledRealExample, true);
  assert.equal(patch.frames.length, 8);
  assert.deepEqual(patch.queryRange, { from: '2025-05-01', to: '2026-11-15' });
  const projectUrl = new URL('https://example.test/ingress/?example=saved');
  assert.ok(patch.frames.every(frame => frame.band === 4 &&
    new URL(frame.imageUrl, projectUrl).pathname.startsWith('/ingress/public/real-example/ra180-dec-30-d4/') &&
    frame.archiveUrl.startsWith('https://irsa.ipac.caltech.edu/')));

  const unsafe = structuredClone(manifest);
  unsafe.frames[0].image_url = '/public/other-example/frame_000.png';
  await assert.rejects(loadBundledRealPatch({ fetchImpl: async () =>
    new Response(JSON.stringify(unsafe), { status: 200 }) }),
  error => error.code === 'example_unavailable');
});

function sequenceManifest(status = 'partial', requestOverride = {}) {
  return {
    status, sequence_id: 'a'.repeat(32),
    request: { ra_deg: WISE_0855.raDeg, dec_deg: WISE_0855.decDeg, band: 'D4',
      fov_arcmin: 5, start_date: '2025-05-01', end_date: new Date().toISOString().slice(0, 10),
      ...requestOverride },
    grid: { width_pixels: 50, pixel_scale_arcsec: 6, orientation: 'north up, east left' },
    candidate_count: 102, attempted_count: 3, skipped_count: 99,
    limitations: ['Instrument quality flags have not yet been applied.', 'PNG values are for visualization, not photometry.'],
    failures: status === 'partial' ? [{ error: 'archive timeout' }] : [],
    frames: status === 'empty' ? [] : [
      { index: 0, band: 'D4', date_utc: '2025-05-07 03:12:00.000', image_url: `/outputs/${'a'.repeat(32)}/frame_000.png`,
        source_url: 'https://irsa.ipac.caltech.edu/one', center_wavelength_um: 3.23, coverage_fraction: 0.8 },
      { index: 1, band: 'D4', date_utc: '2025-05-07 21:05:00.000', image_url: `/outputs/${'a'.repeat(32)}/frame_001.png`,
        source_url: 'https://irsa.ipac.caltech.edu/two', center_wavelength_um: null, coverage_fraction: 1 },
    ],
  };
}

test('real adapter posts a single detector and maps backend frames without losing same-day observations', async () => {
  let requested = '';
  let body;
  const fetchImpl = async (url, options) => {
    requested = url;
    assert.equal(options.method, 'POST');
    body = JSON.parse(options.body);
    return new Response(JSON.stringify(sequenceManifest()), { status: 200 });
  };
  const patch = await fetchRealPatch({ ra: WISE_0855.ra, dec: WISE_0855.dec, band: 4, fetchImpl });
  assert.equal(requested, '/api/sequence');
  assert.ok(Math.abs(body.ra_deg - WISE_0855.raDeg) < 1e-6);
  assert.equal(body.fov_arcmin, 5);
  assert.equal(body.band, 'D4');
  assert.equal(body.start_date, '2025-05-01');
  assert.equal(body.end_date, new Date().toISOString().slice(0, 10));
  assert.deepEqual(patch.queryRange, { from: body.start_date, to: body.end_date });
  assert.equal(patch.source, 'real');
  assert.equal(patch.status, 'ok');
  assert.equal(patch.backendStatus, 'partial');
  assert.equal(patch.cachedPartial, false);
  assert.equal(patch.bands.length, 6);
  assert.deepEqual(patch.bands.map(band => band.rangeUm), [
    [0.744, 1.116], [1.099, 1.651], [1.636, 2.421],
    [2.423, 3.822], [3.809, 4.420], [4.412, 5.002],
  ]);
  assert.equal(patch.candidateCount, 102);
  assert.equal(patch.attemptedCount, 3);
  assert.equal(patch.skippedCount, 99);
  assert.equal(patch.frames.length, 2);
  assert.notEqual(patch.frames[0].dateId, patch.frames[1].dateId);
  const changedOrder = sequenceManifest();
  changedOrder.frames[0].index = 99;
  assert.equal(normalizeSequencePatch(changedOrder, parseCoordinates(WISE_0855.ra, WISE_0855.dec),
    4, patch.queryRange, 5).frames[0].dateId, patch.frames[0].dateId);
  assert.equal(patch.dates[0].id, patch.frames[0].dateId);
  assert.equal(patch.frames[1].wavelengthUm, null);
  assert.ok(patch.frames.every(f => f.imageUrl.startsWith('/outputs/') && f.flags.some(flag => flag.code === 'quality_unchecked')));
  assert.ok(patch.frames[0].flags.some(flag => flag.code === 'partial_coverage'));
  assert.ok(patch.frames[0].flags.some(flag => flag.code === 'partial_sequence'));
  assert.equal(patch.failureCount, 1);
  assert.ok(patch.warnings.some(w => w.includes('could not be used')));

  const cachedManifest = sequenceManifest();
  cachedManifest.cached_partial = true;
  const refreshed = await fetchRealPatch({ ra: WISE_0855.ra, dec: WISE_0855.dec,
    band: 4, refresh: true, fetchImpl: async (url, options) => {
      assert.equal(url, '/api/sequence?refresh=1');
      assert.deepEqual(JSON.parse(options.body), body);
      return new Response(JSON.stringify(cachedManifest), { status: 200 });
    } });
  assert.equal(refreshed.cachedPartial, true);
});

test('archive empty, failed, and busy states remain distinct', async () => {
  const coords = parseCoordinates(WISE_0855.ra, WISE_0855.dec);
  const empty = sequenceManifest('empty', { end_date: '2025-06-01' });
  assert.equal(normalizeSequencePatch(empty, coords, 4, { from: '2025-05-01', to: '2025-06-01' }).status, 'empty');
  empty.failures = [{ error: 'timeout' }];
  assert.throws(() => normalizeSequencePatch(empty, coords, 4, { from: '2025-05-01', to: '2025-06-01' }),
    error => error instanceof PatchDataError && error.code === 'unavailable');
  assert.throws(() => normalizeSequencePatch(sequenceManifest('ready'), coords, 3, {}),
    error => error instanceof PatchDataError && error.code === 'invalid_response');
  assert.throws(() => normalizeSequencePatch(sequenceManifest('ready'), coords, 4, {}, 10),
    error => error instanceof PatchDataError && error.code === 'invalid_response');
  const fetchImpl = async () => new Response(JSON.stringify({ detail: 'A sequence is already being built.' }), { status: 409 });
  await assert.rejects(fetchRealPatch({ ra: WISE_0855.ra, dec: WISE_0855.dec, fetchImpl }),
    error => error.code === 'busy' && error.message.includes('already being built'));
});

test('frame details report only known archive quality flags and mask status', () => {
  const manifest = sequenceManifest('ready');
  const coords = parseCoordinates(WISE_0855.ra, WISE_0855.dec);
  Object.assign(manifest.frames[0], {
    quality_mask_applied: true, center_flag_names: ['SOURCE', '', null, 'HOT', 'SOURCE'],
  });
  Object.assign(manifest.frames[1], { quality_mask_applied: true, center_flag_names: [] });

  const patch = normalizeSequencePatch(manifest, coords, 4, {}, 5);
  assert.ok(!patch.warnings.some(warning => warning.includes('have not yet been applied')));
  assert.deepEqual(patch.frames[0].flags.find(flag => flag.code === 'archive_flags'), {
    code: 'archive_flags', label: 'Archive quality flags at the target: SOURCE, HOT.',
  });
  assert.ok(patch.frames.every(frame => !frame.flags.some(flag => flag.code === 'quality_unchecked')));
  assert.ok(!patch.frames[1].flags.some(flag => flag.code === 'archive_flags'));

  delete manifest.frames[1].quality_mask_applied;
  const mixed = normalizeSequencePatch(manifest, coords, 4, {}, 5);
  assert.ok(!mixed.warnings.some(warning => warning.includes('have not yet been applied')));
  assert.ok(!mixed.frames[0].flags.some(flag => flag.code === 'quality_unchecked'));
  assert.ok(mixed.frames[1].flags.some(flag => flag.code === 'quality_unchecked'));
});

test('archive outage and disconnected backend have distinct failure codes', async () => {
  const input = { ra: '180', dec: '-30' };
  await assert.rejects(fetchRealPatch({ ...input, fetchImpl: async () =>
    new Response(JSON.stringify({ detail: "IRSA's SPHEREx image search is temporarily unavailable." }), { status: 502 }) }),
  error => error.code === 'archive_unavailable');
  await assert.rejects(fetchRealPatch({ ...input, fetchImpl: async () =>
    new Response(JSON.stringify({ detail: 'The SPHEREx image search did not respond in time for this patch.' }), { status: 502 }) }),
  error => error.code === 'archive_unavailable');
  await assert.rejects(fetchRealPatch({ ...input, fetchImpl: async () =>
    new Response(JSON.stringify({ message: 'The real-data backend is not connected yet.' }), { status: 503 }) }),
  error => error.code === 'backend_unavailable');
  await assert.rejects(fetchRealPatch({ ...input, fetchImpl: async () => { throw new TypeError('network'); } }),
  error => error.code === 'backend_unavailable');
});

test('partial archive listing does not falsely say any cutouts failed', () => {
  const manifest = sequenceManifest('partial');
  manifest.failures = [];
  manifest.archive_listing_partial = true;
  const patch = normalizeSequencePatch(manifest,
    parseCoordinates(WISE_0855.ra, WISE_0855.dec), 4,
    { from: '2025-05-01', to: new Date().toISOString().slice(0, 10) }, 5);
  assert.equal(patch.failureCount, 0);
  assert.equal(patch.archiveListingPartial, true);
  assert.ok(patch.frames.every(frame => frame.flags.some(flag =>
    flag.code === 'partial_sequence' && flag.label.includes('only part of the requested date range'))));
  assert.ok(patch.frames.every(frame => frame.flags.every(flag =>
    !flag.label.includes('could not be loaded'))));
});

test('edge-only cached images cannot masquerade as a useful target sequence', () => {
  const coords = parseCoordinates(WISE_0855.ra, WISE_0855.dec);
  const manifest = sequenceManifest('ready');
  manifest.frames[0].coverage_fraction = 0.04;
  const patch = normalizeSequencePatch(manifest, coords, 4, {}, 5);
  assert.equal(patch.frames.length, 1);
  assert.equal(patch.frames[0].dateId.split('#')[0], '2025-05-07T21:05:00.000Z');
  assert.ok(patch.warnings.some(warning => warning.includes('edge-only observation')));

  // This older wide-view image has 35% pixels but a blank target crosshair.
  manifest.frames[1].coverage_fraction = 0.35;
  assert.throws(() => normalizeSequencePatch(manifest, coords, 4, {}, 5),
    error => error instanceof PatchDataError && error.code === 'low_coverage');
});

test('verified playable-frame selection retains sparse and unmeasured-wavelength exposures', () => {
  const coords = parseCoordinates(WISE_0855.ra, WISE_0855.dec);
  const manifest = sequenceManifest('ready');
  Object.assign(manifest.frames[0], { coverage_fraction: 0.10, center_wavelength_um: null });
  Object.assign(manifest.frames[1], { coverage_fraction: 0.35, center_wavelength_um: null });
  manifest.display = { missing_pixels: 'transparent' };
  assert.throws(() => normalizeSequencePatch(manifest, coords, 4, {}, 5),
    error => error instanceof PatchDataError && error.code === 'low_coverage');

  manifest.frame_filter = 'henry-playable-v1';
  const patch = normalizeSequencePatch(manifest, coords, 4, {}, 5);
  assert.deepEqual(patch.frames.map(frame => frame.coverageFraction), [0.10, 0.35]);
  assert.deepEqual(patch.frames.map(frame => frame.imageUrl), manifest.frames.map(frame => frame.image_url));
  assert.ok(patch.frames.every(frame => frame.wavelengthUm === null &&
    frame.flags.some(flag => flag.code === 'partial_coverage')));
  assert.equal(patch.display.missing_pixels, 'transparent');
});

test('a busy build is retried with the same request, and cancellation stops retries', async () => {
  let calls = 0;
  let waits = 0;
  let busy = 0;
  const fetchImpl = async (url, options) => {
    calls++;
    assert.equal(url, '/api/sequence');
    assert.equal(JSON.parse(options.body).dec_deg, -85);
    return calls <= 2
      ? new Response(JSON.stringify({ detail: 'A sequence is already being built.' }), { status: 409 })
      : new Response(JSON.stringify(sequenceManifest('ready', { ra_deg: 120, dec_deg: -85 })), { status: 200 });
  };
  const patch = await retryWhileBusy(() => fetchRealPatch({ ra: '120', dec: '-85', fov: 5,
    band: 4, fetchImpl }), { wait: async () => { waits++; }, onBusy: () => { busy++; } });
  assert.equal(patch.source, 'real');
  assert.equal(calls, 3);
  assert.equal(waits, 2);
  assert.equal(busy, 2);

  const controller = new AbortController();
  let cancelledCalls = 0;
  await assert.rejects(retryWhileBusy(async () => {
    cancelledCalls++;
    throw new PatchDataError('busy', 'Already building');
  }, { signal: controller.signal, onBusy: () => controller.abort() }),
  error => error.name === 'AbortError');
  assert.equal(cancelledCalls, 1);
});

test('busy retries stop after 30 five-second waits by default', async () => {
  let calls = 0;
  let waits = 0;
  await assert.rejects(retryWhileBusy(async () => {
    calls++;
    throw new PatchDataError('busy', 'Already building');
  }, { wait: async () => { waits++; } }),
  error => error.code === 'busy_timeout');
  assert.equal(calls, 31);
  assert.equal(waits, 30);
});

test('flipbook admission retries only queue saturation with the original request and server delay', async () => {
  const requests = [];
  const delays = [];
  const notices = [];
  const jobId = 'a'.repeat(32);
  const { payload } = await startFlipbook({ ra: '12', dec: '75', band: 'all', refresh: true,
    wait: async ms => { delays.push(ms); },
    onBusy: attempt => { notices.push(attempt); },
    fetchImpl: async (path, { body }) => {
      assert.equal(path, '/api/flipbook?refresh=1');
      requests.push(body);
      if (requests.length === 1) return new Response(JSON.stringify({
        detail: { code: 'flipbook_busy', message: 'Queue is full' },
      }), { status: 503, headers: { 'Retry-After': '4' } });
      if (requests.length === 2) return new Response(JSON.stringify({
        detail: 'The archive queue is full. Try again in a moment.',
      }), { status: 503 });
      return new Response(JSON.stringify({
        job_id: jobId, status: 'running', canonical_fov_arcmin: 20,
        bands: Object.fromEntries([1, 2, 3, 4, 5, 6].map(band => [`D${band}`, { status: 'queued' }])),
      }));
    } });
  assert.equal(payload.job_id, jobId);
  assert.deepEqual(notices, [1, 2]);
  assert.deepEqual(delays, [4000, 3000]);
  assert.equal(new Set(requests).size, 1);
  assert.deepEqual(JSON.parse(requests[0]).band, 'all');

  let attempts = 0;
  await assert.rejects(requestFlipbookBand(jobId, 1, {
    fetchImpl: async () => {
      attempts++;
      return new Response(JSON.stringify({ detail: 'Service unavailable' }),
        { status: 503, headers: { 'Retry-After': '3' } });
    },
    wait: async () => assert.fail('A generic outage is not queue saturation.'),
  }), error => error.code === 'backend_unavailable' && error.message === 'Service unavailable');
  assert.equal(attempts, 1);
});

test('flipbook queue retries are bounded and stop promptly after cancellation', async () => {
  let attempts = 0;
  const delays = [];
  const fetchImpl = async () => {
    attempts++;
    return new Response(JSON.stringify({ detail: { code: 'flipbook_busy' } }),
      { status: 503, headers: { 'Retry-After': '7' } });
  };
  const jobId = 'a'.repeat(32);
  await assert.rejects(requestFlipbookBand(jobId, 1, {
    fetchImpl, wait: async ms => { delays.push(ms); },
  }), error => error.code === 'busy_timeout');
  assert.equal(attempts, 4);
  assert.deepEqual(delays, [7000, 7000, 7000]);

  const controller = new AbortController();
  await assert.rejects(requestFlipbookBand(jobId, 1, {
    fetchImpl, signal: controller.signal, onBusy: () => controller.abort(),
  }), error => error.name === 'AbortError');
  assert.equal(attempts, 5);
});
