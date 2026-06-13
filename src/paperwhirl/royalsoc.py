"""Royal Society family extractor via article HTML scraping.

Stage 6 E9 follow-on (2026-05-27). Royal Society Publishing
(Proc B, Phil Trans B, Royal Society Open Science, Open Biology,
Interface, etc.) runs on the Atypon platform behind Cloudflare.
The citation_xml_url meta tag points at a URL that actually
returns the article HTML (not JATS), so HTML-scrape via the
warmed PlaywrightSession — same template as Oxford / PNAS.

Public surface:
    is_royalsoc_doi(doi)            -> bool
    extract(doi, output_dir=None, session=None) -> dict
    fetch_manuscript_pdf(doi)       -> bytes
    RoyalSocPaywallStub             -> raised on stub pages
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from paperwhirl.browser import PlaywrightSession

ROYALSOC_HOME = "https://royalsocietypublishing.org/"
ROYALSOC_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"

# DOI: 10.1098/<journal-code>.<year>.<id> or 10.1098/<journal-code>.<id>.
# Journal codes include: rspb (Proc B), rstb (Phil Trans B), rsos
# (Open Science), rsob (Open Biology), rsif (Interface), rsbl
# (Biology Letters), rspa (Proc A), rsta (Phil Trans A), rsnr
# (Notes & Records). Match any letters + numeric id.
_ROYALSOC_DOI_RE = re.compile(r"^10\.1098/[a-z]+\.[0-9.]+$", re.IGNORECASE)

_SKIP_SECTIONS = {
    "abstract",
    "ethics",
    "data accessibility",
    "data availability",
    "declaration of ai use",
    "authors’ contributions",
    "authors' contributions",
    "author contributions",
    "conflict of interest declaration",
    "competing interests",
    "competing interest",
    "funding",
    "acknowledgements",
    "acknowledgments",
    "references",
}


class RoyalSocPaywallStub(Exception):
    """Raised when royalsocietypublishing.org serves a stub."""


def is_royalsoc_doi(doi: str) -> bool:
    return bool(_ROYALSOC_DOI_RE.match(doi or ""))


def _detect_stub(soup: BeautifulSoup) -> bool:
    for h2 in soup.find_all("h2"):
        # Royal Society numbers section titles two ways:
        # "2 Results and methods" (no period) and "2. Results"
        # (period); strip both forms before matching.
        t = re.sub(r"^\d+\.?\s+", "", h2.get_text(" ", strip=True)).strip().lower()
        if t in ("results", "results and methods", "discussion", "conclusions"):
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
    authors: list[str] = []
    for a in authors_raw:
        if "," in a:
            surname, given = a.split(",", 1)
            authors.append(f"{given.strip()} {surname.strip()}")
        else:
            authors.append(a.strip())

    journal = _meta("citation_journal_title") or "Royal Society Publishing"
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
            for p in abs_section.find_all(["p", "div"]):
                if p.name == "div" and p.get("role") != "paragraph":
                    continue
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


_FIG_NUM_RE = re.compile(r"Figure\s+(\d+)", re.IGNORECASE)


def _extract_figures(
    soup: BeautifulSoup,
    output_dir: Path,
    session: PlaywrightSession,
) -> list[dict[str, Any]]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    figures: list[dict[str, Any]] = []
    seen: set[int] = set()
    for fig_el in soup.find_all("div", class_=re.compile(r"\bfig\b")):
        # Find figure number from data-content-id, id attr, or
        # nested label text.
        number = None
        for cand in (fig_el.get("data-content-id"), fig_el.get("id")):
            if cand:
                m = re.search(r"[fF](?:ig)?[._-]?(\d+)$", cand)
                if m:
                    number = int(m.group(1))
                    break
        if number is None:
            for txt in fig_el.find_all(string=_FIG_NUM_RE):
                m = _FIG_NUM_RE.search(str(txt))
                if m:
                    number = int(m.group(1))
                    break
        if number is None or number in seen:
            continue
        seen.add(number)
        label = f"Figure {number}"

        cap_parts: list[str] = []
        for cap in fig_el.find_all(["p", "div"]):
            if cap.name == "div" and cap.get("role") != "paragraph":
                continue
            t = cap.get_text(" ", strip=True)
            if not t or _FIG_NUM_RE.fullmatch(t):
                continue
            cap_parts.append(t)
        caption_text = " ".join(cap_parts) if cap_parts else label

        img_url = ""
        img = fig_el.find("img")
        if img:
            img_url = img.get("src") or img.get("data-src") or ""
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            elif img_url.startswith("/"):
                img_url = "https://royalsocietypublishing.org" + img_url

        asset: str | None = None
        if img_url:
            # Plain requests works (signed CDN; like Oxford).
            data = None
            try:
                r = requests.get(
                    img_url,
                    headers={"User-Agent": ROYALSOC_UA},
                    timeout=30,
                )
                if r.status_code == 200:
                    data = r.content
            except requests.RequestException as exc:
                print(f"    [royalsoc-fig] F{number} fetch failed: {type(exc).__name__}: {exc}")
            # Fallback to in-page session if direct request didn't work
            if not data:
                try:
                    data = session.fetch_bytes(img_url)
                except Exception:
                    data = None
            if data and (data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG"):
                ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
                dest = figures_dir / f"figure_{number}{ext}"
                dest.write_bytes(data)
                asset = str(dest.relative_to(output_dir))
                print(f"    [royalsoc-fig] F{number} → {dest.name} ({len(data)} bytes)")

        figures.append({
            "number": number,
            "label": label,
            "caption_text": caption_text,
            "asset": asset,
        })

    figures.sort(key=lambda f: f["number"])
    return figures


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Royal Society lays out the article body as a flat sequence
    of h2/h3/p/div under one `<div class="widget-items">` — there
    is no `<section>` wrapper per heading. Walk sibling-by-sibling
    between adjacent h2 elements and gather paragraph-like content
    in between.

    First iteration of this walker called h2.find_parent('section')
    and got the same root container for every h2, so every section
    ended up with identical full-article text. Fixed by sibling
    traversal.
    """
    sections: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    article_body = (
        soup.find("div", class_="article-body")
        or soup.find("div", class_="widget-items")
    )
    if article_body is None:
        return sections

    # All h2s under the article body, in document order.
    h2s = article_body.find_all("h2")
    # Royal Society numbers sections ("1 Introduction"); strip
    # leading number for the skip-section check, keep original
    # in title.
    for idx, h2 in enumerate(h2s):
        title = h2.get_text(" ", strip=True)
        # Tolerate both "2 Results" and "2. Results" numbering.
        norm = re.sub(r"^\d+\.?\s+", "", title).strip().lower()
        if not norm or norm in _SKIP_SECTIONS or norm in seen_titles:
            continue
        seen_titles.add(norm)

        # Walk siblings of h2 until the next h2 (any level).
        parts: list[str] = []
        for sib in h2.next_siblings:
            if getattr(sib, "name", None) == "h2":
                break
            # Only element nodes have find/find_all; skip text
            # whitespace nodes.
            if getattr(sib, "find_all", None) is None:
                continue
            # If the sibling itself is a paragraph-like wrapper,
            # grab paragraphs from inside it.
            for child in sib.find_all(["h3", "h4", "p", "div"]) + ([sib] if sib.name in ("p", "div", "h3", "h4") else []):
                if child.find_parent(class_=re.compile(r"\bfig\b")):
                    continue
                if child.find_parent(class_=re.compile(r"table-wrap|table-frame")):
                    continue
                if child.name == "div" and child.get("role") != "paragraph":
                    continue
                if child.name in ("h3", "h4"):
                    t = child.get_text(" ", strip=True)
                    if t:
                        parts.append(f"### {t}")
                    continue
                t = child.get_text(" ", strip=True)
                if t:
                    parts.append(t)

        # De-dup adjacent identical chunks that can come from
        # capturing both the wrapping div and its inner <p>.
        deduped: list[str] = []
        for p in parts:
            if not deduped or deduped[-1] != p:
                deduped.append(p)

        text = "\n\n".join(deduped)
        if text:
            sections.append({"title": title, "text": text})
    return sections


