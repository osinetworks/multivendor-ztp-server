#!/usr/bin/env python3
# ============================================================
# ztp_server.py
# Flask-based ZTP HTTP server.
# Serves bootstrap, configs, firmware.
# Provides REST API for manifest lookup, notifications, mgmt.
# Supports priority-based provisioning order.
# ============================================================

import os
import re
import json
import logging
import datetime
import threading
from collections import deque
from flask import Flask, request, jsonify, send_from_directory, abort

from inventory_manager import InventoryManager, InventoryPersistError, display_name

# -----------------------------------------------------------
# Configuration from environment
# -----------------------------------------------------------
# docker-compose expands '${HTTP_PORT}' even when .env does not define it, so
# the variable reaches us as an EMPTY STRING and os.environ.get's default never
# applies. int("") then killed the server at import time and the container
# restart-looped with no useful message. Treat empty exactly like unset.
_ENV_WARNINGS: list = []


def _env_str(name: str, default: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        if name in os.environ:
            _ENV_WARNINGS.append(f"{name} is empty — falling back to {default!r}")
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        if name in os.environ:
            _ENV_WARNINGS.append(f"{name} is empty — falling back to {default}")
        return default
    try:
        return int(raw)
    except ValueError:
        _ENV_WARNINGS.append(f"{name}={raw!r} is not an integer — falling back to {default}")
        return default


HOST            = _env_str("ZTP_HOST",         "0.0.0.0")
PORT            = _env_int("ZTP_PORT",         8080)
SERVER_IP       = _env_str("ZTP_SERVER_IP",    "169.254.254.1")
BASE_DIR        = _env_str("ZTP_BASE_DIR",     "/var/www/ztp")
INVENTORY_PATH  = _env_str("INVENTORY_PATH",   "/var/www/ztp/config/inventory.yaml")
LOG_LEVEL       = _env_str("LOG_LEVEL",        "INFO")
LOG_DIR         = _env_str("LOG_DIR",          "/var/www/ztp/logs")
DEFAULT_CONFIG  = _env_str("DEFAULT_CONFIG",   "generic.cfg")

# Max seconds a switch is held back waiting for lower-priority switches before
# the manifest is released anyway. Without this a single switch that never
# boots (spare / RMA / still in its box) blocks every higher priority forever.
PRIORITY_WAIT_TIMEOUT = _env_int("PRIORITY_WAIT_TIMEOUT", 1800)  # 30 min
# How often the bootstrap script re-polls while waiting
PRIORITY_POLL_INTERVAL = 15  # seconds

# Keep the in-memory event log bounded — the container runs for weeks.
MAX_EVENTS = _env_int("ZTP_MAX_EVENTS", 2000)

# -----------------------------------------------------------
# Logging setup
# -----------------------------------------------------------
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "ztp_server.log")),
    ]
)
logger = logging.getLogger("ztp.server")

for _warning in _ENV_WARNINGS:
    logger.warning(f"[env] {_warning}")

# -----------------------------------------------------------
# Flask app
# -----------------------------------------------------------
app = Flask(__name__)
inventory = InventoryManager(INVENTORY_PATH)

# Guards every mutation of the state dicts below. gunicorn runs 1 worker with
# many threads (see entrypoint.sh), so all requests share this process memory.
state_lock = threading.RLock()

# In-memory ZTP event log (bounded — see MAX_EVENTS)
ztp_events = deque(maxlen=MAX_EVENTS)

# Priority state tracker: serial → True if ztp_complete received (success OR failure;
# a failed switch must not block the switches queued behind it).
completed_serials: dict = {}

# Serials whose ztp_complete carried a failure detail: serial → reason
failed_serials: dict = {}

# Progress tracker: serial → progress dict
progress_state: dict = {}

# First time each serial was told to wait, used to enforce PRIORITY_WAIT_TIMEOUT
first_wait_seen: dict = {}

# Survives container restarts so a rollout in progress is not re-run from zero
STATE_FILE = os.path.join(LOG_DIR, "ztp_state.json")

# A serial is used to build file paths and URLs — allow only what a real
# switch serial can contain, so it can never escape LOG_DIR.
SERIAL_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")


def ts():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def clean_serial(raw: str) -> str:
    """
    Normalise and validate a serial coming from an untrusted source
    (URL path, query string or JSON body). Returns 'UNKNOWN' for anything
    that is not a plausible serial, so it can never be used for path
    traversal when building '<LOG_DIR>/<serial>.log'.
    """
    serial = str(raw or "").strip().upper()
    if not SERIAL_RE.match(serial):
        if serial:
            logger.warning(f"Rejected malformed serial: {raw!r}")
        return "UNKNOWN"
    return serial


def device_log_path(serial: str) -> str:
    """Path of the per-device log file. Serial is already validated."""
    return os.path.join(LOG_DIR, f"{clean_serial(serial)}.log")


