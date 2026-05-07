const CACHE = "mmflux-v7";
const OFFLINE = "/offline";

const PRECACHE = [
  OFFLINE,
  "/static/styles.css",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png"
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;

  const url = new URL(e.request.url);
  const isNavigation = e.request.mode === "navigate";
  const isStatic = url.pathname.startsWith("/static/");

  if (isNavigation) {
    // Páginas HTML: sempre busca da rede — nunca serve HTML cacheado
    // Só exibe offline se realmente sem conexão
    e.respondWith(
      fetch(e.request).catch(() =>
        caches.match(OFFLINE)
      )
    );
    return;
  }

  if (isStatic) {
    // Assets estáticos: cache-first com atualização em background
    e.respondWith(
      caches.match(e.request).then(cached => {
        const network = fetch(e.request).then(res => {
          caches.open(CACHE).then(c => c.put(e.request, res.clone()));
          return res;
        });
        return cached || network;
      })
    );
    return;
  }

  // Demais requisições (API, etc): network-only
  e.respondWith(fetch(e.request));
});
