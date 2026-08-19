/* eslint-disable */
const CACHE_NAME = 'wellnesscrm-app-shell-v1'

const ASSETS_TO_CACHE = [
  '/',
  '/index.html',
  // In a real build, we'd pre-cache specific assets, but for Vite we'll cache them dynamically.
]

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE)
    })
  )
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    })
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)

  // 1. Never cache API requests. Sensitive data must not be in the SW cache.
  if (url.pathname.startsWith('/api/')) {
    return // Let the browser handle it natively
  }

  // 2. Only cache GET requests.
  if (event.request.method !== 'GET') {
    return
  }

  // 3. For assets (like JS/CSS from Vite, usually cache-busted), do Cache-First
  if (url.pathname.startsWith('/assets/') || url.pathname.match(/\.(png|jpe?g|svg|woff2?|css|js)$/)) {
    event.respondWith(
      caches.match(event.request).then((cachedResponse) => {
        if (cachedResponse) {
          return cachedResponse
        }
        return fetch(event.request).then((networkResponse) => {
          if (networkResponse.ok) {
            const responseToCache = networkResponse.clone()
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(event.request, responseToCache)
            })
          }
          return networkResponse
        })
      })
    )
    return
  }

  // 4. For everything else (like HTML navigation), do Network-First, falling back to cache
  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse.ok) {
          const responseToCache = networkResponse.clone()
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache)
          })
        }
        return networkResponse
      })
      .catch(() => {
        return caches.match(event.request).then((cachedResponse) => {
          if (cachedResponse) {
            return cachedResponse
          }
          // If the specific request isn't cached (e.g. some react-router route),
          // fallback to index.html since it's a SPA.
          if (event.request.mode === 'navigate') {
            return caches.match('/index.html')
          }
          return null
        })
      })
  )
})
