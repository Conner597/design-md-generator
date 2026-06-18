# Design MD Generator

A small internal tool that takes a **firm name** and **homepage URL**, scrapes
the site's branding via the **Firecrawl API**, and produces a downloadable
`design.md` containing machine-readable design tokens (YAML front matter) and
human-readable sections.

This runs locally and is intended for internal use until the registration flow
is integrated.

## Layout

```
design-md-generator/
├── backend/                FastAPI app: scraping + design.md generation
│   ├── main.py             POST /api/generate endpoint
│   ├── scraper.py          Firecrawl API call, in-house fallback, logo pipeline
│   ├── design_md.py        Renders ScrapeResult → design.md string
│   ├── potrace.exe         Bundled potrace binary (monochrome vectorization)
│   ├── requirements.txt
│   └── test/
│       └── fixtures/
│           └── firecrawl-branding.sample.json   Stripe.com fixture for dev
└── frontend/               React (Vite) form: name + URL → download
```

## Run it

Two terminals.

### 1. Backend (port 8000)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install vtracer              # colour raster → SVG vectorizer
playwright install chromium      # headless browser for logo fallback
uvicorn main:app --reload --port 8000
```

### 2. Frontend (port 5173)

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**, enter a firm name and URL, and click **Generate**.
The browser downloads `<firm-slug>-design.md`.

You can also hit the backend directly:

```bash
curl -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"firm_name":"Acme Advisors","firm_url":"https://acme.com"}'
```

Response: `{ "filename": "acme-advisors-design.md", "content": "---\n..." }`

## Data sources

### Primary — Firecrawl API (`formats: ["branding"]`)

Every generation starts with a call to the Firecrawl `/v1/scrape` endpoint
using `formats: ["branding"]`. This returns a structured `data.branding` object
with LLM-extracted design tokens. The generator maps these directly into the
output — no recomputation of values Firecrawl already provides.

Firecrawl is given a 90-second timeout (the branding format renders the full
page with JavaScript, which takes longer than a plain HTML scrape).

### Fallback — In-house CSS/HTML scraper

If Firecrawl is unavailable (timeout, rate limit) or returns incomplete data
for a token group (colors, fonts, sizes, spacing), the in-house scraper runs
to fill the gaps. It uses:

1. `curl_cffi` impersonating Chrome's TLS fingerprint
2. Plain `httpx` if curl_cffi is unavailable
3. Headless Chromium via Playwright for JS-heavy sites

In-house values are only used for fields where Firecrawl returned null or
nothing — Firecrawl values always take priority.

## Output format

```yaml
---
name: "Acme Advisors"
url: "https://acme.com"
logo: embedded
colors:
  primary: "#1A1C1E"
  secondary: "#4A6FA5"
  accent: "#B8422E"
  background: "#F7F5F2"
  text_primary: "#1A1C1E"
  link: "#B8422E"
fonts:
  - "Public Sans"
  - "Space Grotesk"
typography:
  primary_font: "Public Sans"
  heading_font: "Space Grotesk"
  h1: "48px"
  h2: "32px"
  body: "16px"
spacing:
  base_unit: 4
  border_radius: "4px"
personality:
  tone: "professional"
  energy: "Medium"
  audience: "individuals seeking financial advisory services"
components:
  button_primary:
    background: "#1A1C1E"
    text_color: "#FFFFFF"
    border_radius: "4px"
  button_secondary:
    background: "#FFFFFF"
    text_color: "#1A1C1E"
    border_color: "#1A1C1E"
    border_radius: "4px"
images:
  favicon: "https://acme.com/favicon.svg"
  og_image: "https://acme.com/og.png"
