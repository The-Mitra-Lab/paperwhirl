"""Generic PDF fall-through for DOIs without a dedicated extractor.

Stage 5 E11 (2026-05-23). Two phases:

  Phase 1 (cheap): plain httpx for landing-page + downloads. Works
  for publishers that don't bot-block the default UA — eLife, MDPI,
  Oxford open-access journals, etc. — plus any OA candidate URL on
  an institutional repository.

  Phase 2 (Playwright): warm a stealth browser session at the
  publisher's origin, fetch the landing page through Cloudflare,
  re-collect candidates, and download via the same session (cookies
  carry over). Required for Wiley / PNAS / aggressive bot-blockers.

Candidate URLs come from two sources:
  - Publisher's own `<meta name="citation_pdf_url">` tag (the Google
    Scholar indexing convention).
  - Unpaywall API — sometimes provides a better-targeted URL than
    the meta tag (e.g. Wiley meta tag returns `/doi/pdf/` which is
    an HTML viewer wrapper, while Unpaywall returns `/doi/pdfdirect/`
    which is the actual PDF binary).

The backend runs on the user's laptop, so requests inherit the
laptop's network identity — VPN routing → institutional IP →
institutional access for subscribed journals.

Known limitation: publishers with very aggressive Cloudflare bot
detection (currently observed on Wiley) may block our headless
Playwright even with stealth. Same class as the existing "PMC PDF
reCAPTCHA" carry-forward issue. Manual PDF-drop is the workaround.

Public surface:
  fetch_pdf_for_unknown_publisher(doi) -> bytes | None
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

_META_PATTERNS = [
    re.compile(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        re.IGNORECASE,
    ),
]


def _find_citation_pdf_url(html: str, base_url: str) -> Optional[str]:
    for pat in _META_PATTERNS:
        m = pat.search(html)
        if m:
            return urljoin(base_url, m.group(1))
    return None


def _looks_like_pdf(content: bytes, content_type: str) -> bool:
    if "pdf" in (content_type or "").lower():
        return True
    return bool(content) and content[:5] == b"%PDF-"


def _get_unpaywall_pdf_url(doi: str) -> Optional[str]:
    """Return the Unpaywall best-OA PDF URL for this DOI, or None.

    Unpaywall asks for an email per request as a rate-limiting
    identifier (they validate the domain has MX but never send mail).
    Operator-configurable via `unpaywall_email` in config; default
    is a real-MX placeholder.
    """
    import httpx
    from paperwhirl import config as pw_config

    email = pw_config.get_unpaywall_email() or "paperwhirl-tool@gmail.com"
    try:
        r = httpx.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": email},
            timeout=15.0,
        )
        if r.status_code != 200:
            print(f"  [pdf-fetch] Unpaywall {doi} -> HTTP {r.status_code}")
            return None
        oa = (r.json().get("best_oa_location") or {})
        return oa.get("url_for_pdf")
    except Exception as exc:
        print(f"  [pdf-fetch] Unpaywall lookup failed: {type(exc).__name__}: {exc}")
        return None


# ---- Phase 1: cheap path (httpx only) -----------------------------------

def _fetch_landing_html_httpx(landing_url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (final_url_after_redirects, html) or (None, None) on failure."""
    import httpx
    try:
        r = httpx.get(
            landing_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            timeout=20.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            print(f"  [pdf-fetch] httpx landing {landing_url} -> HTTP {r.status_code}")
            return None, None
        return str(r.url), r.text
    except Exception as exc:
        print(f"  [pdf-fetch] httpx landing failed: {type(exc).__name__}: {exc}")
        return None, None


def _download_pdf_httpx(url: str) -> Optional[bytes]:
    import httpx
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"},
            timeout=60.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            print(f"  [pdf-fetch] httpx {url} -> HTTP {r.status_code}")
            return None
        if not _looks_like_pdf(r.content, r.headers.get("content-type", "")):
            print(f"  [pdf-fetch] httpx {url} returned non-PDF (content-type={r.headers.get('content-type')!r})")
            return None
        return r.content
    except Exception as exc:
        print(f"  [pdf-fetch] httpx {url}: {type(exc).__name__}: {exc}")
        return None


def _try_cheap_path(doi: str) -> Optional[bytes]:
    """Phase 1: pure httpx — landing, meta tag, Unpaywall, downloads."""
    landing_url = f"https://doi.org/{doi}"
    final_url, html = _fetch_landing_html_httpx(landing_url)

    candidates: list[tuple[str, str]] = []
    if html:
        pdf_url = _find_citation_pdf_url(html, final_url or landing_url)
        if pdf_url:
            candidates.append(("publisher-meta", pdf_url))

    oa_url = _get_unpaywall_pdf_url(doi)
    if oa_url and oa_url not in (c[1] for c in candidates):
        candidates.append(("unpaywall", oa_url))

    for label, url in candidates:
        print(f"  [pdf-fetch] httpx try: {label} -> {url}")
        data = _download_pdf_httpx(url)
        if data:
            print(f"  [pdf-fetch] httpx {label} OK ({len(data)} bytes)")
            return data
    return None


# ---- Phase 2: Playwright session (Cloudflare / bot-block) --------------

def _try_playwright_path(doi: str) -> Optional[bytes]:
    """Phase 2: spin up a stealth browser, do landing + downloads in one session.

    Cookies set during the landing-page warmup carry over to the PDF
    download (`fetch_bytes` uses page-context fetch with credentials),
    which is how the user's normal browser flow works.
    """
    try:
        from paperwhirl.browser import PlaywrightSession
    except Exception as exc:
        print(f"  [pdf-fetch] Playwright unavailable: {type(exc).__name__}: {exc}")
        return None

    landing_url = f"https://doi.org/{doi}"

    try:
        with PlaywrightSession(landing_url) as sess:
            try:
                html = sess.fetch_html(landing_url, settle_seconds=2)
            except Exception as exc:
                print(f"  [pdf-fetch] Playwright landing failed: {type(exc).__name__}: {exc}")
                html = None

            candidates: list[tuple[str, str]] = []
            if html:
                base_url = sess._page.url  # post-redirect URL
                pdf_url = _find_citation_pdf_url(html, base_url)
                if pdf_url:
                    candidates.append(("publisher-meta", pdf_url))

            oa_url = _get_unpaywall_pdf_url(doi)
            if oa_url and oa_url not in (c[1] for c in candidates):
                candidates.append(("unpaywall", oa_url))

            for label, url in candidates:
                try:
                    data = sess.fetch_bytes(url)
                    if _looks_like_pdf(data, ""):
                        print(f"  [pdf-fetch] Playwright {label} OK ({len(data)} bytes)")
                        return data
                    print(f"  [pdf-fetch] Playwright {label} {url}: non-PDF response")
                except Exception as exc:
                    print(f"  [pdf-fetch] Playwright {label} {url}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"  [pdf-fetch] Playwright session failed: {type(exc).__name__}: {exc}")

    return None


# ---- Orchestrator ------------------------------------------------------

def fetch_pdf_for_unknown_publisher(doi: str) -> Optional[bytes]:
    """Generic PDF fall-through for DOIs without a dedicated extractor.

    Phase 1: httpx-only (fast, works for non-bot-blocked publishers).
    Phase 2: Playwright session (handles Cloudflare and bot-detection).
    Returns the PDF bytes on success, None if everything fails.
    """
    # Strip any bioRxiv/medRxiv-style version suffix (`v1`, `v2`, …)
    # before hitting doi.org and Unpaywall — both expect the canonical
    # versionless DOI and 404 on the versioned form. The version is a
    # CSHL URL convention, not part of the Crossref-registered DOI.
    canonical = re.sub(r"v\d+$", "", doi)
    if canonical != doi:
        print(f"  [pdf-fetch] stripped version suffix: {doi} -> {canonical}")
    print(f"  [pdf-fetch] starting fall-through for {canonical}")
    data = _try_cheap_path(canonical)
    if data:
        return data
    print(f"  [pdf-fetch] cheap path failed; trying Playwright")
    return _try_playwright_path(canonical)
