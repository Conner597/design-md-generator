"""Scrape a firm's homepage and reverse-engineer design tokens.

Fetching escalates through three layers to get past bot defenses:
  1. curl_cffi with browser TLS impersonation (defeats fingerprint blocks)
  2. plain httpx (fallback when curl_cffi isn't installed)
  3. a headless browser via Playwright (defeats JavaScript challenges)
Layers 1/3 are optional dependencies; the tool degrades gracefully without them.

All extraction is heuristic: it reads the site's published CSS/HTML and makes a
best guess at the palette, fonts, radii, and logo. Output should be reviewed.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import reduce
from math import gcd
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

FIRECRAWL_API_KEY = "fc-ac2034b4859c4dce96715fcbab9da052"
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - optional dependency
    HAS_CURL_CFFI = False

try:
    import vtracer as _vtracer_mod
    HAS_VTRACER = True
except Exception:  # pragma: no cover - optional dependency
    HAS_VTRACER = False

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TRACKING_PARAMS = {
    "gclid", "gad_source", "gad_campaignid", "gbraid", "wbraid", "fbclid",
    "msclkid", "mc_cid", "mc_eid", "yclid", "_hsenc", "_hsmi",
}

HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
RGB_RE = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", re.I)
FONT_RE = re.compile(r"font-family\s*:\s*([^;}{]+)", re.I)
RADIUS_RE = re.compile(r"border-radius\s*:\s*([0-9.]+)px", re.I)
BG_URL_RE = re.compile(r"background(?:-image)?\s*:\s*[^;]*url\(([^)]+)\)", re.I)
CSS_VAR_DEF_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;}{]+)")
VAR_USE_RE = re.compile(r"var\(\s*(--[\w-]+)")
BG_DECL_RE = re.compile(r"background(?:-color)?\s*:\s*([^;}{]+)", re.I)
LOGO_RE = re.compile(r"logo", re.I)
BRAND_HINT = re.compile(r"logo|brand|site-?title|navbar-brand", re.I)
ICON_HINT = re.compile(
    r"\b(icon|search|magnif|menu|hamburger|close|arrow|chevron|caret|social|"
    r"share|cart|toggle|burger|pin|marker|location|map|place|geo|phone|mail|envelope)\b",
    re.I,
)

# Regexes for enhanced token extraction
RULE_BLOCK_RE = re.compile(r'([^{}/]+)\{([^{}]*)\}', re.S)
FONT_SIZE_PROP_RE = re.compile(r'font-size\s*:\s*([^;}{]+)', re.I)
SPACING_VAL_RE = re.compile(
    r'(?:margin|padding)(?:-top|-right|-bottom|-left)?\s*:\s*([0-9.]+)px', re.I
)

GENERIC_FONTS = {
    "inherit", "initial", "unset", "sans-serif", "serif", "monospace",
    "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace", "-apple-system",
    "blinkmacsystemfont", "cursive", "fantasy", "revert", "none",
}

DEFAULT_COLORS = {
    "primary": "#1A1C1E",
    "secondary": "#6C7278",
    "accent": "#6C7278",
    "background": "#F7F5F2",
    "text_primary": "#1A1C1E",
    "link": "#B8422E",
}

_TONE_KEYWORDS: dict[str, set[str]] = {
    "Professional": {
        "finance", "advisory", "investment", "wealth", "consulting", "law", "legal",
        "compliance", "insurance", "accounting", "management", "services", "firm",
    },
    "Creative": {"design", "creative", "studio", "art", "brand", "agency", "portfolio", "visual"},
    "Innovative": {
        "tech", "software", "platform", "ai", "cloud", "digital", "startup",
        "saas", "app", "developer", "api", "data",
    },
    "Friendly": {"community", "family", "kids", "local", "neighborhood", "together", "support"},
    "Luxury": {"luxury", "premium", "exclusive", "bespoke", "estate", "concierge", "prestige"},
}

_ENERGY_HIGH_WORDS = {"dynamic", "fast", "instant", "live", "energetic", "exciting", "vibrant", "bold"}
_ENERGY_LOW_WORDS = {
    "trusted", "steady", "long-term", "stable", "heritage", "established", "conservative", "tradition",
}

_CSS_DUMP_JS = (
    "() => { let out = []; for (const s of document.styleSheets) { try { "
    "for (const r of (s.cssRules || [])) out.push(r.cssText); } catch (e) {} } "
    "return out.join('\\n'); }"
)

# JS executed inside Playwright to find the logo in the rendered DOM with visibility checks.
_LOGO_EXTRACT_JS = """
() => {
    function getComputedFill(el) {
        try {
            const shape = el.querySelector('path,rect,circle,polygon,ellipse');
            if (shape) {
                const f = window.getComputedStyle(shape).fill;
                if (f && f !== 'none') return f;
            }
        } catch(e) {}
        return null;
    }
    function isUIIcon(el) {
        const cls = String(el.className || '') + ' ' + String(el.id || '');
        return /menu|hamburger|search|close|arrow|chevron|social/i.test(cls);
    }
    function hasRealPaths(el) {
        const paths = el.querySelectorAll('path[d],polygon[points],circle[r],rect[width]');
        if (paths.length === 0) return false;
        let total = 0;
        paths.forEach(p => { total += (p.getAttribute('d') || p.getAttribute('points') || '').length; });
        return total >= 20;
    }

    // Primary pass: visible SVGs with a measurable bounding box
    const svgSelectors = [
        'header a svg', 'nav a svg',
        '[class*="logo"] svg', '[id*="logo"] svg', '[class*="brand"] svg',
        'a[href="/"] svg', "a[href='./'] svg",
        '[class*="navbar"] svg', '[class*="nav-brand"] svg'
    ];
    for (const sel of svgSelectors) {
        try {
            for (const el of document.querySelectorAll(sel)) {
                if (el.getAttribute('aria-hidden') === 'true') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 20 || rect.height < 20) continue;
                if (rect.width > 500 || rect.height > 250) continue;
                if (isUIIcon(el)) continue;
                return { type: 'svg', svg: el.outerHTML, computedFill: getComputedFill(el) };
            }
        } catch(e) {}
    }

    // Fallback pass: SVGs with zero/unknown bbox (e.g. inside position:fixed headers)
    // Accept only if they contain substantial path data — rules out tiny UI icons.
    const svgFallbackSelectors = [
        'header svg', 'nav svg',
        '[class*="logo"] svg', '[id*="logo"] svg', '[class*="brand"] svg',
        'a[href="/"] svg', '[class*="navbar"] svg'
    ];
    for (const sel of svgFallbackSelectors) {
        try {
            for (const el of document.querySelectorAll(sel)) {
                if (el.getAttribute('aria-hidden') === 'true') continue;
                if (isUIIcon(el)) continue;
                if (!hasRealPaths(el)) continue;
                return { type: 'svg', svg: el.outerHTML, computedFill: getComputedFill(el) };
            }
        } catch(e) {}
    }

    const imgSelectors = [
        'header img[class*="logo"]', 'nav img[class*="logo"]',
        'img[alt*="logo" i]', 'img[src*="logo" i]',
        '[class*="logo"] img', '[id*="logo"] img',
        'header a img', 'nav a img'
    ];
    for (const sel of imgSelectors) {
        try {
            const el = document.querySelector(sel);
            if (el) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    return { type: 'img', src: el.currentSrc || el.src };
                }
            }
        } catch(e) {}
    }
    return null;
}
"""


@dataclass
class ScrapeResult:
    firm_name: str
    url: str
    colors: dict      # primary, secondary, accent, background, text_primary, link
    fonts: list       # all detected font families ordered by prominence
    typography: dict  # primary_font, heading_font, h1_size, h2_size, body_size
    spacing: dict     # base_unit (int), border_radius (str)
    personality: dict # tone, energy, audience
    logo: dict        # type, svg, url
    components: dict = field(default_factory=dict)  # buttonPrimary, buttonSecondary
    images: dict = field(default_factory=dict)      # favicon, ogImage


# --------------------------------------------------------------------------- #
# Color helpers
# --------------------------------------------------------------------------- #
def _normalize_hex(value: str) -> str:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    clamp = lambda v: max(0, min(255, v))
    return "#{:02X}{:02X}{:02X}".format(clamp(r), clamp(g), clamp(b))


def _hex_to_hsl(hex_str: str) -> tuple[float, float, float]:
    h = hex_str.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    light = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, light
    delta = mx - mn
    sat = delta / (2 - mx - mn) if light > 0.5 else delta / (mx + mn)
    if mx == r:
        hue = ((g - b) / delta) % 6
    elif mx == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    return hue * 60, sat, light


def _hsv_sat(hex_str: str) -> float:
    """HSV saturation: colorfulness independent of darkness."""
    h = hex_str.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


def _is_chromatic(hex_str: str) -> bool:
    return _hsv_sat(hex_str) > 0.15


# --------------------------------------------------------------------------- #
# Fetching (layered anti-bot)
# --------------------------------------------------------------------------- #
def _clean_url(url: str) -> str:
    if not urlparse(url).scheme:
        url = "https://" + url
    parts = urlparse(url)
    query = {k: v for k, v in parse_qs(parts.query).items() if k.lower() not in TRACKING_PARAMS}
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def _root(url: str) -> str:
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, "/", "", "", ""))


@contextmanager
def _session():
    """Yield an HTTP session: curl_cffi (impersonating Chrome) if available, else httpx."""
    if HAS_CURL_CFFI:
        sess = cffi_requests.Session()
        try:
            yield sess
        finally:
            sess.close()
    else:
        client = httpx.Client(follow_redirects=True, timeout=20.0, headers=BROWSER_HEADERS)
        try:
            yield client
        finally:
            client.close()


def _get(session, url: str):
    if HAS_CURL_CFFI:
        resp = session.get(url, impersonate="chrome", timeout=25, allow_redirects=True)
    else:
        resp = session.get(url)
    resp.raise_for_status()
    return resp


def _browser_fetch(url: str) -> tuple[str, str, str, dict | None]:
    """Render in headless Chromium; also extracts logo via JS evaluation.

    Returns (final_url, html, css, playwright_logo_hint).
    """
    from playwright.sync_api import sync_playwright  # optional dependency

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=40_000)
            html = page.content()
            final_url = page.url
            css = page.evaluate(_CSS_DUMP_JS)
            try:
                logo_hint = page.evaluate(_LOGO_EXTRACT_JS)
            except Exception:
                logo_hint = None
        finally:
            browser.close()
    return final_url, html, css or "", logo_hint


def fetch_site(url: str) -> tuple[str, str, str, dict | None]:
    """Return (final_url, html, combined_css, playwright_logo_hint), escalating through anti-bot layers."""
    cleaned = _clean_url(url)
    candidates = [cleaned]
    if _root(cleaned) != cleaned:
        candidates.append(_root(cleaned))

    last_error: Exception | None = None
    with _session() as sess:
        for candidate in candidates:
            try:
                resp = _get(sess, candidate)
                final_url = str(resp.url)
                html = resp.text
                return final_url, html, _gather_css(sess, html, final_url), None
            except Exception as exc:  # noqa: BLE001
                last_error = exc

    try:
        return _browser_fetch(cleaned)
    except Exception as exc:  # noqa: BLE001
        last_error = last_error or exc
    raise last_error if last_error else RuntimeError("Could not fetch the site.")


def _gather_css(session, html: str, base_url: str, max_sheets: int = 8) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    for style in soup.find_all("style"):
        if style.string:
            parts.append(style.string)
    for el in soup.find_all(style=True):
        parts.append(el["style"])
    hrefs: list[str] = []
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        rel = [rel] if isinstance(rel, str) else rel
        if any("stylesheet" in str(r).lower() for r in rel) and link.get("href"):
            hrefs.append(urljoin(base_url, link["href"]))
    for href in hrefs[:max_sheets]:
        try:
            parts.append(_get(session, href).text[:500_000])
        except Exception:
            continue
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Token extraction
# --------------------------------------------------------------------------- #
def _collect_color_weights(css: str, html: str) -> dict:
    """Tally colors, weighting the signals that actually indicate a brand color."""
    weights: dict[str, int] = {}

    def add(hex_value: str, amount: int = 1) -> None:
        weights[hex_value] = weights.get(hex_value, 0) + amount

    def colors_in(blob: str) -> list[str]:
        out = [_normalize_hex(m) for m in HEX_RE.findall(blob)]
        out += [_rgb_to_hex(int(r), int(g), int(b)) for r, g, b in RGB_RE.findall(blob)]
        return out

    text = css + " " + html
    for hx in colors_in(text):
        add(hx, 1)
    for decl in BG_DECL_RE.findall(css):
        for hx in colors_in(decl):
            add(hx, 3)
    # Skip CSS variables from known UI frameworks — their palette tokens swamp brand colors.
    _FRAMEWORK_PREFIXES = ("--bs-", "--tw-", "--mdb-", "--pico-", "--chakra-", "--ant-")
    var_hex: dict[str, str] = {}
    for name, value in CSS_VAR_DEF_RE.findall(css):
        if any(name.startswith(p) for p in _FRAMEWORK_PREFIXES):
            continue
        found = colors_in(value)
        if found:
            var_hex[name] = found[0]
    for name in VAR_USE_RE.findall(css):
        if name in var_hex:
            add(var_hex[name], 3)
    meta = BeautifulSoup(html, "html.parser").find("meta", attrs={"name": "theme-color"})
    if meta and meta.get("content"):
        for hx in colors_in(meta["content"]):
            add(hx, 25)
    return weights


def _extract_link_color(css: str, fallback: str, background: str) -> str:
    """Find the color declared on bare <a> elements (not hover/active/visited).

    Skips any candidate that is too close to the background — that would mean
    links are invisible (e.g. a white label on a colored button container).
    """
    for m in RULE_BLOCK_RE.finditer(css):
        sel = m.group(1).strip()
        if re.search(r'\ba\b', sel) and not re.search(r'hover|visited|active|focus', sel, re.I):
            hexes = [_normalize_hex(h) for h in HEX_RE.findall(m.group(2))]
            hexes += [_rgb_to_hex(int(r), int(g), int(b)) for r, g, b in RGB_RE.findall(m.group(2))]
            for hx in hexes:
                if hx != background and _hex_to_hsl(hx)[2] < 0.9:
                    return hx
    return fallback


def extract_colors(css: str, html: str) -> dict:
    weights = _collect_color_weights(css, html)
    if not weights:
        return dict(DEFAULT_COLORS)

    ranked = [h for h, _ in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)]
    chromatic = [h for h in ranked if _is_chromatic(h)]
    light_colors = [h for h in ranked if _hex_to_hsl(h)[2] > 0.82]
    dark_colors = [h for h in ranked if _hex_to_hsl(h)[2] < 0.35]

    primary = chromatic[0] if chromatic else (dark_colors[0] if dark_colors else ranked[0])
    accent = chromatic[1] if len(chromatic) > 1 else primary
    background = light_colors[0] if light_colors else "#FFFFFF"
    text_primary = dark_colors[0] if dark_colors else primary
    link = _extract_link_color(css, primary, background)

    return {
        "primary": primary,
        "accent": accent,
        "background": background,
        "text_primary": text_primary,
        "link": link,
    }


def _to_px(value: str, base: int = 16) -> str | None:
    """Convert a CSS length (px/rem/em) to a px string, or return None if unparseable."""
    v = value.strip().lower()
    m = re.match(r'^([0-9.]+)(px|rem|em)$', v)
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2)
    if unit == 'px':
        return f"{int(round(num))}px"
    return f"{int(round(num * base))}px"


def _size_for_selector(css: str, selector_pattern: str) -> str | None:
    """Return the font-size for the first CSS rule whose selector matches the pattern."""
    pattern = re.compile(selector_pattern, re.I)
    for m in RULE_BLOCK_RE.finditer(css):
        sel = m.group(1).strip()
        if pattern.search(sel) and not re.search(r'@media|@supports', sel, re.I):
            fs_m = FONT_SIZE_PROP_RE.search(m.group(2))
            if fs_m:
                result = _to_px(fs_m.group(1).strip())
                if result:
                    return result
    return None


def _font_in_selector(css: str, selector_pattern: str) -> str | None:
    """Return the first named font-family found in rules matching selector_pattern."""
    pattern = re.compile(selector_pattern, re.I)
    for m in RULE_BLOCK_RE.finditer(css):
        sel = m.group(1).strip()
        if pattern.search(sel) and not re.search(r'@media|@supports|@keyframes', sel, re.I):
            ff_m = FONT_RE.search(m.group(2))
            if ff_m:
                first = ff_m.group(1).split(",")[0].strip().strip("'\"")
                if first and first.lower() not in GENERIC_FONTS and not first.lower().startswith("var("):
                    return first
    return None


def extract_typography(css: str) -> dict:
    freq: dict[str, int] = {}
    for raw in FONT_RE.findall(css):
        first = raw.split(",")[0].strip().strip("'\"")
        if first and first.lower() not in GENERIC_FONTS and not first.lower().startswith("var("):
            freq[first] = freq.get(first, 0) + 1
    ranked_fonts = [f for f, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)]

    # Look for fonts explicitly used in heading vs body selectors — more reliable
    # than raw frequency, which can get the roles backwards on sites that define
    # heading styles with more declarations than body styles.
    heading_from_css = _font_in_selector(css, r'\bh[1-3]\b')
    body_from_css = _font_in_selector(css, r'\bbody\b')

    if heading_from_css and body_from_css and heading_from_css != body_from_css:
        primary_font = body_from_css
        heading_font = heading_from_css
    elif heading_from_css:
        # We know the heading font; pick the most-frequent *other* font for body.
        primary_font = next((f for f in ranked_fonts if f != heading_from_css), heading_from_css)
        heading_font = heading_from_css
    elif body_from_css:
        primary_font = body_from_css
        heading_font = next((f for f in ranked_fonts if f != body_from_css), body_from_css)
    else:
        primary_font = ranked_fonts[0] if ranked_fonts else "Public Sans"
        heading_font = ranked_fonts[1] if len(ranked_fonts) > 1 else primary_font

    h1_size = _size_for_selector(css, r'\bh1\b') or "48px"
    h2_size = _size_for_selector(css, r'\bh2\b') or "32px"
    body_size = (
        _size_for_selector(css, r'\bbody\b')
        or _size_for_selector(css, r'\bp\b')
        or "16px"
    )

    return {
        "primary_font": primary_font,
        "heading_font": heading_font,
        "h1_size": h1_size,
        "h2_size": h2_size,
        "body_size": body_size,
    }


def extract_spacing(css: str) -> dict:
    radius_vals = [
        int(round(float(v))) for v in RADIUS_RE.findall(css) if 0 < float(v) <= 64
    ]
    border_radius = (
        f"{Counter(radius_vals).most_common(1)[0][0]}px" if radius_vals else "0px"
    )

    spacing_vals = [
        int(round(float(v))) for v in SPACING_VAL_RE.findall(css) if 0 < float(v) <= 128
    ]
    base_unit = 4
    if len(spacing_vals) >= 3:
        small_vals = sorted(set(v for v in spacing_vals if v >= 2))[:20]
        if small_vals:
            try:
                g = reduce(gcd, small_vals)
                if g in (2, 4, 6, 8, 10, 12, 16):
                    base_unit = g
            except Exception:
                pass

    return {"base_unit": base_unit, "border_radius": border_radius}


def extract_personality(html: str, colors: dict) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(t for t in soup.stripped_strings)[:8000].lower()

    tone = "Professional"
    best_score = 0
    for tone_name, keywords in _TONE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score, tone = score, tone_name

    primary_sat = _hsv_sat(colors.get("primary", "#1A1C1E"))
    accent_sat = _hsv_sat(colors.get("accent", colors.get("primary", "#1A1C1E")))
    avg_sat = (primary_sat + accent_sat) / 2

    if any(w in text for w in _ENERGY_LOW_WORDS):
        energy = "Low"
    elif avg_sat > 0.55 or any(w in text for w in _ENERGY_HIGH_WORDS):
        energy = "High"
    elif avg_sat > 0.25:
        energy = "Medium"
    else:
        energy = "Low"

    def _trim_at_word(s: str, limit: int) -> str:
        if len(s) <= limit:
            return s
        return s[:limit].rsplit(" ", 1)[0].rstrip(".,;:")

    audience = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        desc = meta_desc["content"].strip()
        for pattern in [r"for ([^.]+)", r"serving ([^.]+)", r"helping ([^.]+)"]:
            m = re.search(pattern, desc, re.I)
            if m:
                audience = _trim_at_word(m.group(1).strip().rstrip(",."), 100)
                break
        if not audience:
            audience = _trim_at_word(desc, 150)
    if not audience:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            audience = _trim_at_word(og_desc["content"].strip(), 150)
    if not audience:
        audience = "General"

    return {"tone": tone, "energy": energy, "audience": audience}


# --------------------------------------------------------------------------- #
# Logo detection (HTML-based: scored heuristics)
# --------------------------------------------------------------------------- #
def _attr_blob(el) -> str:
    cls = " ".join(el.get("class") or [])
    return f"{cls} {el.get('id') or ''} {el.get('alt') or ''} {el.get('aria-label') or ''}".lower()


def _in_header(el) -> bool:
    return any(getattr(p, "name", None) in ("header", "nav") for p in el.parents)


def _has_brand_ancestor(el) -> bool:
    for parent in el.parents:
        blob = f"{' '.join(parent.get('class') or [])} {parent.get('id') or ''}"
        if BRAND_HINT.search(blob):
            return True
        if getattr(parent, "name", None) == "a" and (parent.get("href") or "") in ("/", "#"):
            return True
    return False


def _svg_is_square(svg) -> bool:
    vb = svg.get("viewBox") or svg.get("viewbox")
    if vb:
        nums = re.findall(r"-?\d+\.?\d*", vb)
        if len(nums) == 4 and float(nums[2]) and float(nums[3]):
            ratio = abs(float(nums[2]) / float(nums[3]))
            return 0.7 <= ratio <= 1.4
    w = re.findall(r"\d+\.?\d*", svg.get("width") or "")
    h = re.findall(r"\d+\.?\d*", svg.get("height") or "")
    if w and h and float(w[0]) and float(h[0]):
        return 0.7 <= float(w[0]) / float(h[0]) <= 1.4
    return False


def _svg_looks_like_logo(svg) -> bool:
    """Accept a brand-mark SVG; reject UI icons (search, menu, map pins, etc.)."""
    self_blob = _attr_blob(svg)
    self_branded = bool(BRAND_HINT.search(self_blob))
    if not (self_branded or _has_brand_ancestor(svg)):
        return False
    if svg.get("aria-hidden") == "true":
        return False
    if ICON_HINT.search(self_blob) and not self_branded:
        return False
    if (svg.get("width") or "").strip().lower().endswith("em") and not self_branded:
        return False
    if _svg_is_square(svg) and not self_branded:
        return False
    return True


def _real_img_url(img) -> str | None:
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        val = (img.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            return val
    for attr in ("srcset", "data-srcset"):
        val = img.get(attr) or ""
        first = val.split(",")[0].strip().split(" ")[0]
        if first and not first.startswith("data:"):
            return first
    return None


def _is_tiny(img) -> bool:
    for dim in (img.get("width"), img.get("height")):
        m = re.match(r"\s*(\d+)", dim or "")
        if m and int(m.group(1)) <= 24:
            return True
    return False


def _find_logo_in_jsonld(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        logo = node.get("logo")
        if isinstance(logo, str):
            found.append(logo)
        elif isinstance(logo, dict) and isinstance(logo.get("url"), str):
            found.append(logo["url"])
        for value in node.values():
            found.extend(_find_logo_in_jsonld(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_logo_in_jsonld(item))
    return found


def _jsonld_logos(soup) -> list[str]:
    out: list[str] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            out.extend(_find_logo_in_jsonld(json.loads(tag.string or "")))
        except Exception:
            continue
    return out


def _icon_href(soup, want: str) -> str | None:
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        rel = [rel] if isinstance(rel, str) else rel
        if any(want in str(r).lower() for r in rel) and link.get("href"):
            return link["href"]
    return None


def extract_logo(html: str, base_url: str) -> dict:
    """Score every plausible logo source and return the best one.

    Higher score = more trustworthy as the actual brand mark. Explicit
    declarations (JSON-LD, og:logo) beat the home-link/logo-classed image, which
    beats a genuine brand SVG, which beats favicons and share images.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[int, str, str]] = []  # (score, kind, payload)

    for ref in _jsonld_logos(soup):
        if ref and not ref.startswith("data:"):
            candidates.append((100, "img", urljoin(base_url, ref)))

    og_logo = soup.find("meta", property="og:logo")
    if og_logo and og_logo.get("content"):
        candidates.append((90, "img", urljoin(base_url, og_logo["content"])))

    # First img inside a header/nav anchor link — almost always the main logo.
    for sel_parent in ("header", "nav"):
        for anchor in soup.find_all(sel_parent):
            img = anchor.find("a", href=True) and anchor.find("img")
            if not img:
                img = anchor.find("img")
            if img:
                src = _real_img_url(img)
                if src and not _is_tiny(img):
                    candidates.append((88, "img", urljoin(base_url, src)))
                break

    for img in soup.find_all("img"):
        src = _real_img_url(img)
        if not src:
            continue
        score = 0
        if "logo" in _attr_blob(img):
            score += 50
        if "logo" in src.lower():
            score += 30
        if _has_brand_ancestor(img):
            score += 40
        if _in_header(img):
            score += 45
        if _is_tiny(img):
            score -= 25
        if score > 0:
            candidates.append((min(score, 95), "img", urljoin(base_url, src)))

    for svg in soup.find_all("svg"):
        if _svg_looks_like_logo(svg):
            score = 94 if BRAND_HINT.search(_attr_blob(svg)) else 72
            candidates.append((score, "svg", str(svg)))

    for el in soup.find_all(class_=LOGO_RE) + soup.find_all(id=LOGO_RE):
        match = BG_URL_RE.search(el.get("style") or "")
        if match:
            ref = match.group(1).strip().strip("'\"")
            if ref and not ref.startswith("data:"):
                candidates.append((45, "img", urljoin(base_url, ref)))

    apple = _icon_href(soup, "apple-touch-icon")
    if apple:
        candidates.append((22, "img", urljoin(base_url, apple)))
    icon = _icon_href(soup, "icon")
    if icon:
        candidates.append((14, "img", urljoin(base_url, icon)))
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        candidates.append((16, "img", urljoin(base_url, og_image["content"])))

    if not candidates:
        return {"type": "none", "url": None, "svg": None}
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, kind, payload = candidates[0]
    if kind == "svg":
        return {"type": "svg", "svg": payload, "url": None}
    return {"type": "img", "url": payload, "svg": None}


