"""PaperWhirl FastAPI backend."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from functools import partial
from pathlib import Path

import json

import uvicorn
import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response, StreamingResponse

from paperwhirl.generate import (
    DEFAULT_MODEL,
    DEFAULT_SECTION_PREAMBLE,
    build_discussion_prompt,
    build_figure_prompt,
    build_overview_prompt,
    build_table_prompt,
    call_openai_section,
    discussion_section_schema,
    extract_pdf_to_skeleton,
    figure_section_schema,
    generate_review_session,
    load_yaml,
    merge_streamed_packet,
    overview_schema,
    slugify,
    table_section_schema,
    write_yaml,
)
from paperwhirl.biorxiv import BiorxivSession, extract as biorxiv_extract, is_biorxiv
from paperwhirl.resolve import resolve_and_extract, resolve_only
from paperwhirl import config as pw_config
from paperwhirl import reading_lists as rl
from paperwhirl.web import discuss_tools

# --- Model menu: remote-config manifest (Stage 6 E16) ----------------
# The Settings dropdown is a *curated* list — there is no free-text
# model field (a typo'd id just fails at generate time). To keep the
# list current as OpenAI's lineup changes WITHOUT shipping a new
# notarized build, the backend fetches a small JSON manifest from the
# official GitHub repo on startup and fills the dropdown from it.
# Everything degrades to a baked-in fallback so the app always works
# offline / before the repo is live. IMPORTANT: only models on the
# current OpenAI responses-API code path belong in the manifest —
# auto-updating the *menu* does not auto-support a new model *family*
# (those carry per-family code, e.g. the gpt-5.5 reasoning param).
MODEL_MANIFEST_URL = os.environ.get(
    "PAPERWHIRL_MODEL_MANIFEST_URL",
    "https://raw.githubusercontent.com/The-Mitra-Lab/paperwhirl/main/models.json",
)
FALLBACK_MODEL_OPTIONS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"]
FALLBACK_DEFAULT_MODEL = DEFAULT_MODEL  # generate.py's last-resort default
_MODELS_CACHE_PATH = Path.home() / ".paperwhirl" / "models_cache.json"

# Mutable module state, seeded from the on-disk cache (or the static
# fallback) at import and refreshed from the network on startup.
_model_options: list[str] = list(FALLBACK_MODEL_OPTIONS)
_default_model: str = FALLBACK_DEFAULT_MODEL


def _apply_model_manifest(data: object) -> bool:
    """Validate a parsed manifest and update the in-memory model list +
    default. Returns True only if the manifest was usable; a bad shape
    is ignored so the current list survives."""
    if not isinstance(data, dict):
        return False
    raw = data.get("models")
    if not isinstance(raw, list):
        return False
    models = [m for m in raw if isinstance(m, str) and m]
    if not models:
        return False
    default = data.get("default")
    global _model_options, _default_model
    _model_options = models
    _default_model = (
        default if isinstance(default, str) and default in models else models[0]
    )
    return True


def _seed_models_from_cache() -> None:
    """Load the last-good manifest from disk so an offline launch shows
    the most recent known list rather than the static fallback."""
    try:
        _apply_model_manifest(json.loads(_MODELS_CACHE_PATH.read_text()))
    except (OSError, ValueError):
        pass


def _refresh_models_from_manifest() -> None:
    """Fetch + apply + cache the manifest. Never fatal: any failure
    leaves the seeded/fallback list untouched. Runs on a background
    thread so it never delays startup, health, or the splash."""
    import httpx

    try:
        r = httpx.get(MODEL_MANIFEST_URL, timeout=3.0, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(
            f"[paperwhirl] model manifest fetch failed ({exc}); "
            "using cached/fallback model list",
            file=sys.stderr,
        )
        return
    if _apply_model_manifest(data):
        try:
            _MODELS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _MODELS_CACHE_PATH.write_text(json.dumps(data))
        except OSError:
            pass
        print(
            f"[paperwhirl] model manifest: {len(_model_options)} models, "
            f"default {_default_model}",
            file=sys.stderr,
        )


_seed_models_from_cache()


def _resolve_key(form_key: str = "") -> str:
    """OpenAI key, in precedence order: explicit request value, then
    the OPENAI_API_KEY env var, then the stored config."""
    return (
        form_key
        or os.environ.get("OPENAI_API_KEY", "")
        or (pw_config.get_api_key() or "")
    )


def _resolve_model() -> str:
    """Generation model, in precedence order: PAPERWHIRL_OPENAI_MODEL
    env var, then the stored config (only if it's still a current
    model), then the manifest default. A stored id that dropped out of
    the list (e.g. an old gpt-5.2 from a 0.1.1 install) falls through
    to the default instead of erroring at generate time."""
    env = os.environ.get("PAPERWHIRL_OPENAI_MODEL")
    if env:
        return env
    stored = pw_config.get_model()
    if stored and stored in _model_options:
        return stored
    return _default_model


def _mask_key(key: str) -> str | None:
    """A display-safe key fragment — last 4 chars only, or None."""
    if not key:
        return None
    return f"…{key[-4:]}" if len(key) > 4 else "…"


RESULTS_DIR = pw_config.cache_dir()

# E8 cache-sweep TTL — entries in RESULTS_DIR/ older than this get
# wiped on server startup. Covers extracted-but-never-saved papers.
# 7 days lines up with a typical reading-session arc; tunable.
CACHE_TTL_DAYS = 7

app = FastAPI(title="PaperWhirl")

# Stage 6 E1: the Tauri webview (`tauri://localhost`) calls the
# backend at `http://127.0.0.1:<port>` — cross-origin. The backend
# binds to 127.0.0.1 so only local processes can reach it; given
# that, permissive CORS adds no real attack surface. In dev (browser
# Phase A + Vite proxy) the proxy makes everything same-origin, so
# CORS is a no-op there.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, bool]:
    # Stage 6 E1: readiness probe for the Tauri sidecar. Cheap,
    # no side effects, distinct from /api/config so failure modes
    # don't mix. Polled every 100 ms by the production shell until
    # it gets a 200.
    return {"ok": True}


@app.on_event("startup")
def _run_slug_migration() -> None:
    """Stage 5 E6 (2026-05-18): if the data folder still has the
    pre-E6 slug layout (no `slug_version: 2` stamp in
    `<data>/config.yaml`), run the one-time v1 → v2 slug migration.

    Idempotent: re-runs are no-ops once the stamp is in place.
    Tar-backs up the folder before any rename — see
    `paperwhirl.migration` for the full plan. Failures here
    propagate; the backend should NOT silently come up with an
    inconsistent library. The .tgz backup is the user's safety
    net if the migration crashed mid-flight.
    """
    folder = pw_config.get_folder()
    if folder is None or not folder.exists():
        return
    from paperwhirl.migration import migrate_v1_to_v2, needs_migration
    if not needs_migration(folder):
        return
    migrate_v1_to_v2(folder)


@app.on_event("startup")
def _refresh_model_manifest() -> None:
    """Stage 6 E16: refresh the model dropdown from the remote manifest
    on a background thread. Non-blocking — health/splash never wait on
    it; the seeded cache (or static fallback) covers the brief window
    until it returns, and Settings isn't the first thing a user opens."""
    threading.Thread(target=_refresh_models_from_manifest, daemon=True).start()


@app.on_event("startup")
def _sweep_stale_cache() -> None:
    """E8 startup sweep: rmtree any RESULTS_DIR/<slug>/ whose mtime
    is older than CACHE_TTL_DAYS. Cheap and idempotent."""
    import shutil
    import time
    if not RESULTS_DIR.exists():
        return
    cutoff = time.time() - CACHE_TTL_DAYS * 86400
    swept = 0
    for sub in RESULTS_DIR.iterdir():
        if not sub.is_dir():
            continue
        try:
            if sub.stat().st_mtime < cutoff:
                shutil.rmtree(sub)
                swept += 1
        except OSError as exc:
            print(
                f"[paperwhirl] cache sweep: could not remove {sub.name!r}: {exc}",
                file=sys.stderr,
            )
    if swept:
        print(
            f"[paperwhirl] cache sweep: removed {swept} stale extraction(s) "
            f"older than {CACHE_TTL_DAYS} days",
            file=sys.stderr,
        )


@app.on_event("startup")
def _sweep_orphan_library_papers() -> None:
    """Stage 5 E4 startup sweep: remove any papers/<slug>/ dirs that
    aren't referenced by any reading list. Enforces the "every
    saved paper is in at least one list" invariant even for state
    that pre-dates the E4 cascade fix (e.g. lists deleted in
    earlier versions left orphans behind; hand-edited list YAMLs;
    etc.). Idempotent — if all is consistent, a no-op."""
    import shutil
    folder = pw_config.get_folder()
    if folder is None or not folder.exists():
        return
    orphans = rl.find_unreferenced_papers(folder)
    if not orphans:
        return
    print(
        f"[paperwhirl] library sweep: removing {len(orphans)} orphan paper(s) "
        f"(not referenced by any reading list)",
        file=sys.stderr,
    )
    for slug in orphans:
        pdir = rl.paper_dir(folder, slug)
        try:
            if pdir.exists():
                shutil.rmtree(pdir)
                print(f"  [orphan-gc] removed {slug}", file=sys.stderr)
        except OSError as exc:
            print(
                f"[paperwhirl] library sweep: could not remove {slug!r}: {exc}",
                file=sys.stderr,
            )


