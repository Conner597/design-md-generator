"""Render a ScrapeResult into a DESIGN.md string.

Format: YAML front matter (design tokens) followed by human-readable markdown
sections: Colors, Fonts, Typography, Spacing, Personality, Components, and
Logo & Images. The logo is embedded as inline SVG when available.
"""
from __future__ import annotations

from scraper import ScrapeResult


def _yaml_escape(value: str) -> str:
    return value.replace('"', '\\"')


def _front_matter(r: ScrapeResult) -> str:
    lines = ["---", f'name: "{_yaml_escape(r.firm_name)}"', f'url: "{r.url}"']

    if r.logo.get("type") == "img" and r.logo.get("url"):
        lines.append(f'logo: "{r.logo["url"]}"')
    elif r.logo.get("type") == "svg":
        lines.append("logo: embedded")

    lines += [
        "colors:",
        f'  primary: "{r.colors["primary"]}"',
        f'  secondary: "{r.colors.get("secondary", r.colors["accent"])}"',
        f'  accent: "{r.colors["accent"]}"',
        f'  background: "{r.colors["background"]}"',
        f'  text_primary: "{r.colors["text_primary"]}"',
        f'  link: "{r.colors["link"]}"',
    ]

    if r.fonts:
        lines.append("fonts:")
        for font in r.fonts:
            lines.append(f'  - "{_yaml_escape(font)}"')

    lines += [
        "typography:",
        f'  primary_font: "{_yaml_escape(r.typography["primary_font"])}"',
        f'  heading_font: "{_yaml_escape(r.typography["heading_font"])}"',
        f'  h1: "{r.typography["h1_size"]}"',
        f'  h2: "{r.typography["h2_size"]}"',
        f'  body: "{r.typography["body_size"]}"',
        "spacing:",
        f'  base_unit: {r.spacing["base_unit"]}',
        f'  border_radius: "{r.spacing["border_radius"]}"',
        "personality:",
        f'  tone: "{_yaml_escape(r.personality["tone"])}"',
        f'  energy: "{r.personality["energy"]}"',
        f'  audience: "{_yaml_escape(r.personality["audience"])}"',
    ]

    # Components (present when FC branding returned button data)
    bp = (r.components or {}).get("buttonPrimary") or {}
    bs = (r.components or {}).get("buttonSecondary") or {}
    if bp or bs:
        lines.append("components:")
        if bp:
            lines += [
                "  button_primary:",
                f'    background: "{bp.get("background", "")}"',
                f'    text_color: "{bp.get("textColor", "")}"',
                f'    border_radius: "{bp.get("borderRadius", "")}"',
            ]
        if bs:
            lines += [
                "  button_secondary:",
                f'    background: "{bs.get("background", "")}"',
                f'    text_color: "{bs.get("textColor", "")}"',
                f'    border_color: "{bs.get("borderColor", "")}"',
                f'    border_radius: "{bs.get("borderRadius", "")}"',
            ]

    # Images (favicon / ogImage)
    favicon  = (r.images or {}).get("favicon")
    og_image = (r.images or {}).get("ogImage")
    if favicon or og_image:
        lines.append("images:")
        if favicon:
            lines.append(f'  favicon: "{favicon}"')
        if og_image:
            lines.append(f'  og_image: "{og_image}"')

    lines.append("---")
    return "\n".join(lines)


def _body(r: ScrapeResult) -> str:
    font_lines = [f"- **{f}**" for f in (r.fonts or [r.typography["primary_font"]])]

    parts = [
        f"# {r.firm_name}",
        "",
        "## Overview",
        "",
        f"Design tokens extracted from [{r.url}]({r.url}) via Firecrawl branding analysis. "
        "Colors, fonts, spacing, and logo reflect the site's published brand. "
        "Review all values before use.",
        "",
        "## Colors",
        "",
        f"- **Primary:** `{r.colors['primary']}`",
        f"- **Secondary:** `{r.colors.get('secondary', r.colors['accent'])}`",
        f"- **Accent:** `{r.colors['accent']}`",
        f"- **Background:** `{r.colors['background']}`",
        f"- **Text Primary:** `{r.colors['text_primary']}`",
        f"- **Link:** `{r.colors['link']}`",
        "",
        "## Fonts",
        "",
        *font_lines,
        "",
        "## Typography",
        "",
        f"- primary: **{r.typography['primary_font']}**",
        f"- heading: **{r.typography['heading_font']}**",
        f"- h1: **{r.typography['h1_size']}**",
        f"- h2: **{r.typography['h2_size']}**",
        f"- body: **{r.typography['body_size']}**",
        "",
        "## Spacing",
        "",
        f"- Base Unit: **{r.spacing['base_unit']}**",
        f"- Border Radius: **{r.spacing['border_radius']}**",
        "",
        "## Personality",
        "",
        f"- Tone: **{r.personality['tone']}**",
        f"- Energy: **{r.personality['energy']}**",
        f"- Audience: **{r.personality['audience']}**",
        "",
    ]

    # Components section (buttons)
    bp = (r.components or {}).get("buttonPrimary") or {}
    bs = (r.components or {}).get("buttonSecondary") or {}
    if bp or bs:
        parts += ["## Components", ""]
        if bp:
            parts += [
                "**Button — Primary**",
                f"- Background: `{bp.get('background', 'n/a')}`",
                f"- Text: `{bp.get('textColor', 'n/a')}`",
                f"- Border Radius: `{bp.get('borderRadius', 'n/a')}`",
                "",
            ]
        if bs:
            parts += [
                "**Button — Secondary**",
                f"- Background: `{bs.get('background', 'n/a')}`",
                f"- Text: `{bs.get('textColor', 'n/a')}`",
                f"- Border Color: `{bs.get('borderColor', 'n/a')}`",
                f"- Border Radius: `{bs.get('borderRadius', 'n/a')}`",
                "",
            ]

    # Logo & Images section
    parts += ["## Logo & Images", ""]

    logo = r.logo
    if logo.get("type") == "svg" and logo.get("svg"):
        parts += ["**Logo (inline SVG):**", "", logo["svg"], ""]
    elif logo.get("type") == "img" and logo.get("url"):
        parts += [
            f"![{r.firm_name} logo]({logo['url']})",
            "",
            f"Source: <{logo['url']}>",
            "",
        ]
    else:
        parts += ["_No logo found during the scrape._", ""]

    favicon  = (r.images or {}).get("favicon")
    og_image = (r.images or {}).get("ogImage")
    if favicon:
        parts += [f"**Favicon:** <{favicon}>", ""]
    if og_image:
        parts += [f"**OG Image / Banner:** <{og_image}>", ""]

    return "\n".join(parts)


def build_design_md(result: ScrapeResult) -> str:
    return _front_matter(result) + "\n\n" + _body(result)