def _fetch_svg_file(url: str) -> str | None:
    """Download a URL and return its content if it is (or contains) SVG markup."""
    try:
        with _session() as sess:
            resp = _get(sess, url)
            ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
            text = resp.text if hasattr(resp, "text") else resp.content.decode("utf-8", errors="replace")
            if ct == "image/svg+xml" or "<svg" in text.lower():
                return text.strip()
    except Exception:
        pass
    return None


def _svg_has_vector_paths(svg_text: str) -> bool:
    """True when SVG contains actual vector geometry (not just a base64 <image> wrapper)."""
    if not svg_text or "<svg" not in svg_text.lower():
        return False
    return bool(re.search(
        r'<(path|polygon|polyline|circle|ellipse|rect|line)\b[^>]*\b(d|points|r|width|x1)=',
        svg_text, re.I,
    ))


def _vtracer_vectorize(raw: bytes) -> str | None:
    """Vectorize raw image bytes using vtracer (colour-aware path tracing)."""
    import tempfile, os
    if not HAS_VTRACER:
        return None
    inp_fd, inp_path = tempfile.mkstemp(suffix=".png")
    out_fd, out_path = tempfile.mkstemp(suffix=".svg")
    os.close(inp_fd)
    os.close(out_fd)
    try:
        with open(inp_path, "wb") as f:
            f.write(raw)
        _vtracer_mod.convert_image_to_svg_py(inp_path, out_path)
        with open(out_path, "r", encoding="utf-8") as f:
            svg = f.read()
        return svg if "<svg" in svg.lower() else None
    except Exception:
        return None
    finally:
        for p in (inp_path, out_path):
            try:
                os.unlink(p)
            except Exception:
                pass


