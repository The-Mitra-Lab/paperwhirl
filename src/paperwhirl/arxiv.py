"""arXiv extractor for direct preprint targets.

Triggered only when the *target* paper itself is an arXiv preprint —
either via the arXiv DOI prefix (`10.48550/arXiv.*`) or a raw arXiv
identifier (e.g., `2401.00368` or `cond-mat/0301001`). bioRxiv/arXiv
preprints are not substitutes for paywalled published versions.

Public surface:
  is_arxiv_doi(doi)       -> bool
  is_arxiv_id(s)          -> bool
  doi_to_arxiv_id(doi)    -> str | None
  ArxivNoHTML             -> exception when arxiv.org/html is not available
  extract(arxiv_id, ...)  -> dict [session skeleton]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_HTML = "https://arxiv.org/html"
USER_AGENT = "Mozilla/5.0 (PaperWhirl/0.1; mailto:rob.mitra@gmail.com)"

# New-style IDs (post-2007): 2401.00368, 2401.00368v2
_NEW_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
# Old-style: cond-mat/0301001
_OLD_ID_RE = re.compile(r"^[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?$")
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arXiv\.(.+)$", re.IGNORECASE)


class ArxivNoHTML(Exception):
    """Raised when arxiv.org/html/{id} is not available for this paper."""


def is_arxiv_doi(doi: str) -> bool:
    return bool(_ARXIV_DOI_RE.match(doi))


def is_arxiv_id(s: str) -> bool:
    s = s.strip()
    return bool(_NEW_ID_RE.match(s) or _OLD_ID_RE.match(s))


def doi_to_arxiv_id(doi: str) -> str | None:
    m = _ARXIV_DOI_RE.match(doi)
    return m.group(1) if m else None


def _strip_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id)


_EMPTY_META: dict[str, Any] = {
    "title": "", "authors": [], "abstract": "", "year": None, "doi": None,
}


def _fetch_arxiv_metadata(arxiv_id: str) -> dict[str, Any]:
    """Query the arXiv Atom API for clean metadata.

    Stage 5 E7 S14 (2026-05-19): the arXiv API is rate-limited and
    frequently slow. It used to be a HARD dependency — a ReadTimeout
    here crashed the whole extraction even though the HTML render
    (the actual content source) was available. Now it fails soft:
    one retry, then return empty metadata. `extract()` backfills
    title / year from the HTML render so a flaky API never blocks a
    paper. The API is still preferred when it answers (clean author
    list, abstract, DOI), but it's no longer load-bearing.
    """
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            r = requests.get(
                ARXIV_API,
                params={"id_list": _strip_version(arxiv_id)},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            r.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                _time.sleep(1.5)
    else:
        print(
            f"  [arxiv] metadata API unavailable for {arxiv_id} "
            f"({type(last_exc).__name__}); backfilling from HTML"
        )
        return dict(_EMPTY_META)

    soup = BeautifulSoup(r.text, "xml")
    entry = soup.find("entry")
    if not entry:
        # "Rate exceeded" plain-text body or genuinely no record.
        return dict(_EMPTY_META)

    title = entry.find("title").get_text(strip=True) if entry.find("title") else ""
    title = re.sub(r"\s+", " ", title)
    authors = [a.find("name").get_text(strip=True) for a in entry.find_all("author")]
    summary = entry.find("summary").get_text(strip=True) if entry.find("summary") else ""

    year = None
    published = entry.find("published")
    if published:
        m = re.match(r"(\d{4})", published.get_text())
        if m:
            year = int(m.group(1))

    doi = None
    for link in entry.find_all("link"):
        if link.get("title") == "doi":
            href = link.get("href", "")
            m = re.search(r"doi\.org/(.+)$", href)
            if m:
                doi = m.group(1)

    return {
        "title": title,
        "authors": authors,
        "abstract": summary,
        "year": year,
        "doi": doi,
    }


def _backfill_metadata_from_html(
    meta: dict[str, Any], soup: BeautifulSoup, arxiv_id: str
) -> dict[str, Any]:
    """Stage 5 E7 S14: fill missing metadata fields from the LaTeXML
    HTML render + the arXiv ID, for when the metadata API was slow /
    rate-limited. Only fills fields the API left empty — the API is
    still authoritative when it answered.

    - title: `h1.ltx_title_document` (clean), else the doc `<title>`.
    - year: from the arXiv ID's YYMM prefix (`2605` → 2026). New-style
      IDs only; old-style (`hep-th/9901001`) skip this.
    - authors: from `.ltx_personname` (S21). LaTeXML usually emits one
      `.ltx_personname` per author with a clean name; some papers mash
      all authors + affiliations into one blob. We strip trailing
      affiliation/footnote markers and keep entries that still look
      like a single name (short, no internal comma), so the clean
      case yields proper authors and the blob case degrades to empty
      rather than shipping garbage.
    """
    if not meta.get("title"):
        t = soup.select_one("h1.ltx_title_document") or soup.select_one("h1.ltx_title")
        title = ""
        if t:
            title = re.sub(r"\s+", " ", t.get_text(" ", strip=True))
        elif soup.title:
            # arXiv abs/html <title> is "[id] Real Title" — strip the id.
            title = re.sub(r"^\[\S+\]\s*", "", soup.title.get_text(strip=True))
        meta["title"] = title

    if meta.get("year") is None:
        m = re.match(r"(\d{2})(\d{2})\.", _strip_version(arxiv_id))
        if m:
            meta["year"] = 2000 + int(m.group(1))

    if not meta.get("authors"):
        names: list[str] = []
        seen: set[str] = set()
        for el in soup.select(".ltx_personname"):
            name = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
            # Drop trailing affiliation / footnote markers (superscript
            # digits, *, daggers, sections, commas).
            name = re.sub(r"[\s,;0-9*†‡§¶∗⋆]+$", "", name).strip()
            # Keep only things that still look like a single author name:
            # short, with a letter, no internal comma (a comma usually
            # means several authors got mashed into one element).
            if (
                name
                and len(name) <= 60
                and "," not in name
                and re.search(r"[A-Za-z]", name)
                and name not in seen
            ):
                seen.add(name)
                names.append(name)
        if names:
            meta["authors"] = names

    return meta


def _fetch_html(arxiv_id: str) -> tuple[BeautifulSoup, str]:
    url = f"{ARXIV_HTML}/{arxiv_id}"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        raise ArxivNoHTML(f"arxiv.org/html/{arxiv_id} returned {r.status_code}")
    # arXiv returns a 200 even for "HTML not available" — detect via the
    # presence of the LaTeXML container.
    if "ltx_page_main" not in r.text and "ltx_document" not in r.text:
        raise ArxivNoHTML(f"arxiv.org/html/{arxiv_id} has no rendered HTML")
    return BeautifulSoup(r.text, "lxml"), r.url


# arXiv section titles like "1 Introduction" — strip the leading number for skip-matching.
_SKIP_SECTIONS = {
    "acknowledgements",
    "acknowledgments",
    "references",
    "appendix",
}


def _section_title_normalized(raw: str) -> str:
    # Drop a leading "1 ", "2.1 ", "A.1 ", etc.
    t = re.sub(r"^[A-Z]?\d+(\.\d+)*\s+", "", raw)
    return re.sub(r"\s+", " ", t).strip().lower()


def _extract_references(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Stage 5 E8 S1 (arXiv add, 2026-05-20): parse the LaTeXML
    bibliography (`li.ltx_bibitem`) into reference records.

    arXiv references are rendered as plain-text citations (not the
    structured surname/year/title fields JATS gives), so the record
    carries `raw` (the citation string) plus a DOI if one is linked;
    the structured fields stay empty. `n` comes from the bibitem's
    tag ("[12]") when it's a bare integer, else the 1-based order.

    Returns [] when LaTeXML didn't render the bibliography (some
    papers have an empty `ltx_biblist`) — `lookup_reference` then
    reports "no parsed references" gracefully.
    """
    refs: list[dict[str, Any]] = []
    for idx, li in enumerate(soup.select("li.ltx_bibitem"), start=1):
        tag = li.select_one(".ltx_tag")
        label = re.sub(r"[\[\]]", "", tag.get_text(strip=True)) if tag else ""
        n = int(label) if label.isdigit() else idx
        blocks = li.select(".ltx_bibblock")
        raw = " ".join(b.get_text(" ", strip=True) for b in blocks) if blocks \
            else li.get_text(" ", strip=True)
        raw = re.sub(r"\s+", " ", raw).strip()
        doi = None
        for a in li.find_all("a", href=True):
            m = re.search(r"doi\.org/(10\.\S+)", a["href"])
            if m:
                doi = m.group(1)
                break
        refs.append({
            "n": n,
            "authors": [],
            "year": None,
            "title": "",
            "source": "",
            "doi": doi,
            "pmid": None,
            "raw": raw,
        })
    return refs


