import { Component, Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react'

/**
 * A client location on a map, drawn in the site's own colours.
 *
 * A marketing locator, service-area panel, or checkout panel can share this
 * shell. Callers differ only in size and whether the map can be panned; the
 * surrounding business details stay in the caller.
 *
 * This file is the shell: the frame, the deferral, the failure notice. It
 * contains no MapLibre code at all, which is the point.
 *
 * ---------------------------------------------------------------------------
 * Why the renderer is behind `React.lazy`
 * ---------------------------------------------------------------------------
 *
 * MapLibre is by some margin the largest dependency on the site — a megabyte
 * raw, a quarter of that gzipped — and almost nobody sees a map. It was already
 * behind a dynamic `import()` inside the effect, so the library itself was its
 * own chunk. What was *not* deferred was everything around it: the map style
 * document, the client pin geometry, the bundled tile worker's
 * emitted URL, and this component's own body all sat in the entry chunk of
 * every page, because they were static imports of a module the homepage
 * imports statically.
 *
 * Splitting at a component boundary rather than at the `import('maplibre-gl')`
 * call moves all of that with it, and matches what `main.jsx` does for the
 * admin routes: `lazy` the component, `Suspense` the wait.
 *
 * The deferral is still an `IntersectionObserver` rather than a plain mount,
 * because `lazy` starts fetching the moment the component renders and a locator
 * at the bottom of a long homepage should not compete with the hero for
 * bandwidth. So the lazy element is only rendered once the frame is within a
 * screen of the viewport: the chunk is fetched when a map is genuinely about to
 * be drawn, and never for a visitor who bounces off the top of the page.
 *
 * ---------------------------------------------------------------------------
 * What a visitor sees when it does not work
 * ---------------------------------------------------------------------------
 *
 * Every caller should render its address, phone number and a "Directions"
 * link in the card beside this frame, so the information the map carries is
 * never only in the map. This component's job when things go wrong is
 * therefore to say plainly that the picture is missing and get out of the way —
 * not to substitute a picture of a map for a map.
 *
 * Three failures, one outcome. No WebGL and a broken tile style are caught
 * inside `BrandedMapLive`. A chunk that never arrives — a blocked CDN, a dropped
 * connection mid-scroll, a stale index after a deploy — cannot be: `React.lazy`
 * rejects before the component ever runs, and an unhandled rejection there
 * unmounts the whole tree above it. That is why there is an error boundary
 * here. Without it a failed chunk takes the homepage down instead of costing it
 * a map.
 */

/* Rendering starts this far before the panel enters the viewport — roughly a
   phone screen of runway, enough for the chunk and first tiles on a decent
   connection without fetching for someone who bounces off the hero. */
const PRELOAD_MARGIN = '600px'

export const DEFAULT_ZOOM = 16.4

const BrandedMapLive = lazy(() => import('./BrandedMapLive.jsx'))

/** True once the element has been within `PRELOAD_MARGIN` of the viewport. */
function useNearViewport(ref) {
  const [near, setNear] = useState(false)

  useEffect(() => {
    const node = ref.current
    if (!node || near) return
    // jsdom and older browsers have no observer. Treating "cannot tell" as
    // "visible" keeps the map working; it only forfeits the deferral.
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true)
      return
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNear(true)
          observer.disconnect()
        }
      },
      { rootMargin: PRELOAD_MARGIN },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [near, ref])

  return near
}

/** A zero/zero pair is a missing location, not a usable client coordinate. */
export function hasStorePosition(lat, lng) {
  return Number.isFinite(lat) && Number.isFinite(lng) && (lat !== 0 || lng !== 0)
}

/**
 * Catches a map chunk that never arrives.
 *
 * Renders nothing of its own: the frame and the notice are the shell's, so a
 * failure here looks exactly like a failure inside the renderer.
 */
class MapChunkBoundary extends Component {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error) {
    console.error('[map] could not load the map chunk', error)
    this.props.onFailed?.()
  }

  render() {
    return this.state.failed ? null : this.props.children
  }
}

/*
 * The core kit uses plain defaults so it can be dropped into any app. A host
 * can pass translated strings from its own copy system.
 */
export default function BrandedMapCanvas({
  lat,
  lng,
  mapsUrl,
  pinLabel = 'Location',
  unavailableLabel = 'This map is unavailable.',
  zoomInLabel = 'Zoom in',
  zoomOutLabel = 'Zoom out',
  zoom = DEFAULT_ZOOM,
  interactive = true,
  pinScale = 1,
  mapStyle,
  pinPath,
  pinColor,
  pinTransform,
  className = '',
}) {
  const frameRef = useRef(null)
  const near = useNearViewport(frameRef)
  const [status, setStatus] = useState('idle')
  const hasPosition = hasStorePosition(lat, lng)
  const draw = near && hasPosition

  const fail = useCallback(() => setStatus('failed'), [])

  useEffect(() => {
    if (draw) setStatus((current) => (current === 'idle' ? 'loading' : current))
  }, [draw])

  /*
   * An empty `.map-canvas` holds the frame open before the map arrives and
   * after it has failed.
   *
   * `.map-canvas` is where the height lives — 380–560px in the locator, 210px
   * in checkout — so without this the panel is zero tall until the chunk lands
   * and then shoves the page down, and the failure notice, which is absolutely
   * positioned to `inset: 0`, would have nothing to cover.
   */
  const placeholder = hasPosition ? <div className="map-canvas" aria-hidden="true" /> : null
  const live = draw && status !== 'failed'

  return (
    <div className={`map-frame${className ? ` ${className}` : ''}`} ref={frameRef} data-status={status}>
      {live ? (
        <MapChunkBoundary onFailed={fail}>
          {/* The reserved frame, not a spinner: it is the same size either way,
              so nothing under it moves when the map arrives, and there is
              nothing here worth announcing to a screen reader. */}
          <Suspense fallback={placeholder}>
            <BrandedMapLive
              lat={lat}
              lng={lng}
              mapsUrl={mapsUrl}
              pinLabel={pinLabel}
              zoomInLabel={zoomInLabel}
              zoomOutLabel={zoomOutLabel}
              zoom={zoom}
              interactive={interactive}
              pinScale={pinScale}
              mapStyle={mapStyle}
              pinPath={pinPath}
              pinColor={pinColor}
              pinTransform={pinTransform}
              onStatus={setStatus}
            />
          </Suspense>
        </MapChunkBoundary>
      ) : (
        placeholder
      )}
      {status === 'failed' && <p className="map-notice">{unavailableLabel}</p>}
    </div>
  )
}
