"""PMC family detector + NXML extractor.

Public surface:
  doi_to_pmcid(doi)    -> pmcid_or_none
  fetch_nxml(pmcid)    -> str
  extract(pmcid)       -> dict [session skeleton]
"""

from __future__ import annotations

import functools
import re
import time
from pathlib import Path
from typing import Any

import requests

from paperwhirl import config as pw_config
from paperwhirl.biorxiv import _elem_text, _strip_ns, parse_jats

NCBI_TOOL = "paperwhirl"
NCBI_EMAIL = "rob.mitra@gmail.com"

IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PMC_ARTICLE_URL = "https://pmc.ncbi.nlm.nih.gov/articles"

# Stage 6 E11 follow-up (2026-05-28): the NXML cache used to
# live at `<repo-root>/data/pmc/.nxml_cache`, which worked in
# dev (source tree is writable) but lands inside the read-only
# .app bundle when installed via the E10 zip distribution
# (PermissionError on first extract). Move to the platform-
# specific user cache root, matching where the other extractors
# already keep their per-paper caches (jats_cache, extractions).
NXML_CACHE_DIR = pw_config._cache_root() / "pmc" / ".nxml_cache"


class PMCStubError(Exception):
    """Stage 5 E3 (2026-05-18): raised when PMC's NXML body is a
    license-restriction stub (author opted out of PMC full-text
    archiving) instead of real paper content. The body text in
    these cases is a single paragraph saying *"The license terms
    selected by the author(s) for this preprint version do not
    permit archiving in PMC. The full text is available from the
    preprint server."* — useless for grounding Discuss.

    Carries the DOI parsed from the PMC NXML (`article-meta`'s
    `article-id` element), so callers that started from a direct
    PMCID input — and therefore don't have a DOI in scope — can
    still fall through to `_resolve_doi(doi)` for a better source.
    `doi` is None if the NXML didn't include one (rare; mostly
    very old papers).

    The resolver catches this and, for bioRxiv-DOI'd papers, falls
    through to the bioRxiv extractor (which gets real text from
    the preprint). For non-bioRxiv DOIs with no known publisher
    extractor, the error propagates so the user sees a meaningful
    message rather than a packet with no content.
    """

    def __init__(self, message: str, doi: str | None = None):
        super().__init__(message)
        self.doi = doi


# Stable phrase in PMC's license-restriction stub. Case-insensitive
# substring match against the parsed body text.
_PMC_STUB_PHRASE = "do not permit archiving in pmc"


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def _doi_to_pmid_via_esearch(doi: str) -> str | None:
    """Look up the PMID for a DOI via PubMed esearch.

    Stage 5 E7 S4 (2026-05-19): PMC's IDCONV endpoint is
    case-sensitive on the DOI suffix — `10.1074/jbc.r117.809194`
    misses while `10.1074/jbc.R117.809194` hits. PubMed esearch
    on `<doi>[DOI]` is case-insensitive and returns the same PMID
    for either form. Used as the fall-through path in
    `doi_to_pmcid` when IDCONV returns no record.
    """
    try:
        r = requests.get(
            ESEARCH_URL,
            params={
                "db": "pubmed",
                "term": f"{doi}[DOI]",
                "retmode": "json",
                "tool": NCBI_TOOL,
                "email": NCBI_EMAIL,
            },
            timeout=15,
        )
        r.raise_for_status()
        idlist = (
            r.json().get("esearchresult", {}).get("idlist", [])
        )
    except (requests.RequestException, ValueError):
        return None
    return idlist[0] if idlist else None


def doi_to_pmcid(doi: str) -> str | None:
    """Resolve a DOI to a PMCID.

    Two-stage lookup. IDCONV is the fast happy path but is
    case-sensitive on the DOI suffix; when it misses, fall through
    to PubMed esearch (case-insensitive) → `pmid_to_pmcid`. This
    rescues user-pasted DOIs whose case doesn't match the canonical
    form PMC indexes (e.g. JBC review DOIs use uppercase `R117`
    while users often paste lowercase).
    """
    try:
        r = requests.get(
            IDCONV_URL,
            params={
                "ids": doi,
                "format": "json",
                "tool": NCBI_TOOL,
                "email": NCBI_EMAIL,
            },
            timeout=15,
        )
        r.raise_for_status()
        records = r.json().get("records", [])
    except (requests.RequestException, ValueError):
        records = []
    if records:
        pmcid = records[0].get("pmcid")
        if pmcid:
            return pmcid

    # IDCONV missed (case-sensitive on suffix, or DOI just not in
    # PMC's index). Try PubMed esearch (case-insensitive).
    pmid = _doi_to_pmid_via_esearch(doi)
    if pmid is None:
        return None
    return pmid_to_pmcid(pmid)


