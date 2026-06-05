"""LLM tools for the search-thread Discuss mode (Stage 5 E2).

Exposes a single `search_papers` tool against Europe PMC, which
indexes both PubMed and the major preprint servers (bioRxiv,
medRxiv, ResearchSquare, ChemRxiv, ...). The `sources` arg
filters the result set to all / published-only / preprints-only.

The OpenAI tool-dispatch loop in `web.server.discuss` looks up
functions here by name (`TOOL_SCHEMAS` defines the schemas;
`dispatch(name, args)` runs the call and returns a JSON-encoded
string of the result).

Out of scope (intentionally): NCBI E-utilities, the literal
api.biorxiv.org endpoints, rate-limit token buckets. Europe PMC
covers the queries we care about in one call with no per-second
limit concerns at our usage scale.
"""

from __future__ import annotations

import json
from typing import Any

import requests

EUROPEPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_UA = "PaperWhirl/0.1 (mailto:rob.mitra@gmail.com)"


def _build_query(query: str, sources: str) -> str:
    """Append a SRC: filter to scope the search to a subset of
    Europe PMC's index. 'all' passes the query through unchanged."""
    base = query.strip()
    if not base:
        return ""
    if sources == "published":
        return f"({base}) AND SRC:MED"
    if sources == "preprints":
        return f"({base}) AND SRC:PPR"
    return base  # "all" or anything unrecognized


def _pick_identifier(rec: dict) -> tuple[str | None, str | None]:
    """Pick the most-useful resolvable identifier for a Europe PMC
    record. Returns (id, kind) where `kind` is one of "pmid", "pmcid",
    "doi"; or (None, None) if the record has nothing the extract
    pipeline can resolve.

    Priority: PMID first (cleanest path for the existing pipeline,
    which maps PMID → PMCID → NXML); PMCID next; DOI last (covers
    preprints).
    """
    pmid = (rec.get("pmid") or "").strip()
    if pmid:
        return pmid, "pmid"
    pmcid = (rec.get("pmcid") or "").strip()
    if pmcid:
        return pmcid, "pmcid"
    doi = (rec.get("doi") or "").strip()
    if doi:
        return doi, "doi"
    return None, None


def _author_list(rec: dict, max_authors: int = 3) -> list[str]:
    """Europe PMC returns authors as a single 'Smith J, Jones K, ...'
    string. Split on ', ' and cap so the model doesn't see a 40-author
    list for every record."""
    raw = (rec.get("authorString") or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) > max_authors:
        return parts[:max_authors] + ["et al."]
    return parts


