#!/usr/bin/env bash
# Re-download the vendored front-end libraries from the npm registry.
#
# MapLibre stays on v5: deck.gl 9.4's MapboxOverlay reads `map.transform`, which MapLibre 6 removed.
# After upgrading deck.gl, re-check MapView.syncDeckFramebuffer() in prack/static/js/map.js
# (workaround for overlays shifting after the map is resized).
set -euo pipefail

MAPLIBRE=${MAPLIBRE:-5.24.0}
DECK=${DECK:-9.4.0}
UPLOT=${UPLOT:-1.6.32}

root="$(cd "$(dirname "$0")/.." && pwd)"
vendor="$root/prack/static/vendor"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

npm pack --silent "maplibre-gl@$MAPLIBRE" "deck.gl@$DECK" "uplot@$UPLOT" >/dev/null
for f in *.tgz; do mkdir -p "${f%.tgz}" && tar xzf "$f" -C "${f%.tgz}"; done

cp "maplibre-gl-$MAPLIBRE/package/dist/maplibre-gl.js" "maplibre-gl-$MAPLIBRE/package/dist/maplibre-gl.css" \
   "maplibre-gl-$MAPLIBRE/package/LICENSE.txt" "$vendor/maplibre-gl/"
cp "deck.gl-$DECK/package/dist.min.js" "$vendor/deck.gl/deck.gl.min.js"
cp "deck.gl-$DECK/package/LICENSE" "$vendor/deck.gl/LICENSE"
cp "uplot-$UPLOT/package/dist/uPlot.iife.min.js" "uplot-$UPLOT/package/dist/uPlot.min.css" \
   "uplot-$UPLOT/package/LICENSE" "$vendor/uplot/"

echo "Vendored maplibre-gl $MAPLIBRE, deck.gl $DECK, uplot $UPLOT into $vendor"