@functools.lru_cache(maxsize=256)
def pmid_to_ids(pmid: str) -> tuple[str | None, str | None]:
    """Resolve a PMID to (pmcid, doi).

    Tries IDCONV first (PMC-centric — fastest path when the paper IS
    in PMC). If IDCONV returns nothing, falls back to NCBI's
    `esummary` endpoint, which knows every PubMed record regardless
    of PMC deposit status and returns the DOI in the `articleids`
    list. Stage 5 E2 fix: the IDCONV-only version returned (None,
    None) for any paper not in PMC, which silently broke the
    Discuss-panel PMID-click flow for journals like Brain that
    don't deposit to PMC even though PubMed clearly knows the DOI.

    Either field can still be None if the underlying record genuinely
    lacks that mapping (e.g. a paper that exists only in PubMed with
    no DOI assigned — rare for recent papers, more common for old
    ones).

    Stage 6 E7 (2026-05-26): in-process LRU cache. The PMID → (PMC,
    DOI) mapping is stable, and the same request often calls this
    twice — once in `resolve_only` for the paste-opens pre-check,
    then again in `resolve_and_extract`. Without the cache, NCBI gets
    hit twice and the second call has been observed to rate-limit
    on bursty back-to-back PMID inputs, returning empty results that
    made the paste-opens pre-check silently miss the existing saved
    paper and force a full re-extraction.
    """
    # IDCONV: fast happy-path for PMC-deposited papers.
    try:
        r = requests.get(
            IDCONV_URL,
            params={
                "ids": pmid,
                "format": "json",
                "tool": NCBI_TOOL,
                "email": NCBI_EMAIL,
            },
            timeout=15,
        )
        r.raise_for_status()
        records = r.json().get("records", [])
    except (requests.RequestException, ValueError):
        records = []
    if records:
        rec = records[0]
        pmcid = rec.get("pmcid")
        doi = rec.get("doi")
        if pmcid or doi:
            return pmcid, doi

    # IDCONV came back empty. Try esummary — the canonical PubMed
    # metadata endpoint that includes DOIs in articleids for any
    # record, not just PMC-deposited ones.
    try:
        r = requests.get(
            ESUMMARY_URL,
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "json",
                "tool": NCBI_TOOL,
                "email": NCBI_EMAIL,
            },
            timeout=15,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
    except (requests.RequestException, ValueError):
        return None, None
    rec = result.get(str(pmid)) or result.get(pmid)
    if not isinstance(rec, dict):
        return None, None
    pmcid: str | None = None
    doi: str | None = None
    for entry in rec.get("articleids", []):
        idtype = entry.get("idtype", "")
        value = (entry.get("value") or "").strip()
        if not value:
            continue
        if idtype == "pmc" and not pmcid:
            # esummary returns PMCID as e.g. "PMC1234567"; normalize.
            pmcid = value if value.upper().startswith("PMC") else f"PMC{value}"
        elif idtype == "doi" and not doi:
            doi = value
    return pmcid, doi


def pmid_to_pmcid(pmid: str) -> str | None:
    """Resolve a PMID to a PMC ID (or None if no PMC version exists).

    Kept as a thin wrapper over pmid_to_ids() for backward compat —
    new code should call pmid_to_ids() and use the DOI fallback when
    the PMC lookup returns None.
    """
    pmcid, _ = pmid_to_ids(pmid)
    return pmcid


def fetch_nxml(pmcid: str, cache_dir: Path = NXML_CACHE_DIR) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{pmcid}.xml"

    if cache_path.exists():
        print(f"  [cache hit] {cache_path}")
        return cache_path.read_text(encoding="utf-8")

    pmcid_num = pmcid.replace("PMC", "")
    url = EFETCH_URL
    params = {
        "db": "pmc",
        "id": pmcid_num,
        "tool": NCBI_TOOL,
        "email": NCBI_EMAIL,
    }
    print(f"  [fetch] NXML for {pmcid}")
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    xml = r.text
    cache_path.write_text(xml, encoding="utf-8")
    print(f"  [cached] {len(xml)} chars → {cache_path}")
    return xml


