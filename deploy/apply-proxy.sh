#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$ROOT_DIR/deploy"
RUNTIME_DIR="$DEPLOY_DIR/xcbot"
TEMPLATE_CFG="$DEPLOY_DIR/config.template.json"
RUNTIME_CFG="$RUNTIME_DIR/config.json"

PROXY_DIR="$DEPLOY_DIR/proxy/mihomo"
MMDB_FILE="$PROXY_DIR/Country.mmdb"

mkdir -p "$RUNTIME_DIR"

mkdir -p "$PROXY_DIR"

need_mmdb="0"
if [[ ! -f "$MMDB_FILE" ]]; then
  need_mmdb="1"
elif [[ ! -s "$MMDB_FILE" ]]; then
  need_mmdb="1"
fi

if [[ "$need_mmdb" == "1" ]]; then
  echo "Country.mmdb missing/empty, downloading..."
  tmpfile="$(mktemp)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "https://github.com/Dreamacro/maxmind-geoip/releases/latest/download/Country.mmdb" -o "$tmpfile" || \
    curl -fsSL "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/country.mmdb" -o "$tmpfile"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmpfile" "https://github.com/Dreamacro/maxmind-geoip/releases/latest/download/Country.mmdb" || \
    wget -qO "$tmpfile" "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/country.mmdb"
  else
    echo "Neither curl nor wget found; please install one to download Country.mmdb" >&2
    exit 1
  fi

  if [[ ! -s "$tmpfile" ]]; then
    echo "Failed to download Country.mmdb" >&2
    exit 1
  fi

  mv -f "$tmpfile" "$MMDB_FILE"
  echo "Downloaded: $MMDB_FILE"
fi

cp -f "$TEMPLATE_CFG" "$RUNTIME_CFG"
echo "Applied config: $TEMPLATE_CFG -> $RUNTIME_CFG"

docker compose -f "$ROOT_DIR/docker-compose.proxy.yml" up -d --build >/dev/null

docker compose -f "$ROOT_DIR/docker-compose.proxy.yml" run --rm xcbot-cli status
