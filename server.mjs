import { createServer } from 'node:http';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { Readable } from 'node:stream';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const proxyTimeoutMs = Number(process.env.INGRESS_API_TIMEOUT_MS || 600_000);
const mime = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
};

function jsonError(response, status, message) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify({ status: 'unavailable', message }));
}

async function requestBody(request) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > 64_000) throw new Error('Request body too large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export async function handleIngressRequest(request, response, {
  siteRoot = root, apiOrigin = process.env.INGRESS_API_ORIGIN || '',
  timeoutMs = proxyTimeoutMs, fetchImpl = fetch,
} = {}) {
    const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
    const isApi = url.pathname.startsWith('/api/');
    const isOutput = url.pathname.startsWith('/outputs/');
    if (isApi || isOutput) {
      if (!apiOrigin) {
        jsonError(response, 503, 'The real-data backend is not connected yet.');
        return;
      }
      if (isOutput && !['GET', 'HEAD'].includes(request.method) ||
          isApi && !['GET', 'POST', 'HEAD'].includes(request.method)) {
        jsonError(response, 405, 'This request method is not supported.');
        return;
      }
      try {
        const upstreamPath = isApi ? url.pathname.slice('/api'.length) : url.pathname;
        const target = new URL(upstreamPath + url.search, apiOrigin);
        const body = request.method === 'POST' ? await requestBody(request) : undefined;
        const upstream = await fetchImpl(target, {
          method: request.method,
          headers: {
            Accept: request.headers.accept || '*/*',
            ...(body ? { 'Content-Type': request.headers['content-type'] || 'application/json' } : {}),
          },
          body,
          signal: AbortSignal.timeout(timeoutMs),
        });
        response.writeHead(upstream.status, {
          'Content-Type': upstream.headers.get('content-type') || 'application/octet-stream',
          'Cache-Control': upstream.headers.get('cache-control') || 'no-store',
        });
        if (upstream.body && request.method !== 'HEAD') {
          Readable.fromWeb(upstream.body).on('error', error => response.destroy(error)).pipe(response);
        }
        else response.end();
      } catch (error) {
        if (response.headersSent) { response.destroy(error); return; }
        jsonError(response, error.name === 'TimeoutError' ? 504 : error.message === 'Request body too large' ? 413 : 502,
          error.name === 'TimeoutError'
            ? 'The SPHEREx sequence took too long. Try a narrower date range.'
            : error.message === 'Request body too large'
              ? 'The request is too large.' : 'Could not reach the SPHEREx backend.');
      }
      return;
    }
    try {
      const pathname = decodeURIComponent(url.pathname);
      if (pathname.split('/').some(segment => segment.startsWith('.'))) {
        response.writeHead(403); response.end('Forbidden'); return;
      }
      const full = path.resolve(siteRoot, `.${pathname === '/' ? '/index.html' : pathname}`);
      if (!full.startsWith(siteRoot + path.sep)) {
        response.writeHead(403); response.end('Forbidden'); return;
      }
      const info = await stat(full);
      if (!info.isFile()) throw new Error('Not a file');
      response.writeHead(200, { 'Content-Type': mime[path.extname(full)] || 'application/octet-stream' });
      createReadStream(full).pipe(response);
    } catch {
      response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      response.end('Not found');
    }
}

export function createIngressServer(options = {}) {
  return createServer((request, response) => handleIngressRequest(request, response, options));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 5173);
  createIngressServer().listen(port, () => {
    console.log(`Ingress frontend: http://localhost:${port}`);
    if (!process.env.INGRESS_API_ORIGIN) console.log('Real-data backend not connected; the synthetic WISE demo is available.');
  });
}
