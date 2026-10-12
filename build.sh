#!/bin/bash
# Build the Remote Bridge ipk. Requires ares-package (@webos-tools/cli).
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build && mkdir -p build/app/assets
cp -r src/* build/app/
cp daemon/bridge.py daemon/97-kodiremote build/app/assets/
chmod +x build/app/assets/97-kodiremote
ares-package build/app -o build >/dev/null
echo "Built: $(ls build/*.ipk)"
