#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

V=$(git describe --tags --always --dirty 2>/dev/null || echo "v$(date +%Y%m%d-%H%M)")
R=$(git rev-parse --short HEAD 2>/dev/null || echo "local")
D=$(date -u +%Y-%m-%dT%H:%M:%SZ)

docker build \
  --build-arg VERSION="$V" \
  --build-arg VCS_REF="$R" \
  --build-arg BUILD_DATE="$D" \
  -t "deckmover:$V" \
  -t deckmover:local \
  .

echo "Built tags: deckmover:$V and deckmover:local"
echo "Tip: set the Unraid template Repository to deckmover:$V for a visible version."
