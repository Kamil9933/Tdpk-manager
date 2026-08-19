// Minimal service worker — exists so Android/Chrome treats this as an installable
// PWA. Deliberately does NOT cache API responses or pages: this is a live admin
// tool (orders, stock, PostEx status), so showing stale data offline would be
// actively misleading. Only static assets (icons, fonts) get a light cache.
const CACHE = "tdpk-static-v1";
const STATIC_ASSETS = ["/static/icon-192.png", "/static/icon-512.png", "/static/manifest.json"];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC_ASSETS).catch(() => {})));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isStaticAsset = url.pathname.startsWith("/static/");
  if (!isStaticAsset || event.request.method !== "GET") {
    return; // let the browser handle it normally — always fresh from the network
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
