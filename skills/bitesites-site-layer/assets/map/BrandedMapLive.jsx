import { useEffect, useRef } from 'react'
/*
 * MapLibre's tile worker, bundled by Vite and addressed by URL.
 *
 * MapLibre finds its own worker with `new URL('./maplibre-gl-worker.mjs',
 * import.meta.url)`. That works from `node_modules`, but the bundler rewrites
 * `import.meta.url` to point inside `/assets/`, where no worker file was ever
 * emitted — so the request falls through Hosting's SPA rewrite, comes back as
 * `index.html`, and the browser rejects it for having a non-JavaScript MIME
 * type. The worker never starts, and because every tile is fetched and parsed
 * *in* that worker, the map draws its background and nothing else: no roads,
 * no water, no labels, and not one `.pbf` request.
 *
 * `?worker&url` makes Vite bundle the worker with its own imports resolved and
 * hand back the emitted asset's URL, which `setWorkerUrl` below installs before
 * the first map is constructed.
 *
 * This import is the reason the file exists separately from `BrandedMapCanvas`:
 * it is a static import, so it is emitted into whatever chunk holds it. In the
 * shell it put the worker asset — and the map style, and this module's own
 * code — on the critical path of every page. Here it rides the lazy chunk.
 */
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import { brandedMapStyle } from './branded-map-style'

const DEFAULT_PIN_COLOR = '#1d4ed8'

/**
 * The MapLibre half of a branded client map. Never imported directly —
 * `BrandedMapCanvas`
 * is the component everything else renders, and it reaches this through
 * `React.lazy` once a map is genuinely about to be drawn.
 *
 * Two decisions are worth knowing before changing anything here.
 *
 * **There is no API key.** Tiles come from OpenFreeMap and are rendered by
 * MapLibre, both of which are free and keyless. That removes the whole class of
 * failure a Google Maps embed brings with it — no billing account to lapse, no
 * browser key to leak or mis-restrict, no `gm_authFailure` to catch. (Address
 * autofill on the delivery flow does use a Google key; that is a different job
 * with a different failure mode, and it degrades to a plain form rather than to
 * a blank rectangle.)
 *
 * **The pin is a real marker, not a centred overlay.** MapLibre re-projects it
 * on every frame, so it stays on the verified client coordinates while the
 * visitor pans and zooms. The common iframe trick — a logo absolutely
 * positioned at 50%/50% of the panel — only looks right on a map nobody is
 * allowed to move, which is why embeds that use it have to disable panning.
 */

/**
 * Builds a white teardrop badge with an optional client logo/monogram path.
 * `pinTransform` fits that path into the badge; when no path is supplied, a
 * simple circle keeps the neutral kit usable before the client's logo geometry
 * is available.
 */
function pinSvg(label, scale, {
  path = null,
  color = DEFAULT_PIN_COLOR,
  pathTransform = 'translate(0 0) scale(1)',
} = {}) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 64 80')
  svg.setAttribute('width', String(Math.round(46 * scale)))
  svg.setAttribute('height', String(Math.round(58 * scale)))
  svg.setAttribute('role', 'img')
  svg.setAttribute('aria-label', label)
  const innerMark = path
    ? `<g transform="${pathTransform}"><path d="${path}" fill="${color}" fill-rule="evenodd" /></g>`
    : `<circle cx="32" cy="27" r="10" fill="${color}" />`
  svg.innerHTML = `
    <path
      d="M32 76 C25.5 63 9 46 9 27 A23 23 0 1 1 55 27 C55 46 38.5 63 32 76 Z"
      fill="#ffffff"
      stroke="${color}"
      stroke-width="3"
      stroke-linejoin="round"
    />
    ${innerMark}
  `
  return svg
}

/** Builds the marker's DOM: a real link, so hover, focus, and Enter are free. */
function pinElement({ href, label, scale, path, color, pathTransform }) {
  const anchor = document.createElement('a')
  anchor.className = 'store-pin'
  anchor.href = href
  anchor.target = '_blank'
  anchor.rel = 'noreferrer'
  anchor.setAttribute('aria-label', label)
  anchor.append(pinSvg(label, scale, { path, color, pathTransform }))
  return anchor
}

