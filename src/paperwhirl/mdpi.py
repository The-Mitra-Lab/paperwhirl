"""MDPI figure recovery from the article HTML page.

Stage 5 E7 S13 (2026-05-19). MDPI papers are gold OA, but PMC's
NXML deposit for them sometimes lacks figures (or PMC's CDN scrape
is throttled). The obvious recovery — fetch the publisher PDF — does
NOT work: MDPI's WAF hard-blocks the `/pdf` endpoint (403 to
requests, "Failed to fetch" to in-page fetch, hidden download
button). The article *page*, however, loads fine when navigated
directly, and the per-figure images under
`/<journal>/<id>/article_deploy/html/images/...-g00N.png` ARE
fetchable via an in-page browser fetch (verified: 1.3 MB full-res
PNG came back clean while the PDF stayed walled).

So instead of a publisher-PDF fetch, we scrape figure image URLs +
captions straight from the article HTML and download the images.

Public surface:
    extract_figures(doi, paper_dir)  -> list[figure_dict]

Returns figure dicts in the session-skeleton schema (same shape
`extract.extract_figures_only_from_pdf` produces), or [] on any
failure. Used by `resolve._supplement_figures_from_pdf` for
`10.3390/...` papers that reach it with no usable figures.

Gotcha carried over from the PDF attempt: drive the article-URL
*resolution* with plain requests (the doi.org redirect through a
Playwright page trips MDPI's "Access Denied" wall), then navigate
the page directly to the resolved URL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests

from paperwhirl.browser import PlaywrightSession

MDPI_HOME = "https://www.mdpi.com/"

# Full-res figure image. MDPI serves these from a CDN host via a
# protocol-relative src, e.g.
# `//pub.mdpi-res.com/<journal>/<id>/article_deploy/html/images/<base>-g<NNN>.png`.
# Capture the (optional scheme +) host + path and the figure number;
# prefer .png (full-res) over the "-550.jpg" preview variant.
_FIG_IMG_RE = re.compile(
    r'((?:https?:)?//[^"\s]*?/article_deploy/html/images/[^"\s]*?-g0*(\d+)\.png)',
    re.IGNORECASE,
)


def _norm_img_url(u: str) -> str:
    """Normalize an MDPI figure-image URL to an absolute https URL on
    the **www.mdpi.com** host.

    The article markup points images at the CDN host
    (`//pub.mdpi-res.com/…`), but that's cross-origin from the article
    page — the in-page browser fetch (which carries the page's
    Cloudflare cookie) then fails CORS. www.mdpi.com serves the same
    path same-origin, so we keep only the path and force that host.
    """
    from urllib.parse import urlparse
    if u.startswith("//"):
        u = "https:" + u
    path = urlparse(u).path if u.startswith("http") else u
    return "https://www.mdpi.com" + path
# Caption block: <div class="html-fig_description">Figure N. …</div>
_FIG_CAP_RE = re.compile(
    r'class="html-fig_description"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)


def _resolve_article_url(doi: str) -> str:
    """Resolve a `10.3390/...` DOI to its MDPI article URL via the
    doi.org redirect, using plain requests (no JS — avoids tripping
    MDPI's bot wall, which the Playwright-driven redirect does)."""
    r = requests.head(
        f"https://doi.org/{doi}",
        allow_redirects=True,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 PaperWhirl/0.1"},
    )
    return r.url.rstrip("/")


def _parse_captions(html: str) -> dict[int, str]:
    """Map figure number → caption text from the article HTML."""
    caps: dict[int, str] = {}
    for m in _FIG_CAP_RE.finditer(html):
        raw = re.sub(r"<[^>]+>", " ", m.group(1))
        raw = re.sub(r"\s+", " ", raw).strip()
        nm = re.match(r"Figure\s+(\d+)\.?\s*", raw, re.IGNORECASE)
        if nm:
            caps[int(nm.group(1))] = raw
    return caps


def _figure_record(number: int, asset_rel: str, caption: str) -> dict[str, Any]:
    """One figure entry in the session-skeleton schema (mirrors the
    shape arxiv/pmc/extract produce)."""
    return {
        "id": f"figure_{number}",
        "order": number,
        "label": f"Figure {number}",
        "source_figure_id": f"source_fig_{number}",
        "view": {
            "source_page": None,
            "asset": asset_rel,
            "asset_role": "mdpi_html",
            "crop": None,
            "original_caption": caption,
            "display_legend": "",
        },
        "analysis": {
            "motivation": "",
            "question": "",
            "approach": "",
            "evidence": "",
            "interpretation": "",
            "linked_claims": [],
        },
    }


def extract_figures(doi: str, paper_dir: Path) -> list[dict[str, Any]]:
    """Scrape + download figures from the MDPI article page.

    Returns figure dicts (skeleton schema) with images saved under
    `paper_dir/figures/figure_<N>.png`. Returns [] on any failure —
    the caller treats that as "no figures recovered" and degrades
    gracefully.
    """
    try:
        article_url = _resolve_article_url(doi)
    except requests.RequestException as exc:
        print(f"  [mdpi] could not resolve article URL for {doi}: {exc}")
        return []

    figures_dir = paper_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    try:
        with PlaywrightSession(MDPI_HOME) as session:
            session._warm()
            session._page.goto(
                article_url, wait_until="domcontentloaded", timeout=60000
            )
            import time as _time
            for _ in range(60):
                if "Just a moment" not in session._page.title():
                    break
                _time.sleep(0.5)

            html = session._page.content()
            captions = _parse_captions(html)

            # number → full image URL (first .png wins per number)
            img_urls: dict[int, str] = {}
            for m in _FIG_IMG_RE.finditer(html):
                num = int(m.group(2))
                if num not in img_urls:
                    img_urls[num] = _norm_img_url(m.group(1))

            if not img_urls:
                print(f"  [mdpi] {doi}: no figure images found on article page")
                return []

            figures: list[dict[str, Any]] = []
            for num in sorted(img_urls):
                try:
                    data = session.fetch_bytes(img_urls[num])
                except Exception as exc:
                    print(f"  [mdpi] figure {num} fetch failed: {type(exc).__name__}")
                    continue
                if data[:4] != b"\x89PNG" and data[:3] != b"\xff\xd8\xff":
                    print(f"  [mdpi] figure {num}: not an image, skipping")
                    continue
                dest = figures_dir / f"figure_{num}.png"
                dest.write_bytes(data)
                figures.append(
                    _figure_record(
                        num,
                        f"figures/{dest.name}",
                        captions.get(num, f"Figure {num}"),
                    )
                )
                print(f"    [mdpi-fig] g{num:03d} → {dest.name} ({len(data)} bytes)")

            return figures
    except Exception as exc:
        print(f"  [mdpi] {doi}: figure scrape failed: {type(exc).__name__}: {exc}")
        return []
