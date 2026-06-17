# Design MD Generator

A small internal tool that takes a **firm name** and **homepage URL**, scrapes
the site, reverse-engineers its branding (colors, fonts, spacing, personality,
and logo), and produces a downloadable `DESIGN.md` in Firecrawl's branding
specification format.

The `DESIGN.md` combines machine-readable design tokens (YAML front matter) with
human-readable sections. Tokens give agents exact values; sections explain how
to apply them.

This runs locally and the process is manual for now — intended for internal use
until the registration flow is integrated.

## Layout

```
design-md-generator/
├── backend/      FastAPI app: scraping + DESIGN.md generation
└── frontend/     React (Vite) form: name + URL -> download
```

## Run it

Two terminals.

### 1. Backend (port 8000)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium      # enables headless-browser logo detection
uvicorn main:app --reload --port 8000
```

`playwright install chromium` downloads a headless browser used for two things:
logo extraction via JS evaluation (always attempted when no inline SVG is found)
and as a last-resort HTTP fallback for JavaScript-challenge sites. Without it,
the tool still works but logo detection is less reliable.

### 2. Frontend (port 5173)

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173, enter a firm name and URL, and click **Generate**.
The browser downloads `<firm-slug>-design.md`. The Vite dev server proxies
`/api` to the backend on port 8000.

You can also hit the backend directly:

```bash
curl -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"firm_name":"Acme Advisors","firm_url":"https://acme.com"}'
```

## Output format

The output matches Firecrawl's branding specification:

```yaml
---
name: "Acme Advisors"
url: "https://acme.com"
logo: embedded          # or a URL if SVG conversion fails
colors:
  primary: "#1A1C1E"
  accent: "#B8422E"
  background: "#F7F5F2"
  text_primary: "#1A1C1E"
  link: "#B8422E"
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
  tone: "Professional"
  energy: "Low"
  audience: "individuals seeking financial advisory services"
---
```

Followed by markdown sections:

- **Colors** — five named roles with hex values
- **Fonts** — body and heading fonts with role labels
- **Typography** — font names and h1/h2/body sizes
- **Spacing** — base unit and border radius
- **Personality** — tone, energy, and audience
- **Logo** — always embedded as SVG (see below)

## Logo handling

Every logo in the output is an SVG:

- **Inline SVG found in DOM** → embedded as-is.
- **`.svg` URL** → file is fetched and inlined.
- **Raster image (PNG/JPG)** → downloaded, base64-encoded, and wrapped in an
  `<svg><image href="data:image/png;base64,..." /></svg>` container.
- **Falls back to the img URL** only if all download attempts fail.

Detection uses two passes:

1. **HTML heuristic** — scores every plausible source (JSON-LD `Organization.logo`,
   `og:logo`, logo-classed `<img>`, brand SVGs in the header/nav, favicons).
2. **Playwright JS pass** — if no inline SVG was found, a headless browser renders
   the page, evaluates a visibility-checking JS finder (`getBoundingClientRect`),
   and extracts the logo from the live DOM. This catches JS-injected logos that
   never appear in the static HTML.

## Notes / limitations

- **Getting past bot defenses.** Fetching escalates through three layers:
  (1) `curl_cffi` impersonating Chrome's TLS fingerprint; (2) plain `httpx`
  if `curl_cffi` isn't installed; (3) headless Chromium via Playwright.
  Sites with advanced Cloudflare/DataDome behavioral analysis may still block
  a headless browser without residential proxies.
- **Font roles** are determined by which font appears in `h1`–`h3` vs `body`
  CSS rules, falling back to frequency ranking. Review the result on serif/display
  fonts that may be used in unexpected roles.
- **Personality** (tone, energy, audience) is inferred from page text and color
  saturation — not guaranteed to be accurate. Always review before use.
- **Extraction is heuristic overall.** Colors are ranked by frequency and
  weighted by background declarations and CSS variable usage. Sizes are parsed
  from CSS rules and converted to px. All values should be reviewed.
