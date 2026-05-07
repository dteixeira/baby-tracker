// Version is stamped at build time by the Dockerfile (sed replacement).
// Never edit this line manually — just rebuild the image.
const CACHE_VERSION = 'CACHE_VERSION_PLACEHOLDER';
const CACHE         = `baby-tracker-${CACHE_VERSION}`;

// CDN assets only — cached aggressively (they're versioned by URL anyway)
const CDN_SHELL = [
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js',
  'https://cdn.jsdelivr.net/npm/flatpickr/dist/themes/dark.min.css',
  'https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css',
  'https://cdn.jsdelivr.net/npm/flatpickr',
  'https://unpkg.com/lucide@latest/dist/umd/lucide.min.js',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(CDN_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API — network only (no caching)
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}', { status: 503 })));
    return;
  }

  // CDN — cache-first (URLs are immutably versioned)
  if (url.origin !== self.location.origin) {
    e.respondWith(
      caches.match(e.request).then(cached => cached || fetch(e.request).then(resp => {
        if (resp.ok) caches.open(CACHE).then(c => c.put(e.request, resp.clone()));
        return resp;
      }))
    );
    return;
  }

  // App shell (same origin, non-API) — network-first so deploys are picked up immediately
  e.respondWith(
    fetch(e.request).then(resp => {
      if (resp.ok) caches.open(CACHE).then(c => c.put(e.request, resp.clone()));
      return resp;
    }).catch(() => caches.match(e.request))
  );
});
