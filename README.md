# iPhone 3D Ad Rig

A self-contained, browser-based 3D iPhone advertisement rig built with Three.js. Renders a cinematic 30-second spot with a procedurally-built iPhone, then lets you put **your own app on the screen** — as a static screenshot, an uploaded image, or a **live, interactive web page**.

## Run

```bash
python serve.py
# → http://127.0.0.1:8742/index.html
```

A modern browser with WebGL is required. The Python server also powers the proxy and screenshot features below, so use it rather than opening the file directly.

## Features

- **Cinematic 30s loop** — phone rises, camera orbits, light sweeps the metal edge, tagline fades in (*"The future, in your hands."*).
- **Interactive 3D** — drag to orbit (with flick momentum + spring-back), scroll to zoom.
- **Spacebar play/pause** — freezes the whole scene; toggle button bottom-right.
- **Phone models** — iPhone 16 Pro / Pro Max, 15 Pro, 14 Pro / Pro Max, 13 (Dynamic Island or notch per model).
- **Your screen content:**
  - **App screenshot** — upload a PNG/JPG; the scene recolors to match its palette and adds color-matched status/home spacers.
  - **Logo** — appears with the tagline in the finale.
  - **Custom tagline** — type your own.
  - **Live URL** — type any URL to load a real, interactive page on the phone glass. Public sites route through a rewriting proxy that tunnels HTML/CSS/JS and the page's own `fetch`/XHR so they actually render.
- **Capture:**
  - **Save PNG** / **Record** — export the demo or screenshot view as image/video.
  - **Capture live** — renders the current live page (the route you navigated to, session preserved) in a real headless browser via `/shot`, freezes it onto the glass, then Save PNG / Record work.

## Files

- `index.html` — the entire rig (Three.js via CDN, single file).
- `serve.py` — static server + rewriting proxy (`/proxy?u=`) + headless screenshot endpoint (`/shot?u=`).
- `test-*.html` — small standalone harnesses used while building.

## Notes / limits

- The proxy is display-grade; WebSocket-heavy apps (e.g. visual editors) may be partial.
- Live-iframe pixels can't be read directly by the canvas — that's why **Capture live** uses the server-side headless render.
- For your own sites, sending `Content-Security-Policy: frame-ancestors 'self' http://127.0.0.1:8742` lets them embed natively without the proxy.
