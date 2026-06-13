"""eLife family extractor via JATS XML + CDN figures.

Stage 6 E9 Step 1 (2026-05-26). eLife is gold open access; their
public API returns the canonical JATS XML URL and CDN-hosted
JPG figures for every published article — no Playwright, no
Cloudflare warmup, no auth. The JATS shape is the same one
bioRxiv uses, so `parse_jats` is reused verbatim.

Most eLife papers index in PMC within days of publication, so
the existing PMC route in `resolve.py` catches them first; this
extractor handles the gap (brand-new papers PMC hasn't indexed,
or papers PMC missed entirely).

Public surface:
  is_elife_doi(doi)               -> bool
  extract(doi, output_dir=None)   -> dict [session skeleton]
  fetch_manuscript_pdf(doi)       -> bytes  (Download button)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests

from paperwhirl.biorxiv import parse_jats

ELIFE_API = "https://api.elifesciences.org/articles"
ELIFE_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"

# DOI shape: 10.7554/eLife.<numeric-id> or 10.7554/eLife.<id>.<version>.
# The version suffix names a specific reviewed-preprint revision; the
# canonical record-of-version DOI is the one without the .N suffix and
# is what we resolve against the eLife API.
_ELIFE_DOI_RE = re.compile(r"^10\.7554/eLife\.(\d+)(?:\.\d+)?$", re.IGNORECASE)


def is_elife_doi(doi: str) -> bool:
    return bool(_ELIFE_DOI_RE.match(doi or ""))


def _article_id(doi: str) -> str:
    m = _ELIFE_DOI_RE.match(doi)
    if not m:
        raise ValueError(f"not an eLife DOI: {doi!r}")
    return m.group(1)


def _fetch_api(article_id: str) -> dict:
    """Hit the eLife public API for the article's canonical metadata.

    Returns the parsed JSON. Includes the JATS XML URL (`xml`), the
    PDF URL (`pdf`), and the figures-only PDF URL (`figuresPdf`).
    Raises requests.RequestException on network failure.
    """
    r = requests.get(
        f"{ELIFE_API}/{article_id}",
        headers={"User-Agent": ELIFE_UA},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _fetch_xml(xml_url: str) -> str:
    r = requests.get(
        xml_url,
        headers={"User-Agent": ELIFE_UA},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def _figure_jpg_url(article_id: str, graphic_href: str) -> str:
    """Map a JATS <graphic href> to the live eLife CDN JPG URL.

    The href in the XML points at a .tif (e.g.,
    `elife-107565-fig1-v1.tif`); the CDN serves a same-name `.jpg`
    variant alongside it that browsers can render directly. Just
    substitute the extension.
    """
    base = graphic_href.rsplit("/", 1)[-1]
    if base.lower().endswith(".tif") or base.lower().endswith(".tiff"):
        base = re.sub(r"\.tiff?$", ".jpg", base, flags=re.IGNORECASE)
    return f"https://cdn.elifesciences.org/articles/{article_id}/{base}"


def fetch_manuscript_pdf(doi: str) -> bytes:
    """Download the eLife article PDF.

    The API's `pdf` field carries the canonical CDN URL; fetch via
    plain requests (eLife's CDN is unauthenticated public S3).
    """
    article_id = _article_id(doi)
    meta = _fetch_api(article_id)
    pdf_url = meta.get("pdf")
    if not pdf_url:
        raise ValueError(f"no PDF URL in eLife API response for {doi}")
    r = requests.get(
        pdf_url,
        headers={"User-Agent": ELIFE_UA},
        timeout=60,
    )
    r.raise_for_status()
    return r.content


def extract(
    doi: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from an eLife article."""
    from datetime import datetime, timezone

    article_id = _article_id(doi)
    print(f"  [elife] resolving {doi} (article id {article_id})")
    meta = _fetch_api(article_id)
    xml_url = meta.get("xml")
    if not xml_url:
        raise ValueError(f"no XML URL in eLife API response for {doi}")

    print(f"  [elife] fetching JATS: {xml_url}")
    xml = _fetch_xml(xml_url)
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
            img_url = _figure_jpg_url(article_id, href)
            try:
                data = requests.get(
                    img_url,
                    headers={"User-Agent": ELIFE_UA},
                    timeout=30,
                ).content
                if data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG":
                    dest = figures_dir / f"figure_{fig['number']}.jpg"
                    dest.write_bytes(data)
                    asset = str(dest.relative_to(out))
                    print(f"    [elife-fig] F{fig['number']} → {dest.name} ({len(data)} bytes)")
                else:
                    print(f"    [elife-fig] F{fig['number']} fetch returned non-image data")
            except requests.RequestException as exc:
                print(f"    [elife-fig] F{fig['number']} fetch failed: {type(exc).__name__}: {exc}")

        source_figures.append({
            "id": source_id,
            "figure": fig["label"],
            "page": None,
            "asset": asset,
            "caption": fig["caption_text"],
            "extraction_method": "elife_jats+elife_cdn",
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
                "asset_role": "elife_cdn" if asset else "missing",
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

    # Tables. eLife uses standard JATS <table-wrap> with embedded
    # <table> markup; parse_jats already collected them.
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
            "extraction_method": "elife_jats",
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
            "abstract": parsed.get("abstract", ""),
            "authors": authors,
            "first_author": authors[0].split()[-1] if authors else "",
            "year": parsed.get("year"),
            "journal": parsed.get("journal") or "eLife",
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
        # Stage 5 E8 S1: parsed bibliography for lookup_reference.
        "references": parsed.get("references", []),
        "exports": {
            "pdf": {
                "path": None,
                "created_at": None,
            }
        },
    }
