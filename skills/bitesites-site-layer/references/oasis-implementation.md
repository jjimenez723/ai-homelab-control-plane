# Oasis reference implementation

This site keeps the two layers in separate concerns.

## BiteSites footer

The current footer credit is mounted in three surfaces:

- `src/App.jsx` — the main storefront footer;
- `src/seo/pages/ContentChrome.jsx` — content/SEO pages;
- `src/referral/ReferralLanding.jsx` — referral landing page.

Each mounts `<BiteSitesCreditFooter onDark />` inside a `.wrap`, after the footer's primary content. `src/components/host.css` supplies the Oasis-only spacing and uses the local Inter family for the `Made by` eyebrow. The vendored component files and `bitesites-logo-full.webp` stay together in `src/components/`.

The default link is `https://bitesites.org`. The accessible name is `Made by BiteSites — visit bitesites.org`; the visible mark and eyebrow are decorative to assistive technology so the credit is not announced twice. The link uses `target="_blank"` and `rel="noopener"` so referral traffic remains visible to BiteSites analytics while the opener stays protected.

## Oasis map

`src/components/StoreMap.jsx` is the homepage section. `StoreMapCanvas.jsx` is the shared shell used by the homepage, pickup checkout, and cart pickup panel. `StoreMapLive.jsx` is the lazy MapLibre renderer. `src/components/oasis-map-style.js` now exports both:

- `brandedMapStyle({ name, palette })` — reusable style factory;
- `oasisMapStyle()` — the unchanged Oasis wrapper for existing callers.

The published Oasis business data lives in `src/content/defaults.js` and is editable through the content system. The current reference values are:

```js
{
  address: '425 Boulevard, Hasbrouck Heights, NJ 07604',
  phone: '(201) 727-9588',
  mapsUrl: 'https://maps.google.com/?cid=2928276166684654981',
  lat: 40.8691236,
  lng: -74.0713883,
}
```

The Google URL is a business-profile URL, not a free-form address search. The map stores coordinates so drawing never depends on runtime geocoding. The UI still renders the address, phone, hours, and directions link beside the map.

The map style uses OpenFreeMap's public vector tiles and glyphs. MapLibre paints the vector layers with literal Oasis colors: pale ground, green landcover, aqua water, blue-grey buildings/roads, navy labels, and white halos. The Oasis pin uses the generated logo drop path from `oasis-logo-paths.js` and the logo's blue ink. A real marker is anchored at the verified coordinates, so it continues to point to the storefront while the map moves.

The map is intentionally not an iframe. It is lazy-loaded near the viewport, loads MapLibre's worker through Vite's `?worker&url` import, disables rotation, uses cooperative gestures for an interactive locator, and reports style/WebGL/chunk failures without hiding the business details. `attributionControl` remains enabled and the style includes:

```js
OpenFreeMap · © OpenStreetMap
```

That map credit is separate from the BiteSites footer credit.
