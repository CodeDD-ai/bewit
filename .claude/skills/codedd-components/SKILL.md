---
name: codedd-components
description: Reuse existing CodeDD marketing components instead of creating duplicates. Apply when building pages, navigation, headers, footers, CTAs, mega menus, or marketing sections.
---

# CodeDD component reuse

Search `src/components/` before creating a new component. Extend data or props; do not fork a lookalike.

## Inventory

| Component | Path | Reuse for |
|-----------|------|-----------|
| `Header` | `src/components/layout/Header.tsx` | Site chrome, frosted nav bar, mobile sheet |
| `MegaMenu` | `src/components/layout/MegaMenu.tsx` | Dropdown / drawer of columns + optional footer row |
| `Footer` | `src/components/layout/Footer.tsx` | Site footer |
| `CodeddButton` | `src/components/ui/CodeddButton.tsx` | PrimeReact buttons |
| `MarketingStub` | `src/components/marketing/MarketingStub.tsx` | Placeholder marketing pages (pass `path` for JSON-LD) |
| `PageJsonLd` / `JsonLd` | `src/components/seo/` | Per-page + raw JSON-LD |
| `createPageMetadata` | `src/lib/seo.ts` | Titles, canonical, Open Graph, Twitter |
| `PageHero` | `src/components/marketing/PageHero.tsx` | Product / marketing page headers (`stacked` or `split`) |
| `SectionIntro` | `src/components/marketing/SectionIntro.tsx` | Centered eyebrow + h2 + lead |
| `FeatureSteps` | `src/components/marketing/FeatureSteps.tsx` | Numbered how-it-works steps |
| `PillarGrid` | `src/components/marketing/PillarGrid.tsx` | Weighted capability cards (AI-Native six pillars) |
| `FigureBand` | `src/components/marketing/FigureBand.tsx` | Full-width explanation plus a `HintFigure` (`stack` or `split`) |
| `MetricDialBand` | `src/components/marketing/MetricDialBand.tsx` | Three-up memo KPIs wrapping `MetricDialCard` |
| `HintFigure` | `src/components/marketing/HintFigure.tsx` | Framed findings / SBOM / licence / activity / DORA / domain / key-person / AI-Native / quality / CLI sketches (`hint`) |
| `ContentPair` | `src/components/marketing/ContentPair.tsx` | Two-column copy + media cells |
| `IntegrationStrip` | `src/components/marketing/IntegrationStrip.tsx` | Live stack lockup with official SVG marks |
| `PrivacyNotes` | `src/components/marketing/PrivacyNotes.tsx` | Scoped data-handling claims |
| `AdvisorPreview` | `src/components/marketing/AdvisorPreview.tsx` | Scripted CodeDD Advisor chat demo |
| `FaqList` | `src/components/marketing/FaqList.tsx` | Visible Q&A (also feeds JSON-LD) |
| `CtaBand` | `src/components/marketing/CtaBand.tsx` | Closing navy band + demo CTA |
| `ProductPageView` | `src/components/marketing/ProductPageView.tsx` | Renders a product page from `src/lib/product-pages/` |
| `CaseStudyView` | `src/components/marketing/case-studies/CaseStudyView.tsx` | Renders a case study page from `src/lib/case-studies/` (registry: `getCaseStudy`, `getAllCaseStudies`) |
| `HomeLanding` | `src/components/marketing/home/` | The `/` landing page. Copy and screen fixtures live in `src/lib/home-page.ts` |
| `Landing*` sections (`LandingHero`, `LandingStatement`, `LandingEnter`, `LandingFigureCards`, `LandingLifecycle`, `LandingSplitFeature`, `LandingPortfolio`, `LandingSecurity`, `LandingGetStarted`, wrapped in `LandingPage`) | `src/components/marketing/home/HomeLanding.tsx` | Segment landing pages built on the home layout, e.g. `/solutions/private-equity` (copy in `src/lib/solutions/private-equity.ts`) |
| `ExecutiveScreen` / `AiNativeScreen` / `PortfolioScreen` | `src/components/marketing/home/ProductScreens.tsx` | Illustrative product screens (traffic lights for verdicts only) |
| `AppChrome` | `src/components/marketing/AppChrome.tsx` | The one window frame (three dots + `codedd.app / slug` title). `HintFigure` and `ScreenFrame` both use it — do not add a second frame style |
| `TrustStrip` | `src/components/marketing/` | Trust row on product pages |

Nav **content** lives in `src/lib/nav.ts`, not in the components.

Local site: `http://localhost:3005` (`SITE_PORT` in `src/lib/site.ts`). Port 3000 is not this website.

## MegaMenu

Pass a `MegaMenuData` object:

```ts
{
  id, label, href,
  columns: [{ title, icon, links: [{ href, label, description }] }],
  footer?: { label, icon, links, cta } // optional divider row
}
```

- `variant="dropdown"` — desktop panel under `Header`
- `variant="drawer"` — stacked layout inside the mobile sheet
- Icons are PrimeIcons (`pi-shield`). Pass the name; the component prefixes `pi`.
- Column count is data-driven (`--mega-col-count`). Products uses 4 columns; other menus may use fewer.
- Do not build a second mega menu. Add columns/links in `nav.ts`.

To add a Products link: edit `productColumns` in `src/lib/nav.ts`. Insight routes are flattened into `productCategories` for `/products/[category]`.

## Header / Footer

- One `Header`, one `Footer`, mounted in `src/app/layout.tsx`.
- Header styles: `Header.module.css` + tokens (`--header-*`).
- Footer styles: `Footer.module.css` + tokens (`--footer-*`).
- Logo on dark surfaces: `/codedd_white.svg`. Logo on light surfaces: `/codedd_navy.svg`. Logo mark only: `/codedd.svg`.

