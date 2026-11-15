import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const sample = await readFile(new URL('../public/real-example/ra180-dec-30-d4/manifest.json', import.meta.url), 'utf8');
const archiveError = () => new Response(JSON.stringify({
  detail: "IRSA's SPHEREx image search is temporarily unavailable. Please try again shortly.",
}), { status: 502 });
const flush = () => new Promise(resolve => setImmediate(resolve));

function completePreparation(original, body) {
  const manifest = structuredClone(original);
  manifest.request = { ...body, fov_arcmin: 20 };
  manifest.grid = { ...manifest.grid, width_pixels: 200, height_pixels: 200 };
  const bands = Object.fromEntries(Array.from({ length: 6 }, (_, index) =>
    [`D${index + 1}`, { status: 'empty' }]));
  bands[body.band] = { status: manifest.status, manifest };
  return new Response(JSON.stringify({ job_id: 'a'.repeat(32), status: 'complete',
    requested_band: body.band, canonical_fov_arcmin: 20, bands,
    completed_count: 6, total_count: 6 }), { status: 200 });
}

async function mount(url, fetchImpl) {
  const listeners = {};
  const windowListeners = {};
  const app = {
    innerHTML: '',
    addEventListener(type, callback) { (listeners[type] ||= []).push(callback); },
    querySelector() { return null; },
  };
  globalThis.location = new URL(url);
  globalThis.history = { replaceState(_state, _title, next) { globalThis.location = new URL(next); } };
  globalThis.document = { querySelector() { return app; }, addEventListener() {}, hidden: false };
  globalThis.window = { addEventListener(type, callback) { (windowListeners[type] ||= []).push(callback); } };
  globalThis.fetch = fetchImpl;
  await import(`../src/app.js?resilience=${Math.random()}`);
  const click = (action, extras = {}) => listeners.click.forEach(callback => callback({ target: {
    closest: () => ({ dataset: { action, ...extras }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  return { app, click, windowListeners };
}

test('an exact failed live request can display only its matching saved real observations', async () => {
  const calls = [];
  const { app, click } = await mount('http://localhost:5173/?ra=180&dec=-30&band=4&fov=5&from=2025-05-01&to=2026-11-15',
    async (url, options) => {
      calls.push({ url, body: options?.body && JSON.parse(options.body) });
      return String(url).endsWith('manifest.json')
        ? new Response(sample, { status: 200 }) : archiveError();
    });
  await flush(); await flush();
  assert.deepEqual(calls.map(call => call.url), ['/api/prepare', './public/real-example/ra180-dec-30-d4/manifest.json']);
  assert.match(app.innerHTML, /Saved real SPHEREx observations/);
  assert.match(app.innerHTML, /fixed snapshot, not a live search or simulated images/);
  assert.match(app.innerHTML, /frame_000\.png/);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
  assert.doesNotMatch(app.innerHTML, /Synthetic demo — not SPHEREx observations/);

  click('band', { band: '3' });
  await flush(); await flush();
  assert.equal(calls.length, 2); // The saved snapshot contains only Band 4.
  assert.match(app.innerHTML, /data-band="3"[^>]*disabled/);
  assert.doesNotMatch(app.innerHTML, /Band 3 did not load/);
  assert.equal(globalThis.location.searchParams.get('band'), '4');
  click('retry'); // The visible saved-example banner offers a live retry.
  await flush(); await flush();
  assert.equal(calls.length, 4);
  assert.equal(calls[2].url, '/api/prepare?refresh=1');
  assert.equal(calls[2].body.band, 'D4');
  assert.match(app.innerHTML, /Saved real SPHEREx observations/);

  click('fov', { fov: '20' });
  await flush(); await flush();
  assert.equal(calls.length, 4); // The saved 5′ snapshot has no parent science pixels.
  assert.match(app.innerHTML, /data-fov="20"[^>]*disabled/);
  assert.match(app.innerHTML, /5′ wide/);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
});

test('a 20-minute field never silently displays a 5-minute example, and a different patch can return', async () => {
  const calls = [];
  const { app, click, windowListeners } = await mount('http://localhost:5173/?ra=180&dec=-30&band=3&fov=20&from=2024-06-20&to=2026-11-15',
    async (url, options) => {
      calls.push({ url, body: options?.body && JSON.parse(options.body) });
      return String(url).endsWith('manifest.json')
        ? new Response(sample, { status: 200 }) : archiveError();
    });
  await flush(); await flush();
  assert.deepEqual(calls.map(call => call.url), ['/api/prepare']);
  assert.match(app.innerHTML, /Could not load this patch/);
  assert.match(app.innerHTML, /View saved real observations at RA 180°, Dec −30° \(5′\)/);
  assert.doesNotMatch(app.innerHTML, /frame_000\.png/);

  click('saved-real');
  await flush(); await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, './public/real-example/ra180-dec-30-d4/manifest.json');
  assert.match(app.innerHTML, /Saved real SPHEREx observations/);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
  assert.equal(globalThis.location.searchParams.get('band'), '4');

  globalThis.location = new URL('http://localhost:5173/?ra=181&dec=-30&band=4&fov=5&from=2025-05-01&to=2026-11-15');
  windowListeners.popstate.forEach(callback => callback());
  await flush(); await flush();
  assert.equal(calls.length, 3);
  assert.match(app.innerHTML, /Could not load this patch/);
  assert.match(app.innerHTML, /Return to last real view/);
  assert.doesNotMatch(app.innerHTML, /frame_000\.png/);
  click('return-real');
  assert.match(app.innerHTML, /Saved real SPHEREx observations/);
  assert.equal(globalThis.location.searchParams.get('ra'), '180');
});

test('a partial real sequence can probe the archive again and gives a truthful coverage note', async () => {
  const partial = JSON.parse(sample);
  partial.status = 'partial';
  partial.archive_listing_partial = true;
  partial.failures = [];
  for (const frame of partial.frames) {
    frame.image_url = frame.image_url.replace('/public/real-example/ra180-dec-30-d4/',
      `/outputs/${partial.sequence_id}/`);
  }
  let calls = 0;
  const { app, click } = await mount('http://localhost:5173/?ra=180&dec=-30&band=4&fov=5&from=2025-05-01&to=2026-11-15',
    async (_url, options) => {
      calls++;
      const body = JSON.parse(options.body);
      assert.equal(body.band, 'D4');
      return completePreparation(partial, body);
    });
  await flush();
  assert.equal(calls, 1);
  assert.match(app.innerHTML, /Partial date coverage/);
  assert.doesNotMatch(app.innerHTML, /0 exposures could not be loaded/);
  assert.doesNotMatch(app.innerHTML, /Some observations could not be loaded for this sequence/);
  click('refresh-observations');
  await flush();
  assert.equal(calls, 2);
});

test('a failed refresh keeps a live real sequence instead of replacing it with the older sample', async () => {
  const live = JSON.parse(sample);
  for (const frame of live.frames) {
    frame.image_url = frame.image_url.replace('/public/real-example/ra180-dec-30-d4/',
      `/outputs/${live.sequence_id}/`);
  }
  let calls = 0;
  const { app, click } = await mount('http://localhost:5173/?ra=180&dec=-30&band=4&fov=5&from=2025-05-01&to=2026-11-15',
    async (url, options) => {
      calls++;
      assert.equal(url, calls === 1 ? '/api/prepare' : '/api/prepare?refresh=1');
      return calls === 1 ? completePreparation(live, JSON.parse(options.body)) : archiveError();
    });
  await flush();
  assert.match(app.innerHTML, /Real SPHEREx observation|Loading real image/);
  click('retry');
  await flush(); await flush();
  assert.equal(calls, 2);
  assert.match(app.innerHTML, /Latest search did not load · still showing Band 4/);
  assert.doesNotMatch(app.innerHTML, /Saved real SPHEREx observations/);
  assert.match(app.innerHTML, /\/outputs\/0351a355746245378a187c58d3f0fb88\/frame_000\.png/);
});
