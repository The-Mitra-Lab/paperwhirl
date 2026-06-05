"""Generate a PaperWhirl review packet from a session skeleton.

Takes an extraction skeleton, asks OpenAI for structured interpretive
content, and merges that content into the review packet.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from paperwhirl.extract import (
    build_skeleton,
    extract_pages,
    find_figures,
    parse_pdfinfo,
    render_pages,
    write_audit,
    write_page_text,
)


DEFAULT_MODEL = "gpt-5.5"
MAX_SOURCE_CHARS = 45000

KIND_VALUES = ["empirical", "method", "resource", "math", "theory", "review", "unknown"]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "paper"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_yaml(data: dict[str, Any], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True, width=100)


def extract_pdf_to_skeleton(pdf_path: Path, output_dir: Path, slug: str) -> Path:
    paper_dir = output_dir / slug
    pages_dir = paper_dir / "pages"
    paper_dir.mkdir(parents=True, exist_ok=True)

    pdfinfo = parse_pdfinfo(pdf_path)
    pages = extract_pages(pdf_path)
    write_page_text(pages, paper_dir / "extracted_text.txt")

    figures = find_figures(pages)
    # Stage 6 E9 follow-on (2026-05-27): for figures-at-end PDFs
    # (JNeurosci early-posted, submission-style, theses) the captions
    # live on text pages and the figures themselves live on dedicated
    # image pages later in the document. The default E8 rule
    # asset_page = caption_page would render the caption page (text
    # only) as the figure asset. Detect via pdfimages -list and
    # remap to the actual image-bearing pages when the signal fires.
    from paperwhirl.extract import _remap_figures_at_end
    _remap_figures_at_end(pdf_path, figures)
    rendered_pages = render_pages(pdf_path, [item["asset_page"] for item in figures], pages_dir)
    # Figure-region cropping deferred to Stage 5 — see Stage 4 E7
    # Step 2.5 history and FEATURES.md. Vector PDFs render with
    # surrounding body text in the figure asset, but this is OK
    # enough for v1; the figures ARE visible and the LLM analysis
    # uses the cropped + uncropped versions about the same way.
    skeleton = build_skeleton(pdf_path, slug, paper_dir, pages, pdfinfo, figures, rendered_pages)

    skeleton_path = paper_dir / "session_skeleton.yaml"
    write_yaml(skeleton, skeleton_path)
    write_audit(figures, paper_dir / "extraction_audit.md")
    return skeleton_path


def claim_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "statement", "notes"],
        "properties": {
            "id": {"type": "string"},
            "statement": {"type": "string"},
            "notes": {"type": ["string", "null"]},
        },
    }


def generation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["paper_kind", "overview", "figures", "discussion", "generation_notes"],
        "properties": {
            "paper_kind": {
                "type": "object",
                "additionalProperties": False,
                "required": ["primary", "secondary"],
                "properties": {
                    "primary": {"type": "string", "enum": KIND_VALUES},
                    "secondary": {"type": ["string", "null"], "enum": KIND_VALUES + [None]},
                },
            },
            "overview": {
                "type": "object",
                "additionalProperties": False,
                # Stage 5 E7 S24 (2026-05-20): `approach` ("What the
                # authors did") sits between gap and claims — the
                # methods/approach narrative, distinct from claims
                # (the conclusions).
                "required": ["background", "gap", "approach", "claims"],
                "properties": {
                    "background": {"type": "string"},
                    "gap": {"type": "string"},
                    "approach": {"type": "string"},
                    "claims": {
                        "type": "array",
                        "items": claim_schema(),
                        "minItems": 1,
                        "maxItems": 3,
                    },
                },
            },
            "figures": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "view", "analysis"],
                    "properties": {
                        "id": {"type": "string"},
                        "view": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["display_legend"],
                            "properties": {
                                "display_legend": {"type": "string"},
                            },
                        },
                        "analysis": {
                            "type": "object",
                            "additionalProperties": False,
                            # Stage 5 E7 S25 (2026-05-20): `results`
                            # (≤3 sentences answering the figure's
                            # question) sits between approach and
                            # evidence; approach is now kept short.
                            "required": [
                                "motivation",
                                "question",
                                "approach",
                                "results",
                                "evidence",
                                "interpretation",
                                "linked_claims",
                            ],
                            "properties": {
                                "motivation": {"type": "string"},
                                "question": {"type": "string"},
                                "approach": {"type": "string"},
                                "results": {"type": "string"},
                                "evidence": {"type": "string"},
                                "interpretation": {"type": "string"},
                                "linked_claims": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
            "discussion": {
                "type": "object",
                "additionalProperties": False,
                "required": ["synthesis", "takeaways", "caveats", "next_steps"],
                "properties": {
                    "synthesis": {"type": "string"},
                    "takeaways": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "caveats": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 2,
                    },
                    "next_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 3,
                    },
                },
            },
            "generation_notes": {"type": "array", "items": {"type": "string"}},
        },
    }


# ---------------------------------------------------------------------------
# Per-section schemas + builders for E3 (streaming generation).
#
# The single-call path (`generation_schema` / `build_prompt` / `call_openai`)
# above is left intact for backward compat with /api/generate. The functions
# below carve the same content into independently-callable slices so the
# server can stream sections as they complete.
# ---------------------------------------------------------------------------


def overview_schema() -> dict[str, Any]:
    """Top-level schema for the overview-only call.

    Returns paper_kind + overview + a generation_notes side-channel.
    """
    full = generation_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["paper_kind", "overview", "generation_notes"],
        "properties": {
            "paper_kind": full["properties"]["paper_kind"],
            "overview": full["properties"]["overview"],
            "generation_notes": full["properties"]["generation_notes"],
        },
    }


def figure_section_schema() -> dict[str, Any]:
    """Top-level schema for one figure's section call.

    Mirrors the per-figure object from `generation_schema()` but at the top
    level so OpenAI strict JSON output is satisfied.
    """
    full_figure = generation_schema()["properties"]["figures"]["items"]
    # Strip the figure id requirement; the server already knows it.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["view", "analysis"],
        "properties": {
            "view": full_figure["properties"]["view"],
            "analysis": full_figure["properties"]["analysis"],
        },
    }


def discussion_section_schema() -> dict[str, Any]:
    """Top-level schema for the closing-discussion call."""
    return generation_schema()["properties"]["discussion"]


def table_section_schema() -> dict[str, Any]:
    """Top-level schema for one table's analysis call.

    Tables get a much shorter analysis than figures — just one
    interpretation paragraph (the caption already tells the reader
    what the table is about; PaperWhirl's job is to say what it means).
    No caveats: per user direction, tables don't carry enough
    interpretive weight on their own to need a caveats section.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["analysis"],
        "properties": {
            "analysis": {
                "type": "object",
                "additionalProperties": False,
                "required": ["interpretation", "linked_claims"],
                "properties": {
                    "interpretation": {"type": "string"},
                    "linked_claims": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }


DEFAULT_SECTION_PREAMBLE = (
    "PaperWhirl is an opinionated, figure-grounded paper review. Use the "
    "extracted paper text and original captions as evidence. Do not invent "
    "methods, results, gene names, quantitative results, or claims unsupported "
    "by the source context. Be direct and useful rather than hedged, but mark "
    "real uncertainty in caveats. "
    # Stage 5 E7 S18 (2026-05-19): the renderer supports LaTeX math, but
    # the model wasn't told — so writeups of math/theory papers came out
    # with no equations. Make the capability explicit.
    "When the paper is mathematical or quantitative (equations, models, "
    "loss functions, formal definitions, complexity bounds), express that "
    "content in LaTeX: inline math with $...$ and display math with $$...$$ "
    "(both render). Reproduce a key equation when it is the clearest, most "
    "faithful expression rather than paraphrasing it in prose. Keep display "
    "equations reasonably compact — they render inside narrow columns. For "
    "papers with no real mathematical content, don't manufacture any."
)


def section_preamble() -> str:
    """The review preamble shared by every section prompt.

    Returns the user's override from `~/.paperwhirl/config.json` if
    one is set (the Settings panel's "advanced" generation prompt),
    otherwise the built-in default. A bad override silently degrades
    output quality — hence it lives behind the advanced affordance.
    """
    try:
        from paperwhirl import config as _config
        override = _config.get_generation_prompt()
        if override:
            return override
    except Exception as _exc:
        import sys as _sys
        print(
            f"[paperwhirl] could not load section-preamble override; using default: {_exc}",
            file=_sys.stderr,
        )
    return DEFAULT_SECTION_PREAMBLE


def build_overview_prompt(skeleton: dict[str, Any]) -> str:
    source_context = compact_source_context(skeleton)
    return (
        "Generate ONLY the overview block for a Full-mode PaperWhirl review "
        "session.\n\n"
        f"{section_preamble()}\n\n"
        "Return paper_kind + overview (background, gap, approach, claims).\n\n"
        # Stage 5 E7 S24 (2026-05-20): `approach` and `claims` must NOT
        # overlap. approach = WHAT THE AUTHORS DID (the methods /
        # experimental narrative — systems, techniques, perturbations,
        # analyses); claims = WHAT THEY FOUND (the headline conclusions).
        "- approach: 2-4 sentences on WHAT THE AUTHORS DID — the "
        "experimental / analytical narrative (what they built, measured, "
        "or tested, in what system). Use action verbs. Do NOT state "
        "findings or conclusions here.\n"
        "- claims: AT MOST 3 headline findings — the conclusions the paper "
        "establishes, as assertions about reality. These are NOT one-per-"
        "figure; the figure-by-figure walkthrough that follows covers "
        "per-figure detail. Synthesize to the 2-3 things the paper actually "
        "shows. Do NOT restate the approach.\n"
        "Claim ids should be stable strings (claim_1, claim_2, claim_3) — "
        "the figure calls that fire next link to these ids, so pick them "
        "now and do not renumber.\n\n"
        "Source context JSON:\n"
        f"{json.dumps(source_context, ensure_ascii=False, indent=2)}"
    )


def build_figure_prompt(
    skeleton: dict[str, Any],
    figure_index: int,
    overview: dict[str, Any],
) -> str:
    """Build a prompt for one figure's analysis, given the overview.

    Keyed by array index rather than figure id, because real papers
    sometimes have duplicate labels (e.g. two Figure 2's, no Figure 1).
    The id is still passed to the model as metadata for context.
    """
    source_context = compact_source_context(skeleton)
    figures = skeleton.get("figures", [])
    if figure_index < 0 or figure_index >= len(figures):
        raise ValueError(f"figure_index {figure_index} out of range (0..{len(figures)-1})")
    figure = figures[figure_index]
    figure_meta = {
        "index": figure_index,
        "id": figure.get("id"),
        "label": figure.get("label"),
        "source_page": figure.get("view", {}).get("source_page"),
        "original_caption": figure.get("view", {}).get("original_caption"),
    }
    claim_ids = [c.get("id") for c in overview.get("claims", [])]
    return (
        f"Generate ONLY the analysis for figure at index {figure_index} "
        f"(id={figure.get('id')!r}, label={figure.get('label')!r}) of a "
        f"Full-mode PaperWhirl review session.\n\n"
        f"{section_preamble()}\n\n"
        "Return one view + analysis block for this single figure. "
        "view.display_legend should be a rewritten explanatory legend "
        "(panel-by-panel where useful), not a copy of the original caption. "
        # Stage 5 E7 S25 (2026-05-20): tightened analysis-field roles.
        "analysis fields, in order and kept distinct:\n"
        "- motivation: why this experiment/analysis was done.\n"
        "- question: the specific question the figure addresses.\n"
        "- approach: SHORT — one sentence on what was done to answer it "
        "(the assay/analysis). Don't belabor methods.\n"
        "- results: AT MOST 3 sentences directly answering the question — "
        "what the figure shows (the finding). This is the heart of the "
        "block.\n"
        "- evidence: the concrete data behind the main results — cite the "
        "specific panels, effect sizes, n, and p-values that back the "
        "finding. The figure's legend is shown to the reader already, so "
        "don't re-describe what each panel depicts; point to the data that "
        "supports the result.\n"
        "- interpretation: what it means for the paper's claims — "
        "significance, caveats.\n"
        "analysis.linked_claims must contain only claim ids that already "
        f"appear in the overview claims list: {claim_ids}.\n\n"
        f"This figure's metadata:\n{json.dumps(figure_meta, ensure_ascii=False, indent=2)}\n\n"
        f"Overview already generated (claims/background/gap for context):\n"
        f"{json.dumps(overview, ensure_ascii=False, indent=2)}\n\n"
        "Source context JSON (full paper):\n"
        f"{json.dumps(source_context, ensure_ascii=False, indent=2)}"
    )


def build_table_prompt(
    skeleton: dict[str, Any],
    table_index: int,
    overview: dict[str, Any],
) -> str:
    """Build a prompt for one table's analysis, given the overview.

    Short by design: one interpretation paragraph (~2-4 sentences). The
    caption already explains what the table is; the model's job is to
    say what it means.
    """
    source_context = compact_source_context(skeleton)
    tables = skeleton.get("tables", [])
    if table_index < 0 or table_index >= len(tables):
        raise ValueError(f"table_index {table_index} out of range (0..{len(tables)-1})")
    table = tables[table_index]
    table_meta = {
        "index": table_index,
        "id": table.get("id"),
        "label": table.get("label"),
        "caption_text": table.get("view", {}).get("caption_text"),
        "html": table.get("view", {}).get("html"),
    }
    claim_ids = [c.get("id") for c in overview.get("claims", [])]
    return (
        f"Generate ONLY the short analysis for table at index {table_index} "
        f"(id={table.get('id')!r}, label={table.get('label')!r}) of a "
        f"Full-mode PaperWhirl review session.\n\n"
        f"{section_preamble()}\n\n"
        "Return one analysis block for this single table. "
        "analysis.interpretation is ONE short paragraph (~2-4 sentences) "
        "that says why the table matters and what it shows — do NOT recap "
        "the caption; the user already sees that. "
        "analysis.linked_claims must contain only claim ids that appear in "
        f"the overview claims list: {claim_ids}.\n\n"
        f"This table's metadata (caption + HTML body):\n"
        f"{json.dumps(table_meta, ensure_ascii=False, indent=2)}\n\n"
        f"Overview already generated (claims/background/gap for context):\n"
        f"{json.dumps(overview, ensure_ascii=False, indent=2)}\n\n"
        "Source context JSON (full paper):\n"
        f"{json.dumps(source_context, ensure_ascii=False, indent=2)}"
    )


def build_discussion_prompt(
    skeleton: dict[str, Any],
    overview: dict[str, Any],
    figure_analyses: list[dict[str, Any]],
    table_analyses: list[dict[str, Any]] | None = None,
) -> str:
    source_context = compact_source_context(skeleton)
    table_block = (
        f"Table interpretations (already generated, in order):\n"
        f"{json.dumps(table_analyses, ensure_ascii=False, indent=2)}\n\n"
    ) if table_analyses else ""
    return (
        "Generate ONLY the closing discussion block for a Full-mode PaperWhirl "
        "review session.\n\n"
        f"{section_preamble()}\n\n"
        "Return discussion (synthesis, takeaways, caveats, next_steps). "
        "Keep discussion.synthesis short: answer 'What is the impact of this "
        "paper?' in one compact paragraph. For next_steps, do not list fixes "
        "for the paper's caveats. Instead, write two or three visionary but "
        "plausible directions: what the next paper could be, what larger "
        "research program this enables, or what important biological/technical "
        "question should now become addressable. Give each direction enough "
        "concrete detail to be useful.\n\n"
        "Overview (already generated):\n"
        f"{json.dumps(overview, ensure_ascii=False, indent=2)}\n\n"
        "Figure analyses (already generated, in order):\n"
        f"{json.dumps(figure_analyses, ensure_ascii=False, indent=2)}\n\n"
        f"{table_block}"
        "Source context JSON:\n"
        f"{json.dumps(source_context, ensure_ascii=False, indent=2)}"
    )


def call_openai_section(
    prompt: str,
    model: str,
    schema: dict[str, Any],
    schema_name: str,
) -> dict[str, Any]:
    """OpenAI strict structured call for one section.

    Returns the parsed JSON dict directly (no raw response wrapper — the
    caller decides what to keep).

    Stage 5 E15 (2026-05-24): per-call timeout (180s) + retry cap (1)
    so a rate-limited or stuck request fails fast with an actionable
    message rather than hanging behind the SDK defaults (600s × 2
    retries ≈ 30 min). RateLimitError and APITimeoutError are caught
    and re-raised with user-facing guidance.
    """
    try:
        from openai import OpenAI, RateLimitError, APITimeoutError
    except ImportError as exc:
        raise RuntimeError("The openai package is not installed.") from exc

    client = OpenAI(timeout=180.0, max_retries=1)
    extra: dict[str, Any] = {}
    if model.startswith("gpt-5.5"):
        extra["reasoning"] = {
            "effort": os.environ.get("PAPERWHIRL_REASONING_EFFORT", "low"),
        }
    try:
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You generate structured PaperWhirl review-session content for local "
                        "research-paper review. You are evidence-grounded, concise, and explicit "
                        "about figure-to-claim links.\n\n"
                        "Audience: a field-adjacent expert or a first-year graduate student new "
                        "to this specific subfield. The background and gap should orient that "
                        "reader before the figure walkthrough; figure analyses should explain "
                        "why each panel matters in terms they can follow without assuming "
                        "specialist jargon."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            **extra,
        )
    except RateLimitError as exc:
        raise RuntimeError(
            "OpenAI rate-limited this request and the retry was also "
            "throttled. Wait a minute and try again, or check your "
            "usage / tier limits at platform.openai.com/usage."
        ) from exc
    except APITimeoutError as exc:
        raise RuntimeError(
            "OpenAI took longer than 180s to respond. The request may "
            "still be running on their side; try again, or lower the "
            "reasoning effort in Settings → Advanced."
        ) from exc
    output_text = getattr(response, "output_text", None)
    if not output_text:
        output_text = _extract_response_text(response)
    return json.loads(output_text)


def merge_streamed_packet(
    skeleton: dict[str, Any],
    overview: dict[str, Any],
    figure_sections: dict[int, dict[str, Any]],
    discussion: dict[str, Any],
    slug: str,
    paper_kind: dict[str, Any] | None = None,
    generation_notes: list[str] | None = None,
    table_sections: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a final packet from independently-generated section dicts.

    figure_sections / table_sections are keyed by array index (not id),
    so papers with duplicate labels merge correctly.
    """
    packet = deepcopy(skeleton)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    packet["session"]["id"] = f"{slug}_stage3_e3"
    packet["session"]["updated_at"] = now
    if paper_kind is not None:
        packet["paper_kind"] = paper_kind
    packet["overview"] = overview
    packet["discussion"] = discussion

    for idx, figure in enumerate(packet.get("figures", [])):
        section = figure_sections.get(idx)
        if not section:
            continue
        original_caption = figure.get("view", {}).get("original_caption") or ""
        generated_legend = section["view"]["display_legend"]
        figure["view"]["display_legend"] = original_caption or generated_legend
        figure["view"]["generated_display_legend"] = generated_legend
        figure["analysis"] = section["analysis"]

    if table_sections:
        for idx, table in enumerate(packet.get("tables", [])):
            section = table_sections.get(idx)
            if not section:
                continue
            table["analysis"] = section["analysis"]

    packet["generation"] = {
        "provider": "openai",
        "model": os.environ.get("PAPERWHIRL_OPENAI_MODEL", DEFAULT_MODEL),
        "created_at": now,
        "mode": "streamed_sections",
        "notes": generation_notes or [],
    }
    return packet


def compact_source_context(skeleton: dict[str, Any]) -> dict[str, Any]:
    pages = skeleton.get("extracted_source", {}).get("text", {}).get("pages", [])
    page_chunks = []
    total_chars = 0
    for page in pages:
        text = re.sub(r"\s+", " ", page.get("text", "")).strip()
        if not text:
            continue
        remaining = MAX_SOURCE_CHARS - total_chars
        if remaining <= 0:
            break
        excerpt = text[: min(len(text), remaining, 6000)]
        page_chunks.append({"page": page.get("page"), "text": excerpt})
        total_chars += len(excerpt)

    figures = []
    for figure in skeleton.get("figures", []):
        figures.append(
            {
                "id": figure.get("id"),
                "order": figure.get("order"),
                "label": figure.get("label"),
                "source_page": figure.get("view", {}).get("source_page"),
                "original_caption": figure.get("view", {}).get("original_caption"),
            }
        )

    return {
        "paper": skeleton.get("paper", {}),
        "paper_kind_guess": skeleton.get("paper_kind", {}),
        "overview": skeleton.get("overview", {}),
        "figures": figures,
        "page_text_excerpts": page_chunks,
    }


def build_prompt(skeleton: dict[str, Any]) -> str:
    source_context = compact_source_context(skeleton)
    figure_ids = [figure.get("id") for figure in skeleton.get("figures", [])]
    return (
        "Generate the interpretive content for a Full-mode PaperWhirl review session.\n\n"
        "PaperWhirl is an opinionated, figure-grounded paper review. Use the extracted "
        "paper text and original captions as evidence. Do not invent methods, results, "
        "gene names, quantitative results, or claims that are not supported by the source "
        "context. Be direct and useful rather than hedged, but mark real uncertainty in "
        "caveats.\n\n"
        "Return only structured data matching the schema. Preserve these figure ids exactly: "
        f"{figure_ids}.\n\n"
        "Claim ids should be stable strings like claim_1, claim_2, claim_3. Each figure's "
        "linked_claims list should contain only claim ids that appear in overview.claims. "
        "Write display legends as explanatory PaperWhirl legends, not copies of the original "
        "captions, but know that the current viewer will display original captions by default. "
        "The top-level discussion is the "
        "closing synthesis, not an interactive chat. Keep discussion.synthesis short: answer "
        "'What is the impact of this paper?' in one compact paragraph. For next_steps, do "
        "not list fixes for the paper's caveats. Instead, write two or three visionary but "
        "plausible directions: what the next paper could be, what larger research program "
        "this enables, or what important biological/technical question should now become "
        "addressable. Give each direction enough concrete detail to be useful.\n\n"
        "Source context JSON:\n"
        f"{json.dumps(source_context, ensure_ascii=False, indent=2)}"
    )


def call_openai(prompt: str, model: str) -> tuple[dict[str, Any], Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The openai package is not installed in this environment.") from exc

    client = OpenAI()
    # gpt-5.5 supports a `reasoning.effort` knob; Instant tier == "low".
    # Other models ignore it / error, so only attach when the model is in
    # the gpt-5.5 family. Override the effort via PAPERWHIRL_REASONING_EFFORT.
    extra: dict[str, Any] = {}
    if model.startswith("gpt-5.5"):
        extra["reasoning"] = {
            "effort": os.environ.get("PAPERWHIRL_REASONING_EFFORT", "low"),
        }
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "You generate structured PaperWhirl review-session content for local "
                    "research-paper review. You are evidence-grounded, concise, and explicit "
                    "about figure-to-claim links.\n\n"
                    "Audience: a field-adjacent expert or a first-year graduate student new "
                    "to this specific subfield. The background and gap should orient that "
                    "reader before the figure walkthrough; figure analyses should explain "
                    "why each panel matters in terms they can follow without assuming "
                    "specialist jargon."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "paperwhirl_review_generation",
                "description": "Interpretive fields for a PaperWhirl review session packet.",
                "strict": True,
                "schema": generation_schema(),
            }
        },
        **extra,
    )
    output_text = getattr(response, "output_text", None)
    if not output_text:
        output_text = _extract_response_text(response)
    return json.loads(output_text), response


def _extract_response_text(response: Any) -> str:
    chunks = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)
    if not chunks:
        raise RuntimeError("OpenAI response did not contain output text.")
    return "\n".join(chunks)


def merge_generation(skeleton: dict[str, Any], generated: dict[str, Any], slug: str) -> dict[str, Any]:
    packet = deepcopy(skeleton)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    packet["session"]["id"] = f"{slug}_stage2_e3"
    packet["session"]["updated_at"] = now
    packet["paper_kind"] = generated["paper_kind"]
    packet["overview"] = generated["overview"]
    packet["discussion"] = generated["discussion"]

    generated_by_id = {figure["id"]: figure for figure in generated["figures"]}
    for figure in packet.get("figures", []):
        generated_figure = generated_by_id.get(figure.get("id"))
        if not generated_figure:
            continue
        original_caption = figure.get("view", {}).get("original_caption") or ""
        generated_legend = generated_figure["view"]["display_legend"]
        figure["view"]["display_legend"] = original_caption or generated_legend
        figure["view"]["generated_display_legend"] = generated_legend
        figure["analysis"] = generated_figure["analysis"]

    packet["generation"] = {
        "provider": "openai",
        "model": os.environ.get("PAPERWHIRL_OPENAI_MODEL", DEFAULT_MODEL),
        "created_at": now,
        "notes": generated.get("generation_notes", []),
    }
    return packet


def validate_packet(packet: dict[str, Any]) -> list[str]:
    errors = []
    claims = packet.get("overview", {}).get("claims", [])
    claim_ids = {claim.get("id") for claim in claims}
    if not packet.get("overview", {}).get("background"):
        errors.append("overview.background is empty")
    if not packet.get("overview", {}).get("gap"):
        errors.append("overview.gap is empty")
    if not claims:
        errors.append("overview.claims is empty")

    for figure in packet.get("figures", []):
        figure_id = figure.get("id", "<missing>")
        if not figure.get("view", {}).get("display_legend"):
            errors.append(f"{figure_id} view.display_legend is empty")
        analysis = figure.get("analysis", {})
        for key in ["motivation", "question", "approach", "evidence", "interpretation"]:
            if not analysis.get(key):
                errors.append(f"{figure_id} analysis.{key} is empty")
        for claim_id in analysis.get("linked_claims", []):
            if claim_id not in claim_ids:
                errors.append(f"{figure_id} links unknown claim id {claim_id}")

    discussion = packet.get("discussion", {})
    if not discussion.get("synthesis"):
        errors.append("discussion.synthesis is empty")
    if not discussion.get("takeaways"):
        errors.append("discussion.takeaways is empty")
    if len(discussion.get("caveats", [])) > 2:
        errors.append("discussion.caveats has more than two items")
    next_steps = discussion.get("next_steps", [])
    if len(next_steps) > 3:
        errors.append("discussion.next_steps has more than three items")
    return errors


def _write_generation_audit(packet: dict[str, Any], errors: list[str], output_path: Path) -> None:
    lines = ["# Generation Audit", ""]
    lines.append(f"Status: {'needs review' if errors else 'valid'}")
    lines.append("")
    if errors:
        lines.append("## Validation Issues")
        lines.extend(f"- {error}" for error in errors)
        lines.append("")
    lines.append("## Figures")
    lines.append("")
    lines.append("| Figure | Legend? | Analysis? | Linked Claims |")
    lines.append("|---|---|---|---|")
    for figure in packet.get("figures", []):
        analysis = figure.get("analysis", {})
        analysis_ok = all(
            analysis.get(key)
            for key in ["motivation", "question", "approach", "evidence", "interpretation"]
        )
        lines.append(
            f"| {figure.get('label')} | "
            f"{'yes' if figure.get('view', {}).get('display_legend') else 'no'} | "
            f"{'yes' if analysis_ok else 'no'} | "
            f"{', '.join(analysis.get('linked_claims', [])) or 'none'} |"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_review_session(
    skeleton_path: Path,
    output_path: Path | None = None,
    prompt_path: Path | None = None,
    response_path: Path | None = None,
    audit_path: Path | None = None,
    model: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    skeleton = load_yaml(skeleton_path)
    slug = skeleton.get("paper", {}).get("id") or slugify(skeleton_path.parent.name)
    prompt = build_prompt(skeleton)

    paper_dir = skeleton_path.parent
    prompt_path = prompt_path or paper_dir / "generation_prompt.txt"
    response_path = response_path or paper_dir / "generation_response.json"
    output_path = output_path or paper_dir / "review_session.yaml"
    audit_path = audit_path or paper_dir / "generation_audit.md"

    prompt_path.write_text(prompt, encoding="utf-8")
    if dry_run:
        return None

    selected_model = model or os.environ.get("PAPERWHIRL_OPENAI_MODEL", DEFAULT_MODEL)
    generated, raw_response = call_openai(prompt, selected_model)
    response_path.write_text(
        raw_response.model_dump_json(indent=2)
        if hasattr(raw_response, "model_dump_json")
        else json.dumps(generated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    packet = merge_generation(skeleton, generated, slug)
    packet["generation"]["model"] = selected_model
    errors = validate_packet(packet)
    write_yaml(packet, output_path)
    _write_generation_audit(packet, errors, audit_path)
    if errors:
        raise RuntimeError("Generated packet failed validation: " + "; ".join(errors))
    return packet
