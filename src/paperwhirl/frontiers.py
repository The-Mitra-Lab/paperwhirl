"""Frontiers family extractor via JATS XML + CDN figures.

Stage 6 E9 Step 3 (2026-05-27). Frontiers is gold open access;
their article pages serve JATS XML at a stable URL and figure
images at a clean CDN path. No Cloudflare challenge, no auth —
plain requests works.

Most Frontiers papers index in PMC eventually, but indexing lag
is longer than eLife's. This extractor catches papers PMC hasn't
seen yet.

Public surface:
    is_frontiers_doi(doi)           -> bool
    extract(doi, output_dir=None)   -> dict [session skeleton]
    fetch_manuscript_pdf(doi)       -> bytes  (Download button)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests

from paperwhirl.biorxiv import parse_jats

FRONTIERS_HOME = "https://www.frontiersin.org/"
FRONTIERS_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"

# Frontiers DOIs: 10.3389/<journal-code>.<year>.<article-id>
# Examples: 10.3389/fcell.2025.1590311, 10.3389/falgy.2024.1464948.
# The article-id is the trailing numeric component (also the
# directory name in the CDN figure path).
_FRONTIERS_DOI_RE = re.compile(
    r"^10\.3389/[a-z]+\.\d{4}\.(\d+)$", re.IGNORECASE
)


def is_frontiers_doi(doi: str) -> bool:
    return bool(_FRONTIERS_DOI_RE.match(doi or ""))


def _article_id(doi: str) -> str:
    m = _FRONTIERS_DOI_RE.match(doi)
    if not m:
        raise ValueError(f"not a Frontiers DOI: {doi!r}")
    return m.group(1)


def _resolve_canonical_url(doi: str) -> str:
    """Get the journal-slugged canonical article URL.

    Frontiers exposes a short form (`/articles/{doi}/full`) that
    redirects to the canonical `/journals/{journal-slug}/articles/
    {doi}/full`. The XML/PDF endpoints accept only the canonical
    form (the short form returns 404 on XML/PDF without the slug),
    so resolve once via the /full redirect and then substitute the
    suffix.
    """
    r = requests.head(
        f"https://www.frontiersin.org/articles/{doi}/full",
        headers={"User-Agent": FRONTIERS_UA},
        timeout=15,
        allow_redirects=True,
    )
    r.raise_for_status()
    final = r.url
    if not final.endswith("/full"):
        raise ValueError(f"unexpected Frontiers redirect target for {doi}: {final}")
    return final[:-len("/full")]


def _fetch_xml(doi: str) -> str:
    base = _resolve_canonical_url(doi)
    r = requests.get(
        f"{base}/xml",
        headers={"User-Agent": FRONTIERS_UA, "Accept": "application/xml"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def _figure_webp_url(article_id: str, graphic_href: str) -> str:
    """Map a JATS <graphic href> to the live Frontiers CDN URL.

    JATS hrefs are `.tif` filenames (e.g.,
    `fcell-13-1590311-g001.tif`); the CDN serves only the `.webp`
    variant alongside. Substitute extension; the basename otherwise
    matches.
    """
    base = graphic_href.rsplit("/", 1)[-1]
    base = re.sub(r"\.tiff?$", ".webp", base, flags=re.IGNORECASE)
    return f"https://www.frontiersin.org/files/Articles/{article_id}/xml-images/{base}"


def fetch_manuscript_pdf(doi: str) -> bytes:
    base = _resolve_canonical_url(doi)
    r = requests.get(
        f"{base}/pdf",
        headers={"User-Agent": FRONTIERS_UA},
        timeout=60,
    )
    r.raise_for_status()
    return r.content


def extract(
    doi: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a Frontiers article."""
    from datetime import datetime, timezone

    article_id = _article_id(doi)
    print(f"  [frontiers] fetching JATS for {doi} (article {article_id})")
    xml = _fetch_xml(doi)
    parsed = parse_jats(xml)

    slug = doi.split("/", 1)[-1].lower().replace(".", "_")
    out = output_dir or Path("/tmp") / slug
    out.mkdir(parents=True, exist_ok=True)
    figures_dir = out / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    walkthrough_figures = []
    source_figures = []
    for fig in parsed["figures"]:
        source_id = f"source_fig_{fig['number']}"
        asset = None
        href = fig.get("graphic_href") or ""
        if href:
            img_url = _figure_webp_url(article_id, href)
            try:
                data = requests.get(
                    img_url,
                    headers={"User-Agent": FRONTIERS_UA},
                    timeout=30,
                ).content
                # Accept JPEG, PNG, or WEBP magic bytes — Frontiers
                # serves WEBP (RIFF....WEBP header).
                is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
                is_jpeg = data[:3] == b"\xff\xd8\xff"
                is_png = data[:4] == b"\x89PNG"
                if is_webp or is_jpeg or is_png:
                    ext = ".webp" if is_webp else (".jpg" if is_jpeg else ".png")
                    dest = figures_dir / f"figure_{fig['number']}{ext}"
                    dest.write_bytes(data)
                    asset = str(dest.relative_to(out))
                    print(f"    [frontiers-fig] F{fig['number']} → {dest.name} ({len(data)} bytes)")
                else:
                    print(f"    [frontiers-fig] F{fig['number']} fetch returned non-image data")
            except requests.RequestException as exc:
                print(f"    [frontiers-fig] F{fig['number']} fetch failed: {type(exc).__name__}: {exc}")

        source_figures.append({
            "id": source_id,
            "figure": fig["label"],
            "page": None,
            "asset": asset,
            "caption": fig["caption_text"],
            "extraction_method": "frontiers_jats+frontiers_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "source_order": fig.get("source_order", fig["number"]),
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": asset,
                "asset_role": "frontiers_cdn" if asset else "missing",
                "crop": None,
                "original_caption": fig["caption_text"],
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
        })

    # Tables.
    walkthrough_tables = []
    source_tables = []
    for tidx, tbl in enumerate(
        sorted(parsed.get("tables", []),
               key=lambda t: (t.get("number") or 0, t.get("source_order") or 0)),
        start=1,
    ):
        source_table_id = f"source_tbl_{tidx}"
        source_tables.append({
            "id": source_table_id,
            "table": tbl["label"],
            "caption": tbl["caption_text"],
            "html": tbl["html"],
            "extraction_method": "frontiers_jats",
        })
        walkthrough_tables.append({
            "id": f"table_{tidx}",
            "order": tidx,
            "source_order": 9999 + tidx,
            "label": tbl["label"],
            "source_table_id": source_table_id,
            "view": {
                "caption_text": tbl["caption_text"],
                "html": tbl["html"],
                "asset_role": "jats" if tbl["html"] else "missing",
            },
            "analysis": {
                "interpretation": "",
                "linked_claims": [],
            },
        })

    body_text = ""
    pages_list = []
    if parsed["sections"]:
        section_texts = []
        for sec in parsed["sections"]:
            if sec["title"]:
                section_texts.append(f"## {sec['title']}\n\n{sec['text']}")
            else:
                section_texts.append(sec["text"])
        body_text = "\n\n".join(section_texts)
        pages_list = [
            {"page": i + 1, "text": sec["text"]}
            for i, sec in enumerate(parsed["sections"])
        ]

    if output_dir:
        (output_dir / "extracted_text.txt").write_text(body_text, encoding="utf-8")

    title = parsed["title"]
    authors = parsed["authors"]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage6_e9",
            "created_at": now,
            "updated_at": now,
            "mode": "full",
            "title": title,
        },
        "paper": {
            "id": slug,
            "title": title,
            "authors": authors,
            "first_author": authors[0].split()[-1] if authors else "",
            "year": parsed.get("year"),
            "journal": parsed.get("journal") or "Frontiers",
            "doi": doi,
            "pmid": parsed.get("pmid"),
            "pmcid": parsed.get("pmcid"),
            "arxiv_id": None,
            "biorxiv_doi": None,
            "preprint_url": None,
            "source_pdf": None,
            "publication_state": "published",
        },
        "paper_kind": {
            "primary": "empirical",
            "secondary": None,
        },
        "overview": {
            "background": "",
            "gap": "",
            "claims": [],
        },
        "figures": walkthrough_figures,
        "tables": walkthrough_tables,
        "discussion": {
            "synthesis": "",
            "takeaways": [],
            "caveats": [],
            "next_steps": [],
        },
        "extracted_source": {
            "text": {
                "extracted_text_path": "extracted_text.txt",
                "pages": pages_list,
            },
            "figures": source_figures,
            "tables": source_tables,
        },
        "references": parsed.get("references", []),
        "exports": {
            "pdf": {
                "path": None,
                "created_at": None,
            }
        },
    }
