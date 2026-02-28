#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$ROOT_DIR/deploy"
RUNTIME_DIR="$DEPLOY_DIR/nanobot"
TEMPLATE_CFG="$DEPLOY_DIR/config.template.json"
RUNTIME_CFG="$RUNTIME_DIR/config.json"

mkdir -p "$RUNTIME_DIR"

if [[ ! -f "$TEMPLATE_CFG" ]]; then
  echo "Missing template: $TEMPLATE_CFG" >&2
  exit 1
fi

cp -f "$TEMPLATE_CFG" "$RUNTIME_CFG"
echo "Applied config: $TEMPLATE_CFG -> $RUNTIME_CFG"

docker compose -f "$ROOT_DIR/docker-compose.yml" up -d nanobot-gateway >/dev/null

docker compose -f "$ROOT_DIR/docker-compose.yml" restart nanobot-gateway >/dev/null

docker compose -f "$ROOT_DIR/docker-compose.yml" run --rm nanobot-cli status
