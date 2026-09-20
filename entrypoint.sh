#!/bin/bash
# ============================================================
# entrypoint.sh - ZTP Server container startup
# ============================================================

set -e

ZTP_BASE_DIR="${ZTP_BASE_DIR:-/var/www/ztp}"
LOG_DIR="${LOG_DIR:-/var/www/ztp/logs}"
INVENTORY_PATH="${INVENTORY_PATH:-/var/www/ztp/config/inventory.yaml}"
ZTP_PORT="${ZTP_PORT:-8080}"
PRIORITY_WAIT_TIMEOUT="${PRIORITY_WAIT_TIMEOUT:-1800}"
DEFAULT_CONFIG="${DEFAULT_CONFIG:-generic.cfg}"
export DEFAULT_CONFIG

# 1 worker + threads: all threads share the same memory so completed_serials
# and ztp_events are consistent across all requests. Multiple workers would
# create isolated processes that cannot see each other's state, causing
# priority gating to deadlock (ztp_complete hits worker A, manifest check
# hits worker B which has empty state and waits forever).
ZTP_WORKERS="${ZTP_WORKERS:-1}"
ZTP_THREADS="${ZTP_THREADS:-16}"

# Standard gunicorn timeout — manifest endpoint now returns instantly (202)
# instead of blocking, so no long timeout is needed.
GUNICORN_TIMEOUT=60

echo "=========================================="
echo " Multi-Vendor ZTP Server"
echo "=========================================="
echo " Base dir         : $ZTP_BASE_DIR"
echo " Inventory        : $INVENTORY_PATH"
echo " Log dir          : $LOG_DIR"
echo " Port             : $ZTP_PORT"
echo " Workers          : $ZTP_WORKERS (1 worker + $ZTP_THREADS threads)"
echo " Gunicorn timeout : ${GUNICORN_TIMEOUT}s (manifest polls from bootstrap, no long holds)"
echo " Priority timeout : ${PRIORITY_WAIT_TIMEOUT}s (then released anyway)"
echo " Fallback config  : $DEFAULT_CONFIG"
echo "=========================================="

# Ensure required directories exist
mkdir -p "$ZTP_BASE_DIR/configs"
mkdir -p "$ZTP_BASE_DIR/firmware"
mkdir -p "$ZTP_BASE_DIR/logs"
mkdir -p "$ZTP_BASE_DIR/config"
mkdir -p /var/lib/dnsmasq

# Copy default inventory if not present
if [ ! -f "$INVENTORY_PATH" ]; then
    if [ -f "/etc/ztp/inventory.yaml" ]; then
        cp /etc/ztp/inventory.yaml "$INVENTORY_PATH"
        echo "[entrypoint] Copied default inventory to $INVENTORY_PATH"
    else
        echo "[entrypoint] WARNING: No inventory file found at $INVENTORY_PATH"
    fi
fi

# -----------------------------------------------------------
# Auto-detect network interface for dnsmasq
# -----------------------------------------------------------
if [ -n "${ZTP_INTERFACE:-}" ]; then
    IFACE="$ZTP_INTERFACE"
    echo "[entrypoint] Using interface from ZTP_INTERFACE env: $IFACE"
else
    IFACE=$(ip -o link show | awk -F': ' '$2 != "lo" && $3 ~ /UP/ {print $2; exit}')
    if [ -z "$IFACE" ]; then
        IFACE=$(ip -o link show | awk -F': ' '$2 != "lo" {print $2; exit}')
    fi
    echo "[entrypoint] Auto-detected interface: $IFACE"
fi

if [ -z "$IFACE" ]; then
    echo "[entrypoint] ERROR: Could not detect a network interface. Set ZTP_INTERFACE env var."
    exit 1
fi

# -----------------------------------------------------------
# Render dnsmasq config from template
# Substitutes placeholders with values from environment (.env)
# -----------------------------------------------------------
# The template now travels with the mounted config directory (docker-compose
# mounts ./config, not individual files), with the image's baked-in copy as the
# fallback for running the image without that mount.
DNSMASQ_TEMPLATE="$ZTP_BASE_DIR/config/dnsmasq.conf.template"
if [ ! -f "$DNSMASQ_TEMPLATE" ]; then
    DNSMASQ_TEMPLATE="/etc/dnsmasq.conf.template"
fi
DNSMASQ_RENDERED="/etc/dnsmasq.conf"        # natural dnsmasq config path
DNSMASQ_IFACE_CONF="/tmp/dnsmasq-interface.conf"

if [ ! -f "$DNSMASQ_TEMPLATE" ]; then
    echo "[entrypoint] ERROR: no dnsmasq template found at"
    echo "[entrypoint]        $ZTP_BASE_DIR/config/dnsmasq.conf.template or /etc/dnsmasq.conf.template"
    exit 1
fi
echo "[entrypoint] dnsmasq template: $DNSMASQ_TEMPLATE"