def save_state():
    """Persist completion state so a container restart does not deadlock ZTP."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "completed_serials": completed_serials,
                "failed_serials":    failed_serials,
                "saved":             ts(),
            }, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"Could not persist ZTP state to {STATE_FILE}: {e}")


def load_state():
    """Restore completion state written by a previous run of this container."""
    if not os.path.isfile(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        completed_serials.update(data.get("completed_serials", {}))
        failed_serials.update(data.get("failed_serials", {}))
        logger.info(
            f"Restored ZTP state from {STATE_FILE}: "
            f"{len(completed_serials)} completed, {len(failed_serials)} failed"
        )
    except Exception as e:
        logger.error(f"Could not restore ZTP state from {STATE_FILE}: {e}")


def record_event(entry: dict):
    """Append an event to the in-memory log and the per-device log file."""
    with state_lock:
        ztp_events.append(entry)
    try:
        with open(device_log_path(entry.get("serial", "UNKNOWN")), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error(f"Could not write device log for {entry.get('serial')}: {e}")


def is_priority_clear(serial: str, arm_timeout: bool = False) -> tuple[bool, str]:
    """
    Check if all switches with LOWER priority number than this serial
    have reported ztp_complete.
    Returns (True, "") if clear to proceed, or (False, reason) if waiting.

    A switch is never held longer than PRIORITY_WAIT_TIMEOUT seconds: if the
    switches it waits for never report in, the manifest is released anyway
    rather than blocking the rest of the rollout indefinitely.

    arm_timeout:
        True  — only /api/manifest, where a real switch is actually polling.
                Starts (and may expire) that switch's timeout clock.
        False — read-only status / dashboard endpoints. They must never start
                the clock, otherwise simply leaving './start.sh watch' open
                would time out switches that have not even booted yet.
    """
    serial = clean_serial(serial)
    my_priority = inventory.get_priority(serial)

    # Collect all priorities lower than mine
    blocking_priorities = [p for p in inventory.get_all_priorities() if p < my_priority]

    if not blocking_priorities:
        if arm_timeout:
            with state_lock:
                first_wait_seen.pop(serial, None)
        return True, ""  # no lower priority switches — proceed immediately

    # Find the first lower-priority switch that has not reported in.
    # completed_serials is mutated by notification threads, so read it under
    # the same lock those writers take.
    blocker = None
    with state_lock:
        for priority in blocking_priorities:
            for s in inventory.get_serials_with_priority(priority):
                if not completed_serials.get(s, False):
                    blocker = f"priority {priority} switch {s}"
                    break
            if blocker:
                break

    if blocker is None:
        if arm_timeout:
            with state_lock:
                first_wait_seen.pop(serial, None)
        return True, ""

    # Read-only caller: report the state, never start or expire the clock.
    if not arm_timeout:
        with state_lock:
            started = first_wait_seen.get(serial)
        if started is None:
            return False, f"waiting for {blocker} to complete"
        left = max(0, PRIORITY_WAIT_TIMEOUT - int(now() - started))
        return False, f"waiting for {blocker} to complete ({left}s before timeout release)"

    # Still blocked — but a polling switch waits at most PRIORITY_WAIT_TIMEOUT.
    with state_lock:
        started = first_wait_seen.setdefault(serial, now())
    waited = int(now() - started)

    if waited >= PRIORITY_WAIT_TIMEOUT:
        logger.warning(
            f"[PRIORITY-TIMEOUT] {serial} waited {waited}s for {blocker} "
            f"(limit {PRIORITY_WAIT_TIMEOUT}s) — releasing manifest anyway"
        )
        return True, ""

    remaining = PRIORITY_WAIT_TIMEOUT - waited
    return False, f"waiting for {blocker} to complete ({remaining}s before timeout release)"


def wait_remaining(serial: str) -> int:
    """
    Seconds before this serial's manifest is released by PRIORITY_WAIT_TIMEOUT.

    Returned to a waiting switch so its bootstrap can keep its own polling
    deadline above the server's — a bootstrap that gave up first used to exit
    without ever reporting, leaving the queue behind it stuck.
    """
    with state_lock:
        started = first_wait_seen.get(clean_serial(serial))
    if started is None:
        return PRIORITY_WAIT_TIMEOUT
    return max(0, PRIORITY_WAIT_TIMEOUT - int(now() - started))


load_state()


# -----------------------------------------------------------
# File serving endpoints
# -----------------------------------------------------------

def bootstrap_path(name: str) -> str:
    """
    Locate a bootstrap script.

    docker-compose bind-mounts ./scripts as a directory so edits are picked up
    (see the note there); the Dockerfile also bakes copies into BASE_DIR for
    anyone running the image without that mount. Prefer the mount.
    """
    mounted = os.path.join(BASE_DIR, "scripts", name)
    if os.path.isfile(mounted):
        return mounted
    return os.path.join(BASE_DIR, name)


def _inject_server_config(script_path: str) -> str:
    """
    Read bootstrap script and replace __ZTP_SERVER_IP__ / __ZTP_SERVER_PORT__
    placeholders with the actual values from environment.
    This avoids hardcoding the server IP in scripts — change .env, rebuild,
    and the new IP is automatically sent to every switch.
    """
    with open(script_path, "r") as f:
        content = f.read()
    content = content.replace("__ZTP_SERVER_IP__",   SERVER_IP)
    content = content.replace("__ZTP_SERVER_PORT__", str(PORT))
    return content


@app.route("/bootstrap/arista", methods=["GET"])
@app.route("/bootstrap",         methods=["GET"])  # legacy compatibility
def serve_bootstrap_arista():
    """Serve the Arista EOS bootstrap script with server IP/port injected."""
    client_ip     = request.remote_addr
    arista_serial = request.headers.get("X-Arista-Serial", "")
    arista_sku    = request.headers.get("X-Arista-SKU", "")
    logger.info(f"[ARISTA] Bootstrap requested by {client_ip} serial={arista_serial} sku={arista_sku}")
    script = _inject_server_config(bootstrap_path("bootstrap_arista"))
    return script, 200, {"Content-Type": "text/plain"}


@app.route("/bootstrap/cisco",       methods=["GET"])
@app.route("/bootstrap/cisco.cfg",   methods=["GET"])
def serve_bootstrap_cisco():
    """
    Serve a Cisco AutoInstall-compatible IOS config stub for C9200L/C9200 switches.

    C9200L does NOT support Python/Guest Shell ZTP. It uses Cisco AutoInstall:
    - DHCP option 67 → URL of a plain IOS text config
    - Switch downloads the config and applies it as startup-config on boot

    Strategy:
    1. Serve a minimal stub IOS config (no mgmt IP — avoids conflicts)
    2. Embed an EEM applet that fires on first boot, discovers the switch's
       real serial via CLI, fetches its manifest from our ZTP API, downloads
       and applies the real config, handles firmware, then notifies ZTP complete.

    The EEM applet uses Tcl (natively available on all IOS XE versions) to
    perform HTTP requests and CLI configuration — no Guest Shell required.
    """
    client_ip = request.remote_addr
    logger.info(f"[CISCO] AutoInstall config requested by {client_ip}")

    # C9200L runs CAT9K_LITE_IOSXE with ~1.9 GB flash.
    # Guest Shell needs 1.1 GB free to activate — repeated failed ZTP attempts
    # leave guestshell.tar on flash, exhausting space → "Script execution failed".
    #
    # Solution: serve a plain IOS config stub (text/plain) via a URL WITHOUT .py extension.
    # AutoInstall applies it as startup-config on first boot.
    # An embedded EEM applet then handles the rest via native IOS XE Tcl (no Guest Shell needed).
    #
    # Key: DHCP Option 67 must point to /bootstrap/cisco (no .py) so the switch
    # treats the HTTP response as a config file, not a Python script execution trigger.
    config = _build_cisco_autoinstall_config(SERVER_IP, str(PORT))
    return config, 200, {"Content-Type": "text/plain"}


@app.route("/bootstrap/cisco/guestshell", methods=["GET"])
def serve_bootstrap_cisco_guestshell():
    """
    Serve the Guest Shell Python bootstrap (scripts/bootstrap_cisco).

    This is the ALTERNATIVE Cisco path, for platforms that can actually run
    Guest Shell (C9300/C9500 and similar with enough free flash). The default
    /bootstrap/cisco route above serves the AutoInstall + EEM/Tcl stub instead,
    because C9200L cannot activate Guest Shell reliably.

    To use this path, point DHCP option 67 at this URL in
    config/dnsmasq.conf.template instead of /bootstrap/cisco.cfg.
    """
    client_ip = request.remote_addr
    logger.info(f"[CISCO] Guest Shell bootstrap requested by {client_ip}")
    script = _inject_server_config(bootstrap_path("bootstrap_cisco"))
    return script, 200, {"Content-Type": "text/plain"}


def _build_cisco_autoinstall_config(server_ip: str, server_port: str) -> str:
    """
    Build a minimal IOS XE config stub that bootstraps ZTP via EEM + Tcl.

    The stub config is applied by AutoInstall as startup-config on first boot.
    It contains an EEM applet that fires 60 seconds after boot, reads the
    switch serial number, downloads a per-serial Tcl script, and executes it.

    Key design decisions for C9200L reliability:
    - Put 'version 17.12' at the top so AutoInstall successfully parses it.
    - Put 'hostname Switch' to satisfy AutoInstall defaults.
    - Use a countdown timer (90s) to give interfaces time to come up securely.
    - Use 'cli command "show version"' to get serial, because 'show inventory'
      output format varies by model. 'show version' always has 'Processor board ID'.
    - Extract the serial with 'regexp' and fall back to 'show inventory' when
      'show version' does not match, so both output formats are covered.
    - Download per-serial Tcl using 'copy http: flash:' with 'noprompt'.
    - All EEM actions are numbered with leading zeros to avoid ordering bugs.
    - 'authorization bypass' allows EEM to run before AAA is configured.
    """
    # Build the config without f-string issues by careful string construction.
    # The $serial EEM variable must NOT be f-string interpolated.
    lines = [
        "version 17.12",
        "service timestamps debug datetime msec",
        "service timestamps log datetime msec",
        "!",
        "hostname Switch",
        "!",
        "! ============================================================",
        "! Cisco AutoInstall Stub Config - Generated by ZTP Server",
        "! Applied automatically by IOS XE AutoInstall on first boot.",
        "! ============================================================",
        "!",
        "! Disable AutoInstall after this first successful apply.",
        "! The EEM applet below will handle the rest of ZTP.",
        "no service config",
        "!",
        "! EEM ZTP Bootstrap applet.",
        "! Fires 90s after boot — gives interfaces time to come up securely.",
        "! Reads serial from 'show version', downloads per-serial Tcl script,",
        "! then runs it. All heavy lifting is done inside the Tcl script.",
        "event manager applet ZTP-Bootstrap authorization bypass",
        " event timer countdown time 90",
        " action 001 syslog msg \"[ZTP] Bootstrap starting — reading serial\"",
        " action 002 cli command \"enable\"",
        " action 003 cli command \"show version\"",
        " action 004 regexp \"Processor board ID ([A-Z0-9]+)\" \"$_cli_result\" match serial",
        " action 005 if $_regexp_result ne 1",
        " action 006   syslog msg \"[ZTP] WARNING: show version serial failed — trying show inventory\"",
        " action 007   cli command \"show inventory\"",
        " action 008   regexp \"SN: ([A-Z0-9]+)\" \"$_cli_result\" match serial",
        " action 009 end",
        " action 010 if $_regexp_result eq 1",
        " action 011   syslog msg \"[ZTP] Serial detected — downloading Tcl script\"",
        # Download Tcl script — noprompt suppresses all confirmation prompts
        f" action 012   cli command \"copy http://{server_ip}:{server_port}/api/eem/" + r"$serial" + " flash:ztp.tcl noprompt\"",
        " action 013   syslog msg \"[ZTP] Running Tcl script...\"",
        " action 014   cli command \"tclsh flash:ztp.tcl\"",
        " action 015   syslog msg \"[ZTP] Tcl script completed\"",
        " action 016 else",
        " action 017   syslog msg \"[ZTP] ERROR: Could not detect serial number — aborting ZTP\"",
        " action 018 end",
        "!",
        "end",
        "",
    ]
    return "\n".join(lines)


@app.route("/api/eem/<serial>", methods=["GET"])
def serve_eem_script(serial):
    """
    Serve a per-serial Tcl ZTP script for Cisco IOS XE.

    Called by the EEM applet embedded in the AutoInstall stub config.
    The Tcl script runs in the IOS XE 'tclsh' environment (always available).

    Tcl in IOS XE can:
    - Execute CLI commands via 'ios_config' and 'typeahead'
    - Use the 'http' package to download files
    - Read/write flash filesystem

    The script:
    1. Polls /api/manifest/<serial> until the priority gate releases it
       (202 = wait, 200 = go) — the same gate the Arista bootstrap obeys
    2. Downloads its config to flash and copies it to startup-config,
       verifying each step
    3. Optionally downloads firmware, verifies it with 'verify /md5',
       and installs it in Install Mode
    4. Notifies the ZTP server (ztp_complete — 'success' or 'failed_*')
    5. Reloads the switch
    """
    serial = clean_serial(serial)
    client_ip = request.remote_addr
    logger.info(f"[CISCO-EEM] Tcl script requested for serial={serial} by {client_ip}")

    # Fetch manifest for this serial
    manifest   = inventory.get_manifest(serial)
    config_file   = manifest.get("config",   DEFAULT_CONFIG)
    firmware_file = manifest.get("firmware", "")
    description   = manifest.get("description", "Unknown")

    base_url = f"http://{SERVER_IP}:{PORT}"

    # Log the event
    record_event({
        "timestamp": ts(),
        "serial":    serial,
        "event":     "eem_script_served",
        "detail":    {"config": config_file, "firmware": firmware_file},
        "client_ip": client_ip,
    })
    with state_lock:
        progress_state[serial] = {
            "step": "starting", "pct": 5,
            "msg": f"EEM Tcl script fetched — applying config: {config_file}",
            "bytes_received": 0, "bytes_total": 0,
            "filename": config_file, "updated": ts(),
        }

    # Generate the per-serial Tcl script.
    # Tcl in IOS XE uses the 'ios_config' proc and runs CLI through 'exec'.
    # The checksum is resolved HERE (the server can read the filesystem) and
    # baked into the script, so the switch can run 'verify /md5' natively.
    fw_md5 = firmware_md5(firmware_file, manifest) if firmware_file else ""
    tcl = _build_cisco_tcl_script(
        serial, base_url, config_file, firmware_file, description, fw_md5=fw_md5
    )

    if firmware_file and not fw_md5:
        logger.warning(
            f"[CISCO-EEM] No checksum published for {firmware_file} — "
            f"{serial} will install an UNVERIFIED image. Create "
            f"firmware/{firmware_file}.md5 with: md5sum {firmware_file} > {firmware_file}.md5"
        )

    logger.info(
        f"[CISCO-EEM] Serving Tcl script for {serial}: config={config_file} "
        f"firmware={firmware_file} md5={'yes' if fw_md5 else 'MISSING'}"
    )
    return tcl, 200, {"Content-Type": "text/plain"}


def firmware_md5(firmware_file: str, manifest: dict) -> str:
    """
    Expected MD5 for a firmware image.

    Prefers the checksum published next to the image
    (firmware/<image>.md5 or .md5sum, as produced by 'md5sum <image> > ...'),
    and falls back to a 'firmware_md5:' field in inventory.yaml. Returns ""
    when no checksum is published anywhere — the caller decides whether that
    is a warning or a hard failure.
    """
    firmware_dir = os.path.join(BASE_DIR, "firmware")
    for suffix in (".md5", ".md5sum"):
        path = os.path.join(firmware_dir, f"{firmware_file}{suffix}")
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                first = f.read().strip().split()
            if first:
                return first[0].lower()
        except OSError as e:
            logger.warning(f"Could not read checksum {path}: {e}")
    return str(manifest.get("firmware_md5") or "").strip().lower()


def _tcl_quote(value) -> str:
    """
    Escape a value for use inside a Tcl double-quoted string.

    Everything interpolated into the generated script comes from inventory.yaml,
    where a description like 'Rack [A] $site' is a perfectly ordinary thing to
    write — and it used to produce a script that died on its very first command,
    BEFORE it could report ztp_started or reach the priority gate. The switch
    then never reported anything and everything queued behind it waited out the
    full priority timeout: exactly the failure this file exists to prevent.
    """
    text = str(value or "")
    text = text.replace("\\", "\\\\")
    for ch in ('"', "[", "]", "$"):
        text = text.replace(ch, "\\" + ch)
    return text.replace("\r", " ").replace("\n", " ")


def _tcl_comment(value) -> str:
    """Plain text for a '#' comment line — no escapes, no surprises."""
    text = re.sub(r"[^A-Za-z0-9 ._:/@+-]", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


# The Tcl body is a plain template, NOT an f-string: Tcl is made of braces and
# doubling every one of them for f-string escaping is how this got unreadable
# (and wrong) before. Placeholders are substituted explicitly below.
_CISCO_TCL_TEMPLATE = r"""#!/usr/bin/tclsh
# ============================================================
# Cisco IOS XE ZTP Tcl Script
# Serial: %%SERIAL%% - %%DESCRIPTION_COMMENT%%
# Generated by ZTP Server. Runs in IOS XE tclsh (no Guest Shell).
#
# Every CLI step is CHECKED. On failure the script reports
# ztp_complete with a 'failed_*' detail and stops, instead of
# reporting success and leaving a half-provisioned switch green
# on the dashboard.
# ============================================================
package require http