def _potrace_vectorize(raw: bytes) -> str | None:
    """Vectorize raw image bytes using potrace (monochrome path tracing)."""
    import subprocess, tempfile, os
    from PIL import Image
    import io as _io

    potrace_bin = os.path.join(os.path.dirname(__file__), "potrace.exe")
    if not os.path.isfile(potrace_bin):
        potrace_bin = "potrace"

    try:
        img = Image.open(_io.BytesIO(raw)).convert("RGBA")
        r, g, b, a = img.split()
        if min(a.getdata()) < 200:
            bw = a.point(lambda v: 0 if v > 128 else 255)
        else:
            gray = img.convert("L")
            w, h = gray.size
            corners = [gray.getpixel((0, 0)), gray.getpixel((w - 1, 0)),
                       gray.getpixel((0, h - 1)), gray.getpixel((w - 1, h - 1))]
            if sum(corners) / 4 > 128:
                bw = gray.point(lambda v: 0 if v < 128 else 255)
            else:
                bw = gray.point(lambda v: 0 if v > 128 else 255)
        bw = bw.convert("1")

        pbm_fd, pbm_path = tempfile.mkstemp(suffix=".pbm")
        svg_fd, svg_path = tempfile.mkstemp(suffix=".svg")
        os.close(pbm_fd)
        os.close(svg_fd)
        try:
            bw.save(pbm_path)
            result = subprocess.run(
                [potrace_bin, pbm_path, "-s", "--flat", "-o", svg_path],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg = f.read()
                if "<svg" in svg.lower():
                    svg = svg.replace('fill="#000000"', 'fill="currentColor"')
                    return svg.strip()
        finally:
            for p in (pbm_path, svg_path):
                try:
                    os.unlink(p)
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _vectorize_image_bytes_to_svg(raw: bytes) -> str | None:
    """Convert raw image bytes to a path-based SVG.

    Colour images → vtracer (faithful colour tracing, installed).
    Monochrome images → potrace (1-bit, bundled exe).
    Returns None when both fail; caller falls back to base64 embed.
    """
    from PIL import Image
    import io as _io

    try:
        rgb_small = Image.open(_io.BytesIO(raw)).convert("RGB").resize((32, 32))
        pixels = list(rgb_small.getdata())
        colorful = sum(
            1 for r, g, b in pixels
            if max(r, g, b) - min(r, g, b) > 40
            and not (r > 200 and g > 200 and b > 200)
        )
        is_multicolor = colorful > len(pixels) * 0.04
    except Exception:
        is_multicolor = False

    if is_multicolor:
        if HAS_VTRACER:
            svg = _vtracer_vectorize(raw)
            if svg:
                logger.debug("[VECTORIZE] vtracer succeeded for colour image")
                return svg
            logger.warning("[VECTORIZE] vtracer failed for colour image; trying potrace (colour will be lost)")
        else:
            logger.info("[VECTORIZE] Colour image but vtracer not available; potrace will be lossy")

    return _potrace_vectorize(raw)


def _vectorize_to_svg(url: str) -> str | None:
    """Download a raster image and vectorize it to a path-based SVG using potrace."""
    try:
        with _session() as sess:
            resp = _get(sess, url)
            ct = (resp.headers.get("content-type") or "image/png").split(";")[0].strip()
            if ct == "image/svg+xml":
                text = resp.text if hasattr(resp, "text") else resp.content.decode("utf-8", errors="replace")
                if "<svg" in text.lower():
                    return text.strip()
            return _vectorize_image_bytes_to_svg(resp.content)
    except Exception:
        pass
    return None


def _raster_url_to_svg(url: str) -> str | None:
    """Download a raster image and embed it base64-encoded inside an SVG wrapper.

    Last resort only — used when potrace vectorization and all other methods fail.
    If the server returns SVG content despite a non-.svg extension, inlines it directly.
    """
    import base64 as _base64
    try:
        with _session() as sess:
            resp = _get(sess, url)
            ct = (resp.headers.get("content-type") or "image/png").split(";")[0].strip()
            if ct == "image/svg+xml":
                text = resp.text if hasattr(resp, "text") else resp.content.decode("utf-8", errors="replace")
                if "<svg" in text.lower():
                    return text.strip()
            raw = resp.content
            if ct not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                ct = "image/png"
            b64 = _base64.b64encode(raw).decode("ascii")
            return (
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'xmlns:xlink="http://www.w3.org/1999/xlink" '
                'viewBox="0 0 200 80">\n'
                f'  <image href="data:{ct};base64,{b64}" '
                'width="100%" height="100%" preserveAspectRatio="xMidYMid meet"/>\n'
                '</svg>'
            )
    except Exception:
        pass
    return None


def _svg_is_meaningful_logo(svg_text: str) -> bool:
    """Return False for tiny UI icons that shouldn't be used as a brand logo."""
    if not svg_text or "<svg" not in svg_text.lower():
        return False
    # Reject icons explicitly marked as decorative/hidden.
    if 'aria-hidden="true"' in svg_text:
        return False
    # Reject tiny icons: width or height <= 32 when stated explicitly.
    wm = re.search(r'<svg[^>]+\bwidth="(\d+(?:\.\d+)?)"', svg_text, re.I)
    hm = re.search(r'<svg[^>]+\bheight="(\d+(?:\.\d+)?)"', svg_text, re.I)
    if wm and float(wm.group(1)) <= 32:
        return False
    if hm and float(hm.group(1)) <= 32:
        return False
    # Reject icons that have no filled shapes (fill="none" root, no other fills).
    fills = [f.strip().lower() for f in re.findall(r'\bfill="([^"]+)"', svg_text, re.I)]
    non_none = [f for f in fills if f != "none"]
    strokes = re.findall(r'\bstroke="([^"]+)"', svg_text, re.I)
    if not non_none and not strokes:
        return False
    return True


def _playwright_fetch_as_svg(url: str) -> str | None:
    """Fetch a logo URL using a full browser navigation (bypasses bot-detection).

    Navigates to the URL like a real user so cookies and JS challenges complete.
    For SVG URLs the <svg> element is extracted from the DOM.
    For raster URLs the raw bytes are captured and base64-embedded in an <svg> wrapper.
    """
    import base64 as _b64
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"])
                page = ctx.new_page()

                raw_bytes: bytes | None = None
                content_type: str = ""

                # Intercept the response to capture raw bytes alongside DOM parsing.
                def _on_response(resp):
                    nonlocal raw_bytes, content_type
                    if resp.url == url or resp.url.split("?")[0] == url.split("?")[0]:
                        try:
                            raw_bytes = resp.body()
                            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                        except Exception:
                            pass

                page.on("response", _on_response)
                nav = page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                if not (nav and nav.ok):
                    return None

                # If the server returned SVG bytes, use them directly.
                if raw_bytes:
                    if content_type == "image/svg+xml" or b"<svg" in raw_bytes[:2048].lower():
                        text = raw_bytes.decode("utf-8", errors="replace").strip()
                        if "<svg" in text.lower():
                            return text
                    if raw_bytes and content_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                        # Try path-based potrace vectorization before falling back to base64.
                        path_svg = _vectorize_image_bytes_to_svg(raw_bytes)
                        if path_svg:
                            return path_svg
                        b64 = _b64.b64encode(raw_bytes).decode("ascii")
                        return (
                            '<svg xmlns="http://www.w3.org/2000/svg" '
                            'xmlns:xlink="http://www.w3.org/1999/xlink" '
                            'viewBox="0 0 200 80">\n'
                            f'  <image href="data:{content_type};base64,{b64}" '
                            'width="100%" height="100%" preserveAspectRatio="xMidYMid meet"/>\n'
                            '</svg>'
                        )

                # Fall back to extracting the SVG element from the rendered DOM
                # (handles cases where Chromium wraps the SVG in an HTML shell).
                svg = page.evaluate(
                    "() => { const s = document.querySelector('svg'); "
                    "return s ? s.outerHTML : null; }"
                )
                if svg and "<svg" in svg.lower():
                    return svg

                # Last resort: screenshot; try potrace first, then base64.
                shot = page.screenshot(type="png")
                path_svg = _vectorize_image_bytes_to_svg(shot)
                if path_svg:
                    return path_svg
                b64 = _b64.b64encode(shot).decode("ascii")
                return (
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'xmlns:xlink="http://www.w3.org/1999/xlink" '
                    'viewBox="0 0 200 80">\n'
                    f'  <image href="data:image/png;base64,{b64}" '
                    'width="100%" height="100%" preserveAspectRatio="xMidYMid meet"/>\n'
                    '</svg>'
                )
            finally:
                browser.close()
    except Exception:
        pass
    return None


def _playwright_fetch_logo_via_site(logo_url: str, site_url: str) -> str | None:
    """Screenshot the logo element directly from the live site.

    Navigates to the homepage, finds the logo element via ranked CSS selectors,
    checks its bounding box to confirm it's a real visible element, then either
    extracts the SVG outerHTML (for SVG logos) or screenshots the element and
    base64-wraps it (for img logos).  Does NOT intercept network routes —
    works regardless of whether the logo URL itself is accessible.
    """
    import base64 as _b64

    _LOGO_SELECTORS = [
        'header img[src*="logo"]',
        'header img[alt*="logo" i]',
        'nav img[src*="logo"]',
        'nav img[alt*="logo" i]',
        '[class*="logo"] img', '[id*="logo"] img',
        '[class*="brand"] img', '[class*="Brand"] img',
        'a[href="/"] img',
        '.navbar-brand img',
        'header svg',
        'nav svg',
        'img[src*="logo"]',
        'header img',
        'nav img',
    ]

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"])
                page = ctx.new_page()
                page.goto(site_url, wait_until="domcontentloaded", timeout=25_000)
                page.wait_for_timeout(2_000)

                for sel in _LOGO_SELECTORS:
                    try:
                        for el in page.locator(sel).all()[:3]:
                            try:
                                tag = el.evaluate("e => e.tagName.toLowerCase()")
                                box = el.bounding_box()
                                if tag == "svg":
                                    svg = el.evaluate("e => e.outerHTML")
                                    if not svg or "<svg" not in svg.lower():
                                        continue
                                    # Accept SVGs even with zero bbox (fixed/CSS-driven headers)
                                    # as long as they contain real path content.
                                    if box and box["width"] >= 20 and box["height"] >= 10:
                                        if _svg_is_meaningful_logo(svg):
                                            return svg
                                    elif _svg_is_meaningful_logo(svg):
                                        # Zero-bbox SVG: only accept if it has substantial path data
                                        import re as _re
                                        path_data = " ".join(_re.findall(r'd="([^"]+)"', svg))
                                        if len(path_data) >= 20:
                                            return svg
                                    continue
                                if not box or box["width"] < 20 or box["height"] < 10:
                                    continue
                                shot = el.screenshot(type="png")
                                if shot:
                                    path_svg = _vectorize_image_bytes_to_svg(shot)
                                    if path_svg:
                                        return path_svg
                                    b64 = _b64.b64encode(shot).decode("ascii")
                                    return (
                                        '<svg xmlns="http://www.w3.org/2000/svg" '
                                        'xmlns:xlink="http://www.w3.org/1999/xlink" '
                                        'viewBox="0 0 200 80">\n'
                                        f'  <image href="data:image/png;base64,{b64}" '
                                        'width="100%" height="100%" '
                                        'preserveAspectRatio="xMidYMid meet"/>\n'
                                        '</svg>'
                                    )
                            except Exception:
                                continue
                    except Exception:
                        continue

                # Try navigating to the logo URL within the now-established session.
                try:
                    resp = page.goto(logo_url, wait_until="domcontentloaded", timeout=15_000)
                    if resp and resp.ok:
                        svg = page.evaluate(
                            "() => { const s = document.querySelector('svg'); "
                            "return s ? s.outerHTML : null; }"
                        )
                        if svg and "<svg" in svg.lower():
                            return svg
                        shot = page.screenshot(type="png")
                        if shot:
                            path_svg = _vectorize_image_bytes_to_svg(shot)
                            if path_svg:
                                return path_svg
                            b64 = _b64.b64encode(shot).decode("ascii")
                            return (
                                '<svg xmlns="http://www.w3.org/2000/svg" '
                                'xmlns:xlink="http://www.w3.org/1999/xlink" '
                                'viewBox="0 0 200 80">\n'
                                f'  <image href="data:image/png;base64,{b64}" '
                                'width="100%" height="100%" '
                                'preserveAspectRatio="xMidYMid meet"/>\n'
                                '</svg>'
                            )
                except Exception:
                    pass
            finally:
                browser.close()
    except Exception:
        pass
    return None


def _screenshot_header_as_logo(site_url: str) -> str | None:
    """Last resort: screenshot the header/nav area and vectorize or base64-wrap it.

    Called only when every other logo extraction method has failed — i.e. the site
    has no extractable logo image at all (text logo, CSS-only, or every URL 404s).
    """
    import base64 as _b64
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"])
                page.set_viewport_size({"width": 1280, "height": 800})
                page.goto(site_url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2_000)
                # Find the header/nav element and screenshot just that region
                header_sel = "header, nav, [class*='header'], [class*='navbar'], [role='banner']"
                header = page.locator(header_sel).first
                box = header.bounding_box() if header else None
                if box and box["width"] > 0 and box["height"] > 0:
                    # Clip to top portion (logo area, not full header if very tall)
                    clip = {"x": box["x"], "y": box["y"],
                            "width": min(box["width"], 400),
                            "height": min(box["height"], 120)}
                    shot = page.screenshot(type="png", clip=clip)
                else:
                    # Fall back to top-left corner of page
                    shot = page.screenshot(type="png", clip={"x": 0, "y": 0, "width": 400, "height": 120})
                if shot:
                    path_svg = _vectorize_image_bytes_to_svg(shot)
                    if path_svg:
                        return path_svg
                    b64 = _b64.b64encode(shot).decode("ascii")
                    return (
                        '<svg xmlns="http://www.w3.org/2000/svg" '
                        'xmlns:xlink="http://www.w3.org/1999/xlink" '
                        'viewBox="0 0 400 120">\n'
                        f'  <image href="data:image/png;base64,{b64}" '
                        'width="400" height="120" preserveAspectRatio="xMidYMid meet"/>\n'
                        '</svg>'
                    )
            finally:
                browser.close()
    except Exception:
        pass
    return None


def _normalize_logo_to_svg(logo: dict) -> dict:
    """Upgrade a logo to inline SVG where possible.

    Priority order (path-based SVG preferred; base64 embed is last resort):
      1. Already inline SVG — returned unchanged.
      2. Fetch URL — inline if server returns SVG content (any extension/CDN).
      3. Vectorize the raster to a path-based SVG via potrace.
      4. Playwright browser fetch — bypasses bot-detection; may yield real SVG DOM.
      5. Base64-embed raster inside an <svg> wrapper — absolute last resort.
    """
    if logo.get("type") == "svg":
        return logo
    if logo.get("type") != "img" or not logo.get("url"):
        return logo
    url = logo["url"]
    svg = _fetch_svg_file(url)
    if svg:
        return {"type": "svg", "svg": svg, "url": None}
    svg = _vectorize_to_svg(url)
    if svg:
        return {"type": "svg", "svg": svg, "url": None}
    svg = _playwright_fetch_as_svg(url)
    if svg:
        return {"type": "svg", "svg": svg, "url": None}
    svg = _raster_url_to_svg(url)
    if svg:
        return {"type": "svg", "svg": svg, "url": None}
    return logo


def _svg_is_monochrome_black(svg_text: str) -> bool:
    """Return True when every explicit fill in the SVG is black (or absent), signalling a monochrome mark."""
    fills = [f.strip().lower() for f in re.findall(r'fill="([^"]+)"', svg_text, re.I)]
    non_none = [f for f in fills if f != "none"]
    return bool(non_none) and all(f in ("#000000", "#000", "black") for f in non_none)


def _playwright_logo_to_result(hint: dict | None, base_url: str) -> dict | None:
    """Convert the raw JS evaluation result from _LOGO_EXTRACT_JS to a logo dict."""
    if not hint:
        return None
    if hint.get("type") == "svg" and hint.get("svg"):
        svg = hint["svg"]
        computed = hint.get("computedFill") or ""
        m = re.match(r'rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', computed)
        if m and _svg_is_monochrome_black(svg):
            hex_fill = _rgb_to_hex(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if hex_fill.upper() not in {"#000000", "#FFFFFF"}:
                svg = re.sub(r'fill="#(?:000000|000)"', f'fill="{hex_fill}"', svg, flags=re.I)
        return {"type": "svg", "svg": svg, "url": None}
    if hint.get("type") == "img" and hint.get("src"):
        src = hint["src"]
        if not src.startswith("data:"):
            return {"type": "img", "url": urljoin(base_url, src), "svg": None}
    return None


# --------------------------------------------------------------------------- #
# Firecrawl integration  (formats: ["branding"])
# --------------------------------------------------------------------------- #

def _firecrawl_fetch(url: str) -> tuple[str, dict, dict] | None:
    """Call the Firecrawl API with formats:["branding"].

    Returns (final_url, metadata, branding_dict) or None on any failure.
    """
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                FIRECRAWL_API_URL,
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"url": url, "formats": ["branding"]},
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("success"):
            logger.warning("Firecrawl success=false for %s", url)
            return None
        page = data.get("data", {})
        branding = page.get("branding") or {}
        metadata = page.get("metadata") or {}
        final_url = metadata.get("url") or metadata.get("sourceURL") or url
        if not branding:
            logger.warning("Firecrawl returned empty branding for %s", url)
            return None
        return final_url, metadata, branding
    except Exception as exc:
        logger.warning("Firecrawl request failed for %s: %s", url, exc)
        return None


def _decode_svg_data_uri(data_uri: str) -> str | None:
    """Decode a data:image/svg+xml;utf8,... or ;base64,... URI to SVG text."""
    try:
        if ";utf8," in data_uri:
            return urllib.parse.unquote(data_uri.split(";utf8,", 1)[1]).strip()
        if ";base64," in data_uri:
            import base64 as _b64
            return _b64.b64decode(data_uri.split(";base64,", 1)[1]).decode("utf-8").strip()
    except Exception:
        pass
    return None


def _parse_branding_response(branding: dict) -> dict:
    """Normalise FC branding dict → shape scrape() expects (snake_case keys, flat paths).

    FC uses camelCase and nested sub-objects; this adapter flattens everything so the
    existing merge logic in scrape() can consume it without changes to key names.
    """
    colors_raw   = branding.get("colors") or {}
    typo_raw     = branding.get("typography") or {}
    ff           = typo_raw.get("fontFamilies") or {}
    fs           = typo_raw.get("fontSizes") or {}
    spacing_raw  = branding.get("spacing") or {}
    pers_raw     = branding.get("personality") or {}
    images_raw   = branding.get("images") or {}
    comp_raw     = branding.get("components") or {}
    fonts_raw    = branding.get("fonts") or []

    fonts = [
        f["family"] for f in fonts_raw
        if isinstance(f, dict) and isinstance(f.get("family"), str) and f["family"].strip()
    ]

    return {
        "colors": {
            "primary":      colors_raw.get("primary"),
            "secondary":    colors_raw.get("secondary"),
            "accent":       colors_raw.get("accent"),
            "background":   colors_raw.get("background"),
            "text_primary": colors_raw.get("textPrimary"),   # camelCase → snake_case
            "link":         colors_raw.get("link"),
        },
        "fonts": fonts,
        "typography": {
            "primary_font": ff.get("primary"),
            "heading_font": ff.get("heading"),
            "h1_size":      fs.get("h1"),
            "h2_size":      fs.get("h2"),
            "body_size":    fs.get("body"),
        },
        "spacing": {
            "base_unit":     spacing_raw.get("baseUnit"),      # camelCase → snake_case
            "border_radius": spacing_raw.get("borderRadius"),  # camelCase → snake_case
        },
        "personality": {
            "tone":     pers_raw.get("tone"),
            "energy":   pers_raw.get("energy"),
            "audience": pers_raw.get("targetAudience"),  # different key name
        },
        "logo_raw": images_raw.get("logo"),   # data: URI or https:// URL
        "images": {
            "favicon": images_raw.get("favicon"),
            "ogImage": images_raw.get("ogImage"),
        },
        "components": {
            "buttonPrimary":   comp_raw.get("buttonPrimary") or {},
            "buttonSecondary": comp_raw.get("buttonSecondary") or {},
        },
    }


def _logo_from_fc_branding(logo_raw: str, base_url: str) -> dict | None:
    """Parse FC branding images.logo into a logo dict (type/svg/url).

    Handles data:image/svg+xml URIs (decoded inline) and regular https:// URLs.
    Returns None when the value is empty or cannot be parsed.
    """
    if not logo_raw:
        return None
    if logo_raw.startswith("data:image/svg+xml"):
        svg_text = _decode_svg_data_uri(logo_raw)
        if svg_text and "<svg" in svg_text.lower():
            return {"type": "svg", "svg": svg_text, "url": None}
        return None
    if logo_raw.startswith(("http://", "https://")):
        return {"type": "img", "url": logo_raw, "svg": None}
    # Relative URL
    abs_url = urljoin(base_url, logo_raw)
    return {"type": "img", "url": abs_url, "svg": None}


def _select_logo_source(
    fc_logo_raw: str | None,
    base_url: str,
    html: str,
    playwright_logo,
    final_url: str,
) -> tuple[dict, str]:
    """SOURCE DECISION POINT — returns (logo_dict, source_label).

    Priority:
      1. Firecrawl branding images.logo (data: URI decoded or URL)
      2. In-house HTML heuristics + Playwright DOM pass
    Logs which path was taken on every call.
    """
    # ── FC path ──────────────────────────────────────────────────────────
    if fc_logo_raw:
        candidate = _logo_from_fc_branding(fc_logo_raw, base_url)
        if candidate:
            logger.info("[LOGO SOURCE] firecrawl — branding.images.logo parsed OK (type=%s)", candidate.get("type"))
            return candidate, "firecrawl"
        logger.warning("[LOGO SOURCE] FC logo_raw present but could not be parsed; falling back to in-house")
    else:
        logger.info("[LOGO SOURCE] FC provided no logo; using in-house extraction")

    # ── In-house path ────────────────────────────────────────────────────
    logger.info("[LOGO SOURCE] inhouse — running HTML heuristics + Playwright")

    pw_logo = _playwright_logo_to_result(playwright_logo, final_url)
    html_logo = extract_logo(html, final_url) if html else {"type": "none", "url": None, "svg": None}

    if pw_logo and pw_logo.get("type") == "svg":
        logger.info("[LOGO SOURCE] inhouse — Playwright result is inline SVG")
        return pw_logo, "inhouse"
    if html_logo.get("type") == "svg":
        logger.info("[LOGO SOURCE] inhouse — HTML heuristic found inline SVG")
        return html_logo, "inhouse"

    # Dedicated Playwright DOM pass (only when playwright_logo wasn't already set)
    if not playwright_logo:
        pw_hint = _playwright_logo_pass(final_url)
        pw_result = _playwright_logo_to_result(pw_hint, final_url)
        if pw_result and pw_result.get("type") == "svg":
            logger.info("[LOGO SOURCE] inhouse — Playwright DOM pass found SVG")
            return pw_result, "inhouse"
        if pw_result:
            pw_logo = pw_result

    result = pw_logo or html_logo or {"type": "none", "url": None, "svg": None}
    if result.get("type") == "none":
        logger.warning("[LOGO SOURCE] No logo found from any source")
    else:
        logger.info("[LOGO SOURCE] inhouse — best candidate type=%s", result.get("type"))
    return result, "inhouse"


def _resolve_logo_format(logo: dict, site_url: str) -> dict:
    """FORMAT DECISION POINT — converts logo to best SVG representation.

    Priority (independent of source):
      1. Already vector SVG (<path>/<polygon>/etc geometry) → use directly
      2. URL or non-vector SVG → vtracer (colour) / potrace (mono) → path SVG
      3. LAST RESORT: <image> base64 embed inside <svg> — logs loudly
    Logs which path was taken on every call.
    """
    # (1) Already a vector SVG?
    if logo.get("type") == "svg":
        svg_text = logo.get("svg", "")
        if _svg_has_vector_paths(svg_text):
            logger.info("[LOGO FORMAT] vector-SVG — has path geometry, using directly")
            return logo
        logger.info("[LOGO FORMAT] SVG present but no vector geometry; will attempt re-vectorization")

    if logo.get("type") not in ("svg", "img") or not (logo.get("url") or logo.get("svg")):
        logger.info("[LOGO FORMAT] No convertible logo (type=%s); returning as-is", logo.get("type"))
        return logo

    url = logo.get("url")
    if not url:
        return logo

    # (2a) Fetch URL — may already serve a vector SVG file
    svg = _fetch_svg_file(url)
    if svg and _svg_has_vector_paths(svg):
        logger.info("[LOGO FORMAT] Fetched vector SVG from URL: %s", url)
        return {"type": "svg", "svg": svg, "url": None}

    # (2b) Vectorize raster (vtracer for colour, potrace for mono)
    svg = _vectorize_to_svg(url)
    if svg:
        if _svg_has_vector_paths(svg):
            logger.info("[LOGO FORMAT] Vectorized raster → path-SVG from %s", url)
        else:
            logger.warning("[LOGO FORMAT] Vectorization produced non-path SVG for %s", url)
        return {"type": "svg", "svg": svg, "url": None}

    # (2c) Playwright browser fetch (bypasses bot-detection on logo asset URLs)
    logger.info("[LOGO FORMAT] Trying Playwright asset-URL fetch for %s", url)
    svg = _playwright_fetch_as_svg(url)
    if svg:
        kind = "vector" if _svg_has_vector_paths(svg) else "non-path"
        logger.info("[LOGO FORMAT] Playwright asset fetch returned %s SVG", kind)
        return {"type": "svg", "svg": svg, "url": None}

    # (2d) Playwright site-visit — navigates homepage, finds logo in live DOM
    logger.info("[LOGO FORMAT] Trying Playwright site-visit logo extraction from %s", site_url)
    svg = _playwright_fetch_logo_via_site(url, site_url)
    if svg:
        kind = "vector" if _svg_has_vector_paths(svg) else "non-path"
        logger.info("[LOGO FORMAT] Playwright site-visit returned %s SVG", kind)
        return {"type": "svg", "svg": svg, "url": None}

    # (3) LAST RESORT: base64-embed raster inside <svg><image>
    logger.warning("[LOGO FORMAT] *** LAST RESORT: base64-embedding raster as <svg><image> for %s ***", url)
    svg = _raster_url_to_svg(url)
    if svg:
        return {"type": "svg", "svg": svg, "url": None}

    return logo


def _playwright_logo_pass(url: str) -> dict | None:
    """Open a minimal Playwright session solely to run the JS logo finder.

    Called when the HTTP fetch path succeeded (so Playwright wasn't used for
    fetching) but the HTML-based logo detection didn't yield an SVG. Many sites
    inject their header logo via JavaScript, which only the rendered DOM exposes.
    Uses domcontentloaded (faster than networkidle) since we only need the DOM.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"])
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Brief wait for hydration of JS frameworks (React/Vue/etc.)
                page.wait_for_timeout(1500)
                return page.evaluate(_LOGO_EXTRACT_JS)
            finally:
                browser.close()
    except Exception:
        return None


def _trim_at_sentence(s: str, limit: int = 200) -> str:
    if len(s) <= limit:
        return s
    chunk = s[:limit]
    for i in range(len(chunk) - 1, -1, -1):
        if chunk[i] in ".!?":
            return chunk[:i + 1]
    return chunk.rsplit(" ", 1)[0].rstrip(".,;:")


def _valid_hex(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    # 8-char hex with alpha (#RRGGBBAA) → strip alpha to #RRGGBB
    m8 = re.match(r'^#([0-9a-fA-F]{6})[0-9a-fA-F]{2}$', v)
    if m8:
        v = "#" + m8.group(1)
    # 4-char hex with alpha (#RGBA) → expand to #RRGGBB
    m4 = re.match(r'^#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])[0-9a-fA-F]$', v)
    if m4:
        v = "#" + m4.group(1) * 2 + m4.group(2) * 2 + m4.group(3) * 2
    if re.match(r'^#[0-9a-fA-F]{3}$|^#[0-9a-fA-F]{6}$', v):
        return _normalize_hex(v)
    return None


def _valid_px(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if re.match(r'^\d+(\.\d+)?px$', v, re.I):
        return v
    # bare number
    m = re.match(r'^(\d+(\.\d+)?)$', v)
    if m:
        return f"{int(float(m.group(1)))}px"
    # rem/em → approximate px (16px base)
    m = re.match(r'^(\d+(\.\d+)?)(rem|em)$', v, re.I)
    if m:
        return f"{round(float(m.group(1)) * 16)}px"
    return None


def _valid_energy(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().capitalize()
    return v if v in ("Low", "Medium", "High") else None


def scrape(firm_name: str, url: str) -> ScrapeResult:
    # ================================================================== #
    # PRIMARY: Firecrawl API — formats:["branding"]
    # ================================================================== #
    fc_result = _firecrawl_fetch(url)
    if fc_result:
        fc_final_url, _fc_meta, fc_branding_raw = fc_result
        fc_brand = _parse_branding_response(fc_branding_raw)
        logger.info("Firecrawl branding retrieved for %s", url)
    else:
        fc_final_url = _fc_meta = fc_branding_raw = fc_brand = None
        logger.info("Firecrawl unavailable; using in-house only for %s", url)

    fc_c = (fc_brand or {}).get("colors") or {}
    fc_t = (fc_brand or {}).get("typography") or {}
    fc_s = (fc_brand or {}).get("spacing") or {}
    fc_p = (fc_brand or {}).get("personality") or {}

    # ================================================================== #
    # FALLBACK: In-house fetch + CSS extraction.
    # Triggered when FC is missing critical token groups or has no logo.
    # ================================================================== #
    _fc_has_colors  = any(_valid_hex(fc_c.get(k)) for k in ("primary", "accent", "background", "text_primary", "link"))
    _fc_has_fonts   = bool((fc_t.get("primary_font") or "").strip() and (fc_t.get("heading_font") or "").strip())
    _fc_has_sizes   = bool(_valid_px(fc_t.get("h1_size")) and _valid_px(fc_t.get("body_size")))
    _fc_has_spacing = isinstance(fc_s.get("base_unit"), int) and bool(_valid_px(fc_s.get("border_radius")))
    _fc_logo_raw    = (fc_brand or {}).get("logo_raw")

    _need_inhouse_tokens = not all([_fc_has_colors, _fc_has_fonts, _fc_has_sizes, _fc_has_spacing])
    _need_inhouse_logo   = not bool(_fc_logo_raw)  # quick check; full parse happens in _select_logo_source
    _need_inhouse_fetch  = _need_inhouse_tokens or _need_inhouse_logo

    if _need_inhouse_fetch:
        logger.info(
            "In-house fetch triggered (missing: tokens=%s logo=%s)",
            _need_inhouse_tokens, _need_inhouse_logo,
        )
        final_url, html, css, playwright_logo = fetch_site(url)
        if _need_inhouse_tokens:
            ih_colors     = extract_colors(css, html)
            ih_typography = extract_typography(css)
            ih_spacing    = extract_spacing(css)
        else:
            ih_colors     = extract_colors("", html)
            ih_typography = {
                "primary_font": "sans-serif", "heading_font": "sans-serif",
                "h1_size": "48px", "h2_size": "32px", "body_size": "16px",
            }
            ih_spacing = {"base_unit": 8, "border_radius": "4px"}
    else:
        logger.info("FC branding complete; skipping in-house CSS fetch")
        final_url       = fc_final_url
        html = css      = ""
        playwright_logo = None
        ih_colors       = {}
        ih_typography   = {
            "primary_font": "sans-serif", "heading_font": "sans-serif",
            "h1_size": "48px", "h2_size": "32px", "body_size": "16px",
        }
        ih_spacing = {"base_unit": 8, "border_radius": "4px"}

    base_url = fc_final_url or final_url
    ih_personality = extract_personality(html, ih_colors or {})

    # ================================================================== #
    # Merge: Firecrawl value first; in-house as field-level fallback.
    # With the branding format, FC values are trusted directly (no CSS
    # appearance check) per the "map directly from the response" requirement.
    # ================================================================== #
    _GENERIC_COLORS = {"#000000", "#FFFFFF", "#FFF", "#000"}

    def _fc_color(key: str, reject_generic: bool = False) -> str | None:
        val = _valid_hex(fc_c.get(key))
        if not val:
            return None
        if reject_generic and val.upper() in _GENERIC_COLORS:
            return None
        return val

    colors = {
        "primary":      _fc_color("primary", reject_generic=True) or ih_colors.get("primary", DEFAULT_COLORS["primary"]),
        "secondary":    _fc_color("secondary")    or ih_colors.get("secondary", ih_colors.get("accent", DEFAULT_COLORS["accent"])),
        "accent":       _fc_color("accent")       or ih_colors.get("accent", DEFAULT_COLORS["accent"]),
        "background":   _fc_color("background")   or ih_colors.get("background", DEFAULT_COLORS["background"]),
        "text_primary": _fc_color("text_primary") or ih_colors.get("text_primary", DEFAULT_COLORS["text_primary"]),
        "link":         _fc_color("link")         or ih_colors.get("link", DEFAULT_COLORS["link"]),
    }

    typography = {
        "primary_font": (fc_t.get("primary_font") or "").strip() or ih_typography["primary_font"],
        "heading_font": (fc_t.get("heading_font") or "").strip() or ih_typography["heading_font"],
        "h1_size":      _valid_px(fc_t.get("h1_size"))   or ih_typography["h1_size"],
        "h2_size":      _valid_px(fc_t.get("h2_size"))   or ih_typography["h2_size"],
        "body_size":    _valid_px(fc_t.get("body_size")) or ih_typography["body_size"],
    }

    spacing = {
        "base_unit":     (fc_s.get("base_unit") if isinstance(fc_s.get("base_unit"), int) else None)
                         or ih_spacing["base_unit"],
        "border_radius": _valid_px(fc_s.get("border_radius")) or ih_spacing["border_radius"],
    }

    personality = {
        "tone":     (fc_p.get("tone") or "").strip()                          or ih_personality["tone"],
        "energy":   _valid_energy(fc_p.get("energy"))                         or ih_personality["energy"],
        "audience": _trim_at_sentence((fc_p.get("audience") or "").strip())   or ih_personality["audience"],
    }

    # Fonts: FC branding returns [{family, role}, ...] via adapter as plain strings.
    _fc_fonts = [
        f.strip() for f in ((fc_brand or {}).get("fonts") or [])
        if isinstance(f, str) and f.strip() and f.strip().lower() not in GENERIC_FONTS
    ]
    if not _fc_fonts:
        seen: set[str] = set()
        _fc_fonts = []
        for _f in [typography["primary_font"], typography["heading_font"]]:
            if _f and _f.lower() not in GENERIC_FONTS and _f not in seen:
                _fc_fonts.append(_f)
                seen.add(_f)
    fonts = _fc_fonts or ["sans-serif"]

    # Components and images come directly from FC branding (no in-house fallback).
    components = (fc_brand or {}).get("components") or {}
    fc_images  = (fc_brand or {}).get("images") or {}
    images = {
        "favicon": fc_images.get("favicon"),
        "ogImage": fc_images.get("ogImage"),
    }

    # ================================================================== #
    # SOURCE DECISION POINT — pick the best logo candidate.
    # ================================================================== #
    logo_candidate, logo_source = _select_logo_source(
        fc_logo_raw=_fc_logo_raw,
        base_url=base_url,
        html=html,
        playwright_logo=playwright_logo,
        final_url=final_url,
    )

    # ================================================================== #
    # FORMAT DECISION POINT — convert candidate to best SVG representation.
    # ================================================================== #
    logo = _resolve_logo_format(logo_candidate, site_url=base_url)

    # Bind potrace's currentColor placeholder to the actual brand primary.
    if logo.get("type") == "svg" and logo.get("svg") and "currentColor" in logo["svg"]:
        logo = {"type": "svg", "svg": logo["svg"].replace("currentColor", colors["primary"]), "url": None}

    # When the SVG logo is still monochrome black, ask Playwright for the computed
    # fill so we can embed the real brand colour.
    if logo.get("type") == "svg" and logo.get("svg") and _svg_is_monochrome_black(logo["svg"]):
        pw_hint = _playwright_logo_pass(base_url)
        if isinstance(pw_hint, dict):
            computed = pw_hint.get("computedFill") or ""
            m = re.match(r'rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', computed)
            if m:
                hex_fill = _rgb_to_hex(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if hex_fill.upper() not in {"#000000", "#FFFFFF"}:
                    svg_text = re.sub(r'fill="#(?:000000|000)"', f'fill="{hex_fill}"',
                                      logo["svg"], flags=re.I)
                    logo = {"type": "svg", "svg": svg_text, "url": None}

    logger.info(
        "scrape complete — logo_source=%s logo_type=%s",
        logo_source, logo.get("type"),
    )

    return ScrapeResult(
        firm_name=firm_name,
        url=base_url,
        colors=colors,
        fonts=fonts,
        typography=typography,
        spacing=spacing,
        personality=personality,
        logo=logo,
        components=components,
        images=images,
    )
