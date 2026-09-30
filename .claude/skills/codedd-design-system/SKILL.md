---
name: codedd-design-system
description: Use the CodeDD token palette and shared CSS utilities instead of inventing new colors, type, or spacing. Apply when adding or restyling UI, writing CSS, or choosing brand colors, buttons, dividers, or typography.
---

# CodeDD design system

Reuse tokens and utilities. Do not introduce a parallel palette, type scale, or one-off hex values.

## Where things live

| What | Path |
|------|------|
| Color, type, space, breakpoints, header tokens | `src/styles/tokens.css` |
| Shared utilities + Tailwind breakpoints | `src/app/globals.css` |
| PrimeReact theme bridge | `src/styles/primereact-overrides.css` |
| Human-readable token table | `docs/DESIGN_SYSTEM.md` |

Import order is already set in `globals.css`. Do not add a second global stylesheet for colors.

## Palette — use the variable, never the hex

```
--codedd-teal / --codedd-teal-dark / --codedd-teal-light / --codedd-teal-muted
--codedd-purple / --codedd-pink / --codedd-green
--codedd-navy / --codedd-navy-light
--codedd-ink / --codedd-muted / --codedd-border
--codedd-surface / --codedd-surface-muted / --codedd-cloud / --codedd-less-white
--codedd-on-navy-muted
--codedd-{good,medium,high,critical}-border / --codedd-unqualified(-tint|-border)
```

Semantic aliases: `--color-primary`, `--color-primary-hover`, `--color-accent-gradient`.

Header chrome: `--header-bg`, `--header-fg`, `--header-fg-muted`, `--header-underline`, `--header-border`, `--header-blur`.
Footer chrome: `--footer-bg`, `--footer-link`, `--footer-link-hover`, `--footer-border`.

If a needed color is missing, add **one token** to `tokens.css` and reuse it. Do not hardcode `#32afc3` in a component.

## Type and layout tokens

- Font: `--font-sans`, `--font-mono`
- Size: `--text-2xs` … `--text-5xl`
- Leading: `--leading-tight`, `--leading-normal`, `--leading-relaxed`
- Radius: `--radius-sm|md|lg|full`
- Shadow: `--shadow-sm|md|brand`
- Layout: `--container-max`, `--section-py`, `--header-height`, `--tap-min`

## Reuse these classes (globals.css)

| Class | Use |
|-------|-----|
| `.container-codedd` | Page / panel max-width + horizontal padding |
| `.section-codedd` | Vertical section padding |
| `.eyebrow-codedd` | Roboto Mono, Lagoon, all caps |
| `.eyebrow-codedd-mark` | Eyebrow plus a teal square (product heroes) |
| `.hero-title-codedd` | Page / product h1 (`--text-3xl` → `--text-5xl`, weight 700) |
| `.hero-lead-codedd` | Hero / section lead (`--text-base` → `--text-lg`, caption gray) |
| `.section-title-codedd` | Section h2 |
| `.figure-codedd` | Roboto Mono key figures |
| `.panel-codedd` / `.panel-dark-codedd` | Quiet panel / navy band |
| `.pill-codedd` + `-good` / `-medium` / `-high` / `-critical` / `-unqualified` | Verdict pills |
| `.btn-codedd-lg` | Larger CTA padding |
| `.divider-codedd` | 1px `--codedd-border` rule |
| `.focus-codedd` | Teal focus ring |
| `.tap-target-codedd` | Minimum 44px hit area |
| `.btn-codedd` + `.btn-codedd-primary` | Teal CTA |
| `.btn-codedd-primary-light` | Teal CTA with white label (landing page; ~2.3:1 contrast, prefer `-primary`) |
| `.btn-codedd-navy` | Navy CTA |
| `.btn-codedd-ghost` | Transparent button |
| `.link-headline-codedd` | Nav / list title |
| `.link-subline-codedd` | Nav / list description |
| `.gradient-text` | Brand gradient on text |

Component CSS modules may add **layout only**. Colors, type, radius, and shadows must come from tokens or the classes above.

## When to expand globals.css

Add a utility there only if a third call site would otherwise copy the same rule. Prefer a token + one class over a new CSS module color.

## PrimeReact

Use `CodeddButton` for PrimeReact actions. Do not restyle `.p-button` inline — extend `primereact-overrides.css` with tokens.
