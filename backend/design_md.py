"""Render a ScrapeResult into a DESIGN.md string.

Format: YAML front matter (design tokens) followed by human-readable markdown
sections matching Firecrawl's branding output: Colors, Fonts, Typography,
Spacing, Personality, and Logo. The logo is embedded as inline SVG when
available, otherwise referenced by URL.
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
        f'  accent: "{r.colors["accent"]}"',
        f'  background: "{r.colors["background"]}"',
        f'  text_primary: "{r.colors["text_primary"]}"',
        f'  link: "{r.colors["link"]}"',
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
        "---",
    ]
    return "\n".join(lines)


def _body(r: ScrapeResult) -> str:
    parts = [
        f"# {r.firm_name}",
        "",
        "## Overview",
        "",
        f"Design tokens reverse-engineered from [{r.url}]({r.url}). "
        "Colors, fonts, spacing, and logo were extracted automatically from "
        "the site's published styles. Review all values before use.",
        "",
        "## Colors",
        "",
        f"- **Primary** `{r.colors['primary']}`",
        f"- **Accent** `{r.colors['accent']}`",
        f"- **Background** `{r.colors['background']}`",
        f"- **Text Primary** `{r.colors['text_primary']}`",
        f"- **Link** `{r.colors['link']}`",
        "",
        "## Fonts",
        "",
        f"- **{r.typography['primary_font']}** (body)",
        f"- **{r.typography['heading_font']}** (heading)",
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
        "## Logo",
        "",
    ]

    logo = r.logo
    if logo.get("type") == "svg" and logo.get("svg"):
        parts += ["Embedded inline SVG:", "", logo["svg"]]
    elif logo.get("type") == "img" and logo.get("url"):
        parts += [
            f"![{r.firm_name} logo]({logo['url']})",
            "",
            f"Source: <{logo['url']}>",
        ]
    else:
        parts.append("_No logo found during the scrape._")

    parts.append("")
    return "\n".join(parts)


def build_design_md(result: ScrapeResult) -> str:
    return _front_matter(result) + "\n\n" + _body(result)
