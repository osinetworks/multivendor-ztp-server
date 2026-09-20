#!/bin/bash
# ============================================================
# start.sh - ZTP Docker Management Script
# ============================================================

set -uo pipefail

IMAGE_NAME="ztp-server"
CONTAINER_NAME="ztp-server"
COMPOSE_FILE="docker-compose.yaml"
ENV_FILE=".env"

# Always run relative to the repo root, whatever the caller's cwd is.
cd "$(dirname "$(readlink -f "$0")")" || exit 1

# The API port lives in .env (HTTP_PORT). It used to be hardcoded to 8080 in
# every curl below, so changing .env silently broke every management command.
HTTP_PORT="8080"
if [ -f "$ENV_FILE" ]; then
    # Strip an inline comment too: 'HTTP_PORT=9090   # lab' used to yield
    # '9090#lab' and every curl below then pointed at a nonsense URL.
    _port=$(grep -E '^[[:space:]]*HTTP_PORT[[:space:]]*=' "$ENV_FILE" \
            | tail -1 | cut -d= -f2- | sed 's/#.*//' | tr -d ' "'"'"'')
    if [ -n "${_port:-}" ]; then
        if [[ "$_port" =~ ^[0-9]+$ ]]; then
            HTTP_PORT="$_port"
        else
            echo "[WARN] HTTP_PORT in $ENV_FILE is not a number ('$_port') — using 8080" >&2
        fi
    fi
fi
API="http://localhost:${HTTP_PORT}"

# Config served to a switch whose own <serial>.cfg is missing.
DEFAULT_CONFIG="generic.cfg"
if [ -f "$ENV_FILE" ]; then
    _dc=$(grep -E '^[[:space:]]*DEFAULT_CONFIG[[:space:]]*=' "$ENV_FILE" \
          | tail -1 | cut -d= -f2- | sed 's/#.*//' | tr -d ' "'"'"'')
    [ -n "${_dc:-}" ] && DEFAULT_CONFIG="$_dc"
fi

# Set before check_deps refines it, so 'set -u' is safe on every path and the
# API-only commands never need a Docker daemon at all.
DOCKER_CMD="docker"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------

