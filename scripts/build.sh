#!/usr/bin/env bash
# Build the PaperWhirl frontend and mirror it into the Python package.
#
# Steps:
#   1. `npm ci`   in src/web/frontend (reproducible install).
#   2. `npm run build` — emits src/web/frontend/dist/.
#   3. Copy that dist/ into src/paperwhirl/web/frontend_dist/ so the
#      Python wheel ships the bundle as package data.
#
# Run from the repo root or any subdir — the script resolves paths
# relative to its own location.
#
# After this finishes, run `pip install -e .` (or `pip install .`) to
# pick up the bundled frontend.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

FRONTEND_SRC="$REPO_ROOT/src/web/frontend"
FRONTEND_OUT="$FRONTEND_SRC/dist"
PACKAGE_DEST="$REPO_ROOT/src/paperwhirl/web/frontend_dist"

echo "==> npm ci in $FRONTEND_SRC"
( cd "$FRONTEND_SRC" && npm ci )

echo "==> npm run build"
( cd "$FRONTEND_SRC" && npm run build )

if [ ! -d "$FRONTEND_OUT" ]; then
    echo "build failed: $FRONTEND_OUT not produced" >&2
    exit 1
fi

echo "==> copy $FRONTEND_OUT -> $PACKAGE_DEST"
rm -rf "$PACKAGE_DEST"
mkdir -p "$PACKAGE_DEST"
cp -R "$FRONTEND_OUT/." "$PACKAGE_DEST/"

echo "==> done. Now run: pip install -e ."