def _scrape_figure_urls(pmcid: str) -> dict[str, str]:
    """Scrape the PMC article page to build a filename → CDN URL map.

    Stage 5 E7 S7 (2026-05-19): retry once with a short backoff when
    the parsed map comes back empty. PMC's article-page endpoint is
    intermittently shielded by Cloudflare-style throttling — the
    second request a couple of seconds later often passes (observed
    on `10.3390/cimb46030169` during E7 tire-kick: first call
    succeeded, retry seconds later returned nothing, the third
    attempt after a restart succeeded). A real User-Agent is sent
    along with the request so anonymous Python looks less obviously
    bot-shaped; doesn't bypass real anti-bot, just buys back the
    "we're a browser, not a script" tier.

    Stage 6 E3 follow-up (2026-05-25): switched from `requests` to
    `httpx` for the GET. The bundled python-build-standalone
    interpreter's `requests` library produces a TLS/HTTP fingerprint
    that Cloudflare's bot detection classifies as bot traffic — same
    code with `requests` gets HTTP 403, with `httpx` gets 200. Same
    machine, same network, same User-Agent. `httpx`'s httpcore/h11
    stack looks different enough on the wire to pass. No effect on
    the dev micromamba Python (both work there).

    Returns {} on persistent failure — callers gracefully degrade to
    "no figures from PMC CDN" and let the supplement helper (S5/S6)
    rescue from the publisher PDF.
    """
    import httpx
    url = f"{PMC_ARTICLE_URL}/{pmcid}/"
    # Stage 6 E3 follow-up (2026-05-25): the regex used to hardcode
    # the requested pmcid in the URL path, but PMC's CDN sometimes
    # stores a paper's figure blobs under a *different* PMCID than
    # the article URL — observed on PMC13058728 (Nature Neuroscience
    # `10.1038/s41593-026-02226-y`) where blobs live at
    # `pmc/blobs/571a/13061623/.../...`. PMC appears to re-ID for
    # storage even when the article URL stays stable. Match any
    # PMCID-shaped digit run — still scoped to PMC's CDN domain.
    pattern = r"https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[0-9a-f]+/\d+/[0-9a-f]+/([^\"\s]+\.(?:jpg|png|gif))"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    def _try() -> dict[str, str]:
        try:
            r = httpx.get(url, timeout=30.0, headers=headers, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError:
            return {}
        out: dict[str, str] = {}
        for m in re.finditer(pattern, r.text):
            out[m.group(1)] = m.group(0)
        return out

    url_map = _try()
    if url_map:
        return url_map
    # Empty map on the first try — could be a legitimately
    # figureless page or a transient throttle. Sleep briefly and
    # retry once before giving up.
    time.sleep(1.5)
    return _try()


def parse_pmc_nxml(xml_str: str) -> dict[str, Any]:
    """Parse PMC NXML, handling the <pmc-articleset> wrapper."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_str)
    if root.tag == "pmc-articleset":
        article = root.find("article")
        if article is None:
            raise ValueError("no <article> inside <pmc-articleset>")
        xml_str = ET.tostring(article, encoding="unicode")

    return parse_jats(xml_str)


def fetch_manuscript_pdf(pmcid: str, *, patient: bool = False) -> bytes:
    """Download the canonical PMC PDF for an article.

    PMC gates PDF downloads behind a JavaScript proof-of-work
    challenge (cookie name `cloudpmc-viewer-pow`) — plain `requests`
    just gets an HTML interstitial page that says
    "Preparing to download…". We launch a Playwright session,
    navigate to the PDF URL so the JS runs and sets the cookie,
    then do an in-page fetch which inherits that cookie and
    receives the real PDF bytes.

    `patient=True` extends the POW wait from 3s to 15s. Use it on
    the user-initiated Download-manuscript path where waiting is
    acceptable; leave it False on the figure-supplement / body-
    extraction paths where 3s avoids piling latency onto papers
    whose POW never resolves (PMC author-manuscript / reCAPTCHA-
    gated cases — see Stage 6 E3 follow-up #2).
    """
    import time as _time
    from paperwhirl.browser import PlaywrightSession
    pdf_url = f"{PMC_ARTICLE_URL}/{pmcid}/pdf/"
    timeout_seconds = 15.0 if patient else 3.0
    poll_iterations = int(timeout_seconds / 0.5)
    with PlaywrightSession(pdf_url) as session:
        session._warm()  # navigates to pdf_url, JS starts the POW
        # Wait for the POW challenge to complete (cookie appears).
        # difficulty=4 typically resolves in 1-3s on a modern CPU,
        # but Stage 6 E7 tire-kick (2026-05-27) found papers like
        # NAR/PMC10250231 where POW takes 5-10s. The patient= flag
        # gives those a longer window on the user-initiated
        # download path.
        for _ in range(poll_iterations):
            cookies = session._page.context.cookies()
            if any(c["name"] == "cloudpmc-viewer-pow" for c in cookies):
                break
            _time.sleep(0.5)
        else:
            raise RuntimeError(
                f"PMC PDF POW cookie did not appear in "
                f"{timeout_seconds:.0f}s for {pmcid}"
            )
        # The POW JS may also have triggered a navigation. fetch_bytes
        # does an in-page fetch with credentials:include, so the cookie
        # will accompany the request.
        data = session.fetch_bytes(pdf_url)
        if not data[:4] == b"%PDF":
            raise RuntimeError(
                f"PMC returned non-PDF bytes for {pmcid} "
                f"(starts with {data[:20]!r})"
            )
        return data


def _extract_from_pmc_pdf(
    pmcid: str,
    doi: str | None,
    parsed: dict[str, Any],
    output_dir: Path | None,
    now: str | None = None,
) -> dict[str, Any]:
    """Stage 5 E7 S1: when PMC's NXML body is empty but PMC hosts
    the PDF (`pmc-prop-has-pdf=yes`), fetch the PDF via
    `fetch_manuscript_pdf` and build the skeleton by running the
    standard PDF-extraction pipeline. Overlay PMC NXML metadata
    onto the PDF-derived `paper` block so we get reliable
    title/authors/year/journal/DOI/PMID even when the PDF's
    first-page text is awkward to parse.

    Raises on PMC PDF fetch failure or PDF-extraction failure; the
    caller (`extract`) catches and falls back to `PMCStubError`.
    """
    from datetime import datetime, timezone
    from paperwhirl.extract import (
        build_skeleton,
        extract_pages,
        find_figures,
        parse_pdfinfo,
        render_pages,
        write_page_text,
    )

    if output_dir is None:
        raise ValueError("output_dir required for PMC PDF fallback")
    if now is None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    slug = pmcid.lower()

    print(
        f"  [pmc] {pmcid}: NXML body empty but PMC hosts PDF; "
        f"fetching from PMC PDF mirror"
    )
    pdf_bytes = fetch_manuscript_pdf(pmcid)

    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "uploaded.pdf"
    pdf_path.write_bytes(pdf_bytes)

    pdfinfo = parse_pdfinfo(pdf_path)
    pages = extract_pages(pdf_path)
    write_page_text(pages, output_dir / "extracted_text.txt")

    figures = find_figures(pages)
    pages_dir = output_dir / "pages"
    rendered_pages = render_pages(
        pdf_path,
        [item["asset_page"] for item in figures],
        pages_dir,
    )
    skel = build_skeleton(
        pdf_path, slug, output_dir, pages, pdfinfo, figures, rendered_pages,
    )

    # Overlay PMC NXML metadata. NXML is canonical; PDF first-page
    # heuristics aren't. Only overwrite when NXML actually supplies
    # the field — otherwise keep what build_skeleton produced.
    paper = skel.setdefault("paper", {})
    if parsed.get("title"):
        paper["title"] = parsed["title"]
    if parsed.get("authors"):
        paper["authors"] = parsed["authors"]
        paper["first_author"] = (
            parsed["authors"][0].split()[-1] if parsed["authors"] else ""
        )
    if parsed.get("year") is not None:
        paper["year"] = parsed["year"]
    if parsed.get("journal"):
        paper["journal"] = parsed["journal"]
    paper["doi"] = doi or parsed.get("doi") or paper.get("doi")
    if parsed.get("pmid"):
        paper["pmid"] = parsed["pmid"]
    paper["pmcid"] = pmcid
    paper["publication_state"] = "published"
    if "session" in skel:
        skel["session"]["title"] = paper.get("title", skel["session"].get("title"))
    return skel


def extract(
    pmcid: str,
    doi: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Full extraction: fetch NXML, parse, fetch figures, build skeleton."""
    from datetime import datetime, timezone
    from paperwhirl import stage

    with stage("pmc.fetch_nxml"):
        xml = fetch_nxml(pmcid)
    with stage("pmc.parse_nxml"):
        parsed = parse_pmc_nxml(xml)

    slug = pmcid.lower()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    out = output_dir or Path("/tmp") / slug
    out.mkdir(parents=True, exist_ok=True)

    with stage("pmc.scrape_fig_urls"):
        figure_urls = _scrape_figure_urls(pmcid)

    figures_dir = out / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    walkthrough_figures = []
    source_figures = []
    _fig_loop_start = time.monotonic()
    # Use 1-indexed position in the figure list for filenames / ids so
    # papers with duplicate labels (e.g. two Figure 2's, no Figure 1)
    # don't collide on disk. The human label stays as-is.
    for idx, fig in enumerate(parsed["figures"]):
        slot = idx + 1
        source_id = f"source_fig_{slot}"
        asset = None

        graphic_href = fig.get("graphic_href", "")
        candidates = [
            f"{graphic_href}.jpg",
            f"{graphic_href}.png",
            graphic_href,
        ]
        fig_url = None
        for candidate in candidates:
            if candidate in figure_urls:
                fig_url = figure_urls[candidate]
                break

        if fig_url:
            dest = figures_dir / f"figure_{slot}.jpg"
            try:
                img_resp = requests.get(fig_url, timeout=30)
                img_resp.raise_for_status()
                dest.write_bytes(img_resp.content)
                asset = str(dest.relative_to(out))
                print(f"    [fig] {graphic_href} → {dest.name}")
            except Exception as exc:
                print(f"    [fig] {graphic_href} fetch failed: {exc}")
        else:
            print(f"    [fig] no CDN URL found for {graphic_href}")

        source_figures.append({
            "id": source_id,
            "figure": fig["label"],
            "page": None,
            "asset": asset,
            "caption": fig["caption_text"],
            "extraction_method": "pmc_nxml+pmc_cdn",
        })
        walkthrough_figures.append({
            "id": f"figure_{slot}",
            "order": slot,
            "source_order": fig.get("source_order", slot),
            "label": fig["label"],
            "source_figure_id": source_id,
            "view": {
                "source_page": None,
                "asset": asset,
                "asset_role": "pmc_cdn" if asset else "missing",
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
    print(f"  [timing] pmc.fetch_figures: {time.monotonic() - _fig_loop_start:.2f}s")

    # Tables (from parse_jats <table-wrap> walker). Caption + raw HTML
    # for structured tables; `view.asset` (an image fetched off the
    # PMC CDN) for tables deposited as graphics (JCI-style). The model
    # fills only `analysis.interpretation` downstream.
    #
    # Stage 6 E7 tire-kick fix (2026-05-26): sort by extracted table
    # number before enumerating. PMC stores some publishers' tables in
    # citation-order (T3, T5, T2, T1, ... — observed on JCI/PMC12126230)
    # rather than positional order; without this sort the UI renders
    # them in that wrong order.
    walkthrough_tables: list[dict[str, Any]] = []
    source_tables: list[dict[str, Any]] = []
    sorted_tables = sorted(
        parsed.get("tables", []),
        key=lambda t: (t.get("number") or 0, t.get("source_order") or 0),
    )
    for tidx, tbl in enumerate(sorted_tables):
        tslot = tidx + 1
        source_table_id = f"source_tbl_{tslot}"
        # Same fetch dance as figures above — handles JCI's "table is a
        # graphic" deposit pattern. Empty graphic_href on bioRxiv-style
        # `<table>` markup just falls through with asset=None.
        table_asset = None
        tbl_graphic_href = tbl.get("graphic_href", "")
        if tbl_graphic_href:
            candidates = [
                f"{tbl_graphic_href}.jpg",
                f"{tbl_graphic_href}.png",
                tbl_graphic_href,
            ]
            tbl_url = None
            for candidate in candidates:
                if candidate in figure_urls:
                    tbl_url = figure_urls[candidate]
                    break
            if tbl_url:
                dest = figures_dir / f"table_{tslot}.jpg"
                try:
                    img_resp = requests.get(tbl_url, timeout=30)
                    img_resp.raise_for_status()
                    dest.write_bytes(img_resp.content)
                    table_asset = str(dest.relative_to(out))
                    print(f"    [tbl] {tbl_graphic_href} → {dest.name}")
                except Exception as exc:
                    print(f"    [tbl] {tbl_graphic_href} fetch failed: {exc}")
            else:
                print(f"    [tbl] no CDN URL found for {tbl_graphic_href}")

        if tbl["html"]:
            asset_role = "pmc_jats"
        elif table_asset:
            asset_role = "pmc_cdn"
        else:
            asset_role = "missing"

        source_tables.append({
            "id": source_table_id,
            "table": tbl["label"],
            "caption": tbl["caption_text"],
            "html": tbl["html"],
            "asset": table_asset,
            "extraction_method": (
                "pmc_nxml+pmc_cdn" if table_asset else "pmc_nxml"
            ),
        })
        walkthrough_tables.append({
            "id": f"table_{tslot}",
            "order": tslot,
            # Stage 6 E7 (2026-05-26): override source_order to match
            # the post-sort table position. The PacketView re-sorts by
            # source_order, so if we keep the original document-order
            # value (which is what was wrong for this paper) the
            # frontend just un-sorts the fix. 9999 base keeps tables
            # after all figures; +tslot preserves the number-sorted
            # order we just established.
            "source_order": 9999 + tslot,
            "label": tbl["label"],
            "source_table_id": source_table_id,
            "view": {
                "caption_text": tbl["caption_text"],
                "html": tbl["html"],
                "asset": table_asset,
                "asset_role": asset_role,
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

    # Stage 5 E3 fix #1+#2 (plus E7 S1 extension): detect "PMC has
    # no useful XML content" before writing the empty packet to disk.
    # Three patterns now:
    #   - Explicit license-restriction stub (SMURF / PMC12154772):
    #     PMC's NXML body has a single paragraph saying the author
    #     opted out of PMC archiving. Matches _PMC_STUB_PHRASE.
    #     PMC has neither body nor PDF → raise PMCStubError.
    #   - No <body> element at all (Peng et al. 2019 *Cell Research*
    #     / PMC6796938): PMC has only the front-matter + abstract;
    #     the full text was never deposited and no PDF either.
    #     → raise PMCStubError.
    #   - Empty body BUT PMC hosts a PDF (Frazier et al. 2019 *JBC* /
    #     PMC6462508 — explicit `pmc-prop-has-pdf=yes` custom-meta,
    #     plus a `<!-- ... do not allow downloading of the full text
    #     in XML form -->` comment). Use the PMC-hosted PDF as the
    #     source: fetch it via `fetch_manuscript_pdf` (POW-gated, but
    #     already wired for the manuscript-download button), run the
    #     standard PDF extraction, and overlay the NXML front-matter
    #     metadata onto the PDF-derived skeleton. Adds JBC and other
    #     PDF-only PMC deposits without writing a new family
    #     extractor.
    stub_phrase_hit = _PMC_STUB_PHRASE in body_text.lower()
    empty_content = not parsed["sections"] and not parsed["figures"]
    if stub_phrase_hit:
        raise PMCStubError(
            f"PMC has only license-restriction stub for {pmcid}",
            doi=doi or parsed.get("doi"),
        )
    if empty_content:
        if parsed.get("has_pmc_pdf"):
            try:
                return _extract_from_pmc_pdf(
                    pmcid, doi or parsed.get("doi"), parsed, output_dir, now
                )
            except Exception as exc:
                # PDF fetch / extraction failed — fall back to the
                # existing stub path so the resolver can try the
                # publisher / preprint branches as a last resort.
                print(
                    f"  [pmc] {pmcid}: NXML empty + pmc-prop-has-pdf but "
                    f"PDF path failed ({type(exc).__name__}: {exc}); "
                    f"falling through"
                )
        raise PMCStubError(
            f"PMC has only empty body (PMC has metadata + abstract only) for {pmcid}",
            doi=doi or parsed.get("doi"),
        )

    if output_dir:
        (output_dir / "extracted_text.txt").write_text(body_text, encoding="utf-8")

    title = parsed["title"]
    authors = parsed["authors"]
    paper_doi = doi or parsed.get("doi")

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage2_e9",
            "created_at": now,
            "updated_at": now,
            "mode": "full",
            "title": title,
        },
        "paper": {
            "id": slug,
            "title": title,
            "abstract": parsed.get("abstract", ""),
            "authors": authors,
            "first_author": authors[0].split()[-1] if authors else "",
            "year": parsed.get("year"),
            "journal": parsed.get("journal"),
            "doi": paper_doi,
            "pmid": parsed.get("pmid"),
            "pmcid": pmcid,
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
        # Stage 5 E8 S1: parsed bibliography for the lookup_reference
        # Discuss tool. Not surfaced in the packet UI or per-section
        # generation — tool-only.
        "references": parsed.get("references", []),
        "exports": {
            "pdf": {
                "path": None,
                "created_at": None,
            }
        },
    }
