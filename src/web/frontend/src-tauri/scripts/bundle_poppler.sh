#!/usr/bin/env bash
# Stage 6 E4: copy Poppler's pdftotext + pdftoppm out of the
# project's conda env into src-tauri/target/poppler/ for inclusion
# in the .app. inject_python.sh copies the result into
# PaperWhirl.app/Contents/Resources/poppler/; the sidecar prepends
# poppler/bin to PATH at runtime so subprocess.run(["pdftotext", ...])
# calls in the Python code resolve to the bundled binary.
#
# Source: $CONDA_PREFIX (the active paperwhirl env's conda-forge
# poppler). Per the Stage 6 Mirror-Dev principle, we use the same
# Poppler dev uses — not a different upstream.
#
# Chained from bundle_python.sh; not normally invoked directly.

set -euo pipefail

if [[ -z "${CONDA_PREFIX:-}" ]]; then
    echo "[bundle_poppler] error: CONDA_PREFIX unset"
    echo "                 activate the paperwhirl env first"
    exit 1
fi

# Poppler CLIs the Python code actually calls:
#   pdftotext  - body text + caption extraction (extract.py, biorxiv.py, resolve.py)
#   pdftoppm   - page-image renders (extract.py)
#   pdfinfo    - page count + PDF metadata (extract.py via parse_pdfinfo)
#   pdfimages  - figure-asset page detection (extract.py)
POPPLER_TOOLS=(pdftotext pdftoppm pdfinfo pdfimages)

for tool in "${POPPLER_TOOLS[@]}"; do
    if [[ ! -x "${CONDA_PREFIX}/bin/${tool}" ]]; then
        echo "[bundle_poppler] error: ${tool} missing from ${CONDA_PREFIX}/bin"
        echo "                 expected poppler in the conda env (see environment.yml)"
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAURI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${TAURI_DIR}/target/poppler"
BIN_DIR="${OUT_DIR}/bin"
LIB_DIR="${OUT_DIR}/lib"

echo "[bundle_poppler] source: ${CONDA_PREFIX}"
echo "[bundle_poppler] target: ${OUT_DIR}"

# Clean slate every run. The closure walk is fast; idempotency from
# a stamp file isn't worth the bookkeeping when the inputs are a
# handful of binaries.
rm -rf "${OUT_DIR}"
mkdir -p "${BIN_DIR}" "${LIB_DIR}"

# 1. Copy the binaries we actually call.
for tool in "${POPPLER_TOOLS[@]}"; do
    cp "${CONDA_PREFIX}/bin/${tool}" "${BIN_DIR}/${tool}"
    chmod u+w "${BIN_DIR}/${tool}"
done

# 2. Walk otool -L transitively, copying every non-system dylib
# into LIB_DIR. References can be absolute (/path/to/lib.dylib),
# @rpath/lib.dylib, or @loader_path/lib.dylib — all resolve to a
# basename under ${CONDA_PREFIX}/lib for the conda-forge poppler
# stack.
deps_of() {
    # Drop the first line (the binary's own name) and the
    # current binary's self-reference. Print each dep path.
    otool -L "$1" | tail -n +2 | awk '{print $1}'
}

is_system() {
    case "$1" in
        /usr/lib/*|/System/*) return 0 ;;
        *) return 1 ;;
    esac
}

resolve_src() {
    # Map any otool-reported reference to a real file under
    # ${CONDA_PREFIX}/lib by basename.
    local ref="$1"
    local name
    name="$(basename "${ref}")"
    echo "${CONDA_PREFIX}/lib/${name}"
}

declare -A SEEN
queue=()
for tool in "${POPPLER_TOOLS[@]}"; do
    queue+=("${BIN_DIR}/${tool}")
done

while [[ ${#queue[@]} -gt 0 ]]; do
    target="${queue[0]}"
    queue=("${queue[@]:1}")
    while IFS= read -r dep; do
        [[ -z "${dep}" ]] && continue
        if is_system "${dep}"; then continue; fi
        name="$(basename "${dep}")"
        # Skip the binary's self-id reference.
        if [[ "$(basename "${target}")" == "${name}" ]]; then continue; fi
        if [[ -n "${SEEN[${name}]:-}" ]]; then continue; fi
        SEEN[${name}]=1
        src="$(resolve_src "${dep}")"
        if [[ ! -f "${src}" ]]; then
            echo "[bundle_poppler] error: missing dep ${name} (looked for ${src})"
            exit 1
        fi
        cp "${src}" "${LIB_DIR}/${name}"
        chmod u+w "${LIB_DIR}/${name}"
        queue+=("${LIB_DIR}/${name}")
    done < <(deps_of "${target}")
done

echo "[bundle_poppler] copied $(ls "${LIB_DIR}" | wc -l | tr -d ' ') dylibs"

# 3. Rewrite install names so every non-system reference resolves
# inside the bundle.
# - dylibs: own id -> @loader_path/<name>; each dep -> @loader_path/<dep>
# - binaries: each dep -> @loader_path/../lib/<dep>
for dylib in "${LIB_DIR}"/*.dylib; do
    name="$(basename "${dylib}")"
    install_name_tool -id "@loader_path/${name}" "${dylib}"
    while IFS= read -r dep; do
        [[ -z "${dep}" ]] && continue
        if is_system "${dep}"; then continue; fi
        depname="$(basename "${dep}")"
        if [[ "${depname}" == "${name}" ]]; then continue; fi
        install_name_tool -change "${dep}" "@loader_path/${depname}" "${dylib}"
    done < <(deps_of "${dylib}")
done

for bin in "${BIN_DIR}"/*; do
    while IFS= read -r dep; do
        [[ -z "${dep}" ]] && continue
        if is_system "${dep}"; then continue; fi
        depname="$(basename "${dep}")"
        install_name_tool -change "${dep}" "@loader_path/../lib/${depname}" "${bin}"
    done < <(deps_of "${bin}")
done

# 4. Re-sign ad-hoc. install_name_tool invalidates the original
# signature, and Apple Silicon refuses to load binaries with a
# broken signature. inject_python.sh re-signs again with --deep
# inside the .app, but doing it here makes the standalone bundle
# self-contained (useful for dev testing target/poppler/ directly).
echo "[bundle_poppler] re-signing ad-hoc"
for f in "${LIB_DIR}"/*.dylib "${BIN_DIR}"/*; do
    codesign --force --sign - "${f}" 2>/dev/null
done

# 5. Sanity check: no remaining absolute references to anything
# outside /usr/lib or /System/.
echo "[bundle_poppler] verifying install names"
leaks=0
for f in "${BIN_DIR}"/* "${LIB_DIR}"/*.dylib; do
    while IFS= read -r dep; do
        [[ -z "${dep}" ]] && continue
        case "${dep}" in
            @loader_path/*|/usr/lib/*|/System/*) ;;
            *)
                # Anything else (absolute conda path, @rpath, …) is a leak.
                echo "[bundle_poppler] leak: ${f} -> ${dep}"
                leaks=$((leaks + 1))
                ;;
        esac
    done < <(deps_of "${f}")
done

if [[ ${leaks} -gt 0 ]]; then
    echo "[bundle_poppler] error: ${leaks} unresolved reference(s)"
    exit 1
fi

echo "[bundle_poppler] done"
du -sh "${OUT_DIR}"
