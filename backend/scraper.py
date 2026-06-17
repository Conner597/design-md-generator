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
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from functools import reduce
from math import gcd
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - optional dependency
    HAS_CURL_CFFI = False

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
                const cls = String(el.className || '') + ' ' + String(el.id || '');
                if (/menu|hamburger|search|close|arrow|chevron|social/i.test(cls)) continue;
                return { type: 'svg', svg: el.outerHTML };
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
    colors: dict      # primary, accent, background, text_primary, link
    typography: dict  # primary_font, heading_font, h1_size, h2_size, body_size
    spacing: dict     # base_unit (int), border_radius (str)
    personality: dict # tone, energy, audience
    logo: dict        # type, svg, url


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
    var_hex: dict[str, str] = {}
    for name, value in CSS_VAR_DEF_RE.findall(css):
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
                if first and first.lower() not in GENERIC_FONTS:
                    return first
    return None


def extract_typography(css: str) -> dict:
    freq: dict[str, int] = {}
    for raw in FONT_RE.findall(css):
        first = raw.split(",")[0].strip().strip("'\"")
        if first and first.lower() not in GENERIC_FONTS:
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
            score += 12
        if _is_tiny(img):
            score -= 25
        if score > 0:
            candidates.append((min(score, 95), "img", urljoin(base_url, src)))

    for svg in soup.find_all("svg"):
        if _svg_looks_like_logo(svg):
            score = 55 if BRAND_HINT.search(_attr_blob(svg)) else 38
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


def _raster_url_to_svg(url: str) -> str | None:
    """Download a raster image and embed it base64-encoded inside an SVG wrapper.

    If the server returns an SVG content-type despite a non-.svg extension,
    the raw SVG is inlined directly instead of being base64-wrapped.
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
                'xmlns:xlink="http://www.w3.org/1999/xlink">\n'
                f'  <image href="data:{ct};base64,{b64}" '
                'width="100%" height="100%" preserveAspectRatio="xMidYMid meet"/>\n'
                '</svg>'
            )
    except Exception:
        pass
    return None


def _normalize_logo_to_svg(logo: dict) -> dict:
    """Ensure the logo is always stored as SVG in the output.

    - Inline SVG already: returned unchanged.
    - URL ending in .svg: fetched and inlined.
    - Raster URL (png/jpg/etc.): downloaded, base64-encoded, wrapped in <svg>.
    - Falls back to the original dict if every download attempt fails.
    """
    if logo.get("type") == "svg":
        return logo
    if logo.get("type") != "img" or not logo.get("url"):
        return logo
    url = logo["url"]
    if urlparse(url).path.lower().endswith(".svg"):
        svg = _fetch_svg_file(url)
        if svg:
            return {"type": "svg", "svg": svg, "url": None}
    svg = _raster_url_to_svg(url)
    if svg:
        return {"type": "svg", "svg": svg, "url": None}
    return logo


def _playwright_logo_to_result(hint: dict | None, base_url: str) -> dict | None:
    """Convert the raw JS evaluation result from _LOGO_EXTRACT_JS to a logo dict."""
    if not hint:
        return None
    if hint.get("type") == "svg" and hint.get("svg"):
        return {"type": "svg", "svg": hint["svg"], "url": None}
    if hint.get("type") == "img" and hint.get("src"):
        src = hint["src"]
        if not src.startswith("data:"):
            return {"type": "img", "url": urljoin(base_url, src), "svg": None}
    return None


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


def scrape(firm_name: str, url: str) -> ScrapeResult:
    final_url, html, css, playwright_logo = fetch_site(url)
    colors = extract_colors(css, html)

    # Start with the HTML-based heuristic (always available).
    html_logo = extract_logo(html, final_url)

    # Playwright logo hint from the fetch layer (only present when Playwright
    # was used for fetching, i.e., HTTP layers failed).
    fetch_pw_logo = _playwright_logo_to_result(playwright_logo, final_url)

    # If we don't already have an SVG from either source, run a dedicated
    # Playwright pass. JS-rendered sites inject the header logo dynamically;
    # the static HTML never contains it.
    if fetch_pw_logo and fetch_pw_logo.get("type") == "svg":
        logo = fetch_pw_logo
    elif html_logo.get("type") == "svg":
        logo = html_logo
    else:
        # No SVG yet — try Playwright's visibility-aware JS finder.
        pw_hint = _playwright_logo_pass(final_url)
        pw_logo = _playwright_logo_to_result(pw_hint, final_url)
        # Prefer an SVG from Playwright; fall back to the best HTML result.
        if pw_logo and pw_logo.get("type") == "svg":
            logo = pw_logo
        else:
            logo = fetch_pw_logo or pw_logo or html_logo

    # Always convert the final logo to SVG — embed inline SVG files and
    # base64-wrap raster images so the output never contains a bare img URL.
    logo = _normalize_logo_to_svg(logo)

    return ScrapeResult(
        firm_name=firm_name,
        url=final_url,
        colors=colors,
        typography=extract_typography(css),
        spacing=extract_spacing(css),
        personality=extract_personality(html, colors),
        logo=logo,
    )