---
```

Followed by markdown sections: Colors, Fonts, Typography, Spacing, Personality,
Components, and Logo & Images.

### Fields

| Section | Fields | Source |
|---------|--------|--------|
| Colors | primary, secondary, accent, background, text_primary, link | Firecrawl → in-house CSS |
| Fonts | list of font family names | Firecrawl → in-house CSS |
| Typography | primary_font, heading_font, h1, h2, body | Firecrawl → in-house CSS |
| Spacing | base_unit (int), border_radius | Firecrawl → in-house CSS |
| Personality | tone, energy, audience | Firecrawl → in-house text analysis |
| Components | button_primary, button_secondary (bg, text_color, border_radius, border_color) | Firecrawl only |
| Images | favicon, og_image | Firecrawl → page metadata |
| Logo | inline path-based SVG (see below) | Two-stage pipeline |

## Logo pipeline

Logo processing has two independent decision points, each logged explicitly.

### Decision 1 — Source (`[LOGO SOURCE]`)

1. **Firecrawl** — `branding.images.logo` is checked first. Firecrawl may
   return the logo as a `data:image/svg+xml;utf8,...` URI (inline SVG) or as
   a plain `https://` URL. Either is accepted.
2. **In-house fallback** — if Firecrawl provides no logo, the in-house scraper
   runs: HTML heuristics score every plausible candidate (JSON-LD, og:logo,
   logo-classed elements, header/nav SVGs), then a Playwright DOM pass extracts
   the logo from the live rendered page.

### Decision 2 — Format (`[LOGO FORMAT]`)

Once a logo source is identified, the format is resolved in priority order:

1. **Vector SVG with path geometry** — if the SVG already contains `<path>`,
   `<polygon>`, `<polyline>`, or `<circle>` elements, it is used directly.
2. **Vectorize raster (colour) via vtracer** — colour raster images are
   converted to path-based SVG using the vtracer Python package.
3. **Vectorize raster (monochrome) via potrace** — monochrome/greyscale images
   are converted using the bundled `potrace.exe`.
4. **Base64 embed** — only used as an absolute last resort if all vectorization
   attempts fail. Logged loudly: `*** LAST RESORT: base64-embedding raster ***`.

The output logo is always an inline SVG. The goal is always a path-based SVG
(`<path d="...">`) so the logo can be restyled with CSS color variables.

## Firecrawl field mapping

How `data.branding` fields map to design.md (camelCase → snake_case adapter
applied internally):

| design.md field | Firecrawl path |
|-----------------|----------------|
| `colors.primary` | `colors.primary` |
| `colors.secondary` | `colors.secondary` |
| `colors.accent` | `colors.accent` |
| `colors.background` | `colors.background` |
| `colors.text_primary` | `colors.textPrimary` |
| `colors.link` | `colors.link` |
| `fonts[]` | `fonts[].family` |
| `typography.primary_font` | `typography.fontFamilies.primary` |
| `typography.heading_font` | `typography.fontFamilies.heading` |
| `typography.h1` | `typography.fontSizes.h1` |
| `typography.h2` | `typography.fontSizes.h2` |
| `typography.body` | `typography.fontSizes.body` |
| `spacing.base_unit` | `spacing.baseUnit` |
| `spacing.border_radius` | `spacing.borderRadius` |
| `personality.tone` | `personality.tone` |
| `personality.energy` | `personality.energy` |
| `personality.audience` | `personality.targetAudience` |
| `components.button_primary` | `components.buttonPrimary` |
| `components.button_secondary` | `components.buttonSecondary` |
| `images.favicon` | `images.favicon` |
| `images.og_image` | `images.ogImage` |
| logo | `images.logo` (decoded from data URI or fetched from URL) |

## Notes / limitations

- **Firecrawl is LLM-based.** Color role classification (which hex is "primary"
  vs "secondary") and personality fields are non-deterministic — the same site
  scraped twice may produce slightly different results. This is inherent to
  Firecrawl's approach and not a bug in the generator. Deterministic fields
  (hex values themselves, font names, pixel sizes, component colors) are stable.

- **Firecrawl timeout.** The branding format renders the full page with
  JavaScript before analysis, which can take 60–90 seconds. Sites with heavy
  bot protection (Cloudflare, DataDome) may cause a 408 timeout on the first
  call. If that happens the in-house fallback runs automatically.

- **Personality fields** (tone, energy, audience) are inferred — always review
  before use.

- **Hex color validation.** The generator accepts 3-char (`#RGB`), 6-char
  (`#RRGGBB`), and 8-char (`#RRGGBBAA`) hex values. Alpha is stripped from
  8-char values. Non-hex formats (rgb(), hsl()) returned by Firecrawl are
  currently not supported and will fall through to the in-house fallback.
