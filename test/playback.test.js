import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('playback waits for real images, changes speed, and scrubs without replacing controls', async t => {
  const listeners = {};
  const makeElement = () => ({
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
  });
  const image = makeElement();
  const playback = makeElement();
  const scrubber = makeElement();
  const ordinal = makeElement();
  const nodes = new Map([
    ['[data-frame-image]', image],
    ['.image-view', makeElement()],
    ['.frame-date', makeElement()],
    ['.frame-ordinal', ordinal],
    ['[data-frame-wavelength]', makeElement()],
    ['[data-frame-coverage]', makeElement()],
    ['[data-range="frame"]', scrubber],
    ['[data-action="play"]', playback],
  ]);
  let html = '';
  let renders = 0;
  const app = {
    set innerHTML(value) { html = value; renders++; },
    addEventListener(type, callback) { (listeners[type] ||= []).push(callback); },
    querySelector(selector) { return nodes.get(selector) || null; },
    querySelectorAll() { return []; },
  };
  globalThis.location = new URL('https://example.github.io/ingress/');
  globalThis.history = {
    replaceState(_state, _title, url) { globalThis.location = new URL(url); },
  };
  globalThis.document = {
    querySelector: selector => selector === '#app' ? app : { getAttribute: () => 'true' },
    addEventListener() {},
    hidden: false,
  };
  globalThis.window = { addEventListener() {} };
  const manifest = await readFile(new URL('../public/real-example/ra180-dec-30-d4/manifest.json',
    import.meta.url), 'utf8');
  globalThis.fetch = async url => {
    assert.equal(String(url), './public/real-example/ra180-dec-30-d4/manifest.json');
    return new Response(manifest);
  };
  globalThis.CSS = { escape: value => value };
  const decode = [];
  globalThis.Image = class {
    decode() { return new Promise(resolve => decode.push(resolve)); }
  };
  const intervals = [];
  t.mock.method(globalThis, 'setInterval', (callback, delay) => {
    const timer = { callback, delay, cleared: false };
    intervals.push(timer);
    return timer;
  });
  t.mock.method(globalThis, 'clearInterval', timer => { timer.cleared = true; });

  await import('../src/app.js?playback-controls');
  await new Promise(resolve => setImmediate(resolve));
  const initialRenders = renders;
  assert.match(html, /Frame 1 of 8/);

  const click = action => listeners.click.forEach(listener => listener({ target: {
    closest: () => ({ dataset: { action }, tagName: 'BUTTON' }),
    matches: () => false,
  } }));
  const move = (type, range, value) => listeners[type].forEach(listener => listener({
    target: { dataset: { range }, value },
  }));

  click('play');
  assert.equal(playback.attributes['aria-label'], 'Pause timeline');
  assert.equal(intervals[0].delay, 1000 / 6);
  intervals[0].callback();
  assert.equal(image.src, undefined);
  decode.forEach(resolve => resolve());
  await new Promise(resolve => setImmediate(resolve));
  intervals[0].callback();
  assert.match(image.src, /frame_001\.png$/);
  assert.match(ordinal.textContent, /Frame 2 of 8/);
  assert.equal(scrubber.value, 2);
  assert.equal(renders, initialRenders);

  move('change', 'playback-rate', '20');
  assert.equal(intervals[0].cleared, true);
  assert.equal(intervals[1].delay, 1000 / 20);
  intervals[1].callback();
  assert.match(image.src, /frame_002\.png$/);
  move('input', 'frame', '8');
  move('change', 'frame', '8');
  assert.equal(intervals[1].cleared, true);
  assert.equal(playback.attributes['aria-label'], 'Play timeline');
  assert.match(image.src, /frame_007\.png$/);
  assert.match(scrubber.attributes['aria-valuetext'], /Frame 8 of 8/);
  assert.match(location.searchParams.get('date'), /^2026-06-25/);
  assert.equal(renders, initialRenders);
});
