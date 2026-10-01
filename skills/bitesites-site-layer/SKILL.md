---
name: bitesites-site-layer
description: Install or replicate the BiteSites footer attribution and a branded, accurate client-location map in a web site. Use when a site needs the BiteSites credit layer, a custom map that follows the client palette, or both.
---

# BiteSites site layer

Use this skill when BiteSites is building or maintaining any client website or web app that needs the agency attribution and/or a custom location map. Keep the two layers separate: the footer says who built the site; the map keeps its required OpenFreeMap/OpenStreetMap attribution visible. Oasis is only the reference implementation in the optional reference file; do not copy its business data, colors, logo geometry, or naming into another client.

## Prompt shape

For a future site, a concise invocation is: “Use `$bitesites-site-layer`. Add
the BiteSites footer attribution, check Angel for the approved client logo and
brand colors, verify the business listing and doorway coordinates, and add the
custom MapLibre location map with the client's palette.” The skill should then
report any missing Angel reference or location authority before choosing values.

## First collect the client inputs

Do not invent a pin location or derive colors from memory. Obtain:

- the exact public business address, phone, directions/business-profile URL, and verified latitude/longitude;
- the client logo in a usable format, plus the primary, secondary, dark, and light colors as hex values;
- the footer background treatment and whether the footer is dark or light;
- the site's framework/build tool and whether MapLibre is already installed.

For an existing site, find its source of truth for business data before wiring the map. Keep address, phone, directions URL, latitude, and longitude in that data/config layer rather than in the map component.

If the request says to “check Angel,” first look for the approved Angel
reference or asset source available in the current workspace/app. This workspace
does not contain an Angel reference, so do not pretend one was checked: if it
is not available, ask for the exact Angel location or use the logo/brand system
the client supplied.

## Footer attribution

Copy the vendored files from [assets/footer/](assets/footer/) as a unit:

- `bitesites-credit-footer.jsx`
- `bitesites-credit-footer.js`
- `bitesites-credit-footer.css`
- `bitesites-logo-full.webp`

Mount `BiteSitesCreditFooter` as its own block-level sibling of the footer's main content row. Use `onDark` when the footer is dark; otherwise override the CSS custom properties in the host stylesheet. Leave the vendored files unchanged so an upstream BiteSites update can be copied over cleanly. The default accessible label and `https://bitesites.org` link are part of the attribution contract; do not replace them with an unlabeled image.

Import the stylesheet once. If the footer has a fixed mobile dock or other reserved band, reserve that space in the page layout and leave `bottomInset: 'auto'` so the morph measures the real document tail.

## Branded map

Use the neutral MapLibre/OpenFreeMap pattern in [assets/map/](assets/map/) rather than a Google iframe or a CSS hue filter:

1. Install `maplibre-gl` and keep the worker URL import used by `BrandedMapLive.jsx`.
2. Copy `BrandedMapCanvas.jsx`, `BrandedMapLive.jsx`, `branded-map-style.js`, and `map.css` as a set; adapt only the host layout and any project-specific loading/translation hooks.
3. Call `brandedMapStyle({ name, palette })` with the client's literal hex palette. Map styles are JSON/WebGL data and cannot read CSS custom properties.
4. Pass the verified `lat`, `lng`, and directions URL from the business data. The marker must be a real MapLibre `Marker`, not a centered absolutely-positioned logo.
5. Use the client's logo geometry or a simple brand-shaped path for `pinPath`, pass its fitting transform as `pinTransform`, and use the primary brand color for `pinColor`. Keep the pin legible against the map; a white badge with a colored mark is the safe default. The neutral kit does not import Oasis artwork.
6. Keep `attributionControl` enabled. The style's `MAP_ATTRIBUTION` string is required visible credit for OpenFreeMap and OpenStreetMap.
7. Keep the address, phone, hours, and directions link outside the canvas so they remain available when WebGL, tiles, the lazy chunk, or the network fails.

`BrandedMapCanvas` already provides lazy loading, a stable placeholder, no-coordinate protection, an error boundary, and a failure notice. Preserve those behaviors when adapting it. On checkout or another scroll-sensitive panel, use `interactive={false}`.

## Accuracy gate

Before considering a client map complete, verify all of these against the same business listing or an authoritative source:

- the address shown beside the map matches the listing;
- the directions URL opens the intended business profile, not merely a street-address search;
- the pin sits on the correct storefront/entrance at the chosen zoom;
- latitude is between -90 and 90, longitude is between -180 and 180, and neither is `0`/`0`;
- the marker follows the location when the visitor pans or zooms;
- the map still shows the required attribution when compacted on mobile.

If the listing and the doorway disagree, prefer the verified storefront coordinate and record the reason in the site handoff notes. Never silently "improve" a coordinate by geocoding an address at runtime.

## References and bundled code

- Read [references/oasis-implementation.md](references/oasis-implementation.md) only when auditing or updating the Oasis reference implementation.
- Read [references/map-implementation.md](references/map-implementation.md) when adapting the map to a new framework or when checking the failure/performance behavior.
- The files under `assets/` are BiteSites-owned source/assets to copy or adapt; they are not prompts or generic placeholders. Oasis-specific implementation notes live only in the optional reference file.