set SERIAL  "%%SERIAL%%"
set BASE    "%%BASE_URL%%"
set CFG     "%%CONFIG_FILE%%"
set FW      "%%FIRMWARE_FILE%%"
set FWMD5   "%%FIRMWARE_MD5%%"
set DESC    "%%DESCRIPTION%%"
set MAXWAIT %%MAX_WAIT%%
set POLL    %%POLL_INTERVAL%%

proc notify {event detail} {
    global BASE SERIAL
    set url "$BASE/api/notify_get?serial=$SERIAL&event=$event&detail=$detail"
    catch {
        set tok [::http::geturl $url -timeout 10000]
        ::http::cleanup $tok
    }
}

proc abort_ztp {reason} {
    puts "\[ZTP\] ABORT: $reason"
    # Report completion even though we failed: the switches queued behind this
    # one must not wait out the full priority timeout for a box that gave up.
    notify "ztp_complete" $reason
    exit 1
}

proc cli_run {cmd} {
    set out ""
    catch {exec $cmd} out
    return $out
}

proc cli_ok {out} {
    foreach bad {"%Error" "% Error" "Invalid input" "No such file" "Error opening" "Timed out" "Permission denied"} {
        if {[string match "*$bad*" $out]} { return 0 }
    }
    return 1
}

