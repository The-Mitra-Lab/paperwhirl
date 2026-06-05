#!/usr/bin/env bash
# Stage 6 E3: install Playwright's bundled Chromium into
# src-tauri/target/browsers/ for inclusion in the .app.
# inject_python.sh copies the result into
# PaperWhirl.app/Contents/Resources/browsers/; the sidecar
# sets PLAYWRIGHT_BROWSERS_PATH to that location at runtime.
#
# Uses the bundled Python's Playwright to do the install, so
# the Chromium revision pin matches the bundled Playwright
# version exactly — no version-drift footguns. Idempotent:
# Playwright detects when the right Chromium rev is already
# present and skips the download.
#
# Chained from bundle_python.sh; not normally invoked directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAURI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_DIR="${TAURI_DIR}/target/python"
BROWSERS_DIR="${TAURI_DIR}/target/browsers"

if [[ ! -x "${PYTHON_DIR}/bin/python3.12" ]]; then
    echo "[bundle_chromium] error: ${PYTHON_DIR}/bin/python3.12 not found"
    echo "                  run bundle_python.sh first"
    exit 1
fi

mkdir -p "${BROWSERS_DIR}"

echo "[bundle_chromium] target: ${BROWSERS_DIR}"
echo "[bundle_chromium] installing Chromium via the bundled Playwright"

PLAYWRIGHT_BROWSERS_PATH="${BROWSERS_DIR}" \
    "${PYTHON_DIR}/bin/python3.12" -m playwright install chromium

echo "[bundle_chromium] done"
du -sh "${BROWSERS_DIR}"