## PageHero

Reuse `PageHero` for product and marketing headers. Do not create a second hero layout.

```tsx
<PageHero
  eyebrow="SAST"
  title="Know which security findings are real"
  description="…"
  variant="stacked" // or "split"
  image={{ src: "/products/sast-hero.svg", alt: "…", width: 1600, height: 840, priority: true }}
/>
```

| Variant | Layout |
|---------|--------|
| `stacked` | Centered eyebrow + h1 + lead + CTA. Image is 100% of the container below. Set `bleed` to span the viewport. |
| `split` | Copy and image share one row from `64rem` (`--bp-lg`). Phone stacks copy, then image. |

Page-specific copy lives in `src/lib/product-pages/` — one file per product, registered in `index.ts` and imported via `src/lib/products.ts`. `PageHero` and `ContentPair` only render.

## ContentPair

Two identical cells: eyebrow (+ optional PrimeIcon), `h2` copy, optional image card. Optional `cta: { href, label }` renders a text link under the copy (e.g. into a related product page).

```tsx
<ContentPair
  items={[
    { eyebrow: "Verified findings", icon: "pi-check-circle", title: "…", image },
    { eyebrow: "Severity in context", icon: "pi-chart-bar", title: "…", image },
  ]}
/>
```

Phone stacks. Two columns from `48rem` (`--bp-md`). Set `linked: true` for a dotted connector between the two sketches. Do not fork a second two-up layout.

Product pages compose sections in `src/lib/product-pages/{slug}.ts`:

`intro` · `steps` · `content-pair` · `pillar-grid` · `figure-band` · `metric-dials` · `integrations` · `privacy` · `trust` · `advisor-preview` · `faq` · `cta-band`

Do not add testimonials or ROI percentages unless they are real. Prefer `hint: "findings" | "owasp" | "cli" | "sbom" | "stack" | "scorecard" | "licence" | "licence-posture" | "licence-detail" | "activity" | "activity-status" | "activity-mix" | "dora" | "dora-trend" | "dora-repos" | "domain" | "domain-table" | "domain-scope" | "key-person" | "key-person-repos" | "key-person-domains" | "ai-native" | "ai-native-depth" | "ai-native-repos" | "ai-native-map" | "ai-dev" | "ai-dev-inventory" | "ai-dev-repos" | "quality" | "quality-health" | "quality-remediate" | "quality-bench" | "debt" | "debt-cli"` over SVG files — `HintFigure` wraps them in a browser or terminal frame. Use `PageHero` `variant="split"` when copy and the sketch should share one row from `--bp-lg`. `FigureBand` is the full-width graph + explanation block (`layout: "split"` from `--bp-lg`). `PillarGrid` is the weighted card set. `metric-dials` is a three-up `MetricDialBand` of `MetricDialCard` from `alternatives/AlternativeBlocks`. Give every `FeatureSteps` item an icon so the row stays unified; do not highlight only one card. `FaqList` is an accordion; answers stay in the HTML. Privacy copy must stay scoped (source erased after audit, CLI local-first). Never claim “your code never leaves” for cloud audits. List only live integrations — not Jira until it ships. Official marks live in `public/integrations/` (SVG, not PNG). Pass `{ label, src }` to `IntegrationStrip`. Not every product page includes Advisor — omit `advisor-preview` when the view is scores and hours, not a findings conversation.

## AdvisorPreview

Scripted CodeDD Advisor demo. Do not wire this to a live chat API. Page copy and Q&A live in `src/lib/product-pages/` (or props if used outside a product page).

```tsx
<AdvisorPreview
  eyebrow="CodeDD Advisor"
  title="Ask which findings actually matter"
  description="…"
  mode="Security"
  auditName="Sample repository audit"
  prompts={[
    {
      question: "Which security findings are about exposed secrets?",
      answer: {
        paragraphs: ["…"],
        findings: [{ title: "…", file: "…", severity: "Critical", estimate: "~2h" }],
        closing: "…",
      },
    },
  ]}
/>
```

The first prompt is the default visible conversation. The rest become suggestion chips. Findings lists and fix-time estimates are illustrative — say so in the panel caption or audit meta.

### Hero images — PNG vs WebP vs SVG

The component accepts all three. Layout does not change. Optimization does:

| Source | Use for | What Next.js does |
|--------|---------|-------------------|
| **SVG** | Logos, diagrams, UI illustrations | Left as SVG (`unoptimized`). Scales without blur. Poor fit for photos or dense screenshots. |
| **PNG** | Product screenshots with text / sharp UI | Author at ~1600px wide. `next/image` serves WebP/AVIF to supporting browsers. |
| **WebP** | Same as PNG, smaller source file | Also fine. Next.js may still emit AVIF. |

Pass intrinsic `width` / `height` so the aspect ratio is reserved (no layout shift). Write a real `alt` that could replace the image. Prefer `/public/products/{slug}-hero.{svg\|webp\|png}`.

## CTAs

- Link-style CTA: `<Link className="btn-codedd btn-codedd-primary">`
- PrimeReact CTA: `<CodeddButton label="…" />`
- Do not invent a third button component.

## New components

1. Check this inventory and `docs/LANDING_COMPONENTS.md`.
2. Put marketing sections in `src/components/marketing/`.
3. Put chrome in `src/components/layout/`.
4. Put primitives in `src/components/ui/`.
5. Style with tokens + `globals.css` utilities. CSS modules are for structure only.
6. Accept content via props or `src/lib/*` — do not hardcode page copy inside a primitive.