proc flash_has {name} {
    set out [cli_run "dir flash:$name"]
    if {![cli_ok $out]} { return 0 }
    return [string match "*$name*" $out]
}

# Priority gate. The AutoInstall/EEM path used to skip this entirely, so Cisco
# switches provisioned immediately no matter what priority they were given.
# /api/manifest returns 202 while lower-priority switches are still running and
# 200 once this switch is released (or the server's own timeout fires).
proc wait_for_release {} {
    global BASE SERIAL MAXWAIT POLL
    set waited 0
    while {$waited <= $MAXWAIT} {
        set code 0
        if {[catch {
            set tok [::http::geturl "$BASE/api/manifest/$SERIAL" -timeout 30000]
            set code [::http::ncode $tok]
            ::http::cleanup $tok
        } err]} {
            puts "\[ZTP\] manifest poll failed: $err"
        }
        if {$code == 200} {
            puts "\[ZTP\] Priority gate cleared after ${waited}s"
            return 1
        } elseif {$code == 202} {
            puts "\[ZTP\] Waiting for lower-priority switches (${waited}s elapsed)..."
        } else {
            puts "\[ZTP\] Unexpected manifest status '$code' - retrying"
        }
        after [expr {$POLL * 1000}]
        incr waited $POLL
    }
    return 0
}

puts "===================================================="
puts "\[ZTP\] ZTP starting for $SERIAL ($DESC)"
puts "===================================================="

notify "ztp_started" "tcl_script_running"

if {![wait_for_release]} {
    abort_ztp "failed_manifest"
}

# -------------------------------------------------------
# STEP 1/3: Apply configuration
# -------------------------------------------------------
puts "\[ZTP\] STEP 1/3: Applying config $CFG..."
catch {exec "delete /force flash:ztp_cfg.txt"}

typeahead "\n"
set dl [cli_run "copy $BASE/configs/$CFG flash:ztp_cfg.txt"]
puts "\[ZTP\] Config download: $dl"
if {![flash_has "ztp_cfg.txt"]} {
    abort_ztp "failed_config"
}

typeahead "\n"
set sc [cli_run "copy flash:ztp_cfg.txt startup-config"]
puts "\[ZTP\] startup-config: $sc"
if {![cli_ok $sc]} {
    abort_ztp "failed_config"
}

# A copy that 'succeeded' but wrote nothing is still a failed provision.
set verify_cfg [cli_run "show startup-config"]
if {[string length $verify_cfg] < 200} {
    puts "\[ZTP\] startup-config looks empty after copy"
    abort_ztp "failed_config"
}

catch {exec "delete /force flash:ztp_cfg.txt"}
notify "config_applied" "$CFG"
puts "\[ZTP\] STEP 1/3: OK"

if {$FW eq ""} {
    # -------------------------------------------------------
    # Config-only ZTP
    # -------------------------------------------------------
    puts "\[ZTP\] STEP 2/3: No firmware assigned - skipping upgrade"
    puts "\[ZTP\] STEP 3/3: Notifying ZTP server and reloading..."
    notify "ztp_complete" "success"
    typeahead "\n"
    cli_run "reload in 1 reason ZTP-complete"
    puts "\[ZTP\] Done."
    exit 0
}