def _write_pdf(data: bytes, paper_dir: Path) -> Path:
    paper_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = paper_dir / "uploaded.pdf"
    pdf_path.write_bytes(data)
    return pdf_path


async def _heartbeat_until_done(future, interval: float = 15):
    """Async generator that yields periodically while `future` is pending.

    Stage 5 E14 (2026-05-24). Lets a `yield sse({"type": "heartbeat"})`
    from the caller keep the SSE connection alive across long sync
    calls (typically an LLM request in an executor). Tauri's WKWebView
    aborts idle fetches around 60s, so any phase that goes silent for
    longer than that — extraction with Playwright, a slow figure or
    discussion call — has to emit *something* periodically. Returns
    silently once the future is done; caller then reads `future.result()`.
    """
    while not future.done():
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=interval)
        except asyncio.TimeoutError:
            yield


@app.post("/api/generate/stream")
async def generate_stream(
    pdf: UploadFile | None = File(None),
    identifier: str = Form(""),
    api_key: str = Form(""),
    force_slug: str = Form(""),
):
    """Streaming generation: emits SSE events as each section completes.

    Accepts multipart/form-data with either a PDF upload OR an identifier
    string (DOI / PMID / PMCID / arXiv ID / URL). Mirrors /api/generate's
    signature.

    `force_slug` (E8 Re-generate path): when non-empty, skip the
    paste-opens pre-check AND override the slug that extraction would
    naturally produce. The cache subdir + skeleton land under
    `force_slug` so a follow-up `/save?overwrite=true` on that slug
    cleanly replaces the existing saved entry without slug drift.
    DOI-as-slug refactor is deferred; this
    is the tactical workaround that keeps Re-generate cheap.

    Events (each line is `data: <json>\\n\\n`):
      {"type":"already_saved","slug":"..."} — E8 paste-opens: the
                                              paper is already in the
                                              library (by slug or DOI
                                              match); stream ends here.
      {"type":"extracting"}                 — sent if no library match
      {"type":"skeleton","paper":{...},
       "figures":[...], "slug":"..."}      — after extraction
      {"type":"overview","content":{...}}   — after overview call
      {"type":"figure","index":N,"id":"...",
       "content":{...}}                    — once per figure as it completes
      {"type":"figure_error","index":N,"id":"...",
       "error":"..."}                      — if a figure call fails
      {"type":"discussion","content":{...}} — after discussion call
      {"type":"done","slug":"..."}          — after final yaml written
      {"type":"error","error":"..."}        — on fatal error
    """
    key = _resolve_key(api_key)
    if not key:
        raise HTTPException(status_code=401, detail="OpenAI API key required")
    os.environ["OPENAI_API_KEY"] = key

    # Read the PDF bytes here (inside the request handler) before the
    # async generator runs — UploadFile.read() needs the request body
    # still open.
    pdf_data: bytes | None = None
    input_name = identifier
    if pdf:
        pdf_data = await pdf.read()
        input_name = Path(pdf.filename or "paper").stem

    # Stage 6 E5: Re-generate fallback for saved papers that have no
    # remote identifier (sample_et_al-style PDF drops). The bytes
    # were persisted to <data>/papers/<slug>/uploaded.pdf at original
    # save time; use them as pdf_data so the same pipeline runs.
    if force_slug and not pdf and not identifier:
        folder = pw_config.get_folder()
        if folder is not None:
            saved_pdf = folder / "papers" / force_slug / "uploaded.pdf"
            if saved_pdf.exists():
                pdf_data = saved_pdf.read_bytes()
                input_name = force_slug

    if not pdf and not identifier and pdf_data is None:
        if force_slug:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot re-generate: this paper has no identifier and "
                    "no saved source PDF."
                ),
            )
        raise HTTPException(status_code=400, detail="Provide a PDF or identifier")

    model = _resolve_model()

    async def event_stream():
        def sse(payload: dict) -> str:
            return f"data: {json.dumps(payload)}\n\n"

        loop = asyncio.get_event_loop()
        try:
            # E8 paste-opens pre-check. Before paying for extraction +
            # generation, cheaply resolve the input to a slug / DOI and
            # check the library. If it's already saved (by slug match
            # OR by DOI match against a different slug — the Moudgil
            # twin scenario), emit `already_saved` and stop. The
            # frontend opens the saved packet. Zero LLM cost.
            # Skipped when `force_slug` is set (Re-generate path —
            # user has already confirmed they want to overwrite).
            data_folder = pw_config.get_folder()
            if data_folder is not None and not force_slug:
                try:
                    pre = await loop.run_in_executor(
                        None,
                        lambda: resolve_only(input_name, pdf_data=pdf_data),
                    )
                except Exception as _pre_exc:
                    # Stage 6 E7 (2026-05-26): surface the swallowed
                    # exception. We were silently missing paste-opens
                    # on PMID inputs and couldn't tell why.
                    print(
                        f"  [paste-opens] resolve_only raised: "
                        f"{type(_pre_exc).__name__}: {_pre_exc}",
                        file=sys.stderr, flush=True,
                    )
                    pre = {"slug": None, "doi": None}
                print(
                    f"  [paste-opens] input={input_name!r} → "
                    f"slug={pre.get('slug')!r} doi={pre.get('doi')!r}",
                    file=sys.stderr, flush=True,
                )
                existing_slug: str | None = None
                if pre.get("slug") and rl.is_saved(data_folder, pre["slug"]):
                    existing_slug = pre["slug"]
                elif pre.get("doi"):
                    existing_slug = rl.find_by_doi(data_folder, pre["doi"])
                if existing_slug:
                    yield sse({
                        "type": "already_saved",
                        "slug": existing_slug,
                    })
                    return

            yield sse({"type": "extracting"})

            # 1. Extraction (synchronous, run in executor). Wrapped in a
            # heartbeat loop so the SSE connection doesn't go idle long
            # enough for the client (notably Tauri WKWebView, ~60s) to
            # abort it. Surfaced 2026-05-23 during Stage 5 E11 testing:
            # Cracco/Wiley extraction takes ~60–90s for the Playwright
            # PDF fetch, and the gap between this `extracting` event
            # and the eventual `skeleton` event was tripping WKWebView's
            # idle-fetch timeout into a "Load Failed" error.
            extract_future = loop.run_in_executor(
                None,
                lambda: resolve_and_extract(input_name, RESULTS_DIR, pdf_data=pdf_data),
            )
            async for _ in _heartbeat_until_done(extract_future):
                yield sse({"type": "heartbeat"})
            skel, slug = extract_future.result()
            # Stage 6 E6 belt-and-suspenders: refuse to commit a
            # resolver-chosen slug that already names a different
            # saved paper. Three legitimate cases pass through:
            # (a) force_slug Re-generate, (b) drop's DOI matches the
            # existing saved paper's DOI (paste-opens twin that slipped
            # the pre-check), (c) target slug not in the library.
            # Everything else is corruption — typically a Crossref
            # false-match. Surfaced 2026-05-25 when a no-DOI lab
            # manuscript was misclassified as 10_1101_2024_08_13_607862
            # and overwrote that saved paper's review packet.
            if not force_slug and data_folder is not None and rl.is_saved(data_folder, slug):
                existing_meta = rl.get_paper(data_folder, slug) or {}
                drop_doi = ((skel.get("paper") or {}) or {}).get("doi") if isinstance(skel, dict) else None
                existing_doi = ((existing_meta.get("paper") or {}).get("doi")
                                if isinstance(existing_meta, dict) else None)
                same_paper = bool(drop_doi and existing_doi and drop_doi == existing_doi)
                if not same_paper:
                    yield sse({
                        "type": "error",
                        "error": (
                            f"Refusing to overwrite saved paper {slug!r}: "
                            "this drop resolved to that slug but doesn't match "
                            "the existing paper. If this drop really is the same "
                            "paper, delete the existing entry first."
                        ),
                    })
                    return
            # E8 Re-generate: if force_slug was supplied, rename the
            # natural-derived cache subdir to force_slug so the rest
            # of the pipeline (yaml writes, post-`done` `/save`) all
            # use the saved paper's existing slug. This preserves
            # display name + list memberships through overwrite.
            if force_slug and slug != force_slug:
                import shutil as _shutil_rename
                natural_dir = RESULTS_DIR / slug
                forced_dir = RESULTS_DIR / force_slug
                if forced_dir.exists():
                    _shutil_rename.rmtree(forced_dir)
                natural_dir.rename(forced_dir)
                slug = force_slug
            paper_dir = RESULTS_DIR / slug
            paper_dir.mkdir(parents=True, exist_ok=True)
            write_yaml(skel, paper_dir / "session_skeleton.yaml")

            figures_meta = skel.get("figures", [])
            tables_meta = skel.get("tables", [])
            figure_ids = [f.get("id") for f in figures_meta]
            table_ids = [t.get("id") for t in tables_meta]
            yield sse({
                "type": "skeleton",
                "slug": slug,
                "paper": skel.get("paper", {}),
                # Send full extracted figure + table metadata so the frontend
                # can show images, captions, and table bodies immediately,
                # before the model-generated analyses arrive.
                "figures": [
                    {
                        "id": f.get("id"),
                        "order": f.get("order"),
                        "source_order": f.get("source_order"),
                        "label": f.get("label"),
                        "view": {
                            "source_page": f.get("view", {}).get("source_page"),
                            "asset": f.get("view", {}).get("asset"),
                            "asset_role": f.get("view", {}).get("asset_role"),
                            "original_caption": f.get("view", {}).get("original_caption"),
                        },
                    }
                    for f in figures_meta
                ],
                "tables": [
                    {
                        "id": t.get("id"),
                        "order": t.get("order"),
                        "source_order": t.get("source_order"),
                        "label": t.get("label"),
                        "view": {
                            "caption_text": t.get("view", {}).get("caption_text"),
                            "html": t.get("view", {}).get("html"),
                            # Stage 6 E7 (2026-05-26): image-only tables
                            # (JCI-style PMC deposits) need the asset URL
                            # forwarded so TableCard can render the image.
                            "asset": t.get("view", {}).get("asset"),
                            "asset_role": t.get("view", {}).get("asset_role"),
                        },
                    }
                    for t in tables_meta
                ],
                "figure_ids": figure_ids,
                "table_ids": table_ids,
            })

            # Stage 5 E3 (2026-05-18): forward any extraction-level
            # warnings (e.g. "couldn't fetch figures from publisher;
            # try VPN") so the frontend can render them as a banner
            # on the rendered packet. Emitted AFTER the skeleton so
            # the frontend's scaffold packet exists by the time the
            # warning handler runs. Previously these only landed in
            # the backend log and the user saw a silently-figureless
            # paper with no signal that something had gone wrong.
            for warning_msg in skel.get("warnings", []):
                yield sse({"type": "warning", "message": warning_msg})

            # 2. Overview call.
            overview_prompt = build_overview_prompt(skel)
            overview_future = loop.run_in_executor(
                None,
                lambda: call_openai_section(
                    overview_prompt, model, overview_schema(), "paperwhirl_overview",
                ),
            )
            async for _ in _heartbeat_until_done(overview_future):
                yield sse({"type": "heartbeat"})
            overview_payload = overview_future.result()
            overview = overview_payload["overview"]
            paper_kind = overview_payload["paper_kind"]
            generation_notes = overview_payload.get("generation_notes", [])
            yield sse({
                "type": "overview",
                "content": {"paper_kind": paper_kind, "overview": overview},
            })

            # 3. Figures + tables sequentially, in source-document order.
            # Build a merged item list sorted by source_order so cards
            # arrive at the user in the paper's natural reading flow.
            items: list[dict[str, Any]] = []
            for idx, f in enumerate(figures_meta):
                items.append({
                    "kind": "figure",
                    "index": idx,
                    "id": figure_ids[idx],
                    "source_order": f.get("source_order", idx + 1),
                })
            for idx, t in enumerate(tables_meta):
                items.append({
                    "kind": "table",
                    "index": idx,
                    "id": table_ids[idx],
                    "source_order": t.get("source_order", 9999 + idx),
                })
            items.sort(key=lambda it: it["source_order"])

            figure_sections: dict[int, dict] = {}
            table_sections: dict[int, dict] = {}
            for item in items:
                kind = item["kind"]
                idx = item["index"]
                iid = item["id"]
                try:
                    if kind == "figure":
                        future = loop.run_in_executor(
                            None,
                            lambda idx=idx: call_openai_section(
                                build_figure_prompt(skel, idx, overview),
                                model,
                                figure_section_schema(),
                                "paperwhirl_figure",
                            ),
                        )
                        async for _ in _heartbeat_until_done(future):
                            yield sse({"type": "heartbeat"})
                        content = future.result()
                        figure_sections[idx] = content
                        yield sse({
                            "type": "figure", "index": idx, "id": iid,
                            "content": content,
                        })
                    else:
                        future = loop.run_in_executor(
                            None,
                            lambda idx=idx: call_openai_section(
                                build_table_prompt(skel, idx, overview),
                                model,
                                table_section_schema(),
                                "paperwhirl_table",
                            ),
                        )
                        async for _ in _heartbeat_until_done(future):
                            yield sse({"type": "heartbeat"})
                        content = future.result()
                        table_sections[idx] = content
                        yield sse({
                            "type": "table", "index": idx, "id": iid,
                            "content": content,
                        })
                except Exception as exc:
                    yield sse({
                        "type": f"{kind}_error", "index": idx, "id": iid,
                        "error": f"{type(exc).__name__}: {exc}",
                    })

            # 4. Discussion (depends on overview + figures + tables).
            figure_analyses_for_prompt = [
                {"index": idx, "id": figure_ids[idx], **figure_sections[idx]}
                for idx in sorted(figure_sections.keys())
            ]
            table_analyses_for_prompt = [
                {"index": idx, "id": table_ids[idx], **table_sections[idx]}
                for idx in sorted(table_sections.keys())
            ]
            discussion_prompt = build_discussion_prompt(
                skel, overview, figure_analyses_for_prompt,
                table_analyses=table_analyses_for_prompt or None,
            )
            discussion_future = loop.run_in_executor(
                None,
                lambda: call_openai_section(
                    discussion_prompt, model,
                    discussion_section_schema(), "paperwhirl_discussion",
                ),
            )
            async for _ in _heartbeat_until_done(discussion_future):
                yield sse({"type": "heartbeat"})
            discussion = discussion_future.result()
            yield sse({"type": "discussion", "content": discussion})

            # 5. Merge + write final yaml.
            packet = merge_streamed_packet(
                skel, overview, figure_sections, discussion, slug,
                paper_kind=paper_kind, generation_notes=generation_notes,
                table_sections=table_sections or None,
            )
            write_yaml(packet, paper_dir / "review_session.yaml")
            # E8: no auto-save. The cache holds the extraction; the
            # paper enters the library only when the user clicks
            # Save-to-list. With the paste-opens pre-check at the top
            # of this generator, an already-saved paper never reaches
            # this point — so the same-slug / DOI-dedup branches that
            # used to live here are unreachable and have been deleted.
            yield sse({"type": "done", "slug": slug})

        except Exception as exc:
            # Stage 5 E7 S2: log to backend stderr before yielding the
            # SSE error. Previously the exception only reached the
            # client; the backend log stopped at the last `[timing]`
            # line and looked like a silent success.
            print(
                f"[generate] FAILED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            yield sse({"type": "error", "error": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/packet/{slug}")
