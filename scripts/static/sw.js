// Deliberately minimal: this exists only to satisfy PWA installability
// criteria (Android/Chrome require a registered service worker before
// showing an install prompt; iOS ignores it entirely). It does NOT cache
// anything — /api/swipe/next and /api/swipe/action must always hit the
// network fresh, since caching either would show stale candidates or
// silently no-op a swipe action. If offline support is ever wanted later,
// add a narrow cache scoped to /static/* only — never the /api/* routes.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
