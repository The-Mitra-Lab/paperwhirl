"""Reading List + per-paper persistence under a user-chosen data folder.

Layout (per E6 design):

    <data folder>/
      config.yaml
      papers/
        <paper-slug>/
          review_session.yaml
          figures/
          extracted_text.txt
          display_name.txt
      lists/
        _index.yaml
        <list-slug>/
          list.yaml

This module is filesystem-only — no network, no auth. The backend
exposes it through HTTP endpoints in `server.py`.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return s or "list"


# ---------------------------------------------------------------------------
# Display name helpers
# ---------------------------------------------------------------------------


def auto_shortname(packet: dict[str, Any]) -> str:
    """Build a short default name like 'Mullins et al. 2025'."""
    paper = packet.get("paper", {})
    authors = paper.get("authors", []) or []
    year = paper.get("year")

    def surname(full: str) -> str:
        return full.strip().split()[-1] if full.strip() else ""

    if not authors:
        base = paper.get("first_author") or paper.get("title") or "unknown"
    elif len(authors) == 1:
        base = surname(authors[0])
    elif len(authors) == 2:
        base = f"{surname(authors[0])} & {surname(authors[1])}"
    else:
        base = f"{surname(authors[0])} et al."

    return f"{base} {year}".strip() if year else base


def get_display_name(folder: Path, paper_slug: str) -> str:
    f = folder / "papers" / paper_slug / "display_name.txt"
    if f.exists():
        return f.read_text().strip() or paper_slug
    # Fall back to auto-shortname from the saved packet, if any.
    packet_path = folder / "papers" / paper_slug / "review_session.yaml"
    if packet_path.exists():
        try:
            return auto_shortname(_read_yaml(packet_path))
        except Exception as _exc:
            import sys as _sys
            print(
                f"[paperwhirl] could not auto-shortname {paper_slug!r}; "
                f"falling back to the slug: {_exc}",
                file=_sys.stderr,
            )
    return paper_slug


def set_display_name(folder: Path, paper_slug: str, name: str) -> None:
    paper_dir = folder / "papers" / paper_slug
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "display_name.txt").write_text(name.strip() + "\n")


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def _index_path(folder: Path) -> Path:
    return folder / "lists" / "_index.yaml"


def _list_dir(folder: Path, slug: str) -> Path:
    return folder / "lists" / slug


def _list_yaml_path(folder: Path, slug: str) -> Path:
    return _list_dir(folder, slug) / "list.yaml"


def read_index(folder: Path) -> dict[str, Any]:
    return _read_yaml(_index_path(folder)) or {
        "schema_version": "paperwhirl.reading_list_index.v1",
        "lists": [],
    }


def _write_index(folder: Path, data: dict[str, Any]) -> None:
    _write_yaml(_index_path(folder), data)


def _index_update(folder: Path, slug: str, **fields: Any) -> None:
    """Upsert an entry in _index.yaml."""
    idx = read_index(folder)
    entries = idx.get("lists", [])
    for i, e in enumerate(entries):
        if e.get("slug") == slug:
            entries[i] = {**e, **fields, "updated_at": _now()}
            break
    else:
        entries.append({
            "slug": slug,
            "name": fields.get("name", slug),
            "description": fields.get("description", ""),
            "updated_at": _now(),
        })
    idx["lists"] = entries
    _write_index(folder, idx)


def _index_remove(folder: Path, slug: str) -> None:
    idx = read_index(folder)
    idx["lists"] = [e for e in idx.get("lists", []) if e.get("slug") != slug]
    _write_index(folder, idx)


def _unique_slug(folder: Path, base: str) -> str:
    """Append -2, -3, … until the slug is free."""
    slug = base
    n = 2
    while _list_dir(folder, slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_list(folder: Path, name: str, description: str = "") -> dict[str, Any]:
    slug = _unique_slug(folder, slugify(name))
    _list_dir(folder, slug).mkdir(parents=True, exist_ok=True)
    now = _now()
    data = {
        "schema_version": "paperwhirl.reading_list.v1",
        "slug": slug,
        "name": name,
        "description": description,
        "created_at": now,
        "updated_at": now,
        "papers": [],
    }
    _write_yaml(_list_yaml_path(folder, slug), data)
    _index_update(folder, slug, name=name, description=description)
    return data


def get_list(folder: Path, slug: str) -> dict[str, Any] | None:
    path = _list_yaml_path(folder, slug)
    if not path.exists():
        return None
    return _read_yaml(path)


def lists_containing_paper(folder: Path, paper_slug: str) -> list[dict[str, str]]:
    """Stage 6 E7 (2026-05-26): reverse lookup — which lists currently
    reference this paper? Returns [{slug, name}, ...] in index order.
    Cheap (reads <list>/list.yaml files we already touch frequently);
    called per `GET /api/papers/{slug}` so the packet view can show
    a small list-membership line under the closing discussion.
    """
    out: list[dict[str, str]] = []
    idx = read_index(folder)
    for entry in idx.get("lists", []):
        list_slug = entry.get("slug")
        if not list_slug:
            continue
        data = get_list(folder, list_slug) or {}
        papers = data.get("papers") or []
        if any(p.get("slug") == paper_slug for p in papers):
            out.append({
                "slug": list_slug,
                "name": entry.get("name") or list_slug,
            })
    return out


def update_list(
    folder: Path,
    slug: str,
    name: str | None = None,
    description: str | None = None,
    papers: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    data = get_list(folder, slug)
    if data is None:
        return None
    if name is not None:
        data["name"] = name
    if description is not None:
        data["description"] = description
    if papers is not None:
        data["papers"] = papers
    data["updated_at"] = _now()
    _write_yaml(_list_yaml_path(folder, slug), data)
    _index_update(folder, slug, name=data["name"], description=data.get("description", ""))
    return data


def preview_list_orphans(folder: Path, slug: str) -> list[str] | None:
    """Compute which paper slugs would become unreferenced (orphan)
    if `slug` were deleted, WITHOUT performing the deletion.

    Returns None if the list doesn't exist. Returns [] if no papers
    would be orphaned (all of this list's papers are also in others).
    Used by both the frontend confirm dialog (so it shows an accurate
    count) and `delete_list` itself (single source of truth).
    """
    d = _list_dir(folder, slug)
    if not d.exists():
        return None
    list_yaml = d / "list.yaml"
    if not list_yaml.exists():
        return []
    data = _read_yaml(list_yaml)
    this_papers: list[str] = [
        p.get("slug") for p in data.get("papers", [])
        if p.get("slug")
    ]
    if not this_papers:
        return []

    other_referenced: set[str] = set()
    lists_root = folder / "lists"
    if lists_root.exists():
        for sub in lists_root.iterdir():
            if not sub.is_dir() or sub.name == slug:
                continue
            other_yaml = sub / "list.yaml"
            if not other_yaml.exists():
                continue
            other_data = _read_yaml(other_yaml)
            for p in other_data.get("papers", []):
                ps = p.get("slug")
                if ps:
                    other_referenced.add(ps)

    return [ps for ps in this_papers if ps not in other_referenced]


def delete_list(folder: Path, slug: str) -> dict[str, Any] | None:
    """Delete a reading list AND cascade-delete any papers that
    become unreferenced (no other list contains them).

    Stage 5 E4 (2026-05-18): pre-E4, deleting a list left papers on
    disk forever even when no list referenced them — silently
    orphaned. The library has no GC, so those papers were
    unreachable from the UI and consumed disk space indefinitely.
    The new behavior enforces the invariant "every paper in
    papers/<slug>/ is in at least one list" by cascade-deleting
    on list removal.

    Returns:
        {"slug": <list slug>, "cascaded_papers": [<paper_slug>, ...]}
        on success, or None if the list didn't exist.
    """
    orphans = preview_list_orphans(folder, slug)
    if orphans is None:
        return None
    d = _list_dir(folder, slug)
    shutil.rmtree(d)
    _index_remove(folder, slug)
    for ps in orphans:
        pdir = paper_dir(folder, ps)
        if pdir.exists():
            shutil.rmtree(pdir)
    return {"slug": slug, "cascaded_papers": orphans}


def find_unreferenced_papers(folder: Path) -> list[str]:
    """Return slugs of papers/<slug>/ dirs that aren't referenced by
    any list. Stage 5 E4 startup-sweep helper — cleans up
    pre-existing orphans from before `delete_list` cascaded
    (and from any other code paths that may have left an orphan
    behind, e.g. hand-edited list YAMLs).
    """
    pdir = papers_dir(folder)
    if not pdir.exists():
        return []
    existing = {sub.name for sub in pdir.iterdir() if sub.is_dir()}

    referenced: set[str] = set()
    lists_root = folder / "lists"
    if lists_root.exists():
        for sub in lists_root.iterdir():
            if not sub.is_dir():
                continue
            list_yaml = sub / "list.yaml"
            if not list_yaml.exists():
                continue
            data = _read_yaml(list_yaml)
            for p in data.get("papers", []):
                ps = p.get("slug")
                if ps:
                    referenced.add(ps)

    return sorted(existing - referenced)


def add_paper_to_list(folder: Path, list_slug: str, paper_slug: str) -> dict[str, Any] | None:
    data = get_list(folder, list_slug)
    if data is None:
        return None
    papers = data.get("papers", [])
    if not any(p.get("slug") == paper_slug for p in papers):
        papers.append({"slug": paper_slug, "added_at": _now()})
    data["papers"] = papers
    data["updated_at"] = _now()
    _write_yaml(_list_yaml_path(folder, list_slug), data)
    _index_update(folder, list_slug, name=data["name"])
    return data


def remove_paper_from_list(
    folder: Path, list_slug: str, paper_slug: str,
) -> dict[str, Any] | None:
    data = get_list(folder, list_slug)
    if data is None:
        return None
    data["papers"] = [p for p in data.get("papers", []) if p.get("slug") != paper_slug]
    data["updated_at"] = _now()
    _write_yaml(_list_yaml_path(folder, list_slug), data)
    _index_update(folder, list_slug, name=data["name"])
    return data


# ---------------------------------------------------------------------------
# Per-paper storage
# ---------------------------------------------------------------------------


def papers_dir(folder: Path) -> Path:
    return folder / "papers"


def paper_dir(folder: Path, paper_slug: str) -> Path:
    return papers_dir(folder) / paper_slug


def get_paper(folder: Path, paper_slug: str) -> dict[str, Any] | None:
    path = paper_dir(folder, paper_slug) / "review_session.yaml"
    if not path.exists():
        return None
    return _read_yaml(path)


def get_discussion(folder: Path, paper_slug: str) -> list[dict[str, Any]]:
    """Return the saved Discuss thread for a paper, or an empty list."""
    path = paper_dir(folder, paper_slug) / "discussion.yaml"
    if not path.exists():
        return []
    data = _read_yaml(path) or {}
    return data.get("messages", [])


def set_discussion(
    folder: Path, paper_slug: str, messages: list[dict[str, Any]],
) -> bool:
    """Persist the Discuss thread alongside a saved paper.

    Returns False if the paper isn't saved yet (no papers/<slug>/ dir).
    """
    pdir = paper_dir(folder, paper_slug)
    if not pdir.exists():
        return False
    _write_yaml(pdir / "discussion.yaml", {
        "schema_version": "paperwhirl.discussion.v1",
        "updated_at": _now(),
        "messages": messages,
    })
    return True


def save_packet(folder: Path, paper_slug: str, source_dir: Path) -> dict[str, Any]:
    """Copy a freshly-generated packet directory into papers/<paper_slug>/.

    `source_dir` is the per-paper extraction directory the streaming
    endpoint wrote to (e.g. results/stage2/experiment5/<slug>/).
    Idempotent — re-saving overwrites.
    """
    dest = paper_dir(folder, paper_slug)
    dest.mkdir(parents=True, exist_ok=True)

    # Copy the small, durable artifacts. Skip per-run scratch
    # files (logs, prompt dumps). discussion.yaml is included so
    # any browse-time Discuss thread (E8: persisted to the cache
    # subdir before the paper enters the library) migrates with
    # the rest of the packet.
    for name in (
        "review_session.yaml",
        "session_skeleton.yaml",
        "extracted_text.txt",
        "discussion.yaml",
    ):
        src = source_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)

    figures_src = source_dir / "figures"
    if figures_src.exists():
        figures_dst = dest / "figures"
        figures_dst.mkdir(exist_ok=True)
        for f in figures_src.iterdir():
            if f.is_file():
                shutil.copy2(f, figures_dst / f.name)

    # Stage 6 E8 Step 3 (2026-05-26): always persist uploaded.pdf
    # when it exists. The original E5 gate ("only when no
    # identifier was extracted") assumed that for identified
    # papers, Re-generate / Download manuscript could re-fetch
    # via fetch_publisher_pdf. E7 family-drops on PNAS proved
    # that wrong: a generic-PDF drop ends up with a DOI (via
    # E8 Step 1 Crossref enrichment) but there's no working
    # publisher fetcher for it, so re-fetch fails. Without the
    # local copy, the Download-manuscript button disappeared
    # for any generic-PDF save and Re-generate had nothing to
    # fall back to. Always-save costs ~1-5 MB per dropped PDF
    # and removes that whole failure class.
    uploaded_src = source_dir / "uploaded.pdf"
    if uploaded_src.exists():
        shutil.copy2(uploaded_src, dest / "uploaded.pdf")

    # Set display name if not already set.
    name_file = dest / "display_name.txt"
    if not name_file.exists():
        packet = get_paper(folder, paper_slug) or {}
        name_file.write_text(auto_shortname(packet) + "\n")

    return {
        "slug": paper_slug,
        "display_name": (name_file.read_text().strip() if name_file.exists() else paper_slug),
        "saved_at": _now(),
    }


def is_saved(folder: Path, paper_slug: str) -> bool:
    """True if the paper has been saved to the data folder
    (review_session.yaml present under papers/<slug>/)."""
    return (paper_dir(folder, paper_slug) / "review_session.yaml").exists()


def delete_paper(folder: Path, paper_slug: str) -> bool:
    """Remove a saved paper completely: its papers/<slug>/ dir AND
    any membership in lists/*/list.yaml.

    Used by the E8 "Delete from library" rail action and by the
    "remove from only list → confirm delete" branch. Returns True
    if anything was deleted, False if the paper dir was absent
    (so the HTTP layer can map that to a 404).

    Stage 6 E11 follow-up (2026-05-28): the rmtree step used to be
    a bare `shutil.rmtree(pdir)`. When it raised mid-walk — most
    reliably when Dropbox sync was holding a file open on the
    discussion.yaml during the cross-mac propagation — the
    exception bubbled out before the list-yaml cleanup ran, and
    the user was left with a partial papers/<slug>/ tree AND a
    dangling list-yaml reference (we hit exactly this earlier
    today). Now: best-effort delete, retry once after a short
    pause, ALWAYS run the list cleanup. The startup orphan sweep
    catches any directory residue that survives.
    """
    import sys as _sys
    import time as _time

    pdir = paper_dir(folder, paper_slug)
    if not pdir.exists():
        return False

    try:
        shutil.rmtree(pdir)
    except OSError as exc:
        print(
            f"  [delete_paper] partial rmtree of {pdir}: {exc}; "
            f"retrying after 0.5s",
            file=_sys.stderr, flush=True,
        )
        _time.sleep(0.5)
        shutil.rmtree(pdir, ignore_errors=True)
        if pdir.exists():
            print(
                f"  [delete_paper] {pdir} still has residue after retry; "
                f"startup orphan sweep will pick it up",
                file=_sys.stderr, flush=True,
            )

    lists_root = folder / "lists"
    if lists_root.exists():
        for sub in lists_root.iterdir():
            if not sub.is_dir():
                continue
            list_yaml = sub / "list.yaml"
            if not list_yaml.exists():
                continue
            data = _read_yaml(list_yaml)
            papers = data.get("papers", [])
            kept = [p for p in papers if p.get("slug") != paper_slug]
            if len(kept) != len(papers):
                data["papers"] = kept
                data["updated_at"] = _now()
                _write_yaml(list_yaml, data)
                _index_update(folder, sub.name, name=data.get("name", sub.name))
    return True


def find_by_doi(
    folder: Path, doi: str, exclude_slug: str | None = None
) -> str | None:
    """Find a saved paper by DOI. Returns its slug or None.

    Stage 5 E6 (2026-05-18): post-migration, every DOI-having paper
    lives at `slug_for_paper(doi=normalize_doi(doi))`. So this is now
    an O(1) directory-existence check instead of the pre-E6 walk over
    papers/*/review_session.yaml matching on `paper.doi`. The
    `exclude_slug` parameter is kept for callers that want to suppress
    matching against a slug they just computed (typically the
    just-extracted paper).
    """
    if not doi:
        return None
    from paperwhirl.slugs import slug_for_paper
    try:
        slug = slug_for_paper(doi=doi)
    except ValueError:
        return None
    if exclude_slug and slug == exclude_slug:
        return None
    if (paper_dir(folder, slug) / "review_session.yaml").exists():
        return slug
    return None


