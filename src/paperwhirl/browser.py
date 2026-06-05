"""Shared Playwright + stealth session for publisher HTML scrapers.

Used by Cell and Science extractors (and reusable by bioRxiv, though
bioRxiv keeps its own session class to host the in-page-fetch JATS
quirk on top of this pattern).

Public surface:
  PlaywrightSession(warmup_url) -> context manager
    .fetch_html(url) -> str
    .fetch_bytes(url) -> bytes
"""

from __future__ import annotations

import time
from typing import Any


class PlaywrightSession:
    """Stealth Playwright session that warms a publisher domain once."""

    def __init__(self, warmup_url: str, *, headless: bool = True):
        self.warmup_url = warmup_url
        self.headless = headless
        self._stealth_ctx = None
        self._pw = None
        self._browser = None
        self._page = None
        self._warmed = False

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        self._stealth_ctx = Stealth().use_sync(sync_playwright())
        self._pw = self._stealth_ctx.__enter__()
        self._browser = self._pw.chromium.launch(headless=self.headless)
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
        if self._stealth_ctx is not None:
            self._stealth_ctx.__exit__(*exc)

    def _warm(self) -> None:
        if self._warmed:
            return
        self._page.goto(self.warmup_url, wait_until="domcontentloaded", timeout=60000)
        # Check first, then short sleep — avoids paying 2s when Cloudflare
        # already cleared (warm-up usually completes instantly post-init).
        for i in range(30):
            if "Just a moment" not in self._page.title():
                break
            time.sleep(0.5)
        print(f"  [cloudflare] warmed {self.warmup_url} — title: {self._page.title()!r}")
        self._warmed = True

    def fetch_html(self, url: str, *, settle_seconds: int = 1) -> str:
        """Navigate to url, clear Cloudflare if needed, return HTML.

        Optimized for first-content latency: skips the prior networkidle
        wait (publisher analytics keep loading long after the article HTML
        is rendered) and polls Cloudflare title every 0.5s instead of 2s.
        """
        self._warm()
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        cleared_at: float | None = None
        for i in range(60):
            title = self._page.title()
            if "Just a moment" not in title:
                cleared_at = i * 0.5
                print(f"  [page] reached {url} after {cleared_at}s — title: {title!r}")
                break
            time.sleep(0.5)
        else:
            print(f"  [page] WARNING: still on challenge — title: {self._page.title()!r}")
        if settle_seconds:
            time.sleep(settle_seconds)
        return self._page.content()

    def fetch_bytes(self, url: str) -> bytes:
        """In-page fetch that inherits page cookies (image/asset downloads)."""
        self._warm()
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