async def get_packet(slug: str):
    packet_path = RESULTS_DIR / slug / "review_session.yaml"
    if not packet_path.exists():
        raise HTTPException(status_code=404, detail="Packet not found")
    return JSONResponse(
        {
            "packet": load_yaml(packet_path),
            "base_url": f"/api/assets/{slug}",
            "slug": slug,
        }
    )


@app.get("/api/packets")
async def list_packets():
    packets = []
    if RESULTS_DIR.exists():
        for d in sorted(RESULTS_DIR.iterdir()):
            review = d / "review_session.yaml"
            if review.exists():
                data = load_yaml(review)
                packets.append(
                    {
                        "slug": d.name,
                        "title": data.get("session", {}).get("title", d.name),
                    }
                )
    return JSONResponse({"packets": packets})


@app.get("/api/assets/{slug}/{path:path}")
async def serve_asset(slug: str, path: str):
    file_path = RESULTS_DIR / slug / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    if not file_path.resolve().is_relative_to(RESULTS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(file_path)


def _load_paper_references(slug: str) -> list[dict]:
    """Stage 5 E8 S1: load a paper's parsed `references` from its
    session_skeleton.yaml (library wins over cache, mirroring
    `_build_discuss_system_prompt`). Returns [] if the paper has no
    parsed bibliography — the `lookup_reference` tool then reports
    that honestly."""
    folder = pw_config.get_folder()
    candidate_dirs: list[Path] = []
    if folder is not None:
        candidate_dirs.append(rl.paper_dir(folder, slug))
    candidate_dirs.append(RESULTS_DIR / slug)
    for paper_dir in candidate_dirs:
        skel_path = paper_dir / "session_skeleton.yaml"
        if not skel_path.exists():
            continue
        try:
            skel = yaml.safe_load(skel_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        refs = skel.get("references")
        if isinstance(refs, list):
            return refs
    return []


def _load_paper_figures(slug: str) -> list[dict]:
    """Stage 5 E8 S2: load a paper's figures (number, label, caption,
    on-disk asset path) from session_skeleton.yaml, library-or-cache.
    Used by the `lookup_figure` tool to attach a figure image to the
    Discuss conversation on demand. Returns [] if none."""
    folder = pw_config.get_folder()
    candidate_dirs: list[Path] = []
    if folder is not None:
        candidate_dirs.append(rl.paper_dir(folder, slug))
    candidate_dirs.append(RESULTS_DIR / slug)
    for paper_dir in candidate_dirs:
        skel_path = paper_dir / "session_skeleton.yaml"
        if not skel_path.exists():
            continue
        try:
            skel = yaml.safe_load(skel_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        figs = skel.get("figures")
        if not isinstance(figs, list):
            return []
        out: list[dict] = []
        for f in figs:
            view = f.get("view") or {}
            asset = view.get("asset")
            out.append({
                "n": f.get("order"),
                "label": f.get("label") or f.get("id"),
                "caption": view.get("display_legend") or view.get("original_caption") or "",
                "path": (paper_dir / asset) if asset else None,
            })
        return out
    return []


def _figure_data_url(path: Path) -> str | None:
    """Base64 data URL for a figure image, or None if unreadable."""
    import base64
    try:
        data = path.read_bytes()
    except OSError:
        return None
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _build_discuss_system_prompt(slug: str, context_section: str | None) -> str:
    # Stage 5 E2 tire-kick fix (2026-05-18): look in BOTH cache and
    # library. Prior code read only from RESULTS_DIR (cache), which
    # silently broke Discuss for any saved paper whose cache was GC'd
    # (or never warmed in this process). Library wins when both exist
    # — the saved copy is canonical. Mirrors the pattern of
    # `_discussion_path` which already does library-or-cache.
    folder = pw_config.get_folder()
    candidate_dirs: list[Path] = []
    if folder is not None:
        candidate_dirs.append(rl.paper_dir(folder, slug))
    candidate_dirs.append(RESULTS_DIR / slug)

    packet_yaml = ""
    paper_text = ""
    for paper_dir in candidate_dirs:
        packet_path = paper_dir / "review_session.yaml"
        text_path = paper_dir / "extracted_text.txt"
        if not packet_yaml and packet_path.exists():
            packet_yaml = packet_path.read_text(encoding="utf-8")
        if not paper_text and text_path.exists():
            paper_text = text_path.read_text(encoding="utf-8")
        if packet_yaml and paper_text:
            break

    # Second tire-kick finding: library-saved papers don't have
    # extracted_text.txt — that flat-text dump is a cache-only file
    # that doesn't get copied during /save. But the per-page paper
    # text IS preserved in session_skeleton.yaml under
    # `extracted_source.text.pages`. Reconstruct from there as a
    # fallback so library-only papers still ground Discuss properly.
    if not paper_text:
        for paper_dir in candidate_dirs:
            skel_path = paper_dir / "session_skeleton.yaml"
            if not skel_path.exists():
                continue
            try:
                skel = yaml.safe_load(skel_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            pages = (
                (skel.get("extracted_source") or {})
                .get("text", {})
                .get("pages", [])
            )
            if isinstance(pages, list) and pages:
                paper_text = "\n\n".join(
                    p.get("text", "") for p in pages
                    if isinstance(p, dict)
                ).strip()
                if paper_text:
                    break

    context_hint = ""
    if context_section:
        context_hint = (
            f"\nThe user clicked 'Discuss' on the '{context_section}' section. "
            "Start by focusing on that context, but answer any question they ask.\n"
        )

    return (
        f"{discuss_prompt()}\n"
        f"{context_hint}\n"
        "--- REVIEW PACKET ---\n"
        f"{packet_yaml}\n\n"
        "--- EXTRACTED PAPER TEXT ---\n"
        f"{paper_text}\n"
    )


DEFAULT_DISCUSS_PROMPT = (
    "You are a knowledgeable discussion partner for a PaperWhirl paper review session. "
    "You have read the full paper and the generated review packet below.\n\n"
    "**Default to short answers — 3–4 sentences.** Match length to the question: "
    "a yes/no question gets a one-line answer, a factual lookup gets a sentence. "
    "Only expand to a paragraph or more when the question genuinely needs it — "
    "the user asks for a method walkthrough, a specific quantitative breakdown, "
    "an 'in depth' explanation, or otherwise signals they want detail. "
    "Never pad with preamble, restate the question, or use filler like "
    "'great question' / 'happy to help'. No headers, bullet lists, or section "
    "labels unless the user explicitly asks for them.\n\n"
    "Answer directly, citing specific figures, results, and methods when relevant. "
    "Be opinionated but correctable — if the user spots an error in the review, "
    "acknowledge it. "
    # Stage 5 E7 S18: math renders via KaTeX — use it for equations.
    "When math is the clearest expression, write LaTeX: inline with $...$ and "
    "display with $$...$$ (both render). "
    # Stage 5 E8 S1: per-paper reference lookup tool.
    "When the user asks about a specific citation in this paper (e.g. "
    "'what's reference 12?'), call the lookup_reference tool with the "
    "number rather than guessing — it returns the paper's own "
    "bibliography entry. "
    # Stage 5 E8 S2: per-paper figure-image lookup tool.
    "You have every figure's caption already. When a question needs "
    "the figure's visual content and the caption isn't enough (e.g. "
    "'what's the trend in panel B of figure 3?'), call lookup_figure "
    "with the number — the image is attached so you can read it."
)


def discuss_prompt() -> str:
    """The Discuss system prompt's instruction body — overridable via
    Settings → Advanced → Discussion prompt. Falls back to the default."""
    override = pw_config.get_discussion_prompt()
    return override if override else DEFAULT_DISCUSS_PROMPT


DEFAULT_GENERAL_DISCUSS_PROMPT = (
    "You are a research assistant helping the user explore the scientific "
    "literature. No paper is loaded — the user is asking general questions "
    "about a topic, looking for follow-up reading, or thinking through a "
    "research question.\n\n"
    "**Default to short answers — 3–4 sentences.** Match length to the question: "
    "a yes/no question gets a one-line answer, a factual lookup gets a sentence. "
    "Only expand to a paragraph or more when the question genuinely needs it. "
    "Never pad with preamble, restate the question, or use filler like "
    "'great question' / 'happy to help'. No headers, bullet lists, or section "
    "labels unless the user explicitly asks for them.\n\n"
    "**When asked for paper recommendations, default to 3 strong picks** — "
    "not an exhaustive enumeration. Prefer prose form (\"Smith 2022, Jones "
    "2023, and Chen 2024 are the ones I'd start with…\") over bullet lists. "
    "Give a one-line justification per paper. Only go longer if the user "
    "asks for more or for a comprehensive list.\n\n"
    "You have a `search_papers` tool for finding real papers in PubMed and "
    "preprint servers. **Call it when the user asks for paper recommendations, "
    "specific recent results, or 'find me X' — otherwise answer from your own "
    "knowledge without calling it.** Don't search for conceptual questions "
    "(\"what is a UMAP?\"), opinion questions, or anything you can answer "
    "directly.\n\n"
    "You also have a `list_my_papers` tool that returns the user's saved "
    "library — either every paper or just one named reading list. **Call it "
    "whenever the user references their own collection** — \"my library\", "
    "\"my reading list\", \"the Methods list\", \"papers I've saved on X\", "
    "etc. Two common uses: (1) answer questions directly about the library "
    "(\"what DOIs are in list Y?\" → list them); (2) ground a recommendation "
    "query — pull titles from the matching list, distill 2–3 topic keywords, "
    "then call `search_papers` with those keywords to find similar work. "
    "If the user names a list that doesn't exist, the tool returns the "
    "available list names — apologize briefly and offer to use one of them.\n\n"
    "**Source selection.** Read what the user actually asked:\n"
    "  • `sources=\"all\"` is the default for unspecified queries and "
    "any wording that names BOTH (\"papers or preprints\", \"papers and "
    "preprints\", \"literature\", \"work on X\"). When in doubt, prefer "
    "`\"all\"` — better to surface a peer-reviewed version of a paper "
    "that's both on bioRxiv and in Cell than to hide it.\n"
    "  • `sources=\"preprints\"` ONLY when the user explicitly says "
    "\"preprints\", \"bioRxiv\", \"medRxiv\", or asks for what's brand-new "
    "/ just-submitted / last-week-style fresh.\n"
    "  • `sources=\"published\"` ONLY when the user explicitly says "
    "\"published\", \"peer-reviewed\", \"PubMed\", or asks about an older "
    "foundational paper. Note that PubMed lags weeks-to-months for newly-"
    "published papers — for \"recent\" generic queries, stick with "
    "`\"all\"` so you get both the fresh preprints AND the recently-"
    "published versions.\n\n"
    "**Search budget: 5 calls max per question.** Plan your searches up "
    "front:\n"
    "  • 1 broad query to scan the field. Trust its top results.\n"
    "  • Optionally 1 more if the user's question covers genuinely "
    "distinct topics or if the first returned nothing useful.\n"
    "  • For each specific paper you'll name in the final answer that "
    "did NOT come from the broad searches (classic foundational papers "
    "you know from training), do EXACTLY ONE targeted verification "
    "search (title + first-author + year).\n"
    "  • If your plan would need more than 5 total, recommend fewer "
    "papers instead of fishing for variations of the same query.\n"
    "Do NOT iteratively re-search the same topic with reworded queries — "
    "if the first broad query didn't find what you wanted, the second "
    "rewording almost never will. The whole budget should support 3-5 "
    "recommended papers, each with a verified identifier.\n\n"
    "**Query construction tip: lead with topic / title keywords, not author "
    "surnames.** Search engines (especially bioRxiv) weight content terms "
    "more heavily than author names. \"scalable transcription factor "
    "mapping\" finds the Mullins paper; \"Mullins transcription factor\" "
    "often misses it because the surname is treated as a generic word. If "
    "the user names an author, infer 2-3 likely title terms from the topic "
    "context and search for those — only include the surname when you have "
    "no other context. Same for years: \"2024 single cell deconvolution\" "
    "is rarely better than \"single cell deconvolution\" alone. For "
    "verification searches on a known paper, use a few words from the title "
    "plus author+year (e.g., \"Kawahara glutamate receptors RNA editing 2004\").\n\n"
    "**Every paper you name gets a clickable identifier — verified by a "
    "tool call, NEVER guessed from memory.** Use the exact identifier the "
    "`search_papers` tool returned (`PMID 12345678` for MED records, "
    "`PMC1234567` for PMC records, `doi:10.1101/...` for preprints). The "
    "renderer turns these into clickable buttons. If you want to mention "
    "a paper you know from training (a classic foundational result, an "
    "older paper that didn't come up in your first search), call "
    "`search_papers` again with its title + author + year to verify it and "
    "get the real identifier — see the search-strategy guidance above. "
    "Fabricating identifiers from memory sends users to the wrong paper "
    "(or a 404), which is the worst possible failure mode and erodes "
    "trust in the whole system. If a verification search turns up nothing "
    "credible, name the paper bare and tell the user \"I couldn't find a "
    "verified identifier for this one — search PubMed directly by title.\" "
    "Don't wrap identifiers in markdown links pointing at external URLs; "
    "that defeats the click-to-open flow.\n\n"
    "When naming papers, be concrete (first author, year, journal) so the "
    "user can look them up. Be honest about uncertainty — if you are not "
    "sure a paper exists, or you are working from training-cutoff knowledge "
    "that may be stale, say so."
)


def _build_general_discuss_system_prompt(interests: str | None) -> str:
    """System prompt for general-chat mode (no paper loaded).

    Same voice rules as the per-paper prompt but no packet or paper
    text to ground in. Research interests, when set, get spliced in
    as a labeled fragment the model can use to tailor suggestions.
    """
    if interests and interests.strip():
        interests_block = (
            "\n--- USER RESEARCH INTERESTS ---\n"
            f"{interests.strip()}\n"
        )
    else:
        interests_block = ""
    return f"{DEFAULT_GENERAL_DISCUSS_PROMPT}\n{interests_block}"


@app.post("/api/discuss")
async def discuss(request: Request):
    """Two modes selected by slug presence: truthy slug → per-paper
    thread (packet + paper text grounded); empty → general-chat
    thread (interests-aware, no paper context). Single endpoint so
    streaming / error / abort behavior stays identical across modes."""
    body = await request.json()
    slug = body.get("slug", "")
    messages = body.get("messages", [])
    context_section = body.get("context_section")
    api_key = body.get("api_key", "")

    key = _resolve_key(api_key)
    if not key:
        raise HTTPException(status_code=401, detail="OpenAI API key required")
    os.environ["OPENAI_API_KEY"] = key

    if not messages:
        raise HTTPException(status_code=400, detail="messages required")

    if slug:
        system_prompt = _build_discuss_system_prompt(slug, context_section)
    else:
        system_prompt = _build_general_discuss_system_prompt(
            pw_config.get_interests()
        )
    model = _resolve_model()

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openai package not installed")

    client = OpenAI(api_key=key)

    api_messages = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        api_messages.append({"role": msg["role"], "content": msg["content"]})

    # Tool gating by thread:
    #  - search thread (slug empty)  → `search_papers`.
    #  - per-paper thread (slug set) → `lookup_reference` (E8 S1) and,
    #    later, `lookup_figure` (S2). On-demand only: default behavior
    #    is unchanged — the model calls a tool just when a question
    #    needs it, so a normal conversation runs exactly as before.
    if slug:
        thread_tools = discuss_tools.PAPER_TOOL_SCHEMAS
        paper_references = _load_paper_references(slug)
        paper_figures = _load_paper_figures(slug)
    else:
        thread_tools = discuss_tools.TOOL_SCHEMAS
        paper_references = []
        paper_figures = []
    use_tools = True

    async def stream_response():
        # Tool-use loop. Each pass through the loop streams one
        # assistant turn:
        #   - text deltas → forwarded to the client as SSE
        #   - tool-call deltas → accumulated
        # If the turn ended with tool calls, execute each one, append
        # an assistant message recording the call(s) + tool messages
        # with the results, and loop again. Otherwise break — the
        # assistant has produced its final answer.
        #
        # No tools mode (per-paper): the loop runs exactly once with
        # tool_calls always empty, matching the Stage 4 behavior.
        event_loop = asyncio.get_event_loop()
        loop_messages = list(api_messages)
        while True:
            kwargs = {
                "model": model,
                "messages": loop_messages,
                "stream": True,
            }
            if use_tools:
                kwargs["tools"] = thread_tools
            response = client.chat.completions.create(**kwargs)

            collected_text = ""
            # OpenAI streams tool_call argument fragments incrementally.
            # Index them by `index` (the position in the assistant
            # message's tool_calls array) and concatenate as fragments
            # arrive. `id` and `function.name` show up in the first
            # chunk; `function.arguments` builds up across chunks.
            tool_call_acc: dict[int, dict] = {}

            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    collected_text += delta.content
                    yield f"data: {json.dumps({'content': delta.content})}\n\n"
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        slot = tool_call_acc.setdefault(idx, {
                            "id": None, "name": None, "args": "",
                        })
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments

            if not tool_call_acc:
                break  # plain assistant turn, no tool calls — done

            # Build the assistant message that called the tools, then
            # execute and append the tool results. OpenAI requires the
            # assistant tool-call message to come *before* the tool
            # results in the message list.
            assistant_msg = {
                "role": "assistant",
                "content": collected_text or None,
                "tool_calls": [
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["args"] or "{}",
                        },
                    }
                    for slot in tool_call_acc.values()
                ],
            }
            loop_messages.append(assistant_msg)

            for slot in tool_call_acc.values():
                name = slot["name"] or ""
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                # Status SSE so the panel can show a transient
                # "searching for X..." line while the call runs.
                yield (
                    f"data: {json.dumps({'type': 'tool_call', 'name': name, 'query': args.get('query', ''), 'sources': args.get('sources', 'all'), 'status': 'running'})}\n\n"
                )
                # Dispatch in a thread executor — the preprints path
                # uses sync_playwright (BiorxivSession), which refuses
                # to run inside an asyncio event loop. Without this
                # the call raises silently, our exception handler
                # returns [], and the model reports "0 results" for
                # queries that actually have results. Detaching to a
                # worker thread sidesteps the asyncio loop entirely.
                # Per-paper tools (E8) are dispatched here with the
                # server-injected paper context; search_papers goes
                # through discuss_tools.dispatch as before.
                image_message: dict | None = None
                if name == "lookup_reference":
                    result_json = discuss_tools.lookup_reference(
                        paper_references, args.get("n"),
                    )
                elif name == "lookup_figure":
                    # E8 S2: tool result is a small text confirmation;
                    # the actual image goes in a follow-up user message
                    # (OpenAI tool messages can't carry images, but a
                    # user message with image_url can — the model reads
                    # it on the next loop pass).
                    n = args.get("n")
                    fig = next(
                        (f for f in paper_figures if f.get("n") == n), None
                    )
                    data_url = (
                        _figure_data_url(fig["path"])
                        if fig and fig.get("path") else None
                    )
                    if data_url:
                        result_json = json.dumps({
                            "figure": n,
                            "label": fig.get("label"),
                            "caption": fig.get("caption", "")[:500],
                            "note": "image attached in the next message",
                        })
                        image_message = {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": f"Image of figure {n}:"},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    else:
                        result_json = json.dumps({
                            "error": (
                                f"figure {n} has no image available"
                                if fig else
                                f"no figure numbered {n} in this paper"
                            )
                        })
                else:
                    result_json = await event_loop.run_in_executor(
                        None, lambda: discuss_tools.dispatch(name, args),
                    )
                try:
                    parsed = json.loads(result_json)
                    count = len(parsed) if isinstance(parsed, list) else 0
                except (json.JSONDecodeError, TypeError):
                    count = 0
                yield (
                    f"data: {json.dumps({'type': 'tool_call', 'name': name, 'query': args.get('query', ''), 'sources': args.get('sources', 'all'), 'status': 'done', 'count': count})}\n\n"
                )
                loop_messages.append({
                    "role": "tool",
                    "tool_call_id": slot["id"],
                    "content": result_json,
                })
                # E8 S2: append the figure image as a user message
                # *after* its tool result (valid OpenAI ordering).
                if image_message is not None:
                    loop_messages.append(image_message)
            # Loop runs again with the tool results in context.

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# E7 Phase A — Reading List endpoints (local file storage)
# ---------------------------------------------------------------------------


def _require_folder() -> Path:
    folder = pw_config.get_folder()
    if folder is None:
        raise HTTPException(
            status_code=409,
            detail="No data folder configured yet. POST /api/config first.",
        )
    if not folder.exists():
        # Folder went missing (Dropbox unmounted, user moved it, etc).
        raise HTTPException(
            status_code=410,
            detail=f"Configured data folder no longer exists: {folder}",
        )
    return folder


def _config_response() -> dict:
    """The shape returned by GET and PUT /api/config. The raw API key
    never leaves the backend — only a masked fragment and a boolean.
    `has_api_key` / `model` reflect the *effective* values (env var
    overrides included), so the panel shows what generation will use."""
    cfg = pw_config.load_config() or {}
    effective_key = _resolve_key()
    return {
        "folder": cfg.get("folder"),
        "has_api_key": bool(effective_key),
        "api_key_masked": _mask_key(effective_key),
        "model": _resolve_model(),
        "model_options": _model_options,
        "default_model": _default_model,
        "generation_prompt": cfg.get("generation_prompt", ""),
        "default_generation_prompt": DEFAULT_SECTION_PREAMBLE,
        "discussion_prompt": cfg.get("discussion_prompt", ""),
        "default_discussion_prompt": DEFAULT_DISCUSS_PROMPT,
        "interests": cfg.get("interests", ""),
    }


def _set_folder(folder_raw: str) -> Path:
    """Validate, create, and bootstrap a data folder. Returns the
    resolved Path. Raises HTTPException on a bad path."""
    folder = Path(folder_raw).expanduser().resolve()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not create folder: {exc}")
    pw_config.bootstrap_folder(folder)
    return folder


@app.get("/api/config")
async def get_config():
    return _config_response()


@app.post("/api/config")
async def set_config(request: Request):
    """First-run: set the data folder. The Settings panel uses PUT."""
    body = await request.json()
    folder_raw = (body or {}).get("folder", "").strip()
    if not folder_raw:
        raise HTTPException(status_code=400, detail="folder required")
    folder = _set_folder(folder_raw)
    pw_config.update_config(folder=folder)
    return _config_response()


@app.put("/api/config")
async def put_config(request: Request):
    """Settings panel: merge-update any subset of the config fields."""
    body = await request.json() or {}
    allowed = (
        "folder",
        "api_key",
        "model",
        "reasoning_effort",
        "generation_prompt",
        "discussion_prompt",
        "interests",
    )
    fields = {k: body[k] for k in allowed if k in body}
    if not fields:
        raise HTTPException(status_code=400, detail="no recognised config fields")

    folder_raw = str(fields.get("folder", "")).strip()
    if folder_raw:
        fields["folder"] = str(_set_folder(folder_raw))
    pw_config.update_config(**fields)
    return _config_response()


@app.get("/api/lists")
async def list_lists():
    folder = _require_folder()
    return rl.read_index(folder)


@app.post("/api/lists")
async def create_list(request: Request):
    folder = _require_folder()
    body = await request.json()
    name = (body or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    description = (body or {}).get("description", "")
    return rl.create_list(folder, name, description)


@app.get("/api/lists/{slug}")
async def get_list(slug: str):
    folder = _require_folder()
    data = rl.get_list(folder, slug)
    if data is None:
        raise HTTPException(status_code=404, detail=f"list {slug!r} not found")
    return data


@app.put("/api/lists/{slug}")
async def put_list(slug: str, request: Request):
    folder = _require_folder()
    body = await request.json()
    data = rl.update_list(
        folder, slug,
        name=body.get("name"),
        description=body.get("description"),
        papers=body.get("papers"),
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"list {slug!r} not found")
    return data


@app.delete("/api/lists/{slug}")
async def delete_list(slug: str):
    """Delete a reading list. Cascade-deletes any papers that aren't
    in any other list — Stage 5 E4 fix to the "silently orphans
    papers" issue. Response includes the cascaded slugs so the
    frontend can update its state and (optionally) surface what
    was removed."""
    folder = _require_folder()
    result = rl.delete_list(folder, slug)
    if result is None:
        raise HTTPException(status_code=404, detail=f"list {slug!r} not found")
    return {
        "deleted": slug,
        "cascaded_papers": result.get("cascaded_papers", []),
    }


@app.get("/api/lists/{slug}/orphan_preview")
async def list_orphan_preview(slug: str):
    """Stage 5 E4: returns the list of paper slugs that would become
    unreferenced (and therefore cascade-deleted) if this list were
    deleted. The frontend hits this BEFORE confirming so the dialog
    can show an accurate count regardless of which lists are
    currently expanded in the rail."""
    folder = _require_folder()
    orphans = rl.preview_list_orphans(folder, slug)
    if orphans is None:
        raise HTTPException(status_code=404, detail=f"list {slug!r} not found")
    return {"orphans": orphans, "count": len(orphans)}


@app.post("/api/lists/{slug}/papers")
async def add_paper(slug: str, request: Request):
    folder = _require_folder()
    body = await request.json()
    paper_slug = (body or {}).get("paper_slug", "").strip()
    if not paper_slug:
        raise HTTPException(status_code=400, detail="paper_slug required")
    data = rl.add_paper_to_list(folder, slug, paper_slug)
    if data is None:
        raise HTTPException(status_code=404, detail=f"list {slug!r} not found")
    return data


@app.delete("/api/lists/{slug}/papers/{paper_slug}")
async def remove_paper(slug: str, paper_slug: str):
    folder = _require_folder()
    data = rl.remove_paper_from_list(folder, slug, paper_slug)
    if data is None:
        raise HTTPException(status_code=404, detail=f"list {slug!r} not found")
    return data


def _resolve_paper(
    folder: Path, slug: str,
) -> tuple[dict, Path, str, str] | None:
    """E8: return (packet, dir, display_name, base_url) for a slug,
    preferring the library copy and falling back to the cache copy.
    Used by GET endpoints that should work for both saved papers and
    browse-mode (cache-only) papers — so the user can download a PDF
    or markdown summary without first committing to a reading list.
    Returns None when the slug exists in neither.
    """
    if rl.is_saved(folder, slug):
        packet = rl.get_paper(folder, slug)
        if packet is None:
            return None
        return (
            packet,
            rl.paper_dir(folder, slug),
            rl.get_display_name(folder, slug),
            f"/api/papers/{slug}/assets",
        )
    cache_packet_path = RESULTS_DIR / slug / "review_session.yaml"
    if cache_packet_path.exists():
        packet = load_yaml(cache_packet_path)
        return (
            packet,
            RESULTS_DIR / slug,
            # Browse-mode has no display_name.txt — auto-derive the
            # same shortname save_packet would use as its default.
            rl.auto_shortname(packet) if packet else slug,
            f"/api/assets/{slug}",
        )
    return None


@app.get("/api/papers/{slug}")
async def get_paper(slug: str):
    folder = _require_folder()
    resolved = _resolve_paper(folder, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"paper {slug!r} not found")
    packet, pdir, display_name, base_url = resolved
    # Stage 6 E5: a no-identifier saved paper (sample_et_al-style)
    # has its source persisted as uploaded.pdf — the frontend's
    # Download-manuscript gate can't infer that from the packet
    # metadata alone (no DOI / PMCID / arXiv ID to match on), so
    # surface a flag.
    has_local_manuscript = (
        (pdir / "manuscript.pdf").exists()
        or (pdir / "uploaded.pdf").exists()
    )
    # Stage 6 E7 (2026-05-26): surface the lists this paper currently
    # belongs to so PacketView can render a small membership line.
    list_memberships = rl.lists_containing_paper(folder, slug)
    return {
        "packet": packet,
        "slug": slug,
        "display_name": display_name,
        "base_url": base_url,
        "has_local_manuscript": has_local_manuscript,
        "list_memberships": list_memberships,
    }


@app.post("/api/papers/{slug}/save")
async def save_paper(slug: str, overwrite: bool = False):
    """Idempotent "ensure this paper is saved" endpoint.

    Behavior matrix:
    - data-folder copy already exists, no `overwrite`:
      no-op; return the existing save info. This is what makes the
      endpoint safe to call from `SaveToList.ensureSaved` on a
      reopened paper (which has no fresh cache) without 404'ing.
    - data-folder copy exists, `overwrite=true`:
      copies the fresh cache over the saved version. Used by the
      E8 Re-generate button on the saved-paper view.
    - data-folder copy doesn't exist, fresh cache exists:
      first-time save (Save-to-list).
    - neither exists: 404.
    """
    folder = _require_folder()
    source = RESULTS_DIR / slug
    saved = rl.is_saved(folder, slug)

    if saved and not overwrite:
        # Idempotent no-op. Return the same shape save_packet would
        # have returned so SaveToList.ensureSaved is happy.
        from datetime import datetime, timezone
        return {
            "slug": slug,
            "display_name": rl.get_display_name(folder, slug),
            "saved_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
        }

    if not source.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no active extraction for {slug!r} at {source}",
        )
    # E8 Re-generate: don't clear discussion here. The frontend's
    # regeneratePaper handler PUTs an empty discussion BEFORE starting
    # the stream, so the panel is empty during regen and the user can
    # start a fresh thread mid-stream that we want to preserve. If we
    # cleared at save_packet time, those mid-stream messages would be
    # lost. (Display name + list memberships still stay — they live
    # outside discussion.yaml and outside the cache copy.)
    result = rl.save_packet(folder, slug, source)
    # E8: drop the cache copy now that the durable copy is in place.
    # Avoids the orphan-cache pileup that surfaced during the E7
    # tire-kick (142 MB / 23 papers on the dev box).
    try:
        import shutil as _shutil
        _shutil.rmtree(source)
    except OSError as exc:
        print(
            f"[paperwhirl] post-save cache sweep: could not remove {source}: {exc}",
            file=sys.stderr,
        )
    return result


@app.delete("/api/papers/{slug}")
async def delete_paper(slug: str):
    """E8: remove a paper from the library completely. Deletes
    `<data>/papers/<slug>/` AND drops the slug from every list yaml's
    `papers:` array. Idempotent — 404 only when the paper dir is
    genuinely absent.
    """
    folder = _require_folder()
    if not rl.delete_paper(folder, slug):
        raise HTTPException(status_code=404, detail=f"paper {slug!r} not in library")
    return {"deleted": slug}


@app.put("/api/papers/{slug}/display_name")
async def put_display_name(slug: str, request: Request):
    folder = _require_folder()
    body = await request.json()
    name = (body or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    rl.set_display_name(folder, slug, name)
    return {"slug": slug, "display_name": name}


def _discussion_path(folder: Path, slug: str) -> Path | None:
    """E8: pick the right discussion.yaml location.

    Library copy if the paper is saved; else the cache copy if a
    cache extraction exists; None if neither exists (caller maps to
    a no-op or 404).
    """
    if rl.is_saved(folder, slug):
        return rl.paper_dir(folder, slug) / "discussion.yaml"
    cache_dir = RESULTS_DIR / slug
    if cache_dir.exists():
        return cache_dir / "discussion.yaml"
    return None


@app.get("/api/papers/{slug}/discussion")
async def get_paper_discussion(slug: str):
    folder = _require_folder()
    path = _discussion_path(folder, slug)
    if path is None or not path.exists():
        return {"messages": []}
    data = yaml.safe_load(path.read_text()) or {}
    return {"messages": data.get("messages", [])}


@app.put("/api/papers/{slug}/discussion")
async def put_paper_discussion(slug: str, request: Request):
    folder = _require_folder()
    # Stage 6 E7 follow-up (2026-05-27): the frontend auto-PUTs
    # Discuss state on most state changes. When something cancels
    # the request mid-body (window.location.reload from the
    # Settings folder-change fix, rapid paper switching, page
    # navigation) starlette raises ClientDisconnect and FastAPI
    # logs a noisy 500-shaped ASGI traceback even though nothing
    # is actually wrong — the next successful PUT supersedes
    # this one. Catch it, return 499 (Client Closed Request)
    # quietly. The handler is idempotent on retry, so the user
    # never sees a missing save.
    from starlette.requests import ClientDisconnect
    try:
        body = await request.json()
    except ClientDisconnect:
        return Response(status_code=499)
    messages = (body or {}).get("messages", [])
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    path = _discussion_path(folder, slug)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"paper {slug!r} not in library or cache",
        )
    from datetime import datetime, timezone
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "schema_version": "paperwhirl.discussion.v1",
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "messages": messages,
    }, sort_keys=False, allow_unicode=True))
    return {"slug": slug, "count": len(messages)}


