"""Canonical slug derivation — Stage 5 E6 (2026-05-18).

The single source of truth for "what slug does this paper get on
disk." Replaces the pre-E6 pattern where each extractor produced
its own slug from its preferred input field (PMC used the PMCID,
bioRxiv stripped the DOI prefix, Nature/Cell/Science took the DOI
suffix, PDF drops used the filename) — which caused the same paper
to land at different slugs depending on entry point, with
tactical patches (`find_by_doi`, `PMCStubError.doi`, etc.)
accumulating around the asymmetry.

Rule:
    1. DOI available (either `paper.doi` OR `paper.biorxiv_doi`)
       → `slugify(normalize_doi(doi))`.
    2. PDF bytes available, no DOI
       → `nodoi_<sha256_prefix_8>`. Stable across re-drops of
         the same file.
    3. PMCID available, no DOI (extremely rare — most NXML records
       carry a DOI)
       → `slugify(pmcid.lower())`. Carries the pre-E6 behavior
         for the long tail.

The multi-field DOI lookup (rule #1 checks both `paper.doi` and
`paper.biorxiv_doi`) matters for historical saved packets that
populated only one of the two fields — without the multi-field
check, the migration would compute the `nodoi_<hash>` fallback
for those even though their DOI is right there.

`normalize_doi` was the dropped E5's planned helper for dedup
comparison; here it becomes load-bearing for slug derivation
itself. Same shape, more responsibility.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final

from paperwhirl.generate import slugify  # shared with other modules

_DOI_URL_PREFIX_RE: Final = re.compile(
    r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE
)
_DOI_VERSION_SUFFIX_RE: Final = re.compile(r"v\d+$")


def normalize_doi(doi: str | None) -> str | None:
    """Normalize a DOI string for stable comparison + slug derivation.

    Transformations (all idempotent, conservative — only strip
    things that aren't part of the canonical DOI per the DOI
    Handbook):
      - strip whitespace
      - strip leading `http(s)?://(dx\\.)?doi\\.org/`
      - lowercase (DOIs are case-insensitive)
      - strip trailing `v\\d+$` (bioRxiv preprint version suffix —
        per user policy E6: v1 and v2 are the same paper for
        identity purposes)

    Returns None for falsy / empty input.
    """
    if not doi:
        return None
    s = doi.strip()
    s = _DOI_URL_PREFIX_RE.sub("", s)
    s = s.strip().lower()
    s = _DOI_VERSION_SUFFIX_RE.sub("", s)
    return s or None


def _pdf_content_hash(pdf_bytes: bytes) -> str:
    """8-char SHA-256 prefix of the PDF bytes. Stable across re-drops
    of the same file, so DOI-less PDFs at least dedup against
    themselves."""
    return hashlib.sha256(pdf_bytes).hexdigest()[:8]


def slug_for_paper(
    *,
    doi: str | None = None,
    biorxiv_doi: str | None = None,
    pdf_bytes: bytes | None = None,
    pmcid: str | None = None,
) -> str:
    """Derive the canonical slug for a paper. See module docstring
    for the three-way rule.

    Multi-field DOI lookup: `doi` and `biorxiv_doi` are checked in
    that order; first non-empty wins. This handles historical
    packets where one field was populated and the other wasn't.

    Raises `ValueError` if none of the inputs are usable — the
    caller has nothing to make a slug from. The DOI fallback ladder
    (PDF hash → PMCID → error) covers every realistic extraction
    path; hitting the error means the upstream code lost the paper's
    identifying information entirely.
    """
    canonical = normalize_doi(doi) or normalize_doi(biorxiv_doi)
    if canonical:
        return slugify(canonical)
    if pdf_bytes:
        return f"nodoi_{_pdf_content_hash(pdf_bytes)}"
    if pmcid:
        return slugify(pmcid.lower())
    raise ValueError(
        "slug_for_paper: need at least one of doi / biorxiv_doi / "
        "pdf_bytes / pmcid to derive a slug"
    )


def slug_from_packet(packet: dict[str, Any]) -> str | None:
    """Convenience for the migration script: derive a slug from an
    extracted review_session.yaml's `paper` block.

    Returns None if the packet has no DOI and no PMCID (so the
    migration can decide what to do — typically keep the old slug,
    OR try the PDF-hash path by reading uploaded.pdf from the
    paper dir if it exists).

    Does NOT try the PDF-hash path itself because the packet doesn't
    carry the PDF bytes — that's a migration-time concern handled by
    the caller.
    """
    paper = packet.get("paper") or {}
    try:
        return slug_for_paper(
            doi=paper.get("doi"),
            biorxiv_doi=paper.get("biorxiv_doi"),
            pmcid=paper.get("pmcid"),
        )
    except ValueError:
        return None
