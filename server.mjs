import { createServer } from 'node:http';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT || 5173);
const apiOrigin = process.env.INGRESS_API_ORIGIN || '';
const mime = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
};

createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
    if (url.pathname.startsWith('/api/')) {
      if (!apiOrigin) {
        response.writeHead(503, { 'Content-Type': 'application/json; charset=utf-8' });
        response.end(JSON.stringify({ status: 'unavailable', message: 'The real-data backend is not connected yet.' }));
        return;
      }
      const target = new URL(url.pathname + url.search, apiOrigin);
      const upstream = await fetch(target, { signal: AbortSignal.timeout(60_000) });
      response.writeHead(upstream.status, {
        'Content-Type': upstream.headers.get('content-type') || 'application/octet-stream',
        'Cache-Control': upstream.headers.get('cache-control') || 'no-store',
      });
      response.end(Buffer.from(await upstream.arrayBuffer()));
      return;
    }
    const pathname = decodeURIComponent(url.pathname);
    const full = path.resolve(root, `.${pathname === '/' ? '/index.html' : pathname}`);
    if (!full.startsWith(root + path.sep)) {
      response.writeHead(403); response.end('Forbidden'); return;
    }
    const info = await stat(full);
    if (!info.isFile()) throw new Error('Not a file');
    response.writeHead(200, { 'Content-Type': mime[path.extname(full)] || 'application/octet-stream' });
    createReadStream(full).pipe(response);
  } catch (error) {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('Not found');
  }
}).listen(port, () => {
  console.log(`Ingress frontend: http://localhost:${port}`);
  if (!apiOrigin) console.log('Real-data backend not connected; the synthetic WISE demo is available.');
});
