#!/usr/bin/env bash
# Stage 6 E10 (2026-05-27, revised): zip-distribution variant.
#
# We started with a .dmg installer (custom background + branded
# drag-to-install layout), but Apple removed the `hdiutil
# internet-enable` flag in macOS 10.15 (2019) so every .dmg now
# leaves a residual mount on the user's desktop until they
# manually eject. For an unsigned test build distributed
# through Dropbox, the .zip path is cleaner: extract in place,
# drag .app to Applications, no mount-to-clean-up.
#
# Uses `ditto -c -k --keepParent --sequesterRsrc` — the macOS-
# aware zip path. Standard Unix `zip` mangles the .app's
# internal symlinks (Chromium framework, Poppler dylib install
# names) and resource forks; ditto preserves them. Also matches
# what Apple's notarization service expects when we hand it a
# zipped .app in E12.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP_PATH="$ROOT/src-tauri/target/release/bundle/macos/PaperWhirl.app"
ZIP_PATH="$ROOT/src-tauri/target/release/bundle/macos/PaperWhirl.zip"

if [ ! -d "$APP_PATH" ]; then
    echo "[make_zip] missing $APP_PATH — has tauri build + inject_python run?" >&2
    exit 1
fi

rm -f "$ZIP_PATH"

# cd into the .app's parent so the zip stores the .app at its
# top level (no extra path prefix); --keepParent preserves the
# .app's bundle name as the top-level entry inside the zip.
( cd "$(dirname "$APP_PATH")" && \
    ditto -c -k --keepParent --sequesterRsrc \
        "$(basename "$APP_PATH")" "$ZIP_PATH" )

echo "[make_zip] done — $ZIP_PATH"
du -sh "$ZIP_PATH"
