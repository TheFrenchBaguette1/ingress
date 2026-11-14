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
