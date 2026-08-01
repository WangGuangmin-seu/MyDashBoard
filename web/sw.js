// Service worker — cache-first shell + stale-while-revalidate snapshot (spec §8.2).
// Goal: "open and see the latest immediately" — paint last-known data in
// milliseconds from cache, then fetch a fresh snapshot in the background and
// notify the page to silently re-render.

const CACHE = "dashboard-v1";
const SHELL = ["./", "index.html", "app.js", "manifest.json"];
const SNAPSHOT_KEY = "data/snapshot.json"; // canonical cache key (query stripped)

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function isSnapshot(url) {
  return url.pathname.endsWith("/data/snapshot.json") || url.pathname.endsWith("data/snapshot.json");
}

async function notifyUpdated() {
  const clients = await self.clients.matchAll({ type: "window" });
  clients.forEach((c) => c.postMessage({ type: "snapshot-updated" }));
}

// Return cached snapshot instantly; refresh cache from network in background.
async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(SNAPSHOT_KEY);
  const network = fetch(request, { cache: "no-store" })
    .then(async (resp) => {
      if (resp && resp.ok) {
        await cache.put(SNAPSHOT_KEY, resp.clone());
        if (cached) notifyUpdated(); // only ping if we had already shown stale data
      }
      return resp;
    })
    .catch(() => null);
  return cached || (await network) || new Response("{}", { headers: { "Content-Type": "application/json" } });
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;

  if (isSnapshot(url)) {
    event.respondWith(staleWhileRevalidate(event.request));
    return;
  }
  if (url.origin === self.location.origin) {
    // cache-first for the static shell
    event.respondWith(
      caches.match(event.request, { ignoreSearch: true }).then(
        (hit) => hit || fetch(event.request).then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
          return resp;
        }).catch(() => caches.match("index.html"))
      )
    );
  }
});
