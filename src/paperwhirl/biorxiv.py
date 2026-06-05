"""bioRxiv family detector + JATS-XML extractor.

Public surface:
  is_biorxiv(pdf_path)          -> (bool, doi_or_none, reason)
  resolve_doi_by_title(title)   -> doi_or_none
  BiorxivSession                -> context manager for Playwright lifecycle
  fetch_jats(doi)               -> str  (convenience; launches its own session)
  extract(pdf_path)             -> dict [session skeleton]
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from paperwhirl import config as pw_config

BIORXIV_DOI_PREFIXES = ("10.1101/", "10.64898/")

STAMP_RE = re.compile(
    r"bioRxiv\s+preprint\s+doi\s*:\s*https?://doi\.org/(10\.\d{4,}/\S+?)(?:\s|;|$)",
    re.IGNORECASE,
)

BIORXIV_HOME = "https://www.biorxiv.org/"
BIORXIV_API = "https://api.biorxiv.org/details/biorxiv"
# Stage 5 E13 (2026-05-24): medRxiv shares CSHL's DOI prefixes with
# bioRxiv (both 10.1101/ and 10.64898/), but lives behind a sibling
# API endpoint and a separate web host. The bioRxiv extractor handles
# both — we detect which one any given paper came from by which
# endpoint resolved it and then route every subsequent fetch
# (Cloudflare warmup, article-page HTML, figure CDN, manuscript PDF)
# to the matching host.
MEDRXIV_API = "https://api.biorxiv.org/details/medrxiv"


def _server_from_url(url: str) -> str:
    """Return 'biorxiv' or 'medrxiv' from a URL's host."""
    from urllib.parse import urlparse
    return "medrxiv" if "medrxiv.org" in urlparse(url).netloc else "biorxiv"


def _home_for_server(server: str) -> str:
    return f"https://www.{server}.org/"
JATS_CACHE_DIR = pw_config.jats_cache_dir()

CROSSREF_URL = "https://api.crossref.org/works"
CROSSREF_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"

CROSSREF_MARGIN = 1.5
CROSSREF_MIN_SCORE = 40.0


def _pdf_text_pages(pdf_path: Path, first: int = 1, last: int = 2) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", "-f", str(first), "-l", str(last), str(pdf_path), "-"],
        capture_output=True, text=True,
    )
    return result.stdout


_LINE_NUM_RE = re.compile(r"^\s*\d+\s+")


def _extract_page1_title(pdf_path: Path) -> str:
    text = _pdf_text_pages(pdf_path, first=1, last=1)
    candidates = []
    for line in text.splitlines()[:50]:
        cleaned = _LINE_NUM_RE.sub("", line).strip()
        if len(cleaned) < 20:
            continue
        if re.search(r"bioRxiv|doi\.org|preprint|copyright|license|funder|©", cleaned, re.I):
            continue
        candidates.append(cleaned)
    if not candidates:
        return ""
    return " ".join(candidates[:2])


def _is_biorxiv_doi(doi: str) -> bool:
    return any(doi.startswith(p) for p in BIORXIV_DOI_PREFIXES)


