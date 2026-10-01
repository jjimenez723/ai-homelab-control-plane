/*
 * Bite Sites — Credit Footer
 *
 * Dependency-free ES module. `render()` builds the strip and starts it;
 * `attach()` starts it against markup you wrote yourself.
 *
 * VENDORED — do not edit. Project overrides live in host.css.
 */

export const BITESITES_URL = 'https://bitesites.org';

const SVG_NS = 'http://www.w3.org/2000/svg';

/*
 * The chevrons as vector, normalised so each arm's bounding box sits at 0,0 with
 * size CHEV_W x CHEV_H. Placing them by transform is what makes the morph a
 * morph: all three lockups use this identical shape at a uniform scale (measured
 * aspect .776/.781/.779), so the arms genuinely travel between states instead of
 * two stills cross-dissolving.
 */
const CHEV_W = 146.109375;
const CHEV_H = 188.328125;
const CHEV_LEFT =
  'M146.109 0L146.109 20.781L60.625 92.641L146.109 166.875L146.109 188.328L0 91.953Z';
const CHEV_RIGHT = 'M0 188.328L0 167.547L85.484 95.688L0 21.453L0 0L146.109 96.375Z';
/* The two bites out of the left arm's leading edge, in that same local space. */
const CHEV_BITES = [
  [57.465, 43.233],
  [91.602, 18.862],
];
const CHEV_BITE_R = 24.369;

/*
 * Keyframes measured off the three lockups, in the shared 500x500 canvas the
 * artwork is drawn in: [full lockup, wordmark, bare mark]. x/y are the arm's
 * top-left, s scales CHEV_W/CHEV_H to that state's size. The source art has the
 * wordmark state 4px out of vertical alignment and both the wordmark and mark
 * states a few px off centre — harmless under a dissolve, but a vector morph
 * shows it, so these are squared up about x=250.
 */
export const CHEV_STATES = {
  left: { x: [22.5, 5.5, 14], y: [38.5, 172.5, 136], s: [1.273, 0.77, 1.321] },
  right: { x: [291.5, 382, 293], y: [38.5, 172.5, 136], s: [1.273, 0.77, 1.321] },
};
/*
 * The type block ("BITE || SITES" plus the tagline) rides one transform,
 * expressed as an offset from its own resting centre at 252.5,338.5. Glyphs
 * scale uniformly between states — only tracking differs — so one sprite covers
 * every state and nothing has to cross-dissolve.
 */
export const TYPE_STATES = { x: [0, -2.5, -2.5], y: [0, -97.5, -78.5], s: [1, 0.604, 0.42] };

/* Index into the *_STATES arrays that the strip rests at before the first frame. */
const REST = 2;

export const DEFAULTS = {
  href: BITESITES_URL,
  label: 'Made by BiteSites — visit bitesites.org',
  /* The caption above the mark. Pass '' or null to ship the mark uncaptioned,
     which is what the agency's own site does. */
  eyebrow: 'Made by',
  copy: `bitesites.org · © ${new Date().getFullYear()} BiteSites. All rights reserved.`,
  sizes: {
    wide: { expanded: 158, collapsed: 62 },
    compact: { expanded: 124, collapsed: 54 },
  },
  compactViewportWidth: 900,
  minTravel: 90,
  endPad: 16,
  settlePad: 12,
  /* Band at the bottom of the viewport the morph should finish above. 'auto'
     measures whatever the host reserved below the strip; a number pins it; 0 is
     the pre-2.1 behaviour. See measureBottomInset. */
  bottomInset: 'auto',
};

const smoothStep = (start, end, value) => {
  const progress = Math.min(1, Math.max(0, (value - start) / (end - start)));
  return progress * progress * (3 - 2 * progress);
};

/*
 * Catmull-Rom through three keyframes pinned at p = 0, .5 and 1. It passes
 * exactly through each lockup while keeping velocity continuous across the
 * midpoint, so the arms never stall halfway the way easing each half separately
 * would make them.
 */
export const tween3 = ([a, b, c], p) => {
  const first = p < 0.5;
  const u = first ? p * 2 : p * 2 - 1;
  const pa = first ? a : b;
  const pb = first ? b : c;
  const ma = first ? b - a : (c - a) / 2;
  const mb = first ? (c - a) / 2 : c - b;
  const u2 = u * u;
  const u3 = u2 * u;
  return (
    (2 * u3 - 3 * u2 + 1) * pa +
    (u3 - 2 * u2 + u) * ma +
    (3 * u2 - 2 * u3) * pb +
    (u3 - u2) * mb
  );
};

