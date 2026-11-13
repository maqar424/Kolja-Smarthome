/* Service worker: makes the app installable and opens instantly.
 *
 * Network first for the app shell (so updates show up right away), cached copy
 * as fallback. API calls and the live stream are never cached: without the Pi
 * there is nothing to control anyway.
 */
const CACHE = "licht-v2";
const SHELL = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/floorplan.svg",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/events" || url.pathname === "/health") return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
