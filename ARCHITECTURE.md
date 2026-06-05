# Architecture

PaperWhirl is a **standalone macOS desktop app** that turns a paper
(pasted identifier or search hit) into an interactive, figure-grounded
review session. It runs entirely on the user's machine — there is no
hosted backend — which is also what gives it subscription-paywalled
publisher access: requests originate from the user's own network.

This document is the map. It explains the process model, the
extraction pipeline that is the heart of the system, how summaries are
generated, and how the whole thing is packaged into a double-clickable
`.app`.

## 1. Process model

The app is two cooperating processes wrapped in one bundle:

```
┌─────────────────────────── PaperWhirl.app ───────────────────────────┐
│                                                                       │
│   Tauri shell (Rust)              Python backend (FastAPI + uvicorn)  │
│   ─────────────────               ──────────────────────────────     │
│   • spawns the backend as a       • extraction pipeline (resolve →    │
│     sidecar on a random free        family extractors → skeleton)     │
│     port, polls /api/health       • LLM generation (per-section,      │
│   • shows a splash until ready      streamed over SSE)                │
│   • navigates the native          • Reading List storage (files)      │
│     WebView to 127.0.0.1:<port>   • serves the built React SPA, so    │
│   • SIGKILLs the backend on quit    UI + API are same-origin          │
│                                                                       │
│   React SPA (Vite build) ── served by the backend, talks to /api ──   │
└───────────────────────────────────────────────────────────────────────┘
```

Key choices:

- **Sidecar, not embedded.** The Rust shell launches the bundled Python
  interpreter as a subprocess (`sidecar.rs`), waits for `/api/health`,
  then points the WebView at the local server. This keeps the heavy
  Python/Chromium/Poppler stack out of the UI process.
- **Same-origin.** The backend serves the React build, so the UI and the
  API share an origin. This removes CORS entirely and lets `<a download>`
  work inside the macOS WebView.
- **Everything is bundled.** A relocatable Python (python-build-standalone),
  a Chromium for Playwright, and Poppler binaries all ship inside
  `Contents/Resources/` — the user needs no terminal, Python, or other
  prerequisites.

## 2. Repository layout

```
src/paperwhirl/        Python package — the engine
  resolve.py           the pipeline entry point (identifier/PDF → skeleton)
  pmc.py biorxiv.py     family extractors, one module per source/publisher
  nature.py cell.py     (each: detector → fetch → parse → skeleton builder)
  science.py mdpi.py
  arxiv.py elife.py
  pnas.py frontiers.py
  oxford.py plos.py
  jneurosci.py royalsoc.py
  extract.py           generic PDF fall-through (Poppler + pdfplumber)
  generate.py          LLM generation: prompts, schemas, the OpenAI call
  reading_lists.py     file-based Reading List storage
  config.py            ~/.paperwhirl/config.json (API key, model, folder)
  slugs.py migration.py slug derivation + one-time library migrations
  web/
    server.py          FastAPI app: /api/generate/stream, /api/discuss, …
    discuss_tools.py   on-demand tools the Discuss LLM can call

src/web/frontend/      React + Vite + Tailwind single-page app
  src/                 components (PacketView, DiscussPanel, Settings, …)
  src-tauri/           the Rust shell, entitlements, build/sign scripts

scripts/               top-level build helpers
models.json            the model-menu manifest the app fetches on startup
```

## 3. The extraction pipeline (the hard part)

Turning an arbitrary paper into a clean, figure-grounded structure is
the core problem. PDF parsing is layout-fragile, so PaperWhirl **prefers
structured sources** and only falls back to PDF when nothing better
exists.

```
input (DOI / PMID / PMCID / arXiv id / URL / PDF)
  → identify   sniff the family; extract a DOI (PDF stamp → regex →
               Crossref title lookup with a score-margin guard)
  → resolve    upgrade the DOI to the cleanest available source
  → dispatch   pick the extractor for that source/family
  → extract    family module → common "session skeleton" schema
```

**Source preference order** (cleanest first):

1. **PMC NXML** — structured XML; captions and sections are first-class
   fields, not a parsing problem.
2. **Publisher HTML / JATS** — Nature, Cell, Science, eLife, PNAS,
   Frontiers, Oxford, PLOS, JNeurosci, Royal Society, MDPI. Each is a
   small module with the same shape (detector → fetcher → parser →
   skeleton builder → figure hydration).
3. **bioRxiv / medRxiv JATS** — preprint XML, fetched through a
   Playwright Cloudflare warm-up.
4. **arXiv** — direct preprint source.
5. **Generic PDF** — the universal fall-through: Poppler + pdfplumber
   extract text and crop figures; Crossref enriches metadata from a
   DOI found in the text.

Two deliberate invariants:

- **Preprints are never substituted for paywalled published papers.**
  A bioRxiv/arXiv source is used only when the *target itself* is that
  preprint — peer review changes figures and claims, and a review must
  match the version it claims to review.
- **All extractors emit the same skeleton schema** (overview, ordered
  figures with captions + assets, tables, body text, metadata), so
  everything downstream is source-agnostic.

## 4. Generation

The skeleton feeds an LLM that produces the review packet — but the
*renderer never calls the LLM*; generation produces structured prose,
rendering only formats it.

- **Per-section streaming.** The overview, each figure analysis, and the
  closing discussion are separate structured calls, streamed to the
  client over Server-Sent Events so the UI fills in progressively
  instead of waiting for one giant response.
- **Strict structured output.** Each call uses a JSON schema (OpenAI
  `responses` API, strict mode), so the packet shape is guaranteed and
  parsing is trivial.
- **Curated, self-updating model menu.** The selectable models come from
  `models.json`, fetched from this repo on startup with a baked-in
  fallback — so the menu tracks the current model lineup without
  shipping a new build, while staying a vetted dropdown (no free-text
  model field).
- **Interactive Discuss.** A separate streaming chat endpoint grounds
  the model in the packet + extracted paper text, and exposes on-demand
  tools (look up a reference, look up a figure, search for papers).

## 5. Reading Lists

Saved papers and lists are plain files under a user-chosen folder
(`papers/` + `lists/`), with no database and no auth. Pointing the app
at a Dropbox/iCloud folder gives cross-machine sync for free. A startup
sweep enforces the invariant that every saved paper belongs to at least
one list.

## 6. Packaging & distribution

`npm run build-app` produces the `.app`: a Tauri release build, then
injection of the bundled Python, Chromium, and Poppler trees. The app is
then Developer-ID signed with a hardened runtime
(`src-tauri/scripts/sign_app.sh`), notarized and stapled
(`notarize.sh`), and zipped with `ditto` into a Gatekeeper-clean
distributable. The result installs by dragging `PaperWhirl.app` to
Applications — no warnings, no prerequisites.