/*
 * 0 is the full lockup (settled), 1 is the bare mark (away). Progress is tied to
 * the strip's own arrival, never to scrollY over document height — that plays
 * the whole sequence below the fold on any page longer than the one it was
 * written against.
 */
export function creditProgress({
  containerTop,
  containerHeight,
  logoCentre,
  viewportHeight,
  bottomInset = 0,
  expanded,
  collapsed,
  minTravel,
  endPad,
  settlePad,
}) {
  /*
   * Run the morph across the logo's own entrance: it starts as the compact mark
   * clears the fold and ends once the expanded lockup is fully on screen, with
   * scroll left to spare. The floor moves the *start*, not the finish — a footer
   * whose morph wants to finish past the bottom of the document never finishes,
   * and stops part-open.
   */
  const enter = logoCentre - collapsed / 2;
  const settled = Math.min(containerHeight - endPad, logoCentre + expanded / 2 + settlePad);
  const travel = Math.max(minTravel, settled - enter);
  /* Everything is measured to the *usable* bottom edge, not the viewport's, so
     a host that reserves a band down there moves the whole window rather than
     spending it on scroll the morph has already finished. */
  const arrived = viewportHeight - bottomInset - containerTop;
  return 1 - Math.min(1, Math.max(0, (arrived - enter) / travel));
}

/*
 * How much document is left below the strip: the band that is still scrollable
 * once the strip's own bottom edge has arrived.
 *
 * A footer's bottom edge is usually the bottom of the document, which is why the
 * morph could measure straight to the viewport's bottom and be right. Hosts
 * routinely break that: a fixed mobile action bar with the matching
 * `body { padding-bottom }`, a cookie strip, a chat launcher's gutter. That band
 * is dead scroll the morph knows nothing about, so the sequence runs a whole
 * band-height too early — on a phone it plays off the bottom of the screen and
 * finishes behind the very bar the padding was reserved for, and the visitor
 * reaches the bottom of the page to a mark that settled 300px ago.
 *
 * Measuring the tail rather than hard-coding a bar height means the strip
 * follows whatever the host actually reserved, including a bar that only exists
 * under a breakpoint, and collapses to 0 — today's behaviour — when the strip
 * really is the last thing in the document.
 */
export function measureBottomInset(containerRect) {
  const documentHeight = Math.max(
    document.documentElement.scrollHeight,
    document.body ? document.body.scrollHeight : 0,
  );
  return Math.max(0, documentHeight - (window.scrollY + containerRect.bottom));
}

let instanceCount = 0;

/*
 * Starts the morph on an existing `.bitesites-morph-logo` element. Returns
 * `{ update, destroy }` — call `destroy()` when the element unmounts, or a
 * single-page app leaks a scroll listener per navigation.
 */