def resolve_doi_by_title(title: str) -> str | None:
    if not title:
        return None
    try:
        r = requests.get(
            CROSSREF_URL,
            params={"query.title": title, "rows": 5},
            headers={"User-Agent": CROSSREF_UA},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  [crossref] request failed: {exc}")
        return None

    items = r.json().get("message", {}).get("items", [])
    if not items:
        return None

    top = items[0]
    top_doi = top.get("DOI", "")
    top_score = float(top.get("score", 0))

    if not _is_biorxiv_doi(top_doi):
        return None
    if top_score < CROSSREF_MIN_SCORE:
        return None

    if len(items) >= 2:
        next_score = float(items[1].get("score", 0))
        if next_score > 0 and top_score < CROSSREF_MARGIN * next_score:
            return None

    return top_doi


def is_biorxiv(pdf_path: str | Path) -> tuple[bool, str | None, str]:
    """Detect whether a PDF is a bioRxiv paper and return its DOI.

    Returns (is_biorxiv, doi_or_none, reason_string).

    Stage 5 E2 fix: the prior "match the regex anywhere on pages 1-2"
    approach silently misidentified non-bioRxiv PDFs that happened to
    cite a bioRxiv preprint in their first two pages (e.g. a Nat Comm
    paper citing bin2cell on page 1 → extracted as bin2cell). The
    bioRxiv watermark is repeated as a HEADER on every page of an
    actual bioRxiv preprint, so we now require the same DOI to appear
    in the page-header region (first ~300 chars) of at least 2
    distinct pages. Citations in body text or references fail this
    test cleanly.
    """
    pdf_path = Path(pdf_path)
    # Scan first 4 pages — bioRxiv stamps every page, so 2+ matches
    # are easy to find for a real preprint; 4-page window keeps us
    # cheap on short PDFs.
    text = _pdf_text_pages(pdf_path, first=1, last=4)
    # pdftotext separates pages with form-feed.
    pages = text.split("\x0c")
    HEADER_CHARS = 300

    doi_page_count: dict[str, int] = {}
    for page in pages:
        if not page.strip():
            continue
        head = page[:HEADER_CHARS]
        for m in STAMP_RE.finditer(head):
            doi = m.group(1).rstrip(".")
            doi_page_count[doi] = doi_page_count.get(doi, 0) + 1

    # A DOI that appears in 2+ page headers is the paper's own stamp.
    # A single occurrence anywhere is a citation, not a stamp.
    own = [d for d, n in doi_page_count.items() if n >= 2]
    if own:
        # Tie-break: take whichever DOI has the highest count.
        doi = max(own, key=lambda d: doi_page_count[d])
        return True, doi, f"stamp in {doi_page_count[doi]} page headers: {doi}"

    title = _extract_page1_title(pdf_path)
    if not title:
        return False, None, "no header-stamp; could not extract page-1 title"

    doi = resolve_doi_by_title(title)
    if doi:
        return True, doi, f"no header-stamp; title-lookup resolved: {doi} (title: {title[:60]})"

    return False, None, f"no header-stamp; title-lookup found no bioRxiv DOI (title: {title[:60]})"


def _resolve_jats_url(doi: str) -> tuple[str, dict]:
    # bioRxiv's details API expects the VERSIONLESS DOI — it returns
    # all versions of a paper in `collection`, and the caller picks
    # the latest. Crossref-style versioned DOIs (e.g. `…v1`) come
    # through here when the user pastes a bioRxiv URL that includes
    # the version (our URL parser intentionally captures it). Strip
    # the trailing `vN` so the API recognises the paper; the version
    # info is rediscovered from the response.
    lookup_doi = re.sub(r"v\d+$", "", doi)
    collection: list = []
    for endpoint in (BIORXIV_API, MEDRXIV_API):
        r = requests.get(f"{endpoint}/{lookup_doi}", timeout=15)
        r.raise_for_status()
        collection = r.json().get("collection", [])
        if collection:
            break
    if not collection:
        raise ValueError(f"no versions found for DOI {lookup_doi} (tried biorxiv + medrxiv)")
    latest = max(collection, key=lambda x: int(x.get("version", 0)))
    jats_url = latest.get("jatsxml", "")
    if not jats_url:
        raise ValueError(f"no jatsxml URL for DOI {lookup_doi} v{latest.get('version')}")
    return jats_url, latest


def _doi_slug(doi: str) -> str:
    return doi.split("/", 1)[-1]


# Stage 5 E7 S22b: bioRxiv full-text HTML figure anchor. Each figure
# block has an <a href="…/F<N>.large.jpg?…" title="<full caption>">.
# Captures (image_url, figure_number, caption).
_BIORXIV_FIG_ANCHOR_RE = re.compile(
    r'href="(https://www\.biorxiv\.org/content/[^"]*?/F(\d+)\.large\.jpg[^"]*)"\s+title="([^"]*)"',
    re.IGNORECASE,
)


def _biorxiv_fig_record(number: int, asset_rel: str, caption: str) -> dict[str, Any]:
    """One figure entry in the session-skeleton schema (S22b)."""
    return {
        "id": f"figure_{number}",
        "order": number,
        "label": f"Figure {number}",
        "source_figure_id": f"source_fig_{number}",
        "view": {
            "source_page": None,
            "asset": asset_rel,
            "asset_role": "biorxiv_html",
            "crop": None,
            "original_caption": caption,
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
    }


class BiorxivSession:
    """Playwright-backed session that warms Cloudflare once for multiple fetches."""

    def __init__(self, cache_dir: Path = JATS_CACHE_DIR):
        self.cache_dir = cache_dir
        self._pw = None
        self._browser = None
        self._page = None
        # Stage 5 E13: track which origin Cloudflare has been warmed
        # at; re-warm if a fetch targets a different origin (medrxiv
        # vs biorxiv). None means no warmup has happened yet.
        self._warmed_home: str | None = None
        # Populated by fetch_jats — last call's resolved JATS URL and
        # the bioRxiv-API metadata for the selected version (for code
        # that needs to know the version number or the JATS URL after
        # the fetch).
        self.last_jats_url: str | None = None
        self.last_meta: dict | None = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        self._page = ctx.new_page()
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _warm_cloudflare(self, url: str | None = None):
        # Pick warmup origin from the target URL when given, else
        # default to biorxiv.org. Re-warm if the origin has changed
        # since the previous warmup (covers a single session that
        # crosses biorxiv and medrxiv URLs).
        if url:
            target_home = _home_for_server(_server_from_url(url))
        else:
            target_home = BIORXIV_HOME
        if self._warmed_home == target_home:
            return
        self._page.goto(target_home, wait_until="domcontentloaded", timeout=60000)
        for _ in range(8):
            time.sleep(2)
            if "Just a moment" not in self._page.title():
                break
        print(f"  [cloudflare] warmed {target_home} — page title: {self._page.title()!r}")
        self._warmed_home = target_home

    def _in_page_fetch(self, url: str) -> str:
        self._warm_cloudflare(url)
        result = self._page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                if (!r.ok) return {status: r.status, text: ''};
                const text = await r.text();
                return {status: r.status, text};
            }""",
            url,
        )
        if result["status"] != 200:
            raise RuntimeError(f"fetch {url} returned status {result['status']}")
        return result["text"]

    def _in_page_fetch_bytes(self, url: str) -> bytes:
        self._warm_cloudflare(url)
        import base64
        result = self._page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                if (!r.ok) return {status: r.status, data: ''};
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                return {status: r.status, data: btoa(binary)};
            }""",
            url,
        )
        if result["status"] != 200:
            raise RuntimeError(f"fetch {url} returned status {result['status']}")
        return base64.b64decode(result["data"])

    def fetch_figure(self, jats_url: str, hwp_id: str, dest: Path) -> Path:
        # /content/early/ → /content/{server}/early/, matching the
        # JATS URL's own host (biorxiv vs medrxiv).
        server = _server_from_url(jats_url)
        base = jats_url.replace("/content/early/", f"/content/{server}/early/")
        base = re.sub(r"\.source\.xml$", "", base)
        fig_url = f"{base}/{hwp_id}.large.jpg"
        data = self._in_page_fetch_bytes(fig_url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def scrape_article_figures(
        self, doi: str, version: int, paper_dir: Path
    ) -> list[dict[str, Any]]:
        """Stage 5 E7 S22b (2026-05-20): pull clean figure images +
        captions from the bioRxiv full-text HTML page.

        For preprints whose JATS is abstract-only (no `<fig>`), the
        old path fell to PDF page-crops — crude, mis-framed figures.
        But the article HTML serves proper figure images at
        `/content/biorxiv/early/.../F<N>.large.jpg`, each with its
        full caption in the anchor's `title=` attribute. Scrape those
        instead. Returns skeleton-schema figure dicts (asset_role
        `biorxiv_html`), or [] on any failure so the caller keeps the
        PDF-crop figures as a last resort.
        """
        import html as _html

        versionless = re.sub(r"v\d+$", "", doi)
        # Server (biorxiv vs medrxiv) inferred from the JATS URL the
        # last fetch_jats call resolved; default to biorxiv when no
        # prior fetch (legacy callers).
        server = (
            _server_from_url(self.last_jats_url)
            if self.last_jats_url else "biorxiv"
        )
        url = f"https://www.{server}.org/content/{versionless}v{version}.full"
        try:
            page = self._in_page_fetch(url)
        except Exception as exc:
            print(f"  [biorxiv] full-text HTML fetch failed ({url}): {exc}")
            return []

        figures_dir = paper_dir / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        seen: set[int] = set()
        figures: list[dict[str, Any]] = []
        for m in _BIORXIV_FIG_ANCHOR_RE.finditer(page):
            img_url, num_s, caption_raw = m.group(1), m.group(2), m.group(3)
            num = int(num_s)
            if num in seen:
                continue
            seen.add(num)
            clean_url = img_url.split("?", 1)[0]  # drop ?width=…&height=… resize
            try:
                data = self._in_page_fetch_bytes(clean_url)
            except Exception as exc:
                print(f"  [biorxiv-fig] F{num} fetch failed: {type(exc).__name__}")
                continue
            if data[:3] != b"\xff\xd8\xff" and data[:4] != b"\x89PNG":
                continue
            dest = figures_dir / f"figure_{num}.jpg"
            dest.write_bytes(data)
            caption = re.sub(r"\s+", " ", _html.unescape(caption_raw)).strip()
            figures.append(_biorxiv_fig_record(num, f"figures/{dest.name}", caption))
            print(f"    [biorxiv-fig] F{num} → {dest.name} ({len(data)} bytes)")

        figures.sort(key=lambda f: f["order"])
        return figures

    def fetch_jats(self, doi: str) -> str:
        slug = _doi_slug(doi)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.cache_dir / f"{slug}.source.xml"

        jats_url, meta = _resolve_jats_url(doi)
        jats_url = re.sub(r"(?<!:)//", "/", jats_url)
        self.last_jats_url = jats_url
        self.last_meta = meta

        if cache_path.exists():
            cached = cache_path.read_text(encoding="utf-8")
            # Stage 5 E7 S22a (2026-05-20): don't trust an empty /
            # abstract-only cached JATS. bioRxiv often serves a
            # metadata-only `.source.xml` for fresh preprints and fills
            # in the body + figures later; the old "cache forever"
            # behavior meant Re-generate could never pick that up (it
            # reused the stale empty file and re-ran the PDF fallback).
            # Re-fetch when the cache has neither figures nor sections.
            try:
                _p = parse_jats(cached)
                _usable = bool(_p.get("figures") or _p.get("sections"))
            except Exception:
                _usable = False
            if _usable:
                print(f"  [cache hit] {cache_path}")
                return cached
            print(
                f"  [cache stale] {cache_path} has no figures/sections; re-fetching"
            )

        version = meta.get("version", "?")
        print(f"  [fetch] DOI {doi} v{version}: {jats_url}")

        xml = self._in_page_fetch(jats_url)
        cache_path.write_text(xml, encoding="utf-8")
        print(f"  [cached] {len(xml)} chars → {cache_path}")
        return xml


def fetch_jats(doi: str, cache_dir: Path = JATS_CACHE_DIR) -> str:
    """Convenience wrapper: launches a one-shot Playwright session."""
    with BiorxivSession(cache_dir=cache_dir) as session:
        return session.fetch_jats(doi)


# Stage 5 E2: bioRxiv-direct keyword search. Europe PMC's preprint
# index lags bioRxiv by days-to-weeks for recent submissions; for
# time-sensitive discovery we hit biorxiv.org/search directly, which
# is the same surface the website's advanced-search uses.
#
# The search page renders results as Drupal/Highwire "citation" cards
# with stable class names (`highwire-article-citation`,
# `highwire-cite-linked-title`, etc.). We parse those with regex
# rather than adding a BeautifulSoup dep — fragile in principle, but
# the markup hasn't changed in years and the parser fails closed
# (empty list) rather than dirty.

_BIORXIV_SEARCH_URL = "https://www.biorxiv.org/search"

_BIORXIV_RESULT_RE = re.compile(
    r'<div class="highwire-article-citation[^"]*"[^>]*'
    r'data-pisa="biorxiv;([\d.]+)v(\d+)"[^>]*>(.+?)</div>\s*</li>',
    re.DOTALL,
)
_BIORXIV_TITLE_RE = re.compile(
    r'class="highwire-cite-linked-title"[^>]*>\s*'
    r'<span class="highwire-cite-title">(.+?)</span>',
    re.DOTALL,
)
_BIORXIV_AUTHOR_RE = re.compile(
    r'<span class="nlm-given-names">(.+?)</span>\s*'
    r'<span class="nlm-surname">(.+?)</span>',
)
# Parse the actual DOI from the metadata block of a search result.
# bioRxiv historically used `10.1101/...` for all preprints; newer
# submissions (2026+) use `10.64898/...`. Stage 5 E2 tire-kick caught
# us hard-coding `10.1101/<pisa_id>` and silently producing wrong
# DOIs (which then 404'd on the bioRxiv API). Pull the real prefix
# from the doi.org link in the result HTML — never construct it.
_BIORXIV_DOI_RE = re.compile(
    # bioRxiv's HTML uses inconsistent whitespace inside tags
    # (single space, double space, etc.). Tolerate any leading
    # whitespace before `class=` and don't assume a specific
    # attribute order. The key signal is the doi.org URL — that
    # carries the actual DOI with its real prefix (10.1101 for
    # older preprints, 10.64898 for newer ones).
    r'<span[^>]*class="highwire-cite-metadata-doi[^"]*"[^>]*>.*?'
    r'doi\.org/(10\.\d+/[\d.]+)',
    re.DOTALL,
)


def _author_short(given: str, surname: str) -> str:
    """Format an author as 'Surname I' (matches Europe PMC's
    authorString convention used elsewhere by search_papers)."""
    initial = (given.strip()[:1] or "").upper()
    return f"{surname.strip()} {initial}".strip()


def _parse_biorxiv_search_html(html: str, max_results: int) -> list[dict]:
    """Parse a bioRxiv search-results HTML page into structured records
    matching the discuss_tools.search_papers shape."""
    out: list[dict] = []
    for paper_id, _version, block in _BIORXIV_RESULT_RE.findall(html):
        # Pull the actual DOI from the result block — bioRxiv uses
        # different DOI prefixes for older (10.1101/) vs newer
        # (10.64898/) preprints, so we MUST NOT construct it from
        # the pisa-id with a hard-coded prefix. Skip the result if
        # we can't find a real DOI link.
        m_doi = _BIORXIV_DOI_RE.search(block)
        if not m_doi:
            continue
        doi = m_doi.group(1).rstrip(".")
        m_title = _BIORXIV_TITLE_RE.search(block)
        title = (m_title.group(1) if m_title else "").strip()
        authors = [_author_short(g, s) for g, s in _BIORXIV_AUTHOR_RE.findall(block)]
        if len(authors) > 3:
            authors = authors[:3] + ["et al."]
        # The pisa-id starts with YYYY.MM.DD for date-formatted bioRxiv
        # IDs, regardless of the DOI prefix.
        year = paper_id.split(".", 1)[0] if "." in paper_id else ""
        out.append({
            "id": doi,
            "kind": "doi",
            "source": "PPR",
            "title": title.rstrip("."),
            "authors": authors,
            "year": year,
            "journal": "bioRxiv",
            "doi": doi,
        })
        if len(out) >= max_results:
            break
    return out


def search_biorxiv(query: str, max_results: int = 10) -> list[dict]:
    """Keyword-search bioRxiv directly via its search page.

    Real-time coverage of bioRxiv's index (no Europe PMC ingest lag).
    Cost: ~10 s per call due to Playwright Cloudflare-warm + page
    fetch. Returns an empty list on any failure — the caller treats
    that as "no results found."
    """
    q = (query or "").strip()
    if not q:
        return []
    # bioRxiv's search uses + for inter-term separation and accepts a
    # numresults%3A<N> suffix to override the default page size.
    terms = "+".join(q.split())
    url = f"{_BIORXIV_SEARCH_URL}/{terms}+numresults%3A{max(1, min(max_results, 25))}"
    try:
        with BiorxivSession() as session:
            html = session._in_page_fetch(url)
    except Exception as exc:
        # Log loudly. Stage 5 E2 tire-kick had this swallowing a
        # sync_playwright-in-asyncio error silently — the tool
        # reported "0 results" for queries that actually had results
        # because the exception was caught here and `[]` returned
        # without surfacing the problem. Keep returning `[]` so the
        # model gets a clean empty-list result, but make the failure
        # visible in backend logs.
        print(
            f"  [biorxiv-search] FAILED for {url!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        return []
    return _parse_biorxiv_search_html(html, max_results)


def fetch_manuscript_pdf(doi: str, version: int | None = None) -> bytes:
    """Download the canonical bioRxiv PDF for a preprint.

    bioRxiv is behind Cloudflare, so plain requests gets a 403
    challenge page — same as JATS. Goes through a Playwright session
    that warms the cookie. If `version` is None, picks the latest
    version known to the bioRxiv API (same heuristic the JATS path
    uses).
    """
    versionless = re.sub(r"v\d+$", "", doi)
    with BiorxivSession() as bsession:
        # Always resolve via the API so we know which server hosts
        # this paper (biorxiv vs medrxiv) even when version is given.
        jats_url, meta = _resolve_jats_url(versionless)
        if version is None:
            version = int(meta.get("version", 1))
        server = _server_from_url(jats_url)
        url = f"https://www.{server}.org/content/{versionless}v{version}.full.pdf"
        return bsession._in_page_fetch_bytes(url)


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for key in list(el.attrib):
            if "}" in key:
                new_key = key.split("}", 1)[1]
                if new_key not in el.attrib:
                    el.attrib[new_key] = el.attrib.pop(key)
                else:
                    del el.attrib[key]


def _elem_text(el) -> str:
    if el is None:
        return ""
    return " ".join("".join(el.itertext()).split())


def _parse_references(root) -> list[dict[str, Any]]:
    """Stage 5 E8 S1 (2026-05-20): parse the JATS `<ref-list>` into
    structured reference records. Namespaces are already stripped by
    the caller (`parse_jats`). Each `<ref>` carries either a
    `<mixed-citation>` or `<element-citation>` with surnames, year,
    article-title, source (journal), and pub-ids (doi/pmid).

    Returns `[{n, authors, year, title, source, doi, pmid, raw}]`
    in document order. `n` is the 1-based position (matches the
    common in-text [N] numbering); a `<label>` is preferred when it
    is a bare integer. `raw` is a readable fallback citation built
    from the structured fields (the JATS itertext mashes tokens
    together without spaces, so we don't use it directly).
    """
    refs: list[dict[str, Any]] = []
    for idx, ref in enumerate(root.findall(".//ref-list//ref"), start=1):
        cite = (
            ref.find(".//element-citation")
            or ref.find(".//mixed-citation")
            or ref
        )
        surnames = [
            s.text.strip()
            for s in cite.findall(".//surname")
            if s.text and s.text.strip()
        ]
        # `<string-name>` (unstructured) fallback when no <surname>.
        if not surnames:
            surnames = [
                _elem_text(sn)
                for sn in cite.findall(".//string-name")
                if _elem_text(sn)
            ]
        year_el = cite.find(".//year")
        year = None
        if year_el is not None and (year_el.text or "").strip()[:4].isdigit():
            year = int(year_el.text.strip()[:4])
        title = _elem_text(cite.find(".//article-title")) or _elem_text(
            cite.find(".//chapter-title")
        )
        source = _elem_text(cite.find(".//source"))
        doi = None
        pmid = None
        for pid in cite.findall(".//pub-id"):
            t = (pid.get("pub-id-type") or "").lower()
            val = (pid.text or "").strip()
            if t == "doi" and not doi:
                doi = val
            elif t == "pmid" and not pmid:
                pmid = val

        label_el = ref.find("label")
        label = (label_el.text or "").strip() if label_el is not None else ""
        n = int(label) if label.isdigit() else idx

        authors_str = ", ".join(surnames[:8]) + (
            " et al." if len(surnames) > 8 else ""
        )
        raw = ". ".join(
            p for p in (
                authors_str,
                str(year) if year else "",
                title,
                source,
            ) if p
        ).strip()
        # Last-resort raw if the structured fields were empty.
        if not raw:
            raw = _elem_text(cite)

        refs.append({
            "n": n,
            "authors": surnames,
            "year": year,
            "title": title,
            "source": source,
            "doi": doi,
            "pmid": pmid,
            "raw": raw,
        })
    return refs


def parse_jats(xml_str: str) -> dict[str, Any]:
    """Parse JATS XML into a dict of extracted fields."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_str)

    _HWP_NS = "{http://schema.highwire.org/Journal}"
    fig_hwp_ids: dict[str, str] = {}
    for fig in root.iter():
        if fig.tag.endswith("}fig") or fig.tag == "fig":
            orig_id = fig.get("id", "")
            hwp_id = fig.get(f"{_HWP_NS}id", "")
            if orig_id and hwp_id:
                fig_hwp_ids[orig_id] = hwp_id

    _strip_ns(root)

    title = _elem_text(root.find(".//article-title"))

    authors = []
    for contrib in root.findall(".//contrib[@contrib-type='author']"):
        surname = contrib.findtext(".//surname", "")
        given = contrib.findtext(".//given-names", "")
        if surname:
            authors.append(f"{given} {surname}".strip())

    doi_el = root.find(".//article-id[@pub-id-type='doi']")
    doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None

    # Stage 5 E3 (2026-05-18): PMC NXML records carry pmid/pmcid in
    # `article-id` elements alongside the DOI. The parser previously
    # only looked for the DOI; downstream pmc.extract() hardcoded
    # pmid=None, so saved packets lost the PMID even for papers
    # PubMed clearly indexed (e.g. SMURF / PMC12154772 = PMID 40502153).
    # bioRxiv preprints typically have no PMID, so this is a no-op on
    # the bioRxiv path.
    pmid_el = root.find(".//article-id[@pub-id-type='pmid']")
    pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else None

    pmcid_el = root.find(".//article-id[@pub-id-type='pmcid']")
    pmcid = pmcid_el.text.strip() if pmcid_el is not None and pmcid_el.text else None
    if pmcid and not pmcid.upper().startswith("PMC"):
        # NXML often stores the bare numeric PMC id ("12154772") under
        # pub-id-type="pmcid"; the rest of the codebase uses the
        # "PMC<digits>" form. Normalize on the way out.
        pmcid = f"PMC{pmcid}"

    # Publication year — bioRxiv preprints use pub-type="epreprint"
    # (sometimes with no pub-type at all on <pub-date>), and PMC NXML
    # uses epub / ppub / pub. Try the targeted queries first, then fall
    # back to the FIRST <year> found anywhere under <article-meta>, so
    # we never silently drop the year just because the wrapper element
    # doesn't match a known shape.
    year: int | None = None
    for q in (
        ".//pub-date[@pub-type='epreprint']/year",
        ".//pub-date[@pub-type='epub']/year",
        ".//pub-date[@pub-type='ppub']/year",
        ".//pub-date[@date-type='pub']/year",
        ".//pub-date/year",
    ):
        el = root.find(q)
        if el is not None and el.text and el.text.strip().isdigit():
            year = int(el.text.strip())
            break
    if year is None:
        meta = root.find(".//article-meta")
        if meta is not None:
            for el in meta.iter("year"):
                if el.text and el.text.strip().isdigit():
                    year = int(el.text.strip())
                    break

    journal = _elem_text(root.find(".//journal-meta/journal-title-group/journal-title"))
    if not journal:
        journal = _elem_text(root.find(".//journal-meta/journal-title"))

    abstract = _elem_text(root.find(".//abstract"))

    # Single document-order pass to collect figures and tables. Each gets
    # a `source_order` integer so the UI can render them interleaved in
    # the paper's natural reading order (Fig 1 -> Fig 2 -> Table 1 -> ...).
    import xml.etree.ElementTree as _ET
    _SUPP_RE = re.compile(
        r"(?:Figure|Table)\s+S\d|supplement|supp\b|extended\s+data",
        re.IGNORECASE,
    )

    figures: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    source_order = 0
    for el in root.iter():
        if el.tag == "fig":
            label = el.findtext("label", "").strip()
            if _SUPP_RE.search(label):
                continue
            source_order += 1
            num_match = re.search(r"(\d+)", label)
            number = int(num_match.group(1)) if num_match else 0

            cap_title = _elem_text(el.find(".//caption/title"))
            cap_paragraphs = [_elem_text(p) for p in el.findall(".//caption/p")]
            caption_text = " ".join([cap_title] + cap_paragraphs).strip()

            graphic = el.find(".//graphic")
            href = graphic.get("href", "") if graphic is not None else ""

            fig_id = el.get("id", "")
            hwp_id = fig_hwp_ids.get(fig_id, "")

            figures.append({
                "number": number,
                "source_order": source_order,
                "label": label,
                "caption_title": cap_title,
                "caption_text": caption_text,
                "graphic_href": href,
                "hwp_id": hwp_id,
            })

        elif el.tag == "table-wrap":
            label = el.findtext("label", "").strip()
            if _SUPP_RE.search(label):
                continue
            source_order += 1
            num_match = re.search(r"(\d+)", label)
            number = int(num_match.group(1)) if num_match else 0

            cap_title = _elem_text(el.find(".//caption/title"))
            cap_paragraphs = [_elem_text(p) for p in el.findall(".//caption/p")]
            caption_text = " ".join([cap_title] + cap_paragraphs).strip()

            table_el = el.find(".//table")
            html = (
                _ET.tostring(table_el, encoding="unicode", method="html")
                if table_el is not None else ""
            )
            # Stage 6 E7 tire-kick fix (2026-05-26): JCI (and other
            # publishers) deposit tables to PMC as `<graphic>` images
            # inside <table-wrap>, not as structured `<table>` markup.
            # Capture the graphic_href so the PMC / bioRxiv extractor
            # can fetch the table image like a figure asset.
            tbl_graphic = el.find(".//graphic")
            tbl_graphic_href = (
                tbl_graphic.get("href", "")
                if tbl_graphic is not None else ""
            )

            tables.append({
                "number": number,
                "source_order": source_order,
                "label": label,
                "caption_title": cap_title,
                "caption_text": caption_text,
                "html": html,
                "graphic_href": tbl_graphic_href,
            })

    _SKIP_SECTIONS = {
        "author contributions", "declaration of interests",
        "competing financial interests", "data availability",
        "code availability", "data and code availability",
        "data and materials availability", "computational resources",
        "funding", "resource availability", "supplemental information",
        "supplementary figures", "extended data figures",
        "supporting information",
    }

    sections = []
    for sec in root.findall(".//body/sec") + root.findall(".//back/sec"):
        sec_title = sec.findtext("title", "").strip()
        if sec_title.lower() in _SKIP_SECTIONS:
            continue
        paragraphs = [_elem_text(p) for p in sec.findall("p")]
        for subsec in sec.findall(".//sec"):
            sub_title = subsec.findtext("title", "").strip()
            if sub_title.lower() in _SKIP_SECTIONS:
                continue
            if sub_title:
                paragraphs.append(f"### {sub_title}")
            paragraphs.extend(_elem_text(p) for p in subsec.findall("p"))
        text = "\n\n".join(p for p in paragraphs if p)
        if sec_title or text:
            sections.append({"title": sec_title, "text": text})

    # Stage 5 E7 S1: detect the "PMC hosts a PDF even though the XML
    # body is empty" case (JBC reviews and similar publisher-PDF-only
    # deposits). Driven by the `pmc-prop-has-pdf` custom-meta value
    # on PMC NXML; harmless on bioRxiv JATS (the element doesn't
    # exist there, so this stays False).
    has_pmc_pdf = False
    for cm in root.findall(".//custom-meta"):
        name_el = cm.find("meta-name")
        value_el = cm.find("meta-value")
        if (
            name_el is not None
            and (name_el.text or "").strip() == "pmc-prop-has-pdf"
            and value_el is not None
            and (value_el.text or "").strip().lower() == "yes"
        ):
            has_pmc_pdf = True
            break

    return {
        "title": title,
        "authors": authors,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "year": year,
        "journal": journal or None,
        "abstract": abstract,
        "figures": figures,
        "tables": tables,
        "sections": sections,
        "has_pmc_pdf": has_pmc_pdf,
        "references": _parse_references(root),
    }


def _jats_has_body(parsed: dict) -> bool:
    return bool(parsed["figures"]) or bool(parsed["sections"])


def extract(
    pdf_path: str | Path,
    output_dir: Path | None = None,
    session: BiorxivSession | None = None,
) -> dict[str, Any]:
    """Full extraction: detect, fetch JATS, parse, build session skeleton."""
    from datetime import datetime, timezone

    pdf_path = Path(pdf_path)
    detected, doi, reason = is_biorxiv(pdf_path)
    if not detected or not doi:
        raise ValueError(f"not a bioRxiv paper: {reason}")

    if session:
        xml = session.fetch_jats(doi)
    else:
        xml = fetch_jats(doi)

    parsed = parse_jats(xml)
    slug = _doi_slug(doi)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if not _jats_has_body(parsed):
        print(f"  [fallback] JATS body empty for {doi}; using E2 PDF extraction")
        return _extract_with_e2_fallback(pdf_path, parsed, slug, now, output_dir)

    out = output_dir or Path("/tmp") / slug
    out.mkdir(parents=True, exist_ok=True)

    jats_url = getattr(session, "last_jats_url", None)
    if not jats_url:
        jats_url, _ = _resolve_jats_url(doi)
        jats_url = re.sub(r"(?<!:)//", "/", jats_url)

    figures_dir = out / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    walkthrough_figures = []
    source_figures = []
    for fig in parsed["figures"]:
        source_id = f"source_fig_{fig['number']}"
        asset = None
        hwp_id = fig.get("hwp_id")
        if hwp_id and session:
            dest = figures_dir / f"figure_{fig['number']}.jpg"
            try:
                session.fetch_figure(jats_url, hwp_id, dest)
                asset = str(dest.relative_to(out))
                print(f"    [fig] {hwp_id} → {dest.name}")
            except Exception as exc:
                print(f"    [fig] {hwp_id} fetch failed: {exc}")

        source_figures.append({
            "id": source_id,
            "figure": fig["label"],
            "page": None,
            "asset": asset,
            "caption": fig["caption_text"],
            "extraction_method": "jats_xml+biorxiv_cdn",
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
                "asset_role": "biorxiv_cdn" if asset else "missing",
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

    # Tables (from parse_jats <table-wrap> walker).
    # Stage 6 E7 tire-kick fix (2026-05-26): sort by extracted number
    # before enumerating. Some publishers' JATS stores tables in
    # citation order rather than positional order; without this sort
    # the UI renders them in the wrong order. (Observed on JCI/PMC
    # for `10.1172/JCI182088` — symmetric fix in pmc.py.)
    walkthrough_tables: list[dict[str, Any]] = []
    source_tables: list[dict[str, Any]] = []
    sorted_tables = sorted(
        parsed.get("tables", []),
        key=lambda t: (t.get("number") or 0, t.get("source_order") or 0),
    )
    for tidx, tbl in enumerate(sorted_tables):
        tslot = tidx + 1
        source_table_id = f"source_tbl_{tslot}"
        source_tables.append({
            "id": source_table_id,
            "table": tbl["label"],
            "caption": tbl["caption_text"],
            "html": tbl["html"],
            "extraction_method": "jats_xml",
        })
        walkthrough_tables.append({
            "id": f"table_{tslot}",
            "order": tslot,
            # Stage 6 E7 (2026-05-26): see pmc.py for the rationale —
            # override source_order so the frontend's source_order
            # re-sort respects the number-order we just established.
            "source_order": 9999 + tslot,
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
        pages_list = [{"page": i + 1, "text": sec["text"]} for i, sec in enumerate(parsed["sections"])]

    if output_dir:
        (output_dir / "extracted_text.txt").write_text(body_text, encoding="utf-8")

    title = parsed["title"]
    authors = parsed["authors"]

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage2_e5",
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
            # Stage 6 E7 (2026-05-26): was hard-coded "bioRxiv";
            # medRxiv papers route through the same extractor (E13)
            # and parse_jats already pulls "medRxiv" from
            # <journal-title>. Twin fix in resolve.py.
            "journal": parsed.get("journal") or "bioRxiv",
            "doi": doi,
            "pmid": None,
            "arxiv_id": None,
            "biorxiv_doi": doi,
            "preprint_url": f"https://doi.org/{doi}",
            "source_pdf": str(pdf_path),
            "publication_state": "preprint",
        },
        "paper_kind": {
            "primary": "method",
            "secondary": "empirical",
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


def _extract_with_e2_fallback(
    pdf_path: Path,
    jats_parsed: dict,
    slug: str,
    now: str,
    output_dir: Path | None,
) -> dict[str, Any]:
    from paperwhirl import extract as e2

    out = output_dir or Path("/tmp") / slug
    out.mkdir(parents=True, exist_ok=True)

    pages = e2.extract_pages(pdf_path)
    pdfinfo = e2.parse_pdfinfo(pdf_path)
    figures = e2.find_figures(pages)

    render_page_nums = sorted({
        p for f in figures
        for p in (f["caption_page"], f["caption_page"] - 1)
        if p >= 1
    })
    rendered_pages = e2.render_pages(pdf_path, render_page_nums, out / "pages")

    image_scores = e2.pdf_image_page_scores(pdf_path)
    e2.choose_asset_pages(figures, rendered_pages, image_scores, len(pages))

    # Figure-region cropping was attempted here (pdfplumber bbox,
    # then LLM-vision) but neither produced reliable crops on
    # vector PDFs — pdfplumber misses text-glyph axis labels; the
    # vision model hallucinated coordinates. Punted to Stage 5
    # for a proper iteration pass (image segmentation, alternative
    # vision models, or fine-tuned bbox detector). Helpers kept in
    # extract.py as the starting point. See FEATURES.md.

    e2_skeleton = e2.build_skeleton(
        pdf_path=pdf_path,
        slug=slug,
        output_dir=out,
        pages=pages,
        pdfinfo=pdfinfo,
        figures=figures,
        rendered_pages=rendered_pages,
    )

    doi = jats_parsed["doi"]
    e2_skeleton["session"]["id"] = f"{slug}_stage2_e5"
    if jats_parsed["title"]:
        e2_skeleton["session"]["title"] = jats_parsed["title"]
    e2_skeleton["paper"]["title"] = jats_parsed["title"] or e2_skeleton["paper"]["title"]
    e2_skeleton["paper"]["authors"] = jats_parsed["authors"] or e2_skeleton["paper"]["authors"]
    e2_skeleton["paper"]["first_author"] = (
        jats_parsed["authors"][0].split()[-1] if jats_parsed["authors"]
        else e2_skeleton["paper"]["first_author"]
    )
    e2_skeleton["paper"]["doi"] = doi
    e2_skeleton["paper"]["biorxiv_doi"] = doi
    # Stage 6 E7 (2026-05-26): see twin comment in extract() above.
    e2_skeleton["paper"]["journal"] = jats_parsed.get("journal") or "bioRxiv"
    e2_skeleton["paper"]["publication_state"] = "preprint"
    e2_skeleton["paper"]["preprint_url"] = f"https://doi.org/{doi}" if doi else None
    # Stage 5 E8 S1: carry parsed references (abstract-only JATS has
    # none, but a fuller JATS that still lacked figures might).
    if jats_parsed.get("references"):
        e2_skeleton["references"] = jats_parsed["references"]

    return e2_skeleton