def _extract_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for sec in soup.select("section.ltx_section"):
        h2 = sec.find("h2", recursive=False)
        if not h2:
            continue
        title_raw = h2.get_text(" ", strip=True)
        if _section_title_normalized(title_raw) in _SKIP_SECTIONS:
            continue

        parts: list[str] = []
        for el in sec.find_all(["h3", "p"]):
            if el.find_parent("figure"):
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
            sections.append({"title": title_raw, "text": text})
    return sections


_FIG_LABEL_RE = re.compile(r"(figure\s*\d+)\b", re.IGNORECASE)


def _text_with_math(el: Any) -> str:
    """Stage 5 E7 S20 (2026-05-19): extract an element's text with
    inline math preserved as LaTeX.

    arXiv's LaTeXML renders math as `<math alttext="\\tau^{2}">` whose
    subtree holds BOTH the rendered MathML glyphs and an
    `<annotation encoding="application/x-tex">` with the source. A
    plain `get_text()` concatenates the glyph and the source —
    "⋆ \\star denotes …" — which renders as garbage in the legend.
    Walk the tree instead, replacing each `<math>` with `$alttext$`
    so KaTeX renders it. Non-math text is passed through verbatim;
    whitespace is collapsed by the caller.
    """
    out: list[str] = []
    for child in getattr(el, "children", []):
        name = getattr(child, "name", None)
        if name == "math":
            ann = child.find("annotation")
            tex = (child.get("alttext") or (ann.get_text() if ann else "") or "").strip()
            if tex:
                out.append(f"${tex}$")
        elif name is None:
            out.append(str(child))
        else:
            out.append(_text_with_math(child))
    return "".join(out)


