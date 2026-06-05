"""Local-machine config for PaperWhirl.

Persists the user's settings at `~/.paperwhirl/config.json`:

    folder            data folder (reading lists + per-paper packets)
    api_key           OpenAI API key
    model             generation model id
    reasoning_effort  reasoning effort for the gpt-5.5 family
    generation_prompt optional override of the review preamble
    discussion_prompt optional override of the Discuss system prompt
    interests         free-text research interests, injected into the
                      general-chat Discuss system prompt (Stage 5 E1)

All fields are optional. The file is created on first write and
chmod 600 — the API key lives here in plaintext, same threat model
as a `.env` file (a Keychain-backed store is later, app-shell work).

Public surface:
    load_config()            -> dict | None
    update_config(**fields)  -> dict        (merge read-modify-write)
    get_folder()             -> Path | None
    get_api_key()            -> str | None
    get_model()              -> str | None
    get_reasoning_effort()   -> str | None
    get_generation_prompt()  -> str | None
    get_discussion_prompt()  -> str | None
    bootstrap_folder(folder)
    cache_dir()              -> Path        (extraction scratch)
    jats_cache_dir()         -> Path        (raw JATS XML cache)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path.home() / ".paperwhirl"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

# Fields the config file is allowed to hold (besides the `updated_at`
# stamp). Anything else passed to update_config is ignored.
_FIELDS = (
    "folder",
    "api_key",
    "model",
    "reasoning_effort",
    "generation_prompt",
    "discussion_prompt",
    "interests",
)


def load_config() -> dict[str, Any] | None:
    """Return the parsed config dict, or None if not yet set."""
    if not _CONFIG_FILE.exists():
        return None
    try:
        return json.loads(_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def update_config(**fields: Any) -> dict[str, Any]:
    """Merge `fields` into the config — read-modify-write.

    Only keys in `_FIELDS` are accepted; others are ignored. A value
    of None or "" clears that field. `folder` is expanded/resolved to
    an absolute path string. Returns the new config dict.
    """
    cfg = load_config() or {}
    for key, value in fields.items():
        if key not in _FIELDS:
            continue
        if value in (None, ""):
            cfg.pop(key, None)
            continue
        if key == "folder":
            cfg[key] = str(Path(value).expanduser().resolve())
        else:
            cfg[key] = value
    cfg["updated_at"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )

    _CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(_CONFIG_FILE, 0o600)
    except OSError:
        pass
    return cfg


def _get(key: str) -> Any | None:
    cfg = load_config()
    if not cfg:
        return None
    return cfg.get(key) or None


def get_folder() -> Path | None:
    """Return the configured data folder as a Path, or None."""
    f = _get("folder")
    return Path(f) if f else None


def get_api_key() -> str | None:
    """Return the stored OpenAI API key, or None."""
    return _get("api_key")


def get_model() -> str | None:
    """Return the stored generation model id, or None (caller defaults)."""
    return _get("model")


def get_reasoning_effort() -> str | None:
    """Return the stored reasoning effort, or None (caller defaults)."""
    return _get("reasoning_effort")


def get_generation_prompt() -> str | None:
    """Return the stored generation-prompt override, or None."""
    return _get("generation_prompt")


def get_discussion_prompt() -> str | None:
    """Return the stored discussion-prompt override, or None."""
    return _get("discussion_prompt")


def get_unpaywall_email() -> str | None:
    """Return the email Unpaywall should use to identify our requests.

    Unpaywall requires *some* email in every API call as a rate-limiting
    identifier; they validate that the domain has MX records but never
    actually send mail. Operator-configurable so a lab/institution can
    set its own attribution.
    """
    return _get("unpaywall_email")


def get_interests() -> str | None:
    """Return the user's research-interests free-text, or None.

    Injected into the general-chat Discuss system prompt so paper
    suggestions can be tailored. Not injected into the per-paper
    prompt — that one is already grounded in the packet + paper text.
    """
    return _get("interests")


def _cache_root() -> Path:
    """Platform-specific user cache base for PaperWhirl, without a subdir.

    Kept off the user-chosen data folder because that folder is often
    a synced location (Dropbox, iCloud) and shouldn't pick up hundreds
    of MB of figures the user never asked to save.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "PaperWhirl"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "PaperWhirl" if local else Path.home() / "AppData" / "Local" / "PaperWhirl"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "PaperWhirl"


def cache_dir() -> Path:
    """Scratch dir for fresh extractions (one subdir per slug)."""
    d = _cache_root() / "extractions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jats_cache_dir() -> Path:
    """Cache dir for raw bioRxiv / PMC JATS XML fetches."""
    d = _cache_root() / "jats_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bootstrap_folder(folder: Path) -> None:
    """Create the on-disk layout under the chosen data folder.

    Idempotent — re-running on an existing folder leaves real data
    alone and only fills in missing pieces.
    """
    folder = folder.expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "papers").mkdir(exist_ok=True)
    (folder / "lists").mkdir(exist_ok=True)

    config_yaml = folder / "config.yaml"
    if not config_yaml.exists():
        config_yaml.write_text(yaml.safe_dump({
            "schema_version": "paperwhirl.local.v1",
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            # Stage 5 E6 (2026-05-18): new folders skip the v1→v2
            # slug migration. Pre-existing folders without this stamp
            # trigger the migration on backend start.
            "slug_version": 2,
        }, sort_keys=False))

    index = folder / "lists" / "_index.yaml"
    if not index.exists():
        index.write_text(yaml.safe_dump({
            "schema_version": "paperwhirl.reading_list_index.v1",
            "lists": [],
        }, sort_keys=False))