# -------------------------------------------------------
# STEP 2/3: Firmware download + integrity check
# -------------------------------------------------------
puts "\[ZTP\] STEP 2/3: Firmware $FW"
if {[flash_has $FW]} {
    puts "\[ZTP\] Firmware already on flash - skipping download."
} else {
    # C9200L flash fills up after prior installs - free it first.
    puts "\[ZTP\] Removing inactive packages to free flash..."
    typeahead "y\n"
    puts "\[ZTP\] Remove inactive: [cli_run {install remove inactive}]"

    puts "\[ZTP\] Downloading firmware (may take 10-30 min)..."
    typeahead "\n"
    set fwdl [cli_run "copy $BASE/firmware/$FW flash:$FW"]
    puts "\[ZTP\] Firmware download: $fwdl"
    if {![flash_has $FW]} {
        abort_ztp "failed_firmware"
    }
}

if {$FWMD5 ne ""} {
    puts "\[ZTP\] Verifying MD5 ($FWMD5)..."
    set vr [cli_run "verify /md5 flash:$FW $FWMD5"]
    puts "\[ZTP\] $vr"
    if {![string match "*Verified*" $vr]} {
        puts "\[ZTP\] MD5 mismatch - deleting corrupt image"
        catch {exec "delete /force flash:$FW"}
        abort_ztp "failed_firmware"
    }
    puts "\[ZTP\] MD5 verified OK"
} else {
    puts "\[ZTP\] WARNING: no checksum published for $FW - INTEGRITY NOT VERIFIED"
    notify "firmware_warning" "no_checksum_for_$FW"
}
notify "firmware_downloaded" "$FW"

# -------------------------------------------------------
# STEP 3/3: Install (Install Mode - the switch reloads itself)
# -------------------------------------------------------
puts "\[ZTP\] Setting boot system to packages.conf..."
ios_config "no boot system"
ios_config "boot system flash:packages.conf"
typeahead "\n"
puts "\[ZTP\] write memory: [cli_run {write memory}]"

# Sent BEFORE the install: 'activate' reboots the switch and the session dies,
# so this is the last chance to unblock the queue behind us.
notify "ztp_complete" "reloading_for_firmware"

puts "\[ZTP\] Running: install add file flash:$FW activate commit"
typeahead "\n\n\n"
set inst [cli_run "install add file flash:$FW activate commit"]
puts "\[ZTP\] Install result: $inst"