export function attach(node, options = {}) {
  const config = { ...DEFAULTS, ...options };
  const container = config.container ?? node.closest('.bitesites-credit');
  const leftArm = node.querySelector('[data-bitesites-arm="left"]');
  const rightArm = node.querySelector('[data-bitesites-arm="right"]');
  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');

  let frame = 0;
  let lastProgress = -1;
  let lastCompact = null;
  /*
   * The morph needs the strip's own geometry, which costs two rect reads. Skip
   * them entirely while the strip is nowhere near the viewport — this is what
   * keeps a footer animation off the scroll budget for the whole page above it.
   */
  let inRange = false;

  const write = (progress, compact) => {
    const tier = compact ? config.sizes.compact : config.sizes.wide;
    const size = tier.expanded + (tier.collapsed - tier.expanded) * progress;

    node.style.setProperty('--bitesites-logo-size', `${size}px`);
    /*
     * Arms are SVG transform attributes rather than CSS: they animate in the
     * viewBox's own units, and unlike the frame size they cost nothing to reflow.
     */
    for (const [arm, state] of [
      [leftArm, CHEV_STATES.left],
      [rightArm, CHEV_STATES.right],
    ]) {
      if (!arm) continue;
      const x = tween3(state.x, progress).toFixed(2);
      const y = tween3(state.y, progress).toFixed(2);
      const s = tween3(state.s, progress).toFixed(4);
      arm.setAttribute('transform', `translate(${x} ${y}) scale(${s})`);
    }
    /*
     * Percentages resolve against the stage, which is the 500-unit canvas scaled
     * to the frame size, so the same numbers work at every logo size.
     */
    node.style.setProperty('--bitesites-type-x', `${(tween3(TYPE_STATES.x, progress) / 5).toFixed(3)}%`);
    node.style.setProperty('--bitesites-type-y', `${(tween3(TYPE_STATES.y, progress) / 5).toFixed(3)}%`);
    node.style.setProperty('--bitesites-type-scale', tween3(TYPE_STATES.s, progress).toFixed(4));
    /* Fade windows overlap on purpose: two that merely touch leave one frame empty. */
    node.style.setProperty('--bitesites-type-opacity', String(1 - smoothStep(0.58, 0.9, progress)));
    /*
     * The tagline is 8px tall by the time the mark is compact, so it leaves while
     * it is still legible rather than dissolving into a grey smear.
     */
    node.style.setProperty('--bitesites-tag-opacity', String(1 - smoothStep(0.03, 0.24, progress)));

    const contentOpacity = smoothStep(0.1, 0.6, 1 - progress);
    container?.style.setProperty('--bitesites-credit-content-opacity', String(contentOpacity));
    config.onProgress?.(progress, contentOpacity);
  };

  const update = () => {
    frame = 0;
    if (!inRange) return;

    /* Matches the CSS breakpoint where the strip stops having room to spare. */
    const compact = window.innerWidth <= config.compactViewportWidth;
    const tier = compact ? config.sizes.compact : config.sizes.wide;
    const containerRect = container?.getBoundingClientRect();
    const containerTop = containerRect?.top ?? window.innerHeight;
    const containerHeight = containerRect?.height ?? tier.expanded;
    const logoRect = node.getBoundingClientRect();

    /*
     * Clamped, and only ever downwards. The guarantee this rests on is that at
     * maximum scroll the sums cancel to exactly `containerHeight` — which is
     * always more than `settled`, because `settled` is capped at
     * `containerHeight - endPad` — so the morph is certain to finish, with
     * `endPad` of scroll to spare, whatever the viewport height. That holds for
     * any inset at or below the true tail, so clamping down stays safe while
     * clamping up would ask for scroll the page may not have. Half a viewport is
     * the ceiling: past that the settled lockup rides above the middle of the
     * screen and stops reading as the footer arriving.
     */
    const bottomInset = containerRect
      ? Math.min(
          config.bottomInset === 'auto'
            ? measureBottomInset(containerRect)
            : Math.max(0, Number(config.bottomInset) || 0),
          window.innerHeight / 2,
        )
      : 0;

    let progress = creditProgress({
      containerTop,
      containerHeight,
      /* The frame is centred in a fixed-height row, so its centre stays put as it resizes. */
      logoCentre: logoRect.top - containerTop + logoRect.height / 2,
      viewportHeight: window.innerHeight,
      bottomInset,
      expanded: tier.expanded,
      collapsed: tier.collapsed,
      minTravel: config.minTravel,
      endPad: config.endPad,
      settlePad: config.settlePad,
    });

    /*
     * Reduced motion snaps to an end rather than freezing: a visitor who asked
     * for less motion should still get the full lockup at the bottom of the page,
     * in one step instead of across 200px of scroll.
     */
    if (motionQuery.matches) progress = progress > 0.5 ? 1 : 0;
    progress = smoothStep(0, 1, progress);

    /*
     * The frame size is a layout property, so writing it reflows the strip. Past
     * either end the morph is pinned and nothing actually changes, so bail out
     * instead of re-dirtying layout on every remaining scroll frame.
     */
    const quantised = Math.round(progress * 1e3) / 1e3;
    if (quantised === lastProgress && compact === lastCompact) return;
    lastProgress = quantised;
    lastCompact = compact;

    write(progress, compact);
  };

  const queueUpdate = () => {
    if (!frame) frame = window.requestAnimationFrame(update);
  };

  /*
   * Paint the resting pose synchronously, before the observer has fired. Without
   * it the frame reserves its full height and draws a half-placed mark, silently,
   * because a CSS custom property has no "unset" error.
   */
  write(1, window.innerWidth <= config.compactViewportWidth);

  let rangeObserver = null;
  if (container && 'IntersectionObserver' in window) {
    rangeObserver = new IntersectionObserver(
      ([entry]) => {
        inRange = entry.isIntersecting;
        if (inRange) queueUpdate();
      },
      /* About a viewport of lead-in, so the morph is already tracking on arrival. */
      { rootMargin: '100% 0px' },
    );
    rangeObserver.observe(container);
  } else {
    inRange = true;
    update();
  }

  window.addEventListener('scroll', queueUpdate, { passive: true });
  window.addEventListener('resize', queueUpdate);
  motionQuery.addEventListener('change', queueUpdate);

  return {
    update: queueUpdate,
    destroy() {
      rangeObserver?.disconnect();
      window.removeEventListener('scroll', queueUpdate);
      window.removeEventListener('resize', queueUpdate);
      motionQuery.removeEventListener('change', queueUpdate);
      if (frame) window.cancelAnimationFrame(frame);
    },
  };
}

