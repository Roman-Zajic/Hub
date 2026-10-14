// Minimal service worker. Its only job: browsers offer "Install app" (a window without an address bar) for sites that have one.
// It caches nothing, so updates always arrive straight from GitHub Pages.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