log()     { echo -e "${GREEN}[ZTP]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }

usage() {
    echo -e "
${BOLD}Multi-Vendor ZTP - Docker Management Script${NC}

Usage:
  ${BOLD}./start.sh${NC} [command]

Commands:
  ${GREEN}build${NC}       Build the Docker image
  ${GREEN}start${NC}       Start ZTP server (build if needed)
  ${GREEN}stop${NC}        Stop and remove the container
  ${GREEN}restart${NC}     Stop, rebuild, and start
  ${GREEN}logs${NC}        Follow container logs
  ${GREEN}status${NC}      Show container and service status
  ${GREEN}priority${NC}    Show provisioning priority status
  ${GREEN}shell${NC}       Open a shell inside the container
  ${GREEN}reload${NC}      Hot-reload inventory without restart
  ${GREEN}reset${NC}       Forget ZTP completion state (all, or one SERIAL)
  ${GREEN}switches${NC}    List all switches in inventory
  ${GREEN}events${NC}      Show recent ZTP events
  ${GREEN}watch${NC}       Live progress dashboard (auto-refresh)
  ${GREEN}clean${NC}       Remove container and image
  ${GREEN}help${NC}        Show this help message

Examples:
  ./start.sh start
  ./start.sh logs
  ./start.sh priority
  ./start.sh events
  ./start.sh reset HBG25500VQC
"
    exit 0
}

# -----------------------------------------------------------
# Prerequisite checks
# -----------------------------------------------------------

check_deps() {
    local missing=0
    if ! command -v docker &>/dev/null; then
        error "docker not found — please install Docker"
        exit 1
    fi

    # Every other helper script in this repo calls 'sudo docker'. This one
    # called plain 'docker', so on a host where the user is not in the docker
    # group './start.sh start' failed while ./docker-compose-start.sh worked.
    DOCKER_CMD="docker"
    if ! docker info &>/dev/null; then
        if command -v sudo &>/dev/null; then
            warn "Docker daemon not reachable as $(id -un) — using 'sudo docker'."
            warn "To avoid sudo: sudo usermod -aG docker $(id -un) && newgrp docker"
            DOCKER_CMD="sudo docker"
            if ! $DOCKER_CMD info &>/dev/null; then
                error "Cannot talk to the Docker daemon, even with sudo. Is it running?"
                exit 1
            fi
        else
            error "Cannot talk to the Docker daemon and sudo is not available."
            exit 1
        fi
    fi

    if $DOCKER_CMD compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="$DOCKER_CMD compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
        [ "$DOCKER_CMD" = "sudo docker" ] && COMPOSE_CMD="sudo docker-compose"
    else
        error "Docker Compose not found (install Docker Compose v2 or docker-compose v1)"
        missing=1
    fi

    if [ "$missing" -eq 1 ]; then
        exit 1
    fi
}

check_env() {
    # .env is what every network-specific value comes from. Without it compose
    # falls back to the built-in defaults in docker-compose.yaml, which are
    # almost certainly not this network.
    if [ ! -f "$ENV_FILE" ]; then
        warn "$ENV_FILE not found — using the built-in defaults from docker-compose.yaml."
        warn "  Create it with: cp .env.example .env   (then set SERVER_IP / INTERFACE)"
    fi

    if [ ! -f "config/inventory.yaml" ]; then
        warn "config/inventory.yaml not found — switches will use defaults only"
        warn "  Create it with: cp config/inventory.yaml.template config/inventory.yaml"
    fi

    # Only the host-side config generator reads config.yaml, but a missing one
    # is the single most common reason './generate_generic_config.py' fails.
    if [ ! -f "config/config.yaml" ]; then
        warn "config/config.yaml not found — generate_generic_config.py cannot run"
        warn "  Create it with: cp config/config.yaml.template config/config.yaml"
    fi

    if [ ! -f "config/dnsmasq.conf.template" ]; then
        warn "config/dnsmasq.conf.template not found — DHCP will not work"
    fi

    local fw_count=0
    fw_count=$(find firmware/ \( -name "*.swi" -o -name "*.bin" \) 2>/dev/null | wc -l) || fw_count=0
    if [ "$fw_count" -eq 0 ]; then
        warn "No firmware files found in firmware/ — switches cannot download firmware"
    else
        log "Found $fw_count firmware image(s) in firmware/"
    fi

    local cfg_count=0
    cfg_count=$(find configs/ -name "*.cfg" 2>/dev/null | wc -l) || cfg_count=0
    log "Found $cfg_count config file(s) in configs/"

    # The fallback is what an unregistered switch gets. Without it such a
    # switch 404s on both its own .cfg and the fallback, and aborts ZTP.
    if [ ! -f "configs/${DEFAULT_CONFIG}" ]; then
        warn "configs/${DEFAULT_CONFIG} missing — a switch that is not in the inventory"
        warn "  will fail ZTP with 'failed_config'. Generate it with:"
        warn "    ./generate_generic_config.py"
    fi
}

# -----------------------------------------------------------
# Commands
# -----------------------------------------------------------

cmd_build() {
    section "Building Docker Image"
    $COMPOSE_CMD -f "$COMPOSE_FILE" build --no-cache
    log "Build complete: ${IMAGE_NAME}:latest"
}

cmd_start() {
    section "Starting ZTP Server"
    check_env

    mkdir -p logs configs firmware

    # Build image if it doesn't exist
    if ! $DOCKER_CMD image inspect "${IMAGE_NAME}:latest" &>/dev/null; then
        log "Image '${IMAGE_NAME}:latest' not found — building first..."
        cmd_build
    else
        log "Image '${IMAGE_NAME}:latest' found — skipping build (use './start.sh restart' to rebuild)"
    fi

    $COMPOSE_CMD -f "$COMPOSE_FILE" up -d
    if [ $? -ne 0 ]; then
        error "Failed to start container — run './start.sh logs' for details"
        exit 1
    fi
    log "Container started: $CONTAINER_NAME"

    # Wait for health check
    echo -ne "${CYAN}[ZTP]${NC} Waiting for server to be ready"
    local ready=0
    for i in $(seq 1 15); do
        sleep 2
        if curl -sf "${API}/health" &>/dev/null; then
            ready=1
            break
        fi
        echo -n "."
    done
    echo ""

    if [ "$ready" -eq 1 ]; then
        log "ZTP server is healthy ✓"
        cmd_status
    else
        warn "Server did not respond on :${HTTP_PORT} — check logs: ./start.sh logs"
    fi
}

cmd_stop() {
    section "Stopping ZTP Server"
    # --remove-orphans cleans up containers from old compose projects
    # (e.g. if container_name changed from arista-ztp to ztp-server)
    $COMPOSE_CMD -f "$COMPOSE_FILE" down --remove-orphans

    # Also force-remove any leftover container with the old name
    if $DOCKER_CMD ps -a --format '{{.Names}}' | grep -q "^arista-ztp$"; then
        warn "Removing leftover container 'arista-ztp' from previous version..."
        $DOCKER_CMD rm -f arista-ztp 2>/dev/null || true
    fi

    log "Container stopped."
}

cmd_restart() {
    section "Restarting ZTP Server"
    cmd_stop
    log "Force-rebuilding image with --no-cache to pick up code changes..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" build --no-cache
    log "Build complete."
    cmd_start
}

cmd_logs() {
    section "Container Logs (Ctrl+C to exit)"
    $COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail=100
}

cmd_status() {
    section "ZTP Server Status"

    if $DOCKER_CMD ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        local started
        started=$($DOCKER_CMD inspect --format '{{.State.StartedAt}}' "$CONTAINER_NAME" 2>/dev/null || echo "unknown")
        echo -e "  Container : ${GREEN}RUNNING${NC} (started $started)"
    else
        echo -e "  Container : ${RED}NOT RUNNING${NC}"
        return
    fi

    if curl -sf "${API}/health" &>/dev/null; then
        local health sw_count events_count completed failed
        health=$(curl -s "${API}/health")
        sw_count=$(echo "$health"     | python3 -c "import sys,json; print(json.load(sys.stdin).get('switches','?'))" 2>/dev/null || echo "?")
        events_count=$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('events_recorded','?'))" 2>/dev/null || echo "?")
        completed=$(echo "$health"    | python3 -c "import sys,json; c=json.load(sys.stdin).get('completed',[]); print(', '.join(c) if c else 'none')" 2>/dev/null || echo "?")
        failed=$(echo "$health"       | python3 -c "import sys,json; c=json.load(sys.stdin).get('failed',[]); print(', '.join(c) if c else 'none')" 2>/dev/null || echo "?")
        echo -e "  HTTP API  : ${GREEN}OK${NC} — $sw_count switch(es) in inventory, $events_count event(s) recorded"
        echo -e "  Completed : $completed"
        if [ "$failed" = "none" ]; then
            echo -e "  Failed    : ${GREEN}none${NC}"
        else
            echo -e "  Failed    : ${RED}$failed${NC}"
        fi
    else
        echo -e "  HTTP API  : ${RED}NOT RESPONDING${NC} on :${HTTP_PORT}"
    fi

    local fw_count=0 cfg_count=0
    fw_count=$(find firmware/ \( -name "*.swi" -o -name "*.bin" \) 2>/dev/null | wc -l) || true
    cfg_count=$(find configs/ -name "*.cfg" 2>/dev/null | wc -l) || true

    echo -e "  Firmware  : $fw_count image(s) available"
    echo -e "  Configs   : $cfg_count .cfg file(s) available"
    echo ""
    echo -e "  ${CYAN}Endpoints:${NC}"
    echo -e "    Bootstrap (Arista) : ${API}/bootstrap/arista"
    echo -e "    Bootstrap (Cisco)  : ${API}/bootstrap/cisco"
    echo -e "    Health             : ${API}/health"
    echo -e "    Priority status    : ${API}/api/status"
    echo -e "    Switches           : ${API}/api/switches"
    echo -e "    Events             : ${API}/api/events"
}

