#!/usr/bin/env bash
# Run a command inside the BlocksDS build container, e.g. `tools/docker.sh make nds`.
# The image is (re)built automatically when the Dockerfile changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=debug-dsi-build
command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }
command -v docker >/dev/null || { echo "Install and start Docker Desktop, then retry." >&2; exit 1; }
# The BlocksDS image is x86 Linux, including on Apple Silicon.
PLATFORM=linux/amd64
HASH=$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha1(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()[:12])' "$ROOT/tools/docker/Dockerfile")
TAG="$IMAGE:$HASH-amd64"

if ! docker info >/dev/null 2>&1; then
    echo "Docker is not reachable. Start Docker Desktop (or your Docker engine), then retry." >&2
    exit 1
fi
if ! docker image inspect "$TAG" >/dev/null 2>&1; then
    docker build --platform "$PLATFORM" -t "$TAG" -t "$IMAGE:latest" -f "$ROOT/tools/docker/Dockerfile" "$ROOT/tools/docker"
fi

RUN_ARGS=(--rm --init --platform "$PLATFORM" -v "$ROOT:/work")
[ -t 0 ] && [ -t 1 ] && RUN_ARGS+=(-it)

[ "$#" -gt 0 ] || set -- bash
exec docker run "${RUN_ARGS[@]}" -u "$(id -u):$(id -g)" -w /work "$TAG" "$@"
