#!/usr/bin/env bash
# Installs and bootstraps my-gmail-assistant.
# Usage: ./install.sh [--docker]
#   (no flag)  local venv install
#   --docker   build & start via docker compose

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

MODE="${1:-local}"
[[ "$MODE" == "--docker" ]] && MODE="docker"

log() { printf '\033[1;32m[install]\033[0m %s\n' "$1"; }
err() { printf '\033[1;31m[install]\033[0m %s\n' "$1" >&2; }

ensure_env_file() {
    if [[ ! -f .env ]]; then
        cp .env.example .env
        log ".env created from .env.example — fill in your credentials before running."
    else
        log ".env already exists, skipping."
    fi
}

install_local() {
    command -v python3 >/dev/null 2>&1 || { err "python3 not found."; exit 1; }

    log "Creating virtualenv (.venv)..."
    python3 -m venv .venv

    log "Installing dependencies..."
    ./.venv/bin/pip install --upgrade pip >/dev/null
    ./.venv/bin/pip install -r requirements.txt

    ensure_env_file

    log "Done. Activate with: source .venv/bin/activate"
    log "Run with: python main.py --sync-history"
}

install_docker() {
    command -v docker >/dev/null 2>&1 || { err "docker not found."; exit 1; }
    docker compose version >/dev/null 2>&1 || { err "docker compose plugin not found."; exit 1; }

    ensure_env_file

    log "Building and starting containers..."
    docker compose up --build -d

    log "Assistant:  http://localhost:8000/healthz"
    log "Metrics:    http://localhost:8000/metrics"
    log "Prometheus: http://localhost:9090"
    log "Grafana:    http://localhost:3000"
}

case "$MODE" in
    local) install_local ;;
    docker) install_docker ;;
    *) err "Unknown mode: $MODE (use --docker or nothing)"; exit 1 ;;
esac
