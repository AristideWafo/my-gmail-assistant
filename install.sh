#!/usr/bin/env bash
# Installs and bootstraps my-gmail-assistant.
# Usage: ./install.sh [--docker | --release]
#   (no flag)  local venv install
#   --docker   build from this checkout & start via docker compose
#   --release  pull the published image (VERSION in .env) & start via docker compose
# With GRAFANA_HOSTNAME set in .env, both Docker modes also publish Grafana through the Traefik
# already running on the host (docker-compose.traefik.yml).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

MODE="${1:-local}"

log() { printf '\033[1;32m[install]\033[0m %s\n' "$1"; }
err() { printf '\033[1;31m[install]\033[0m %s\n' "$1" >&2; }

# Prints "created" when it had to write the file: nothing can start on the example's placeholders.
ensure_env_file() {
    if [[ ! -f .env ]]; then
        cp .env.example .env
        echo created
    fi
}

# .env is read, not sourced: its values are not shell and may hold any character.
env_value() {
    sed -n "s/^$1=//p" .env | tail -n 1 \
        | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

install_local() {
    command -v python3 >/dev/null 2>&1 || { err "python3 not found."; exit 1; }

    log "Creating virtualenv (.venv)..."
    python3 -m venv .venv

    log "Installing dependencies..."
    ./.venv/bin/pip install --upgrade pip >/dev/null
    ./.venv/bin/pip install -r requirements.txt

    if [[ -n "$(ensure_env_file)" ]]; then
        log ".env created from .env.example — fill in your credentials before running."
    else
        log ".env already exists, skipping."
    fi

    log "Done. Activate with: source .venv/bin/activate"
    log "Run with: python main.py --sync-history"
}

check_traefik() {
    local network="$1" password
    password="$(env_value GRAFANA_ADMIN_PASSWORD)"
    case "$password" in
        "" | admin | change_me)
            err "GRAFANA_HOSTNAME is set: Grafana will be reachable from the internet."
            err "Set a real GRAFANA_ADMIN_PASSWORD in .env first."
            exit 1
            ;;
    esac
    if ! docker network inspect "$network" >/dev/null 2>&1; then
        err "Docker network '$network' not found: Traefik is not installed on this host, or it"
        err "uses another network (TRAEFIK_NETWORK in .env). This project does not start Traefik."
        exit 1
    fi
}

# $1: "build" to build the image from this checkout, "pull" to run the published one.
install_docker() {
    local source="$1" hostname network
    local files=(-f docker-compose.yml)

    command -v docker >/dev/null 2>&1 || { err "docker not found."; exit 1; }
    docker compose version >/dev/null 2>&1 || { err "docker compose plugin not found."; exit 1; }

    if [[ -n "$(ensure_env_file)" ]]; then
        log ".env created from .env.example."
        err "Fill in your credentials in .env, then run this again. Nothing was started."
        exit 1
    fi

    [[ "$source" == "build" ]] && files+=(-f docker-compose.build.yml)

    hostname="$(env_value GRAFANA_HOSTNAME)"
    if [[ -n "$hostname" ]]; then
        network="$(env_value TRAEFIK_NETWORK)"
        check_traefik "${network:-proxy}"
        files+=(-f docker-compose.traefik.yml)
    fi

    if [[ "$source" == "build" ]]; then
        log "Building and starting containers..."
        docker compose "${files[@]}" up --build -d
    else
        log "Pulling images and starting containers..."
        docker compose "${files[@]}" pull
        docker compose "${files[@]}" up -d
    fi

    log "Assistant:  http://localhost:8000/healthz"
    log "Metrics:    http://localhost:8000/metrics"
    log "Prometheus: http://localhost:9090"
    log "Grafana:    http://localhost:3000"
    if [[ -n "$hostname" ]]; then
        log "Grafana:    https://$hostname (through Traefik)"
        # A plain "docker compose up -d" later would otherwise drop the Traefik labels.
        [[ -n "$(env_value COMPOSE_FILE)" ]] \
            || log "Add COMPOSE_FILE=docker-compose.yml:docker-compose.traefik.yml to .env so that later 'docker compose' commands keep Grafana published."
    fi
}

case "$MODE" in
    local) install_local ;;
    --docker) install_docker build ;;
    --release) install_docker pull ;;
    *) err "Unknown mode: $MODE (use --docker, --release or nothing)"; exit 1 ;;
esac
