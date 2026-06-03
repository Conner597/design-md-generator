# Design MD Generator

A small internal tool that takes a **firm name** and **homepage URL**, scrapes
the site, reverse-engineers its design tokens (colors, fonts, radii) and logo,
and produces a downloadable `DESIGN.md`.

The `DESIGN.md` combines machine-readable design tokens (YAML front matter) with
human-readable rationale (markdown prose). Tokens give agents exact values;
prose explains how to apply them.

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
playwright install chromium      # optional: enables the headless-browser fallback
uvicorn main:app --reload --port 8000
```

The `playwright install chromium` step is optional — it downloads a headless
browser used only as a last-resort fallback for sites with JavaScript bot
challenges. Without it, the tool still works using the faster HTTP layers.

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

```
---
name: "Acme Advisors"
url: "https://acme.com"
logo: "https://acme.com/logo.png"   # or `logo: embedded` when an SVG is inlined
colors:
  primary: "#1A1C1E"
  secondary: "#B8422E"
  tertiary: "#6C7278"
  neutral: "#F7F5F2"
typography:
  h1:
    fontFamily: Space Grotesk
    fontSize: 3rem
  body-md:
    fontFamily: Public Sans
    fontSize: 1rem
  label-caps:
    fontFamily: Space Grotesk
    fontSize: 0.75rem
rounded:
  sm: 4px
  md: 8px
spacing:
  sm: 8px
  md: 16px
---

# Acme Advisors

## Overview
...

## Colors
- **Primary** `#1A1C1E`
...

## Logo
<inline SVG>            # when the logo is an SVG
![Acme Advisors logo](https://acme.com/logo.png)   # when it is a raster image
```

## Logo handling

- **SVG logo found** → the raw `<svg>` is embedded inline in the Logo section
  (valid HTML inside markdown), and the front matter records `logo: embedded`.
- **Raster logo (PNG/JPG) found** → the front matter carries `logo: "<url>"` and
  the Logo section references the image by URL (an image isn't embeddable inline,
  so a pointer is used).
- Detection order: logo-hinted inline SVG or SVG in the header/nav → `<img>` with
  "logo" in its class/id/alt/src → `og:image` → touch icon / favicon → first image.

## Notes / limitations

- **Getting past bot defenses.** Fetching escalates through three layers:
  (1) `curl_cffi` impersonating Chrome's TLS fingerprint, which defeats
  fingerprint-based blocks; (2) plain `httpx` if `curl_cffi` isn't installed;
  (3) headless Chromium via Playwright, which runs the page's JavaScript and
  clears most "checking your browser" challenges. The very aggressive setups
  (advanced Cloudflare/DataDome with behavioral analysis + IP reputation) can
  still block even a headless browser without residential proxies — those sites
  may simply not be scrapable with a local tool.
- **Logo detection is scored, not first-match.** Sources are ranked by how
  trustworthy they are as the real brand mark: JSON-LD `Organization.logo` and
  `og:logo` first, then a logo-classed or home-link `<img>`, then a genuine
  brand SVG, then favicons / `og:image`. UI icons (search, menu, map pins, and
  square unlabeled glyphs) are actively rejected so they don't get mistaken for
  the logo. It's still heuristic — review the result.
- Extraction is heuristic overall. Palette assignment ranks colors by frequency
  and classifies them by lightness/saturation; fonts come from `font-family`
  declarations. Font sizes and spacing use sensible defaults.