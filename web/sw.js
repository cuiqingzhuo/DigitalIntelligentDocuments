const CACHE = 'diw-pilot-1';
const FILES = ['/', '/assets/style.css', '/assets/mark.svg', '/assets/common.js', '/assets/reader.js'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(
  keys.filter(key => key.startsWith('diw-pilot-') && key !== CACHE).map(key => caches.delete(key))
)).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== location.origin || event.request.method !== 'GET' || event.request.headers.has('Authorization')) return;
  const assets = FILES.includes(url.pathname) || url.pathname.startsWith('/assets/samples/');
  const reader = url.pathname.startsWith('/shelf/');
  if (!assets && !reader) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && assets) {
      const clone = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(event.request,clone)));
    }
    return response;
  }).catch(() => caches.match(reader ? '/' : event.request).then(response => response || Response.error())));
});
