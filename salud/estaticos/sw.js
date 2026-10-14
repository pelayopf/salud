// Service worker: hace la web instalable y deja ver lo último sin conexión.
// Las páginas van primero a la red (los datos cambian) y solo tiran de la
// copia guardada si no hay conexión. CSS, JS e iconos llevan su versión en
// la URL, así que se sirven de caché. Subir VERSION al cambiar este fichero.
const VERSION = "salud-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((claves) => Promise.all(claves.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function guardar(peticion, respuesta) {
  if (respuesta.ok) {
    const copia = respuesta.clone();
    caches.open(VERSION).then((c) => c.put(peticion, copia));
  }
  return respuesta;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;

  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then((r) => guardar(req, r))
        .catch(() =>
          caches.match(req).then(
            (copia) =>
              copia ||
              new Response(
                "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>" +
                  "<body style='font:16px system-ui;padding:32px;color:#16232a;background:#f6f4ee'>" +
                  "<h1 style='color:#0e4a5c'>Sin conexión</h1><p>Esta página no está guardada. " +
                  "Comprueba que Tailscale está conectado.</p>",
                { headers: { "Content-Type": "text/html; charset=utf-8" } },
              ),
          ),
        ),
    );
    return;
  }

  if (url.pathname.startsWith("/estaticos/")) {
    e.respondWith(caches.match(req).then((copia) => copia || fetch(req).then((r) => guardar(req, r))));
  }
});