def _extract_figures(
    soup: BeautifulSoup,
    html_url: str,
    output_dir: Path,
) -> list[dict[str, Any]]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    figures: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    for fig_el in soup.find_all("figure", class_="ltx_figure"):
        # Find the "Figure N:" caption anywhere in this figure's
        # subtree. S19 (2026-05-19): composite figures wrap subfigure
        # panels, each with its own "(a)/(b) …" figcaption plus the real
        # parent "Figure N:" caption. The old `find("figcaption")`
        # grabbed the FIRST descendant caption — a subfigure's "(a) …" —
        # which doesn't match `Figure \d+`, so the whole composite figure
        # (e.g. Figure 3 of 2512.02556: Prefilling / Decoding panels) was
        # dropped. Scan all figcaptions in the subtree for the one that
        # actually carries the figure number.
        # (S16: figures with a caption but no <img> are kept with
        # asset=None and filled from the PDF later.)
        caption_text = ""
        number: int | None = None
        for fc in fig_el.find_all("figcaption"):
            # S20: math-aware extraction so inline equations in the
            # legend survive as `$…$` instead of garbled glyph+source.
            txt = re.sub(r"\s+", " ", _text_with_math(fc)).strip()
            m = _FIG_LABEL_RE.search(txt)
            if m:
                caption_text = txt
                number = int(re.search(r"\d+", m.group(1)).group(0))
                break
        if number is None:
            continue
        if number in seen_numbers:
            continue
        seen_numbers.add(number)

        asset: str | None = None
        imgs = fig_el.find_all("img")
        if imgs:
            # Use the first image — arXiv figures with subfigures still
            # have one root img per subfigure; we keep the leading one.
            img_src = imgs[0].get("src", "")
            # Stage 6 E7 follow-up (2026-05-27): arXiv's LaTeXML
            # rendering produces TWO different src patterns depending
            # on the source-build version:
            #   (a) plain filename, e.g. `x1.png`   — relative to
            #       the article directory. Needs the page URL
            #       treated as a directory (trailing slash).
            #   (b) article-id prefix, e.g.
            #       `2605.13301v1/x1.png`   — already-rooted within
            #       /html/, replaces the article-id segment in the
            #       page URL. Must NOT have a trailing slash on
            #       page URL (otherwise the path doubles, per the
            #       prior Stage 5 E7 S15 bug).
            # Detect by checking whether the src starts with an
            # arXiv-shaped ID (NNNN.NNNN[N][vN]/). Stage 5 E7 S15
            # fixed (b); this restores (a) for newer papers like
            # 2601.21590 where the src is just "x1.png" and
            # urljoin against a slashless page URL produced
            # `https://arxiv.org/html/x1.png` (404).
            if re.match(r"^\d{4}\.\d{4,5}(v\d+)?/", img_src):
                base = html_url  # pattern (b)
            else:
                base = html_url if html_url.endswith("/") else html_url + "/"
            full_url = urljoin(base, img_src)
            if full_url:
                ext = Path(img_src).suffix.lower() or ".png"
                dest = figures_dir / f"figure_{number}{ext}"
                try:
                    data = requests.get(full_url, headers={"User-Agent": USER_AGENT}, timeout=30)
                    data.raise_for_status()
                    dest.write_bytes(data.content)
                    asset = str(dest.relative_to(output_dir))
                    print(f"    [fig] Fig {number} → {dest.name} ({len(data.content)} bytes)")
                except Exception as exc:
                    print(f"    [fig] Fig {number} download failed: {exc}")
        else:
            print(f"    [fig] Fig {number} has no <img> (inline-rendered); will try PDF")

        figures.append({
            "number": number,
            "label": f"Figure {number}",
            "caption_text": caption_text,
            "asset": asset,
        })

    figures.sort(key=lambda f: f["number"])
    return figures


