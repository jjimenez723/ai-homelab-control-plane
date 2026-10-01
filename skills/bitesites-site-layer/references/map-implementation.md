# Map adaptation reference

The bundled map code is neutral BiteSites code. It can be copied into another Vite/React client and then connected to that client's content model. Oasis-specific code is kept out of this map kit.

## Required pieces

| Piece | Purpose | Client-specific work |
| --- | --- | --- |
| `BrandedMapCanvas.jsx` | lazy viewport loading, reserved frame, error boundary, fallback | translation hook and host class names |
| `BrandedMapLive.jsx` | MapLibre construction, worker URL, marker, controls, failure reporting | logo path, style factory, labels |
| `branded-map-style.js` | vector source, semantic palette, road/land/water/label layers, map attribution | name, palette, optional layer tuning |
| `map.css` | frame, card, pin, MapLibre controls, mobile behavior | host tokens and layout |

## Stable integration shape

```jsx
<BrandedMapCanvas
  lat={business.lat}
  lng={business.lng}
  mapsUrl={business.mapsUrl}
  mapStyle={() => brandedMapStyle({
    name: business.name,
    palette: brandMapPalette,
  })}
  pinPath={brandPinPath}
  pinColor={brandMapPalette.label}
  pinTransform={brandPinTransform}
  interactive
/>
```

Keep the style factory and pin path stable with module constants or `useMemo`; an inline function/object can rebuild the map whenever the parent renders. If the client has no clean vector logo path, use a simple geometric pin/monogram rather than trying to rasterize a full wordmark into a tiny marker.

## Do not regress these invariants

- `setWorkerUrl(...)` runs before `new Map(...)`.
- `attributionControl` stays enabled.
- `Marker#setLngLat([lng, lat])` is used; a centered overlay is not equivalent.
- zero/zero or missing coordinates do not start MapLibre.
- the map is not the only place where the address or directions action exists.
- interactive maps use cooperative gestures and no rotation; checkout maps can use `interactive={false}`.
- visible OpenFreeMap/OpenStreetMap credit stays readable on mobile.
