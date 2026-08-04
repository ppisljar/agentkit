/**
 * claudeKit UI theme preset.
 *
 * The kit's components are written against Tailwind's `gray` scale, used MONOTONICALLY: 50 is the
 * lightest surface, 900 the darkest ink. That is the whole reason this preset can exist — a host
 * with a dark UI supplies a reversed ramp and the same markup renders dark, with no `dark:`
 * variants and no per-host forks of the components.
 *
 * Backing the scale with CSS variables rather than renaming ~150 className sites was deliberate:
 * the rename would have been a large, purely visual diff across four apps with no way to prove it
 * changed nothing. Here the defaults ARE the stock Tailwind values, so a host that ignores this
 * preset entirely renders exactly as before.
 *
 * Usage (host tailwind.config.js):
 *     presets: [require('../../claudeKit/ui/tailwind-preset.cjs')]
 * and import the companion `ui/theme.css` for the default values. Override any --ck-gray-* in your
 * own CSS to re-theme; see HomeFlix's theme.css for a dark mapping.
 *
 * Values are raw "R G B" triplets so Tailwind's `<alpha-value>` (bg-white/50 etc.) keeps working.
 */

const gray = {}
for (const step of [50, 100, 200, 300, 400, 500, 600, 700, 800, 900]) {
  gray[step] = `rgb(var(--ck-gray-${step}) / <alpha-value>)`
}

module.exports = {
  theme: {
    extend: {
      colors: {
        gray,
        // `white` and `black` are surfaces and ink too — a dark host needs them inverted or every
        // Card stays a white rectangle no matter what the gray ramp says.
        white: 'rgb(var(--ck-white) / <alpha-value>)',
        black: 'rgb(var(--ck-black) / <alpha-value>)',
        // Label colour on a SOLID accent button. Deliberately not `white`: the accent stays blue
        // (or red) in a dark theme, so its label must stay light even where `white` has been
        // inverted to a dark surface. Sharing one token for both would make one of them illegible.
        onaccent: 'rgb(var(--ck-on-accent) / <alpha-value>)',
      },
    },
  },
}
