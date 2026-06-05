#!/usr/bin/env bash
# Build the macOS .icns from the canonical icon source.
#
# Inputs:
#   assets/icons/icon_source_1024.png    1024x1024 full-bleed PNG (no
#                                        squircle yet; produced by
#                                        Nano Banana from the original
#                                        PaperWhirl logo).
#
# Outputs:
#   assets/icons/icon_squircle_1024.png  intermediate: source with a
#                                        185 px rounded-corner alpha
#                                        mask applied (the macOS Big
#                                        Sur+ icon shape).
#   assets/icons/icon.iconset/           intermediate: 10 multi-res
#                                        PNGs in the iconutil format.
#   assets/icons/icon.icns               final: what Tauri consumes.
#
# Both intermediates are gitignored. Only the source PNG and the
# .icns are tracked.
#
# Requires: python with Pillow (in the paperwhirl env), sips,
# iconutil. All present on macOS by default once Pillow is
# installed.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
ICONS_DIR="$REPO_ROOT/assets/icons"

SRC="$ICONS_DIR/icon_source_1024.png"
SQUIRCLE="$ICONS_DIR/icon_squircle_1024.png"
ICONSET="$ICONS_DIR/icon.iconset"
ICNS="$ICONS_DIR/icon.icns"

if [ ! -f "$SRC" ]; then
    echo "missing $SRC" >&2
    exit 1
fi

echo "==> applying squircle mask (radius 185)"
python <<PY
from PIL import Image, ImageDraw
src = Image.open("$SRC").convert("RGBA")
w, h = src.size
assert (w, h) == (1024, 1024), f"expected 1024x1024, got {w}x{h}"
mask = Image.new("L", (w, h), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=185, fill=255)
out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
out.paste(src, (0, 0), mask)
out.save("$SQUIRCLE")
PY

echo "==> generating iconset"
rm -rf "$ICONSET"
mkdir "$ICONSET"
sips -Z 16   "$SQUIRCLE" --out "$ICONSET/icon_16x16.png"       > /dev/null
sips -Z 32   "$SQUIRCLE" --out "$ICONSET/icon_16x16@2x.png"    > /dev/null
sips -Z 32   "$SQUIRCLE" --out "$ICONSET/icon_32x32.png"       > /dev/null
sips -Z 64   "$SQUIRCLE" --out "$ICONSET/icon_32x32@2x.png"    > /dev/null
sips -Z 128  "$SQUIRCLE" --out "$ICONSET/icon_128x128.png"     > /dev/null
sips -Z 256  "$SQUIRCLE" --out "$ICONSET/icon_128x128@2x.png"  > /dev/null
sips -Z 256  "$SQUIRCLE" --out "$ICONSET/icon_256x256.png"     > /dev/null
sips -Z 512  "$SQUIRCLE" --out "$ICONSET/icon_256x256@2x.png"  > /dev/null
sips -Z 512  "$SQUIRCLE" --out "$ICONSET/icon_512x512.png"     > /dev/null
sips -Z 1024 "$SQUIRCLE" --out "$ICONSET/icon_512x512@2x.png"  > /dev/null

echo "==> folding into .icns"
rm -f "$ICNS"
iconutil --convert icns "$ICONSET" --output "$ICNS"

echo "==> done: $ICNS"
ls -la "$ICNS"
