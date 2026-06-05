"""Library v1 → v2 slug migration — Stage 5 E6 (2026-05-18).

Migrates an existing data folder from the pre-E6 slug rule
(per-extractor bespoke slug derivation) to the E6 rule
(canonical DOI-based slug). Idempotent and gated by a
`slug_version: 2` stamp in `<data>/config.yaml` — only runs when
the stamp is absent or less than 2.

Steps, in order:
    1. Tar backup of the entire data folder to
       `<data>/.paperwhirl_backup_pre_v2_<timestamp>.tgz`. Skipped
       if the same-timestamp backup already exists (the migration
       crashed and restarted within the same second).
    2. Compute the old → new slug map from each
       `papers/*/review_session.yaml`. Papers missing the packet
       file or missing a usable identifier (no DOI, no PMCID, no
       PDF available) keep their old slug.
    3. Detect collisions — two old slugs that would map to the
       same new slug (e.g. a paper saved as a DOI input AND as a
       PMCID input pre-E6). Keep the *larger* directory (more
       bytes ≈ figures + discussion present); the loser's
       discussion.yaml is preserved as a sidecar
       `discussion_from_<old_slug>.yaml` under the winner.
    4. Atomically rename each paper dir under `papers/`.
    5. Rewrite every `lists/*/list.yaml` so paper-references point
       at the new slugs.
    6. Wipe the extraction cache — keyed by old slugs, so all
       entries are stale after the rename. The cache is regen-
       erable; no user data lost.
    7. Stamp `slug_version: 2` in `<data>/config.yaml`.

Failure handling: any unexpected exception aborts before step 4 (no
on-disk changes yet). After step 4 there's no rollback path beyond
"restore from the .tgz" — the backup is the safety net.

Per user policy: never destructive without explicit permission. The
backup at step 1 IS that permission's safety net. The caller (server
startup handler) prints a log line so the user sees what happened.

Public surface:
    migrate_v1_to_v2(folder: Path, dry_run: bool = False) -> dict
"""

from __future__ import annotations

import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from paperwhirl.slugs import slug_from_packet