export default function BrandedMapLive({
  lat,
  lng,
  mapsUrl,
  pinLabel,
  zoomInLabel,
  zoomOutLabel,
  zoom,
  interactive,
  pinScale,
  mapStyle = brandedMapStyle,
  pinPath = null,
  pinColor = DEFAULT_PIN_COLOR,
  pinTransform,
  onStatus,
}) {
  const canvasRef = useRef(null)
  /* The shell owns `status`; this only reports. Held in a ref so a caller that
     passes an inline arrow does not re-run the effect and rebuild the map. */
  const report = useRef(onStatus)
  report.current = onStatus

  useEffect(() => {
    if (!canvasRef.current) return undefined

    let map = null
    let cancelled = false

    async function draw() {
      try {
        /*
         * Named imports, not a default one. MapLibre v6 dropped the default
         * export that v5 shipped, so `import('maplibre-gl').default` is
         * `undefined` — and destructuring it here fails at `new undefined.Map`
         * a few lines later, which looks exactly like "no WebGL". Pulling the
         * three constructors out by name breaks loudly instead if the package
         * ever moves them again.
         */
        const [{ Map: MapLibreMap, Marker, NavigationControl, setWorkerUrl }] = await Promise.all([
          import('maplibre-gl'),
          import('maplibre-gl/dist/maplibre-gl.css'),
        ])
        if (cancelled || !canvasRef.current) return

        // Must happen before the first map is constructed; repeat calls are
        // harmless, so this needs no "have I done this already" flag.
        setWorkerUrl(maplibreWorkerUrl)

        map = new MapLibreMap({
          container: canvasRef.current,
          style: typeof mapStyle === 'function' ? mapStyle() : mapStyle,
          center: [lng, lat],
          zoom,
          /* The checkout panel is a picture of where to collect an order, not
             something to explore: switching interaction off entirely means a
             thumb dragging down the page never gets caught by a map. */
          interactive,
          // A wheel over an embedded map should scroll the page, not zoom the
          // map. Cooperative gestures require ctrl/⌘ + wheel, or two fingers on
          // a touchscreen, which is the same contract Google's embed uses.
          cooperativeGestures: interactive,
          // This is a storefront locator. A tilted, rotated map is a way to get
          // lost in it, and north-up matches the printed directions people
          // arrive with.
          dragRotate: false,
          pitchWithRotate: false,
          attributionControl: { compact: true },
          locale: {
            'NavigationControl.ZoomIn': zoomInLabel,
            'NavigationControl.ZoomOut': zoomOutLabel,
          },
        })
        if (interactive) {
          map.touchZoomRotate.disableRotation()
          map.addControl(new NavigationControl({ showCompass: false }), 'bottom-right')
        }

        new Marker({
          element: pinElement({ href: mapsUrl, label: pinLabel, scale: pinScale, path: pinPath, color: pinColor, pathTransform: pinTransform }),
          anchor: 'bottom',
        })
          .setLngLat([lng, lat])
          .addTo(map)

        let loaded = false
        map.on('load', () => {
          loaded = true
          if (!cancelled) report.current?.('ready')
        })
        /*
         * MapLibre reports failures through this event rather than by throwing,
         * so without it a broken style would leave the panel blank forever.
         *
         * The two guards matter. A single tile that 404s at the edge of a pan
         * is routine and arrives here carrying `sourceId`; treating that as a
         * dead map would flash "could not load" over a map that is working. So
         * only style-level errors — the ones with no source attached — count,
         * and only before `load`, after which the map has demonstrably drawn.
         */
        map.on('error', (event) => {
          console.error('[map]', event?.sourceId || 'style', event?.error || event)
          if (cancelled || loaded || event?.sourceId) return
          report.current?.('failed')
        })
      } catch (error) {
        // No WebGL, or an offline visitor. The address and the directions
        // button beside it are rendered either way, so this degrades to the
        // information the map was carrying rather than to nothing. (A chunk
        // that never arrives is caught a level up, by the shell's boundary,
        // because `React.lazy` fails before this component ever runs.)
        //
        // Logged rather than swallowed: a silent catch here turns every cause
        // into the same blank panel, which is not something anyone can debug
        // from a bug report.
        console.error('[map] could not start', error)
        if (!cancelled) report.current?.('failed')
      }
    }

    draw()

    return () => {
      cancelled = true
      map?.remove()
    }
  }, [lat, lng, mapsUrl, pinLabel, zoom, interactive, pinScale, mapStyle, pinPath, pinColor, pinTransform, zoomInLabel, zoomOutLabel])

  return <div className="map-canvas" ref={canvasRef} />
}
