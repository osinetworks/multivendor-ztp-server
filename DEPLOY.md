# Deployment Guide

Detailed setup and operation of the Multi-Vendor ZTP server. For the short
version, see [README.md](README.md).

## Prerequisites

- **Docker & Docker Compose** — the server runs entirely as a container.
- **Physical connectivity** — an interface on this host connected to the
  switch-facing (management) network.
- **Docker daemon access** — either membership of the `docker` group or sudo.
  `./start.sh` detects which it needs and falls back to `sudo docker` on its own.
- **Nothing else.** Python, Flask and dnsmasq live inside the image. The host
  only needs a virtualenv for the optional helper scripts (see *Host-side
  tooling* in the README).

## Installation

1. **Clone / extract the project** and `cd` into the directory holding
   `start.sh` and `docker-compose.yaml`.

2. **Copy the three templates.** All three are gitignored because they hold
   real serials, addresses and password hashes:

   ```bash
   cp .env.example                   .env
   cp config/inventory.yaml.template config/inventory.yaml
   cp config/config.yaml.template    config/config.yaml
   chmod +x start.sh
   ```

## Configuration

### 1. `.env` — everything network-specific

`.env` is the single source of truth. `docker-compose.yaml` passes it to the
container, and `entrypoint.sh` renders `config/dnsmasq.conf.template` from it at
startup. **Do not edit a generated `dnsmasq.conf`** — there is no checked-in
copy; it is written to `/etc/dnsmasq.conf` inside the container on every boot.

| Key | Meaning |
|---|---|
| `SERVER_IP` | This host's address on the switch-facing interface. Handed out as the default gateway and baked into every bootstrap URL. |
| `HTTP_PORT` | Port the ZTP API and file server listen on (default 8080). |
| `INTERFACE` | Switch-facing NIC for dnsmasq. Blank = auto-detect the first non-loopback UP interface. |
| `DHCP_RANGE_START` / `DHCP_RANGE_END` / `DHCP_SUBNET` / `DHCP_LEASE` | The pool handed to booting switches. |
| `DNS_SERVER` | DHCP option 6. |
| `DEFAULT_CONFIG` | Served when a switch's own `<serial>.cfg` is missing (default `generic.cfg`). |
| `PRIORITY_WAIT_TIMEOUT` | Seconds a switch waits for lower priorities before being released anyway (default 1800). |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

Every key has a fallback in `docker-compose.yaml`, so a missing or partial
`.env` starts the server with defaults rather than failing — but those defaults
are almost certainly not your network. `./start.sh start` warns when `.env` is
absent.

### 2. `config/inventory.yaml` — what each switch gets

Field-by-field documentation lives in `config/inventory.yaml.template`. The
essentials:

- `serial` — the lookup key. Case-insensitive.
- `hostname` — used by the config generator and by `backup_switches.py`.
- `platform` — `eos`, `eos64` or `cisco_ios`. Also decides which template
  family the generator may use; a template of the wrong family is always
  refused, however it was selected.
- `firmware` — image filename in `firmware/`.
- `priority` — provisioning order.
- `mgmt_oob_ip_address` / `mgmt_svi_ip_address` — management addressing, in CIDR form.

> [!IMPORTANT]
> **Provisioning priorities**
> - Lower numbers provision first (`1` before `2`).
> - Switches sharing a priority provision in parallel.
> - A switch at priority *N* waits until every switch below *N* has reported
>   `ztp_complete` — success **or** failure.
> - Nothing waits forever: after `PRIORITY_WAIT_TIMEOUT` the manifest is
>   released anyway and the event is logged as `[PRIORITY-TIMEOUT]`.

The config is always served as `<serial>.cfg`; there is no per-switch `config:`
key, and adding one has no effect.

### 3. `config/config.yaml` — template variables

Read by `generate_generic_config.py` on the **host**, not by the server. It
holds the site variables (VLANs, gateways, NTP, MLAG addressing) and the
password hashes that end up in the generated configs. Per-customer variants
(`config/config_<customer>.yaml`) are described in the README.

### 4. Filesystem layout

| Directory | Contents |
|---|---|
| `config/` | inventory, template variables, dnsmasq template — bind-mounted into the container as a directory |
| `configs/` | generated `<serial>.cfg` files served to switches |
| `firmware/` | `.swi` (Arista) and `.bin` (Cisco) images, each with a `.md5` next to it |
| `scripts/` | server, inventory manager, bootstrap scripts, host helpers |
| `templates/` | Jinja2 templates used by the config generator |
| `logs/` | server log, per-device logs, `ztp_state.json` |

