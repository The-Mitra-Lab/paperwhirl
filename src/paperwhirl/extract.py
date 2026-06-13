"""PDF-to-session-skeleton extraction (E2 path).

Grabs and organizes source material from a local PDF. Does not write
PaperWhirl interpretation prose.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
import yaml


# Wiley journals (and a few other publishers) typeset figure captions
# as `F I G U R E 1` — one space between each letter. The PDF text
# extracts in the same spaced form, which `FIGURE_RE` below would not
# match. Normalize the spaced form to `Figure N` before applying the
# main matcher. Stage 5 E11 (2026-05-23) — surfaced when verifying the
# generic publisher-PDF fall-through on Cracco 2022 (Wiley).
_SPACED_FIGURE_RE = re.compile(
    r"\bF\s+I\s+G\s+U\s+R\s+E\s+(\d+)", re.IGNORECASE
)


FIGURE_RE = re.compile(
    # Caption starts EITHER at the line start (optional leading
    # line-number) OR after a column gap of 5+ whitespace chars
    # within a line. The latter handles multi-column PDFs where
    # `pdftotext -layout` merges left-column body text and right-
    # column caption text onto the same physical line: the marker
    # lands mid-line preceded by a wide whitespace run that no
    # natural prose produces. Added in Stage 6 E8 Step 2
    # (2026-05-26) when PNAS Fig. 3 was being missed.
    # The marker is one of: `Fig.` / `Fig` / `Figure` / `FIG` — the
    # period after `Fig` is optional because some bioRxiv preprints
    # write captions as "Fig 1. Triptolide treatment…" (no period on
    # "Fig"). The all-caps `FIG` (no period) is ASM house style —
    # Journal of Virology, mBio, AAC, JB, etc. typeset captions as
    # "FIG 1 LIGHT, but not TNF-α, …". The matcher is case-sensitive,
    # so this needs its own alternative; added Stage 6 E14-era
    # (2026-05-28) when a JVI paper (10.1128/JVI.01503-16) extracted
    # via the generic-PDF fall-through with zero figures — PMC's NXML
    # body was empty and the PMC PDF mirror was reCAPTCHA-walled. After "Fig N" or "Figure N" the separator is either:
    #   - explicit punctuation `[:.|]` (Cell/Nature/Springer style), OR
    #   - whitespace followed by a capital letter (LaTeX default
    #     style: "Fig. 1 Relational annotation priors carry…").
    # The lookahead in the second branch is zero-width so the capital
    # stays in the caption body (consumed later by caption_from_pages).
    # Group 3 is the punctuation char when the first branch matches,
    # None when the lookahead branch matches — downstream code only
    # checks `== "|"` so None falls through to the default safely.
    r"(?m)(?:^\s*(?:\d+\s+)?|\s{5,})(FIG\.?|Fig\.?|Figure)\s+(\d+)(?:\s*([:.|])|\s+(?=[A-Z]))"
)
LINE_NUMBER_RE = re.compile(r"^\s*\d+\s+")


def clean_pdf_text(text: str) -> str:
    return text.replace("\xad", "")


def run_text(command: list[str]) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return result.stdout


def parse_pdfinfo(pdf_path: Path) -> dict[str, str]:
    output = run_text(["pdfinfo", str(pdf_path)])
    info: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        info[key.strip()] = value.strip()
    return info


def extract_pages(pdf_path: Path) -> list[str]:
    text = run_text(["pdftotext", "-layout", str(pdf_path), "-"])
    # Normalize letter-spaced figure markers ("F I G U R E 1" → "Figure 1")
    # at the source so both find_figures and caption_from_pages see clean
    # text. See _SPACED_FIGURE_RE.
    text = _SPACED_FIGURE_RE.sub(r"Figure \1", text)
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def write_page_text(pages: list[str], output_path: Path) -> None:
    chunks = []
    for index, page_text in enumerate(pages, start=1):
        chunks.append(f"\n\n===== PAGE {index} =====\n\n{page_text.rstrip()}\n")
    output_path.write_text("".join(chunks).lstrip(), encoding="utf-8")


def parse_title_and_authors(first_page: str, pdfinfo: dict[str, str]) -> tuple[str, list[str]]:
    title = clean_pdf_text(pdfinfo.get("Title") or "")
    raw_lines = []
    for line in clean_pdf_text(first_page).splitlines():
        if re.match(r"^\s{20,}\S", line):
            continue
        primary_column = re.split(r"\s{8,}", line.strip(), maxsplit=1)[0]
        raw_lines.append(primary_column.strip())
    lines = [line for line in raw_lines if line]

    if title:
        title_words = set(re.findall(r"[A-Za-z0-9]+", title.lower()))
        title_line_count = 0
        found_title = False
        seen_words: set[str] = set()
        first_title_word = next(iter(re.findall(r"[A-Za-z0-9]+", title.lower())), "")
        for index, line in enumerate(lines[:12]):
            line_words = set(re.findall(r"[A-Za-z0-9]+", line.lower()))
            overlap = title_words.intersection(line_words)
            if not found_title and first_title_word in overlap:
                found_title = True
            if found_title:
                if overlap:
                    seen_words.update(overlap)
                    title_line_count = index + 1
                if len(seen_words) >= len(title_words):
                    break
    else:
        title_line_count = 0
        title_lines = []
        for line in lines[:5]:
            if re.search(r"\d", line) and "," in line:
                break
            title_lines.append(line)
        title_line_count = len(title_lines)
        if title_lines:
            title = " ".join(title_lines)
    title = re.sub(r"^\s*\d+\s+", "", title).strip()

    authors: list[str] = []
    author_lines = []
    for line in lines[title_line_count:]:
        if re.fullmatch(r"\d+", line):
            break
        if line.startswith(("Department", "Division", "Intellectual", "Abstract", "Introduction")):
            break
        if "Abstract" in line or "Corresponding author" in line:
            break
        if author_lines and "," not in line and len(line.split()) > 6:
            break
        if len(line.split()) > 18:
            break
        author_lines.append(line)

    author_block = " ".join(author_lines)
    if author_block:
        cleaned = re.sub(r"(?<=[A-Za-z.])\d+(?:,\d+)*\*?", "", author_block)
        cleaned = re.sub(r"\bet\s+al\.?", "", cleaned)
        cleaned = re.sub(r"\s+,", ",", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.replace(" and ", ", ")
        authors = [part.strip() for part in cleaned.split(",") if part.strip()]
    return title, authors


def metadata_page(pages: list[str]) -> str:
    if not pages:
        return ""
    first_page = pages[0]
    front_matter_markers = (
        "ARTICLE SUMMARY",
        "RESEARCH ARTICLE SUMMARY",
        "NEWS & VIEWS",
        "NEWS AND VIEWS",
    )
    if len(pages) > 1 and any(marker in first_page.upper() for marker in front_matter_markers):
        return pages[1]
    return first_page


def normalize_caption(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = re.sub(r"^\s*\d+\s+", "", line).strip()
        if not stripped:
            continue
        if stripped.startswith("Science ") or stripped.startswith("Downloaded from "):
            break
        if stripped:
            lines.append(stripped)
    caption = " ".join(lines)
    caption = re.sub(r"\s+", " ", caption)
    return caption


def trim_caption_block(text: str) -> str:
    lines = []
    blank_run = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Science ") or stripped.startswith("Downloaded from "):
            break
        if not stripped:
            if lines:
                blank_run += 1
            if blank_run >= 2:
                break
            continue
        blank_run = 0
        lines.append(line)
    return "\n".join(lines)


def caption_from_pages(pages: list[str], page_index: int, match: re.Match[str]) -> str:
    page_text = pages[page_index - 1]
    start = match.start()
    next_match = FIGURE_RE.search(page_text, match.end())
    end = next_match.start() if next_match else len(page_text)
    first_chunk = trim_caption_block(page_text[start:end])
    chunks = [first_chunk]
    if normalize_caption(first_chunk).rstrip().endswith("."):
        return normalize_caption("\n".join(chunks))

    next_page_index = page_index
    while next_page_index < len(pages):
        next_page = pages[next_page_index]
        if FIGURE_RE.search(next_page):
            break
        continuation_lines = []
        started = False
        for line in next_page.splitlines():
            if LINE_NUMBER_RE.match(line):
                started = True
                continuation_lines.append(line)
            elif started and line.strip():
                continuation_lines.append(line)
            elif started:
                break
        if not continuation_lines:
            break
        chunks.append("\n".join(continuation_lines))
        next_page_index += 1

    return normalize_caption("\n".join(chunks))


def _image_bearing_pages(pdf_path: Path, min_dim: int = 300) -> list[int]:
    """Return pages whose `pdfimages -list` row has a "substantial"
    raster image — i.e., not the publisher logo. min_dim filters
    by both width AND height (default 300px so a 400x200 logo
    still gets excluded while a 600x400 figure passes).

    Returns a sorted, de-duplicated list of 1-based page numbers.
    Empty list on any pdfimages failure (Poppler missing, malformed
    PDF) — caller treats that as "no figures-at-end signal."
    """
    try:
        out = subprocess.check_output(
            ["pdfimages", "-list", str(pdf_path)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    pages: set[int] = set()
    for line in out.splitlines()[2:]:  # skip header + dashes
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            page = int(parts[0])
            width = int(parts[3])
            height = int(parts[4])
        except ValueError:
            continue
        if width >= min_dim and height >= min_dim:
            pages.add(page)
    return sorted(pages)


def _remap_figures_at_end(
    pdf_path: Path,
    figures: list[dict[str, Any]],
) -> None:
    """Detect figures-at-end layout (submission-style PDFs, JNeurosci
    early-posted, theses) and remap each figure's `asset_page` to
    the actual image page.

    Default `find_figures` assigns asset_page = caption_page (the
    inline-layout rule from E8 Step 2 follow-up). That's wrong for
    PDFs that cluster all captions on a few pages and then put
    each figure on its own dedicated image page after — the
    rendered "asset" ends up being the caption page (text only),
    not the figure.

    Layout signal: max(caption_pages) < min(image_pages). When that
    holds and at least as many image pages as figures exist,
    reassign figure N → Nth image page (in document order). When
    fewer image pages exist than figures, only the first K get
    remapped; the rest keep their caption_page.

    Mutates `figures` in place. No-op when the inline-layout
    signal doesn't fire or pdfimages is unavailable.
    """
    if not figures:
        return
    image_pages = _image_bearing_pages(pdf_path)
    if not image_pages:
        return
    caption_pages = [f["caption_page"] for f in figures]
    if max(caption_pages) >= min(image_pages):
        # Inline layout (or interleaved); current heuristic is fine.
        return
    # Figures-at-end: remap by figure-number order.
    sorted_figs = sorted(figures, key=lambda f: f["number"])
    for idx, fig in enumerate(sorted_figs):
        if idx < len(image_pages):
            fig["asset_page"] = image_pages[idx]


def find_figures(pages: list[str]) -> list[dict[str, Any]]:
    figures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_index, page_text in enumerate(pages, start=1):
        for match in FIGURE_RE.finditer(page_text):
            number = match.group(2)
            figure_label = f"Figure {number}"
            if figure_label in seen:
                continue
            if "Extended Data" in page_text[max(0, match.start() - 40) : match.start() + 80]:
                continue
            seen.add(figure_label)
            # Stage 6 E8 Step 2 follow-up (2026-05-26): the asset
            # image lives on the same page as its caption. The
            # previous "Fig. N. / Fig. N: → caption_page - 1" rule
            # was a Cell/Nature spanning-figure assumption (figure
            # at top of one page, caption at top of next) but the
            # PNAS / Wiley / Frontiers / eLife / Oxford layouts
            # and modern Cell/Nature itself all inline the caption
            # next to the image. The previous-page rule was
            # pulling every figure's asset one page behind — Fig 2
            # card showed Fig 1, etc. — surfaced on the PNAS tire-
            # kick. Cell/Nature go through their own family
            # extractors anyway; this fallback's job is the long
            # tail, where "caption ⇒ same page" is the norm.
            asset_page = page_index
            figures.append(
                {
                    "number": int(number),
                    "label": figure_label,
                    "marker": match.group(1),
                    "separator": match.group(3),
                    "caption_page": page_index,
                    "asset_page": asset_page,
                    "caption": caption_from_pages(pages, page_index, match),
                }
            )
    figures.sort(key=lambda item: item["number"])
    return figures


def figure_bbox_from_pdf(
    pdf_path: Path, page_num: int
) -> tuple[float, float, float, float] | None:
    """Return (x0, top, x1, bot) in PDF points covering all non-text
    drawing primitives on this page — typically the figure region.

    Uses pdfplumber to query lines / curves / rects / embedded images.
    Returns None if pdfplumber isn't available, the page has no
    drawings, or the bbox covers >80% of the page area (probably
    catching header/footer ornaments rather than a real figure).
    Used by `crop_assets_to_figures` to crop a full-page render down
    to just the figure area on vector-PDF papers (common LaTeX
    bioRxiv preprints).
    """
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_num < 1 or page_num > len(pdf.pages):
                return None
            page = pdf.pages[page_num - 1]
            W, H = page.width, page.height
            prims = list(page.lines) + list(page.curves) + list(page.rects) + list(page.images)
            # Filter out degenerate / out-of-bounds primitives. Some
            # PDFs include vector commands with absurd coordinates
            # (e.g. bottom > 70000pt on an 842pt page — clipped curves
            # that the rendered output doesn't show but pdfplumber
            # still surfaces). Including them blows the bbox up to
            # the full page and the sanity check below kills the crop.
            prims = [
                p for p in prims
                if 0 <= p["x0"] < p["x1"] <= W and 0 <= p["top"] < p["bottom"] <= H
            ]
            if not prims:
                return None
            x0 = min(p["x0"] for p in prims)
            x1 = max(p["x1"] for p in prims)
            top = min(p["top"] for p in prims)
            bot = max(p["bottom"] for p in prims)
            if x1 <= x0 or bot <= top:
                return None
            # Sanity check: if the bbox covers >80% of the page area
            # we're probably picking up header/footer rules or border
            # ornaments rather than a real figure. Skip the crop and
            # fall back to the full page render.
            area_ratio = ((x1 - x0) * (bot - top)) / (W * H)
            if area_ratio > 0.80:
                return None
            return (x0, top, x1, bot)
    except Exception:
        return None


def _llm_figure_bbox_pct(
    image_path: Path, model: str | None = None
) -> tuple[float, float, float, float] | None:
    """Ask OpenAI vision to locate the figure on this page render.

    Returns (x, y, width, height) as percentages 0-100 of the image
    dimensions, or None if anything goes wrong (no API key, no openai
    package, network error, model returned unparseable JSON, etc).
    Cropping callers should treat None as "fall back to the next
    technique" rather than an error.

    Failures log a `[crop-llm]` line to stderr so we can debug why a
    crop pass didn't fire — silent failure here was the whole reason
    the first LLM-vision attempt looked like a no-op.
    """
    import sys as _sys
    log = lambda msg: print(f"[crop-llm] {image_path.name}: {msg}", file=_sys.stderr, flush=True)

    api_key = os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        log("no OPENAI_API_KEY in env; skipping LLM bbox")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log("openai package not installed")
        return None
    try:
        with open(image_path, "rb") as f:
            import base64
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        log(f"could not read image: {e}")
        return None

    use_model = model or os.environ.get("PAPERWHIRL_OPENAI_MODEL") or "gpt-5-mini"
    client = OpenAI(api_key=api_key)
    prompt = (
        "This image is a single rendered page from a scientific paper. "
        "The page contains body text plus ONE figure (a chart, plot, "
        "diagram, or schematic). Identify the bounding box of the "
        "figure ONLY. INCLUDE axis labels, tick labels, legend, panel "
        "labels (A/B/C), and the figure's title or caption if it sits "
        "directly under the artwork. EXCLUDE surrounding body "
        "paragraphs and any caption text more than one line below the "
        "artwork. If the page has no figure (e.g. all body text), "
        'return {"present": false}.'
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["present", "x", "y", "width", "height"],
        "properties": {
            "present": {"type": "boolean"},
            "x": {"type": "number", "minimum": 0, "maximum": 100},
            "y": {"type": "number", "minimum": 0, "maximum": 100},
            "width": {"type": "number", "minimum": 0, "maximum": 100},
            "height": {"type": "number", "minimum": 0, "maximum": 100},
        },
    }
    log(f"calling model={use_model}")
    try:
        resp = client.chat.completions.create(
            model=use_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "figure_bbox",
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        import json
        raw = resp.choices[0].message.content or "{}"
        log(f"response: {raw[:200]}")
        data = json.loads(raw)
    except Exception as e:
        log(f"API call failed: {type(e).__name__}: {e}")
        return None
    if not data.get("present"):
        log("model returned present=false; no figure on this page")
        return None
    try:
        x = float(data["x"]); y = float(data["y"])
        w = float(data["width"]); h = float(data["height"])
    except (KeyError, TypeError, ValueError) as e:
        log(f"bad bbox shape: {e}")
        return None
    if w <= 0 or h <= 0:
        log(f"bbox has non-positive dims: w={w} h={h}")
        return None
    log(f"bbox pct: x={x:.1f} y={y:.1f} w={w:.1f} h={h:.1f}")
    return (x, y, w, h)


def crop_assets_to_figures(
    figures: list[dict[str, Any]],
    rendered_pages: dict[int, Path],
    pdf_path: Path,
    dpi: int = 180,
    padding_pts: float = 12.0,
) -> None:
    """Crop each figure's asset-page render to just the figure region.

    Strategy, in order:
    1. LLM vision (gpt-class model) returns a percent-bbox — preferred
       because it sees axis labels and panel labels as part of the
       figure, not as text-to-exclude.
    2. pdfplumber drawing-primitive bbox — fallback when LLM has no
       API key / fails / parses to None. Misses text glyphs in
       figures, so axis labels can get cut off.
    3. No crop — fallback when both above fail. Keeps the full page
       render the user has been seeing.
    """
    try:
        from PIL import Image
    except ImportError:
        return
    import sys as _sys
    scale = dpi / 72.0
    cropped: set[int] = set()
    for fig in figures:
        page_num = fig.get("asset_page")
        if page_num is None or page_num in cropped:
            continue
        img_path = rendered_pages.get(page_num)
        if img_path is None or not Path(img_path).exists():
            continue
        # First: LLM vision (returns percentages of image dims).
        pct = _llm_figure_bbox_pct(img_path)
        if pct is not None:
            with Image.open(img_path) as img:
                W, H = img.size
                x, y, w, h = pct
                px_x0 = max(0, int(W * x / 100.0))
                px_top = max(0, int(H * y / 100.0))
                px_x1 = min(W, int(W * (x + w) / 100.0))
                px_bot = min(H, int(H * (y + h) / 100.0))
                if px_x1 > px_x0 and px_bot > px_top:
                    print(f"[crop] {img_path.name} cropped via LLM", file=_sys.stderr, flush=True)
                    img.crop((px_x0, px_top, px_x1, px_bot)).save(img_path)
                    cropped.add(page_num)
                    continue
        # Fallback: pdfplumber geometric bbox.
        bbox = figure_bbox_from_pdf(pdf_path, page_num)
        if bbox is None:
            print(f"[crop] {img_path.name} no usable bbox; keeping full page", file=_sys.stderr, flush=True)
            continue
        print(f"[crop] {img_path.name} cropped via pdfplumber", file=_sys.stderr, flush=True)
        x0, top, x1, bot = bbox
        with Image.open(img_path) as img:
            W, H = img.size
            px_x0 = max(0, int((x0 - padding_pts) * scale))
            px_top = max(0, int((top - padding_pts) * scale))
            px_x1 = min(W, int((x1 + padding_pts) * scale))
            px_bot = min(H, int((bot + padding_pts) * scale))
            if px_x1 <= px_x0 or px_bot <= px_top:
                continue
            img.crop((px_x0, px_top, px_x1, px_bot)).save(img_path)
        cropped.add(page_num)


def render_pages(pdf_path: Path, pages: list[int], pages_dir: Path) -> dict[int, Path]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    rendered: dict[int, Path] = {}
    for page in sorted(set(pages)):
        prefix = pages_dir / f"page_{page:03d}"
        subprocess.run(
            [
                "pdftoppm",
                "-png",
                "-r",
                "180",
                "-f",
                str(page),
                "-l",
                str(page),
                str(pdf_path),
                str(prefix),
            ],
            check=True,
        )
        matches = sorted(pages_dir.glob(f"page_{page:03d}-*.png"))
        if matches:
            rendered[page] = matches[0]
    return rendered


def pdf_image_page_scores(pdf_path: Path) -> dict[int, int]:
    output = run_text(["pdfimages", "-list", str(pdf_path)])
    scores: dict[int, int] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[0].isdigit() or parts[2] == "smask":
            continue
        page = int(parts[0])
        try:
            width = int(parts[3])
            height = int(parts[4])
        except ValueError:
            continue
        scores[page] = scores.get(page, 0) + width * height
    return scores


def page_visual_score(path: Path) -> float:
    image = Image.open(path).convert("RGB").resize((160, 220))
    pixels = list(image.getdata())
    nonwhite = 0
    color = 0
    for red, green, blue in pixels:
        if red < 245 or green < 245 or blue < 245:
            nonwhite += 1
        if max(red, green, blue) - min(red, green, blue) > 18:
            color += 1
    total = max(1, len(pixels))
    return (nonwhite / total) + 4.0 * (color / total)


def choose_asset_pages(
    figures: list[dict[str, Any]],
    rendered_pages: dict[int, Path],
    image_scores: dict[int, int],
    page_count: int,
) -> None:
    for figure in figures:
        default_page = (
            figure["caption_page"]
            if figure["marker"] == "Figure" or figure["separator"] == "|"
            else max(1, figure["caption_page"] - 1)
        )
        candidates = sorted(
            {
                page
                for page in (
                    default_page,
                    figure["caption_page"] - 1,
                    figure["caption_page"],
                )
                if 1 <= page <= page_count and page in rendered_pages
            }
        )
        if not candidates:
            figure["asset_page"] = default_page
            continue
        if image_scores.get(figure["caption_page"], 0):
            figure["asset_page"] = figure["caption_page"]
        elif image_scores.get(default_page, 0):
            figure["asset_page"] = default_page
        else:
            figure["asset_page"] = max(candidates, key=lambda page: page_visual_score(rendered_pages[page]))


def build_skeleton(
    pdf_path: Path,
    slug: str,
    output_dir: Path,
    pages: list[str],
    pdfinfo: dict[str, str],
    figures: list[dict[str, Any]],
    rendered_pages: dict[int, Path],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    title, authors = parse_title_and_authors(metadata_page(pages), pdfinfo)

    source_figures = []
    walkthrough_figures = []
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    for figure in figures:
        source_id = f"source_fig_{figure['number']}"
        asset_path = rendered_pages.get(figure["asset_page"])
        figure_asset_path = None
        if asset_path:
            figure_asset_path = figures_dir / f"figure_{figure['number']}.png"
            shutil.copyfile(asset_path, figure_asset_path)
        asset = str(figure_asset_path.relative_to(output_dir)) if figure_asset_path else None
        source_figures.append(
            {
                "id": source_id,
                "figure": figure["label"],
                "page": figure["caption_page"],
                "asset": asset,
                "caption": figure["caption"],
                "extraction_method": "pdftotext caption regex + pdftoppm preceding page render",
            }
        )
        walkthrough_figures.append(
            {
                "id": f"figure_{figure['number']}",
                "order": figure["number"],
                "label": figure["label"],
                "source_figure_id": source_id,
                "view": {
                    "source_page": figure["caption_page"],
                    "asset": asset,
                    "asset_role": "rendered_page_crop" if asset else "missing",
                    "crop": None,
                    "original_caption": figure["caption"],
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
            }
        )

    return {
        "schema_version": "paperwhirl.review_session.v2",
        "session": {
            "id": f"{slug}_stage2_e2",
            "created_at": now,
            "updated_at": now,
            "mode": "full",
            "title": title,
        },
        "paper": {
            "id": slug,
            "title": title,
            "abstract": "",
            "authors": authors,
            "first_author": authors[0].split()[-1] if authors else "",
            "year": None,
            "journal": None,
            "doi": None,
            "pmid": None,
            "arxiv_id": None,
            "biorxiv_doi": None,
            "preprint_url": None,
            "source_pdf": str(pdf_path),
            "publication_state": "manuscript",
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
                "pages": [
                    {
                        "page": page_number,
                        "text": page_text.strip(),
                    }
                    for page_number, page_text in enumerate(pages, start=1)
                ],
            },
            "figures": source_figures,
        },
        "exports": {
            "pdf": {
                "path": None,
                "created_at": None,
            }
        },
    }


def extract_figures_only_from_pdf(
    pdf_path: Path, output_dir: Path
) -> list[dict[str, Any]]:
    """Run the PDF figure-extraction pipeline and return JUST the
    figures list, in the schema the renderer expects.

    Stage 5 E2 tire-kick (2026-05-18): used as a figure-source
    fallback when a structured extractor (PMC NXML, Nature HTML,
    etc.) returns zero figures. Reuses the same pipeline as
    `build_skeleton` so the output figure shape matches what other
    extractors produce — text/metadata stay from the structured
    source, figures come from the PDF.

    Returns an empty list on any failure or if the PDF has no
    detectable figures.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        pages = extract_pages(pdf_path)
        pdfinfo = parse_pdfinfo(pdf_path)
        figures_raw = find_figures(pages)
        if not figures_raw:
            return []
        render_page_nums = sorted({
            p for f in figures_raw
            for p in (f["caption_page"], f["caption_page"] - 1)
            if p >= 1
        })
        rendered_pages = render_pages(pdf_path, render_page_nums, output_dir / "pages")
        image_scores = pdf_image_page_scores(pdf_path)
        choose_asset_pages(figures_raw, rendered_pages, image_scores, len(pages))
        skel = build_skeleton(
            pdf_path=pdf_path,
            slug=output_dir.name,
            output_dir=output_dir,
            pages=pages,
            pdfinfo=pdfinfo,
            figures=figures_raw,
            rendered_pages=rendered_pages,
        )
        return skel.get("figures", [])
    except Exception as exc:
        print(f"  [pdf-figures] extraction failed: {type(exc).__name__}: {exc}")
        return []


def write_audit(figures: list[dict[str, Any]], output_path: Path) -> None:
    lines = [
        "# Extraction Audit",
        "",
        "| Figure | Asset OK? | Caption OK? | Page OK? | Notes |",
        "|---|---|---|---|---|",
    ]
    for figure in figures:
        lines.append(
            f"| {figure['label']} | unchecked | unchecked | unchecked | "
            f"caption page {figure['caption_page']}; asset page {figure['asset_page']} |"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
