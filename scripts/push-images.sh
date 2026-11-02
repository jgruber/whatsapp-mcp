#!/usr/bin/env bash
# Build the bridge and MCP images from this working tree and push them to
# Docker Hub under docker.io/jgruber/*. Tags both with the current short SHA
# and (if on main) :latest. For CI, see .github/workflows/docker-publish.yml.
set -euo pipefail

OWNER="${OWNER:-jgruber}"
SHA="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BRIDGE="docker.io/${OWNER}/whatsapp-mcp-bridge"
SERVER="docker.io/${OWNER}/whatsapp-mcp-server"

TAGS=("$SHA")
if [ "$BRANCH" = "main" ]; then TAGS+=("latest"); fi

echo "SHA=$SHA  branch=$BRANCH  tags=${TAGS[*]}"

# Build once, tag many — docker buildx can push every tag in one go.
build_push() {
  local image="$1" context="$2"
  local args=()
  for t in "${TAGS[@]}"; do args+=("-t" "${image}:${t}"); done
  docker buildx build --push "${args[@]}" "$context"
}

build_push "$BRIDGE" whatsapp-bridge
build_push "$SERVER" whatsapp-mcp-server

echo
echo "Pushed:"
for t in "${TAGS[@]}"; do
  echo "  $BRIDGE:$t"
  echo "  $SERVER:$t"
done
