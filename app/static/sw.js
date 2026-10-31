// Minimální service worker – nutný pro instalaci aplikace na plochu a pro příjem sdílení.
// Nic necachuje: data o vlacích musí být vždy aktuální ze serveru.
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {});
