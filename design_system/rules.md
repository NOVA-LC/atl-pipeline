# Design System Rules

These 8 rules are enforced in the builder's system prompt. The code critic also
scans output for violations and downgrades scores accordingly.

Refer back to `primitives.css` for the actual tokens.

---

### 1. Color: 3 colors maximum, accent < 8% of viewport
The builder picks `--bg`, `--fg`, `--accent` per brand palette and uses ONLY those
three. No secondary accents, no gradients with extra colors, no decorative tinting
that adds a 4th hue. Accent should appear in CTAs, key highlights, and one
focal element — not as a background fill on large areas.

### 2. Typography: 2 type families maximum
One for display (`--font-display`), one for body (`--font-body`). Often they're
the same family. NEVER three fonts. Default to system stack unless the brand
research surfaces a specific pairing.

### 3. Spacing: only the scale tokens
All padding/margin/gap MUST be one of `--s-1` through `--s-10`. No `padding: 17px`.
No `margin-top: 13px`. The code critic flags any arbitrary length unit.

### 4. Contrast: WCAG AA minimum
Body text: contrast ratio ≥ 4.5:1 against background.
Large text (≥ 24px): ≥ 3:1.
Lighthouse a11y gate (≥ 90) catches violations.

### 5. Mobile hero: one line at 375px
The H1 must fit on a single line in a 375px viewport without wrapping. If the
business name is long, use a shorter pitch line instead. Test with the
responsive technical gate.

### 6. CTA: real link, real target
Every call-to-action button has a real `href` — `tel:`, `mailto:`, an anchor
to a section on the page, or an outbound URL. Never `href="#"` placeholders.
Phone CTAs are mandatory for any service business.

### 7. No emoji icons
On any prospect worth $1K+ (which is every CloseAlone Academy build), emojis
read as template. Use inline SVG icons or no icons. The code critic flags
emoji presence in `<button>`, `<a>`, headings, and nav items.

### 8. No stock photography
Real prospect assets only (origin tracked in `assets_manifest.json`) or a FLUX-
generated atmospheric image. NEVER unsplash / shutterstock / pexels imagery.
Empty slot is preferable to stock.

---

## Enforcement layers

1. **Builder system prompt** — these rules are in the prompt verbatim.
2. **Code critic** — scans rendered HTML for violations (regex + structural).
3. **Vision critic** — judges holistically; rule violations usually show up as
   low Composition / Craft / Color scores.
4. **Technical gates** — Lighthouse a11y catches rule 4; responsive check catches
   rule 5; HTML validation catches malformed CTAs (rule 6).

If a build wants to break a rule for a good reason, the builder writes a
`<!-- design-system-exception: rule-N, reason: ... -->` comment. The code
critic counts exceptions and downgrades sites with >2.
