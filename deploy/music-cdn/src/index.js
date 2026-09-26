// The music CDN: a private R2 bucket behind a custom domain. R2's pre-signed
// URLs do not work on custom domains, so this Worker checks the backend's own
// signature (music_engine/cdn.py) and streams from the bucket, Range included,
// which the player needs to seek. The backend uploads through it with the same
// secret as a bearer token.
//
// GET|HEAD /<key>?e=<expires>&s=<signature>   signed read
// PUT      /<key>  Authorization: Bearer <CDN_SECRET>

const encoder = new TextEncoder();

function equal(a, b) {
  const x = encoder.encode(a);
  const y = encoder.encode(b);
  return x.byteLength === y.byteLength && crypto.subtle.timingSafeEqual(x, y);
}

async function sign(secret, payload) {
  const key = await crypto.subtle.importKey(
    'raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const mac = new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(payload)));
  return [...mac].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 32);
}

async function signedFor(env, key, url) {
  const expires = Number(url.searchParams.get('e'));
  const signature = url.searchParams.get('s') || '';
  if (!Number.isInteger(expires) || expires * 1000 < Date.now()) return false;
  return equal(await sign(env.CDN_SECRET, `music-cdn:${key}:${expires}`), signature);
}

// A 404 for every refusal, like the backend's own links: nothing tells a
// prober whether a key exists.
const notFound = () => new Response('Not found', { status: 404 });

function objectHeaders(object) {
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set('ETag', object.httpEtag);
  headers.set('Accept-Ranges', 'bytes');
  // Private: a shared cache must not keep a file a signature was needed for.
  headers.set('Cache-Control', 'private, max-age=86400');
  headers.set('X-Content-Type-Options', 'nosniff');
  // The player downloads songs with fetch() to keep them on the device, and a
  // cross-origin fetch needs CORS to read the body. Any origin is safe: the
  // signature is the access check, there are no cookies to ride along, and
  // anyone holding a link can already download it without a browser.
  headers.set('Access-Control-Allow-Origin', '*');
  headers.set('Access-Control-Expose-Headers', 'Content-Length, Content-Range, ETag');
  return headers;
}

async function read(request, env, key) {
  if (request.method === 'HEAD') {
    const object = await env.BUCKET.head(key);
    if (!object) return notFound();
    const headers = objectHeaders(object);
    headers.set('Content-Length', String(object.size));
    return new Response(null, { headers });
  }

  let object;
  try {
    object = await env.BUCKET.get(key, { range: request.headers, onlyIf: request.headers });
  } catch {
    return new Response('Range not satisfiable', { status: 416 });
  }
  if (!object) return notFound();
  const headers = objectHeaders(object);
  if (!('body' in object)) return new Response(null, { status: 304, headers });

  if (request.headers.has('Range') && object.range) {
    const offset = object.range.offset ?? object.size - object.range.suffix;
    const length = object.range.length ?? object.size - offset;
    headers.set('Content-Range', `bytes ${offset}-${offset + length - 1}/${object.size}`);
    headers.set('Content-Length', String(length));
    return new Response(object.body, { status: 206, headers });
  }
  headers.set('Content-Length', String(object.size));
  return new Response(object.body, { headers });
}

async function write(request, env, key) {
  if (!equal(request.headers.get('Authorization') || '', `Bearer ${env.CDN_SECRET}`)) {
    return new Response('Forbidden', { status: 403 });
  }
  await env.BUCKET.put(key, request.body, {
    httpMetadata: { contentType: request.headers.get('Content-Type') || 'application/octet-stream' },
  });
  return new Response(null, { status: 201 });
}

export default {
  async fetch(request, env) {
    if (!env.CDN_SECRET) return new Response('CDN_SECRET is not set', { status: 500 });
    const url = new URL(request.url);
    let key;
    try {
      key = decodeURIComponent(url.pathname.slice(1));
    } catch {
      return notFound();
    }
    if (!key) return notFound();

    if (request.method === 'PUT') return write(request, env, key);
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, HEAD, PUT' } });
    }
    if (!(await signedFor(env, key, url))) return notFound();
    return read(request, env, key);
  },
};
