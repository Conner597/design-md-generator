"""FastAPI backend for the Design MD Generator.

POST /api/generate  { firm_name, firm_url }  ->  { filename, content }
"""
from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from design_md import build_design_md
from scraper import scrape

app = FastAPI(title="Design MD Generator")

# Local-only tool; allow the Vite dev server (and anything else) to call it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    firm_name: str
    firm_url: str


class GenerateResponse(BaseModel):
    filename: str
    content: str


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "firm"


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/api/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    name = req.firm_name.strip()
    url = req.firm_url.strip()
    if not name or not url:
        raise HTTPException(status_code=400, detail="firm_name and firm_url are both required.")
    try:
        result = scrape(name, url)
    except Exception as exc:  # noqa: BLE001 - surface any scrape failure to the UI
        raise HTTPException(status_code=502, detail=f"Could not scrape the site: {exc}")
    return GenerateResponse(filename=f"{_slug(name)}-design.md", content=build_design_md(result))
