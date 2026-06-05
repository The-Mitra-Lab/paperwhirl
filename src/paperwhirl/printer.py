"""PDF rendering for saved review packets via Playwright `page.pdf()`.

Launches a headless Chromium against the running backend's own React
app at a special print URL, waits for the packet to fully render, and
returns the PDF bytes. Chromium is already a required dependency
(paywalled-publisher scraping), so this adds no new dependency weight.

The print URL renders `PrintView` — a figures-at-end layout: the
analytical text flows as the document body, then each figure sits
alone on its own page with its legend. That layout, not the
rendering engine, is what avoids the whitespace gaps the inline
layout produced at page boundaries.
"""

from __future__ import annotations

import os


def render_pdf(url: str, *, settle_seconds: float = 0.5) -> bytes:
    """Synchronous — call from a thread (e.g. run_in_executor)."""
    # PLAYWRIGHT_BROWSERS_PATH=0 puts the chromium under the env's
    # site-packages; matches how the rest of the app uses Playwright.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1100, "height": 1400})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait for the React app to set the print-ready sentinel.
            try:
                page.wait_for_selector("[data-print-ready='1']", timeout=20000)
            except Exception as _exc:
                # Fall through anyway — better a partial render than nothing.
                import sys as _sys
                print(
                    f"[paperwhirl] print-ready sentinel timed out for {url!r}; "
                    f"PDF may be incomplete: {_exc}",
                    file=_sys.stderr,
                )
            # Small settle to let async figures finish painting.
            page.wait_for_timeout(int(settle_seconds * 1000))
            return page.pdf(
                format="Letter",
                print_background=True,
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
            )
        finally:
            browser.close()
