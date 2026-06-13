"""Science family (AAAS) extractor via article HTML scraping.

Handles Science, Science Advances, Science Immunology, Science
Translational Medicine, Science Signaling, Science Robotics. All
share the Atypon platform layout under `science.org`.

Coverage:
  - `10.1126/sciadv.*`  — Science Advances is fully OA; happy path
    works on any IP.
  - `10.1126/science.*` — Science proper is paywalled. From the
    cluster, the page returns a stub ("Access the full article")
    and this extractor raises SciencePaywallStub. From the user's
    laptop (subscribing IP), the same code path renders full
    content identically to OA.

Public surface:
  is_science_doi(doi) -> bool
  extract(doi, ...)   -> dict [session skeleton]
  SciencePaywallStub  -> exception raised when the page is a stub
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from paperwhirl.browser import PlaywrightSession

SCIENCE_HOME = "https://www.science.org/"
SCIENCE_DOI_PREFIX = "10.1126/"

# Sub-journal slugs that share the science.org platform. Detector
# accepts any 10.1126/ DOI — sub-journal routing only affects URL
# choice if needed.
SCIENCE_SUBJOURNALS = {
    "sciadv": "open",
    "science": "paywalled",
    "sciimmunol": "paywalled",
    "scitranslmed": "paywalled",
    "scisignal": "paywalled",
    "scirobotics": "paywalled",
}


class SciencePaywallStub(Exception):
    """Raised when science.org returns a paywall stub instead of the article."""


def is_science_doi(doi: str) -> bool:
    if not doi.startswith(SCIENCE_DOI_PREFIX):
        return False
    suffix = doi.split("/", 1)[-1]
    slug = suffix.split(".", 1)[0]
    return slug in SCIENCE_SUBJOURNALS


def _article_url(doi: str) -> str:
    return f"https://www.science.org/doi/{doi}"


def fetch_manuscript_pdf(doi: str) -> bytes:
    """Download the Science-family PDF for a DOI.

    science.org PDFs sit at `…/doi/pdf/<doi>`. Same Cloudflare +
    subscription dance as Cell — needs the warm Playwright session.
    Raises when the response isn't a PDF (paywalled on this
    network).
    """
    url = f"https://www.science.org/doi/pdf/{doi}"
    with PlaywrightSession(SCIENCE_HOME) as session:
        return session.fetch_bytes(url)


def _detect_stub(soup: BeautifulSoup) -> bool:
    for h2 in soup.find_all("h2"):
        if "Access the full article" in h2.get_text(" ", strip=True):
            return True
    # If the page returned the AAAS homepage shell instead of the article,
    # there is no h1 title and no body sections — also a failure.
    if not soup.find("h1"):
        return True
    return False


_BODY_SECTION_TITLES = {
    "INTRODUCTION", "RESULTS", "DISCUSSION", "MATERIALS AND METHODS",
    "Introduction", "Results", "Discussion", "Materials and Methods",
    "Conclusion", "Conclusions",
}

_SKIP_SECTIONS = {
    "abstract",
    "acknowledgments",
    "acknowledgements",
    "supplementary materials",
    "references",
    "references and notes",
    "(0) eletters",
    "editor's summary",
    "editor’s summary",
    "we recommend",
    "information & authors",
    "metrics & citations",
    "figures",
    "tables",
    "multimedia",
    "share",
    "view options",
    "recommended articles from trendmd",
}


def _normalize_section_title(title: str) -> str:
    t = re.sub(r"\s*\(\d+\)\s*$", "", title)
    return re.sub(r"\s+", " ", t).strip().lower()


def _extract_metadata(soup: BeautifulSoup, doi: str) -> dict[str, Any]:
    def _meta(name: str) -> str:
        m = soup.find("meta", attrs={"name": name})
        return m.get("content", "") if m else ""

    def _meta_all(name: str) -> list[str]:
        return [
            m.get("content", "")
            for m in soup.find_all("meta", attrs={"name": name})
            if m.get("content")
        ]

    title = _meta("dc.Title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""

    authors = _meta_all("dc.Creator")
    if not authors:
        authors = [
            el.get_text(" ", strip=True)
            for el in soup.select(".hlFld-ContribAuthor")
            if el.get_text(strip=True)
        ]

    year = None
    date = _meta("dc.Date")
    m = re.match(r"(\d{4})", date)
    if m:
        year = int(m.group(1))

    abs_el = soup.find("section", attrs={"role": "doc-abstract"})
    abstract = ""
    if abs_el:
        text = abs_el.get_text(" ", strip=True)
        abstract = re.sub(r"^\s*Abstract\s*", "", text)

    suffix = doi.split("/", 1)[-1]
    slug = suffix.split(".", 1)[0]
    journal_map = {
        "sciadv": "Science Advances",
        "science": "Science",
        "sciimmunol": "Science Immunology",
        "scitranslmed": "Science Translational Medicine",
        "scisignal": "Science Signaling",
        "scirobotics": "Science Robotics",
    }
    journal = journal_map.get(slug, "Science")

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "journal": journal,
        "year": year,
    }


_FIG_LABEL_RE = re.compile(r"Fig\.?\s*(\d+)", re.IGNORECASE)


def _fetch_figure_with_retry(
    session: PlaywrightSession,
    url: str,
    *,
    attempts: int = 3,
    sleep_seconds: float = 2.0,
) -> bytes | None:
    for i in range(attempts):
        try:
            return session.fetch_bytes(url)
        except Exception as exc:
            if i == attempts - 1:
                print(f"      [fig] retry {i+1}/{attempts} failed: {exc}")
                return None
            time.sleep(sleep_seconds * (i + 1))
    return None


def _extract_figures(
    soup: BeautifulSoup,
    output_dir: Path,
    session: PlaywrightSession,
) -> list[dict[str, Any]]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    figures: list[dict[str, Any]] = []
    for fig_el in soup.find_all("figure", class_="graphic"):
        fid = fig_el.get("id", "")
        m = re.fullmatch(r"F(\d+)", fid)
        if not m:
            continue
        number = int(m.group(1))

        cap_el = fig_el.find("div", class_="caption")
        heading_el = cap_el.find("span", class_="heading") if cap_el else None
        label = heading_el.get_text(" ", strip=True) if heading_el else f"Fig. {number}"
        caption_text = cap_el.get_text(" ", strip=True) if cap_el else ""

        notes_el = fig_el.find("div", class_="notes")
        notes_text = notes_el.get_text(" ", strip=True) if notes_el else ""
        full_caption = f"{caption_text} {notes_text}".strip()

        img = fig_el.find("img")
        img_src = img.get("src", "") if img else ""
        full_url = urljoin(SCIENCE_HOME, img_src) if img_src else ""

        asset: str | None = None
        if full_url:
            ext = ".jpg" if full_url.lower().endswith(".jpg") else ".png"
            dest = figures_dir / f"figure_{number}{ext}"
            data = _fetch_figure_with_retry(session, full_url)
            if data is not None:
                dest.write_bytes(data)
                asset = str(dest.relative_to(output_dir))
                print(f"    [fig] Fig {number} → {dest.name} ({len(data)} bytes)")
            else:
                print(f"    [fig] Fig {number} download failed after retries")

        figures.append({
            "number": number,
            "label": label,
            "caption_text": full_caption,
            "asset": asset,
        })

    figures.sort(key=lambda f: f["number"])
    return figures


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    for sec in soup.find_all("section"):
        h2 = sec.find("h2", recursive=False)
        if not h2:
            continue
        title = h2.get_text(" ", strip=True)
        norm = _normalize_section_title(title)
        if norm in _SKIP_SECTIONS:
            continue
        if norm in seen_titles:
            continue
        seen_titles.add(norm)

        parts: list[str] = []
        for el in sec.find_all(["h3", "p", "div"]):
            if el.find_parent("figure"):
                continue
            if el.name == "div" and el.get("role") != "paragraph":
                continue
            txt = el.get_text(" ", strip=True)
            if not txt:
                continue
            if el.name == "h3":
                parts.append(f"### {txt}")
            else:
                parts.append(txt)

        text = "\n\n".join(parts)
        if text:
            sections.append({"title": title, "text": text})

    return sections


def extract(
    doi: str,
    output_dir: Path | None = None,
    session: PlaywrightSession | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a science.org article."""
    from datetime import datetime, timezone

    url = _article_url(doi)
    print(f"  [science] fetching {url}")

    owns_session = session is None
    if owns_session:
        session = PlaywrightSession(SCIENCE_HOME).__enter__()

    try:
        html = session.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        if _detect_stub(soup):
            raise SciencePaywallStub(
                f"science.org served a stub for {doi} (paywalled from this IP)"
            )

        meta = _extract_metadata(soup, doi)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        slug = doi.split("/", 1)[-1].lower().replace("/", "_")
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
            "extraction_method": "science_html+aaas_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "aaas_cdn" if fig["asset"] else "missing",
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
        section_texts = [f"## {sec['title']}\n\n{sec['text']}" for sec in sections]
        body_text = "\n\n".join(section_texts)
        pages_list = [
            {"page": i + 1, "text": sec["text"]}
            for i, sec in enumerate(sections)
        ]

    if output_dir:
        (output_dir / "extracted_text.txt").write_text(body_text, encoding="utf-8")

    authors = meta["authors"]

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage2_e11",
            "created_at": now,
            "updated_at": now,
            "mode": "full",
            "title": meta["title"],
        },
        "paper": {
            "id": slug,
            "title": meta["title"],
            "abstract": meta.get("abstract", ""),
            "authors": authors,
            "first_author": authors[0].split()[-1] if authors else "",
            "year": meta["year"],
            "journal": meta["journal"],
            "doi": doi,
            "pmid": None,
            "pmcid": None,
            "arxiv_id": None,
            "biorxiv_doi": None,
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
