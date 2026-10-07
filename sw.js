const CACHE_NAME = "vyom-shell-security-v2";
const SHELL = ["/", "/index.html", "/manifest.json"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL)).catch(() => null));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(data.title || "VYOM Alert", {
    body: data.body || "New VYOM notification",
    data: { url: data.url || "/" }
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  let destination = new URL("/", self.location.origin);
  try {
    const requested = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin);
    if (requested.origin === self.location.origin) destination = requested;
  } catch (_) {}
  event.waitUntil(clients.openWindow(destination.href));
});
