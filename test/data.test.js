import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseCoordinates, loadDemoPatch, fetchRealPatch, normalizeRealPatch, PatchDataError, WISE_0855 } from '../src/data.js';

test('sexagesimal and decimal coordinates agree, with bad values rejected', () => {
  const sexagesimal = parseCoordinates(WISE_0855.ra, WISE_0855.dec);
  const decimal = parseCoordinates('133.79512917', '-7.24514722');
  assert.equal(sexagesimal.isValid, true);
  assert.ok(Math.abs(sexagesimal.raDeg - decimal.raDeg) < 1e-6);
  assert.ok(Math.abs(sexagesimal.decDeg - decimal.decDeg) < 1e-6);
  assert.equal(parseCoordinates('25h00m00s', '0').isValid, false);
  assert.equal(parseCoordinates('08h55m10s', '-91').isValid, false);
  assert.equal(parseCoordinates('133.80', '-7.2451').isWiseExample, false);
});

test('synthetic WISE example is locally sourced and target limited', async () => {
  const manifest = JSON.parse(await readFile(new URL('../public/demo/frames.json', import.meta.url), 'utf8'));
  const fetchImpl = async url => {
    assert.equal(url, './public/demo/frames.json');
    return new Response(JSON.stringify(manifest), { status: 200 });
  };
  const patch = await loadDemoPatch({ fetchImpl });
  assert.equal(patch.source, 'synthetic');
  assert.equal(patch.frames.length, 18);
  assert.ok(patch.frames.every(f => f.imageUrl.startsWith('./public/demo/frames/')));
  await assert.rejects(loadDemoPatch({ ra: '110', dec: '12', fetchImpl }),
    error => error instanceof PatchDataError && error.code === 'demo_target_unavailable');
});

test('real adapter sends decimal coordinates and refuses unverified source', async () => {
  let requested = '';
  const payload = {
    status: 'ok', source: 'real',
    dates: [{ id: '2025-05-07', observedAt: '2025-05-07T03:12:00Z' }],
    frames: [{ dateId: '2025-05-07', band: 4, wavelengthUm: null, imageUrl: '/api/images/frame.png', flags: [] }],
  };
  const fetchImpl = async url => { requested = url; return new Response(JSON.stringify(payload), { status: 200 }); };
  const patch = await fetchRealPatch({ ra: WISE_0855.ra, dec: WISE_0855.dec, fov: 5, fetchImpl });
  assert.match(requested, /^\/api\/patch\?ra=133\.795/);
  assert.match(requested, /fov=5/);
  assert.equal(patch.frames[0].wavelengthUm, null);
  assert.equal(patch.source, 'real');
  assert.throws(() => normalizeRealPatch({ ...payload, source: 'synthetic' }, patch.target),
    error => error.code === 'invalid_response');
});
