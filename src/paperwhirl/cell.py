"""Cell-family (Elsevier) extractor via article HTML scraping.

Covers OA papers on cell.com (Cell, Molecular Cell, Cell Reports,
Cell Stem Cell, Immunity, Neuron, etc.). On the cluster, paywalled
papers redirect to a journal landing page; this extractor detects
that and raises CellPaywallStub so the resolver can fall through.
On the user's laptop (subscribing IP), the same code path renders
full content for paywalled papers identically to OA ones.

Public surface:
  is_cell_doi(doi)   -> bool
  extract(doi, ...)  -> dict [session skeleton]
  CellPaywallStub    -> exception raised when the page is a stub
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from paperwhirl.browser import PlaywrightSession

CELL_HOME = "https://www.cell.com/"
CROSSREF_WORKS = "https://api.crossref.org/works"
CROSSREF_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"

# Elsevier journal-slug whitelist on the cell.com platform. Unknown
# Elsevier DOIs fall through to PDF fallback.
CELL_JOURNAL_SLUGS = {
    "j.cell": "cell",
    "j.molcel": "molecular-cell",
    "j.celrep": "cell-reports",
    "j.celrep.med": "cell-reports-medicine",
    "j.celrep.methods": "cell-reports-methods",
    "j.celrep.physsci": "cell-reports-physical-science",
    "j.stem": "cell-stem-cell",
    "j.immuni": "immunity",
    "j.neuron": "neuron",
    "j.cmet": "cell-metabolism",
    "j.chom": "cell-host-microbe",
    "j.cels": "cell-systems",
    "j.devcel": "developmental-cell",
    "j.cub": "current-biology",
    "j.str": "structure",
    "j.chembiol": "chembiol",
    "j.ajhg": "ajhg",
    "j.isci": "iscience",
}

# Body sections to skip when assembling extracted text. Compared as
# lower-cased, stripped of decorative characters like the ★ in
# "STAR★Methods".
_SKIP_SECTIONS = {
    "highlights",
    "graphical abstract",
    "keywords",
    "acknowledgments",
    "acknowledgements",
    "author contributions",
    "declaration of interests",
    "declaration of competing interests",
    "data and code availability",
    "data availability",
    "supplemental information",
    "supplementary information",
    "references",
    "consortia",
    "star methods",
}


def _normalize_section_title(title: str) -> str:
    """Lowercase + decorative chars to spaces + collapse whitespace + drop trailing count."""
    t = re.sub(r"[★*†‡§]", " ", title)
    t = re.sub(r"\s*\(\d+\)\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()


class CellPaywallStub(Exception):
    """Raised when cell.com returns a journal landing instead of the article."""


def is_cell_doi(doi: str) -> bool:
    if not doi.startswith("10.1016/"):
        return False
    suffix = doi.split("/", 1)[-1]
    # Match the longest slug prefix in the whitelist; e.g.,
    # "j.celrep.med.2024.01.001" should match "j.celrep.med", not "j.celrep".
    for slug in sorted(CELL_JOURNAL_SLUGS, key=len, reverse=True):
        if suffix.startswith(slug + "."):
            return True
    return False


def _journal_slug_for_doi(doi: str) -> str | None:
    suffix = doi.split("/", 1)[-1]
    for slug in sorted(CELL_JOURNAL_SLUGS, key=len, reverse=True):
        if suffix.startswith(slug + "."):
            return CELL_JOURNAL_SLUGS[slug]
    return None


def _pii_to_dashed(pii: str) -> str:
    """Convert PII like S0092867425004167 to S0092-8674(25)00416-7."""
    pii = pii.strip().upper()
    m = re.fullmatch(r"S(\d{4})(\d{4})(\d{2})(\d{5})(\d|X)", pii)
    if not m:
        return pii
    a, b, c, d, e = m.groups()
    return f"S{a}-{b}({c}){d}-{e}"


def _resolve_pii(doi: str) -> str | None:
    """Look up the Elsevier PII via Crossref alternative-id."""
    try:
        r = requests.get(
            f"{CROSSREF_WORKS}/{doi}",
            headers={"User-Agent": CROSSREF_UA},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  [crossref] PII lookup failed: {exc}")
        return None
    msg = r.json().get("message", {})
    for alt in msg.get("alternative-id", []) or []:
        if re.fullmatch(r"S\d{15}[\dX]", alt or "", flags=re.IGNORECASE):
            return alt
    return None


def _article_url(doi: str, pii: str) -> str:
    journal = _journal_slug_for_doi(doi) or "cell"
    return f"https://www.cell.com/{journal}/fulltext/{_pii_to_dashed(pii)}"


def _detect_stub(soup: BeautifulSoup) -> bool:
    title = soup.title.get_text(strip=True) if soup.title else ""
    # Landing-page signature on the cluster.
    if re.match(r"^Cell Press:\s", title):
        return True
    # No real article body.
    if not soup.find("section", id="bodymatter"):
        return True
    return False


def _extract_metadata(soup: BeautifulSoup, doi: str) -> dict[str, Any]:
    # Citation meta tags are the most reliable signal.
    def _meta_all(name: str) -> list[str]:
        return [m.get("content", "") for m in soup.find_all("meta", attrs={"name": name}) if m.get("content")]

    def _meta(name: str) -> str:
        vals = _meta_all(name)
        return vals[0] if vals else ""

    title = _meta("citation_title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

    authors = _meta_all("citation_author")
    journal = _meta("citation_journal_title") or "Cell"
    pmid = _meta("citation_pmid") or None

    year = None
    date = _meta("citation_date") or _meta("citation_online_date")
    m = re.match(r"(\d{4})", date or "")
    if m:
        year = int(m.group(1))

    abstract = ""
    abs_el = soup.find("section", id="author-abstract")
    if abs_el:
        # The "Summary" header is the abstract label — strip it.
        text = abs_el.get_text(" ", strip=True)
        text = re.sub(r"^\s*Summary\s*", "", text)
        abstract = text

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "journal": journal,
        "pmid": pmid,
        "year": year,
    }


_FIG_ID_RE = re.compile(r"fig(\d+)$")


def _fetch_figure_with_retry(
    session: PlaywrightSession,
    url: str,
    *,
    attempts: int = 5,
    sleep_seconds: float = 2.0,
) -> bytes | None:
    """Cell's CDN throws 403s on bursty in-page fetches; back off and retry.

    Exponential back-off (2s, 4s, 8s, 16s) gives the Elsevier rate-limit
    window time to clear. The prior 3-attempt linear-back-off setup
    (2s, 4s, 6s) was still dropping occasional figures.
    """
    import time
    for i in range(attempts):
        try:
            return session.fetch_bytes(url)
        except Exception as exc:
            if i == attempts - 1:
                print(f"      [fig] retry {i+1}/{attempts} failed: {exc}")
                return None
            wait = sleep_seconds * (2 ** i)
            print(f"      [fig] retry {i+1}/{attempts} ({exc}); waiting {wait:.0f}s")
            time.sleep(wait)
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
        m = _FIG_ID_RE.fullmatch(fid)
        if not m:
            # Skips: undfig1 (unnumbered/graphical abstract), figs1+ (supplementary).
            continue
        number = int(m.group(1))

        label_el = fig_el.find("span", class_="figure__label")
        label = label_el.get_text(strip=True) if label_el else f"Figure {number}"

        title_text_el = fig_el.find("span", class_="figure__title__text")
        title_text = title_text_el.get_text(" ", strip=True) if title_text_el else ""

        body_el = fig_el.find("div", class_="figure__caption__text__content")
        body_text = body_el.get_text(" ", strip=True) if body_el else ""

        caption_text = f"{label}. {title_text} {body_text}".strip(". ").strip()

        a_full = fig_el.find("a", class_="icon-full-screen")
        full_href = a_full.get("href", "") if a_full else ""
        if full_href:
            full_url = urljoin(CELL_HOME, full_href)
        else:
            img = fig_el.find("img")
            full_url = urljoin(CELL_HOME, img.get("src", "")) if img else ""

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
            "caption_text": caption_text,
            "asset": asset,
        })

    figures.sort(key=lambda f: f["number"])
    return figures


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    body = soup.find("section", id="bodymatter")
    if not body:
        return []

    # Top-level body sections are <section> elements that hold a direct h2
    # child (Introduction, Results, Discussion, ...). Sub-sections nest
    # below and contain h3s instead.
    sections: list[dict[str, str]] = []
    for sec in body.find_all("section"):
        h2 = sec.find("h2", recursive=False)
        if not h2:
            continue
        title = h2.get_text(" ", strip=True)
        if _normalize_section_title(title) in _SKIP_SECTIONS:
            continue

        parts: list[str] = []
        for el in sec.find_all(["h3", "div"]):
            if el.name == "h3":
                txt = el.get_text(" ", strip=True)
                if txt:
                    parts.append(f"### {txt}")
                continue
            if el.get("role") != "paragraph":
                continue
            if el.find_parent("figure"):
                continue
            txt = el.get_text(" ", strip=True)
            if txt:
                parts.append(txt)

        text = "\n\n".join(parts)
        if text:
            sections.append({"title": title, "text": text})

    return sections


def fetch_manuscript_pdf(doi: str) -> bytes:
    """Download the Cell-family PDF for a DOI.

    cell.com PDFs sit at `…/fulltext/<PII>` paged through a "PDF"
    download endpoint at `…/pdf/<PII>.pdf`. Need the same Playwright
    session we use for HTML extraction so Cloudflare clears and the
    subscription cookie (when present from the running machine's IP)
    accompanies the request. Returns the PDF bytes; raises if the
    response isn't a PDF (paywalled on this network).
    """
    pii = _resolve_pii(doi)
    if not pii:
        raise ValueError(f"could not resolve Elsevier PII for DOI {doi}")
    journal = _journal_slug_for_doi(doi) or "cell"
    url = f"https://www.cell.com/{journal}/pdf/{_pii_to_dashed(pii)}.pdf"
    with PlaywrightSession(CELL_HOME) as session:
        return session.fetch_bytes(url)


def extract(
    doi: str,
    output_dir: Path | None = None,
    session: PlaywrightSession | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a cell.com article page."""
    from datetime import datetime, timezone

    from paperwhirl import stage

    with stage("cell.resolve_pii"):
        pii = _resolve_pii(doi)
    if not pii:
        raise ValueError(f"could not resolve Elsevier PII for DOI {doi}")
    url = _article_url(doi, pii)
    print(f"  [cell] fetching {url}")

    owns_session = session is None
    if owns_session:
        with stage("cell.playwright_warmup"):
            session = PlaywrightSession(CELL_HOME).__enter__()

    try:
        with stage("cell.fetch_html"):
            html = session.fetch_html(url)
        with stage("cell.parse"):
            soup = BeautifulSoup(html, "lxml")

            if _detect_stub(soup):
                raise CellPaywallStub(
                    f"cell.com served a stub for {doi} (paywalled from this IP)"
                )

            meta = _extract_metadata(soup, doi)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        slug = doi.split("/", 1)[-1].lower().replace("/", "_")
        out = output_dir or Path("/tmp") / slug
        out.mkdir(parents=True, exist_ok=True)

        with stage("cell.fetch_figures"):
            figures = _extract_figures(soup, out, session)
        with stage("cell.parse_sections"):
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
            "extraction_method": "cell_html+elsevier_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "elsevier_cdn" if fig["asset"] else "missing",
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
            section_texts.append(f"## {sec['title']}\n\n{sec['text']}")
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
            "pmid": meta["pmid"],
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