def _fill_missing_figure_assets_from_pdf(
    figures: list[dict[str, Any]], arxiv_id: str, output_dir: Path
) -> list[dict[str, Any]]:
    """Stage 5 E7 S16 (2026-05-19): fill in images for figures the HTML
    couldn't supply (inline-rendered, no `<img>` — asset is None) by
    extracting them from the arXiv PDF.

    Matches by figure number: PDF figure N's cropped image becomes
    HTML figure N's asset, while the (cleaner) HTML caption is kept.
    arXiv PDFs are public (no Cloudflare), so the fetch is cheap. Best
    effort — any figure the PDF extractor can't find stays asset=None
    (caption-only card), and the whole step no-ops if nothing's
    missing or the PDF can't be fetched.
    """
    import shutil

    missing = [f for f in figures if not f.get("asset")]
    if not missing:
        return figures

    print(
        f"  [arxiv] {len(missing)} figure(s) had no HTML image; "
        f"supplementing from PDF"
    )
    try:
        pdf_bytes = fetch_manuscript_pdf(arxiv_id)
    except Exception as exc:
        print(f"  [arxiv] PDF fetch for figure supplement failed: {exc}")
        return figures

    pdf_path = output_dir / "arxiv_source.pdf"
    pdf_path.write_bytes(pdf_bytes)

    from paperwhirl.extract import extract_figures_only_from_pdf

    tmp = output_dir / "_pdf_figs"
    pdf_figs = extract_figures_only_from_pdf(pdf_path, tmp)
    by_num: dict[int, dict[str, Any]] = {
        pf.get("order"): pf for pf in pdf_figs if pf.get("order") is not None
    }

    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    for f in missing:
        pf = by_num.get(f["number"])
        pf_asset = (pf or {}).get("view", {}).get("asset")
        if not pf_asset:
            continue
        src_path = tmp / pf_asset
        if not src_path.exists():
            continue
        ext = src_path.suffix or ".png"
        dest = figures_dir / f"figure_{f['number']}{ext}"
        try:
            shutil.copyfile(src_path, dest)
        except OSError as exc:
            print(f"    [fig] Fig {f['number']} PDF copy failed: {exc}")
            continue
        f["asset"] = str(dest.relative_to(output_dir))
        print(f"    [fig] Fig {f['number']} ← PDF crop ({dest.name})")

    shutil.rmtree(tmp, ignore_errors=True)
    return figures


