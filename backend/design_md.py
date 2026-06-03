"""Render a ScrapeResult into a DESIGN.md string.

Format: machine-readable YAML front matter (design tokens) followed by
human-readable markdown prose (Overview, Colors, Logo). The logo is embedded
as inline SVG when available, otherwise referenced by URL.
"""
from __future__ import annotations

from scraper import ScrapeResult

COLOR_LABELS = {
    "primary": "Primary",
    "secondary": "Secondary",
    "tertiary": "Tertiary",
    "neutral": "Neutral",
}


def _yaml_escape(value: str) -> str:
    return value.replace('"', '\\"')


def _front_matter(r: ScrapeResult) -> str:
    lines = ["---", f'name: "{_yaml_escape(r.firm_name)}"', f'url: "{r.url}"']
    if r.logo.get("type") == "img" and r.logo.get("url"):
        lines.append(f'logo: "{r.logo["url"]}"')
    elif r.logo.get("type") == "svg":
        lines.append("logo: embedded")
    lines.append("colors:")
    for key in ("primary", "secondary", "tertiary", "neutral"):
        lines.append(f'  {key}: "{r.colors[key]}"')
    lines.append("typography:")
    for key in ("h1", "body-md", "label-caps"):
        token = r.typography[key]
        lines.append(f"  {key}:")
        lines.append(f"    fontFamily: {token['fontFamily']}")
        lines.append(f"    fontSize: {token['fontSize']}")
    lines.append("rounded:")
    lines.append(f"  sm: {r.rounded['sm']}")
    lines.append(f"  md: {r.rounded['md']}")
    lines.append("spacing:")
    lines.append(f"  sm: {r.spacing['sm']}")
    lines.append(f"  md: {r.spacing['md']}")
    lines.append("---")
    return "\n".join(lines)


def _body(r: ScrapeResult) -> str:
    parts = [
        f"# {r.firm_name}",
        "",
        "## Overview",
        "",
        f"Design tokens reverse-engineered from [{r.url}]({r.url}). The front "
        "matter above is machine-readable for agents; the notes below explain "
        "how to apply it. Colors, fonts, radii, and the logo were extracted "
        "automatically from the site's published styles and should be reviewed "
        "and adjusted before use.",
        "",
        "## Colors",
        "",
    ]
    for key, label in COLOR_LABELS.items():
        parts.append(f"- **{label}** `{r.colors[key]}`")
    parts += ["", "## Logo", ""]
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
