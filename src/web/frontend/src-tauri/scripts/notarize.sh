#!/usr/bin/env bash
# Stage 6 E12: notarize the signed PaperWhirl.app and produce a stapled,
# distributable PaperWhirl.zip.
#
# Pre-req: the .app is already Developer ID signed with hardened runtime
# (scripts/sign_app.sh), and a notarytool keychain profile exists
# (default name: paperwhirl-notary — created once via
# `xcrun notarytool store-credentials`).
#
# Flow:
#   1. ditto the signed .app into a zip (notarytool ingests a zipped .app).
#   2. Submit to Apple's notary service and --wait for the verdict.
#   3. On "Accepted", staple the ticket to the .app so Gatekeeper passes
#      offline / on first launch.
#   4. Re-zip the STAPLED .app — that zip is the distributable artifact
#      (you cannot staple a .zip directly, only the .app inside it).
#
# Usage:
#   bash notarize.sh [/path/to/PaperWhirl.app]
#   NOTARY_PROFILE=other-profile bash notarize.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAURI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP="${1:-${TAURI_DIR}/target/release/bundle/macos/PaperWhirl.app}"
ZIP="${APP%/*}/PaperWhirl.zip"
PROFILE="${NOTARY_PROFILE:-paperwhirl-notary}"

[[ -d "${APP}" ]] || { echo "[notarize] error: app not found: ${APP}" >&2; exit 1; }

# Refuse to notarize an unsigned / ad-hoc bundle — submission would just
# be rejected after the upload. A Developer ID signature has an Authority
# line naming the team. Capture codesign's output first (it writes to
# stderr); piping straight into `grep -q` trips SIGPIPE + pipefail and
# would falsely report "not signed".
CODESIGN_INFO="$(codesign -dvv "${APP}" 2>&1 || true)"
if ! grep -q "Authority=Developer ID Application" <<<"${CODESIGN_INFO}"; then
    echo "[notarize] error: ${APP} is not Developer ID signed — run sign_app.sh first" >&2
    exit 1
fi

echo "[notarize] (1/4) zipping signed .app for submission…"
rm -f "${ZIP}"
( cd "${APP%/*}" && ditto -c -k --keepParent --sequesterRsrc "$(basename "${APP}")" "${ZIP}" )

echo "[notarize] (2/4) submitting to Apple (profile: ${PROFILE}) — this can take a few minutes…"
xcrun notarytool submit "${ZIP}" --keychain-profile "${PROFILE}" --wait

echo "[notarize] (3/4) stapling the ticket to the .app…"
xcrun stapler staple "${APP}"

echo "[notarize] (4/4) re-zipping the stapled .app…"
rm -f "${ZIP}"
( cd "${APP%/*}" && ditto -c -k --keepParent --sequesterRsrc "$(basename "${APP}")" "${ZIP}" )

echo "[notarize] verifying Gatekeeper acceptance (offline)…"
spctl -a -vvv --type exec "${APP}"
xcrun stapler validate "${APP}"
echo "[notarize] done — distributable artifact: ${ZIP}"
du -sh "${ZIP}"