def fetch_manuscript_pdf(doi: str) -> bytes:
    """Download the Royal Society PDF.

    Same two-stage shape as Oxford's fetcher (Stage 6 E7 follow-up,
    2026-05-27): `session.fetch_bytes` is an in-page JS fetch and
    chokes on the PDF response — empirically the Royal Society
    article-pdf endpoint produces a "TypeError: Failed to fetch"
    inside the in-page fetch even though the URL is same-origin
    (probably the Content-Disposition: attachment header or the
    Cloudflare-binary path). Use Playwright's context.request
    (browser HTTP stack, no CORS, sends cookies) with a plain
    requests fallback.
    """
    with PlaywrightSession(ROYALSOC_HOME) as session:
        session._warm()
        html = session.fetch_html(f"https://doi.org/{doi}")
        soup = BeautifulSoup(html, "lxml")
        meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        pdf_url = meta.get("content") if meta else ""
        if not pdf_url:
            raise ValueError(f"no citation_pdf_url for Royal Society DOI {doi}")
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url

        response_err = "not attempted"
        try:
            response = session._page.context.request.get(pdf_url)
        except Exception as exc:
            response = None
            response_err = f"{type(exc).__name__}: {exc}"
        if response is not None:
            if response.ok:
                body = response.body()
                ct = (response.headers.get("content-type") or "").lower()
                if "pdf" in ct or body[:4] == b"%PDF":
                    return body
                response_err = (
                    f"non-PDF response: status={response.status} "
                    f"content-type={ct!r}"
                )
            else:
                response_err = (
                    f"status={response.status} "
                    f"content-type={(response.headers.get('content-type') or '')!r}"
                )

    r = requests.get(
        pdf_url,
        headers={"User-Agent": ROYALSOC_UA},
        timeout=60,
        allow_redirects=True,
    )
    ct = (r.headers.get("content-type") or "").lower()
    if r.status_code == 200 and ("pdf" in ct or r.content[:4] == b"%PDF"):
        return r.content
    raise RuntimeError(
        f"Royal Society PDF fetch failed: context.request={response_err!r}, "
        f"requests=status={r.status_code} content-type={ct!r}"
    )


def extract(
    doi: str,
    output_dir: Path | None = None,
    session: PlaywrightSession | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from a royalsocietypublishing.org article."""
    from datetime import datetime, timezone

    print(f"  [royalsoc] fetching article for {doi}")

    owns_session = session is None
    if owns_session:
        session = PlaywrightSession(ROYALSOC_HOME).__enter__()

    try:
        html = session.fetch_html(f"https://doi.org/{doi}")
        soup = BeautifulSoup(html, "lxml")

        if _detect_stub(soup):
            raise RoyalSocPaywallStub(
                f"royalsocietypublishing.org served a stub for {doi}"
            )

        meta = _extract_metadata(soup, doi)
        slug = doi.split("/", 1)[-1].lower().replace(".", "_").replace("/", "_")
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
            "extraction_method": "royalsoc_html+royalsoc_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "royalsoc_cdn" if fig["asset"] else "missing",
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
