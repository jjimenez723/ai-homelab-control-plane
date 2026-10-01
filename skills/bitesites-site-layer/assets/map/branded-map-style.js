/**
 * A branded basemap, as a MapLibre style document.
 *
 * This is the whole reason the location map is not a Google embed. A style is
 * the map's own paint instructions — the tiles arrive as vector geometry with
 * no colour of their own, and every fill below is chosen from the site's
 * palette supplied by the client. The water, labels, ground, roads, and
 * buildings are separate semantic tokens so a new brand can change them
 * without rewriting the layer structure.
 *
 * The alternative everyone reaches for first — an iframe under a CSS
 * `filter: hue-rotate(...)` — cannot do this. A filter bends the finished
 * picture, so pushing the water toward navy drags the roads and the labels with
 * it, and the map ends up a uniformly tinted photograph of somebody else's
 * design. Here the roads stay white on purpose while the water moves.
 *
 * Tiles, fonts, and sprites all come from OpenFreeMap, which serves
 * OpenStreetMap data with no API key, no account, and no billing relationship.
 * That is the second reason this is not Google: there is no key to leak, to
 * restrict, to rotate, or to have rejected at load time.
 */

/* Documented at https://openfreemap.org — public endpoints, no key. */
const OPENFREEMAP_TILES = 'https://tiles.openfreemap.org/planet'
const OPENFREEMAP_GLYPHS = 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf'

/**
 * OpenStreetMap's licence requires visible credit. MapLibre renders this into
 * its own attribution control, which `BrandedMapLive` leaves switched on and
 * collapsed — removing it would put the site out of compliance, so it is part
 * of the style rather than a prop a caller can forget.
 */
export const MAP_ATTRIBUTION =
  '<a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> · ' +
  '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap</a>'

/* The site's palette, resolved to literals: a style document is JSON handed to
   a WebGL renderer, so it cannot read the CSS custom properties by name. These
   keys are deliberately semantic so the same style can be reused for another
   client by passing that client's palette. */
export const DEFAULT_MAP_COLORS = {
  ground: '#e7edf5',
  greenery: '#d8e9e0',
  water: '#c8e4f3',
  waterLine: '#a9d3ea',
  building: '#d6e0ee',
  road: '#ffffff',
  roadCasing: '#ccd8e8',
  majorRoad: '#f5f9fd',
  majorCasing: '#b3cbe6',
  motorway: '#dceaf8',
  motorwayCasing: '#a9c8e6',
  label: '#031d3a',
  labelMuted: '#667085',
  waterLabel: '#0a6ab8',
  halo: '#ffffff',
}

const SOURCE = 'openmaptiles'

/** A road stripe that grows with zoom. MapLibre interpolates between stops. */
function roadWidth(stops) {
  return { type: 'exponential', base: 1.6, stops }
}

/**
 * Builds the style document.
 *
 * Taking the palette as an argument keeps this honest about being data: the
 * dashboard does not edit map colours today, but nothing here reaches for a
 * module-level constant that would have to be rewritten if it ever does.
 */
