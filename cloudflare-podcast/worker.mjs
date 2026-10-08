function rangeFor(header, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!match || (!match[1] && !match[2])) return null;
  const start = match[1] ? Number(match[1]) : Math.max(0, size - Number(match[2]));
  const end = match[1] && match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end || start >= size) return null;
  return { offset: start, length: end - start + 1 };
}

export default {
  async fetch(request, env) {
    if (!['GET', 'HEAD'].includes(request.method)) {
      return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, HEAD' } });
    }
    const key = new URL(request.url).pathname.slice(1);
    // Only podcast artifacts are public; bucket listing and writes are unavailable.
    if (!/^(feed\.xml|cover\.(jpg|png)|media\/[A-Za-z0-9_-]{11}\.mp3)$/.test(key)) {
      return new Response('Not found', { status: 404 });
    }
    try {
      const metadata = await env.PODCAST_BUCKET.head(key);
      if (!metadata) return new Response('Not found', { status: 404 });
      const headers = new Headers();
      metadata.writeHttpMetadata(headers);
      headers.set('Content-Type', key === 'feed.xml' ? 'application/rss+xml; charset=utf-8'
        : key.endsWith('.mp3') ? 'audio/mpeg' : key.endsWith('.png') ? 'image/png' : 'image/jpeg');
      headers.set('ETag', metadata.httpEtag);
      headers.set('Accept-Ranges', 'bytes');
      headers.set('Cache-Control', key === 'feed.xml' ? 'no-cache, max-age=0' : 'public, max-age=3600');
      headers.set('X-Content-Type-Options', 'nosniff');
      if (request.headers.get('If-None-Match') === metadata.httpEtag) {
        return new Response(null, { status: 304, headers });
      }
      const rangeHeader = request.method === 'GET' ? request.headers.get('Range') : null;
      const ifRange = request.headers.get('If-Range');
      const useRange = rangeHeader && (!ifRange || ifRange === metadata.httpEtag);
      const range = useRange ? rangeFor(rangeHeader, metadata.size) : null;
      if (useRange && !range) {
        headers.set('Content-Range', `bytes */${metadata.size}`);
        return new Response(null, { status: 416, headers });
      }
      headers.set('Content-Length', String(range ? range.length : metadata.size));
      if (range) headers.set('Content-Range', `bytes ${range.offset}-${range.offset + range.length - 1}/${metadata.size}`);
      if (request.method === 'HEAD') return new Response(null, { headers });
      const object = await env.PODCAST_BUCKET.get(key, range ? { range } : undefined);
      if (!object) return new Response('Not found', { status: 404 });
      return new Response(object.body, { status: range ? 206 : 200, headers });
    } catch {
      return new Response('Temporarily unavailable', { status: 503, headers: { 'Retry-After': '60' } });
    }
  }
};