def search_papers(
    query: str,
    max_results: int = 5,
    sources: str = "all",
) -> list[dict[str, Any]]:
    """Search the scientific literature for papers matching `query`.

    Args:
        query: free-text search query. Plain keywords work; Europe
            PMC additionally supports rich syntax (AUTH:"Smith J",
            JOURNAL:Nature, etc.).
        max_results: how many records to return. Default 5 — keeps the
            model's context lean and matches the system-prompt
            instruction to recommend ~3 papers by default.
        sources: which index to hit —
            - "all" → Europe PMC (published + preprints + everything)
            - "published" → Europe PMC scoped to SRC:MED (PubMed)
            - "preprints" → **bioRxiv directly** via its search page,
              NOT Europe PMC's SRC:PPR filter. Europe PMC's preprint
              ingest lags bioRxiv by days-to-weeks for recent
              submissions; for time-sensitive discovery the direct
              path is required. Slower (~10 s due to Playwright
              Cloudflare warm-up) but accurate.

    Returns:
        List of `{id, kind, source, title, authors, year, journal,
        doi}` records. `id` is the most-useful resolvable identifier
        (PMID > PMCID > DOI); records with no resolvable identifier
        are dropped — there's no point showing the model a paper it
        can't recommend clickably.

        Empty list on any HTTP / parsing failure. The LLM sees `[]`
        and decides what to do (typically: fall back to training-
        cutoff knowledge with an explicit hedge).
    """
    # Preprints route through bioRxiv directly — Europe PMC's preprint
    # coverage has a real lag that makes time-sensitive discovery
    # ("the Guo et al preprint from last week") fail silently.
    if sources == "preprints":
        from paperwhirl.biorxiv import search_biorxiv
        return search_biorxiv(query, max_results)

    q = _build_query(query, sources)
    if not q:
        return []

    try:
        r = requests.get(
            EUROPEPMC_URL,
            params={
                "query": q,
                "format": "json",
                "resultType": "lite",
                "pageSize": max(1, min(int(max_results), 25)),
            },
            headers={"User-Agent": EUROPEPMC_UA},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    raw = (data.get("resultList") or {}).get("result") or []
    out: list[dict[str, Any]] = []
    for rec in raw:
        ident, kind = _pick_identifier(rec)
        if not ident:
            continue
        out.append({
            "id": ident,
            "kind": kind,
            "source": rec.get("source", ""),
            "title": (rec.get("title") or "").strip().rstrip("."),
            "authors": _author_list(rec),
            "year": (rec.get("pubYear") or "").strip(),
            "journal": (rec.get("journalTitle") or "").strip(),
            "doi": (rec.get("doi") or "").strip() or None,
        })
        if len(out) >= max_results:
            break
    return out


# OpenAI tool schemas — passed verbatim to chat.completions.create
# via the `tools=` kwarg. Names match the Python function names so
# `dispatch(name, args)` can look them up by string.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_papers",
            "description": (
                "Search the scientific literature (PubMed + preprint "
                "servers like bioRxiv and medRxiv, via Europe PMC). "
                "Returns a short list of real papers matching the "
                "query, each with an identifier the user can click "
                "to open. Call this when the user asks for paper "
                "recommendations, recent results in a field, or "
                "'find me X' style queries. Don't call it for "
                "conceptual questions you can answer directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Free-text keyword query. Plain English "
                            "works best — Europe PMC handles the "
                            "stemming and matching."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "How many papers to return. Default 5; "
                            "max 25. Prefer the default unless the "
                            "user explicitly asked for a comprehensive "
                            "list."
                        ),
                        "default": 5,
                    },
                    "sources": {
                        "type": "string",
                        "enum": ["all", "published", "preprints"],
                        "description": (
                            "Scope. 'all' = published + preprints "
                            "(default). 'published' = peer-reviewed "
                            "papers only (PubMed / MEDLINE). "
                            "'preprints' = preprint servers only "
                            "(bioRxiv, medRxiv, etc.). Pick based "
                            "on what the user asked for."
                        ),
                        "default": "all",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def list_my_papers(list_name: str | None = None) -> dict[str, Any]:
    """Return papers in the user's library, optionally filtered to a
    specific reading list.

    Stage 5 E12 (2026-05-23). Lets the search-thread LLM ground its
    answers in the user's actual collection — for queries like
    "what are the DOIs of the papers in my Methods list" or
    "recommend more papers like the ones in my long-term reading."

    `list_name=None` returns all saved papers across all lists
    (deduped). A string filters to the named list, matched
    case-insensitively against the human-readable list name.
    Unknown name returns `{"error": ..., "available_lists": [...]}`
    so the model can recover gracefully.

    Each paper carries title / first_author / year / doi / journal.
    """
    from paperwhirl import config as pw_config
    from paperwhirl import reading_lists as rl

    folder = pw_config.get_folder()
    if folder is None:
        return {"error": "No reading folder is configured. The user "
                         "hasn't pointed PaperWhirl at a sync folder yet."}

    idx = rl.read_index(folder)
    all_lists = idx.get("lists", []) or []

    if list_name:
        wanted = list_name.strip().lower()
        matched_slug: str | None = None
        for entry in all_lists:
            if (entry.get("name") or "").strip().lower() == wanted:
                matched_slug = entry.get("slug")
                break
        if not matched_slug:
            return {
                "error": f"No reading list named {list_name!r}.",
                "available_lists": [
                    e.get("name") for e in all_lists if e.get("name")
                ],
            }
        listdata = rl.get_list(folder, matched_slug) or {}
        paper_slugs = [p.get("slug") for p in (listdata.get("papers") or []) if p.get("slug")]
    else:
        seen: set[str] = set()
        for entry in all_lists:
            listdata = rl.get_list(folder, entry.get("slug")) or {}
            for p in (listdata.get("papers") or []):
                slug = p.get("slug")
                if slug:
                    seen.add(slug)
        paper_slugs = sorted(seen)

    papers: list[dict[str, Any]] = []
    for slug in paper_slugs:
        meta = rl.get_paper(folder, slug)
        if not meta:
            continue
        p = meta.get("paper") or {}
        papers.append({
            "title": p.get("title"),
            "first_author": p.get("first_author"),
            "year": p.get("year"),
            "doi": p.get("doi"),
            "journal": p.get("journal"),
        })

    return {
        "list": list_name if list_name else "(entire library)",
        "count": len(papers),
        "papers": papers,
    }


TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "list_my_papers",
        "description": (
            "Return the papers in the user's library — either all saved "
            "papers (default) or those in a specific reading list. Call "
            "this when the user references 'my library', 'my saved "
            "papers', 'my reading list', or names a list by name. Each "
            "paper comes with title, first_author, year, DOI, and "
            "journal. Use the result to answer direct questions about "
            "the user's collection (e.g. 'what DOIs are in list X') OR "
            "to ground a follow-up `search_papers` query for similarity "
            "asks (e.g. 'recommend more papers like the ones in list X' "
            "— distill topic keywords from the returned titles, then "
            "search for those)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "list_name": {
                    "type": "string",
                    "description": (
                        "Optional. Match against the human-readable "
                        "reading-list name, case-insensitively. Omit "
                        "to get every saved paper across all lists."
                    ),
                },
            },
        },
    },
})


