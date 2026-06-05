"""Table-driven tests for paperwhirl.resolve._parse_input.

The classifier is a chokepoint — every identifier the user pastes
(in the InputArea, in the Discuss panel's future paper suggestions,
etc.) flows through it. Regressions here silently break ingestion,
so the failure cases from `ISSUES.md` (Stage 4 E6 tire-kicking,
2026-05-16) get pinned down here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run from anywhere (CI, repo root, this dir) without requiring
# `pip install -e .` first.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from paperwhirl.resolve import _parse_input  # noqa: E402


CASES: list[tuple[str, tuple[str, str]]] = [
    # Bare PMID
    ("12345678", ("pmid", "12345678")),
    # PMID with prefix — failed before E7 Step 2
    ("PMID: 12345678", ("pmid", "12345678")),
    ("PMID:12345678", ("pmid", "12345678")),
    ("pmid 12345678", ("pmid", "12345678")),
    # PMCID with and without prefix
    ("PMC12345", ("pmcid", "PMC12345")),
    ("PMCID: PMC12345", ("pmcid", "PMC12345")),
    # PubMed URL
    ("https://pubmed.ncbi.nlm.nih.gov/12345678/", ("pmid", "12345678")),
    # bioRxiv URL variants — the .full / .full.pdf / .abstract
    # suffixes were captured into the DOI before E7 Step 2.
    (
        "https://www.biorxiv.org/content/10.1101/2024.01.15.575738v1",
        ("doi", "10.1101/2024.01.15.575738v1"),
    ),
    (
        "https://www.biorxiv.org/content/10.1101/2024.01.15.575738v1.full",
        ("doi", "10.1101/2024.01.15.575738v1"),
    ),
    (
        "https://www.biorxiv.org/content/10.1101/2024.01.15.575738v1.full.pdf",
        ("doi", "10.1101/2024.01.15.575738v1"),
    ),
    (
        "https://www.biorxiv.org/content/10.1101/2024.01.15.575738v1.abstract",
        ("doi", "10.1101/2024.01.15.575738v1"),
    ),
    # doi.org link
    (
        "https://doi.org/10.1101/2024.01.15.575738",
        ("doi", "10.1101/2024.01.15.575738"),
    ),
    # Bare DOI and DOI with prefix
    ("10.1038/s41586-024-01234-5", ("doi", "10.1038/s41586-024-01234-5")),
    ("doi:10.1038/s41586-024-01234-5", ("doi", "10.1038/s41586-024-01234-5")),
    ("DOI: 10.1038/s41586-024-01234-5", ("doi", "10.1038/s41586-024-01234-5")),
    # arXiv with prefix — failed before E7 Step 2
    ("arXiv:2401.12345", ("arxiv_id", "2401.12345")),
]


def test_parse_input_cases() -> None:
    failures: list[str] = []
    for inp, expected in CASES:
        got = _parse_input(inp)
        if got != expected:
            failures.append(f"  {inp!r}\n    got      {got}\n    expected {expected}")
    assert not failures, "\n" + "\n".join(failures)


if __name__ == "__main__":
    # Allow `python tests/test_parse_input.py` without pytest installed.
    test_parse_input_cases()
    print(f"OK — {len(CASES)} cases")
