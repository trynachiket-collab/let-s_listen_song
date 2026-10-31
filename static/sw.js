const CACHE_NAME = "syncplay-shell-v1";
const APP_SHELL = ["/", "/manifest.json", "/icon.svg", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  // Only cache the local app shell. Do not cache Socket.IO, API responses,
  // YouTube streams, or third-party resources.
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/socket.io/") || ["/create", "/search"].includes(url.pathname)) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then(response => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put("/", copy));
        return response;
      }).catch(() => caches.match("/"))
    );
    return;
  }

  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request))
    );
  }
});