if {[cli_ok $inst]} {
    notify "firmware_applied" "$FW"
    puts "\[ZTP\] STEP 3/3: OK - switch is rebooting"
} else {
    # Still reachable, so the install did not reboot us: it failed.
    notify "ztp_complete" "failed_install"
    puts "\[ZTP\] STEP 3/3: FAILED"
    exit 1
}
"""


def _build_cisco_tcl_script(
    serial: str,
    base_url: str,
    config_file: str,
    firmware_file: str,
    description: str,
    fw_md5: str = "",
    max_wait: int = 0,
) -> str:
    """
    Build a Tcl ZTP script for IOS XE tclsh.

    Unlike the first version of this script, it (a) waits on the priority gate
    before touching the switch and (b) verifies every step, so a failure is
    reported as a failure instead of ending with an unconditional 'success'.
    """
    # Outlast the server's own release timeout, so the script is still polling
    # when the gate opens rather than having given up minutes earlier.
    if not max_wait:
        max_wait = PRIORITY_WAIT_TIMEOUT + 600

    replacements = {
        # clean_serial() already restricted the serial, but every other value
        # comes straight from inventory.yaml and has to be escaped.
        "%%SERIAL%%":              _tcl_quote(serial),
        "%%BASE_URL%%":            _tcl_quote(base_url),
        "%%CONFIG_FILE%%":         _tcl_quote(config_file),
        "%%FIRMWARE_FILE%%":       _tcl_quote(firmware_file or ""),
        "%%FIRMWARE_MD5%%":        _tcl_quote(fw_md5 or ""),
        "%%DESCRIPTION%%":         _tcl_quote(description),
        "%%DESCRIPTION_COMMENT%%": _tcl_comment(description),
        "%%MAX_WAIT%%":            str(int(max_wait)),
        "%%POLL_INTERVAL%%":       str(int(PRIORITY_POLL_INTERVAL)),
    }
    # One pass, so a value that happens to contain a placeholder literal
    # (a description reading '%%MAX_WAIT%%') is not substituted again.
    return re.sub(
        r"%%[A-Z_]+%%",
        lambda m: replacements.get(m.group(0), m.group(0)),
        _CISCO_TCL_TEMPLATE,
    )


@app.route("/configs/<path:filename>", methods=["GET"])
def serve_config(filename):
    """Serve per-switch or generic configuration files."""
    from werkzeug.security import safe_join
    config_dir = os.path.join(BASE_DIR, "configs")
    filepath = safe_join(config_dir, filename)
    if filepath is None or not os.path.isfile(filepath):
        logger.warning(f"Config not found: {filename} (requested by {request.remote_addr})")
        abort(404)
    logger.info(f"Serving config: {filename} to {request.remote_addr}")
    return send_from_directory(config_dir, filename, mimetype="text/plain")


@app.route("/firmware/<path:filename>", methods=["GET"])
def serve_firmware(filename):
    """
    Serve firmware images with live byte-level progress tracking.
    Streams the file in chunks and updates progress_state so the
    /api/progress/<serial> endpoint can report download percentage.
    Serial is looked up by matching the requesting IP to a known switch.
    """
    from flask import Response, stream_with_context
    from werkzeug.security import safe_join

    firmware_dir = os.path.join(BASE_DIR, "firmware")
    # safe_join refuses anything that escapes firmware_dir ('..', absolute paths,
    # url-encoded traversal). Plain os.path.join would happily serve /etc/passwd.
    filepath = safe_join(firmware_dir, filename)
    if filepath is None or not os.path.isfile(filepath):
        logger.warning(f"Firmware not found: {filename} (requested by {request.remote_addr})")
        abort(404)

    total_bytes = os.path.getsize(filepath)
    client_ip   = request.remote_addr

    # Try to find which serial this IP belongs to (from recent events)
    serial = "UNKNOWN"
    with state_lock:
        for e in reversed(ztp_events):
            if e.get("client_ip") == client_ip and e.get("serial") and e["serial"] != "UNKNOWN":
                serial = e["serial"]
                break
    # Fall back to a per-IP key so two unidentified switches downloading at the
    # same time do not overwrite each other's progress.
    progress_key = serial if serial != "UNKNOWN" else f"UNKNOWN@{client_ip}"

    logger.info(f"Serving firmware: {filename} ({total_bytes/1024/1024:.1f}MB) to {client_ip} (serial={serial})")

    # Init progress
    with state_lock:
        progress_state[progress_key] = {
            "step":           "firmware_download",
            "pct":            0,
            "msg":            f"Downloading {filename}",
            "bytes_received": 0,
            "bytes_total":    total_bytes,
            "filename":       filename,
            "updated":        ts(),
        }

    CHUNK = 1024 * 1024  # 1MB chunks

    def generate():
        sent = 0
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                yield chunk
                sent += len(chunk)
                pct = int(sent * 100 / total_bytes) if total_bytes else 100
                with state_lock:
                    progress_state.setdefault(progress_key, {}).update({
                        "pct":            pct,
                        "bytes_received": sent,
                        "msg":            f"Downloading {filename} — {sent/1024/1024:.1f}/{total_bytes/1024/1024:.1f} MB ({pct}%)",
                        "updated":        ts(),
                    })
        # Mark download complete
        with state_lock:
            progress_state.setdefault(progress_key, {}).update({
                "step":    "firmware_install",
                "pct":     100,
                "msg":     "Download complete — waiting for install to begin",
                "updated": ts(),
            })
        logger.info(f"Firmware download complete for {serial}: {filename}")

    return Response(
        stream_with_context(generate()),
        mimetype="application/octet-stream",
        headers={"Content-Length": str(total_bytes)},
    )


# -----------------------------------------------------------
# API: Manifest — with priority gating
# -----------------------------------------------------------

@app.route("/api/manifest/<serial>", methods=["GET"])
def api_manifest(serial):
    """
    Return JSON manifest for a given switch serial.
    Called by the bootstrap script running on the switch.

    Priority gating — NON-BLOCKING design:
    - If this switch is clear to proceed → return 200 with manifest
    - If this switch must wait for lower-priority switches → return 202
      with {"status": "waiting", "reason": "..."}
    - The bootstrap script polls this endpoint every PRIORITY_POLL_INTERVAL
      seconds until it receives a 200. This keeps gunicorn workers free
      to handle other requests (firmware downloads, notifications) while
      switches are waiting.
    """
    serial = clean_serial(serial)
    manifest = inventory.get_manifest(serial)
    my_priority = manifest.get("priority", 99)

    logger.info(f"Manifest request: {serial} priority={my_priority} source={manifest['source']}")

    # --- Non-blocking priority gate (the only caller that arms the timeout) ---
    clear, reason = is_priority_clear(serial, arm_timeout=True)
    if not clear:
        logger.info(f"Manifest deferred for {serial} (priority {my_priority}): {reason}")
        return jsonify({
            "status":        "waiting",
            "serial":        serial,
            "priority":      my_priority,
            "reason":        reason,
            # The bootstrap uses these to keep its own deadline above ours.
            "wait_timeout":  PRIORITY_WAIT_TIMEOUT,
            "remaining":     wait_remaining(serial),
            "poll_interval": PRIORITY_POLL_INTERVAL,
        }), 202

    # Clear to proceed — build and return full manifest
    platform = manifest.get("platform", "eos")
    if platform == "cisco_ios":
        manifest["bootstrap_url"] = f"http://{SERVER_IP}:{PORT}/bootstrap/cisco"
        manifest["vendor"] = "cisco"
    else:
        manifest["bootstrap_url"] = f"http://{SERVER_IP}:{PORT}/bootstrap/arista"
        manifest["vendor"] = "arista"

    logger.info(f"Manifest released: {serial} → vendor={manifest['vendor']} config={manifest['config']} firmware={manifest['firmware']} priority={my_priority}")

    record_event({
        "timestamp": ts(),
        "serial":    serial,
        "event":     "manifest_served",
        "detail":    manifest,
        "client_ip": request.remote_addr,
    })

    return jsonify(manifest), 200


# -----------------------------------------------------------
# API: Notifications (from bootstrap on switch)
# -----------------------------------------------------------

@app.route("/api/notify", methods=["POST"])
def api_notify():
    """
    Receive ZTP status events from switches (JSON POST — Arista bootstrap).
    When event=ztp_complete is received, mark serial as done
    so higher-priority switches waiting in api_manifest are unblocked.
    """
    data = request.get_json(silent=True) or {}
    return _handle_notification(
        serial = data.get("serial", "UNKNOWN"),
        event  = data.get("event",  "unknown"),
        detail = data.get("detail", ""),
        source = "NOTIFY",
    )


@app.route("/api/notify_get", methods=["GET"])
def api_notify_get():
    """
    GET-based notification endpoint for Cisco IOS XE Tcl scripts.
    IOS XE tclsh's http package can only do GET requests, so this mirrors
    /api/notify but accepts parameters via query string. Both endpoints share
    the exact same state machine (_handle_notification) — they used to have
    two separate, subtly different copies of it.
    """
    resp, code = _handle_notification(
        serial = request.args.get("serial", "UNKNOWN"),
        event  = request.args.get("event",  "unknown"),
        detail = request.args.get("detail", ""),
        source = "NOTIFY/GET",
    )
    # Tcl's http::geturl is happiest with a plain body
    return "ok", code


# Details a switch sends with ztp_complete when it gave up rather than succeeded.
FAILURE_DETAILS = ("failed_config", "failed_firmware", "failed_install")


def _handle_notification(serial: str, event: str, detail, source: str):
    """
    Single state machine for switch notifications, shared by the POST and GET
    endpoints. Updates the event log, per-switch progress and the priority gate.
    """
    serial = clean_serial(serial)
    event  = str(event or "unknown")[:64]

    entry = {
        "timestamp": ts(),
        "serial":    serial,
        "event":     event,
        "detail":    detail,
        "client_ip": request.remote_addr,
    }
    record_event(entry)

    detail_str = str(detail)

    with state_lock:
        if event == "ztp_started":
            progress_state[serial] = {
                "step": "starting", "pct": 5,
                "msg": "ZTP started — fetching manifest",
                "bytes_received": 0, "bytes_total": 0,
                "filename": "", "updated": ts(),
            }

        elif event == "config_applied":
            progress_state[serial] = {
                "step": "config_done", "pct": 33,
                "msg": f"Config applied: {detail_str}",
                "bytes_received": 0, "bytes_total": 0,
                "filename": detail_str, "updated": ts(),
            }

        elif event == "config_fallback":
            progress_state.setdefault(serial, {}).update({
                "msg": f"WARNING: per-serial config missing, using {DEFAULT_CONFIG} "
                       f"(asked for {detail_str})",
                "updated": ts(),
            })

        elif event == "firmware_downloaded":
            progress_state.setdefault(serial, {}).update({
                "step": "firmware_install", "pct": 66,
                "msg": "Firmware downloaded — starting install",
                "filename": detail_str, "updated": ts(),
            })

        elif event == "firmware_applied":
            progress_state.setdefault(serial, {}).update({
                "step": "firmware_install_done", "pct": 95,
                "msg": f"Firmware installed: {detail_str}",
                "updated": ts(),
            })

        elif event == "firmware_warning":
            progress_state.setdefault(serial, {}).update({
                "msg": f"WARNING: {detail_str}",
                "updated": ts(),
            })

        elif event in ("firmware_failed", "config_failed"):
            progress_state.setdefault(serial, {}).update({
                "step": "failed",
                "msg": f"FAILED: {event} — {detail_str}",
                "updated": ts(),
            })

        elif event == "ztp_complete":
            # A switch also sends ztp_complete when it ABORTED, so the switches
            # queued behind it are not blocked forever. Unblock the gate either
            # way, but never report an aborted run as a success.
            failed = detail_str in FAILURE_DETAILS or detail_str.startswith("failed")
            completed_serials[serial] = True
            if failed:
                failed_serials[serial] = detail_str
                progress_state[serial] = {
                    "step": "failed", "pct": 100,
                    "msg": f"ZTP ABORTED: {detail_str}",
                    "bytes_received": 0, "bytes_total": 0,
                    "filename": "", "updated": ts(),
                }
            else:
                failed_serials.pop(serial, None)
                progress_state[serial] = {
                    "step": "done", "pct": 100,
                    "msg": f"ZTP complete: {detail_str}",
                    "bytes_received": 0, "bytes_total": 0,
                    "filename": "", "updated": ts(),
                }
            save_state()
            priority = inventory.get_priority(serial)
            verdict = "FAILED" if failed else "OK"
            logger.info(
                f"[COMPLETE/{source}] {serial} (priority {priority}) reported "
                f"ztp_complete [{verdict}: {detail_str}] — unblocking next priority"
            )

    log_fn = logger.info if event in (
        "ztp_complete", "config_applied", "firmware_applied",
        "firmware_downloaded", "ztp_started"
    ) else logger.warning
    log_fn(f"[{source}] {serial} → {event}: {detail_str}")

    return jsonify({"status": "ok"}), 200


# -----------------------------------------------------------
# API: Progress / priority / status
# -----------------------------------------------------------

def _switch_view(sw: dict) -> dict:
    """Common per-switch view used by the progress / priority / status APIs."""
    serial   = clean_serial(sw.get("serial", ""))
    platform = sw.get("platform", "eos")
    priority = int(sw.get("priority", 99))
    failure  = failed_serials.get(serial)
    return {
        "serial":      serial,
        "description": display_name(sw),
        "hostname":    sw.get("hostname", ""),
        "vendor":      "cisco" if platform == "cisco_ios" else "arista",
        "platform":    platform,
        "priority":    priority,
        "firmware":    sw.get("firmware", ""),
        "config":      f"{serial}.cfg",
        "completed":   bool(completed_serials.get(serial, False)) and not failure,
        "failed":      bool(failure),
        "failure":     failure,
    }


def _idle_progress(serial: str = "") -> dict:
    """
    Progress for a switch with no live entry.

    After a container restart the completion state is restored from disk but
    progress_state starts empty, so report what we do know instead of claiming
    a finished switch is still "waiting to boot".
    """
    failure = failed_serials.get(serial)
    if failure:
        return {
            "step": "failed", "pct": 100,
            "msg": f"ZTP ABORTED: {failure} (reported before restart)",
            "bytes_received": 0, "bytes_total": 0,
            "filename": "", "updated": None,
        }
    if completed_serials.get(serial):
        return {
            "step": "done", "pct": 100,
            "msg": "ZTP complete (reported before restart)",
            "bytes_received": 0, "bytes_total": 0,
            "filename": "", "updated": None,
        }
    return {
        "step": "not_started", "pct": 0,
        "msg": "Waiting for switch to boot",
        "bytes_received": 0, "bytes_total": 0,
        "filename": "", "updated": None,
    }


@app.route("/api/progress", methods=["GET"])
def api_progress_all():
    """
    Return live progress for all switches.

    A switch whose serial could not be identified yet is tracked under
    'UNKNOWN@<ip>' (see serve_firmware). Those rows used to be dropped here,
    so a switch pulling a 1.8 GB image showed up nowhere at all — they are
    now appended after the inventory rows.
    """
    result = []
    with state_lock:
        for sw in inventory.list_switches():
            view = _switch_view(sw)
            prog = progress_state.get(view["serial"]) or _idle_progress(view["serial"])
            result.append({**view, **prog})

        known = {row["serial"] for row in result}
        for key, prog in progress_state.items():
            if key in known:
                continue
            result.append({
                "serial":      key,
                "description": "Unregistered / not yet identified",
                "hostname":    "",
                "vendor":      "unknown",
                "platform":    "",
                "priority":    99,
                "firmware":    "",
                "config":      "",
                "completed":   False,
                "failed":      False,
                "failure":     None,
                **prog,
            })
    return jsonify(sorted(result, key=lambda x: (x["priority"], x["serial"])))


@app.route("/api/progress/<serial>", methods=["GET"])
def api_progress_serial(serial):
    """Return live progress for a specific switch."""
    serial = clean_serial(serial)
    with state_lock:
        prog = progress_state.get(serial) or _idle_progress(serial)
        failure = failed_serials.get(serial)
        return jsonify({
            "serial":    serial,
            "completed": bool(completed_serials.get(serial, False)) and not failure,
            "failed":    bool(failure),
            "failure":   failure,
            **prog,
        })


@app.route("/api/priority", methods=["GET"])
def api_priority_status():
    """Show provisioning priority status — which switches are done and which are waiting."""
    result = []
    for sw in inventory.list_switches():
        view = _switch_view(sw)
        clear, reason = is_priority_clear(view["serial"])
        view.update({"clear_to_go": clear, "waiting_for": reason or None})
        result.append(view)
    return jsonify(sorted(result, key=lambda x: (x["priority"], x["serial"])))


@app.route("/api/status", methods=["GET"])
def api_status():
    """
    Full provisioning status overview, grouped by priority.

    Shape matches what './start.sh priority' renders:
      {
        "total_switches": 14, "completed_count": 3, "failed_count": 0,
        "provisioning_order": [
          {"priority": 1, "label": "Priority 1 (2 switches)",
           "all_complete": true, "switches": [ ... ]}
        ]
      }
    """
    groups: dict = {}
    total = completed = failed = 0

    for sw in inventory.list_switches():
        view = _switch_view(sw)
        clear, reason = is_priority_clear(view["serial"])
        view.update({"clear_to_go": clear, "waiting_for": reason or None})

        total += 1
        if view["completed"]:
            completed += 1
        if view["failed"]:
            failed += 1

        groups.setdefault(view["priority"], []).append(view)

    order = []
    for p in sorted(groups):
        switches = sorted(groups[p], key=lambda x: x["serial"])
        n = len(switches)
        order.append({
            "priority":     p,
            "label":        f"Priority {p} ({n} switch{'es' if n != 1 else ''})",
            "all_complete": all(s["completed"] or s["failed"] for s in switches),
            "switches":     switches,
        })

    return jsonify({
        "total_switches":     total,
        "completed_count":    completed,
        "failed_count":       failed,
        "provisioning_order": order,
    })


# -----------------------------------------------------------
# API: Inventory management (used by ztp_cli.py and start.sh)
# -----------------------------------------------------------

@app.route("/api/switches", methods=["GET"])
def api_switches():
    """Returns the parsed inventory (list of all configured switches)."""
    switches = []
    for sw in inventory.list_switches():
        entry = dict(sw)
        entry["description"] = display_name(sw)
        switches.append(entry)
    return jsonify(switches)


@app.route("/api/switches/<serial>", methods=["GET"])
def api_switch_get(serial):
    """Return the manifest that would be served to this serial."""
    return jsonify(inventory.get_manifest(clean_serial(serial))), 200


@app.route("/api/switches", methods=["POST"])
def api_switch_add():
    """Add or update a switch in the inventory and persist it to YAML."""
    data = request.get_json(silent=True) or {}
    serial = clean_serial(data.get("serial", ""))
    if serial == "UNKNOWN":
        return jsonify({"status": "error", "error": "valid 'serial' is required"}), 400
    if not data.get("firmware"):
        return jsonify({"status": "error", "error": "'firmware' is required"}), 400

    try:
        priority = int(data.get("priority", 99))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "'priority' must be an integer"}), 400

    try:
        entry = inventory.add_switch(
            serial      = serial,
            firmware    = data["firmware"],
            description = data.get("description", ""),
            hostname    = data.get("hostname", ""),
            platform    = data.get("platform", "eos"),
            tags        = data.get("tags", []),
            priority    = priority,
        )
    except InventoryPersistError as e:
        # Answering 'ok' for a change that never reached disk is how an edit
        # silently disappears on the next restart.
        logger.error(f"[API] Could not persist switch {serial}: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

    logger.info(f"[API] Switch added/updated via API: {serial}")
    return jsonify({"status": "ok", "switch": entry}), 200


@app.route("/api/switches/<serial>", methods=["DELETE"])
def api_switch_remove(serial):
    """Remove a switch from the inventory and persist the change."""
    serial = clean_serial(serial)
    try:
        removed = inventory.remove_switch(serial)
    except InventoryPersistError as e:
        logger.error(f"[API] Could not persist removal of {serial}: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

    if removed:
        logger.info(f"[API] Switch removed via API: {serial}")
        return jsonify({"status": "ok", "removed": serial}), 200
    return jsonify({"status": "error", "error": f"{serial} not in inventory"}), 404


@app.route("/api/inventory/reload", methods=["POST"])
def api_inventory_reload():
    """
    Hot-reload inventory.yaml without restarting the container.
    This is what './start.sh reload' and 'ztp_cli.py reload' call.
    """
    inventory.reload()
    switches = inventory.list_switches()
    logger.info(f"[API] Inventory reloaded on request: {len(switches)} switches")
    return jsonify({
        "status":   "ok",
        "switches": len(switches),
        "path":     INVENTORY_PATH,
        "reloaded": ts(),
    }), 200


@app.route("/api/state/reset", methods=["POST"])
def api_state_reset():
    """
    Forget ZTP completion state so switches can be provisioned again.

    Completion is persisted to logs/ztp_state.json so a container restart does
    not re-run a finished rollout — but that also means a switch that is being
    re-deployed (RMA, lab re-run) stays 'done' forever and its priority gate
    stays pre-cleared. Operators had to delete the state file by hand and know
    that it existed; './start.sh reset [SERIAL]' calls this instead.

    Body: {"serial": "ABC123"} to clear one switch, {} to clear all of them.
    """
    data = request.get_json(silent=True) or {}
    raw = data.get("serial")

    with state_lock:
        if raw:
            serial = clean_serial(raw)
            if serial == "UNKNOWN":
                return jsonify({"status": "error", "error": f"invalid serial {raw!r}"}), 400
            existed = serial in completed_serials or serial in progress_state
            completed_serials.pop(serial, None)
            failed_serials.pop(serial, None)
            progress_state.pop(serial, None)
            first_wait_seen.pop(serial, None)
            cleared = [serial] if existed else []
        else:
            cleared = sorted(set(completed_serials) | set(progress_state))
            completed_serials.clear()
            failed_serials.clear()
            progress_state.clear()
            first_wait_seen.clear()
        save_state()

    logger.warning(f"[API] ZTP state reset: {cleared or 'nothing to clear'}")
    return jsonify({"status": "ok", "cleared": cleared}), 200


# -----------------------------------------------------------
# API: File listings
# -----------------------------------------------------------

def _list_dir(subdir: str, suffixes: tuple) -> list:
    path = os.path.join(BASE_DIR, subdir)
    try:
        return sorted(
            f for f in os.listdir(path)
            if f.endswith(suffixes) and os.path.isfile(os.path.join(path, f))
        )
    except FileNotFoundError:
        logger.warning(f"Directory not found: {path}")
        return []


@app.route("/api/configs", methods=["GET"])
def api_configs():
    """List the config files available to be served to switches."""
    return jsonify(_list_dir("configs", (".cfg", ".txt")))


@app.route("/api/firmware", methods=["GET"])
def api_firmware():
    """List the firmware images available to be served to switches."""
    return jsonify(_list_dir("firmware", (".swi", ".bin")))


# -----------------------------------------------------------
# API: Events and health
# -----------------------------------------------------------

@app.route("/api/events", methods=["GET"])
def api_events():
    """Return the recent ZTP event log (newest last)."""
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit, MAX_EVENTS))
    with state_lock:
        return jsonify(list(ztp_events)[-limit:])


@app.route("/api/events/<serial>", methods=["GET"])
def api_events_serial(serial):
    """Return the recent ZTP events for one switch."""
    serial = clean_serial(serial)
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit, MAX_EVENTS))
    with state_lock:
        matching = [e for e in ztp_events if e.get("serial") == serial]
    return jsonify(matching[-limit:])


@app.route("/health", methods=["GET"])
def health():
    """
    Health and summary endpoint. The extra fields are what './start.sh status'
    renders — it used to ask for them while this only returned {status, ...}.
    """
    with state_lock:
        done   = sorted(s for s in completed_serials if s not in failed_serials)
        failed = sorted(failed_serials)
        events = len(ztp_events)
    return jsonify({
        "status":           "ok",
        "inventory_loaded": bool(inventory.list_switches()),
        "switches":         len(inventory.list_switches()),
        "events_recorded":  events,
        "completed":        done,
        "failed":           failed,
        "priority_timeout": PRIORITY_WAIT_TIMEOUT,
        "time":             ts(),
    })


if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
