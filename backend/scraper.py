"""Scrape a firm's homepage and reverse-engineer design tokens.

All extraction is heuristic: it reads the site's published CSS/HTML and makes a
best guess at the palette, fonts, radii, and logo. Output is meant to be
reviewed and adjusted by a human, not trusted blindly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

# A realistic browser header set. Many sites reject requests whose User-Agent
# self-identifies as a bot or that omit the usual browser headers.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Ad/analytics query params that should be dropped before fetching.
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


@dataclass
class ScrapeResult:
    firm_name: str
    url: str
    colors: dict
    typography: dict
    rounded: dict
    spacing: dict
    logo: dict


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


def _clean_url(url: str) -> str:
    if not urlparse(url).scheme:
        url = "https://" + url
    parts = urlparse(url)
    query = {k: v for k, v in parse_qs(parts.query).items() if k.lower() not in TRACKING_PARAMS}
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def _root(url: str) -> str:
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, "/", "", "", ""))


def fetch_site(url: str) -> tuple[str, str, str]:
    """Return (final_url, html, combined_css).

    Tries the cleaned URL first, then falls back to the bare homepage if the
    first attempt is blocked or fails.
    """
    cleaned = _clean_url(url)
    candidates = [cleaned]
    root = _root(cleaned)
    if root != cleaned:
        candidates.append(root)

    last_error: Exception | None = None
    with httpx.Client(follow_redirects=True, timeout=20.0, headers=BROWSER_HEADERS) as client:
        for candidate in candidates:
            try:
                resp = client.get(candidate)
                resp.raise_for_status()
                html = resp.text
                css = _gather_css(client, html, str(resp.url))
                return str(resp.url), html, css
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                last_error = exc
    raise last_error if last_error else RuntimeError("Could not fetch the site.")


def _gather_css(client: httpx.Client, html: str, base_url: str, max_sheets: int = 8) -> str:
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
            parts.append(client.get(href).text[:500_000])
        except Exception:
            continue
    return "\n".join(parts)


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


def _in_header(el) -> bool:
    return any(getattr(p, "name", None) in ("header", "nav") for p in el.parents)


def _real_img_url(img) -> str | None:
    """Return the best real image URL from an <img>, accounting for lazy-loading.

    Many sites put a placeholder (or nothing) in src and the real URL in
    data-src / data-lazy-src / srcset, so check those first and skip data: URIs.
    """
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


def extract_logo(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Inline SVG with a logo hint or sitting in the header/nav.
    for svg in soup.find_all("svg"):
        classes = " ".join(svg.get("class") or [])
        hint = f"{classes} {svg.get('id') or ''} {svg.get('aria-label') or ''}".lower()
        if "logo" in hint or _in_header(svg):
            return {"type": "svg", "svg": str(svg), "url": None}

    # 2. <img> whose attributes hint "logo" -> resolve its real (possibly lazy) URL.
    for img in soup.find_all("img"):
        attrs = [" ".join(img.get("class")) if img.get("class") else None, img.get("id"),
                 img.get("alt"), img.get("src"), img.get("data-src")]
        hint = " ".join(filter(None, attrs)).lower()
        if "logo" in hint:
            src = _real_img_url(img)
            if src:
                return {"type": "img", "url": urljoin(base_url, src), "svg": None}

    # 3. CSS background-image on an element whose class/id hints "logo".
    for el in soup.find_all(class_=LOGO_RE) + soup.find_all(id=LOGO_RE):
        match = BG_URL_RE.search(el.get("style") or "")
        if match:
            ref = match.group(1).strip().strip("'\"")
            if ref and not ref.startswith("data:"):
                return {"type": "img", "url": urljoin(base_url, ref), "svg": None}

    # 4. Social / icon fallbacks.
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return {"type": "img", "url": urljoin(base_url, og["content"]), "svg": None}
    for want in ("apple-touch-icon", "icon"):
        for link in soup.find_all("link"):
            rel = link.get("rel") or []
            rel = [rel] if isinstance(rel, str) else rel
            if any(want in str(r).lower() for r in rel) and link.get("href"):
                return {"type": "img", "url": urljoin(base_url, link["href"]), "svg": None}

    # 5. Last resort: first image with a real URL.
    for img in soup.find_all("img"):
        src = _real_img_url(img)
        if src:
            return {"type": "img", "url": urljoin(base_url, src), "svg": None}
    return {"type": "none", "url": None, "svg": None}


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