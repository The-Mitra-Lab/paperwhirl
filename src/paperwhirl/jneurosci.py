"""JNeurosci family extractor via article HTML scraping.

Stage 6 E9 follow-on (2026-05-27). Journal of Neuroscience
(Society for Neuroscience) is on the Highwire platform behind
Cloudflare — same shape as bioRxiv but HTML-only (no JATS XML
exposed publicly). Scrape the article HTML through a warmed
PlaywrightSession; metadata via citation_* meta tags; figures
via `<div class="fig" id="F<N>">` containers with `.large.jpg`
hrefs same-origin on jneurosci.org.

Most JNeurosci papers eventually index in PMC. This extractor
handles the new-paper gap.

Public surface:
    is_jneurosci_doi(doi)          -> bool
    extract(doi, output_dir=None, session=None) -> dict
    fetch_manuscript_pdf(doi)      -> bytes
    JNeurosciPaywallStub           -> raised on stub pages
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from paperwhirl.browser import PlaywrightSession

JNS_HOME = "https://www.jneurosci.org/"

# DOI: 10.1523/JNEUROSCI.<article-code>.<year>. The JNEUROSCI
# segment is case-insensitive in practice.
_JNS_DOI_RE = re.compile(r"^10\.1523/JNEUROSCI\.[0-9A-Z.-]+$", re.IGNORECASE)

# Section titles to drop. Lower-cased for comparison.
_SKIP_SECTIONS = {
    "abstract",
    "significance statement",  # JNeurosci-specific short summary
    "data availability",
    "code availability",
    "acknowledgments",
    "acknowledgements",
    "author contributions",
    "footnotes",
    "references",
}


class JNeurosciPaywallStub(Exception):
    """Raised when jneurosci.org serves a stub (paywalled / pre-pub)."""


def is_jneurosci_doi(doi: str) -> bool:
    return bool(_JNS_DOI_RE.match(doi or ""))


def _detect_stub(soup: BeautifulSoup) -> bool:
    # Full-text articles have a Results / Discussion h2 inside a
    # `section.section-content`; the stub page lacks both.
    for h2 in soup.find_all("h2"):
        t = h2.get_text(" ", strip=True).strip().lower()
        if t in ("results", "discussion"):
            return False
    return True


def _extract_metadata(soup: BeautifulSoup, doi: str) -> dict[str, Any]:
    def _meta_all(name: str) -> list[str]:
        return [m.get("content", "") for m in soup.find_all("meta", attrs={"name": name}) if m.get("content")]

    def _meta(name: str) -> str:
        vals = _meta_all(name)
        return vals[0] if vals else ""

    title = _meta("citation_title")
    authors_raw = _meta_all("citation_author")
    # JNeurosci emits authors as either "Surname, Given" or
    # "Given Surname" depending on the renderer; normalize by
    # flipping any with a comma.
    authors: list[str] = []
    for a in authors_raw:
        if "," in a:
            surname, given = a.split(",", 1)
            authors.append(f"{given.strip()} {surname.strip()}")
        else:
            authors.append(a.strip())

    journal = _meta("citation_journal_title") or "Journal of Neuroscience"
    pmid = _meta("citation_pmid") or None

    year = None
    date = _meta("citation_publication_date") or _meta("citation_online_date")
    m = re.match(r"(\d{4})", date or "")
    if m:
        year = int(m.group(1))

    abstract = ""
    abs_h2 = soup.find("h2", string=re.compile(r"^\s*Abstract\s*$", re.IGNORECASE))
    if abs_h2 is not None:
        abs_section = abs_h2.find_parent("section") or abs_h2.parent
        if abs_section is not None:
            parts = []
            for p in abs_section.find_all(["p"]):
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
        "pmid": pmid,
        "year": year,
    }


_FIG_ID_RE = re.compile(r"^F(\d+)$")


def _extract_figures(
    soup: BeautifulSoup,
    output_dir: Path,
    session: PlaywrightSession,
) -> list[dict[str, Any]]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    figures: list[dict[str, Any]] = []
    for fig_el in soup.find_all("div", class_="fig"):
        fid = fig_el.get("id", "")
        m = _FIG_ID_RE.match(fid)
        if not m:
            continue
        number = int(m.group(1))
        label = f"Figure {number}"

        cap_el = fig_el.find(class_=re.compile(r"fig-caption", re.I))
        caption_text = cap_el.get_text(" ", strip=True) if cap_el else label

        # Find the `.large.jpg` href; multiple anchors point at variants
        # of the same image (download / carousel / direct). Strip query
        # strings before fetching to dedupe.
        img_url = ""
        for a in fig_el.find_all("a", href=True):
            href = a["href"]
            if ".large.jpg" in href.lower():
                img_url = href.split("?", 1)[0]
                break
        if not img_url:
            img = fig_el.find("img")
            if img:
                src = img.get("data-src") or img.get("src") or ""
                if src and not src.startswith("data:"):
                    img_url = src

        asset: str | None = None
        if img_url:
            try:
                data = session.fetch_bytes(img_url)
            except Exception as exc:
                print(f"    [jneurosci-fig] F{number} fetch failed: {type(exc).__name__}: {exc}")
                data = None
            if data and (data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG"):
                ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
                dest = figures_dir / f"figure_{number}{ext}"
                dest.write_bytes(data)
                asset = str(dest.relative_to(output_dir))
                print(f"    [jneurosci-fig] F{number} → {dest.name} ({len(data)} bytes)")

        figures.append({
            "number": number,
            "label": label,
            "caption_text": caption_text,
            "asset": asset,
        })

    figures.sort(key=lambda f: f["number"])
    return figures


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Walk Highwire `<div class="section">` blocks under each h2.
    JNeurosci uses divs styled as sections, not the HTML5 <section>
    element. The exact class list is `"section"` with an optional
    sub-type ("abstract", "results", "discussion", "materials-
    methods", "fn-group", "ref-list", etc.). Filter by `section`
    being IN the class list and the h2 title not being a non-body
    section.
    """
    sections: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for sec in soup.find_all("div", class_=lambda c: c and "section" in c):
        h2 = sec.find("h2", recursive=False) or sec.find("h2")
        if not h2:
            continue
        title = h2.get_text(" ", strip=True)
        norm = re.sub(r"\s+", " ", title).strip().lower()
        if not norm or norm in _SKIP_SECTIONS or norm in seen_titles:
            continue
        seen_titles.add(norm)

        parts: list[str] = []
        for child in sec.find_all(["h3", "h4", "p"]):
            if child.find_parent("figure"):
                continue
            if child.find_parent(class_="fig"):
                continue
            if child.find_parent(class_=re.compile(r"table-wrap|table-frame")):
                continue
            t = child.get_text(" ", strip=True)
            if not t:
                continue
            if child.name in ("h3", "h4"):
                parts.append(f"### {t}")
            else:
                parts.append(t)

        text = "\n\n".join(parts)
        if text:
            sections.append({"title": title, "text": text})
    return sections