def _packet_markdown(packet: dict, display_name: str, discussion: list[dict] | None = None) -> str:
    """Render a packet (and optional Discuss thread) as markdown.

    Mirrors what the user sees in PacketView: overview -> figures/tables
    in source order -> closing discussion -> (optional) chat thread.
    """
    lines: list[str] = []
    p = packet
    title = (p.get("session") or {}).get("title") or (p.get("paper") or {}).get("title") or display_name
    lines.append(f"# {title}\n")
    paper = p.get("paper") or {}
    authors = paper.get("authors") or []
    byline_bits: list[str] = []
    if authors:
        first = authors[0].split()[-1]
        if len(authors) >= 3:
            byline_bits.append(f"{first} et al.")
            byline_bits.append(f"Senior Author: {authors[-1]}")
        elif len(authors) == 2:
            byline_bits.append(f"{first} & {authors[-1].split()[-1]}")
        else:
            byline_bits.append(first)
    venue_bits: list[str] = []
    if paper.get("journal"): venue_bits.append(paper["journal"])
    if paper.get("year"): venue_bits.append(str(paper["year"]))
    if venue_bits: byline_bits.append(" ".join(venue_bits))
    if byline_bits:
        lines.append(f"*{'. '.join(byline_bits)}.*\n")

    ov = p.get("overview") or {}
    lines.append("\n## Overview\n")
    if ov.get("background"):
        lines.append(f"**Background.** {ov['background']}\n")
    if ov.get("gap"):
        lines.append(f"**Gap.** {ov['gap']}\n")
    claims = ov.get("claims") or []
    if claims:
        lines.append("**Main claims.**")
        for i, c in enumerate(claims, 1):
            lines.append(f"{i}. {c.get('statement', '')}")
        lines.append("")

    # Figures + tables interleaved by source_order.
    items: list[tuple[int, str, dict]] = []
    for f in (p.get("figures") or []):
        items.append((f.get("source_order", 0), "figure", f))
    for t in (p.get("tables") or []):
        items.append((t.get("source_order", 0), "table", t))
    items.sort(key=lambda it: it[0])

    for _, kind, item in items:
        label = item.get("label") or item.get("id", "")
        lines.append(f"\n## {label.rstrip('.')}\n")
        view = item.get("view") or {}
        if kind == "figure":
            cap = view.get("original_caption") or view.get("display_legend") or ""
            if cap:
                lines.append(f"*{cap}*\n")
            an = item.get("analysis") or {}
            for k, lbl in [
                ("motivation", "Motivation"), ("question", "Question"),
                ("approach", "Approach"), ("evidence", "Evidence"),
                ("interpretation", "Interpretation"),
            ]:
                if an.get(k):
                    lines.append(f"**{lbl}.** {an[k]}\n")
            links = an.get("linked_claims") or []
            if links:
                lines.append(f"_Linked claims: {', '.join(c.replace('_', ' ') for c in links)}_\n")
        else:
            cap = view.get("caption_text") or ""
            if cap:
                lines.append(f"*{cap}*\n")
            an = item.get("analysis") or {}
            if an.get("interpretation"):
                lines.append(f"**Interpretation.** {an['interpretation']}\n")

    disc = p.get("discussion") or {}
    if disc.get("synthesis") or disc.get("takeaways") or disc.get("next_steps"):
        lines.append("\n## Closing\n")
        if disc.get("synthesis"):
            lines.append(f"{disc['synthesis']}\n")
        if disc.get("takeaways"):
            lines.append("**Takeaways.**")
            for t in disc["takeaways"]:
                lines.append(f"- {t}")
            lines.append("")
        if disc.get("caveats"):
            lines.append("**Caveats.**")
            for c in disc["caveats"]:
                lines.append(f"- {c}")
            lines.append("")
        if disc.get("next_steps"):
            lines.append("**Next steps.**")
            for s in disc["next_steps"]:
                lines.append(f"- {s}")
            lines.append("")

    if discussion:
        lines.append("\n## Discussion thread\n")
        for m in discussion:
            role = "**You**" if m.get("role") == "user" else "**Assistant**"
            lines.append(f"{role}: {m.get('content', '')}\n")

    return "\n".join(lines)