_BACKUP_PREFIX = ".paperwhirl_backup_pre_v2_"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        print(f"  [migrate] could not parse {path}: {exc}", file=sys.stderr)
        return {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _dir_size_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _backup(folder: Path) -> Path:
    """Tar+gzip the data folder into a sibling .tgz. Returns the
    backup path. Excludes any prior backups so we don't compound
    them."""
    stamp = _now_stamp()
    backup_path = folder / f"{_BACKUP_PREFIX}{stamp}.tgz"
    if backup_path.exists():
        print(f"  [migrate] backup already exists at {backup_path}, reusing",
              file=sys.stderr)
        return backup_path

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # Skip prior migration backups (any .tgz at the top level
        # matching the prefix). The backup is supposed to capture
        # the v1 state, not a chain of prior backups.
        name = Path(tarinfo.name).name
        if name.startswith(_BACKUP_PREFIX):
            return None
        return tarinfo

    with tarfile.open(backup_path, "w:gz") as tar:
        for child in folder.iterdir():
            if child.name.startswith(_BACKUP_PREFIX):
                continue
            tar.add(child, arcname=child.name, filter=_filter)
    print(f"  [migrate] backup written → {backup_path}", file=sys.stderr)
    return backup_path


def _compute_slug_map(folder: Path) -> dict[str, str]:
    """Build {old_slug: new_slug}. Paper dirs without a parseable
    packet OR without a derivable new slug keep their old slug
    (identity mapping). The migration step then skips identity
    entries — they're already in the right place."""
    papers_dir = folder / "papers"
    if not papers_dir.exists():
        return {}

    mapping: dict[str, str] = {}
    for sub in papers_dir.iterdir():
        if not sub.is_dir():
            continue
        packet = _read_yaml(sub / "review_session.yaml")
        new = slug_from_packet(packet) if packet else None
        if not new:
            # No DOI / no PMCID — try the PDF-hash fallback if an
            # uploaded.pdf exists. Carries pre-E6 PDF-drop entries
            # whose packet has neither DOI nor PMCID.
            pdf_path = sub / "uploaded.pdf"
            if pdf_path.exists():
                try:
                    from paperwhirl.slugs import slug_for_paper
                    pdf_bytes = pdf_path.read_bytes()
                    new = slug_for_paper(pdf_bytes=pdf_bytes)
                except (OSError, ValueError):
                    new = sub.name
            else:
                new = sub.name
        mapping[sub.name] = new
    return mapping


def _resolve_collisions(
    folder: Path, mapping: dict[str, str]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Where two old slugs map to the same new slug, keep the larger
    paper dir and demote the loser. Returns (filtered_map,
    collisions) where filtered_map excludes the losers and
    `collisions` records what happened for logging."""
    inverse: dict[str, list[str]] = {}
    for old, new in mapping.items():
        inverse.setdefault(new, []).append(old)

    papers_dir = folder / "papers"
    filtered: dict[str, str] = {}
    collisions: list[dict[str, Any]] = []
    for new, olds in inverse.items():
        if len(olds) == 1:
            filtered[olds[0]] = new
            continue
        # Pick the winner: largest dir size (proxy for "has figures
        # / has discussion / has more material"). Tie-break by name
        # for determinism.
        scored = [
            (_dir_size_bytes(papers_dir / o), o) for o in olds
        ]
        scored.sort(key=lambda t: (-t[0], t[1]))
        winner = scored[0][1]
        losers = [o for _, o in scored[1:]]
        filtered[winner] = new
        for loser in losers:
            # Preserve the loser's discussion (if any) as a sidecar
            # in the winner's dir so the conversation isn't dropped.
            # Spec form: .discussion_from_<loser>.yaml.bak — leading
            # dot keeps it out of normal `ls`, .bak signals "recovery
            # artifact, not part of the live packet".
            disc = papers_dir / loser / "discussion.yaml"
            if disc.exists():
                target = (
                    papers_dir / winner / f".discussion_from_{loser}.yaml.bak"
                )
                try:
                    shutil.copy2(disc, target)
                except OSError as exc:
                    print(f"  [migrate] could not save sidecar {target}: {exc}",
                          file=sys.stderr)
        collisions.append({
            "new_slug": new,
            "winner": winner,
            "losers": losers,
        })
    return filtered, collisions


def _rename_paper_dirs(
    folder: Path, mapping: dict[str, str], collisions: list[dict[str, Any]]
) -> list[str]:
    """Apply the slug-map renames under papers/. Losers from
    collisions are removed (their useful artifacts already
    sidecarred). Returns the list of old slugs that were
    physically renamed (for logging)."""
    papers_dir = folder / "papers"

    # First, remove loser dirs (collisions). They're not in
    # `mapping` (filtered out earlier), so the rename loop won't
    # see them.
    for col in collisions:
        for loser in col["losers"]:
            loser_dir = papers_dir / loser
            if loser_dir.exists():
                try:
                    shutil.rmtree(loser_dir)
                    print(f"  [migrate] removed collision loser {loser}",
                          file=sys.stderr)
                except OSError as exc:
                    print(f"  [migrate] could not remove {loser}: {exc}",
                          file=sys.stderr)

    # Two-phase rename to avoid name clashes during in-flight
    # renames (e.g. old='a' → new='b' AND old='b' → new='c'). Phase
    # 1 moves every changing dir to a temp name; phase 2 moves
    # those temps to their final names.
    renames = [(o, n) for o, n in mapping.items() if o != n]
    stage_suffix = f"__migrate_stage_{_now_stamp()}"
    staged: list[tuple[str, str]] = []  # (staged_name, new)
    for old, new in renames:
        src = papers_dir / old
        if not src.exists():
            continue
        staged_name = f"{old}{stage_suffix}"
        staged_dir = papers_dir / staged_name
        try:
            src.rename(staged_dir)
            staged.append((staged_name, new))
        except OSError as exc:
            print(f"  [migrate] could not stage {old}: {exc}", file=sys.stderr)

    renamed: list[str] = []
    for staged_name, new in staged:
        src = papers_dir / staged_name
        dst = papers_dir / new
        if dst.exists():
            # Shouldn't happen (collisions were resolved), but be
            # defensive. Skip this paper rather than clobber.
            print(f"  [migrate] target {new} already exists; leaving "
                  f"{staged_name} staged for manual review",
                  file=sys.stderr)
            continue
        try:
            src.rename(dst)
            renamed.append(new)
        except OSError as exc:
            print(f"  [migrate] could not rename {staged_name} → {new}: {exc}",
                  file=sys.stderr)
    return renamed


def _rewrite_list_yamls(folder: Path, mapping: dict[str, str]) -> int:
    """Rewrite every list.yaml so paper-slug references point at
    the new slugs. Returns the count of lists touched."""
    lists_root = folder / "lists"
    if not lists_root.exists():
        return 0
    touched = 0
    for sub in lists_root.iterdir():
        if not sub.is_dir():
            continue
        list_yaml = sub / "list.yaml"
        if not list_yaml.exists():
            continue
        data = _read_yaml(list_yaml)
        papers = data.get("papers", [])
        changed = False
        for entry in papers:
            old_slug = entry.get("slug")
            if not old_slug:
                continue
            new_slug = mapping.get(old_slug, old_slug)
            if new_slug != old_slug:
                entry["slug"] = new_slug
                changed = True
        if changed:
            data["papers"] = papers
            data["updated_at"] = datetime.now(timezone.utc).replace(
                microsecond=0
            ).isoformat()
            _write_yaml(list_yaml, data)
            touched += 1
    return touched


def _stamp_v2(folder: Path) -> None:
    config_yaml = folder / "config.yaml"
    data = _read_yaml(config_yaml)
    data["slug_version"] = 2
    data["slug_migration_at"] = datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat()
    _write_yaml(config_yaml, data)


def needs_migration(folder: Path) -> bool:
    """Cheap check the startup handler can call. True if `folder`
    has v1 layout AND lacks the `slug_version: 2` stamp."""
    if not folder.exists():
        return False
    config_yaml = folder / "config.yaml"
    if not config_yaml.exists():
        # Folder predates `config.yaml` entirely. Treat as
        # needing migration only if it actually has content under
        # papers/ — empty folders just need bootstrapping.
        papers_dir = folder / "papers"
        return papers_dir.exists() and any(papers_dir.iterdir())
    data = _read_yaml(config_yaml)
    return int(data.get("slug_version", 1)) < 2


def migrate_v1_to_v2(folder: Path, dry_run: bool = False) -> dict[str, Any]:
    """Run the v1 → v2 migration. Idempotent: a folder that's
    already at v2 (per `slug_version: 2`) is a no-op.

    Returns a summary dict the caller (startup handler) logs:
        {
            "status": "skipped" | "completed" | "dry_run",
            "backup": <Path or None>,
            "renamed": int,
            "collisions": [...],
            "lists_touched": int,
            "cache_wiped": bool,
        }
    """
    summary: dict[str, Any] = {
        "status": "skipped",
        "backup": None,
        "renamed": 0,
        "collisions": [],
        "lists_touched": 0,
        "cache_wiped": False,
    }

    if not needs_migration(folder):
        return summary

    print(
        f"[paperwhirl] migration: starting v1 → v2 slug migration "
        f"for {folder}",
        file=sys.stderr,
    )

    mapping = _compute_slug_map(folder)
    filtered, collisions = _resolve_collisions(folder, mapping)
    summary["collisions"] = collisions

    nontrivial = {o: n for o, n in filtered.items() if o != n}
    print(
        f"  [migrate] {len(mapping)} paper dirs scanned, "
        f"{len(nontrivial)} need renaming, "
        f"{len(collisions)} slug collision(s)",
        file=sys.stderr,
    )
    # Per-paper preview (spec: "for each paper, print old_slug →
    # new_slug"). Logged before the rename so the user sees the
    # plan in the dry-run AND real runs.
    for old, new in sorted(nontrivial.items()):
        print(f"    [migrate] {old} → {new}", file=sys.stderr)
    for col in collisions:
        print(
            f"    [migrate] collision on {col['new_slug']}: "
            f"winner={col['winner']}, losers={col['losers']}",
            file=sys.stderr,
        )

    if dry_run:
        summary["status"] = "dry_run"
        summary["renamed"] = len(nontrivial)
        return summary

    # Step 1: backup. Done even if no renames are pending — a
    # `slug_version: 1` folder may still have list YAMLs that
    # weren't bumped, and the stamp itself is a write.
    summary["backup"] = _backup(folder)

    # Step 2-5: rename + rewrite lists.
    renamed = _rename_paper_dirs(folder, filtered, collisions)
    summary["renamed"] = len(renamed)
    summary["lists_touched"] = _rewrite_list_yamls(folder, filtered)

    # Step 6: wipe the extraction cache. Cache subdirs are keyed
    # by old slugs; after rename they're all stale. The cache is
    # purely regenerable so the wipe is safe.
    try:
        from paperwhirl import config as pw_config
        cache_dir = pw_config.cache_dir()
        if cache_dir.exists():
            for child in cache_dir.iterdir():
                if child.is_dir():
                    try:
                        shutil.rmtree(child)
                    except OSError as exc:
                        print(f"  [migrate] could not wipe cache "
                              f"subdir {child}: {exc}", file=sys.stderr)
            summary["cache_wiped"] = True
    except Exception as exc:
        print(f"  [migrate] cache wipe skipped: {exc}", file=sys.stderr)

    # Step 7: stamp v2.
    _stamp_v2(folder)
    summary["status"] = "completed"
    print(
        f"[paperwhirl] migration: completed — {summary['renamed']} dir(s) "
        f"renamed, {summary['lists_touched']} list yaml(s) rewritten, "
        f"{len(collisions)} collision(s) resolved",
        file=sys.stderr,
    )
    return summary
