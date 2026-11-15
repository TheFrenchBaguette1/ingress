import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const sample = await readFile(new URL('../public/real-example/ra180-dec-30-d4/manifest.json', import.meta.url), 'utf8');
const archiveError = () => new Response(JSON.stringify({
  detail: "IRSA's SPHEREx image search is temporarily unavailable. Please try again shortly.",
}), { status: 502 });
const flush = () => new Promise(resolve => setImmediate(resolve));

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
  assert.deepEqual(calls.map(call => call.url), ['/api/sequence', './public/real-example/ra180-dec-30-d4/manifest.json']);
  assert.match(app.innerHTML, /Saved real SPHEREx observations/);
  assert.match(app.innerHTML, /fixed snapshot, not a live search or simulated images/);
  assert.match(app.innerHTML, /frame_000\.png/);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
  assert.doesNotMatch(app.innerHTML, /Synthetic demo — not SPHEREx observations/);

  click('band', { band: '3' });
  await flush(); await flush();
  assert.equal(calls.length, 3); // No narrower bundled replacement for Band 3.
  assert.match(app.innerHTML, /Showing previously loaded real observations/);
  assert.match(app.innerHTML, /new search \(Band 3, 5′ wide\) did not load/);
  assert.equal(globalThis.location.searchParams.get('band'), '4');
  click('retry-failed-query');
  await flush(); await flush();
  assert.equal(calls.length, 4);
  assert.equal(calls[3].body.band, 'D3');
  assert.match(app.innerHTML, /Showing previously loaded real observations/);

  click('fov', { fov: '20' });
  await flush(); await flush();
  assert.equal(calls.length, 5);
  assert.equal(calls[4].body.fov_arcmin, 20);
  assert.match(app.innerHTML, /new search \(Band 4, 20′ wide\) did not load/);
  assert.equal(globalThis.location.searchParams.get('fov'), '5');
  click('retry-failed-query');
  await flush(); await flush();
  assert.equal(calls[5].body.fov_arcmin, 20);
  assert.equal(calls.length, 6);
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
  assert.deepEqual(calls.map(call => call.url), ['/api/sequence']);
  assert.match(app.innerHTML, /Could not load this patch/);
  assert.match(app.innerHTML, /View saved real observations \(5′\)/);
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
      assert.equal(JSON.parse(options.body).band, 'D4');
      return new Response(JSON.stringify(partial), { status: 200 });
    });
  await flush();
  assert.equal(calls, 1);
  assert.match(app.innerHTML, /Partial date coverage/);
  assert.doesNotMatch(app.innerHTML, /0 exposures could not be loaded/);
  assert.doesNotMatch(app.innerHTML, /Some observations could not be loaded for this sequence/);
  click('retry');
  await flush();
  assert.equal(calls, 2);
});