def _safe_filename(name: str) -> str:
    import re
    n = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-") or "paperwhirl"
    return n


@app.get("/api/papers/{slug}/summary.md")
async def get_summary_md(slug: str):
    folder = _require_folder()
    resolved = _resolve_paper(folder, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"paper {slug!r} not found")
    packet, _, name, _ = resolved
    md = _packet_markdown(packet, name)
    return _markdown_response(md, f"{_safe_filename(name)}_summary.md")


@app.get("/api/papers/{slug}/summary_with_discussion.md")
async def get_summary_with_discussion_md(slug: str):
    folder = _require_folder()
    resolved = _resolve_paper(folder, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"paper {slug!r} not found")
    packet, _, name, _ = resolved
    # Discussion may live in cache (browse-mode) or library (saved) —
    # _discussion_path picks the right one.
    disc_path = _discussion_path(folder, slug)
    disc = []
    if disc_path is not None and disc_path.exists():
        data = yaml.safe_load(disc_path.read_text()) or {}
        disc = data.get("messages", [])
    md = _packet_markdown(packet, name, discussion=disc)
    return _markdown_response(md, f"{_safe_filename(name)}_summary_with_discussion.md")


def _markdown_response(md: str, filename: str):
    from starlette.responses import Response
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/papers/{slug}/pdf")
async def get_packet_pdf(
    slug: str, request: Request, include_discussion: bool = False
):
    folder = _require_folder()
    resolved = _resolve_paper(folder, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"paper {slug!r} not found")
    _, _, name, _ = resolved
    from paperwhirl.printer import render_pdf

    mode = "full_with_discussion" if include_discussion else "full"
    # Use the request's own base URL so the printer renders against the
    # backend at whatever host:port we're actually serving on.
    base = str(request.base_url).rstrip("/")
    url = f"{base}/?slug={slug}&print={mode}"
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, render_pdf, url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF render failed: {exc}")
    suffix = "_review_with_discussion.pdf" if include_discussion else "_review.pdf"
    from starlette.responses import Response
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_filename(name)}{suffix}"',
        },
    )