# Name → function lookup. `dispatch` is the single entry point the
# /api/discuss tool-use loop calls — keeps the loop ignorant of
# specific tool names.
_TOOL_FUNCS = {
    "search_papers": search_papers,
    "list_my_papers": list_my_papers,
}


def dispatch(name: str, args: dict[str, Any]) -> str:
    """Call a tool by name and return its result as a JSON string
    (the OpenAI tool-message protocol expects string content).

    Unknown tool name → error message in JSON form. Tool function
    exceptions bubble up as JSON error messages too so the model
    sees the failure as a tool result rather than the whole
    streaming generator crashing.
    """
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool: {name!r}"})
    try:
        result = fn(**args)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return json.dumps(result)


# ─── Per-paper Discuss tools (Stage 5 E8) ──────────────────────────
# These run on the per-paper thread (slug truthy), not the search
# thread. They need the paper's data (references, figures), which the
# server injects — the LLM only supplies the reference/figure NUMBER,
# never the slug. The server calls `lookup_reference` directly with
# the references it loaded from the skeleton (not via `dispatch`,
# which is search-only and context-free).

LOOKUP_REFERENCE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup_reference",
        "description": (
            "Look up a numbered reference from THIS paper's "
            "bibliography. Call this when the user asks about a "
            "specific citation (e.g. 'what's reference 12?', 'what "
            "paper do they cite for X?'). Returns the reference's "
            "authors, year, title, journal, and DOI/PMID if known. "
            "Only the paper's own reference list is available — not "
            "the full text of the cited papers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "The reference number (1-based).",
                },
            },
            "required": ["n"],
        },
    },
}

LOOKUP_FIGURE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup_figure",
        "description": (
            "Fetch the actual image of a numbered figure from THIS "
            "paper so you can see the panels. Call this when the user "
            "asks something that needs the visual content (e.g. "
            "'what does panel B of figure 3 show?', 'describe the "
            "trend in figure 2'). The image is attached to the "
            "conversation after the call so you can read it directly. "
            "You already have every figure's caption in context — only "
            "call this when the caption isn't enough and you need to "
            "look at the figure itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "The figure number (1-based).",
                },
            },
            "required": ["n"],
        },
    },
}

# Built up per S-experiment: S1 = lookup_reference; S2 = lookup_figure.
PAPER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    LOOKUP_REFERENCE_SCHEMA,
    LOOKUP_FIGURE_SCHEMA,
]


def lookup_reference(references: list[dict[str, Any]], n: int) -> str:
    """Return reference `n` from the paper's parsed bibliography as a
    JSON string (the tool-message protocol wants string content).

    `references` is the skeleton's `references` list (server-loaded).
    Graceful JSON error when the paper has no parsed references or the
    number is out of range — the model relays that honestly rather
    than fabricating a citation.
    """
    if not references:
        return json.dumps({
            "error": "no parsed references available for this paper",
        })
    for ref in references:
        if ref.get("n") == n:
            return json.dumps(ref)
    return json.dumps({
        "error": (
            f"no reference numbered {n}; this paper has "
            f"{len(references)} parsed references "
            f"(1–{len(references)})"
        ),
    })
