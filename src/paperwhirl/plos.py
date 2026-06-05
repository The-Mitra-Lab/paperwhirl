"""PLOS family extractor via JATS XML + CDN figures.

Stage 6 E9.5 Step 1 (2026-05-27). PLOS journals (PLOS Biology,
PLOS One, PLOS Genetics, PLOS Pathogens, PLOS Computational
Biology, PLOS Medicine) are gold OA. Their `/article/file`
endpoint returns canonical JATS XML via a signed Google
Cloud Storage redirect; figure images come from
`/article/figure/image`. Plain requests, no Cloudflare
challenge.

Most PLOS papers index in PMC quickly, so the existing PMC
route catches them first; this extractor handles the new-
paper gap and any PMC misses.

Public surface:
    is_plos_doi(doi)               -> bool
    extract(doi, output_dir=None)  -> dict
    fetch_manuscript_pdf(doi)      -> bytes
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests

from paperwhirl.biorxiv import parse_jats

PLOS_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"

# DOI: 10.1371/journal.<short>.<id>. The <short> tag identifies
# which PLOS journal hosts the paper; we need it in the URL path.
_PLOS_DOI_RE = re.compile(
    r"^10\.1371/journal\.(pbio|pone|pgen|ppat|pcbi|pmed|pntd|pclm|pwat|pdig|pmen|pstr)\.\d+$",
    re.IGNORECASE,
)

# DOI short → journal-URL slug. Drawn from the live URL each
# journal uses; not every PLOS journal slug derives by string
# substitution (plosgenetics ≠ plosgen, etc.), so explicit map.
_PLOS_JOURNAL_SLUGS = {
    "pbio": "plosbiology",
    "pone": "plosone",
    "pgen": "plosgenetics",
    "ppat": "plospathogens",
    "pcbi": "ploscompbiol",
    "pmed": "plosmedicine",
    "pntd": "plosntds",
    "pclm": "climate",
    "pwat": "water",
    "pdig": "digitalhealth",
    "pmen": "mentalhealth",
    "pstr": "sustainabilitytransformation",
}


def is_plos_doi(doi: str) -> bool:
    return bool(_PLOS_DOI_RE.match(doi or ""))


def _journal_slug(doi: str) -> str:
    m = _PLOS_DOI_RE.match(doi)
    if not m:
        raise ValueError(f"not a PLOS DOI: {doi!r}")
    short = m.group(1).lower()
    return _PLOS_JOURNAL_SLUGS[short]


def _xml_url(doi: str) -> str:
    slug = _journal_slug(doi)
    return f"https://journals.plos.org/{slug}/article/file?id={doi}&type=manuscript"


def _pdf_url(doi: str) -> str:
    slug = _journal_slug(doi)
    return f"https://journals.plos.org/{slug}/article/file?id={doi}&type=printable"


def _figure_url(doi: str, figure_doi: str, *, size: str = "large") -> str:
    """Build the canonical PLOS figure image URL.

    `figure_doi` is the full figure DOI as stored in the JATS
    `<graphic href="info:doi/...">` — strip the `info:doi/`
    prefix in the caller.
    """
    slug = _journal_slug(doi)
    return f"https://journals.plos.org/{slug}/article/figure/image?size={size}&id={figure_doi}"


def _fetch_xml(doi: str) -> str:
    r = requests.get(
        _xml_url(doi),
        headers={"User-Agent": PLOS_UA, "Accept": "application/xml"},
        timeout=30,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r.text


def fetch_manuscript_pdf(doi: str) -> bytes:
    r = requests.get(
        _pdf_url(doi),
        headers={"User-Agent": PLOS_UA},
        timeout=60,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r.content


def extract(
    doi: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a PLOS article."""
    from datetime import datetime, timezone

    print(f"  [plos] fetching JATS for {doi}")
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
        # JATS encodes figure refs as `info:doi/10.1371/...gNNN`.
        figure_doi = href.removeprefix("info:doi/") if href else ""
        if figure_doi:
            img_url = _figure_url(doi, figure_doi, size="large")
            try:
                r = requests.get(
                    img_url,
                    headers={"User-Agent": PLOS_UA},
                    timeout=30,
                    allow_redirects=True,
                )
                data = r.content if r.status_code == 200 else None
            except requests.RequestException as exc:
                print(f"    [plos-fig] F{fig['number']} fetch failed: {type(exc).__name__}: {exc}")
                data = None
            if data and (data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG"):
                ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
                dest = figures_dir / f"figure_{fig['number']}{ext}"
                dest.write_bytes(data)
                asset = str(dest.relative_to(out))
                print(f"    [plos-fig] F{fig['number']} → {dest.name} ({len(data)} bytes)")

        source_figures.append({
            "id": source_id,
            "figure": fig["label"],
            "page": None,
            "asset": asset,
            "caption": fig["caption_text"],
            "extraction_method": "plos_jats+plos_cdn",
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
                "asset_role": "plos_cdn" if asset else "missing",
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
            "extraction_method": "plos_jats",
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
            "journal": parsed.get("journal") or "PLOS",
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
