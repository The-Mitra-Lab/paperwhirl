"""Oxford Academic family extractor via article HTML scraping.

Stage 6 E9 Step 4 (2026-05-27). Oxford Academic journals (Brain,
NAR, Bioinformatics, MBE, Human Molecular Genetics, etc.) ride
the Silverchair platform behind Cloudflare. Plain requests gets
403 + the XML endpoint is subscription-gated, so we scrape the
HTML page through a Playwright session that warms Cloudflare —
same shape as cell.py / nature.py / science.py / pnas.py.

DOI shape: 10.1093/<journal-code>/<article-id>. Oxford also
mints DOIs for books at 10.1093/<numeric-isbn>.<chapter>.<id>;
those are NOT in scope here. The detector rules them out by
requiring the second path segment to start with letters (book
DOIs start with a digit there).

Public surface:
    is_oxford_doi(doi)             -> bool
    extract(doi, output_dir=None, session=None) -> dict
    fetch_manuscript_pdf(doi)      -> bytes  (Download button)
    OxfordPaywallStub              -> raised on stub pages
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from paperwhirl.browser import PlaywrightSession

OXFORD_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"

OXFORD_HOME = "https://academic.oup.com/"

# DOI: 10.1093/<journal-code>/<article-id>. Journal codes are
# letters (brain, nar, bioinformatics, mbe, hmg, etc.); the
# article ID is alphanumeric. Excludes book DOIs that start
# with a numeric ISBN (10.1093/9780...).
_OXFORD_DOI_RE = re.compile(r"^10\.1093/[a-z][a-z0-9]*/[a-z0-9._-]+$", re.IGNORECASE)

# Section titles to drop from the body.
_SKIP_SECTIONS = {
    "abstract",
    "data availability",
    "data and code availability",
    "code availability",
    "supplementary data",
    "supplementary material",
    "supplementary materials",
    "acknowledgements",
    "acknowledgments",
    "author contributions",
    "competing interests",
    "competing interest",
    "conflict of interest statement",
    "funding",
    "references",
}


class OxfordPaywallStub(Exception):
    """Raised when academic.oup.com serves a stub (paywalled / pre-pub)."""


def is_oxford_doi(doi: str) -> bool:
    return bool(_OXFORD_DOI_RE.match(doi or ""))


def _detect_stub(soup: BeautifulSoup) -> bool:
    # No real body if Results/Discussion are not present as section
    # headings. For review articles those headings may not exist;
    # accept any h2-headed section with substantial body content.
    h2s = soup.find_all("h2", class_=re.compile(r"section-title", re.I))
    body_h2s = [
        h for h in h2s
        if h.get_text(" ", strip=True).strip().lower() not in {"abstract", "graphical abstract"}
    ]
    return len(body_h2s) == 0


def _extract_metadata(soup: BeautifulSoup, doi: str) -> dict[str, Any]:
    def _meta_all(name: str) -> list[str]:
        return [m.get("content", "") for m in soup.find_all("meta", attrs={"name": name}) if m.get("content")]

    def _meta(name: str) -> str:
        vals = _meta_all(name)
        return vals[0] if vals else ""

    title = _meta("citation_title")
    authors_raw = _meta_all("citation_author")
    # Oxford emits citation_author as "Surname, Given" — flip to
    # "Given Surname" to match the rest of the schema.
    authors: list[str] = []
    for a in authors_raw:
        if "," in a:
            surname, given = a.split(",", 1)
            authors.append(f"{given.strip()} {surname.strip()}")
        else:
            authors.append(a.strip())

    journal = _meta("citation_journal_title") or "Oxford Academic"
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
    for fig_el in soup.find_all("div", class_=re.compile(r"\bfig-section\b", re.I)):
        # Figure number lives in a label-like element ("Figure 1"); the
        # data-content-id ("awaf082-f1") also encodes it after the f.
        label_text = ""
        for cand in fig_el.find_all(string=_FIG_NUM_RE):
            label_text = str(cand).strip()
            break
        if not label_text:
            cid = fig_el.get("data-content-id", "")
            m = re.search(r"f(\d+)$", cid, re.IGNORECASE)
            if m:
                label_text = f"Figure {int(m.group(1))}"
        m = _FIG_NUM_RE.search(label_text)
        if not m:
            continue
        number = int(m.group(1))
        if number in seen:
            continue
        seen.add(number)
        label = f"Figure {number}"

        # Caption: the figure block contains a description paragraph
        # right after the label. Pull paragraphs and div role=paragraph.
        cap_parts: list[str] = []
        for cap in fig_el.find_all(["p", "div"]):
            if cap.find_parent("img") is not None:
                continue
            if cap.name == "div" and cap.get("role") != "paragraph":
                continue
            t = cap.get_text(" ", strip=True)
            if not t:
                continue
            # Skip the literal label "Figure N" line.
            if _FIG_NUM_RE.fullmatch(t):
                continue
            cap_parts.append(t)
        caption_text = " ".join(cap_parts) if cap_parts else label

        img_url = ""
        img = fig_el.find("img")
        if img:
            img_url = img.get("src") or img.get("data-src") or ""
            if img_url.startswith("//"):
                img_url = "https:" + img_url

        asset: str | None = None
        if img_url:
            # The Silverchair CDN serves these via a signed URL that's
            # public for anyone — but the article page is on
            # academic.oup.com, so an in-page browser fetch trips CORS.
            # Plain requests works (signature carries the auth).
            data = None
            try:
                r = requests.get(
                    img_url,
                    headers={"User-Agent": OXFORD_UA},
                    timeout=30,
                )
                if r.status_code == 200:
                    data = r.content
                else:
                    print(f"    [oxford-fig] F{number} HTTP {r.status_code}")
            except requests.RequestException as exc:
                print(f"    [oxford-fig] F{number} fetch failed: {type(exc).__name__}: {exc}")
            if data and (data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG"):
                ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
                dest = figures_dir / f"figure_{number}{ext}"
                dest.write_bytes(data)
                asset = str(dest.relative_to(output_dir))
                print(f"    [oxford-fig] F{number} → {dest.name} ({len(data)} bytes)")

        figures.append({
            "number": number,
            "label": label,
            "caption_text": caption_text,
            "asset": asset,
        })

    figures.sort(key=lambda f: f["number"])
    return figures


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for h2 in soup.find_all("h2", class_=re.compile(r"section-title", re.I)):
        title = h2.get_text(" ", strip=True)
        if not title:
            continue
        norm = re.sub(r"\s+", " ", title).strip().lower()
        if norm in _SKIP_SECTIONS:
            continue

        sec = h2.find_parent("section") or h2.parent
        if sec is None:
            continue

        parts: list[str] = []
        for child in sec.find_all(["h3", "h4", "p", "div"]):
            if child.find_parent(class_=re.compile(r"\bfig-section\b", re.I)):
                continue
            if child.find_parent(class_=re.compile(r"table-wrap|table-frame", re.I)):
                continue
            if child.name in ("h3", "h4"):
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
    """Download the Oxford article PDF via the citation_pdf_url path.

    Three-stage with two CORS-aware fetches:
    1. Read citation_pdf_url from the article HTML
       (Playwright session, clears Cloudflare).
    2. Try the same Playwright session's `context.request.get(url)`
       — that's the browser's HTTP stack, not an in-page fetch, so
       it sends the Cloudflare + subscription cookies AND is not
       subject to CORS. The figure-asset bug taught us
       `session.fetch_bytes` (in-page fetch + credentials: include)
       trips CORS on `oup.silverchair-cdn.com`.
    3. If the request-context fetch fails for any reason (older
       Playwright versions, network), fall back to plain requests
       — handy when the host has IP-based subscription access
       and the local machine carries that IP (e.g., the user's
       WashU laptop). Plain requests has no cookies but doesn't
       trip CORS either.
    """
    with PlaywrightSession(OXFORD_HOME) as session:
        session._warm()
        html = session.fetch_html(f"https://doi.org/{doi}")
        soup = BeautifulSoup(html, "lxml")
        meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        pdf_url = meta.get("content") if meta else ""
        if not pdf_url:
            raise ValueError(f"no citation_pdf_url for Oxford DOI {doi}")
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url

        # Stage A: browser-context request (cookies + no CORS).
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

    # Stage B: plain requests fallback for IP-subscription cases
    # where the cookie isn't needed.
    r = requests.get(
        pdf_url,
        headers={"User-Agent": OXFORD_UA},
        timeout=60,
        allow_redirects=True,
    )
    ct = (r.headers.get("content-type") or "").lower()
    if r.status_code == 200 and ("pdf" in ct or r.content[:4] == b"%PDF"):
        return r.content
    raise RuntimeError(
        f"Oxford PDF fetch failed: context.request={response_err!r}, "
        f"requests=status={r.status_code} content-type={ct!r}"
    )


def extract(
    doi: str,
    output_dir: Path | None = None,
    session: PlaywrightSession | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from an Oxford Academic article page."""
    from datetime import datetime, timezone

    print(f"  [oxford] fetching article for {doi}")

    owns_session = session is None
    if owns_session:
        session = PlaywrightSession(OXFORD_HOME).__enter__()

    try:
        # doi.org redirects to the canonical academic.oup.com URL; the
        # session goto follows redirects automatically.
        html = session.fetch_html(f"https://doi.org/{doi}")
        soup = BeautifulSoup(html, "lxml")

        if _detect_stub(soup):
            raise OxfordPaywallStub(
                f"academic.oup.com served a stub for {doi} (paywalled or pre-publication)"
            )

        meta = _extract_metadata(soup, doi)
        slug = doi.split("/", 1)[-1].lower().replace("/", "_").replace(".", "_")
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
            "extraction_method": "oxford_html+silverchair_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "silverchair_cdn" if fig["asset"] else "missing",
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
