#!/bin/bash
# Build the Remote Bridge ipk (tools/pack_ipk.py: ares-package's ipks fail on webOS 5).
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build && mkdir -p build/app/assets
cp -r src/* build/app/
cp daemon/bridge.py daemon/97-kodiremote build/app/assets/
chmod +x build/app/assets/97-kodiremote
python3 tools/pack_ipk.py build/app build >/dev/null
echo "Built: $(ls build/*.ipk)"