def fetch_manuscript_pdf(doi: str) -> bytes:
    """Download the JNeurosci PDF via citation_pdf_url meta resolution."""
    with PlaywrightSession(JNS_HOME) as session:
        session._warm()
        html = session.fetch_html(f"https://doi.org/{doi}")
        soup = BeautifulSoup(html, "lxml")
        meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        pdf_url = meta.get("content") if meta else ""
        if not pdf_url:
            raise ValueError(f"no citation_pdf_url for JNeurosci DOI {doi}")
        return session.fetch_bytes(pdf_url)


def extract(
    doi: str,
    output_dir: Path | None = None,
    session: PlaywrightSession | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a jneurosci.org article."""
    from datetime import datetime, timezone

    print(f"  [jneurosci] fetching article for {doi}")

    owns_session = session is None
    if owns_session:
        session = PlaywrightSession(JNS_HOME).__enter__()

    try:
        # The doi.org redirect lands on /lookup/doi/{doi} which then
        # 301s to the canonical /content/<vol>/<issue>/<id> URL; the
        # Playwright session follows both redirects automatically.
        html = session.fetch_html(f"https://doi.org/{doi}")
        soup = BeautifulSoup(html, "lxml")

        if _detect_stub(soup):
            raise JNeurosciPaywallStub(
                f"jneurosci.org served a stub for {doi} (paywalled or pre-publication)"
            )

        meta = _extract_metadata(soup, doi)
        slug = doi.split("/", 1)[-1].lower().replace(".", "_").replace("-", "_")
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
            "extraction_method": "jneurosci_html+highwire_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "highwire_cdn" if fig["asset"] else "missing",
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
            "pmid": meta["pmid"],
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