## Generating configs

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
./generate_generic_config.py
```

One `.cfg` per inventory switch plus `generic.cfg` lands in `configs/`. The
script exits non-zero if anything failed to render, so it is safe to chain.

It refuses to render a switch whose credential variables are missing or still
hold a `REPLACE_ME__…` placeholder, naming both the switch and the variable.
A `cisco_ios` switch needs `cisco_admin_password_hash` (and
`cisco_custom_user_password_hash` when `custom_username` is set) in IOS format:
the `$6$` hashes used for Arista are not rejected by IOS, they are treated as a
cleartext password.

## Publishing firmware checksums

The bootstrap verifies every image it downloads, on both vendors — Arista with
`md5sum` in the bootstrap script, Cisco with the switch's native `verify /md5`.
Without a checksum the image is installed **unverified** and only a warning is
logged.

```bash
cd firmware
md5sum EOS64-4.35.5M.swi > EOS64-4.35.5M.swi.md5
cd ..
./check_firmware_hashes.sh     # exits non-zero if anything is missing or corrupt
```

## Running the server

```bash
./start.sh start      # build if needed, then start
./start.sh status     # container + API health, completed / failed switches
./start.sh watch      # live dashboard, refreshes every 5s
./start.sh logs       # follow container stdout
./start.sh stop
./start.sh restart    # full --no-cache rebuild
```

### Hot-reloading the inventory

`config/` is bind-mounted as a directory, so edits on the host are visible to
the container immediately — including edits from editors that save by replacing
the file:

```bash
./start.sh reload
```

> [!NOTE]
> `ztp_cli.py add` / `remove` (and `POST /api/switches`) rewrite
> `config/inventory.yaml` through the YAML serialiser, which **cannot preserve
> comments**. Every rewrite logs a warning, and the version from before the
> first one is kept as `config/inventory.yaml.bak`. That backup is written
> **once** — a later API write must not overwrite it with an already-stripped
> copy — so if a `.bak` is already there (including one left by an earlier
> round) no new backup is taken and the skip is logged. Edit the file by hand
> if your comments matter.

### Re-provisioning a switch

Completion state lives in `logs/ztp_state.json` and survives container
restarts, which is what stops a restart from re-running a finished rollout. It
also means a finished switch stays finished:

```bash
./start.sh reset HBG25500VQC   # one switch
./start.sh reset               # everything (asks first)
```

## How a switch boots

**Arista (EOS)**
1. Boots with no startup-config, DHCPs with vendor class `Arista Networks`.
2. dnsmasq answers with option 67 → `/bootstrap/arista`.
3. The bootstrap script polls `/api/manifest/<serial>` until the priority gate
   opens (202 → wait, 200 → go).
4. Writes `startup-config`, verifies it, then downloads and installs firmware —
   unless it is already booted from the image the inventory asks for.
5. Reports `ztp_complete`, retrying until the server confirms, then reboots.

**Cisco (IOS XE / Catalyst 9000)**
1. Boots with no startup-config, DHCPs with vendor class `ciscopnp`.
2. dnsmasq answers with option 67 → `/bootstrap/cisco.cfg`, a small AutoInstall
   stub config containing an EEM applet.
3. The applet reads the serial and downloads `/api/eem/<serial>`, a per-switch
   Tcl script.
4. The Tcl script polls the same `/api/manifest/<serial>` gate, applies the
   config, verifies the firmware with `verify /md5`, installs it and reports in.
   Every step is checked: a failure is reported as `failed_config`,
   `failed_firmware` or `failed_install`, never as success.

C9200L cannot run Guest Shell reliably, which is why AutoInstall + EEM is the
default Cisco path. Platforms that can (C9300/C9500) may instead point option 67
at `/bootstrap/cisco/guestshell` in `config/dnsmasq.conf.template`.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| No DHCP offer | Another DHCP server on the segment; wrong `INTERFACE`; `network_mode: host` missing from `docker-compose.yaml`. |
| Switch never fetches a config | `./start.sh events` — was the serial recognised and the manifest served? |
| Switch stuck waiting | `./start.sh priority` shows what it is waiting for and how long until the timeout release. |
| Server unreachable from the switch | `SERVER_IP` in `.env` must be this host's address on the switch-facing interface. |
| Config generator fails | Missing `config/config.yaml`, or a site tag that matches no block in the variable file. The message names the switch. |
| A finished switch will not re-provision | `./start.sh reset <SERIAL>`. |
| `ztp_cli.py add/remove` returns HTTP 500 | The inventory could not be written — check ownership of `config/` and the server log. The change is rejected rather than kept in memory only. |

### Logs

- Container stdout: `./start.sh logs`
- ZTP events: `./start.sh events`
- Per-device history: `logs/<SERIAL>.log`
- Server log: `logs/ztp_server.log`
