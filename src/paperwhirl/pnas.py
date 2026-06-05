"""PNAS family extractor via article HTML scraping.

Stage 6 E9 Step 2 (2026-05-26). PNAS is part-OA: papers come out
of an initial six-month embargo and many are OA at publication
via PNAS Open Access. Behind Cloudflare with a JS challenge —
needs a Playwright session for warmup, then plain HTML parsing.

Most PNAS papers index in PMC quickly, so the existing PMC route
in resolve.py catches them first; this extractor handles the
new-paper gap (PMC metadata-only / `pmc-prop-has-pdf` stubs) and
papers PMC missed.

Public surface:
    is_pnas_doi(doi)               -> bool
    extract(doi, output_dir=None, session=None) -> dict
    fetch_manuscript_pdf(doi)      -> bytes  (Download button)
    PNASPaywallStub                -> raised on stub pages
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from paperwhirl.browser import PlaywrightSession

PNAS_HOME = "https://www.pnas.org/"

_PNAS_DOI_RE = re.compile(r"^10\.1073/pnas\.[0-9a-z.]+$", re.IGNORECASE)

# Section titles to drop from the body (matches the Cell/Nature pattern).
_SKIP_SECTIONS = {
    "abstract",
    "significance",
    "data, materials, and software availability",
    "data availability",
    "supporting information",
    "acknowledgments",
    "author contributions",
    "competing interest",
    "competing interests",
    "references",
    "footnotes",
}


class PNASPaywallStub(Exception):
    """Raised when pnas.org returns a stub instead of full article text."""


def is_pnas_doi(doi: str) -> bool:
    return bool(_PNAS_DOI_RE.match(doi or ""))


def _article_url(doi: str) -> str:
    return f"https://www.pnas.org/doi/{doi}"


def _detect_stub(soup: BeautifulSoup) -> bool:
    # No real body content. The article page renders the full text in
    # multiple <h2>-headed sections; a stub has only metadata/abstract.
    if not soup.find("h2", string=re.compile(r"\bResults?\b|\bDiscussion\b", re.IGNORECASE)):
        return True
    return False


def _extract_metadata(soup: BeautifulSoup, doi: str) -> dict[str, Any]:
    def _meta_all(name: str) -> list[str]:
        return [m.get("content", "") for m in soup.find_all("meta", attrs={"name": name}) if m.get("content")]

    def _meta(name: str) -> str:
        vals = _meta_all(name)
        return vals[0] if vals else ""

    title = _meta("citation_title")
    authors_raw = _meta_all("citation_author")
    # PNAS emits citation_author as "Surname, Given" — flip to
    # "Given Surname" to match the conventional shape used by other
    # extractors.
    authors: list[str] = []
    for a in authors_raw:
        if "," in a:
            surname, given = a.split(",", 1)
            authors.append(f"{given.strip()} {surname.strip()}")
        else:
            authors.append(a.strip())

    journal = _meta("citation_journal_title") or "Proceedings of the National Academy of Sciences"

    year = None
    date = _meta("citation_publication_date") or _meta("citation_online_date")
    m = re.match(r"(\d{4})", date or "")
    if m:
        year = int(m.group(1))

    # PNAS marks the abstract under <h2>Abstract</h2>; its parent
    # section is the abstract block.
    abstract = ""
    abs_h2 = soup.find("h2", string=re.compile(r"^\s*Abstract\s*$", re.IGNORECASE))
    if abs_h2 is not None:
        abs_section = abs_h2.find_parent("section") or abs_h2.parent
        if abs_section is not None:
            # Pull all descendant paragraphs, skipping the heading itself.
            parts = []
            for p in abs_section.find_all(["p", "div"]):
                t = p.get_text(" ", strip=True)
                if t and t.lower() != "abstract":
                    parts.append(t)
            abstract = " ".join(parts)

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "journal": journal,
        "year": year,
    }


_FIG_ID_RE = re.compile(r"fig0*(\d+)$", re.IGNORECASE)


def _extract_figures(
    soup: BeautifulSoup,
    output_dir: Path,
    session: PlaywrightSession,
) -> list[dict[str, Any]]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    figures: list[dict[str, Any]] = []
    for fig_el in soup.find_all("figure"):
        fid = fig_el.get("id", "")
        m = _FIG_ID_RE.fullmatch(fid)
        if not m:
            # Skip non-numbered figures (graphical abstract, etc.).
            continue
        number = int(m.group(1))

        # Caption: PNAS uses <figcaption> with the label + body together.
        cap_el = fig_el.find("figcaption") or fig_el.find("div", class_=re.compile(r"caption", re.IGNORECASE))
        caption_text = cap_el.get_text(" ", strip=True) if cap_el else f"Figure {number}"
        label = f"Figure {number}"

        # Image: the large variant lives under /cms/.../images/large/...jpg.
        # Sometimes the <img> uses a thumbnail src with the large href on
        # the parent <a> — prefer the large src when both are present.
        img_url = ""
        a_full = fig_el.find("a", href=re.compile(r"/images/large/"))
        if a_full and a_full.get("href"):
            img_url = urljoin(PNAS_HOME, a_full["href"])
        if not img_url:
            img = fig_el.find("img")
            if img:
                src = img.get("src") or img.get("data-src") or ""
                if src:
                    img_url = urljoin(PNAS_HOME, src)

        asset: str | None = None
        if img_url:
            try:
                data = session.fetch_bytes(img_url)
            except Exception as exc:
                print(f"    [pnas-fig] F{number} fetch failed: {type(exc).__name__}: {exc}")
                data = None
            if data and (data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG"):
                ext = ".jpg" if img_url.lower().rstrip("/").endswith(".jpg") else ".png"
                dest = figures_dir / f"figure_{number}{ext}"
                dest.write_bytes(data)
                asset = str(dest.relative_to(output_dir))
                print(f"    [pnas-fig] F{number} → {dest.name} ({len(data)} bytes)")

        figures.append({
            "number": number,
            "label": label,
            "caption_text": caption_text,
            "asset": asset,
        })

    figures.sort(key=lambda f: f["number"])
    return figures


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Walk h2 section headings and collect paragraphs under each."""
    sections: list[dict[str, str]] = []
    for h2 in soup.find_all("h2"):
        title = h2.get_text(" ", strip=True)
        if not title:
            continue
        norm = re.sub(r"\s+", " ", title).strip().lower()
        if norm in _SKIP_SECTIONS:
            continue

        # The h2's parent <section> typically holds the body content.
        # Fall back to walking siblings if no <section> wraps it.
        sec = h2.find_parent("section") or h2.parent
        if sec is None:
            continue

        # PNAS uses the Atypon-platform convention `<div role="paragraph">`
        # for body text rather than <p>. Match both so any other Atypon
        # journals that ride the same code path also work.
        parts: list[str] = []
        for child in sec.find_all(["h3", "p", "div"]):
            if child.find_parent("figure"):
                continue
            if child.find_parent("table"):
                continue
            if child.name == "h3":
                t = child.get_text(" ", strip=True)
                if t:
                    parts.append(f"### {t}")
                continue
            if child.name == "div" and child.get("role") != "paragraph":
                continue
            t = child.get_text(" ", strip=True)
            if t:
                parts.append(t)

        text = "\n\n".join(parts)
        if text:
            sections.append({"title": title, "text": text})
    return sections