export function brandedMapStyle({ name = 'Branded location map', palette = {} } = {}) {
  const COLOR = { ...DEFAULT_MAP_COLORS, ...palette }

  return {
    version: 8,
    name,
    glyphs: OPENFREEMAP_GLYPHS,
    sources: {
      [SOURCE]: { type: 'vector', url: OPENFREEMAP_TILES, attribution: MAP_ATTRIBUTION },
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': COLOR.ground } },

      /* Parks and woodland. Kept close to the ground colour: this map exists to
         show one location, and a bright green block three streets away would
         pull the eye off the pin. */
      {
        id: 'landcover',
        type: 'fill',
        source: SOURCE,
        'source-layer': 'landcover',
        filter: ['in', 'class', 'wood', 'grass', 'farmland'],
        paint: { 'fill-color': COLOR.greenery, 'fill-opacity': 0.7 },
      },
      {
        id: 'park',
        type: 'fill',
        source: SOURCE,
        'source-layer': 'park',
        paint: { 'fill-color': COLOR.greenery, 'fill-opacity': 0.8 },
      },

      /* Water is the layer carrying the brand aqua. */
      {
        id: 'water',
        type: 'fill',
        source: SOURCE,
        'source-layer': 'water',
        filter: ['!=', 'brunnel', 'tunnel'],
        paint: { 'fill-color': COLOR.water },
      },
      {
        id: 'waterway',
        type: 'line',
        source: SOURCE,
        'source-layer': 'waterway',
        paint: { 'line-color': COLOR.waterLine, 'line-width': roadWidth([[9, 0.6], [16, 4]]) },
      },

      {
        id: 'building',
        type: 'fill',
        source: SOURCE,
        'source-layer': 'building',
        minzoom: 13,
        paint: {
          'fill-color': COLOR.building,
          'fill-opacity': { type: 'exponential', base: 1, stops: [[13, 0], [14.5, 1]] },
        },
      },

      /* Roads are drawn casing-first so the darker outline sits under the
         stripe, which is how a road network reads as connected rather than as a
         pile of overlapping rectangles. Minor roads first, motorways last, so
         importance survives the overlap. */
      {
        id: 'road-minor-casing',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['in', 'class', 'minor', 'service', 'track'],
        minzoom: 13,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.roadCasing, 'line-width': roadWidth([[13, 1.4], [18, 12]]) },
      },
      {
        id: 'road-minor',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['in', 'class', 'minor', 'service', 'track'],
        minzoom: 13,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.road, 'line-width': roadWidth([[13, 0.6], [18, 9]]) },
      },
      {
        id: 'road-secondary-casing',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['in', 'class', 'secondary', 'tertiary'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.roadCasing, 'line-width': roadWidth([[10, 1.6], [18, 18]]) },
      },
      {
        id: 'road-secondary',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['in', 'class', 'secondary', 'tertiary'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.road, 'line-width': roadWidth([[10, 0.8], [18, 14]]) },
      },
      /* Primary roads get a deliberate casing/stripe pair at the default zoom. */
      {
        id: 'road-primary-casing',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['in', 'class', 'primary', 'trunk'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.majorCasing, 'line-width': roadWidth([[8, 1.8], [18, 22]]) },
      },
      {
        id: 'road-primary',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['in', 'class', 'primary', 'trunk'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.majorRoad, 'line-width': roadWidth([[8, 1], [18, 17]]) },
      },
      {
        id: 'road-motorway-casing',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['==', 'class', 'motorway'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.motorwayCasing, 'line-width': roadWidth([[6, 1.8], [18, 26]]) },
      },
      {
        id: 'road-motorway',
        type: 'line',
        source: SOURCE,
        'source-layer': 'transportation',
        filter: ['==', 'class', 'motorway'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': COLOR.motorway, 'line-width': roadWidth([[6, 1], [18, 20]]) },
      },

      /* Labels last: text drawn before a fill would be buried by it. */
      {
        id: 'road-label',
        type: 'symbol',
        source: SOURCE,
        'source-layer': 'transportation_name',
        minzoom: 14,
        layout: {
          'symbol-placement': 'line',
          'text-field': ['get', 'name'],
          'text-font': ['Noto Sans Regular'],
          'text-size': roadWidth([[14, 10], [18, 13]]),
        },
        paint: { 'text-color': COLOR.labelMuted, 'text-halo-color': COLOR.halo, 'text-halo-width': 1.4 },
      },
      {
        id: 'water-label',
        type: 'symbol',
        source: SOURCE,
        'source-layer': 'water_name',
        layout: {
          'symbol-placement': 'line',
          'text-field': ['get', 'name'],
          'text-font': ['Noto Sans Italic'],
          'text-size': 12,
        },
        paint: { 'text-color': COLOR.waterLabel, 'text-halo-color': COLOR.halo, 'text-halo-width': 1.4 },
      },
      {
        id: 'place-label',
        type: 'symbol',
        source: SOURCE,
        'source-layer': 'place',
        filter: ['in', 'class', 'city', 'town', 'village', 'suburb', 'neighbourhood'],
        layout: {
          'text-field': ['get', 'name'],
          'text-font': ['Noto Sans Bold'],
          'text-size': [
            'interpolate',
            ['linear'],
            ['zoom'],
            10,
            ['match', ['get', 'class'], 'city', 15, 'town', 13, 11],
            16,
            ['match', ['get', 'class'], 'city', 20, 'town', 17, 14],
          ],
          'text-max-width': 8,
          'text-transform': 'uppercase',
          'text-letter-spacing': 0.06,
        },
        paint: { 'text-color': COLOR.label, 'text-halo-color': COLOR.halo, 'text-halo-width': 1.8 },
      },
    ],
  }
}
