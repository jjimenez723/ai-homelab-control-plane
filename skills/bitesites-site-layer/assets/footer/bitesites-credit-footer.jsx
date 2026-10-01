/*
 * Bite Sites — Credit Footer, React port.
 *
 * The markup is declarative; the morph is not. All the geometry lives in
 * bitesites-credit-footer.js and is written straight onto the node as custom
 * properties, which keeps a scroll-linked animation on the compositor and out of
 * the reconciler. Do not reach for useState here — a scroll handler that
 * re-renders every frame looks fine on a laptop and costs a phone real battery.
 *
 * VENDORED — do not edit. Project overrides live in host.css.
 */

import { useEffect, useId, useRef } from 'react'
import { attach, BITESITES_URL, CHEV_STATES, DEFAULTS } from './bitesites-credit-footer.js'
import './bitesites-credit-footer.css'
import logoFull from './bitesites-logo-full.webp'

const CHEV_W = 146.109375
const CHEV_H = 188.328125
const CHEV_LEFT =
  'M146.109 0L146.109 20.781L60.625 92.641L146.109 166.875L146.109 188.328L0 91.953Z'
const CHEV_RIGHT = 'M0 188.328L0 167.547L85.484 95.688L0 21.453L0 0L146.109 96.375Z'
const CHEV_BITES = [
  [57.465, 43.233],
  [91.602, 18.862],
]
const CHEV_BITE_R = 24.369
/* Resting pose for the first paint: the bare mark, which is where the morph starts. */
const REST = 2

export default function BiteSitesCreditFooter({
  href = BITESITES_URL,
  label = DEFAULTS.label,
  eyebrow = DEFAULTS.eyebrow,
  copy = DEFAULTS.copy,
  onDark = false,
  logoSrc = logoFull,
  ...options
}) {
  const logoRef = useRef(null)
  const maskId = `bitesites-bite-${useId().replace(/:/g, '')}`
  const armPose = (state) =>
    `translate(${state.x[REST]} ${state.y[REST]}) scale(${state.s[REST]})`

  useEffect(() => {
    const handle = attach(logoRef.current, options)
    return () => handle.destroy()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className={`bitesites-credit${onDark ? ' bitesites-credit--on-dark' : ''}`}>
      <div className="bitesites-credit-inner">
        {/* aria-hidden: the anchor's label already says "Made by BiteSites", and a
            screen reader should not hear the attribution twice. */}
        {eyebrow ? (
          <p className="bitesites-credit-eyebrow" aria-hidden="true">
            {eyebrow}
          </p>
        ) : null}
        <a
          ref={logoRef}
          className="bitesites-morph-logo"
          href={href}
          target="_blank"
          /* noopener without noreferrer: the opener stays protected while normal
             referral traffic remains visible to bitesites.org analytics. */
          rel="noopener"
          aria-label={label}
        >
          <span className="bitesites-morph-stage" aria-hidden="true">
            <svg
              className="bitesites-morph-svg"
              viewBox="0 0 500 500"
              fill="currentColor"
              focusable="false"
            >
              <defs>
                <mask
                  id={maskId}
                  maskUnits="userSpaceOnUse"
                  x="-2"
                  y="-2"
                  width={CHEV_W + 4}
                  height={CHEV_H + 4}
                >
                  <rect x="-2" y="-2" width={CHEV_W + 4} height={CHEV_H + 4} fill="#fff" />
                  {CHEV_BITES.map(([cx, cy]) => (
                    <circle key={cx} cx={cx} cy={cy} r={CHEV_BITE_R} fill="#000" />
                  ))}
                </mask>
              </defs>
              <g data-bitesites-arm="left" transform={armPose(CHEV_STATES.left)}>
                <path d={CHEV_LEFT} mask={`url(#${maskId})`} />
              </g>
              <g data-bitesites-arm="right" transform={armPose(CHEV_STATES.right)}>
                <path d={CHEV_RIGHT} />
              </g>
            </svg>
            {/* Both layers are the same full lockup, each clipped to one band of it. */}
            <span className="bitesites-morph-type">
              <img
                className="bitesites-morph-wordline"
                src={logoSrc}
                alt=""
                width="500"
                height="500"
              />
              <img
                className="bitesites-morph-tagline"
                src={logoSrc}
                alt=""
                width="500"
                height="500"
              />
            </span>
          </span>
        </a>
        <p className="bitesites-credit-copy">{copy}</p>
      </div>
    </div>
  )
}