def fetch_manuscript_pdf(arxiv_id: str) -> bytes:
    """Download arXiv's canonical PDF for a preprint. Plain HTTP —
    arXiv is public and has no Cloudflare friction."""
    import requests
    # Strip any trailing version (e.g. 2401.12345v2 → 2401.12345);
    # arxiv.org/pdf accepts both, but versionless gets the latest.
    bare = re.sub(r"v\d+$", "", arxiv_id)
    url = f"https://arxiv.org/pdf/{bare}.pdf"
    r = requests.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if not ctype.startswith("application/pdf"):
        raise RuntimeError(f"arXiv returned non-PDF for {arxiv_id}: {ctype}")
    return r.content


def extract(
    arxiv_id: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Extract a session skeleton from an arXiv preprint."""
    from datetime import datetime, timezone

    arxiv_id = arxiv_id.strip()
    print(f"  [arxiv] fetching metadata for {arxiv_id}")
    meta = _fetch_arxiv_metadata(arxiv_id)

    print(f"  [arxiv] fetching HTML for {arxiv_id}")
    soup, html_url = _fetch_html(arxiv_id)

    # S14: backfill any metadata the (flaky) API didn't supply from
    # the HTML render we just fetched, so a slow API never produces a
    # titleless packet.
    meta = _backfill_metadata_from_html(meta, soup, arxiv_id)

    slug = _strip_version(arxiv_id).replace("/", "_")
    out = output_dir or Path("/tmp") / slug
    out.mkdir(parents=True, exist_ok=True)

    figures = _extract_figures(soup, html_url, out)
    # S16: fill images for any inline-rendered (no-<img>) figures from
    # the arXiv PDF, matched by figure number.
    figures = _fill_missing_figure_assets_from_pdf(figures, arxiv_id, out)
    sections = _extract_sections(soup)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

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
            "extraction_method": "arxiv_html",
        })
        walkthrough_figures.append({
            "id": f"figure_{fig['number']}",
            "order": fig["number"],
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": fig["asset"],
                "asset_role": "arxiv_html" if fig["asset"] else "missing",
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
    title = meta["title"]
    canonical_doi = meta.get("doi") or f"10.48550/arXiv.{_strip_version(arxiv_id)}"

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage2_e11",
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
            "year": meta["year"],
            "journal": "arXiv",
            "doi": canonical_doi,
            "pmid": None,
            "pmcid": None,
            "arxiv_id": _strip_version(arxiv_id),
            "biorxiv_doi": None,
            "preprint_url": f"https://arxiv.org/abs/{_strip_version(arxiv_id)}",
            "source_pdf": None,
            "publication_state": "preprint",
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
        # Stage 5 E8 S1 (arXiv): parsed bibliography for lookup_reference.
        "references": _extract_references(soup),
        "exports": {
            "pdf": {
                "path": None,
                "created_at": None,
            }
        },
    }
