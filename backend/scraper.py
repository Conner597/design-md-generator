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
from contextlib import contextmanager
from dataclasses import dataclass
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
LOGO_RE = re.compile(r"logo", re.I)
BRAND_HINT = re.compile(r"logo|brand|site-?title|navbar-brand", re.I)
ICON_HINT = re.compile(
    r"\b(icon|search|magnif|menu|hamburger|close|arrow|chevron|caret|social|"
    r"share|cart|toggle|burger|pin|marker|location|map|place|geo|phone|mail|envelope)\b",
    re.I,
)

GENERIC_FONTS = {
    "inherit", "initial", "unset", "sans-serif", "serif", "monospace",
    "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace", "-apple-system",
    "blinkmacsystemfont", "cursive", "fantasy", "revert", "none",
}

DEFAULT_COLORS = {
    "primary": "#1A1C1E",
    "secondary": "#6C7278",
    "tertiary": "#B8422E",
    "neutral": "#F7F5F2",
}

_CSS_DUMP_JS = (
    "() => { let out = []; for (const s of document.styleSheets) { try { "
    "for (const r of (s.cssRules || [])) out.push(r.cssText); } catch (e) {} } "
    "return out.join('\\n'); }"
)


@dataclass
class ScrapeResult:
    firm_name: str
    url: str
    colors: dict
    typography: dict
    rounded: dict
    spacing: dict
    logo: dict


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


def _browser_fetch(url: str) -> tuple[str, str, str]:
    """Render the page in headless Chromium to get past JavaScript challenges."""
    from playwright.sync_api import sync_playwright  # optional dependency

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=40_000)
            html = page.content()
            final_url = page.url
            css = page.evaluate(_CSS_DUMP_JS)
        finally:
            browser.close()
    return final_url, html, css or ""


def fetch_site(url: str) -> tuple[str, str, str]:
    """Return (final_url, html, combined_css), escalating through anti-bot layers."""
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
                return final_url, html, _gather_css(sess, html, final_url)
            except Exception as exc:  # noqa: BLE001 - try the next layer/candidate
                last_error = exc

    # Final layer: headless browser (handles JS challenges). Optional dependency;
    # if Playwright or its browser isn't installed, this raises and we surface the
    # original HTTP error instead.
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
def extract_colors(css: str, html: str) -> dict:
    counts: dict[str, int] = {}
    text = css + " " + html
    for match in HEX_RE.findall(text):
        hx = _normalize_hex(match)
        counts[hx] = counts.get(hx, 0) + 1
    for r, g, b in RGB_RE.findall(text):
        hx = _rgb_to_hex(int(r), int(g), int(b))
        counts[hx] = counts.get(hx, 0) + 1
    if not counts:
        return dict(DEFAULT_COLORS)

    ranked = [h for h, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]
    light = next((h for h in ranked if _hex_to_hsl(h)[2] > 0.85), None)
    dark = next((h for h in ranked if _hex_to_hsl(h)[2] < 0.25), None)
    accents = [h for h in ranked if _hex_to_hsl(h)[1] > 0.25 and h not in (light, dark)]

    chosen: dict[str, str | None] = {
        "primary": dark,
        "secondary": accents[0] if accents else None,
        "tertiary": accents[1] if len(accents) > 1 else None,
        "neutral": light,
    }

    def next_unused() -> str | None:
        used = {v for v in chosen.values() if v}
        return next((h for h in ranked if h not in used), None)

    for key in ("primary", "secondary", "tertiary", "neutral"):
        if not chosen[key]:
            chosen[key] = next_unused()
    for key, fallback in DEFAULT_COLORS.items():
        if not chosen[key]:
            chosen[key] = fallback
    return {k: chosen[k] for k in ("primary", "secondary", "tertiary", "neutral")}


def extract_typography(css: str) -> dict:
    freq: dict[str, int] = {}
    for raw in FONT_RE.findall(css):
        first = raw.split(",")[0].strip().strip("'\"")
        if first and first.lower() not in GENERIC_FONTS:
            freq[first] = freq.get(first, 0) + 1
    ranked = [f for f, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)]
    body = ranked[0] if ranked else "Public Sans"
    heading = ranked[1] if len(ranked) > 1 else body
    label = ranked[1] if len(ranked) > 1 else "Space Grotesk"
    return {
        "h1": {"fontFamily": heading, "fontSize": "3rem"},
        "body-md": {"fontFamily": body, "fontSize": "1rem"},
        "label-caps": {"fontFamily": label, "fontSize": "0.75rem"},
    }


def extract_rounded(css: str) -> dict:
    vals = sorted({int(round(float(v))) for v in RADIUS_RE.findall(css) if 0 < float(v) <= 64})
    if len(vals) >= 2:
        return {"sm": f"{vals[0]}px", "md": f"{vals[1]}px"}
    if len(vals) == 1:
        return {"sm": f"{vals[0]}px", "md": f"{vals[0] * 2}px"}
    return {"sm": "4px", "md": "8px"}


# --------------------------------------------------------------------------- #
# Logo detection (scored: prefer the real brand mark, reject UI icons)
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
    """Accept a brand-mark SVG; reject UI icons (search, menu, map pins, square glyphs)."""
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
    # Icons are square; brand wordmarks are usually wider than tall. Only trust a
    # square SVG as a logo if it explicitly carries a logo/brand label itself.
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


def scrape(firm_name: str, url: str) -> ScrapeResult:
    final_url, html, css = fetch_site(url)
    return ScrapeResult(
        firm_name=firm_name,
        url=final_url,
        colors=extract_colors(css, html),
        typography=extract_typography(css),
        rounded=extract_rounded(css),
        spacing={"sm": "8px", "md": "16px"},
        logo=extract_logo(html, final_url),
    )