import assert from 'node:assert/strict';
import test from 'node:test';
import worker from './worker.mjs';

const bytes = new TextEncoder().encode('0123456789');
const env = {
  PODCAST_BUCKET: {
    async head() { return { size: bytes.length, httpEtag: '"test"', writeHttpMetadata() {} }; },
    async get(key, options) {
      const range = options?.range;
      return { body: range ? bytes.slice(range.offset, range.offset + range.length) : bytes };
    }
  }
};
const url = 'https://pursuit.example.workers.dev/media/EPISODE0001.mp3';

test('podcast clients can retrieve a byte range', async () => {
  const response = await worker.fetch(new Request(url, { headers: { Range: 'bytes=2-5' } }), env);
  assert.equal(response.status, 206);
  assert.equal(response.headers.get('Content-Range'), 'bytes 2-5/10');
  assert.equal(response.headers.get('Content-Length'), '4');
  assert.equal(await response.text(), '2345');
});

test('suffix and open-ended ranges work', async () => {
  for (const [range, expected] of [['bytes=-3', '789'], ['bytes=8-', '89']]) {
    const response = await worker.fetch(new Request(url, { headers: { Range: range } }), env);
    assert.equal(response.status, 206);
    assert.equal(await response.text(), expected);
  }
});

test('invalid and multi-part ranges fail explicitly', async () => {
  for (const range of ['bytes=10-', 'bytes=5-2', 'bytes=-0', 'bytes=0-1,4-5']) {
    const response = await worker.fetch(new Request(url, { headers: { Range: range } }), env);
    assert.equal(response.status, 416);
  }
});

test('HEAD exposes length without downloading the body', async () => {
  const response = await worker.fetch(new Request(url, { method: 'HEAD' }), env);
  assert.equal(response.headers.get('Content-Length'), '10');
  assert.equal(await response.text(), '');
});

test('feed is served as RSS without stale caching', async () => {
  const response = await worker.fetch(new Request('https://example.com/feed.xml'), env);
  assert.equal(response.headers.get('Content-Type'), 'application/rss+xml; charset=utf-8');
  assert.equal(response.headers.get('Cache-Control'), 'no-cache, max-age=0');
});

test('private artifacts, listings and writes are rejected', async () => {
  for (const path of ['/', '/podcast-r2-credentials.json', '/other.mp3']) {
    const response = await worker.fetch(new Request(`https://example.com${path}`), env);
    assert.equal(response.status, 404);
  }
  const response = await worker.fetch(new Request(url, { method: 'PUT', body: 'overwrite' }), env);
  assert.equal(response.status, 405);
});

test('conditional requests and unavailable storage are handled', async () => {
  const response = await worker.fetch(new Request(url, { headers: { 'If-None-Match': '"test"' } }), env);
  assert.equal(response.status, 304);
  const missing = await worker.fetch(new Request(url), { PODCAST_BUCKET: { async head() { return null; } } });
  assert.equal(missing.status, 404);
  const failed = await worker.fetch(new Request(url), { PODCAST_BUCKET: { async head() { throw new Error('offline'); } } });
  assert.equal(failed.status, 503);
});
