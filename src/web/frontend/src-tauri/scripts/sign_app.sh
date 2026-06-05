#!/usr/bin/env bash
# Stage 6 E12: Developer ID signing for the injected PaperWhirl.app.
#
# Must run AFTER inject_python.sh has injected the python / browsers /
# poppler trees, because adding files to a bundle invalidates any prior
# signature. Signs inside-out (nested Mach-O first, nested bundles next,
# outer .app last) with hardened runtime + our entitlements so the result
# can be notarized. Apple explicitly advises against `codesign --deep`
# for *signing* (only verification), so we walk the tree ourselves.
#
# Usage:
#   PAPERWHIRL_SIGN_ID="Developer ID Application: Name (TEAMID)" \
#     bash sign_app.sh [/path/to/PaperWhirl.app]
#
# Iterate fast by re-running this against the existing built .app — no
# rebuild needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAURI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP="${1:-${TAURI_DIR}/target/release/bundle/macos/PaperWhirl.app}"
ENTITLEMENTS="${TAURI_DIR}/Entitlements.plist"

if [[ -z "${PAPERWHIRL_SIGN_ID:-}" ]]; then
    echo "[sign_app] error: set PAPERWHIRL_SIGN_ID to your Developer ID Application identity" >&2
    echo "           e.g. PAPERWHIRL_SIGN_ID=\"Developer ID Application: Rob Mitra (C6TY3RQDUX)\"" >&2
    exit 1
fi
[[ -d "${APP}" ]]          || { echo "[sign_app] error: app not found: ${APP}" >&2; exit 1; }
[[ -f "${ENTITLEMENTS}" ]] || { echo "[sign_app] error: entitlements not found: ${ENTITLEMENTS}" >&2; exit 1; }

echo "[sign_app] signing ${APP}"
echo "[sign_app]   identity: ${PAPERWHIRL_SIGN_ID}"

SIGN=(codesign --force --timestamp --options runtime \
      --entitlements "${ENTITLEMENTS}" --sign "${PAPERWHIRL_SIGN_ID}")

# 1) Every nested Mach-O file (dylibs, .so, helper executables), content
#    first. `find -type f` skips symlinks, so PBS's real dylib targets get
#    signed rather than their aliases. `file` is the reliable Mach-O test
#    since Chromium helpers carry no extension.
echo "[sign_app]   (1/3) nested Mach-O files…"
count=0
while IFS= read -r f; do
    if file -b "$f" | grep -q "Mach-O"; then
        "${SIGN[@]}" "$f" >/dev/null
        count=$((count + 1))
    fi
done < <(find "${APP}/Contents/Resources" "${APP}/Contents/MacOS" -type f)
echo "[sign_app]         signed ${count} Mach-O files"

# 2) Nested bundles (Chromium.app and its .framework dirs), deepest path
#    first so each is sealed only after its own contents are signed.
echo "[sign_app]   (2/3) nested bundles…"
while IFS= read -r b; do
    echo "[sign_app]         ${b#"${APP}/"}"
    "${SIGN[@]}" "$b"
done < <(find "${APP}/Contents/Resources" -type d \( -name "*.app" -o -name "*.framework" \) \
         | awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)

# 3) Seal the outer bundle last.
echo "[sign_app]   (3/3) outer .app…"
"${SIGN[@]}" "${APP}"

echo "[sign_app] verifying (codesign --strict)…"
codesign --verify --deep --strict --verbose=2 "${APP}"
echo "[sign_app] Gatekeeper assessment (spctl)…"
spctl -a -vvv --type exec "${APP}" || echo "[sign_app] note: spctl will pass only AFTER notarization+staple"
echo "[sign_app] done"
