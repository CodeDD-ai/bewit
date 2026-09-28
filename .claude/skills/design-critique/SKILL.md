---
name: design-critique
description: Review a CodeDD marketing page as a UI/UX designer — capture it in a real browser at desktop and mobile widths, critique copy, flow, figures, and layout against the ICP rubric, then refine and re-capture until it passes. Use when asked to review, critique, polish, or QA how a page looks or reads, or after rewriting a product/solutions page.
---

# Design critique loop

A page is not done until it has been **seen** at desktop and mobile and passed the rubric below. Reading the source is not enough — alignment, wrapping, card fatigue, and overflow only show up rendered.

## The loop

1. **Capture** the page at both widths (see *Capturing*). Look at every section, top to bottom.
2. **Critique** against the rubric. Write findings as a short table: section · issue · fix · priority (P1 misleads or loses the ICP, P2 clear gain, P3 polish).
3. **Refine** — fix P1 and P2 in the page data (`src/lib/**`) and shared components. Stay inside the design system (`codedd-design-system`, `codedd-components`, `codedd-responsive` skills).
4. **Re-capture** the sections you touched at both widths and confirm each fix. Check `document.documentElement.scrollWidth === innerWidth` on mobile (no horizontal scroll).
5. Run `npm run typecheck` and `npm run lint` (inside the `codedd-website` Docker container if Node is not on the host: `docker exec codedd-website npm run typecheck`).
6. Report: what changed, what you verified, and any P3 you left.

Stop after two refine rounds and report remaining issues instead of looping further.

## Capturing

Local site: `http://localhost:3005` (served from the `codedd-website` Docker container).

**Desktop (1280) and mobile (375) in one command:** headless Edge driven over the DevTools protocol with device emulation, full-page, cut into screen-sized sections. Run from the repo root, then read the PNGs:

```powershell
& "$PWD\.claude\skills\design-critique\shoot.ps1" -Url http://localhost:3005/products/application-security -Name appsec
```

- Output goes to `$env:TEMP\codedd-shots` (`<name>-<width>-NN.png`) unless `-OutDir` is given. `-Widths 1280,768,375` for a figure review.
- `-ReducedMotion` renders animated widgets (e.g. the Advisor preview) in their end state.
- `-Script "<js>"` runs JavaScript before the capture and prints its result — e.g. dispatch `mouseover` on a nav link to open a mega menu, or measure how many lines each menu description takes.
- Each width prints the page height and **whether the page overflows horizontally** — treat any overflow as P1.
- The script scrolls the page once before capturing so scroll-triggered reveals are visible.
- Keep `shoot.ps1` ASCII-only: Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI, and an em dash becomes a curly quote that breaks string parsing.
- Needs only Edge and .NET — no installs. The site blocks framing (`frame-ancestors 'none'`), so iframe tricks will not work; do not weaken that header for screenshots.

**Interactive checks** (hover states, open menus, accordions): use a browser pane with device emulation. Scroll with `window.scrollTo({ top, behavior: "instant" })` — the site uses smooth scrolling. Reset the viewport afterwards. In-pane screenshots can come back blank while the dev server recompiles; wait and retry, or fall back to `shoot.ps1`.

## Rubric

### Audience and message (weight these first)
- **ICP:** PE funds, VCs, investors, companies preparing to sell, CTOs (in portfolio companies and inside PE firms), CEOs. Roughly **70% technical, 30% non-technical** readers — but always **investor language**.
- **Three moments:** pre-deal tech DD · hold period (growth, value creation) · pre-sale preparation. A product page should make clear where it helps in at least one.
- **H1 answers a decision question**, not a feature name ("Which security risks are real — and what it takes to close them", not "Know the issues rules miss").
- **Lead with the verdict, then the method.** Methodology caveats ("N/A is not zero", "this score is not that score") belong in the FAQ, not the body.
- **No internal jargon in body copy:** slice (unless explained), bar, flags red/orange, classified files, LOC-per-dev without context, CLI commands inside paragraphs, UI mechanics ("I switched to the per-repo table").
- **"Estate"** is kept, but define it inline at first use **on every page** ("the estate — every repository in scope"). Visitors land on any page first.
- **No invented proof.** No customer quotes, outcomes, or logos that do not exist. Illustrative sample data is fine if it is plausible.