def fetch_manuscript_pdf(doi: str) -> bytes:
    """Download the PNAS article PDF via the citation_pdf_url path.

    Needs the same Playwright session shape as the HTML extractor so
    Cloudflare clears before the PDF request.
    """
    url = f"https://www.pnas.org/doi/pdf/{doi}"
    with PlaywrightSession(PNAS_HOME) as session:
        session._warm()
        return session.fetch_bytes(url)


def extract(
    doi: str,
    output_dir: Path | None = None,
    session: PlaywrightSession | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a pnas.org article page."""
    from datetime import datetime, timezone

    url = _article_url(doi)
    print(f"  [pnas] fetching {url}")

    owns_session = session is None
    if owns_session:
        session = PlaywrightSession(PNAS_HOME).__enter__()

    try:
        html = session.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        if _detect_stub(soup):
            raise PNASPaywallStub(
                f"pnas.org served a stub for {doi} (paywalled or pre-publication)"
            )

        meta = _extract_metadata(soup, doi)
        slug = doi.split("/", 1)[-1].lower().replace(".", "_")
        out = output_dir or Path("/tmp") / slug
        out.mkdir(parents=True, exist_ok=True)

        figures = _extract_figures(soup, out, session)
        sections = _extract_sections(soup)
    finally:
        if owns_session:
            session.__exit__(None, None, None)

    walkthrough_figures = []
    source_figures = []
    for fig in figures:
        source_id = f"source_fig_{fig['number']}"
        source_figures.append({
            "id": source_id,
            "figure": fig["label"],
            "page": None,
            "asset": fig["asset"],
            "caption": fig["caption_text"],
            "extraction_method": "pnas_html+pnas_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "pnas_cdn" if fig["asset"] else "missing",
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

    body_text = ""
    pages_list = []
    if sections:
        section_texts = [f"## {s['title']}\n\n{s['text']}" for s in sections]
        body_text = "\n\n".join(section_texts)
        pages_list = [
            {"page": i + 1, "text": s["text"]}
            for i, s in enumerate(sections)
        ]

    if output_dir:
        (output_dir / "extracted_text.txt").write_text(body_text, encoding="utf-8")

    authors = meta["authors"]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage6_e9",
            "created_at": now,
            "updated_at": now,
            "mode": "full",
            "title": meta["title"],
        },
        "paper": {
            "id": slug,
            "title": meta["title"],
            "authors": authors,
            "first_author": authors[0].split()[-1] if authors else "",
            "year": meta["year"],
            "journal": meta["journal"],
            "doi": doi,
            "pmid": None,
            "pmcid": None,
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
        },
        "exports": {
            "pdf": {
                "path": None,
                "created_at": None,
            }
        },
    }
