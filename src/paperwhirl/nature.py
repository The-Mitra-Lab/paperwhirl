"""Nature/Springer family extractor via article HTML scraping.

Covers papers published by Springer Nature (Nature, Nature journals,
Nature Communications, etc.) that are not yet in PMC.

Public surface:
  is_nature_doi(doi)      -> bool
  extract(doi)            -> dict [session skeleton]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

NATURE_DOI_PREFIXES = ("10.1038/",)
NATURE_ARTICLE_URL = "https://www.nature.com/articles"

_SKIP_SECTIONS = {
    "similar content being viewed by others",
    "author contributions",
    "data availability",
    "code availability",
    "competing interests",
    "additional information",
    "supplementary information",
    "extended data",
    "extended data figures and tables",
    "rights and permissions",
    "about this article",
    "acknowledgements",
    "peer review information",
    "reprints and permissions",
    "ethics declarations",
    "reporting summary",
    "source data",
}


def is_nature_doi(doi: str) -> bool:
    return any(doi.startswith(p) for p in NATURE_DOI_PREFIXES)


def _nature_article_slug(doi: str) -> str:
    """Map a Nature/NPG DOI to its nature.com/articles/<slug> slug.

    Stage 5 E7 S10 (2026-05-19): two DOI formats coexist.
    - Modern: `10.1038/s41586-024-07487-w` → slug is the whole
      suffix `s41586-024-07487-w`.
    - Legacy NPG: `10.1038/sj.<journal>.<pii>` (e.g.
      `10.1038/sj.cdd.4401778`) → nature.com serves these at
      `/articles/<pii>` (`/articles/4401778`), NOT at
      `/articles/sj.cdd.4401778` (which 302-redirects to a
      broken IDP transit page). The pii is the last
      dot-separated segment of the suffix.

    Surfaced on PMID 16397584 / `10.1038/sj.cdd.4401778`
    (Cell Death & Differentiation review) which has no PMC
    deposit, so the Nature path is the only structured source.
    """
    suffix = doi.split("/", 1)[-1]
    if suffix.startswith("sj."):
        return suffix.rsplit(".", 1)[-1]
    return suffix


def _fetch_article_html(doi: str) -> BeautifulSoup:
    url = f"{NATURE_ARTICLE_URL}/{_nature_article_slug(doi)}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def _extract_metadata(soup: BeautifulSoup, doi: str) -> dict[str, Any]:
    title_el = soup.find("h1", class_="c-article-title")
    title = title_el.get_text(strip=True) if title_el else ""

    author_els = soup.find_all("a", attrs={"data-test": "author-name"})
    authors = [a.get_text(strip=True) for a in author_els]

    abstract_el = soup.find("div", attrs={"id": "Abs1-content"})
    abstract = abstract_el.get_text(strip=True) if abstract_el else ""

    journal_el = soup.find("i", attrs={"data-test": "journal-title"})
    journal = journal_el.get_text(strip=True) if journal_el else "Nature"

    # Year from <time datetime="YYYY-MM-DD"> or citation_publication_date meta.
    year: int | None = None
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        m = re.match(r"(\d{4})", time_el.get("datetime", ""))
        if m:
            year = int(m.group(1))
    if year is None:
        meta_el = soup.find("meta", attrs={"name": "citation_publication_date"})
        if meta_el:
            m = re.match(r"(\d{4})", meta_el.get("content", ""))
            if m:
                year = int(m.group(1))

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "journal": journal,
        "year": year,
    }


def _fetch_with_retry(
    url: str,
    *,
    attempts: int = 3,
    sleep_seconds: float = 2.0,
    timeout: int = 30,
) -> bytes | None:
    """Springer CDN occasionally times out under load; retry with back-off.

    Mirrors cell.py's _fetch_figure_with_retry. Returns None after all
    attempts fail; caller logs and continues.
    """
    import time
    for i in range(attempts):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if i == attempts - 1:
                print(f"      [fig] retry {i+1}/{attempts} failed: {exc}")
                return None
            time.sleep(sleep_seconds * (i + 1))
    return None


def _extract_figures(
    soup: BeautifulSoup, doi: str, output_dir: Path,
) -> list[dict[str, Any]]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    figures = []
    for fig_div in soup.find_all("div", class_="c-article-section__figure"):
        title_el = fig_div.find(attrs={"data-test": "figure-caption-text"})
        if not title_el:
            continue

        title_text = title_el.get_text(" ", strip=True)
        label_match = re.match(r"(Fig\.\s*\d+)", title_text)
        if not label_match:
            continue
        label = label_match.group(1)
        num_match = re.search(r"(\d+)", label)
        number = int(num_match.group(1)) if num_match else 0

        bottom_el = fig_div.find(attrs={"data-test": "bottom-caption"})
        bottom_text = bottom_el.get_text(" ", strip=True) if bottom_el else ""
        caption_text = f"{title_text} {bottom_text}".strip()

        img_el = fig_div.find("img", src=re.compile(r"springernature"))
        if not img_el:
            continue

        src = img_el.get("src", "")
        full_url = src.replace("lw685", "full")
        if full_url.startswith("//"):
            full_url = "https:" + full_url

        dest = figures_dir / f"figure_{number}.png"
        asset = None
        data = _fetch_with_retry(full_url)
        if data is not None:
            dest.write_bytes(data)
            asset = str(dest.relative_to(output_dir))
            print(f"    [fig] Fig {number} → {dest.name} ({len(data)} bytes)")
        else:
            print(f"    [fig] Fig {number} download failed after retries")

        figures.append({
            "number": number,
            "label": label,
            "caption_text": caption_text,
            "asset": asset,
        })

    return sorted(figures, key=lambda f: f["number"])


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    body = soup.find("div", class_="c-article-body")
    if not body:
        return []

    sections = []
    current_title = ""
    current_paragraphs: list[str] = []

    for el in body.find_all(["h2", "h3", "p"]):
        if el.name in ("h2", "h3"):
            if current_title or current_paragraphs:
                if current_title.lower() not in _SKIP_SECTIONS:
                    text = "\n\n".join(p for p in current_paragraphs if p)
                    if current_title or text:
                        sections.append({"title": current_title, "text": text})
                current_paragraphs = []
            current_title = el.get_text(strip=True)
        elif el.name == "p":
            parent_fig = el.find_parent("figure")
            if parent_fig:
                continue
            text = el.get_text(" ", strip=True)
            if text:
                current_paragraphs.append(text)

    if current_title.lower() not in _SKIP_SECTIONS:
        text = "\n\n".join(p for p in current_paragraphs if p)
        if current_title or text:
            sections.append({"title": current_title, "text": text})

    return sections


def fetch_manuscript_pdf(doi: str) -> bytes:
    """Download Nature/Springer's PDF for an article.

    Nature serves PDFs at `nature.com/articles/<id>.pdf`. Whether
    the response is the actual PDF or a paywall landing depends on
    the running machine's subscription IP — works fully from the
    primary user's WashU laptop under the laptop-first deployment
    model.
    """
    import requests
    url = f"https://www.nature.com/articles/{_nature_article_slug(doi)}.pdf"
    r = requests.get(url, timeout=60, allow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 PaperWhirl/0.1"})
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if not ctype.startswith("application/pdf"):
        raise RuntimeError(
            f"Nature returned non-PDF for {doi} (likely paywall on this network): {ctype}"
        )
    return r.content


def extract(
    doi: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a Nature/Springer article page."""
    from datetime import datetime, timezone

    print(f"  [nature] fetching article page for {doi}")
    soup = _fetch_article_html(doi)

    meta = _extract_metadata(soup, doi)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    slug = doi.split("/", 1)[-1].lower().replace("/", "_")
    out = output_dir or Path("/tmp") / slug
    out.mkdir(parents=True, exist_ok=True)

    figures = _extract_figures(soup, doi, out)
    sections = _extract_sections(soup)

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
            "extraction_method": "nature_html+springer_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "springer_cdn" if fig["asset"] else "missing",
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
        section_texts = []
        for sec in sections:
            if sec["title"]:
                section_texts.append(f"## {sec['title']}\n\n{sec['text']}")
            else:
                section_texts.append(sec["text"])
        body_text = "\n\n".join(section_texts)
        pages_list = [
            {"page": i + 1, "text": sec["text"]}
            for i, sec in enumerate(sections)
        ]

    if output_dir:
        (output_dir / "extracted_text.txt").write_text(body_text, encoding="utf-8")

    title = meta["title"]
    authors = meta["authors"]

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage2_e10",
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
            "year": meta.get("year"),
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