cmd_priority() {
    section "Provisioning Priority Status"
    local data
    data=$(curl -sf "${API}/api/status" 2>/dev/null) || true
    if [ -z "$data" ]; then
        error "Server not reachable — is the container running?"
        exit 1
    fi

    echo "$data" | python3 -c "
import sys, json
data = json.load(sys.stdin)
failed = data.get('failed_count', 0)
print(f\"Completed: {data.get('completed_count',0)}/{data.get('total_switches',0)}\"
      + (f'   Failed: {failed}' if failed else ''))
print()
for group in data.get('provisioning_order', []):
    done = '✓' if group.get('all_complete') else '...'
    print(f\"  [{done}] {group.get('label','')}\")
    for sw in group.get('switches', []):
        if sw.get('failed'):
            status = f\"✗ FAILED ({sw.get('failure')})\"
        elif sw.get('completed'):
            status = '✓ done'
        elif sw.get('clear_to_go'):
            status = '▶ go'
        else:
            status = f\"⏳ {sw.get('waiting_for')}\"
        print(f\"       {sw.get('serial',''):20} {sw.get('description',''):18} {sw.get('vendor',''):8} {status}\")
    print()
"
}

cmd_shell() {
    section "Opening Shell in Container"
    $DOCKER_CMD exec -it "$CONTAINER_NAME" /bin/bash 2>/dev/null || $DOCKER_CMD exec -it "$CONTAINER_NAME" /bin/sh
}

cmd_reload() {
    section "Hot-Reloading Inventory"
    local result
    result=$(curl -sf -X POST "${API}/api/inventory/reload" 2>/dev/null) || true
    if [ -n "$result" ]; then
        log "Inventory reloaded: $result"
    else
        error "Server not reachable on :${HTTP_PORT} — is the container running?"
        exit 1
    fi
}

cmd_reset() {
    section "Resetting ZTP Completion State"
    # Completion is persisted to logs/ztp_state.json so a container restart does
    # not re-run a finished rollout. Re-provisioning the same switch (RMA, lab
    # re-run) therefore needed that file deleted by hand — this asks the server
    # to forget instead, with no restart and no knowledge of where it lives.
    local serial="${1:-}"
    local payload="{}"
    if [ -n "$serial" ]; then
        payload="{\"serial\": \"${serial}\"}"
        log "Clearing state for ${serial}..."
    else
        warn "This clears completion state for ALL switches — they will provision again."
        read -rp "Are you sure? [y/N] " confirm
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            log "Aborted."
            return
        fi
    fi

    local result
    result=$(curl -sf -X POST -H "Content-Type: application/json" \
                  -d "$payload" "${API}/api/state/reset" 2>/dev/null) || true
    if [ -n "$result" ]; then
        log "State reset: $result"
    else
        error "Server not reachable on :${HTTP_PORT} — is the container running?"
        exit 1
    fi
}

cmd_switches() {
    section "Registered Switches"
    local data
    data=$(curl -sf "${API}/api/switches" 2>/dev/null) || true
    if [ -z "$data" ]; then
        error "Server not reachable — is the container running?"
        exit 1
    fi

    if [ "$data" = "[]" ]; then
        warn "No switches registered in inventory."
        return
    fi

    printf "%-22s %-15s %-10s %-8s %-25s %-30s\n" "SERIAL" "DESCRIPTION" "PRIORITY" "VENDOR" "CONFIG" "FIRMWARE"
    printf '%0.s─' {1..115}; echo ""
    echo "$data" | python3 -c "
import sys, json
for sw in json.load(sys.stdin):
    vendor = 'cisco' if sw.get('platform') == 'cisco_ios' else 'arista'
    print('{:<22} {:<15} {:<10} {:<8} {:<25} {}'.format(
        sw.get('serial',''), sw.get('description','')[:13],
        sw.get('priority', 99), vendor,
        sw.get('serial','') + '.cfg', sw.get('firmware',''),
    ))
"
}

cmd_events() {
    section "Recent ZTP Events"
    local data
    data=$(curl -sf "${API}/api/events" 2>/dev/null) || true
    if [ -z "$data" ]; then
        error "Server not reachable — is the container running?"
        exit 1
    fi

    if [ "$data" = "[]" ]; then
        warn "No ZTP events recorded yet."
        return
    fi

    echo "$data" | python3 -c "
import sys, json
for e in json.load(sys.stdin)[-30:]:
    print('[{}] {:20} {:25} {}'.format(
        e.get('timestamp',''), e.get('serial',''),
        e.get('event',''), str(e.get('detail',''))[:50],
    ))
"
}

cmd_watch() {
    section "Live ZTP Progress Dashboard"
    echo -e "  Refreshing every 5s — ${YELLOW}Ctrl+C to exit${NC}
"

    while true; do
        # Move cursor to top, clear screen
        clear
        echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════════${NC}"
        echo -e "${BOLD}  Multi-Vendor ZTP — Live Progress$(date +'  %H:%M:%S')${NC}"
        echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════════${NC}"

        local data
        data=$(curl -sf "${API}/api/progress" 2>/dev/null) || true

        if [ -z "$data" ]; then
            echo -e "  ${RED}Server not reachable on :${HTTP_PORT}${NC}"
        else
            echo "$data" | python3 -c "
import sys, json

RESET  = '[0m'
BOLD   = '[1m'
GREEN  = '[0;32m'
YELLOW = '[1;33m'
RED    = '[0;31m'
CYAN   = '[0;36m'
GRAY   = '[0;37m'

BAR_WIDTH = 30

def bar(pct, width=BAR_WIDTH):
    filled = int(width * pct / 100)
    return GREEN + '█' * filled + GRAY + '░' * (width - filled) + RESET

def fmt_bytes(b):
    if b >= 1024*1024*1024:
        return f'{b/1024/1024/1024:.1f}GB'
    elif b >= 1024*1024:
        return f'{b/1024/1024:.1f}MB'
    elif b >= 1024:
        return f'{b/1024:.1f}KB'
    return f'{b}B'

switches = json.load(sys.stdin)
total  = len(switches)
done   = sum(1 for s in switches if s.get('completed'))
failed = sum(1 for s in switches if s.get('failed'))
line = f'  Switches: {GREEN}{done}{RESET}/{total} complete'
if failed:
    line += f'   {RED}{failed} failed{RESET}'
print(line)
print()

last_priority = None
for sw in switches:
    priority = sw.get('priority', 99)
    if priority != last_priority:
        print(f'  {CYAN}Priority {priority}{RESET}')
        last_priority = priority

    serial  = sw.get('serial','')
    desc    = sw.get('description','')
    vendor  = sw.get('vendor','')
    pct     = sw.get('pct', 0)
    msg     = sw.get('msg', '')
    step    = sw.get('step', '')
    brecv   = sw.get('bytes_received', 0)
    btotal  = sw.get('bytes_total', 0)
    compl   = sw.get('completed', False)

    # Status icon — a switch that reported ztp_complete with a failure detail
    # is flagged 'failed', and must not be painted as a green success.
    if sw.get('failed') or step == 'failed':
        icon = RED + '✗' + RESET
    elif compl:
        icon = GREEN + '✓' + RESET
    elif step == 'not_started':
        icon = GRAY + '○' + RESET
    else:
        icon = YELLOW + '▶' + RESET

    vcolor = CYAN if vendor == 'arista' else YELLOW
    print(f'  {icon} {BOLD}{serial}{RESET}  {vcolor}{vendor:8}{RESET} {desc}')

    if step not in ('not_started', 'done', 'failed', ''):
        # Progress bar (only meaningful during download)
        if btotal > 0:
            print(f'      [{bar(pct)}] {pct:3d}%  {fmt_bytes(brecv)}/{fmt_bytes(btotal)}')
        else:
            print(f'      [{bar(pct)}] {pct:3d}%')

    # Status message
    if sw.get('failed') or step == 'failed' or 'FAIL' in msg.upper() or 'ERROR' in msg.upper():
        msg_color = RED
    elif compl:
        msg_color = GREEN
    else:
        msg_color = GRAY
    print(f'      {msg_color}{msg}{RESET}')
    print()
"
        fi

        sleep 5
    done
}

cmd_clean() {
    section "Cleaning Up"
    warn "This will remove the container and Docker image."
    read -rp "Are you sure? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        $COMPOSE_CMD -f "$COMPOSE_FILE" down --rmi local --volumes --remove-orphans 2>/dev/null || true
        $DOCKER_CMD rm -f arista-ztp 2>/dev/null || true
        $DOCKER_CMD rmi "${IMAGE_NAME}:latest" 2>/dev/null || true
        log "Cleanup complete."
    else
        log "Aborted."
    fi
}

# -----------------------------------------------------------
# Entry point
# -----------------------------------------------------------

COMMAND="${1:-help}"

# Usage and unknown-command errors must work on a machine with no Docker daemon
# running — check_deps probes 'docker info' and exits when it cannot reach it.
case "$COMMAND" in
    help|--help|-h)
        usage
        ;;
    build|start|stop|restart|logs|status|shell|clean)
        check_deps                      # these actually drive Docker
        ;;
    priority|reload|reset|switches|events|watch)
        : ;;                            # these only talk to the HTTP API
    *)
        error "Unknown command: $COMMAND"
        usage
        ;;
esac

case "$COMMAND" in
    build)    cmd_build    ;;
    start)    cmd_start    ;;
    stop)     cmd_stop     ;;
    restart)  cmd_restart  ;;
    logs)     cmd_logs     ;;
    status)   cmd_status   ;;
    priority) cmd_priority ;;
    shell)    cmd_shell    ;;
    reload)   cmd_reload   ;;
    reset)    cmd_reset "${2:-}" ;;
    switches) cmd_switches ;;
    events)   cmd_events   ;;
    watch)    cmd_watch    ;;
    clean)    cmd_clean    ;;
esac