/* Builds the mark's inner SVG + type layers into an existing anchor element. */
export function buildLogo(anchor, { logoSrc, label = DEFAULTS.label } = {}) {
  instanceCount += 1;
  const maskId = `bitesites-bite-${instanceCount}`;

  anchor.classList.add('bitesites-morph-logo');
  anchor.setAttribute('aria-label', label);

  const stage = document.createElement('span');
  stage.className = 'bitesites-morph-stage';
  stage.setAttribute('aria-hidden', 'true');

  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'bitesites-morph-svg');
  svg.setAttribute('viewBox', '0 0 500 500');
  svg.setAttribute('fill', 'currentColor');
  svg.setAttribute('focusable', 'false');

  const defs = document.createElementNS(SVG_NS, 'defs');
  const mask = document.createElementNS(SVG_NS, 'mask');
  mask.setAttribute('id', maskId);
  mask.setAttribute('maskUnits', 'userSpaceOnUse');
  mask.setAttribute('x', '-2');
  mask.setAttribute('y', '-2');
  mask.setAttribute('width', String(CHEV_W + 4));
  mask.setAttribute('height', String(CHEV_H + 4));

  const plate = document.createElementNS(SVG_NS, 'rect');
  plate.setAttribute('x', '-2');
  plate.setAttribute('y', '-2');
  plate.setAttribute('width', String(CHEV_W + 4));
  plate.setAttribute('height', String(CHEV_H + 4));
  plate.setAttribute('fill', '#fff');
  mask.append(plate);

  for (const [cx, cy] of CHEV_BITES) {
    const bite = document.createElementNS(SVG_NS, 'circle');
    bite.setAttribute('cx', String(cx));
    bite.setAttribute('cy', String(cy));
    bite.setAttribute('r', String(CHEV_BITE_R));
    bite.setAttribute('fill', '#000');
    mask.append(bite);
  }
  defs.append(mask);
  svg.append(defs);

  for (const [side, d, masked] of [
    ['left', CHEV_LEFT, true],
    ['right', CHEV_RIGHT, false],
  ]) {
    const state = CHEV_STATES[side];
    const group = document.createElementNS(SVG_NS, 'g');
    group.dataset.bitesitesArm = side;
    group.setAttribute(
      'transform',
      `translate(${state.x[REST]} ${state.y[REST]}) scale(${state.s[REST]})`,
    );
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', d);
    if (masked) path.setAttribute('mask', `url(#${maskId})`);
    group.append(path);
    svg.append(group);
  }
  stage.append(svg);

  /*
   * Both layers are the same full lockup, each clipped to one band of it, so the
   * type keeps its designed letterforms without shipping extra crops.
   */
  const type = document.createElement('span');
  type.className = 'bitesites-morph-type';
  for (const className of ['bitesites-morph-wordline', 'bitesites-morph-tagline']) {
    const img = document.createElement('img');
    img.className = className;
    img.src = logoSrc;
    /* Only the anchor carries an accessible name; three copies of it would be read aloud. */
    img.alt = '';
    img.width = 500;
    img.height = 500;
    type.append(img);
  }
  stage.append(type);
  anchor.append(stage);

  return anchor;
}

/*
 * Builds the whole strip into `mount` and starts it. `logoSrc` is required —
 * the path to bitesites-logo-full.webp as your build serves it.
 */
export function render(mount, options = {}) {
  const config = { ...DEFAULTS, ...options };
  if (!config.logoSrc) throw new Error('bitesites-credit-footer: logoSrc is required');

  const strip = document.createElement('div');
  strip.className = `bitesites-credit${config.onDark ? ' bitesites-credit--on-dark' : ''}`;

  const inner = document.createElement('div');
  inner.className = 'bitesites-credit-inner';

  const anchor = document.createElement('a');
  anchor.href = config.href;
  anchor.target = '_blank';
  /* noopener, deliberately without noreferrer: the opener stays protected while
     normal referral traffic remains visible to bitesites.org analytics. */
  anchor.rel = 'noopener';
  buildLogo(anchor, { logoSrc: config.logoSrc, label: config.label });

  const copy = document.createElement('p');
  copy.className = 'bitesites-credit-copy';
  copy.textContent = config.copy;

  if (config.eyebrow) {
    const eyebrow = document.createElement('p');
    eyebrow.className = 'bitesites-credit-eyebrow';
    eyebrow.textContent = config.eyebrow;
    /* The anchor's label already says "Made by BiteSites"; a screen reader
       should not hear the attribution twice. */
    eyebrow.setAttribute('aria-hidden', 'true');
    inner.append(eyebrow);
  }

  inner.append(anchor, copy);
  strip.append(inner);
  mount.append(strip);

  return { element: strip, ...attach(anchor, { ...config, container: strip }) };
}
