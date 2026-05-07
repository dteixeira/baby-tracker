#!/usr/bin/env bash
# Build a multi-arch Docker image (linux/amd64 + linux/arm64) and push to a
# local (or any) registry.
#
# Usage:
#   ./scripts/build-push.sh <registry> [tag] [image-name]
#
# Arguments:
#   registry    Registry URL, e.g. registry.local:5000 or 192.168.1.10:5000
#   tag         Image tag. Defaults to the current git tag (git describe --tags).
#               Pass "latest" to force that tag.
#   image-name  Image name inside the registry. Defaults to "baby-tracker".
#
# Examples:
#   ./scripts/build-push.sh registry.local:5000
#   ./scripts/build-push.sh registry.local:5000 v1.2.0
#   ./scripts/build-push.sh 192.168.1.10:5000 v1.2.0 my-tracker

set -euo pipefail

REGISTRY="${1:-}"
TAG="${2:-}"
IMAGE_NAME="${3:-baby-tracker}"
BUILDER_NAME="baby-tracker-builder"

# ── Validate ──────────────────────────────────────────────────────────────────

if [[ -z "$REGISTRY" ]]; then
  echo "Error: registry URL is required." >&2
  echo "Usage: $0 <registry> [tag] [image-name]" >&2
  exit 1
fi

if [[ -z "$TAG" ]]; then
  TAG="$(git describe --tags --exact-match 2>/dev/null || true)"
  if [[ -z "$TAG" ]]; then
    echo "Error: no tag provided and HEAD has no git tag." >&2
    echo "Either 'git tag vX.Y.Z' on this commit or pass a tag as the second argument." >&2
    exit 1
  fi
  echo "→ Using git tag: $TAG"
fi

FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}"

# ── Ensure buildx builder exists ─────────────────────────────────────────────
# The default docker driver does not support multi-platform exports to a
# registry. We need a docker-container driver builder.

if ! docker buildx inspect "$BUILDER_NAME" &>/dev/null; then
  echo "→ Creating buildx builder '$BUILDER_NAME'…"
  docker buildx create \
    --name "$BUILDER_NAME" \
    --driver docker-container \
    --bootstrap
else
  echo "→ Using existing buildx builder '$BUILDER_NAME'"
fi

# ── Build & push ──────────────────────────────────────────────────────────────

echo "→ Building ${FULL_IMAGE}:${TAG} for linux/amd64 + linux/arm64…"

docker buildx build \
  --builder "$BUILDER_NAME" \
  --platform linux/amd64,linux/arm64 \
  --tag "${FULL_IMAGE}:${TAG}" \
  --tag "${FULL_IMAGE}:latest" \
  --push \
  "$(git rev-parse --show-toplevel)"

echo ""
echo "✓ Pushed:"
echo "  ${FULL_IMAGE}:${TAG}"
echo "  ${FULL_IMAGE}:latest"
