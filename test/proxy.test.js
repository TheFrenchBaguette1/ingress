import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable, Writable } from 'node:stream';
import { finished } from 'node:stream/promises';
import { handleIngressRequest } from '../server.mjs';

function incoming(method, url, body = '') {
  const request = Readable.from(body ? [Buffer.from(body)] : []);
  request.method = method;
  request.url = url;
  request.headers = { host: 'localhost', 'content-type': 'application/json' };
  return request;
}

function outgoing() {
  const chunks = [];
  const response = new Writable({ write(chunk, encoding, done) { chunks.push(chunk); done(); } });
  response.headersSent = false;
  response.writeHead = (status, headers) => {
    response.statusCode = status;
    response.headers = headers;
    response.headersSent = true;
    return response;
  };
  response.body = () => Buffer.concat(chunks);
  return response;
}

test('same-origin proxy forwards sequence JSON and image bytes from backend paths', async () => {
  const seen = [];
  const fetchImpl = async (target, options) => {
    seen.push({ path: target.pathname, method: options.method, body: options.body?.toString() });
    if (target.pathname === '/sequence') {
      return new Response(JSON.stringify({ status: 'ready' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }
    if (target.pathname === '/outputs/abc/frame_000.png') {
      return new Response(new Uint8Array([137, 80, 78, 71]), {
        status: 200, headers: { 'Content-Type': 'image/png' },
      });
    }
    return new Response('', { status: 404 });
  };
  const options = { apiOrigin: 'http://backend.test:8000', fetchImpl };
  const sequence = outgoing();
  await handleIngressRequest(incoming('POST', '/api/sequence', '{"band":"D4"}'), sequence, options);
  await finished(sequence);
  assert.equal(sequence.statusCode, 200);
  assert.equal(JSON.parse(sequence.body()).status, 'ready');

  const image = outgoing();
  await handleIngressRequest(incoming('GET', '/outputs/abc/frame_000.png'), image, options);
  await finished(image);
  assert.equal(image.statusCode, 200);
  assert.equal(image.headers['Content-Type'], 'image/png');
  assert.deepEqual([...image.body()], [137, 80, 78, 71]);
  assert.deepEqual(seen, [
    { path: '/sequence', method: 'POST', body: '{"band":"D4"}' },
    { path: '/outputs/abc/frame_000.png', method: 'GET', body: undefined },
  ]);
});

test('proxy shows backend disconnect and never serves dotfiles', async () => {
  const disconnected = outgoing();
  await handleIngressRequest(incoming('POST', '/api/sequence', '{}'), disconnected, { apiOrigin: '' });
  await finished(disconnected);
  assert.equal(disconnected.statusCode, 503);
  assert.match(JSON.parse(disconnected.body()).message, /not connected/);

  const secret = outgoing();
  await handleIngressRequest(incoming('GET', '/.git/config'), secret, { apiOrigin: '' });
  await finished(secret);
  assert.equal(secret.statusCode, 403);
});