def _fetch_manuscript_pdf_by_family(paper: dict) -> bytes:
    """Dispatch a publisher PDF fetch via the shared resolver helper.

    Stage 6 E7 follow-up (2026-05-27): this used to be a separate
    in-server dispatcher that knew only the original 6 publisher
    families (PMC / arXiv / bioRxiv / Nature / Cell / Science).
    The Stage 6 E9 work added eLife / Frontiers / PLOS / PNAS /
    Oxford / JNeurosci / Royal Society fetchers to
    `fetch_publisher_pdf_bytes` in resolve.py but this server-
    side duplicate was overlooked — so downloads for the new
    families silently fell out of the cascade and returned 404.
    Now delegates to the single shared dispatcher; the `errors`
    accumulator (added in the same commit) lets us surface a
    detailed reason in the 404 response.
    """
    from paperwhirl.resolve import fetch_publisher_pdf_bytes

    errors: list[str] = []
    # patient=True: this is the user-initiated Download-manuscript
    # path. Waiting up to 15s for PMC's POW cookie is fine here
    # (extraction-time callers stay on the default 3s).
    data = fetch_publisher_pdf_bytes(paper, errors=errors, patient=True)
    if data:
        return data
    if errors:
        raise ValueError(
            "could not fetch manuscript PDF from any source — "
            + "; ".join(errors)
        )
    raise ValueError("no recognised identifier on the paper")


