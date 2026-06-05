#!/usr/bin/env bash
# Stage 6 E2: bundle a redistributable Python interpreter + the
# paperwhirl package + all pip dependencies into
# src-tauri/target/python/. Tauri's bundle.resources then copies
# that tree into PaperWhirl.app/Contents/Resources/python/ during
# `npm run tauri build`.
#
# Idempotent — re-runs are seconds when the bundled tree is
# already at the pinned PBS version. Bumping PBS_RELEASE or
# PYTHON_VERSION below triggers a clean re-extract on the next
# run.

set -euo pipefail

# Pinned PBS release + Python version. Bump together; the asset
# filename embeds both.
# https://github.com/astral-sh/python-build-standalone/releases
PBS_RELEASE="20260510"
PYTHON_VERSION="3.12.13"
ARCH="aarch64-apple-darwin"

TARBALL="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${ARCH}-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${TARBALL}"

CACHE_DIR="${HOME}/.cache/paperwhirl-build"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAURI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# scripts/ -> src-tauri/ -> frontend/ -> web/ -> src/ -> repo root
REPO_ROOT="$(cd "${TAURI_DIR}/../../../.." && pwd)"
OUT_DIR="${TAURI_DIR}/target/python"
STAMP="${OUT_DIR}/PYTHON_VERSION"
WANT="${PYTHON_VERSION}+${PBS_RELEASE}"

echo "[bundle_python] repo root: ${REPO_ROOT}"
echo "[bundle_python] target:    ${OUT_DIR}"
echo "[bundle_python] version:   ${WANT}"

mkdir -p "${CACHE_DIR}"

# 1. Download (cached).
if [[ ! -f "${CACHE_DIR}/${TARBALL}" ]]; then
    echo "[bundle_python] downloading ${TARBALL}"
    curl --fail --location --progress-bar \
        --output "${CACHE_DIR}/${TARBALL}" \
        "${URL}"
else
    echo "[bundle_python] cached:  ${CACHE_DIR}/${TARBALL}"
fi

# 2. Extract (idempotent via stamp file).
if [[ -f "${STAMP}" ]] && [[ "$(cat "${STAMP}")" == "${WANT}" ]]; then
    echo "[bundle_python] python tree already at ${WANT}; skipping extract"
else
    echo "[bundle_python] extracting to ${OUT_DIR}"
    rm -rf "${OUT_DIR}"
    mkdir -p "${OUT_DIR}"
    # PBS tarballs unpack to a top-level python/ dir; strip it so
    # OUT_DIR has bin/, lib/, etc. directly.
    tar -xzf "${CACHE_DIR}/${TARBALL}" -C "${OUT_DIR}" --strip-components=1
    echo "${WANT}" > "${STAMP}"
fi

# 2.5. Copy the Vite frontend build into the paperwhirl package
# at src/paperwhirl/web/frontend_dist/ so setuptools picks it up
# via package-data. That puts the React build *inside* the
# installed paperwhirl package; the backend's StaticFiles mount
# at server.py serves it. With the build inside the backend, the
# Tauri shell can point its window at http://127.0.0.1:<port>/
# and everything (UI + API) is same-origin — no cross-origin
# download/CORS dance.
FRONTEND_DIST="${REPO_ROOT}/src/web/frontend/dist"
PKG_FRONTEND_DIST="${REPO_ROOT}/src/paperwhirl/web/frontend_dist"
if [[ ! -d "${FRONTEND_DIST}" ]]; then
    echo "[bundle_python] error: ${FRONTEND_DIST} not found"
    echo "                run 'npm run build' first (or use 'npm run build-app')"
    exit 1
fi
echo "[bundle_python] copying frontend dist into paperwhirl package"
rm -rf "${PKG_FRONTEND_DIST}"
cp -R "${FRONTEND_DIST}" "${PKG_FRONTEND_DIST}"

# 3. Install paperwhirl + deps into the bundled interpreter.
# Always re-run; pip is fast when everything is already
# satisfied. Local source install (not editable) — the bundled
# .app needs site-packages to contain a real copy, not a symlink
# back to the source tree.
echo "[bundle_python] installing paperwhirl + deps from ${REPO_ROOT}"
"${OUT_DIR}/bin/pip" install \
    --quiet \
    --disable-pip-version-check \
    --no-cache-dir \
    "${REPO_ROOT}"
# Belt-and-suspenders: pip's "already satisfied" check skips the
# install when paperwhirl 0.1.0 is already present, even if the
# source files have changed. Force-reinstall paperwhirl alone so
# code edits without a version bump always land in the bundle.
# --no-deps keeps this fast — deps were just confirmed satisfied
# by the install above.
echo "[bundle_python] force-reinstalling paperwhirl (source edits)"
"${OUT_DIR}/bin/pip" install \
    --quiet \
    --disable-pip-version-check \
    --no-cache-dir \
    --force-reinstall \
    --no-deps \
    "${REPO_ROOT}"

# 4. Replace bin/paperwhirl with a relocatable wrapper.
# pip writes an absolute-path shebang into console scripts that
# points at the build machine's interpreter — useless when the
# .app moves to a colleague's Mac. The replacement is a tiny sh
# script that finds its sibling python3.12 at runtime via $0.
echo "[bundle_python] writing relocatable bin/paperwhirl wrapper"
cat > "${OUT_DIR}/bin/paperwhirl" <<'WRAPPER'
#!/bin/sh
# Stage 6 E2 — relocatable launcher for the bundled paperwhirl.
# Built by src-tauri/scripts/bundle_python.sh. Resolves the
# bundled Python relative to its own location so the .app works
# wherever the user drags it.
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "${HERE}/python3.12" -m paperwhirl "$@"
WRAPPER
chmod +x "${OUT_DIR}/bin/paperwhirl"

echo "[bundle_python] done"
echo "[bundle_python] bin/paperwhirl: ${OUT_DIR}/bin/paperwhirl"

# Stage 6 E3: chain Chromium install. Keeps one
# `npm run build-app` call producing the full runtime.
echo "[bundle_python] chaining into bundle_chromium.sh"
bash "${SCRIPT_DIR}/bundle_chromium.sh"

# Stage 6 E4: chain Poppler bundle.
echo "[bundle_python] chaining into bundle_poppler.sh"
bash "${SCRIPT_DIR}/bundle_poppler.sh"
