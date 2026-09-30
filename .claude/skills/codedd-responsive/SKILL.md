---
name: codedd-responsive
description: Mobile-first CodeDD layout using shared breakpoints. Apply when writing CSS, media queries, grids, navigation, mega menus, or any responsive / mobile layout.
---

# CodeDD responsive design

Layout is **mobile first** and **CSS-driven**. Do not use `window.innerWidth` or resize listeners to choose layout. JS may only toggle interaction state (open/closed).

## Canonical breakpoints

Defined in `src/styles/tokens.css` and wired to Tailwind in `src/app/globals.css` `@theme`.

| Token | Value | Tailwind | Typical use |
|-------|-------|----------|-------------|
| (base) | `0` | — | Phone. Default styles. |
| `--bp-sm` | `40rem` (640px) | `sm:` | Large phones |
| `--bp-md` | `48rem` (768px) | `md:` | Tablets, 2-col grids |
| `--bp-lg` | `64rem` (1024px) | `lg:` | Desktop nav, 4-col mega menu |
| `--bp-xl` | `80rem` (1280px) | `xl:` | Wide desktop |
| `--bp-2xl` | `96rem` | `2xl:` | Extra wide |

`@media` in CSS modules **must use these rem values** (custom properties cannot be used in `@media` in all browsers):

```css
/* phone styles first */

@media (min-width: 48rem) { /* --bp-md */ }
@media (min-width: 64rem) { /* --bp-lg */ }
```

Do not introduce `1024px`, `768px`, or a fifth breakpoint without adding it to `tokens.css` **and** `@theme` in `globals.css`.

## How to write layout

1. Style the phone column/stack as the default.
2. Enhance at `md` (2 columns) then `lg` (desktop nav / 4 columns).
3. Prefer CSS Grid + `minmax(0, 1fr)` over fixed pixel widths.
4. Use `.container-codedd` for horizontal padding; do not re-create page gutters.
5. Hit targets on phone: `--tap-min` or `.tap-target-codedd` (44px).
6. Honor `prefers-reduced-motion` for transforms/transitions you add.

## Navigation pattern

- `< lg`: hamburger + sheet. `MegaMenu` uses `variant="drawer"` (stacked columns).
- `>= lg`: inline nav. `MegaMenu` uses `variant="dropdown"` (up to 4 columns + footer row).
- Header/MegaMenu already implement this. Do not add a JS breakpoint fork.

## Page heroes

`PageHero` (`src/components/marketing/PageHero.tsx`) is mobile-first:

- Phone: copy stacks above the image. Image is `width: 100%`; columns use `minmax(0, 1fr)` so nothing overflows.
- `stacked`: stays a single column at every breakpoint. Copy is centered and narrower than the image.
- `split`: becomes two equal columns at `--bp-lg` (`64rem`). Below that it is the phone stack.

Do not add a JS breakpoint to swap stacked/split. Choose the variant in page data.

## Content pairs

`ContentPair` stacks on phone and becomes two equal columns at `--bp-md` (`48rem`). Set `linked: true` for a dotted connector between the two sketches. Cells use `minmax(0, 1fr)` and `overflow-wrap` so long headings do not overflow. Do not fork a second two-up layout.

`PrivacyNotes` becomes three columns at `--bp-md`. `HintFigure` findings hide the file / OWASP columns on smaller screens.

## Tailwind

Use the same steps: unprefixed classes first, then `md:`, then `lg:`.

```tsx
<ul className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
```