@app.get("/api/papers/{slug}/manuscript.pdf")
async def get_manuscript_pdf(slug: str):
    """Serve (and cache) the original publisher PDF for a saved paper.

    Resolution order:
    1. `<paper_dir>/manuscript.pdf` already cached → stream it.
    2. `<paper_dir>/uploaded.pdf` exists (user drop / bioRxiv PDF
       fallback) → copy to manuscript.pdf and stream.
    3. Family-specific fetch (PMC, arXiv, bioRxiv via Playwright,
       Nature, Cell, Science via Playwright) → cache and stream.
    4. 404 with the underlying reason.

    Frontend hits this same-origin URL so the `download` HTML
    attribute works (was being ignored cross-origin to the
    publisher) and so cross-origin / Cloudflare / paywall friction
    never reaches the browser.
    """
    folder = _require_folder()
    resolved = _resolve_paper(folder, slug)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"paper {slug!r} not found")
    paper_packet, pdir, name, _ = resolved

    cached = pdir / "manuscript.pdf"

    def _respond(path: Path) -> FileResponse:
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"{_safe_filename(name)}_manuscript.pdf",
        )

    if cached.exists():
        return _respond(cached)

    # Fast path: bioRxiv PDF-fallback and user-uploaded flows saved
    # the source PDF as uploaded.pdf — reuse it as the manuscript.
    uploaded = pdir / "uploaded.pdf"
    if uploaded.exists():
        try:
            import shutil
            shutil.copyfile(uploaded, cached)
        except OSError:
            return _respond(uploaded)
        return _respond(cached)

    # Else: family-specific fetch. Runs in an executor — Playwright
    # is sync and would block the event loop.
    paper_meta = paper_packet.get("paper", {}) or {}
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: _fetch_manuscript_pdf_by_family(paper_meta)
        )
    except Exception as exc:
        # Family helpers throw ValueError for "no identifier" and
        # RuntimeError when the publisher refuses the PDF (paywall on
        # this network, Cloudflare 403, etc.). Both want a 404 with
        # the underlying reason so the frontend can surface it
        # cleanly.
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        cached.write_bytes(data)
    except OSError as exc:
        print(f"[paperwhirl] warning: could not cache manuscript {slug!r}: {exc}",
              file=sys.stderr)
        # Still serve the bytes we have — caching is a perf hint, not
        # a requirement.
        from starlette.responses import Response
        return Response(
            content=data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{_safe_filename(name)}_manuscript.pdf"',
            },
        )
    return _respond(cached)


