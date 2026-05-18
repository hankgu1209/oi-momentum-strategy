#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "${1:-}" == "--pull" ]]; then
  git pull --ff-only origin "${2:-main}"
fi

mkdir -p data configs

if [[ ! -f configs/strategy.local.yaml ]]; then
  cp configs/strategy.example.yaml configs/strategy.local.yaml
  echo "Created configs/strategy.local.yaml from template."
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit DASHBOARD_DOMAIN and DASHBOARD_PASSWORD_HASH, then run again."
  exit 1
fi

docker compose --profile domain up -d --build
docker compose ps

domain="$(grep -E '^DASHBOARD_DOMAIN=' .env | tail -n 1 | cut -d= -f2- || true)"
https_port="$(grep -E '^CADDY_HTTPS_PORT=' .env | tail -n 1 | cut -d= -f2- || true)"
https_port="${https_port:-443}"

if [[ -n "$domain" && "$domain" != "dashboard.example.com" ]]; then
  if [[ "$https_port" == "443" ]]; then
    echo "Dashboard: https://$domain/"
  else
    echo "Dashboard: https://$domain:$https_port/"
  fi
fi

