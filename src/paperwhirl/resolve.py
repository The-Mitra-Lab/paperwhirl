"""Source-resolution pipeline.

Given any input (PDF file, DOI, URL, PMID, PMCID, or free-text identifier),
detect the paper family and resolve to the best available structured source.

Pipeline order:
  collect DOI / arXiv id / PMCID
    -> PMC NXML
    -> direct bioRxiv preprint  (10.1101/ only — never as a substitute
       for a paywalled published DOI)
    -> direct arXiv preprint    (10.48550/arXiv.* or raw arXiv id)
    -> Nature/Springer HTML
    -> Cell-family HTML         (OA on cluster; paywalled also from
       subscribing IP — laptop deployment)
    -> Science-family HTML      (sciadv on cluster; paywalled from
       subscribing IP)
    -> PDF fallback (for PDF uploads only)
    -> not reachable

Public surface:
  resolve_and_extract(input_str_or_path, output_dir) -> (skeleton_dict, slug)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from paperwhirl.arxiv import (
    extract as arxiv_extract,
    is_arxiv_doi,
    is_arxiv_id,
    doi_to_arxiv_id,
    ArxivNoHTML,
)
from paperwhirl.biorxiv import (
    BiorxivSession,
    extract as biorxiv_extract,
    is_biorxiv,
    resolve_doi_by_title,
)
from paperwhirl.cell import (
    extract as cell_extract,
    is_cell_doi,
    CellPaywallStub,
)
from paperwhirl.elife import extract as elife_extract, is_elife_doi
from paperwhirl.frontiers import (
    extract as frontiers_extract,
    is_frontiers_doi,
)
from paperwhirl.nature import extract as nature_extract, is_nature_doi
from paperwhirl.oxford import (
    OxfordPaywallStub,
    extract as oxford_extract,
    is_oxford_doi,
)
from paperwhirl.jneurosci import (
    JNeurosciPaywallStub,
    extract as jneurosci_extract,
    is_jneurosci_doi,
)
from paperwhirl.plos import (
    extract as plos_extract,
    is_plos_doi,
)
from paperwhirl.royalsoc import (
    RoyalSocPaywallStub,
    extract as royalsoc_extract,
    is_royalsoc_doi,
)
from paperwhirl.pnas import (
    PNASPaywallStub,
    extract as pnas_extract,
    is_pnas_doi,
)
from paperwhirl.pmc import (
    PMCStubError,
    doi_to_pmcid,
    extract as pmc_extract,
    pmid_to_ids,
)
from paperwhirl.science import (
    extract as science_extract,
    is_science_doi,
    SciencePaywallStub,
)
from paperwhirl.generate import extract_pdf_to_skeleton, slugify


_DOI_RE = re.compile(r"(10\.\d{4,}/\S+)")
_PDF_DOI_RE = re.compile(r"(?:doi|DOI)[:\s]*(?:https?://doi\.org/)?(10\.\d{4,}/[^\s,;\"']+)")
_PMCID_RE = re.compile(r"PMC\d+", re.IGNORECASE)
_PMID_RE = re.compile(r"^\d{6,9}$")

_URL_DOI_RE = re.compile(r"doi\.org/(10\.\d{4,}/\S+)")
_URL_PMCID_RE = re.compile(r"pmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d+)", re.IGNORECASE)
_URL_PUBMED_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
# bioRxiv URL — capture the DOI directly after `/content/`. The old
# regex assumed an intermediate path segment (legacy `…/content/
# biorxiv/early/…`); modern URLs go straight from `content/` to the
# DOI. Optional `(?:[^/]+/)*?` handles the legacy form too. The
# version suffix `v\d+` IS part of the canonical bioRxiv DOI and
# stays in the capture; any trailing `.full` / `.full.pdf` /
# `.abstract` / `.figures-only` / `.supplementary-material` segments
# are NOT part of the DOI and are excluded.
_URL_BIORXIV_RE = re.compile(
    r"biorxiv\.org/content/(?:[^/]+/)*?"
    r"(10\.\d{4,}/[^/?#\s]+?)"
    r"(?:\.(?:full|abstract|figures-only|supplementary-material))?"
    r"(?:\.pdf)?"
    r"(?:[/?#]|$)"
)
_URL_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|html|pdf)/(\S+?)(?:\.pdf)?$")

# Case-insensitive identifier-prefix labels users commonly paste in
# from PubMed / bioRxiv / arXiv pages or citation managers. The
# normalizer strips these (and any leading whitespace/colon) before
# the classifier sees the input — otherwise `PMID: 12345678` etc.
# fall through to "unknown" because the bare PMID regex requires the
# whole string to be digits.
_PREFIX_RE = re.compile(
    r"^(?:pmid|pmcid|doi|arxiv)\s*[:\s]\s*", re.IGNORECASE
)

# Trailing publisher URL-path suffixes that aren't part of a DOI.
# When the general DOI fallback fires on a bioRxiv-like URL (or any
# URL containing a DOI followed by these suffixes), strip them off
# the captured DOI so we don't ship garbage to downstream resolvers.
_DOI_TRAILING_SUFFIX_RE = re.compile(
    r"\.(?:full|abstract|figures-only|supplementary-material)(?:\.pdf)?$",
    re.IGNORECASE,
)


def _normalize_input(raw: str) -> str:
    """Strip whitespace, common identifier prefixes, and URL query /
    fragment so the classifier sees a clean string. Repeats the
    prefix strip in case the user pasted multiple prefixes (rare but
    cheap to handle)."""
    s = raw.strip()
    # Strip case-insensitive prefixes like "PMID:", "DOI:",
    # "PMCID:", "arXiv:" — including the surrounding whitespace.
    for _ in range(3):
        new = _PREFIX_RE.sub("", s, count=1).strip()
        if new == s:
            break
        s = new
    # For URLs, drop the query string and fragment before regex
    # matching — they only confuse the per-family regexes.
    if s.startswith(("http://", "https://")):
        if "#" in s:
            s = s.split("#", 1)[0]
        if "?" in s:
            s = s.split("?", 1)[0]
    return s


def _parse_input(raw: str) -> tuple[str, str]:
    """Classify the input string and extract the identifier.

    Returns (input_type, identifier) where input_type is one of:
    'pdf', 'pmcid', 'pmid', 'doi', 'arxiv_id', 'unknown'.
    """
    # PDF path check happens on the raw (pre-normalization) value so
    # a literal path like "PMID.pdf" isn't accidentally normalized.
    raw_stripped = raw.strip()
    if Path(raw_stripped).suffix.lower() == ".pdf" and Path(raw_stripped).exists():
        return "pdf", raw_stripped

    raw = _normalize_input(raw)

    m = _URL_PMCID_RE.search(raw)
    if m:
        return "pmcid", m.group(1).upper()

    m = _URL_PUBMED_RE.search(raw)
    if m:
        return "pmid", m.group(1)

    m = _URL_BIORXIV_RE.search(raw)
    if m:
        return "doi", m.group(1)

    m = _URL_ARXIV_RE.search(raw)
    if m:
        return "arxiv_id", m.group(1)

    m = _URL_DOI_RE.search(raw)
    if m:
        return "doi", m.group(1).rstrip(".")

    if _PMCID_RE.fullmatch(raw):
        return "pmcid", raw.upper()

    if _PMID_RE.fullmatch(raw):
        return "pmid", raw

    m = _DOI_RE.search(raw)
    if m:
        # Strip publisher URL-path suffixes that aren't DOI characters
        # (the general regex's `\S+` is too greedy and otherwise
        # captures things like `.full.pdf`).
        captured = m.group(1).rstrip(".")
        captured = _DOI_TRAILING_SUFFIX_RE.sub("", captured)
        return "doi", captured

    if is_arxiv_id(raw):
        return "arxiv_id", raw

    return "unknown", raw


def _extract_doi_from_pdf(pdf_path: Path) -> str | None:
    """Extract the paper's own DOI from the first few pages of a PDF.

    Stage 5 E2 fix: the prior "first match wins" heuristic returned
    cited DOIs whenever a paper mentioned another paper's DOI before
    its own (e.g. a Nat Comm paper citing bin2cell in its abstract
    → returned bin2cell's DOI → extracted bin2cell instead of the
    real paper). Now we count occurrences across pages 1-3 and pick
    the most-frequent: the paper's own DOI is typically repeated in
    headers/footers / "Cite as" blocks / journal sidebars, while
    citations appear once. bioRxiv DOIs are filtered out — those
    should have been caught by `is_biorxiv` above; if we got here,
    any bioRxiv DOI mention is almost certainly a citation.
    """
    from paperwhirl.biorxiv import _pdf_text_pages, BIORXIV_DOI_PREFIXES
    text = _pdf_text_pages(pdf_path, first=1, last=3)

    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for m in _DOI_RE.finditer(text):
        doi = m.group(1).rstrip(".,;:)")
        if any(doi.startswith(p) for p in BIORXIV_DOI_PREFIXES):
            continue
        if doi not in counts:
            first_seen[doi] = m.start()
        counts[doi] = counts.get(doi, 0) + 1

    if not counts:
        return None

    # Most-frequent wins. Tie-break: prefer the one seen earlier
    # (lower start offset). For single-occurrence ties, this falls
    # back to the same behavior as the original first-match heuristic.
    return max(
        counts,
        key=lambda d: (counts[d], -first_seen[d]),
    )


def _try_pmc_upgrade(doi: str) -> str | None:
    """Check if a DOI has a PMC version."""
    print(f"  [resolve] checking PMC for DOI {doi}")
    return doi_to_pmcid(doi)


def resolve_only(
    input_str: str,
    pdf_data: bytes | None = None,
) -> dict[str, Any]:
    """Cheap pre-resolution for the E8 paste-opens check.

    Computes the deterministic slug and a sniffed DOI for an input
    WITHOUT running figure extraction, publisher fetch, or LLM. Used
    by the streaming endpoint to decide whether a paper is already
    saved (by slug or by DOI) before paying for a full generation.

    Network calls limited to: at most one PMID → DOI/PMCID eutils
    lookup. PDF DOI sniffing reads PDF text only (no network).

    Returns:
        {"slug": str | None, "doi": str | None}
        Either or both can be None for inputs we can't pre-resolve.
        The caller checks `slug` against `is_saved` and `doi` against
        `find_by_doi`; either match means "already saved, just open
        it".

    Slug derivation (Stage 5 E6, 2026-05-18): uses the same
    `slug_for_paper` helper as `resolve_and_extract`, so pre-check
    and full extraction always produce the same slug — any
    pre-existing library entry for this paper will be detected by
    a single slug equality check (no DOI-fallback dedup pass
    needed for the DOI-present cases). The pre-check still does
    network work (PMID → eutils, PMCID → NXML fetch, PDF DOI
    sniffing) so the slug it computes matches the canonical-DOI
    slug the extractor would.
    """
    import tempfile

    from paperwhirl.slugs import slug_for_paper

    if pdf_data is not None:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_data)
            tf_path = Path(tf.name)
        try:
            doi = _extract_doi_from_pdf(tf_path)
        finally:
            try:
                tf_path.unlink()
            except OSError:
                pass
        slug = slug_for_paper(doi=doi, pdf_bytes=pdf_data)
        return {"slug": slug, "doi": doi}

    input_type, identifier = _parse_input(input_str)

    if input_type == "pdf":
        pdf_path = Path(identifier)
        try:
            doi = _extract_doi_from_pdf(pdf_path)
        except Exception:
            doi = None
        try:
            pdf_bytes = pdf_path.read_bytes()
        except OSError:
            pdf_bytes = None
        slug = slug_for_paper(doi=doi, pdf_bytes=pdf_bytes)
        return {"slug": slug, "doi": doi}

    if input_type == "pmcid":
        # Pre-fetch NXML so the slug derives from the DOI when
        # PMC has one (almost always true). Falls back to a
        # PMCID-based slug on any fetch/parse failure — that
        # only impacts dedup for the rare PMC-no-DOI case.
        doi = None
        try:
            from paperwhirl.pmc import fetch_nxml, parse_pmc_nxml
            xml = fetch_nxml(identifier)
            parsed = parse_pmc_nxml(xml)
            doi = parsed.get("doi")
        except Exception:
            pass
        slug = slug_for_paper(doi=doi, pmcid=identifier)
        return {"slug": slug, "doi": doi}

    if input_type == "arxiv_id":
        arxiv_doi = f"10.48550/arXiv.{identifier}"
        return {
            "slug": slug_for_paper(doi=arxiv_doi),
            "doi": arxiv_doi,
        }

    if input_type == "doi":
        return {
            "slug": slug_for_paper(doi=identifier),
            "doi": identifier,
        }

    if input_type == "pmid":
        try:
            pmcid, doi = pmid_to_ids(identifier)
        except Exception:
            return {"slug": None, "doi": None}
        if not (doi or pmcid):
            return {"slug": None, "doi": None}
        slug = slug_for_paper(doi=doi, pmcid=pmcid)
        return {"slug": slug, "doi": doi}

    return {"slug": None, "doi": None}


def resolve_and_extract(
    input_str: str,
    output_dir: Path,
    pdf_data: bytes | None = None,
) -> tuple[dict[str, Any], str]:
    """Resolve input to best source and extract a session skeleton.

    Args:
        input_str: A DOI, PMID, PMCID, URL, PDF path, or identifier.
        output_dir: Base directory for results.
        pdf_data: Raw PDF bytes if uploaded via the web UI.

    Returns:
        (skeleton_dict, slug)
    """
    from paperwhirl import stage
    with stage("resolve_and_extract.total"):
        return _resolve_and_extract_inner(input_str, output_dir, pdf_data)


def _resolve_and_extract_inner(
    input_str: str,
    output_dir: Path,
    pdf_data: bytes | None = None,
) -> tuple[dict[str, Any], str]:
    if pdf_data:
        return _resolve_pdf(input_str, output_dir, pdf_data)

    input_type, identifier = _parse_input(input_str)
    print(f"  [resolve] input_type={input_type}, identifier={identifier}")

    if input_type == "pdf":
        pdf_path = Path(identifier)
        pdf_data = pdf_path.read_bytes()
        return _resolve_pdf(pdf_path.stem, output_dir, pdf_data, pdf_path=pdf_path)

    if input_type == "pmcid":
        return _extract_pmc(identifier, output_dir)

    if input_type == "pmid":
        pmcid, doi = pmid_to_ids(identifier)
        if pmcid:
            try:
                return _extract_pmc(pmcid, output_dir)
            except PMCStubError as exc:
                # Stage 5 E3 fix #1: PMC has only the license-restriction
                # stub. If we have a DOI from pmid_to_ids, fall through to
                # _resolve_doi which will catch the stub again and route
                # to bioRxiv (for bioRxiv DOIs) or raise (for others with
                # no other fallback). The second PMC call inside
                # _resolve_doi is a wasted fetch but the path is simpler
                # than threading a "skip PMC" flag through.
                if not doi:
                    raise
                print(f"  [resolve] {exc}; trying DOI path with {doi}")
        if doi:
            # Paywalled / not-PMC-deposited papers: fall through to the
            # DOI path so we still try publisher HTML extractors (Nature,
            # Cell, Science) before giving up. The publisher reachability
            # depends on the running machine's network — on the WashU
            # laptop, paywalled content renders fully under the
            # laptop-first deployment model.
            return _resolve_doi(doi, output_dir)
        raise ValueError(
            f"PMID {identifier} has no PMC version and no DOI on PubMed"
        )

    if input_type == "arxiv_id":
        return _extract_arxiv(identifier, output_dir)

    if input_type == "doi":
        return _resolve_doi(identifier, output_dir)

    raise ValueError(f"Could not resolve identifier: {identifier}")


def _resolve_pdf(
    name: str,
    output_dir: Path,
    pdf_data: bytes,
    pdf_path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Resolve a PDF upload to best source.

    Preference order (Stage 5 E6, 2026-05-18):
        PMC > Nature > Cell > Science > arXiv > bioRxiv > PDF.

    Slug derivation: the PDF is sniffed for a DOI first (temp
    location); if found, slug derives from `slug_for_paper(doi=doi)`.
    If no DOI is detectable, slug = `nodoi_<sha256_prefix>` from
    the PDF bytes (stable across re-drops of the same file). So
    the same PDF uploaded twice converges on the same library
    entry regardless of filename. Pre-E6 the slug was the
    filename stem, which produced silent duplicates for the same
    PDF dropped with different filenames.
    """
    import tempfile

    from paperwhirl.slugs import slug_for_paper

    # --- Step 0: Sniff DOI from a temp copy of the PDF ---
    # We need the PDF on disk to run is_biorxiv / DOI extraction,
    # but we can't create the final paper_dir until we know the
    # slug. Use a temp file; once we have the slug, save the bytes
    # to paper_dir/uploaded.pdf and delete the temp.
    sniff_path: Path | None = pdf_path
    temp_handle = None
    if sniff_path is None:
        tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tf.write(pdf_data)
        tf.close()
        sniff_path = Path(tf.name)
        temp_handle = sniff_path

    try:
        is_biorxiv_paper, doi, reason = is_biorxiv(sniff_path)
        print(f"  [resolve] bioRxiv detection: {reason}")

        if not doi:
            doi = _extract_doi_from_pdf(sniff_path)
            if doi:
                print(f"  [resolve] extracted DOI from PDF text: {doi}")

        # Crossref title lookup may surface DOI candidates; fold them
        # into `doi` so slug derivation can use them. The Crossref
        # branch's old "try PMC upgrade per candidate" inline path
        # is dropped — once we have a DOI, the unified resolver loop
        # below tries PMC anyway as part of the standard priority
        # order.
        if not doi:
            from paperwhirl.biorxiv import _extract_page1_title
            title = _extract_page1_title(sniff_path)
            if title:
                doi_candidates = _resolve_doi_by_crossref_any(title)
                if doi_candidates:
                    doi = doi_candidates[0]
                    print(f"  [resolve] Crossref title lookup → DOI candidate {doi}")
    finally:
        # Temp file is consumed; the bytes live in pdf_data and will
        # be written to the final paper_dir below.
        if temp_handle is not None:
            try:
                temp_handle.unlink()
            except OSError:
                pass

    # --- Step 1: Compute the canonical slug and prepare paper_dir ---
    slug = slug_for_paper(doi=doi, pdf_bytes=pdf_data)
    paper_dir = output_dir / slug
    paper_dir.mkdir(parents=True, exist_ok=True)

    # Save the dropped PDF under the final paper_dir for downstream
    # use (figure-supplement fallback, manuscript download, etc.).
    # `pdf_path` is the on-disk reference the rest of the function
    # uses — point it at the final location.
    if pdf_path is None:
        pdf_path = paper_dir / "uploaded.pdf"
        pdf_path.write_bytes(pdf_data)

    # --- Step 2: Extractor cascade ---
    # Order (Stage 5 E6): PMC > Nature > Cell > Science > arXiv >
    # bioRxiv > PDF fallback. Publishers ahead of preprints because
    # published versions are canonical. Each branch catches ANY
    # exception and falls through to the next — keeps a Cloudflare
    # blip on Nature from killing the whole resolver.
    #
    # The local-PDF supplement helper (Stage 5 E2 architectural fix)
    # runs before each structured-extractor return: if the chosen
    # extractor returned 0 figures, the dropped PDF supplies them.
    def _supp(skel: dict[str, Any]) -> dict[str, Any]:
        return _supplement_figures_from_pdf(skel, paper_dir, local_pdf_path=pdf_path)

    # 1. PMC.
    if doi:
        pmcid = _try_pmc_upgrade(doi)
        if pmcid:
            print(f"  [resolve] PMC upgrade available: {pmcid}")
            try:
                skel = pmc_extract(pmcid, doi=doi, output_dir=paper_dir)
                skel["paper"]["source_pdf"] = str(pdf_path)
                return _supp(skel), slug
            except PMCStubError as exc:
                print(f"  [resolve] {exc}; falling through")
            except Exception as exc:
                print(f"  [resolve] PMC extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 1.5. eLife (Stage 6 E9 Step 1). Gold OA; clean JATS XML from
    # api.elifesciences.org. Most eLife papers index in PMC and are
    # caught above; this branch handles the new-paper gap.
    if doi and is_elife_doi(doi):
        print(f"  [resolve] trying eLife JATS for {doi}")
        try:
            skel = elife_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except Exception as exc:
            print(f"  [resolve] eLife extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 1.7. Frontiers (Stage 6 E9 Step 3). Gold OA; JATS XML via
    # frontiersin.org canonical URL + WEBP figures from
    # /files/Articles/{id}/xml-images/.
    if doi and is_frontiers_doi(doi):
        print(f"  [resolve] trying Frontiers JATS for {doi}")
        try:
            skel = frontiers_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except Exception as exc:
            print(f"  [resolve] Frontiers extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 1.8. PLOS (Stage 6 E9 follow-on). Gold OA; JATS XML via
    # signed-GCS redirect, figures via /article/figure/image.
    if doi and is_plos_doi(doi):
        print(f"  [resolve] trying PLOS JATS for {doi}")
        try:
            skel = plos_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except Exception as exc:
            print(f"  [resolve] PLOS extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 2. Nature.
    if doi and is_nature_doi(doi):
        print(f"  [resolve] trying Nature HTML extraction for {doi}")
        try:
            skel = nature_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except Exception as exc:
            print(f"  [resolve] Nature extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 3. Cell.
    if doi and is_cell_doi(doi):
        print(f"  [resolve] trying Cell HTML extraction for {doi}")
        try:
            skel = cell_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except CellPaywallStub as exc:
            print(f"  [resolve] Cell stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Cell extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4. Science.
    if doi and is_science_doi(doi):
        print(f"  [resolve] trying Science HTML extraction for {doi}")
        try:
            skel = science_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except SciencePaywallStub as exc:
            print(f"  [resolve] Science stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Science extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.5. PNAS (Stage 6 E9 Step 2).
    if doi and is_pnas_doi(doi):
        print(f"  [resolve] trying PNAS HTML for {doi}")
        try:
            skel = pnas_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except PNASPaywallStub as exc:
            print(f"  [resolve] PNAS stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] PNAS extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.7. Oxford Academic / Silverchair (Stage 6 E9 Step 4).
    if doi and is_oxford_doi(doi):
        print(f"  [resolve] trying Oxford HTML for {doi}")
        try:
            skel = oxford_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except OxfordPaywallStub as exc:
            print(f"  [resolve] Oxford stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Oxford extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.8. JNeurosci (Stage 6 E9 follow-on). Highwire HTML.
    if doi and is_jneurosci_doi(doi):
        print(f"  [resolve] trying JNeurosci HTML for {doi}")
        try:
            skel = jneurosci_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except JNeurosciPaywallStub as exc:
            print(f"  [resolve] JNeurosci stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] JNeurosci extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.9. Royal Society (Stage 6 E9 follow-on). Atypon HTML.
    if doi and is_royalsoc_doi(doi):
        print(f"  [resolve] trying Royal Society HTML for {doi}")
        try:
            skel = royalsoc_extract(doi, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except RoyalSocPaywallStub as exc:
            print(f"  [resolve] Royal Society stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Royal Society extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 5. arXiv.
    if doi and is_arxiv_doi(doi):
        arxiv_id = doi_to_arxiv_id(doi)
        print(f"  [resolve] trying arXiv HTML for {arxiv_id}")
        try:
            skel = arxiv_extract(arxiv_id, output_dir=paper_dir)
            skel["paper"]["source_pdf"] = str(pdf_path)
            return _supp(skel), slug
        except ArxivNoHTML as exc:
            print(f"  [resolve] arXiv HTML unavailable: {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] arXiv extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 6. bioRxiv (preprint).
    if is_biorxiv_paper and doi:
        print(f"  [resolve] using bioRxiv JATS for {doi}")
        try:
            with BiorxivSession() as bsession:
                skel = biorxiv_extract(pdf_path, output_dir=paper_dir, session=bsession)
            return _supp(skel), slug
        except Exception as exc:
            print(f"  [resolve] bioRxiv extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 7. PDF fallback (last resort).
    print(f"  [resolve] falling back to PDF extraction")
    skeleton_path = extract_pdf_to_skeleton(pdf_path, output_dir, slug)
    from paperwhirl.generate import load_yaml
    skel = load_yaml(skeleton_path)

    # Stage 6 E8 Step 1: enrich the skeleton with Crossref
    # metadata when we have a DOI. extract.build_skeleton hard-
    # codes doi/year/journal=None and uses a fragile heuristic
    # for title/authors; Crossref returns canonical values for
    # any publisher with a DOI. We always set the DOI itself
    # (even if Crossref fails) so the saved paper is at least
    # identifiable; Crossref-derived fields are gated on a
    # successful fetch.
    if doi:
        skel["paper"]["doi"] = doi
        meta = _fetch_crossref_metadata(doi)
        if meta:
            print(
                f"  [crossref] enriched generic-PDF skeleton: "
                f"title={'y' if meta.get('title') else 'n'} "
                f"authors={len(meta.get('authors') or [])} "
                f"year={meta.get('year')} "
                f"journal={meta.get('journal')!r}"
            )
            if meta.get("title"):
                skel["paper"]["title"] = meta["title"]
                skel["session"]["title"] = meta["title"]
            if meta.get("authors"):
                skel["paper"]["authors"] = meta["authors"]
                skel["paper"]["first_author"] = (
                    meta["authors"][0].split()[-1]
                )
            if meta.get("year"):
                skel["paper"]["year"] = meta["year"]
            if meta.get("journal"):
                skel["paper"]["journal"] = meta["journal"]

    return skel, slug


def _has_usable_figures(skel: dict[str, Any]) -> bool:
    """Stage 5 E7 S5 (2026-05-19): "Does this skeleton have at least
    one figure with an actual image asset on disk?" Distinguishes
    "PMC NXML had figure refs but the CDN fetch found nothing" (e.g.
    Novarino 2014 Science / PMC4157572 — 4 captions, 0 assets) from
    "PMC returned real figures." The supplement helper and the
    resolver's PMC short-circuit both need this — without it, a
    truthy-but-asset-less figures list blocks the PDF supplement and
    the user sees broken figure cards.

    A figure counts as usable when `view.asset` is a non-empty
    string. The PMC extractor sets `view.asset_role = "missing"`
    explicitly in this case, but checking `asset` is enough and is
    forward-compatible with any extractor that just leaves it None.
    """
    for fig in skel.get("figures") or []:
        view = fig.get("view") or {}
        if view.get("asset"):
            return True
    return False


def fetch_publisher_pdf_bytes(
    paper_meta: dict[str, Any],
    errors: list[str] | None = None,
    *,
    patient: bool = False,
) -> bytes | None:
    """Try to fetch a publisher PDF given a paper's identifier(s).

    Returns the PDF bytes on success, None on any failure. Tries
    each identifier source in priority order (bioRxiv > PMC >
    arXiv > eLife > Frontiers > PLOS > Nature > Cell > Science
    > PNAS > Oxford > JNeurosci > Royal Society). Used by BOTH
    the /api/papers/{slug}/manuscript.pdf endpoint and the
    `_supplement_figures_from_pdf` extraction-layer fallback —
    same dispatch, two callers.

    Stage 6 E7 follow-up (2026-05-27): the manuscript endpoint
    had drifted to a duplicate `_fetch_manuscript_pdf_by_family`
    helper in server.py that knew only the original 6 families
    (PMC/arXiv/bioRxiv/Nature/Cell/Science) — none of the
    Stage 6 E9 publishers (eLife/Frontiers/PLOS/PNAS/Oxford/
    JNeurosci/Royal Society) were in it, so downloads for those
    families silently fell out of the cascade and returned 404.
    Caught when an Oxford NAR paper's PMC POW timed out and the
    Oxford branch was never reached. Consolidated here; the
    `errors` accumulator lets the manuscript endpoint surface a
    detailed reason in its 404 response.

    Errors per source are logged AND appended to `errors` (when
    provided). The caller decides whether None is fatal
    (server.py turns it into a 404 with the joined errors) or
    no-op (the supplement helper falls back to "no figures").
    """
    import sys as _sys
    from paperwhirl.biorxiv import _is_biorxiv_doi

    def _try(label: str, fn):
        try:
            return fn()
        except Exception as exc:
            msg = f"{label}: {type(exc).__name__}: {exc}"
            print(
                f"  [publisher-pdf] {msg}",
                file=_sys.stderr,
                flush=True,
            )
            if errors is not None:
                errors.append(msg)
            return None

    doi = paper_meta.get("doi") or ""
    biorxiv_doi = paper_meta.get("biorxiv_doi") or (
        doi if _is_biorxiv_doi(doi) else None
    )

    if biorxiv_doi:
        from paperwhirl.biorxiv import fetch_manuscript_pdf as biorxiv_fetch
        data = _try(f"bioRxiv ({biorxiv_doi})", lambda: biorxiv_fetch(biorxiv_doi))
        if data:
            return data

    pmcid = paper_meta.get("pmcid")
    if pmcid:
        from paperwhirl.pmc import fetch_manuscript_pdf as pmc_fetch
        # `patient` extends PMC's POW-cookie wait from 3s to 15s
        # on the user-initiated download path (Stage 6 E7
        # follow-up: NAR/PMC10250231 needed 5-10s).
        data = _try(f"PMC ({pmcid})", lambda: pmc_fetch(pmcid, patient=patient))
        if data:
            return data

    arxiv_id = paper_meta.get("arxiv_id")
    if arxiv_id:
        from paperwhirl.arxiv import fetch_manuscript_pdf as arxiv_fetch
        data = _try(f"arXiv ({arxiv_id})", lambda: arxiv_fetch(arxiv_id))
        if data:
            return data

    if is_elife_doi(doi):
        from paperwhirl.elife import fetch_manuscript_pdf as elife_fetch
        data = _try(f"eLife ({doi})", lambda: elife_fetch(doi))
        if data:
            return data
    if is_frontiers_doi(doi):
        from paperwhirl.frontiers import fetch_manuscript_pdf as frontiers_fetch
        data = _try(f"Frontiers ({doi})", lambda: frontiers_fetch(doi))
        if data:
            return data
    if is_plos_doi(doi):
        from paperwhirl.plos import fetch_manuscript_pdf as plos_fetch
        data = _try(f"PLOS ({doi})", lambda: plos_fetch(doi))
        if data:
            return data
    if doi.startswith("10.1038/"):
        from paperwhirl.nature import fetch_manuscript_pdf as nature_fetch
        data = _try(f"Nature ({doi})", lambda: nature_fetch(doi))
        if data:
            return data
    if doi.startswith("10.1016/"):
        from paperwhirl.cell import fetch_manuscript_pdf as cell_fetch
        data = _try(f"Cell ({doi})", lambda: cell_fetch(doi))
        if data:
            return data
    if doi.startswith("10.1126/"):
        from paperwhirl.science import fetch_manuscript_pdf as science_fetch
        data = _try(f"Science ({doi})", lambda: science_fetch(doi))
        if data:
            return data
    if is_pnas_doi(doi):
        from paperwhirl.pnas import fetch_manuscript_pdf as pnas_fetch
        data = _try(f"PNAS ({doi})", lambda: pnas_fetch(doi))
        if data:
            return data
    if is_oxford_doi(doi):
        from paperwhirl.oxford import fetch_manuscript_pdf as oxford_fetch
        data = _try(f"Oxford ({doi})", lambda: oxford_fetch(doi))
        if data:
            return data
    if is_jneurosci_doi(doi):
        from paperwhirl.jneurosci import fetch_manuscript_pdf as jneurosci_fetch
        data = _try(f"JNeurosci ({doi})", lambda: jneurosci_fetch(doi))
        if data:
            return data
    if is_royalsoc_doi(doi):
        from paperwhirl.royalsoc import fetch_manuscript_pdf as royalsoc_fetch
        data = _try(f"Royal Society ({doi})", lambda: royalsoc_fetch(doi))
        if data:
            return data
    # Note: MDPI (10.3390/) is intentionally NOT here. Its PDF endpoint
    # is WAF-blocked; figure recovery goes through
    # `mdpi.extract_figures` (article-page image scrape) in
    # `_supplement_figures_from_pdf` instead — see Stage 5 E7 S13.

    return None


def _supplement_figures_from_pdf(
    skel: dict[str, Any],
    paper_dir: Path,
    local_pdf_path: Path | None = None,
) -> dict[str, Any]:
    """Stage 5 E2 tire-kick (2026-05-18): if a structured extractor
    (PMC NXML, Nature HTML, etc.) returned a skeleton with no figures,
    try to fill them in from the PDF.

    Stage 5 E10 S1 (2026-05-21): extended to handle **partial
    coverage**. When the structured extractor returns some figures
    with assets and some without (e.g. bioRxiv CDN serves F1/F2/F4
    but 404s on F3 for the EGR1 paper), match the PDF's figures by
    `order` and swap them into only the missing slots — preserving
    the structured caption / source_figure_id / analysis on slots
    that worked. The old all-or-nothing gate (`_has_usable_figures`)
    let partial failures ship silently with broken figure cards.

    Priority for PDF source:
    1. `local_pdf_path` — explicitly-provided PDF. Used by the
       user-dropped-PDF flow where the PDF lives at a path the
       caller knows (often different from `paper_dir`).
    2. `paper_dir/uploaded.pdf` — convention path the resolver
       writes to when the user dropped a PDF in the same dir.
    3. Publisher PDF fetched via `fetch_publisher_pdf_bytes`. Slow
       (Playwright Cloudflare warm-up) but works for click-to-extract
       flows where there is no local PDF.

    On failure (no PDF available, no figures extractable from the PDF),
    returns `skel` unchanged — the packet ships figureless rather than
    erroring. The user sees what we have; nothing breaks.

    Pre-existing skeletons whose figures are all already present pass
    through this function as fast no-ops.
    """
    from paperwhirl import extract as e2

    existing = skel.get("figures") or []
    missing_indices = [
        i for i, f in enumerate(existing)
        if not ((f.get("view") or {}).get("asset"))
    ]

    # Fast no-op: structured extractor returned figures and every one
    # has an asset. Nothing to do.
    if existing and not missing_indices:
        return skel

    # Did the source reference any figures at all? Gates the
    # user-visible "try VPN" warning (E7 S11): zero refs implies the
    # paper genuinely has none, so the warning would be a false alarm.
    had_figure_refs = bool(existing)

    # When ALL slots are missing assets, fall back to the original
    # wholesale-replace behavior — the placeholders carry no usable
    # info, so let the PDF extractor produce a self-consistent figures
    # list with proper ids/orders rather than half-merging onto empty
    # slots. When SOME slots are missing, partial-fill below preserves
    # the working slots' captions and metadata.
    all_missing_with_placeholders = (
        bool(existing) and len(missing_indices) == len(existing)
    )
    if all_missing_with_placeholders:
        print(
            f"  [supplement] {paper_dir.name}: clearing "
            f"{len(existing)} placeholder figure(s) "
            f"(all assets missing)"
        )
        skel["figures"] = []
        existing = []
        missing_indices = []

    paper_meta = skel.get("paper", {}) or {}
    slug = paper_dir.name

    # Stage 5 E7 S13: MDPI papers can't be recovered via the publisher
    # PDF (WAF-blocked), but the article page's figure images ARE
    # fetchable. Try the HTML figure scrape directly — no PDF involved.
    doi = paper_meta.get("doi") or ""
    if doi.startswith("10.3390/"):
        from paperwhirl.mdpi import extract_figures as mdpi_extract_figures
        mdpi_figs = mdpi_extract_figures(doi, paper_dir)
        if mdpi_figs:
            skel["figures"] = mdpi_figs
            print(f"  [supplement] {slug}: merged {len(mdpi_figs)} figures from MDPI article page")
            return skel
        # No figures recovered from MDPI either — fall through to the
        # generic PDF path (likely also fails for MDPI) and then the
        # S11 warning logic.

    # Stage 6 E3 follow-up (2026-05-25): if Poppler's pdftotext isn't on
    # PATH, the PDF figure extractor (extract.py) will fail regardless of
    # whether we manage to fetch a PDF. Skip the whole fetch waterfall
    # (PMC POW + Cloudflare warmup + E11 fall-through can eat 30-60s)
    # and bail straight to the figureless-with-warning state. The check
    # is a no-op once Stage 6 E4 lands and pdftotext is bundled.
    import shutil as _shutil
    if _shutil.which("pdftotext") is None:
        print(
            f"  [supplement] {slug}: pdftotext not available — skipping "
            f"publisher PDF supplement (Stage 6 E4 will close this gap)"
        )
        if had_figure_refs and not skel.get("warnings"):
            skel["warnings"] = [
                "Figures couldn't be rendered: the bundled app is missing "
                "the PDF figure extractor (Stage 6 E4). Source had figure "
                "references but they couldn't be downloaded from the "
                "publisher CDN."
            ]
        return skel

    pdf_path: Path | None = None
    if local_pdf_path is not None and local_pdf_path.exists():
        pdf_path = local_pdf_path
        print(f"  [supplement] {slug}: using caller-provided PDF for figures")
    elif (paper_dir / "uploaded.pdf").exists():
        pdf_path = paper_dir / "uploaded.pdf"
        print(f"  [supplement] {slug}: using uploaded.pdf for figures")
    else:
        if existing:
            print(
                f"  [supplement] {slug}: {len(missing_indices)} of "
                f"{len(existing)} figure(s) missing assets; fetching "
                f"publisher PDF to fill the gaps"
            )
        else:
            print(f"  [supplement] {slug}: structured extractor found 0 figures; fetching publisher PDF")
        pdf_bytes = fetch_publisher_pdf_bytes(paper_meta)
        if not pdf_bytes and paper_meta.get("doi"):
            # Stage 5 E11 (2026-05-23): generic PDF fall-through for
            # any publisher without a dedicated `fetch_manuscript_pdf`
            # (Wiley, OUP, JCI, PNAS, eLife, ...). Also catches Cracco-
            # style cases where the family fetcher exists but blocks
            # (PMC PDF reCAPTCHA): Unpaywall + citation_pdf_url often
            # surfaces a working URL on the publisher's own domain that
            # the Playwright session can reach with VPN-inherited
            # institutional access.
            print(f"  [supplement] {slug}: family fetcher returned no PDF; trying generic E11 fall-through")
            from paperwhirl.pdf_fetch import fetch_pdf_for_unknown_publisher
            pdf_bytes = fetch_pdf_for_unknown_publisher(paper_meta["doi"])
        if not pdf_bytes:
            # Stage 5 E3 (2026-05-18): record a user-visible warning
            # on the packet so the frontend can surface a banner. The
            # silent-figureless-packet case (Peng on non-VPN network)
            # made it look like the paper just has no figures, which
            # is misleading — the figures exist; we couldn't reach
            # them. Distinct from log-only output because the user
            # can act on this (turn on VPN, try a different network).
            # E7 S11: only warn when the source actually referenced
            # figures; otherwise the paper probably has none and the
            # "try VPN" message is a false alarm.
            if had_figure_refs:
                print(f"  [supplement] {slug}: no publisher PDF available — shipping figureless (figures referenced; warning)")
                skel.setdefault("warnings", []).append(
                    "Figures couldn't be rendered: your network may not have "
                    "access to this publisher. Try connecting to your "
                    "institution's VPN and re-extracting."
                )
            else:
                print(f"  [supplement] {slug}: no figure references in source and no publisher PDF — shipping figureless (paper likely has none)")
            return skel
        pdf_path = paper_dir / "supplement.pdf"
        try:
            pdf_path.write_bytes(pdf_bytes)
        except OSError as exc:
            print(f"  [supplement] {slug}: could not save supplement PDF: {exc}")
            skel.setdefault("warnings", []).append(
                f"Figures couldn't be rendered: failed to save publisher PDF ({exc})."
            )
            return skel

    figures = e2.extract_figures_only_from_pdf(pdf_path, paper_dir)
    if not figures:
        print(f"  [supplement] {slug}: PDF figure extraction returned 0 figures")
        # E7 S11: same gating as the no-PDF branch. If the source
        # referenced figures we expected to find them and couldn't —
        # surface the parser-shortfall note. If it referenced none,
        # the PDF confirming zero figures is just corroboration that
        # the paper has none; ship silently.
        if had_figure_refs:
            skel.setdefault("warnings", []).append(
                "Figures couldn't be rendered: the PDF didn't contain any "
                "figures we could parse. Some papers (e.g. theory / commentary) "
                "genuinely have none; for others this may be a parser shortfall."
            )
        return skel

    # Partial-coverage path (E10 S1): structured extractor returned
    # some figures with assets, some without. Match PDF figures by
    # `order` and swap into the missing slots only.
    if existing:
        by_order = {pf.get("order"): pf for pf in figures}
        filled = 0
        source_figs = (skel.get("extracted_source") or {}).get("figures") or []
        source_by_id = {sf.get("id"): sf for sf in source_figs}
        for idx in missing_indices:
            slot = existing[idx]
            order = slot.get("order")
            pf = by_order.get(order)
            if not pf:
                continue
            pv = pf.get("view") or {}
            sv = slot.setdefault("view", {})
            sv["asset"] = pv.get("asset")
            sv["asset_role"] = "pdf_supplement"
            if not sv.get("source_page"):
                sv["source_page"] = pv.get("source_page")
            sf = source_by_id.get(slot.get("source_figure_id"))
            if sf is not None:
                sf["asset"] = pv.get("asset")
                sf["extraction_method"] = (
                    (sf.get("extraction_method") or "") + " + pdf_supplement"
                ).strip(" +")
            filled += 1
        if filled:
            print(
                f"  [supplement] {slug}: filled {filled} missing "
                f"figure(s) from PDF (partial coverage)"
            )
        unfilled = len(missing_indices) - filled
        if unfilled:
            # PDF didn't have a figure with the matching order — could
            # be a parser shortfall on either side. Surface as a
            # warning so the user knows why the slot is still empty.
            print(
                f"  [supplement] {slug}: {unfilled} missing slot(s) "
                f"could not be matched in PDF"
            )
            skel.setdefault("warnings", []).append(
                "Some figures couldn't be rendered: the publisher CDN "
                "didn't return them and they couldn't be located in the "
                "PDF either."
            )
        return skel

    skel["figures"] = figures
    print(f"  [supplement] {slug}: merged {len(figures)} figures from PDF")
    return skel


def _resolve_doi(doi: str, output_dir: Path) -> tuple[dict[str, Any], str]:
    """Resolve a DOI through the linear pipeline.

    Order (Stage 5 E6, 2026-05-18): PMC > Nature > Cell > Science >
    arXiv > bioRxiv. Publishers are tried before preprints because
    the published version is the canonical, finalized record; the
    preprint is an earlier-draft snapshot. All publisher / preprint
    branches catch ANY exception and fall through to the next path
    — so Nature timing out on Cloudflare doesn't kill the whole
    resolver, it falls through to Cell, Science, arXiv, bioRxiv.

    Slug is computed once at the top via `slug_for_paper(doi=doi)`
    — same slug regardless of which extractor wins, which gives us
    free dedup across entry points (DOI / PMID / PMCID / URL paste
    all converge on the same slug after `pmid_to_ids` / NXML lookup
    surfaces the DOI).
    """
    from paperwhirl.biorxiv import _is_biorxiv_doi
    from paperwhirl.slugs import slug_for_paper

    slug = slug_for_paper(doi=doi)
    paper_dir = output_dir / slug

    # 1. PMC (highest priority: best structured XML when full text
    #    is deposited).
    pmcid = _try_pmc_upgrade(doi)
    if pmcid:
        try:
            skel = pmc_extract(pmcid, doi=doi, output_dir=paper_dir)
        except PMCStubError as exc:
            print(f"  [resolve] {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] PMC extraction failed: {type(exc).__name__}: {exc}; falling through")
        else:
            # Stage 5 E7 S5: PMC sometimes returns captions with
            # `view.asset = None` / `asset_role = "missing"` when the
            # CDN fetch fails (Novarino 2014 / PMC4157572). Stage 5
            # E10 S1 (2026-05-21): also handles partial misses (some
            # figures present, some missing) by routing both through
            # the supplement helper — which is a fast no-op when all
            # figures are present.
            #
            # bioRxiv DOIs with NO usable figures still fall through
            # to the bioRxiv extractor (richer canonical source for
            # preprints). bioRxiv DOIs with PARTIAL coverage go to
            # supplement — the partial-fill path keeps PMC's working
            # figures and PDF-supplements only the missing ones.
            if not _has_usable_figures(skel) and _is_biorxiv_doi(doi):
                print(
                    f"  [resolve] PMC returned 0 usable figures for bioRxiv DOI {doi}; "
                    f"falling through"
                )
            else:
                return _supplement_figures_from_pdf(skel, paper_dir), slug

    # 1.5. eLife (Stage 6 E9 Step 1). Gold OA; clean JATS XML from
    # api.elifesciences.org. Most eLife papers index in PMC and are
    # caught above; this branch handles the new-paper gap.
    if is_elife_doi(doi):
        print(f"  [resolve] trying eLife JATS for {doi}")
        try:
            skel = elife_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except Exception as exc:
            print(f"  [resolve] eLife extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 1.7. Frontiers (Stage 6 E9 Step 3).
    if is_frontiers_doi(doi):
        print(f"  [resolve] trying Frontiers JATS for {doi}")
        try:
            skel = frontiers_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except Exception as exc:
            print(f"  [resolve] Frontiers extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 1.8. PLOS (Stage 6 E9 follow-on).
    if is_plos_doi(doi):
        print(f"  [resolve] trying PLOS JATS for {doi}")
        try:
            skel = plos_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except Exception as exc:
            print(f"  [resolve] PLOS extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 2. Nature (publisher HTML — published version preferred over preprint).
    if is_nature_doi(doi):
        print(f"  [resolve] trying Nature HTML extraction for {doi}")
        try:
            skel = nature_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except Exception as exc:
            print(f"  [resolve] Nature extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 3. Cell.
    if is_cell_doi(doi):
        print(f"  [resolve] trying Cell HTML extraction for {doi}")
        try:
            skel = cell_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except CellPaywallStub as exc:
            print(f"  [resolve] Cell stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Cell extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4. Science.
    if is_science_doi(doi):
        print(f"  [resolve] trying Science HTML extraction for {doi}")
        try:
            skel = science_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except SciencePaywallStub as exc:
            print(f"  [resolve] Science stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Science extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.5. PNAS (Stage 6 E9 Step 2).
    if is_pnas_doi(doi):
        print(f"  [resolve] trying PNAS HTML for {doi}")
        try:
            skel = pnas_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except PNASPaywallStub as exc:
            print(f"  [resolve] PNAS stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] PNAS extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.7. Oxford Academic / Silverchair (Stage 6 E9 Step 4).
    if is_oxford_doi(doi):
        print(f"  [resolve] trying Oxford HTML for {doi}")
        try:
            skel = oxford_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except OxfordPaywallStub as exc:
            print(f"  [resolve] Oxford stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Oxford extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.8. JNeurosci (Stage 6 E9 follow-on). Highwire HTML.
    if is_jneurosci_doi(doi):
        print(f"  [resolve] trying JNeurosci HTML for {doi}")
        try:
            skel = jneurosci_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except JNeurosciPaywallStub as exc:
            print(f"  [resolve] JNeurosci stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] JNeurosci extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 4.9. Royal Society (Stage 6 E9 follow-on). Atypon HTML.
    if is_royalsoc_doi(doi):
        print(f"  [resolve] trying Royal Society HTML for {doi}")
        try:
            skel = royalsoc_extract(doi, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except RoyalSocPaywallStub as exc:
            print(f"  [resolve] Royal Society stub (paywalled): {exc}; falling through")
        except Exception as exc:
            print(f"  [resolve] Royal Society extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 5. arXiv (preprint).
    if is_arxiv_doi(doi):
        arxiv_id = doi_to_arxiv_id(doi)
        print(f"  [resolve] trying arXiv extraction for {arxiv_id}")
        try:
            skel = arxiv_extract(arxiv_id, output_dir=paper_dir)
            return _supplement_figures_from_pdf(skel, paper_dir), slug
        except Exception as exc:
            print(f"  [resolve] arXiv extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 6. bioRxiv (preprint). Most complex branch — has its own
    #    JATS + PDF fallback chain inside it.
    if _is_biorxiv_doi(doi):
        from datetime import datetime, timezone
        from paperwhirl.biorxiv import (
            _extract_with_e2_fallback,
            parse_jats,
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        try:
            # E10 S1 (2026-05-21): build the skeleton inside the
            # BiorxivSession context (it needs the warmed session to
            # fetch JATS, figure images, and the bioRxiv PDF), then
            # close the session BEFORE calling the supplement helper.
            # If we call the supplement inside the with-block and a
            # figure is missing, the supplement spins up a SECOND
            # nested Playwright session to fetch the publisher PDF —
            # which fails and triggers the "try VPN" warning even
            # though the PDF is reachable. EGR1 paper's F3 was the
            # canonical case that surfaced this.
            skel: dict[str, Any] | None = None
            with BiorxivSession() as bsession:
                xml = bsession.fetch_jats(doi)
                parsed = parse_jats(xml)

                # WITHDRAWN papers: bioRxiv adds a new version whose
                # title starts with "WITHDRAWN:" and whose JATS body
                # is just the retraction notice. Surface this plainly
                # instead of silently falling through.
                title = (parsed.get("title") or "").strip()
                if title.upper().startswith("WITHDRAWN"):
                    raise ValueError(
                        f"bioRxiv DOI {doi} has been WITHDRAWN by the "
                        f"authors. Title: {title}"
                    )

                # Happy path: JATS body has figure refs.
                if parsed["figures"]:
                    skel = _build_biorxiv_skeleton_from_jats(
                        doi, parsed, bsession, paper_dir,
                    )

                # JATS has no figure refs → fall back to PDF
                # extraction. See Stage 5 E2 for the rationale.
                else:
                    version = (bsession.last_meta or {}).get("version", 1)
                    versionless_doi = re.sub(r"v\d+$", "", doi)
                    pdf_url = (
                        f"https://www.biorxiv.org/content/"
                        f"{versionless_doi}v{version}.full.pdf"
                    )
                    print(
                        f"  [biorxiv] empty JATS body for {doi}; "
                        f"falling back to PDF at {pdf_url}"
                    )
                    pdf_bytes = bsession._in_page_fetch_bytes(pdf_url)
                    pdf_path = paper_dir / "uploaded.pdf"
                    pdf_path.write_bytes(pdf_bytes)
                    now = (
                        datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                    )
                    skel = _extract_with_e2_fallback(
                        pdf_path, parsed, slug, now, paper_dir
                    )
                    # Stage 5 E7 S22b: the PDF fallback gives body
                    # text but crude page-crop figures. bioRxiv's
                    # article HTML serves clean per-figure images —
                    # prefer those when available.
                    html_figs = bsession.scrape_article_figures(
                        doi, version, paper_dir
                    )
                    if html_figs:
                        skel["figures"] = html_figs
                        print(
                            f"  [biorxiv] replaced PDF page-crops with "
                            f"{len(html_figs)} article-page figure(s)"
                        )

            # Session closed. Now safe to call the supplement helper,
            # which may open its own BiorxivSession to fetch the
            # publisher PDF for any figures the bioRxiv CDN didn't
            # serve. No-op when every figure already has an asset.
            if skel is not None:
                return _supplement_figures_from_pdf(skel, paper_dir), slug
        except ValueError:
            # WITHDRAWN-paper case (raised explicitly above) — let it
            # propagate; the user should see the meaningful message
            # rather than have it swallowed by the catch-all fall-
            # through.
            raise
        except Exception as exc:
            print(f"  [resolve] bioRxiv extraction failed: {type(exc).__name__}: {exc}; falling through")

    # 7. Generic PDF fall-through (Stage 5 E11, 2026-05-23). Catches
    #    both "no detector matched" (Wiley, OUP, JCI, PNAS, eLife, ...)
    #    and "detector matched but extractor failed" (e.g. Cell
    #    paywalled stub). Tries the publisher's own PDF first via the
    #    `citation_pdf_url` meta-tag convention — backend inherits the
    #    user's network identity, so institutional VPN access kicks in
    #    naturally — and falls back to Unpaywall for OA copies. PDF
    #    bytes flow through `_resolve_pdf`, which yields a packet at
    #    PDF-supplement fidelity. A warning banner notes the lower
    #    fidelity so the user knows reference lookups + per-section
    #    figure mapping are best-effort.
    from paperwhirl.pdf_fetch import fetch_pdf_for_unknown_publisher

    print(f"  [resolve] no family extractor succeeded — trying generic PDF fall-through for {doi}")
    pdf_data = fetch_pdf_for_unknown_publisher(doi)
    if pdf_data:
        skel, slug_from_pdf = _resolve_pdf(doi, output_dir, pdf_data)
        if slug_from_pdf != slug:
            print(f"  [resolve] WARN: PDF-derived slug {slug_from_pdf} differs from DOI slug {slug}")
        skel.setdefault("warnings", []).append(
            "Extracted from the publisher PDF as a fall-through — no "
            "structured-text extractor exists for this publisher, so "
            "reference lookups and per-section figure mapping are "
            "best-effort. Figures and body text are present."
        )
        return skel, slug_from_pdf

    raise ValueError(
        f"Could not resolve DOI {doi} — not in PMC, not a known preprint, "
        f"and no PDF was reachable via the publisher (even on VPN) or "
        f"Unpaywall."
    )


def _extract_arxiv(arxiv_id: str, output_dir: Path) -> tuple[dict[str, Any], str]:
    """Extract from arXiv by id.

    Stage 5 E6 (2026-05-18): slug now derives from the arXiv DOI
    canonical form (`10.48550/arXiv.<id>`) via `slug_for_paper`
    instead of the bespoke `slugify(id-normalized)` pattern. Same
    paper accessed via arXiv ID OR arXiv DOI converges on the
    same slug.
    """
    from paperwhirl.slugs import slug_for_paper
    arxiv_doi = f"10.48550/arXiv.{arxiv_id}"
    slug = slug_for_paper(doi=arxiv_doi)
    paper_dir = output_dir / slug
    skel = arxiv_extract(arxiv_id, output_dir=paper_dir)
    return skel, slug


def _extract_pmc(pmcid: str, output_dir: Path) -> tuple[dict[str, Any], str]:
    """Extract from PMC by PMCID.

    Stage 5 E6 (2026-05-18): pre-fetches PMC NXML to read the DOI
    out of the article-meta, so the slug derives from the canonical
    DOI rather than the PMCID. Different inputs for the same paper
    (DOI / PMCID / PMID resolving to PMCID) converge on the same
    slug. The NXML cache means the subsequent `pmc_extract` call
    hits the cached file, not the network.

    Stage 5 E3 fix #1 carried over: if PMC has only the
    license-restriction stub OR empty body, `PMCStubError` carries
    the DOI; `_extract_pmc` falls through to `_resolve_doi` with
    that DOI so the user pasting a bare PMC ID for a stub-only
    paper still gets a clean extraction via the bioRxiv / publisher
    paths.
    """
    from paperwhirl.pmc import fetch_nxml, parse_pmc_nxml
    from paperwhirl.slugs import slug_for_paper

    # Pre-fetch NXML to learn the DOI for slug derivation. Cached
    # to disk, so the later `pmc_extract` call reuses it for free.
    doi = None
    try:
        xml = fetch_nxml(pmcid)
        parsed = parse_pmc_nxml(xml)
        doi = parsed.get("doi")
    except Exception as exc:
        # Network / parse failure — fall back to the PMCID-based
        # slug rather than aborting. The DOI lookup is an
        # optimization for dedup; the extractor will retry the
        # fetch and surface a real error if PMC is unreachable.
        print(f"  [_extract_pmc] could not pre-fetch DOI from NXML: {exc}")
    slug = slug_for_paper(doi=doi, pmcid=pmcid)
    paper_dir = output_dir / slug
    try:
        skel = pmc_extract(pmcid, output_dir=paper_dir)
    except PMCStubError as exc:
        if not exc.doi:
            raise
        print(f"  [resolve] {exc}; falling through to DOI path with {exc.doi}")
        return _resolve_doi(exc.doi, output_dir)
    # Stage 5 E7 S6 (2026-05-19): symmetric to S5 on the _resolve_doi
    # path. PMC's article-page scrape (`_scrape_figure_urls`) is
    # intermittently blocked by Cloudflare; when it returns an empty
    # map, pmc_extract returns figures with `view.asset = None`. The
    # PMID / PMCID entry point used to short-circuit on that — UI
    # rendered placeholder figures. Now we fall through to the
    # publisher-PDF supplement, same as the DOI path. If supplement
    # also fails (no publisher branch matches, or the publisher
    # blocks), the warning banner fires and figures ship empty
    # instead of broken.
    # E10 S1 (2026-05-21): always route through the supplement helper
    # — it no-ops when all figures are present, partial-fills when
    # PMC served some figures but missed others, and fully replaces
    # when PMC found 0 usable assets (the original S5/S6 case).
    return _supplement_figures_from_pdf(skel, paper_dir), slug


# Stage 6 E6: Crossref relevance score is a BM25-style measure
# that can score high across unrelated papers sharing topic
# keywords. A direct title-similarity check is the right gate.
# Threshold 0.85 rejects shared-keyword false matches while
# accepting preprint-vs-revision and case/punctuation drift.
_CROSSREF_TITLE_SIMILARITY_MIN = 0.85


def _normalize_title(s: str) -> str:
    """Lowercase, collapse non-alphanumeric runs to single spaces."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _title_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def _resolve_doi_by_crossref_any(title: str) -> list[str]:
    """Look up DOI candidates by title via Crossref, with strict gating.

    Returns up to 5 DOIs that pass ALL of:
    - min relevance score (CROSSREF_MIN_SCORE)
    - top-vs-next-best score margin (CROSSREF_MARGIN) — rejects
      ambiguous matches
    - title similarity >= 0.85 between the query title and the
      candidate's own Crossref-reported title (rejects shared-
      keyword false matches that score crosses don't catch)

    Stage 6 E6 (2026-05-25): previously this caller only checked
    `score >= CROSSREF_MIN_SCORE`, which admitted a completely
    different bioRxiv paper as a "match" for a lab-internal
    manuscript with no DOI — different authors, different title,
    overlapping medical keywords. Tightened to mirror
    `is_biorxiv`'s margin check + a direct title comparison.
    """
    import requests as _requests
    from paperwhirl.biorxiv import (
        CROSSREF_URL, CROSSREF_UA, CROSSREF_MIN_SCORE, CROSSREF_MARGIN,
    )

    try:
        r = _requests.get(
            CROSSREF_URL,
            params={"query.title": title, "rows": 5},
            headers={"User-Agent": CROSSREF_UA},
            timeout=15,
        )
        r.raise_for_status()
    except _requests.RequestException:
        return []

    items = r.json().get("message", {}).get("items", [])
    if not items:
        return []

    top_score = float(items[0].get("score", 0))
    if top_score < CROSSREF_MIN_SCORE:
        return []
    if len(items) >= 2:
        next_score = float(items[1].get("score", 0))
        if next_score > 0 and top_score < CROSSREF_MARGIN * next_score:
            print(
                f"  [crossref] rejecting ambiguous top hit "
                f"(top={top_score:.1f} next={next_score:.1f} "
                f"margin<{CROSSREF_MARGIN})"
            )
            return []

    candidates = []
    for item in items:
        score = float(item.get("score", 0))
        if score < CROSSREF_MIN_SCORE:
            continue
        cand_titles = item.get("title") or []
        if not cand_titles:
            continue
        sim = _title_similarity(title, cand_titles[0])
        if sim < _CROSSREF_TITLE_SIMILARITY_MIN:
            print(
                f"  [crossref] rejecting low-similarity candidate "
                f"{item.get('DOI', '')!r} "
                f"(score={score:.1f} sim={sim:.2f} < {_CROSSREF_TITLE_SIMILARITY_MIN})"
            )
            continue
        candidates.append(item.get("DOI", ""))
    return candidates


# Stage 6 E8 Step 1 (2026-05-26): Crossref enrichment for the
# generic-PDF fall-through path. The resolver extracts a DOI
# from PDF text but the downstream extract.build_skeleton hard-
# codes doi/year/journal=None and falls back to a heuristic for
# title/authors that fails on layouts like PNAS's title page.
# When we have a DOI, Crossref returns canonical title +
# authors + year + container-title in one call — fills the gap
# for any publisher we don't have a structured extractor for.
#
# Fails closed: any error returns None and the caller leaves
# whatever it already had.
def _fetch_crossref_metadata(doi: str) -> dict | None:
    """Look up canonical metadata for a DOI via Crossref.

    Returns a dict with optional keys: title, authors (list of
    "Given Family" strings), year (int), journal. Missing
    fields are omitted. Returns None on any failure (network,
    HTTP error, DOI not in Crossref).
    """
    import requests as _requests
    from paperwhirl.biorxiv import CROSSREF_URL, CROSSREF_UA

    try:
        r = _requests.get(
            f"{CROSSREF_URL}/{doi}",
            headers={"User-Agent": CROSSREF_UA},
            timeout=10,
        )
        if r.status_code == 404:
            print(f"  [crossref] DOI {doi} not in Crossref")
            return None
        r.raise_for_status()
    except _requests.RequestException as exc:
        print(f"  [crossref] metadata fetch failed for {doi}: {type(exc).__name__}: {exc}")
        return None

    msg = r.json().get("message") or {}
    out: dict[str, Any] = {}

    titles = msg.get("title") or []
    if titles:
        out["title"] = titles[0].strip()

    authors = []
    for a in msg.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = f"{given} {family}".strip()
        if name:
            authors.append(name)
    if authors:
        out["authors"] = authors

    # Year: prefer the published-print date, then online, then
    # `created` (deposit date) as a last resort. date-parts is
    # nested: [[YYYY, MM, DD]].
    for key in ("published-print", "published-online", "issued", "created"):
        date_block = msg.get(key) or {}
        parts = date_block.get("date-parts") or []
        if parts and parts[0]:
            yr = parts[0][0]
            if isinstance(yr, int):
                out["year"] = yr
                break

    containers = msg.get("container-title") or []
    if containers:
        out["journal"] = containers[0].strip()

    return out or None


def _build_biorxiv_skeleton_from_jats(
    doi: str,
    parsed: dict,
    session: BiorxivSession,
    output_dir: Path,
) -> dict[str, Any]:
    """Build a skeleton from already-parsed bioRxiv JATS (no PDF needed)."""
    from datetime import datetime, timezone
    from paperwhirl.biorxiv import _doi_slug

    slug = _doi_slug(doi)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    jats_url = getattr(session, "last_jats_url", None)
    if not jats_url:
        from paperwhirl.biorxiv import _resolve_jats_url
        import re as _re
        jats_url, _ = _resolve_jats_url(doi)
        jats_url = _re.sub(r"(?<!:)//", "/", jats_url)

    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    walkthrough_figures = []
    source_figures = []
    for fig in parsed["figures"]:
        source_id = f"source_fig_{fig['number']}"
        asset = None
        hwp_id = fig.get("hwp_id")
        if hwp_id:
            dest = figures_dir / f"figure_{fig['number']}.jpg"
            try:
                session.fetch_figure(jats_url, hwp_id, dest)
                asset = str(dest.relative_to(output_dir))
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

    if output_dir:
        (output_dir / "extracted_text.txt").write_text(body_text, encoding="utf-8")

    title = parsed["title"]
    authors = parsed["authors"]

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
            "authors": authors,
            "first_author": authors[0].split()[-1] if authors else "",
            # Stage 6 E7 (2026-05-26): year was hard-coded None and
            # journal was hard-coded "bioRxiv" in this DOI-paste
            # builder. parse_jats already returns both correctly
            # (medRxiv JATS journal-title is "medRxiv"; pub-date
            # carries the year). Twin fix in biorxiv.py's PDF-drop
            # builder.
            "year": parsed.get("year"),
            "journal": parsed.get("journal") or "bioRxiv",
            "doi": doi,
            "pmid": None,
            "arxiv_id": None,
            "biorxiv_doi": doi,
            "preprint_url": f"https://doi.org/{doi}",
            "source_pdf": None,
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
        # E8 S1 + E10 S2 (2026-05-21): carry parsed bibliography so
        # the per-paper Discuss tool `lookup_reference(n)` can answer
        # ref questions. Was present in biorxiv.py's builder but
        # missing from this resolve.py twin — DOI-paste / click-to-
        # extract bioRxiv papers (e.g. EGR1) shipped without refs.
        "references": parsed.get("references", []),
        "exports": {
            "pdf": {
                "path": None,
                "created_at": None,
            }
        },
    }