# Validate required env vars. docker-compose supplies a default for every one
# of these (see docker-compose.yaml), and anything still missing falls back to
# the built-in default below — so this only reports what the operator should
# set in .env to match their own network.
MISSING=""
for VAR in SERVER_IP HTTP_PORT DHCP_RANGE_START DHCP_RANGE_END DHCP_SUBNET DHCP_LEASE DNS_SERVER; do
    VAL="${!VAR:-}"
    if [ -z "$VAL" ]; then
        # Map docker-compose env names to .env names
        case "$VAR" in
            SERVER_IP)       VAL="${ZTP_SERVER_IP:-}" ;;
            HTTP_PORT)       VAL="${ZTP_PORT:-}" ;;
        esac
    fi
    if [ -z "$VAL" ]; then
        MISSING="$MISSING $VAR"
    fi
done

if [ -n "$MISSING" ]; then
    echo "[entrypoint] WARNING: not set in .env, using built-in defaults:${MISSING}"
fi

# Use ZTP_ prefixed names (passed by docker-compose from .env)
_SERVER_IP="${ZTP_SERVER_IP:-169.254.254.1}"
_HTTP_PORT="${ZTP_PORT:-8080}"
_DHCP_START="${DHCP_RANGE_START:-169.254.254.100}"
_DHCP_END="${DHCP_RANGE_END:-169.254.254.200}"
_DHCP_SUBNET="${DHCP_SUBNET:-255.255.255.0}"
_DHCP_LEASE="${DHCP_LEASE:-1h}"
_DNS="${DNS_SERVER:-1.1.1.1,8.8.8.8}"

echo "[entrypoint] Rendering dnsmasq config from template..."
echo "[entrypoint]   SERVER_IP   : $_SERVER_IP"
echo "[entrypoint]   HTTP_PORT   : $_HTTP_PORT"
echo "[entrypoint]   DHCP range  : $_DHCP_START - $_DHCP_END / $_DHCP_SUBNET"
echo "[entrypoint]   DHCP lease  : $_DHCP_LEASE"
echo "[entrypoint]   DNS servers : $_DNS"

sed     -e "s|__SERVER_IP__|${_SERVER_IP}|g"     -e "s|__HTTP_PORT__|${_HTTP_PORT}|g"     -e "s|__DHCP_RANGE_START__|${_DHCP_START}|g"     -e "s|__DHCP_RANGE_END__|${_DHCP_END}|g"     -e "s|__DHCP_SUBNET__|${_DHCP_SUBNET}|g"     -e "s|__DHCP_LEASE__|${_DHCP_LEASE}|g"     -e "s|__DNS_SERVER__|${_DNS}|g"     "$DNSMASQ_TEMPLATE" > "$DNSMASQ_RENDERED"

echo "[entrypoint] dnsmasq config rendered → $DNSMASQ_RENDERED (natural path)"

# Write interface override
echo "interface=${IFACE}" > "$DNSMASQ_IFACE_CONF"
echo "[entrypoint] dnsmasq interface set to: $IFACE"

# -----------------------------------------------------------
# Start dnsmasq
# -----------------------------------------------------------
echo "[entrypoint] Starting dnsmasq..."
# Load main rendered config (dhcp-range, option 67, etc.) first,
# then the interface override. Using --conf-file replaces the default
# /etc/dnsmasq.conf path, so we must specify both explicitly.
dnsmasq \
    --no-daemon \
    --log-dhcp \
    --log-queries \
    --log-facility=- \
    --conf-file="$DNSMASQ_RENDERED" \
    --conf-file="$DNSMASQ_IFACE_CONF" &
DNSMASQ_PID=$!

# -----------------------------------------------------------
# Start gunicorn (production WSGI server)
# -----------------------------------------------------------
echo "[entrypoint] Starting ZTP server via gunicorn..."
export PYTHONPATH="/usr/local/bin:${PYTHONPATH:-}"

gunicorn \
    --workers "$ZTP_WORKERS" \
    --threads "$ZTP_THREADS" \
    --bind "0.0.0.0:${ZTP_PORT}" \
    --chdir /usr/local/bin \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    --timeout "$GUNICORN_TIMEOUT" \
    ztp_server:app &
GUNICORN_PID=$!

echo "[entrypoint] Services started (dnsmasq PID=$DNSMASQ_PID, gunicorn PID=$GUNICORN_PID)"

# Graceful shutdown on SIGTERM/SIGINT
trap "echo '[entrypoint] Shutting down...'; kill $DNSMASQ_PID $GUNICORN_PID 2>/dev/null; exit 0" SIGTERM SIGINT

# Wait for either process to exit
wait -n 2>/dev/null || wait
echo "[entrypoint] A service exited, shutting down."
kill $DNSMASQ_PID $GUNICORN_PID 2>/dev/null
exit 1