### Figures (critique each one on its own, not only the layout around it)
- **Message:** what is the one thing this figure should tell an investor or CTO? Cover the copy — does the figure still say it? If not, change the figure, not the caption.
- **Representative:** it must look like the real product. Compare with the app (`C:\CodeDD\codedd_app\src\…`, e.g. `portfolio/TechnologyStack`) — same concepts, labels, pills, and states. Do not show features the product lacks, and do not hide the ones that carry the story (e.g. version-lag and maintenance pills).
- **Design:** a clear visual hierarchy (verdict → breakdown → detail), severity and status colour from the tokens and used consistently (Critical red, High orange, good green), plain labels in body type, mono only for code/metadata, nothing cramped or truncated at 375px.
- **Chart choice:** the chart type fits the data (trend → bars/line over time, share → split bar, comparison → paired values). No decorative charts; no dials for numbers that need context.
- **Drawn to the data:** every bar, segment, ring, and dot is sized from its value (inline `width`/`flex`, not fixed-width classes). A value of 0 draws nothing — say why inline ("no evidence found") instead of a stub.
- **Tone from value bands, not by hand:** derive status colour from the value (e.g. 70+ good, 40–69 fair, under 40 poor) so two different values never share a colour by accident. Status colours on table cells must outrank the base cell colour.
- **SVG charts scale their text with their width** — check label size at every width and cap the chart's `max-width`. Never `preserveAspectRatio="none"` on a chart with dots or text.
- **Tables inside figures and answers:** the column that carries the message stays visible at 375px (drop redundant columns first); words never break mid-word.
- **Alt text** (`LABELS` in `HintFigure.tsx`, `role="img"`) states the figure's message and its key numbers. Update it with every figure change — it is what screen readers and LLM crawlers get.
- Check figures at **1280, 768, and 375** — tablet width is where side-by-side layouts squeeze figures.
- Each figure has one job and a headline number a reader gets in 3 seconds.
- **Numbers reconcile** — inside a figure, between figures on the page, and with the copy (e.g. the hero funnel, the filter view, and the CLI all use the same sample).
- A figure never contradicts the product: no filter chip that its own rows violate, no code diff implying CodeDD writes fixes.
- Engineer-grade detail (file paths, terminals) is fine in the *action* section, not as the only proof of value.

### Flow and layout
- Natural order: problem → verdict → action → where it fits (deal moments) → objections (privacy, FAQ) → CTA. Pages do **not** need identical structures.
- **Card fatigue:** avoid more than two consecutive 3-card grids of the same look; on mobile that is 6–9 stacked cards. Vary with `sequence` steps, a figure band, or a content pair.
- Card titles align across a row (a stray eyebrow in one card breaks it).
- Body text in cards: ≤ 4 lines at desktop, ≤ 6 at 375px. Hero lead ≤ 5 lines at 375px.
- Mono type only for code/metadata, not for plain labels in tables.
- No horizontal scroll at 375px; terminals/code blocks should fit without scrolling where possible (shorten lines).

### Repetition
- **Advisor previews only where one answer combines data a single view cannot, or compares over time** — currently Application security, Technical debt, Team & key-person risk, Development activity. Never on DORA or AI in Development (the Advisor has no data for them). Each answer: verdict sentence first, then a basic chart (`bar` for categories, `line` for trends) and/or a table, a decision-oriented closing line, and the data it combined. Numbers must reconcile with the page's figures, and money stays out (the Advisor reports days, not dollars).
- **Privacy block** only where source access is the real objection (currently Application security). Do not repeat it for its own sake.
- **Git integration strip** only where a connection is needed to measure something (DORA, AI in Development). Tech logo walls that show detection breadth (AI Native) are content, not integrations.

### CTAs
- Hero and closing band: **Start for Free** (primary, `${APP_ORIGIN}/signup`) + **Book a Demo** (outlined). Product pages get these by default from `ProductPageView` — only override with a reason.
- Closing band title is page-specific and states what the reader will see.
