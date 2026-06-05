#!/usr/bin/env bash
# Stage 6 E2: post-build step that injects the bundled Python
# tree into PaperWhirl.app/Contents/Resources/python/.
#
# Why a post-build step instead of Tauri's bundle.resources?
# Tauri's resource copy chokes on the python tree's combination
# of symlinks + size with "Operation not permitted (os error 1)"
# during the .app bundle phase.
#
# Why the move-to-/tmp dance? macOS applies a write-protection
# to freshly-built .app bundles while they sit inside the
# project's target/ directory. Symptom: cp / ditto / install
# all fail with "Operation not permitted" on most descendants.
# Moving the .app to /tmp first lifts the protection; once
# python is injected we move it back. Mechanism isn't fully
# documented but empirically reproducible.
#
# Invoked by the `build-app` npm script after `tauri build`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAURI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_SRC="${TAURI_DIR}/target/python"
BROWSERS_SRC="${TAURI_DIR}/target/browsers"
POPPLER_SRC="${TAURI_DIR}/target/poppler"
APP="${TAURI_DIR}/target/release/bundle/macos/PaperWhirl.app"
STAGING="/tmp/PaperWhirl-injecting-$$.app"

if [[ ! -d "${PYTHON_SRC}" ]]; then
    echo "[inject_python] error: ${PYTHON_SRC} not found"
    echo "                run bundle_python.sh first (or use 'npm run build-app')"
    exit 1
fi

if [[ ! -d "${BROWSERS_SRC}" ]]; then
    echo "[inject_python] error: ${BROWSERS_SRC} not found"
    echo "                run bundle_chromium.sh first (or use 'npm run build-app')"
    exit 1
fi

if [[ ! -d "${POPPLER_SRC}" ]]; then
    echo "[inject_python] error: ${POPPLER_SRC} not found"
    echo "                run bundle_poppler.sh first (or use 'npm run build-app')"
    exit 1
fi

if [[ ! -d "${APP}" ]]; then
    echo "[inject_python] error: ${APP} not found"
    echo "                run 'npm run tauri build' first"
    exit 1
fi

echo "[inject_python] staging ${APP}"
echo "[inject_python]    via  ${STAGING}"

# Move to /tmp to lift the freshly-built write protection.
rm -rf "${STAGING}"
mv "${APP}" "${STAGING}"

# Tauri ad-hoc-signed the .app; strip the signature so we can
# inject new content. E7 will re-sign properly for notarization.
codesign --remove-signature "${STAGING}" 2>/dev/null || true

# Replace any prior python tree (e.g. if the .app moved here
# already had one). cp -R preserves the symlinks PBS ships
# (python -> python3.12 etc.).
rm -rf "${STAGING}/Contents/Resources/python"
ditto "${PYTHON_SRC}" "${STAGING}/Contents/Resources/python"

# Stage 6 E3: inject Playwright's bundled Chromium too.
# The sidecar sets PLAYWRIGHT_BROWSERS_PATH to point at this
# tree at runtime.
echo "[inject_python] injecting browsers tree"
rm -rf "${STAGING}/Contents/Resources/browsers"
ditto "${BROWSERS_SRC}" "${STAGING}/Contents/Resources/browsers"

# Stage 6 E4: inject the Poppler bundle. The sidecar prepends
# poppler/bin to PATH so subprocess.run(["pdftotext", ...])
# calls in the Python code resolve here.
echo "[inject_python] injecting poppler tree"
rm -rf "${STAGING}/Contents/Resources/poppler"
ditto "${POPPLER_SRC}" "${STAGING}/Contents/Resources/poppler"

# Move back into the build output dir.
mv "${STAGING}" "${APP}"

# Ad-hoc re-sign so macOS lets the .app launch. Without any
# signature at all, Finder + spctl reject the bundle with
# "code object is not signed at all". `-` is the ad-hoc
# identity; --deep covers the injected python tree's dylibs.
# E7 will replace this with a Developer ID signature for
# notarization.
echo "[inject_python] ad-hoc re-signing the .app"
codesign --force --deep --sign - "${APP}"

echo "[inject_python] done"
echo "[inject_python] $(find "${APP}/Contents/Resources/python" -type f | wc -l | tr -d ' ') files in bundle"
du -sh "${APP}"