@app.get("/api/papers/{slug}/assets/{path:path}")
async def get_paper_asset(slug: str, path: str):
    folder = _require_folder()
    file_path = rl.paper_dir(folder, slug) / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    # Resolve to prevent path traversal.
    try:
        file_path.resolve().relative_to(rl.paper_dir(folder, slug).resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="bad path")
    return FileResponse(file_path)


# ---------------------------------------------------------------------------
# Frontend mount + entry points
# ---------------------------------------------------------------------------

# The bundled React build lives next to this module as `frontend_dist/`.
# `scripts/build.sh` (or equivalent) populates it from
# `src/web/frontend/dist/` before `pip install`.
_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend_dist"


class _NoCacheHTML(StaticFiles):
    """StaticFiles that sets Cache-Control: no-store on the entry HTML.

    Hashed bundle assets (`index-<hash>.js`, `index-<hash>.css`) keep
    long-lived caching — their filenames change when content changes.
    Only the entry HTML must always be fresh so the browser picks up
    new bundle hashes on each load.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Path is "" for directory roots (served as index.html via html=True),
        # or the literal "*.html" for direct hits.
        if path == "" or path.endswith(".html") or path == ".":
            response.headers["Cache-Control"] = "no-store"
        return response


if _FRONTEND_DIST.exists():
    app.mount("/", _NoCacheHTML(directory=str(_FRONTEND_DIST), html=True), name="frontend")


def _read_host_port() -> tuple[str, int]:
    """Resolve PAPERWHIRL_HOST / PAPERWHIRL_PORT with safe loopback defaults."""
    return (
        os.environ.get("PAPERWHIRL_HOST", "127.0.0.1"),
        int(os.environ.get("PAPERWHIRL_PORT", "8000")),
    )


def _start_parent_death_watchdog() -> None:
    """Sidecar self-terminates when its Tauri-shell parent dies.

    Stage 6 E9 follow-on (2026-05-27). E7 batch 3's
    `WindowEvent::Destroyed → app_handle.exit(0)` catches the
    normal quit-window-button path but not Activity Monitor force-
    quits, OS-initiated kills, or crashes. Tire-kick on 2026-05-27
    showed four orphan sidecars accumulated across multiple
    .app launches — and one of them silently handled a DOI paste
    using a stale code path.

    Watchdog approach: record the initial parent PID at startup;
    on macOS, when the parent exits, the child gets reparented to
    launchd (PID 1). The daemon thread polls `os.getppid()` every
    few seconds and `os._exit`s when reparented or when the parent
    PID changes. Gated on `PAPERWHIRL_MANAGED_BY_TAURI=1` (set by
    sidecar.rs) so the dev `paperwhirl` console-script terminal
    workflow — where the user IS the parent and Ctrl-C is fine —
    keeps working unchanged.
    """
    if os.environ.get("PAPERWHIRL_MANAGED_BY_TAURI") != "1":
        return
    initial_ppid = os.getppid()
    if initial_ppid == 1:
        # Already orphaned at startup (shouldn't happen under Tauri,
        # but guard against the pathological case to avoid an
        # immediate self-exit).
        return

    def _watchdog() -> None:
        import sys as _sys
        import time as _time
        while True:
            _time.sleep(5)
            try:
                ppid = os.getppid()
            except OSError:
                ppid = 1
            if ppid != initial_ppid or ppid == 1:
                print(
                    f"[sidecar-watchdog] parent {initial_ppid} died "
                    f"(ppid now {ppid}); exiting",
                    file=_sys.stderr, flush=True,
                )
                os._exit(0)

    import threading
    t = threading.Thread(target=_watchdog, daemon=True, name="parent-death-watchdog")
    t.start()


def main() -> None:
    """Packaged entry point — called by the `paperwhirl` console script.

    Starts uvicorn against this module's `app`. Reload is off here: the
    installed app has no `src/` tree to watch and shouldn't restart on
    site-packages changes. For the hot-reload dev loop, run
    `python -m paperwhirl.web.server` instead — that path keeps reload
    on against the live `src/paperwhirl/` tree.
    """
    _start_parent_death_watchdog()
    host, port = _read_host_port()
    uvicorn.run("paperwhirl.web.server:app", host=host, port=port)


if __name__ == "__main__":
    # Dev recipe: `python -m paperwhirl.web.server` keeps auto-reload on
    # against the live `src/paperwhirl/` tree (works with `pip install
    # -e .`). The console script `paperwhirl` calls `main()` above with
    # reload off — the packaged path doesn't need to watch source files.
    _PAPERWHIRL_DIR = Path(__file__).resolve().parent.parent
    host, port = _read_host_port()
    uvicorn.run(
        "paperwhirl.web.server:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=[str(_PAPERWHIRL_DIR)],
    )
