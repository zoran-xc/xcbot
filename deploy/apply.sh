#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$ROOT_DIR/deploy"
RUNTIME_DIR="$DEPLOY_DIR/nanobot"
TEMPLATE_CFG="$DEPLOY_DIR/config.template.json"
RUNTIME_CFG="$RUNTIME_DIR/config.json"
HASH_FILE="$RUNTIME_DIR/.code_hash"

mkdir -p "$RUNTIME_DIR"

if [[ ! -f "$TEMPLATE_CFG" ]]; then
  echo "Missing template: $TEMPLATE_CFG" >&2
  exit 1
fi

cp -f "$TEMPLATE_CFG" "$RUNTIME_CFG"
echo "Applied config: $TEMPLATE_CFG -> $RUNTIME_CFG"

CODE_HASH=""
if command -v git >/dev/null 2>&1 && git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  CODE_HASH="$({
    git -C "$ROOT_DIR" rev-parse HEAD
    git -C "$ROOT_DIR" status --porcelain
    git -C "$ROOT_DIR" diff --no-ext-diff
  } | shasum -a 256 | awk '{print $1}')"
else
  CODE_HASH="$({
    find "$ROOT_DIR/nanobot" -type f -print0 2>/dev/null | sort -z | xargs -0 shasum -a 256 2>/dev/null || true
    shasum -a 256 "$ROOT_DIR/Dockerfile" 2>/dev/null || true
    shasum -a 256 "$ROOT_DIR/docker-compose.yml" 2>/dev/null || true
  } | shasum -a 256 | awk '{print $1}')"
fi

PREV_HASH=""
if [[ -f "$HASH_FILE" ]]; then
  PREV_HASH="$(cat "$HASH_FILE" | tr -d ' \n\r\t')"
fi

if [[ -n "$CODE_HASH" && "$CODE_HASH" != "$PREV_HASH" ]]; then
  echo "Code changed, rebuilding nanobot-gateway..."
  docker compose -f "$ROOT_DIR/docker-compose.yml" build nanobot-gateway
  printf "%s\n" "$CODE_HASH" > "$HASH_FILE"
else
  echo "Code unchanged, skip build."
fi

docker compose -f "$ROOT_DIR/docker-compose.yml" up -d nanobot-gateway >/dev/null

docker compose -f "$ROOT_DIR/docker-compose.yml" restart nanobot-gateway >/dev/null

docker compose -f "$ROOT_DIR/docker-compose.yml" run --rm nanobot-cli status
